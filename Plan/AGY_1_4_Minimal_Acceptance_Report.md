# AGY 1.4 Minimal Compatibility — Acceptance Report

> Issue: XinyiS7/ExoCore#64
> Acceptance owner: Solaire / gpt-5.6-sol
> Frozen spec: `a3632d9` (`Plan/AGY_1_4_Minimal_Acceptance_Spec.md`)
> Construction baseline: `5daba9a425844963531dfd3dd799c390d348890f`
> Date: 2026-10-16

## Verdict

**FINAL PASS**

Source, regression, deployment, and Alicia-owned real smoke gates all pass.

## Gate Results

| Gate | Result | Evidence |
|---|---|---|
| G1 Version envelope | PASS | `process.py` accepts `>=1.1.20,<1.5`; lower bound unchanged; 1.5+ maps to `agy_version_unsupported`. |
| G2 Early fail-closed | PASS | Version rejection remains before models, quota, process spawn, and stdin; existing fake-AGY boundary path retained with `bad_version=1.5.0`. |
| G3 No behavioral drift | PASS | Production delta is only the upper-bound constant and explanatory text; no wire, capability, parser, argv, environment, model, persistence, or lifecycle change. |
| G4 Documentation consistency | PASS | `process.py`, `ndjson.py`, `AGENTS.md`, and `README.md` agree on `>=1.1.20,<1.5`; Plan records Alicia's reduced scope. No new capture, fixture, test module, or probe. |
| G5 Mechanical regression | PASS | Focused adapter suite 51/51; final full suite 357 PASS + 1 existing host-symlink skip; diff check clean. |
| G6 Alicia-owned real smoke | PASS | After restarting Runtime, Alicia confirmed an existing AGY G045 conversation communicated normally with no `agy_version_unsupported` (`XinyiS7/ExoCore#64`, comment `6212967867`). No acceptance agent created a G045 conversation/message or paid probe. |

## Independent Verification Notes

- `HEAD == origin/main == 5daba9a`; worktree clean.
- Frozen spec blob remained unchanged (`c06e5d66cd7c2e3ad7b60aafb548c624f4f7c85b`).
- Two full-suite attempts exposed different transient Windows process-timing failures. Each failing test then passed three isolated repetitions; both affected modules passed, and the final complete 358-test run passed with only the existing unelevated-host symlink skip. Neither failure touched the changed version-boundary path.
- No migration and no database write are involved.

## Deploy Handoff

`DEPLOY_STEP: completed — Alicia restarted run-runtime in her normal visible service pane; no migration.`

Alicia completed the existing-conversation smoke: AGY communication was normal and no `agy_version_unsupported` occurred.