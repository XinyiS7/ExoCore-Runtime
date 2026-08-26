"""Provider-neutral generation and turn lifecycle orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
import hashlib
import json
from uuid import UUID, uuid4

from exocore_runtime.contracts import (
    CancelResult,
    FakeBehavior,
    GenerationResult,
    GenerationSpec,
    ProviderEvent,
    RetireResult,
    RuntimeEvent,
    TurnRequest,
)
from exocore_runtime.errors import (
    ConflictError,
    InvalidRequestError,
    ProviderAdapterError,
    ProviderProtocolError,
    RetiredError,
)
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
        provider: RuntimeProviderAdapter | Mapping[str, RuntimeProviderAdapter],
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.providers = (
            dict(provider)
            if isinstance(provider, Mapping)
            else {"fake": provider}
        )
        self.journal = EventJournal(store, secret_values)
        self.instance_id = str(uuid4())
        self.recovered_starting_generations = self.store.fail_inherited_starting_generations()
        self.recovered_sent_requests = self.store.recover_after_restart()
        self._claim_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._claim_locks_guard = asyncio.Lock()
        self._generation_turn_locks: dict[str, asyncio.Lock] = {}
        self._generation_turn_locks_guard = asyncio.Lock()
        self._shutting_down = False

    async def ensure_generation(
        self,
        binding_id: UUID,
        spec: GenerationSpec,
    ) -> GenerationResult:
        if self._shutting_down:
            raise ConflictError("runtime is shutting down")
        binding = str(binding_id)
        provider = self._provider_for_kind(spec.runtime_kind)
        record, created = self.store.ensure_generation(binding, spec)
        if not created:
            if record.status == "active" and record.runtime_kind == "antigravity":
                try:
                    acquired = await provider.ensure_generation(binding, spec)
                    self._validate_acquired_generation(record.provider_session_id, spec, acquired)
                except Exception:
                    self.store.fail_generation(binding, include_active=True)
                    raise
            return GenerationResult(
                binding_id=binding_id,
                status=record.status,
                provider_session_id=record.provider_session_id,
            )
        acquired = None
        try:
            acquired = await provider.ensure_generation(binding, spec)
            self._validate_acquired_generation(spec.provider_session_id, spec, acquired)
            record = self.store.activate_generation(binding, acquired.provider_session_id)
        except Exception as original_error:
            self.store.fail_generation(binding)
            if acquired is not None:
                try:
                    await provider.retire(binding, "generation activation failed")
                except BaseException:
                    original_error.add_note("generation acquisition cleanup also failed")
            raise
        return GenerationResult(
            binding_id=binding_id,
            status=record.status,
            provider_session_id=record.provider_session_id,
        )

    def preflight_turn(self, binding_id: UUID, request: TurnRequest) -> None:
        if self._shutting_down:
            raise ConflictError("runtime is shutting down")
        binding = str(binding_id)
        request_id = str(request.request_id)
        payload_hash = self._request_hash(request)
        generation = self.store.get_generation(binding)
        self._provider_for_kind(generation.runtime_kind)
        self._validate_runtime_turn_fields(generation.runtime_kind, request)
        existing = self.store.get_request(binding, request_id)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise ConflictError("request identity is immutable")
            return
        if generation.status == "retired":
            raise RetiredError("generation is retired")
        if generation.status != "active":
            raise ConflictError("generation is not active")
        if generation.runtime_kind == "antigravity":
            self._validate_bootstrap_state(generation.bootstrap_sent, request)

    async def stream_turn(
        self,
        binding_id: UUID,
        request: TurnRequest,
    ) -> AsyncIterator[RuntimeEvent]:
        if self._shutting_down:
            raise ConflictError("runtime is shutting down")
        binding = str(binding_id)
        request_id = str(request.request_id)
        payload_hash = self._request_hash(request)
        generation = self.store.get_generation(binding)
        provider = self._provider_for_kind(generation.runtime_kind)
        self._validate_runtime_turn_fields(generation.runtime_kind, request)
        request_lock = await self._get_claim_lock(binding, request_id)
        async with request_lock:
            record, disposition = self.store.claim_request(
                binding,
                request_id,
                payload_hash,
                self.instance_id,
            )

        if disposition == "replay":
            for event in self.journal.replay(binding, request_id):
                yield event
            return
        if disposition == "observer":
            async for event in self._wait_and_replay(binding, request_id):
                yield event
            return

        generation_lock = await self._get_generation_turn_lock(binding)
        try:
            async with generation_lock:
                async for event in self._run_owned_turn(
                    binding_id,
                    request,
                    provider,
                ):
                    yield event
        except asyncio.CancelledError as original_error:
            try:
                await self.cancel(binding_id, request.request_id)
            except BaseException:
                original_error.add_note("owner cancellation cleanup also failed")
            raise

    async def _run_owned_turn(
        self,
        binding_id: UUID,
        request: TurnRequest,
        provider: RuntimeProviderAdapter,
    ) -> AsyncIterator[RuntimeEvent]:
        binding = str(binding_id)
        request_id = str(request.request_id)
        current = self.store.get_request(binding, request_id)
        if current is None:
            raise ConflictError("request disappeared")
        if current.terminal:
            for event in self.journal.replay(binding, request_id):
                yield event
            return

        generation = self.store.get_generation(binding)
        is_first_turn = generation.runtime_kind == "antigravity" and not generation.bootstrap_sent
        if generation.runtime_kind == "antigravity":
            try:
                self._validate_bootstrap_state(generation.bootstrap_sent, request)
            except (ConflictError, InvalidRequestError):
                terminal, _ = self.journal.terminal(
                    binding,
                    request_id,
                    "error",
                    {"code": "bootstrap_state_conflict"},
                    "failed",
                    "bootstrap_state_conflict",
                )
                yield terminal
                return
        try:
            await provider.prepare_turn(
                binding,
                request,
                is_first_turn=is_first_turn,
                generation_identity_hash=generation.identity_hash,
                expected_provider_session_id=generation.provider_session_id,
            )
        except asyncio.CancelledError:
            raise
        except ProviderAdapterError as exc:
            if exc.fatal_generation:
                self.store.fail_generation(binding, include_active=True)
            terminal, _ = self.journal.terminal(
                binding,
                request_id,
                "error",
                {"code": exc.code},
                "failed",
                exc.code,
            )
            yield terminal
            return
        except Exception:
            self.store.fail_generation(binding, include_active=True)
            terminal, _ = self.journal.terminal(
                binding,
                request_id,
                "error",
                {"code": "provider_prepare_exception"},
                "failed",
                "provider_prepare_exception",
            )
            yield terminal
            return

        current = self.store.get_request(binding, request_id)
        if current is None:
            raise ConflictError("request disappeared")
        if current.terminal:
            await provider.cancel(binding, request_id)
            for event in self.journal.replay(binding, request_id):
                yield event
            return

        try:
            self.store.mark_sent(
                binding,
                request_id,
                self.instance_id,
                consume_bootstrap=is_first_turn,
            )
        except (ConflictError, RetiredError):
            current = self.store.get_request(binding, request_id)
            if current is not None and current.terminal:
                await provider.cancel(binding, request_id)
                for event in self.journal.replay(binding, request_id):
                    yield event
                return
            terminal, _ = self.journal.terminal(
                binding,
                request_id,
                "error",
                {"code": "send_boundary_conflict"},
                "failed",
                "send_boundary_conflict",
            )
            await provider.cancel(binding, request_id)
            yield terminal
            return
        async for event in self._stream_provider_events(binding_id, request, provider):
            yield event

    async def _stream_provider_events(
        self,
        binding_id: UUID,
        request: TurnRequest,
        provider: RuntimeProviderAdapter,
    ) -> AsyncIterator[RuntimeEvent]:
        binding = str(binding_id)
        request_id = str(request.request_id)
        yielded_sequence = 0
        pending_terminal: ProviderEvent | None = None
        failure_code: str | None = None
        failure_status = "indeterminate"
        try:
            async for provider_event in provider.stream_turn(binding, request):
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
            raise
        except ProviderAdapterError as exc:
            failure_code = exc.code
            failure_status = exc.terminal_status
            if exc.fatal_generation:
                self.store.fail_generation(binding, include_active=True)
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
            terminal_status = pending_terminal.terminal_status or "failed"
            terminal_code = str(pending_terminal.payload.get("code", "provider_error"))
            terminal, _ = self.journal.terminal(
                binding,
                request_id,
                "error",
                pending_terminal.payload,
                terminal_status,
                terminal_code,
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
        generation = self.store.get_generation(binding)
        provider = self._provider_for_kind(generation.runtime_kind)
        record = self.store.get_request(binding, request_key)
        if record is None:
            raise ConflictError("request has not been prepared")
        if record.terminal:
            if record.status == "cancelled":
                await provider.cancel(binding, request_key)
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
            await provider.cancel(binding, request_key)
        status = self.store.get_request(binding, request_key).status
        return CancelResult(
            binding_id=binding_id,
            request_id=request_id,
            status=status,
            changed=changed,
        )

    async def retire(self, binding_id: UUID, reason: str) -> RetireResult:
        binding = str(binding_id)
        generation = self.store.get_generation(binding)
        provider = self._provider_for_kind(generation.runtime_kind)
        record, changed = self.store.retire_generation(binding, reason)
        await provider.retire(binding, reason)
        return RetireResult(binding_id=binding_id, status=record.status, changed=changed)

    async def shutdown(self) -> None:
        self._shutting_down = True
        self.store.terminalize_open_requests_for_shutdown()
        unique_providers = {id(provider): provider for provider in self.providers.values()}
        results = await asyncio.gather(
            *(provider.shutdown() for provider in unique_providers.values()),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise ProviderAdapterError("provider_shutdown_failed")

    def _provider_for_kind(self, runtime_kind: str) -> RuntimeProviderAdapter:
        provider = self.providers.get(runtime_kind)
        if provider is None:
            raise InvalidRequestError("unsupported runtime kind")
        return provider

    @staticmethod
    def _validate_acquired_generation(expected_session_id, spec, acquired) -> None:
        if acquired.observed_model != spec.provider_model_id:
            raise ProviderProtocolError("provider observed model mismatch")
        if expected_session_id is not None and acquired.provider_session_id != expected_session_id:
            raise ProviderProtocolError("provider session identity mismatch")

    @staticmethod
    def _validate_runtime_turn_fields(runtime_kind: str, request: TurnRequest) -> None:
        if runtime_kind == "antigravity":
            if request.behavior != FakeBehavior.NORMAL:
                raise InvalidRequestError("fake behavior is unavailable for antigravity")
            return
        if request.bootstrap_context is not None or request.ephemeral_current is not None:
            raise InvalidRequestError("fake turns do not accept antigravity context")

    @staticmethod
    def _validate_bootstrap_state(bootstrap_sent: bool, request: TurnRequest) -> None:
        if bootstrap_sent and request.bootstrap_context is not None:
            raise ConflictError("generation bootstrap was already consumed")
        if not bootstrap_sent and request.bootstrap_context is None:
            raise InvalidRequestError("first antigravity turn requires bootstrap context")

    async def _get_claim_lock(self, binding_id: str, request_id: str) -> asyncio.Lock:
        key = (binding_id, request_id)
        async with self._claim_locks_guard:
            return self._claim_locks.setdefault(key, asyncio.Lock())

    async def _get_generation_turn_lock(self, binding_id: str) -> asyncio.Lock:
        async with self._generation_turn_locks_guard:
            return self._generation_turn_locks.setdefault(binding_id, asyncio.Lock())

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
