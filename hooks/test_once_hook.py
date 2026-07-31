#!/usr/bin/env python3
"""Codex lifecycle hook for the Test Once plugin."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PLUGIN_ROOT / "skills" / "test-once" / "scripts" / "test_once.py"
sys.path.insert(0, str(RUNNER.parent))

import test_once  # noqa: E402


def emit_context(event: str, context: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
        )
    )


def configured_context(repo: Path, config: dict[str, Any]) -> str:
    suite_lines = []
    for name, suite in config["suites"].items():
        suite_lines.append(f"- {name}: `{suite['command']}`")
    runner = shlex.quote(str(RUNNER))
    repo_arg = shlex.quote(str(repo))
    return (
        "Test Once shared full-suite policy is active for this repository.\n"
        + "\n".join(suite_lines)
        + "\nWhen repository-wide tests are needed, do not execute a listed command "
        "directly. Run `python3 "
        + runner
        + " --repo "
        + repo_arg
        + " run --suite <name>`. A reported `TEST-ONCE HIT: PASS` is valid "
        "verification evidence for the exact current source snapshot, command, and "
        "environment fingerprint, so do not rerun that suite. Focused tests are not "
        "covered and should run normally. Never claim a different suite passed."
    )


def command_from_payload(payload: dict[str, Any]) -> tuple[str | None, Path]:
    tool_input = payload.get("tool_input")
    cwd = Path(str(payload.get("cwd") or Path.cwd()))
    if not isinstance(tool_input, dict):
        return None, cwd
    command = tool_input.get("command")
    if not isinstance(command, str):
        command = tool_input.get("cmd")
    workdir = tool_input.get("workdir")
    if isinstance(workdir, str) and workdir:
        cwd = Path(workdir)
    return command if isinstance(command, str) else None, cwd


def wrapper_command(repo: Path, suite_name: str) -> str:
    pieces = [
        shlex.quote(sys.executable or "python3"),
        shlex.quote(str(RUNNER)),
        "--repo",
        shlex.quote(str(repo)),
        "run",
        "--suite",
        shlex.quote(suite_name),
    ]
    return " ".join(pieces)


def handle_session_start(payload: dict[str, Any]) -> None:
    cwd = Path(str(payload.get("cwd") or Path.cwd()))
    try:
        repo, _ = test_once.discover_repo(cwd)
        config = test_once.load_config(repo)
    except test_once.TestOnceError:
        return
    emit_context("SessionStart", configured_context(repo, config))


def handle_pre_tool_use(payload: dict[str, Any]) -> None:
    if payload.get("tool_name") != "Bash":
        return
    command, cwd = command_from_payload(payload)
    if not command or str(RUNNER) in command or RUNNER.name in command:
        return
    try:
        repo, _ = test_once.discover_repo(cwd)
        config = test_once.load_config(repo)
        matches = test_once.find_matching_suites(config, command)
    except test_once.TestOnceError:
        return
    if not matches:
        return
    if len(matches) > 1:
        reason = (
            "Test Once configuration is ambiguous: this command matches multiple "
            f"suites ({', '.join(matches)}). Give their `match` entries unique values."
        )
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                },
                ensure_ascii=False,
            )
        )
        return

    suite_name = matches[0]
    updated = wrapper_command(repo, suite_name)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": (
                        f"Redirected configured full suite `{suite_name}` through "
                        "the shared single-flight cache."
                    ),
                    "updatedInput": {"command": updated},
                    "additionalContext": (
                        f"Test Once redirected the direct `{suite_name}` command. "
                        "Use the rewritten result as the verification evidence."
                    ),
                }
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        event = payload.get("hook_event_name")
        if event == "SessionStart":
            handle_session_start(payload)
        elif event == "PreToolUse":
            handle_pre_tool_use(payload)
        return 0
    except Exception as exc:
        print(f"Test Once hook failed safely: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
