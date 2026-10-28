import json
from pathlib import Path
import sqlite3
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

    def write_provider_db(self, payloads):
        directory = self.layout.profile / ".gemini" / "antigravity-cli" / "conversations"
        directory.mkdir(parents=True, exist_ok=True)
        db = directory / "session.db"
        connection = sqlite3.connect(db)
        try:
            connection.execute(
                "create table steps (idx integer primary key, step_type integer, step_payload blob)"
            )
            for index, payload in payloads:
                connection.execute(
                    "insert into steps values (?, 17, ?)", (index, payload)
                )
            connection.commit()
        finally:
            connection.close()
        return db

    def test_provider_error_text_is_read_from_agy_own_database(self):
        # The stdout frame only carries the step marker, never the message.
        message = (
            b"API error (attempt 1): request failed: Post https://example.invalid: "
            b"write tcp 127.0.0.1:1->127.0.0.1:2: wsasend: forcibly closed."
        )
        self.write_provider_db([(7, b"\x0a\x03old"), (8, b"\xc2\x01\x20" + message)])
        trace = FailureDiagnostics(
            self.layout, "request", {"EXOCORE_RUNTIME_TOKEN": "private-token-value"},
            provider_session_id="session",
        )
        trace.observe("stdout", b'{"event":"step_update","step_update":{"step_type":"error_message"}}\n')
        trace.persist("agy_unknown_step")
        data = json.loads(trace.path.read_text(encoding="utf-8"))
        self.assertEqual(data["provider_error"]["source"], "agy_conversation_db")
        self.assertEqual(data["provider_error"]["text"], message.decode())
        self.assertEqual(data["provider_error"]["truncated"], False)

    def test_missing_provider_database_leaves_the_record_empty(self):
        trace = FailureDiagnostics(self.layout, "request", {}, provider_session_id="session")
        trace.observe("stderr", b"original failure")
        trace.persist("agy_stderr")
        data = json.loads(trace.path.read_text(encoding="utf-8"))
        self.assertEqual(data["provider_error"], None)
        self.assertEqual(data["last_record"]["raw_text"], "original failure")
