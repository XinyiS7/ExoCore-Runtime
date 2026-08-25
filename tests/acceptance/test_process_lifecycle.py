import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ProcessLifecycleTests(unittest.TestCase):
    def test_module_startup_health_and_shutdown_leave_no_listener(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            env = os.environ.copy()
            env.update(
                {
                    "EXOCORE_RUNTIME_HOST": "127.0.0.1",
                    "EXOCORE_RUNTIME_PORT": str(port),
                    "EXOCORE_RUNTIME_TOKEN": "process-secret-canary",
                    "EXOCORE_RUNTIME_STATE_PATH": str(Path(temp_dir) / "state.sqlite3"),
                }
            )
            process = subprocess.Popen(
                [sys.executable, "-m", "exocore_runtime"],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                for _ in range(200):
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=1) as response:
                            health = json.loads(response.read())
                        break
                    except OSError:
                        if process.poll() is not None:
                            stdout, stderr = process.communicate()
                            self.fail(f"gateway exited before health: {stdout!r} {stderr!r}")
                        time.sleep(0.01)
                else:
                    self.fail("gateway did not become healthy")
                self.assertEqual(health, {"status": "ok", "schema_version": "v1"})
                binding_id = "11111111-1111-1111-1111-111111111111"
                invalid_payload = json.dumps(
                    {
                        "provider_model_id": "fake-model",
                        "bootstrap_fingerprint": "bootstrap",
                        "config_fingerprint": "config",
                        "extra": "process-secret-canary",
                    }
                ).encode("utf-8")
                invalid_request = Request(
                    f"http://127.0.0.1:{port}/v1/generations/{binding_id}",
                    data=invalid_payload,
                    headers={
                        "Authorization": "Bearer process-secret-canary",
                        "Content-Type": "application/json",
                    },
                    method="PUT",
                )
                with self.assertRaises(HTTPError) as caught:
                    urlopen(invalid_request, timeout=2)
                self.assertEqual(caught.exception.code, 422)
                validation_body = caught.exception.read()
                self.assertEqual(json.loads(validation_body), {"error": "invalid_request"})
                self.assertNotIn(b"process-secret-canary", validation_body)
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=5)
                    self.fail("gateway required forced kill")
            combined = stdout + stderr
            self.assertNotIn(b"process-secret-canary", combined)
            closed_probe = socket.socket()
            closed_probe.settimeout(0.2)
            try:
                self.assertNotEqual(closed_probe.connect_ex(("127.0.0.1", port)), 0)
            finally:
                closed_probe.close()


if __name__ == "__main__":
    unittest.main()
