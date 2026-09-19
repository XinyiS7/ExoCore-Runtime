"""Authenticated legacy agent-declaration upgrade through the production adapter.

Frozen authority: ``Plan/Archived/Subscription_Runtime_AGY_Legacy_Tool_Declaration_Upgrade_Plan.md``
sections 2 and 4 plus the LU-01..LU-16 matrix. The historical frontmatter
templates are spelled out literally here so the recognizer is never validated
against its own renderer.
"""

import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from exocore_runtime.contracts import GenerationSpec, TurnRequest
from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.adapter import AntigravityAdapter
from exocore_runtime.providers.antigravity.control import (
    PROJECT_RULES_ARTIFACT,
    CanonicalControlStore,
)
from exocore_runtime.providers.antigravity.process import (
    AgyProcessConfig,
    AgyProcessSupervisor,
)
from exocore_runtime.providers.antigravity.renderer import (
    generation_agent_name,
    render_agent_markdown,
)
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


ADAPTER_LOGGER = "exocore_runtime.providers.antigravity.adapter"
DESCRIPTION_LINE = "description: ExoCore generation-private subscription runtime agent."

# The two frozen historical declarations (plan section 4). Literal by design.
L3_TEMPLATE = (
    "---\n"
    "name: {agent_name}\n"
    f"{DESCRIPTION_LINE}\n"
    "tools:\n"
    "  - view_file\n"
    "  - write_to_file\n"
    "  - run_command\n"
    "---\n"
)
L0_TEMPLATE = (
    "---\n"
    "name: {agent_name}\n"
    f"{DESCRIPTION_LINE}\n"
    "---\n"
)
CURRENT_FRONTMATTER = [
    "tools:",
    "  - view_file",
    "  - write_to_file",
    "  - run_command",
    "  - search_web",
]
L3_SHAPE_LABEL = "tools:view_file,write_to_file,run_command"


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


class LegacyDeclarationUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
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
        self.agent_name = generation_agent_name(str(self.binding_id))
        self.system_canary = "LEGACY-UPGRADE-SYSTEM-CANARY"
        self.spec = GenerationSpec(
            runtime_kind="antigravity",
            bootstrap_fingerprint="bootstrap-1",
            system_instructions=self.system_canary,
        )
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
        return service, adapter

    # --- helpers -----------------------------------------------------------

    def turn(self, thinking: str = "auto", bootstrap: bool = False) -> TurnRequest:
        return TurnRequest(
            request_id=uuid4(),
            user_message="legacy declaration upgrade turn",
            requested_model_id="gemini-3.1-pro-preview",
            requested_thinking_level=thinking,
            bootstrap_context={"history": []} if bootstrap else None,
        )

    def evidence(self):
        if not self.evidence_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.evidence_path.read_text(encoding="utf-8").splitlines()
        ]

    def generation_root(self) -> Path:
        return next(self.data_root.iterdir())

    def agent_path(self, root: Path) -> Path:
        return next((root / "profile" / ".gemini" / "config" / "agents").glob("*/agent.md"))

    def metadata_path(self, root: Path) -> Path:
        return root / "generation.json"

    def session_id(self, service):
        return service.store.get_generation(str(self.binding_id)).provider_session_id

    def frontmatter(self, root: Path) -> list[str]:
        return self.agent_path(root).read_text(encoding="utf-8").split("---\n")[1].splitlines()

    def write_frontmatter(self, root: Path, frontmatter: str) -> bytes:
        """Re-shape the artifact while keeping metadata anchored to its bytes."""

        agent = self.agent_path(root)
        body = agent.read_text(encoding="utf-8").split("---\n", 2)[2]
        pinned = (frontmatter.format(agent_name=self.agent_name) + body).encode("utf-8")
        agent.write_bytes(pinned)
        metadata_file = self.metadata_path(root)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata["agent_markdown_sha256"] = hashlib.sha256(pinned).hexdigest()
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        return pinned

    def write_legacy_artifact(
        self,
        root: Path,
        *,
        template: str = L3_TEMPLATE,
        drop_rules_keys: bool = False,
    ) -> bytes:
        """Leave the artifact exactly as the previous runtime would have.

        The frozen frontmatter is combined with the real body the production
        renderer produced (instructions, optional rules section, transport
        trailer), and the recorded hash is re-pinned to those bytes, which is
        what a generation staged under the old declaration would carry.
        """

        pinned = self.write_frontmatter(root, template)
        if not drop_rules_keys:
            return pinned
        metadata_file = self.metadata_path(root)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata.pop("project_rules_present", None)
        metadata.pop("project_rules_sha256", None)
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        return pinned

    def isolate(self, label: str) -> None:
        """Give one subtest its own state, data root, evidence and binding."""

        self.state_path = self.root / f"state-{label}.sqlite3"
        self.data_root = self.root / f"providers-{label}"
        self.evidence_path = self.root / f"evidence-{label}.jsonl"
        self.binding_id = uuid4()
        self.agent_name = generation_agent_name(str(self.binding_id))

    async def activate(self, spec: GenerationSpec | None = None):
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, spec or self.spec)
        first = await collect(service, self.binding_id, self.turn(bootstrap=True))
        self.assertEqual(first[-1].event_type, "done")
        return service, adapter

    # --- upgrade paths -----------------------------------------------------

    async def test_active_three_tool_legacy_generation_upgrades_and_resumes_same_session(self) -> None:
        service, _ = await self.activate()
        session = self.session_id(service)
        root = self.generation_root()
        self.write_legacy_artifact(root)
        self.assertEqual(self.frontmatter(root)[2:], ["tools:", "  - view_file", "  - write_to_file", "  - run_command"])

        with self.assertLogs(ADAPTER_LOGGER, level="INFO") as captured:
            events = await collect(service, self.binding_id, self.turn(thinking="low"))

        self.assertEqual(events[-1].event_type, "done")
        frontmatter = self.frontmatter(root)
        self.assertEqual(frontmatter[0], f"name: {self.agent_name}")
        self.assertEqual(frontmatter[1], DESCRIPTION_LINE)
        self.assertEqual(frontmatter[2:], CURRENT_FRONTMATTER)
        metadata = json.loads(self.metadata_path(root).read_text(encoding="utf-8"))
        self.assertEqual(
            metadata["agent_markdown_sha256"],
            hashlib.sha256(self.agent_path(root).read_bytes()).hexdigest(),
        )
        # Continuity: same provider session, strict resume, no bootstrap resend.
        self.assertEqual(self.session_id(service), session)
        spawns = [item for item in self.evidence() if item["kind"] == "spawn"]
        self.assertEqual(len(spawns), 2)
        argv = spawns[-1]["argv"]
        self.assertEqual(argv[argv.index("--conversation") + 1], session)
        turns = [item for item in self.evidence() if item["kind"] == "turn"]
        self.assertTrue(turns[0]["bootstrap_present"])
        self.assertFalse(turns[-1]["bootstrap_present"])
        # Exactly one upgrade line, and only after convergence.
        self.assertEqual(len(captured.output), 1)
        self.assertIn("declaration upgraded", captured.output[0])
        self.assertIn(L3_SHAPE_LABEL, captured.output[0])
        self.assertIn(str(self.binding_id), captured.output[0])

    async def test_active_pre_cp2_generation_upgrades_with_rules_normalization(self) -> None:
        service, _ = await self.activate()
        session = self.session_id(service)
        root = self.generation_root()
        self.write_legacy_artifact(root, template=L0_TEMPLATE, drop_rules_keys=True)
        self.assertEqual(self.frontmatter(root), [f"name: {self.agent_name}", DESCRIPTION_LINE])

        with self.assertLogs(ADAPTER_LOGGER, level="INFO") as captured:
            events = await collect(service, self.binding_id, self.turn(thinking="high"))

        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(self.frontmatter(root)[2:], CURRENT_FRONTMATTER)
        metadata = json.loads(self.metadata_path(root).read_text(encoding="utf-8"))
        self.assertIs(metadata["project_rules_present"], False)
        self.assertEqual(
            metadata["agent_markdown_sha256"],
            hashlib.sha256(self.agent_path(root).read_bytes()).hexdigest(),
        )
        self.assertEqual(self.session_id(service), session)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("pre-cp2-no-tools-block", captured.output[0])

    async def test_staged_legacy_generation_upgrades_on_ensure(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        root = self.generation_root()
        self.write_legacy_artifact(root)
        self.assertFalse(any(item["kind"] == "spawn" for item in self.evidence()))

        with self.assertLogs(ADAPTER_LOGGER, level="INFO") as captured:
            await service.ensure_generation(self.binding_id, self.spec)

        self.assertEqual(self.frontmatter(root)[2:], CURRENT_FRONTMATTER)
        metadata = json.loads(self.metadata_path(root).read_text(encoding="utf-8"))
        self.assertEqual(
            metadata["agent_markdown_sha256"],
            hashlib.sha256(self.agent_path(root).read_bytes()).hexdigest(),
        )
        self.assertEqual(len(captured.output), 1)
        # The upgraded staging stays a state-only operation.
        self.assertFalse(any(item["kind"] == "spawn" for item in self.evidence()))

    # --- fail-closed negatives --------------------------------------------

    async def test_washed_out_legacy_file_without_the_hash_anchor_stays_fatal(self) -> None:
        service, _ = await self.activate()
        root = self.generation_root()
        agent = self.agent_path(root)
        metadata_file = self.metadata_path(root)
        original_metadata = metadata_file.read_bytes()
        # An attacker rewrites the file into a historical shape but cannot make
        # the recorded hash agree with it: this must never be laundered.
        body = agent.read_text(encoding="utf-8").split("---\n", 2)[2]
        washed = (L3_TEMPLATE.format(agent_name=self.agent_name) + body).encode("utf-8")
        agent.write_bytes(washed)

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            events = await collect(service, self.binding_id, self.turn())

        self.assertEqual(events[-1].payload, {"code": "agy_custom_agent_invalid"})
        self.assertEqual(agent.read_bytes(), washed)
        self.assertEqual(metadata_file.read_bytes(), original_metadata)

    async def test_unknown_legacy_shapes_stay_fatal_even_with_a_matching_anchor(self) -> None:
        cases = {
            "empty-tools-key": (
                "---\n"
                "name: {agent_name}\n"
                f"{DESCRIPTION_LINE}\n"
                "tools:\n"
                "---\n"
            ),
            "reordered-tools": (
                "---\n"
                "name: {agent_name}\n"
                f"{DESCRIPTION_LINE}\n"
                "tools:\n"
                "  - run_command\n"
                "  - view_file\n"
                "  - write_to_file\n"
                "---\n"
            ),
            "extra-frontmatter-key": (
                "---\n"
                "name: {agent_name}\n"
                f"{DESCRIPTION_LINE}\n"
                "model: something\n"
                "tools:\n"
                "  - view_file\n"
                "  - write_to_file\n"
                "  - run_command\n"
                "---\n"
            ),
            "rewritten-description": (
                "---\n"
                "name: {agent_name}\n"
                "description: ExoCore generation-private subscription runtime agent\n"
                "tools:\n"
                "  - view_file\n"
                "  - write_to_file\n"
                "  - run_command\n"
                "---\n"
            ),
        }
        for label, frontmatter in cases.items():
            with self.subTest(shape=label):
                self.isolate(label)
                service, _ = await self.activate()
                root = self.generation_root()
                pinned = self.write_frontmatter(root, frontmatter)
                metadata_file = self.metadata_path(root)
                anchored_metadata = metadata_file.read_bytes()

                with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
                    events = await collect(service, self.binding_id, self.turn())

                self.assertEqual(events[-1].payload, {"code": "agy_custom_agent_invalid"})
                self.assertEqual(self.agent_path(root).read_bytes(), pinned)
                self.assertEqual(metadata_file.read_bytes(), anchored_metadata)

    async def test_tampered_instructions_inside_a_legacy_shape_stay_fatal(self) -> None:
        service, _ = await self.activate()
        root = self.generation_root()
        agent = self.agent_path(root)
        body = agent.read_text(encoding="utf-8").split("---\n", 2)[2]
        tampered = (
            L3_TEMPLATE.format(agent_name=self.agent_name)
            + body.replace(self.system_canary, self.system_canary + " TAMPERED", 1)
        ).encode("utf-8")
        agent.write_bytes(tampered)
        metadata_file = self.metadata_path(root)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata["agent_markdown_sha256"] = hashlib.sha256(tampered).hexdigest()
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        anchored_metadata = metadata_file.read_bytes()

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            events = await collect(service, self.binding_id, self.turn())

        self.assertEqual(events[-1].payload, {"code": "agy_custom_agent_invalid"})
        self.assertEqual(agent.read_bytes(), tampered)
        self.assertEqual(metadata_file.read_bytes(), anchored_metadata)

    async def test_pre_cp2_shape_is_rejected_for_a_rules_owning_generation(self) -> None:
        spec = self.spec.model_copy(update={"project_rules": "PROJECT-RULES-CANARY"})
        service, _ = await self.activate(spec=spec)
        root = self.generation_root()
        pinned = self.write_legacy_artifact(root, template=L0_TEMPLATE)
        metadata_file = self.metadata_path(root)
        anchored_metadata = metadata_file.read_bytes()

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            events = await collect(service, self.binding_id, self.turn())

        self.assertEqual(events[-1].payload, {"code": "agy_custom_agent_invalid"})
        self.assertEqual(self.agent_path(root).read_bytes(), pinned)
        self.assertEqual(metadata_file.read_bytes(), anchored_metadata)

    async def test_pre_cp2_shape_is_rejected_when_canonical_rules_backing_exists(self) -> None:
        service, _ = await self.activate()
        root = self.generation_root()
        CanonicalControlStore(root).write_canonical(
            PROJECT_RULES_ARTIFACT.canonical_name,
            "STRAY-CANONICAL-RULES",
        )
        pinned = self.write_legacy_artifact(root, template=L0_TEMPLATE)
        metadata_file = self.metadata_path(root)
        anchored_metadata = metadata_file.read_bytes()

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            events = await collect(service, self.binding_id, self.turn())

        self.assertEqual(events[-1].payload, {"code": "agy_custom_agent_invalid"})
        self.assertEqual(self.agent_path(root).read_bytes(), pinned)
        self.assertEqual(metadata_file.read_bytes(), anchored_metadata)

    async def test_missing_metadata_is_never_upgraded(self) -> None:
        service, _ = await self.activate()
        root = self.generation_root()
        pinned = self.write_legacy_artifact(root)
        self.metadata_path(root).unlink()

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            events = await collect(service, self.binding_id, self.turn())

        self.assertEqual(events[-1].payload, {"code": "agy_generation_artifact_missing"})
        self.assertEqual(self.agent_path(root).read_bytes(), pinned)
        self.assertFalse(self.metadata_path(root).exists())

    async def test_staged_identity_mismatch_never_partially_rewrites_the_hash(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        root = self.generation_root()
        self.write_legacy_artifact(root)
        metadata_file = self.metadata_path(root)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata["bootstrap_fingerprint"] = "different-bootstrap"
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        tampered_metadata = metadata_file.read_bytes()
        before = self.frontmatter(root)

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            with self.assertRaises(ProviderAdapterError) as caught:
                await service.ensure_generation(self.binding_id, self.spec)

        self.assertEqual(caught.exception.code, "agy_artifact_identity_mismatch")
        self.assertEqual(metadata_file.read_bytes(), tampered_metadata)
        self.assertEqual(self.frontmatter(root), before)

    # --- ordering probes (R2 recheck) --------------------------------------

    async def test_staged_pre_cp2_with_wrong_identity_never_rewrites_metadata(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        root = self.generation_root()
        self.write_legacy_artifact(root, template=L0_TEMPLATE, drop_rules_keys=True)
        metadata_file = self.metadata_path(root)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        # An identity field the legacy-rules guard cannot see, so only the strict
        # expected-fields comparison rejects this state.
        metadata["runtime_kind"] = "foreign-runtime"
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        tampered = metadata_file.read_bytes()
        agent_bytes = self.agent_path(root).read_bytes()

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            with self.assertRaises(ProviderAdapterError) as caught:
                await service.ensure_generation(self.binding_id, self.spec)

        self.assertEqual(caught.exception.code, "agy_artifact_identity_mismatch")
        # The pre-CP2 normalization must not have been persisted on the way to
        # the fatal: metadata and declaration stay byte-unchanged.
        self.assertEqual(metadata_file.read_bytes(), tampered)
        self.assertEqual(self.agent_path(root).read_bytes(), agent_bytes)

    async def test_active_pre_cp2_with_wrong_identity_never_rewrites_metadata(self) -> None:
        service, _ = await self.activate()
        root = self.generation_root()
        self.write_legacy_artifact(root, template=L0_TEMPLATE, drop_rules_keys=True)
        metadata_file = self.metadata_path(root)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata["bootstrap_fingerprint"] = "different-bootstrap"
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        tampered = metadata_file.read_bytes()
        agent_bytes = self.agent_path(root).read_bytes()

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            events = await collect(service, self.binding_id, self.turn())

        self.assertEqual(events[-1].payload, {"code": "agy_artifact_identity_mismatch"})
        self.assertEqual(metadata_file.read_bytes(), tampered)
        self.assertEqual(self.agent_path(root).read_bytes(), agent_bytes)

    async def test_rules_owning_generation_cannot_upgrade_as_anchored_rules_free(self) -> None:
        for label, drop_backing in (("backing-present", False), ("backing-removed", True)):
            with self.subTest(disguise=label):
                self.isolate(label)
                spec = self.spec.model_copy(update={"project_rules": "PROJECT-RULES-CANARY"})
                service, _ = await self.activate(spec=spec)
                root = self.generation_root()
                # The disguise: metadata declares rules-free explicitly, and the
                # anchored declaration is a rules-free legacy shape carrying the
                # real instructions.
                if drop_backing:
                    (root / "control" / PROJECT_RULES_ARTIFACT.canonical_name).unlink()
                rules_free_body = render_agent_markdown(
                    self.agent_name, self.system_canary, None
                ).split("---\n", 2)[2]
                pinned = (
                    L3_TEMPLATE.format(agent_name=self.agent_name) + rules_free_body
                ).encode("utf-8")
                agent = self.agent_path(root)
                agent.write_bytes(pinned)
                metadata_file = self.metadata_path(root)
                metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
                metadata["project_rules_present"] = False
                metadata.pop("project_rules_sha256", None)
                metadata["agent_markdown_sha256"] = hashlib.sha256(pinned).hexdigest()
                metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
                tampered = metadata_file.read_bytes()

                with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
                    events = await collect(service, self.binding_id, self.turn())

                # The rules-derived identity is authenticated before the commit
                # phase, so neither the declaration nor the metadata may have
                # been upgraded on the way to the fatal.
                self.assertEqual(
                    events[-1].payload, {"code": "agy_artifact_identity_mismatch"}
                )
                self.assertEqual(agent.read_bytes(), pinned)
                self.assertEqual(metadata_file.read_bytes(), tampered)

    async def test_session_backfill_never_lands_before_identity_authentication(self) -> None:
        service, _ = await self.activate()
        root = self.generation_root()
        metadata_file = self.metadata_path(root)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        self.assertIsNotNone(metadata["provider_session_id"])
        metadata["provider_session_id"] = None
        metadata["bootstrap_fingerprint"] = "different-bootstrap"
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
        tampered = metadata_file.read_bytes()
        agent_bytes = self.agent_path(root).read_bytes()

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            events = await collect(service, self.binding_id, self.turn())

        self.assertEqual(events[-1].payload, {"code": "agy_artifact_identity_mismatch"})
        self.assertEqual(metadata_file.read_bytes(), tampered)
        self.assertEqual(self.agent_path(root).read_bytes(), agent_bytes)

    async def test_legitimate_backfill_and_rules_normalization_still_commit(self) -> None:
        service, _ = await self.activate()
        root = self.generation_root()
        metadata_file = self.metadata_path(root)
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        recorded_session = metadata["provider_session_id"]
        # A lost artifact session plus the pre-CP3 rules shape: both are legal
        # commit-phase operations and must still land on an authenticated state.
        metadata["provider_session_id"] = None
        metadata.pop("project_rules_present", None)
        metadata.pop("project_rules_sha256", None)
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")

        events = await collect(service, self.binding_id, self.turn())

        self.assertEqual(events[-1].event_type, "done")
        settled = json.loads(metadata_file.read_text(encoding="utf-8"))
        self.assertEqual(settled["provider_session_id"], recorded_session)
        self.assertIs(settled["project_rules_present"], False)
        self.assertNotIn("project_rules_sha256", settled)

    # --- idempotency -------------------------------------------------------

    async def test_current_policy_artifact_is_idempotent_and_silent(self) -> None:
        service, _ = await self.activate()
        root = self.generation_root()
        # Warm-up turn: the first post-activation prepare backfills the provider
        # session into generation.json once. Idempotency is asserted on the
        # settled artifact after that known one-time write.
        events = await collect(service, self.binding_id, self.turn(thinking="low"))
        self.assertEqual(events[-1].event_type, "done")
        agent_bytes = self.agent_path(root).read_bytes()
        metadata_file = self.metadata_path(root)
        metadata_bytes = metadata_file.read_bytes()

        with self.assertNoLogs(ADAPTER_LOGGER, level="INFO"):
            events = await collect(service, self.binding_id, self.turn(thinking="low"))

        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(self.agent_path(root).read_bytes(), agent_bytes)
        # ``_update_provider_session`` legitimately rewrites the metadata file
        # every turn; idempotency here means the content is byte-identical, so
        # no declaration hash, schema or upgrade action changed.
        self.assertEqual(metadata_file.read_bytes(), metadata_bytes)


if __name__ == "__main__":
    unittest.main()
