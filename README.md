# ExoCore Runtime Gateway

A provider-neutral loopback service for subscription-backed model transports. It owns only durable provider transport state; canonical conversation and memory data remain outside this sibling service.

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

Runtime turns only work when both halves agree. The correctness contract is:

1. Django and Runtime share the **same Runtime URL** (`SUBSCRIPTION_RUNTIME_URL` against `EXOCORE_RUNTIME_HOST` + `EXOCORE_RUNTIME_PORT`).
2. Django and Runtime share the **same bearer** (`SUBSCRIPTION_RUNTIME_TOKEN` / `EXOCORE_RUNTIME_TOKEN`).
3. The preset allowlist matches (`SUBSCRIPTION_RUNTIME_PRESET_ALLOWLIST`; the launcher uses `8`).
4. Migrations are current (`python.exe manage.py migrate --check --noinput`).
5. The Runtime answers the exact health contract: `GET /v2/health` returns `status=ok`, `schema_version=v2`, `protocol=subscription-runtime-v2` and all five capabilities (`generation_state_only`, `durable_control_events`, `requested_effective_execution`, `strict_session_resume`, `request_journal_replay`).

`../start_backend_with_runtime.ps1` is **one convenience implementation** of that contract (random process-scoped token, port checks, health gate, lifecycle ownership, pre-Django reconcile step). It is not the contract itself: a manual two-terminal startup that satisfies 1-5 is equally valid.

### Pre-launch checklist

1. Stop any running Django/`runserver` and Runtime first (the launcher refuses an occupied `8000` or `8766`).
2. `python.exe manage.py migrate --check --noinput` - unapplied migrations abort startup.
3. Start the Runtime: the launcher, or `EXOCORE_RUNTIME_TOKEN=<secret> python.exe -m exocore_runtime`.
4. `curl http://127.0.0.1:8766/v2/health` - expect the full v2 contract from item 5 above.
5. Start Django with the same URL / bearer / allowlist, then `curl http://127.0.0.1:8000/` - expect `200`.

### Pre-send orphan reconciliation coverage

A runtime turn that dies between the durable prepare commit and the send boundary leaves a `prepared` turn that blocks its conversation until it is settled as `presend_abandoned`. Settlement runs automatically in a quiescent window only:

| Startup | Automatic reconcile | Where |
|---|---|---|
| `manage.py runserver` (default reload) | yes | `RUN_MAIN` child app-startup seam |
| launcher (`--noreload`) | yes | launcher pre-Django step |
| manual `manage.py runserver --noreload` | **not promised** | run `manage.py reconcile_runtime_presend` yourself |

The sweep is only sound while one serving Django process owns runtime turns (single-backend / quiescent window). A multi-worker deployment must re-evaluate it.

### Token hygiene (hygiene, not correctness)

The bearer is a process-scoped secret: the launcher generates it in memory and never writes it to `.env`, a token file, the command line, or a log. Manual startup usually means typing or persisting it, which is why the launcher is preferred - but a manual setup that satisfies the startup contract still works.

## Runtime providers

- `fake`: deterministic protocol and lifecycle fixture retained from Milestone A.
- `antigravity`: official AGY `>=1.1.20,<1.3` using consumer `account_default` authentication and the pinned `gemini-3.1-pro-high` model. The verified envelope is 1.1.20 - 1.2.4 (1.2.4 NDJSON capture lives in `tests/fixtures/agy_1_2_4_success.jsonl`); a new minor boundary needs a fresh capture before the gate is widened.

The AGY adapter has no API-key, Vertex, Python SDK, shell, or unofficial executable fallback. Each generation receives an isolated profile, custom agent, deny-all tool policy, empty workspace, one-shot PreInvocation mailbox, and supervised process tree. SQLite remains the durable source for bootstrap, send, replay, cancellation, and terminal status.

Retiring a generation immediately stops its process and deletes only its generation-owned provider root. Completed journal replay remains available in SQLite. The adapter never deletes or copies the official Windows keyring or canonical ExoCore data.

## Scope limits

This service does not import Django or ExoCore production packages and does not own Endpoint, AgentPreset, Conversation, Message, memory compaction, attachments, MCP, frontend behavior, or other provider adapters.
