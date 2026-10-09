"""Mid-turn Collection inspections (ExoCore #43).

Covers the store (write / overwrite / refusals / discard boundary), the
service guard (only a ``sent`` request of an active generation accepts one),
the terminal cleanup ordering (discard before reclaim, after the durable
terminal), and the HTTP route shape.
"""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from fastapi.testclient import TestClient

from exocore_runtime.api import create_app
from exocore_runtime.config import RuntimeConfig
from exocore_runtime.contracts import GenerationSpec, TurnRequest
from exocore_runtime.errors import (
    AttachmentSizeExceededError,
    ConflictError,
    InvalidRequestError,
    RetiredError,
)
from exocore_runtime.providers.antigravity.attachments import AttachmentStore
from exocore_runtime.providers.fake import DeterministicFakeAdapter
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore

PNG_BYTES = b"\x89PNG\r\n\x1a\ninspection-pixels"
JPEG_BYTES = b"\xff\xd8\xffinspection-jpeg"


async def collect(service, binding_id, request):
    return [event async for event in service.stream_turn(binding_id, request)]


async def wait_until(predicate, *, attempts=600):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


def turn(*, bootstrap=None):
    return TurnRequest(
        request_id=uuid4(),
        user_message="hello",
        requested_model_id="gemini-3.1-pro-preview",
        requested_thinking_level="auto",
        runtime_mcp_tools=(
            {"name": "use_collection_item", "eager": False, "max_call_seconds": None},
        ),
        bootstrap_context=bootstrap,
    )


def fake_spec() -> GenerationSpec:
    return GenerationSpec(
        runtime_kind="fake",
        bootstrap_fingerprint="bootstrap-1",
        system_instructions="system",
    )


class InspectionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "generation"
        (self.root / "workspace").mkdir(parents=True)
        self.store = AttachmentStore(self.root)
        self.request_id = str(uuid4())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def inspections(self) -> Path:
        return self.root / "workspace" / self.request_id / "inspections"

    def test_stage_writes_absolute_file_and_overwrites_same_item(self) -> None:
        path = self.store.stage_inspection(
            self.request_id, "item-7", "image/png", PNG_BYTES
        )
        self.assertTrue(path.is_absolute())
        self.assertEqual(path, (self.inspections() / "item-7.png").resolve())
        self.assertEqual(path.read_bytes(), PNG_BYTES)

        replacement = PNG_BYTES + b"-second"
        again = self.store.stage_inspection(
            self.request_id, "item-7", "image/png", replacement
        )
        self.assertEqual(again, path)
        self.assertEqual(path.read_bytes(), replacement)
        self.assertEqual(
            sorted(entry.name for entry in self.inspections().iterdir()),
            ["item-7.png"],
        )

    def test_no_per_request_count_or_total_cap(self) -> None:
        # A1: only the per-file limit applies; many items in one turn are fine.
        for item in range(1, 8):
            self.store.stage_inspection(
                self.request_id, f"item-{item}", "image/jpeg", JPEG_BYTES
            )
        self.assertEqual(len(list(self.inspections().iterdir())), 7)

    def test_refusals_write_nothing(self) -> None:
        cases = (
            ("image/gif", PNG_BYTES, InvalidRequestError),
            ("image/png", JPEG_BYTES, InvalidRequestError),
            ("image/png", b"", InvalidRequestError),
            (
                "image/png",
                b"\x89PNG\r\n\x1a\n" + b"0" * (20 * 1024 * 1024),
                AttachmentSizeExceededError,
            ),
        )
        for mime, data, error in cases:
            with self.subTest(mime=mime, size=len(data)):
                with self.assertRaises(error):
                    self.store.stage_inspection(self.request_id, "item-7", mime, data)
        for bad_id in ("item-0", "att-7", "item-7.png", "../item-7"):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                self.store.stage_inspection(self.request_id, bad_id, "image/png", PNG_BYTES)
        self.assertFalse(self.inspections().exists())

    def test_discard_removes_only_inspections_and_is_idempotent(self) -> None:
        self.store.stage(self.request_id, "att-1", PNG_BYTES)
        self.store.stage_inspection(self.request_id, "item-7", "image/png", PNG_BYTES)
        self.store.discard_inspections(self.request_id)
        self.assertFalse(self.inspections().exists())
        self.assertTrue(
            (self.root / "workspace" / self.request_id / "attachments").is_dir()
        )
        self.store.discard_inspections(self.request_id)
        self.store.discard_inspections(str(uuid4()))


class InspectionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = RuntimeStateStore(Path(self.temp.name) / "runtime.sqlite3")
        self.provider = DeterministicFakeAdapter()
        self.service = RuntimeService(self.store, self.provider)
        self.binding_id = uuid4()
        await self.service.ensure_generation(self.binding_id, fake_spec())

    async def asyncTearDown(self) -> None:
        try:
            await self.service.shutdown()
        except Exception:
            pass
        self.temp.cleanup()

    def key(self, request) -> tuple[str, str]:
        return (str(self.binding_id), str(request.request_id))

    def spy_cleanup_order(self) -> list[tuple[str, str | None]]:
        """Record discard/reclaim calls with the request's durable status."""

        observed: list[tuple[str, str | None]] = []
        real_discard = self.provider.discard_inspections
        real_reclaim = self.provider.reclaim_request

        async def discard(binding_id, request_id):
            record = self.store.get_request(binding_id, request_id)
            observed.append(("discard", record.status if record else None))
            await real_discard(binding_id, request_id)

        def reclaim(binding_id, request_id):
            observed.append(("reclaim", None))
            real_reclaim(binding_id, request_id)

        self.provider.discard_inspections = discard
        self.provider.reclaim_request = reclaim
        return observed

    async def stage(self, request, inspection_id="item-7") -> str:
        return await self.service.stage_inspection(
            self.binding_id,
            request.request_id,
            inspection_id,
            "image/png",
            PNG_BYTES,
        )

    async def test_sent_request_accepts_inspection_and_cancel_discards_it(self) -> None:
        observed = self.spy_cleanup_order()
        request = turn(bootstrap={"history": []})
        self.provider.set_behavior(str(request.request_id), "cancel_late")
        owner = asyncio.create_task(collect(self.service, self.binding_id, request))
        await wait_until(lambda: self.provider.turn_sends[self.key(request)] > 0)

        path = await self.stage(request)
        self.assertTrue(Path(path).is_absolute())
        self.assertIn(
            (*self.key(request), "item-7"), self.provider.staged_inspections
        )

        await self.service.cancel(self.binding_id, request.request_id)
        events = await asyncio.wait_for(owner, timeout=2)
        self.assertTrue(events[-1].terminal)
        self.assertEqual(
            observed, [("discard", "cancelled"), ("reclaim", None)]
        )
        self.assertFalse(self.provider.staged_inspections)

    async def test_completed_turn_discards_once_before_reclaim(self) -> None:
        observed = self.spy_cleanup_order()
        request = turn(bootstrap={"history": []})
        events = await collect(self.service, self.binding_id, request)
        self.assertEqual(events[-1].event_type, "done")
        self.assertEqual(
            observed, [("discard", "completed"), ("reclaim", None)]
        )

    async def test_send_boundary_conflict_discards_before_reclaim(self) -> None:
        observed = self.spy_cleanup_order()
        request = turn(bootstrap={"history": []})

        def conflict(*args, **kwargs):
            raise ConflictError("generation is not active")

        with mock.patch.object(self.store, "mark_sent", conflict):
            events = await collect(self.service, self.binding_id, request)
        self.assertEqual(events[-1].payload, {"code": "send_boundary_conflict"})
        self.assertEqual(observed, [("discard", "failed"), ("reclaim", None)])

    async def test_only_a_sent_request_accepts_inspections(self) -> None:
        unknown = turn()
        with self.assertRaises(ConflictError):
            await self.stage(unknown)

        finished = turn(bootstrap={"history": []})
        await collect(self.service, self.binding_id, finished)
        with self.assertRaises(ConflictError):
            await self.stage(finished)

        prepared = turn()
        self.store.claim_request(
            str(self.binding_id),
            str(prepared.request_id),
            "a" * 64,
            prepared.requested_model_id,
            prepared.requested_thinking_level,
            "owner",
        )
        with self.assertRaises(ConflictError):
            await self.stage(prepared)

        await self.service.retire(self.binding_id, "test")
        with self.assertRaises(RetiredError):
            await self.stage(prepared)
        self.assertFalse(self.provider.staged_inspections)


class InspectionHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.token = "inspection-token"
        self.provider = DeterministicFakeAdapter()
        self.store = RuntimeStateStore(root / "runtime.sqlite3")
        config = RuntimeConfig(
            host="127.0.0.1",
            port=8766,
            token=self.token,
            state_path=self.store.path,
            provider_data_root=root / "providers",
        )
        self.client = TestClient(create_app(config, self.provider, self.store))
        self.client.__enter__()
        self.binding_id = str(uuid4())
        self.request_id = str(uuid4())
        self.store.ensure_generation(self.binding_id, fake_spec())

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def auth(self, mime="image/png"):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": mime}

    def url(self, inspection_id="item-7") -> str:
        return (
            f"/v2/generations/{self.binding_id}/turns/{self.request_id}/"
            f"inspections/{inspection_id}"
        )

    def mark_sent(self) -> None:
        self.store.claim_request(
            self.binding_id,
            self.request_id,
            "a" * 64,
            "gemini-3.1-pro-preview",
            "auto",
            "owner",
        )
        self.store.freeze_resolution(
            self.binding_id,
            self.request_id,
            "owner",
            self.provider.resolve_execution("gemini-3.1-pro-preview", "auto"),
        )
        self.store.activate_generation(self.binding_id, "session-1", self.request_id)
        self.store.mark_sent(
            self.binding_id, self.request_id, "owner", consume_bootstrap=True
        )

    def test_sent_request_returns_absolute_path(self) -> None:
        self.mark_sent()
        response = self.client.put(self.url(), headers=self.auth(), content=PNG_BYTES)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"path"})
        self.assertTrue(Path(body["path"]).is_absolute())
        self.assertEqual(
            self.provider.staged_inspections[
                (self.binding_id, self.request_id, "item-7")
            ],
            ("image/png", PNG_BYTES),
        )

    def test_request_not_sent_is_conflict(self) -> None:
        response = self.client.put(self.url(), headers=self.auth(), content=PNG_BYTES)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"error": "identity_conflict"})
        self.assertFalse(self.provider.staged_inspections)

    def test_http_validation(self) -> None:
        self.mark_sent()
        cases = (
            (self.url("att-7"), self.auth(), PNG_BYTES, 400, "invalid_request"),
            (self.url(), self.auth("image/gif"), PNG_BYTES, 400, "invalid_request"),
            (self.url(), self.auth(), b"", 400, "invalid_request"),
            (
                self.url(),
                {**self.auth(), "Content-Length": str(20 * 1024 * 1024 + 1)},
                PNG_BYTES,
                400,
                "attachment_size_exceeded",
            ),
            (
                self.url(),
                {"Content-Type": "image/png"},
                PNG_BYTES,
                401,
                "unauthorized",
            ),
        )
        for url, headers, content, status, code in cases:
            with self.subTest(url=url, status=status, code=code):
                response = self.client.put(url, headers=headers, content=content)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json(), {"error": code})
        self.assertFalse(self.provider.staged_inspections)


if __name__ == "__main__":
    unittest.main()
