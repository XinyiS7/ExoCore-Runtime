from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from exocore_runtime.config import RuntimeConfig


class RuntimeConfigTests(unittest.TestCase):
    def test_loopback_ipv4_and_ipv6_are_accepted(self) -> None:
        for host in ("127.0.0.1", "127.1.2.3", "::1"):
            config = RuntimeConfig(host, 8766, "canary-token", Path("state.db"))
            self.assertEqual(config.host, host)

    def test_non_loopback_and_unspecified_addresses_are_rejected(self) -> None:
        for host in ("0.0.0.0", "::", "192.168.1.10", "localhost"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                RuntimeConfig(host, 8766, "canary-token", Path("state.db"))

    def test_token_is_required_and_redacted_from_repr(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig("127.0.0.1", 8766, "", Path("state.db"))
        config = RuntimeConfig("127.0.0.1", 8766, "canary-token", Path("state.db"))
        self.assertNotIn("canary-token", repr(config))
        self.assertIn("[REDACTED]", repr(config))
        self.assertIn("memory_mcp_root='[PRIVATE]'", repr(config))

    def test_default_agy_timeout_budget_supports_voice_manifest(self) -> None:
        config = RuntimeConfig(
            "127.0.0.1", 8766, "canary-token", Path("state.db")
        )
        self.assertLess(45, config.agy_idle_timeout)
        self.assertLessEqual(config.agy_idle_timeout, config.agy_hard_timeout)
        with self.assertRaisesRegex(ValueError, "cannot exceed hard timeout"):
            RuntimeConfig(
                "127.0.0.1",
                8766,
                "canary-token",
                Path("state.db"),
                agy_idle_timeout=181,
                agy_hard_timeout=180,
            )

    def test_memory_mcp_root_env_override_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "os.environ",
            {
                "EXOCORE_RUNTIME_TOKEN": "canary-token",
                "EXOCORE_RUNTIME_MEMORY_MCP_ROOT": temp_dir,
            },
            clear=False,
        ):
            config = RuntimeConfig.from_env()

        self.assertEqual(config.effective_memory_mcp_root, Path(temp_dir).resolve())


if __name__ == "__main__":
    unittest.main()
