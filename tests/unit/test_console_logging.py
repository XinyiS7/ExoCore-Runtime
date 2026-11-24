"""Console lines carry one timestamp each (XinyiS7/ExoCore#141)."""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"

# Drives the real ``main()`` in a child process: only the app build and the
# server loop are replaced, while uvicorn applies the logging configuration it
# was really given.
_CHILD = textwrap.dedent(
    """
    import logging
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, sys.argv[1])
    import uvicorn
    from exocore_runtime import __main__ as entry

    def create_app(config):
        logging.getLogger("exocore_runtime.startup").warning("STARTUP-WARNING")
        return object()

    def run(app, **kwargs):
        uvicorn.Config(app, **kwargs)
        for name in ("exocore_runtime.service", "third.party"):
            logger = logging.getLogger(name)
            logger.debug("%s-DEBUG", name)
            logger.info("%s-INFO", name)
            logger.warning("%s-WARNING", name)
            logger.error("%s-ERROR", name)
        logging.getLogger("uvicorn.error").debug("uvicorn.error-DEBUG")
        logging.getLogger("uvicorn.error").info("uvicorn.error-INFO")
        logging.getLogger("uvicorn.error").error("uvicorn.error-ERROR")
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d', "ACCESS", "GET", "/", "1.1", 200
        )
        print("ACCESS_LOG", kwargs["access_log"], file=sys.stdout)

    entry.RuntimeConfig = SimpleNamespace(
        from_env=lambda: SimpleNamespace(host="127.0.0.1", port=1)
    )
    entry.create_app = create_app
    entry.uvicorn = SimpleNamespace(run=run)
    entry.main()
    """
)


class ConsoleLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD, str(_SRC)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        cls.stdout = completed.stdout.splitlines()
        cls.stderr = completed.stderr.splitlines()

    def test_each_visible_record_is_one_stamped_line(self) -> None:
        expected = [("WARNING", "exocore_runtime.startup", "STARTUP-WARNING")]
        for name in ("exocore_runtime.service", "third.party"):
            for level in ("WARNING", "ERROR"):
                expected.append((level, name, f"{name}-{level}"))
        for level in ("INFO", "ERROR"):
            expected.append((level, "uvicorn.error", f"uvicorn.error-{level}"))
        stamp = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}"
        self.assertEqual(len(self.stderr), len(expected), self.stderr)
        for line, (level, name, message) in zip(self.stderr, expected):
            with self.subTest(message=message):
                self.assertRegex(
                    line, f"^{stamp} {level} \\[{re.escape(name)}\\] {re.escape(message)}$"
                )

    def test_levels_and_access_log_stay_unchanged(self) -> None:
        console = "\n".join(self.stderr + self.stdout)
        for hidden in (
            "exocore_runtime.service-INFO",
            "third.party-INFO",
            "-DEBUG",
            "ACCESS -",
        ):
            self.assertNotIn(hidden, console)
        self.assertEqual(self.stdout, ["ACCESS_LOG False"])


if __name__ == "__main__":
    unittest.main()
