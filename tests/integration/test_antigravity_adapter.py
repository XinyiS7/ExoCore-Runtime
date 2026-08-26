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

from exocore_runtime.contracts import GenerationSpec, TurnRequest
from exocore_runtime.errors import InvalidRequestError, ProviderAdapterError
from exocore_runtime.providers.antigravity.adapter import AntigravityAdapter
from exocore_runtime.providers.antigravity.process import AgyProcessConfig, AgyProcessSupervisor
from exocore_runtime.providers.antigravity.renderer import (
    DENY_POLICY,
    render_agent_markdown,
)
from exocore_runtime.providers.fake import DeterministicFakeAdapter
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
        self.binding_id = uuid4()
        self.system_canary = "SYSTEM-INSTRUCTIONS-PRIVATE-CANARY"
        self.spec = GenerationSpec(
            runtime_kind="antigravity",
            provider_model_id="gemini-3.1-pro-high",
            bootstrap_fingerprint="bootstrap-1",
            config_fingerprint="config-1",
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

    def build_service(self, scenario="normal"):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fake_agy.py"
        environment = {
            "FAKE_AGY_SCENARIO": scenario,
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
            mailbox_ttl_seconds=30,
        )
        service = RuntimeService(
            RuntimeStateStore(self.state_path),
            {"fake": DeterministicFakeAdapter(), "antigravity": adapter},
        )
        self.services.append(service)
        return service, adapter

    def evidence(self):
        if not self.evidence_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.evidence_path.read_text(encoding="utf-8").splitlines()
        ]

    async def test_first_same_process_restart_resume_and_retire(self) -> None:
        service, adapter = self.build_service()
        generation = await service.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(generation.status, "active")
        self.assertEqual(
            generation.provider_session_id,
            "11111111-2222-3333-4444-555555555555",
        )
        first = TurnRequest(
            request_id=uuid4(),
            user_message="CURRENT-USER-CANARY first",
            bootstrap_context={"continuity_anchor": "ANCHOR-ONE"},
            ephemeral_current="EPHEMERAL-CANARY first-only",
        )
        first_events = await collect(service, self.binding_id, first)
        self.assertEqual(first_events[-1].event_type, "done")
        self.assertEqual(
            [event.event_type for event in first_events],
            ["lifecycle", "lifecycle", "thinking_delta", "content_delta", "usage", "done"],
        )
        self.assertTrue(service.store.get_generation(str(self.binding_id)).bootstrap_sent)

        second = TurnRequest(
            request_id=uuid4(),
            user_message="CURRENT-USER-CANARY second",
        )
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
        self.assertEqual(
            set(settings["permissions"]["deny"]),
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
        hooks = json.loads(
            (
                generation_root / "profile" / ".gemini" / "config" / "hooks.json"
            ).read_text(encoding="utf-8")
        )
        hook_config = hooks["exocore-runtime-ephemeral"]
        self.assertEqual(set(hook_config), {"enabled", "PreInvocation"})
        self.assertFalse(any((generation_root / "workspace").iterdir()))
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
        self.assertEqual(resumed.provider_session_id, generation.provider_session_id)
        third = TurnRequest(
            request_id=uuid4(),
            user_message="CURRENT-USER-CANARY resume",
        )
        third_events = await collect(restarted, self.binding_id, third)
        self.assertEqual(third_events[-1].event_type, "done")
        spawns = [item for item in self.evidence() if item["kind"] == "spawn"]
        self.assertEqual(len(spawns), 2)
        resume_argv = spawns[-1]["argv"]
        self.assertEqual(
            resume_argv,
            [
                "--agent",
                f"exocore-runtime-{str(self.binding_id).replace('-', '')}",
                "--model",
                "gemini-3.1-pro-high",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--print-timeout",
                "3s",
                "--sandbox",
                "--conversation",
                generation.provider_session_id,
            ],
        )
        forbidden = {
            "--add-dir",
            "--dangerously-skip-permissions",
            "--mode",
            "--project",
            "--prompt",
            "-p",
        }
        self.assertTrue(forbidden.isdisjoint(resume_argv))
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
                TurnRequest(request_id=uuid4(), user_message="after retire"),
            )
        self.assertEqual(restarted_adapter.supervisor.quota_snapshot, {"weekly": 84, "5h": 93})

    async def test_antigravity_rejects_fake_control_and_later_bootstrap_before_request_row(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        bad_behavior = TurnRequest(
            request_id=uuid4(),
            user_message="bad",
            bootstrap_context={"history": []},
            behavior="exception",
        )
        with self.assertRaises(InvalidRequestError):
            service.preflight_turn(self.binding_id, bad_behavior)
        self.assertIsNone(
            service.store.get_request(str(self.binding_id), str(bad_behavior.request_id))
        )
        first = TurnRequest(
            request_id=uuid4(),
            user_message="first",
            bootstrap_context={"history": []},
        )
        await collect(service, self.binding_id, first)
        later_bootstrap = TurnRequest(
            request_id=uuid4(),
            user_message="later",
            bootstrap_context={"history": []},
        )
        with self.assertRaises(Exception):
            service.preflight_turn(self.binding_id, later_bootstrap)
        self.assertIsNone(
            service.store.get_request(str(self.binding_id), str(later_bootstrap.request_id))
        )

    async def test_stale_presend_mailbox_fails_once_then_generation_remains_usable(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = TurnRequest(
            request_id=uuid4(),
            user_message="stale request",
            bootstrap_context={"history": []},
            ephemeral_current="STALE-PRIVATE-CANARY",
        )
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
        self.assertEqual(generation.status, "active")
        self.assertFalse(generation.bootstrap_sent)
        self.assertFalse(mailbox.pending_path.exists())
        self.assertFalse((mailbox.root / "claimed.json").exists())

        next_request = TurnRequest(
            request_id=uuid4(),
            user_message="usable after stale",
            bootstrap_context={"history": []},
        )
        next_events = await collect(service, self.binding_id, next_request)
        self.assertEqual(next_events[-1].event_type, "done")
        persisted = b"".join(
            path.read_bytes()
            for path in self.data_root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(b"STALE-PRIVATE-CANARY", persisted)

    async def test_explicit_ensure_identity_failure_removes_pending_plaintext_before_refusal(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        mailbox = adapter._mailbox(str(self.binding_id))
        request_id = str(uuid4())
        mailbox.prepare(request_id, "ENSURE-IDENTITY-PRIVATE-CANARY")
        identity_path = mailbox.root / "identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["generation_id"] = "tampered-generation"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")

        with self.assertRaises(ProviderAdapterError) as caught:
            await service.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(caught.exception.code, "ephemeral_identity_mismatch")
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
        request = TurnRequest(
            request_id=uuid4(),
            user_message="identity failure",
            bootstrap_context={"history": []},
            ephemeral_current="IDENTITY-PRIVATE-CANARY",
        )
        mailbox = adapter._mailbox(str(self.binding_id))
        mailbox.prepare(str(request.request_id), request.ephemeral_current)
        identity_path = mailbox.root / "identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["generation_id"] = "tampered-generation"
        identity_path.write_text(json.dumps(identity), encoding="utf-8")

        events = await collect(service, self.binding_id, request)
        self.assertEqual(events[-1].payload, {"code": "ephemeral_identity_mismatch"})
        self.assertEqual(
            service.store.get_generation(str(self.binding_id)).status,
            "failed",
        )
        self.assertFalse(mailbox.pending_path.exists())
        self.assertFalse((mailbox.root / "claimed.json").exists())
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)
        persisted = b"".join(
            path.read_bytes()
            for path in self.data_root.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(b"IDENTITY-PRIVATE-CANARY", persisted)

    async def test_post_mailbox_presend_failure_removes_plaintext_and_fails_generation(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = TurnRequest(
            request_id=uuid4(),
            user_message="render failure",
            bootstrap_context={"history": []},
            ephemeral_current="POST-WRITE-PRIVATE-CANARY",
        )
        with patch(
            "exocore_runtime.providers.antigravity.adapter.render_stdin_line",
            side_effect=TypeError("fixture render failure"),
        ):
            events = await collect(service, self.binding_id, request)
        self.assertEqual(events[-1].payload, {"code": "agy_turn_prepare_failed"})
        self.assertEqual(
            service.store.get_generation(str(self.binding_id)).status,
            "failed",
        )
        self.assertFalse(
            service.store.get_generation(str(self.binding_id)).bootstrap_sent
        )
        mailbox = adapter._mailbox(str(self.binding_id))
        self.assertFalse(mailbox.pending_path.exists())
        self.assertFalse((mailbox.root / "claimed.json").exists())
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
                    update={
                        "bootstrap_fingerprint": f"bootstrap-{scenario}",
                        "config_fingerprint": f"config-{scenario}",
                    }
                )
                await service.ensure_generation(binding_id, spec)
                request = TurnRequest(
                    request_id=uuid4(),
                    user_message="fault",
                    bootstrap_context={"history": []},
                    ephemeral_current="EPHEMERAL-CANARY fault",
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

    async def test_explicit_ensure_restores_security_artifacts_before_resume_spawn(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        await service.shutdown()
        self.services.remove(service)
        generation_root = next(self.data_root.iterdir())
        profile = generation_root / "profile"
        hooks_path = profile / ".gemini" / "config" / "hooks.json"
        settings_path = profile / ".gemini" / "antigravity-cli" / "settings.json"
        agent_path = next((profile / ".gemini" / "config" / "agents").glob("*/agent.md"))
        hooks_path.unlink()
        settings_path.write_text(
            json.dumps({"modelProvider": "account_default", "permissions": {"deny": []}}),
            encoding="utf-8",
        )
        agent_path.write_text("tampered custom agent", encoding="utf-8")
        (generation_root / "workspace" / "pollution.txt").write_text(
            "workspace canary",
            encoding="utf-8",
        )

        restarted, _ = self.build_service()
        await restarted.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "spawn"]), 2)
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(set(hooks), {"exocore-runtime-ephemeral"})
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings["modelProvider"], "account_default")
        self.assertEqual(settings["permissions"]["deny"], list(DENY_POLICY))
        self.assertIn(self.system_canary, agent_path.read_text(encoding="utf-8"))
        self.assertFalse(any((generation_root / "workspace").iterdir()))

    async def test_lazy_prepare_fails_before_spawn_when_custom_agent_is_unverifiable(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        await service.shutdown()
        self.services.remove(service)
        generation_root = next(self.data_root.iterdir())
        profile = generation_root / "profile"
        hooks_path = profile / ".gemini" / "config" / "hooks.json"
        settings_path = profile / ".gemini" / "antigravity-cli" / "settings.json"
        agent_path = next(
            (profile / ".gemini" / "config" / "agents").glob("*/agent.md")
        )
        original_hooks = hooks_path.read_bytes()
        original_settings = settings_path.read_bytes()
        original_agent = agent_path.read_bytes()

        restarted, adapter = self.build_service()
        durable_generation = restarted.store.get_generation(str(self.binding_id))
        identity_hash = durable_generation.identity_hash
        expected_session = durable_generation.provider_session_id
        for path, replacement, expected_code in (
            (hooks_path, b"{}", "agy_hook_policy_invalid"),
            (
                settings_path,
                b'{"modelProvider":"account_default","permissions":{"deny":[]}}',
                "agy_deny_policy_invalid",
            ),
            (agent_path, b"tampered custom agent", "agy_custom_agent_invalid"),
        ):
            with self.subTest(path=path.name):
                path.write_bytes(replacement)
                with self.assertRaises(ProviderAdapterError) as caught:
                    adapter._load_layout(
                        str(self.binding_id),
                        identity_hash,
                        expected_session,
                    )
                self.assertEqual(caught.exception.code, expected_code)
                if path == hooks_path:
                    path.write_bytes(original_hooks)
                elif path == settings_path:
                    path.write_bytes(original_settings)
                else:
                    path.write_bytes(original_agent)
        pollution = generation_root / "workspace" / "pollution.txt"
        pollution.write_text("workspace canary", encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            adapter._load_layout(
                str(self.binding_id),
                identity_hash,
                expected_session,
            )
        self.assertEqual(caught.exception.code, "agy_workspace_invalid")
        pollution.unlink()

        metadata_path = generation_root / "generation.json"
        original_metadata = metadata_path.read_bytes()
        metadata = json.loads(original_metadata)
        metadata["provider_model_id"] = "tampered-provider-model"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            adapter._load_layout(
                str(self.binding_id),
                identity_hash,
                expected_session,
            )
        self.assertEqual(caught.exception.code, "agy_artifact_identity_mismatch")
        metadata_path.write_bytes(original_metadata)

        metadata = json.loads(original_metadata)
        metadata["provider_session_id"] = "tampered-provider-session"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            adapter._load_layout(
                str(self.binding_id),
                identity_hash,
                expected_session,
            )
        self.assertEqual(caught.exception.code, "agy_artifact_identity_mismatch")
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
            adapter._load_layout(
                str(self.binding_id),
                identity_hash,
                expected_session,
            )
        self.assertEqual(caught.exception.code, "agy_custom_agent_invalid")
        metadata_path.write_bytes(original_metadata)

        agent_path.write_text("tampered custom agent", encoding="utf-8")
        request = TurnRequest(
            request_id=uuid4(),
            user_message="lazy resume",
            bootstrap_context={"history": []},
        )
        events = await collect(restarted, self.binding_id, request)
        self.assertEqual(events[-1].payload, {"code": "agy_custom_agent_invalid"})
        self.assertEqual(
            restarted.store.get_generation(str(self.binding_id)).status,
            "failed",
        )
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "spawn"]), 1)
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)

    async def test_gateway_and_parent_secrets_are_not_inherited_by_agy(self) -> None:
        service, _ = self.build_service()
        with patch.dict(
            os.environ,
            {
                "EXOCORE_RUNTIME_TOKEN": "bearer-secret-canary",
                "OTHER_SECRET_VALUE": "parent-secret-canary",
            },
        ):
            await service.ensure_generation(self.binding_id, self.spec)
        spawn = next(item for item in self.evidence() if item["kind"] == "spawn")
        self.assertEqual(spawn["sensitive_env_present"], [])

    async def test_concurrent_distinct_first_turns_terminalize_bootstrap_loser(self) -> None:
        service, _ = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        first = TurnRequest(
            request_id=uuid4(),
            user_message="first contender",
            bootstrap_context={"anchor": "first"},
        )
        second = TurnRequest(
            request_id=uuid4(),
            user_message="second contender",
            bootstrap_context={"anchor": "second"},
        )
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

    async def test_concurrent_adapter_ensure_launches_one_cli_process(self) -> None:
        _, adapter = self.build_service()
        acquired = await asyncio.gather(
            adapter.ensure_generation(str(self.binding_id), self.spec),
            adapter.ensure_generation(str(self.binding_id), self.spec),
        )
        self.assertEqual(acquired[0].provider_session_id, acquired[1].provider_session_id)
        self.assertEqual(
            len([item for item in self.evidence() if item["kind"] == "spawn"]),
            1,
        )

    async def test_model_mismatch_fails_generation_before_any_user_stdin(self) -> None:
        service, _ = self.build_service("model_mismatch")
        with self.assertRaises(ProviderAdapterError) as caught:
            await service.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(caught.exception.code, "agy_model_mismatch")
        self.assertEqual(
            service.store.get_generation(str(self.binding_id)).status,
            "failed",
        )
        self.assertFalse(any(item["kind"] == "turn" for item in self.evidence()))

    async def test_delayed_result_tail_is_rejected_before_next_stdin_send(self) -> None:
        service, _ = self.build_service("delayed_event_after_result")
        await service.ensure_generation(self.binding_id, self.spec)
        first = TurnRequest(
            request_id=uuid4(),
            user_message="first",
            bootstrap_context={"history": []},
        )
        first_events = await collect(service, self.binding_id, first)
        self.assertEqual(first_events[-1].event_type, "done")
        (self.root / "tail-release").touch()
        await asyncio.sleep(0.1)
        second = TurnRequest(request_id=uuid4(), user_message="second")
        second_events = await collect(service, self.binding_id, second)
        self.assertEqual(second_events[-1].payload, {"code": "agy_unsolicited_output"})
        self.assertEqual(
            service.store.get_request(str(self.binding_id), str(second.request_id)).status,
            "indeterminate",
        )
        self.assertEqual(len([item for item in self.evidence() if item["kind"] == "turn"]), 1)

    async def test_startup_faults_fail_before_user_stdin_and_timeout_process_is_reaped(self) -> None:
        scenarios = {
            "bad_version": "agy_version_unsupported",
            "auth_missing": "agy_auth_unavailable",
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
                    update={
                        "bootstrap_fingerprint": f"bootstrap-{scenario}",
                        "config_fingerprint": f"config-{scenario}",
                    }
                )
                with self.assertRaises(ProviderAdapterError) as caught:
                    await service.ensure_generation(uuid4(), spec)
                self.assertEqual(caught.exception.code, expected_code)
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
                    )
                    self.assertNotIn(str(process_id), check.stdout)
                self.state_path = original_state
                self.data_root = original_data
                self.evidence_path = original_evidence

    async def test_shutdown_attempts_mailbox_cleanup_when_supervisor_cleanup_reports_failure(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = TurnRequest(
            request_id=uuid4(),
            user_message="shutdown prepare",
            bootstrap_context={"history": []},
            ephemeral_current="SHUTDOWN-PRIVATE-CANARY",
        )
        generation = service.store.get_generation(str(self.binding_id))
        await adapter.prepare_turn(
            str(self.binding_id),
            request,
            is_first_turn=True,
            generation_identity_hash=generation.identity_hash,
            expected_provider_session_id=generation.provider_session_id,
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
        self.assertNotIn(str(self.binding_id), adapter._prepared)
        await adapter.supervisor.shutdown()

    async def test_shutdown_terminalizes_sent_turn_before_process_cleanup(self) -> None:
        service, _ = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        request = TurnRequest(
            request_id=uuid4(),
            user_message="shutdown",
            bootstrap_context={"history": []},
        )
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
        first = TurnRequest(
            request_id=uuid4(),
            user_message="sent before retire",
            bootstrap_context={"history": []},
        )
        second = TurnRequest(
            request_id=uuid4(),
            user_message="prepared before retire",
        )
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
                )
                if str(process_id) not in check.stdout:
                    break
                await asyncio.sleep(0.01)
            self.assertNotIn(str(process_id), check.stdout)

    async def test_owner_task_cancellation_closes_process_tree_and_both_ownership_maps(self) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        request = TurnRequest(
            request_id=uuid4(),
            user_message="owner disconnect",
            bootstrap_context={"history": []},
        )
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
        self.assertEqual(dispose_attempts, 3)
        self.assertIn(
            "cancelled stream process cleanup also failed",
            getattr(caught.exception, "__notes__", []),
        )
        durable = service.store.get_request(str(self.binding_id), str(request.request_id))
        self.assertEqual(durable.status, "cancelled")
        self.assertEqual(
            service.store.terminal_count(str(self.binding_id), str(request.request_id)),
            1,
        )
        self.assertNotIn(str(self.binding_id), adapter._prepared)
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)
        for process_id in process_ids:
            for _ in range(100):
                check = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if str(process_id) not in check.stdout:
                    break
                await asyncio.sleep(0.01)
            self.assertNotIn(str(process_id), check.stdout)

    async def test_idempotent_old_cancel_does_not_cancel_new_active_request(self) -> None:
        service, _ = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        old = TurnRequest(
            request_id=uuid4(),
            user_message="old request",
            bootstrap_context={"history": []},
        )
        old_owner = asyncio.create_task(collect(service, self.binding_id, old))
        for _ in range(200):
            record = service.store.get_request(str(self.binding_id), str(old.request_id))
            if record is not None and record.status == "sent":
                break
            await asyncio.sleep(0.01)
        await service.cancel(self.binding_id, old.request_id)
        await asyncio.wait_for(old_owner, timeout=3)

        current = TurnRequest(request_id=uuid4(), user_message="current request")
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
        request = TurnRequest(
            request_id=uuid4(),
            user_message="slow",
            bootstrap_context={"history": []},
        )
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
        )
        self.assertNotIn(str(child_pid), check.stdout)


if __name__ == "__main__":
    unittest.main()
