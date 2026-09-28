"""Strict official AGY 1.1.20 - 1.2.5 NDJSON parsing and normalized rendering."""

from __future__ import annotations

import json
import math
from typing import Any

from exocore_runtime.contracts import ProviderEvent, ProviderGeneration
from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.mcp_policy import MCP_SERVER_NAME


_LIFECYCLE_STEP_TYPES = frozenset({"user_input", "checkpoint", "system_message"})
_TEXT_STEP_TYPES = {
    "agent_response": "content_delta",
    "agent_thought": "thinking_delta",
    "agent_thoughts": "thinking_delta",
}
_OBSERVED_STEP_STATES = {
    "user_input": frozenset({"DONE"}),
    "checkpoint": frozenset({"DONE"}),
    "system_message": frozenset({"DONE"}),
    "unknown": frozenset({"DONE"}),
    "agent_response": frozenset({"ACTIVE", "DONE"}),
    "agent_thought": frozenset({"ACTIVE", "DONE"}),
    "agent_thoughts": frozenset({"ACTIVE", "DONE"}),
    "tool": frozenset({"ACTIVE", "DONE", "ERROR"}),
}
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)


def _finite_duration(value: object) -> float | None:
    """Coerce a provider-declared duration, or return None when unusable.

    Bools, non-numbers, negatives, NaN/infinite floats, and integers that a
    float cannot represent (for example ``10**400``) are omitted rather than
    projected. The frozen duration policy is "project only finite
    non-negative seconds"; raising a raw OverflowError here would bypass the
    fail-closed parser contract, so coercion failure is never fatal.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        duration = float(value)
    except OverflowError:
        return None
    if not math.isfinite(duration) or duration < 0:
        return None
    return duration


# The AGY provider hides a Memory tool's real identity in exactly two shapes:
# the generic dispatcher (``call_mcp_tool`` + ``tool_info.parameters``) for lazy
# tools, and the derived ``mcp_<server>_<tool>`` name for eager tools. Every
# other provider tool name is already its own identity.
_MCP_DISPATCH_TOOL_NAME = "call_mcp_tool"
_MCP_EAGER_TOOL_PREFIX = f"mcp_{MCP_SERVER_NAME}_"


def _project_display_tool_name(
    provider_tool_name: str,
    tool_info: object,
    active_mcp_tool_names: frozenset[str],
) -> str:
    """Project a bounded provider tool name for safe display.

    Native tool names pass through unchanged. Two provider shapes carry a Memory
    tool's real identity, and both resolve to a short name only when the name is
    in the manifest accepted by the active AGY process:

    - lazy: ``call_mcp_tool`` with ``tool_info.parameters.{ServerName,ToolName}``
      resolves when ``tool_info`` is an object, ``parameters`` is an object,
      ``ServerName`` is exactly ``MCP_SERVER_NAME``, and ``ToolName`` is an
      enabled name;
    - eager: ``mcp_<MCP_SERVER_NAME>_<tool>`` resolves when ``<tool>`` is an
      enabled name.

    Anything else - missing or malformed ``tool_info``, another server, another
    server's eager prefix, an unknown, non-string, or future tool name - passes
    through unchanged instead of failing the turn. This projection is display
    only: it grants no execution authority and must never be treated as proof
    that a tool ran.
    """

    if provider_tool_name == _MCP_DISPATCH_TOOL_NAME:
        return _project_lazy_mcp_tool_name(
            provider_tool_name, tool_info, active_mcp_tool_names
        )
    if provider_tool_name.startswith(_MCP_EAGER_TOOL_PREFIX):
        tool_name = provider_tool_name[len(_MCP_EAGER_TOOL_PREFIX) :]
        if tool_name in active_mcp_tool_names:
            return tool_name
    return provider_tool_name


def _project_lazy_mcp_tool_name(
    provider_tool_name: str,
    tool_info: object,
    active_mcp_tool_names: frozenset[str],
) -> str:
    """Resolve the lazy dispatcher shape, or keep the dispatcher name when unsure."""

    if not isinstance(tool_info, dict):
        return provider_tool_name
    parameters = tool_info.get("parameters")
    if not isinstance(parameters, dict):
        return provider_tool_name
    if parameters.get("ServerName") != MCP_SERVER_NAME:
        return provider_tool_name
    tool_name = parameters.get("ToolName")
    if not isinstance(tool_name, str) or tool_name not in active_mcp_tool_names:
        return provider_tool_name
    return tool_name


def parse_line(line: bytes) -> dict[str, Any]:
    if not line or not line.strip():
        raise ProviderAdapterError("agy_empty_stdout", terminal_status="indeterminate")
    try:
        decoded = line.decode("utf-8", errors="strict")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderAdapterError(
            "agy_malformed_ndjson",
            terminal_status="indeterminate",
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderAdapterError("agy_malformed_ndjson", terminal_status="indeterminate")
    return payload


def parse_init(
    payload: dict[str, Any],
    expected_model: str,
    expected_effort: str,
) -> ProviderGeneration:
    if payload.get("event") != "init":
        raise ProviderAdapterError("agy_init_missing", fatal_generation=True)
    conversation_id = payload.get("conversation_id")
    init = payload.get("init")
    if (
        not isinstance(conversation_id, str)
        or not conversation_id
        or not isinstance(init, dict)
    ):
        raise ProviderAdapterError("agy_init_invalid", fatal_generation=True)
    observed_model = init.get("model")
    if observed_model != expected_model:
        raise ProviderAdapterError("agy_model_mismatch", fatal_generation=True)
    return ProviderGeneration(
        provider_session_id=conversation_id,
        observed_model=observed_model,
        observed_effort=expected_effort,
    )


class AgyTurnNormalizer:
    """Consumes one turn from one ordered stdout reader."""

    def __init__(
        self,
        conversation_id: str,
        active_mcp_tool_names: tuple[str, ...] = (),
    ) -> None:
        self.conversation_id = conversation_id
        self.active_mcp_tool_names = frozenset(active_mcp_tool_names)
        self.result_seen = False
        self._step_usage_by_index: dict[int, dict[str, int]] = {}

    def consume(self, payload: dict[str, Any]) -> list[ProviderEvent]:
        if self.result_seen:
            raise ProviderAdapterError(
                "agy_event_after_result",
                terminal_status="indeterminate",
            )
        event_type = payload.get("event")
        if event_type == "step_update":
            return self._consume_step(payload.get("step_update"))
        if event_type == "result":
            self.result_seen = True
            return self._consume_result(payload.get("result"))
        raise ProviderAdapterError("agy_unknown_event", terminal_status="indeterminate")

    def _consume_step(self, step: object) -> list[ProviderEvent]:
        if not isinstance(step, dict):
            raise ProviderAdapterError("agy_malformed_step", terminal_status="indeterminate")
        self._validate_conversation(step)
        step_index = step.get("step_index")
        step_type = step.get("step_type")
        state = step.get("state")
        if (
            not isinstance(step_index, int)
            or step_index < 0
            or not isinstance(step_type, str)
            or not isinstance(state, str)
        ):
            raise ProviderAdapterError("agy_malformed_step", terminal_status="indeterminate")
        allowed_states = _OBSERVED_STEP_STATES.get(step_type)
        if allowed_states is None:
            raise ProviderAdapterError("agy_unknown_step", terminal_status="indeterminate")
        if state not in allowed_states:
            raise ProviderAdapterError("agy_unknown_step_state", terminal_status="indeterminate")
        lifecycle = {
            "step_index": step_index,
            "step_type": step_type,
            "state": state,
        }
        if "usage" in step:
            projected = self._project_usage(step["usage"])
            if step_index not in self._step_usage_by_index:
                self._step_usage_by_index[step_index] = projected
            else:
                self._step_usage_by_index[step_index].update(projected)
        if step_type in _TEXT_STEP_TYPES:
            text = step.get("text_delta")
            if text is None or text == "":
                return [ProviderEvent(event_type="lifecycle", payload=lifecycle)]
            if not isinstance(text, str):
                raise ProviderAdapterError("agy_malformed_text_delta", terminal_status="indeterminate")
            return [ProviderEvent(event_type=_TEXT_STEP_TYPES[step_type], payload={"text": text})]
        if step_type in _LIFECYCLE_STEP_TYPES:
            return [ProviderEvent(event_type="lifecycle", payload=lifecycle)]
        if step_type == "unknown":
            forbidden_body = any(
                key in step for key in ("text_delta", "tool_info", "tool_name", "usage")
            )
            if state != "DONE" or forbidden_body:
                raise ProviderAdapterError("agy_unknown_step", terminal_status="indeterminate")
            return [ProviderEvent(event_type="lifecycle", payload=lifecycle)]
        if step_type == "tool":
            # Bounded lifecycle only: the projected tool name is a short
            # provider-declared identifier or a whitelisted Memory short name,
            # while tool_info (parameters, output, error bodies) must never
            # reach a ProviderEvent or the durable journal. The raw name stays
            # the fail-closed bound; only ``_project_display_tool_name`` may
            # rewrite a generic ``call_mcp_tool`` into its frozen Memory
            # identity. Capture evidence:
            # tests/fixtures/agy_1_2_5_tool_success.jsonl,
            # agy_1_2_5_tool_failure.jsonl, agy_1_2_7_mcp_success.jsonl.
            raw_tool_name = step.get("tool_name")
            if not isinstance(raw_tool_name, str) or not (1 <= len(raw_tool_name) <= 100):
                raise ProviderAdapterError("agy_malformed_step", terminal_status="indeterminate")
            lifecycle["category"] = "provider_tool"
            lifecycle["tool_name"] = _project_display_tool_name(
                raw_tool_name,
                step.get("tool_info"),
                self.active_mcp_tool_names,
            )
            duration = _finite_duration(step.get("duration_seconds"))
            if duration is not None:
                lifecycle["duration_seconds"] = duration
            if state == "ERROR":
                lifecycle["outcome"] = "tool_error"
            elif state == "DONE":
                lifecycle["outcome"] = "tool_completed"
            return [ProviderEvent(event_type="lifecycle", payload=lifecycle)]
        raise ProviderAdapterError("agy_unknown_step", terminal_status="indeterminate")

    def _consume_result(self, result: object) -> list[ProviderEvent]:
        if not isinstance(result, dict):
            raise ProviderAdapterError("agy_malformed_result", terminal_status="indeterminate")
        self._validate_conversation(result)
        status = result.get("status")
        if not isinstance(status, str):
            raise ProviderAdapterError("agy_malformed_result", terminal_status="indeterminate")
        # Validate result.usage if present (preserves strictness, AC-05)
        if "usage" in result:
            self._project_usage(result.get("usage"))

        events: list[ProviderEvent] = []
        if self._step_usage_by_index:
            aggregated_usage: dict[str, int] = {}
            for step_usage in self._step_usage_by_index.values():
                for key, val in step_usage.items():
                    aggregated_usage[key] = aggregated_usage.get(key, 0) + val
            if aggregated_usage:
                events.append(ProviderEvent(event_type="usage", payload=aggregated_usage))

        if status == "SUCCESS":
            events.append(
                ProviderEvent(
                    event_type="done",
                    payload={"finish_reason": "stop"},
                )
            )
        else:
            events.append(
                ProviderEvent(
                    event_type="error",
                    payload={"code": "agy_result_error"},
                    terminal_status="failed",
                )
            )
        return events

    def _validate_conversation(self, payload: dict[str, Any]) -> None:
        if payload.get("conversation_id") != self.conversation_id:
            raise ProviderAdapterError(
                "agy_conversation_mismatch",
                terminal_status="indeterminate",
                fatal_generation=True,
            )

    @staticmethod
    def _project_usage(raw: object) -> dict[str, int]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ProviderAdapterError("agy_malformed_usage", terminal_status="indeterminate")
        projected: dict[str, int] = {}
        for key in _USAGE_KEYS:
            value = raw.get(key)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ProviderAdapterError("agy_malformed_usage", terminal_status="indeterminate")
            projected[key] = value
        return projected
