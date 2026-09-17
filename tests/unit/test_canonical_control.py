"""Generic canonical-control backing: atomic writes, healing, reparse rejection."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.control import (
    CanonicalControlStore,
    ReservedControlArtifact,
)


ARTIFACT = ReservedControlArtifact("canonical_demo.txt", "reserved/demo.txt")

# Windows: a helper process must never open a visible console window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def make_directory_reparse_point(link: Path, target: Path) -> bool:
    """Create a directory junction (symlink fallback) without elevation."""

    if os.name != "nt":
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except OSError:
            return False
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        creationflags=NO_WINDOW,
    )
    return completed.returncode == 0


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class CanonicalControlStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "generation"
        (self.root / "workspace").mkdir(parents=True)
        self.store = CanonicalControlStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def target_path(self) -> Path:
        return self.root / "workspace" / "reserved" / "demo.txt"

    def test_prepare_creates_real_control_directory(self) -> None:
        self.store.prepare()
        control = self.root / "control"
        self.assertTrue(control.is_dir())
        self.assertFalse(control.is_symlink())
        self.store.prepare()

    def test_prepare_rejects_reparse_control_directory(self) -> None:
        control = self.root / "control"
        control.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        control.rmdir()
        if not make_directory_reparse_point(control, outside):
            self.skipTest("host cannot create directory reparse points")
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.prepare()
        self.assertEqual(caught.exception.code, "agy_control_backing_invalid")
        self.assertTrue(caught.exception.fatal_generation)

    def test_reparse_canonical_control_directory_never_reads_or_writes_outside(self) -> None:
        # Regression (CP1 R1-02): the canonical read path validated only the
        # canonical file, so replacing ``control/`` with a junction redirected
        # read/verify/restore to an outside directory.
        self.store.write_canonical("canonical_demo.txt", "rules-v1")
        control = self.root / "control"
        control.rename(Path(self.temp.name) / "relocated_control")
        outside = Path(self.temp.name) / "outside_control"
        outside.mkdir()
        outside_body = outside / "canonical_demo.txt"
        outside_body.write_text("OUTSIDE", encoding="utf-8")
        if not make_directory_reparse_point(control, outside):
            self.skipTest("host cannot create directory reparse points")

        for operation in (
            lambda: self.store.read_canonical("canonical_demo.txt"),
            lambda: self.store.verify(ARTIFACT),
            lambda: self.store.restore(ARTIFACT),
            lambda: self.store.write_canonical("canonical_demo.txt", "tampered"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(ProviderAdapterError) as caught:
                    operation()
                self.assertEqual(caught.exception.code, "agy_control_backing_invalid")
                self.assertTrue(caught.exception.fatal_generation)

        self.assertEqual(outside_body.read_text(encoding="utf-8"), "OUTSIDE")
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["canonical_demo.txt"])
        self.assertFalse((self.root / "workspace" / "reserved").exists())
        self.assertTrue(control.exists())

    def test_reparse_generation_root_fails_closed_on_canonical_read(self) -> None:
        real_root = Path(self.temp.name) / "real_generation"
        (real_root / "workspace").mkdir(parents=True)
        CanonicalControlStore(real_root).write_canonical("canonical_demo.txt", "rules-v1")
        linked_root = Path(self.temp.name) / "linked_generation"
        if not make_directory_reparse_point(linked_root, real_root):
            self.skipTest("host cannot create directory reparse points")
        linked_store = CanonicalControlStore(linked_root)
        with self.assertRaises(ProviderAdapterError) as caught:
            linked_store.read_canonical("canonical_demo.txt")
        self.assertEqual(caught.exception.code, "agy_control_backing_invalid")
        self.assertTrue(caught.exception.fatal_generation)
        with self.assertRaises(ProviderAdapterError) as caught:
            linked_store.prepare()
        self.assertEqual(caught.exception.code, "agy_control_backing_invalid")
        self.assertEqual(
            (real_root / "control" / "canonical_demo.txt").read_text(encoding="utf-8"),
            "rules-v1",
        )

    def test_file_at_control_backing_path_fails_closed(self) -> None:
        (self.root / "control").write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.read_canonical("canonical_demo.txt")
        self.assertEqual(caught.exception.code, "agy_control_backing_invalid")
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.write_canonical("canonical_demo.txt", "rules-v1")
        self.assertEqual(caught.exception.code, "agy_control_backing_invalid")
        self.assertEqual((self.root / "control").read_text(encoding="utf-8"), "not a directory")

    def test_write_canonical_is_atomic_and_readable(self) -> None:
        self.store.write_canonical("canonical_demo.txt", "authoritative body")
        self.assertEqual(self.store.read_canonical("canonical_demo.txt"), "authoritative body")
        contents = sorted(path.name for path in (self.root / "control").iterdir())
        self.assertEqual(contents, ["canonical_demo.txt"])

    def test_write_canonical_rejects_unsafe_names(self) -> None:
        for name in ("../escape.txt", "a/b.txt", "a\\b.txt", "C:escape.txt", "", ".", ".."):
            with self.subTest(name=name):
                with self.assertRaises(ProviderAdapterError) as caught:
                    self.store.write_canonical(name, "body")
                self.assertEqual(caught.exception.code, "agy_control_backing_invalid")

    def test_restore_materializes_missing_and_is_idempotent(self) -> None:
        self.store.write_canonical("canonical_demo.txt", "rules-v1")
        self.assertTrue(self.store.restore(ARTIFACT))
        target = self.target_path()
        self.assertEqual(target.read_text(encoding="utf-8"), "rules-v1")
        self.assertFalse(target.is_symlink())
        self.assertTrue(self.store.verify(ARTIFACT))
        self.assertFalse(self.store.restore(ARTIFACT))

    def test_restore_repairs_tampered_target_and_preserves_ordinary_files(self) -> None:
        self.store.write_canonical("canonical_demo.txt", "rules-v1")
        self.store.restore(ARTIFACT)
        ordinary = self.root / "workspace" / "ordinary.txt"
        ordinary.write_text("ordinary body", encoding="utf-8")
        nested = self.root / "workspace" / "plain" / "deep.txt"
        nested.parent.mkdir()
        nested.write_text("nested body", encoding="utf-8")
        self.target_path().write_text("tampered", encoding="utf-8")
        self.assertTrue(self.store.restore(ARTIFACT))
        self.assertEqual(self.target_path().read_text(encoding="utf-8"), "rules-v1")
        self.assertEqual(ordinary.read_text(encoding="utf-8"), "ordinary body")
        self.assertEqual(nested.read_text(encoding="utf-8"), "nested body")

    def test_restore_fails_closed_when_canonical_backing_missing(self) -> None:
        self.store.prepare()
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.restore(ARTIFACT)
        self.assertEqual(caught.exception.code, "agy_control_backing_missing")
        self.assertTrue(caught.exception.fatal_generation)

    def test_real_directory_at_reserved_path_fails_closed_without_destroying_it(self) -> None:
        self.store.write_canonical("canonical_demo.txt", "rules-v1")
        reserved = self.root / "workspace" / "reserved"
        (reserved / "demo.txt").mkdir(parents=True)
        inner = reserved / "demo.txt" / "owned.txt"
        inner.write_text("belongs to someone", encoding="utf-8")
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.restore(ARTIFACT)
        self.assertEqual(caught.exception.code, "agy_control_artifact_invalid")
        self.assertTrue(inner.is_file())
        self.assertEqual(inner.read_text(encoding="utf-8"), "belongs to someone")

    def test_reparse_parent_directory_fails_closed_without_touching_outside(self) -> None:
        self.store.write_canonical("canonical_demo.txt", "rules-v1")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        sentinel = outside / "demo.txt"
        sentinel.write_text("outside sentinel", encoding="utf-8")
        reserved = self.root / "workspace" / "reserved"
        if not make_directory_reparse_point(reserved, outside):
            self.skipTest("host cannot create directory reparse points")
        self.assertFalse(self.store.verify(ARTIFACT))
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.restore(ARTIFACT)
        self.assertEqual(caught.exception.code, "agy_control_artifact_invalid")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside sentinel")
        self.assertTrue(reserved.is_dir())

    def test_reparse_point_at_target_path_is_replaced_with_canonical_copy(self) -> None:
        self.store.write_canonical("canonical_demo.txt", "rules-v1")
        self.store.restore(ARTIFACT)
        target = self.target_path()
        target.unlink()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("outside sentinel", encoding="utf-8")
        if not make_directory_reparse_point(target, outside):
            self.skipTest("host cannot create directory reparse points")
        self.assertTrue(self.store.restore(ARTIFACT))
        self.assertFalse(is_reparse_point(target))
        self.assertEqual(target.read_text(encoding="utf-8"), "rules-v1")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside sentinel")

    def test_file_symlink_target_is_replaced_with_canonical_copy(self) -> None:
        self.store.write_canonical("canonical_demo.txt", "rules-v1")
        reserved = self.root / "workspace" / "reserved"
        reserved.mkdir()
        decoy = Path(self.temp.name) / "decoy.txt"
        decoy.write_text("decoy body", encoding="utf-8")
        try:
            os.symlink(decoy, reserved / "demo.txt")
        except OSError:
            self.skipTest("host cannot create file symlinks without elevation")
        self.assertTrue(self.store.restore(ARTIFACT))
        target = self.target_path()
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "rules-v1")
        self.assertEqual(decoy.read_text(encoding="utf-8"), "decoy body")

    def test_restore_rejects_escaping_workspace_path(self) -> None:
        self.store.write_canonical("canonical_demo.txt", "rules-v1")
        escaping = ReservedControlArtifact("canonical_demo.txt", "../escape.txt")
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.restore(escaping)
        self.assertEqual(caught.exception.code, "agy_control_artifact_invalid")
        self.assertFalse((self.root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
