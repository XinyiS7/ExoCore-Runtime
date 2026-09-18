"""Runtime v2 generation, resolution, activation, and turn orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from enum import Enum
import logging
import threading
from uuid import UUID, uuid4

from exocore_runtime.contracts import (
    CancelResult,
    EffectiveResolution,
    GenerationResult,
    GenerationSpec,
    ProviderEvent,
    RetireResult,
    RuntimeEvent,
    TurnRequest,
    canonical_turn_request_hash,
)
from exocore_runtime.errors import (
    CancelUnregisteredError,
    ConflictError,
    ProviderAdapterError,
    ProviderProtocolError,
    RetiredError,
)
from exocore_runtime.event_journal import EventJournal
from exocore_runtime.providers.base import (
    ProviderCancelOutcome,
    ProviderCancelReceipt,
    RuntimeProviderAdapter,
)
from exocore_runtime.state_store import RequestRecord, RuntimeStateStore


_NONTERMINAL_TYPES = frozenset({"thinking_delta", "content_delta", "lifecycle", "usage"})
_TERMINAL_TYPES = frozenset({"done", "error"})
_LOGGER = logging.getLogger(__name__)


class ArbiterState(str, Enum):
    """Whole-request terminal arbitration states (Plan CP4 §3.3.1)."""

    OPEN = "open"
    NATURAL_TERMINAL_PENDING = "natural_terminal_pending"
    CANCELLING = "cancelling"
    TERMINAL = "terminal"


class _RequestArbiter:
    """One terminal winner slot per claimed Runtime request.

    The lock is deliberately a plain ``threading.Lock``: every critical
    section is synchronous (state compare/transition, task registration,
    snapshot references) and must never await provider, journal, or network
    I/O (Law 5). Holding it across an await would stall the single event loop;
    the discipline is that no ``with arbiter.lock`` block in this module
    contains an await.
    """

    __slots__ = (
        "binding_id",
        "request_id",
        "state",
        "lock",
        "done_event",
        "cancellation_task",
        "failure",
    )

    def __init__(self, binding_id: str, request_id: str) -> None:
        self.binding_id = binding_id
        self.request_id = request_id
        self.state = ArbiterState.OPEN
        self.lock = threading.Lock()
        self.done_event = asyncio.Event()
        self.cancellation_task: asyncio.Task[bool] | None = None
        self.failure: BaseException | None = None


class RuntimeService:
    """Coordinates durable v2 truth without owning canonical ExoCore chat data."""

    def __init__(
        self,
        store: RuntimeStateStore,
        provider: RuntimeProviderAdapter | Mapping[str, RuntimeProviderAdapter],
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self.store = store
        self.providers = dict(provider) if isinstance(provider, Mapping) else {"fake": provider}
        self.journal = EventJournal(store, secret_values)
        self.instance_id = str(uuid4())
        self.recovered_sent_requests = self.store.recover_after_restart()
        self._claim_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._claim_locks_guard = asyncio.Lock()
        self._generation_turn_locks: dict[str, asyncio.Lock] = {}
        self._generation_turn_locks_guard = asyncio.Lock()
        self._arbiters: dict[tuple[str, str], _RequestArbiter] = {}
        self._cancellation_tasks: set[asyncio.Task[bool]] = set()
        self._shutting_down = False

    async def ensure_generation(self, binding_id: UUID, spec: GenerationSpec) -> GenerationResult:
        """Create or confirm state only; never acquire or reconcile a process."""
        if self._shutting_down:
            raise ConflictError("runtime is shutting down")
        binding = str(binding_id)
        provider = self._provider_for_kind(spec.runtime_kind)
        record, created = self.store.ensure_generation(binding, spec)
        if created or record.status == "starting":
            try:
                provider.stage_generation(binding, spec)
            except Exception:
                if created:
                    self.store.fail_generation(binding)
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
        payload_hash = self._request_hash(request)
        generation = self.store.get_generation(binding)
        self._provider_for_kind(generation.runtime_kind)
        existing = self.store.get_request(binding, str(request.request_id))
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise ConflictError("request identity is immutable")
            return
        if generation.status == "retired":
            raise RetiredError("generation is retired")
        if generation.status not in {"starting", "active"}:
            raise ConflictError("generation is not executable")
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
        generation = self.store.get_generation(binding)
        provider = self._provider_for_kind(generation.runtime_kind)
        request_lock = await self._get_claim_lock(binding, request_id)
        async with request_lock:
            record, disposition = self.store.claim_request(
                binding,
                request_id,
                self._request_hash(request),
                request.requested_model_id,
                request.requested_thinking_level,
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
                async for event in self._run_owned_turn(binding_id, request, provider, record):
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
        claimed: RequestRecord,
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
        arbiter = self._get_or_create_arbiter(binding, request_id)
        if not self._arbiter_open(arbiter):
            # A cancellation arbiter claimed this request while the owner was
            # waiting for its turn lock: produce no local effect and replay.
            async for event in self._replay_arbiter_outcome(arbiter, binding, request_id, 0):
                yield event
            return

        if current.resolution_status == "pending":
            try:
                resolution = provider.resolve_execution(
                    current.requested_model_id,
                    current.requested_thinking_level,
                )
            except ProviderAdapterError as exc:
                if exc.code != "unsupported_requested_execution":
                    raise
                if not self._arbiter_open(arbiter):
                    async for event in self._replay_arbiter_outcome(
                        arbiter, binding, request_id, 0
                    ):
                        yield event
                    return
                self.store.mark_resolution_unsupported(binding, request_id, self.instance_id)
                async for event in self._conclude_with_terminal(
                    arbiter,
                    binding,
                    request_id,
                    self._terminal_writer(
                        binding, request_id, "error", {"code": exc.code}, "failed", exc.code
                    ),
                    provider,
                    yielded_sequence=0,
                ):
                    yield event
                return
            current = self.store.freeze_resolution(
                binding,
                request_id,
                self.instance_id,
                resolution,
            )
        elif current.resolution_status == "unsupported":
            async for event in self._conclude_with_terminal(
                arbiter,
                binding,
                request_id,
                self._terminal_writer(
                    binding,
                    request_id,
                    "error",
                    {"code": "unsupported_requested_execution"},
                    "failed",
                    "unsupported_requested_execution",
                ),
                provider,
                yielded_sequence=0,
            ):
                yield event
            return
        resolution = self._resolution_from_record(current)

        generation = self.store.get_generation(binding)
        is_first_turn = not generation.bootstrap_sent
        try:
            self._validate_bootstrap_state(generation.bootstrap_sent, request)
        except ConflictError:
            async for event in self._conclude_with_terminal(
                arbiter,
                binding,
                request_id,
                self._terminal_writer(
                    binding,
                    request_id,
                    "error",
                    {"code": "bootstrap_state_conflict"},
                    "failed",
                    "bootstrap_state_conflict",
                ),
                provider,
                yielded_sequence=0,
            ):
                yield event
            return

        if not self._arbiter_open(arbiter):
            async for event in self._replay_arbiter_outcome(arbiter, binding, request_id, 0):
                yield event
            return
        try:
            acquired = await provider.prepare_turn(
                generation,
                request,
                resolution.process_options,
                is_first_turn=is_first_turn,
            )
        except asyncio.CancelledError:
            raise
        except ProviderAdapterError as exc:
            async for event in self._conclude_with_terminal(
                arbiter,
                binding,
                request_id,
                self._terminal_writer(
                    binding, request_id, "error", {"code": exc.code}, "failed", exc.code
                ),
                provider,
                yielded_sequence=0,
            ):
                yield event
            return
        except Exception:
            async for event in self._conclude_with_terminal(
                arbiter,
                binding,
                request_id,
                self._terminal_writer(
                    binding,
                    request_id,
                    "error",
                    {"code": "provider_prepare_exception"},
                    "failed",
                    "provider_prepare_exception",
                ),
                provider,
                yielded_sequence=0,
            ):
                yield event
            return
        # Post-provider-await re-observation (Plan CP4 §3.4.2): once a
        # cancellation arbiter claimed the request, the owner must not
        # activate, resolve, mark, or send anything else.
        if not self._arbiter_open(arbiter):
            async for event in self._replay_arbiter_outcome(arbiter, binding, request_id, 0):
                yield event
            return
        try:
            self._validate_acquired_generation(generation.provider_session_id, resolution, acquired)
            was_starting = generation.status == "starting"
            if was_starting:
                generation = self.store.activate_generation(
                    binding,
                    acquired.provider_session_id,
                    request_id,
                )
        except asyncio.CancelledError:
            raise
        except ProviderAdapterError as exc:
            async for event in self._conclude_with_terminal(
                arbiter,
                binding,
                request_id,
                self._terminal_writer(
                    binding, request_id, "error", {"code": exc.code}, "failed", exc.code
                ),
                provider,
                yielded_sequence=0,
            ):
                yield event
            return
        except Exception:
            async for event in self._conclude_with_terminal(
                arbiter,
                binding,
                request_id,
                self._terminal_writer(
                    binding,
                    request_id,
                    "error",
                    {"code": "provider_prepare_exception"},
                    "failed",
                    "provider_prepare_exception",
                ),
                provider,
                yielded_sequence=0,
            ):
                yield event
            return

        control_events: list[RuntimeEvent] = []
        generation = self.store.get_generation(binding)
        if generation.activation_request_id == request_id:
            control_events.append(
                self.store.append_control_event(
                    binding,
                    request_id,
                    "generation_activated",
                    {"provider_session_id": generation.provider_session_id},
                )
            )
        control_events.append(
            self.store.append_control_event(
                binding,
                request_id,
                "execution_resolved",
                {
                    "effective_provider_model_slug": resolution.provider_model_slug,
                    "effective_effort": resolution.effort,
                    "resolver_policy_revision": resolution.resolver_policy_revision,
                },
            )
        )
        for event in sorted(control_events, key=lambda item: item.sequence):
            yield event
        yielded_sequence = control_events[-1].sequence

        current = self.store.get_request(binding, request_id)
        if current is None:
            raise ConflictError("request disappeared")
        if current.terminal:
            for event in self.journal.replay(binding, request_id):
                if event.sequence > yielded_sequence:
                    yield event
            return
        if not self._arbiter_open(arbiter):
            async for event in self._replay_arbiter_outcome(
                arbiter, binding, request_id, yielded_sequence
            ):
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
            terminal = self._claim_natural_terminal(
                arbiter,
                self._terminal_writer(
                    binding,
                    request_id,
                    "error",
                    {"code": "send_boundary_conflict"},
                    "failed",
                    "send_boundary_conflict",
                ),
            )
            if terminal is None:
                async for event in self._replay_arbiter_outcome(
                    arbiter, binding, request_id, yielded_sequence
                ):
                    yield event
                return
            await self._cleanup_unsent_request(binding, request_id, provider)
            self._reclaim_request_safely(binding, request_id, provider)
            if terminal.sequence > yielded_sequence:
                yield terminal
            return
        async for event in self._stream_provider_events(
            binding_id,
            request,
            provider,
            arbiter,
            yielded_sequence,
        ):
            yield event

    async def _stream_provider_events(
        self,
        binding_id: UUID,
        request: TurnRequest,
        provider: RuntimeProviderAdapter,
        arbiter: _RequestArbiter,
        yielded_sequence: int,
    ) -> AsyncIterator[RuntimeEvent]:
        binding = str(binding_id)
        request_id = str(request.request_id)
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
                if not self._arbiter_open(arbiter):
                    # The cancellation arbiter owns the terminal; stop
                    # producing durable output and replay the winner.
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
            writer = self._terminal_writer(
                binding, request_id, "error", {"code": failure_code}, failure_status, failure_code
            )
        elif pending_terminal is None:
            writer = self._terminal_writer(
                binding,
                request_id,
                "error",
                {"code": "unexpected_provider_eof"},
                "indeterminate",
                "unexpected_provider_eof",
            )
        else:
            writer = lambda: self._persist_provider_terminal(  # noqa: E731
                binding, request_id, pending_terminal
            )
        async for event in self._conclude_with_terminal(
            arbiter,
            binding,
            request_id,
            writer,
            provider,
            yielded_sequence=yielded_sequence,
        ):
            yield event

    async def cancel(self, binding_id: UUID, request_id: UUID) -> CancelResult:
        """Single-owner cancel entry: durable terminal wins, otherwise arbitrate.

        Every cancel caller either (a) reads an already durable terminal and
        changes nothing, (b) becomes the one first canceller that registers a
        Runtime-owned settlement task, or (c) joins the in-flight settlement.
        The arbitration block below is await-free on purpose: shutdown's
        admission close + settlement snapshot cannot interleave with it, and
        no two callers can both become the first canceller.
        """

        binding = str(binding_id)
        request_key = str(request_id)
        generation = self.store.get_generation(binding)
        provider = self._provider_for_kind(generation.runtime_kind)
        record = self.store.get_request(binding, request_key)
        if record is None:
            raise CancelUnregisteredError("cancel arrived before durable registration")
        if record.terminal:
            return CancelResult(
                binding_id=binding_id,
                request_id=request_id,
                status=record.status,
                changed=False,
            )
        if self._shutting_down:
            raise ConflictError("runtime is shutting down")

        first = False
        task: asyncio.Task[bool] | None = None
        wait_for_release = False
        arbiter = self._get_or_create_arbiter(binding, request_key)
        with arbiter.lock:
            if arbiter.state is ArbiterState.OPEN:
                if self._shutting_down:
                    raise ConflictError("runtime is shutting down")
                arbiter.state = ArbiterState.CANCELLING
                task = asyncio.create_task(
                    self._settle_cancellation(binding, request_key, provider, arbiter)
                )
                self._cancellation_tasks.add(task)
                task.add_done_callback(self._cancellation_tasks.discard)
                arbiter.cancellation_task = task
                first = True
            else:
                task = arbiter.cancellation_task
                wait_for_release = task is None

        changed = False
        if task is not None:
            settlement_result = await asyncio.shield(task)
            changed = bool(first and settlement_result)
        elif wait_for_release:
            await arbiter.done_event.wait()
        if arbiter.failure is not None:
            raise ProviderAdapterError("cancel_settlement_failed") from arbiter.failure
        final = self.store.get_request(binding, request_key)
        if final is None or not final.terminal:
            raise ConflictError("cancel terminal truth is not durable")
        return CancelResult(
            binding_id=binding_id,
            request_id=request_id,
            status=final.status,
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
        """Close admission atomically, settle, then terminalize what remains.

        The admission close and the settlement snapshot are one await-free
        block, so a concurrent cancel registration is either snapshotted here
        or refused by ``_shutting_down``; there is no in-between state
        (Law 8). Settlement tasks are shielded and never cancelled: the
        cancellation task itself is the terminal-truth owner.
        """

        self._shutting_down = True
        settlement_tasks = tuple(self._cancellation_tasks)
        if settlement_tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in settlement_tasks),
                return_exceptions=True,
            )
        self.store.terminalize_open_requests_for_shutdown()
        unique = {id(provider): provider for provider in self.providers.values()}
        results = await asyncio.gather(
            *(provider.shutdown() for provider in unique.values()), return_exceptions=True
        )
        self._arbiters.clear()
        if any(isinstance(result, BaseException) for result in results):
            raise ProviderAdapterError("provider_shutdown_failed")

    async def _settle_cancellation(
        self,
        binding: str,
        request_id: str,
        provider: RuntimeProviderAdapter,
        arbiter: _RequestArbiter,
    ) -> bool:
        """Settle one cancelled request; the caller only shield-waits (Law 7).

        Returns whether this settlement is cancel-induced (``changed=True``
        for the first canceller). A provider cancel that raises can only
        produce an honest cancel-induced indeterminate; a receipt can only
        produce ``cancelled`` from a positive disposal proof, otherwise the
        adapter-certified natural terminal or ``cancel_ownership_unknown``.
        """

        changed = False
        failure: BaseException | None = None
        try:
            try:
                receipt = await provider.cancel(binding, request_id)
            except asyncio.CancelledError as exc:
                # Runtime-owned settlement tasks are never cancelled by
                # callers or graceful shutdown; a cancellation here is a
                # defect, journaled conservatively before propagating.
                self._journal_cancel_failure(binding, request_id, "cancel_settlement_cancelled")
                failure = exc
                raise
            except Exception:
                self._journal_cancel_failure(binding, request_id, "cancel_cleanup_failed")
                changed = True
            else:
                changed = self._persist_cancel_receipt(binding, request_id, receipt)
        except BaseException as exc:
            if failure is None:
                failure = exc
            raise
        finally:
            self._conclude_arbiter(arbiter, failure=failure)
            self._reclaim_request_safely(binding, request_id, provider)
        return changed

    def _persist_cancel_receipt(
        self,
        binding: str,
        request_id: str,
        receipt: ProviderCancelReceipt,
    ) -> bool:
        if receipt.outcome in {
            ProviderCancelOutcome.CANCELLED_PRESTART,
            ProviderCancelOutcome.CANCELLED_ACTIVE,
            ProviderCancelOutcome.CANCELLED_ABANDONED,
        }:
            self.journal.terminal(
                binding,
                request_id,
                "error",
                {"code": "cancelled"},
                "cancelled",
                "cancelled",
            )
            return True
        if receipt.outcome is ProviderCancelOutcome.NATURAL_TERMINAL_READY:
            natural = receipt.natural_terminal
            if natural is None:
                raise ProviderAdapterError("cancel_receipt_incomplete")
            self._persist_provider_terminal(binding, request_id, natural)
            return False
        self._journal_cancel_failure(binding, request_id, "cancel_ownership_unknown")
        return True

    def _journal_cancel_failure(self, binding: str, request_id: str, code: str) -> RuntimeEvent:
        terminal, _ = self.journal.terminal(
            binding,
            request_id,
            "error",
            {"code": code},
            "indeterminate",
            code,
        )
        return terminal

    def _persist_provider_terminal(
        self,
        binding: str,
        request_id: str,
        event: ProviderEvent,
    ) -> RuntimeEvent:
        """Shared bounded mapping for one provider terminal (normal or rescued)."""

        if event.event_type == "error":
            status = event.terminal_status or "failed"
            code = str(event.payload.get("code", "provider_error"))
            terminal, _ = self.journal.terminal(
                binding, request_id, "error", event.payload, status, code
            )
            return terminal
        if event.event_type == "done":
            terminal, _ = self.journal.terminal(
                binding, request_id, "done", event.payload, "completed", "completed"
            )
            return terminal
        raise ProviderAdapterError("provider_terminal_event_invalid")

    async def _conclude_with_terminal(
        self,
        arbiter: _RequestArbiter,
        binding: str,
        request_id: str,
        writer: Callable[[], RuntimeEvent],
        provider: RuntimeProviderAdapter,
        *,
        yielded_sequence: int,
    ) -> AsyncIterator[RuntimeEvent]:
        """Claim the natural-terminal slot or replay the arbitration winner."""

        terminal = self._claim_natural_terminal(arbiter, writer)
        if terminal is not None:
            self._reclaim_request_safely(binding, request_id, provider)
            if terminal.sequence > yielded_sequence:
                yield terminal
            return
        async for event in self._replay_arbiter_outcome(
            arbiter, binding, request_id, yielded_sequence
        ):
            yield event

    def _claim_natural_terminal(
        self,
        arbiter: _RequestArbiter,
        writer: Callable[[], RuntimeEvent],
    ) -> RuntimeEvent | None:
        """Atomically claim and persist the natural terminal, or return None.

        Returns ``None`` when a cancellation settlement already owns the
        terminal; the caller must wait for the released outcome and replay it.
        There is deliberately no await between the claim and the durable
        write, so a claimed slot can never be left pending by task
        cancellation (Plan CP4 §3.4.1).
        """

        with arbiter.lock:
            if arbiter.state is not ArbiterState.OPEN:
                return None
            arbiter.state = ArbiterState.NATURAL_TERMINAL_PENDING
        try:
            terminal = writer()
        except BaseException as exc:
            self._conclude_arbiter(arbiter, failure=exc)
            raise
        self._conclude_arbiter(arbiter)
        return terminal

    def _conclude_arbiter(
        self,
        arbiter: _RequestArbiter,
        failure: BaseException | None = None,
    ) -> None:
        with arbiter.lock:
            arbiter.state = ArbiterState.TERMINAL
            if failure is not None:
                arbiter.failure = failure
        arbiter.done_event.set()
        self._arbiters.pop((arbiter.binding_id, arbiter.request_id), None)

    async def _replay_arbiter_outcome(
        self,
        arbiter: _RequestArbiter,
        binding: str,
        request_id: str,
        yielded_sequence: int,
    ) -> AsyncIterator[RuntimeEvent]:
        """Wait for the released arbitration outcome, then replay the winner."""

        await arbiter.done_event.wait()
        if arbiter.failure is not None:
            raise ProviderAdapterError("terminal_arbitration_failed") from arbiter.failure
        for event in self.journal.replay(binding, request_id):
            if event.sequence > yielded_sequence:
                yield event

    def _get_or_create_arbiter(self, binding_id: str, request_id: str) -> _RequestArbiter:
        """Await-free registry access so stream and cancel share one arbiter."""

        key = (binding_id, request_id)
        arbiter = self._arbiters.get(key)
        if arbiter is None:
            arbiter = _RequestArbiter(binding_id, request_id)
            self._arbiters[key] = arbiter
        return arbiter

    @staticmethod
    def _arbiter_open(arbiter: _RequestArbiter) -> bool:
        with arbiter.lock:
            return arbiter.state is ArbiterState.OPEN

    def _reclaim_request_safely(
        self,
        binding: str,
        request_id: str,
        provider: RuntimeProviderAdapter,
    ) -> None:
        """Post-terminal provider-proof release; never rewrites terminal truth."""

        try:
            provider.reclaim_request(binding, request_id)
        except Exception:
            _LOGGER.warning("runtime provider request reclaim failed", exc_info=False)

    async def _cleanup_unsent_request(
        self,
        binding: str,
        request_id: str,
        provider: RuntimeProviderAdapter,
    ) -> None:
        """Best-effort fence/cleanup for an exact request that failed the send boundary."""

        try:
            await provider.cancel(binding, request_id)
        except BaseException:
            _LOGGER.warning("runtime unsent request cleanup failed", exc_info=False)

    def _terminal_writer(
        self,
        binding: str,
        request_id: str,
        event_type: str,
        payload: dict[str, object],
        status: str,
        terminal_code: str,
    ) -> Callable[[], RuntimeEvent]:
        """Build one synchronous terminal persist step for the arbiter gate."""

        journal = self.journal

        def write_terminal() -> RuntimeEvent:
            terminal, _ = journal.terminal(
                binding,
                request_id,
                event_type,
                payload,
                status,
                terminal_code,
            )
            return terminal

        return write_terminal

    def _provider_for_kind(self, runtime_kind: str) -> RuntimeProviderAdapter:
        provider = self.providers.get(runtime_kind)
        if provider is None:
            raise ConflictError("unsupported runtime kind")
        return provider

    @staticmethod
    def _resolution_from_record(record: RequestRecord) -> EffectiveResolution:
        if (
            record.resolution_status != "resolved"
            or record.effective_provider_model_slug is None
            or record.effective_effort is None
            or record.resolver_policy_revision is None
            or record.process_options is None
        ):
            raise ConflictError("request has no complete frozen execution")
        return EffectiveResolution(
            provider_model_slug=record.effective_provider_model_slug,
            effort=record.effective_effort,
            resolver_policy_revision=record.resolver_policy_revision,
            process_options=record.process_options,
        )

    @staticmethod
    def _validate_acquired_generation(expected_session_id, resolution, acquired) -> None:
        if (
            acquired.observed_model != resolution.provider_model_slug
            or acquired.observed_effort != resolution.effort
        ):
            raise ProviderProtocolError("provider observed execution mismatch")
        if expected_session_id is not None and acquired.provider_session_id != expected_session_id:
            raise ProviderAdapterError("resume_identity_mismatch")

    @staticmethod
    def _validate_bootstrap_state(bootstrap_sent: bool, request: TurnRequest) -> None:
        if bootstrap_sent and request.bootstrap_context is not None:
            raise ConflictError("generation bootstrap was already consumed")
        if not bootstrap_sent and request.bootstrap_context is None:
            raise ConflictError("first turn requires bootstrap context")

    async def _get_claim_lock(self, binding_id: str, request_id: str) -> asyncio.Lock:
        key = (binding_id, request_id)
        async with self._claim_locks_guard:
            return self._claim_locks.setdefault(key, asyncio.Lock())

    async def _get_generation_turn_lock(self, binding_id: str) -> asyncio.Lock:
        async with self._generation_turn_locks_guard:
            return self._generation_turn_locks.setdefault(binding_id, asyncio.Lock())

    async def _wait_and_replay(
        self, binding_id: str, request_id: str
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
        return canonical_turn_request_hash(request)
