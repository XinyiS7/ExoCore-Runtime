"""Provider protocol; adapters own capability/process details, never durable request truth."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
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


class ProviderCancelOutcome(str, Enum):
    """Provider-neutral physical proof attached to one explicit cancel.

    ``cancelled`` may only be inferred from one of the three positive
    disposal proofs; a session that is simply missing, a request that is no
    longer current, or an uncertain cleanup must all fall through to
    ``OWNERSHIP_UNKNOWN``. ``NATURAL_TERMINAL_READY`` carries a provider
    adapter-certified terminal event (never a raw process candidate).
    """

    CANCELLED_PRESTART = "cancelled_prestart"
    CANCELLED_ACTIVE = "cancelled_active"
    CANCELLED_ABANDONED = "cancelled_abandoned"
    NATURAL_TERMINAL_READY = "natural_terminal_ready"
    OWNERSHIP_UNKNOWN = "ownership_unknown"


@dataclass(frozen=True)
class ProviderCancelReceipt:
    """One cancel receipt; identity is carried by the call arguments.

    ``NATURAL_TERMINAL_READY`` must carry exactly one adapter-certified
    terminal ``ProviderEvent`` (normalized ``done``/``error``). Every other
    outcome must not carry one.
    """

    outcome: ProviderCancelOutcome
    natural_terminal: ProviderEvent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ProviderCancelOutcome):
            raise ValueError("cancel receipt outcome must be a ProviderCancelOutcome")
        if self.outcome is ProviderCancelOutcome.NATURAL_TERMINAL_READY:
            if (
                self.natural_terminal is None
                or self.natural_terminal.event_type not in {"done", "error"}
            ):
                raise ValueError(
                    "natural terminal receipt requires a normalized terminal event"
                )
        elif self.natural_terminal is not None:
            raise ValueError("only a natural terminal receipt may carry an event")


class RuntimeProviderAdapter(Protocol):
    def resolve_execution(
        self,
        requested_model_id: str,
        requested_thinking_level: str,
    ) -> EffectiveResolution: ...

    def stage_generation(self, binding_id: str, spec: GenerationSpec) -> None: ...

    async def stage_attachment(
        self,
        binding_id: str,
        request_id: str,
        artifact_id: str,
        data: bytes,
        *,
        guard: Callable[[], None],
    ) -> None: ...

    async def discard_attachments(
        self,
        binding_id: str,
        request_id: str,
        *,
        guard: Callable[[], None],
    ) -> None: ...

    async def stage_inspection(
        self,
        binding_id: str,
        request_id: str,
        inspection_id: str,
        mime_type: str,
        data: bytes,
        *,
        guard: Callable[[], None],
    ) -> Path:
        """Stage one mid-turn inspection file; return its absolute path."""
        ...

    async def discard_inspections(self, binding_id: str, request_id: str) -> None:
        """Idempotently remove the request's inspections after its terminal.

        Awaited by the service before ``reclaim_request`` on every terminal
        path; it may take the provider artifact lock and touch the filesystem,
        which ``reclaim_request`` must not.
        """
        ...

    async def read_generated_artifact(
        self,
        binding_id: str,
        artifact_ref: str,
    ) -> tuple[dict, bytes]: ...

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

    async def cancel(self, binding_id: str, request_id: str) -> ProviderCancelReceipt: ...

    def reclaim_request(self, binding_id: str, request_id: str) -> None:
        """Local, synchronous, idempotent release of one request proof.

        Called only after the Runtime request reached a durable terminal and
        its waiters were released. Must not perform external I/O; a failure
        here is an implementation defect and never rewrites terminal truth.
        """
        ...

    async def retire(self, binding_id: str, reason: str) -> None: ...

    async def shutdown(self) -> None: ...
