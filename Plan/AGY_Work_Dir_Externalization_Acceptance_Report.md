# AGY Work Directory Externalization — Independent Acceptance Report

## Verdict

**PASS** `[gpt-5.6-sol / Solaire]`

- Baseline: `0956ee533b6462929b9a26052e40197398e33e25`
- Construction owner: `[claude-opus-5-5 / 砚]`
- Acceptance mode: independent verify
- Linked issue: `XinyiS7/ExoCore#46`
- DEPLOY_STEP: none — no schema or data migration.
- RELEASE CONDITION: when no turn is active, restart `run-runtime` and `run-exocore` together. The local `run-runtime` alias already sets `EXOCORE_RUNTIME_AGY_WORK_DIR=D:/Alicia`; the currently running service will not adopt it until restart.

## Frozen behavior accepted

1. Every AGY session launched by this Runtime instance uses the one configured process work directory; there is no per-preset or per-binding override.
2. With `EXOCORE_RUNTIME_AGY_WORK_DIR=D:/Alicia`, session spawn and all AGY preflight commands use `D:/Alicia` as cwd.
3. Profile, mailbox, control, staging workspace, attachments, inspections and generated-artifact snapshots remain generation-private.
4. Retire removes only the generation root and does not remove ordinary files under `D:/Alicia`.
5. Invalid configured paths fail explicitly; no fallback to the private staging workspace occurs after an invalid external path was configured.
6. Existing-session continuity survives the cwd switch with the same provider conversation ID.
7. Ambient `.agents` customizations under the external cwd are intentionally shared by Alicia's explicit D2 decision. MCP/plugin rejection continues to inspect the actual process cwd.

## Verification results

| Gate | Independent evidence | Result |
|---|---|---|
| Baseline integrity | HEAD and `origin/main` both `0956ee5`; repository clean before verification; changed-file scope limited to config/wiring/process adapter, tests, fixtures and documentation | PASS |
| Configuration | `RuntimeConfig.from_env` reads `EXOCORE_RUNTIME_AGY_WORK_DIR`; relative, missing and file paths raise `ValueError`; repr redacts the path | PASS |
| Global wiring | `RuntimeConfig -> create_app -> AntigravityAdapter -> GenerationLayout.work_dir -> preflight/spawn`; one adapter-level value applies to every AGY binding | PASS |
| Default compatibility | Unset configuration continues to use `<generation root>/workspace` | PASS |
| Private-state boundary | Staging workspace and all Runtime-owned state remain rooted in the generation directory; retire still calls `rmtree` only on that root | PASS |
| External-file survival | Independent real smoke wrote a relative file under `D:/Alicia`; it was absent from the generation root and survived retire | PASS |
| Gate 1 continuity | Disposable isolated Runtime restart changed cwd from private workspace to `D:/Alicia`; the resumed turn completed with the identical provider conversation ID and recalled the prior canary | PASS |
| Fail-closed customization guard | External cwd MCP config/plugin sources are rejected before spawn | PASS |
| Regression | Targeted 5/5 passed; full suite ran 349 tests with 348 passed, 1 pre-existing host-symlink skip, 0 failures/errors; `git diff --check` passed | PASS |
| Cleanup | Acceptance probe, temporary state/provider roots and generation root were removed; repository remained clean | PASS |

### Independent real-entry observations

- Official AGY: `1.3.3`.
- Production `RuntimeService + AntigravityAdapter` path with disposable UUID bindings and temporary state/provider roots.
- Provider conversation ID remained identical across restart and cwd change.
- Init timings: `2.42s`, `2.86s`, `2.22s`, all below the frozen `15s` gate.
- Reported cwd: `D:\Alicia`.
- Generation root disappeared after retire; the external probe remained until explicit probe cleanup.
- Existing Runtime process, production provider root, Alessandro and all existing bindings were untouched.

## Findings and accepted limitations

- No P0/P1 findings.
- `P2-DOC-01`: the archived Plan's original status banner says construction had not started, while its appended construction record documents completion. The frozen plan body was deliberately not rewritten during verification; this report is the authoritative closeout verdict.
- Ambient customization sharing is an intentional user-approved boundary change, not evidence that the control capability surface remained unchanged.
- The existing MCP/plugin guard checks the configured cwd itself. No ancestor `.agents` source exists in Alicia's current `D:/Alicia` layout; no speculative ancestor guard was added.

## Ledger

`Cycle: R1 | Checkpoint: FINAL | Baseline: 0956ee5 | Verdict: PASS`

`Owners: Construction 0, Acceptance 0, Harness 0, Spec 0, Environment 0`

`Findings: P2-DOC-01 | Consecutive FAIL Count: 0`