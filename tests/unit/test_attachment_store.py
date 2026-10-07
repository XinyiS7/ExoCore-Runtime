from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from exocore_runtime.contracts import AttachmentManifest
from exocore_runtime.errors import (
    AttachmentCapacityExceededError,
    AttachmentSizeExceededError,
    AttachmentStagingError,
    ProviderAdapterError,
)
from exocore_runtime.providers.antigravity.attachments import AttachmentStore


PNG = b"\x89PNG\r\n\x1a\ncanonical-pixels"
JPEG = b"\xff\xd8\xffcanonical-jpeg"
WEBP = b"RIFF\x00\x00\x00\x00WEBPcanonical-webp"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def make_directory_link(target: Path, link: Path) -> bool:
    """Create a directory symlink or a Windows junction for the safety test."""

    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        pass
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
        creationflags=NO_WINDOW,
    )
    return completed.returncode == 0


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "generation"
        (self.root / "workspace").mkdir(parents=True)
        self.store = AttachmentStore(self.root)
        self.request_id = str(uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, data=PNG, **overrides):
        values = {
            "artifact_id": "att-7",
            "display_name": "photo.png",
            "mime_type": "image/png",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        values.update(overrides)
        return AttachmentManifest(**values)

    def test_stage_uses_opaque_name_atomic_overwrite_and_request_caps(self):
        path = self.store.stage(self.request_id, "att-1", b"first")
        self.assertEqual(path.name, "att-1.blob")
        self.assertEqual(path.read_bytes(), b"first")
        self.store.stage(self.request_id, "att-1", b"replacement")
        self.assertEqual(path.read_bytes(), b"replacement")
        self.assertFalse(any(entry.suffix == ".tmp" for entry in path.parent.iterdir()))

        for index in range(2, 6):
            self.store.stage(self.request_id, f"att-{index}", b"x")
        with self.assertRaises(AttachmentCapacityExceededError):
            self.store.stage(self.request_id, "att-6", b"x")

        other_request = str(uuid4())
        with mock.patch(
            "exocore_runtime.providers.antigravity.attachments."
            "MAX_ATTACHMENT_TOTAL_BYTES",
            5,
        ):
            self.store.stage(other_request, "att-1", b"123")
            with self.assertRaises(AttachmentCapacityExceededError):
                self.store.stage(other_request, "att-2", b"456")

    def test_discard_is_idempotent_and_never_follows_link(self):
        self.store.stage(self.request_id, "att-1", b"data")
        request_root = self.root / "workspace" / self.request_id
        self.store.discard(self.request_id)
        self.assertFalse(request_root.exists())
        self.store.discard(self.request_id)

        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        request_root.parent.mkdir(parents=True, exist_ok=True)
        if not make_directory_link(outside, request_root):
            self.skipTest("host cannot create directory links")
        with self.assertRaises(AttachmentStagingError):
            self.store.discard(self.request_id)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_materialize_verifies_digest_mime_and_recovers_final(self):
        blob = self.store.stage(self.request_id, "att-7", PNG)
        manifest = self.manifest()
        paths = self.store.materialize(self.request_id, (manifest,))
        final = paths["att-7"]
        self.assertEqual(final.name, "att-7.png")
        self.assertEqual(final.read_bytes(), PNG)
        self.assertFalse(blob.exists())

        recovered = self.store.materialize(self.request_id, (manifest,))
        self.assertEqual(recovered["att-7"], final)

        self.store.stage(self.request_id, "att-7", JPEG)
        jpeg_manifest = self.manifest(
            JPEG,
            mime_type="image/jpeg",
            display_name="photo.jpg",
        )
        replaced = self.store.materialize(self.request_id, (jpeg_manifest,))["att-7"]
        self.assertEqual(replaced.name, "att-7.jpg")
        self.assertEqual(replaced.read_bytes(), JPEG)
        self.assertFalse(final.exists())

    def test_materialize_phase_one_non_image_types_without_signature_check(self):
        cases = (
            ("text/plain", ".txt", b"plain text"),
            ("audio/wav", ".wav", b"wav fixture bytes"),
            ("audio/webm", ".webm", b"webm fixture bytes"),
        )
        for index, (mime_type, suffix, data) in enumerate(cases, start=1):
            artifact_id = f"att-{index}"
            self.store.stage(self.request_id, artifact_id, data)
            manifest = self.manifest(
                data,
                artifact_id=artifact_id,
                display_name=f"fixture{suffix}",
                mime_type=mime_type,
            )
            with self.subTest(mime_type=mime_type):
                path = self.store.materialize(self.request_id, (manifest,))[artifact_id]
                self.assertEqual(path.suffix, suffix)
                self.assertEqual(path.read_bytes(), data)

    def test_webp_signature_is_checked_explicitly(self):
        self.store.stage(self.request_id, "att-7", WEBP)
        valid = self.manifest(WEBP, mime_type="image/webp")
        self.assertEqual(
            self.store.materialize(self.request_id, (valid,))["att-7"].suffix,
            ".webp",
        )

        wrong = b"not-webp-data"
        self.store.stage(self.request_id, "att-7", wrong)
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.materialize(
                self.request_id,
                (self.manifest(wrong, mime_type="image/webp"),),
            )
        self.assertEqual(caught.exception.code, "attachment_mime_mismatch")

    def test_non_image_digest_mismatch_still_fails(self):
        data = b"plain text"
        self.store.stage(self.request_id, "att-7", data)
        manifest = self.manifest(
            data,
            mime_type="text/plain",
            sha256="0" * 64,
        )
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.materialize(self.request_id, (manifest,))
        self.assertEqual(caught.exception.code, "attachment_digest_mismatch")

    def test_stage_enforces_the_single_file_cap_before_writing(self) -> None:
        from exocore_runtime.contracts import MAX_ATTACHMENT_BYTES

        with self.assertRaises(AttachmentSizeExceededError):
            self.store.stage(
                self.request_id,
                "att-7",
                b"x" * (MAX_ATTACHMENT_BYTES + 1),
            )
        request_root = self.root / "workspace" / self.request_id
        self.assertFalse(request_root.exists())

    def test_stage_enforces_the_real_total_cap_without_writing_the_rejected_file(self) -> None:
        chunk = b"x" * (17 * 1024 * 1024)
        self.store.stage(self.request_id, "att-1", chunk)
        self.store.stage(self.request_id, "att-2", chunk)
        with self.assertRaises(AttachmentCapacityExceededError):
            self.store.stage(self.request_id, "att-3", chunk)
        attachment_dir = self.root / "workspace" / self.request_id / "attachments"
        self.assertEqual(
            sorted(path.name for path in attachment_dir.iterdir()),
            ["att-1.blob", "att-2.blob"],
        )

    def test_materialize_rejects_size_mismatch_as_digest_truth(self) -> None:
        self.store.stage(self.request_id, "att-7", PNG)
        with self.assertRaises(ProviderAdapterError) as caught:
            self.store.materialize(
                self.request_id,
                (self.manifest(size=len(PNG) + 1),),
            )
        self.assertEqual(caught.exception.code, "attachment_digest_mismatch")

    def test_materialize_removes_stale_non_image_sibling(self) -> None:
        text = b"plain text"
        self.store.stage(self.request_id, "att-7", text)
        old_path = self.store.materialize(
            self.request_id,
            (self.manifest(text, mime_type="text/plain"),),
        )["att-7"]
        self.assertEqual(old_path.suffix, ".txt")

        self.store.stage(self.request_id, "att-7", PNG)
        new_path = self.store.materialize(
            self.request_id,
            (self.manifest(PNG, mime_type="image/png"),),
        )["att-7"]
        self.assertFalse(old_path.exists())
        self.assertTrue(new_path.exists())

    def test_materialize_cleans_stale_finals_only_for_the_same_artifact(self) -> None:
        self.store.stage(self.request_id, "att-7", PNG)
        paths = self.store.materialize(self.request_id, (self.manifest(),))
        attachment_dir = paths["att-7"].parent
        stale_png = attachment_dir / "att-7.png"
        final_jpg = attachment_dir / "att-7.jpg"
        final_jpg.write_bytes(JPEG)
        self.assertTrue(stale_png.exists())

        recovered = self.store.materialize(
            self.request_id,
            (self.manifest(JPEG, mime_type="image/jpeg"),),
        )
        self.assertEqual(recovered["att-7"], final_jpg)
        self.assertFalse(stale_png.exists())
        sibling = attachment_dir / "att-9.png"
        sibling.write_bytes(PNG)
        self.store.materialize(
            self.request_id,
            (self.manifest(JPEG, mime_type="image/jpeg"),),
        )
        self.assertTrue(sibling.exists())

    def test_display_name_is_metadata_and_never_a_path(self) -> None:
        manifest = self.manifest(display_name='../../evil"name.png')
        self.store.stage(self.request_id, "att-7", PNG)
        paths = self.store.materialize(self.request_id, (manifest,))
        resolved = paths["att-7"]
        self.assertEqual(resolved.name, "att-7.png")
        self.assertEqual(
            resolved.parent,
            self.root / "workspace" / self.request_id / "attachments",
        )
        self.assertFalse((self.root / "evil\"name.png").exists())

    def test_materialize_failures_are_typed(self):
        with self.assertRaises(ProviderAdapterError) as missing:
            self.store.materialize(self.request_id, (self.manifest(),))
        self.assertEqual(missing.exception.code, "attachment_not_staged")

        self.store.stage(self.request_id, "att-7", PNG)
        with self.assertRaises(ProviderAdapterError) as digest:
            self.store.materialize(
                self.request_id,
                (self.manifest(sha256="0" * 64),),
            )
        self.assertEqual(digest.exception.code, "attachment_digest_mismatch")

        wrong_magic = b"not-a-png"
        self.store.stage(self.request_id, "att-7", wrong_magic)
        with self.assertRaises(ProviderAdapterError) as mime:
            self.store.materialize(
                self.request_id,
                (self.manifest(wrong_magic),),
            )
        self.assertEqual(mime.exception.code, "attachment_mime_mismatch")

    def test_invalid_identity_and_unsafe_workspace_fail_closed(self):
        with self.assertRaises(ValueError):
            self.store.stage(self.request_id, "../escape", b"data")
        with self.assertRaises(ValueError):
            self.store.stage("../request", "att-1", b"data")

        workspace = self.root / "workspace"
        workspace.rmdir()
        workspace.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(AttachmentStagingError):
            self.store.stage(self.request_id, "att-1", b"data")
