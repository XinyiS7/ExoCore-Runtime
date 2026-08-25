"""Command-line startup for the loopback runtime gateway."""

from __future__ import annotations

import uvicorn

from exocore_runtime.api import create_app
from exocore_runtime.config import RuntimeConfig


def main() -> None:
    config = RuntimeConfig.from_env()
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
