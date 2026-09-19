import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from pydantic import ValidationError

from exocore_runtime.contracts import GenerationSpec, ProcessExecutionOptions, TurnRequest
from exocore_runtime.errors import ConflictError, StateResetRequiredError
from exocore_runtime.providers.antigravity.capabilities import (
    LAUNCH_ENVIRONMENT_REVISION,
    SECURITY_POLICY_REVISION,
    resolve_execution,
)
from exocore_runtime.providers.fake import DeterministicFakeAdapter
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


def request(*, request_id=None, thinking="auto", model="gemini-3.1-pro-preview", bootstrap=None):
    return TurnRequest(
        request_id=request_id or uuid4(),
        user_message="hello",
        requested_model_id=model,
        requested_thinking_level=thinking,
        bootstrap_context=bootstrap,
    )


class CapabilityPolicyTests(unittest.TestCase):
    def test_current_product_slice_is_explicit(self) -> None:
        cases = {
            "auto": ("gemini-3.1-pro-high", "high"),
            "low": ("gemini-3.1-pro-low", "low"),
            "high": ("gemini-3.1-pro-high", "high"),
        }
        for thinking, expected in cases.items():
            with self.subTest(thinking=thinking):
                resolution = resolve_execution("gemini-3.1-pro-preview", thinking)
                self.assertEqual((resolution.provider_model_slug, resolution.effort), expected)
        for thinking in ("off", "medium", "max"):
            with self.subTest(thinking=thinking), self.assertRaisesRegex(
                Exception, "unsupported_requested_execution"
            ):
                resolve_execution("gemini-3.1-pro-preview", thinking)
        with self.assertRaisesRegex(Exception, "unsupported_requested_execution"):
            resolve_execution("gemini-3.7-flash-preview", "high")

    def test_agy_resolver_opts_out_of_the_neutral_sandbox_default(self) -> None:
        options = resolve_execution("gemini-3.1-pro-preview", "auto").process_options
        self.assertIs(options.sandbox, False)
        self.assertEqual(options.security_policy_revision, SECURITY_POLICY_REVISION)
        self.assertEqual(SECURITY_POLICY_REVISION, "agy-tool-perm-v4")
        # The provider-neutral default itself stays untouched.
        neutral = ProcessExecutionOptions(
            provider_model_slug="gemini-3.1-pro-high",
            effort="high",
            security_policy_revision=SECURITY_POLICY_REVISION,
            launch_environment_revision=LAUNCH_ENVIRONMENT_REVISION,
        )
        self.assertIs(neutral.sandbox, True)

    def test_generation_contract_structurally_excludes_execution_and_session(self) -> None:
        fields = set(GenerationSpec.model_fields)
        self.assertEqual(
            fields,
            {
                "schema_version",
                "runtime_kind",
                "bootstrap_fingerprint",
                "system_instructions",
                # CP3 (B-prime): project rules travel as their own frozen field
                # and are never concatenated into the system instructions.
                "project_rules",
            },
        )
        forbidden = {
            "provider_session_id",
            "provider_model_id",
            "effort",
            "config_fingerprint",
            "session_policy_revision",
        }
        self.assertTrue(fields.isdisjoint(forbidden))
        with self.assertRaises(ValidationError):
            GenerationSpec(
                bootstrap_fingerprint="b",
                system_instructions="system",
                provider_session_id="forbidden",
            )


class RuntimeV2ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = RuntimeStateStore(Path(self.temp.name) / "runtime.sqlite3")
        self.provider = DeterministicFakeAdapter()
        self.service = RuntimeService(self.store, self.provider)
        self.binding_id = uuid4()
        self.spec = GenerationSpec(
            bootstrap_fingerprint="bootstrap",
            system_instructions="system",
        )

    async def asyncTearDown(self) -> None:
        await self.service.shutdown()
        self.temp.cleanup()

    async def test_put_is_state_only_and_first_owned_turn_orders_control_before_stdin(self) -> None:
        result = await self.service.ensure_generation(self.binding_id, self.spec)
        self.assertEqual(result.status, "starting")
        self.assertEqual(self.provider.process_spawns[str(self.binding_id)], 0)
        turn = request(bootstrap={"history": []})
        events = await collect(self.service, self.binding_id, turn)
        self.assertEqual(
            [event.event_type for event in events[:2]],
            ["generation_activated", "execution_resolved"],
        )
        self.assertEqual(self.provider.process_spawns[str(self.binding_id)], 1)
        self.assertEqual(self.provider.turn_sends[(str(self.binding_id), str(turn.request_id))], 1)
        self.assertLess(events[0].sequence, events[1].sequence)
        self.assertEqual(events[-1].terminal_status, "completed")
        durable = self.store.read_events(str(self.binding_id), str(turn.request_id))
        self.assertEqual(events, durable)

    async def test_completed_replay_and_observer_do_not_resolve_or_touch_process(self) -> None:
        await self.service.ensure_generation(self.binding_id, self.spec)
        turn = request(bootstrap={"history": []})
        first = await collect(self.service, self.binding_id, turn)
        resolver_before = self.provider.resolver_calls.copy()
        prepares_before = self.provider.process_prepares.copy()
        replay = await collect(self.service, self.binding_id, turn)
        self.assertEqual(first, replay)
        self.assertEqual(self.provider.resolver_calls, resolver_before)
        self.assertEqual(self.provider.process_prepares, prepares_before)
        self.assertEqual(self.provider.turn_sends[(str(self.binding_id), str(turn.request_id))], 1)

    async def test_observer_waits_without_resolver_or_process_mutation(self) -> None:
        await self.service.ensure_generation(self.binding_id, self.spec)
        turn = request(bootstrap={"history": []})
        self.provider.set_behavior(str(turn.request_id), "cancel_late")
        owner = asyncio.create_task(collect(self.service, self.binding_id, turn))
        key = (str(self.binding_id), str(turn.request_id))
        for _ in range(100):
            if self.provider.turn_sends[key]:
                break
            await asyncio.sleep(0.01)
        resolver_before = self.provider.resolver_calls.copy()
        prepares_before = self.provider.process_prepares.copy()
        observer = asyncio.create_task(collect(self.service, self.binding_id, turn))
        await asyncio.sleep(0.03)
        self.assertFalse(observer.done())
        self.assertEqual(self.provider.resolver_calls, resolver_before)
        self.assertEqual(self.provider.process_prepares, prepares_before)
        observer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await observer
        await self.service.cancel(self.binding_id, turn.request_id)
        await owner

    async def test_same_effective_options_reuse_and_changed_options_respawn_same_session(self) -> None:
        await self.service.ensure_generation(self.binding_id, self.spec)
        first = request(thinking="auto", bootstrap={"history": []})
        await collect(self.service, self.binding_id, first)
        session = self.store.get_generation(str(self.binding_id)).provider_session_id
        second = request(thinking="high")
        await collect(self.service, self.binding_id, second)
        self.assertEqual(self.provider.process_spawns[str(self.binding_id)], 1)
        third = request(thinking="low")
        await collect(self.service, self.binding_id, third)
        self.assertEqual(self.provider.process_spawns[str(self.binding_id)], 2)
        self.assertEqual(self.provider.process_disposals[str(self.binding_id)], 1)
        self.assertEqual(
            self.store.get_generation(str(self.binding_id)).provider_session_id,
            session,
        )

    async def test_frozen_resolution_survives_owner_restart_without_resolver(self) -> None:
        await self.service.ensure_generation(self.binding_id, self.spec)
        turn = request(bootstrap={"history": []})
        payload_hash = self.service._request_hash(turn)
        record, _ = self.store.claim_request(
            str(self.binding_id),
            str(turn.request_id),
            payload_hash,
            turn.requested_model_id,
            turn.requested_thinking_level,
            "dead-owner",
        )
        resolution = self.provider.resolve_execution(
            turn.requested_model_id,
            turn.requested_thinking_level,
        )
        self.store.freeze_resolution(
            str(self.binding_id),
            str(turn.request_id),
            "dead-owner",
            resolution,
        )
        restarted_provider = DeterministicFakeAdapter()
        restarted = RuntimeService(RuntimeStateStore(self.store.path), restarted_provider)
        events = await collect(restarted, self.binding_id, turn)
        self.assertEqual(events[-1].terminal_status, "completed")
        self.assertEqual(sum(restarted_provider.resolver_calls.values()), 0)
        await restarted.shutdown()

    async def test_request_hash_includes_requested_model_and_thinking(self) -> None:
        base = request(request_id=uuid4(), thinking="auto", bootstrap={"history": []})
        changed_thinking = base.model_copy(update={"requested_thinking_level": "high"})
        changed_model = base.model_copy(update={"requested_model_id": "other-model"})
        self.assertNotEqual(self.service._request_hash(base), self.service._request_hash(changed_thinking))
        self.assertNotEqual(self.service._request_hash(base), self.service._request_hash(changed_model))


class FreshSchemaGateTests(unittest.TestCase):
    def test_provider_session_alias_is_rejected_across_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RuntimeStateStore(Path(temp_dir) / "runtime.sqlite3")
            spec = GenerationSpec(
                bootstrap_fingerprint="bootstrap",
                system_instructions="system",
            )
            first = str(uuid4())
            second = str(uuid4())
            store.ensure_generation(first, spec)
            store.ensure_generation(second, spec)
            store.activate_generation(first, "shared-session", str(uuid4()))
            with self.assertRaises(ConflictError):
                store.activate_generation(second, "shared-session", str(uuid4()))
            self.assertIsNone(store.get_generation(second).provider_session_id)

    def test_v1_schema_requires_explicit_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runtime.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE generations(binding_id TEXT PRIMARY KEY)")
            connection.commit()
            connection.close()
            with self.assertRaises(StateResetRequiredError):
                RuntimeStateStore(path)


if __name__ == "__main__":
    unittest.main()
