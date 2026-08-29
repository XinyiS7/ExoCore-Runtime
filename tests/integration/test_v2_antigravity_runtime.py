import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from exocore_runtime.contracts import GenerationSpec, TurnRequest
from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.adapter import AntigravityAdapter
from exocore_runtime.providers.antigravity.process import AgyProcessConfig, AgyProcessSupervisor
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


class V2AntigravityRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_path = self.root / "runtime.sqlite3"
        self.data_root = self.root / "providers"
        self.evidence_path = self.root / "evidence.jsonl"
        self.binding_id = uuid4()
        self.spec = GenerationSpec(
            runtime_kind="antigravity",
            bootstrap_fingerprint="bootstrap",
            system_instructions="private system canary",
        )
        self.services = []

    async def asyncTearDown(self) -> None:
        for service in reversed(self.services):
            try:
                await service.shutdown()
            except ProviderAdapterError:
                pass
        self.temp.cleanup()

    def build_service(self, scenario="normal"):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fake_agy.py"
        config = AgyProcessConfig(
            command_prefix=(sys.executable, str(fixture)),
            init_timeout_seconds=0.5,
            idle_timeout_seconds=0.5,
            hard_timeout_seconds=3,
            close_timeout_seconds=0.5,
            result_settle_seconds=0.02,
            require_official_executable=False,
            environment_overrides={
                "FAKE_AGY_SCENARIO": scenario,
                "FAKE_AGY_EVIDENCE": str(self.evidence_path),
                "FAKE_AGY_CONVERSATION": "11111111-2222-3333-4444-555555555555",
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            },
        )
        adapter = AntigravityAdapter(
            self.data_root,
            AgyProcessSupervisor(config),
            mailbox_ttl_seconds=30,
        )
        service = RuntimeService(RuntimeStateStore(self.state_path), {"antigravity": adapter})
        self.services.append(service)
        return service, adapter

    def turn(self, *, thinking="auto", bootstrap=None, request_id=None):
        return TurnRequest(
            request_id=request_id or uuid4(),
            user_message="CURRENT-USER-CANARY",
            requested_model_id="gemini-3.1-pro-preview",
            requested_thinking_level=thinking,
            bootstrap_context=bootstrap,
            ephemeral_current="EPHEMERAL-CANARY",
        )

    def evidence(self):
        if not self.evidence_path.exists():
            return []
        return [json.loads(line) for line in self.evidence_path.read_text(encoding="utf-8").splitlines()]

    async def test_unsupported_resolution_is_durable_and_precedes_any_spawn(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        unsupported = self.turn(thinking="medium", bootstrap={"history": []})
        events = await collect(service, self.binding_id, unsupported)
        self.assertEqual(events[-1].payload, {"code": "unsupported_requested_execution"})
        record = service.store.get_request(str(self.binding_id), str(unsupported.request_id))
        self.assertEqual(record.resolution_status, "unsupported")
        self.assertIsNone(record.effective_provider_model_slug)
        self.assertFalse(any(item["kind"] == "spawn" for item in self.evidence()))

    async def test_state_only_put_then_atomic_activation_controls_and_strict_respawn(self) -> None:
        service, _ = self.build_service()
        result = await service.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(result.status, "starting")
        self.assertFalse(any(item["kind"] == "spawn" for item in self.evidence()))

        first = self.turn(bootstrap={"history": []})
        first_events = await collect(service, self.binding_id, first)
        self.assertEqual(
            [event.event_type for event in first_events[:2]],
            ["generation_activated", "execution_resolved"],
        )
        self.assertTrue(
            all("step_type" not in event.payload and "state" not in event.payload for event in first_events[:2])
        )
        generation = service.store.get_generation(str(self.binding_id))
        metadata_path = self.data_root / self.binding_id.hex / "generation.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["provider_session_id"], generation.provider_session_id)

        second = self.turn(thinking="high")
        await collect(service, self.binding_id, second)
        spawns = [item for item in self.evidence() if item["kind"] == "spawn"]
        self.assertEqual(len(spawns), 1)

        third = self.turn(thinking="low")
        await collect(service, self.binding_id, third)
        spawns = [item for item in self.evidence() if item["kind"] == "spawn"]
        self.assertEqual(len(spawns), 2)
        self.assertIn("--effort", spawns[-1]["argv"])
        self.assertIn("low", spawns[-1]["argv"])
        self.assertIn("--conversation", spawns[-1]["argv"])
        index = spawns[-1]["argv"].index("--conversation")
        self.assertEqual(spawns[-1]["argv"][index + 1], generation.provider_session_id)
        self.assertEqual(
            service.store.get_generation(str(self.binding_id)).provider_session_id,
            generation.provider_session_id,
        )

    async def test_artifact_id_sqlite_null_is_adopted_without_fresh_replacement(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        turn = self.turn(bootstrap={"history": []})
        payload_hash = service._request_hash(turn)
        service.store.claim_request(
            str(self.binding_id),
            str(turn.request_id),
            payload_hash,
            turn.requested_model_id,
            turn.requested_thinking_level,
            service.instance_id,
        )
        resolution = adapter.resolve_execution(turn.requested_model_id, turn.requested_thinking_level)
        service.store.freeze_resolution(
            str(self.binding_id), str(turn.request_id), service.instance_id, resolution
        )
        acquired = await adapter.prepare_turn(
            service.store.get_generation(str(self.binding_id)),
            turn,
            resolution.process_options,
            is_first_turn=True,
        )
        self.assertIsNone(service.store.get_generation(str(self.binding_id)).provider_session_id)
        await adapter.shutdown()

        restarted, _ = self.build_service()
        events = await collect(restarted, self.binding_id, turn)
        self.assertEqual(events[0].event_type, "generation_activated")
        self.assertEqual(events[0].payload["provider_session_id"], acquired.provider_session_id)
        self.assertEqual(
            restarted.store.get_generation(str(self.binding_id)).provider_session_id,
            acquired.provider_session_id,
        )
        spawns = [item for item in self.evidence() if item["kind"] == "spawn"]
        self.assertIn("--conversation", spawns[-1]["argv"])

    async def test_sqlite_id_restores_missing_artifact_session_and_conflict_fails_before_stdin(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        session_id = service.store.get_generation(str(self.binding_id)).provider_session_id
        await service.shutdown()
        metadata_path = self.data_root / self.binding_id.hex / "generation.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["provider_session_id"] = None
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        restarted, _ = self.build_service()
        restored_turn = self.turn(thinking="low")
        restored = await collect(restarted, self.binding_id, restored_turn)
        self.assertEqual(restored[-1].terminal_status, "completed")
        restored_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(restored_metadata["provider_session_id"], session_id)
        await restarted.shutdown()

        conflict = json.loads(metadata_path.read_text(encoding="utf-8"))
        conflict["provider_session_id"] = "different-session"
        metadata_path.write_text(json.dumps(conflict), encoding="utf-8")
        conflicted, _ = self.build_service()
        before_turns = len([item for item in self.evidence() if item["kind"] == "turn"])
        events = await collect(conflicted, self.binding_id, self.turn())
        self.assertEqual(events[-1].payload, {"code": "agy_artifact_session_conflict"})
        after_turns = len([item for item in self.evidence() if item["kind"] == "turn"])
        self.assertEqual(after_turns, before_turns)

    async def test_both_session_sources_empty_allow_a_new_request_to_retry_fresh(self) -> None:
        failing, failing_adapter = self.build_service("init_timeout")
        await failing.ensure_generation(self.binding_id, self.spec)
        failed = await collect(failing, self.binding_id, self.turn(bootstrap={"history": []}))
        self.assertEqual(failed[-1].payload, {"code": "agy_init_timeout"})
        generation = failing.store.get_generation(str(self.binding_id))
        self.assertEqual((generation.status, generation.provider_session_id), ("starting", None))
        metadata_path = self.data_root / self.binding_id.hex / "generation.json"
        self.assertIsNone(json.loads(metadata_path.read_text(encoding="utf-8"))["provider_session_id"])
        await failing_adapter.shutdown()

        retried, _ = self.build_service()
        events = await collect(retried, self.binding_id, self.turn(bootstrap={"history": []}))
        self.assertEqual(events[0].event_type, "generation_activated")
        self.assertEqual(events[-1].terminal_status, "completed")
        self.assertIsNotNone(retried.store.get_generation(str(self.binding_id)).provider_session_id)

    async def test_known_sqlite_session_rebuilds_fully_missing_metadata(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        session_id = service.store.get_generation(str(self.binding_id)).provider_session_id
        await service.shutdown()
        metadata_path = self.data_root / self.binding_id.hex / "generation.json"
        metadata_path.unlink()

        restarted, _ = self.build_service()
        events = await collect(restarted, self.binding_id, self.turn(thinking="low"))
        self.assertEqual(events[-1].terminal_status, "completed")
        rebuilt = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(rebuilt["provider_session_id"], session_id)
        spawn = [item for item in self.evidence() if item["kind"] == "spawn"][-1]
        index = spawn["argv"].index("--conversation")
        self.assertEqual(spawn["argv"][index + 1], session_id)

    async def test_known_resume_mismatch_is_stable_and_never_fresh(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        session_id = service.store.get_generation(str(self.binding_id)).provider_session_id
        await service.shutdown()

        mismatched, _ = self.build_service("resume_mismatch")
        events = await collect(mismatched, self.binding_id, self.turn(thinking="low"))
        self.assertEqual(events[-1].payload, {"code": "resume_identity_mismatch"})
        self.assertEqual(
            mismatched.store.get_generation(str(self.binding_id)).provider_session_id,
            session_id,
        )
        spawn = [item for item in self.evidence() if item["kind"] == "spawn"][-1]
        self.assertIn("--conversation", spawn["argv"])
        index = spawn["argv"].index("--conversation")
        self.assertEqual(spawn["argv"][index + 1], session_id)


if __name__ == "__main__":
    unittest.main()
