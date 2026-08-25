# ExoCore-Runtime Agent Guide

This sibling service is an outbound provider runtime. It must remain independent from Django and the ExoCore Python packages.

## Boundaries

- Do not import ExoCore, Django, AGY, Google SDK, MCP, or legacy `engines.bridge` code.
- Keep canonical Conversation/Message/Memory ownership in ExoCore. This service owns only provider transport state.
- Bind only to an IP address verified as loopback before Uvicorn starts.
- Never log or persist the bearer token.
- SQLite state and its event journal are the durable truth; in-memory adapters are not.
- A `sqlite3.Connection` context manager commits/rolls back but does not close the handle. Every connection owner must close explicitly so Windows restart/cleanup can release the database file.
- Milestone A uses only the deterministic fake provider. Real CLI/provider work requires a later explicit checkpoint.

## Commands

```bash
python.exe -m pip install -e .
python.exe -m unittest discover -s tests -v
python.exe -m exocore_runtime
```

Use Python 3.12 and ASCII double quotes in Python source.
