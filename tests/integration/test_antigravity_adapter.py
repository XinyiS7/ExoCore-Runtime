import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

# Windows: helper processes must never open a visible console window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PNG_BYTES = b"\x89PNG\r\n\x1a\nattachment-pixels"
JPEG_BYTES = b"\xff\xd8\xffattachment-jpeg"

from exocore_runtime.contracts import (
    AttachmentManifest,
    GenerationSpec,
    RuntimeMcpTool,
    TurnRequest,
    runtime_mcp_manifest_sha256,
)
from exocore_runtime.errors import (
    AttachmentCapacityExceededError,
    ConflictError,
    ProviderAdapterError,
    RequestRegisteredError,
    RetiredError,
)
from exocore_runtime.providers.antigravity.adapter import AntigravityAdapter
from exocore_runtime.providers.antigravity.capabilities import SECURITY_POLICY_REVISION
from exocore_runtime.providers.antigravity.control import (
    CanonicalControlStore,
    ReservedControlArtifact,
)
from exocore_runtime.providers.antigravity.process import AgyProcessConfig, AgyProcessSupervisor
from exocore_runtime.providers.antigravity.renderer import (
    AGENT_TOOLS,
    ALLOW_POLICY,
    DENY_POLICY,
    MCP_SERVER_NAME,
    render_agent_markdown,
)
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


class AntigravityAdapterTests(unittest.IsolatedAsyncioTestCase):
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
        self.system_canary = "SYSTEM-INSTRUCTIONS-PRIVATE-CANARY"
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

    def build_service(
        self,
        scenario="normal",
        reserved_artifacts=None,
        memory_mcp_root=None,
    ):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fake_agy.py"
        environment = {
            "FAKE_AGY_SCENARIO": scenario,
            "FAKE_AGY_EVIDENCE": str(self.evidence_path),
            "FAKE_AGY_CONVERSATION": "11111111-2222-3333-4444-555555555555",
            "FAKE_AGY_RELEASE_TAIL": str(self.root / "tail-release"),
            "FAKE_AGY_QUOTA_MARKER": str(self.root / "quota-retry-marker"),
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
        adapter_kwargs = {
            "memory_mcp_root": memory_mcp_root or self.root,
            "mailbox_ttl_seconds": 30,
        }
        if reserved_artifacts is not None:
            adapter_kwargs["reserved_artifacts"] = reserved_artifacts
        adapter = AntigravityAdapter(
            self.data_root,
            AgyProcessSupervisor(process_config),
            **adapter_kwargs,
        )
        service = RuntimeService(
            RuntimeStateStore(self.state_path),
            {"antigravity": adapter},
        )
        self.services.append(service)
        return service, adapter

    def test_manifest_drives_config_and_timeout_preflight(self) -> None:
        _service, adapter = self.build_service()
        manifest = (
            RuntimeMcpTool(name="memory_search", eager=False),
            RuntimeMcpTool(name="send_voice_msg", eager=True),
        )
        config = adapter._expected_mcp_config(str(self.binding_id), manifest)
        server = config["mcpServers"][MCP_SERVER_NAME]
        self.assertEqual(
            server["enabledTools"], ["memory_search", "send_voice_msg"]
        )
        self.assertEqual(
            server["tools"], {"send_voice_msg": {"eager": True}}
        )
        self.assertNotIn("send_voice_msg", AGENT_TOOLS)

        adapter.stage_generation(str(self.binding_id), self.spec)
        generation_root = next(self.data_root.iterdir())
        config_path = (
            generation_root / "profile" / ".gemini" / "config" / "mcp_config.json"
        )
        adapter._atomic_write_json(config_path, config)
        adapter.stage_generation(str(self.binding_id), self.spec)
        self.assertEqual(json.loads(config_path.read_text(encoding="utf-8")), config)

        bounded = (RuntimeMcpTool(
            name="send_voice_msg", eager=True, max_call_seconds=1
        ),)
        options = adapter.resolve_execution(
            "gemini-3.1-pro-preview", "auto"
        ).process_options.model_copy(
            update={
                "mcp_manifest_sha256": runtime_mcp_manifest_sha256(bounded)
            }
        )
        with self.assertRaises(ProviderAdapterError) as caught:
            adapter._validate_mcp_manifest(bounded, options)
        self.assertEqual(caught.exception.code, "agy_mcp_tool_timeout_invalid")

    async def test_manifest_digest_mismatch_fails_before_config_or_process_mutation(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        generation = service.store.get_generation(str(self.binding_id))
        config_path = (
            next(self.data_root.iterdir())
            / "profile"
            / ".gemini"
            / "config"
            / "mcp_config.json"
        )
        original_config = config_path.read_bytes()
        base = adapter.resolve_execution(
            request.requested_model_id,
            request.requested_thinking_level,
        ).process_options

        for label, options in (
            ("missing", base),
            (
                "mismatched",
                base.model_copy(update={"mcp_manifest_sha256": "a" * 64}),
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ProviderAdapterError) as caught:
                    await adapter.prepare_turn(
                        generation,
                        request,
                        options,
                        is_first_turn=True,
                    )
                self.assertEqual(caught.exception.code, "agy_mcp_manifest_mismatch")
                self.assertEqual(config_path.read_bytes(), original_config)
                self.assertEqual(adapter.supervisor._sessions, {})
                self.assertEqual(
                    [item for item in self.evidence() if item["kind"] == "spawn"],
                    [],
                )
                self.assertEqual(adapter._requests, {})

    def test_memory_mcp_root_requires_server_marker(self) -> None:
        bad_root = self.root / "not-exocore"
        bad_root.mkdir()

        with self.assertRaisesRegex(ValueError, "engines/mcp/servers/memory/server.py"):
            self.build_service(memory_mcp_root=bad_root)

    def test_alternate_mcp_customization_sources_are_rejected(self) -> None:
        cases = (
            ("workspace-direct", "workspace", "mcp_config.json", False),
            ("workspace-plugin", "workspace", "plugins", True),
            ("profile-direct", "profile", "mcp_config.json", False),
            ("profile-plugin", "profile", "plugins", True),
        )
        for name, scope, leaf, is_directory in cases:
            with self.subTest(source=name):
                case_root = self.root / f"guard-{name}"
                profile = case_root / "profile"
                workspace = case_root / "workspace"
                profile.mkdir(parents=True)
                workspace.mkdir(parents=True)
                target = (profile if scope == "profile" else workspace) / ".agents" / leaf
                if is_directory:
                    target.mkdir(parents=True)
                else:
                    target.parent.mkdir(parents=True)
                    target.write_text("{}", encoding="utf-8")

                with self.assertRaises(ProviderAdapterError) as caught:
                    AntigravityAdapter._require_profile_only_mcp_control(profile, workspace)

                self.assertEqual(
                    caught.exception.code,
                    "agy_uncontrolled_mcp_source_forbidden",
                )

    @staticmethod
    def manifest() -> tuple[RuntimeMcpTool, ...]:
        return (RuntimeMcpTool(name="memory_search", eager=True),)

    def turn(self, *, thinking="auto", bootstrap=None, ephemeral=None, request_id=None):
        return TurnRequest(
            request_id=request_id or uuid4(),
            user_message="CURRENT-USER-CANARY",
            requested_model_id="gemini-3.1-pro-preview",
            requested_thinking_level=thinking,
            runtime_mcp_tools=self.manifest(),
            bootstrap_context=bootstrap,
            ephemeral_current=ephemeral,
        )

    def evidence(self):
        if not self.evidence_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.evidence_path.read_text(encoding="utf-8").splitlines()
        ]

    def session_id(self, service):
        return service.store.get_generation(str(self.binding_id)).provider_session_id

    def generation_root(self) -> Path:
        return self.data_root / self.binding_id.hex

    def request_attachment_dir(self, request_id) -> Path:
        return self.generation_root() / "workspace" / str(request_id) / "attachments"

    def attachment_manifest(self, data=PNG_BYTES, **overrides) -> AttachmentManifest:
        values = {
            "artifact_id": "att-7",
            "display_name": "photo.png",
            "mime_type": "image/png",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        values.update(overrides)
        return AttachmentManifest(**values)

    async def wait_for_claim_waiters(self, lock, expected: int) -> None:
        """Deterministic ordering: wait until N tasks are queued on the lock."""

        for _ in range(500):
            waiters = getattr(lock, "_waiters", None)
            if waiters is not None and len(waiters) >= expected:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"claim lock did not queue {expected} waiter(s)")

    async def test_first_same_process_restart_resume_and_retire(self) -> None:
        service, adapter = self.build_service()
        generation = await service.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(generation.status, "starting")
        self.assertFalse(any(item["kind"] == "spawn" for item in self.evidence()))
        first_session = None

        first = self.turn(
            bootstrap={"continuity_anchor": "ANCHOR-ONE"},
            ephemeral="EPHEMERAL-CANARY first-only",
        )
        first_events = await collect(service, self.binding_id, first)
        self.assertEqual(first_events[-1].event_type, "done")
        self.assertEqual(
            [event.event_type for event in first_events[:2]],
            ["generation_activated", "execution_resolved"],
        )
        self.assertEqual(
            self.session_id(service),
            "11111111-2222-3333-4444-555555555555",
        )
        first_session = self.session_id(service)
        self.assertTrue(service.store.get_generation(str(self.binding_id)).bootstrap_sent)

        second = self.turn()
        second_events = await collect(service, self.binding_id, second)
        self.assertEqual(second_events[-1].event_type, "done")
        replay = await collect(service, self.binding_id, second)
        self.assertEqual(second_events, replay)
        self.assertEqual(
            next(event for event in second_events if event.event_type == "usage").payload[
                "cache_read_tokens"
            ],
            50,
        )
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "spawn"]), 1)
        turns = [item for item in self.evidence() if item["kind"] == "turn"]
        self.assertEqual(len(turns), 2)
        self.assertTrue(turns[0]["bootstrap_present"])
        self.assertFalse(turns[1]["bootstrap_present"])
        self.assertEqual([item["current_user_occurrences"] for item in turns], [1, 1])
        self.assertEqual([item["ephemeral_in_stdin"] for item in turns], [False, False])

        generation_root = next(self.data_root.iterdir())
        settings = json.loads(
            (
                generation_root
                / "profile"
                / ".gemini"
                / "antigravity-cli"
                / "settings.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(settings["modelProvider"], "account_default")
        self.assertEqual(settings["permissions"]["deny"], list(DENY_POLICY))
        self.assertEqual(settings["permissions"]["allow"], list(ALLOW_POLICY))
        self.assertNotIn("mcp(*)", settings["permissions"]["deny"])
        self.assertEqual(ALLOW_POLICY, (f"mcp({MCP_SERVER_NAME}/*)",))
        self.assertIn("mcp(chrome_devtools/*)", settings["permissions"]["deny"])
        self.assertIn("mcp(chrome-devtools/*)", settings["permissions"]["deny"])
        mcp_config = json.loads(
            (
                generation_root / "profile" / ".gemini" / "config" / "mcp_config.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(list(mcp_config["mcpServers"]), [MCP_SERVER_NAME])
        server_config = mcp_config["mcpServers"][MCP_SERVER_NAME]
        self.assertEqual(server_config["command"], str(Path(sys.executable).resolve()))
        self.assertEqual(server_config["cwd"], str(self.root.resolve()))
        self.assertEqual(server_config["env"], {"PYTHONPATH": str(self.root.resolve())})
        self.assertEqual(
            server_config["args"],
            [
                "-m",
                "engines.mcp.servers.memory.server",
                "--runtime-binding-id",
                str(self.binding_id),
            ],
        )
        # The model-visible enumeration and the eager subset come from the single
        # Runtime MCP fact source; the entry carries no other field.
        self.assertEqual(server_config["enabledTools"], ["memory_search"])
        self.assertEqual(
            server_config["tools"],
            {"memory_search": {"eager": True}},
        )
        self.assertEqual(
            set(server_config),
            {"command", "args", "cwd", "env", "enabledTools", "tools"},
        )
        # CP2/CP5 plus native web, URL-read, and image-generation unlocks: the
        # declared workload tools stay in the custom-agent declaration; MCP is
        # exposed through its isolated profile server config + scoped allow.
        agent_markdown = (
            generation_root
            / "profile"
            / ".gemini"
            / "config"
            / "agents"
            / f"exocore-runtime-{str(self.binding_id).replace('-', '')}"
            / "agent.md"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            agent_markdown.split("---\n")[1].splitlines(),
            [
                f"name: exocore-runtime-{str(self.binding_id).replace('-', '')}",
                "description: ExoCore generation-private subscription runtime agent.",
                "tools:",
                "  - view_file",
                "  - write_to_file",
                "  - run_command",
                "  - search_web",
                "  - read_url_content",
                "  - generate_image",
            ],
        )
        hooks = json.loads(
            (
                generation_root / "profile" / ".gemini" / "config" / "hooks.json"
            ).read_text(encoding="utf-8")
        )
        hook_config = hooks["exocore-runtime-ephemeral"]
        self.assertEqual(set(hook_config), {"enabled", "PreInvocation"})
        # CP1 workspace continuity: the workspace directory survives turns as a
        # plain directory; this fixture scenario writes no files into it.
        self.assertTrue((generation_root / "workspace").is_dir())
        metadata = (generation_root / "generation.json").read_text(encoding="utf-8")
        self.assertNotIn(self.system_canary, metadata)
        database_bytes = b"".join(
            path.read_bytes() for path in self.root.glob("runtime.sqlite3*")
        )
        self.assertNotIn(self.system_canary.encode(), database_bytes)
        self.assertNotIn(b"EPHEMERAL-CANARY", database_bytes)
        receipt = (generation_root / "mailbox" / "receipt.json").read_bytes()
        self.assertNotIn(b"EPHEMERAL-CANARY", receipt)

        await service.shutdown()
        self.services.remove(service)
        restarted, restarted_adapter = self.build_service()
        resumed = await restarted.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(resumed.status, "active")
        self.assertEqual(resumed.provider_session_id, first_session)
        third = self.turn(thinking="low")
        third_events = await collect(restarted, self.binding_id, third)
        self.assertEqual(third_events[-1].event_type, "done")
        self.assertEqual(self.session_id(restarted), first_session)
        spawns = [item for item in self.evidence() if item["kind"] == "spawn"]
        self.assertEqual(len(spawns), 2)
        resume_argv = spawns[-1]["argv"]
        self.assertEqual(
            resume_argv,
            [
                "--agent",
                f"exocore-runtime-{str(self.binding_id).replace('-', '')}",
                "--model",
                "gemini-3.1-pro-low",
                "--effort",
                "low",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--print-timeout",
                "3s",
                "--dangerously-skip-permissions",
                "--conversation",
                first_session,
            ],
        )
        forbidden = {
            "--add-dir",
            "--mode",
            "--project",
            "--prompt",
            "-p",
        }
        self.assertTrue(forbidden.isdisjoint(resume_argv))
        # CP2: AGY runs unsandboxed with deny rules as the authority.
        self.assertNotIn("--sandbox", resume_argv)
        self.assertNotIn("CURRENT-USER-CANARY", " ".join(resume_argv))
        self.assertNotIn("EPHEMERAL-CANARY", " ".join(resume_argv))
        self.assertEqual(spawns[-1]["forbidden_env_present"], [])
        self.assertEqual(spawns[-1]["home"], spawns[-1]["userprofile"])

        retired = await restarted.retire(self.binding_id, "test retirement")
        self.assertTrue(retired.changed)
        self.assertFalse(generation_root.exists())
        historical = await collect(restarted, self.binding_id, second)
        self.assertEqual(historical, second_events)
        with self.assertRaises(Exception):
            await collect(
                restarted,
                self.binding_id,
                self.turn(),
            )
        self.assertEqual(restarted_adapter.supervisor.quota_snapshot, {"weekly": 84, "5h": 93})

    async def test_fake_request_control_is_rejected_and_later_bootstrap_has_no_row(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        with self.assertRaises(ValidationError):
            TurnRequest(
                request_id=uuid4(),
                user_message="bad",
                requested_model_id="gemini-3.1-pro-preview",
                requested_thinking_level="auto",
                runtime_mcp_tools=({"name": "memory_search", "eager": True, "max_call_seconds": None},),
                bootstrap_context={"history": []},
                behavior="exception",
            )
        first = self.turn(bootstrap={"history": []})
        await collect(service, self.binding_id, first)
        later_bootstrap = self.turn(bootstrap={"history": []})
        with self.assertRaises(ConflictError):
            service.preflight_turn(self.binding_id, later_bootstrap)
        self.assertIsNone(
            service.store.get_request(str(self.binding_id), str(later_bootstrap.request_id))
        )

    async def test_stale_presend_mailbox_fails_once_then_generation_remains_usable(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []}, ephemeral="STALE-PRIVATE-CANARY")
        mailbox = adapter._mailbox(str(self.binding_id))
        mailbox.prepare(str(request.request_id), request.ephemeral_current)
        pending = json.loads(mailbox.pending_path.read_text(encoding="utf-8"))
        pending["created_at"] = 0
        mailbox.pending_path.write_text(json.dumps(pending), encoding="utf-8")

        events = await collect(service, self.binding_id, request)
        self.assertEqual(events[-1].payload, {"code": "ephemeral_pending_stale"})
        self.assertEqual(
            service.store.get_request(str(self.binding_id), str(request.request_id)).status,
            "failed",
        )
        generation = service.store.get_generation(str(self.binding_id))
        self.assertEqual(generation.status, "starting")
        self.assertIsNone(generation.provider_session_id)
        self.assertFalse(generation.bootstrap_sent)
        self.assertFalse(mailbox.pending_path.exists())
        self.assertFalse((mailbox.root / "claimed.json").exists())

        next_request = self.turn(bootstrap={"history": []})
        next_events = await collect(service, self.binding_id, next_request)
        self.assertEqual(next_events[-1].event_type, "done")
        persisted = b"".join(
            path.read_bytes()
            for path in self.data_root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(b"STALE-PRIVATE-CANARY", persisted)

    async def test_identity_failure_fails_before_stdin_and_removes_pending_plaintext(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        mailbox = adapter._mailbox(str(self.binding_id))
        request = self.turn(bootstrap={"history": []}, ephemeral="ENSURE-IDENTITY-PRIVATE-CANARY")
        mailbox.prepare(str(request.request_id), request.ephemeral_current)
        identity_path = mailbox.root / "identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["generation_id"] = "tampered-generation"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")

        events = await collect(service, self.binding_id, request)
        self.assertEqual(events[-1].payload, {"code": "ephemeral_identity_mismatch"})
        generation = service.store.get_generation(str(self.binding_id))
        self.assertEqual(generation.status, "starting")
        self.assertIsNone(generation.provider_session_id)
        self.assertFalse(mailbox.pending_path.exists())
        self.assertFalse((mailbox.root / "claimed.json").exists())
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)
        persisted = b"".join(
            path.read_bytes()
            for path in self.data_root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(b"ENSURE-IDENTITY-PRIVATE-CANARY", persisted)

    async def test_mailbox_identity_failure_removes_existing_plaintext_and_closes_generation(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []}, ephemeral="IDENTITY-PRIVATE-CANARY")
        mailbox = adapter._mailbox(str(self.binding_id))
        mailbox.prepare(str(request.request_id), request.ephemeral_current)
        identity_path = mailbox.root / "identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["generation_id"] = "tampered-generation"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")

        events = await collect(service, self.binding_id, request)
        self.assertEqual(events[-1].payload, {"code": "ephemeral_identity_mismatch"})
        generation = service.store.get_generation(str(self.binding_id))
        self.assertEqual(generation.status, "starting")
        self.assertIsNone(generation.provider_session_id)
        self.assertFalse(mailbox.pending_path.exists())
        self.assertFalse((mailbox.root / "claimed.json").exists())
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)
        persisted = b"".join(
            path.read_bytes()
            for path in self.data_root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(b"IDENTITY-PRIVATE-CANARY", persisted)

    async def test_post_mailbox_presend_failure_removes_plaintext_and_closes_process(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []}, ephemeral="POST-WRITE-PRIVATE-CANARY")
        with patch(
            "exocore_runtime.providers.antigravity.adapter.render_stdin_line",
            side_effect=TypeError("fixture render failure"),
        ):
            events = await collect(service, self.binding_id, request)
        self.assertEqual(events[-1].payload, {"code": "agy_turn_prepare_failed"})
        generation = service.store.get_generation(str(self.binding_id))
        self.assertEqual(generation.status, "starting")
        self.assertIsNone(generation.provider_session_id)
        self.assertFalse(generation.bootstrap_sent)
        mailbox = adapter._mailbox(str(self.binding_id))
        self.assertFalse(mailbox.pending_path.exists())
        self.assertFalse((mailbox.root / "claimed.json").exists())
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)
        persisted = b"".join(
            path.read_bytes()
            for path in self.data_root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(b"POST-WRITE-PRIVATE-CANARY", persisted)

    async def test_stream_faults_have_safe_indeterminate_truth_and_one_terminal(self) -> None:
        for scenario, expected_code in (
            ("duplicate_result", "agy_duplicate_result"),
            ("event_after_result", "agy_event_after_result"),
            ("malformed_stream", "agy_malformed_ndjson"),
            ("unexpected_eof", "agy_unexpected_eof"),
            ("stderr", "agy_stderr"),
            ("no_receipt", "ephemeral_receipt_missing"),
        ):
            with self.subTest(scenario=scenario):
                binding_id = uuid4()
                scenario_root = self.root / scenario
                scenario_root.mkdir()
                original_state = self.state_path
                original_data = self.data_root
                original_evidence = self.evidence_path
                self.state_path = scenario_root / "runtime.sqlite3"
                self.data_root = scenario_root / "providers"
                self.evidence_path = scenario_root / "evidence.jsonl"
                service, _ = self.build_service(scenario)
                spec = self.spec.model_copy(
                    update={"bootstrap_fingerprint": f"bootstrap-{scenario}"}
                )
                await service.ensure_generation(binding_id, spec)
                request = self.turn(
                    bootstrap={"history": []},
                    ephemeral="EPHEMERAL-CANARY fault",
                )
                events = await collect(service, binding_id, request)
                durable = service.store.get_request(str(binding_id), str(request.request_id))
                self.assertEqual(events[-1].payload, {"code": expected_code})
                self.assertEqual(durable.status, "indeterminate")
                self.assertEqual(service.store.terminal_count(str(binding_id), str(request.request_id)), 1)
                persisted = b"".join(
                    path.read_bytes()
                    for path in scenario_root.rglob("*")
                    if path.is_file()
                )
                self.assertNotIn(b"EPHEMERAL-CANARY", persisted)
                self.state_path = original_state
                self.data_root = original_data
                self.evidence_path = original_evidence

    async def test_resume_restores_security_artifacts_before_resume_spawn(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        generation_root = next(self.data_root.iterdir())
        await service.shutdown()
        self.services.remove(service)
        profile = generation_root / "profile"
        hooks_path = profile / ".gemini" / "config" / "hooks.json"
        mcp_config_path = profile / ".gemini" / "config" / "mcp_config.json"
        settings_path = profile / ".gemini" / "antigravity-cli" / "settings.json"
        agent_path = next((profile / ".gemini" / "config" / "agents").glob("*/agent.md"))
        hooks_path.unlink()
        mcp_config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        settings_path.write_text(
            json.dumps({"modelProvider": "account_default", "permissions": {"deny": []}}),
            encoding="utf-8",
        )
        pollution = generation_root / "workspace" / "pollution.txt"
        pollution.write_text("workspace canary", encoding="utf-8")

        restarted, restarted_adapter = self.build_service()
        await restarted.ensure_generation(self.binding_id, self.spec)
        await collect(restarted, self.binding_id, self.turn(thinking="low"))
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "spawn"]), 2)
        self.assertEqual(
            self.session_id(restarted),
            "11111111-2222-3333-4444-555555555555",
        )
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(set(hooks), {"exocore-runtime-ephemeral"})
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["modelProvider"], "account_default")
        self.assertEqual(settings["permissions"]["deny"], list(DENY_POLICY))
        self.assertEqual(settings["permissions"]["allow"], list(ALLOW_POLICY))
        self.assertEqual(
            json.loads(mcp_config_path.read_text(encoding="utf-8")),
            restarted_adapter._expected_mcp_config(
                str(self.binding_id), self.manifest()
            ),
        )
        self.assertIn(self.system_canary, agent_path.read_text(encoding="utf-8"))
        self.assertEqual(pollution.read_text(encoding="utf-8"), "workspace canary")

    async def test_workspace_files_survive_repeated_ensure_stage_prepare_and_restart(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        generation_root = next(self.data_root.iterdir())
        self.assertTrue((generation_root / "control").is_dir())
        ordinary = generation_root / "workspace" / "notes" / "scratch.txt"
        ordinary.parent.mkdir()
        ordinary.write_text("turn one output", encoding="utf-8")

        await service.ensure_generation(self.binding_id, self.spec)
        adapter.stage_generation(str(self.binding_id), self.spec)
        adapter.stage_generation(str(self.binding_id), self.spec)
        first = await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        self.assertEqual(first[-1].event_type, "done")
        self.assertEqual(ordinary.read_text(encoding="utf-8"), "turn one output")

        await service.shutdown()
        self.services.remove(service)
        restarted, _ = self.build_service()
        resumed = await restarted.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(resumed.status, "active")
        second = await collect(restarted, self.binding_id, self.turn(thinking="low"))
        self.assertEqual(second[-1].event_type, "done")
        self.assertEqual(ordinary.read_text(encoding="utf-8"), "turn one output")

    async def test_workspace_mcp_config_is_rejected_before_process_acquire(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        first = await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        self.assertEqual(first[-1].event_type, "done")
        generation_root = next(self.data_root.iterdir())
        workspace_mcp = generation_root / "workspace" / ".agents" / "mcp_config.json"
        workspace_mcp.parent.mkdir(parents=True)
        workspace_mcp.write_text("{\"mcpServers\": {}}", encoding="utf-8")
        spawn_count = len([item for item in self.evidence() if item["kind"] == "spawn"])

        rejected = await collect(service, self.binding_id, self.turn(thinking="low"))

        self.assertEqual(
            rejected[-1].payload,
            {"code": "agy_uncontrolled_mcp_source_forbidden"},
        )
        self.assertEqual(
            len([item for item in self.evidence() if item["kind"] == "spawn"]),
            spawn_count,
        )
        self.assertTrue(workspace_mcp.exists())

    async def test_registered_reserved_artifact_heals_from_canonical_backing(self) -> None:
        artifact = ReservedControlArtifact("canonical_demo.txt", "reserved/demo.txt")
        service, _ = self.build_service(reserved_artifacts=(artifact,))
        await service.ensure_generation(self.binding_id, self.spec)
        generation_root = next(self.data_root.iterdir())
        workspace = generation_root / "workspace"
        store = CanonicalControlStore(generation_root)
        store.write_canonical("canonical_demo.txt", "reserved body v1")
        ordinary = workspace / "ordinary.txt"
        ordinary.write_text("ordinary body", encoding="utf-8")
        neighbor = workspace / "reserved" / "neighbor.txt"

        first = await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        self.assertEqual(first[-1].event_type, "done")
        target = workspace / "reserved" / "demo.txt"
        self.assertEqual(target.read_text(encoding="utf-8"), "reserved body v1")
        neighbor.write_text("neighbor body", encoding="utf-8")

        # A tampered projection is healed before the next turn runs.
        target.write_text("tampered", encoding="utf-8")
        second = await collect(service, self.binding_id, self.turn(thinking="low"))
        self.assertEqual(second[-1].event_type, "done")
        self.assertEqual(target.read_text(encoding="utf-8"), "reserved body v1")
        self.assertEqual(neighbor.read_text(encoding="utf-8"), "neighbor body")

        # A deleted projection is restored; ordinary files stay untouched.
        target.unlink()
        third = await collect(service, self.binding_id, self.turn(thinking="low"))
        self.assertEqual(third[-1].event_type, "done")
        self.assertEqual(target.read_text(encoding="utf-8"), "reserved body v1")
        self.assertEqual(ordinary.read_text(encoding="utf-8"), "ordinary body")
        self.assertEqual(neighbor.read_text(encoding="utf-8"), "neighbor body")

    async def test_tool_success_reaches_journal_as_bounded_lifecycle_only(self) -> None:
        await self.assert_tool_scenario("tool_success", "tool_completed", 0.25)

    async def test_tool_error_reaches_journal_as_bounded_lifecycle_only(self) -> None:
        await self.assert_tool_scenario("tool_error", "tool_error", None)

    async def assert_tool_scenario(self, scenario, outcome, duration) -> None:
        service, _ = self.build_service(scenario=scenario)
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        events = await collect(service, self.binding_id, request)
        self.assertEqual(events[-1].event_type, "done")
        tool_events = [
            event.payload
            for event in events
            if event.payload.get("step_type") == "tool"
        ]
        self.assertEqual(
            [payload["state"] for payload in tool_events],
            ["ACTIVE", "DONE"] if scenario == "tool_success" else ["ACTIVE", "ERROR"],
        )
        for payload in tool_events:
            self.assertEqual(payload["tool_name"], "view_file")
            self.assertEqual(payload["category"], "provider_tool")
            self.assertNotIn("tool_info", payload)
        self.assertEqual(tool_events[-1]["outcome"], outcome)
        if duration is not None:
            self.assertEqual(tool_events[-1]["duration_seconds"], duration)
        serialized = "".join(event.model_dump_json() for event in events)
        self.assertNotIn("SENSITIVE-FIXTURE-PATH", serialized)
        durable = service.store.read_events(str(self.binding_id), str(request.request_id))
        durable_serialized = "".join(event.model_dump_json() for event in durable)
        self.assertNotIn("SENSITIVE-FIXTURE-PATH", durable_serialized)
        database_bytes = b"".join(
            path.read_bytes() for path in self.root.glob("runtime.sqlite3*")
        )
        self.assertNotIn(b"SENSITIVE-FIXTURE-PATH", database_bytes)

    async def test_lazy_prepare_fails_before_spawn_when_custom_agent_is_unverifiable(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        await service.shutdown()
        self.services.remove(service)
        generation_root = next(self.data_root.iterdir())
        profile = generation_root / "profile"
        hooks_path = profile / ".gemini" / "config" / "hooks.json"
        settings_path = profile / ".gemini" / "antigravity-cli" / "settings.json"
        agent_path = next(
            (profile / ".gemini" / "config" / "agents").glob("*/agent.md")
        )
        original_agent = agent_path.read_bytes()

        restarted, adapter = self.build_service()
        durable_generation = restarted.store.get_generation(str(self.binding_id))
        options = adapter.resolve_execution(
            "gemini-3.1-pro-preview",
            "auto",
        ).process_options.model_copy(
            update={
                "mcp_manifest_sha256": runtime_mcp_manifest_sha256(self.manifest())
            }
        )

        # Hooks and settings are security artifacts that the v2 adapter
        # restores before verification: tampering is silently repaired at load.
        hooks_path.write_bytes(b"{}")
        settings_path.write_text(
            json.dumps({"modelProvider": "account_default", "permissions": {"deny": []}}),
            encoding="utf-8",
        )
        adapter._load_layout(durable_generation, options, self.manifest())
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(set(hooks), {"exocore-runtime-ephemeral"})
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"]["deny"], list(DENY_POLICY))

        # Ordinary workspace files survive reserved-artifact verification at load.
        pollution = generation_root / "workspace" / "pollution.txt"
        pollution.write_text("workspace canary", encoding="utf-8")
        adapter._load_layout(durable_generation, options, self.manifest())
        self.assertEqual(pollution.read_text(encoding="utf-8"), "workspace canary")

        # Canonical agent markdown is identity material: any tamper is fatal
        # before the security restore may run.
        agent_path.write_text("tampered custom agent", encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            adapter._load_layout(durable_generation, options, self.manifest())
        self.assertEqual(caught.exception.code, "agy_custom_agent_invalid")
        agent_path.write_bytes(original_agent)

        metadata_path = generation_root / "generation.json"
        original_metadata = metadata_path.read_bytes()
        metadata = json.loads(original_metadata)
        metadata["bootstrap_fingerprint"] = "tampered-bootstrap"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            adapter._load_layout(durable_generation, options, self.manifest())
        self.assertEqual(caught.exception.code, "agy_artifact_identity_mismatch")
        metadata_path.write_bytes(original_metadata)

        metadata = json.loads(original_metadata)
        metadata["provider_session_id"] = "tampered-provider-session"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            adapter._load_layout(durable_generation, options, self.manifest())
        self.assertEqual(caught.exception.code, "agy_artifact_session_conflict")
        metadata_path.write_bytes(original_metadata)

        malicious_agent = render_agent_markdown(
            f"exocore-runtime-{str(self.binding_id).replace('-', '')}",
            "malicious replacement system",
        )
        agent_path.write_text(malicious_agent, encoding="utf-8")
        metadata = json.loads(original_metadata)
        metadata["agent_markdown_sha256"] = hashlib.sha256(
            malicious_agent.encode("utf-8")
        ).hexdigest()
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            adapter._load_layout(durable_generation, options, self.manifest())
        self.assertEqual(caught.exception.code, "agy_custom_agent_invalid")
        metadata_path.write_bytes(original_metadata)

        agent_path.write_text("tampered custom agent", encoding="utf-8")
        request = self.turn()
        events = await collect(restarted, self.binding_id, request)
        self.assertEqual(events[-1].payload, {"code": "agy_custom_agent_invalid"})
        # The failed lazy turn must not disturb the already-durable session:
        # known continuity is preserved and never silently replaced.
        self.assertEqual(
            restarted.store.get_generation(str(self.binding_id)).provider_session_id,
            "11111111-2222-3333-4444-555555555555",
        )
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "spawn"]), 1)
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)

    async def test_stale_security_policy_options_are_rejected_before_spawn(self) -> None:
        # Frozen earlier policies are no longer current: a request whose frozen
        # resolution still carries them must fail closed instead of reacquiring
        # a process under the old policy. ``v3`` is the 3-tool unlock; ``v4``
        # predates the widened Memory MCP surface (enabledTools + eager), and
        # ``v5`` predates URL-read and image-generation exposure.
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        generation = service.store.get_generation(str(self.binding_id))
        current = adapter.resolve_execution(
            request.requested_model_id,
            request.requested_thinking_level,
        ).process_options.model_copy(
            update={
                "mcp_manifest_sha256": runtime_mcp_manifest_sha256(
                    request.runtime_mcp_tools
                )
            }
        )
        self.assertEqual(current.security_policy_revision, SECURITY_POLICY_REVISION)
        for stale_revision in (
            "agy-tool-perm-v3",
            "agy-tool-perm-v4",
            "agy-tool-perm-v5",
        ):
            with self.subTest(stale_revision=stale_revision):
                stale = current.model_copy(
                    update={"security_policy_revision": stale_revision}
                )
                with self.assertRaises(ProviderAdapterError) as caught:
                    await adapter.prepare_turn(generation, request, stale, is_first_turn=True)
                self.assertEqual(caught.exception.code, "agy_process_options_unsupported")
        self.assertEqual([item for item in self.evidence() if item["kind"] == "spawn"], [])
        self.assertEqual(adapter.supervisor._sessions, {})

    async def test_gateway_and_parent_secrets_are_not_inherited_by_agy(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        with patch.dict(
            os.environ,
            {
                "EXOCORE_RUNTIME_TOKEN": "bearer-secret-canary",
                "OTHER_SECRET_VALUE": "parent-secret-canary",
            },
        ):
            await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        spawn = next(item for item in self.evidence() if item["kind"] == "spawn")
        self.assertEqual(spawn["sensitive_env_present"], [])

    async def test_concurrent_distinct_first_turns_terminalize_bootstrap_loser(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        first = self.turn(bootstrap={"anchor": "first"})
        second = self.turn(bootstrap={"anchor": "second"})
        first_events, second_events = await asyncio.gather(
            collect(service, self.binding_id, first),
            collect(service, self.binding_id, second),
        )
        terminals = {first_events[-1].event_type, second_events[-1].event_type}
        self.assertEqual(terminals, {"done", "error"})
        loser_events = first_events if first_events[-1].event_type == "error" else second_events
        loser_request = first if loser_events is first_events else second
        self.assertEqual(loser_events[-1].payload, {"code": "bootstrap_state_conflict"})
        loser = service.store.get_request(str(self.binding_id), str(loser_request.request_id))
        self.assertEqual(loser.status, "failed")
        self.assertEqual(
            service.store.terminal_count(str(self.binding_id), str(loser_request.request_id)),
            1,
        )
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "turn"]), 1)

    async def test_concurrent_ensure_is_state_only_and_first_turn_launches_once(self) -> None:
        service, adapter = self.build_service()
        acquired = await asyncio.gather(
            service.ensure_generation(self.binding_id, self.spec),
            service.ensure_generation(self.binding_id, self.spec),
        )
        self.assertEqual([result.status for result in acquired], ["starting", "starting"])
        self.assertEqual(
            len([item for item in self.evidence() if item["kind"] == "spawn"]),
            0,
        )
        await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        self.assertEqual(
            len([item for item in self.evidence() if item["kind"] == "spawn"]),
            1,
        )
        self.assertIsNotNone(adapter.supervisor.quota_snapshot)

    async def test_model_mismatch_fails_before_any_user_stdin(self) -> None:
        service, _ = self.build_service("model_mismatch")
        await service.ensure_generation(self.binding_id, self.spec)
        events = await collect(service, self.binding_id, self.turn(bootstrap={"history": []}))
        self.assertEqual(events[-1].payload, {"code": "agy_model_mismatch"})
        self.assertEqual(events[-1].terminal_status, "failed")
        generation = service.store.get_generation(str(self.binding_id))
        self.assertEqual(generation.status, "starting")
        self.assertIsNone(generation.provider_session_id)
        self.assertFalse(any(item["kind"] == "turn" for item in self.evidence()))

    async def test_delayed_result_tail_is_rejected_before_next_stdin_send(self) -> None:
        service, _ = self.build_service("delayed_event_after_result")
        await service.ensure_generation(self.binding_id, self.spec)
        first = self.turn(bootstrap={"history": []})
        first_events = await collect(service, self.binding_id, first)
        self.assertEqual(first_events[-1].event_type, "done")
        (self.root / "tail-release").touch()
        await asyncio.sleep(0.1)
        second = self.turn()
        second_events = await collect(service, self.binding_id, second)
        self.assertEqual(second_events[-1].payload, {"code": "agy_unsolicited_output"})
        self.assertEqual(
            service.store.get_request(str(self.binding_id), str(second.request_id)).status,
            "indeterminate",
        )
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "turn"]), 1)

    async def test_models_progress_stderr_is_accepted_with_strict_stdout(self) -> None:
        service, _ = self.build_service("models_progress_stderr")
        await service.ensure_generation(self.binding_id, self.spec)

        events = await collect(
            service,
            self.binding_id,
            self.turn(bootstrap={"history": []}),
        )

        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "turn"]), 1)

    async def test_verified_1_2_4_version_is_accepted(self) -> None:
        service, _ = self.build_service("version_1_2_4")
        await service.ensure_generation(self.binding_id, self.spec)

        events = await collect(
            service,
            self.binding_id,
            self.turn(bootstrap={"history": []}),
        )

        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "turn"]), 1)

    async def test_startup_faults_fail_before_user_stdin_and_timeout_process_is_reaped(self) -> None:
        scenarios = {
            "bad_version": "agy_version_unsupported",
            "version_stderr": "agy_version_failed",
            "models_missing": "agy_models_unavailable",
            "models_invalid_stdout": "agy_models_unavailable",
            "auth_missing": "agy_auth_unavailable",
            "quota_stderr": "agy_auth_unavailable",
            "init_timeout": "agy_init_timeout",
            "init_empty": "agy_init_eof",
            "init_malformed": "agy_malformed_ndjson",
            "exit_after_init": "agy_exit_after_init",
            "version_timeout": "agy_version_failed",
        }
        for scenario, expected_code in scenarios.items():
            with self.subTest(scenario=scenario):
                scenario_root = self.root / f"startup-{scenario}"
                scenario_root.mkdir()
                original_state = self.state_path
                original_data = self.data_root
                original_evidence = self.evidence_path
                self.state_path = scenario_root / "runtime.sqlite3"
                self.data_root = scenario_root / "providers"
                self.evidence_path = scenario_root / "evidence.jsonl"
                service, _ = self.build_service(scenario)
                spec = self.spec.model_copy(
                    update={"bootstrap_fingerprint": f"bootstrap-{scenario}"}
                )
                binding_id = uuid4()
                await service.ensure_generation(binding_id, spec)
                events = await collect(
                    service,
                    binding_id,
                    self.turn(bootstrap={"history": []}),
                )
                self.assertEqual(events[-1].payload, {"code": expected_code})
                self.assertFalse(any(item["kind"] == "turn" for item in self.evidence()))
                if scenario == "version_timeout":
                    process_id = next(
                        item["pid"] for item in self.evidence() if item["kind"] == "version"
                    )
                    check = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
                        capture_output=True,
                        text=True,
                        check=False,
                        creationflags=NO_WINDOW,
                    )
                    self.assertNotIn(str(process_id), check.stdout)
                self.state_path = original_state
                self.data_root = original_data
                self.evidence_path = original_evidence

    async def test_cold_start_quota_probe_retries_and_then_succeeds(self) -> None:
        # A cold CLI can fail the first ``/quota`` probe transiently (token
        # refresh / warm-up). The bounded retry must absorb it, leaving the
        # generation usable with a correct quota snapshot.
        service, adapter = self.build_service("quota_retry")
        await service.ensure_generation(self.binding_id, self.spec)

        events = await collect(
            service,
            self.binding_id,
            self.turn(bootstrap={"history": []}),
        )

        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(
            len([item for item in self.evidence() if item["kind"] == "quota"]), 2
        )
        self.assertEqual(
            adapter.supervisor.quota_snapshot, {"weekly": 84, "5h": 93}
        )
        self.assertTrue(any(item["kind"] == "spawn" for item in self.evidence()))

    async def test_cold_start_quota_probe_exhaustion_stays_fail_closed(self) -> None:
        # ``auth_missing`` is the fixture's always-failing ``/quota`` scenario.
        # Exhausting the bounded attempts must keep the original fail-closed
        # outcome: same code, exactly three attempts, nothing spawned.
        service, _ = self.build_service("auth_missing")
        await service.ensure_generation(self.binding_id, self.spec)

        events = await collect(
            service,
            self.binding_id,
            self.turn(bootstrap={"history": []}),
        )

        self.assertEqual(events[-1].payload, {"code": "agy_auth_unavailable"})
        self.assertEqual(
            len([item for item in self.evidence() if item["kind"] == "quota"]), 3
        )
        self.assertFalse(any(item["kind"] == "turn" for item in self.evidence()))
        self.assertFalse(any(item["kind"] == "spawn" for item in self.evidence()))

    async def test_shutdown_attempts_mailbox_cleanup_when_supervisor_cleanup_reports_failure(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []}, ephemeral="SHUTDOWN-PRIVATE-CANARY")
        generation = service.store.get_generation(str(self.binding_id))
        resolution = adapter.resolve_execution(
            request.requested_model_id,
            request.requested_thinking_level,
        )
        options = resolution.process_options.model_copy(
            update={
                "mcp_manifest_sha256": runtime_mcp_manifest_sha256(
                    request.runtime_mcp_tools
                )
            }
        )
        await adapter.prepare_turn(
            generation,
            request,
            options,
            is_first_turn=True,
        )
        mailbox = adapter._mailbox(str(self.binding_id))
        self.assertTrue(mailbox.pending_path.exists())
        with patch.object(
            adapter.supervisor,
            "shutdown",
            side_effect=ProviderAdapterError("fixture_supervisor_cleanup_failed"),
        ):
            with self.assertRaises(ProviderAdapterError) as caught:
                await adapter.shutdown()
        self.assertEqual(caught.exception.code, "fixture_supervisor_cleanup_failed")
        self.assertFalse(mailbox.pending_path.exists())
        self.assertEqual(adapter._requests, {})
        await adapter.supervisor.shutdown()

    async def test_shutdown_terminalizes_sent_turn_before_process_cleanup(self) -> None:
        service, _ = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        for _ in range(200):
            record = service.store.get_request(str(self.binding_id), str(request.request_id))
            if record is not None and record.status == "sent":
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("request did not cross the durable send boundary")
        await service.shutdown()
        self.services.remove(service)
        events = await asyncio.wait_for(owner, timeout=3)
        self.assertEqual(events[-1].payload, {"code": "indeterminate_on_shutdown"})
        durable = service.store.get_request(str(self.binding_id), str(request.request_id))
        self.assertEqual(durable.status, "indeterminate")
        self.assertEqual(
            service.store.terminal_count(str(self.binding_id), str(request.request_id)),
            1,
        )

    async def test_retire_terminalizes_sent_and_queued_prepared_requests(self) -> None:
        service, _ = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        first = self.turn(bootstrap={"history": []})
        second = self.turn()
        first_owner = asyncio.create_task(collect(service, self.binding_id, first))
        for _ in range(200):
            record = service.store.get_request(str(self.binding_id), str(first.request_id))
            if record is not None and record.status == "sent":
                break
            await asyncio.sleep(0.01)
        second_owner = asyncio.create_task(collect(service, self.binding_id, second))
        for _ in range(200):
            record = service.store.get_request(str(self.binding_id), str(second.request_id))
            if record is not None and record.status == "prepared":
                break
            await asyncio.sleep(0.01)
        retired = await service.retire(self.binding_id, "retire with work")
        self.assertTrue(retired.changed)
        first_events, second_events = await asyncio.gather(first_owner, second_owner)
        self.assertEqual(first_events[-1].payload, {"code": "indeterminate_on_retire"})
        self.assertEqual(second_events[-1].payload, {"code": "retired_before_send"})
        first_record = service.store.get_request(str(self.binding_id), str(first.request_id))
        second_record = service.store.get_request(str(self.binding_id), str(second.request_id))
        self.assertEqual(first_record.status, "indeterminate")
        self.assertEqual(second_record.status, "failed")

    async def test_hard_owner_exit_closes_windows_job_and_reaps_agy(self) -> None:
        fixture_root = Path(__file__).resolve().parents[1] / "fixtures"
        owner = fixture_root / "hard_crash_agy_owner.py"
        fake_agy = fixture_root / "fake_agy.py"
        crash_root = self.root / "hard-crash"
        evidence_path = self.root / "hard-crash-evidence.jsonl"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        completed = subprocess.run(
            [sys.executable, str(owner), str(fake_agy), str(crash_root), str(evidence_path)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=NO_WINDOW,
        )
        self.assertEqual(completed.returncode, 73)
        evidence = [
            json.loads(line)
            for line in evidence_path.read_text(encoding="utf-8").splitlines()
        ]
        process_ids = [
            next(item["pid"] for item in evidence if item["kind"] == "spawn"),
            next(item["child_pid"] for item in evidence if item["kind"] == "launch_child"),
        ]
        for process_id in process_ids:
            for _ in range(100):
                check = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                    creationflags=NO_WINDOW,
                )
                if str(process_id) not in check.stdout:
                    break
                await asyncio.sleep(0.01)
            self.assertNotIn(str(process_id), check.stdout)

    async def test_owner_task_cancellation_closes_process_tree_and_both_ownership_maps(self) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        process_ids = []
        for _ in range(200):
            evidence = self.evidence()
            children = [item for item in evidence if item["kind"] == "child"]
            spawns = [item for item in evidence if item["kind"] == "spawn"]
            if children and spawns:
                process_ids = [spawns[-1]["pid"], children[-1]["child_pid"]]
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(process_ids), 2)
        original_dispose = adapter.supervisor._dispose_session
        dispose_attempts = 0

        async def fail_twice_before_process_cleanup(session, *, force):
            nonlocal dispose_attempts
            dispose_attempts += 1
            if dispose_attempts <= 2:
                raise ProviderAdapterError("fixture_cleanup_report")
            await original_dispose(session, force=force)

        with patch.object(
            adapter.supervisor,
            "_dispose_session",
            side_effect=fail_twice_before_process_cleanup,
        ):
            self.assertTrue(owner.cancel())
            with self.assertRaises(asyncio.CancelledError) as caught:
                await owner
            # CP4-B: a failed exact force-dispose must never fabricate a
            # cancelled receipt. The settlement reports honest indeterminate;
            # the failed disposal leaves the session poisoned, never silently
            # reusable, and the exact request proof is still reclaimed after
            # the durable terminal.
            self.assertEqual(dispose_attempts, 2)
            self.assertIn(
                "cancelled stream process cleanup also failed",
                getattr(caught.exception, "__notes__", []),
            )
            durable = service.store.get_request(str(self.binding_id), str(request.request_id))
            self.assertEqual(durable.status, "indeterminate")
            self.assertEqual(durable.terminal_code, "cancel_cleanup_failed")
            self.assertEqual(
                service.store.terminal_count(str(self.binding_id), str(request.request_id)),
                1,
            )
            self.assertFalse(
                [key for key in adapter._requests if key[0] == str(self.binding_id)]
            )
            self.assertIn(str(self.binding_id), adapter.supervisor._sessions)
            self.assertTrue(adapter.supervisor._sessions[str(self.binding_id)].poisoned)
        # Once the failure injection is lifted, shutdown retries the poisoned
        # session and the process tree is gone with exactly one durable terminal.
        await service.shutdown()
        self.services.remove(service)
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)
        for process_id in process_ids:
            for _ in range(100):
                check = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                    creationflags=NO_WINDOW,
                )
                if str(process_id) not in check.stdout:
                    break
                await asyncio.sleep(0.01)
            self.assertNotIn(str(process_id), check.stdout)

    async def test_idempotent_old_cancel_does_not_cancel_new_active_request(self) -> None:
        service, _ = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        old = self.turn(bootstrap={"history": []})
        old_owner = asyncio.create_task(collect(service, self.binding_id, old))
        for _ in range(200):
            record = service.store.get_request(str(self.binding_id), str(old.request_id))
            if record is not None and record.status == "sent":
                break
            await asyncio.sleep(0.01)
        await service.cancel(self.binding_id, old.request_id)
        await asyncio.wait_for(old_owner, timeout=3)

        current = self.turn()
        current_owner = asyncio.create_task(collect(service, self.binding_id, current))
        for _ in range(200):
            record = service.store.get_request(str(self.binding_id), str(current.request_id))
            if record is not None and record.status == "sent":
                break
            await asyncio.sleep(0.01)
        repeated_old = await service.cancel(self.binding_id, old.request_id)
        self.assertFalse(repeated_old.changed)
        self.assertEqual(
            service.store.get_request(str(self.binding_id), str(current.request_id)).status,
            "sent",
        )
        self.assertFalse(current_owner.done())
        await service.cancel(self.binding_id, current.request_id)
        current_events = await asyncio.wait_for(current_owner, timeout=3)
        self.assertEqual(current_events[-1].payload, {"code": "cancelled"})

    async def test_cancel_kills_fixture_process_tree_and_persists_cancelled(self) -> None:
        service, _ = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        child_pid = None
        for _ in range(200):
            children = [item for item in self.evidence() if item["kind"] == "child"]
            if children:
                child_pid = children[-1]["child_pid"]
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(child_pid)
        cancelled = await service.cancel(self.binding_id, request.request_id)
        self.assertTrue(cancelled.changed)
        events = await asyncio.wait_for(owner, timeout=3)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})
        durable = service.store.get_request(str(self.binding_id), str(request.request_id))
        self.assertEqual(durable.status, "cancelled")
        check = subprocess.run(
            ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=NO_WINDOW,
        )
        self.assertNotIn(str(child_pid), check.stdout)


    async def test_attachment_turn_projects_only_a_workspace_absolute_path(self) -> None:
        service, _adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        generation_root = self.generation_root()
        metadata_before = json.loads(
            (generation_root / "generation.json").read_text(encoding="utf-8")
        )
        request = self.turn(bootstrap={"continuity_anchor": "anchor"})
        manifest = self.attachment_manifest(display_name="diagram.png")
        await service.stage_attachment(
            self.binding_id,
            request.request_id,
            manifest.artifact_id,
            PNG_BYTES,
        )
        events = await collect(
            service,
            self.binding_id,
            request.model_copy(update={"attachments": (manifest,)}),
        )
        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(events[-1].provider_input_effect, "may_have_reached_provider")
        turns = [item for item in self.evidence() if item["kind"] == "turn"]
        self.assertEqual(len(turns), 1)
        paths = turns[0]["attachment_paths"]
        self.assertEqual(len(paths), 1)
        projected = Path(paths[0])
        self.assertTrue(projected.is_absolute())
        self.assertTrue(projected.is_file())
        self.assertEqual(projected.name, "att-7.png")
        self.assertTrue(projected.is_relative_to(self.data_root))
        self.assertEqual(
            projected.parent,
            self.request_attachment_dir(request.request_id),
        )
        metadata_after = json.loads(
            (generation_root / "generation.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            metadata_before["agent_markdown_sha256"],
            metadata_after["agent_markdown_sha256"],
        )
        self.assertEqual(
            metadata_before["generation_id"],
            metadata_after["generation_id"],
        )
        self.assertEqual(
            len([item for item in self.evidence() if item["kind"] == "spawn"]),
            1,
        )

    async def test_attachment_verify_failures_are_pre_send_and_never_spawn(self) -> None:
        service, _adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)

        missing_request = self.turn(bootstrap={"history": []})
        events = await collect(
            service,
            self.binding_id,
            missing_request.model_copy(
                update={"attachments": (self.attachment_manifest(),)}
            ),
        )
        self.assertEqual(events[-1].payload["code"], "attachment_not_staged")
        self.assertEqual(events[-1].provider_input_effect, "not_sent")
        self.assertFalse(
            self.request_attachment_dir(missing_request.request_id).exists()
        )

        digest_request = self.turn(bootstrap={"history": []})
        await service.stage_attachment(
            self.binding_id,
            digest_request.request_id,
            "att-7",
            PNG_BYTES,
        )
        events = await collect(
            service,
            self.binding_id,
            digest_request.model_copy(
                update={"attachments": (self.attachment_manifest(b"different-bytes"),)}
            ),
        )
        self.assertEqual(events[-1].payload["code"], "attachment_digest_mismatch")
        self.assertEqual(events[-1].provider_input_effect, "not_sent")
        self.assertFalse(
            self.request_attachment_dir(digest_request.request_id).exists()
        )

        mime_request = self.turn(bootstrap={"history": []})
        await service.stage_attachment(
            self.binding_id,
            mime_request.request_id,
            "att-7",
            PNG_BYTES,
        )
        events = await collect(
            service,
            self.binding_id,
            mime_request.model_copy(
                update={
                    "attachments": (
                        self.attachment_manifest(
                            PNG_BYTES,
                            mime_type="image/jpeg",
                            display_name="photo.jpg",
                        ),
                    )
                }
            ),
        )
        self.assertEqual(events[-1].payload["code"], "attachment_mime_mismatch")
        self.assertEqual(events[-1].provider_input_effect, "not_sent")
        self.assertFalse(self.request_attachment_dir(mime_request.request_id).exists())
        self.assertEqual(
            [item for item in self.evidence() if item["kind"] == "spawn"],
            [],
        )

    async def test_concurrent_attachment_stages_share_one_cap_lock(self) -> None:
        service, _adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request_id = uuid4()
        results = await asyncio.gather(
            *[
                service.stage_attachment(
                    self.binding_id,
                    request_id,
                    f"att-{index}",
                    b"x" * 4,
                )
                for index in range(1, 7)
            ],
            return_exceptions=True,
        )
        self.assertEqual(sum(result is None for result in results), 5)
        self.assertEqual(
            sum(
                isinstance(result, AttachmentCapacityExceededError)
                for result in results
            ),
            1,
        )
        files = list(self.request_attachment_dir(request_id).iterdir())
        self.assertEqual(len(files), 5)
        self.assertEqual(sum(path.stat().st_size for path in files), 20)

        total_request = uuid4()
        with patch(
            "exocore_runtime.providers.antigravity.attachments."
            "MAX_ATTACHMENT_TOTAL_BYTES",
            10,
        ):
            results = await asyncio.gather(
                *[
                    service.stage_attachment(
                        self.binding_id,
                        total_request,
                        f"att-{index}",
                        b"y" * 4,
                    )
                    for index in range(1, 6)
                ],
                return_exceptions=True,
            )
        self.assertGreaterEqual(
            sum(
                isinstance(result, AttachmentCapacityExceededError)
                for result in results
            ),
            1,
        )
        self.assertLessEqual(
            sum(
                path.stat().st_size
                for path in self.request_attachment_dir(total_request).iterdir()
            ),
            10,
        )

    async def test_registered_request_blocks_stage_and_discard_and_retire_cleans(self) -> None:
        service, _adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request_id = uuid4()
        await service.stage_attachment(
            self.binding_id,
            request_id,
            "att-7",
            b"payload",
        )
        attachment_dir = self.request_attachment_dir(request_id)
        self.assertTrue(attachment_dir.is_dir())

        service.store.claim_request(
            str(self.binding_id),
            str(request_id),
            "a" * 64,
            "gemini-3.1-pro-preview",
            "auto",
            "owner",
        )
        with self.assertRaises(RequestRegisteredError):
            await service.stage_attachment(
                self.binding_id,
                request_id,
                "att-8",
                b"payload",
            )
        with self.assertRaises(RequestRegisteredError):
            await service.discard_attachments(self.binding_id, request_id)
        self.assertEqual(
            sorted(path.name for path in attachment_dir.iterdir()),
            ["att-7.blob"],
        )

        generation_root = self.generation_root()
        await service.retire(self.binding_id, "test")
        self.assertFalse(generation_root.exists())
        with self.assertRaises(RetiredError):
            await service.stage_attachment(
                self.binding_id,
                request_id,
                "att-9",
                b"payload",
            )
        self.assertFalse(generation_root.exists())

    async def test_stage_vs_retire_linearizes_without_recreating_the_root(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        generation_root = self.generation_root()
        request_id = uuid4()
        lock = await adapter._artifact_lock(str(self.binding_id))
        await lock.acquire()
        stage_task = asyncio.create_task(
            service.stage_attachment(
                self.binding_id,
                request_id,
                "att-7",
                b"payload",
            )
        )
        await asyncio.sleep(0.01)
        retire_task = asyncio.create_task(service.retire(self.binding_id, "race"))
        await asyncio.sleep(0.01)
        lock.release()
        stage_result, retire_result = await asyncio.gather(
            stage_task,
            retire_task,
            return_exceptions=True,
        )
        self.assertNotIsInstance(retire_result, BaseException)
        if isinstance(stage_result, BaseException):
            self.assertIsInstance(stage_result, RetiredError)
        self.assertFalse(generation_root.exists())

    async def test_materialize_recovers_blob_only_and_final_only_windows(self) -> None:
        service, _adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        manifest = self.attachment_manifest()

        final_only = self.turn(bootstrap={"history": []})
        await service.stage_attachment(
            self.binding_id,
            final_only.request_id,
            "att-7",
            PNG_BYTES,
        )
        blob = self.request_attachment_dir(final_only.request_id) / "att-7.blob"
        final = blob.with_suffix(".png")
        os.replace(blob, final)
        events = await collect(
            service,
            self.binding_id,
            final_only.model_copy(update={"attachments": (manifest,)}),
        )
        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(final.read_bytes(), PNG_BYTES)

        both = self.turn()
        await service.stage_attachment(
            self.binding_id,
            both.request_id,
            "att-7",
            PNG_BYTES,
        )
        both_blob = self.request_attachment_dir(both.request_id) / "att-7.blob"
        both_final = both_blob.with_suffix(".png")
        both_final.write_bytes(b"corrupt-final")
        events = await collect(
            service,
            self.binding_id,
            both.model_copy(update={"attachments": (manifest,)}),
        )
        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(both_final.read_bytes(), PNG_BYTES)
        self.assertFalse(both_blob.exists())

    async def test_stage_wins_claim_race_before_registration(self) -> None:
        from exocore_runtime.providers.antigravity import attachments as attachments_module

        service, _adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        manifest = self.attachment_manifest()
        binding = str(self.binding_id)
        request_key = str(request.request_id)
        stage_written = asyncio.Event()
        original_stage = attachments_module.AttachmentStore.stage

        def recording_stage(store_self, request_id, artifact_id, data):
            path = original_stage(store_self, request_id, artifact_id, data)
            stage_written.set()
            return path

        claim_observations: list[bool] = []
        original_claim = service.store.claim_request

        def recording_claim(*args, **kwargs):
            claim_observations.append(stage_written.is_set())
            return original_claim(*args, **kwargs)

        attachments_module.AttachmentStore.stage = recording_stage
        service.store.claim_request = recording_claim
        claim_lock = await service._get_claim_lock(binding, request_key)
        stage_task = None
        turn_task = None
        try:
            await claim_lock.acquire()
            stage_task = asyncio.create_task(
                service.stage_attachment(
                    self.binding_id,
                    request.request_id,
                    manifest.artifact_id,
                    PNG_BYTES,
                )
            )
            await self.wait_for_claim_waiters(claim_lock, 1)
            turn_task = asyncio.create_task(
                collect(
                    service,
                    self.binding_id,
                    request.model_copy(update={"attachments": (manifest,)}),
                )
            )
            await self.wait_for_claim_waiters(claim_lock, 2)
            self.assertIsNone(service.store.get_request(binding, request_key))
            claim_lock.release()
            await asyncio.wait_for(stage_task, timeout=5)
            events = await asyncio.wait_for(turn_task, timeout=5)
        finally:
            attachments_module.AttachmentStore.stage = original_stage
            service.store.claim_request = original_claim
            if claim_lock.locked():
                claim_lock.release()
            pending = [
                task for task in (stage_task, turn_task) if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self.assertEqual(claim_observations, [True])
        self.assertEqual(events[-1].event_type, "done")
        final = self.request_attachment_dir(request.request_id) / "att-7.png"
        self.assertEqual(final.read_bytes(), PNG_BYTES)
        self.assertFalse((final.parent / "att-7.blob").exists())
        self.assertEqual(
            service.store.get_request(binding, request_key).status,
            "completed",
        )

    async def test_discard_wins_claim_race_before_registration(self) -> None:
        from exocore_runtime.providers.antigravity import attachments as attachments_module

        service, _adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        binding = str(self.binding_id)
        request_key = str(request.request_id)
        await service.stage_attachment(
            self.binding_id,
            request.request_id,
            "att-7",
            PNG_BYTES,
        )
        staged_dir = self.request_attachment_dir(request.request_id)
        self.assertTrue((staged_dir / "att-7.blob").is_file())

        discard_done = asyncio.Event()
        original_discard = attachments_module.AttachmentStore.discard

        def recording_discard(store_self, request_id):
            original_discard(store_self, request_id)
            discard_done.set()

        claim_observations: list[bool] = []
        original_claim = service.store.claim_request

        def recording_claim(*args, **kwargs):
            claim_observations.append(discard_done.is_set())
            return original_claim(*args, **kwargs)

        attachments_module.AttachmentStore.discard = recording_discard
        service.store.claim_request = recording_claim
        claim_lock = await service._get_claim_lock(binding, request_key)
        discard_task = None
        turn_task = None
        try:
            await claim_lock.acquire()
            discard_task = asyncio.create_task(
                service.discard_attachments(self.binding_id, request.request_id)
            )
            await self.wait_for_claim_waiters(claim_lock, 1)
            turn_task = asyncio.create_task(
                collect(service, self.binding_id, request)
            )
            await self.wait_for_claim_waiters(claim_lock, 2)
            self.assertIsNone(service.store.get_request(binding, request_key))
            claim_lock.release()
            await asyncio.wait_for(discard_task, timeout=5)
            self.assertFalse(staged_dir.exists())
            events = await asyncio.wait_for(turn_task, timeout=5)
        finally:
            attachments_module.AttachmentStore.discard = original_discard
            service.store.claim_request = original_claim
            if claim_lock.locked():
                claim_lock.release()
            pending = [
                task for task in (discard_task, turn_task) if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self.assertEqual(claim_observations, [True])
        self.assertEqual(events[-1].event_type, "done")
        self.assertFalse(staged_dir.exists())
        self.assertEqual(
            service.store.get_request(binding, request_key).status,
            "completed",
        )

    async def test_claim_wins_race_blocks_stage_and_discard_without_mutation(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        manifest = self.attachment_manifest()
        await service.stage_attachment(
            self.binding_id,
            request.request_id,
            manifest.artifact_id,
            PNG_BYTES,
        )
        attachment_dir = self.request_attachment_dir(request.request_id)
        blob = attachment_dir / "att-7.blob"
        final = blob.with_suffix(".png")
        final.write_bytes(PNG_BYTES)

        entered = asyncio.Event()
        release = asyncio.Event()
        original_prepare = adapter.prepare_turn

        async def blocked_prepare(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_prepare(*args, **kwargs)

        adapter.prepare_turn = blocked_prepare
        turn_task = asyncio.create_task(
            collect(
                service,
                self.binding_id,
                request.model_copy(update={"attachments": (manifest,)}),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        durable = service.store.get_request(
            str(self.binding_id),
            str(request.request_id),
        )
        self.assertIsNotNone(durable)
        self.assertEqual(durable.status, "prepared")

        with self.assertRaises(RequestRegisteredError):
            await service.stage_attachment(
                self.binding_id,
                request.request_id,
                "att-8",
                b"other-bytes",
            )
        with self.assertRaises(RequestRegisteredError):
            await service.discard_attachments(self.binding_id, request.request_id)
        self.assertEqual(blob.read_bytes(), PNG_BYTES)
        self.assertEqual(final.read_bytes(), PNG_BYTES)
        self.assertFalse((attachment_dir / "att-8.blob").exists())

        release.set()
        events = await asyncio.wait_for(turn_task, timeout=5)
        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(final.read_bytes(), PNG_BYTES)
        self.assertFalse(blob.exists())

if __name__ == "__main__":
    unittest.main()