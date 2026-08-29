"""Strict official AGY 1.1.20 NDJSON parsing and normalized rendering."""

from __future__ import annotations

import json
from typing import Any

from exocore_runtime.contracts import ProviderEvent, ProviderGeneration
from exocore_runtime.errors import ProviderAdapterError


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
    "tool": frozenset({"ACTIVE", "ERROR"}),
}
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)


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

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.result_seen = False
        self._latest_step_usage: dict[str, int] = {}

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
            self._latest_step_usage = self._project_usage(step["usage"])
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
            lifecycle["category"] = "provider_tool"
            if state == "ERROR":
                lifecycle["outcome"] = "tool_error"
            return [ProviderEvent(event_type="lifecycle", payload=lifecycle)]
        raise ProviderAdapterError("agy_unknown_step", terminal_status="indeterminate")

    def _consume_result(self, result: object) -> list[ProviderEvent]:
        if not isinstance(result, dict):
            raise ProviderAdapterError("agy_malformed_result", terminal_status="indeterminate")
        self._validate_conversation(result)
        status = result.get("status")
        if not isinstance(status, str):
            raise ProviderAdapterError("agy_malformed_result", terminal_status="indeterminate")
        events: list[ProviderEvent] = []
        usage = {
            **self._latest_step_usage,
            **self._project_usage(result.get("usage")),
        }
        if usage:
            events.append(ProviderEvent(event_type="usage", payload=usage))
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
