"""Command-line startup for the loopback runtime gateway."""

from __future__ import annotations

import copy
import logging.config

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from exocore_runtime.api import create_app
from exocore_runtime.config import RuntimeConfig

# Same format as the ExoCore Django console, so both can be read side by side.
_CONSOLE_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def _console_log_config() -> dict:
    """Uvicorn's own configuration, plus timestamps and a root handler.

    Root WARNING is what Python's last-resort handler already printed for the
    Runtime loggers; levels and the disabled access log are unchanged.
    """

    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["formatters"]["default"] = {"format": _CONSOLE_FORMAT}
    log_config["root"] = {"handlers": ["default"], "level": "WARNING"}
    return log_config


def main() -> None:
    log_config = _console_log_config()
    # Building the app may already warn, before uvicorn configures logging.
    logging.config.dictConfig(log_config)
    config = RuntimeConfig.from_env()
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        access_log=False,
        log_config=log_config,
        server_header=False,
    )


if __name__ == "__main__":
    main()
