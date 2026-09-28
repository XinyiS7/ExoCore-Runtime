"""Deterministic AGY exact-request fence and certification races (Plan CP4 §6.3)."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from exocore_runtime.contracts import (
    GenerationSpec,
    TurnRequest,
    runtime_mcp_manifest_sha256,
)
from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.adapter import AntigravityAdapter
from exocore_runtime.providers.antigravity.ephemeral_hook import EphemeralMailbox
from exocore_runtime.providers.antigravity.process import (
    AgyProcessConfig,
    AgyProcessSupervisor,
)
from exocore_runtime.providers.base import ProviderCancelOutcome
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


# Windows: helper processes must never open a visible console window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


async def wait_until(predicate, *, attempts=2000):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


def process_running(process_id: int) -> bool:
    check = subprocess.run(
        ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=NO_WINDOW,
    )
    return str(process_id) in check.stdout


class AgyCancelRaceTests(unittest.IsolatedAsyncioTestCase):
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
        self.spec = GenerationSpec(
            runtime_kind="antigravity",
            bootstrap_fingerprint="bootstrap-1",
            system_instructions="SYSTEM-INSTRUCTIONS-PRIVATE-CANARY",
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
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        }
        process_config = AgyProcessConfig(
            command_prefix=(sys.executable, str(fixture)),
            init_timeout_seconds=1,
            idle_timeout_seconds=2,
            hard_timeout_seconds=5,
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

    def turn(self, *, bootstrap=None, request_id=None):
        return TurnRequest(
            request_id=request_id or uuid4(),
            user_message="CURRENT-USER-CANARY",
            requested_model_id="gemini-3.1-pro-preview",
            requested_thinking_level="auto",
            runtime_mcp_tools=({"name": "memory_search", "eager": True, "max_call_seconds": None},),
            bootstrap_context=bootstrap,
            ephemeral_current="EPHEMERAL-PRIVATE-CANARY",
        )

    def evidence(self):
        if not self.evidence_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.evidence_path.read_text(encoding="utf-8").splitlines()
        ]

    def turn_count(self) -> int:
        return len([item for item in self.evidence() if item["kind"] == "turn"])

    def request_states(self, adapter, binding_id=None):
        binding = str(binding_id or self.binding_id)
        return [key for key in adapter._requests if key[0] == binding]

    def terminal_count(self, service, request, binding_id=None):
        binding = str(binding_id or self.binding_id)
        return service.store.terminal_count(binding, str(request.request_id))

    def record(self, service, request, binding_id=None):
        binding = str(binding_id or self.binding_id)
        return service.store.get_request(binding, str(request.request_id))

    # ------------------------------------------------------------ CP4-B

    async def test_prestart_cancel_fences_exact_request_without_stdin(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        binding = str(self.binding_id)
        service.store.claim_request(
            binding,
            str(request.request_id),
            service._request_hash(request),
            request.requested_model_id,
            request.requested_thinking_level,
            "test-owner",
        )
        generation = service.store.get_generation(binding)
        resolution = adapter.resolve_execution(
            request.requested_model_id,
            request.requested_thinking_level,
        )
        resolution = resolution.model_copy(
            update={
                "process_options": resolution.process_options.model_copy(
                    update={
                        "mcp_manifest_sha256": runtime_mcp_manifest_sha256(
                            request.runtime_mcp_tools
                        )
                    }
                )
            }
        )
        service.store.freeze_resolution(
            binding, str(request.request_id), "test-owner", resolution
        )
        await adapter.prepare_turn(
            generation,
            request,
            resolution.process_options,
            is_first_turn=True,
        )
        mailbox = adapter._mailbox(binding)
        self.assertTrue(mailbox.pending_path.exists())
        self.assertEqual(self.turn_count(), 0)

        result = await service.cancel(self.binding_id, request.request_id)

        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.changed)
        self.assertEqual(self.record(service, request).status, "cancelled")
        self.assertEqual(self.turn_count(), 0)
        self.assertEqual(self.request_states(adapter), [])
        self.assertNotIn(binding, adapter.supervisor._sessions)
        self.assertFalse(mailbox.pending_path.exists())
        self.assertFalse((mailbox.root / "claimed.json").exists())
        # The fenced exact request can never be started by a stale owner.
        with self.assertRaises(ProviderAdapterError) as caught:
            async for _ in adapter.stream_turn(binding, request):
                pass
        self.assertEqual(caught.exception.code, "agy_turn_not_prepared")
        self.assertEqual(self.turn_count(), 0)

    async def test_late_prestart_fence_after_stream_entry_never_writes_stdin(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        real_begin = adapter.supervisor._begin_request
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_begin(binding_id, request_id, stdin_line):
            entered.set()
            await release.wait()
            return await real_begin(binding_id, request_id, stdin_line)

        with patch.object(adapter.supervisor, "_begin_request", side_effect=gated_begin):
            owner = asyncio.create_task(collect(service, self.binding_id, request))
            await asyncio.wait_for(entered.wait(), timeout=5)
            result = await service.cancel(self.binding_id, request.request_id)
            self.assertEqual(result.status, "cancelled")
            self.assertTrue(result.changed)
            release.set()
            events = await asyncio.wait_for(owner, timeout=3)

        self.assertEqual(events[-1].payload, {"code": "cancelled"})
        self.assertEqual(self.turn_count(), 0)
        self.assertEqual(self.terminal_count(service, request), 1)
        self.assertEqual(self.request_states(adapter), [])

    async def test_active_hard_cancel_kills_process_tree_under_two_seconds(self) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        await wait_until(
            lambda: [item for item in self.evidence() if item["kind"] == "child"]
        )
        child_pid = [item for item in self.evidence() if item["kind"] == "child"][-1][
            "child_pid"
        ]
        started = time.monotonic()

        result = await service.cancel(self.binding_id, request.request_id)
        events = await asyncio.wait_for(owner, timeout=3)
        await wait_until(lambda: not process_running(child_pid))
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.changed)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})
        self.assertEqual(self.record(service, request).status, "cancelled")
        self.assertEqual(self.terminal_count(service, request), 1)
        self.assertLess(elapsed, 2.0)
        self.assertFalse(process_running(child_pid))
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)

    async def test_abandoned_owner_cleanup_is_force_not_graceful(self) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        await wait_until(
            lambda: [item for item in self.evidence() if item["kind"] == "child"]
        )
        calls: list[bool] = []
        real_dispose = adapter.supervisor._dispose_session

        async def spy_dispose(session, *, force):
            calls.append(force)
            await real_dispose(session, force=force)

        with patch.object(adapter.supervisor, "_dispose_session", side_effect=spy_dispose):
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner

        self.assertEqual(calls, [True])
        self.assertEqual(self.record(service, request).status, "cancelled")
        self.assertEqual(self.record(service, request).terminal_code, "cancelled")
        self.assertEqual(self.terminal_count(service, request), 1)

    # ------------------------------------------------------------ RT-RACE-1

    async def test_explicit_cancel_joins_inflight_abandonment_instead_of_ownership_unknown(
        self,
    ) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        binding = str(self.binding_id)
        request = self.turn(bootstrap={"history": []})
        request_key = str(request.request_id)
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        await wait_until(
            lambda: [item for item in self.evidence() if item["kind"] == "child"]
        )

        begun = asyncio.Event()
        dispose_entered = asyncio.Event()
        release_dispose = asyncio.Event()
        join_seen = asyncio.Event()
        joins: list[tuple[str, str]] = []
        dispose_calls = 0
        real_begin = adapter.supervisor._begin_abandonment
        real_dispose = adapter.supervisor._dispose_session
        real_settle = adapter.supervisor.settle_abandonment

        def observed_begin(binding_id, request_id):
            task = real_begin(binding_id, request_id)
            begun.set()
            return task

        async def gated_dispose(session, *, force):
            nonlocal dispose_calls
            dispose_calls += 1
            dispose_entered.set()
            await release_dispose.wait()
            await real_dispose(session, force=force)

        async def observed_settle(binding_id, request_id):
            joins.append((binding_id, request_id))
            join_seen.set()
            return await real_settle(binding_id, request_id)

        with patch.object(adapter.supervisor, "_begin_abandonment", side_effect=observed_begin):
            with patch.object(adapter.supervisor, "_dispose_session", side_effect=gated_dispose):
                with patch.object(
                    adapter.supervisor, "settle_abandonment", side_effect=observed_settle
                ):
                    # The owner-stream teardown is inside the exact-request
                    # abandonment and its force-dispose is in flight: the
                    # immutable abandoned receipt is not published yet.
                    owner.cancel()
                    await asyncio.wait_for(begun.wait(), timeout=5)
                    await asyncio.wait_for(dispose_entered.wait(), timeout=5)
                    self.assertIsNone(
                        adapter.supervisor.abandoned_proof(binding, request_key)
                    )
                    self.assertFalse(self.record(service, request).terminal)

                    cancel_task = asyncio.create_task(
                        service.cancel(self.binding_id, request.request_id)
                    )
                    await asyncio.wait_for(join_seen.wait(), timeout=5)
                    # The explicit cancel joined the same settlement instead
                    # of classifying the in-flight disposal as unknown.
                    self.assertFalse(cancel_task.done())
                    self.assertEqual(joins, [(binding, request_key)])

                    release_dispose.set()
                    result = await asyncio.wait_for(cancel_task, timeout=5)
                    with self.assertRaises(asyncio.CancelledError):
                        await owner

        self.assertEqual(dispose_calls, 1)
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.changed)
        durable = self.record(service, request)
        self.assertEqual(durable.status, "cancelled")
        self.assertEqual(durable.terminal_code, "cancelled")
        self.assertEqual(self.terminal_count(service, request), 1)
        self.assertEqual(adapter.supervisor._abandoned, {})
        self.assertEqual(adapter.supervisor._abandonment_tasks, {})
        self.assertEqual(self.request_states(adapter), [])

    async def test_explicit_cancel_joins_inflight_abandonment_after_request_state_removal(
        self,
    ) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        binding = str(self.binding_id)
        request = self.turn(bootstrap={"history": []})
        request_key = str(request.request_id)
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        await wait_until(
            lambda: [item for item in self.evidence() if item["kind"] == "child"]
        )

        begun = asyncio.Event()
        dispose_entered = asyncio.Event()
        release_dispose = asyncio.Event()
        join_seen = asyncio.Event()
        dispose_calls = 0
        real_begin = adapter.supervisor._begin_abandonment
        real_dispose = adapter.supervisor._dispose_session
        real_settle = adapter.supervisor.settle_abandonment

        def observed_begin(binding_id, request_id):
            task = real_begin(binding_id, request_id)
            begun.set()
            return task

        async def gated_dispose(session, *, force):
            nonlocal dispose_calls
            dispose_calls += 1
            dispose_entered.set()
            await release_dispose.wait()
            await real_dispose(session, force=force)

        async def observed_settle(binding_id, request_id):
            join_seen.set()
            return await real_settle(binding_id, request_id)

        with patch.object(adapter.supervisor, "_begin_abandonment", side_effect=observed_begin):
            with patch.object(adapter.supervisor, "_dispose_session", side_effect=gated_dispose):
                with patch.object(
                    adapter.supervisor, "settle_abandonment", side_effect=observed_settle
                ):
                    owner.cancel()
                    await asyncio.wait_for(begun.wait(), timeout=5)
                    await asyncio.wait_for(dispose_entered.wait(), timeout=5)
                    # E2E run #5 observed exactly this: the adapter request
                    # state is already gone while the exact proof is still
                    # being generated. The classification must not depend on
                    # that registry.
                    adapter._requests.pop((binding, request_key), None)
                    self.assertEqual(self.request_states(adapter), [])

                    cancel_task = asyncio.create_task(
                        service.cancel(self.binding_id, request.request_id)
                    )
                    await asyncio.wait_for(join_seen.wait(), timeout=5)
                    self.assertFalse(cancel_task.done())

                    release_dispose.set()
                    result = await asyncio.wait_for(cancel_task, timeout=5)
                    with self.assertRaises(asyncio.CancelledError):
                        await owner

        self.assertEqual(dispose_calls, 1)
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.changed)
        self.assertEqual(self.record(service, request).terminal_code, "cancelled")
        self.assertEqual(self.terminal_count(service, request), 1)
        self.assertEqual(adapter.supervisor._abandoned, {})
        self.assertEqual(adapter.supervisor._abandonment_tasks, {})

    async def test_inflight_abandonment_cleanup_failure_stays_honest_indeterminate(
        self,
    ) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        binding = str(self.binding_id)
        request = self.turn(bootstrap={"history": []})
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        await wait_until(
            lambda: [item for item in self.evidence() if item["kind"] == "child"]
        )

        begun = asyncio.Event()
        dispose_entered = asyncio.Event()
        release_dispose = asyncio.Event()
        join_seen = asyncio.Event()
        dispose_calls = 0
        real_begin = adapter.supervisor._begin_abandonment
        real_settle = adapter.supervisor.settle_abandonment

        def observed_begin(binding_id, request_id):
            task = real_begin(binding_id, request_id)
            begun.set()
            return task

        async def failing_dispose(session, *, force):
            nonlocal dispose_calls
            dispose_calls += 1
            dispose_entered.set()
            if dispose_calls == 1:
                await release_dispose.wait()
            raise ProviderAdapterError("fixture_cleanup_report")

        async def observed_settle(binding_id, request_id):
            join_seen.set()
            return await real_settle(binding_id, request_id)

        with patch.object(adapter.supervisor, "_begin_abandonment", side_effect=observed_begin):
            with patch.object(adapter.supervisor, "_dispose_session", side_effect=failing_dispose):
                with patch.object(
                    adapter.supervisor, "settle_abandonment", side_effect=observed_settle
                ):
                    owner.cancel()
                    await asyncio.wait_for(begun.wait(), timeout=5)
                    await asyncio.wait_for(dispose_entered.wait(), timeout=5)
                    cancel_task = asyncio.create_task(
                        service.cancel(self.binding_id, request.request_id)
                    )
                    await asyncio.wait_for(join_seen.wait(), timeout=5)
                    self.assertFalse(cancel_task.done())

                    release_dispose.set()
                    result = await asyncio.wait_for(cancel_task, timeout=5)
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await owner

        self.assertIn(
            "cancelled stream process cleanup also failed",
            getattr(caught.exception, "__notes__", []),
        )
        # The abandonment cleanup failed; the cancel's own exact disposal is
        # a second attempt and also fails, so the honest outcome can only be
        # indeterminate - never a fabricated cancelled terminal.
        self.assertEqual(dispose_calls, 2)
        self.assertEqual(result.status, "indeterminate")
        self.assertTrue(result.changed)
        durable = self.record(service, request)
        self.assertEqual(durable.status, "indeterminate")
        self.assertEqual(durable.terminal_code, "cancel_cleanup_failed")
        self.assertEqual(self.terminal_count(service, request), 1)
        self.assertIn(binding, adapter.supervisor._sessions)
        self.assertTrue(adapter.supervisor._sessions[binding].poisoned)

    async def test_shutdown_waits_for_inflight_abandonment_without_deadlock(self) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        binding = str(self.binding_id)
        request = self.turn(bootstrap={"history": []})
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        await wait_until(
            lambda: [item for item in self.evidence() if item["kind"] == "child"]
        )

        begun = asyncio.Event()
        dispose_entered = asyncio.Event()
        release_dispose = asyncio.Event()
        real_begin = adapter.supervisor._begin_abandonment
        real_dispose = adapter.supervisor._dispose_session

        def observed_begin(binding_id, request_id):
            task = real_begin(binding_id, request_id)
            begun.set()
            return task

        async def gated_dispose(session, *, force):
            dispose_entered.set()
            await release_dispose.wait()
            await real_dispose(session, force=force)

        with patch.object(adapter.supervisor, "_begin_abandonment", side_effect=observed_begin):
            with patch.object(adapter.supervisor, "_dispose_session", side_effect=gated_dispose):
                owner.cancel()
                await asyncio.wait_for(begun.wait(), timeout=5)
                await asyncio.wait_for(dispose_entered.wait(), timeout=5)
                shutdown_task = asyncio.create_task(service.shutdown())
                await asyncio.sleep(0)
                # The in-flight exact-request cleanup gates the process tree,
                # so shutdown cannot have finished; once released it must
                # settle without deadlocking or leaking proof residue.
                self.assertFalse(shutdown_task.done())
                release_dispose.set()
                await asyncio.wait_for(shutdown_task, timeout=5)
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.services.remove(service)
        self.assertNotIn(binding, adapter.supervisor._sessions)
        self.assertEqual(adapter.supervisor._abandonment_tasks, {})
        self.assertEqual(adapter.supervisor._abandoned, {})

    async def test_request_a_proof_is_not_consumed_by_request_b(self) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        binding = str(self.binding_id)
        first = self.turn(bootstrap={"history": []})
        first_owner = asyncio.create_task(collect(service, self.binding_id, first))
        await wait_until(
            lambda: [item for item in self.evidence() if item["kind"] == "child"]
        )
        first_owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_owner
        self.assertEqual(self.record(service, first).status, "cancelled")
        # The consumed abandoned proof is released with the request's durable
        # ack; a different exact request gets its own honest classification.
        self.assertIsNone(
            adapter.supervisor.abandoned_proof(binding, str(first.request_id))
        )
        other = self.turn()
        receipt = await adapter.cancel(binding, str(other.request_id))
        self.assertEqual(receipt.outcome, ProviderCancelOutcome.OWNERSHIP_UNKNOWN)
        self.assertIsNone(receipt.natural_terminal)
        self.assertEqual(self.terminal_count(service, first), 1)

    async def test_cancel_of_noncurrent_request_never_closes_the_active_session(self) -> None:
        service, adapter = self.build_service("slow_tree")
        await service.ensure_generation(self.binding_id, self.spec)
        binding = str(self.binding_id)
        request = self.turn(bootstrap={"history": []})
        owner = asyncio.create_task(collect(service, self.binding_id, request))
        await wait_until(
            lambda: [item for item in self.evidence() if item["kind"] == "child"]
        )
        ghost = self.turn()

        receipt = await adapter.cancel(binding, str(ghost.request_id))

        self.assertEqual(receipt.outcome, ProviderCancelOutcome.OWNERSHIP_UNKNOWN)
        self.assertEqual(self.record(service, request).status, "sent")
        self.assertIn(binding, adapter.supervisor._sessions)
        self.assertFalse(owner.done())
        result = await service.cancel(self.binding_id, request.request_id)
        self.assertEqual(result.status, "cancelled")
        events = await asyncio.wait_for(owner, timeout=3)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})

    # ------------------------------------------------------------ CP4-C

    async def test_cancel_certifies_candidate_and_rescues_certified_done(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        binding = str(self.binding_id)
        request_key = str(request.request_id)
        validations = 0
        real_validate = EphemeralMailbox.validate_receipt

        def counting_validate(self_mailbox, request_id, payload_hash):
            nonlocal validations
            validations += 1
            return real_validate(self_mailbox, request_id, payload_hash)

        real_certify = adapter._certify_request
        owner_blocked = asyncio.Event()
        release_owner = asyncio.Event()
        certify_calls = 0

        async def gated_certify(binding_id, request_id, candidate):
            nonlocal certify_calls
            certify_calls += 1
            if certify_calls == 1:
                owner_blocked.set()
                await release_owner.wait()
            return await real_certify(binding_id, request_id, candidate)

        with patch.object(EphemeralMailbox, "validate_receipt", counting_validate):
            with patch.object(adapter, "_certify_request", side_effect=gated_certify):
                owner = asyncio.create_task(collect(service, binding, request))
                await asyncio.wait_for(owner_blocked.wait(), timeout=5)
                state = adapter._requests[(binding, request_key)]
                self.assertIsNone(state.certified_terminal)
                self.assertIsNotNone(adapter.supervisor.request_candidate(binding, request_key))

                result = await service.cancel(self.binding_id, request.request_id)
                self.assertEqual(result.status, "completed")
                self.assertFalse(result.changed)

                release_owner.set()
                events = await asyncio.wait_for(owner, timeout=3)

        self.assertEqual(certify_calls, 2)
        self.assertEqual(validations, 1)
        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(self.record(service, request).status, "completed")
        self.assertEqual(self.terminal_count(service, request), 1)

    async def test_mailbox_failure_is_identical_for_owner_and_cancel_rescue(self) -> None:
        service, adapter = self.build_service("no_receipt")
        await service.ensure_generation(self.binding_id, self.spec)
        request = self.turn(bootstrap={"history": []})
        binding = str(self.binding_id)
        request_key = str(request.request_id)
        real_certify = adapter._certify_request
        owner_blocked = asyncio.Event()
        release_owner = asyncio.Event()
        certify_calls = 0

        async def gated_certify(binding_id, request_id, candidate):
            nonlocal certify_calls
            certify_calls += 1
            if certify_calls == 1:
                owner_blocked.set()
                await release_owner.wait()
            return await real_certify(binding_id, request_id, candidate)

        with patch.object(adapter, "_certify_request", side_effect=gated_certify):
            owner = asyncio.create_task(collect(service, binding, request))
            await asyncio.wait_for(owner_blocked.wait(), timeout=5)

            result = await service.cancel(self.binding_id, request.request_id)
            self.assertEqual(result.status, "indeterminate")
            self.assertFalse(result.changed)

            release_owner.set()
            events = await asyncio.wait_for(owner, timeout=3)

        self.assertEqual(certify_calls, 2)
        self.assertEqual(
            events[-1].payload,
            {"code": "ephemeral_receipt_missing"},
        )
        self.assertEqual(events[-1].terminal_status, "indeterminate")
        record = self.record(service, request)
        self.assertEqual(record.status, "indeterminate")
        self.assertEqual(record.terminal_code, "ephemeral_receipt_missing")
        self.assertEqual(self.terminal_count(service, request), 1)
        self.assertNotIn(str(self.binding_id), adapter.supervisor._sessions)
        persisted = b"".join(
            path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        )
        self.assertNotIn(b"EPHEMERAL-PRIVATE-CANARY", persisted)

    async def test_certified_proof_survives_until_reclaim_and_stays_bounded(self) -> None:
        service, adapter = self.build_service()
        await service.ensure_generation(self.binding_id, self.spec)
        binding = str(self.binding_id)
        request = self.turn(bootstrap={"history": []})
        request_key = str(request.request_id)
        observed: list[bool | None] = []
        real_reclaim = adapter.reclaim_request

        def spy_reclaim(binding_id, request_id):
            state = adapter._requests.get((binding_id, request_id))
            observed.append(
                state.certified_terminal is not None if state is not None else None
            )
            real_reclaim(binding_id, request_id)

        adapter.reclaim_request = spy_reclaim
        events = await collect(service, binding, request)

        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(observed, [True])
        self.assertEqual(self.request_states(adapter), [])
        self.assertIsNone(
            adapter.supervisor.request_candidate(binding, request_key)
        )
        self.assertIsNone(adapter.supervisor.abandoned_proof(binding, request_key))
        self.assertIn(binding, adapter.supervisor._sessions)


if __name__ == "__main__":
    unittest.main()
