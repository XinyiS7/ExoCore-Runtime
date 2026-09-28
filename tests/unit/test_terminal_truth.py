from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from pydantic import ValidationError

from exocore_runtime.contracts import (
    EffectiveResolution,
    GenerationSpec,
    ProcessExecutionOptions,
    ProviderEvent,
    ProviderGeneration,
    RuntimeEvent,
    TurnRequest,
)
from exocore_runtime.errors import ProviderAdapterError, StateResetRequiredError
from exocore_runtime.providers.base import (
    ProviderCancelOutcome,
    ProviderCancelReceipt,
)
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


def store_resolution() -> EffectiveResolution:
    options = ProcessExecutionOptions(
        provider_model_slug="fake-model-high",
        effort="high",
        security_policy_revision="fake-security-v1",
        launch_environment_revision="fake-launch-v1",
    )
    return EffectiveResolution(
        provider_model_slug=options.provider_model_slug,
        effort=options.effort,
        resolver_policy_revision="fake-policy-v1",
        process_options=options,
    )


class BootstrapBoundaryAdapter:
    """Adapter whose protocol failures terminate at prepare or after send.

    The legacy v1-era "invalid_status" boundary is structurally impossible in
    v2: ProviderAdapterError refuses terminal_status values outside
    {"failed", "indeterminate"} at construction time. The two remaining
    boundaries cover fail-before-send (bootstrap not consumed) and
    fail-after-send (bootstrap consumed) durable truth.
    """
    def __init__(self, failure_boundary: str) -> None:
        self.failure_boundary = failure_boundary

    def resolve_execution(
        self,
        requested_model_id: str,
        requested_thinking_level: str,
    ) -> EffectiveResolution:
        return store_resolution()

    def stage_generation(self, binding_id: str, spec: GenerationSpec) -> None:
        return None

    async def stage_attachment(
        self,
        binding_id,
        request_id,
        artifact_id,
        data,
        *,
        guard,
    ) -> None:
        guard()

    async def discard_attachments(self, binding_id, request_id, *, guard) -> None:
        guard()

    async def prepare_turn(
        self,
        generation,
        request,
        options,
        *,
        is_first_turn: bool,
    ) -> ProviderGeneration:
        if self.failure_boundary == "prepare":
            raise ProviderAdapterError("prepare_failed")
        return ProviderGeneration(
            provider_session_id=f"session-{generation.binding_id}",
            observed_model=options.provider_model_slug,
            observed_effort=options.effort,
        )

    async def stream_turn(self, binding_id, request):
        if self.failure_boundary == "stream":
            raise ProviderAdapterError("post_send_failed")
        yield ProviderEvent(
            event_type="error",
            payload={"code": "post_send_failed"},
            terminal_status="failed",
        )

    async def cancel(self, binding_id, request_id):
        return ProviderCancelReceipt(ProviderCancelOutcome.OWNERSHIP_UNKNOWN)

    def reclaim_request(self, binding_id, request_id):
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
                    provider_input_effect=(
                        "may_have_reached_provider"
                        if status == "completed"
                        else "not_sent"
                    ),
                )
                self.assertEqual(event.terminal_status, status)
                self.assertIs(event.bootstrap_consumed, False)
                self.assertEqual(
                    event.provider_input_effect,
                    "may_have_reached_provider" if status == "completed" else "not_sent",
                )

        nonterminal = RuntimeEvent(
            **{**self.event_data(), "event_type": "content_delta"},
        )
        self.assertIsNone(nonterminal.terminal_status)
        self.assertIsNone(nonterminal.bootstrap_consumed)
        self.assertIsNone(nonterminal.provider_input_effect)

    def test_incomplete_wrong_or_nonterminal_truth_fails_closed(self) -> None:
        cases = (
            {"terminal": True},
            {"terminal": True, "terminal_status": "failed"},
            {"terminal": True, "bootstrap_consumed": False},
            {"terminal": True, "provider_input_effect": "not_sent"},
            {
                "terminal": True,
                "terminal_status": "failed",
                "bootstrap_consumed": False,
                "provider_input_effect": "consumed",
            },
            {
                "terminal": True,
                "terminal_status": "unknown",
                "bootstrap_consumed": False,
                "provider_input_effect": "not_sent",
            },
            {
                "terminal": True,
                "terminal_status": "failed",
                "bootstrap_consumed": "false",
                "provider_input_effect": "not_sent",
            },
            {
                "terminal": True,
                "terminal_status": "completed",
                "bootstrap_consumed": True,
                "provider_input_effect": "not_sent",
            },
            {"terminal_status": "failed", "bootstrap_consumed": False},
            {"terminal_status": None, "bootstrap_consumed": False},
            {
                "terminal_status": "failed",
                "bootstrap_consumed": False,
                "provider_input_effect": "not_sent",
            },
            {
                "provider_input_effect": None,
                "terminal_status": None,
                "bootstrap_consumed": False,
            },
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
            runtime_kind="fake",
            bootstrap_fingerprint="bootstrap",
            system_instructions="system",
        )
        self.store.ensure_generation(binding_id, spec)
        return binding_id

    def claim(self, binding_id):
        request_id = str(uuid4())
        self.store.claim_request(
            binding_id,
            request_id,
            "a" * 64,
            "gemini-3.1-pro-preview",
            "auto",
            "owner",
        )
        return request_id

    def claim_resolved(self, binding_id):
        request_id = self.claim(binding_id)
        self.store.freeze_resolution(binding_id, request_id, "owner", store_resolution())
        return request_id

    def mark_sent(self, binding_id, request_id, *, consume_bootstrap=False):
        self.store.activate_generation(binding_id, f"session-{binding_id}", request_id)
        self.store.mark_sent(
            binding_id,
            request_id,
            "owner",
            consume_bootstrap=consume_bootstrap,
        )

    def test_persisted_terminal_status_matrix_and_nonterminal_nulls(self) -> None:
        for status in ("completed", "failed", "cancelled", "indeterminate"):
            with self.subTest(status=status):
                binding_id = self.active_generation()
                request_id = self.claim_resolved(binding_id)
                self.mark_sent(binding_id, request_id, consume_bootstrap=True)
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
                self.assertIs(replay[-1].bootstrap_consumed, True)
                self.assertEqual(
                    replay[-1].provider_input_effect,
                    "may_have_reached_provider",
                )

    def test_persisted_provider_input_effect_is_two_state_durable_truth(self) -> None:
        prepared_binding = self.active_generation()
        prepared_request = self.claim_resolved(prepared_binding)
        not_sent, _ = self.store.append_terminal(
            prepared_binding,
            prepared_request,
            "error",
            {"code": "prepare_failed"},
            "failed",
            "prepare_failed",
        )
        self.assertEqual(not_sent.provider_input_effect, "not_sent")

        sent_binding = self.active_generation()
        sent_request = self.claim_resolved(sent_binding)
        self.mark_sent(sent_binding, sent_request, consume_bootstrap=True)
        may_have, _ = self.store.append_terminal(
            sent_binding,
            sent_request,
            "done",
            {"finish_reason": "stop"},
            "completed",
            "completed",
        )
        self.assertEqual(may_have.provider_input_effect, "may_have_reached_provider")
        replayed = self.store.read_events(sent_binding, sent_request)[-1]
        self.assertEqual(replayed.provider_input_effect, "may_have_reached_provider")


    def test_duplicate_terminal_preserves_original_terminal_time_truth(self) -> None:
        binding_id = self.active_generation()
        first_request = self.claim_resolved(binding_id)
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

        second_request = self.claim_resolved(binding_id)
        self.mark_sent(binding_id, second_request, consume_bootstrap=True)
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
        retired_request = self.claim_resolved(retired_binding)
        self.mark_sent(retired_binding, retired_request, consume_bootstrap=True)
        self.store.retire_generation(retired_binding, "done")
        retired = self.store.read_events(retired_binding, retired_request)[-1]
        self.assertEqual((retired.terminal_status, retired.bootstrap_consumed), ("indeterminate", True))

        prepared_binding = self.active_generation()
        prepared_request = self.claim_resolved(prepared_binding)
        sent_binding = self.active_generation()
        sent_request = self.claim_resolved(sent_binding)
        self.mark_sent(sent_binding, sent_request, consume_bootstrap=True)

        self.assertEqual(self.store.terminalize_open_requests_for_shutdown(), 2)
        prepared = self.store.read_events(prepared_binding, prepared_request)[-1]
        sent = self.store.read_events(sent_binding, sent_request)[-1]
        self.assertEqual((prepared.terminal_status, prepared.bootstrap_consumed), ("failed", False))
        self.assertEqual((sent.terminal_status, sent.bootstrap_consumed), ("indeterminate", True))
        self.assertEqual(prepared.provider_input_effect, "not_sent")
        self.assertEqual(sent.provider_input_effect, "may_have_reached_provider")

        restart_binding = self.active_generation()
        restart_request = self.claim_resolved(restart_binding)
        self.mark_sent(restart_binding, restart_request, consume_bootstrap=True)
        self.assertEqual(self.store.recover_after_restart(), 1)
        first_projection = self.store.read_events(restart_binding, restart_request)[-1]
        reopened = RuntimeStateStore(self.path)
        second_projection = reopened.read_events(restart_binding, restart_request)[-1]
        self.assertEqual(first_projection.model_dump_json(), second_projection.model_dump_json())
        self.assertEqual(
            (second_projection.terminal_status, second_projection.bootstrap_consumed),
            ("indeterminate", True),
        )
        self.assertEqual(
            second_projection.provider_input_effect,
            "may_have_reached_provider",
        )


class V1StoreResetGuardTests(unittest.TestCase):
    def test_v1_store_fails_loud_with_reset_required_and_is_never_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.sqlite3"
            binding_id = str(uuid4())
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
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            finally:
                connection.close()

            for attempt in (1, 2):
                with self.subTest(attempt=attempt), self.assertRaises(
                    StateResetRequiredError
                ) as caught:
                    RuntimeStateStore(path)
                self.assertEqual(caught.exception.code, "v2_state_reset_required")
            connection = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()
            # The v1 file must remain byte-for-byte untouched: no row migration,
            # no schema projection, no v2 markers.
            self.assertIn("generations", tables)
            self.assertNotIn("runtime_meta", tables)
            self.assertEqual(user_version, 1)


class ProviderBoundaryProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failures_keep_terminal_and_bootstrap_truth(self) -> None:
        cases = (
            ("prepare", "failed", False, "not_sent"),
            ("stream", "failed", True, "may_have_reached_provider"),
        )
        for boundary, expected_status, expected_consumed, expected_effect in cases:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp_dir:
                adapter = BootstrapBoundaryAdapter(boundary)
                service = RuntimeService(
                    RuntimeStateStore(Path(temp_dir) / "runtime.sqlite3"),
                    {"antigravity": adapter},
                )
                binding_id = uuid4()
                spec = GenerationSpec(
                    runtime_kind="antigravity",
                    bootstrap_fingerprint="bootstrap",
                    system_instructions="system",
                )
                await service.ensure_generation(binding_id, spec)
                request = TurnRequest(
                    request_id=uuid4(),
                    user_message="hello",
                    requested_model_id="gemini-3.1-pro-preview",
                    requested_thinking_level="auto",
                    runtime_mcp_tools=({"name": "memory_search", "eager": True, "max_call_seconds": None},),
                    bootstrap_context={"history": []},
                )
                events = await collect(service, binding_id, request)
                terminal = events[-1]
                self.assertEqual(terminal.terminal_status, expected_status)
                self.assertIs(terminal.bootstrap_consumed, expected_consumed)
                self.assertEqual(terminal.provider_input_effect, expected_effect)
                replay = await collect(service, binding_id, request)
                self.assertEqual(
                    [event.model_dump_json() for event in events],
                    [event.model_dump_json() for event in replay],
                )
                await service.shutdown()


if __name__ == "__main__":
    unittest.main()