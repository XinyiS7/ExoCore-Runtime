"""Deterministic AGY requested-to-effective execution policy."""

from __future__ import annotations

from exocore_runtime.contracts import EffectiveResolution, ProcessExecutionOptions
from exocore_runtime.errors import ProviderAdapterError


RESOLVER_POLICY_REVISION = "agy-gemini-3.1-pro-v1"
SECURITY_POLICY_REVISION = "agy-seven-deny-v1"
LAUNCH_ENVIRONMENT_REVISION = "agy-isolated-env-v1"

_POLICY: dict[tuple[str, str], tuple[str, str]] = {
    ("gemini-3.1-pro-preview", "auto"): ("gemini-3.1-pro-high", "high"),
    ("gemini-3.1-pro-preview", "low"): ("gemini-3.1-pro-low", "low"),
    ("gemini-3.1-pro-preview", "high"): ("gemini-3.1-pro-high", "high"),
}


def resolve_execution(requested_model_id: str, requested_thinking_level: str) -> EffectiveResolution:
    pair = _POLICY.get((requested_model_id, requested_thinking_level))
    if pair is None:
        raise ProviderAdapterError(
            "unsupported_requested_execution",
            status_code=422,
        )
    model_slug, effort = pair
    options = ProcessExecutionOptions(
        provider_model_slug=model_slug,
        effort=effort,
        security_policy_revision=SECURITY_POLICY_REVISION,
        launch_environment_revision=LAUNCH_ENVIRONMENT_REVISION,
    )
    return EffectiveResolution(
        provider_model_slug=model_slug,
        effort=effort,
        resolver_policy_revision=RESOLVER_POLICY_REVISION,
        process_options=options,
    )
