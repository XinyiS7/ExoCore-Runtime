import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from exocore_runtime.contracts import ProcessExecutionOptions
from exocore_runtime.providers.antigravity.diagnostics import FailureDiagnostics
from exocore_runtime.providers.antigravity.process import GenerationLayout


class FailureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "providers" / "binding"
        self.layout = GenerationLayout(
            binding_id="binding", root=root, profile=root / "profile",
            work_dir=root / "workspace", agent_name="fixture",
            provider_session_id="session",
            execution_options=ProcessExecutionOptions(
                provider_model_slug="gemini-3.1-pro-medium", effort="medium",
                sandbox=False, security_policy_revision="fixture",
                launch_environment_revision="fixture",
            ),
        )

    def test_secrets_are_redacted_before_persistence(self):
        trace = FailureDiagnostics(self.layout, "request", {"EXOCORE_RUNTIME_TOKEN": "private-token-value"}, provider_session_id="session", secret_values=("config-only-token",))
        trace.observe("stdout", b'{"event":"result","result":{"status":"ERROR","error":"private-token-value","config_secret":"config-only-token","authorization":"Bearer other-token"}}\n')
        trace.mark_result()
        trace.persist("agy_result_error")
        data = json.loads(trace.path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(data["last_record"]["raw_text"]), {
            "event": "result", "result": {
                "status": "ERROR", "error": "[REDACTED]", "config_secret": "[REDACTED]", "authorization": "[REDACTED]",
            },
        })
        self.assertEqual(data["last_record"], data["result_record"])
        self.assertEqual(data["last_record"]["truncated"], False)

    def test_truncation_is_explicit(self):
        trace = FailureDiagnostics(self.layout, "request", {}, provider_session_id="session")
        trace.observe("stderr", b"x" * (256 * 1024 + 1))
        trace.persist("agy_stderr")
        data = json.loads(trace.path.read_text(encoding="utf-8"))
        self.assertEqual(data["last_record"], {
            "channel": "stderr", "original_bytes": 256 * 1024 + 1,
            "truncated": True, "raw_text": "x" * (256 * 1024),
        })

    def test_disk_failure_is_explicit_and_does_not_escape(self):
        trace = FailureDiagnostics(self.layout, "request", {}, provider_session_id="session")
        trace.observe("stderr", b"original failure")
        with patch("exocore_runtime.providers.antigravity.diagnostics.os.open", side_effect=OSError("disk full")):
            with self.assertLogs("exocore_runtime.providers.antigravity.diagnostics", level="WARNING") as logs:
                result = trace.persist("agy_stderr")
        self.assertEqual(result, None)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].args, ("OSError", trace.path, "agy_stderr"))