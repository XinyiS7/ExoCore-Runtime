import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from uuid import uuid4


# Windows: helper processes must never open a visible console window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
from exocore_runtime.contracts import GenerationSpec, TurnRequest
from exocore_runtime.providers.fake import DeterministicFakeAdapter
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


class CrashLifecycleTests(unittest.TestCase):
    """Durable v2 state-machine invariants across a hard owner-process exit.

    The v1-era "inherited starting generation becomes failed without
    reacquisition" behavior is explicitly superseded: under the frozen v2
    contract a starting generation with no durable session is an unknown
    fresh acquisition and may simply be retried (G-03), never auto-failed.
    These probes exercise the fake provider's observable state only; AGY
    artifact semantics are covered by the v2 antigravity integration suite.
    """

    def _crash_child(self, state_path: Path, binding_id, request_id, boundary: str) -> None:
        script = textwrap.dedent(
            f"""
            import asyncio
            import os
            from pathlib import Path
            from uuid import UUID
            from exocore_runtime.contracts import GenerationSpec, TurnRequest
            from exocore_runtime.providers.fake import DeterministicFakeAdapter
            from exocore_runtime.service import RuntimeService
            from exocore_runtime.state_store import RuntimeStateStore

            store = RuntimeStateStore(Path({str(state_path)!r}))
            service = RuntimeService(store, DeterministicFakeAdapter())
            binding = UUID({str(binding_id)!r})
            spec = GenerationSpec(
                bootstrap_fingerprint="bootstrap",
                system_instructions="system",
            )
            asyncio.run(service.ensure_generation(binding, spec))
            if {boundary!r} == "starting":
                os._exit(91)
            request = TurnRequest(
                request_id=UUID({str(request_id)!r}),
                user_message="crash",
                requested_model_id="gemini-3.1-pro-preview",
                requested_thinking_level="auto",
                runtime_mcp_tools=({{"name": "memory_search", "eager": True, "max_call_seconds": None}},),
                bootstrap_context={{"history": []}},
            )
            payload_hash = service._request_hash(request)
            store.claim_request(
                str(binding),
                str(request.request_id),
                payload_hash,
                request.requested_model_id,
                request.requested_thinking_level,
                "crash-owner",
            )
            if {boundary!r} == "prepared":
                os._exit(91)
            resolution = service.providers["fake"].resolve_execution(
                request.requested_model_id,
                request.requested_thinking_level,
            )
            store.freeze_resolution(
                str(binding),
                str(request.request_id),
                "crash-owner",
                resolution,
            )
            store.activate_generation(
                str(binding),
                f"session-{{binding}}",
                str(request.request_id),
            )
            store.mark_sent(
                str(binding),
                str(request.request_id),
                "crash-owner",
                consume_bootstrap=True,
            )
            os._exit(91)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            creationflags=NO_WINDOW,
        )
        self.assertEqual(result.returncode, 91)

    def test_starting_generation_survives_restart_and_may_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "runtime.sqlite3"
            binding_id = uuid4()
            self._crash_child(state_path, binding_id, uuid4(), "starting")
            provider = DeterministicFakeAdapter()
            service = RuntimeService(RuntimeStateStore(state_path), provider)
            spec = GenerationSpec(
                bootstrap_fingerprint="bootstrap",
                system_instructions="system",
            )
            resumed = asyncio.run(service.ensure_generation(binding_id, spec))
            self.assertEqual(resumed.status, "starting")
            self.assertEqual(provider.process_spawns[str(binding_id)], 0)
            request = TurnRequest(
                request_id=uuid4(),
                user_message="crash",
                requested_model_id="gemini-3.1-pro-preview",
                requested_thinking_level="auto",
                runtime_mcp_tools=({"name": "memory_search", "eager": True, "max_call_seconds": None},),
                bootstrap_context={"history": []},
            )
            events = asyncio.run(self._collect(service, binding_id, request))
            self.assertEqual(events[-1].terminal_status, "completed")
            self.assertEqual(provider.turn_sends[(str(binding_id), str(request.request_id))], 1)

    def test_prepared_and_sent_boundaries_survive_hard_process_exit(self) -> None:
        for crash_state in ("prepared", "sent"):
            with self.subTest(crash_state=crash_state), tempfile.TemporaryDirectory() as temp_dir:
                state_path = Path(temp_dir) / "runtime.sqlite3"
                binding_id = uuid4()
                request_id = uuid4()
                self._crash_child(state_path, binding_id, request_id, crash_state)
                provider = DeterministicFakeAdapter()
                service = RuntimeService(RuntimeStateStore(state_path), provider)
                request = TurnRequest(
                    request_id=request_id,
                    user_message="crash",
                    requested_model_id="gemini-3.1-pro-preview",
                    requested_thinking_level="auto",
                    runtime_mcp_tools=({"name": "memory_search", "eager": True, "max_call_seconds": None},),
                    bootstrap_context={"history": []},
                )
                events = asyncio.run(self._collect(service, binding_id, request))
                if crash_state == "prepared":
                    self.assertEqual(events[-1].event_type, "done")
                    self.assertEqual(sum(provider.turn_sends.values()), 1)
                else:
                    self.assertEqual(
                        events[-1].payload,
                        {"code": "indeterminate_after_restart"},
                    )
                    self.assertEqual(events[-1].terminal_status, "indeterminate")
                    self.assertIs(events[-1].bootstrap_consumed, True)
                    self.assertEqual(sum(provider.turn_sends.values()), 0)

    @staticmethod
    async def _collect(service, binding_id, request):
        return [event async for event in service.stream_turn(binding_id, request)]


if __name__ == "__main__":
    unittest.main()