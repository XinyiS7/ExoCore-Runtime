"""Generic canonical backing for workspace-reserved control artifacts.

The generation root owns a ``control/`` directory beside ``workspace/``. It
stores the authoritative body of every reserved control artifact; the
workspace copy is a materialized projection that is verified and repaired
before each turn. Ordinary workspace files are never traversed or removed by
this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import stat
from uuid import uuid4

from exocore_runtime.errors import ProviderAdapterError


@dataclass(frozen=True)
class ReservedControlArtifact:
    """One canonical body plus its workspace-relative materialized path."""

    canonical_name: str
    workspace_relative_path: str


# Registrations arrive with their owning seam: CP3 adds the project-rules
# artifact, CP5 adds the MCP config artifact. The default is intentionally
# empty so this primitive ships without inventing a production artifact.
RESERVED_CONTROL_ARTIFACTS: tuple[ReservedControlArtifact, ...] = ()


class CanonicalControlStore:
    """Generation-private canonical backing store with idempotent healing."""

    CONTROL_DIRECTORY_NAME = "control"

    def __init__(self, generation_root: Path) -> None:
        self.generation_root = Path(generation_root)
        self.control_dir = self.generation_root / self.CONTROL_DIRECTORY_NAME
        self.workspace_dir = self.generation_root / "workspace"

    def prepare(self) -> None:
        """Ensure the canonical backing directory exists as a real directory."""

        self._validate_control_backing()
        self._ensure_real_directory(self.control_dir)

    def _validate_control_backing(self) -> None:
        """Revalidate the backing chain before any canonical access.

        A canonical body is only authoritative while ``generation_root`` and
        its ``control/`` directory are real directories owned by this
        generation. Replacing either one with a junction or symlink redirects
        every canonical read and write to an outside directory, so each access
        re-checks the chain instead of trusting the state seen at
        ``prepare()`` time. A missing control directory is reported as missing
        by the caller; a redirected one is always invalid.
        """

        if self._is_link_or_reparse(self.generation_root) or not self.generation_root.is_dir():
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True)
        if self._is_link_or_reparse(self.control_dir):
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True)
        if self.control_dir.exists() and not self.control_dir.is_dir():
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True)

    def write_canonical(self, canonical_name: str, content: str) -> None:
        """Atomically store one authoritative body inside the control directory."""

        self._validate_canonical_name(canonical_name)
        self.prepare()
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True) from exc
        self._atomic_write(
            self.control_dir / canonical_name,
            encoded,
            error_code="agy_control_backing_invalid",
        )

    def read_canonical(self, canonical_name: str) -> str:
        """Strictly read one authoritative body; fail closed when unavailable."""

        self._validate_canonical_name(canonical_name)
        try:
            return self._read_canonical_bytes(canonical_name).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True) from exc

    def verify(self, artifact: ReservedControlArtifact) -> bool:
        """Report whether the materialized copy matches the canonical body."""

        canonical_bytes = self._read_canonical_bytes(artifact.canonical_name)
        target = self._target_path(artifact)
        if not self._parents_are_real_directories(artifact):
            return False
        if self._is_link_or_reparse(target) or not target.is_file():
            return False
        try:
            return target.read_bytes() == canonical_bytes
        except OSError:
            return False

    def restore(self, artifact: ReservedControlArtifact) -> bool:
        """Idempotently repair one materialized copy; returns True when repaired."""

        canonical_bytes = self._read_canonical_bytes(artifact.canonical_name)
        target = self._target_path(artifact)
        self._ensure_parent_directories(artifact)
        if self._is_link_or_reparse(target) or not target.is_file():
            self._remove_tampered_target(target)
        elif target.read_bytes() == canonical_bytes:
            return False
        self._atomic_write(target, canonical_bytes)
        if self._is_link_or_reparse(target) or not target.is_file():
            raise ProviderAdapterError("agy_control_artifact_invalid", fatal_generation=True)
        if target.read_bytes() != canonical_bytes:
            raise ProviderAdapterError("agy_control_artifact_invalid", fatal_generation=True)
        return True

    def _target_path(self, artifact: ReservedControlArtifact) -> Path:
        relative = artifact.workspace_relative_path
        if not isinstance(relative, str) or not relative:
            raise ProviderAdapterError("agy_control_artifact_invalid", fatal_generation=True)
        parts = Path(relative).parts
        invalid_part = any(
            part in {"", ".", ".."}
            or any(separator in part for separator in (os.sep, os.altsep or os.sep, ":"))
            for part in parts
        )
        if Path(relative).is_absolute() or invalid_part:
            raise ProviderAdapterError("agy_control_artifact_invalid", fatal_generation=True)
        if not self.workspace_dir.is_dir() or self._is_link_or_reparse(self.workspace_dir):
            raise ProviderAdapterError("agy_workspace_invalid", fatal_generation=True)
        return self.workspace_dir.joinpath(*parts)

    def _parents_are_real_directories(self, artifact: ReservedControlArtifact) -> bool:
        current = self.workspace_dir
        for part in Path(artifact.workspace_relative_path).parts[:-1]:
            current = current / part
            if (
                not current.is_dir()
                or self._is_link_or_reparse(current)
            ):
                return False
        return True

    def _ensure_parent_directories(self, artifact: ReservedControlArtifact) -> None:
        current = self.workspace_dir
        for part in Path(artifact.workspace_relative_path).parts[:-1]:
            current = current / part
            if current.exists() or current.is_symlink() or self._is_link_or_reparse(current):
                if self._is_link_or_reparse(current) or not current.is_dir():
                    raise ProviderAdapterError(
                        "agy_control_artifact_invalid", fatal_generation=True
                    )
                continue
            try:
                current.mkdir()
            except OSError as exc:
                raise ProviderAdapterError(
                    "agy_control_artifact_invalid", fatal_generation=True
                ) from exc

    def _read_canonical_bytes(self, canonical_name: str) -> bytes:
        self._validate_canonical_name(canonical_name)
        self._validate_control_backing()
        path = self.control_dir / canonical_name
        if self._is_link_or_reparse(path) or (path.exists() and not path.is_file()):
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ProviderAdapterError("agy_control_backing_missing", fatal_generation=True) from exc
        except OSError as exc:
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True) from exc

    @staticmethod
    def _validate_canonical_name(canonical_name: str) -> None:
        if (
            not isinstance(canonical_name, str)
            or not canonical_name
            or canonical_name in {".", ".."}
            or any(separator in canonical_name for separator in ("/", "\\", ":"))
        ):
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True)

    @staticmethod
    def _remove_tampered_target(target: Path) -> None:
        if target.is_symlink() or CanonicalControlStore._is_link_or_reparse(target):
            try:
                if target.is_dir():
                    os.rmdir(target)
                else:
                    target.unlink()
            except OSError as exc:
                raise ProviderAdapterError(
                    "agy_control_artifact_invalid", fatal_generation=True
                ) from exc
            return
        if target.is_dir():
            # A real directory at a reserved file path is ambiguous: removing it
            # could destroy content this module never owned. Fail closed instead.
            raise ProviderAdapterError("agy_control_artifact_invalid", fatal_generation=True)

    def _ensure_real_directory(self, path: Path) -> None:
        if self._is_link_or_reparse(path):
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True)
        if path.exists():
            if not path.is_dir():
                raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True)
            return
        try:
            path.mkdir(parents=True)
        except OSError as exc:
            raise ProviderAdapterError("agy_control_backing_invalid", fatal_generation=True) from exc

    @staticmethod
    def _atomic_write(
        path: Path,
        content: bytes,
        *,
        error_code: str = "agy_control_artifact_invalid",
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except OSError as exc:
            raise ProviderAdapterError(error_code, fatal_generation=True) from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        if path.is_symlink():
            return True
        try:
            attributes = path.lstat().st_file_attributes
        except (AttributeError, FileNotFoundError):
            return False
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
