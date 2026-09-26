from __future__ import annotations

import unittest
from uuid import uuid4

from pydantic import ValidationError

from exocore_runtime.contracts import (
    AttachmentManifest,
    RUNTIME_CAPABILITIES,
    TurnRequest,
    canonical_turn_request_hash,
)


class AttachmentContractTests(unittest.TestCase):
    def manifest(self, **overrides):
        values = {
            "artifact_id": "att-7",
            "display_name": "photo.png",
            "mime_type": "image/png",
            "size": 8,
            "sha256": "a" * 64,
        }
        values.update(overrides)
        return AttachmentManifest(**values)

    def request(self, **overrides):
        values = {
            "request_id": uuid4(),
            "user_message": "look",
            "requested_model_id": "gemini-3.1-pro-preview",
            "requested_thinking_level": "auto",
        }
        values.update(overrides)
        return TurnRequest(**values)

    def test_manifest_is_strict_frozen_and_bounded(self):
        manifest = self.manifest()
        self.assertEqual(manifest.artifact_id, "att-7")
        invalid = (
            {"artifact_id": "att-0"},
            {"artifact_id": "../att-7"},
            {"display_name": ""},
            {"display_name": "x" * 256},
            {"mime_type": "image/gif"},
            {"size": 0},
            {"size": 20 * 1024 * 1024 + 1},
            {"sha256": "A" * 64},
            {"unknown": "field"},
        )
        payload = manifest.model_dump(mode="json")
        for mutation in invalid:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValidationError):
                    AttachmentManifest.model_validate({**payload, **mutation})
        with self.assertRaises(ValidationError):
            self.manifest(size=True)
        with self.assertRaises(ValidationError):
            AttachmentManifest.model_validate_json(
                '{"artifact_id":"att-7","display_name":"p.png",'
                '"mime_type":"image/png","size":8,"sha256":"'
                + "a" * 64
                + '","extra":1}'
            )

    def test_attachment_order_and_every_manifest_field_enter_request_hash(self):
        first = self.manifest(artifact_id="att-7", sha256="a" * 64)
        second = self.manifest(artifact_id="att-8", sha256="b" * 64)
        request_id = uuid4()
        base = self.request(
            request_id=request_id,
            attachments=(first, second),
        )
        reversed_order = self.request(
            request_id=request_id,
            attachments=(second, first),
        )
        self.assertEqual(base.attachments, (first, second))
        self.assertNotEqual(
            canonical_turn_request_hash(base),
            canonical_turn_request_hash(reversed_order),
        )
        for field, changed in (
            ("artifact_id", "att-9"),
            ("display_name", "other.png"),
            ("mime_type", "image/jpeg"),
            ("size", 9),
            ("sha256", "c" * 64),
        ):
            mutated = self.manifest(**{field: changed})
            candidate = self.request(
                request_id=request_id,
                attachments=(mutated, second),
            )
            with self.subTest(field=field):
                self.assertNotEqual(
                    canonical_turn_request_hash(base),
                    canonical_turn_request_hash(candidate),
                )

    def test_empty_message_requires_attachments_and_caps_are_exact(self):
        attachment = self.manifest()
        self.assertEqual(
            self.request(user_message="", attachments=(attachment,)).user_message,
            "",
        )
        with self.assertRaises(ValidationError):
            self.request(user_message="", attachments=())
        with self.assertRaises(ValidationError):
            self.request(attachments=tuple(
                self.manifest(artifact_id=f"att-{index}")
                for index in range(1, 7)
            ))
        with self.assertRaises(ValidationError):
            self.request(attachments=(
                self.manifest(artifact_id="att-1", size=20 * 1024 * 1024),
                self.manifest(artifact_id="att-2", size=20 * 1024 * 1024),
                self.manifest(artifact_id="att-3", size=20 * 1024 * 1024),
            ))
        with self.assertRaises(ValidationError):
            self.request(attachments=(attachment, attachment))

    def test_capability_revision_includes_turn_attachments(self):
        self.assertEqual(RUNTIME_CAPABILITIES[-1], "turn_attachments")
        self.assertEqual(RUNTIME_CAPABILITIES.count("turn_attachments"), 1)

    def test_manifest_is_identity_metadata_only(self):
        self.assertEqual(
            set(AttachmentManifest.model_fields),
            {"artifact_id", "display_name", "mime_type", "size", "sha256"},
        )
        for forbidden in ("path", "storage_path", "workspace", "content"):
            with self.subTest(field=forbidden):
                with self.assertRaises(ValidationError):
                    AttachmentManifest.model_validate(
                        {**self.manifest().model_dump(mode="json"), forbidden: "x"}
                    )
