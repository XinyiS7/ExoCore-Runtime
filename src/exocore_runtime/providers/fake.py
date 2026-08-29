"""Deterministic in-process provider used only for isolated runtime tests."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator

from exocore_runtime.contracts import (
    EffectiveResolution,
    GenerationSpec,
    ProcessExecutionOptions,
    ProviderEvent,
    ProviderGeneration,
    TurnRequest,
)
from exocore_runtime.state_store import GenerationRecord


class DeterministicFakeAdapter:
    def __init__(self) -> None:
        self.generation_stages: Counter[str] = Counter()
        self.process_prepares: Counter[str] = Counter()
        self.process_spawns: Counter[str] = Counter()
        self.process_disposals: Counter[str] = Counter()
        self.turn_sends: Counter[tuple[str, str]] = Counter()
        self.resolver_calls: Counter[tuple[str, str]] = Counter()
        self._cancel_signals: dict[tuple[str, str], asyncio.Event] = {}
        self._behaviors: dict[str, str] = {}
        self._options: dict[str, ProcessExecutionOptions] = {}

    def set_behavior(self, request_id: str, behavior: str) -> None:
        self._behaviors[request_id] = behavior

    def resolve_execution(
        self,
        requested_model_id: str,
        requested_thinking_level: str,
    ) -> EffectiveResolution:
        self.resolver_calls[(requested_model_id, requested_thinking_level)] += 1
        effort = "low" if requested_thinking_level == "low" else "high"
        options = ProcessExecutionOptions(
            provider_model_slug=f"fake-{requested_model_id}-{effort}",
            effort=effort,
            security_policy_revision="fake-security-v1",
            launch_environment_revision="fake-launch-v1",
        )
        return EffectiveResolution(
            provider_model_slug=options.provider_model_slug,
            effort=options.effort,
            resolver_policy_revision="fake-policy-v1",
            process_options=options,
        )

    def stage_generation(self, binding_id: str, spec: GenerationSpec) -> None:
        self.generation_stages[binding_id] += 1

    async def prepare_turn(
        self,
        generation: GenerationRecord,
        request: TurnRequest,
        options: ProcessExecutionOptions,
        *,
        is_first_turn: bool,
    ) -> ProviderGeneration:
        binding_id = generation.binding_id
        self.process_prepares[binding_id] += 1
        previous = self._options.get(binding_id)
        if previous != options:
            if previous is not None:
                self.process_disposals[binding_id] += 1
            self.process_spawns[binding_id] += 1
            self._options[binding_id] = options
        session_id = generation.provider_session_id or f"fake-session-{binding_id}"
        return ProviderGeneration(
            provider_session_id=session_id,
            observed_model=options.provider_model_slug,
            observed_effort=options.effort,
        )

    async def stream_turn(
        self,
        binding_id: str,
        request: TurnRequest,
    ) -> AsyncIterator[ProviderEvent]:
        request_id = str(request.request_id)
        key = (binding_id, request_id)
        self.turn_sends[key] += 1
        cancel_signal = self._cancel_signals.setdefault(key, asyncio.Event())
        behavior = self._behaviors.get(request_id, "normal")

        if behavior == "empty":
            return
        if behavior == "exception":
            raise RuntimeError("deterministic fake provider exception")
        if behavior == "malformed":
            yield {"not": "a provider event"}  # type: ignore[misc]
            return

        yield ProviderEvent(event_type="thinking_delta", payload={"text": "fake-thinking"})
        if behavior == "cancel_late":
            await cancel_signal.wait()
            yield ProviderEvent(event_type="content_delta", payload={"text": "late-content"})
            yield ProviderEvent(event_type="done", payload={"finish_reason": "late"})
            return

        yield ProviderEvent(event_type="content_delta", payload={"text": "fake-content"})
        if behavior == "unexpected_eof":
            return
        if behavior == "provider_error":
            yield ProviderEvent(event_type="error", payload={"code": "fake_provider_error"})
            return
        yield ProviderEvent(event_type="usage", payload={"input_tokens": 3, "output_tokens": 2})
        yield ProviderEvent(event_type="done", payload={"finish_reason": "stop"})
        if behavior == "duplicate_terminal":
            yield ProviderEvent(event_type="done", payload={"finish_reason": "duplicate"})
        elif behavior == "terminal_then_event":
            yield ProviderEvent(event_type="content_delta", payload={"text": "after-terminal"})

    async def cancel(self, binding_id: str, request_id: str) -> None:
        self._cancel_signals.setdefault((binding_id, request_id), asyncio.Event()).set()

    async def retire(self, binding_id: str, reason: str) -> None:
        self._options.pop(binding_id, None)

    async def shutdown(self) -> None:
        self._options.clear()
