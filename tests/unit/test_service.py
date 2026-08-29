import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from exocore_runtime.contracts import GenerationSpec, TurnRequest
from exocore_runtime.errors import ConflictError, RetiredError
from exocore_runtime.providers.fake import DeterministicFakeAdapter
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


def turn(*, request_id=None, thinking="auto", bootstrap=None):
    return TurnRequest(
        request_id=request_id or uuid4(),
        user_message="hello",
        requested_model_id="gemini-3.1-pro-preview",
        requested_thinking_level=thinking,
        bootstrap_context=bootstrap,
    )


class RuntimeServiceTests(unittest.IsolatedAsyncioTestCase):
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
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_generation_put_is_state_only_idempotent_and_identity_is_immutable(self) -> None:
        repeated = await self.service.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(repeated.status, "starting")
        # Staging is an idempotent artifact ensure while the generation is
        # starting; the process itself must never be spawned by a PUT.
        self.assertEqual(self.provider.generation_stages[str(self.binding_id)], 2)
        self.assertEqual(self.provider.process_spawns[str(self.binding_id)], 0)
        changed = self.spec.model_copy(update={"bootstrap_fingerprint": "bootstrap-2"})
        with self.assertRaises(ConflictError):
            await self.service.ensure_generation(self.binding_id, changed)
        changed_system = self.spec.model_copy(update={"system_instructions": "other"})
        with self.assertRaises(ConflictError):
            await self.service.ensure_generation(self.binding_id, changed_system)
        self.assertEqual(self.provider.generation_stages[str(self.binding_id)], 2)

    async def test_completed_request_replays_without_second_send(self) -> None:
        request = turn(bootstrap={"history": []})
        first = await collect(self.service, self.binding_id, request)
        second = await collect(self.service, self.binding_id, request)
        self.assertEqual(first, second)
        self.assertEqual(
            self.provider.turn_sends[(str(self.binding_id), str(request.request_id))],
            1,
        )
        self.assertEqual([event.sequence for event in first], list(range(1, len(first) + 1)))
        self.assertEqual(sum(event.terminal for event in first), 1)
        self.assertEqual(first[-1].terminal_status, "completed")
        self.assertIs(first[-1].bootstrap_consumed, True)
        self.assertEqual(
            [event.event_type for event in first[:2]],
            ["generation_activated", "execution_resolved"],
        )
        self.assertTrue(
            all(
                event.terminal_status is None and event.bootstrap_consumed is None
                for event in first[:-1]
            )
        )
        self.assertEqual(self.store.terminal_count(str(self.binding_id), str(request.request_id)), 1)

    async def test_completed_request_replays_after_new_service_lifecycle(self) -> None:
        request = turn(bootstrap={"history": []})
        first = await collect(self.service, self.binding_id, request)
        await self.service.shutdown()
        restarted_provider = DeterministicFakeAdapter()
        restarted = RuntimeService(RuntimeStateStore(self.store.path), restarted_provider)
        replay = await collect(restarted, self.binding_id, request)
        self.assertEqual(first, replay)
        self.assertEqual(sum(restarted_provider.turn_sends.values()), 0)
        await restarted.shutdown()

    async def test_prepared_request_can_continue_after_restart(self) -> None:
        request = turn(bootstrap={"history": []})
        payload_hash = self.service._request_hash(request)
        self.store.claim_request(
            str(self.binding_id),
            str(request.request_id),
            payload_hash,
            request.requested_model_id,
            request.requested_thinking_level,
            "dead-instance",
        )
        # No graceful shutdown: the owner process is presumed crashed, so the
        # durable prepared row is reclaimed by the restarted service owner.
        restarted_provider = DeterministicFakeAdapter()
        restarted = RuntimeService(RuntimeStateStore(self.store.path), restarted_provider)
        events = await collect(restarted, self.binding_id, request)
        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(sum(restarted_provider.turn_sends.values()), 1)
        await restarted.shutdown()

    async def test_sent_without_terminal_becomes_indeterminate_after_restart(self) -> None:
        request = turn(bootstrap={"history": []})
        payload_hash = self.service._request_hash(request)
        self.store.claim_request(
            str(self.binding_id),
            str(request.request_id),
            payload_hash,
            request.requested_model_id,
            request.requested_thinking_level,
            "dead-instance",
        )
        resolution = self.provider.resolve_execution(
            request.requested_model_id,
            request.requested_thinking_level,
        )
        self.store.freeze_resolution(
            str(self.binding_id),
            str(request.request_id),
            "dead-instance",
            resolution,
        )
        self.store.activate_generation(
            str(self.binding_id),
            f"session-{self.binding_id}",
            str(request.request_id),
        )
        self.store.mark_sent(
            str(self.binding_id),
            str(request.request_id),
            "dead-instance",
            consume_bootstrap=True,
        )
        # No graceful shutdown: the owner process is presumed crashed, so only
        # restart recovery may make the sent row indeterminate.
        restarted_provider = DeterministicFakeAdapter()
        restarted = RuntimeService(RuntimeStateStore(self.store.path), restarted_provider)
        replay = await collect(restarted, self.binding_id, request)
        self.assertEqual(replay[-1].payload, {"code": "indeterminate_after_restart"})
        self.assertEqual(replay[-1].event_type, "error")
        self.assertTrue(replay[-1].terminal)
        self.assertEqual(replay[-1].terminal_status, "indeterminate")
        self.assertIs(replay[-1].bootstrap_consumed, True)
        self.assertEqual(
            self.store.get_request(str(self.binding_id), str(request.request_id)).status,
            "indeterminate",
        )
        self.assertEqual(sum(restarted_provider.turn_sends.values()), 0)
        await restarted.shutdown()

    async def test_abnormal_provider_streams_have_one_non_success_terminal(self) -> None:
        cases = {
            "duplicate_terminal": "failed",
            "terminal_then_event": "failed",
            "malformed": "failed",
            "unexpected_eof": "indeterminate",
            "empty": "indeterminate",
            "exception": "indeterminate",
            "provider_error": "failed",
        }
        for behavior, expected_status in cases.items():
            with self.subTest(behavior=behavior):
                binding_id = uuid4()
                await self.service.ensure_generation(binding_id, self.spec)
                request = turn(bootstrap={"history": []})
                self.provider.set_behavior(str(request.request_id), behavior)
                events = await collect(self.service, binding_id, request)
                record = self.store.get_request(str(binding_id), str(request.request_id))
                self.assertEqual(record.status, expected_status)
                self.assertEqual(sum(event.terminal for event in events), 1)
                self.assertEqual(events[-1].event_type, "error")
                self.assertEqual(events[-1].terminal_status, expected_status)
                self.assertIs(events[-1].bootstrap_consumed, True)
                self.assertEqual(
                    self.store.terminal_count(str(binding_id), str(request.request_id)),
                    1,
                )

    async def test_cancel_wins_and_late_events_cannot_replace_terminal(self) -> None:
        request = turn(bootstrap={"history": []})
        self.provider.set_behavior(str(request.request_id), "cancel_late")
        task = asyncio.create_task(collect(self.service, self.binding_id, request))
        for _ in range(100):
            if self.provider.turn_sends[(str(self.binding_id), str(request.request_id))]:
                break
            await asyncio.sleep(0.01)
        first_cancel = await self.service.cancel(self.binding_id, request.request_id)
        second_cancel = await self.service.cancel(self.binding_id, request.request_id)
        events = await asyncio.wait_for(task, timeout=2)
        self.assertTrue(first_cancel.changed)
        self.assertFalse(second_cancel.changed)
        self.assertEqual(events[-1].payload, {"code": "cancelled"})
        self.assertEqual(events[-1].terminal_status, "cancelled")
        self.assertIs(events[-1].bootstrap_consumed, True)
        self.assertNotIn("late-content", str(events))
        self.assertEqual(self.store.terminal_count(str(self.binding_id), str(request.request_id)), 1)

    async def test_retire_is_idempotent_rejects_new_turn_and_preserves_replay(self) -> None:
        completed = turn(bootstrap={"history": []})
        original = await collect(self.service, self.binding_id, completed)
        first = await self.service.retire(self.binding_id, "done")
        second = await self.service.retire(self.binding_id, "again")
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        new_request = turn()
        with self.assertRaises(RetiredError):
            self.service.preflight_turn(self.binding_id, new_request)
        replay = await collect(self.service, self.binding_id, completed)
        self.assertEqual(original, replay)

    async def test_concurrent_duplicate_claim_sends_once_and_returns_same_journal(self) -> None:
        request = turn(bootstrap={"history": []})
        first, second = await asyncio.gather(
            collect(self.service, self.binding_id, request),
            collect(self.service, self.binding_id, request),
        )
        self.assertEqual(first, second)
        self.assertEqual(
            self.provider.turn_sends[(str(self.binding_id), str(request.request_id))],
            1,
        )

    async def test_duplicate_observer_has_no_poll_limit_and_its_cancellation_preserves_owner(self) -> None:
        request = turn(bootstrap={"history": []})
        self.provider.set_behavior(str(request.request_id), "cancel_late")
        owner = asyncio.create_task(collect(self.service, self.binding_id, request))
        key = (str(self.binding_id), str(request.request_id))
        for _ in range(100):
            if self.provider.turn_sends[key]:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.provider.turn_sends[key], 1)

        original_get_request = self.store.get_request
        observer_polls = 0

        def counting_get_request(binding_id, request_id):
            nonlocal observer_polls
            if (binding_id, request_id) == key:
                observer_polls += 1
            return original_get_request(binding_id, request_id)

        self.store.get_request = counting_get_request
        real_sleep = asyncio.sleep

        async def accelerated_sleep(delay):
            await real_sleep(0)

        with patch("exocore_runtime.service.asyncio.sleep", new=accelerated_sleep):
            observer = asyncio.create_task(collect(self.service, self.binding_id, request))
            for _ in range(10_000):
                if observer_polls > 1_100:
                    break
                await real_sleep(0)
            self.assertGreater(observer_polls, 1_100)
            self.assertFalse(observer.done())
            observer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await observer

        durable = original_get_request(*key)
        self.assertEqual(durable.status, "sent")
        self.assertEqual(self.provider.turn_sends[key], 1)
        await self.service.cancel(self.binding_id, request.request_id)
        owner_events = await asyncio.wait_for(owner, timeout=2)
        replay = await collect(self.service, self.binding_id, request)
        self.assertEqual(owner_events, replay)
        self.assertEqual(replay[-1].payload, {"code": "cancelled"})
        self.assertEqual(self.store.terminal_count(*key), 1)

    async def test_different_requests_keep_sequence_and_terminal_isolated(self) -> None:
        good = turn(bootstrap={"history": []})
        bad = turn()
        self.provider.set_behavior(str(bad.request_id), "exception")
        good_events, bad_events = await asyncio.gather(
            collect(self.service, self.binding_id, good),
            collect(self.service, self.binding_id, bad),
        )
        self.assertEqual(good_events[-1].event_type, "done")
        self.assertEqual(bad_events[-1].event_type, "error")
        self.assertEqual(good_events[0].sequence, 1)
        self.assertEqual(bad_events[0].sequence, 1)
        self.assertTrue(all(event.request_id == good.request_id for event in good_events))
        self.assertTrue(all(event.request_id == bad.request_id for event in bad_events))


if __name__ == "__main__":
    unittest.main()