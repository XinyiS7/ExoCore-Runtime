# Subscription Runtime Canonical-Authoritative Rebase Construction Plan (Frozen)

## 1. Executive Summary & Core Architectural Thesis

### 1.1 Root Problem: Inverted Sovereignty & Brittle Dispositions
In the prior Subscription Runtime implementation, ExoCore’s relationship with the external AGY provider inverted the system's sovereignty hierarchy:
- The local AGY conversation session (`RuntimeBinding`, `--conversation <uuid>`) was treated as an immutable sovereign entity.
- If canonical history changed (e.g. user fixed a typo in past messages, pruned messages, or an external AI arrival occurred), `_verify_coverage_guard_locked()` raised `RuntimeCoveragePrefixMismatch`, irreversibly locking the binding into `recovery_required`.
- If `system_instructions` changed (e.g. persona prompt updated), `preflight_runtime_identity()` raised `RuntimePreflightError("runtime_system_instructions_conflict")` directly without modifying DB status.
- If the user switched to Direct API for several turns and returned to AGY, the system attempted to stitch synthetic `PriorDeltaTurn` text into the user prompt or failed with `runtime_continuity_order_unsupported`.

### 1.2 The Sovereign Resolution
ExoCore's database (`Conversation` and `Message`) is the **sole Canonical Truth**.
The provider-side AGY session is merely a **disposable, lazily reconstructed projection cache**.

When provider continuity cannot cleanly represent the canonical truth, ExoCore does not fail the user. Instead:
> **Stale Provider Projection Staged & Retired** → **Fresh Generation Bootstrapped from Current Canonical Truth** → **Turn Executes Seamlessly**.

---

## 2. Hard Invariant Boundaries & Classification

### 2.1 The Two Fundamental Continuity Domains
We strictly distinguish between **Process Continuity** and **History Projection Continuity**:

| Domain | Scenario | Disposition | Rationale |
|---|---|---|---|
| **Process Continuity** | Process killed, Gateway restarted, or model parameter changed without canonical message gap. | `REUSE` | History projection remains 100% valid; keep existing session via exact reacquire (`--conversation <uuid>`). |
| **History Projection Divergence** | History edited, pruned, or deleted in ExoCore (`prefix_digest != stored_digest`). | `REBASE_CANONICAL_HISTORY_CHANGED` | AGY internal state differs from database truth. Reincarnate from canonical history. |
| **Outside-Provider Progression** | Conversation progressed via Direct API, DeepSeek, or background assistant arrivals while AGY was inactive. | `REBASE_CANONICAL_GAP` | AGY has a canonical gap. Bootstrap fresh from current canonical history. |
| **Instruction / Context Drift** | Core `system_instructions` updated. | `REBASE_SYSTEM_INSTRUCTIONS_CHANGED` | AGY generation instructions are immutable per generation; rebase cleanly. |
| **Dynamic Injections (Non-Trigger)** | `dynamic_injections` changed (memory plasmids, vibe metrics, search back context, register short injections). | `REUSE` (No Rebase!) | Dynamic injections go into `ephemeral_current` per turn. They are expected to vary turn-by-turn and do NOT invalidate generation bootstrap instructions. |
| **Attachment-Bearing Gap** | Outside gap contains any Message with non-empty `attachment_ids`. | `BLOCK_UNSUPPORTED_CANONICAL_CONTENT` | Current `GenerationBootstrap.historical_flow` lacks attachment semantics. We must NEVER silently drop attachments while claiming canonical completeness. |
| **Transport Uncertainty** | POST sent, connection dropped, in-flight status unknown (`STATUS_INDETERMINATE`). | `BLOCK_PROVIDER_UNCERTAIN` | Real side-effect ambiguity. Must NEVER auto-rebase over unknown operations; preserve strict `recovery_required`. |

---

## 3. The Rebase Seam & Strict Pre-User Canonical Cut

### 3.1 Seam Placement: Pre-User-Persistence
Auto-rebase MUST NOT wait until `prepare_turn()`. In `prepare_turn()`, the user message has already been saved to the database. If retire/rebase fails, an orphaned user message remains; furthermore, excluding the current user from the bootstrap requires fragile message subtraction.

**The Canonical Seam:**
In `agents/services.py::BaseChatService.process_chat`:
- Right after `_build_context_generator()` completes (Phase A) and before `_locked_user_message_create()` (Phase B):
  1. `ctx.system_prompt` is fully built and frozen.
  2. Canonical message history in DB represents the complete, stable history **strictly prior** to the incoming turn.
  3. The current user message is **not yet in the database**.

At this seam, we evaluate the `ContinuityDisposition` and execute the rebase if required.
- **Strict Canonical Cut Guaranteed by Construction**: Because `user_msg` does not yet exist in the DB, any fresh `GenerationBootstrap` generated from DB history captures exactly all prior turns. The current user input will only exist in the active `TurnRequest`. Double-counting is structurally impossible.

---

## 4. Crash-Safe Rebase Lifecycle & State Machine

### 4.1 Dedicated Bounded Rebase Fields (Zero CheckConstraint Pollution)
Existing `indeterminate_operation` choices (`ensure_generation`, `retire`) and its CheckConstraint (`status == indeterminate` $\iff$ `indeterminate_operation != ""`) remain completely untouched.
Instead, `RuntimeBinding` gains dedicated, bounded rebase lifecycle fields:
```python
REBASE_STATE_IDLE = ""
REBASE_STATE_RETIRE_PENDING = "retire_pending"
REBASE_STATE_REPLACEMENT_PENDING = "replacement_pending"
REBASE_STATE_CHOICES = [
    (REBASE_STATE_IDLE, "Idle"),
    (REBASE_STATE_RETIRE_PENDING, "Retire pending"),
    (REBASE_STATE_REPLACEMENT_PENDING, "Replacement pending"),
]

rebase_state = models.CharField(max_length=32, choices=REBASE_STATE_CHOICES, blank=True, default="")
rebase_reason = models.CharField(max_length=100, blank=True, default="")
```

### 4.2 Canonical-Authoritative Recompute (Option B)
TX1 freezes **rebase intent and old-generation identity**, not the future bootstrap body. The replacement bootstrap is recomputed from the latest canonical truth when TX2 (or crash recovery) executes.

```text
[Phase 1: TX1 - Classify & Stage Intent]
  Lock Conversation + current RuntimeBinding (SELECT FOR UPDATE)
  Prove no unresolved turn (no PREPARED / SENT / INDETERMINATE)
  Evaluate ContinuityDisposition
  If REBASE_*:
    old_binding.rebase_state = "retire_pending"
    old_binding.rebase_reason = disposition.reason
    old_binding.save(update_fields=["rebase_state", "rebase_reason", "updated_at"])
COMMIT TX1

[Network Window: Non-Atomic Provider Cleanup]
  try:
    client.retire(old_public_binding_id, reason=disposition.reason)
  except RuntimeClientError (network failure / transport drop):
    # Strict fail-closed boundary for genuine transport uncertainty
    with transaction.atomic():
      old_binding.rebase_state = ""
      old_binding.status = STATUS_INDETERMINATE
      old_binding.indeterminate_operation = OPERATION_RETIRE
      old_binding.save()
    raise

[Phase 2: TX2 - Recompute Bootstrap from Truth & Commit Advance]
  Lock Conversation + old RuntimeBinding
  Re-verify that no concurrent message was inserted during the network window.
  If drift detected: abort stale projection, recompute pre-user context from current truth.
  
  Mark old binding:
    status = STATUS_RETIRED
    retired_reason = old_binding.rebase_reason
    retired_at = timezone.now()
    rebase_state = ""
    rebase_reason = ""
    indeterminate_operation = ""
  
  Recompute fresh GenerationBootstrap from latest canonical DB history.
  Create replacement RuntimeBinding:
    generation = old.generation + 1
    status = STATUS_STARTING
    bootstrap_snapshot = fresh_bootstrap_snapshot
    bootstrap_fingerprint = canonical_json_sha256(fresh_bootstrap_snapshot)
    bootstrap_coverage_anchor_message_pk = latest_canonical_message.pk
    bootstrap_coverage_digest = canonical_message_prefix_digest(...)
COMMIT TX2
```

### 4.3 Idempotent Crash Recovery
If Gateway successfully retired the old generation, but Django crashed or restarted before TX2 committed:
On the next request:
1. `preflight_runtime_identity()` detects: "The latest binding has `rebase_state in {'retire_pending', 'replacement_pending'}`."
2. Because the remote retire succeeded (or is cleanly idempotent via `client.retire`), preflight executes TX2: recomputing the fresh bootstrap from the *then-current* canonical truth and creating generation $N+1$.
3. Zero manual recovery commands needed.

### 4.4 Conservative STARTING Cleanup Policy
In the absence of a dedicated gateway registration bit, any `STARTING` generation that cannot be proven un-staged local-only must go through remote `client.retire()`. Gateway's `/v2/generations/{binding_id}/retire` handles un-staged or already-retired IDs safely.

---

## 5. Implementation Scope & File Breakdown

### 5.1 `ExoCore/bridge/models.py` & Migration
1. Add `rebase_state` and `rebase_reason` to `RuntimeBinding`.
2. Add migration `bridge/migrations/0004_runtime_binding_rebase_state.py`.

### 5.2 `ExoCore/bridge/subscription_runtime/contracts.py`
1. Define the typed enum:
   ```python
   class ContinuityDispositionType(str, Enum):
       REUSE = "reuse"
       REBASE_CANONICAL_HISTORY_CHANGED = "rebase_canonical_history_changed"
       REBASE_CANONICAL_GAP = "rebase_canonical_gap"
       REBASE_SYSTEM_INSTRUCTIONS_CHANGED = "rebase_system_instructions_changed"
       BLOCK_PROVIDER_UNCERTAIN = "block_provider_uncertain"
       BLOCK_UNSUPPORTED_CANONICAL_CONTENT = "block_unsupported_canonical_content"
   ```

### 5.3 `ExoCore/bridge/subscription_runtime/bindings.py`
1. **`evaluate_continuity_disposition(...)`**:
   - Inspects active binding against canonical DB history and current `system_instructions`:
     - Unresolved or indeterminate turns -> `BLOCK_PROVIDER_UNCERTAIN`.
     - Outside messages between last anchor and DB tip:
       - Any message has non-empty `attachment_ids` -> `BLOCK_UNSUPPORTED_CANONICAL_CONTENT`.
       - Else -> `REBASE_CANONICAL_GAP`.
     - `prefix_digest != stored_digest` -> `REBASE_CANONICAL_HISTORY_CHANGED`.
     - `stored_system != current_system` -> `REBASE_SYSTEM_INSTRUCTIONS_CHANGED`.
     - Else -> `REUSE`.
2. **`execute_canonical_rebase(...)`**:
   - Implements the crash-safe two-phase protocol (TX1 -> `client.retire` -> TX2).
   - Recomputes bootstrap from current canonical truth upon TX2.
3. **`recover_staged_rebase_if_needed(...)`**:
   - Idempotently completes dangling `rebase_state` upon preflight.

### 5.4 `ExoCore/agents/services.py` & `agents/runtime_turn.py`
1. In `BaseChatService.process_chat`:
   - Invoke continuity evaluation at the pre-user seam (between Phase A and Phase B).
   - If `BLOCK_*`, emit appropriate typed SSE error.
   - If `REBASE_*`, execute rebase, establish generation $N+1$, and continue seamlessly into user message persistence and turn execution.

---

## 6. Comprehensive Acceptance Matrix (11 Test Cases)

| # | Test Case | Scenario | Expected Behavior |
|---|---|---|---|
| 1 | **Historical Message Edit** | User edits an assistant message typo in DB. | `REBASE_CANONICAL_HISTORY_CHANGED`. Generation advances to $N+1$; AGY replies with corrected history; zero user errors. |
| 2 | **Endpoint Hopping (Text Gap)** | Turn 1 & 2 on AGY. Turn 3 on Direct API (text only). User switches back to AGY. | `REBASE_CANONICAL_GAP`. Generation advances to $N+1$; AGY receives full history including Direct turn; flawless context. |
| 3 | **Multiple Consecutive Assistant Messages** | Turn 1 on AGY. Background AI arrival inserts an assistant message. User sends Turn 2. | `REBASE_CANONICAL_GAP`. Rebase incorporates arrival cleanly; Turn 2 succeeds. |
| 4 | **Core System Instructions Update** | Agent system prompt is updated in preset/context. | `REBASE_SYSTEM_INSTRUCTIONS_CHANGED`. Rebase spins up fresh generation with new system instructions; no hard crash. |
| 5 | **Dynamic Injections Change Only** | Memory plasmid or vibe context changes between turns; core prompt and history unchanged. | `REUSE`. Must NOT rebase; existing session reused via fast-path. |
| 6 | **AGY In-Provider Model Switch** | User changes model `3.1` → `3.8` → `3.1` on AGY without canonical gap. | `REUSE`. Same binding generation maintained; process updated/reacquired without generation advance. |
| 7 | **Attachment-Bearing Gap Protection** | Direct API turn contains an image/file attachment. User switches back to AGY. | `BLOCK_UNSUPPORTED_CANONICAL_CONTENT`. Typed refusal (`runtime_continuity_attachments_unsupported`); does NOT silently drop attachment context. |
| 8 | **True Transport Uncertainty** | Turn marked `STATUS_INDETERMINATE` due to mid-flight network loss. | `BLOCK_PROVIDER_UNCERTAIN`. Rebase strictly refused; fail-closed `recovery_required` preserved. |
| 9 | **Retire Transport Indeterminate** | Network fails during `client.retire()`. | Binding marked uncertain (`status=indeterminate, indeterminate_operation=retire`); replacement $N+1$ is NOT created. |
| 10 | **Crash between Retire & Replacement** | Remote retire succeeds, but crash simulated before TX2 commits $N+1$. | Next call idempotently detects staged rebase and finishes creating $N+1$ without requiring manual intervention. |
| 11 | **Concurrent History Modification** | Canonical history modified concurrently during the network retire window. | TX2 detects mutation drift; aborts stale projection and recomputes pre-user projection from latest canonical truth before creating/using $N+1$. Zero stale snapshot leakage. |
