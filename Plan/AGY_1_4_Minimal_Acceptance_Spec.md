# AGY 1.4 Minimal Compatibility — Frozen Acceptance Spec

> Issue: XinyiS7/ExoCore#64
> Acceptance owner: Solaire
> Frozen: 2026-10-16
> Original Plan baseline: `de08429268b75d2f5c9ba875d423364b5b1cd685`
> Original Plan blob: `90500965fef0907a6f5a13862d4ac974ba3af271`
> Scope amendment: Alicia explicitly replaced the capture-heavy path with a minimal version-envelope change and will perform the real G045 smoke test herself.

## 1. Scope

This checkpoint restores the existing Gemini 3.8 AGY route after the official CLI auto-updated to 1.4.0.

Allowed changes:

- accept official AGY versions `>=1.1.20,<1.5`;
- keep `1.5.0+` fail-closed;
- move the existing fake unsupported-version stamp to `1.5.0`;
- synchronize existing envelope comments and documentation.

Explicitly excluded:

- Argon or any model-policy change;
- Runtime wire, capability, parser, event, environment, argv, resolver, persistence, migration, or lifecycle changes;
- new capture harnesses, fixtures, test modules, temporary G045 conversations, messages, presets, or database-backed probes;
- agent-triggered paid G045 turns. Alicia owns the real smoke test.

## 2. Frozen Gates

### G1 — Version envelope

The production preflight MUST accept versions from `1.1.20` through all `1.4.x` releases and MUST reject versions below `1.1.20` or at/above `1.5.0` with `agy_version_unsupported`.

### G2 — Rejection remains early and fail-closed

The existing unsupported-version path MUST still fail before model/quota probing, AGY session spawn, or user stdin. Existing fake-AGY evidence and tests are reused; no parallel harness is required.

### G3 — No behavioral drift

The implementation MUST NOT alter Runtime capabilities, shared wire shapes, AGY argv/environment, parser behavior, model resolution, session identity, tool handling, persistence, or cleanup behavior. The production-code delta is limited to the version-boundary condition and its explanatory text.

### G4 — Documentation consistency

`process.py`, `ndjson.py`, `AGENTS.md`, and `README.md` MUST state the same envelope: `>=1.1.20,<1.5`. The amended Plan MUST record that the earlier four-turn capture matrix was superseded by Alicia's minimal path rather than claiming it was executed.

### G5 — Mechanical regression

The existing version-boundary/adapter tests and the complete ExoCore-Runtime test suite MUST pass without new skips or weakened assertions. `git diff --check` MUST pass.

### G6 — Alicia-owned real smoke

After source acceptance, Alicia will restart Runtime and send one ordinary message through an existing G045 conversation using Endpoint 7 with `gemini-3.8-flash`. Success means the turn reaches a normal terminal response without `agy_version_unsupported`.

Acceptance agents MUST NOT create temporary G045 conversations/messages or run a substitute paid probe. Until Alicia reports this smoke result, the source verdict is `PASS / USER-SMOKE-PENDING`; final PASS and issue closure require her confirmation.

## 3. Evidence and Delivery

Required evidence:

- construction commit and exact changed-file list;
- frozen-spec integrity;
- focused adapter/version test result;
- full Runtime suite result;
- `git diff --check` and clean worktree;
- Alicia's manual smoke result for final PASS.

`DEPLOY_STEP: Alicia restarts run-runtime in her normal visible service pane; no migration.`
