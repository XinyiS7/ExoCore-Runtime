"""Start one supervised fake AGY process, then hard-exit to test Job cleanup."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from exocore_runtime.contracts import ProcessExecutionOptions
from exocore_runtime.providers.antigravity.capabilities import (
    LAUNCH_ENVIRONMENT_REVISION,
    SECURITY_POLICY_REVISION,
)
from exocore_runtime.providers.antigravity.process import (
    AgyProcessConfig,
    AgyProcessSupervisor,
    GenerationLayout,
)


async def run() -> None:
    fixture, root, evidence = map(Path, sys.argv[1:4])
    profile = root / "profile"
    workspace = root / "workspace"
    settings = profile / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"modelProvider": "account_default"}), encoding="utf-8")
    workspace.mkdir(parents=True)
    supervisor = AgyProcessSupervisor(
        AgyProcessConfig(
            command_prefix=(sys.executable, str(fixture)),
            init_timeout_seconds=2,
            idle_timeout_seconds=10,
            hard_timeout_seconds=20,
            close_timeout_seconds=1,
            require_official_executable=False,
            environment_overrides={
                "FAKE_AGY_SCENARIO": "launch_child_before_init",
                "FAKE_AGY_EVIDENCE": str(evidence),
            },
        )
    )
    await supervisor.ensure(
        GenerationLayout(
            binding_id="hard-crash-binding",
            root=root,
            profile=profile,
            workspace=workspace,
            agent_name="exocore-runtime-hardcrashbinding",
            provider_session_id=None,
            execution_options=ProcessExecutionOptions(
                provider_model_slug="gemini-3.1-pro-high",
                effort="high",
                security_policy_revision=SECURITY_POLICY_REVISION,
                launch_environment_revision=LAUNCH_ENVIRONMENT_REVISION,
            ),
        )
    )
    os._exit(73)


asyncio.run(run())
