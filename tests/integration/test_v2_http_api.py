from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from exocore_runtime.api import create_app
from exocore_runtime.config import RuntimeConfig
from exocore_runtime.providers.fake import DeterministicFakeAdapter
from exocore_runtime.state_store import RuntimeStateStore


class V2HttpContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.token = "v2-test-token"
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

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def test_exact_v2_health_and_no_v1_routes(self) -> None:
        response = self.client.get("/v2/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ok",
                "schema_version": "v2",
                "protocol": "subscription-runtime-v2",
                "capabilities": [
                    "generation_state_only",
                    "durable_control_events",
                    "requested_effective_execution",
                    "strict_session_resume",
                    "request_journal_replay",
                    "turn_attachments",
                    "runtime_mcp_tool_manifest",
                ],
            },
        )
        old = self.client.get("/v1/health", headers=self.auth())
        self.assertEqual(old.status_code, 404)

    def test_generation_put_is_v2_state_only(self) -> None:
        binding_id = uuid4()
        response = self.client.put(
            f"/v2/generations/{binding_id}",
            headers=self.auth(),
            json={
                "schema_version": "v2",
                "runtime_kind": "fake",
                "bootstrap_fingerprint": "bootstrap",
                "system_instructions": "system",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "starting")
        self.assertEqual(self.provider.process_spawns[str(binding_id)], 0)
        self.assertEqual(self.provider.generation_stages[str(binding_id)], 1)

    def test_attachment_stage_discard_and_registration_boundary(self) -> None:
        from exocore_runtime.contracts import GenerationSpec

        binding_id = uuid4()
        request_id = uuid4()
        self.store.ensure_generation(
            str(binding_id),
            GenerationSpec(
                runtime_kind="fake",
                bootstrap_fingerprint="bootstrap",
                system_instructions="system",
            ),
        )
        base = f"/v2/generations/{binding_id}/turns/{request_id}/attachments"
        stage = self.client.put(
            f"{base}/att-7",
            headers={**self.auth(), "Content-Type": "application/octet-stream"},
            content=b"canonical-bytes",
        )
        self.assertEqual(stage.status_code, 200)
        self.assertEqual(stage.content, b"")
        self.assertEqual(stage.headers["content-length"], "0")
        self.assertNotIn("content-type", stage.headers)
        self.assertEqual(
            self.provider.staged_attachments[
                (str(binding_id), str(request_id), "att-7")
            ],
            b"canonical-bytes",
        )
        discard = self.client.delete(base, headers=self.auth())
        self.assertEqual(discard.status_code, 200)
        self.assertEqual(discard.content, b"")
        self.assertEqual(discard.headers["content-length"], "0")
        again = self.client.delete(base, headers=self.auth())
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.content, b"")
        self.assertFalse(self.provider.staged_attachments)

        self.store.claim_request(
            str(binding_id),
            str(request_id),
            "a" * 64,
            "gemini-3.1-pro-preview",
            "auto",
            "owner",
        )
        for method, url in (("PUT", f"{base}/att-7"), ("DELETE", base)):
            response = self.client.request(
                method,
                url,
                headers={**self.auth(), "Content-Type": "application/octet-stream"},
                content=b"new" if method == "PUT" else None,
            )
            with self.subTest(method=method):
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json(), {"error": "request_registered"})
        self.assertFalse(self.provider.staged_attachments)

    def test_attachment_http_validation_and_retire_boundary(self) -> None:
        from exocore_runtime.contracts import GenerationSpec

        binding_id = uuid4()
        request_id = uuid4()
        self.store.ensure_generation(
            str(binding_id),
            GenerationSpec(
                runtime_kind="fake",
                bootstrap_fingerprint="bootstrap",
                system_instructions="system",
            ),
        )
        url = (
            f"/v2/generations/{binding_id}/turns/{request_id}/attachments/att-7"
        )
        wrong_type = self.client.put(
            url,
            headers={**self.auth(), "Content-Type": "image/png"},
            content=b"data",
        )
        self.assertEqual(wrong_type.status_code, 400)
        invalid_id = self.client.put(
            url.rsplit("/", 1)[0] + "/bad",
            headers={**self.auth(), "Content-Type": "application/octet-stream"},
            content=b"data",
        )
        self.assertEqual(invalid_id.status_code, 400)
        oversized = self.client.put(
            url,
            headers={
                **self.auth(),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(20 * 1024 * 1024 + 1),
            },
            content=b"data",
        )
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(oversized.json(), {"error": "attachment_size_exceeded"})

        self.store.retire_generation(str(binding_id), "test")
        retired = self.client.put(
            url,
            headers={**self.auth(), "Content-Type": "application/octet-stream"},
            content=b"data",
        )
        self.assertEqual(retired.status_code, 409)
        self.assertEqual(retired.json(), {"error": "generation_retired"})
        self.assertFalse(self.provider.staged_attachments)

    def test_attachment_endpoints_require_bearer_and_strict_request_shape(self) -> None:
        from exocore_runtime.contracts import GenerationSpec

        binding_id = uuid4()
        request_id = uuid4()
        self.store.ensure_generation(
            str(binding_id),
            GenerationSpec(
                runtime_kind="fake",
                bootstrap_fingerprint="bootstrap",
                system_instructions="system",
            ),
        )
        base = f"/v2/generations/{binding_id}/turns/{request_id}/attachments"
        for method, url in (("PUT", f"{base}/att-7"), ("DELETE", base)):
            response = self.client.request(
                method,
                url,
                headers={"Content-Type": "application/octet-stream"},
                content=b"data" if method == "PUT" else None,
            )
            with self.subTest(method=method):
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"error": "unauthorized"})

        empty = self.client.put(
            f"{base}/att-7",
            headers={**self.auth(), "Content-Type": "application/octet-stream"},
            content=b"",
        )
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json(), {"error": "invalid_request"})

        chunked = self.client.put(
            f"{base}/att-7",
            headers={**self.auth(), "Content-Type": "application/octet-stream"},
            content=iter([b"data"]),
        )
        self.assertEqual(chunked.status_code, 400)
        self.assertEqual(chunked.json(), {"error": "invalid_request"})

        missing_binding = uuid4()
        missing_put = self.client.put(
            f"/v2/generations/{missing_binding}/turns/{request_id}/attachments/att-7",
            headers={**self.auth(), "Content-Type": "application/octet-stream"},
            content=b"data",
        )
        self.assertEqual(missing_put.status_code, 404)
        self.assertEqual(missing_put.json(), {"error": "not_found"})
        missing_delete = self.client.delete(
            f"/v2/generations/{missing_binding}/turns/{request_id}/attachments",
            headers=self.auth(),
        )
        self.assertEqual(missing_delete.status_code, 404)
        self.assertEqual(missing_delete.json(), {"error": "not_found"})
        self.assertFalse(self.provider.staged_attachments)

    def test_v1_generation_shape_is_rejected_without_mutation(self) -> None:
        binding_id = uuid4()
        response = self.client.put(
            f"/v2/generations/{binding_id}",
            headers=self.auth(),
            json={
                "schema_version": "v1",
                "runtime_kind": "fake",
                "provider_model_id": "fake-model",
                "bootstrap_fingerprint": "bootstrap",
                "config_fingerprint": "config",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"error": "invalid_request"})
        self.assertEqual(sum(self.provider.generation_stages.values()), 0)

    def _completed_request(self):
        binding_id = uuid4()
        response = self.client.put(
            f"/v2/generations/{binding_id}",
            headers=self.auth(),
            json={
                "schema_version": "v2",
                "runtime_kind": "fake",
                "bootstrap_fingerprint": "bootstrap",
                "system_instructions": "system",
            },
        )
        self.assertEqual(response.status_code, 200)
        request_id = uuid4()
        turn = {
            "schema_version": "v2",
            "request_id": str(request_id),
            "user_message": "journal probe",
            "requested_model_id": "gemini-3.1-pro-preview",
            "requested_thinking_level": "auto",
            "runtime_mcp_tools": [
                {"name": "memory_search", "eager": True, "max_call_seconds": None}
            ],
            "bootstrap_context": {"history": []},
            "continuity_delta": [],
            "ephemeral_current": None,
        }
        response = self.client.post(
            f"/v2/generations/{binding_id}/turns",
            headers=self.auth(),
            json=turn,
        )
        self.assertEqual(response.status_code, 200)
        return binding_id, request_id

    def test_pre_manifest_turn_shape_is_rejected_without_registration(self) -> None:
        binding_id = uuid4()
        created = self.client.put(
            f"/v2/generations/{binding_id}",
            headers=self.auth(),
            json={
                "schema_version": "v2",
                "runtime_kind": "fake",
                "bootstrap_fingerprint": "bootstrap",
                "system_instructions": "system",
            },
        )
        self.assertEqual(created.status_code, 200)
        request_id = uuid4()
        old_turn = {
            "schema_version": "v2",
            "request_id": str(request_id),
            "user_message": "old client",
            "requested_model_id": "gemini-3.1-pro-preview",
            "requested_thinking_level": "auto",
            "bootstrap_context": {"history": []},
        }
        response = self.client.post(
            f"/v2/generations/{binding_id}/turns",
            headers=self.auth(),
            json=old_turn,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"error": "invalid_request"})
        self.assertIsNone(
            self.store.get_request(str(binding_id), str(request_id))
        )

    def test_journal_replay_requires_auth_and_forbids_token_in_path(self) -> None:
        binding_id, request_id = self._completed_request()
        url = f"/v2/generations/{binding_id}/turns/{request_id}/journal"
        self.assertEqual(self.client.get(url).status_code, 401)
        token_url = f"/v2/generations/{binding_id}/turns/{request_id}/{self.token}/journal"
        self.assertEqual(self.client.get(token_url, headers=self.auth()).status_code, 401)

    def test_journal_replay_404_and_409_codes_are_exact(self) -> None:
        missing = self.client.get(
            f"/v2/generations/{uuid4()}/turns/{uuid4()}/journal",
            headers=self.auth(),
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), {"error": "not_found"})

        from exocore_runtime.contracts import GenerationSpec

        binding_id = uuid4()
        self.store.ensure_generation(
            str(binding_id),
            GenerationSpec(
                runtime_kind="fake",
                bootstrap_fingerprint="bootstrap",
                system_instructions="system",
            ),
        )
        request_id = uuid4()
        self.store.claim_request(
            str(binding_id),
            str(request_id),
            "a" * 64,
            "gemini-3.1-pro-preview",
            "auto",
            "owner",
        )
        prepared = self.client.get(
            f"/v2/generations/{binding_id}/turns/{request_id}/journal",
            headers=self.auth(),
        )
        self.assertEqual(prepared.status_code, 409)
        self.assertEqual(prepared.json(), {"error": "journal_not_terminal"})

    def test_cancel_of_unregistered_request_uses_the_cancel_specific_code(self) -> None:
        from exocore_runtime.contracts import GenerationSpec

        binding_id = uuid4()
        self.store.ensure_generation(
            str(binding_id),
            GenerationSpec(
                runtime_kind="fake",
                bootstrap_fingerprint="bootstrap",
                system_instructions="system",
            ),
        )
        unregistered = self.client.post(
            f"/v2/generations/{binding_id}/turns/{uuid4()}/cancel",
            headers=self.auth(),
        )
        self.assertEqual(unregistered.status_code, 409)
        self.assertEqual(
            unregistered.json(),
            {"error": "cancel_request_unregistered"},
        )
        missing_binding = self.client.post(
            f"/v2/generations/{uuid4()}/turns/{uuid4()}/cancel",
            headers=self.auth(),
        )
        self.assertEqual(missing_binding.status_code, 404)
        self.assertEqual(missing_binding.json(), {"error": "not_found"})

    def test_journal_replay_returns_exact_header_and_ordered_frames(self) -> None:
        binding_id, request_id = self._completed_request()
        store_request = self.store.get_request(str(binding_id), str(request_id))
        self.assertIsNotNone(store_request)
        expected_hash = store_request.payload_hash
        response = self.client.get(
            f"/v2/generations/{binding_id}/turns/{request_id}/journal",
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"].split(";", 1)[0],
            "application/x-ndjson",
        )
        lines = [
            line
            for line in response.text.split("\n")
            if line.strip()
        ]
        header = json.loads(lines[0])
        self.assertEqual(header["frame_type"], "journal_header")
        self.assertEqual(header["schema_version"], "v2")
        self.assertEqual(header["binding_id"], str(binding_id))
        self.assertEqual(header["request_id"], str(request_id))
        self.assertEqual(header["request_payload_sha256"], expected_hash)
        self.assertEqual(header["request_status"], "completed")
        self.assertEqual(header["event_count"], len(lines) - 1)
        self.assertEqual(header["last_sequence"], len(lines) - 1)
        events = [json.loads(line) for line in lines[1:]]
        for index, event in enumerate(events, start=1):
            self.assertEqual(event["sequence"], index)
        self.assertTrue(events[-1]["terminal"])
        self.assertEqual(events[-1]["terminal_status"], "completed")
        self.assertEqual(
            events[-1]["provider_input_effect"],
            "may_have_reached_provider",
        )

    def test_journal_replay_never_touches_provider_or_process(self) -> None:
        binding_id, request_id = self._completed_request()
        spawns_before = sum(self.provider.process_spawns.values())
        prepares_before = sum(self.provider.process_prepares.values())
        sends_before = sum(self.provider.turn_sends.values())
        resolver_before = sum(self.provider.resolver_calls.values())
        response = self.client.get(
            f"/v2/generations/{binding_id}/turns/{request_id}/journal",
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sum(self.provider.process_spawns.values()), spawns_before)
        self.assertEqual(sum(self.provider.process_prepares.values()), prepares_before)
        self.assertEqual(sum(self.provider.turn_sends.values()), sends_before)
        self.assertEqual(sum(self.provider.resolver_calls.values()), resolver_before)


if __name__ == "__main__":
    unittest.main()
