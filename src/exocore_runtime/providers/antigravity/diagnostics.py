"""Private, bounded AGY failure evidence; never part of the provider wire.

Files live outside generation roots so retiring a generation does not destroy
its error evidence. They may contain private conversation data: do not publish
or feed them into model context. Only failure records are persisted.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import sqlite3

_LOGGER = logging.getLogger(__name__)
_RECORD_LIMIT = 256 * 1024
_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")
_AUTH_VALUE = re.compile(
    r'(?i)("(?:authorization|access_token|refresh_token|api_key|password|secret)"\s*:\s*")[^"\r\n]*(")'
)
_BEARER = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+")
# AGY reports provider failures as its own step type and keeps the human
# readable text only in its conversation database: the stdout frame carries the
# step marker and no message (observed 2026-10-28: an IPv6 connection reset).
# The payload is an uncompressed protobuf whose error text is the longest
# printable run; there is no supported schema to address it by field number.
_PROVIDER_ERROR_STEP_TYPE = 17
_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{50,}")
_PROVIDER_ERROR_LIMIT = 2000


class FailureDiagnostics:
    """Keep only the latest record and result, and write them on failure."""

    def __init__(
        self, layout, request_id: str, environment: dict[str, str], *,
        provider_session_id: str, secret_values: tuple[str, ...] = (),
    ) -> None:
        self.path = (
            layout.root.parent.parent / "diagnostics" / "antigravity"
            / layout.binding_id / f"{request_id}.json"
        )
        self.identity = {
            "binding_id": layout.binding_id,
            "request_id": request_id,
            "provider_session_id": provider_session_id,
            "model": layout.execution_options.provider_model_slug,
        }
        self.secrets = tuple(sorted({
            value for key, value in environment.items()
            if value and any(marker in key.upper() for marker in _SECRET_MARKERS)
        } | {value for value in secret_values if value}, key=len, reverse=True))
        self._profile = layout.profile
        self.last_record: dict | None = None
        self.result_record: dict | None = None
        self.provider_error: dict | None = None

    def observe(self, channel: str, line: bytes | None) -> None:
        if line is None:
            self.last_record = {"channel": channel, "eof": True}
            return
        text = self._redact(line.decode("utf-8", errors="replace"))
        encoded = text.encode("utf-8")
        self.last_record = {
            "channel": channel,
            "original_bytes": len(line),
            "truncated": len(encoded) > _RECORD_LIMIT,
            "raw_text": encoded[:_RECORD_LIMIT].decode("utf-8", errors="replace"),
        }

    def _redact(self, text: str) -> str:
        # Redact before truncation so a boundary cannot expose half a token.
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = _AUTH_VALUE.sub(r"\1[REDACTED]\2", text)
        return _BEARER.sub(r"\1[REDACTED]", text)

    def read_provider_error(self) -> None:
        """Best-effort read of AGY's own error text for this session.

        A failure to read must never replace the original error, so every
        problem is reported as a warning and leaves ``provider_error`` empty.
        """
        db = (
            self._profile / ".gemini" / "antigravity-cli" / "conversations"
            / f"{self.identity['provider_session_id']}.db"
        )
        if not db.is_file():
            return
        try:
            # mode=ro, never immutable: AGY keeps a WAL and an immutable read
            # returns a stale snapshot that misses the newest error step.
            connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                row = connection.execute(
                    "select step_payload from steps where step_type=? order by idx desc limit 1",
                    (_PROVIDER_ERROR_STEP_TYPE,),
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            _LOGGER.warning(
                "AGY provider error read failed (%s): %s", type(exc).__name__, db
            )
            return
        payload = row[0] if row else None
        if not isinstance(payload, (bytes, bytearray)):
            return
        runs = _PRINTABLE_RUN.findall(bytes(payload))
        if not runs:
            return
        # A length-delimited run can start on the delimiter byte itself.
        text = self._redact(max(runs, key=len).decode("ascii", errors="replace")).strip()
        self.provider_error = {
            "source": "agy_conversation_db",
            "text": text[:_PROVIDER_ERROR_LIMIT],
            "truncated": len(text) > _PROVIDER_ERROR_LIMIT,
        }

    def mark_result(self) -> None:
        self.result_record = self.last_record

    def persist(self, error_code: str, notes: list[dict] | None = None) -> None:
        if self.provider_error is None:
            self.read_provider_error()
        payload = {
            **self.identity,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "error_code": error_code,
            "last_record": self.last_record,
            "result_record": self.result_record,
            "provider_error": self.provider_error,
            "notes": notes or [],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            _LOGGER.warning("AGY failure evidence saved: %s (%s)", self.path, error_code)
        except OSError as exc:
            # A diagnostic disk failure must not replace the original error.
            _LOGGER.warning(
                "AGY failure evidence write failed (%s): %s (%s)",
                type(exc).__name__, self.path, error_code,
            )
