#!/usr/bin/env python3
"""Cursor hook adapter for the shared Test Once runner."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = PLUGIN_ROOT / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import test_once_hook as shared  # noqa: E402


test_once = shared.test_once


def payload_cwd(payload: dict[str, Any]) -> Path:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for name in ("working_directory", "workdir"):
            value = tool_input.get(name)
            if isinstance(value, str) and value:
                return Path(value)
    value = payload.get("cwd")
    if isinstance(value, str) and value:
        return Path(value)
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return Path(roots[0])
    project = os.environ.get("CURSOR_PROJECT_DIR")
    return Path(project) if project else Path.cwd()


def handle_session_start(payload: dict[str, Any]) -> None:
    try:
        repo, _ = test_once.discover_repo(payload_cwd(payload))
        config = test_once.load_config(repo)
    except test_once.TestOnceError:
        return
    print(
        json.dumps(
            {"additional_context": shared.configured_context(repo, config)},
            ensure_ascii=False,
        )
    )


def handle_pre_tool_use(payload: dict[str, Any]) -> None:
    if payload.get("tool_name") != "Shell":
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return
    if str(shared.RUNNER) in command or shared.RUNNER.name in command:
        return
    try:
        cwd = payload_cwd(payload)
        repo, _ = test_once.discover_repo(cwd)
        if cwd.expanduser().resolve() != repo:
            return
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
                    "permission": "deny",
                    "user_message": reason,
                    "agent_message": reason,
                },
                ensure_ascii=False,
            )
        )
        return

    suite_name = matches[0]
    updated = dict(tool_input)
    updated["command"] = shared.wrapper_command(repo, suite_name)
    print(
        json.dumps(
            {
                "permission": "allow",
                "updated_input": updated,
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
        if event == "sessionStart":
            handle_session_start(payload)
        elif event == "preToolUse":
            handle_pre_tool_use(payload)
        return 0
    except Exception as exc:
        print(f"Test Once Cursor hook failed safely: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
