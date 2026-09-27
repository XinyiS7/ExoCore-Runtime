# ExoCore-Runtime Agent Guide

This service is an outbound provider runtime. It must remain independent from Django and the ExoCore Python packages.

## Repository

- Independent repository: `git@github.com:XinyiS7/ExoCore-Runtime.git`, branch `main`.
- Extracted 2026-09-26 from the umbrella `ExoCore_Project` repository with full history preserved. The umbrella no longer tracks this directory; it now only holds cross-repo docs/scripts, nginx config and local data.
- The live worktree normally sits at `ExoCore_Project/ExoCore-Runtime/` as a nested independent checkout, so sibling paths such as `../ExoCore` or `../start_backend_with_runtime.ps1` belong to the umbrella and are local conveniences, not part of this repository.
- Cross-repo plans that cover this service together with Django live in `ExoCore/Plan/` of the sibling `ExoCore` repository.

## Boundaries

- Do not import ExoCore, Django, AGY, Google SDK, MCP, or legacy `engines.bridge` code.
- Keep canonical Conversation/Message/Memory ownership in ExoCore. This service owns only provider transport state.
- Bind only to an IP address verified as loopback before Uvicorn starts.
- Never log or persist the bearer token.
- SQLite state and its event journal are the durable truth; in-memory adapters are not.
- A `sqlite3.Connection` context manager commits/rolls back but does not close the handle. Every connection owner must close explicitly so Windows restart/cleanup can release the database file.
- Milestone A fake behavior remains a deterministic test surface. Milestone B adds only the official AGY adapter (verified envelope 1.1.20 - 1.2.4, `tests/fixtures/agy_1_2_4_success.jsonl`); never add API-key, SDK, Vertex, shell, or unofficial executable fallback.
- AGY profiles, custom agents, hooks, workspace, temp/cache roots, mailbox artifacts, and request-scoped turn-attachment staging (opaque `.blob` artifacts materialized into final request files) must remain generation-private under the configured provider data root. Retire deletes only that generation root.
- The `turn_attachments` capability is declared by the `GET /v2/health` handshake (exact `RUNTIME_CAPABILITIES` list); clients must gate on that handshake instead of any static endpoint table. Attachment staging is request-scoped and bounded (5 files / 20 MiB each / 50 MiB total), verified (size/SHA256/MIME) before provider spawn, materialized crash-idempotently, discarded idempotently pre-send, and refused with `request_registered` once the request is durably registered.
- SQLite owns send/bootstrap/terminal truth. AGY process ownership must survive Gateway hard exit through the Windows Job Object, and shutdown/retire must terminalize open requests before cleanup.

## Commands

```bash
python.exe -m pip install -e .
python.exe -m unittest discover -s tests -v
python.exe -m exocore_runtime
```

Use Python 3.12 and ASCII double quotes in Python source.

Daily local launch (Alicia): `run-runtime` for this service (:8766) alongside `run-exocore` for Django — both are bash aliases in `~/.bashrc`; nginx runs as a persistent Docker container and is not part of the startup steps. The umbrella's `../start_backend_with_runtime.ps1` is an optional convenience wrapper only; do not present it as the normal startup path.
