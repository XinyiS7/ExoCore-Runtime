# ExoCore Runtime Gateway

A provider-neutral loopback service for subscription-backed model transports. Milestone A contains only the versioned protocol, durable SQLite transport state, and a deterministic fake provider.

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

`EXOCORE_RUNTIME_HOST` must be a literal loopback IP address. Non-loopback values are rejected before Uvicorn starts. `GET /v1/health` is public; all generation and turn routes require `Authorization: Bearer ...`.

## Milestone A limits

No Django, ExoCore database, AGY, real model, external API, MCP, hook, profile, or subscription integration is present. The `fake` provider is intentionally deterministic and exists only to verify protocol and lifecycle invariants.
