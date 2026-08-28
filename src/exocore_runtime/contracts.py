"""Versioned public and provider-neutral protocol contracts."""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FakeBehavior(StrEnum):
    NORMAL = "normal"
    DUPLICATE_TERMINAL = "duplicate_terminal"
    TERMINAL_THEN_EVENT = "terminal_then_event"
    MALFORMED = "malformed"
    UNEXPECTED_EOF = "unexpected_eof"
    EMPTY = "empty"
    EXCEPTION = "exception"
    PROVIDER_ERROR = "provider_error"
    CANCEL_LATE = "cancel_late"


class GenerationSpec(StrictContract):
    schema_version: Literal["v1"] = "v1"
    runtime_kind: Literal["fake", "antigravity"] = "fake"
    provider_model_id: str = Field(min_length=1, max_length=200)
    bootstrap_fingerprint: str = Field(min_length=1, max_length=256)
    config_fingerprint: str = Field(min_length=1, max_length=256)
    provider_session_id: str | None = Field(
        default=None,
        max_length=512,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    system_instructions: str | None = Field(default=None, min_length=1, max_length=1_000_000)

    @model_validator(mode="after")
    def validate_runtime_fields(self) -> "GenerationSpec":
        if self.runtime_kind == "antigravity":
            if self.provider_model_id != "gemini-3.1-pro-high":
                raise ValueError("antigravity requires the pinned provider model")
            if self.system_instructions is None or not self.system_instructions.strip():
                raise ValueError("antigravity requires system instructions")
        elif self.system_instructions is not None:
            raise ValueError("fake generations do not accept system instructions")
        return self


def generation_identity_from_hashes(
    *,
    schema_version: str,
    runtime_kind: str,
    provider_model_id: str,
    bootstrap_fingerprint: str,
    config_fingerprint: str,
    provider_session_id: str | None,
    system_instructions_sha256: str | None,
) -> str:
    payload = {
        "schema_version": schema_version,
        "runtime_kind": runtime_kind,
        "provider_model_id": provider_model_id,
        "bootstrap_fingerprint": bootstrap_fingerprint,
        "config_fingerprint": config_fingerprint,
        "provider_session_id": provider_session_id,
        "system_instructions_sha256": system_instructions_sha256,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def generation_identity(spec: GenerationSpec) -> str:
    instructions = spec.system_instructions
    return generation_identity_from_hashes(
        schema_version=spec.schema_version,
        runtime_kind=spec.runtime_kind,
        provider_model_id=spec.provider_model_id,
        bootstrap_fingerprint=spec.bootstrap_fingerprint,
        config_fingerprint=spec.config_fingerprint,
        provider_session_id=spec.provider_session_id,
        system_instructions_sha256=(
            hashlib.sha256(instructions.strip().encode("utf-8")).hexdigest()
            if instructions is not None
            else None
        ),
    )


class GenerationResult(StrictContract):
    schema_version: Literal["v1"] = "v1"
    binding_id: UUID
    status: Literal["starting", "active", "retired", "failed"]
    provider_session_id: str | None = None


class TurnRequest(StrictContract):
    schema_version: Literal["v1"] = "v1"
    request_id: UUID
    user_message: str = Field(min_length=1, max_length=1_000_000)
    bootstrap_context: dict[str, Any] | None = None
    ephemeral_current: str | None = Field(default=None, max_length=1_000_000)
    behavior: FakeBehavior = FakeBehavior.NORMAL

    @model_validator(mode="after")
    def validate_payload_budget(self) -> "TurnRequest":
        if self.bootstrap_context is not None:
            encoded = json.dumps(
                self.bootstrap_context,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > 1_000_000:
                raise ValueError("bootstrap context exceeds the v1 byte budget")
        return self


class RuntimeEvent(StrictContract):
    schema_version: Literal["v1"] = "v1"
    binding_id: UUID
    request_id: UUID
    sequence: int = Field(ge=1)
    event_type: Literal[
        "thinking_delta",
        "content_delta",
        "lifecycle",
        "usage",
        "done",
        "error",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    terminal: bool = False
    terminal_status: Literal[
        "completed",
        "failed",
        "cancelled",
        "indeterminate",
    ] | None = None
    bootstrap_consumed: StrictBool | None = None

    @model_validator(mode="after")
    def validate_terminal_truth(self) -> "RuntimeEvent":
        if self.terminal:
            if self.terminal_status is None or self.bootstrap_consumed is None:
                raise ValueError("terminal events require complete terminal truth")
        elif self.terminal_status is not None or self.bootstrap_consumed is not None:
            raise ValueError("non-terminal events cannot carry terminal truth")
        return self


class CancelResult(StrictContract):
    schema_version: Literal["v1"] = "v1"
    binding_id: UUID
    request_id: UUID
    status: Literal["completed", "failed", "cancelled", "indeterminate"]
    changed: bool


class RetireRequest(StrictContract):
    schema_version: Literal["v1"] = "v1"
    reason: str = Field(default="retired", min_length=1, max_length=200)


class RetireResult(StrictContract):
    schema_version: Literal["v1"] = "v1"
    binding_id: UUID
    status: Literal["retired"]
    changed: bool


class ProviderGeneration(StrictContract):
    provider_session_id: str
    observed_model: str


class ProviderEvent(StrictContract):
    event_type: Literal[
        "thinking_delta",
        "content_delta",
        "lifecycle",
        "usage",
        "done",
        "error",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    terminal_status: Literal["failed", "indeterminate"] | None = None
