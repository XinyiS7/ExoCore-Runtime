"""Provider protocol; adapters own capability/process details, never durable request truth."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from exocore_runtime.contracts import (
    EffectiveResolution,
    GenerationSpec,
    ProcessExecutionOptions,
    ProviderEvent,
    ProviderGeneration,
    TurnRequest,
)
from exocore_runtime.state_store import GenerationRecord


class RuntimeProviderAdapter(Protocol):
    def resolve_execution(
        self,
        requested_model_id: str,
        requested_thinking_level: str,
    ) -> EffectiveResolution: ...

    def stage_generation(self, binding_id: str, spec: GenerationSpec) -> None: ...

    async def prepare_turn(
        self,
        generation: GenerationRecord,
        request: TurnRequest,
        options: ProcessExecutionOptions,
        *,
        is_first_turn: bool,
    ) -> ProviderGeneration: ...

    def stream_turn(
        self,
        binding_id: str,
        request: TurnRequest,
    ) -> AsyncIterator[ProviderEvent]: ...

    async def cancel(self, binding_id: str, request_id: str) -> None: ...

    async def retire(self, binding_id: str, reason: str) -> None: ...

    async def shutdown(self) -> None: ...
