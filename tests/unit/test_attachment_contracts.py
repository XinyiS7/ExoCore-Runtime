from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
from uuid import uuid4

from pydantic import ValidationError

from exocore_runtime.contracts import (
    ATTACHMENT_EXTENSIONS,
    AttachmentManifest,
    RUNTIME_CAPABILITIES,
    RuntimeMcpTool,
    TurnRequest,
    canonical_turn_request_hash,
    runtime_mcp_manifest_sha256,
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
            "runtime_mcp_tools": (
                {"name": "memory_search", "eager": True, "max_call_seconds": None},
            ),
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

    def test_phase_one_mime_table_is_strict_and_fixture_pinned(self):
        for mime_type in ATTACHMENT_EXTENSIONS:
            with self.subTest(mime_type=mime_type):
                self.assertEqual(self.manifest(mime_type=mime_type).mime_type, mime_type)
        for invalid in (
            "audio/x-wav",
            "audio/webm;codecs=opus",
            "text/markdown",
            "video/webm",
            7,
            b"image/png",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.manifest(mime_type=invalid)

        fixture = (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "runtime_attachment_mime_table.json"
        )
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            "74cf4189edc0ebdace5cea1b2cd1810e5a24122014f2901ddbd71c6c882ef82a",
        )
        self.assertEqual(payload["extensions"], ATTACHMENT_EXTENSIONS)
        from exocore_runtime.providers.antigravity.attachments import (
            _SIGNATURE_CHECKERS,
        )

        self.assertEqual(
            payload["signature_checked"],
            sorted(_SIGNATURE_CHECKERS),
        )
        self.assertTrue(
            all(
                not mime.startswith("image/") or mime in _SIGNATURE_CHECKERS
                for mime in ATTACHMENT_EXTENSIONS
            )
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

    def test_capability_revision_includes_manifest_after_turn_attachments(self):
        self.assertEqual(
            RUNTIME_CAPABILITIES[-3:],
            (
                "turn_attachments",
                "runtime_mcp_tool_manifest",
                "generated_artifacts",
            ),
        )
        self.assertEqual(RUNTIME_CAPABILITIES.count("runtime_mcp_tool_manifest"), 1)
        self.assertEqual(RUNTIME_CAPABILITIES.count("generated_artifacts"), 1)

    def test_runtime_mcp_manifest_is_exact_strict_unique_and_bounded(self):
        tool = RuntimeMcpTool(
            name="send_voice_msg", eager=True, max_call_seconds=45
        )
        self.assertEqual(
            tuple(tool.model_dump(mode="json")),
            ("name", "eager", "max_call_seconds"),
        )
        for mutation in (
            {"name": "SendVoice"},
            {"name": "x" * 101},
            {"eager": 1},
            {"max_call_seconds": 0},
            {"description": "forbidden"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                RuntimeMcpTool.model_validate(
                    {**tool.model_dump(mode="json"), **mutation}
                )
        with self.assertRaises(ValidationError):
            self.request(runtime_mcp_tools=(tool, tool))
        with self.assertRaises(ValidationError):
            self.request(runtime_mcp_tools=())
        with self.assertRaises(ValidationError):
            self.request(
                runtime_mcp_tools=tuple(
                    RuntimeMcpTool(name=f"tool_{index}", eager=False)
                    for index in range(65)
                )
            )

    def test_runtime_mcp_manifest_order_and_fields_determine_digest(self):
        first = RuntimeMcpTool(name="memory_search", eager=False)
        second = RuntimeMcpTool(
            name="send_voice_msg", eager=True, max_call_seconds=45
        )
        manifest = (first, second)
        self.assertEqual(
            runtime_mcp_manifest_sha256(manifest),
            runtime_mcp_manifest_sha256(manifest),
        )
        self.assertNotEqual(
            runtime_mcp_manifest_sha256(manifest),
            runtime_mcp_manifest_sha256(tuple(reversed(manifest))),
        )
        self.assertNotEqual(
            runtime_mcp_manifest_sha256(manifest),
            runtime_mcp_manifest_sha256(
                (first, second.model_copy(update={"eager": False}))
            ),
        )
        request_id = uuid4()
        ordered = self.request(request_id=request_id, runtime_mcp_tools=manifest)
        reversed_request = self.request(
            request_id=request_id,
            runtime_mcp_tools=tuple(reversed(manifest)),
        )
        self.assertNotEqual(
            canonical_turn_request_hash(ordered),
            canonical_turn_request_hash(reversed_request),
        )

    def test_cross_repository_manifest_fixture_has_pinned_order_and_digest(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "runtime_mcp_manifest.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        manifest = tuple(RuntimeMcpTool.model_validate(item) for item in payload)
        self.assertEqual(
            tuple(tool.name for tool in manifest),
            (
                "register",
                "memory_plasmid",
                "chronicle",
                "memory_search",
                "private_log",
                "schedule_wakeup",
                "heartbeat_policy",
                "use_skill",
                "trace_self",
                "send_voice_msg",
                "wezterm_bridge",
                "add_to_collection",
            ),
        )
        self.assertEqual(len(manifest), 12)
        self.assertEqual(
            tuple(tool.name for tool in manifest if tool.eager),
            (
                "register",
                "memory_plasmid",
                "chronicle",
                "memory_search",
                "private_log",
                "send_voice_msg",
            ),
        )
        self.assertEqual(
            manifest[-2].model_dump(mode="json"),
            {"name": "wezterm_bridge", "eager": False, "max_call_seconds": 30},
        )
        self.assertEqual(
            manifest[-1].model_dump(mode="json"),
            {"name": "add_to_collection", "eager": False, "max_call_seconds": None},
        )
        self.assertEqual(
            runtime_mcp_manifest_sha256(manifest),
            "92341eddc9d814fb315d1122337a9ff8761fb31a784f797dc16a4f1a522a33d2",
        )

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
