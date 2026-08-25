import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from uuid import uuid4

from exocore_runtime.contracts import GenerationSpec, TurnRequest
from exocore_runtime.errors import ConflictError
from exocore_runtime.providers.fake import DeterministicFakeAdapter
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


class CrashLifecycleTests(unittest.TestCase):
    def _crash_child(self, state_path: Path, binding_id, request_id, state: str) -> None:
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
                provider_model_id="fake-model",
                bootstrap_fingerprint="bootstrap",
                config_fingerprint="config",
            )
            asyncio.run(service.ensure_generation(binding, spec))
            request = TurnRequest(request_id=UUID({str(request_id)!r}), user_message="crash")
            payload_hash = service._request_hash(request)
            store.claim_request(str(binding), str(request.request_id), payload_hash, "crash-owner")
            if {state!r} == "sent":
                store.mark_sent(str(binding), str(request.request_id), "crash-owner")
            os._exit(91)
            """
        )
        result = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(result.returncode, 91)

    def _crash_generation_child(self, state_path: Path, binding_id, boundary: str) -> None:
        script = textwrap.dedent(
            f"""
            import asyncio
            import os
            from pathlib import Path
            from uuid import UUID
            from exocore_runtime.contracts import GenerationSpec
            from exocore_runtime.providers.fake import DeterministicFakeAdapter
            from exocore_runtime.state_store import RuntimeStateStore

            store = RuntimeStateStore(Path({str(state_path)!r}))
            binding = UUID({str(binding_id)!r})
            spec = GenerationSpec(
                provider_model_id="fake-model",
                bootstrap_fingerprint="bootstrap",
                config_fingerprint="config",
            )
            record, created = store.ensure_generation(str(binding), spec)
            assert created and record.status == "starting"
            if {boundary!r} == "post_acquisition":
                provider = DeterministicFakeAdapter()
                asyncio.run(provider.ensure_generation(str(binding), spec))
                assert provider.generation_acquisitions[str(binding)] == 1
            os._exit(92)
            """
        )
        result = subprocess.run([sys.executable, "-c", script], check=False)
        self.assertEqual(result.returncode, 92)

    def test_inherited_starting_generation_becomes_failed_without_reacquisition(self) -> None:
        for boundary in ("pre_acquisition", "post_acquisition"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp_dir:
                state_path = Path(temp_dir) / "runtime.sqlite3"
                binding_id = uuid4()
                self._crash_generation_child(state_path, binding_id, boundary)
                provider = DeterministicFakeAdapter()
                service = RuntimeService(RuntimeStateStore(state_path), provider)
                spec = GenerationSpec(
                    provider_model_id="fake-model",
                    bootstrap_fingerprint="bootstrap",
                    config_fingerprint="config",
                )
                self.assertEqual(service.recovered_starting_generations, 1)
                result = asyncio.run(service.ensure_generation(binding_id, spec))
                self.assertEqual(result.status, "failed")
                self.assertEqual(provider.generation_acquisitions[str(binding_id)], 0)
                self.assertEqual(
                    service.store.get_generation(str(binding_id)).status,
                    "failed",
                )
                with self.assertRaises(ConflictError):
                    service.store.activate_generation(str(binding_id), "late-session")
                changed = spec.model_copy(update={"provider_model_id": "other-model"})
                with self.assertRaises(ConflictError):
                    asyncio.run(service.ensure_generation(binding_id, changed))

    def test_prepared_and_sent_boundaries_survive_hard_process_exit(self) -> None:
        for crash_state in ("prepared", "sent"):
            with self.subTest(crash_state=crash_state), tempfile.TemporaryDirectory() as temp_dir:
                state_path = Path(temp_dir) / "runtime.sqlite3"
                binding_id = uuid4()
                request_id = uuid4()
                self._crash_child(state_path, binding_id, request_id, crash_state)
                provider = DeterministicFakeAdapter()
                service = RuntimeService(RuntimeStateStore(state_path), provider)
                request = TurnRequest(request_id=request_id, user_message="crash")
                events = asyncio.run(self._collect(service, binding_id, request))
                if crash_state == "prepared":
                    self.assertEqual(events[-1].event_type, "done")
                    self.assertEqual(sum(provider.turn_sends.values()), 1)
                else:
                    self.assertEqual(
                        events[-1].payload,
                        {"code": "indeterminate_after_restart"},
                    )
                    self.assertEqual(sum(provider.turn_sends.values()), 0)

    @staticmethod
    async def _collect(service, binding_id, request):
        return [event async for event in service.stream_turn(binding_id, request)]


if __name__ == "__main__":
    unittest.main()
