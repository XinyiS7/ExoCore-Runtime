"""Deterministic AGY requested-to-effective execution policy."""

from __future__ import annotations

from exocore_runtime.contracts import EffectiveResolution, ProcessExecutionOptions
from exocore_runtime.errors import ProviderAdapterError


RESOLVER_POLICY_REVISION = "agy-gemini-high-v2"
SECURITY_POLICY_REVISION = "agy-tool-perm-v6"
LAUNCH_ENVIRONMENT_REVISION = "agy-isolated-env-v1"

# AGY runs every Gemini model at its high tier whatever thinking level was
# requested. Which models may reach AGY at all is ExoCore's exact Endpoint
# policy; whether the derived slug exists is the ``agy models`` gate in
# ``process.ensure()`` (``frozen_execution_unavailable``).
_EFFORT = "high"
_MODEL_PREFIX = "gemini-"
_RELEASE_CHANNEL_SUFFIX = "-preview"


def resolve_execution(requested_model_id: str, requested_thinking_level: str) -> EffectiveResolution:
    del requested_thinking_level  # every level maps to the high tier
    if not requested_model_id.startswith(_MODEL_PREFIX):
        raise ProviderAdapterError(
            "unsupported_requested_execution",
            status_code=422,
        )
    base = requested_model_id.removesuffix(_RELEASE_CHANNEL_SUFFIX)
    model_slug, effort = f"{base}-{_EFFORT}", _EFFORT
    # Provider-neutral ``ProcessExecutionOptions`` keeps ``sandbox=True`` as its
    # default; this provider deliberately opts out. The verified 1.2.5 tool
    # unlock runs the CLI directly on the host with the deny policy as the
    # authority (see renderer.DENY_POLICY and the CP0 evidence gate report), so
    # a sandboxed spawn would be an incoherent half-state.
    options = ProcessExecutionOptions(
        provider_model_slug=model_slug,
        effort=effort,
        sandbox=False,
        security_policy_revision=SECURITY_POLICY_REVISION,
        launch_environment_revision=LAUNCH_ENVIRONMENT_REVISION,
    )
    return EffectiveResolution(
        provider_model_slug=model_slug,
        effort=effort,
        resolver_policy_revision=RESOLVER_POLICY_REVISION,
        process_options=options,
    )
