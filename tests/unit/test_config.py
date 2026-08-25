from pathlib import Path
import unittest

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


if __name__ == "__main__":
    unittest.main()
