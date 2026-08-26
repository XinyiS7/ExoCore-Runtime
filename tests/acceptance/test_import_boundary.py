import ast
from pathlib import Path
import unittest


class ImportBoundaryTests(unittest.TestCase):
    def test_runtime_source_preserves_milestone_b_import_and_execution_boundary(self) -> None:
        root = Path(__file__).resolve().parents[2] / "src" / "exocore_runtime"
        python_files = tuple(root.rglob("*.py"))
        forbidden_roots = {"django", "agents", "engines", "google", "mcp"}
        subprocess_importers = set()
        for path in python_files:
            relative = path.relative_to(root).as_posix()
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = [node.module.split(".")[0]]
                self.assertTrue(
                    forbidden_roots.isdisjoint(imported),
                    f"forbidden import in {relative}: {imported}",
                )
                if "subprocess" in imported:
                    subprocess_importers.add(relative)
            lowered = source.lower()
            for marker in (
                "sys.path",
                "google python sdk",
                "vertex adc",
                "shell=true",
                "binary patch",
                "chrome devtools",
            ):
                with self.subTest(path=relative, marker=marker):
                    self.assertNotIn(marker, lowered)
        self.assertEqual(
            subprocess_importers,
            {
                "providers/antigravity/adapter.py",
                "providers/antigravity/process.py",
            },
        )


if __name__ == "__main__":
    unittest.main()
