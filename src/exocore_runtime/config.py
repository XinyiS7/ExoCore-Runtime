"""Environment configuration with pre-bind loopback validation."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path


def _default_runtime_root() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if root:
        return Path(root) / "ExoCore-Runtime"
    return Path.home() / ".local" / "state" / "ExoCore-Runtime"


def _default_state_path() -> Path:
    return _default_runtime_root() / "runtime.sqlite3"


def _default_memory_mcp_root() -> Path:
    """Resolve the canonical sibling ExoCore checkout without machine constants."""
    return Path(__file__).resolve().parents[3] / "ExoCore"


@dataclass(frozen=True, repr=False)
class RuntimeConfig:
    """Validated runtime configuration. The token is deliberately absent from repr."""

    host: str
    port: int
    token: str
    state_path: Path
    provider_data_root: Path | None = None
    agy_executable: str | None = None
    agy_init_timeout: float = 15.0
    agy_idle_timeout: float = 60.0
    agy_hard_timeout: float = 180.0
    agy_close_timeout: float = 5.0
    agy_mailbox_ttl: float = 120.0
    memory_mcp_root: Path | None = None

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as exc:
            raise ValueError("runtime host must be a literal loopback IP address") from exc
        if not address.is_loopback:
            raise ValueError("runtime host must be loopback")
        if not 1 <= self.port <= 65535:
            raise ValueError("runtime port must be between 1 and 65535")
        if not self.token:
            raise ValueError("runtime bearer token is required")
        for name in (
            "agy_init_timeout",
            "agy_idle_timeout",
            "agy_hard_timeout",
            "agy_close_timeout",
            "agy_mailbox_ttl",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.agy_idle_timeout > self.agy_hard_timeout:
            raise ValueError("AGY idle timeout cannot exceed hard timeout")

    def __repr__(self) -> str:
        return (
            f"RuntimeConfig(host={self.host!r}, port={self.port!r}, "
            "token='[REDACTED]', state_path='[PRIVATE]', "
            "provider_data_root='[PRIVATE]', memory_mcp_root='[PRIVATE]')"
        )

    @property
    def effective_provider_data_root(self) -> Path:
        return self.provider_data_root or (self.state_path.parent / "providers")

    @property
    def effective_memory_mcp_root(self) -> Path:
        return (self.memory_mcp_root or _default_memory_mcp_root()).resolve()

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        provider_root = os.environ.get("EXOCORE_RUNTIME_PROVIDER_DATA_ROOT")
        memory_mcp_root = os.environ.get("EXOCORE_RUNTIME_MEMORY_MCP_ROOT")
        return cls(
            host=os.environ.get("EXOCORE_RUNTIME_HOST", "127.0.0.1"),
            port=int(os.environ.get("EXOCORE_RUNTIME_PORT", "8766")),
            token=os.environ.get("EXOCORE_RUNTIME_TOKEN", ""),
            state_path=Path(os.environ.get("EXOCORE_RUNTIME_STATE_PATH", _default_state_path())),
            provider_data_root=Path(provider_root) if provider_root else None,
            agy_executable=os.environ.get("EXOCORE_RUNTIME_AGY_EXECUTABLE") or None,
            agy_init_timeout=float(os.environ.get("EXOCORE_RUNTIME_AGY_INIT_TIMEOUT", "15")),
            agy_idle_timeout=float(os.environ.get("EXOCORE_RUNTIME_AGY_IDLE_TIMEOUT", "60")),
            agy_hard_timeout=float(os.environ.get("EXOCORE_RUNTIME_AGY_HARD_TIMEOUT", "180")),
            agy_close_timeout=float(os.environ.get("EXOCORE_RUNTIME_AGY_CLOSE_TIMEOUT", "5")),
            agy_mailbox_ttl=float(os.environ.get("EXOCORE_RUNTIME_AGY_MAILBOX_TTL", "120")),
            memory_mcp_root=Path(memory_mcp_root) if memory_mcp_root else None,
        )
