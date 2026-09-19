"""Bounded tool lifecycle projection pinned to captured AGY 1.2.x evidence."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.ndjson import AgyTurnNormalizer, parse_init


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_SYNTHETIC_CONVERSATION = "33333333-3333-4333-8333-333333333333"


def load_fixture(name: str) -> list[dict]:
    lines = [
        line
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [json.loads(line) for line in lines]


def consume_fixture(
    payloads: list[dict],
    *,
    model: str = "gemini-3.1-pro-high",
    effort: str = "high",
):
    generation = parse_init(payloads[0], model, effort)
    normalizer = AgyTurnNormalizer(generation.provider_session_id)
    events = []
    for payload in payloads[1:]:
        events.extend(normalizer.consume(payload))
    return events


def tool_step(**overrides) -> dict:
    step = {
        "conversation_id": _SYNTHETIC_CONVERSATION,
        "step_index": 1,
        "step_type": "tool",
        "state": "DONE",
        "tool_name": "run_command",
    }
    step.update(overrides)
    return {"event": "step_update", "step_update": step}


class ToolLifecycleFixtureTests(unittest.TestCase):
    def test_captured_1_2_5_success_fixture_projects_bounded_tool_lifecycle(self) -> None:
        events = consume_fixture(load_fixture("agy_1_2_5_tool_success.jsonl"))
        self.assertEqual(
            [event.event_type for event in events],
            [
                "lifecycle",
                "lifecycle",
                "lifecycle",
                "lifecycle",
                "lifecycle",
                "lifecycle",
                "lifecycle",
                "content_delta",
                "content_delta",
                "content_delta",
                "usage",
                "done",
            ],
        )
        tool_events = [
            event.payload
            for event in events
            if event.payload.get("step_type") == "tool"
        ]
        self.assertEqual(
            tool_events,
            [
                {
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "ACTIVE",
                    "category": "provider_tool",
                    "tool_name": "run_command",
                },
                {
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "DONE",
                    "category": "provider_tool",
                    "tool_name": "run_command",
                    "duration_seconds": 4.0443208,
                    "outcome": "tool_completed",
                },
                {
                    "step_index": 4,
                    "step_type": "tool",
                    "state": "ACTIVE",
                    "category": "provider_tool",
                    "tool_name": "view_file",
                },
                {
                    "step_index": 4,
                    "step_type": "tool",
                    "state": "DONE",
                    "category": "provider_tool",
                    "tool_name": "view_file",
                    "duration_seconds": 0.019152,
                    "outcome": "tool_completed",
                },
            ],
        )
        serialized = "".join(event.model_dump_json() for event in events)
        for residue in (
            "tool_info",
            "parameters",
            "CommandLine",
            "echo CP0-SUCCESS-MARKER",
            "probe_input.txt",
            "<workspace>",
        ):
            self.assertNotIn(residue, serialized)

    def test_captured_1_2_7_mcp_fixture_projects_generic_dispatch_without_raw_body(self) -> None:
        payloads = load_fixture("agy_1_2_7_mcp_success.jsonl")
        generation = parse_init(payloads[0], "gemini-3.1-pro-low", "low")
        normalizer = AgyTurnNormalizer(generation.provider_session_id)
        events = []
        for payload in payloads[1:]:
            events.extend(normalizer.consume(payload))

        tool_events = [
            event.payload
            for event in events
            if event.payload.get("step_type") == "tool"
        ]
        mcp_events = [
            payload for payload in tool_events if payload.get("tool_name") == "call_mcp_tool"
        ]
        self.assertEqual(
            mcp_events,
            [
                {
                    "step_index": 4,
                    "step_type": "tool",
                    "state": "ACTIVE",
                    "category": "provider_tool",
                    "tool_name": "call_mcp_tool",
                },
                {
                    "step_index": 4,
                    "step_type": "tool",
                    "state": "DONE",
                    "category": "provider_tool",
                    "tool_name": "call_mcp_tool",
                    "duration_seconds": 0.062178,
                    "outcome": "tool_completed",
                },
            ],
        )
        serialized_mcp_lifecycle = "".join(
            json.dumps(payload, sort_keys=True, ensure_ascii=False) for payload in mcp_events
        )
        for residue in (
            "tool_info",
            "parameters",
            "ServerName",
            "ToolName",
            "exocore-memory",
            "memory_search",
            "CP5-MEMORY-CANARY-9F3A",
            "memory_plasmid",
            "user_manual",
        ):
            self.assertNotIn(residue, serialized_mcp_lifecycle)

    def test_captured_1_2_7_search_web_fixture_projects_bounded_lifecycle_and_grounded_content(self) -> None:
        payloads = load_fixture("agy_1_2_7_search_web_success.jsonl")
        events = consume_fixture(payloads, model="gemini-3.1-pro-low", effort="low")

        tool_events = [
            event.payload
            for event in events
            if event.payload.get("step_type") == "tool"
        ]
        self.assertEqual(
            tool_events,
            [
                {
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "ACTIVE",
                    "category": "provider_tool",
                    "tool_name": "search_web",
                },
                {
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "DONE",
                    "category": "provider_tool",
                    "tool_name": "search_web",
                    "duration_seconds": 2.3529645,
                    "outcome": "tool_completed",
                },
            ],
        )
        # Exactly one native search invocation, and its real wire envelope
        # repeats the query in ``tool_info`` with no ``output`` at all: the
        # output-less DONE is the observed 1.2.7 shape, not a parser gap.
        self.assertEqual({payload["tool_name"] for payload in tool_events}, {"search_web"})
        done_step = next(
            payload["step_update"]
            for payload in payloads
            if payload.get("event") == "step_update"
            and payload["step_update"].get("step_type") == "tool"
            and payload["step_update"].get("state") == "DONE"
        )
        self.assertEqual(sorted(done_step["tool_info"]), ["name", "parameters"])
        self.assertNotIn("output", done_step["tool_info"])

        serialized_lifecycle = "".join(
            json.dumps(payload, sort_keys=True, ensure_ascii=False) for payload in tool_events
        )
        for residue in (
            "tool_info",
            "parameters",
            "query",
            "Python 3 official documentation",
            "docs.python.org",
            "grounding-api-redirect",
            "vertexaisearch",
            "<workspace>",
        ):
            self.assertNotIn(residue, serialized_lifecycle)

        # Grounded search output travels as ordinary assistant content: the
        # cited documentation link and the provider grounding source link stay
        # in the text, and no search-specific result event exists.
        content = "".join(
            event.payload["text"]
            for event in events
            if event.event_type == "content_delta"
        )
        self.assertIn("[docs.python.org/3/](https://docs.python.org/3/)", content)
        self.assertIn("https://vertexaisearch.cloud.google.com/grounding-api-redirect/", content)
        self.assertEqual(content, payloads[-1]["result"]["response"])
        self.assertNotIn(
            "docs.python.org",
            "".join(
                event.model_dump_json()
                for event in events
                if event.event_type not in {"content_delta"}
            ),
        )

        self.assertEqual(
            [event.event_type for event in events][-3:],
            ["content_delta", "usage", "done"],
        )
        usage_event = next(event for event in events if event.event_type == "usage")
        self.assertEqual(usage_event.payload, payloads[-1]["result"]["usage"])

    def test_captured_1_2_5_failure_fixture_projects_tool_error_without_raw_body(self) -> None:
        events = consume_fixture(load_fixture("agy_1_2_5_tool_failure.jsonl"))
        self.assertEqual(events[-1].event_type, "done")
        tool_events = [
            event.payload
            for event in events
            if event.payload.get("step_type") == "tool"
        ]
        self.assertEqual(
            tool_events,
            [
                {
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "ACTIVE",
                    "category": "provider_tool",
                    "tool_name": "view_file",
                },
                {
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "ERROR",
                    "category": "provider_tool",
                    "tool_name": "view_file",
                    "duration_seconds": 0.016582,
                    "outcome": "tool_error",
                },
            ],
        )
        serialized = "".join(event.model_dump_json() for event in events)
        for residue in (
            "tool_info",
            "parameters",
            "AbsolutePath",
            "TOOL_ERROR",
            "this_file_does_not_exist.txt",
            "GetFileAttributesEx",
            "<profile>",
        ):
            self.assertNotIn(residue, serialized)


class ToolLifecycleBoundaryTests(unittest.TestCase):
    def normalize(self, payload: dict):
        normalizer = AgyTurnNormalizer(_SYNTHETIC_CONVERSATION)
        return normalizer.consume(payload)

    def test_tool_done_without_duration_omits_duration(self) -> None:
        payload = self.normalize(tool_step())[0].payload
        self.assertEqual(
            payload,
            {
                "step_index": 1,
                "step_type": "tool",
                "state": "DONE",
                "category": "provider_tool",
                "tool_name": "run_command",
                "outcome": "tool_completed",
            },
        )

    def test_invalid_durations_are_omitted(self) -> None:
        for duration in (True, False, float("nan"), float("inf"), float("-inf"), -1, "3"):
            with self.subTest(duration=duration):
                payload = self.normalize(tool_step(duration_seconds=duration))[0].payload
                self.assertNotIn("duration_seconds", payload)
                self.assertEqual(payload["outcome"], "tool_completed")

    def test_huge_integer_durations_are_omitted_without_overflow(self) -> None:
        # Regression (CP1 R1-01): ``math.isfinite`` on an int that no float can
        # represent raised a raw OverflowError, bypassing the fail-closed
        # parser contract. Unrepresentable values are omitted instead.
        for duration in (10**400, -(10**400), 10**309, 2**2000):
            with self.subTest(duration=duration):
                payload = self.normalize(tool_step(duration_seconds=duration))[0].payload
                self.assertNotIn("duration_seconds", payload)
                self.assertEqual(payload["outcome"], "tool_completed")

    def test_largest_representable_integer_duration_is_still_projected(self) -> None:
        payload = self.normalize(tool_step(duration_seconds=10**308))[0].payload
        self.assertEqual(payload["duration_seconds"], 1e308)
        self.assertIsInstance(payload["duration_seconds"], float)

    def test_integer_duration_is_projected_as_finite_float(self) -> None:
        payload = self.normalize(tool_step(duration_seconds=3))[0].payload
        self.assertEqual(payload["duration_seconds"], 3.0)
        self.assertIsInstance(payload["duration_seconds"], float)

    def test_malformed_tool_name_fails_closed(self) -> None:
        cases = (
            {"nested": "value"},
            ["value"],
            7,
            None,
            "",
            "x" * 101,
        )
        for tool_name in cases:
            with self.subTest(tool_name=tool_name):
                with self.assertRaises(ProviderAdapterError) as caught:
                    self.normalize(tool_step(tool_name=tool_name))
                self.assertEqual(caught.exception.code, "agy_malformed_step")
                self.assertEqual(caught.exception.terminal_status, "indeterminate")
        step = tool_step()
        del step["step_update"]["tool_name"]
        with self.assertRaises(ProviderAdapterError) as caught:
            self.normalize(step)
        self.assertEqual(caught.exception.code, "agy_malformed_step")

    def test_unobserved_tool_state_is_fail_closed(self) -> None:
        with self.assertRaises(ProviderAdapterError) as caught:
            self.normalize(tool_step(state="CANCELLED"))
        self.assertEqual(caught.exception.code, "agy_unknown_step_state")
        self.assertEqual(caught.exception.terminal_status, "indeterminate")

    def test_tool_info_bodies_never_reach_projection(self) -> None:
        normalizer = AgyTurnNormalizer(_SYNTHETIC_CONVERSATION)
        event = normalizer.consume(
            tool_step(
                tool_info={
                    "name": "run_command",
                    "parameters": {"CommandLine": "SECRET-COMMAND"},
                    "output": "SECRET-OUTPUT",
                    "error": {"message": "SECRET-ERROR"},
                }
            )
        )[0]
        serialized = event.model_dump_json()
        for residue in ("tool_info", "SECRET-COMMAND", "SECRET-OUTPUT", "SECRET-ERROR"):
            self.assertNotIn(residue, serialized)
        self.assertNotIn("tool_info", event.payload)
        events = normalizer.consume(
            {
                "event": "result",
                "result": {"conversation_id": _SYNTHETIC_CONVERSATION, "status": "SUCCESS"},
            }
        )
        self.assertEqual([event.event_type for event in events], ["done"])


if __name__ == "__main__":
    unittest.main()
