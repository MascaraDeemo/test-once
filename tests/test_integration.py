from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PLUGIN_ROOT / "skills" / "test-once" / "scripts" / "test_once.py"
HOOK = PLUGIN_ROOT / "hooks" / "test_once_hook.py"


class TestOnceIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.cache = self.base / "cache"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def command(
        self,
        repo: Path,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        merged = os.environ.copy()
        merged["TEST_ONCE_CACHE_DIR"] = str(self.cache)
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(RUNNER), "--repo", str(repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=merged,
            check=check,
        )

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def make_repo(self, name: str) -> tuple[Path, Path, str]:
        repo = self.base / name
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.email", "test@example.com")
        self.git(repo, "config", "user.name", "Test")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        (repo / "runner.py").write_text(
            textwrap.dedent(
                """
                import os
                from pathlib import Path
                import sys
                import time

                counter = Path(sys.argv[1])
                count = int(counter.read_text() or "0") if counter.exists() else 0
                counter.write_text(str(count + 1))
                print(f"underlying-run={count + 1}", flush=True)
                if mutation := os.environ.get("RUNNER_MUTATE"):
                    Path(mutation).write_text("changed during test\\n")
                time.sleep(float(os.environ.get("RUNNER_SLEEP", "0")))
                raise SystemExit(int(os.environ.get("RUNNER_EXIT", "0")))
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "initial")
        counter = self.base / f"{name}-count.txt"
        test_command = shlex.join([sys.executable, "runner.py", str(counter)])
        self.command(
            repo,
            "init",
            "--command",
            test_command,
            "--fingerprint-command",
            "none",
            "--env",
            "RUNNER_SLEEP",
            "--env",
            "RUNNER_EXIT",
            "--env",
            "RUNNER_MUTATE",
        )
        self.git(repo, "add", ".codex/test-once.json")
        self.git(repo, "commit", "-qm", "configure test once")
        return repo, counter, test_command

    def test_success_is_reused_and_dirty_change_gets_new_key(self) -> None:
        repo, counter, _ = self.make_repo("reuse")
        first = self.command(repo, "run")
        second = self.command(repo, "run")
        self.assertIn("TEST-ONCE STORED: PASS", first.stdout)
        self.assertIn("TEST-ONCE HIT: PASS", second.stdout)
        self.assertEqual(counter.read_text(), "1")

        (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        third = self.command(repo, "run")
        fourth = self.command(repo, "run")
        self.assertIn("TEST-ONCE STORED: PASS", third.stdout)
        self.assertIn("TEST-ONCE HIT: PASS", fourth.stdout)
        self.assertEqual(counter.read_text(), "2")

        (repo / "untracked.txt").write_text("first\n", encoding="utf-8")
        fifth = self.command(repo, "run")
        sixth = self.command(repo, "run")
        self.assertIn("TEST-ONCE STORED: PASS", fifth.stdout)
        self.assertIn("TEST-ONCE HIT: PASS", sixth.stdout)
        self.assertEqual(counter.read_text(), "3")

        (repo / "untracked.txt").write_text("second\n", encoding="utf-8")
        seventh = self.command(repo, "run")
        self.assertIn("TEST-ONCE STORED: PASS", seventh.stdout)
        self.assertEqual(counter.read_text(), "4")

    def test_stats_reports_executed_and_avoided_test_work(self) -> None:
        repo, counter, _ = self.make_repo("stats")
        env = {"RUNNER_EXIT": "0", "RUNNER_SLEEP": "0.2"}

        first = self.command(repo, "run", env=env)
        second = self.command(repo, "run", env=env)
        self.command(repo, "status", "--json", env=env)
        stats = self.command(repo, "stats", "--json", env=env)
        payload = json.loads(stats.stdout)

        self.assertIn("TEST-ONCE STORED: PASS", first.stdout)
        self.assertIn("TEST-ONCE HIT: PASS", second.stdout)
        self.assertEqual(counter.read_text(), "1")
        self.assertEqual(payload["suite"], "full-ut")
        self.assertEqual(payload["run_requests"], 2)
        self.assertEqual(payload["executed_runs"], 1)
        self.assertEqual(payload["passed_runs"], 1)
        self.assertEqual(payload["failed_runs"], 0)
        self.assertEqual(payload["unstable_runs"], 0)
        self.assertEqual(payload["cache_hits"], 1)
        self.assertEqual(payload["runs_avoided"], 1)
        self.assertEqual(payload["cache_hit_rate"], 0.5)
        self.assertGreaterEqual(payload["executed_test_seconds"], 0.15)
        self.assertGreaterEqual(payload["test_seconds_avoided"], 0.15)
        self.assertGreater(payload["estimated_wall_seconds_saved"], 0)
        self.assertGreaterEqual(payload["hit_resolution_seconds"], 0)
        self.assertIsInstance(payload["first_event_at"], str)
        self.assertIsInstance(payload["last_event_at"], str)
        self.assertTrue(payload["stats_path"].endswith("stats.json"))
        human = self.command(repo, "stats", env=env).stdout
        self.assertIn("TEST-ONCE STATS", human)
        self.assertIn("runs_avoided: 1", human)
        self.assertIn("cache_hit_rate: 50.0%", human)

    def test_concurrent_sessions_execute_underlying_command_once(self) -> None:
        repo, counter, _ = self.make_repo("concurrent")
        env = os.environ.copy()
        env.update(
            {
                "TEST_ONCE_CACHE_DIR": str(self.cache),
                "RUNNER_SLEEP": "0.8",
                "RUNNER_EXIT": "0",
            }
        )
        argv = [sys.executable, str(RUNNER), "--repo", str(repo), "run"]
        first = subprocess.Popen(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        time.sleep(0.1)
        second = subprocess.Popen(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        first_output, _ = first.communicate(timeout=10)
        second_output, _ = second.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, first_output)
        self.assertEqual(second.returncode, 0, second_output)
        self.assertEqual(counter.read_text(), "1")
        self.assertIn("TEST-ONCE STORED: PASS", first_output + second_output)
        self.assertIn("TEST-ONCE HIT: PASS", first_output + second_output)
        self.assertIn("TEST-ONCE WAIT:", first_output + second_output)
        stats = json.loads(
            self.command(
                repo,
                "stats",
                "--json",
                env={"RUNNER_SLEEP": "0.8", "RUNNER_EXIT": "0"},
            ).stdout
        )
        self.assertEqual(stats["run_requests"], 2)
        self.assertEqual(stats["executed_runs"], 1)
        self.assertEqual(stats["passed_runs"], 1)
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(stats["runs_avoided"], 1)

    def test_clean_worktrees_share_the_same_commit_result(self) -> None:
        repo, counter, _ = self.make_repo("worktrees")
        first = self.command(repo, "run")
        worktree = self.base / "worktrees-review"
        self.git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        second = self.command(worktree, "run")
        self.assertIn("TEST-ONCE STORED: PASS", first.stdout)
        self.assertIn("TEST-ONCE HIT: PASS", second.stdout)
        self.assertEqual(counter.read_text(), "1")

    def test_selected_environment_change_gets_a_new_key(self) -> None:
        repo, counter, _ = self.make_repo("environment")
        first = self.command(
            repo, "run", env={"RUNNER_EXIT": "0", "RUNNER_SLEEP": "0"}
        )
        second = self.command(
            repo, "run", env={"RUNNER_EXIT": "0", "RUNNER_SLEEP": "0.01"}
        )
        third = self.command(
            repo, "run", env={"RUNNER_EXIT": "0", "RUNNER_SLEEP": "0.01"}
        )
        self.assertIn("TEST-ONCE STORED: PASS", first.stdout)
        self.assertIn("TEST-ONCE STORED: PASS", second.stdout)
        self.assertIn("TEST-ONCE HIT: PASS", third.stdout)
        self.assertEqual(counter.read_text(), "2")

    def test_pnpm_status_fingerprints_node_and_package_manager_versions(self) -> None:
        repo, _, _ = self.make_repo("pnpm-toolchain")
        tool_bin = self.base / "pnpm-toolchain-bin"
        tool_bin.mkdir()
        (tool_bin / "node").write_text(
            "#!/bin/sh\nprintf 'v22.0.0\\n'\n", encoding="utf-8"
        )
        (tool_bin / "pnpm").write_text(
            "#!/bin/sh\nprintf '9.0.0\\n'\n", encoding="utf-8"
        )
        (tool_bin / "node").chmod(0o755)
        (tool_bin / "pnpm").chmod(0o755)
        self.command(
            repo,
            "init",
            "--command",
            "pnpm test",
            "--fingerprint-command",
            "auto",
            "--force",
        )

        status = self.command(
            repo,
            "status",
            "--json",
            env={"PATH": f"{tool_bin}{os.pathsep}{os.environ['PATH']}"},
        )
        payload = json.loads(status.stdout)

        self.assertEqual(
            payload["environment_details"]["fingerprint_command"],
            "node --version && pnpm --version",
        )

    def test_yarn_status_fingerprints_node_and_package_manager_versions(self) -> None:
        repo, _, _ = self.make_repo("yarn-toolchain")
        tool_bin = self.base / "yarn-toolchain-bin"
        tool_bin.mkdir()
        (tool_bin / "node").write_text(
            "#!/bin/sh\nprintf 'v22.0.0\\n'\n", encoding="utf-8"
        )
        (tool_bin / "yarn").write_text(
            "#!/bin/sh\nprintf '4.0.0\\n'\n", encoding="utf-8"
        )
        (tool_bin / "node").chmod(0o755)
        (tool_bin / "yarn").chmod(0o755)
        self.command(
            repo,
            "init",
            "--command",
            "yarn test",
            "--fingerprint-command",
            "auto",
            "--force",
        )

        status = self.command(
            repo,
            "status",
            "--json",
            env={"PATH": f"{tool_bin}{os.pathsep}{os.environ['PATH']}"},
        )
        payload = json.loads(status.stdout)

        self.assertEqual(
            payload["environment_details"]["fingerprint_command"],
            "node --version && yarn --version",
        )

    def test_failure_is_not_reused(self) -> None:
        repo, counter, _ = self.make_repo("failure")
        env = {"RUNNER_EXIT": "7", "RUNNER_SLEEP": "0"}
        first = self.command(repo, "run", env=env, check=False)
        second = self.command(repo, "run", env=env, check=False)
        self.assertEqual(first.returncode, 7, first.stdout)
        self.assertEqual(second.returncode, 7, second.stdout)
        self.assertEqual(counter.read_text(), "2")
        self.assertIn("failures are never reused", first.stdout)
        self.assertNotIn("TEST-ONCE HIT", first.stdout + second.stdout)
        stats = json.loads(self.command(repo, "stats", "--json", env=env).stdout)
        self.assertEqual(stats["run_requests"], 2)
        self.assertEqual(stats["executed_runs"], 2)
        self.assertEqual(stats["failed_runs"], 2)
        self.assertEqual(stats["passed_runs"], 0)
        self.assertEqual(stats["cache_hits"], 0)

    def test_source_change_during_run_is_counted_as_unstable(self) -> None:
        repo, counter, _ = self.make_repo("unstable")
        env = {
            "RUNNER_EXIT": "0",
            "RUNNER_SLEEP": "0",
            "RUNNER_MUTATE": str(repo / "tracked.txt"),
        }

        result = self.command(repo, "run", env=env, check=False)
        stats = json.loads(self.command(repo, "stats", "--json", env=env).stdout)

        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("repository source changed", result.stdout)
        self.assertEqual(counter.read_text(), "1")
        self.assertEqual(stats["run_requests"], 1)
        self.assertEqual(stats["executed_runs"], 1)
        self.assertEqual(stats["unstable_runs"], 1)
        self.assertEqual(stats["passed_runs"], 0)
        self.assertEqual(stats["cache_hits"], 0)

    def test_corrupt_stats_never_breaks_cached_test_result(self) -> None:
        repo, counter, _ = self.make_repo("corrupt-stats")
        first = self.command(repo, "run")
        current = json.loads(self.command(repo, "stats", "--json").stdout)
        Path(current["stats_path"]).write_text(
            json.dumps(
                {
                    "stats_schema": current["stats_schema"],
                    "repo_id": current["repo_id"],
                    "updated_at": None,
                    "suites": {"full-ut": {}},
                }
            ),
            encoding="utf-8",
        )

        second = self.command(repo, "run")
        stats = self.command(repo, "stats", "--json", check=False)

        self.assertIn("TEST-ONCE STORED: PASS", first.stdout)
        self.assertIn("TEST-ONCE HIT: PASS", second.stdout)
        self.assertIn("could not record statistics", second.stdout)
        self.assertEqual(counter.read_text(), "1")
        self.assertEqual(stats.returncode, 2)
        self.assertIn("incompatible Test Once statistics", stats.stdout)

    def test_non_finite_stats_are_rejected(self) -> None:
        repo, _, _ = self.make_repo("non-finite-stats")
        self.command(repo, "run")
        current = json.loads(self.command(repo, "stats", "--json").stdout)
        stats_path = Path(current["stats_path"])
        stored = json.loads(stats_path.read_text(encoding="utf-8"))
        stored["suites"]["full-ut"]["executed_test_seconds"] = float("nan")
        stats_path.write_text(json.dumps(stored), encoding="utf-8")

        stats = self.command(repo, "stats", "--json", check=False)

        self.assertEqual(stats.returncode, 2)
        self.assertIn("incompatible Test Once statistics", stats.stdout)

    def test_hook_injects_policy_and_rewrites_only_exact_suite(self) -> None:
        repo, _, test_command = self.make_repo("hook")
        start_payload = {
            "hook_event_name": "SessionStart",
            "cwd": str(repo),
            "source": "startup",
        }
        started = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(start_payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        start_result = json.loads(started.stdout)
        context = start_result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("shared full-suite policy is active", context)
        self.assertIn("full-ut", context)

        pre_payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "cwd": str(repo),
            "tool_input": {"command": f"rtk {test_command}"},
        }
        rewritten = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(pre_payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        pre_result = json.loads(rewritten.stdout)
        output = pre_result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertFalse(output["updatedInput"]["command"].startswith("rtk "))
        self.assertIn("test_once.py", output["updatedInput"]["command"])
        self.assertIn("run --suite full-ut", output["updatedInput"]["command"])

        pre_payload["tool_input"]["command"] = "pytest tests/test_small.py"
        untouched = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(pre_payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertEqual(untouched.stdout, "")


if __name__ == "__main__":
    unittest.main()
