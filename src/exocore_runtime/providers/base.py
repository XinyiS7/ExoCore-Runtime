"""Provider protocol; concrete adapters cannot own durable transport truth."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from exocore_runtime.contracts import (
    GenerationSpec,
    ProviderEvent,
    ProviderGeneration,
    TurnRequest,
)


class RuntimeProviderAdapter(Protocol):
    async def ensure_generation(
        self,
        binding_id: str,
        spec: GenerationSpec,
    ) -> ProviderGeneration: ...

    def stream_turn(
        self,
        binding_id: str,
        request: TurnRequest,
    ) -> AsyncIterator[ProviderEvent]: ...

    async def cancel(self, binding_id: str, request_id: str) -> None: ...

    async def retire(self, binding_id: str, reason: str) -> None: ...
