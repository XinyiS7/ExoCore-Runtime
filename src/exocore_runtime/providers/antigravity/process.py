"""Official AGY process supervision with isolated argv, environment, and cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import threading
import time

from exocore_runtime.contracts import ProviderEvent, ProviderGeneration
from exocore_runtime.errors import ProviderAdapterError
from exocore_runtime.providers.antigravity.ndjson import (
    AgyTurnNormalizer,
    parse_init,
    parse_line,
)


_FORBIDDEN_AUTH_ENV = frozenset(
    {
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "VERTEX_API_KEY",
    }
)
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_GATEWAY_CRASH_JOB_HANDLE: int | None = None
_GATEWAY_CRASH_JOB_LOCK = threading.Lock()


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@dataclass(frozen=True)
class AgyProcessConfig:
    command_prefix: tuple[str, ...]
    init_timeout_seconds: float = 15.0
    idle_timeout_seconds: float = 60.0
    hard_timeout_seconds: float = 180.0
    close_timeout_seconds: float = 5.0
    result_settle_seconds: float = 0.1
    require_official_executable: bool = True
    environment_overrides: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def official(cls, executable: str | None = None, **kwargs) -> "AgyProcessConfig":
        resolved = shutil.which(executable or "agy")
        if resolved is None:
            raise ProviderAdapterError("agy_executable_missing", fatal_generation=True)
        return cls(command_prefix=(str(Path(resolved).resolve()),), **kwargs)


@dataclass(frozen=True)
class GenerationLayout:
    binding_id: str
    root: Path
    profile: Path
    workspace: Path
    agent_name: str
    provider_model_id: str
    provider_session_id: str | None


@dataclass
class _ProcessSession:
    layout: GenerationLayout
    process: asyncio.subprocess.Process
    queue: asyncio.Queue[tuple[str, bytes | None]]
    reader_tasks: tuple[asyncio.Task[None], asyncio.Task[None]]
    provider_session_id: str
    job_handle: int | None = None
    current_request_id: str | None = None


class AgyProcessSupervisor:
    def __init__(self, config: AgyProcessConfig) -> None:
        if not config.command_prefix:
            raise ValueError("AGY command prefix is required")
        self.config = config
        self._ensure_gateway_crash_job()
        self._sessions: dict[str, _ProcessSession] = {}
        self._binding_locks: dict[str, asyncio.Lock] = {}
        self._binding_locks_guard = asyncio.Lock()
        self._preflight_lock = asyncio.Lock()
        self._preflight_complete = False
        self.quota_snapshot: dict[str, int] | None = None

    async def ensure(self, layout: GenerationLayout) -> ProviderGeneration:
        lock = await self._binding_lock(layout.binding_id)
        async with lock:
            self._validate_account_default(layout.profile)
            current = self._sessions.get(layout.binding_id)
            if current is not None and current.process.returncode is None:
                if current.layout.provider_model_id != layout.provider_model_id:
                    raise ProviderAdapterError("agy_model_mismatch", fatal_generation=True)
                return ProviderGeneration(
                    provider_session_id=current.provider_session_id,
                    observed_model=layout.provider_model_id,
                )
            if current is not None:
                await self._dispose_session(current, force=True)
                self._sessions.pop(layout.binding_id, None)
            await self._preflight(layout.profile, layout.workspace)
            session = await self._spawn(layout)
            self._sessions[layout.binding_id] = session
            return ProviderGeneration(
                provider_session_id=session.provider_session_id,
                observed_model=layout.provider_model_id,
            )

    async def stream_turn(
        self,
        binding_id: str,
        request_id: str,
        stdin_line: bytes,
    ) -> AsyncIterator[ProviderEvent]:
        session = self._sessions.get(binding_id)
        if session is None or session.process.returncode is not None:
            raise ProviderAdapterError(
                "agy_process_not_ready",
                terminal_status="indeterminate",
                fatal_generation=True,
            )
        if session.current_request_id is not None:
            raise ProviderAdapterError("agy_generation_busy", terminal_status="indeterminate")
        session.current_request_id = request_id
        try:
            self._reject_unsolicited_output(session)
            stdin = session.process.stdin
            if stdin is None:
                raise ProviderAdapterError("agy_stdin_unavailable", terminal_status="indeterminate")
            try:
                stdin.write(stdin_line)
                await stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise ProviderAdapterError(
                    "agy_stdin_write_failed",
                    terminal_status="indeterminate",
                ) from exc
            normalizer = AgyTurnNormalizer(session.provider_session_id)
            started = time.monotonic()
            while True:
                elapsed = time.monotonic() - started
                remaining = self.config.hard_timeout_seconds - elapsed
                if remaining <= 0:
                    raise ProviderAdapterError("agy_hard_timeout", terminal_status="indeterminate")
                timeout = min(self.config.idle_timeout_seconds, remaining)
                tag, line = await self._next_item(session, timeout, "agy_idle_timeout")
                if tag == "stderr":
                    raise ProviderAdapterError("agy_stderr", terminal_status="indeterminate")
                if line is None:
                    raise ProviderAdapterError("agy_unexpected_eof", terminal_status="indeterminate")
                events = normalizer.consume(parse_line(line))
                if normalizer.result_seen:
                    await self._assert_quiet_after_result(session)
                    session.current_request_id = None
                    for event in events:
                        yield event
                    return
                for event in events:
                    yield event
        except asyncio.CancelledError as original_error:
            await self._finish_cancelled_stream_cleanup(binding_id, original_error)
            raise
        except ProviderAdapterError as original_error:
            try:
                await self.close_binding(binding_id, force=True)
            except BaseException:
                original_error.add_note("process cleanup also failed")
            raise
        finally:
            current = self._sessions.get(binding_id)
            if current is not None and current.process.returncode is not None:
                current.current_request_id = None

    async def cancel(self, binding_id: str, request_id: str) -> None:
        session = self._sessions.get(binding_id)
        if session is None:
            return
        if session.current_request_id != request_id:
            return
        await self.close_binding(binding_id, force=False)

    async def _finish_cancelled_stream_cleanup(
        self,
        binding_id: str,
        original_error: asyncio.CancelledError,
    ) -> None:
        cleanup = asyncio.create_task(self.close_binding(binding_id, force=False))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            cleanup.result()
        except BaseException:
            original_error.add_note("cancelled stream process cleanup also failed")

    async def close_binding(self, binding_id: str, *, force: bool) -> None:
        lock = await self._binding_lock(binding_id)
        async with lock:
            session = self._sessions.get(binding_id)
            if session is None:
                return
            try:
                await self._dispose_session(session, force=force)
            except BaseException:
                if (
                    session.process.returncode is not None
                    and self._sessions.get(binding_id) is session
                ):
                    self._sessions.pop(binding_id, None)
                raise
            if self._sessions.get(binding_id) is session:
                self._sessions.pop(binding_id, None)

    def owns_request(self, binding_id: str, request_id: str) -> bool:
        session = self._sessions.get(binding_id)
        return (
            session is not None
            and session.process.returncode is None
            and session.current_request_id == request_id
        )

    async def shutdown(self) -> None:
        bindings = tuple(self._sessions)
        failures: list[BaseException] = []
        for binding_id in bindings:
            try:
                await self.close_binding(binding_id, force=False)
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise ProviderAdapterError("agy_shutdown_cleanup_failed")

    async def _binding_lock(self, binding_id: str) -> asyncio.Lock:
        async with self._binding_locks_guard:
            return self._binding_locks.setdefault(binding_id, asyncio.Lock())

    async def _preflight(self, profile: Path, workspace: Path) -> None:
        async with self._preflight_lock:
            if self._preflight_complete:
                return
            self._validate_executable()
            environment = self._isolated_environment(profile)
            version_stdout = await self._run_bounded(
                (*self.config.command_prefix, "--version"),
                environment,
                workspace,
                "agy_version_failed",
            )
            try:
                version = version_stdout.decode("utf-8", errors="strict").strip()
            except UnicodeDecodeError as exc:
                raise ProviderAdapterError("agy_version_invalid", fatal_generation=True) from exc
            match = _VERSION_PATTERN.fullmatch(version)
            if match is None:
                raise ProviderAdapterError("agy_version_invalid", fatal_generation=True)
            major, minor, patch = (int(part) for part in match.groups())
            if (major, minor, patch) < (1, 1, 20) or (major, minor) >= (1, 2):
                raise ProviderAdapterError("agy_version_unsupported", fatal_generation=True)
            quota_stdout = await self._run_bounded(
                (*self.config.command_prefix, "-p", "/quota", "--output-format", "json"),
                environment,
                workspace,
                "agy_auth_unavailable",
            )
            self.quota_snapshot = self._parse_quota(quota_stdout)
            self._preflight_complete = True

    def _validate_executable(self) -> None:
        if not self.config.require_official_executable:
            return
        if len(self.config.command_prefix) != 1:
            raise ProviderAdapterError("agy_executable_unofficial", fatal_generation=True)
        executable = Path(self.config.command_prefix[0])
        if executable.name.lower() != "agy.exe" or not executable.is_file():
            raise ProviderAdapterError("agy_executable_unofficial", fatal_generation=True)

    @staticmethod
    def _validate_account_default(profile: Path) -> None:
        settings_path = profile / ".gemini" / "antigravity-cli" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderAdapterError("agy_provider_settings_invalid", fatal_generation=True) from exc
        if not isinstance(settings, dict) or settings.get("modelProvider") != "account_default":
            raise ProviderAdapterError("agy_consumer_provider_required", fatal_generation=True)

    async def _spawn(self, layout: GenerationLayout) -> _ProcessSession:
        argv = [
            *self.config.command_prefix,
            "--agent",
            layout.agent_name,
            "--model",
            layout.provider_model_id,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--print-timeout",
            f"{int(self.config.hard_timeout_seconds)}s",
            "--sandbox",
        ]
        if layout.provider_session_id is not None:
            argv.extend(("--conversation", layout.provider_session_id))
        environment = self._isolated_environment(layout.profile)
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            start_new_session = True
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=layout.workspace,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        except (OSError, ValueError) as exc:
            raise ProviderAdapterError("agy_spawn_failed", fatal_generation=True) from exc
        if process.stdout is None or process.stderr is None or process.stdin is None:
            process.kill()
            await process.wait()
            raise ProviderAdapterError("agy_pipe_setup_failed", fatal_generation=True)
        try:
            job_handle = self._assign_windows_job(process.pid)
        except ProviderAdapterError:
            process.kill()
            await process.wait()
            raise
        queue: asyncio.Queue[tuple[str, bytes | None]] = asyncio.Queue()
        stdout_task = asyncio.create_task(self._pump("stdout", process.stdout, queue))
        stderr_task = asyncio.create_task(self._pump("stderr", process.stderr, queue))
        provisional = _ProcessSession(
            layout=layout,
            process=process,
            queue=queue,
            reader_tasks=(stdout_task, stderr_task),
            provider_session_id="",
            job_handle=job_handle,
        )
        try:
            tag, line = await self._next_item(
                provisional,
                self.config.init_timeout_seconds,
                "agy_init_timeout",
            )
            if tag == "stderr":
                raise ProviderAdapterError("agy_init_stderr", fatal_generation=True)
            if line is None:
                raise ProviderAdapterError("agy_init_eof", fatal_generation=True)
            acquired = parse_init(parse_line(line), layout.provider_model_id)
            if (
                layout.provider_session_id is not None
                and acquired.provider_session_id != layout.provider_session_id
            ):
                raise ProviderAdapterError("agy_resume_session_mismatch", fatal_generation=True)
            provisional.provider_session_id = acquired.provider_session_id
            await self._assert_ready_after_init(provisional)
            return provisional
        except BaseException as original_error:
            try:
                await self._dispose_session(provisional, force=True)
            except BaseException:
                original_error.add_note("init cleanup also failed")
            raise

    @staticmethod
    def _reject_unsolicited_output(session: _ProcessSession) -> None:
        while True:
            try:
                tag, line = session.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if tag == "stderr" and line is None:
                continue
            raise ProviderAdapterError(
                "agy_unsolicited_output",
                terminal_status="indeterminate",
                fatal_generation=True,
            )

    async def _assert_ready_after_init(self, session: _ProcessSession) -> None:
        deadline = time.monotonic() + self.config.result_settle_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                tag, line = await asyncio.wait_for(session.queue.get(), timeout=remaining)
            except TimeoutError:
                return
            if tag == "stderr" and line is None:
                continue
            if tag == "stderr":
                raise ProviderAdapterError("agy_init_stderr", fatal_generation=True)
            if line is None:
                raise ProviderAdapterError("agy_exit_after_init", fatal_generation=True)
            raise ProviderAdapterError("agy_event_before_user", fatal_generation=True)

    async def _assert_quiet_after_result(self, session: _ProcessSession) -> None:
        deadline = time.monotonic() + self.config.result_settle_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                tag, line = await asyncio.wait_for(session.queue.get(), timeout=remaining)
            except TimeoutError:
                return
            if tag == "stderr" and line is None:
                continue
            if tag == "stderr":
                raise ProviderAdapterError("agy_stderr", terminal_status="indeterminate")
            if line is None:
                raise ProviderAdapterError("agy_exit_after_result", terminal_status="indeterminate")
            payload = parse_line(line)
            if payload.get("event") == "result":
                raise ProviderAdapterError("agy_duplicate_result", terminal_status="indeterminate")
            raise ProviderAdapterError("agy_event_after_result", terminal_status="indeterminate")

    async def _next_item(
        self,
        session: _ProcessSession,
        timeout: float,
        timeout_code: str,
    ) -> tuple[str, bytes | None]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderAdapterError(timeout_code, terminal_status="indeterminate")
            try:
                tag, line = await asyncio.wait_for(session.queue.get(), timeout=remaining)
            except TimeoutError as exc:
                raise ProviderAdapterError(timeout_code, terminal_status="indeterminate") from exc
            if tag == "stderr" and line is None:
                continue
            return tag, line

    async def _dispose_session(self, session: _ProcessSession, *, force: bool) -> None:
        process = session.process
        cleanup_failure: BaseException | None = None
        if process.returncode is None and not force:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                    await asyncio.wait_for(
                        process.stdin.wait_closed(),
                        timeout=self.config.close_timeout_seconds,
                    )
                await asyncio.wait_for(process.wait(), timeout=self.config.close_timeout_seconds)
            except (BrokenPipeError, ConnectionError, TimeoutError) as exc:
                cleanup_failure = exc
        if process.returncode is None:
            try:
                await self._kill_tree(process)
            except BaseException as exc:
                if cleanup_failure is None:
                    cleanup_failure = exc
        for task in session.reader_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*session.reader_tasks, return_exceptions=True)
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        job_failure: BaseException | None = None
        try:
            self._close_windows_job(session.job_handle)
        except BaseException as exc:
            job_failure = exc
        session.job_handle = None
        if job_failure is not None:
            raise ProviderAdapterError("agy_job_cleanup_failed") from job_failure
        if cleanup_failure is not None and process.returncode is None:
            raise ProviderAdapterError("agy_process_cleanup_failed") from cleanup_failure

    async def _kill_tree(self, process: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=self.config.close_timeout_seconds)
            if killer.returncode not in (0, 128) and process.returncode is None:
                raise ProviderAdapterError("agy_process_tree_kill_failed")
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.wait_for(process.wait(), timeout=self.config.close_timeout_seconds)

    async def _run_bounded(
        self,
        argv: tuple[str, ...],
        environment: dict[str, str],
        cwd: Path,
        error_code: str,
    ) -> bytes:
        process: asyncio.subprocess.Process | None = None
        job_handle: int | None = None
        original_failure: BaseException | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
                start_new_session=os.name != "nt",
            )
            job_handle = self._assign_windows_job(process.pid)
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.init_timeout_seconds,
            )
            if process.returncode != 0 or stderr:
                raise ProviderAdapterError(error_code, fatal_generation=True)
            return stdout
        except ProviderAdapterError as exc:
            original_failure = exc
            raise
        except (OSError, TimeoutError) as exc:
            failure = ProviderAdapterError(error_code, fatal_generation=True)
            original_failure = failure
            raise failure from exc
        finally:
            if process is not None and process.returncode is None:
                try:
                    await self._kill_tree(process)
                except BaseException as cleanup_error:
                    if original_failure is None:
                        raise ProviderAdapterError("agy_preflight_cleanup_failed") from cleanup_error
                    original_failure.add_note("preflight process cleanup also failed")
            try:
                self._close_windows_job(job_handle)
            except BaseException as cleanup_error:
                if original_failure is None:
                    raise ProviderAdapterError("agy_preflight_cleanup_failed") from cleanup_error
                original_failure.add_note("preflight job cleanup also failed")

    @classmethod
    def _ensure_gateway_crash_job(cls) -> None:
        global _GATEWAY_CRASH_JOB_HANDLE
        if os.name != "nt" or _GATEWAY_CRASH_JOB_HANDLE is not None:
            return
        with _GATEWAY_CRASH_JOB_LOCK:
            if _GATEWAY_CRASH_JOB_HANDLE is None:
                _GATEWAY_CRASH_JOB_HANDLE = cls._assign_windows_job(os.getpid())

    @staticmethod
    def _assign_windows_job(process_id: int) -> int | None:
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ProviderAdapterError("agy_job_create_failed", fatal_generation=True)
        process_handle = None
        try:
            limits = _JobObjectExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ProviderAdapterError("agy_job_configure_failed", fatal_generation=True)
            process_handle = kernel32.OpenProcess(
                _PROCESS_TERMINATE | _PROCESS_SET_QUOTA,
                False,
                process_id,
            )
            if not process_handle:
                raise ProviderAdapterError("agy_job_open_process_failed", fatal_generation=True)
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                raise ProviderAdapterError("agy_job_assign_failed", fatal_generation=True)
            return int(job)
        except BaseException:
            kernel32.CloseHandle(job)
            raise
        finally:
            if process_handle:
                kernel32.CloseHandle(process_handle)

    @staticmethod
    def _close_windows_job(job_handle: int | None) -> None:
        if os.name != "nt" or job_handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        if not kernel32.CloseHandle(wintypes.HANDLE(job_handle)):
            raise ProviderAdapterError("agy_job_close_failed")

    def _isolated_environment(self, profile: Path | None) -> dict[str, str]:
        if any(os.environ.get(name) for name in _FORBIDDEN_AUTH_ENV):
            raise ProviderAdapterError("agy_forbidden_auth_environment", fatal_generation=True)
        environment = {
            name: value
            for name, value in os.environ.items()
            if not self._is_sensitive_environment_name(name)
        }
        for name in _FORBIDDEN_AUTH_ENV:
            environment.pop(name, None)
        environment.update(self.config.environment_overrides)
        if any(
            value and self._is_sensitive_environment_name(name)
            for name, value in self.config.environment_overrides.items()
        ):
            raise ProviderAdapterError("agy_sensitive_environment", fatal_generation=True)
        if any(environment.get(name) for name in _FORBIDDEN_AUTH_ENV):
            raise ProviderAdapterError("agy_forbidden_auth_environment", fatal_generation=True)
        if profile is not None:
            roaming = profile / "AppData" / "Roaming"
            local = profile / "AppData" / "Local"
            config = profile / ".config"
            cache = profile / ".cache"
            data = profile / ".local" / "share"
            temporary = profile / "Temp"
            controlled_roots = (
                profile,
                profile / "AppData",
                roaming,
                local,
                config,
                cache,
                profile / ".local",
                data,
                temporary,
            )
            if any(self._is_link_or_reparse(path) for path in controlled_roots):
                raise ProviderAdapterError("agy_profile_root_escape", fatal_generation=True)
            for path in (roaming, local, config, cache, data, temporary):
                path.mkdir(parents=True, exist_ok=True)
            environment["HOME"] = str(profile)
            environment["USERPROFILE"] = str(profile)
            environment["APPDATA"] = str(roaming)
            environment["LOCALAPPDATA"] = str(local)
            environment["XDG_CONFIG_HOME"] = str(config)
            environment["XDG_CACHE_HOME"] = str(cache)
            environment["XDG_DATA_HOME"] = str(data)
            environment["TEMP"] = str(temporary)
            environment["TMP"] = str(temporary)
        return environment

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
    def _is_sensitive_environment_name(name: str) -> bool:
        upper = name.upper()
        return (
            upper.startswith("EXOCORE_RUNTIME_")
            or any(
                marker in upper
                for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")
            )
        )

    @staticmethod
    async def _pump(
        tag: str,
        stream: asyncio.StreamReader,
        queue: asyncio.Queue[tuple[str, bytes | None]],
    ) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                await queue.put((tag, line))
        finally:
            await queue.put((tag, None))

    @staticmethod
    def _parse_quota(stdout: bytes) -> dict[str, int]:
        try:
            payload = json.loads(stdout.decode("utf-8", errors="strict"))
            usage = payload["usage"]
            command = payload["command"]
            groups = command["data"]["groups"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProviderAdapterError("agy_auth_unavailable", fatal_generation=True) from exc
        if payload.get("status") != "SUCCESS" or payload.get("num_turns") != 0:
            raise ProviderAdapterError("agy_auth_unavailable", fatal_generation=True)
        if not isinstance(usage, dict) or any(value != 0 for value in usage.values()):
            raise ProviderAdapterError("agy_quota_not_zero_turn", fatal_generation=True)
        snapshot: dict[str, int] = {}
        if not isinstance(groups, list):
            raise ProviderAdapterError("agy_auth_unavailable", fatal_generation=True)
        for group in groups:
            if not isinstance(group, dict) or group.get("name") != "Gemini Models":
                continue
            for bucket in group.get("buckets", []):
                if not isinstance(bucket, dict):
                    continue
                window = bucket.get("window")
                remaining = bucket.get("remaining_fraction")
                if window in {"5h", "weekly"} and isinstance(remaining, (int, float)):
                    snapshot[str(window)] = round(float(remaining) * 100)
        if set(snapshot) != {"5h", "weekly"}:
            raise ProviderAdapterError("agy_auth_unavailable", fatal_generation=True)
        return snapshot
