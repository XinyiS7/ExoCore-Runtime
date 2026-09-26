"""Generation-private attachment staging, verification, and materialization."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
from uuid import UUID, uuid4

from exocore_runtime.contracts import (
    ATTACHMENT_ID_PATTERN,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_COUNT,
    MAX_ATTACHMENT_TOTAL_BYTES,
    AttachmentManifest,
)
from exocore_runtime.errors import (
    AttachmentCapacityExceededError,
    AttachmentSizeExceededError,
    AttachmentStagingError,
    ProviderAdapterError,
)


_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class AttachmentStore:
    """Synchronous filesystem operations called only under one artifact lock."""

    def __init__(self, generation_root: Path) -> None:
        self.generation_root = Path(generation_root)
        self.workspace = self.generation_root / "workspace"

    def stage(self, request_id: str, artifact_id: str, data: bytes) -> Path:
        self._validate_request_id(request_id)
        self._validate_artifact_id(artifact_id)
        if type(data) is not bytes:
            raise TypeError("attachment data must be bytes")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentSizeExceededError()
        if not data:
            raise AttachmentStagingError()
        try:
            attachment_dir = self._ensure_attachment_dir(request_id)
            target = attachment_dir / f"{artifact_id}.blob"
            entries = self._capacity_entries(attachment_dir)
            existing_size = target.stat().st_size if target in entries else 0
            projected_count = len(entries) + (0 if target in entries else 1)
            projected_size = sum(entries.values()) - existing_size + len(data)
            if (
                projected_count > MAX_ATTACHMENT_COUNT
                or projected_size > MAX_ATTACHMENT_TOTAL_BYTES
            ):
                raise AttachmentCapacityExceededError()
            self._atomic_write(target, data)
            return target
        except (AttachmentCapacityExceededError, AttachmentSizeExceededError):
            raise
        except (OSError, ValueError) as exc:
            raise AttachmentStagingError() from exc

    def discard(self, request_id: str) -> None:
        self._validate_request_id(request_id)
        request_root = self.workspace / request_id
        if not request_root.exists():
            return
        try:
            self._require_real_directory(self.generation_root)
            self._require_real_directory(self.workspace)
            self._require_real_directory(request_root)
            shutil.rmtree(request_root)
        except (OSError, ValueError) as exc:
            raise AttachmentStagingError() from exc

    def materialize(
        self,
        request_id: str,
        manifests: tuple[AttachmentManifest, ...],
    ) -> dict[str, Path]:
        self._validate_request_id(request_id)
        if not manifests:
            return {}
        attachment_dir = self._attachment_dir(request_id)
        try:
            self._require_real_directory(self.generation_root)
            self._require_real_directory(self.workspace)
            self._require_real_directory(self.workspace / request_id)
            self._require_real_directory(attachment_dir)
        except (OSError, ValueError) as exc:
            raise ProviderAdapterError("attachment_not_staged") from exc

        materialized: dict[str, Path] = {}
        for manifest in manifests:
            extension = _MIME_EXTENSIONS[manifest.mime_type]
            blob = attachment_dir / f"{manifest.artifact_id}.blob"
            final = attachment_dir / f"{manifest.artifact_id}{extension}"
            source = blob if blob.exists() else final
            if not source.exists():
                raise ProviderAdapterError("attachment_not_staged")
            self._require_regular_file(source)
            data = source.read_bytes()
            self._verify_bytes(data, manifest)
            if source == blob:
                self._atomic_replace(blob, final)
            self._remove_sibling_finals(attachment_dir, manifest.artifact_id, final)
            materialized[manifest.artifact_id] = final.resolve(strict=True)
        return materialized

    def prune(self, request_id: str) -> None:
        """Internal best-effort pre-send cleanup with the same path discipline."""

        self.discard(request_id)

    def _ensure_attachment_dir(self, request_id: str) -> Path:
        self._require_real_directory(self.generation_root)
        self._require_real_directory(self.workspace)
        request_root = self.workspace / request_id
        attachment_dir = request_root / "attachments"
        self._mkdir_real(request_root)
        self._mkdir_real(attachment_dir)
        return attachment_dir

    def _attachment_dir(self, request_id: str) -> Path:
        return self.workspace / request_id / "attachments"

    @staticmethod
    def _validate_request_id(request_id: str) -> None:
        try:
            parsed = UUID(request_id)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("invalid request id") from None
        if str(parsed) != request_id:
            raise ValueError("request id must be canonical")

    @staticmethod
    def _validate_artifact_id(artifact_id: str) -> None:
        import re

        if type(artifact_id) is not str or re.fullmatch(
            ATTACHMENT_ID_PATTERN,
            artifact_id,
        ) is None:
            raise ValueError("invalid artifact id")

    @classmethod
    def _mkdir_real(cls, path: Path) -> None:
        if path.exists():
            cls._require_real_directory(path)
            return
        path.mkdir()
        cls._require_real_directory(path)

    @classmethod
    def _capacity_entries(cls, directory: Path) -> dict[Path, int]:
        entries: dict[Path, int] = {}
        for entry in directory.iterdir():
            if entry.is_dir() or cls._is_link_or_reparse(entry):
                raise ValueError("attachment directory contains an unsafe entry")
            cls._require_regular_file(entry)
            entries[entry] = entry.stat().st_size
        return entries

    @classmethod
    def _require_real_directory(cls, path: Path) -> None:
        if not path.is_dir() or cls._is_link_or_reparse(path):
            raise ValueError("attachment path is not a real directory")

    @classmethod
    def _require_regular_file(cls, path: Path) -> None:
        if cls._is_link_or_reparse(path) or not path.is_file():
            raise ValueError("attachment path is not a regular file")

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            return True
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)

    @classmethod
    def _remove_sibling_finals(
        cls,
        attachment_dir: Path,
        artifact_id: str,
        keep: Path,
    ) -> None:
        """Remove only the controlled final variants, never a broad sweep."""

        for extension in _MIME_EXTENSIONS.values():
            candidate = attachment_dir / f"{artifact_id}{extension}"
            if candidate == keep or not candidate.exists():
                continue
            if cls._is_link_or_reparse(candidate) or not candidate.is_file():
                continue
            candidate.unlink()

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_replace(source: Path, target: Path) -> None:
        os.replace(source, target)

    @staticmethod
    def _verify_bytes(data: bytes, manifest: AttachmentManifest) -> None:
        if (
            len(data) != manifest.size
            or hashlib.sha256(data).hexdigest() != manifest.sha256
        ):
            raise ProviderAdapterError("attachment_digest_mismatch")
        if manifest.mime_type == "image/png":
            valid = data.startswith(b"\x89PNG\r\n\x1a\n")
        elif manifest.mime_type == "image/jpeg":
            valid = data.startswith(b"\xff\xd8\xff")
        else:
            valid = (
                len(data) >= 12
                and data.startswith(b"RIFF")
                and data[8:12] == b"WEBP"
            )
        if not valid:
            raise ProviderAdapterError("attachment_mime_mismatch")
