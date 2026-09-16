# ExoCore-Runtime Agent Guide

This sibling service is an outbound provider runtime. It must remain independent from Django and the ExoCore Python packages.

## Boundaries

- Do not import ExoCore, Django, AGY, Google SDK, MCP, or legacy `engines.bridge` code.
- Keep canonical Conversation/Message/Memory ownership in ExoCore. This service owns only provider transport state.
- Bind only to an IP address verified as loopback before Uvicorn starts.
- Never log or persist the bearer token.
- SQLite state and its event journal are the durable truth; in-memory adapters are not.
- A `sqlite3.Connection` context manager commits/rolls back but does not close the handle. Every connection owner must close explicitly so Windows restart/cleanup can release the database file.
- Milestone A fake behavior remains a deterministic test surface. Milestone B adds only the official AGY adapter (verified envelope 1.1.20 - 1.2.4, `tests/fixtures/agy_1_2_4_success.jsonl`); never add API-key, SDK, Vertex, shell, or unofficial executable fallback.
- AGY profiles, custom agents, hooks, workspace, temp/cache roots, and mailbox artifacts must remain generation-private under the configured provider data root. Retire deletes only that generation root.
- SQLite owns send/bootstrap/terminal truth. AGY process ownership must survive Gateway hard exit through the Windows Job Object, and shutdown/retire must terminalize open requests before cleanup.

## Commands

```bash
python.exe -m pip install -e .
python.exe -m unittest discover -s tests -v
python.exe -m exocore_runtime
```

Use Python 3.12 and ASCII double quotes in Python source.
