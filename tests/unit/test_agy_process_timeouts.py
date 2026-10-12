import asyncio
from types import SimpleNamespace
import unittest

from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.process import AgyProcessSupervisor


class AgyProcessActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_provider_event_restarts_the_idle_wait(self) -> None:
        supervisor = object.__new__(AgyProcessSupervisor)
        session = SimpleNamespace(queue=asyncio.Queue())

        async def feed_activity() -> None:
            await asyncio.sleep(0.12)
            await session.queue.put(("stdout", b"first"))
            await asyncio.sleep(0.12)
            await session.queue.put(("stdout", b"second"))

        loop = asyncio.get_running_loop()
        started = loop.time()
        feeder = asyncio.create_task(feed_activity())
        first = await supervisor._next_item(session, 0.2, "agy_idle_timeout")
        second = await supervisor._next_item(session, 0.2, "agy_idle_timeout")
        await feeder

        self.assertEqual(first, ("stdout", b"first"))
        self.assertEqual(second, ("stdout", b"second"))
        self.assertGreater(loop.time() - started, 0.2)


class AgyProcessTimeoutTests(unittest.TestCase):
    def test_normal_wait_uses_idle_timeout(self) -> None:
        timeout, code = AgyProcessSupervisor._turn_wait_budget(
            elapsed=120,
            idle_timeout=60,
            hard_timeout=1800,
        )

        self.assertEqual(timeout, 60)
        self.assertEqual(code, "agy_idle_timeout")

    def test_hard_ceiling_wait_is_not_mislabeled_as_idle(self) -> None:
        timeout, code = AgyProcessSupervisor._turn_wait_budget(
            elapsed=1795,
            idle_timeout=60,
            hard_timeout=1800,
        )

        self.assertEqual(timeout, 5)
        self.assertEqual(code, "agy_hard_timeout")

    def test_expired_hard_ceiling_fails_immediately(self) -> None:
        with self.assertRaises(ProviderAdapterError) as raised:
            AgyProcessSupervisor._turn_wait_budget(
                elapsed=1800,
                idle_timeout=60,
                hard_timeout=1800,
            )

        self.assertEqual(raised.exception.code, "agy_hard_timeout")
        self.assertEqual(raised.exception.terminal_status, "indeterminate")


if __name__ == "__main__":
    unittest.main()
