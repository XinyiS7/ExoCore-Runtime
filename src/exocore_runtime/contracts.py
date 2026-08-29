"""Independent v2 public and provider-neutral runtime contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


PROTOCOL_VERSION = "v2"
RUNTIME_CAPABILITIES = (
    "generation_state_only",
    "durable_control_events",
    "requested_effective_execution",
    "strict_session_resume",
)


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GenerationSpec(StrictContract):
    schema_version: Literal["v2"] = "v2"
    runtime_kind: Literal["fake", "antigravity"] = "fake"
    bootstrap_fingerprint: str = Field(min_length=1, max_length=256)
    system_instructions: str = Field(
        min_length=1,
        max_length=1_000_000,
        repr=False,
    )

    @model_validator(mode="after")
    def validate_system_instructions(self) -> "GenerationSpec":
        if not self.system_instructions.strip():
            raise ValueError("system instructions cannot be blank")
        return self


class GenerationIdentity(StrictContract):
    schema_version: Literal["v2"] = "v2"
    runtime_kind: Literal["fake", "antigravity"]
    bootstrap_fingerprint: str
    system_instructions_sha256: str


def system_instructions_sha256(system_instructions: str) -> str:
    return hashlib.sha256(system_instructions.strip().encode("utf-8")).hexdigest()


def generation_identity(spec: GenerationSpec) -> str:
    payload = GenerationIdentity(
        runtime_kind=spec.runtime_kind,
        bootstrap_fingerprint=spec.bootstrap_fingerprint,
        system_instructions_sha256=system_instructions_sha256(spec.system_instructions),
    )
    canonical = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class GenerationResult(StrictContract):
    schema_version: Literal["v2"] = "v2"
    binding_id: UUID
    status: Literal["starting", "active", "retired", "failed"]
    provider_session_id: str | None = Field(default=None, repr=False)


ThinkingLevel = Literal["off", "auto", "low", "medium", "high", "max"]


class TurnRequest(StrictContract):
    schema_version: Literal["v2"] = "v2"
    request_id: UUID
    user_message: str = Field(min_length=1, max_length=1_000_000, repr=False)
    requested_model_id: str = Field(min_length=1, max_length=200)
    requested_thinking_level: ThinkingLevel
    bootstrap_context: dict[str, Any] | None = Field(default=None, repr=False)
    ephemeral_current: str | None = Field(
        default=None,
        max_length=1_000_000,
        repr=False,
    )

    @model_validator(mode="after")
    def validate_payload_budget(self) -> "TurnRequest":
        if self.bootstrap_context is not None:
            encoded = json.dumps(
                self.bootstrap_context,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > 1_000_000:
                raise ValueError("bootstrap context exceeds the v2 byte budget")
        return self


class ProcessExecutionOptions(StrictContract):
    provider_model_slug: str = Field(min_length=1, max_length=200)
    effort: Literal["low", "medium", "high"]
    sandbox: StrictBool = True
    security_policy_revision: str = Field(min_length=1, max_length=100)
    profile_mode: Literal["generation_private"] = "generation_private"
    launch_environment_revision: str = Field(min_length=1, max_length=100)


class EffectiveResolution(StrictContract):
    provider_model_slug: str
    effort: Literal["low", "medium", "high"]
    resolver_policy_revision: str
    process_options: ProcessExecutionOptions


class RuntimeEvent(StrictContract):
    schema_version: Literal["v2"] = "v2"
    binding_id: UUID
    request_id: UUID
    sequence: int = Field(ge=1)
    event_type: Literal[
        "generation_activated",
        "execution_resolved",
        "thinking_delta",
        "content_delta",
        "lifecycle",
        "usage",
        "done",
        "error",
    ]
    payload: dict[str, Any] = Field(default_factory=dict, repr=False)
    terminal: bool = False
    terminal_status: Literal[
        "completed",
        "failed",
        "cancelled",
        "indeterminate",
    ] | None = None
    bootstrap_consumed: StrictBool | None = None

    @model_validator(mode="after")
    def validate_event_truth(self) -> "RuntimeEvent":
        if self.terminal:
            if self.terminal_status is None or self.bootstrap_consumed is None:
                raise ValueError("terminal events require complete terminal truth")
        elif self.terminal_status is not None or self.bootstrap_consumed is not None:
            raise ValueError("non-terminal events cannot carry terminal truth")
        if self.event_type == "generation_activated":
            if set(self.payload) != {"provider_session_id"}:
                raise ValueError("generation_activated payload is invalid")
            value = self.payload["provider_session_id"]
            if not isinstance(value, str) or not value:
                raise ValueError("generation_activated requires a session id")
        elif self.event_type == "execution_resolved":
            required = {
                "effective_provider_model_slug",
                "effective_effort",
                "resolver_policy_revision",
            }
            if set(self.payload) != required or any(
                not isinstance(self.payload[key], str) or not self.payload[key]
                for key in required
            ):
                raise ValueError("execution_resolved payload is invalid")
        return self


class CancelResult(StrictContract):
    schema_version: Literal["v2"] = "v2"
    binding_id: UUID
    request_id: UUID
    status: Literal["completed", "failed", "cancelled", "indeterminate"]
    changed: bool


class RetireRequest(StrictContract):
    schema_version: Literal["v2"] = "v2"
    reason: str = Field(default="retired", min_length=1, max_length=200)


class RetireResult(StrictContract):
    schema_version: Literal["v2"] = "v2"
    binding_id: UUID
    status: Literal["retired"]
    changed: bool


class ProviderGeneration(StrictContract):
    provider_session_id: str = Field(repr=False)
    observed_model: str
    observed_effort: Literal["low", "medium", "high"]


class ProviderEvent(StrictContract):
    event_type: Literal[
        "thinking_delta",
        "content_delta",
        "lifecycle",
        "usage",
        "done",
        "error",
    ]
    payload: dict[str, Any] = Field(default_factory=dict, repr=False)
    terminal_status: Literal["failed", "indeterminate"] | None = None
