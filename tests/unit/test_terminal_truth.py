from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from pydantic import ValidationError

from exocore_runtime.contracts import (
    GenerationSpec,
    ProviderEvent,
    ProviderGeneration,
    RuntimeEvent,
    TurnRequest,
)
from exocore_runtime.errors import ConflictError, ProviderAdapterError
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


class BootstrapBoundaryAdapter:
    def __init__(self, failure_boundary: str) -> None:
        self.failure_boundary = failure_boundary

    async def ensure_generation(self, binding_id, spec):
        return ProviderGeneration(
            provider_session_id=f"session-{binding_id}",
            observed_model=spec.provider_model_id,
        )

    async def prepare_turn(self, binding_id, request, **kwargs):
        if self.failure_boundary == "prepare":
            raise ProviderAdapterError("prepare_failed")

    async def stream_turn(self, binding_id, request):
        if self.failure_boundary == "invalid_status":
            raise ProviderAdapterError("invalid_status", terminal_status="completed")
        yield ProviderEvent(
            event_type="error",
            payload={"code": "post_send_failed"},
            terminal_status="failed",
        )

    async def cancel(self, binding_id, request_id):
        return None

    async def retire(self, binding_id, reason):
        return None

    async def shutdown(self):
        return None


class RuntimeEventContractTests(unittest.TestCase):
    def event_data(self):
        return {
            "binding_id": uuid4(),
            "request_id": uuid4(),
            "sequence": 1,
            "event_type": "error",
            "payload": {},
        }

    def test_terminal_status_matrix_and_nonterminal_null_contract(self) -> None:
        for status in ("completed", "failed", "cancelled", "indeterminate"):
            with self.subTest(status=status):
                event = RuntimeEvent(
                    **self.event_data(),
                    terminal=True,
                    terminal_status=status,
                    bootstrap_consumed=False,
                )
                self.assertEqual(event.terminal_status, status)
                self.assertIs(event.bootstrap_consumed, False)

        nonterminal = RuntimeEvent(
            **{**self.event_data(), "event_type": "content_delta"},
        )
        self.assertIsNone(nonterminal.terminal_status)
        self.assertIsNone(nonterminal.bootstrap_consumed)

    def test_incomplete_wrong_or_nonterminal_truth_fails_closed(self) -> None:
        cases = (
            {"terminal": True},
            {"terminal": True, "terminal_status": "failed"},
            {"terminal": True, "bootstrap_consumed": False},
            {
                "terminal": True,
                "terminal_status": "unknown",
                "bootstrap_consumed": False,
            },
            {
                "terminal": True,
                "terminal_status": "failed",
                "bootstrap_consumed": "false",
            },
            {"terminal_status": "failed", "bootstrap_consumed": False},
            {"terminal_status": None, "bootstrap_consumed": False},
        )
        for truth in cases:
            with self.subTest(truth=truth), self.assertRaises(ValidationError):
                RuntimeEvent(**self.event_data(), **truth)


class RuntimeStateTerminalTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.store = RuntimeStateStore(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def active_generation(self):
        binding_id = str(uuid4())
        spec = GenerationSpec(
            provider_model_id="fake-model",
            bootstrap_fingerprint="bootstrap",
            config_fingerprint="config",
        )
        self.store.ensure_generation(binding_id, spec)
        self.store.activate_generation(binding_id, f"session-{binding_id}")
        return binding_id

    def claim(self, binding_id):
        request_id = str(uuid4())
        self.store.claim_request(binding_id, request_id, "a" * 64, "owner")
        return request_id

    def test_persisted_terminal_status_matrix_and_nonterminal_nulls(self) -> None:
        for status in ("completed", "failed", "cancelled", "indeterminate"):
            with self.subTest(status=status):
                binding_id = self.active_generation()
                request_id = self.claim(binding_id)
                self.store.mark_sent(binding_id, request_id, "owner")
                nonterminal = self.store.append_event(
                    binding_id, request_id, "content_delta", {"text": status}
                )
                event, changed = self.store.append_terminal(
                    binding_id,
                    request_id,
                    "done" if status == "completed" else "error",
                    {"code": status},
                    status,
                    status,
                )
                replay = self.store.read_events(binding_id, request_id)
                self.assertTrue(changed)
                self.assertIsNone(nonterminal.terminal_status)
                self.assertIsNone(nonterminal.bootstrap_consumed)
                self.assertEqual(replay[-1], event)
                self.assertEqual(replay[-1].terminal_status, status)
                self.assertIs(replay[-1].bootstrap_consumed, False)

    def test_duplicate_terminal_preserves_original_terminal_time_truth(self) -> None:
        binding_id = self.active_generation()
        first_request = self.claim(binding_id)
        original, changed = self.store.append_terminal(
            binding_id,
            first_request,
            "error",
            {"code": "prepare_failed"},
            "failed",
            "prepare_failed",
        )
        self.assertTrue(changed)
        self.assertIs(original.bootstrap_consumed, False)

        second_request = self.claim(binding_id)
        self.store.mark_sent(
            binding_id,
            second_request,
            "owner",
            consume_bootstrap=True,
        )
        self.store.append_terminal(
            binding_id,
            second_request,
            "done",
            {},
            "completed",
            "completed",
        )
        duplicate, duplicate_changed = self.store.append_terminal(
            binding_id,
            first_request,
            "done",
            {"code": "replacement"},
            "completed",
            "replacement",
        )
        self.assertFalse(duplicate_changed)
        self.assertEqual(duplicate, original)
        self.assertEqual(duplicate.terminal_status, "failed")
        self.assertIs(duplicate.bootstrap_consumed, False)

    def test_retire_shutdown_and_restart_use_durable_bootstrap_truth(self) -> None:
        retired_binding = self.active_generation()
        retired_request = self.claim(retired_binding)
        self.store.mark_sent(retired_binding, retired_request, "owner", consume_bootstrap=True)
        self.store.retire_generation(retired_binding, "done")
        retired = self.store.read_events(retired_binding, retired_request)[-1]
        self.assertEqual((retired.terminal_status, retired.bootstrap_consumed), ("indeterminate", True))

        prepared_binding = self.active_generation()
        prepared_request = self.claim(prepared_binding)
        sent_binding = self.active_generation()
        sent_request = self.claim(sent_binding)
        self.store.mark_sent(sent_binding, sent_request, "owner", consume_bootstrap=True)

        self.assertEqual(self.store.terminalize_open_requests_for_shutdown(), 2)
        prepared = self.store.read_events(prepared_binding, prepared_request)[-1]
        sent = self.store.read_events(sent_binding, sent_request)[-1]
        self.assertEqual((prepared.terminal_status, prepared.bootstrap_consumed), ("failed", False))
        self.assertEqual((sent.terminal_status, sent.bootstrap_consumed), ("indeterminate", True))

        restart_binding = self.active_generation()
        restart_request = self.claim(restart_binding)
        self.store.mark_sent(restart_binding, restart_request, "owner", consume_bootstrap=True)
        self.assertEqual(self.store.recover_after_restart(), 1)
        first_projection = self.store.read_events(restart_binding, restart_request)[-1]
        reopened = RuntimeStateStore(self.path)
        second_projection = reopened.read_events(restart_binding, restart_request)[-1]
        self.assertEqual(first_projection.model_dump_json(), second_projection.model_dump_json())
        self.assertEqual(
            (second_projection.terminal_status, second_projection.bootstrap_consumed),
            ("indeterminate", True),
        )


class LegacyEventSchemaUpgradeTests(unittest.TestCase):
    def test_upgrade_is_idempotent_and_does_not_backfill_legacy_terminal_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.sqlite3"
            binding_id = str(uuid4())
            request_id = str(uuid4())
            connection = sqlite3.connect(path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE generations (
                        binding_id TEXT PRIMARY KEY,
                        identity_hash TEXT NOT NULL,
                        runtime_kind TEXT NOT NULL,
                        provider_model_id TEXT NOT NULL,
                        bootstrap_fingerprint TEXT NOT NULL,
                        config_fingerprint TEXT NOT NULL,
                        provider_session_id TEXT,
                        bootstrap_sent INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        retired_reason TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE requests (
                        binding_id TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        owner_id TEXT,
                        last_sequence INTEGER NOT NULL DEFAULT 0,
                        terminal_code TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(binding_id, request_id)
                    );
                    CREATE TABLE events (
                        binding_id TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        is_terminal INTEGER NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(binding_id, request_id, sequence)
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO generations(
                        binding_id, identity_hash, runtime_kind, provider_model_id,
                        bootstrap_fingerprint, config_fingerprint, bootstrap_sent, status
                    ) VALUES (?, ?, 'fake', 'fake-model', 'bootstrap', 'config', 1, 'active')
                    """,
                    (binding_id, "a" * 64),
                )
                connection.execute(
                    """
                    INSERT INTO requests(
                        binding_id, request_id, payload_hash, status, last_sequence, terminal_code
                    ) VALUES (?, ?, ?, 'completed', 1, 'completed')
                    """,
                    (binding_id, request_id, "b" * 64),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        binding_id, request_id, sequence, event_type, payload_json, is_terminal
                    ) VALUES (?, ?, 1, 'done', '{}', 1)
                    """,
                    (binding_id, request_id),
                )
                connection.commit()
            finally:
                connection.close()

            store = RuntimeStateStore(path)
            RuntimeStateStore(path)
            connection = sqlite3.connect(path)
            try:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(events)").fetchall()
                }
                truth = connection.execute(
                    "SELECT terminal_status, bootstrap_consumed FROM events"
                ).fetchone()
            finally:
                connection.close()
            self.assertTrue({"terminal_status", "bootstrap_consumed"}.issubset(columns))
            self.assertEqual(truth, (None, None))
            with self.assertRaisesRegex(ConflictError, "legacy terminal event lacks durable truth"):
                store.read_events(binding_id, request_id)


class ProviderBoundaryProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failures_keep_terminal_and_bootstrap_truth(self) -> None:
        cases = (
            ("prepare", "failed", False),
            ("stream", "failed", True),
            ("invalid_status", "indeterminate", True),
        )
        for boundary, expected_status, expected_consumed in cases:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp_dir:
                adapter = BootstrapBoundaryAdapter(boundary)
                service = RuntimeService(
                    RuntimeStateStore(Path(temp_dir) / "runtime.sqlite3"),
                    {"antigravity": adapter},
                )
                binding_id = uuid4()
                spec = GenerationSpec(
                    runtime_kind="antigravity",
                    provider_model_id="gemini-3.1-pro-high",
                    bootstrap_fingerprint="bootstrap",
                    config_fingerprint="config",
                    system_instructions="system",
                )
                await service.ensure_generation(binding_id, spec)
                request = TurnRequest(
                    request_id=uuid4(),
                    user_message="hello",
                    bootstrap_context={"history": []},
                )
                events = await collect(service, binding_id, request)
                terminal = events[-1]
                self.assertEqual(terminal.terminal_status, expected_status)
                self.assertIs(terminal.bootstrap_consumed, expected_consumed)
                replay = await collect(service, binding_id, request)
                self.assertEqual(
                    [event.model_dump_json() for event in events],
                    [event.model_dump_json() for event in replay],
                )


if __name__ == "__main__":
    unittest.main()
