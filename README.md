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

`EXOCORE_RUNTIME_HOST` must be a literal loopback IP address. Non-loopback values are rejected before Uvicorn starts. `GET /v1/health` is public; all generation and turn routes require `Authorization: Bearer ...`.

Optional AGY configuration uses `EXOCORE_RUNTIME_AGY_EXECUTABLE`, `EXOCORE_RUNTIME_PROVIDER_DATA_ROOT`, and the bounded `EXOCORE_RUNTIME_AGY_*_TIMEOUT` variables defined in `RuntimeConfig`.

## Runtime providers

- `fake`: deterministic protocol and lifecycle fixture retained from Milestone A.
- `antigravity`: official AGY `>=1.1.20,<1.3` using consumer `account_default` authentication and the pinned `gemini-3.1-pro-high` model. The verified envelope is 1.1.20 - 1.2.4 (1.2.4 NDJSON capture lives in `tests/fixtures/agy_1_2_4_success.jsonl`); a new minor boundary needs a fresh capture before the gate is widened.

The AGY adapter has no API-key, Vertex, Python SDK, shell, or unofficial executable fallback. Each generation receives an isolated profile, custom agent, deny-all tool policy, empty workspace, one-shot PreInvocation mailbox, and supervised process tree. SQLite remains the durable source for bootstrap, send, replay, cancellation, and terminal status.

Retiring a generation immediately stops its process and deletes only its generation-owned provider root. Completed journal replay remains available in SQLite. The adapter never deletes or copies the official Windows keyring or canonical ExoCore data.

## Scope limits

This service does not import Django or ExoCore production packages and does not own Endpoint, AgentPreset, Conversation, Message, memory compaction, attachments, MCP, frontend behavior, or other provider adapters.
