import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from exocore_runtime.contracts import ContinuityDeltaTurn, GenerationSpec, TurnRequest
from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.ephemeral_hook import (
    EphemeralMailbox,
    consume_for_hook,
)
from exocore_runtime.providers.antigravity.ndjson import AgyTurnNormalizer, parse_init
from exocore_runtime.providers.antigravity.renderer import (
    DENY_POLICY,
    render_stdin_line,
)
from exocore_runtime.state_store import RuntimeStateStore


class AntigravityComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.binding_id = str(uuid4())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_generation_identity_is_digest_and_sqlite_never_contains_system_plaintext(self) -> None:
        canary = "system-instructions-private-canary"
        spec = GenerationSpec(
            runtime_kind="antigravity",
            bootstrap_fingerprint="bootstrap",
            system_instructions=canary,
        )
        store = RuntimeStateStore(self.root / "runtime.sqlite3")
        identity = store.generation_identity(spec)
        self.assertEqual(len(identity), 64)
        self.assertNotIn(canary, identity)
        record, created = store.ensure_generation(self.binding_id, spec)
        self.assertTrue(created)
        self.assertEqual(record.identity_hash, identity)
        persisted = b"".join(path.read_bytes() for path in self.root.iterdir())
        self.assertNotIn(canary.encode(), persisted)

    def test_renderer_keeps_ephemeral_out_of_stdin_and_bootstrap_is_first_only(self) -> None:
        ephemeral = "ephemeral-render-canary"
        current = "current-user-once"
        request = TurnRequest(
            request_id=uuid4(),
            user_message=current,
            requested_model_id="gemini-3.1-pro-preview",
            requested_thinking_level="auto",
            bootstrap_context={"continuity_anchor": "prior"},
            ephemeral_current=ephemeral,
        )
        first = render_stdin_line(request, is_first_turn=True)
        payload = json.loads(first)
        content = payload["message"]["content"]
        self.assertIn("ExoCorePriorContinuity v1", content)
        self.assertIn("prior", content)
        self.assertEqual(content.count(current), 1)
        self.assertNotIn(ephemeral, content)
        later = request.model_copy(update={"bootstrap_context": None})
        self.assertEqual(
            json.loads(render_stdin_line(later, is_first_turn=False))["message"]["content"],
            current,
        )
        self.assertEqual(
            set(DENY_POLICY),
            {
                "read_file(*)",
                "write_file(*)",
                "read_url(*)",
                "execute_url(*)",
                "command(*)",
                "unsandboxed(*)",
                "mcp(*)",
            },
        )

    def test_renderer_preserves_raw_multiline_history_and_literal_backslash_n(self) -> None:
        current = "continue this conversation"
        multiline = "first paragraph\n\n**bold**\n\n---\n\n```python\nprint('ok')\n```"
        intentional_literal = r"the two characters \n stay literal"
        request = TurnRequest(
            request_id=uuid4(),
            user_message=current,
            requested_model_id="gemini-3.1-pro-preview",
            requested_thinking_level="auto",
            bootstrap_context={
                "synthetic_fake_pair": {
                    "user": "fake user\nsecond line",
                    "assistant": "fake assistant\n\nwith spacing",
                },
                "buffer_turns": "[User] buffered\n\n[AI] buffered reply",
                "historical_flow": [
                    {
                        "role": "user",
                        "content": "older user\n\nsecond paragraph",
                        "timestamp": "2026-08-20 21:13",
                    },
                    {
                        "role": "assistant",
                        "content": multiline + "\n\n" + intentional_literal,
                        "timestamp": None,
                    },
                ],
            },
        )

        rendered = render_stdin_line(request, is_first_turn=True)
        self.assertEqual(rendered.count(b"\n"), 1)
        content = json.loads(rendered)["message"]["content"]

        self.assertIn(multiline, content)
        self.assertIn(intentional_literal, content)
        self.assertNotIn(r"first paragraph\n\n**bold**", content)
        self.assertIn("HistoricalTurn 0 role=user timestamp=2026-08-20 21:13", content)
        self.assertIn("HistoricalTurn 1 role=assistant", content)
        self.assertLess(content.index("older user"), content.index("first paragraph"))
        self.assertEqual(content.count(current), 1)

    def test_later_turn_delta_renders_ordered_sections_then_single_current(self) -> None:
        current = "current later turn"
        multiline = "delta user\n\nsecond paragraph\n\n- list item"
        intentional_literal = r"delta literal \n stays literal"
        request = TurnRequest(
            request_id=uuid4(),
            user_message=current,
            requested_model_id="gemini-3.1-pro-preview",
            requested_thinking_level="auto",
            bootstrap_context=None,
            continuity_delta=(
                ContinuityDeltaTurn(
                    role="user",
                    content=multiline,
                    timestamp="2026-08-20T12:34:56.123456Z",
                ),
                ContinuityDeltaTurn(
                    role="assistant",
                    content="delta assistant reply\n\n" + intentional_literal,
                    timestamp=None,
                ),
            ),
        )

        rendered = render_stdin_line(request, is_first_turn=False)
        self.assertEqual(rendered.count(b"\n"), 1)
        payload = json.loads(rendered)
        self.assertEqual(payload["event"], "user")
        content = payload["message"]["content"]

        self.assertNotIn("ExoCorePriorContinuity v1", content)
        self.assertIn(
            "HistoricalTurn 0 role=user timestamp=2026-08-20T12:34:56.123456Z",
            content,
        )
        self.assertIn("HistoricalTurn 1 role=assistant", content)
        self.assertIn(multiline, content)
        self.assertIn(intentional_literal, content)
        self.assertNotIn(r"delta user\n\nsecond paragraph", content)
        self.assertEqual(content.count(current), 1)
        self.assertLess(
            content.index("delta user"), content.index("delta assistant reply")
        )
        self.assertLess(
            content.index("delta assistant reply"), content.index(current)
        )
        self.assertTrue(content.rstrip().endswith(f"--- end CurrentUserMessage ---"))

    def test_later_turn_empty_delta_preserves_current_user_only_rendering(self) -> None:
        current = "model switch entry"
        request = TurnRequest(
            request_id=uuid4(),
            user_message=current,
            requested_model_id="gemini-3.1-flash",
            requested_thinking_level="low",
            bootstrap_context=None,
            continuity_delta=(),
        )
        rendered = render_stdin_line(request, is_first_turn=False)
        self.assertEqual(rendered.count(b"\n"), 1)
        content = json.loads(rendered)["message"]["content"]
        self.assertEqual(content, current)
        self.assertNotIn("HistoricalTurn", content)
        self.assertNotIn("CurrentUserMessage", content)

    def test_first_turn_delta_follows_bootstrap_sections_before_current(self) -> None:
        current = "first send after unsent failure"
        request = TurnRequest(
            request_id=uuid4(),
            user_message=current,
            requested_model_id="gemini-3.1-pro-preview",
            requested_thinking_level="auto",
            bootstrap_context={
                "synthetic_fake_pair": {"user": "fake", "assistant": "pair"},
                "buffer_turns": "",
                "historical_flow": [],
            },
            continuity_delta=(
                ContinuityDeltaTurn(
                    role="user",
                    content="failed unsent user",
                    timestamp="2026-08-20T13:00:00.000000Z",
                ),
            ),
        )
        rendered = render_stdin_line(request, is_first_turn=True)
        self.assertEqual(rendered.count(b"\n"), 1)
        content = json.loads(rendered)["message"]["content"]
        self.assertIn("ExoCorePriorContinuity v1", content)
        self.assertIn("SyntheticFakePair user", content)
        self.assertIn(
            "HistoricalTurn 0 role=user timestamp=2026-08-20T13:00:00.000000Z",
            content,
        )
        self.assertEqual(content.count(current), 1)
        self.assertLess(
            content.index("SyntheticFakePair user"), content.index("failed unsent user")
        )
        self.assertLess(content.index("failed unsent user"), content.index(current))

    def test_mailbox_consumes_once_and_receipt_contains_no_plaintext(self) -> None:
        canary = "ephemeral-mailbox-canary"
        mailbox = EphemeralMailbox(
            self.root / "mailbox",
            binding_id=self.binding_id,
            generation_id="generation-1",
            ttl_seconds=30,
        )
        request_id = str(uuid4())
        payload_hash = mailbox.prepare(request_id, canary)
        first = consume_for_hook(mailbox.root)
        second = consume_for_hook(mailbox.root)
        self.assertEqual(first, {"injectSteps": [{"ephemeralMessage": canary}]})
        self.assertEqual(second, {})
        mailbox.validate_receipt(request_id, payload_hash)
        self.assertFalse(mailbox.pending_path.exists())
        receipt = mailbox.receipt_path.read_bytes()
        self.assertNotIn(canary.encode(), receipt)
        self.assertNotIn("ephemeral_current", json.loads(receipt))

    def test_presend_mailbox_failures_remove_plaintext_and_allow_next_prepare(self) -> None:
        canary = "stale-mailbox-private-canary"
        mailbox = EphemeralMailbox(
            self.root / "mailbox",
            binding_id=self.binding_id,
            generation_id="generation-1",
            ttl_seconds=30,
        )
        stale_request = str(uuid4())
        mailbox.prepare(stale_request, canary)
        pending = json.loads(mailbox.pending_path.read_text(encoding="utf-8"))
        pending["created_at"] = time.time() - 100
        mailbox.pending_path.write_text(json.dumps(pending), encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            mailbox.prepare(stale_request, canary)
        self.assertEqual(caught.exception.code, "ephemeral_pending_stale")
        self.assertFalse(mailbox.pending_path.exists())
        self.assertNotIn(canary.encode(), b"".join(path.read_bytes() for path in mailbox.root.iterdir()))

        conflicting_request = str(uuid4())
        mailbox.prepare(conflicting_request, canary)
        next_request = str(uuid4())
        with self.assertRaises(ProviderAdapterError) as caught:
            mailbox.prepare(next_request, "next")
        self.assertEqual(caught.exception.code, "ephemeral_pending_conflict")
        self.assertFalse(mailbox.pending_path.exists())
        mailbox.prepare(next_request, "next")
        self.assertTrue(mailbox.pending_path.exists())

    def test_post_write_validation_failure_removes_pending_plaintext(self) -> None:
        canary = "post-write-private-canary"
        mailbox = EphemeralMailbox(
            self.root / "mailbox-post-write",
            binding_id=self.binding_id,
            generation_id="generation-1",
            ttl_seconds=30,
        )
        with patch.object(
            mailbox,
            "validate_pending",
            side_effect=ProviderAdapterError("ephemeral_pending_mismatch"),
        ):
            with self.assertRaises(ProviderAdapterError) as caught:
                mailbox.prepare(str(uuid4()), canary)
        self.assertEqual(caught.exception.code, "ephemeral_pending_mismatch")
        self.assertFalse(mailbox.pending_path.exists())
        self.assertNotIn(canary.encode(), b"".join(path.read_bytes() for path in mailbox.root.iterdir()))

    def test_ndjson_init_and_ordered_step_result_normalization(self) -> None:
        conversation_id = "provider-session-1"
        generation = parse_init(
            {
                "event": "init",
                "conversation_id": conversation_id,
                "init": {"model": "gemini-3.1-pro-high", "tools": ["x"]},
            },
            "gemini-3.1-pro-high",
            "high",
        )
        self.assertEqual(generation.provider_session_id, conversation_id)
        self.assertEqual(generation.observed_effort, "high")
        normalizer = AgyTurnNormalizer(conversation_id)
        events = []
        for payload in (
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": 1,
                    "step_type": "user_input",
                    "state": "DONE",
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": 2,
                    "step_type": "agent_thought",
                    "state": "ACTIVE",
                    "text_delta": "think",
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": 3,
                    "step_type": "agent_response",
                    "state": "DONE",
                    "text_delta": "answer",
                    "usage": {"cache_read_tokens": 3},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": 4,
                    "step_type": "tool",
                    "state": "ERROR",
                    "tool_name": "view_file",
                    "tool_info": {"secret_path": "must-not-project"},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": 5,
                    "step_type": "unknown",
                    "state": "DONE",
                },
            },
            {
                "event": "result",
                "result": {
                    "conversation_id": conversation_id,
                    "status": "SUCCESS",
                    "usage": {"input_tokens": 7},
                },
            },
        ):
            events.extend(normalizer.consume(payload))
        self.assertEqual(
            [event.event_type for event in events],
            ["lifecycle", "thinking_delta", "content_delta", "lifecycle", "lifecycle", "usage", "done"],
        )
        serialized = "".join(event.model_dump_json() for event in events)
        self.assertNotIn("must-not-project", serialized)
        self.assertEqual(
            events[3].payload,
            {
                "step_index": 4,
                "step_type": "tool",
                "state": "ERROR",
                "category": "provider_tool",
                "tool_name": "view_file",
                "outcome": "tool_error",
            },
        )
        self.assertEqual(events[-2].payload, {"cache_read_tokens": 3})
        with self.assertRaises(ProviderAdapterError) as caught:
            normalizer.consume(
                {
                    "event": "result",
                    "result": {"conversation_id": conversation_id, "status": "SUCCESS"},
                }
            )
        self.assertEqual(caught.exception.code, "agy_event_after_result")

    def test_ndjson_rejects_unknown_states_and_projects_only_bounded_tool_metadata(self) -> None:
        conversation_id = "provider-session-safe-projection"
        for step_type, state in (
            ("system_message", "PRIVATE-STATE-CANARY"),
            ("agent_response", "C:/PRIVATE/PATH/CANARY.txt"),
        ):
            with self.subTest(step_type=step_type, state=state):
                normalizer = AgyTurnNormalizer(conversation_id)
                with self.assertRaises(ProviderAdapterError) as caught:
                    normalizer.consume(
                        {
                            "event": "step_update",
                            "step_update": {
                                "conversation_id": conversation_id,
                                "step_index": 1,
                                "step_type": step_type,
                                "state": state,
                            },
                        }
                    )
                self.assertEqual(caught.exception.code, "agy_unknown_step_state")
        normalizer = AgyTurnNormalizer(conversation_id)
        event = normalizer.consume(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": 2,
                    "step_type": "tool",
                    "state": "ERROR",
                    "tool_name": "view_file",
                    "tool_info": {"secret_path": "TOOL-CANARY"},
                },
            }
        )[0]
        serialized = event.model_dump_json()
        self.assertNotIn("TOOL-CANARY", serialized)
        self.assertEqual(event.payload["tool_name"], "view_file")
        self.assertEqual(event.payload["category"], "provider_tool")
        self.assertEqual(event.payload["outcome"], "tool_error")

    def test_known_provider_session_does_not_claim_bootstrap_was_sent(self) -> None:
        spec = GenerationSpec(
            runtime_kind="antigravity",
            bootstrap_fingerprint="bootstrap",
            system_instructions="private system",
        )
        store = RuntimeStateStore(self.root / "known-session.sqlite3")
        record, created = store.ensure_generation(self.binding_id, spec)
        self.assertTrue(created)
        self.assertEqual(record.status, "starting")
        self.assertIsNone(record.provider_session_id)
        self.assertFalse(record.bootstrap_sent)
        store.activate_generation(
            self.binding_id,
            "known-provider-session",
            str(uuid4()),
        )
        activated = store.get_generation(self.binding_id)
        self.assertEqual(activated.provider_session_id, "known-provider-session")
        self.assertFalse(activated.bootstrap_sent)

    def test_init_model_mismatch_is_fatal_before_turn(self) -> None:
        with self.assertRaises(ProviderAdapterError) as caught:
            parse_init(
                {
                    "event": "init",
                    "conversation_id": "provider-session",
                    "init": {"model": "wrong-model"},
                },
                "gemini-3.1-pro-high",
                "high",
            )
        self.assertEqual(caught.exception.code, "agy_model_mismatch")
        self.assertTrue(caught.exception.fatal_generation)

    def test_captured_1_2_4_transcript_normalizes_to_the_recorded_events(self) -> None:
        """Sanitized transcript captured from the official AGY 1.2.4 CLI."""

        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "agy_1_2_4_success.jsonl"
        )
        payloads = [
            json.loads(line)
            for line in fixture.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        generation = parse_init(payloads[0], "gemini-3.1-pro-high", "high")
        self.assertEqual(
            generation.provider_session_id,
            "11111111-2222-3333-4444-555555555555",
        )

        normalizer = AgyTurnNormalizer(generation.provider_session_id)
        events = [
            event
            for payload in payloads[1:]
            for event in normalizer.consume(payload)
        ]

        self.assertEqual(
            [event.event_type for event in events],
            [
                "lifecycle",
                "lifecycle",
                "content_delta",
                "content_delta",
                "usage",
                "done",
            ],
        )
        self.assertEqual(events[0].payload["step_type"], "user_input")
        self.assertEqual(events[0].payload["state"], "DONE")
        self.assertEqual(events[1].payload["step_type"], "unknown")
        self.assertEqual(events[2].payload, {"text": "probe-ok"})
        self.assertEqual(events[3].payload, {"text": "\n"})
        self.assertEqual(events[4].payload, {
            "input_tokens": 7169,
            "output_tokens": 166,
            "thinking_tokens": 163,
            "cache_read_tokens": 0,
            "total_tokens": 7335,
        })
        self.assertEqual(events[5].payload, {"finish_reason": "stop"})

    def test_multi_step_usage_aggregation_within_turn(self) -> None:
        """AC-01: Multiple distinct steps with usage are aggregated per-field."""
        normalizer = AgyTurnNormalizer("sess-multi-step")
        normalizer.consume({
            "event": "step_update",
            "step_update": {
                "conversation_id": "sess-multi-step",
                "step_index": 2,
                "step_type": "agent_response",
                "state": "DONE",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
        })
        normalizer.consume({
            "event": "step_update",
            "step_update": {
                "conversation_id": "sess-multi-step",
                "step_index": 4,
                "step_type": "agent_response",
                "state": "DONE",
                "usage": {"input_tokens": 50, "output_tokens": 20, "thinking_tokens": 15, "total_tokens": 70},
            },
        })
        events = normalizer.consume({
            "event": "result",
            "result": {
                "conversation_id": "sess-multi-step",
                "status": "SUCCESS",
                "usage": {"input_tokens": 99999, "total_tokens": 99999},  # Cumulative, must NOT overwrite
            },
        })
        usage_events = [e for e in events if e.event_type == "usage"]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(
            usage_events[0].payload,
            {
                "input_tokens": 150,
                "output_tokens": 70,
                "thinking_tokens": 15,
                "total_tokens": 220,
            },
        )

    def test_same_step_usage_deduplication_updates_snapshot(self) -> None:
        """AC-02: Same step_index updated multiple times updates snapshot without duplicate counting."""
        normalizer = AgyTurnNormalizer("sess-same-step")
        normalizer.consume({
            "event": "step_update",
            "step_update": {
                "conversation_id": "sess-same-step",
                "step_index": 2,
                "step_type": "agent_response",
                "state": "ACTIVE",
            },
        })
        normalizer.consume({
            "event": "step_update",
            "step_update": {
                "conversation_id": "sess-same-step",
                "step_index": 2,
                "step_type": "agent_response",
                "state": "DONE",
                "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130},
            },
        })
        # Second update on the same step_index (e.g. final enrichment)
        normalizer.consume({
            "event": "step_update",
            "step_update": {
                "conversation_id": "sess-same-step",
                "step_index": 2,
                "step_type": "agent_response",
                "state": "DONE",
                "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
            },
        })
        events = normalizer.consume({
            "event": "result",
            "result": {"conversation_id": "sess-same-step", "status": "SUCCESS"},
        })
        usage_events = [e for e in events if e.event_type == "usage"]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(
            usage_events[0].payload,
            {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
        )

    def test_cumulative_result_usage_does_not_overwrite_turn_usage(self) -> None:
        """AC-03: Cumulative result.usage does not overwrite step truth in later turns."""
        normalizer = AgyTurnNormalizer("sess-turn-2")
        normalizer.consume({
            "event": "step_update",
            "step_update": {
                "conversation_id": "sess-turn-2",
                "step_index": 1,
                "step_type": "agent_response",
                "state": "DONE",
                "usage": {
                    "input_tokens": 278,
                    "output_tokens": 4,
                    "cache_read_tokens": 30214,
                    "total_tokens": 282,
                },
            },
        })
        events = normalizer.consume({
            "event": "result",
            "result": {
                "conversation_id": "sess-turn-2",
                "status": "SUCCESS",
                "num_turns": 2,
                "usage": {
                    "input_tokens": 30662,
                    "output_tokens": 8,
                    "total_tokens": 30670,
                },
            },
        })
        usage_events = [e for e in events if e.event_type == "usage"]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(
            usage_events[0].payload,
            {
                "input_tokens": 278,
                "output_tokens": 4,
                "cache_read_tokens": 30214,
                "total_tokens": 282,
            },
        )

    def test_no_step_usage_means_no_canonical_usage_event(self) -> None:
        """AC-04: If no step has usage, no canonical usage event is emitted even if result has usage."""
        normalizer = AgyTurnNormalizer("sess-no-step-usage")
        normalizer.consume({
            "event": "step_update",
            "step_update": {
                "conversation_id": "sess-no-step-usage",
                "step_index": 1,
                "step_type": "agent_response",
                "state": "DONE",
                "text_delta": "hello",
            },
        })
        events = normalizer.consume({
            "event": "result",
            "result": {
                "conversation_id": "sess-no-step-usage",
                "status": "SUCCESS",
                "usage": {"input_tokens": 5000},
            },
        })
        usage_events = [e for e in events if e.event_type == "usage"]
        self.assertEqual(len(usage_events), 0)

    def test_result_usage_malformed_is_still_rejected(self) -> None:
        """AC-05: result.usage is still validated and rejected if malformed."""
        normalizer = AgyTurnNormalizer("sess-malformed-result")
        with self.assertRaises(ProviderAdapterError) as caught:
            normalizer.consume({
                "event": "result",
                "result": {
                    "conversation_id": "sess-malformed-result",
                    "status": "SUCCESS",
                    "usage": {"input_tokens": "not-an-int"},
                },
            })
        self.assertEqual(caught.exception.code, "agy_malformed_usage")


if __name__ == "__main__":
    unittest.main()
