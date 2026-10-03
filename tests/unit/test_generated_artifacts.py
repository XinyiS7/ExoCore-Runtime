"""Deterministic construction tests; no provider calls or private files."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from exocore_runtime.contracts import RuntimeEvent
from exocore_runtime.providers.antigravity.generated_artifacts import (
    ArtifactIngestionError,
    GeneratedArtifactStore,
    MAX_GENERATED_OUTPUT_BYTES,
    capture_step_payload,
    is_deferrable_capture_failure,
    parse_generated_image_output,
)


class GeneratedImageOutputTests(unittest.TestCase):
    def test_observed_step_output_preserves_path_and_removes_sentence_period(self):
        path = r"C:\managed\profile\brain\session\warm_yellow_sun.jpg"
        output = (
            "Using prompt: A simple warm yellow sun.\n\n"
            f"Generated image is saved at {path}.\n\n"
            " Do not output the path of this image to show to the user."
        )
        result = parse_generated_image_output(output)
        self.assertEqual(result.path, path)
        self.assertEqual(result.suffix, ".jpg")

    def test_spaces_unicode_and_uppercase_suffix_are_preserved(self):
        path = r"C:\managed\暖阳 artwork.JPG"
        result = parse_generated_image_output(f"Generated image is saved at {path}.")
        self.assertEqual(result.path, path)
        self.assertEqual(result.suffix, ".jpg")

    def test_path_without_sentence_delimiter(self):
        result = parse_generated_image_output("Generated image is saved at /managed/image.png")
        self.assertEqual(result.path, "/managed/image.png")

    def test_non_results_and_ambiguous_results_are_rejected(self):
        for output in (
            None, {}, "", "Please see C:\\managed\\image.jpg",
            "Generated image is saved at /managed/a.jpg.\n"
            "Generated image is saved at /managed/b.jpg.",
        ):
            with self.subTest(output=output):
                with self.assertRaises(ArtifactIngestionError):
                    parse_generated_image_output(output)

    def test_unsafe_locator_shapes_are_rejected(self):
        for path in (
            "../image.jpg", "relative/image.jpg", "/managed/../image.jpg",
            "https://example.invalid/image.jpg", "file:///managed/image.jpg",
            r"\\server\share\image.jpg", r"\\?\C:\managed\image.jpg",
            r"C:\managed\image.jpg:stream.jpg", "C:image.jpg",
            "/managed/image.svg", "/managed/image.md", "/managed/a\x00.jpg",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ArtifactIngestionError) as caught:
                    parse_generated_image_output(f"Generated image is saved at {path}.")
                self.assertNotIn(path, str(caught.exception))

    def test_output_budget_is_enforced_in_bytes(self):
        with self.assertRaises(ArtifactIngestionError) as caught:
            parse_generated_image_output("暖" * (MAX_GENERATED_OUTPUT_BYTES // 3 + 1))
        self.assertEqual(caught.exception.code, "artifact_output_too_large")

    def test_invalid_unicode_is_a_safe_bridge_error(self):
        with self.assertRaises(ArtifactIngestionError) as caught:
            parse_generated_image_output("\ud800")
        self.assertEqual(caught.exception.code, "artifact_output_invalid")


class GeneratedArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "generation"
        self.root.mkdir()
        self.source = self.root / "sun.jpg"
        self.data = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-signature-test"
        self.source.write_bytes(self.data)
        self.request = str(uuid4())
        self.store = GeneratedArtifactStore(self.root)

    def output(self, path=None):
        return f"Generated image is saved at {path or self.source}."

    def test_capture_persists_opaque_snapshot_and_survives_source_removal(self):
        descriptor = self.store.capture(self.request, 13, self.output())
        self.assertEqual(descriptor["request_id"], self.request)
        self.assertEqual(descriptor["step_index"], 13)
        self.assertEqual(descriptor["mime_type"], "image/jpeg")
        self.assertNotIn(str(self.source), json.dumps(descriptor))
        self.source.unlink()
        reopened = GeneratedArtifactStore(self.root)
        recovered, data = reopened.read(descriptor["artifact_ref"])
        self.assertEqual(recovered, descriptor)
        self.assertEqual(data, self.data)
        self.assertEqual(reopened.capture(self.request, 13, self.output()), descriptor)
        self.assertEqual(len(list(self.store.directory.glob("*.json"))), 1)

    def test_same_step_in_different_requests_has_different_identity(self):
        first = self.store.capture(self.request, 13, self.output())
        second = self.store.capture(str(uuid4()), 13, self.output())
        self.assertNotEqual(first["artifact_ref"], second["artifact_ref"])

    def test_outside_generation_is_rejected_without_snapshot(self):
        outside = Path(self.temporary.name) / "private.jpg"
        outside.write_bytes(self.data)
        with self.assertRaises(ArtifactIngestionError) as caught:
            self.store.capture(self.request, 13, self.output(outside))
        self.assertEqual(caught.exception.code, "artifact_path_outside_generation")
        self.assertEqual(list(self.store.directory.glob("*.blob")), [])

    def test_opened_handle_outside_generation_is_rejected(self):
        # Simulate a pathname substitution between scope check and open.
        outside = Path(self.temporary.name) / "private.jpg"
        outside.write_bytes(self.data)
        with patch(
            "exocore_runtime.providers.antigravity.generated_artifacts._opened_path",
            return_value=outside,
        ):
            with self.assertRaises(ArtifactIngestionError) as caught:
                self.store.capture(self.request, 13, self.output())
        self.assertEqual(caught.exception.code, "artifact_path_outside_generation")

    def test_mime_mismatch_and_empty_file_are_bridge_errors(self):
        for data, code in ((b"not an image", "artifact_mime_mismatch"), (b"", "artifact_empty")):
            with self.subTest(code=code):
                self.source.write_bytes(data)
                with self.assertRaises(ArtifactIngestionError) as caught:
                    self.store.capture(self.request, 13, self.output())
                self.assertEqual(caught.exception.code, code)

    def test_per_file_limit_rejects_before_registering(self):
        with patch(
            "exocore_runtime.providers.antigravity.generated_artifacts.MAX_GENERATED_IMAGE_BYTES",
            len(self.data) - 1,
        ):
            with self.assertRaises(ArtifactIngestionError) as caught:
                self.store.capture(self.request, 13, self.output())
        self.assertEqual(caught.exception.code, "artifact_size_exceeded")
        self.assertEqual(list(self.store.directory.glob("*.json")), [])

    def test_request_count_budget_and_other_request_are_independent(self):
        for step in range(5):
            self.store.capture(self.request, step, self.output())
        with self.assertRaises(ArtifactIngestionError) as caught:
            self.store.capture(self.request, 5, self.output())
        self.assertEqual(caught.exception.code, "artifact_capacity_exceeded")
        self.store.capture(str(uuid4()), 5, self.output())

    def test_total_budget_includes_previous_persisted_snapshots(self):
        self.store.capture(self.request, 0, self.output())
        with patch(
            "exocore_runtime.providers.antigravity.generated_artifacts.MAX_GENERATED_IMAGE_TOTAL_BYTES",
            len(self.data) * 2 - 1,
        ):
            with self.assertRaises(ArtifactIngestionError) as caught:
                GeneratedArtifactStore(self.root).capture(self.request, 1, self.output())
        self.assertEqual(caught.exception.code, "artifact_capacity_exceeded")

    def test_modified_snapshot_is_not_served(self):
        descriptor = self.store.capture(self.request, 13, self.output())
        snapshot = self.store.directory / f"{descriptor['artifact_ref']}.blob"
        snapshot.write_bytes(b"x" * len(self.data))
        with self.assertRaises(ArtifactIngestionError) as caught:
            self.store.read(descriptor["artifact_ref"])
        self.assertEqual(caught.exception.code, "artifact_integrity_mismatch")

    def test_unknown_and_pathlike_references_never_read_other_files(self):
        for reference in ("../sun.jpg", "A" * 32, "0" * 32):
            with self.subTest(reference=reference):
                with self.assertRaises(ArtifactIngestionError):
                    self.store.read(reference)

    def test_retirement_does_not_recreate_generation(self):
        import shutil

        shutil.rmtree(self.root)
        with self.assertRaises(ArtifactIngestionError) as caught:
            self.store.capture(self.request, 13, self.output())
        self.assertEqual(caught.exception.code, "artifact_capture_failed")
        self.assertFalse(self.root.exists())


class CaptureStepPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "generation"
        self.root.mkdir()
        self.source = self.root / "sun.jpg"
        self.data = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-signature-test"
        self.source.write_bytes(self.data)
        self.request = str(uuid4())

    def step(self, **updates):
        step = {
            "step_type": "tool",
            "state": "DONE",
            "step_index": 13,
            "tool_name": "generate_image",
            "tool_info": {
                "name": "generate_image",
                "output": f"Generated image is saved at {self.source}.",
            },
        }
        step.update(updates)
        return step

    def test_non_results_are_ignored_by_the_capture(self):
        for step in (
            None,
            "not-a-step",
            self.step(state="ACTIVE"),
            self.step(state="ERROR"),
            self.step(
                tool_name="view_file",
                tool_info={"name": "view_file", "output": "plain tool output"},
            ),
        ):
            with self.subTest(step=step):
                self.assertIsNone(capture_step_payload(self.root, self.request, step))

    def test_ready_payload_is_exact_and_carries_no_path(self):
        payload = capture_step_payload(self.root, self.request, self.step())
        self.assertEqual(
            set(payload),
            {
                "outcome",
                "artifact_ref",
                "step_index",
                "index",
                "kind",
                "display_name",
                "mime_type",
                "size",
                "sha256",
            },
        )
        self.assertEqual(payload["outcome"], "ready")
        self.assertEqual(payload["step_index"], 13)
        self.assertEqual(payload["mime_type"], "image/jpeg")
        self.assertEqual(payload["size"], len(self.data))
        serialized = json.dumps(payload)
        self.assertNotIn("sun.jpg", serialized)
        self.assertNotIn(self.temporary.name, serialized)
        RuntimeEvent(
            binding_id=uuid4(),
            request_id=uuid4(),
            sequence=1,
            event_type="artifact",
            payload=payload,
        )

    def test_missing_output_and_unsafe_paths_fail_closed(self):
        cases = (
            (
                self.step(tool_info={"name": "generate_image"}),
                "artifact_output_missing",
            ),
            (
                self.step(
                    tool_info={
                        "name": "generate_image",
                        "output": "no saved-at line",
                    }
                ),
                "artifact_output_invalid",
            ),
            (
                self.step(
                    tool_info={
                        "name": "generate_image",
                        "output": "Generated image is saved at C:\\outside\\image.jpg.",
                    }
                ),
                "artifact_path_outside_generation",
            ),
        )
        for step, code in cases:
            with self.subTest(code=code):
                payload = capture_step_payload(self.root, self.request, step)
                self.assertEqual(
                    payload,
                    {"outcome": "failed", "step_index": 13, "error_code": code},
                )

    def test_unexpected_failure_becomes_a_bounded_failed_payload(self):
        with patch.object(
            GeneratedArtifactStore, "capture", side_effect=RuntimeError("boom")
        ):
            payload = capture_step_payload(self.root, self.request, self.step())
        self.assertEqual(
            payload,
            {
                "outcome": "failed",
                "step_index": 13,
                "error_code": "artifact_capture_failed",
            },
        )

    def test_replay_of_the_same_step_reuses_one_snapshot(self):
        first = capture_step_payload(self.root, self.request, self.step())
        second = capture_step_payload(self.root, self.request, self.step())
        self.assertEqual(first, second)
        self.assertEqual(
            len(list((self.root / "generated_artifacts").glob("*.json"))), 1
        )


class ArtifactEventPayloadTests(unittest.TestCase):
    def event(self, payload):
        return RuntimeEvent(
            binding_id=uuid4(),
            request_id=uuid4(),
            sequence=1,
            event_type="artifact",
            payload=payload,
        )

    def ready(self):
        return {
            "outcome": "ready",
            "artifact_ref": "0" * 32,
            "step_index": 3,
            "index": 0,
            "kind": "image",
            "display_name": "image-3.png",
            "mime_type": "image/png",
            "size": 4,
            "sha256": "1" * 64,
        }

    def test_ready_and_failed_shapes_are_accepted(self):
        self.assertEqual(self.event(self.ready()).payload["outcome"], "ready")
        failed = {
            "outcome": "failed",
            "step_index": 3,
            "error_code": "artifact_capture_failed",
        }
        self.assertEqual(self.event(failed).payload["outcome"], "failed")

    def test_mutations_are_rejected(self):
        mutations = (
            {"outcome": "result"},
            {"artifact_ref": "Z" * 32},
            {"artifact_ref": "0" * 31},
            {"index": 1},
            {"kind": "file"},
            {"display_name": "../image.png"},
            {"display_name": "image-3.svg"},
            {"mime_type": "image/svg+xml"},
            {"size": 0},
            {"sha256": "1" * 63},
            {"path": "C:\\outside\\image.png"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValidationError):
                    self.event({**self.ready(), **mutation})
        for payload in (
            {"outcome": "failed", "step_index": -1, "error_code": "bad"},
            {"outcome": "failed", "step_index": 3, "error_code": "BadCode"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.event(payload)

class StepOutputFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "generation"
        self.root.mkdir()
        self.session = "7b8467dc-aa7c-4491-a2b7-9973cb326d99"
        self.brain = (
            self.root
            / "profile"
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / self.session
        )
        self.image = self.brain / "sun.jpg"
        self.image.parent.mkdir(parents=True)
        self.data = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-signature-test"
        self.image.write_bytes(self.data)
        self.request = str(uuid4())

    def step(self, **updates):
        step = {
            "step_type": "tool",
            "state": "DONE",
            "step_index": 3,
            "tool_name": "generate_image",
            "tool_info": {"name": "generate_image"},
        }
        step.update(updates)
        return step

    def write_step_output(self, text=None):
        if text is None:
            text = (
                "Using prompt: fixture\n\n"
                f"Generated image is saved at {self.image}.\n\n"
                " Do not output the path of this image to show to the user."
            )
        path = self.brain / ".system_generated" / "steps" / "3" / "output.txt"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")

    def test_missing_stream_output_reads_the_step_output_file(self):
        self.write_step_output()
        payload = capture_step_payload(
            self.root, self.request, self.step(), self.session
        )
        self.assertEqual(payload["outcome"], "ready")
        self.assertEqual(payload["mime_type"], "image/jpeg")
        self.assertEqual(payload["size"], len(self.data))

    def test_stream_and_file_absent_stays_a_typed_failure(self):
        payload = capture_step_payload(
            self.root, self.request, self.step(), self.session
        )
        self.assertEqual(
            payload,
            {
                "outcome": "failed",
                "step_index": 3,
                "error_code": "artifact_output_missing",
            },
        )

    def test_invalid_stream_output_falls_back_to_the_step_file(self):
        self.write_step_output()
        payload = capture_step_payload(
            self.root,
            self.request,
            self.step(tool_info={"name": "generate_image", "output": "garbage"}),
            self.session,
        )
        self.assertEqual(payload["outcome"], "ready")
        self.assertEqual(payload["size"], len(self.data))

    def test_unsafe_session_identifier_is_never_used_as_a_path(self):
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text(
            f"Generated image is saved at {self.image}.\n", encoding="utf-8"
        )
        for session in ("../../outside", "a/b", "..", "", None, 42):
            with self.subTest(session=session):
                payload = capture_step_payload(
                    self.root, self.request, self.step(), session
                )
                self.assertEqual(payload["outcome"], "failed")
                self.assertEqual(payload["error_code"], "artifact_output_missing")

    def test_deferrable_policy_only_covers_output_availability(self):
        self.assertTrue(
            is_deferrable_capture_failure(
                {
                    "outcome": "failed",
                    "step_index": 3,
                    "error_code": "artifact_output_missing",
                }
            )
        )
        self.assertTrue(
            is_deferrable_capture_failure(
                {
                    "outcome": "failed",
                    "step_index": 3,
                    "error_code": "artifact_output_invalid",
                }
            )
        )
        self.assertFalse(
            is_deferrable_capture_failure(
                {
                    "outcome": "failed",
                    "step_index": 3,
                    "error_code": "artifact_capture_failed",
                }
            )
        )
        self.assertFalse(
            is_deferrable_capture_failure(
                {"outcome": "ready", "artifact_ref": "0" * 32}
            )
        )
        self.assertFalse(is_deferrable_capture_failure(None))
