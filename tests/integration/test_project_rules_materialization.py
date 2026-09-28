"""CP3 (B-prime): project-rules durable materialization, healing and identity."""

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


RULES_CANARY = "RULES-CANARY-9f31c7"
RULES_BODY = (
    "# Project Rules (test)\n\n"
    f"Always answer with {RULES_CANARY}.\n"
)


class ProjectRulesMaterializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_path = self.root / "runtime.sqlite3"
        self.data_root = self.root / "providers"
        self.evidence_path = self.root / "fixture-evidence.jsonl"
        memory_server_marker = (
            self.root / "engines" / "mcp" / "servers" / "memory" / "server.py"
        )
        memory_server_marker.parent.mkdir(parents=True)
        memory_server_marker.write_text("# test Memory MCP marker\n", encoding="utf-8")
        self.binding_id = uuid4()
        self.services = []

    async def asyncTearDown(self) -> None:
        for service in reversed(self.services):
            try:
                await service.shutdown()
            except ProviderAdapterError:
                pass
        self.temp.cleanup()

    def build_service(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fake_agy.py"
        environment = {
            "FAKE_AGY_SCENARIO": "normal",
            "FAKE_AGY_EVIDENCE": str(self.evidence_path),
            "FAKE_AGY_CONVERSATION": "11111111-2222-3333-4444-555555555555",
            "FAKE_AGY_RELEASE_TAIL": str(self.root / "tail-release"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        }
        process_config = AgyProcessConfig(
            command_prefix=(sys.executable, str(fixture)),
            init_timeout_seconds=0.5,
            idle_timeout_seconds=0.5,
            hard_timeout_seconds=3,
            close_timeout_seconds=0.5,
            result_settle_seconds=0.05,
            require_official_executable=False,
            environment_overrides=environment,
        )
        adapter = AntigravityAdapter(
            self.data_root,
            AgyProcessSupervisor(process_config),
            memory_mcp_root=self.root,
            mailbox_ttl_seconds=30,
        )
        service = RuntimeService(
            RuntimeStateStore(self.state_path),
            {"antigravity": adapter},
        )
        self.services.append(service)
        return service

    def spec(self, project_rules=None) -> GenerationSpec:
        return GenerationSpec(
            runtime_kind="antigravity",
            bootstrap_fingerprint="bootstrap-rules-1",
            system_instructions="SYSTEM-INSTRUCTIONS-BODY",
            project_rules=project_rules,
        )

    def generation_root(self) -> Path:
        return next(path for path in Path(self.data_root).iterdir() if path.is_dir())

    def turn(self, *, bootstrap=None, message="CURRENT-USER-CANARY") -> TurnRequest:
        return TurnRequest(
            request_id=uuid4(),
            user_message=message,
            requested_model_id="gemini-3.1-pro-preview",
            requested_thinking_level="auto",
            runtime_mcp_tools=({"name": "memory_search", "eager": True, "max_call_seconds": None},),
            bootstrap_context=bootstrap,
        )

    async def run_turn(self, service, *, bootstrap=None, message="CURRENT-USER-CANARY"):
        request = self.turn(bootstrap=bootstrap, message=message)
        return [event async for event in service.stream_turn(self.binding_id, request)]

    async def assert_turn_fails(self, service, *, message, expected_code):
        """Fatal prepare failures surface as one terminal error event."""

        events = await self.run_turn(service, message=message)
        self.assertEqual([event.event_type for event in events], ["error"])
        self.assertEqual(events[-1].payload.get("code"), expected_code)
        return events

    async def test_rules_present_materializes_canonical_and_tool_mirror(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        await self.run_turn(service, bootstrap={"continuity_anchor": "rules"})

        root = self.generation_root()
        canonical = root / "control" / "canonical_rules.md"
        mirror = root / "workspace" / "AGENTS.md"
        self.assertEqual(canonical.read_text(encoding="utf-8"), RULES_BODY)
        self.assertEqual(mirror.read_text(encoding="utf-8"), RULES_BODY)
        # The model context comes from the agent definition, not from the mirror.
        agent = (
            root / "profile" / ".gemini" / "config" / "agents"
            / f"exocore-runtime-{str(self.binding_id).replace('-', '')}" / "agent.md"
        )
        agent_markdown = agent.read_text(encoding="utf-8")
        self.assertIn("## ExoCore Project Rules", agent_markdown)
        self.assertIn(RULES_CANARY, agent_markdown)
        metadata = json.loads((root / "generation.json").read_text(encoding="utf-8"))
        self.assertIs(metadata["project_rules_present"], True)

    async def test_rules_absent_materializes_neither_canonical_nor_mirror(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(None))
        await self.run_turn(service, bootstrap={"continuity_anchor": "norules"})

        root = self.generation_root()
        self.assertFalse((root / "control" / "canonical_rules.md").exists())
        self.assertFalse((root / "workspace" / "AGENTS.md").exists())
        metadata = json.loads((root / "generation.json").read_text(encoding="utf-8"))
        self.assertIs(metadata["project_rules_present"], False)

    async def test_empty_rules_present_materialize_an_empty_mirror(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(""))
        await self.run_turn(service, bootstrap={"continuity_anchor": "empty"})

        root = self.generation_root()
        self.assertEqual((root / "control" / "canonical_rules.md").read_text(), "")
        self.assertEqual((root / "workspace" / "AGENTS.md").read_text(), "")

    async def test_mirror_tamper_and_delete_heal_without_touching_siblings(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        await self.run_turn(service, bootstrap={"continuity_anchor": "heal"})

        root = self.generation_root()
        workspace = root / "workspace"
        mirror = workspace / "AGENTS.md"
        sibling = workspace / "sibling.txt"
        sibling.write_text("sibling stays\n", encoding="utf-8")

        mirror.write_text("tampered rules\n", encoding="utf-8")
        await self.run_turn(service, message="CURRENT-USER-CANARY-2")
        self.assertEqual(mirror.read_text(encoding="utf-8"), RULES_BODY)

        mirror.unlink()
        await self.run_turn(service, message="CURRENT-USER-CANARY-3")
        self.assertEqual(mirror.read_text(encoding="utf-8"), RULES_BODY)
        self.assertEqual(sibling.read_text(encoding="utf-8"), "sibling stays\n")

    async def test_canonical_tamper_fails_closed(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        await self.run_turn(service, bootstrap={"continuity_anchor": "tamper"})

        canonical = self.generation_root() / "control" / "canonical_rules.md"
        canonical.write_text("tampered canonical\n", encoding="utf-8")
        await self.assert_turn_fails(
            service,
            message="CURRENT-USER-CANARY-2",
            expected_code="agy_custom_agent_invalid",
        )

    async def test_canonical_deletion_fails_closed(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        await self.run_turn(service, bootstrap={"continuity_anchor": "delete"})

        (self.generation_root() / "control" / "canonical_rules.md").unlink()
        await self.assert_turn_fails(
            service,
            message="CURRENT-USER-CANARY-2",
            expected_code="agy_control_backing_missing",
        )

    async def test_restart_reacquire_preserves_rules_artifacts(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        await self.run_turn(service, bootstrap={"continuity_anchor": "restart"})
        await service.shutdown()

        restarted = self.build_service()
        await self.run_turn(restarted, message="CURRENT-USER-CANARY-2")
        root = self.generation_root()
        self.assertEqual(
            (root / "control" / "canonical_rules.md").read_text(encoding="utf-8"),
            RULES_BODY,
        )
        self.assertEqual(
            (root / "workspace" / "AGENTS.md").read_text(encoding="utf-8"),
            RULES_BODY,
        )

    async def test_retire_removes_rules_artifacts_with_the_generation_root(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        await self.run_turn(service, bootstrap={"continuity_anchor": "retire"})
        root = self.generation_root()
        self.assertTrue((root / "control" / "canonical_rules.md").is_file())

        await service.retire(str(self.binding_id), reason="test_retire")
        self.assertFalse(root.exists())

    async def test_rules_body_never_reaches_the_journal_or_sqlite(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        request = self.turn(bootstrap={"continuity_anchor": "journal"})
        events = [
            event async for event in service.stream_turn(self.binding_id, request)
        ]
        durable = service.store.read_events(str(self.binding_id), str(request.request_id))
        self.assertEqual(len(durable), len(events))
        for event in durable:
            self.assertNotIn(RULES_CANARY, json.dumps(event.payload))
        database_bytes = b"".join(path.read_bytes() for path in self.root.glob("runtime.sqlite3*"))
        self.assertNotIn(RULES_CANARY.encode("utf-8"), database_bytes)

    async def test_missing_metadata_rebuild_verifies_rules_identity(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        await self.run_turn(service, bootstrap={"continuity_anchor": "rebuild"})

        root = self.generation_root()
        metadata_path = root / "generation.json"
        original = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata_path.unlink()

        await self.run_turn(service, message="CURRENT-USER-CANARY-2")
        rebuilt = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(rebuilt["identity_hash"], original["identity_hash"])
        self.assertEqual(rebuilt["project_rules_sha256"], original["project_rules_sha256"])
        self.assertIs(rebuilt["project_rules_present"], True)

    async def test_missing_metadata_rebuild_fails_closed_on_rules_mismatch(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        await self.run_turn(service, bootstrap={"continuity_anchor": "rebuild-fail"})

        root = self.generation_root()
        (root / "generation.json").unlink()
        (root / "control" / "canonical_rules.md").write_text(
            "different rules\n", encoding="utf-8"
        )
        await self.assert_turn_fails(
            service,
            message="CURRENT-USER-CANARY-2",
            expected_code="agy_generation_artifact_missing",
        )


if __name__ == "__main__":
    unittest.main()


class LegacyRulesFreeGenerationTests(ProjectRulesMaterializationTests):
    """R1-03: pre-CP3 rules-free generations survive the CP3 upgrade."""

    async def test_legacy_rules_free_generation_upgrades_without_rotation(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(None))
        await self.run_turn(service, message="LEGACY-TURN-1")

        root = self.generation_root()
        metadata_path = root / "generation.json"
        original = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertIs(original["project_rules_present"], False)
        self.assertNotIn("project_rules_sha256", original)
        self.assertFalse((root / "control" / "canonical_rules.md").exists())

        # Exactly the pre-CP3 metadata shape: no project-rules keys at all.
        legacy = {
            key: value
            for key, value in original.items()
            if key not in ("project_rules_present", "project_rules_sha256")
        }
        metadata_path.write_text(json.dumps(legacy), encoding="utf-8")

        # A restart is the real CP3 upgrade shape: the new process has no
        # prepared session and must load layout from the legacy metadata.
        restarted = self.build_service()
        await restarted.ensure_generation(self.binding_id, self.spec(None))
        await self.run_turn(restarted, message="LEGACY-TURN-2")

        normalized = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(normalized["identity_hash"], original["identity_hash"])
        self.assertEqual(normalized["generation_id"], original["generation_id"])
        self.assertEqual(
            normalized["agent_markdown_sha256"], original["agent_markdown_sha256"]
        )
        self.assertIs(normalized["project_rules_present"], False)
        self.assertNotIn("project_rules_sha256", normalized)
        # A rules-free generation never grows a rules mirror.
        self.assertFalse((root / "workspace" / "AGENTS.md").exists())

    async def test_legacy_metadata_cannot_masquerade_as_rules_free(self) -> None:
        service = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec(RULES_BODY))
        await self.run_turn(service, bootstrap={"continuity_anchor": "legacy-masquerade"})

        metadata_path = self.generation_root() / "generation.json"
        original = json.loads(metadata_path.read_text(encoding="utf-8"))
        stripped = {
            key: value
            for key, value in original.items()
            if key not in ("project_rules_present", "project_rules_sha256")
        }
        metadata_path.write_text(json.dumps(stripped), encoding="utf-8")

        restarted = self.build_service()
        await self.assert_turn_fails(
            restarted,
            message="MASQUERADE",
            expected_code="agy_generation_artifact_invalid",
        )
