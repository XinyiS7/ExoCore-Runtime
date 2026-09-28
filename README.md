# ExoCore Runtime Gateway

A provider-neutral loopback service for subscription-backed model transports. It owns only durable provider transport state; canonical conversation and memory data remain outside this service.

Repository: `XinyiS7/ExoCore-Runtime` — standalone since 2026-09-26, extracted from the umbrella `ExoCore_Project` repository with history preserved.

## Run

```bash
python.exe -m pip install -e .
export EXOCORE_RUNTIME_TOKEN="a-long-local-secret"
python.exe -m exocore_runtime
```

Defaults:

- bind: `127.0.0.1`
- port: `8766`
- state: platform-local `ExoCore-Runtime/runtime.sqlite3`
- provider artifacts: `providers/` beside the SQLite state file

`EXOCORE_RUNTIME_HOST` must be a literal loopback IP address. Non-loopback values are rejected before Uvicorn starts. `GET /v2/health` is public; all generation and turn routes require `Authorization: Bearer ...`.

Optional AGY configuration uses `EXOCORE_RUNTIME_AGY_EXECUTABLE`, `EXOCORE_RUNTIME_PROVIDER_DATA_ROOT`, and the bounded `EXOCORE_RUNTIME_AGY_*_TIMEOUT` variables defined in `RuntimeConfig`.

## Startup contract (Django + Runtime)

Daily local topology:

```text
Browser
  ↓
nginx :8080 / :8443          (persistent Docker container)
  ├─ serves the built frontend with SPA fallback
  └─ /api, /media → Django :8000
                         ↓
                   Runtime :8766
```

Django is normally started manually with `python manage.py runserver`. Subscription Runtime is a separate loopback service on `:8766`, with its own bearer and its own state file. nginx is the browser ingress and is outside this contract; `:8080/:8443` is not an alias of Django `:8000`, which is the API upstream it proxies to.

Runtime correctness depends on:

1. **Runtime URL alignment** - Django `SUBSCRIPTION_RUNTIME_URL` against Runtime `EXOCORE_RUNTIME_HOST` / `EXOCORE_RUNTIME_PORT`.
2. **Bearer alignment** - Django `SUBSCRIPTION_RUNTIME_TOKEN` against Runtime `EXOCORE_RUNTIME_TOKEN`.
3. **Django authorizes the intended Runtime preset** in `SUBSCRIPTION_RUNTIME_PRESET_ALLOWLIST` (the current local setup authorizes presets 1 and 8).
4. **Migrations current** - `python.exe manage.py migrate --check --noinput`.
5. **Exact health contract** - `GET /v2/health` returns `status=ok`, `schema_version=v2`, `protocol=subscription-runtime-v2` and all seven capabilities, in order: `generation_state_only`, `durable_control_events`, `requested_effective_execution`, `strict_session_resume`, `request_journal_replay`, `turn_attachments`, `runtime_mcp_tool_manifest`.

Startup check: `8000` and `8766` free -> migrate check -> start the Runtime -> `curl http://127.0.0.1:8766/v2/health` -> start Django with the matching URL and bearer.

Pre-send reconcile: a `runserver` reload session reconciles abandoned pre-send turns automatically in its `RUN_MAIN` child startup; a `--noreload` session has to run `python manage.py reconcile_runtime_presend` itself. The sweep assumes one serving Django process.

Token hygiene (not correctness): the bearer is process-scoped. The umbrella's `../start_backend_with_runtime.ps1` stays an optional convenience that starts both halves with one in-memory bearer; the daily flow above does not need it.

## Runtime providers

- `fake`: deterministic protocol and lifecycle fixture retained from Milestone A.
- `antigravity`: official AGY `>=1.1.20,<1.3` using consumer `account_default` authentication and the pinned `gemini-3.1-pro-high` model. The verified envelope is 1.1.20 - 1.2.4 (1.2.4 NDJSON capture lives in `tests/fixtures/agy_1_2_4_success.jsonl`); a new minor boundary needs a fresh capture before the gate is widened.

The AGY adapter has no API-key, Vertex, Python SDK, shell, or unofficial executable fallback. Each generation receives an isolated profile, custom agent, deny-all tool policy, a generation-private workspace (request-scoped attachment staging only), a one-shot PreInvocation mailbox, and a supervised process tree. SQLite remains the durable source for bootstrap, send, replay, cancellation, and terminal status.

Retiring a generation immediately stops its process and deletes only its generation-owned provider root. Completed journal replay remains available in SQLite. The adapter never deletes or copies the official Windows keyring or canonical ExoCore data.

### Dynamic Runtime MCP manifest

`runtime_mcp_tool_manifest` is a lockstep wire capability. Every turn carries an ordered ExoCore-owned `runtime_mcp_tools` manifest whose exact item fields are `name`, `eager`, and `max_call_seconds`. Runtime validates its bounded shape, uniqueness, canonical names, digest, and timeout budget, then materializes AGY `enabledTools` and eager entries solely from that manifest. Runtime intentionally keeps no mirror of ExoCore tool names; its own facts remain the MCP server identity, scoped permissions, and AGY-native tools.

The manifest participates in the turn request fingerprint and process execution options, not generation identity. A changed manifest disposes and respawns the AGY process once while resuming the same provider session. Every non-null `max_call_seconds` must be less than the configured AGY idle timeout, and idle must not exceed hard timeout; defaults satisfy `45 < 60 <= 180`.

Django and Runtime must deploy this capability together. With no active turn, stop both services, update both repositories, start Runtime first, verify the exact health capability list, then start Django. A new client with an old Runtime or an old client with a new Runtime fails closed at exact capability/preflight validation before a turn is sent; this checkpoint must not be split-pushed.

### Turn attachments

Current-turn image attachments are staged before send and rendered into the provider's `CurrentUserMessage`; earlier turns are never replayed.

- Capability is declared by the handshake: `turn_attachments` is part of the exact seven-item `RUNTIME_CAPABILITIES` / `GET /v2/health` list; clients must gate on that exact list instead of any static endpoint table.
- Staging uses an authenticated raw `PUT /v2/generations/{binding_id}/turns/{request_id}/attachments/{artifact_id}`, writing request-scoped opaque `.blob` artifacts under the generation workspace with atomic writes; a request accepts up to 5 files, 20 MiB each and 50 MiB total.
- Before provider spawn, size, SHA256 and MIME are verified; staged artifacts materialize crash-idempotently into the request's attachment directory, and only Runtime-owned absolute paths are projected into the rendered input.
- `DELETE /v2/generations/{binding_id}/turns/{request_id}/attachments` prunes pre-send staging idempotently; once the request is durably registered, both PUT and DELETE refuse with `request_registered` and leave bytes unchanged.
- Retire deletes only the generation-owned provider root, which holds every staging and materialized attachment artifact.

## Scope limits

This service does not import Django or ExoCore production packages and does not own Endpoint, AgentPreset, Conversation, Message, memory compaction, canonical attachment records or content (it only stages, materializes and retires request-scoped attachment bytes under the generation root), MCP, frontend behavior, or other provider adapters.
