"""Independent v2 public and provider-neutral runtime contracts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


PROTOCOL_VERSION = "v2"
RUNTIME_CAPABILITIES = (
    "generation_state_only",
    "durable_control_events",
    "requested_effective_execution",
    "strict_session_resume",
    "request_journal_replay",
    "turn_attachments",
    "runtime_mcp_tool_manifest",
    "generated_artifacts",
)


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


PROJECT_RULES_MAX_CHARS = 256_000
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 5
MAX_ATTACHMENT_TOTAL_BYTES = 50 * 1024 * 1024
ATTACHMENT_ID_PATTERN = r"^att-[1-9][0-9]{0,18}$"
ATTACHMENT_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "text/plain": ".txt",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
}
SHA256_PATTERN = r"^[0-9a-f]{64}$"
MCP_TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,99}$"
MAX_RUNTIME_MCP_TOOL_COUNT = 64

# One generated artifact outcome projection: an opaque reference plus bounded
# metadata for ``ready``, or a bounded error code for ``failed``. The raw
# provider tool body and every filesystem path stay Runtime-private.
ARTIFACT_REF_PATTERN = r"^[0-9a-f]{32}$"
MAX_GENERATED_ARTIFACT_BYTES = 20 * 1024 * 1024
GENERATED_ARTIFACT_DISPLAY_NAME_PATTERN = r"^image-[0-9]{1,12}\.(png|jpg|jpeg|gif|webp)$"
GENERATED_ARTIFACT_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)
GENERATED_ARTIFACT_ERROR_CODE_PATTERN = r"^[a-z][a-z0-9_]{0,49}$"


class GenerationSpec(StrictContract):
    schema_version: Literal["v2"] = "v2"
    runtime_kind: Literal["fake", "antigravity"] = "fake"
    bootstrap_fingerprint: str = Field(min_length=1, max_length=256)
    system_instructions: str = Field(
        min_length=1,
        max_length=1_000_000,
        repr=False,
    )
    # Project rules travel as their own frozen field, never concatenated into
    # the system instructions: ``None`` means the project has no rules,
    # ``""`` means rules are present but empty. ExoCore owns the content; the
    # runtime only renders and materializes it.
    project_rules: str | None = Field(
        default=None,
        max_length=PROJECT_RULES_MAX_CHARS,
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
    # CP2-compatible: a rules-absent generation carries no digest at all, so a
    # pre-CP3 (rules-free) identity recomputes byte-identically. Present-empty
    # and present-content generations carry their digest.
    project_rules_sha256: str | None = None


def system_instructions_sha256(system_instructions: str) -> str:
    return hashlib.sha256(system_instructions.strip().encode("utf-8")).hexdigest()


def runtime_mcp_manifest_sha256(
    manifest: tuple[RuntimeMcpTool, ...],
) -> str:
    """Hash the exact ordered manifest using canonical cross-repository JSON."""

    canonical = json.dumps(
        [tool.model_dump(mode="json") for tool in manifest],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_turn_request_hash(request: TurnRequest) -> str:
    """SHA-256 over the complete requested TurnRequest canonical JSON.

    This is the single cross-repository payload identity used for the
    durable request hash, the journal replay header, and ExoCore's
    ``RuntimeTurn.payload_fingerprint``. Any wire change is a hash change.
    """

    canonical = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def generation_identity(spec: GenerationSpec) -> str:
    return generation_identity_parts(
        runtime_kind=spec.runtime_kind,
        bootstrap_fingerprint=spec.bootstrap_fingerprint,
        system_instructions_digest=system_instructions_sha256(
            spec.system_instructions
        ),
        project_rules_digest=project_rules_identity_digest(spec.project_rules),
    )


def project_rules_identity_digest(project_rules: str | None) -> str | None:
    """Identity projection of project rules: absent rules have no digest.

    A rules-free generation keeps the exact pre-CP3 identity payload, so the
    upgrade never rotates a CP2 generation. A present-but-empty ruleset is a
    real fact and carries its own digest.
    """

    if project_rules is None:
        return None
    return project_rules_sha256(project_rules)


def project_rules_sha256(project_rules: str | None) -> str:
    """Identity digest that keeps absent, present-empty and present-content apart.

    ``None`` (the project has no rules) and ``""`` (rules exist but are empty)
    are different generation facts, so the digest covers presence as well as the
    body. The wire spec carries the raw value; this digest is the identity
    projection used by metadata verification and by metadata recovery, where the
    body itself must never be duplicated into durable runtime state.
    """

    payload = {"body": project_rules or "", "present": project_rules is not None}
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def project_rules_absent_digest() -> str:
    """The digest recorded when a generation carries no project rules."""

    return project_rules_sha256(None)


def generation_identity_parts(
    *,
    runtime_kind: str,
    bootstrap_fingerprint: str,
    system_instructions_digest: str,
    project_rules_digest: str | None = None,
) -> str:
    """Recompute a generation identity from durable parts (recovery path).

    The project-rules digest is omitted from the canonical payload when the
    generation has no rules (legacy/CP2 shape). Present rules always contribute
    their digest, so absent != present-empty stays true in both directions.
    """

    payload: dict[str, Any] = {
        # The identity payload is written literally (not from the contract dump)
        # so a rules-free generation hashes exactly like it did before CP3.
        "schema_version": "v2",
        "runtime_kind": runtime_kind,
        "bootstrap_fingerprint": bootstrap_fingerprint,
        "system_instructions_sha256": system_instructions_digest,
    }
    if project_rules_digest is not None:
        payload["project_rules_sha256"] = project_rules_digest
    canonical = json.dumps(
        payload,
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


class ContinuityDeltaTurn(StrictContract):
    """One role-labelled canonical prior turn inside a transport envelope.

    ``user`` turns carry the canonical 27-character UTC timestamp; ``assistant``
    turns must have ``timestamp=None``. The DTO is strict and repr-redacted so
    canonical content is never projected into logs or error text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: Literal["user", "assistant"]
    content: str = Field(max_length=1_000_000, repr=False)
    timestamp: str | None = Field(
        default=None,
        max_length=27,
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
    )

    @model_validator(mode="after")
    def validate_role_timestamp(self) -> "ContinuityDeltaTurn":
        if self.role == "user":
            if self.timestamp is None:
                raise ValueError("user continuity turns require a UTC timestamp")
        elif self.timestamp is not None:
            raise ValueError("assistant continuity turns cannot carry a timestamp")
        return self


class AttachmentManifest(StrictContract):
    artifact_id: str = Field(pattern=ATTACHMENT_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=255)
    mime_type: StrictStr
    size: StrictInt = Field(gt=0, le=MAX_ATTACHMENT_BYTES)
    sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        if value not in ATTACHMENT_EXTENSIONS:
            raise ValueError("unsupported runtime attachment MIME type")
        return value


class RuntimeMcpTool(StrictContract):
    """One exact ExoCore-owned MCP exposure entry for a Runtime turn."""

    name: str = Field(min_length=1, max_length=100, pattern=MCP_TOOL_NAME_PATTERN)
    eager: StrictBool
    max_call_seconds: StrictInt | None = Field(default=None, gt=0)


class TurnRequest(StrictContract):
    schema_version: Literal["v2"] = "v2"
    request_id: UUID
    user_message: str = Field(max_length=1_000_000, repr=False)
    requested_model_id: str = Field(min_length=1, max_length=200)
    requested_thinking_level: ThinkingLevel
    bootstrap_context: dict[str, Any] | None = Field(default=None, repr=False)
    continuity_delta: tuple[ContinuityDeltaTurn, ...] = ()
    runtime_mcp_tools: tuple[RuntimeMcpTool, ...] = Field(
        min_length=1,
        max_length=MAX_RUNTIME_MCP_TOOL_COUNT,
    )
    attachments: tuple[AttachmentManifest, ...] = Field(
        default=(),
        max_length=MAX_ATTACHMENT_COUNT,
        repr=False,
    )
    ephemeral_current: str | None = Field(
        default=None,
        max_length=1_000_000,
        repr=False,
    )

    @model_validator(mode="after")
    def validate_payload_budget(self) -> "TurnRequest":
        if not self.user_message and not self.attachments:
            raise ValueError("turn requires user text or attachments")
        tool_names = [tool.name for tool in self.runtime_mcp_tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("runtime MCP tool names must be request-unique")
        artifact_ids = [attachment.artifact_id for attachment in self.attachments]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("attachment artifact IDs must be request-unique")
        if sum(attachment.size for attachment in self.attachments) > MAX_ATTACHMENT_TOTAL_BYTES:
            raise ValueError("attachments exceed the per-turn byte budget")
        if self.bootstrap_context is not None:
            encoded = json.dumps(
                self.bootstrap_context,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > 1_000_000:
                raise ValueError("bootstrap context exceeds the v2 byte budget")
        encoded = json.dumps(
            [turn.model_dump(mode="json") for turn in self.continuity_delta],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 1_000_000:
            raise ValueError("continuity delta exceeds the v2 byte budget")
        return self


class ProcessExecutionOptions(StrictContract):
    provider_model_slug: str = Field(min_length=1, max_length=200)
    effort: Literal["low", "medium", "high"]
    sandbox: StrictBool = True
    security_policy_revision: str = Field(min_length=1, max_length=100)
    profile_mode: Literal["generation_private"] = "generation_private"
    launch_environment_revision: str = Field(min_length=1, max_length=100)
    # Compatibility default only: persisted pre-CP-D rows parse unchanged.
    # Every newly resolved turn receives a non-null canonical manifest digest.
    mcp_manifest_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


class EffectiveResolution(StrictContract):
    provider_model_slug: str
    effort: Literal["low", "medium", "high"]
    resolver_policy_revision: str
    process_options: ProcessExecutionOptions


def _validate_artifact_payload(payload: dict[str, Any]) -> None:
    """Strict bounded projection for one generated-artifact event."""

    outcome = payload.get("outcome")
    step_index = payload.get("step_index")
    if type(step_index) is not int or step_index < 0:
        raise ValueError("artifact step_index is invalid")
    if outcome == "failed":
        if set(payload) != {"outcome", "step_index", "error_code"}:
            raise ValueError("failed artifact payload is invalid")
        error_code = payload["error_code"]
        if (
            type(error_code) is not str
            or re.fullmatch(GENERATED_ARTIFACT_ERROR_CODE_PATTERN, error_code) is None
        ):
            raise ValueError("artifact error code is invalid")
        return
    if outcome != "ready":
        raise ValueError("artifact outcome is invalid")
    if set(payload) != {
        "outcome",
        "artifact_ref",
        "step_index",
        "index",
        "kind",
        "display_name",
        "mime_type",
        "size",
        "sha256",
    }:
        raise ValueError("ready artifact payload is invalid")
    artifact_ref = payload["artifact_ref"]
    index = payload["index"]
    display_name = payload["display_name"]
    mime_type = payload["mime_type"]
    size = payload["size"]
    sha256 = payload["sha256"]
    if (
        type(artifact_ref) is not str
        or re.fullmatch(ARTIFACT_REF_PATTERN, artifact_ref) is None
        or type(index) is not int
        or index != 0
        or payload["kind"] != "image"
        or type(display_name) is not str
        or re.fullmatch(GENERATED_ARTIFACT_DISPLAY_NAME_PATTERN, display_name) is None
        or type(mime_type) is not str
        or mime_type not in GENERATED_ARTIFACT_MIME_TYPES
        or type(size) is not int
        or not 0 < size <= MAX_GENERATED_ARTIFACT_BYTES
        or type(sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
    ):
        raise ValueError("ready artifact payload is invalid")


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
        "artifact",
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
    provider_input_effect: Literal[
        "not_sent",
        "may_have_reached_provider",
    ] | None = None

    @model_validator(mode="after")
    def validate_event_truth(self) -> "RuntimeEvent":
        if self.terminal:
            if (
                self.terminal_status is None
                or self.bootstrap_consumed is None
                or self.provider_input_effect is None
            ):
                raise ValueError("terminal events require complete terminal truth")
            if (
                self.terminal_status == "completed"
                and self.provider_input_effect != "may_have_reached_provider"
            ):
                raise ValueError(
                    "completed terminal events must report provider input effect"
                )
        elif (
            self.terminal_status is not None
            or self.bootstrap_consumed is not None
            or self.provider_input_effect is not None
        ):
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
        elif self.event_type == "artifact":
            _validate_artifact_payload(self.payload)
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
        "artifact",
        "usage",
        "done",
        "error",
    ]
    payload: dict[str, Any] = Field(default_factory=dict, repr=False)
    terminal_status: Literal["failed", "indeterminate"] | None = None


class JournalReplayHeader(StrictContract):
    """First NDJSON frame of the authenticated read-only journal replay seam."""

    schema_version: Literal["v2"] = "v2"
    frame_type: Literal["journal_header"] = "journal_header"
    binding_id: UUID
    request_id: UUID
    request_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_status: Literal[
        "completed",
        "failed",
        "cancelled",
        "indeterminate",
    ]
    event_count: StrictInt = Field(ge=1)
    last_sequence: StrictInt = Field(ge=1)
