"""Deterministic AGY requested-to-effective execution policy."""

from __future__ import annotations

from exocore_runtime.contracts import EffectiveResolution, ProcessExecutionOptions
from exocore_runtime.errors import ProviderAdapterError


RESOLVER_POLICY_REVISION = "agy-gemini-tier-v3"
SECURITY_POLICY_REVISION = "agy-tool-perm-v6"
LAUNCH_ENVIRONMENT_REVISION = "agy-isolated-env-v1"

# AGY sells fixed model+tier pairs, so an ExoCore thinking level selects one of
# its three tiers instead of travelling as a free-form ``--effort``. ``auto``
# lands on ``medium`` because that is AGY's own default tier for these models;
# ``off`` lands on the lowest tier because AGY cannot disable thinking at all;
# ``max`` folds onto the ceiling. Both falls mirror the gateway contract in
# ``ExoCore/engines/llm.py::_build_thinking_config`` (#58).
#
# Which models may reach AGY at all is ExoCore's exact Endpoint policy; whether
# the derived slug is actually for sale is the ``agy models`` gate in
# ``process.ensure()`` (``frozen_execution_unavailable``). A missing tier fails
# closed: it never downgrades to another tier.
_TIER_BY_THINKING_LEVEL = {
    "off": "low",
    "low": "low",
    "medium": "medium",
    "auto": "medium",
    "high": "high",
    "max": "high",
}
_DEFAULT_TIER = "medium"
_MODEL_PREFIX = "gemini-"
_RELEASE_CHANNEL_SUFFIX = "-preview"


def resolve_execution(requested_model_id: str, requested_thinking_level: str) -> EffectiveResolution:
    if not requested_model_id.startswith(_MODEL_PREFIX):
        raise ProviderAdapterError(
            "unsupported_requested_execution",
            status_code=422,
        )
    tier = _TIER_BY_THINKING_LEVEL.get(requested_thinking_level, _DEFAULT_TIER)
    base = requested_model_id.removesuffix(_RELEASE_CHANNEL_SUFFIX)
    model_slug = f"{base}-{tier}"
    # Provider-neutral ``ProcessExecutionOptions`` keeps ``sandbox=True`` as its
    # default; this provider deliberately opts out. The verified 1.2.5 tool
    # unlock runs the CLI directly on the host with the deny policy as the
    # authority (see renderer.DENY_POLICY and the CP0 evidence gate report), so
    # a sandboxed spawn would be an incoherent half-state.
    options = ProcessExecutionOptions(
        provider_model_slug=model_slug,
        effort=tier,
        sandbox=False,
        security_policy_revision=SECURITY_POLICY_REVISION,
        launch_environment_revision=LAUNCH_ENVIRONMENT_REVISION,
    )
    return EffectiveResolution(
        provider_model_slug=model_slug,
        effort=tier,
        resolver_policy_revision=RESOLVER_POLICY_REVISION,
        process_options=options,
    )
