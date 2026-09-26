"""Deterministic in-process provider used only for isolated runtime tests."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable

from exocore_runtime.contracts import (
    EffectiveResolution,
    GenerationSpec,
    ProcessExecutionOptions,
    ProviderEvent,
    ProviderGeneration,
    TurnRequest,
)
from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.base import (
    ProviderCancelOutcome,
    ProviderCancelReceipt,
)
from exocore_runtime.state_store import GenerationRecord


class DeterministicFakeAdapter:
    def __init__(self) -> None:
        self.generation_stages: Counter[str] = Counter()
        self.process_prepares: Counter[str] = Counter()
        self.process_spawns: Counter[str] = Counter()
        self.process_disposals: Counter[str] = Counter()
        self.turn_sends: Counter[tuple[str, str]] = Counter()
        self.cancel_calls: Counter[tuple[str, str]] = Counter()
        self.cancel_cancelled: set[tuple[str, str]] = set()
        self.reclaims: Counter[tuple[str, str]] = Counter()
        self.staged_attachments: dict[tuple[str, str, str], bytes] = {}
        self.attachment_discards: Counter[tuple[str, str]] = Counter()
        self.resolver_calls: Counter[tuple[str, str]] = Counter()
        self._cancel_signals: dict[tuple[str, str], asyncio.Event] = {}
        self._behaviors: dict[str, str] = {}
        self._options: dict[str, ProcessExecutionOptions] = {}
        self._cancel_gates: dict[str, asyncio.Event] = {}
        self._cancel_receipts: dict[str, ProviderCancelReceipt] = {}
        self._cancel_failures: set[str] = set()
        self._prepare_gates: dict[str, asyncio.Event] = {}
        self._stream_gates: dict[str, asyncio.Event] = {}
        self._send_fences: set[str] = set()

    def set_behavior(self, request_id: str, behavior: str) -> None:
        self._behaviors[request_id] = behavior

    def set_cancel_gate(self, request_id: str) -> asyncio.Event:
        """Hold ``cancel()`` at a deterministic barrier until it is released."""

        gate = self._cancel_gates.get(request_id)
        if gate is None:
            gate = asyncio.Event()
            self._cancel_gates[request_id] = gate
        return gate

    def release_prepare(self, request_id: str) -> None:
        gate = self._prepare_gates.get(request_id)
        if gate is not None:
            gate.set()

    def release_stream(self, request_id: str) -> None:
        gate = self._stream_gates.get(request_id)
        if gate is not None:
            gate.set()

    def set_cancel_receipt(
        self,
        request_id: str,
        outcome: ProviderCancelOutcome,
        natural_terminal: ProviderEvent | None = None,
    ) -> None:
        self._cancel_receipts[request_id] = ProviderCancelReceipt(
            outcome,
            natural_terminal,
        )

    def set_cancel_failure(self, request_id: str) -> None:
        self._cancel_failures.add(request_id)

    def prepare_gate(self, request_id: str) -> asyncio.Event:
        gate = self._prepare_gates.get(request_id)
        if gate is None:
            gate = asyncio.Event()
            self._prepare_gates[request_id] = gate
        return gate

    def stream_gate(self, request_id: str) -> asyncio.Event:
        gate = self._stream_gates.get(request_id)
        if gate is None:
            gate = asyncio.Event()
            self._stream_gates[request_id] = gate
        return gate

    def is_send_fenced(self, request_id: str) -> bool:
        return request_id in self._send_fences

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

    async def stage_attachment(
        self,
        binding_id: str,
        request_id: str,
        artifact_id: str,
        data: bytes,
        *,
        guard: Callable[[], None],
    ) -> None:
        guard()
        self.staged_attachments[(binding_id, request_id, artifact_id)] = data

    async def discard_attachments(
        self,
        binding_id: str,
        request_id: str,
        *,
        guard: Callable[[], None],
    ) -> None:
        guard()
        self.attachment_discards[(binding_id, request_id)] += 1
        for key in [
            key
            for key in self.staged_attachments
            if key[:2] == (binding_id, request_id)
        ]:
            self.staged_attachments.pop(key, None)

    async def prepare_turn(
        self,
        generation: GenerationRecord,
        request: TurnRequest,
        options: ProcessExecutionOptions,
        *,
        is_first_turn: bool,
    ) -> ProviderGeneration:
        binding_id = generation.binding_id
        request_id = str(request.request_id)
        self.process_prepares[binding_id] += 1
        behavior = self._behaviors.get(request_id, "normal")
        if behavior == "prepare_blocked":
            await self.prepare_gate(request_id).wait()
        elif behavior == "prepare_blocked_error":
            await self.prepare_gate(request_id).wait()
            raise ProviderAdapterError("fake_prepare_failure")
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
        if request_id in self._send_fences:
            # A fenced pre-start request can never produce provider input
            # after the arbiter/settlement decided its fate.
            raise ProviderAdapterError(
                "fake_prestart_send_fenced",
                terminal_status="indeterminate",
            )
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
        if behavior == "gated_eof":
            await self.stream_gate(request_id).wait()
            return
        if behavior == "gated_exception":
            await self.stream_gate(request_id).wait()
            raise RuntimeError("deterministic fake provider exception")
        if behavior == "gated_error":
            await self.stream_gate(request_id).wait()
            yield ProviderEvent(event_type="error", payload={"code": "fake_provider_error"})
            return
        if behavior == "gated_malformed":
            await self.stream_gate(request_id).wait()
            yield {"not": "a provider event"}  # type: ignore[misc]
            return
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

    async def cancel(self, binding_id: str, request_id: str) -> ProviderCancelReceipt:
        key = (binding_id, request_id)
        self.cancel_calls[key] += 1
        gate = self._cancel_gates.get(request_id)
        if gate is not None:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                self.cancel_cancelled.add(key)
                raise
        if request_id in self._cancel_failures:
            raise ProviderAdapterError("fake_cancel_cleanup_failed")
        receipt = self._cancel_receipts.get(request_id)
        if receipt is None:
            if self.turn_sends[key] == 0:
                self._send_fences.add(request_id)
                receipt = ProviderCancelReceipt(ProviderCancelOutcome.CANCELLED_PRESTART)
            else:
                receipt = ProviderCancelReceipt(ProviderCancelOutcome.CANCELLED_ACTIVE)
        prepare_gate = self._prepare_gates.get(request_id)
        if prepare_gate is not None:
            prepare_gate.set()
        self._cancel_signals.setdefault(key, asyncio.Event()).set()
        return receipt

    def reclaim_request(self, binding_id: str, request_id: str) -> None:
        self.reclaims[(binding_id, request_id)] += 1

    async def retire(self, binding_id: str, reason: str) -> None:
        self._options.pop(binding_id, None)
        for key in [key for key in self.staged_attachments if key[0] == binding_id]:
            self.staged_attachments.pop(key, None)

    async def shutdown(self) -> None:
        self._options.clear()
