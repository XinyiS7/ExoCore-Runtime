"""Official AGY adapter over generation-private process and mailbox artifacts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import hashlib
import json
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
    ProviderGeneration,
    TurnRequest,
    generation_identity,
    system_instructions_sha256,
)
from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.capabilities import (
    LAUNCH_ENVIRONMENT_REVISION,
    SECURITY_POLICY_REVISION,
    resolve_execution,
)
from exocore_runtime.providers.antigravity.control import (
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
    DENY_POLICY,
    extract_rendered_system_instructions,
    generation_agent_name,
    render_agent_markdown,
    render_stdin_line,
)
from exocore_runtime.state_store import GenerationRecord


class AntigravityAdapter:
    def __init__(
        self,
        data_root: Path,
        supervisor: AgyProcessSupervisor,
        *,
        mailbox_ttl_seconds: float = 120.0,
        reserved_artifacts: tuple[ReservedControlArtifact, ...] = RESERVED_CONTROL_ARTIFACTS,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.supervisor = supervisor
        self.mailbox_ttl_seconds = mailbox_ttl_seconds
        self.reserved_artifacts = tuple(reserved_artifacts)
        self._prepared: dict[str, tuple[str, EphemeralMailbox, str, bytes]] = {}
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

    async def prepare_turn(
        self,
        generation: GenerationRecord,
        request: TurnRequest,
        options: ProcessExecutionOptions,
        *,
        is_first_turn: bool,
    ) -> ProviderGeneration:
        binding_id = generation.binding_id
        mailbox: EphemeralMailbox | None = None
        acquired: ProviderGeneration | None = None
        try:
            lock = await self._artifact_lock(binding_id)
            async with lock:
                if self._shutting_down:
                    raise ProviderAdapterError("agy_adapter_shutting_down", fatal_generation=True)
                layout = self._load_layout(generation, options)
                acquired = await self.supervisor.ensure(layout)
                self._update_provider_session(binding_id, acquired.provider_session_id)
                mailbox = self._mailbox(binding_id)
                request_id = str(request.request_id)
                payload_hash = mailbox.prepare(request_id, request.ephemeral_current)
                stdin_line = render_stdin_line(request, is_first_turn=is_first_turn)
                self._prepared[binding_id] = (request_id, mailbox, payload_hash, stdin_line)
        except asyncio.CancelledError as original_error:
            try:
                self._cleanup_presend_payloads(binding_id, mailbox)
            except OSError:
                original_error.add_note("pre-send mailbox cleanup also failed")
            raise
        except ProviderAdapterError as original_error:
            try:
                self._cleanup_presend_payloads(binding_id, mailbox)
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
                self._cleanup_presend_payloads(binding_id, mailbox)
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
        prepared = self._prepared.get(binding_id)
        if prepared is None or prepared[0] != request_id:
            raise ProviderAdapterError(
                "agy_turn_not_prepared",
                terminal_status="indeterminate",
            )
        _, mailbox, payload_hash, stdin_line = prepared
        deferred_usage: list[ProviderEvent] = []
        original_failure: BaseException | None = None
        try:
            async for event in self.supervisor.stream_turn(binding_id, request_id, stdin_line):
                if event.event_type == "usage":
                    deferred_usage.append(event)
                    continue
                if event.event_type in {"done", "error"}:
                    mailbox.validate_receipt(request_id, payload_hash)
                    mailbox.cleanup_request()
                    for usage in deferred_usage:
                        yield usage
                    yield event
                    return
                yield event
        except asyncio.CancelledError as exc:
            original_failure = exc
            try:
                await self.supervisor.cancel(binding_id, request_id)
            except BaseException:
                exc.add_note("cancelled owner process cleanup also failed")
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
            mailbox_clean = False
            try:
                mailbox.cleanup_request()
                mailbox_clean = True
            except OSError as cleanup_error:
                if original_failure is None:
                    raise ProviderAdapterError("ephemeral_cleanup_failed") from cleanup_error
                original_failure.add_note("ephemeral mailbox cleanup also failed")
            current = self._prepared.get(binding_id)
            if (
                mailbox_clean
                and current is not None
                and current[0] == request_id
                and not self.supervisor.owns_request(binding_id, request_id)
            ):
                self._prepared.pop(binding_id, None)

    async def cancel(self, binding_id: str, request_id: str) -> None:
        await self.supervisor.cancel(binding_id, request_id)
        lock = await self._artifact_lock(binding_id)
        async with lock:
            prepared = self._prepared.get(binding_id)
            if prepared is None or prepared[0] != request_id:
                return
            try:
                prepared[1].cleanup_request()
            except OSError as cleanup_error:
                raise ProviderAdapterError("ephemeral_cleanup_failed") from cleanup_error
            self._prepared.pop(binding_id, None)

    async def retire(self, binding_id: str, reason: str) -> None:
        lock = await self._artifact_lock(binding_id)
        async with lock:
            await self.supervisor.close_binding(binding_id, force=False)
            self._prepared.pop(binding_id, None)
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
        for binding_id, prepared in tuple(self._prepared.items()):
            try:
                prepared[1].cleanup_request()
            except OSError as exc:
                mailbox_failures.append(exc)
            else:
                self._prepared.pop(binding_id, None)
        if supervisor_failure is not None:
            if mailbox_failures:
                supervisor_failure.add_note("ephemeral mailbox cleanup also failed")
            raise supervisor_failure
        if mailbox_failures:
            raise ProviderAdapterError("ephemeral_cleanup_failed") from mailbox_failures[0]

    async def _artifact_lock(self, binding_id: str) -> asyncio.Lock:
        async with self._artifact_locks_guard:
            return self._artifact_locks.setdefault(binding_id, asyncio.Lock())

    def _cleanup_presend_payloads(
        self,
        binding_id: str,
        mailbox: EphemeralMailbox | None,
    ) -> None:
        if mailbox is not None:
            mailbox.cleanup_request()
            return
        EphemeralMailbox.cleanup_payload_files(
            self._generation_root(binding_id) / "mailbox"
        )

    def _ensure_generation_artifacts(
        self,
        binding_id: str,
        spec: GenerationSpec,
    ) -> None:
        root = self._generation_root(binding_id)
        metadata_path = root / "generation.json"
        agent_name = generation_agent_name(binding_id)
        agent_markdown = render_agent_markdown(agent_name, spec.system_instructions)
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
        }
        if metadata_path.exists():
            metadata = self._read_json(metadata_path)
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise ProviderAdapterError("agy_artifact_identity_mismatch", fatal_generation=True)
        else:
            metadata = {**expected, "provider_session_id": None}
        self._restore_security_artifacts(root, metadata, agent_markdown)
        self._atomic_write_json(metadata_path, metadata)

    def _load_layout(
        self,
        generation: GenerationRecord,
        options: ProcessExecutionOptions,
    ) -> GenerationLayout:
        self._validate_process_options(options)
        root = self._generation_root(generation.binding_id)
        metadata_path = root / "generation.json"
        if metadata_path.exists():
            metadata = self._read_json(metadata_path)
        else:
            metadata = self._rebuild_missing_metadata(root, generation)
        artifact_session = metadata.get("provider_session_id")
        if artifact_session is not None and not isinstance(artifact_session, str):
            raise ProviderAdapterError("agy_generation_artifact_invalid", fatal_generation=True)
        if (
            artifact_session is not None
            and generation.provider_session_id is not None
            and artifact_session != generation.provider_session_id
        ):
            raise ProviderAdapterError("agy_artifact_session_conflict", fatal_generation=True)
        if artifact_session is None and generation.provider_session_id is not None:
            metadata["provider_session_id"] = generation.provider_session_id
            self._atomic_write_json(metadata_path, metadata)
        agent_markdown = self._canonical_agent_markdown(root, metadata, generation)
        self._restore_security_artifacts(root, metadata, agent_markdown)
        self._restore_reserved_control_artifacts(root)
        self._verify_security_artifacts(root, generation, metadata)
        return self._layout_from_metadata(root, metadata, options)

    def _rebuild_missing_metadata(
        self,
        root: Path,
        generation: GenerationRecord,
    ) -> dict[str, object]:
        agent_name = generation_agent_name(generation.binding_id)
        agent_path = (
            root / "profile" / ".gemini" / "config" / "agents" / agent_name / "agent.md"
        )
        try:
            agent_markdown = agent_path.read_text(encoding="utf-8")
            rendered = extract_rendered_system_instructions(agent_name, agent_markdown)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderAdapterError("agy_generation_artifact_missing", fatal_generation=True) from exc
        if hashlib.sha256(rendered.encode("utf-8")).hexdigest() != generation.system_instructions_sha256:
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True)
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
            "provider_session_id": generation.provider_session_id,
        }
        self._atomic_write_json(root / "generation.json", metadata)
        return metadata

    def _canonical_agent_markdown(
        self,
        root: Path,
        metadata: dict[str, object],
        generation: GenerationRecord,
    ) -> str:
        agent_name = str(metadata.get("agent_name", ""))
        path = root / "profile" / ".gemini" / "config" / "agents" / agent_name / "agent.md"
        try:
            content = path.read_text(encoding="utf-8")
            rendered = extract_rendered_system_instructions(agent_name, content)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True) from exc
        if hashlib.sha256(rendered.encode("utf-8")).hexdigest() != generation.system_instructions_sha256:
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True)
        return content

    @staticmethod
    def _validate_process_options(options: ProcessExecutionOptions) -> None:
        if (
            options.security_policy_revision != SECURITY_POLICY_REVISION
            or options.launch_environment_revision != LAUNCH_ENVIRONMENT_REVISION
            or options.profile_mode != "generation_private"
        ):
            raise ProviderAdapterError("agy_process_options_unsupported")

    def _restore_security_artifacts(
        self,
        root: Path,
        metadata: dict[str, object],
        agent_markdown: str,
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
        self._atomic_write_text(
            profile / ".gemini" / "config" / "agents" / agent_name / "agent.md",
            agent_markdown,
        )

    def _restore_reserved_control_artifacts(self, root: Path) -> None:
        """Verify and heal only the registered reserved artifacts.

        This pass never traverses or clears ordinary workspace content, and it
        registers nothing by itself: the artifact list is owned by the seam
        that introduces each reserved path.
        """

        store = CanonicalControlStore(root)
        for artifact in self.reserved_artifacts:
            store.restore(artifact)

    def _verify_security_artifacts(
        self,
        root: Path,
        generation: GenerationRecord,
        metadata: dict[str, object],
    ) -> None:
        generation_id = metadata.get("generation_id")
        agent_hash = metadata.get("agent_markdown_sha256")
        system_hash = metadata.get("system_instructions_sha256")
        expected_fields = {
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
        hooks_path = layout_profile / ".gemini" / "config" / "hooks.json"
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
        try:
            agent_bytes = agent_path.read_bytes()
            agent_markdown = agent_bytes.decode("utf-8", errors="strict")
            rendered_system = extract_rendered_system_instructions(
                agent_name,
                agent_markdown,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True) from exc
        if (
            hashlib.sha256(agent_bytes).hexdigest() != agent_hash
            or hashlib.sha256(rendered_system.encode("utf-8")).hexdigest() != system_hash
        ):
            raise ProviderAdapterError("agy_custom_agent_invalid", fatal_generation=True)
        if (
            not layout_workspace.is_dir()
            or self._is_link_or_reparse(layout_workspace)
        ):
            raise ProviderAdapterError("agy_workspace_invalid", fatal_generation=True)
        control_store = CanonicalControlStore(root)
        for artifact in self.reserved_artifacts:
            if not control_store.verify(artifact):
                raise ProviderAdapterError("agy_security_artifact_invalid", fatal_generation=True)

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
            "permissions": {"deny": list(DENY_POLICY)},
        }

    def _layout_from_metadata(
        self,
        root: Path,
        metadata: dict[str, object],
        options: ProcessExecutionOptions,
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
