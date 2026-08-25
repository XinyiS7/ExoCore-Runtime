"""Provider-neutral generation and turn lifecycle orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import hashlib
import json
from uuid import UUID, uuid4

from exocore_runtime.contracts import (
    CancelResult,
    GenerationResult,
    GenerationSpec,
    ProviderEvent,
    RetireResult,
    RuntimeEvent,
    TurnRequest,
)
from exocore_runtime.errors import ConflictError, ProviderProtocolError, RetiredError
from exocore_runtime.event_journal import EventJournal
from exocore_runtime.providers.base import RuntimeProviderAdapter
from exocore_runtime.state_store import RuntimeStateStore


_NONTERMINAL_TYPES = frozenset({"thinking_delta", "content_delta", "lifecycle", "usage"})
_TERMINAL_TYPES = frozenset({"done", "error"})


class RuntimeService:
    """Coordinates adapters through durable state without owning canonical chat data."""

    def __init__(
        self,
        store: RuntimeStateStore,
        provider: RuntimeProviderAdapter,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.provider = provider
        self.journal = EventJournal(store, secret_values)
        self.instance_id = str(uuid4())
        self.recovered_starting_generations = self.store.fail_inherited_starting_generations()
        self.recovered_sent_requests = self.store.recover_after_restart()
        self._claim_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._claim_locks_guard = asyncio.Lock()

    async def ensure_generation(
        self,
        binding_id: UUID,
        spec: GenerationSpec,
    ) -> GenerationResult:
        binding = str(binding_id)
        record, created = self.store.ensure_generation(binding, spec)
        if not created:
            return GenerationResult(
                binding_id=binding_id,
                status=record.status,
                provider_session_id=record.provider_session_id,
            )
        try:
            acquired = await self.provider.ensure_generation(binding, spec)
            if acquired.observed_model != spec.provider_model_id:
                raise ProviderProtocolError("provider observed model mismatch")
            record = self.store.activate_generation(binding, acquired.provider_session_id)
        except Exception:
            self.store.fail_generation(binding)
            raise
        return GenerationResult(
            binding_id=binding_id,
            status=record.status,
            provider_session_id=record.provider_session_id,
        )

    def preflight_turn(self, binding_id: UUID, request: TurnRequest) -> None:
        binding = str(binding_id)
        request_id = str(request.request_id)
        payload_hash = self._request_hash(request)
        existing = self.store.get_request(binding, request_id)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise ConflictError("request identity is immutable")
            return
        generation = self.store.get_generation(binding)
        if generation.status == "retired":
            raise RetiredError("generation is retired")
        if generation.status != "active":
            raise ConflictError("generation is not active")

    async def stream_turn(
        self,
        binding_id: UUID,
        request: TurnRequest,
    ) -> AsyncIterator[RuntimeEvent]:
        binding = str(binding_id)
        request_id = str(request.request_id)
        payload_hash = self._request_hash(request)
        lock = await self._get_claim_lock(binding, request_id)
        async with lock:
            record, disposition = self.store.claim_request(
                binding,
                request_id,
                payload_hash,
                self.instance_id,
            )
            if disposition == "owner":
                self.store.mark_sent(binding, request_id, self.instance_id)

        if disposition == "replay":
            for event in self.journal.replay(binding, request_id):
                yield event
            return
        if disposition == "observer":
            async for event in self._wait_and_replay(binding, request_id):
                yield event
            return

        yielded_sequence = 0
        pending_terminal: ProviderEvent | None = None
        failure_code: str | None = None
        failure_status = "indeterminate"
        try:
            async for provider_event in self.provider.stream_turn(binding, request):
                if not isinstance(provider_event, ProviderEvent):
                    failure_code = "malformed_provider_event"
                    failure_status = "failed"
                    break
                current = self.store.get_request(binding, request_id)
                if current is None:
                    raise ConflictError("request disappeared")
                if current.terminal:
                    break
                if pending_terminal is not None:
                    failure_code = "event_after_terminal"
                    failure_status = "failed"
                    break
                if provider_event.event_type in _TERMINAL_TYPES:
                    pending_terminal = provider_event
                    continue
                if provider_event.event_type not in _NONTERMINAL_TYPES:
                    failure_code = "unknown_provider_event"
                    failure_status = "failed"
                    break
                event = self.journal.append(
                    binding,
                    request_id,
                    provider_event.event_type,
                    provider_event.payload,
                )
                if event is None:
                    break
                yielded_sequence = event.sequence
                yield event
        except asyncio.CancelledError:
            await self.cancel(binding_id, request.request_id)
            raise
        except Exception:
            failure_code = "provider_exception"
            failure_status = "indeterminate"

        current = self.store.get_request(binding, request_id)
        if current is None:
            raise ConflictError("request disappeared")
        if current.terminal:
            for event in self.journal.replay(binding, request_id):
                if event.sequence > yielded_sequence:
                    yield event
            return

        if failure_code is not None:
            terminal, _ = self.journal.terminal(
                binding,
                request_id,
                "error",
                {"code": failure_code},
                failure_status,
                failure_code,
            )
        elif pending_terminal is None:
            terminal, _ = self.journal.terminal(
                binding,
                request_id,
                "error",
                {"code": "unexpected_provider_eof"},
                "indeterminate",
                "unexpected_provider_eof",
            )
        elif pending_terminal.event_type == "error":
            terminal, _ = self.journal.terminal(
                binding,
                request_id,
                "error",
                pending_terminal.payload,
                "failed",
                str(pending_terminal.payload.get("code", "provider_error")),
            )
        else:
            terminal, _ = self.journal.terminal(
                binding,
                request_id,
                "done",
                pending_terminal.payload,
                "completed",
                "completed",
            )
        if terminal.sequence > yielded_sequence:
            yield terminal

    async def cancel(self, binding_id: UUID, request_id: UUID) -> CancelResult:
        binding = str(binding_id)
        request_key = str(request_id)
        record = self.store.get_request(binding, request_key)
        if record is None:
            raise ConflictError("request has not been prepared")
        if record.terminal:
            return CancelResult(
                binding_id=binding_id,
                request_id=request_id,
                status=record.status,
                changed=False,
            )
        terminal, changed = self.journal.terminal(
            binding,
            request_key,
            "error",
            {"code": "cancelled"},
            "cancelled",
            "cancelled",
        )
        if changed:
            await self.provider.cancel(binding, request_key)
        status = self.store.get_request(binding, request_key).status
        return CancelResult(
            binding_id=binding_id,
            request_id=request_id,
            status=status,
            changed=changed,
        )

    async def retire(self, binding_id: UUID, reason: str) -> RetireResult:
        binding = str(binding_id)
        record, changed = self.store.retire_generation(binding, reason)
        if changed:
            await self.provider.retire(binding, reason)
        return RetireResult(binding_id=binding_id, status=record.status, changed=changed)

    async def _get_claim_lock(self, binding_id: str, request_id: str) -> asyncio.Lock:
        key = (binding_id, request_id)
        async with self._claim_locks_guard:
            return self._claim_locks.setdefault(key, asyncio.Lock())

    async def _wait_and_replay(
        self,
        binding_id: str,
        request_id: str,
    ) -> AsyncIterator[RuntimeEvent]:
        while True:
            record = self.store.get_request(binding_id, request_id)
            if record is None:
                raise ConflictError("request disappeared")
            if record.terminal:
                for event in self.journal.replay(binding_id, request_id):
                    yield event
                return
            await asyncio.sleep(0.01)

    @staticmethod
    def _request_hash(request: TurnRequest) -> str:
        canonical = json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()
