"""Deterministic fake provider for protocol and lifecycle verification only."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator

from exocore_runtime.contracts import (
    FakeBehavior,
    GenerationSpec,
    ProviderEvent,
    ProviderGeneration,
    TurnRequest,
)


class DeterministicFakeAdapter:
    def __init__(self) -> None:
        self.generation_acquisitions: Counter[str] = Counter()
        self.turn_sends: Counter[tuple[str, str]] = Counter()
        self._cancel_signals: dict[tuple[str, str], asyncio.Event] = {}

    async def ensure_generation(
        self,
        binding_id: str,
        spec: GenerationSpec,
    ) -> ProviderGeneration:
        self.generation_acquisitions[binding_id] += 1
        session_id = spec.provider_session_id or f"fake-session-{binding_id}"
        return ProviderGeneration(
            provider_session_id=session_id,
            observed_model=spec.provider_model_id,
        )

    async def prepare_turn(
        self,
        binding_id: str,
        request: TurnRequest,
        *,
        is_first_turn: bool,
        generation_identity_hash: str,
        expected_provider_session_id: str | None,
    ) -> None:
        return None

    async def stream_turn(
        self,
        binding_id: str,
        request: TurnRequest,
    ) -> AsyncIterator[ProviderEvent]:
        request_id = str(request.request_id)
        key = (binding_id, request_id)
        self.turn_sends[key] += 1
        cancel_signal = self._cancel_signals.setdefault(key, asyncio.Event())

        if request.behavior == FakeBehavior.EMPTY:
            return
        if request.behavior == FakeBehavior.EXCEPTION:
            raise RuntimeError("deterministic fake provider exception")
        if request.behavior == FakeBehavior.MALFORMED:
            yield {"not": "a provider event"}  # type: ignore[misc]
            return

        yield ProviderEvent(event_type="thinking_delta", payload={"text": "fake-thinking"})

        if request.behavior == FakeBehavior.CANCEL_LATE:
            await cancel_signal.wait()
            yield ProviderEvent(event_type="content_delta", payload={"text": "late-content"})
            yield ProviderEvent(event_type="done", payload={"finish_reason": "late"})
            return

        yield ProviderEvent(event_type="content_delta", payload={"text": "fake-content"})
        if request.behavior == FakeBehavior.UNEXPECTED_EOF:
            return
        if request.behavior == FakeBehavior.PROVIDER_ERROR:
            yield ProviderEvent(event_type="error", payload={"code": "fake_provider_error"})
            return

        yield ProviderEvent(event_type="usage", payload={"input_tokens": 3, "output_tokens": 2})
        yield ProviderEvent(event_type="done", payload={"finish_reason": "stop"})
        if request.behavior == FakeBehavior.DUPLICATE_TERMINAL:
            yield ProviderEvent(event_type="done", payload={"finish_reason": "duplicate"})
        elif request.behavior == FakeBehavior.TERMINAL_THEN_EVENT:
            yield ProviderEvent(event_type="content_delta", payload={"text": "after-terminal"})

    async def cancel(self, binding_id: str, request_id: str) -> None:
        self._cancel_signals.setdefault((binding_id, request_id), asyncio.Event()).set()

    async def retire(self, binding_id: str, reason: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None
