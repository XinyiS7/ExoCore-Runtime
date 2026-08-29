"""Generation-private one-shot mailbox and AGY PreInvocation hook entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from exocore_runtime.errors import ProviderAdapterError


_IDENTITY_FILE = "identity.json"
_PENDING_FILE = "pending.json"
_CLAIMED_FILE = "claimed.json"
_RECEIPT_FILE = "receipt.json"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderAdapterError("ephemeral_mailbox_invalid") from exc
    if not isinstance(value, dict):
        raise ProviderAdapterError("ephemeral_mailbox_invalid")
    return value


def _payload_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unlink_if_present(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


class EphemeralMailbox:
    def __init__(
        self,
        root: Path,
        *,
        binding_id: str,
        generation_id: str,
        ttl_seconds: float,
    ) -> None:
        self.root = Path(root)
        self.binding_id = binding_id
        self.generation_id = generation_id
        self.ttl_seconds = ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_identity()

    @property
    def pending_path(self) -> Path:
        return self.root / _PENDING_FILE

    @property
    def receipt_path(self) -> Path:
        return self.root / _RECEIPT_FILE

    def prepare(self, request_id: str, ephemeral_current: str | None) -> str:
        payload = ephemeral_current or ""
        payload_hash = _payload_hash(payload)
        expected = {
            "schema_version": "v2",
            "binding_id": self.binding_id,
            "generation_id": self.generation_id,
            "request_id": request_id,
            "payload_sha256": payload_hash,
        }
        try:
            if self.pending_path.exists():
                current = _read_object(self.pending_path)
                if all(current.get(key) == value for key, value in expected.items()):
                    self.validate_pending(request_id, payload_hash)
                    return payload_hash
                raise ProviderAdapterError("ephemeral_pending_conflict")
            for stale_name in (_CLAIMED_FILE, _RECEIPT_FILE):
                stale = self.root / stale_name
                if stale.exists():
                    _unlink_if_present(stale)
            pending = {
                **expected,
                "created_at": time.time(),
                "ephemeral_current": payload,
            }
            _atomic_write(self.pending_path, pending)
            self.validate_pending(request_id, payload_hash)
            return payload_hash
        except (OSError, ProviderAdapterError) as original_error:
            try:
                self.cleanup_request()
            except OSError as cleanup_error:
                failure = ProviderAdapterError(
                    "ephemeral_presend_cleanup_failed",
                    fatal_generation=True,
                )
                failure.add_note(f"original pre-send failure: {type(original_error).__name__}")
                raise failure from cleanup_error
            raise

    def validate_pending(self, request_id: str, payload_hash: str) -> None:
        pending = _read_object(self.pending_path)
        self._validate_identity(pending, request_id, payload_hash)
        created_at = pending.get("created_at")
        if not isinstance(created_at, (int, float)) or time.time() - created_at > self.ttl_seconds:
            raise ProviderAdapterError("ephemeral_pending_stale")
        payload = pending.get("ephemeral_current")
        if not isinstance(payload, str) or _payload_hash(payload) != payload_hash:
            raise ProviderAdapterError("ephemeral_pending_mismatch")

    def validate_receipt(self, request_id: str, payload_hash: str) -> None:
        if not self.receipt_path.exists():
            raise ProviderAdapterError(
                "ephemeral_receipt_missing",
                terminal_status="indeterminate",
                fatal_generation=True,
            )
        receipt = _read_object(self.receipt_path)
        try:
            self._validate_identity(receipt, request_id, payload_hash)
        except ProviderAdapterError as exc:
            raise ProviderAdapterError(
                "ephemeral_receipt_mismatch",
                terminal_status="indeterminate",
                fatal_generation=True,
            ) from exc
        if receipt.get("status") != "consumed":
            raise ProviderAdapterError(
                "ephemeral_receipt_invalid",
                terminal_status="indeterminate",
                fatal_generation=True,
            )
        if "ephemeral_current" in receipt:
            raise ProviderAdapterError(
                "ephemeral_receipt_invalid",
                terminal_status="indeterminate",
                fatal_generation=True,
            )

    def cleanup_request(self) -> None:
        self.cleanup_payload_files(self.root)

    @staticmethod
    def cleanup_payload_files(root: Path) -> None:
        for name in (_PENDING_FILE, _CLAIMED_FILE):
            path = Path(root) / name
            if path.exists():
                _unlink_if_present(path)

    def cleanup_stale(self) -> int:
        removed = 0
        now = time.time()
        for name in (_PENDING_FILE, _CLAIMED_FILE):
            path = self.root / name
            if not path.exists():
                continue
            try:
                payload = _read_object(path)
                created_at = payload.get("created_at")
                stale = not isinstance(created_at, (int, float)) or now - created_at > self.ttl_seconds
            except ProviderAdapterError:
                stale = True
            if stale and _unlink_if_present(path):
                removed += 1
        return removed

    def _ensure_identity(self) -> None:
        identity_path = self.root / _IDENTITY_FILE
        expected = {
            "schema_version": "v2",
            "binding_id": self.binding_id,
            "generation_id": self.generation_id,
            "ttl_seconds": self.ttl_seconds,
        }
        if identity_path.exists():
            identity = _read_object(identity_path)
            if identity != expected:
                raise ProviderAdapterError("ephemeral_identity_mismatch", fatal_generation=True)
            return
        _atomic_write(identity_path, expected)

    def _validate_identity(
        self,
        payload: dict[str, Any],
        request_id: str,
        payload_hash: str,
    ) -> None:
        expected = {
            "schema_version": "v2",
            "binding_id": self.binding_id,
            "generation_id": self.generation_id,
            "request_id": request_id,
            "payload_sha256": payload_hash,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ProviderAdapterError("ephemeral_identity_mismatch")


def consume_for_hook(root: Path) -> dict[str, object]:
    identity = _read_object(root / _IDENTITY_FILE)
    receipt_path = root / _RECEIPT_FILE
    pending_path = root / _PENDING_FILE
    claimed_path = root / _CLAIMED_FILE
    if not pending_path.exists():
        if receipt_path.exists():
            return {}
        return {}
    try:
        os.replace(pending_path, claimed_path)
    except FileNotFoundError:
        return {}
    pending: dict[str, Any] | None = None
    try:
        pending = _read_object(claimed_path)
        for key in ("schema_version", "binding_id", "generation_id"):
            if pending.get(key) != identity.get(key):
                raise ProviderAdapterError("ephemeral_identity_mismatch")
        created_at = pending.get("created_at")
        ttl_seconds = identity.get("ttl_seconds")
        if (
            not isinstance(created_at, (int, float))
            or not isinstance(ttl_seconds, (int, float))
            or time.time() - created_at > ttl_seconds
        ):
            raise ProviderAdapterError("ephemeral_pending_stale")
        payload = pending.get("ephemeral_current")
        payload_hash = pending.get("payload_sha256")
        if (
            not isinstance(payload, str)
            or not isinstance(payload_hash, str)
            or _payload_hash(payload) != payload_hash
        ):
            raise ProviderAdapterError("ephemeral_pending_mismatch")
        receipt = {
            "schema_version": "v2",
            "binding_id": pending["binding_id"],
            "generation_id": pending["generation_id"],
            "request_id": pending["request_id"],
            "payload_sha256": payload_hash,
            "status": "consumed",
            "consumed_at": time.time(),
        }
        _atomic_write(receipt_path, receipt)
        if payload:
            return {"injectSteps": [{"ephemeralMessage": payload}]}
        return {}
    except ProviderAdapterError as exc:
        if pending is not None:
            safe_receipt = {
                key: pending.get(key)
                for key in (
                    "schema_version",
                    "binding_id",
                    "generation_id",
                    "request_id",
                    "payload_sha256",
                )
            }
            safe_receipt.update({"status": "error", "code": exc.code})
            _atomic_write(receipt_path, safe_receipt)
        return {}
    finally:
        if claimed_path.exists():
            _unlink_if_present(claimed_path)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mailbox-root", required=True)
    arguments = parser.parse_args()
    try:
        result = consume_for_hook(Path(arguments.mailbox_root))
    except Exception:
        result = {}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
