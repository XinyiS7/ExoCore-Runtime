"""Deterministic whole-request cancel arbitration races (Plan CP4 §6.2).

Every race here is driven by explicit ``asyncio.Event`` barriers and bounded
condition polling; ordering is never guessed with sleep-based timing.
"""

import asyncio
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from exocore_runtime.contracts import GenerationSpec, ProviderEvent, TurnRequest
from exocore_runtime.errors import ConflictError
from exocore_runtime.providers.base import ProviderCancelOutcome
from exocore_runtime.providers.fake import DeterministicFakeAdapter
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


async def wait_until(predicate, *, attempts=600):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


def turn(*, request_id=None, thinking="auto", bootstrap=None):
    return TurnRequest(
        request_id=request_id or uuid4(),
        user_message="hello",
        requested_model_id="gemini-3.1-pro-preview",
        requested_thinking_level=thinking,
        bootstrap_context=bootstrap,
    )


class CancelArbitrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = RuntimeStateStore(Path(self.temp.name) / "runtime.sqlite3")
        self.provider = DeterministicFakeAdapter()
        self.service = RuntimeService(self.store, self.provider)
        self.binding_id = uuid4()
        self.spec = GenerationSpec(
            runtime_kind="fake",
            bootstrap_fingerprint="bootstrap-1",
            system_instructions="system",
        )
        await self.service.ensure_generation(self.binding_id, self.spec)

    async def asyncTearDown(self) -> None:
        try:
            await self.service.shutdown()
        except Exception:
            pass
        self.temp.cleanup()

    def key(self, request) -> tuple[str, str]:
        return (str(self.binding_id), str(request.request_id))

    async def start_owner(self, request, behavior: str | None = None):
        if behavior is not None:
            self.provider.set_behavior(str(request.request_id), behavior)
        return asyncio.create_task(collect(self.service, self.binding_id, request))

    async def wait_sent(self, request) -> None:
        key = self.key(request)
        await wait_until(lambda: self.provider.turn_sends[key] > 0)

    def record(self, request):
        return self.store.get_request(*self.key(request))

    def terminal_count(self, request) -> int:
        return self.store.terminal_count(*self.key(request))

    # ---------------------------------------------------------------- CP4-A

    async def test_kill_failure_never_persists_cancelled(self) -> None:
        request = turn(bootstrap={"history": []})
        self.provider.set_cancel_failure(str(request.request_id))
        owner = await self.start_owner(request, "cancel_late")
        await self.wait_sent(request)

        result = await self.service.cancel(self.binding_id, request.request_id)

        self.assertEqual(result.status, "indeterminate")
        self.assertTrue(result.changed)
        record = self.record(request)
        self.assertEqual(record.status, "indeterminate")
        self.assertEqual(record.terminal_code, "cancel_cleanup_failed")
        self.assertEqual(self.terminal_count(request), 1)
        self.assertEqual(self.service._arbiters, {})
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner

    async def test_cancelled_prestart_never_reaches_provider_input(self) -> None:
        request = turn(bootstrap={"history": []})
        owner = await self.start_owner(request, "prepare_blocked")
        await wait_until(
            lambda: self.provider.process_prepares[str(self.binding_id)] > 0
        )

        result = await self.service.cancel(self.binding_id, request.request_id)

        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.changed)
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})
        key = self.key(request)
        self.assertEqual(self.provider.turn_sends[key], 0)
        self.assertTrue(self.provider.is_send_fenced(str(request.request_id)))
        self.assertEqual(self.terminal_count(request), 1)
        replay = await collect(self.service, self.binding_id, request)
        self.assertEqual(events, replay)

    async def test_duplicate_cancel_runs_one_settlement_and_one_provider_kill(self) -> None:
        request = turn(bootstrap={"history": []})
        owner = await self.start_owner(request, "cancel_late")
        await self.wait_sent(request)
        gate = self.provider.set_cancel_gate(str(request.request_id))

        first = asyncio.create_task(self.service.cancel(self.binding_id, request.request_id))
        second = asyncio.create_task(self.service.cancel(self.binding_id, request.request_id))
        await wait_until(lambda: self.provider.cancel_calls[self.key(request)] == 1)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        gate.set()
        results = await asyncio.gather(first, second)
        statuses = sorted(result.changed for result in results)
        self.assertEqual(statuses, [False, True])
        self.assertEqual(self.provider.cancel_calls[self.key(request)], 1)
        self.assertEqual(self.terminal_count(request), 1)
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})

    async def test_disconnected_first_caller_still_settles(self) -> None:
        request = turn(bootstrap={"history": []})
        owner = await self.start_owner(request, "cancel_late")
        await self.wait_sent(request)
        gate = self.provider.set_cancel_gate(str(request.request_id))

        caller = asyncio.create_task(self.service.cancel(self.binding_id, request.request_id))
        await wait_until(lambda: self.provider.cancel_calls[self.key(request)] == 1)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertNotIn(self.key(request), self.provider.cancel_cancelled)

        gate.set()
        await wait_until(lambda: self.record(request).terminal)
        record = self.record(request)
        self.assertEqual(record.status, "cancelled")
        self.assertEqual(self.terminal_count(request), 1)
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})

    async def test_certified_natural_done_rescue_is_completed_not_cancelled(self) -> None:
        request = turn(bootstrap={"history": []})
        owner = await self.start_owner(request, "cancel_late")
        await self.wait_sent(request)
        self.provider.set_cancel_receipt(
            str(request.request_id),
            ProviderCancelOutcome.NATURAL_TERMINAL_READY,
            ProviderEvent(event_type="done", payload={"finish_reason": "stop"}),
        )

        result = await self.service.cancel(self.binding_id, request.request_id)

        self.assertEqual(result.status, "completed")
        self.assertFalse(result.changed)
        record = self.record(request)
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.terminal_code, "completed")
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(self.terminal_count(request), 1)

    async def test_certified_natural_error_rescue_keeps_provider_terminal(self) -> None:
        request = turn(bootstrap={"history": []})
        owner = await self.start_owner(request, "cancel_late")
        await self.wait_sent(request)
        self.provider.set_cancel_receipt(
            str(request.request_id),
            ProviderCancelOutcome.NATURAL_TERMINAL_READY,
            ProviderEvent(
                event_type="error",
                payload={"code": "fake_provider_error"},
                terminal_status="failed",
            ),
        )

        result = await self.service.cancel(self.binding_id, request.request_id)

        self.assertEqual(result.status, "failed")
        self.assertFalse(result.changed)
        record = self.record(request)
        self.assertEqual(record.terminal_code, "fake_provider_error")
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(events[-1].payload, {"code": "fake_provider_error"})
        self.assertEqual(self.terminal_count(request), 1)

    async def test_late_cancel_after_natural_terminal_never_calls_provider(self) -> None:
        request = turn(bootstrap={"history": []})
        events = await collect(self.service, self.binding_id, request)
        self.assertEqual(events[-1].event_type, "done")

        result = await self.service.cancel(self.binding_id, request.request_id)

        self.assertEqual(result.status, "completed")
        self.assertFalse(result.changed)
        self.assertEqual(self.provider.cancel_calls[self.key(request)], 0)

        failed = turn()
        self.provider.set_behavior(str(failed.request_id), "provider_error")
        failed_events = await collect(self.service, self.binding_id, failed)
        self.assertEqual(failed_events[-1].event_type, "error")

        failed_cancel = await self.service.cancel(self.binding_id, failed.request_id)

        self.assertEqual(failed_cancel.status, "failed")
        self.assertFalse(failed_cancel.changed)
        self.assertEqual(self.provider.cancel_calls[self.key(failed)], 0)
        self.assertEqual(self.record(failed).terminal_code, "fake_provider_error")

    async def test_ownership_unknown_receipt_is_honest_indeterminate(self) -> None:
        request = turn(bootstrap={"history": []})
        owner = await self.start_owner(request, "cancel_late")
        await self.wait_sent(request)
        self.provider.set_cancel_receipt(
            str(request.request_id),
            ProviderCancelOutcome.OWNERSHIP_UNKNOWN,
        )

        result = await self.service.cancel(self.binding_id, request.request_id)

        self.assertEqual(result.status, "indeterminate")
        self.assertTrue(result.changed)
        record = self.record(request)
        self.assertEqual(record.terminal_code, "cancel_ownership_unknown")
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(events[-1].payload, {"code": "cancel_ownership_unknown"})
        self.assertEqual(self.terminal_count(request), 1)

    async def test_cancelled_abandoned_receipt_is_cancelled(self) -> None:
        request = turn(bootstrap={"history": []})
        owner = await self.start_owner(request, "cancel_late")
        await self.wait_sent(request)
        self.provider.set_cancel_receipt(
            str(request.request_id),
            ProviderCancelOutcome.CANCELLED_ABANDONED,
        )

        result = await self.service.cancel(self.binding_id, request.request_id)

        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.changed)
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})
        self.assertEqual(self.terminal_count(request), 1)

    async def test_owner_stream_tails_cannot_steal_the_terminal_from_cancel(self) -> None:
        cases = {
            "gated_eof": "unexpected_provider_eof",
            "gated_exception": "provider_exception",
            "gated_error": "fake_provider_error",
            "gated_malformed": "malformed_provider_event",
        }
        for behavior, losing_code in cases.items():
            with self.subTest(behavior=behavior):
                binding_id = uuid4()
                await self.service.ensure_generation(binding_id, self.spec)
                request = turn(bootstrap={"history": []})
                self.provider.set_behavior(str(request.request_id), behavior)
                owner = asyncio.create_task(
                    collect(self.service, binding_id, request)
                )
                await wait_until(
                    lambda: self.provider.turn_sends[(str(binding_id), str(request.request_id))] > 0
                )
                gate = self.provider.set_cancel_gate(str(request.request_id))
                cancel_task = asyncio.create_task(
                    self.service.cancel(binding_id, request.request_id)
                )
                key = (str(binding_id), str(request.request_id))
                await wait_until(
                    lambda: self.provider.cancel_calls[key] == 1
                )
                self.provider.release_stream(str(request.request_id))
                done, _ = await asyncio.wait({owner}, timeout=0.05)
                self.assertEqual(done, set())
                durable = self.store.get_request(str(binding_id), str(request.request_id))
                self.assertFalse(durable.terminal)
                gate.set()
                result = await asyncio.wait_for(cancel_task, timeout=2)
                self.assertEqual(result.status, "cancelled")
                self.assertTrue(result.changed)
                events = await asyncio.wait_for(owner, timeout=2)
                self.assertEqual(events[-1].payload, {"code": "cancelled"})
                self.assertEqual(
                    self.store.terminal_count(str(binding_id), str(request.request_id)),
                    1,
                )
                journal = "".join(
                    event.model_dump_json()
                    for event in self.store.read_events(
                        str(binding_id), str(request.request_id)
                    )
                )
                self.assertNotIn(losing_code, journal)

    async def test_prestream_owner_error_cannot_steal_the_terminal(self) -> None:
        request = turn(bootstrap={"history": []})
        owner = await self.start_owner(request, "prepare_blocked_error")
        await wait_until(
            lambda: self.provider.process_prepares[str(self.binding_id)] > 0
        )
        gate = self.provider.set_cancel_gate(str(request.request_id))
        cancel_task = asyncio.create_task(
            self.service.cancel(self.binding_id, request.request_id)
        )
        await wait_until(lambda: self.provider.cancel_calls[self.key(request)] == 1)
        self.provider.release_prepare(str(request.request_id))
        await asyncio.sleep(0)
        self.assertFalse(owner.done())
        gate.set()
        result = await asyncio.wait_for(cancel_task, timeout=2)
        self.assertEqual(result.status, "cancelled")
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})
        journal = "".join(
            event.model_dump_json()
            for event in self.store.read_events(*self.key(request))
        )
        self.assertNotIn("fake_prepare_failure", journal)

    async def test_shutdown_waits_for_admitted_settlement_and_never_cancels_it(self) -> None:
        request = turn(bootstrap={"history": []})
        owner = await self.start_owner(request, "cancel_late")
        await self.wait_sent(request)
        gate = self.provider.set_cancel_gate(str(request.request_id))
        cancel_task = asyncio.create_task(
            self.service.cancel(self.binding_id, request.request_id)
        )
        await wait_until(lambda: self.provider.cancel_calls[self.key(request)] == 1)

        shutdown_task = asyncio.create_task(self.service.shutdown())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(shutdown_task.done())
        gate.set()
        result = await asyncio.wait_for(cancel_task, timeout=2)
        await asyncio.wait_for(shutdown_task, timeout=2)
        self.assertEqual(result.status, "cancelled")
        self.assertNotIn(self.key(request), self.provider.cancel_cancelled)
        record = self.record(request)
        self.assertEqual(record.status, "cancelled")
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})

    async def test_shutdown_refuses_new_cancel_admission_for_other_open_request(self) -> None:
        first = turn(bootstrap={"history": []})
        second = turn()
        first_owner = await self.start_owner(first, "cancel_late")
        await self.wait_sent(first)
        second_owner = asyncio.create_task(
            collect(self.service, self.binding_id, second)
        )
        await wait_until(lambda: self.record(second) is not None)
        gate = self.provider.set_cancel_gate(str(first.request_id))
        cancel_task = asyncio.create_task(
            self.service.cancel(self.binding_id, first.request_id)
        )
        await wait_until(lambda: self.provider.cancel_calls[self.key(first)] == 1)

        shutdown_task = asyncio.create_task(self.service.shutdown())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with self.assertRaises(ConflictError):
            await self.service.cancel(self.binding_id, second.request_id)
        gate.set()
        await asyncio.wait_for(cancel_task, timeout=2)
        await asyncio.wait_for(shutdown_task, timeout=2)
        first_events = await asyncio.wait_for(first_owner, timeout=2)
        second_events = await asyncio.wait_for(second_owner, timeout=2)
        self.assertEqual(first_events[-1].payload, {"code": "cancelled"})
        # The second request was already admitted before shutdown; its outcome
        # may be either a naturally completed turn or the shutdown terminal,
        # but it must be exactly one durable terminal (no dangling state).
        self.assertTrue(second_events[-1].terminal)
        self.assertEqual(self.terminal_count(second), 1)

    async def test_reclaim_runs_only_after_the_durable_terminal(self) -> None:
        observed: list[str | None] = []
        real_reclaim = self.provider.reclaim_request

        def spy(binding_id: str, request_id: str) -> None:
            record = self.store.get_request(binding_id, request_id)
            observed.append(record.status if record is not None else None)
            real_reclaim(binding_id, request_id)

        self.provider.reclaim_request = spy

        completed = turn(bootstrap={"history": []})
        await collect(self.service, self.binding_id, completed)

        cancelled = turn()
        self.provider.set_behavior(str(cancelled.request_id), "cancel_late")
        owner = asyncio.create_task(collect(self.service, self.binding_id, cancelled))
        await self.wait_sent(cancelled)
        await self.service.cancel(self.binding_id, cancelled.request_id)
        await asyncio.wait_for(owner, timeout=2)

        self.assertEqual(observed, ["completed", "cancelled"])
        self.assertEqual(self.provider.reclaims[self.key(completed)], 1)
        self.assertEqual(self.provider.reclaims[self.key(cancelled)], 1)

    async def test_cancelled_replay_after_restart_stays_consistent(self) -> None:
        request = turn(bootstrap={"history": []})
        self.provider.set_behavior(str(request.request_id), "cancel_late")
        owner = asyncio.create_task(collect(self.service, self.binding_id, request))
        await self.wait_sent(request)
        await self.service.cancel(self.binding_id, request.request_id)
        original = await asyncio.wait_for(owner, timeout=2)
        await self.service.shutdown()

        restarted_provider = DeterministicFakeAdapter()
        restarted = RuntimeService(
            RuntimeStateStore(self.store.path),
            restarted_provider,
        )
        replay = await collect(restarted, self.binding_id, request)
        await restarted.shutdown()

        self.assertEqual(
            [event.model_dump_json() for event in original],
            [event.model_dump_json() for event in replay],
        )
        self.assertEqual(replay[-1].terminal_status, "cancelled")
        self.assertEqual(
            restarted.store.terminal_count(*self.key(request)),
            1,
        )
        self.assertEqual(sum(restarted_provider.turn_sends.values()), 0)

    async def test_arbiter_registry_is_reclaimed_on_every_terminal_path(self) -> None:
        scenarios = {
            "completed": None,
            "failed": "provider_error",
            "unexpected_eof": "unexpected_eof",
            "cancel": "cancel_late",
        }
        for name, behavior in scenarios.items():
            with self.subTest(scenario=name):
                binding_id = uuid4()
                await self.service.ensure_generation(binding_id, self.spec)
                request = turn(bootstrap={"history": []})
                key = (str(binding_id), str(request.request_id))
                if behavior is not None:
                    self.provider.set_behavior(str(request.request_id), behavior)
                if name == "cancel":
                    owner = asyncio.create_task(collect(self.service, binding_id, request))
                    await wait_until(lambda: self.provider.turn_sends[key] > 0)
                    await self.service.cancel(binding_id, request.request_id)
                    await asyncio.wait_for(owner, timeout=2)
                else:
                    await collect(self.service, binding_id, request)
                self.assertTrue(self.store.get_request(*key).terminal)
                self.assertEqual(self.service._arbiters, {})


if __name__ == "__main__":
    unittest.main()
