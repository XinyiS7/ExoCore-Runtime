import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import uvicorn

from exocore_runtime.api import create_app
from exocore_runtime.config import RuntimeConfig
from exocore_runtime.errors import NotFoundError
from exocore_runtime.providers.antigravity.adapter import AntigravityAdapter
from exocore_runtime.providers.antigravity.process import AgyProcessConfig, AgyProcessSupervisor
from exocore_runtime.providers.fake import DeterministicFakeAdapter


class LiveGateway:
    def __init__(self, state_path: Path, token: str, provider=None) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.token = token
        self.provider = provider or DeterministicFakeAdapter()
        config = RuntimeConfig("127.0.0.1", self.port, token, state_path)
        self.app = create_app(config, provider=self.provider)
        uvicorn_config = uvicorn.Config(
            self.app,
            host=config.host,
            port=config.port,
            log_level="critical",
            access_log=False,
        )
        self.server = uvicorn.Server(uvicorn_config)
        self.thread = threading.Thread(target=self.server.run, name="runtime-test-server")

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            try:
                status, _ = self.request("GET", "/v1/health")
                if status == 200:
                    return self
            except OSError:
                time.sleep(0.01)
        raise RuntimeError("test server did not start")

    def __exit__(self, exc_type, exc, traceback):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("test server did not stop")

    def open_stream_and_disconnect(self, path, body, before_disconnect=None):
        payload = json.dumps(body).encode("utf-8")
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"Authorization: Bearer {self.token}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + payload
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            connection.sendall(request)
            response_head = b""
            while b"\r\n\r\n" not in response_head:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response_head += chunk
            self.assert_response_started(response_head)
            if before_disconnect is not None:
                before_disconnect()
        finally:
            connection.close()

    @staticmethod
    def assert_response_started(response_head):
        if not response_head.startswith(b"HTTP/1.1 200"):
            raise AssertionError(f"observer did not start streaming: {response_head!r}")

    def request(self, method, path, body=None, token=None):
        if body is None or isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as exc:
            return exc.code, exc.read()


class RuntimeHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "runtime.sqlite3"
        self.token = "fake-content"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_health_auth_generation_turn_replay_and_secret_redaction(self) -> None:
        binding_id = uuid4()
        request_id = uuid4()
        generation_path = f"/v1/generations/{binding_id}"
        turn_path = f"{generation_path}/turns"
        spec = {
            "schema_version": "v1",
            "runtime_kind": "fake",
            "provider_model_id": "fake-model",
            "bootstrap_fingerprint": "bootstrap",
            "config_fingerprint": "config",
        }
        turn = {
            "schema_version": "v1",
            "request_id": str(request_id),
            "user_message": "hello",
            "behavior": "normal",
        }
        with LiveGateway(self.state_path, self.token) as gateway:
            health_status, health = gateway.request("GET", "/v1/health")
            self.assertEqual(health_status, 200)
            self.assertEqual(json.loads(health), {"status": "ok", "schema_version": "v1"})
            for supplied_token in (None, "wrong-token"):
                status, body = gateway.request(
                    "PUT", generation_path, spec, token=supplied_token
                )
                self.assertEqual(status, 401)
                self.assertNotIn(self.token.encode(), body)
            unknown_status, _ = gateway.request("GET", "/not-a-route")
            self.assertEqual(unknown_status, 401)
            self.assertEqual(sum(gateway.provider.generation_acquisitions.values()), 0)
            status, generation = gateway.request(
                "PUT", generation_path, spec, token=self.token
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(generation)["status"], "active")
            first_status, first_body = gateway.request(
                "POST", turn_path, turn, token=self.token
            )
            second_status, second_body = gateway.request(
                "POST", turn_path, turn, token=self.token
            )
            self.assertEqual((first_status, second_status), (200, 200))
            self.assertEqual(first_body, second_body)
            events = [json.loads(line) for line in first_body.splitlines()]
            self.assertEqual([event["sequence"] for event in events], list(range(1, 5)))
            self.assertEqual(sum(event["terminal"] for event in events), 1)
            self.assertEqual(events[-1]["event_type"], "done")
            self.assertEqual(events[-1]["terminal_status"], "completed")
            self.assertIs(events[-1]["bootstrap_consumed"], False)
            self.assertTrue(
                all(
                    event["terminal_status"] is None
                    and event["bootstrap_consumed"] is None
                    for event in events[:-1]
                )
            )
            self.assertNotIn(self.token.encode(), first_body)
            self.assertIn(b"[REDACTED]", first_body)
            rejected_status, rejected_body = gateway.request(
                "POST",
                turn_path,
                {"request_id": str(uuid4()), "user_message": self.token},
                token=self.token,
            )
            self.assertEqual(rejected_status, 400)
            self.assertNotIn(self.token.encode(), rejected_body)
            self.assertEqual(
                gateway.provider.turn_sends[(str(binding_id), str(request_id))],
                1,
            )
        persisted = b"".join(path.read_bytes() for path in Path(self.temp.name).iterdir())
        self.assertNotIn(self.token.encode(), persisted)

    def test_authenticated_validation_errors_never_echo_input_or_mutate_provider(self) -> None:
        binding_id = uuid4()
        generation_path = f"/v1/generations/{binding_id}"
        turn_path = f"{generation_path}/turns"
        retire_path = f"{generation_path}/retire"
        cases = (
            (
                "PUT",
                generation_path,
                {
                    "provider_model_id": "fake-model",
                    "bootstrap_fingerprint": "bootstrap",
                    "config_fingerprint": "config",
                    "extra": self.token,
                },
            ),
            (
                "POST",
                turn_path,
                {"request_id": self.token, "user_message": "hello"},
            ),
            (
                "POST",
                retire_path,
                {"reason": "retired", "extra": self.token},
            ),
            (
                "PUT",
                generation_path,
                f'{{"provider_model_id":"{self.token}"'.encode("utf-8"),
            ),
        )
        with LiveGateway(self.state_path, self.token) as gateway:
            for method, path, body in cases:
                with self.subTest(method=method, path=path, body_type=type(body).__name__):
                    status, response = gateway.request(
                        method,
                        path,
                        body,
                        token=self.token,
                    )
                    self.assertEqual(status, 422)
                    self.assertEqual(json.loads(response), {"error": "invalid_request"})
                    self.assertNotIn(self.token.encode(), response)
            unauthorized_status, unauthorized_body = gateway.request(
                "PUT",
                generation_path,
                f'{{"provider_model_id":"{self.token}"'.encode("utf-8"),
                token="wrong-token",
            )
            self.assertEqual(unauthorized_status, 401)
            self.assertEqual(json.loads(unauthorized_body), {"error": "unauthorized"})
            self.assertEqual(sum(gateway.provider.generation_acquisitions.values()), 0)
            self.assertEqual(sum(gateway.provider.turn_sends.values()), 0)
            with self.assertRaises(NotFoundError):
                gateway.app.state.runtime_store.get_generation(str(binding_id))
        persisted = b"".join(path.read_bytes() for path in Path(self.temp.name).iterdir())
        self.assertNotIn(self.token.encode(), persisted)

    def test_http_observer_disconnect_does_not_cancel_or_resend_owner(self) -> None:
        binding_id = uuid4()
        request_id = uuid4()
        generation_path = f"/v1/generations/{binding_id}"
        turn_path = f"{generation_path}/turns"
        cancel_path = f"{turn_path}/{request_id}/cancel"
        spec = {
            "provider_model_id": "fake-model",
            "bootstrap_fingerprint": "bootstrap",
            "config_fingerprint": "config",
        }
        turn = {
            "request_id": str(request_id),
            "user_message": "long HTTP owner",
            "behavior": "cancel_late",
        }
        owner_result = []

        with LiveGateway(self.state_path, self.token) as gateway:
            self.assertEqual(
                gateway.request("PUT", generation_path, spec, self.token)[0],
                200,
            )
            owner_thread = threading.Thread(
                target=lambda: owner_result.append(
                    gateway.request("POST", turn_path, turn, self.token)
                ),
                name="runtime-owner-client",
            )
            owner_thread.start()
            key = (str(binding_id), str(request_id))
            for _ in range(200):
                if gateway.provider.turn_sends[key]:
                    break
                time.sleep(0.01)
            self.assertEqual(gateway.provider.turn_sends[key], 1)
            gateway.open_stream_and_disconnect(turn_path, turn)
            time.sleep(0.1)
            durable = gateway.app.state.runtime_store.get_request(*key)
            self.assertEqual(durable.status, "sent")
            self.assertEqual(gateway.provider.turn_sends[key], 1)
            cancel_status, cancel_body = gateway.request(
                "POST",
                cancel_path,
                token=self.token,
            )
            self.assertEqual(cancel_status, 200)
            self.assertEqual(json.loads(cancel_body)["status"], "cancelled")
            owner_thread.join(timeout=5)
            self.assertFalse(owner_thread.is_alive())
            self.assertEqual(len(owner_result), 1)
            owner_status, owner_body = owner_result[0]
            self.assertEqual(owner_status, 200)
            owner_events = [json.loads(line) for line in owner_body.splitlines()]
            self.assertEqual(owner_events[-1]["payload"], {"code": "cancelled"})
            self.assertEqual(owner_events[-1]["terminal_status"], "cancelled")
            self.assertIs(owner_events[-1]["bootstrap_consumed"], False)
            self.assertEqual(
                gateway.app.state.runtime_store.terminal_count(*key),
                1,
            )
            self.assertEqual(gateway.provider.turn_sends[key], 1)

    def test_antigravity_http_selection_contract_and_fake_control_rejection(self) -> None:
        binding_id = uuid4()
        request_id = uuid4()
        generation_path = f"/v1/generations/{binding_id}"
        turn_path = f"{generation_path}/turns"
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fake_agy.py"
        data_root = Path(self.temp.name) / "providers"
        evidence = Path(self.temp.name) / "evidence.jsonl"
        supervisor = AgyProcessSupervisor(
            AgyProcessConfig(
                command_prefix=(sys.executable, str(fixture)),
                init_timeout_seconds=1,
                idle_timeout_seconds=1,
                hard_timeout_seconds=3,
                close_timeout_seconds=1,
                require_official_executable=False,
                environment_overrides={
                    "FAKE_AGY_SCENARIO": "normal",
                    "FAKE_AGY_EVIDENCE": str(evidence),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
                },
            )
        )
        antigravity = AntigravityAdapter(data_root, supervisor)
        providers = {"fake": DeterministicFakeAdapter(), "antigravity": antigravity}
        spec = {
            "runtime_kind": "antigravity",
            "provider_model_id": "gemini-3.1-pro-high",
            "bootstrap_fingerprint": "bootstrap",
            "config_fingerprint": "config",
            "system_instructions": "private system contract",
        }
        with LiveGateway(self.state_path, self.token, providers) as gateway:
            unknown = {**spec, "runtime_kind": "unknown"}
            unknown_status, unknown_body = gateway.request(
                "PUT", generation_path, unknown, self.token
            )
            self.assertEqual(unknown_status, 422)
            self.assertEqual(json.loads(unknown_body), {"error": "invalid_request"})
            with self.assertRaises(NotFoundError):
                gateway.app.state.runtime_store.get_generation(str(binding_id))

            status, body = gateway.request("PUT", generation_path, spec, self.token)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "active")
            rejected = {
                "request_id": str(request_id),
                "user_message": "must reject",
                "bootstrap_context": {"history": []},
                "behavior": "exception",
            }
            rejected_status, rejected_body = gateway.request(
                "POST", turn_path, rejected, self.token
            )
            self.assertEqual(rejected_status, 400)
            self.assertEqual(json.loads(rejected_body), {"error": "invalid_request"})
            self.assertIsNone(
                gateway.app.state.runtime_store.get_request(
                    str(binding_id), str(request_id)
                )
            )

            turn = {
                "request_id": str(uuid4()),
                "user_message": "hello through HTTP",
                "bootstrap_context": {"history": []},
                "ephemeral_current": "http-ephemeral-canary",
            }
            turn_status, turn_body = gateway.request(
                "POST", turn_path, turn, self.token
            )
            self.assertEqual(turn_status, 200)
            events = [json.loads(line) for line in turn_body.splitlines()]
            self.assertEqual(events[-1]["event_type"], "done")
            self.assertNotIn("http-ephemeral-canary", turn_body.decode("utf-8"))

    def test_antigravity_http_owner_disconnect_closes_process_tree(self) -> None:
        binding_id = uuid4()
        request_id = uuid4()
        generation_path = f"/v1/generations/{binding_id}"
        turn_path = f"{generation_path}/turns"
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fake_agy.py"
        evidence_path = Path(self.temp.name) / "disconnect-evidence.jsonl"
        supervisor = AgyProcessSupervisor(
            AgyProcessConfig(
                command_prefix=(sys.executable, str(fixture)),
                init_timeout_seconds=1,
                idle_timeout_seconds=30,
                hard_timeout_seconds=60,
                close_timeout_seconds=0.5,
                require_official_executable=False,
                environment_overrides={
                    "FAKE_AGY_SCENARIO": "slow_tree",
                    "FAKE_AGY_EVIDENCE": str(evidence_path),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
                },
            )
        )
        antigravity = AntigravityAdapter(
            Path(self.temp.name) / "disconnect-providers",
            supervisor,
        )
        providers = {"fake": DeterministicFakeAdapter(), "antigravity": antigravity}
        spec = {
            "runtime_kind": "antigravity",
            "provider_model_id": "gemini-3.1-pro-high",
            "bootstrap_fingerprint": "bootstrap",
            "config_fingerprint": "config",
            "system_instructions": "private system contract",
        }
        turn = {
            "request_id": str(request_id),
            "user_message": "disconnect owner",
            "bootstrap_context": {"history": []},
        }
        with LiveGateway(self.state_path, self.token, providers) as gateway:
            self.assertEqual(
                gateway.request("PUT", generation_path, spec, self.token)[0],
                200,
            )
            process_ids = []

            def wait_until_provider_child_exists():
                for _ in range(300):
                    if evidence_path.exists():
                        evidence = [
                            json.loads(line)
                            for line in evidence_path.read_text(encoding="utf-8").splitlines()
                        ]
                        spawns = [item for item in evidence if item["kind"] == "spawn"]
                        children = [item for item in evidence if item["kind"] == "child"]
                        if spawns and children:
                            process_ids.extend(
                                (spawns[-1]["pid"], children[-1]["child_pid"])
                            )
                            return
                    time.sleep(0.01)
                raise AssertionError("provider child did not start before disconnect")

            gateway.open_stream_and_disconnect(
                turn_path,
                turn,
                wait_until_provider_child_exists,
            )
            for _ in range(300):
                record = gateway.app.state.runtime_store.get_request(
                    str(binding_id),
                    str(request_id),
                )
                if record is not None and record.status == "cancelled":
                    break
                time.sleep(0.01)
            self.assertIsNotNone(record)
            self.assertEqual(record.status, "cancelled")
            self.assertEqual(len(process_ids), 2)
            self.assertNotIn(str(binding_id), antigravity._prepared)
            self.assertNotIn(str(binding_id), supervisor._sessions)
            for process_id in process_ids:
                check = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotIn(str(process_id), check.stdout)

    def test_restart_replays_completed_request_without_provider_send(self) -> None:
        binding_id = uuid4()
        request_id = uuid4()
        generation_path = f"/v1/generations/{binding_id}"
        turn_path = f"{generation_path}/turns"
        spec = {
            "provider_model_id": "fake-model",
            "bootstrap_fingerprint": "bootstrap",
            "config_fingerprint": "config",
        }
        turn = {"request_id": str(request_id), "user_message": "restart"}
        with LiveGateway(self.state_path, self.token) as first_gateway:
            self.assertEqual(
                first_gateway.request("PUT", generation_path, spec, self.token)[0],
                200,
            )
            first = first_gateway.request("POST", turn_path, turn, self.token)
        with LiveGateway(self.state_path, self.token) as second_gateway:
            replay = second_gateway.request("POST", turn_path, turn, self.token)
            self.assertEqual(first, replay)
            self.assertEqual(sum(second_gateway.provider.turn_sends.values()), 0)
        self.assertFalse(any(thread.name == "runtime-test-server" for thread in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
