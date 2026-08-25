"""Versioned public and provider-neutral protocol contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    runtime_kind: Literal["fake"] = "fake"
    provider_model_id: str = Field(min_length=1, max_length=200)
    bootstrap_fingerprint: str = Field(min_length=1, max_length=256)
    config_fingerprint: str = Field(min_length=1, max_length=256)
    provider_session_id: str | None = Field(default=None, max_length=512)


class GenerationResult(StrictContract):
    schema_version: Literal["v1"] = "v1"
    binding_id: UUID
    status: Literal["starting", "active", "retired", "failed"]
    provider_session_id: str | None = None


class TurnRequest(StrictContract):
    schema_version: Literal["v1"] = "v1"
    request_id: UUID
    user_message: str = Field(min_length=1, max_length=1_000_000)
    behavior: FakeBehavior = FakeBehavior.NORMAL


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
