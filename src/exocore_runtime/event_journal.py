"""Durable normalized event journal facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from exocore_runtime.contracts import RuntimeEvent

if TYPE_CHECKING:
    from exocore_runtime.state_store import RuntimeStateStore


class EventJournal:
    def __init__(
        self,
        store: "RuntimeStateStore",
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self._store = store
        self._secret_values = tuple(secret for secret in secret_values if secret)

    def replay(self, binding_id: str, request_id: str) -> list[RuntimeEvent]:
        return self._store.read_events(binding_id, request_id)

    def append(
        self,
        binding_id: str,
        request_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> RuntimeEvent | None:
        return self._store.append_event(
            binding_id,
            request_id,
            event_type,
            self._redact(payload),
        )

    def terminal(
        self,
        binding_id: str,
        request_id: str,
        event_type: str,
        payload: dict[str, object],
        status: str,
        terminal_code: str,
    ) -> tuple[RuntimeEvent, bool]:
        return self._store.append_terminal(
            binding_id,
            request_id,
            event_type,
            self._redact(payload),
            status,
            terminal_code,
        )

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self._secret_values:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {
                self._redact(key): self._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value
