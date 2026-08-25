"""Environment configuration with pre-bind loopback validation."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path


def _default_state_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if root:
        return Path(root) / "ExoCore-Runtime" / "runtime.sqlite3"
    return Path.home() / ".local" / "state" / "ExoCore-Runtime" / "runtime.sqlite3"


@dataclass(frozen=True, repr=False)
class RuntimeConfig:
    """Validated runtime configuration. The token is deliberately absent from repr."""

    host: str
    port: int
    token: str
    state_path: Path

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

    def __repr__(self) -> str:
        return (
            f"RuntimeConfig(host={self.host!r}, port={self.port!r}, "
            f"token='[REDACTED]', state_path={self.state_path!r})"
        )

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        return cls(
            host=os.environ.get("EXOCORE_RUNTIME_HOST", "127.0.0.1"),
            port=int(os.environ.get("EXOCORE_RUNTIME_PORT", "8766")),
            token=os.environ.get("EXOCORE_RUNTIME_TOKEN", ""),
            state_path=Path(os.environ.get("EXOCORE_RUNTIME_STATE_PATH", _default_state_path())),
        )
