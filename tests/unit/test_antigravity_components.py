import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from exocore_runtime.contracts import GenerationSpec, TurnRequest
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
            provider_model_id="gemini-3.1-pro-high",
            bootstrap_fingerprint="bootstrap",
            config_fingerprint="config",
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

    def test_legacy_generation_identity_migration_scrubs_system_plaintext(self) -> None:
        database = self.root / "legacy-runtime.sqlite3"
        store = RuntimeStateStore(database)
        spec = GenerationSpec(
            provider_model_id="fake-model",
            bootstrap_fingerprint="bootstrap",
            config_fingerprint="config",
        )
        store.ensure_generation(self.binding_id, spec)
        canary = "legacy-system-plaintext-private-canary"
        legacy = spec.model_dump(mode="json")
        legacy["system_instructions"] = canary
        malformed_binding = str(uuid4())
        scalar_binding = str(uuid4())
        store.ensure_generation(
            malformed_binding,
            spec.model_copy(update={"config_fingerprint": "malformed-config"}),
        )
        store.ensure_generation(
            scalar_binding,
            spec.model_copy(update={"config_fingerprint": "scalar-config"}),
        )
        malformed_canary = "malformed-legacy-private-canary"
        scalar_canary = "scalar-legacy-private-canary"
        connection = sqlite3.connect(database)
        try:
            connection.executemany(
                "UPDATE generations SET identity_hash = ? WHERE binding_id = ?",
                (
                    (json.dumps(legacy), self.binding_id),
                    ("not-json-" + malformed_canary, malformed_binding),
                    (json.dumps(scalar_canary), scalar_binding),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        before = database.read_bytes()
        self.assertIn(canary.encode(), before)
        self.assertIn(malformed_canary.encode(), before)
        self.assertIn(scalar_canary.encode(), before)
        RuntimeStateStore(database)
        persisted = b"".join(
            path.read_bytes()
            for path in self.root.glob("legacy-runtime.sqlite3*")
        )
        self.assertNotIn(canary.encode(), persisted)
        self.assertNotIn(malformed_canary.encode(), persisted)
        self.assertNotIn(scalar_canary.encode(), persisted)

    def test_renderer_keeps_ephemeral_out_of_stdin_and_bootstrap_is_first_only(self) -> None:
        ephemeral = "ephemeral-render-canary"
        current = "current-user-once"
        request = TurnRequest(
            request_id=uuid4(),
            user_message=current,
            bootstrap_context={"historical_turns": [{"role": "assistant", "content": "prior"}]},
            ephemeral_current=ephemeral,
        )
        first = render_stdin_line(request, is_first_turn=True)
        payload = json.loads(first)
        content = payload["message"]["content"]
        envelope = json.loads(content)
        self.assertEqual(envelope["current_user_message"], current)
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
        )
        self.assertEqual(generation.provider_session_id, conversation_id)
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
        self.assertNotIn("view_file", serialized)
        self.assertEqual(
            events[3].payload,
            {
                "step_index": 4,
                "step_type": "tool",
                "state": "ERROR",
                "category": "provider_tool",
                "outcome": "tool_error",
            },
        )
        self.assertEqual(events[-2].payload, {"cache_read_tokens": 3, "input_tokens": 7})
        with self.assertRaises(ProviderAdapterError) as caught:
            normalizer.consume(
                {
                    "event": "result",
                    "result": {"conversation_id": conversation_id, "status": "SUCCESS"},
                }
            )
        self.assertEqual(caught.exception.code, "agy_event_after_result")

    def test_ndjson_rejects_unknown_states_and_never_projects_tool_identity(self) -> None:
        conversation_id = "provider-session-safe-projection"
        for step_type, state in (
            ("system_message", "PRIVATE-STATE-CANARY"),
            ("agent_response", "C:/PRIVATE/PATH/CANARY.txt"),
            ("tool", "DONE"),
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
                    "tool_name": "C:/PRIVATE/PATH/TOOL-CANARY.txt",
                },
            }
        )[0]
        serialized = event.model_dump_json()
        self.assertNotIn("TOOL-CANARY", serialized)
        self.assertNotIn("C:/PRIVATE", serialized)
        self.assertEqual(event.payload["category"], "provider_tool")
        self.assertEqual(event.payload["outcome"], "tool_error")

    def test_known_provider_session_does_not_claim_bootstrap_was_sent(self) -> None:
        spec = GenerationSpec(
            runtime_kind="antigravity",
            provider_model_id="gemini-3.1-pro-high",
            bootstrap_fingerprint="bootstrap",
            config_fingerprint="config",
            provider_session_id="known-provider-session",
            system_instructions="private system",
        )
        store = RuntimeStateStore(self.root / "known-session.sqlite3")
        record, created = store.ensure_generation(self.binding_id, spec)
        self.assertTrue(created)
        self.assertEqual(record.provider_session_id, "known-provider-session")
        self.assertFalse(record.bootstrap_sent)

    def test_init_model_mismatch_is_fatal_before_turn(self) -> None:
        with self.assertRaises(ProviderAdapterError) as caught:
            parse_init(
                {
                    "event": "init",
                    "conversation_id": "provider-session",
                    "init": {"model": "wrong-model"},
                },
                "gemini-3.1-pro-high",
            )
        self.assertEqual(caught.exception.code, "agy_model_mismatch")
        self.assertTrue(caught.exception.fatal_generation)


if __name__ == "__main__":
    unittest.main()
