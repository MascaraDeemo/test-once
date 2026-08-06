#!/usr/bin/env python3
"""Reproduce Test Once wall-time savings with native Go and TypeScript tests."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PLUGIN_ROOT / "skills" / "test-once" / "scripts" / "test_once.py"
SLEEP_ENV = "TEST_ONCE_BENCH_SLEEP_MS"


def run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{shlex.join(args)} failed with exit {result.returncode}:\n{result.stdout}"
        )
    return result


def git(repo: Path, *args: str) -> None:
    run(["git", *args], cwd=repo)


def initialize_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "benchmark@example.com")
    git(repo, "config", "user.name", "Test Once Benchmark")


def create_go_repo(repo: Path) -> str:
    initialize_repo(repo)
    (repo / "go.mod").write_text(
        "module example.com/test-once-benchmark\n\ngo 1.21\n",
        encoding="utf-8",
    )
    (repo / "suite_test.go").write_text(
        textwrap.dedent(
            f"""
            package benchmark

            import (
                "os"
                "strconv"
                "testing"
                "time"
            )

            func TestRepositorySuite(t *testing.T) {{
                delay, err := strconv.Atoi(os.Getenv("{SLEEP_ENV}"))
                if err != nil {{
                    t.Fatal(err)
                }}
                time.Sleep(time.Duration(delay) * time.Millisecond)
            }}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add Go benchmark suite")
    return "go test -count=1 ./..."


def create_typescript_repo(repo: Path) -> str:
    initialize_repo(repo)
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "test-once-benchmark",
                "private": True,
                "type": "module",
                "scripts": {"test": "node --test test/*.test.ts"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    test_dir = repo / "test"
    test_dir.mkdir()
    (test_dir / "suite.test.ts").write_text(
        textwrap.dedent(
            f"""
            import assert from "node:assert/strict";
            import test from "node:test";

            test("repository suite", async () => {{
              const delayMs: number = Number(process.env.{SLEEP_ENV});
              await new Promise((resolve) => setTimeout(resolve, delayMs));
              assert.equal(2 + 2, 4);
            }});
            """
        ).lstrip(),
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "add TypeScript benchmark suite")
    return "npm test --silent"


def configure_test_once(
    repo: Path,
    command: str,
    env: dict[str, str],
) -> None:
    run(
        [
            sys.executable,
            str(RUNNER),
            "--repo",
            str(repo),
            "init",
            "--suite",
            "full-ut",
            "--command",
            command,
            "--fingerprint-command",
            "auto",
            "--env",
            SLEEP_ENV,
        ],
        cwd=repo,
        env=env,
    )
    git(repo, "add", ".test-once.json")
    git(repo, "commit", "-qm", "configure Test Once")


def timed(args: list[str], *, cwd: Path, env: dict[str, str]) -> float:
    started = time.perf_counter()
    run(args, cwd=cwd, env=env)
    return time.perf_counter() - started


def benchmark_trial(
    language: str,
    *,
    iterations: int,
    sleep_ms: int,
    trial: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"test-once-{language}-") as raw:
        root = Path(raw)
        repo = root / "repo"
        cache = root / "cache"
        if language == "go":
            command = create_go_repo(repo)
            run(["go", "test", "-run", "^$", "./..."], cwd=repo)
        else:
            command = create_typescript_repo(repo)

        env = os.environ.copy()
        env[SLEEP_ENV] = str(sleep_ms)
        env["TEST_ONCE_CACHE_DIR"] = str(cache)
        configure_test_once(repo, command, env)

        direct_argv = shlex.split(command)
        direct_seconds = sum(
            timed(direct_argv, cwd=repo, env=env) for _ in range(iterations)
        )

        cached_argv = [
            sys.executable,
            str(RUNNER),
            "--repo",
            str(repo),
            "run",
            "--suite",
            "full-ut",
        ]
        test_once_seconds = sum(
            timed(cached_argv, cwd=repo, env=env) for _ in range(iterations)
        )
        stats_result = run(
            [
                sys.executable,
                str(RUNNER),
                "--repo",
                str(repo),
                "stats",
                "--suite",
                "full-ut",
                "--json",
            ],
            cwd=repo,
            env=env,
        )
        stats = json.loads(stats_result.stdout)
        return {
            "trial": trial,
            "direct_wall_seconds": direct_seconds,
            "test_once_wall_seconds": test_once_seconds,
            "executed_runs": stats["executed_runs"],
            "cache_hits": stats["cache_hits"],
            "runs_avoided": stats["runs_avoided"],
            "nominal_test_seconds_avoided": stats["nominal_test_seconds_avoided"],
        }


def command_version(command: list[str]) -> str:
    result = run(command, cwd=PLUGIN_ROOT)
    return result.stdout.strip()


def summarize(
    language: str,
    trials: list[dict[str, Any]],
) -> dict[str, Any]:
    direct = statistics.median(float(item["direct_wall_seconds"]) for item in trials)
    test_once = statistics.median(
        float(item["test_once_wall_seconds"]) for item in trials
    )
    saved = max(direct - test_once, 0.0)
    return {
        "language": language,
        "direct_wall_seconds": direct,
        "test_once_wall_seconds": test_once,
        "wall_seconds_saved": saved,
        "wall_time_reduction": saved / direct if direct else 0.0,
        "executed_runs": int(
            statistics.median(int(item["executed_runs"]) for item in trials)
        ),
        "cache_hits": int(
            statistics.median(int(item["cache_hits"]) for item in trials)
        ),
        "runs_avoided": int(
            statistics.median(int(item["runs_avoided"]) for item in trials)
        ),
        "trials": trials,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Test Once controlled benchmark",
        "",
        (
            f"- Workload: {payload['iterations']} independent full-suite requests, "
            f"{payload['sleep_ms'] / 1000:.1f}s deterministic test body"
        ),
        f"- Result: median of {payload['trial_count']} trials",
        (
            f"- Host: {payload['platform']}; {payload['python_version']}; "
            f"{payload['go_version']}; Node {payload['node_version']}; "
            f"npm {payload['npm_version']}"
        ),
        "",
        (
            "| Suite | Direct total | Test Once total | Underlying runs | "
            "Cache hits | Wall time saved | Reduction |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for result in payload["results"]:
        lines.append(
            f"| {result['language']} "
            f"| {result['direct_wall_seconds']:.3f}s "
            f"| {result['test_once_wall_seconds']:.3f}s "
            f"| {result['executed_runs']} "
            f"| {result['cache_hits']} "
            f"| {result['wall_seconds_saved']:.3f}s "
            f"| {result['wall_time_reduction']:.1%} |"
        )
    lines.extend(
        [
            "",
            (
                "The direct baseline executes the native test command every time. "
                "The Test Once total includes Python startup, Git source "
                "fingerprinting, environment/toolchain fingerprinting, locking, "
                "and cache lookup."
            ),
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--sleep-ms", type=int, default=1000)
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.iterations < 2 or args.trials < 1 or args.sleep_ms < 1:
        raise SystemExit(
            "--iterations must be at least 2; --trials and --sleep-ms must be positive"
        )
    for executable in ("git", "go", "node", "npm"):
        if shutil.which(executable) is None:
            raise SystemExit(f"Required benchmark executable not found: {executable}")

    results = []
    for language in ("Go", "TypeScript"):
        trials = [
            benchmark_trial(
                language.lower(),
                iterations=args.iterations,
                sleep_ms=args.sleep_ms,
                trial=trial,
            )
            for trial in range(1, args.trials + 1)
        ]
        results.append(summarize(language, trials))

    payload = {
        "iterations": args.iterations,
        "trial_count": args.trials,
        "sleep_ms": args.sleep_ms,
        "platform": f"{platform.system()} {platform.machine()}",
        "python_version": f"Python {platform.python_version()}",
        "go_version": command_version(["go", "version"]),
        "node_version": command_version(["node", "--version"]),
        "npm_version": command_version(["npm", "--version"]),
        "results": results,
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
