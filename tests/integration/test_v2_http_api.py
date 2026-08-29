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


if __name__ == "__main__":
    unittest.main()
