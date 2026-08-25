from pathlib import Path
import unittest


class ImportBoundaryTests(unittest.TestCase):
    def test_runtime_source_contains_no_forbidden_import_or_external_invocation(self) -> None:
        root = Path(__file__).resolve().parents[2] / "src" / "exocore_runtime"
        source = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.py"))
        forbidden = (
            "import django",
            "from django",
            "import agents",
            "from agents",
            "import engines",
            "from engines",
            "import google",
            "from google",
            "import agy",
            "subprocess",
            "sys.path",
            "mcp",
        )
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source.lower())


if __name__ == "__main__":
    unittest.main()
