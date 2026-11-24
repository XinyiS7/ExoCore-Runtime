"""Attempt errors versus the turn result (XinyiS7/ExoCore#78).

``agy_agent_response_error.jsonl`` holds the real ERROR frames of requests
54b029cb (step 42) and 7de07e63 (step 47), captured 2026-11-23 from the failure
diagnostics; only the conversation id is replaced. Nothing after those frames
was ever captured (the process was killed on them), so every later frame below
is synthetic and the tests relying on one say so in their name.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.ndjson import AgyTurnNormalizer, parse_line

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "agy_agent_response_error.jsonl"
_CONVERSATION = "11111111-2222-3333-4444-555555555555"
_NDJSON_LOGGER = "exocore_runtime.providers.antigravity.ndjson"


def real_error_frames() -> list[dict]:
    return [parse_line(line) for line in _FIXTURE.read_bytes().splitlines()]


def step(index: int, step_type: str, state: str, **extra) -> dict:
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": _CONVERSATION,
            "step_index": index,
            "step_type": step_type,
            "state": state,
            **extra,
        },
    }


def result(status: str) -> dict:
    payload = {"conversation_id": _CONVERSATION, "status": status, "response": "ignored"}
    if status != "SUCCESS":
        payload["error"] = "blocked"
        payload["finished_reason"] = "error"
    return {"event": "result", "result": payload}


def error_message(index: int) -> dict:
    return step(index, "error_message", "DONE")


class AttemptErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = AgyTurnNormalizer(_CONVERSATION)
        self.error_frame = real_error_frames()[1]

    def feed(self, *frames: dict) -> list[tuple[str, dict]]:
        events = []
        for frame in frames:
            events.extend(self.normalizer.consume(frame))
        self.last_events = events
        return [(event.event_type, event.payload) for event in events]

    def test_real_error_frames_emit_nothing_and_keep_their_usage(self) -> None:
        for frame in real_error_frames():
            with self.subTest(step_index=frame["step_update"]["step_index"]):
                normalizer = AgyTurnNormalizer(_CONVERSATION)
                with self.assertLogs(_NDJSON_LOGGER, "WARNING"):
                    self.assertEqual(normalizer.consume(frame), [])
                usage = normalizer.consume(result("SUCCESS"))[0]
                self.assertEqual(usage.event_type, "usage")
                self.assertEqual(usage.payload, frame["step_update"]["usage"])

    def test_synthetic_retry_after_partial_answer_streams_both_and_finishes(self) -> None:
        events = self.feed(
            step(47, "agent_response", "ACTIVE", text_delta="partial "),
            self.error_frame,
            error_message(48),
            step(49, "agent_response", "ACTIVE", text_delta="full "),
            step(49, "agent_response", "DONE", text_delta="answer"),
            result("SUCCESS"),
        )
        self.assertEqual(
            [payload["text"] for kind, payload in events if kind == "content_delta"],
            ["partial ", "full ", "answer"],
        )
        self.assertEqual(events[-1], ("done", {"finish_reason": "stop"}))
        self.assertNotIn("lifecycle", [kind for kind, _ in events])

    def test_synthetic_exhausted_retry_is_a_failure_not_a_completed_half_answer(self) -> None:
        events = self.feed(
            step(47, "agent_response", "ACTIVE", text_delta="partial"),
            self.error_frame,
            error_message(48),
            result("ERROR"),
        )
        self.assertEqual(events[-1], ("error", {"code": "agy_result_error"}))
        self.assertEqual(self.last_events[-1].terminal_status, "failed")
        self.assertIsNone(self.normalizer.ignored_result_error)

    def test_synthetic_result_error_after_a_new_answer_keeps_the_stale_error_rule(self) -> None:
        events = self.feed(
            step(47, "agent_response", "ACTIVE", text_delta="partial"),
            self.error_frame,
            step(49, "agent_response", "DONE", text_delta="full answer"),
            result("ERROR"),
        )
        self.assertEqual(events[-1], ("done", {"finish_reason": "stop"}))
        self.assertEqual(self.normalizer.ignored_result_error, "blocked")

    def test_synthetic_earlier_answer_before_a_tool_does_not_hide_the_failed_attempt(self) -> None:
        events = self.feed(
            step(36, "agent_response", "DONE", text_delta="first part"),
            step(40, "tool", "ACTIVE", tool_name="view_file"),
            step(40, "tool", "DONE", tool_name="view_file"),
            real_error_frames()[0],
            result("ERROR"),
        )
        self.assertEqual(events[-1], ("error", {"code": "agy_result_error"}))

    def test_synthetic_every_failed_attempt_resets_the_answer(self) -> None:
        events = self.feed(
            step(47, "agent_response", "ACTIVE", text_delta="one"),
            self.error_frame,
            step(50, "agent_response", "ACTIVE", text_delta="two"),
            step(50, "agent_response", "ERROR"),
            result("ERROR"),
        )
        self.assertEqual(events[-1], ("error", {"code": "agy_result_error"}))

    def test_error_frame_text_is_streamed_but_does_not_revive_the_attempt(self) -> None:
        events = self.feed(
            step(47, "agent_response", "ERROR", text_delta="tail"),
            result("ERROR"),
        )
        self.assertEqual(events[0], ("content_delta", {"text": "tail"}))
        self.assertEqual(events[-1], ("error", {"code": "agy_result_error"}))

    def test_only_an_answer_error_resets_the_answer(self) -> None:
        for frame in (
            step(48, "agent_thought", "ERROR"),
            step(48, "agent_thoughts", "ERROR"),
            step(48, "tool", "ERROR", tool_name="view_file"),
            step(48, "agent_response", "CANCELLED"),
        ):
            with self.subTest(frame=frame["step_update"]["step_type"]):
                normalizer = AgyTurnNormalizer(_CONVERSATION)
                normalizer.consume(step(47, "agent_response", "DONE", text_delta="answer"))
                normalizer.consume(frame)
                self.assertEqual(normalizer.consume(result("ERROR"))[-1].event_type, "done")

    def test_failed_attempt_note_counts_streamed_answer_without_quoting_it(self) -> None:
        with self.assertLogs(_NDJSON_LOGGER, "WARNING") as logs:
            self.feed(
                step(46, "agent_thought", "DONE", text_delta="thinking is not answer text"),
                step(47, "agent_response", "ACTIVE", text_delta="CANARY-BODY"),
                self.error_frame,
            )
        self.assertEqual(
            self.normalizer.unrecognized_steps,
            [{
                "step_index": 47,
                "step_type": "agent_response",
                "state": "ERROR",
                "error": None,
                "answer_chars": len("CANARY-BODY"),
            }],
        )
        self.assertEqual(len(logs.output), 1)
        for fact in ("47", "agent_response", "ERROR", str(len("CANARY-BODY"))):
            self.assertIn(fact, logs.output[0])
        self.assertNotIn("CANARY", logs.output[0])


class UnmodeledStateTests(unittest.TestCase):
    """A structurally valid state of a modeled type never ends the turn."""

    def test_every_modeled_type_is_noted_without_lifecycle(self) -> None:
        for step_type, extra in (
            ("user_input", {}),
            ("checkpoint", {}),
            ("system_message", {}),
            ("unknown", {}),
            ("agent_response", {}),
            ("agent_thought", {}),
            ("agent_thoughts", {}),
            ("tool", {"tool_name": "view_file", "tool_info": {"output": "TOOL-CANARY"}}),
        ):
            with self.subTest(step_type=step_type):
                normalizer = AgyTurnNormalizer(_CONVERSATION)
                self.assertEqual(
                    normalizer.consume(step(5, step_type, "CANCELLED", **extra)), []
                )
                self.assertEqual(
                    normalizer.unrecognized_steps,
                    [{"step_index": 5, "step_type": step_type, "state": "CANCELLED", "error": None}],
                )
                self.assertNotIn("CANARY", json.dumps(normalizer.unrecognized_steps))

    def test_note_bounds_the_provider_declared_state(self) -> None:
        normalizer = AgyTurnNormalizer(_CONVERSATION)
        normalizer.consume(step(5, "checkpoint", "X" * 500))
        self.assertEqual(normalizer.unrecognized_steps[0]["state"], "X" * 40)

    def test_thought_text_under_an_unmodeled_state_is_not_lost(self) -> None:
        normalizer = AgyTurnNormalizer(_CONVERSATION)
        events = normalizer.consume(step(5, "agent_thought", "ERROR", text_delta="why"))
        self.assertEqual(
            [(event.event_type, event.payload) for event in events],
            [("thinking_delta", {"text": "why"})],
        )

    def test_malformed_content_stays_fail_closed_under_an_unmodeled_state(self) -> None:
        for frame, code in (
            (step(5, "tool", "CANCELLED"), "agy_malformed_step"),
            (step(5, "agent_response", "ERROR", text_delta=7), "agy_malformed_text_delta"),
            (step(5, "agent_response", "ERROR", usage={"input_tokens": "x"}), "agy_malformed_usage"),
            (step(5, "unknown", "CANCELLED", text_delta="body"), "agy_unknown_step"),
            (step(5, "brand_new_step", "ERROR", text_delta="body"), "agy_unknown_step"),
        ):
            with self.subTest(code=code, step_type=frame["step_update"]["step_type"]):
                with self.assertRaises(ProviderAdapterError) as caught:
                    AgyTurnNormalizer(_CONVERSATION).consume(frame)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.terminal_status, "indeterminate")


if __name__ == "__main__":
    unittest.main()
