import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.process import (
    AgyProcessConfig,
    AgyProcessSupervisor,
    _log_init_stderr,
    _resolve_github_token,
)


class GithubTokenResolutionTests(unittest.TestCase):
    @patch("exocore_runtime.providers.antigravity.process.subprocess.run")
    @patch("exocore_runtime.providers.antigravity.process.shutil.which")
    def test_resolves_token_from_host_github_cli_once(self, which, run):
        which.return_value = "C:/tools/gh.exe"
        run.return_value = subprocess.CompletedProcess(
            args=["C:/tools/gh.exe", "auth", "token", "--hostname", "github.com"],
            returncode=0,
            stdout="github-token\n",
            stderr="",
        )

        self.assertEqual(_resolve_github_token(), "github-token")
        run.assert_called_once_with(
            ["C:/tools/gh.exe", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    @patch("exocore_runtime.providers.antigravity.process.shutil.which", return_value=None)
    def test_missing_github_cli_is_explicit_and_nonfatal(self, _which):
        with self.assertLogs(
            "exocore_runtime.providers.antigravity.process", level="WARNING"
        ) as logs:
            token = _resolve_github_token()

        self.assertIsNone(token)
        self.assertIn("GitHub CLI unavailable", logs.output[0])

    @patch("exocore_runtime.providers.antigravity.process.subprocess.run")
    @patch("exocore_runtime.providers.antigravity.process.shutil.which")
    def test_unauthenticated_github_cli_is_explicit_and_nonfatal(self, which, run):
        which.return_value = "C:/tools/gh.exe"
        run.return_value = subprocess.CompletedProcess(
            args=["C:/tools/gh.exe", "auth", "token", "--hostname", "github.com"],
            returncode=1,
            stdout="",
            stderr="not logged in",
        )

        with self.assertLogs(
            "exocore_runtime.providers.antigravity.process", level="WARNING"
        ) as logs:
            token = _resolve_github_token()

        self.assertIsNone(token)
        self.assertIn("GitHub CLI is not authenticated", logs.output[0])

    @patch("exocore_runtime.providers.antigravity.process.subprocess.run")
    @patch("exocore_runtime.providers.antigravity.process.shutil.which")
    def test_github_cli_failure_is_explicit_and_nonfatal(self, which, run):
        which.return_value = "C:/tools/gh.exe"
        run.side_effect = subprocess.TimeoutExpired("gh", 5)

        with self.assertLogs(
            "exocore_runtime.providers.antigravity.process", level="WARNING"
        ) as logs:
            token = _resolve_github_token()

        self.assertIsNone(token)
        self.assertIn("GitHub CLI token lookup failed", logs.output[0])
        self.assertNotIn("github-token", logs.output[0])

    @patch("exocore_runtime.providers.antigravity.process._resolve_github_token")
    @patch("exocore_runtime.providers.antigravity.process.shutil.which")
    def test_official_config_captures_token_before_profile_isolation(
        self, which, resolve_token
    ):
        which.return_value = sys.executable
        resolve_token.return_value = "github-token"

        config = AgyProcessConfig.official()

        self.assertEqual(config.github_token, "github-token")
        resolve_token.assert_called_once_with()


class IsolatedEnvironmentGithubTokenTests(unittest.TestCase):
    def build_supervisor(self, github_token=None, environment_overrides=None):
        config = AgyProcessConfig(
            command_prefix=("agy",),
            require_official_executable=False,
            github_token=github_token,
            environment_overrides=environment_overrides or {},
        )
        with patch.object(AgyProcessSupervisor, "_ensure_gateway_crash_job"):
            return AgyProcessSupervisor(config)

    def test_configured_token_is_injected_after_sensitive_environment_filter(self):
        supervisor = self.build_supervisor(github_token="configured-token")
        with patch.dict(os.environ, {"GH_TOKEN": "ambient-token"}, clear=True):
            environment = supervisor._isolated_environment(None)

        self.assertEqual(environment["GH_TOKEN"], "configured-token")

    def test_ambient_token_remains_filtered_without_runtime_injection(self):
        supervisor = self.build_supervisor()
        with patch.dict(os.environ, {"GH_TOKEN": "ambient-token"}, clear=True):
            environment = supervisor._isolated_environment(None)

        self.assertNotIn("GH_TOKEN", environment)

    def test_sensitive_environment_override_is_still_rejected(self):
        supervisor = self.build_supervisor(
            environment_overrides={"GH_TOKEN": "override-token"}
        )
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderAdapterError) as raised:
                supervisor._isolated_environment(None)

        self.assertEqual(raised.exception.code, "agy_sensitive_environment")

    def test_init_stderr_redacts_injected_token(self):
        with self.assertLogs(
            "exocore_runtime.providers.antigravity.process", level="WARNING"
        ) as logs:
            _log_init_stderr(
                b"failed while using github-token",
                ("github-token",),
            )

        self.assertNotIn("github-token", logs.output[0])
        self.assertIn("[REDACTED]", logs.output[0])


if __name__ == "__main__":
    unittest.main()
