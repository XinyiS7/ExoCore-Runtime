"""Provider artifact parsing, bounded reads, and immutable snapshot capture.

The caller supplies one request-correlated generate_image tool step, never
assistant prose; the result text comes from the streamed tool output when
present and otherwise from the generation-private step output file with the
same scope and size checks, and capture runs once at turn end when every
step output is complete. Capturing bytes, validating scope and publishing references
are separate operations. A provider tool error must never reach this parser as
successful output or trigger another generation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath, PurePosixPath
from uuid import UUID, uuid4, uuid5

from exocore_runtime.contracts import MAX_GENERATED_ARTIFACT_BYTES


MAX_GENERATED_IMAGE_BYTES = MAX_GENERATED_ARTIFACT_BYTES
MAX_GENERATED_IMAGE_COUNT = 5
MAX_GENERATED_IMAGE_TOTAL_BYTES = 50 * 1024 * 1024
MAX_GENERATED_OUTPUT_BYTES = 64 * 1024
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_SAVED_AT_PREFIX = "Generated image is saved at "
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_LOGGER = logging.getLogger(__name__)


class ArtifactIngestionError(Exception):
    """Safe bridge failure, distinct from provider generation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GeneratedImageLocator:
    """Private candidate only; never serialize its path to events or clients."""

    path: str
    suffix: str


def parse_generated_image_output(output: object) -> GeneratedImageLocator:
    """Parse the observed single-image step result without guessing paths.

    AGY appends one sentence-ending period after the absolute image path.
    Strip that one delimiter only, not arbitrary punctuation or whitespace
    inside the filename. Multiple saved-at records are ambiguous, not a
    supported multi-image manifest. Filesystem containment is enforced by the
    capture owner, not this syntactic parser.
    """
    if not isinstance(output, str):
        raise ArtifactIngestionError("artifact_output_missing")
    try:
        output_size = len(output.encode("utf-8"))
    except UnicodeEncodeError:
        raise ArtifactIngestionError("artifact_output_invalid") from None
    if output_size > MAX_GENERATED_OUTPUT_BYTES:
        raise ArtifactIngestionError("artifact_output_too_large")
    candidates = [
        line[len(_SAVED_AT_PREFIX):]
        for line in output.splitlines()
        if line.startswith(_SAVED_AT_PREFIX)
    ]
    if len(candidates) != 1:
        raise ArtifactIngestionError("artifact_output_invalid")
    candidate = candidates[0]
    if candidate.endswith("."):
        candidate = candidate[:-1]
    if not candidate or any(ord(char) < 32 for char in candidate):
        raise ArtifactIngestionError("artifact_output_invalid")
    # Reject URI/UNC/device locators. Runtime captures managed local files,
    # never remote shares or a URL supplied by the provider.
    if "://" in candidate or candidate.startswith(("\\\\", "//")):
        raise ArtifactIngestionError("artifact_path_invalid")
    windows = PureWindowsPath(candidate)
    posix = PurePosixPath(candidate)
    if windows.is_absolute():
        path = windows
        if ":" in candidate[2:]:
            raise ArtifactIngestionError("artifact_path_invalid")
    elif posix.is_absolute():
        path = posix
    else:
        raise ArtifactIngestionError("artifact_path_invalid")
    if ".." in path.parts or path.suffix.lower() not in _IMAGE_SUFFIXES:
        raise ArtifactIngestionError("artifact_path_invalid")
    return GeneratedImageLocator(candidate, path.suffix.lower())


def _require_unlinked_path(path: Path, root: Path) -> None:
    """Reject reparse points throughout the managed subtree, not just the leaf."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ArtifactIngestionError("artifact_path_outside_generation") from None
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ArtifactIngestionError("artifact_path_invalid")


def _opened_path(handle) -> Path:
    """Resolve the opened object, so a path swap cannot bypass containment."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        get_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
        get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_path.restype = wintypes.DWORD
        size = 32768
        buffer = ctypes.create_unicode_buffer(size)
        length = get_path(msvcrt.get_osfhandle(handle.fileno()), buffer, size, 0)
        if not length or length >= size:
            raise ArtifactIngestionError("artifact_path_invalid")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            raise ArtifactIngestionError("artifact_path_invalid")
        if value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    # Linux test/deployment support uses the opened descriptor, not a second
    # pathname lookup. Other platforms fail closed rather than weaken scope.
    descriptor = Path(f"/proc/self/fd/{handle.fileno()}")
    if not descriptor.exists():
        raise ArtifactIngestionError("artifact_path_verification_unavailable")
    return descriptor.resolve(strict=True)


def _read_scoped_bytes(path: Path, root: Path, maximum: int) -> bytes:
    root = root.absolute()
    path = path.absolute()
    _require_unlinked_path(path, root)
    with path.open("rb") as handle:
        opened = _opened_path(handle)
        _require_unlinked_path(opened, root)
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactIngestionError("artifact_path_invalid")
        if before.st_size <= 0:
            raise ArtifactIngestionError("artifact_empty")
        if before.st_size > maximum:
            raise ArtifactIngestionError("artifact_size_exceeded")
        data = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
        if len(data) > maximum:
            raise ArtifactIngestionError("artifact_size_exceeded")
        if (
            len(data) != before.st_size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ArtifactIngestionError("artifact_changed_during_capture")
        return data


def _image_mime(data: bytes, suffix: str) -> str:
    if suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if suffix == ".gif" and data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if suffix == ".webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ArtifactIngestionError("artifact_mime_mismatch")


class GeneratedArtifactStore:
    """Generation-private immutable snapshots, called under adapter artifact lock.

    The registry contains only sanitized metadata and opaque identities. It is
    not provider truth or a replacement for the durable event journal. Retire
    deletes it with the generation; ExoCore must acquire its own copy first.
    """

    def __init__(self, generation_root: Path) -> None:
        self.root = Path(generation_root).absolute()
        self.directory = self.root / "generated_artifacts"

    @staticmethod
    def artifact_ref(request_id: str, step_index: int) -> str:
        request = UUID(request_id)
        if str(request) != request_id or type(step_index) is not int or step_index < 0:
            raise ValueError("invalid artifact provenance")
        return uuid5(request, f"generate_image:{step_index}:0").hex

    def capture(self, request_id: str, step_index: int, output: object) -> dict:
        """One call/step yields one registered image; replay reuses its snapshot."""
        reference = self.artifact_ref(request_id, step_index)
        try:
            _require_unlinked_path(self.root, self.root)
            self.directory.mkdir(exist_ok=True)
            _require_unlinked_path(self.directory, self.root)
            manifest = self.directory / f"{reference}.json"
            if manifest.exists():
                descriptor = self._descriptor(reference)
                if descriptor["request_id"] != request_id or descriptor["step_index"] != step_index:
                    raise ArtifactIngestionError("artifact_identity_conflict")
                self.read(reference)
                return descriptor
            locator = parse_generated_image_output(output)
            source = Path(locator.path)
            # Provider outputs cannot nominate this store's own snapshots.
            if source.absolute().is_relative_to(self.directory):
                raise ArtifactIngestionError("artifact_path_invalid")
            data = _read_scoped_bytes(source, self.root, MAX_GENERATED_IMAGE_BYTES)
            mime = _image_mime(data, locator.suffix)
            entries = [self._descriptor(item.stem) for item in self.directory.glob("*.json")]
            owned = [item for item in entries if item["request_id"] == request_id]
            if len(owned) >= MAX_GENERATED_IMAGE_COUNT or (
                sum(item["size"] for item in owned) + len(data) > MAX_GENERATED_IMAGE_TOTAL_BYTES
            ):
                raise ArtifactIngestionError("artifact_capacity_exceeded")
            descriptor = {
                "artifact_ref": reference,
                "request_id": request_id,
                "step_index": step_index,
                "index": 0,
                "kind": "image",
                "display_name": f"image-{step_index}{locator.suffix}",
                "mime_type": mime,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            self._atomic_write(self.directory / f"{reference}.blob", data)
            self._atomic_write(manifest, json.dumps(descriptor, sort_keys=True).encode("utf-8"))
            return descriptor
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            raise ArtifactIngestionError("artifact_capture_failed") from None

    def read(self, reference: str) -> tuple[dict, bytes]:
        try:
            descriptor = self._descriptor(reference)
            data = _read_scoped_bytes(
                self.directory / f"{reference}.blob", self.root, MAX_GENERATED_IMAGE_BYTES
            )
            if len(data) != descriptor["size"] or hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
                raise ArtifactIngestionError("artifact_integrity_mismatch")
            return descriptor, data
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            raise ArtifactIngestionError("artifact_unavailable") from None

    def _descriptor(self, reference: str) -> dict:
        if len(reference) != 32 or any(char not in "0123456789abcdef" for char in reference):
            raise ArtifactIngestionError("artifact_reference_invalid")
        raw = _read_scoped_bytes(self.directory / f"{reference}.json", self.root, 4096)
        descriptor = json.loads(raw)
        expected = {
            "artifact_ref", "request_id", "step_index", "index", "kind",
            "display_name", "mime_type", "size", "sha256",
        }
        if not isinstance(descriptor, dict) or set(descriptor) != expected:
            raise ArtifactIngestionError("artifact_manifest_invalid")
        if (
            descriptor["artifact_ref"] != reference
            or self.artifact_ref(descriptor["request_id"], descriptor["step_index"]) != reference
            or descriptor["kind"] != "image"
            or type(descriptor["index"]) is not int or descriptor["index"] != 0
            or type(descriptor["size"]) is not int
            or not 0 < descriptor["size"] <= MAX_GENERATED_IMAGE_BYTES
        ):
            raise ArtifactIngestionError("artifact_manifest_invalid")
        suffix = Path(descriptor["display_name"]).suffix
        if (
            suffix not in _IMAGE_SUFFIXES
            or descriptor["display_name"] != f"image-{descriptor['step_index']}{suffix}"
            or descriptor["mime_type"] not in {"image/png", "image/jpeg", "image/gif", "image/webp"}
            or not isinstance(descriptor["sha256"], str)
            or len(descriptor["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in descriptor["sha256"])
        ):
            raise ArtifactIngestionError("artifact_manifest_invalid")
        return descriptor

    def _atomic_write(self, target: Path, content: bytes) -> None:
        _require_unlinked_path(self.directory, self.root)
        temporary = self.directory / f".{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def _read_step_output_text(
    generation_root: Path,
    provider_session_id: object,
    step_index: int,
) -> str | None:
    """Best-effort bounded read of one provider step-output file.

    The AGY CLI persists a native tool's output under the generation-private
    profile brain directory (``.system_generated/steps/<n>/output.txt``) even
    when the streamed DONE frame carries no ``tool_info.output``. Only this
    exact provider-managed path shape is read; anything unsafe, oversized or
    missing returns ``None`` and the caller keeps its typed failure.
    """

    if (
        type(provider_session_id) is not str
        or _SESSION_ID_PATTERN.fullmatch(provider_session_id) is None
    ):
        return None
    root = Path(generation_root)
    path = (
        root
        / "profile"
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / provider_session_id
        / ".system_generated"
        / "steps"
        / str(step_index)
        / "output.txt"
    )
    try:
        data = _read_scoped_bytes(path, root, MAX_GENERATED_OUTPUT_BYTES)
    except (ArtifactIngestionError, OSError, UnicodeError):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeError:
        return None


def capture_step_payload(
    generation_root: Path,
    request_id: str,
    step: object,
    provider_session_id: object = None,
) -> dict | None:
    """Capture one correlated AGY tool step into a bounded artifact payload.

    ``None`` means this step is not a finished ``generate_image`` result.
    A capture failure becomes a bounded ``failed`` payload: a bridge-side
    problem must never rewrite provider terminal truth, fail the turn, or
    (worst of all) rerun a generation.
    """

    if not isinstance(step, dict) or step.get("state") != "DONE":
        return None
    tool_info = step.get("tool_info")
    names = {step.get("tool_name")}
    if isinstance(tool_info, dict):
        names.add(tool_info.get("name"))
    if "generate_image" not in names:
        return None
    step_index = step.get("step_index")
    if type(step_index) is not int or step_index < 0:
        return None
    output = tool_info.get("output") if isinstance(tool_info, dict) else None
    candidates = []
    if isinstance(output, str) and output:
        candidates.append(output)
    fallback = _read_step_output_text(
        generation_root, provider_session_id, step_index
    )
    if fallback is not None:
        candidates.append(fallback)
    try:
        chosen = None
        last_error: ArtifactIngestionError | None = None
        for candidate in candidates:
            try:
                parse_generated_image_output(candidate)
                chosen = candidate
                break
            except ArtifactIngestionError as exc:
                last_error = exc
        if chosen is None:
            raise (
                last_error
                if last_error is not None
                else ArtifactIngestionError("artifact_output_missing")
            )
        descriptor = GeneratedArtifactStore(generation_root).capture(
            request_id, step_index, chosen
        )
    except ArtifactIngestionError as exc:
        return {"outcome": "failed", "step_index": step_index, "error_code": exc.code}
    except Exception:
        _LOGGER.warning("generated image capture failed unexpectedly", exc_info=True)
        return {
            "outcome": "failed",
            "step_index": step_index,
            "error_code": "artifact_capture_failed",
        }
    return {
        "outcome": "ready",
        "artifact_ref": descriptor["artifact_ref"],
        "step_index": descriptor["step_index"],
        "index": descriptor["index"],
        "kind": descriptor["kind"],
        "display_name": descriptor["display_name"],
        "mime_type": descriptor["mime_type"],
        "size": descriptor["size"],
        "sha256": descriptor["sha256"],
    }