"""Deterministic official-shaped AGY executable fixture. Never imports product code."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def evidence(kind, **fields):
    path = os.environ.get("FAKE_AGY_EVIDENCE")
    if not path:
        return
    safe = {"kind": kind, "pid": os.getpid(), **fields}
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n")


def argument_value(name, default=None):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default


def run_hook(conversation_id):
    if os.environ.get("FAKE_AGY_SCENARIO") == "no_receipt":
        return {}
    home = Path(os.environ["HOME"])
    hooks_path = home / ".gemini" / "config" / "hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    entry = hooks["exocore-runtime-ephemeral"]["PreInvocation"][0]
    command = entry["command"]
    completed = subprocess.run(
        command,
        shell=True,
        cwd=hooks_path.parent,
        input=json.dumps(
            {
                "conversationId": conversation_id,
                "workspacePaths": [os.getcwd()],
                "transcriptPath": "fixture-private-transcript",
                "artifactDirectoryPath": "fixture-artifacts",
                "modelName": argument_value("--model"),
                "invocationNum": 1,
                "initialNumSteps": 0,
            }
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=10,
        check=False,
    )
    evidence(
        "hook",
        exit_code=completed.returncode,
        stderr_bytes=len(completed.stderr.encode("utf-8")),
    )
    if completed.returncode != 0:
        return {}
    return json.loads(completed.stdout)


def quota_response():
    return {
        "conversation_id": "",
        "status": "SUCCESS",
        "response": "",
        "duration_seconds": 0,
        "num_turns": 0,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
        },
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {"window": "weekly", "remaining_fraction": 0.84},
                            {"window": "5h", "remaining_fraction": 0.93},
                        ],
                    }
                ]
            },
        },
    }


def main():
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "normal")
    if "--version" in sys.argv:
        evidence("version", argv=sys.argv[1:])
        if scenario == "version_timeout":
            time.sleep(60)
            return 0
        if scenario == "version_stderr":
            print("fixture version warning", file=sys.stderr)
        if scenario == "version_1_2_4":
            print("1.2.4")
            return 0
        print("1.3.0" if scenario == "bad_version" else "1.1.20")
        return 0
    if "models" in sys.argv:
        evidence("models", argv=sys.argv[1:])
        if scenario == "models_missing":
            return 7
        if scenario == "models_progress_stderr":
            print("Fetching available models...", file=sys.stderr)
        if scenario == "models_invalid_stdout":
            print("INVALID MODEL OUTPUT")
            return 0
        print("gemini-3.1-pro-high     Gemini 3.1 Pro (High)")
        print("gemini-3.1-pro-low      Gemini 3.1 Pro (Low)")
        return 0
    if argument_value("-p") == "/quota":
        evidence("quota", argv=sys.argv[1:])
        if scenario == "auth_missing":
            return 7
        if scenario == "quota_stderr":
            print("fixture quota warning", file=sys.stderr)
        print(json.dumps(quota_response(), separators=(",", ":")))
        return 0

    forbidden_env_present = sorted(
        name
        for name in (
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "VERTEX_API_KEY",
        )
        if os.environ.get(name)
    )
    evidence(
        "spawn",
        argv=sys.argv[1:],
        cwd=os.getcwd(),
        home=os.environ.get("HOME"),
        userprofile=os.environ.get("USERPROFILE"),
        forbidden_env_present=forbidden_env_present,
        sensitive_env_present=sorted(
            name
            for name in ("EXOCORE_RUNTIME_TOKEN", "OTHER_SECRET_VALUE")
            if os.environ.get(name)
        ),
    )
    if scenario == "launch_child_before_init":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        evidence("launch_child", child_pid=child.pid)
    if scenario == "init_timeout":
        time.sleep(60)
        return 0
    if scenario == "init_empty":
        return 9
    if scenario == "init_malformed":
        print("not-json", flush=True)
        return 9

    model = argument_value("--model")
    conversation_id = argument_value("--conversation") or os.environ.get(
        "FAKE_AGY_CONVERSATION",
        "11111111-2222-3333-4444-555555555555",
    )
    if scenario == "resume_mismatch" and argument_value("--conversation"):
        conversation_id = "99999999-8888-7777-6666-555555555555"
    emit(
        {
            "event": "init",
            "conversation_id": conversation_id,
            "init": {
                "cwd": os.getcwd(),
                "model": "wrong-model" if scenario == "model_mismatch" else model,
                "permission_mode": "request-review",
                "tools": [f"tool-{index}" for index in range(57)],
            },
        }
    )
    if scenario == "exit_after_init":
        return 10

    turn_number = 0
    for line in sys.stdin:
        turn_number += 1
        raw = json.loads(line)
        content = raw["message"]["content"]
        hook_output = run_hook(conversation_id)
        injected = bool((hook_output or {}).get("injectSteps"))
        evidence(
            "turn",
            request_number=turn_number,
            stdin_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
            current_user_occurrences=content.count("CURRENT-USER-CANARY"),
            bootstrap_present="ExoCorePriorContinuity" in content,
            ephemeral_in_stdin="EPHEMERAL-CANARY" in content,
            hook_injected=injected,
        )
        emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": turn_number * 10,
                    "step_type": "user_input",
                    "state": "DONE",
                },
            }
        )
        if injected:
            emit(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": conversation_id,
                        "step_index": turn_number * 10 + 1,
                        "step_type": "unknown",
                        "state": "DONE",
                    },
                }
            )
        if scenario == "slow_tree":
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            evidence("child", child_pid=child.pid)
            time.sleep(60)
            continue
        if scenario == "malformed_stream":
            print("{malformed", flush=True)
            continue
        if scenario == "unexpected_eof":
            return 11
        if scenario == "stderr":
            print("fixture stderr warning", file=sys.stderr, flush=True)
        emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": turn_number * 10 + 2,
                    "step_type": "agent_thought",
                    "state": "ACTIVE",
                    "text_delta": "fixture-thinking",
                },
            }
        )
        if scenario == "tool_error":
            emit(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": conversation_id,
                        "step_index": turn_number * 10 + 3,
                        "step_type": "tool",
                        "state": "ACTIVE",
                        "tool_name": "view_file",
                        "tool_info": {"path": "SENSITIVE-FIXTURE-PATH"},
                    },
                }
            )
            emit(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": conversation_id,
                        "step_index": turn_number * 10 + 3,
                        "step_type": "tool",
                        "state": "ERROR",
                        "tool_name": "view_file",
                        "tool_info": {"error": "SENSITIVE-FIXTURE-PATH"},
                    },
                }
            )
        step_usage = {
            "input_tokens": 100,
            "output_tokens": 10,
            "thinking_tokens": 4,
            "cache_read_tokens": 50 if turn_number > 1 else 0,
            "total_tokens": 110,
        }
        emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": turn_number * 10 + 4,
                    "step_type": "agent_response",
                    "state": "DONE",
                    "text_delta": "fixture-answer",
                    "usage": step_usage,
                },
            }
        )
        result = {
            "event": "result",
            "result": {
                "conversation_id": conversation_id,
                "status": "ERROR" if scenario == "result_error" else "SUCCESS",
                "response": "fixture-answer",
                "num_turns": turn_number,
                "usage": {
                    "input_tokens": 100 * turn_number,
                    "output_tokens": 10 * turn_number,
                    "thinking_tokens": 4 * turn_number,
                    "cache_read_tokens": 50 * (turn_number - 1) if turn_number > 1 else 0,
                    "total_tokens": 110 * turn_number,
                },
            },
        }
        emit(result)
        if scenario == "duplicate_result":
            emit(result)
        elif scenario in {"event_after_result", "delayed_event_after_result"}:
            if scenario == "delayed_event_after_result":
                release_path = Path(os.environ["FAKE_AGY_RELEASE_TAIL"])
                deadline = time.monotonic() + 10
                while not release_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            emit(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": conversation_id,
                        "step_index": turn_number * 10 + 5,
                        "step_type": "agent_response",
                        "state": "DONE",
                        "text_delta": "late",
                    },
                }
            )
        elif scenario == "exit_after_result":
            return 12
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
