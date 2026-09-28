"""Official AGY adapter over generation-private process and mailbox artifacts.

改动指南（Change Guide）

- 扩展点：provider 侧附件行为分布在三处——`attachments.AttachmentStore`（stage/materialize/discard 与上限）、本模块的 `stage_attachment` / `discard_attachments` / `prepare_turn`（materialize + 渲染注入）、`renderer.render_stdin_line(..., attachment_paths=...)`；HTTP 面在 `api.py` 的 PUT/DELETE turn-attachments 路由；契约与上限常量在 `contracts.py`（`turn_attachments` capability、AttachmentManifest、MAX_ATTACHMENT_*）。
- 语义口径：staging 为 request 作用域，`request_registered` 之后 PUT/DELETE 必须拒绝且不动 bytes；materialize 崩溃幂等且只投影 Runtime 私有绝对路径（canonical 路径/文件名不得进入 envelope）；discard 幂等；共享 artifact lock 串行化 stage/discard/materialize。监督进程 env 拒绝七个 Google/Gemini auth 名（real 值 fail-closed，空值放行；见 `process.py::_isolated_environment` 的预检）。
- 测试注意：合约/store 单测 `tests/unit/test_attachment_contracts.py`、`test_attachment_store.py`；claim 竞态与中断上传证据 `tests/integration/test_antigravity_adapter.py`、`test_http_api.py`；HTTP 语义 `tests/integration/test_v2_http_api.py`。
- 落盘/边界：所有 staging/final/temp 产物只存在于该 generation 的 provider data root 内；retire 只删除 generation 根；不触碰官方 keyring 与 ExoCore canonical 数据。
- 关联：ExoCore 编排 Plan（已归档）`../ExoCore/Plan/Archived/AGY_Runtime_Current_Turn_Attachments_Plan.md`；X1 真实证据见 `../ExoCore/Plan/AGY_Runtime_Current_Turn_Attachments_acceptance_report.md`。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from uuid import uuid4

from exocore_runtime.contracts import (
    EffectiveResolution,
    GenerationSpec,
    ProcessExecutionOptions,
    ProviderEvent,
    RuntimeMcpTool,
    ProviderGeneration,
    TurnRequest,
    generation_identity,
    generation_identity_parts,
    project_rules_absent_digest,
    project_rules_identity_digest,
    runtime_mcp_manifest_sha256,
    system_instructions_sha256,
)
from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.attachments import AttachmentStore
from exocore_runtime.providers.antigravity.capabilities import (
    LAUNCH_ENVIRONMENT_REVISION,
    SECURITY_POLICY_REVISION,
    resolve_execution,
)
from exocore_runtime.providers.antigravity.control import (
    PROJECT_RULES_ARTIFACT,
    RESERVED_CONTROL_ARTIFACTS,
    CanonicalControlStore,
    ReservedControlArtifact,
)
from exocore_runtime.providers.antigravity.ephemeral_hook import EphemeralMailbox
from exocore_runtime.providers.antigravity.process import (
    AgyProcessSupervisor,
    GenerationLayout,
)
from exocore_runtime.providers.antigravity.renderer import (
    AGENT_TOOLS,
    ALLOW_POLICY,
    DENY_POLICY,
    LEGACY_AGENT_TOOLSETS,
    MCP_SERVER_NAME,
    extract_rendered_system_instructions,
    generation_agent_name,
    render_agent_markdown,
    render_stdin_line,
)
from exocore_runtime.providers.base import (
    ProviderCancelOutcome,
    ProviderCancelReceipt,
)
from exocore_runtime.state_store import GenerationRecord


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AgentDeclarationUpgrade:
    """One authenticated legacy -> current agent declaration transition."""

    generation_id: str
    legacy_shape: str
    previous_hash: str
    current_hash: str


def _declaration_shape_label(tools: tuple[str, ...] | None) -> str:
    """Stable label for a declaration shape in the upgrade log line."""

    if tools is None:
        return "pre-cp2-no-tools-block"
    return "tools:" + ",".join(tools)


@dataclass
class _RequestState:
    """Adapter-private state for one exact prepared request.

    The mailbox receipt validation is the provider-terminal certification
    boundary: the process-level result candidate is only an input to it. The
    state is retained from prepare until the Runtime reclaims the request
    after its durable terminal, so a certified proof can never be lost when
    the owner stream returns or disappears.
    """

    request_id: str
    mailbox: EphemeralMailbox
    payload_hash: str
    stdin_line: bytes
    certification_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    certified_terminal: ProviderEvent | None = None
    mailbox_cleaned: bool = False


class AntigravityAdapter:
    def __init__(
        self,
        data_root: Path,
        supervisor: AgyProcessSupervisor,
        *,
        memory_mcp_root: Path,
        mailbox_ttl_seconds: float = 120.0,
        reserved_artifacts: tuple[ReservedControlArtifact, ...] = RESERVED_CONTROL_ARTIFACTS,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.supervisor = supervisor
        self.memory_mcp_root = Path(memory_mcp_root).resolve()
        memory_server_marker = (
            self.memory_mcp_root / "engines" / "mcp" / "servers" / "memory" / "server.py"
        )
        if not self.memory_mcp_root.is_dir() or not memory_server_marker.is_file():
            raise ValueError(
                "memory MCP root must contain engines/mcp/servers/memory/server.py"
            )
        self.memory_mcp_python = str(Path(sys.executable).resolve())
        self.mailbox_ttl_seconds = mailbox_ttl_seconds
        self.reserved_artifacts = tuple(reserved_artifacts)
        self._requests: dict[tuple[str, str], _RequestState] = {}
        self._artifact_locks: dict[str, asyncio.Lock] = {}
        self._artifact_locks_guard = asyncio.Lock()
        self._shutting_down = False
        self._cleanup_stale_mailboxes()

    def resolve_execution(
        self,
        requested_model_id: str,
        requested_thinking_level: str,
    ) -> EffectiveResolution:
        return resolve_execution(requested_model_id, requested_thinking_level)

    def stage_generation(self, binding_id: str, spec: GenerationSpec) -> None:
        if spec.runtime_kind != "antigravity":
            raise ProviderAdapterError("agy_generation_spec_invalid", fatal_generation=True)
        self._ensure_generation_artifacts(binding_id, spec)

    async def stage_attachment(
        self,
        binding_id: str,
        request_id: str,
        artifact_id: str,
        data: bytes,
        *,
        guard: Callable[[], None],
    ) -> None:
        lock = await self._artifact_lock(binding_id)
        async with lock:
            guard()
            AttachmentStore(self._generation_root(binding_id)).stage(
                request_id,
                artifact_id,
                data,
            )

    async def discard_attachments(
        self,
        binding_id: str,
        request_id: str,
        *,
        guard: Callable[[], None],
    ) -> None:
        lock = await self._artifact_lock(binding_id)
        async with lock:
            guard()
            AttachmentStore(self._generation_root(binding_id)).discard(request_id)

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
        mailbox: EphemeralMailbox | None = None
        acquired: ProviderGeneration | None = None
        try:
            lock = await self._artifact_lock(binding_id)
            async with lock:
                if self._shutting_down:
                    raise ProviderAdapterError("agy_adapter_shutting_down", fatal_generation=True)
                self._validate_mcp_manifest(request.runtime_mcp_tools, options)
                layout = self._load_layout(
                    generation,
                    options,
                    request.runtime_mcp_tools,
                )
                attachment_paths = AttachmentStore(
                    self._generation_root(binding_id)
                ).materialize(request_id, request.attachments)
                acquired = await self.supervisor.ensure(layout)
                self._update_provider_session(binding_id, acquired.provider_session_id)
                mailbox = self._mailbox(binding_id)
                payload_hash = mailbox.prepare(request_id, request.ephemeral_current)
                stdin_line = render_stdin_line(
                    request,
                    is_first_turn=is_first_turn,
                    attachment_paths={
                        artifact_id: str(path)
                        for artifact_id, path in attachment_paths.items()
                    },
                )
                self._requests[(binding_id, request_id)] = _RequestState(
                    request_id=request_id,
                    mailbox=mailbox,
                    payload_hash=payload_hash,
                    stdin_line=stdin_line,
                )
        except asyncio.CancelledError as original_error:
            try:
                self._cleanup_presend_payloads(binding_id, request_id, mailbox)
            except OSError:
                original_error.add_note("pre-send mailbox cleanup also failed")
            raise
        except ProviderAdapterError as original_error:
            try:
                self._cleanup_presend_payloads(binding_id, request_id, mailbox)
            except OSError as cleanup_error:
                failure = ProviderAdapterError(
                    "ephemeral_presend_cleanup_failed",
                    fatal_generation=True,
                )
                try:
                    await self.supervisor.close_binding(binding_id, force=True)
                except BaseException:
                    failure.add_note("process cleanup also failed")
                raise failure from cleanup_error
            if original_error.fatal_generation:
                try:
                    await self.supervisor.close_binding(binding_id, force=True)
                except BaseException:
                    original_error.add_note("process cleanup also failed")
            raise
        except Exception as exc:
            try:
                self._cleanup_presend_payloads(binding_id, request_id, mailbox)
            except OSError as cleanup_error:
                failure = ProviderAdapterError(
                    "ephemeral_presend_cleanup_failed",
                    fatal_generation=True,
                )
                try:
                    await self.supervisor.close_binding(binding_id, force=True)
                except BaseException:
                    failure.add_note("process cleanup also failed")
                raise failure from cleanup_error
            failure = ProviderAdapterError("agy_turn_prepare_failed", fatal_generation=True)
            try:
                await self.supervisor.close_binding(binding_id, force=True)
            except BaseException:
                failure.add_note("process cleanup also failed")
            raise failure from exc
        if acquired is None:
            raise ProviderAdapterError("agy_process_not_ready")
        return acquired

    async def stream_turn(
        self,
        binding_id: str,
        request: TurnRequest,
    ) -> AsyncIterator[ProviderEvent]:
        request_id = str(request.request_id)
        state = self._requests.get((binding_id, request_id))
        if state is None:
            raise ProviderAdapterError(
                "agy_turn_not_prepared",
                terminal_status="indeterminate",
            )
        deferred_usage: list[ProviderEvent] = []
        original_failure: BaseException | None = None
        try:
            async for event in self.supervisor.stream_turn(
                binding_id, request_id, state.stdin_line
            ):
                if event.event_type == "usage":
                    deferred_usage.append(event)
                    continue
                if event.event_type in {"done", "error"}:
                    certified = await self._certify_request(binding_id, request_id, event)
                    for usage in deferred_usage:
                        yield usage
                    yield certified
                    return
                yield event
        except asyncio.CancelledError as exc:
            # The supervisor's own cancellation handler already force-disposed
            # the exact request (or skipped it for a certified candidate); the
            # Runtime arbiter settlement reads that proof afterwards.
            original_failure = exc
            raise
        except ProviderAdapterError as exc:
            original_failure = exc
            if exc.fatal_generation:
                try:
                    await self.supervisor.close_binding(binding_id, force=True)
                except BaseException:
                    exc.add_note("process cleanup also failed")
            raise
        except BaseException as exc:
            original_failure = exc
            raise
        finally:
            self._finalize_request_stream(binding_id, request_id, original_failure)

    async def cancel(self, binding_id: str, request_id: str) -> ProviderCancelReceipt:
        """Explicit cancel classification under the artifact->binding lock order.

        Only one positive physical proof may produce ``CANCELLED_*``; a
        process result candidate is always routed through adapter
        certification, and everything else falls through to
        ``OWNERSHIP_UNKNOWN``. An abandoned-owner cleanup is an exact
        request-scoped settlement: while it is in flight this request has
        neither a session claim to classify nor a published receipt, so the
        classification joins that same cleanup instead of reading a
        half-built disposal state (RT-RACE-1).
        """

        lock = await self._artifact_lock(binding_id)
        async with lock:
            state = self._requests.get((binding_id, request_id))
            if state is not None and state.certified_terminal is not None:
                return ProviderCancelReceipt(
                    ProviderCancelOutcome.NATURAL_TERMINAL_READY,
                    state.certified_terminal,
                )
            candidate = self.supervisor.request_candidate(binding_id, request_id)
            if candidate is not None:
                certified = await self._certify_request(binding_id, request_id, candidate)
                return ProviderCancelReceipt(
                    ProviderCancelOutcome.NATURAL_TERMINAL_READY,
                    certified,
                )
            abandoned = await self.supervisor.settle_abandonment(binding_id, request_id)
            if abandoned == "disposed":
                return ProviderCancelReceipt(ProviderCancelOutcome.CANCELLED_ABANDONED)
            outcome = await self.supervisor.dispose_request(binding_id, request_id)
            if state is not None:
                self._cleanup_state_mailbox(state)
            if outcome == "active":
                return ProviderCancelReceipt(ProviderCancelOutcome.CANCELLED_ACTIVE)
            if outcome == "prestart" and state is not None:
                # Fence the exact prepared request: a stale owner can no
                # longer claim it (the process was force-disposed) and a new
                # stream entry can no longer find its prepared state.
                self._requests.pop((binding_id, request_id), None)
                return ProviderCancelReceipt(ProviderCancelOutcome.CANCELLED_PRESTART)
            # The serialized disposal found no exact claim to dispose, but the
            # abandoned-owner cleanup for this same request may have completed
            # while this call waited for the binding lock. Its immutable proof
            # decides, never an already emptied session registry (RT-RACE-1).
            abandoned = await self.supervisor.settle_abandonment(binding_id, request_id)
            if abandoned == "disposed":
                return ProviderCancelReceipt(ProviderCancelOutcome.CANCELLED_ABANDONED)
            if state is not None:
                self._requests.pop((binding_id, request_id), None)
            return ProviderCancelReceipt(ProviderCancelOutcome.OWNERSHIP_UNKNOWN)

    def reclaim_request(self, binding_id: str, request_id: str) -> None:
        """Local synchronous idempotent release after the durable terminal."""

        self._requests.pop((binding_id, request_id), None)
        self.supervisor.discard_request_proofs(binding_id, request_id)

    async def retire(self, binding_id: str, reason: str) -> None:
        lock = await self._artifact_lock(binding_id)
        async with lock:
            await self.supervisor.close_binding(binding_id, force=False)
            for key in [key for key in self._requests if key[0] == binding_id]:
                self._requests.pop(key, None)
            self.supervisor.discard_binding_proofs(binding_id)
            root = self._generation_root(binding_id)
            if root.exists():
                try:
                    shutil.rmtree(root)
                except OSError as exc:
                    raise ProviderAdapterError("agy_artifact_cleanup_failed") from exc

    async def shutdown(self) -> None:
        self._shutting_down = True
        async with self._artifact_locks_guard:
            locks = tuple(self._artifact_locks.values())
        for lock in locks:
            async with lock:
                pass
        supervisor_failure: BaseException | None = None
        try:
            await self.supervisor.shutdown()
        except BaseException as exc:
            supervisor_failure = exc
        mailbox_failures: list[OSError] = []
        for state in tuple(self._requests.values()):
            if state.mailbox_cleaned:
                continue
            try:
                state.mailbox.cleanup_request()
                state.mailbox_cleaned = True
            except OSError as exc:
                mailbox_failures.append(exc)
        self._requests.clear()
        if supervisor_failure is not None:
            if mailbox_failures:
                supervisor_failure.add_note("ephemeral mailbox cleanup also failed")
            raise supervisor_failure
        if mailbox_failures:
            raise ProviderAdapterError("ephemeral_cleanup_failed") from mailbox_failures[0]

    async def _certify_request(
        self,
        binding_id: str,
        request_id: str,
        candidate: ProviderEvent | None,
    ) -> ProviderEvent:
        """Single-flight idempotent provider-terminal certification.

        A process result candidate is not provider terminal truth: only the
        exact mailbox receipt validation makes an immutable certified
        terminal, with the same cleanup/fatal mapping for the owner stream and
        a late-cancel rescue. Whoever completes first wins; the other side
        reuses the stored result and never re-runs validation/cleanup.
        """

        state = self._requests.get((binding_id, request_id))
        if state is None:
            raise ProviderAdapterError(
                "agy_turn_not_prepared",
                terminal_status="indeterminate",
            )
        async with state.certification_lock:
            if state.certified_terminal is not None:
                return state.certified_terminal
            failure: ProviderAdapterError | None = None
            try:
                state.mailbox.validate_receipt(request_id, state.payload_hash)
            except ProviderAdapterError as exc:
                failure = exc
            if failure is not None:
                certified = self._certification_failure_event(failure)
                if failure.fatal_generation:
                    try:
                        await self.supervisor.close_binding(binding_id, force=True)
                    except BaseException:
                        failure.add_note("process cleanup also failed")
            else:
                if candidate is None:
                    candidate = self.supervisor.request_candidate(binding_id, request_id)
                if candidate is None:
                    raise ProviderAdapterError(
                        "agy_terminal_candidate_missing",
                        terminal_status="indeterminate",
                    )
                certified = candidate
            try:
                state.mailbox.cleanup_request()
                state.mailbox_cleaned = True
            except OSError as cleanup_error:
                if failure is None:
                    raise ProviderAdapterError("ephemeral_cleanup_failed") from cleanup_error
                failure.add_note("ephemeral mailbox cleanup also failed")
            state.certified_terminal = certified
            return certified

    @staticmethod
    def _certification_failure_event(failure: ProviderAdapterError) -> ProviderEvent:
        """Normalize one certification failure for both owner and rescue paths."""

        return ProviderEvent(
            event_type="error",
            payload={"code": failure.code},
            terminal_status=failure.terminal_status,
        )

    def _finalize_request_stream(
        self,
        binding_id: str,
        request_id: str,
        original_failure: BaseException | None,
    ) -> None:
        """Safety-net mailbox cleanup; the request proof stays until reclaim."""

        state = self._requests.get((binding_id, request_id))
        if state is None or state.mailbox_cleaned:
            return
        try:
            state.mailbox.cleanup_request()
            state.mailbox_cleaned = True
        except OSError as cleanup_error:
            if original_failure is None:
                raise ProviderAdapterError("ephemeral_cleanup_failed") from cleanup_error
            original_failure.add_note("ephemeral mailbox cleanup also failed")

    @staticmethod
    def _cleanup_state_mailbox(state: _RequestState) -> None:
        if state.mailbox_cleaned:
            return
        try:
            state.mailbox.cleanup_request()
            state.mailbox_cleaned = True
        except OSError as cleanup_error:
            raise ProviderAdapterError("ephemeral_cleanup_failed") from cleanup_error

    async def _artifact_lock(self, binding_id: str) -> asyncio.Lock:
        async with self._artifact_locks_guard:
            return self._artifact_locks.setdefault(binding_id, asyncio.Lock())

    def _cleanup_presend_payloads(
        self,
        binding_id: str,
        request_id: str,
        mailbox: EphemeralMailbox | None,
    ) -> None:
        if mailbox is not None:
            mailbox.cleanup_request()
        else:
            EphemeralMailbox.cleanup_payload_files(
                self._generation_root(binding_id) / "mailbox"
            )
        try:
            AttachmentStore(self._generation_root(binding_id)).prune(request_id)
        except Exception as exc:
            raise OSError("attachment pre-send cleanup failed") from exc

    def _ensure_generation_artifacts(
        self,
        binding_id: str,
        spec: GenerationSpec,
    ) -> None:
        root = self._generation_root(binding_id)
        metadata_path = root / "generation.json"
        agent_name = generation_agent_name(binding_id)
        agent_markdown = render_agent_markdown(
            agent_name,
            spec.system_instructions,
            spec.project_rules,
        )
        agent_hash = hashlib.sha256(agent_markdown.encode("utf-8")).hexdigest()
        identity_hash = generation_identity(spec)
        expected = {
            "schema_version": "v2",
            "binding_id": binding_id,
            "runtime_kind": "antigravity",
            "bootstrap_fingerprint": spec.bootstrap_fingerprint,
            "system_instructions_sha256": system_instructions_sha256(
                spec.system_instructions
            ),
            "agent_name": agent_name,
            "generation_id": self._generation_id(binding_id, identity_hash),
            "identity_hash": identity_hash,
            "agent_markdown_sha256": agent_hash,
            "project_rules_present": spec.project_rules is not None,
        }
        rules_digest = project_rules_identity_digest(spec.project_rules)
        if rules_digest is not None:
            expected["project_rules_sha256"] = rules_digest
        if metadata_path.exists():
            metadata, _rules_normalized = self._normalized_legacy_rules_metadata(
                root,
                self._read_json(metadata_path),
                binding_id=binding_id,
                runtime_kind="antigravity",
                bootstrap_fingerprint=spec.bootstrap_fingerprint,
                system_instructions_digest=system_instructions_sha256(
                    spec.system_instructions
                ),
                expected_identity_hash=identity_hash,
            )
            upgrade = self._normalize_staged_legacy_declaration(
                root,
                metadata,
                expected,
                spec=spec,
                agent_name=agent_name,
            )
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)
        else:
            metadata = {**expected, "provider_session_id": None}
            upgrade = None
        self._restore_security_artifacts(root, metadata, agent_markdown, None)
        if spec.project_rules is not None:
            # The canonical body is the only durable store of the rules; the
            # workspace mirror is healed from it on every prepare.
            CanonicalControlStore(root).write_canonical(
                PROJECT_RULES_ARTIFACT.canonical_name,
                spec.project_rules,
            )
        self._atomic_write_json(metadata_path, metadata)
        if upgrade is not None:
            # The success line is emission-gated on the declaration and metadata
            # writes having completed, so a torn upgrade stays unreported.
            self._log_declaration_upgrade(binding_id, upgrade)

    def _load_layout(
        self,
        generation: GenerationRecord,
        options: ProcessExecutionOptions,
        mcp_manifest: tuple[RuntimeMcpTool, ...],
    ) -> GenerationLayout:
        """Authenticate the generation's artifacts, then commit its upgrades.

        Authentication is pure: the metadata record, the provider session, the
        declaration and the rules axis are all proven before the first write,
        so a fatal leaves ``generation.json`` and ``agent.md`` byte-unchanged and
        no upgrade can be persisted on a state that the final verification would
        reject. The final ``_verify_security_artifacts`` remains the authority;
        the early checks are its authentication inputs, evaluated ahead of the
        commit phase.
        """

        self._validate_process_options(options)
        self._validate_mcp_manifest(mcp_manifest, options)
        root = self._generation_root(generation.binding_id)
        metadata_path = root / "generation.json"
        if metadata_path.exists():
            metadata, rules_normalized = self._normalized_legacy_rules_metadata(
                root,
                self._read_json(metadata_path),
                binding_id=generation.binding_id,
                runtime_kind=generation.runtime_kind,
                bootstrap_fingerprint=generation.bootstrap_fingerprint,
                system_instructions_digest=generation.system_instructions_sha256,
                expected_identity_hash=generation.identity_hash,
            )
        else:
            metadata = self._rebuild_missing_metadata(root, generation)
            rules_normalized = False
        self._require_matching_identity_fields(generation, metadata)
        artifact_session = metadata.get("provider_session_id")
        if artifact_session is not None and not isinstance(artifact_session, str):
            raise ProviderAdapterError("agy_generation_artifact_invalid", fatal_generation=True)
        if (
            artifact_session is not None
            and generation.provider_session_id is not None
            and artifact_session != generation.provider_session_id
        ):
            raise ProviderAdapterError("agy_artifact_session_conflict", fatal_generation=True)
        agent_markdown, upgrade = self._resolve_agent_markdown(root, metadata, generation)
        self._require_authenticated_rules_identity(root, generation, metadata)
        backfill_session = (
            artifact_session is None and generation.provider_session_id is not None
        )
        if backfill_session:
            metadata["provider_session_id"] = generation.provider_session_id
        if upgrade is not None:
            metadata["agent_markdown_sha256"] = upgrade.current_hash
        if rules_normalized or backfill_session or upgrade is not None:
            self._atomic_write_json(metadata_path, metadata)
        self._restore_security_artifacts(root, metadata, agent_markdown, mcp_manifest)
        self._restore_reserved_control_artifacts(root, metadata)
        self._verify_security_artifacts(root, generation, metadata, mcp_manifest)
        if upgrade is not None:
            # Only after the declaration, its recorded hash and the final
            # security-artifact verification have all converged is the upgrade
            # allowed to be reported as successful.
            self._log_declaration_upgrade(generation.binding_id, upgrade)
        return self._layout_from_metadata(root, metadata, options, mcp_manifest)

    def _rebuild_missing_metadata(
        self,
        root: Path,
        generation: GenerationRecord,
    ) -> dict[str, object]:
        agent_name = generation_agent_name(generation.binding_id)
        agent_path = (
            root / "profile" / ".gemini" / "config" / "agents" / agent_name / "agent.md"
        )
        # The canonical backing is the durable project-rules store; a genuinely
        # absent file means the generation was created without rules. Recovery
        # then recomputes the generation identity from the durable record fields
        # plus that presence/content and fails closed on any mismatch.
        project_rules = CanonicalControlStore(root).read_canonical_if_present(
            PROJECT_RULES_ARTIFACT.canonical_name
        )
        rules_digest = project_rules_identity_digest(project_rules)
        try:
            agent_markdown = agent_path.read_text(encoding="utf-8")
            rendered = extract_rendered_system_instructions(
                agent_name,
                agent_markdown,
                project_rules=project_rules,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderAdapterError("agy_generation_artifact_missing", fatal_generation=True) from exc
        if hashlib.sha256(rendered.encode("utf-8")).hexdigest() != generation.system_instructions_sha256:
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True)
        if (
            generation_identity_parts(
                runtime_kind=generation.runtime_kind,
                bootstrap_fingerprint=generation.bootstrap_fingerprint,
                system_instructions_digest=generation.system_instructions_sha256,
                project_rules_digest=rules_digest,
            )
            != generation.identity_hash
        ):
            raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)
        metadata = {
            "schema_version": "v2",
            "binding_id": generation.binding_id,
            "runtime_kind": generation.runtime_kind,
            "bootstrap_fingerprint": generation.bootstrap_fingerprint,
            "system_instructions_sha256": generation.system_instructions_sha256,
            "agent_name": agent_name,
            "generation_id": self._generation_id(
                generation.binding_id,
                generation.identity_hash,
            ),
            "identity_hash": generation.identity_hash,
            "agent_markdown_sha256": hashlib.sha256(
                agent_markdown.encode("utf-8")
            ).hexdigest(),
            "project_rules_present": project_rules is not None,
            "provider_session_id": generation.provider_session_id,
        }
        if rules_digest is not None:
            metadata["project_rules_sha256"] = rules_digest
        self._atomic_write_json(root / "generation.json", metadata)
        return metadata

    def _normalized_legacy_rules_metadata(
        self,
        root: Path,
        metadata: dict[str, object],
        *,
        binding_id: str,
        runtime_kind: str,
        bootstrap_fingerprint: str,
        system_instructions_digest: str,
        expected_identity_hash: str,
    ) -> tuple[dict[str, object], bool]:
        """Compute the pre-CP3 (rules-free) metadata upgrade without writing it.

        A generation created before project rules existed carries no rules keys,
        no canonical rules backing, and a durable identity that is exactly the
        rules-free identity. Missing keys then mean "absent" and the returned
        dict is the deterministic CP3 shape without rotating identity; the second
        element reports that it differs from the on-disk record so the caller can
        persist it once every other authentication step has passed.

        Every other missing-key situation fails closed: a rules-present
        generation must never silently degrade into a rules-free one (R1-03).
        """

        if "project_rules_present" in metadata or "project_rules_sha256" in metadata:
            return metadata, False
        legacy_identity = generation_identity_parts(
            runtime_kind=runtime_kind,
            bootstrap_fingerprint=bootstrap_fingerprint,
            system_instructions_digest=system_instructions_digest,
            project_rules_digest=None,
        )
        has_canonical_rules = (
            CanonicalControlStore(root).read_canonical_if_present(
                PROJECT_RULES_ARTIFACT.canonical_name
            )
            is not None
        )
        if (
            legacy_identity != expected_identity_hash
            or metadata.get("identity_hash") != expected_identity_hash
            or metadata.get("binding_id") != binding_id
            or has_canonical_rules
        ):
            raise ProviderAdapterError(
                "agy_generation_artifact_invalid", fatal_generation=True
            )
        return {**metadata, "project_rules_present": False}, True

    def _resolve_agent_markdown(
        self,
        root: Path,
        metadata: dict[str, object],
        generation: GenerationRecord,
    ) -> tuple[str, _AgentDeclarationUpgrade | None]:
        """Return this generation's declaration, upgrading a known legacy shape.

        The current policy shape is returned byte-identically and writes
        nothing. Any other file is only considered as a legacy declaration when
        it is provably the untouched artifact recorded in ``generation.json``
        (its bytes hash to the durable ``agent_markdown_sha256``) and matches one
        of the frozen historical declarations carrying the generation's own
        instructions and rules. Everything else keeps failing closed exactly as
        before, so an arbitrary edit cannot masquerade as a policy upgrade.
        """

        agent_name = str(metadata.get("agent_name", ""))
        path = root / "profile" / ".gemini" / "config" / "agents" / agent_name / "agent.md"
        project_rules = self._canonical_project_rules(root, metadata)
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True) from exc
        if self._declaration_instructions(content, agent_name, AGENT_TOOLS, project_rules) is not None:
            return content, None
        self._require_matching_identity_fields(generation, metadata)
        durable_hash = metadata.get("agent_markdown_sha256")
        if not isinstance(durable_hash, str) or hashlib.sha256(raw).hexdigest() != durable_hash:
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True)
        return self._upgrade_legacy_declaration(
            root,
            content,
            metadata,
            agent_name=agent_name,
            project_rules=project_rules,
            generation=generation,
        )

    def _upgrade_legacy_declaration(
        self,
        root: Path,
        content: str,
        metadata: dict[str, object],
        *,
        agent_name: str,
        project_rules: str | None,
        generation: GenerationRecord,
    ) -> tuple[str, _AgentDeclarationUpgrade]:
        """Re-render a recognized legacy declaration onto the current policy.

        Only the Runtime-owned declaration is re-rendered: the instructions body
        and the project rules are taken from the verified file itself, so they
        still have to satisfy their own integrity anchors. Nothing is written
        here; the caller records the returned hash and materializes the returned
        declaration once every authentication step has passed, so a fatal never
        leaves a half-upgraded generation behind.
        """

        for legacy_tools in LEGACY_AGENT_TOOLSETS:
            if legacy_tools is None and (
                project_rules is not None or self._has_canonical_project_rules(root)
            ):
                # The pre-CP2 declaration never shipped alongside project rules;
                # accepting that combination would frame-match a file the
                # runtime never produced.
                continue
            instructions = self._declaration_instructions(
                content, agent_name, legacy_tools, project_rules
            )
            if instructions is None:
                continue
            if (
                hashlib.sha256(instructions.encode("utf-8")).hexdigest()
                != generation.system_instructions_sha256
            ):
                raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True)
            current = render_agent_markdown(agent_name, instructions, project_rules)
            previous_hash = str(metadata.get("agent_markdown_sha256"))
            current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
            return current, _AgentDeclarationUpgrade(
                generation_id=str(metadata.get("generation_id", "")),
                legacy_shape=_declaration_shape_label(legacy_tools),
                previous_hash=previous_hash,
                current_hash=current_hash,
            )
        raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True)

    def _normalize_staged_legacy_declaration(
        self,
        root: Path,
        metadata: dict[str, object],
        expected: dict[str, object],
        *,
        spec: GenerationSpec,
        agent_name: str,
    ) -> _AgentDeclarationUpgrade | None:
        """Bring a staged legacy declaration forward before the strict compare.

        Every non-hash identity field must already match the generation this
        stage call describes, so a metadata file with wrong identity can never
        be partially rewritten on its way to the final fatal. Only the recorded
        declaration hash changes, and only for an on-disk artifact that is
        exactly what that hash recorded and matches a frozen historical shape
        with this spec's instructions verbatim.
        """

        current_hash = str(expected["agent_markdown_sha256"])
        recorded_hash = metadata.get("agent_markdown_sha256")
        if recorded_hash == current_hash:
            return None
        non_hash = {
            key: value for key, value in expected.items() if key != "agent_markdown_sha256"
        }
        if (
            not isinstance(recorded_hash, str)
            or any(metadata.get(key) != value for key, value in non_hash.items())
        ):
            raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)
        path = root / "profile" / ".gemini" / "config" / "agents" / agent_name / "agent.md"
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProviderAdapterError(
                "agy_artifact_identity_mismatch", fatal_generation=True
            ) from exc
        if hashlib.sha256(raw).hexdigest() != recorded_hash:
            raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)
        expected_body = spec.system_instructions.strip()
        for legacy_tools in LEGACY_AGENT_TOOLSETS:
            if legacy_tools is None and (
                spec.project_rules is not None or self._has_canonical_project_rules(root)
            ):
                continue
            instructions = self._declaration_instructions(
                content, agent_name, legacy_tools, spec.project_rules
            )
            if instructions is None:
                continue
            if instructions != expected_body:
                raise ProviderAdapterError(
                    "agy_artifact_identity_mismatch", fatal_generation=True
                )
            metadata["agent_markdown_sha256"] = current_hash
            return _AgentDeclarationUpgrade(
                generation_id=str(expected["generation_id"]),
                legacy_shape=_declaration_shape_label(legacy_tools),
                previous_hash=recorded_hash,
                current_hash=current_hash,
            )
        raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)

    @staticmethod
    def _declaration_instructions(
        content: str,
        agent_name: str,
        tools: tuple[str, ...] | None,
        project_rules: str | None,
    ) -> str | None:
        """Extract the instructions body for one declaration shape, or None."""

        try:
            return extract_rendered_system_instructions(
                agent_name,
                content,
                project_rules=project_rules,
                tools=tools,
            )
        except ValueError:
            return None

    def _require_authenticated_rules_identity(
        self,
        root: Path,
        generation: GenerationRecord,
        metadata: dict[str, object],
    ) -> str | None:
        """Prove the rules axis of the identity before any upgrade may write.

        The declared rules presence selects which canonical body, which
        declaration shapes and which rules-derived identity the rest of the
        layout is allowed to use, so it is authenticated ahead of the commit
        phase and not only in the final verification. ``_verify_security_artifacts``
        repeats these predicates as the last authority; the early copy exists so
        a fatal can no longer land after a write. Materialized-copy verification
        stays late by design: those artifacts are healed from the canonical body
        rather than treated as an authentication input.
        """

        project_rules = self._canonical_project_rules(root, metadata)
        expected_rules_digest = project_rules_identity_digest(project_rules)
        if (
            generation_identity_parts(
                runtime_kind=generation.runtime_kind,
                bootstrap_fingerprint=generation.bootstrap_fingerprint,
                system_instructions_digest=generation.system_instructions_sha256,
                project_rules_digest=expected_rules_digest,
            )
            != generation.identity_hash
        ):
            # A rules-present durable identity can never be reproduced from
            # metadata that resolves to "no rules" (R1-03 masquerade guard).
            raise ProviderAdapterError(
                "agy_artifact_identity_mismatch", fatal_generation=True
            )
        stored_rules_digest = metadata.get("project_rules_sha256")
        if expected_rules_digest is None:
            # CP3 rules-absent metadata omits the digest; the pre-repair absent
            # digest is tolerated so early CP3 artifacts are not declared broken.
            rules_ok = stored_rules_digest in (None, project_rules_absent_digest())
        else:
            rules_ok = stored_rules_digest == expected_rules_digest
        if not rules_ok:
            raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)
        return project_rules

    @staticmethod
    def _has_canonical_project_rules(root: Path) -> bool:
        return (
            CanonicalControlStore(root).read_canonical_if_present(
                PROJECT_RULES_ARTIFACT.canonical_name
            )
            is not None
        )

    @staticmethod
    def _log_declaration_upgrade(
        binding_id: str,
        upgrade: _AgentDeclarationUpgrade,
    ) -> None:
        """Report one converged declaration upgrade (no content, no paths)."""

        _LOGGER.info(
            "agy agent declaration upgraded: binding=%s generation=%s shape=%s %s->%s",
            binding_id,
            upgrade.generation_id,
            upgrade.legacy_shape,
            upgrade.previous_hash[:12],
            upgrade.current_hash[:12],
        )

    def _expected_identity_fields(self, generation: GenerationRecord) -> dict[str, object]:
        """Durable metadata fields a generation's artifact must agree with."""

        return {
            "schema_version": "v2",
            "binding_id": generation.binding_id,
            "runtime_kind": generation.runtime_kind,
            "bootstrap_fingerprint": generation.bootstrap_fingerprint,
            "system_instructions_sha256": generation.system_instructions_sha256,
            "agent_name": generation_agent_name(generation.binding_id),
            "generation_id": self._generation_id(
                generation.binding_id,
                generation.identity_hash,
            ),
            "identity_hash": generation.identity_hash,
        }

    def _require_matching_identity_fields(
        self,
        generation: GenerationRecord,
        metadata: dict[str, object],
    ) -> None:
        """Prove the non-hash identity fields before any upgrade may write."""

        if any(
            metadata.get(key) != value
            for key, value in self._expected_identity_fields(generation).items()
        ):
            raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)

    @staticmethod
    def _validate_process_options(options: ProcessExecutionOptions) -> None:
        if (
            options.security_policy_revision != SECURITY_POLICY_REVISION
            or options.launch_environment_revision != LAUNCH_ENVIRONMENT_REVISION
            or options.profile_mode != "generation_private"
            # AGY is the one provider that opts out of the provider-neutral
            # sandbox default: the verified 1.2.5 tool unlock runs with the
            # deny policy as the authority, so ``sandbox=True`` would append
            # ``--sandbox`` and contradict the frozen security policy.
            or options.sandbox
        ):
            raise ProviderAdapterError("agy_process_options_unsupported")

    def _validate_mcp_manifest(
        self,
        mcp_manifest: tuple[RuntimeMcpTool, ...],
        options: ProcessExecutionOptions,
    ) -> None:
        if (
            options.mcp_manifest_sha256 is None
            or options.mcp_manifest_sha256
            != runtime_mcp_manifest_sha256(mcp_manifest)
        ):
            raise ProviderAdapterError("agy_mcp_manifest_mismatch")
        idle = self.supervisor.config.idle_timeout_seconds
        hard = self.supervisor.config.hard_timeout_seconds
        if idle > hard or any(
            tool.max_call_seconds is not None
            and tool.max_call_seconds >= idle
            for tool in mcp_manifest
        ):
            raise ProviderAdapterError("agy_mcp_tool_timeout_invalid")

    def _restore_security_artifacts(
        self,
        root: Path,
        metadata: dict[str, object],
        agent_markdown: str,
        mcp_manifest: tuple[RuntimeMcpTool, ...] | None,
    ) -> None:
        binding_id = str(metadata["binding_id"])
        generation_id = str(metadata["generation_id"])
        agent_name = str(metadata["agent_name"])
        profile = root / "profile"
        workspace = root / "workspace"
        controlled_directories = (
            root,
            profile,
            profile / ".gemini",
            profile / ".gemini" / "config",
            profile / ".gemini" / "config" / "agents",
            profile / ".gemini" / "config" / "agents" / agent_name,
            profile / ".gemini" / "antigravity-cli",
            profile / "AppData",
            profile / "AppData" / "Roaming",
            profile / "AppData" / "Local",
            profile / ".config",
            profile / ".cache",
            profile / ".local",
            profile / ".local" / "share",
            profile / "Temp",
            root / "mailbox",
        )
        for directory in controlled_directories:
            if self._is_link_or_reparse(directory):
                raise ProviderAdapterError("agy_security_artifact_invalid", fatal_generation=True)
            directory.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            # Workspace continuity: ordinary files persist across repeated
            # stage/prepare/reacquire. Only retire() destroys the generation
            # root. Reserved control artifacts are healed from canonical
            # backing instead of clearing anything here.
            if self._is_link_or_reparse(workspace) or not workspace.is_dir():
                raise ProviderAdapterError("agy_workspace_invalid", fatal_generation=True)
        else:
            workspace.mkdir(parents=True)
        self._require_profile_only_mcp_control(profile, workspace)
        CanonicalControlStore(root).prepare()
        mailbox = EphemeralMailbox(
            root / "mailbox",
            binding_id=binding_id,
            generation_id=generation_id,
            ttl_seconds=self.mailbox_ttl_seconds,
        )
        self._atomic_write_json(
            profile / ".gemini" / "config" / "hooks.json",
            self._expected_hooks(mailbox.root),
        )
        self._atomic_write_json(
            profile / ".gemini" / "antigravity-cli" / "settings.json",
            self._expected_settings(),
        )
        mcp_config_path = profile / ".gemini" / "config" / "mcp_config.json"
        # Generation ensure has no per-turn manifest. It must not erase the
        # config accepted by a live process; first-turn prepare supplies and
        # verifies the authoritative manifest before spawn.
        if mcp_manifest is not None or not mcp_config_path.exists():
            self._atomic_write_json(
                mcp_config_path,
                self._expected_mcp_config(binding_id, mcp_manifest or ()),
            )
        self._atomic_write_text(
            profile / ".gemini" / "config" / "agents" / agent_name / "agent.md",
            agent_markdown,
        )

    def _restore_reserved_control_artifacts(
        self,
        root: Path,
        metadata: dict[str, object],
    ) -> None:
        """Verify and heal only the registered reserved artifacts.

        This pass never traverses or clears ordinary workspace content. The
        artifact list is owned by the seam that introduces each reserved path:
        the project-rules mirror is registered only for generations whose
        metadata records project rules, so a rules-free generation never gains
        an ``AGENTS.md`` it did not ask for.
        """

        store = CanonicalControlStore(root)
        for artifact in self._reserved_artifacts_for(metadata):
            store.restore(artifact)

    def _reserved_artifacts_for(
        self,
        metadata: dict[str, object],
    ) -> tuple[ReservedControlArtifact, ...]:
        if metadata.get("project_rules_present") is True:
            return self.reserved_artifacts + (PROJECT_RULES_ARTIFACT,)
        return self.reserved_artifacts

    def _canonical_project_rules(
        self,
        root: Path,
        metadata: dict[str, object],
    ) -> str | None:
        """Return the generation's project rules body, or ``None`` when absent.

        The canonical backing is the durable store, so a generation whose
        metadata records rules fails closed (``agy_control_backing_missing``)
        when that backing disappears instead of silently degrading into a
        rules-free generation.
        """

        if metadata.get("project_rules_present") is not True:
            return None
        return CanonicalControlStore(root).read_canonical(
            PROJECT_RULES_ARTIFACT.canonical_name
        )

    def _verify_security_artifacts(
        self,
        root: Path,
        generation: GenerationRecord,
        metadata: dict[str, object],
        mcp_manifest: tuple[RuntimeMcpTool, ...],
    ) -> None:
        generation_id = metadata.get("generation_id")
        agent_hash = metadata.get("agent_markdown_sha256")
        system_hash = metadata.get("system_instructions_sha256")
        expected_fields = self._expected_identity_fields(generation)
        if (
            not isinstance(generation_id, str)
            or not isinstance(agent_hash, str)
            or not isinstance(system_hash, str)
            or any(metadata.get(key) != value for key, value in expected_fields.items())
        ):
            raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)
        artifact_session = metadata.get("provider_session_id")
        if (
            artifact_session is not None
            and generation.provider_session_id is not None
            and artifact_session != generation.provider_session_id
        ):
            raise ProviderAdapterError("agy_artifact_session_conflict", fatal_generation=True)
        layout_profile = root / "profile"
        layout_workspace = root / "workspace"
        agent_name = str(metadata["agent_name"])
        mailbox_root = root / "mailbox"
        project_rules = self._canonical_project_rules(root, metadata)
        if (
            generation_identity_parts(
                runtime_kind=generation.runtime_kind,
                bootstrap_fingerprint=generation.bootstrap_fingerprint,
                system_instructions_digest=generation.system_instructions_sha256,
                project_rules_digest=project_rules_identity_digest(project_rules),
            )
            != generation.identity_hash
        ):
            # A rules-present durable identity can never be reproduced from
            # metadata that resolves to "no rules" (R1-03 masquerade guard).
            raise ProviderAdapterError(
                "agy_artifact_identity_mismatch", fatal_generation=True
            )
        hooks_path = layout_profile / ".gemini" / "config" / "hooks.json"
        mcp_config_path = layout_profile / ".gemini" / "config" / "mcp_config.json"
        settings_path = layout_profile / ".gemini" / "antigravity-cli" / "settings.json"
        agent_path = (
            layout_profile / ".gemini" / "config" / "agents" / agent_name / "agent.md"
        )
        controlled_paths = (
            layout_profile,
            layout_profile / ".gemini",
            layout_profile / ".gemini" / "config",
            layout_profile / ".gemini" / "config" / "agents",
            agent_path.parent,
            layout_profile / ".gemini" / "antigravity-cli",
            layout_profile / "AppData",
            layout_profile / "AppData" / "Roaming",
            layout_profile / "AppData" / "Local",
            layout_profile / ".config",
            layout_profile / ".cache",
            layout_profile / ".local",
            layout_profile / ".local" / "share",
            layout_profile / "Temp",
            mailbox_root,
            hooks_path,
            mcp_config_path,
            settings_path,
            agent_path,
            layout_workspace,
        )
        if any(self._is_link_or_reparse(path) for path in controlled_paths):
            raise ProviderAdapterError("agy_security_artifact_invalid", fatal_generation=True)
        mailbox = EphemeralMailbox(
            mailbox_root,
            binding_id=generation.binding_id,
            generation_id=generation_id,
            ttl_seconds=self.mailbox_ttl_seconds,
        )
        if self._read_json(hooks_path) != self._expected_hooks(mailbox.root):
            raise ProviderAdapterError("agy_hook_policy_invalid", fatal_generation=True)
        if self._read_json(settings_path) != self._expected_settings():
            raise ProviderAdapterError("agy_deny_policy_invalid", fatal_generation=True)
        if self._read_json(mcp_config_path) != self._expected_mcp_config(
            generation.binding_id, mcp_manifest
        ):
            raise ProviderAdapterError("agy_mcp_config_invalid", fatal_generation=True)
        try:
            agent_bytes = agent_path.read_bytes()
            agent_markdown = agent_bytes.decode("utf-8", errors="strict")
            rendered_system = extract_rendered_system_instructions(
                agent_name,
                agent_markdown,
                project_rules=project_rules,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True) from exc
        if (
            hashlib.sha256(agent_bytes).hexdigest() != agent_hash
            or hashlib.sha256(rendered_system.encode("utf-8")).hexdigest() != system_hash
        ):
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True)
        stored_rules_digest = metadata.get("project_rules_sha256")
        expected_rules_digest = project_rules_identity_digest(project_rules)
        if expected_rules_digest is None:
            # CP3 rules-absent metadata omits the digest; the pre-repair absent
            # digest is tolerated so early CP3 artifacts are not declared broken.
            rules_ok = stored_rules_digest in (None, project_rules_absent_digest())
        else:
            rules_ok = stored_rules_digest == expected_rules_digest
        if not rules_ok:
            raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)
        if (
            not layout_workspace.is_dir()
            or self._is_link_or_reparse(layout_workspace)
        ):
            raise ProviderAdapterError("agy_workspace_invalid", fatal_generation=True)
        self._require_profile_only_mcp_control(layout_profile, layout_workspace)
        control_store = CanonicalControlStore(root)
        for artifact in self._reserved_artifacts_for(metadata):
            if not control_store.verify(artifact):
                raise ProviderAdapterError("agy_security_artifact_invalid", fatal_generation=True)

    @classmethod
    def _require_profile_only_mcp_control(cls, profile: Path, workspace: Path) -> None:
        """Reject alternate MCP/plugin customization sources before AGY acquisition."""

        for customization_root in (workspace / ".agents", profile / ".agents"):
            direct_mcp = customization_root / "mcp_config.json"
            plugins = customization_root / "plugins"
            if (
                cls._is_link_or_reparse(customization_root)
                or cls._is_link_or_reparse(direct_mcp)
                or cls._is_link_or_reparse(plugins)
                or direct_mcp.exists()
                or plugins.exists()
            ):
                raise ProviderAdapterError(
                    "agy_uncontrolled_mcp_source_forbidden", fatal_generation=True
                )

    @staticmethod
    def _expected_hooks(mailbox_root: Path) -> dict[str, object]:
        hook_command = subprocess.list2cmdline(
            [
                sys.executable,
                "-m",
                "exocore_runtime.providers.antigravity.ephemeral_hook",
                "--mailbox-root",
                str(mailbox_root),
            ]
        )
        return {
            "exocore-runtime-ephemeral": {
                "enabled": True,
                "PreInvocation": [
                    {
                        "type": "command",
                        "command": hook_command,
                        "timeout": 10,
                    }
                ],
            }
        }

    @staticmethod
    def _expected_settings() -> dict[str, object]:
        return {
            "modelProvider": "account_default",
            "permissions": {
                "allow": list(ALLOW_POLICY),
                "deny": list(DENY_POLICY),
            },
        }

    def _expected_mcp_config(
        self,
        binding_id: str,
        mcp_manifest: tuple[RuntimeMcpTool, ...],
    ) -> dict[str, object]:
        # ExoCore owns the ordered visible surface and eager policy. Runtime
        # materializes exactly the received manifest without a local name list.
        return {
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "command": self.memory_mcp_python,
                    "args": [
                        "-m",
                        "engines.mcp.servers.memory.server",
                        "--runtime-binding-id",
                        binding_id,
                    ],
                    "cwd": str(self.memory_mcp_root),
                    "env": {"PYTHONPATH": str(self.memory_mcp_root)},
                    "enabledTools": [tool.name for tool in mcp_manifest],
                    "tools": {
                        tool.name: {"eager": True}
                        for tool in mcp_manifest
                        if tool.eager
                    },
                }
            }
        }

    def _layout_from_metadata(
        self,
        root: Path,
        metadata: dict[str, object],
        options: ProcessExecutionOptions,
        mcp_manifest: tuple[RuntimeMcpTool, ...],
    ) -> GenerationLayout:
        required = (
            "binding_id",
            "agent_name",
            "generation_id",
        )
        if any(not isinstance(metadata.get(key), str) for key in required):
            raise ProviderAdapterError("agy_generation_artifact_invalid", fatal_generation=True)
        provider_session_id = metadata.get("provider_session_id")
        if provider_session_id is not None and not isinstance(provider_session_id, str):
            raise ProviderAdapterError("agy_generation_artifact_invalid", fatal_generation=True)
        return GenerationLayout(
            binding_id=str(metadata["binding_id"]),
            root=root,
            profile=root / "profile",
            workspace=root / "workspace",
            agent_name=str(metadata["agent_name"]),
            provider_session_id=provider_session_id,
            execution_options=options,
            mcp_tool_names=tuple(tool.name for tool in mcp_manifest),
        )

    def _update_provider_session(self, binding_id: str, provider_session_id: str) -> None:
        root = self._generation_root(binding_id)
        metadata_path = root / "generation.json"
        metadata = self._read_json(metadata_path)
        existing = metadata.get("provider_session_id")
        if existing is not None and existing != provider_session_id:
            raise ProviderAdapterError("resume_identity_mismatch")
        metadata["provider_session_id"] = provider_session_id
        self._atomic_write_json(metadata_path, metadata)

    def _mailbox(self, binding_id: str) -> EphemeralMailbox:
        root = self._generation_root(binding_id)
        metadata = self._read_json(root / "generation.json")
        generation_id = metadata.get("generation_id")
        if not isinstance(generation_id, str):
            raise ProviderAdapterError("agy_generation_artifact_invalid", fatal_generation=True)
        return EphemeralMailbox(
            root / "mailbox",
            binding_id=binding_id,
            generation_id=generation_id,
            ttl_seconds=self.mailbox_ttl_seconds,
        )

    def _cleanup_stale_mailboxes(self) -> None:
        for metadata_path in self.data_root.glob("*/generation.json"):
            metadata = self._read_json(metadata_path)
            binding_id = metadata.get("binding_id")
            generation_id = metadata.get("generation_id")
            if not isinstance(binding_id, str) or not isinstance(generation_id, str):
                raise ProviderAdapterError("agy_generation_artifact_invalid", fatal_generation=True)
            mailbox_root = metadata_path.parent / "mailbox"
            try:
                mailbox = EphemeralMailbox(
                    mailbox_root,
                    binding_id=binding_id,
                    generation_id=generation_id,
                    ttl_seconds=self.mailbox_ttl_seconds,
                )
            except ProviderAdapterError:
                EphemeralMailbox.cleanup_payload_files(mailbox_root)
                raise
            mailbox.cleanup_request()

    def _generation_root(self, binding_id: str) -> Path:
        compact = binding_id.replace("-", "")
        if not compact.isalnum():
            raise ProviderAdapterError("agy_binding_id_invalid", fatal_generation=True)
        candidate = self.data_root / compact.lower()
        if self._is_link_or_reparse(candidate):
            raise ProviderAdapterError("agy_data_root_escape", fatal_generation=True)
        root = candidate.resolve()
        if root.parent != self.data_root:
            raise ProviderAdapterError("agy_data_root_escape", fatal_generation=True)
        return root

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        if path.is_symlink():
            return True
        try:
            attributes = path.lstat().st_file_attributes
        except (AttributeError, FileNotFoundError):
            return False
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)

    @staticmethod
    def _generation_id(binding_id: str, identity_hash: str) -> str:
        return hashlib.sha256(
            f"{binding_id}:{identity_hash}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderAdapterError("agy_generation_artifact_invalid", fatal_generation=True) from exc
        if not isinstance(payload, dict):
            raise ProviderAdapterError("agy_generation_artifact_invalid", fatal_generation=True)
        return payload

    @staticmethod
    def _atomic_write_json(path: Path, payload: object) -> None:
        AntigravityAdapter._atomic_write_bytes(
            path,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        AntigravityAdapter._atomic_write_bytes(path, content.encode("utf-8"))

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
