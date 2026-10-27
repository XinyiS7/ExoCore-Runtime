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

_LOGGER = logging.getLogger(__name__)
_RECORD_LIMIT = 256 * 1024
_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")
_AUTH_VALUE = re.compile(
    r'(?i)("(?:authorization|access_token|refresh_token|api_key|password|secret)"\s*:\s*")[^"\r\n]*(")'
)
_BEARER = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+")


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
        self.last_record: dict | None = None
        self.result_record: dict | None = None

    def observe(self, channel: str, line: bytes | None) -> None:
        if line is None:
            self.last_record = {"channel": channel, "eof": True}
            return
        # Redact before truncation so a boundary cannot expose half a token.
        text = line.decode("utf-8", errors="replace")
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = _AUTH_VALUE.sub(r"\1[REDACTED]\2", text)
        text = _BEARER.sub(r"\1[REDACTED]", text)
        encoded = text.encode("utf-8")
        self.last_record = {
            "channel": channel,
            "original_bytes": len(line),
            "truncated": len(encoded) > _RECORD_LIMIT,
            "raw_text": encoded[:_RECORD_LIMIT].decode("utf-8", errors="replace"),
        }

    def mark_result(self) -> None:
        self.result_record = self.last_record

    def persist(self, error_code: str) -> None:
        payload = {
            **self.identity,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "error_code": error_code,
            "last_record": self.last_record,
            "result_record": self.result_record,
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