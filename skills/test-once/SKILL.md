---
name: test-once
description: Reuse passing configured test commands across agents with source, command, environment, and toolchain fingerprints. Use for full suites or explicitly scoped test files and groups with complete declared dependencies. Unconfigured focused tests run normally.
---

# Test Once

Run the bundled `scripts/test_once.py` by resolving it relative to this `SKILL.md`. Prefer an absolute script path. The host plugin's session hook normally supplies the exact installed command automatically.

## Apply the policy

- Cache full suites or explicitly configured test files/groups with complete input scopes. Run unconfigured focused tests directly.
- Accept `TEST-ONCE HIT: PASS` as evidence only for that configured command. A scoped test-file pass does not prove the full suite passed. The runner matches declared source inputs, command, platform, selected environment, and toolchain fingerprints.
- Never infer a full-suite pass from `HEAD` alone. Dirty tracked files and untracked files are included in the source fingerprint.
- Cache only successful runs. Let failed runs execute again.
- If the runner reports that the source changed during testing, do not claim success for the current state; stabilize the worktree and rerun.
- Do not bypass the runner for a command listed in `.test-once.json`. The host hook rewrites exact configured commands as a backstop.
- Treat repository configuration as executable code. Review `.test-once.json`, or the legacy `.codex/test-once.json`, before running a suite from an untrusted repository.

## Use an existing configuration

From the repository, run:

```bash
python3 <skill-dir>/scripts/test_once.py status --suite full-ut
python3 <skill-dir>/scripts/test_once.py run --suite full-ut
```

`run` returns the underlying test exit code on a real run and returns `0` on a valid passing cache hit. Its output includes the cache key, source identity, original command, timestamp, duration, and retained log path.

## Configure a repository

First determine the canonical full-suite command from `AGENTS.md`, CI configuration, or repository documentation. Do not guess. Then initialize:

```bash
python3 <skill-dir>/scripts/test_once.py init \
  --suite full-ut \
  --command 'go test ./...' \
  --fingerprint-command auto
```

This writes `.test-once.json`. Commit that file when the policy should apply to every local Codex, Claude Code, and Cursor session for the repository. Existing `.codex/test-once.json` files remain readable and migrate on the next successful `init` update.

For a TypeScript repository, use its real full-suite command:

```bash
python3 <skill-dir>/scripts/test_once.py init \
  --suite full-ut \
  --command 'pnpm test' \
  --fingerprint-command auto \
  --env NODE_OPTIONS
```

Use repeated `--match` values for verified equivalent alternate spellings that should be intercepted. Matching preserves exact shell text, including quoting, escapes, whitespace, and expansions. Simple unquoted `rtk` or `rtk proxy` prefixes with static executable names are ignored; uncertain wrappers remain unchanged. `rtk` is optional.

Configured commands run from the repository root. Hooks leave requests from child directories unchanged because the same text can select a different suite there. Configure a root-relative command for a child suite, and invoke it from the root or use the explicit runner.

Version 0.3.1 uses a new cache identity schema: earlier passing entries are not reused, while aggregate statistics are retained. Expect one fresh execution for each exact suite after upgrading.

Add `--env NAME` for environment variables that affect test behavior and `--extra-input PATH_OR_GLOB` for ignored files that affect tests. Use `--ttl-seconds N` only when the suite depends on time-varying external state.

For a one-off, unconfigured run:

```bash
python3 <skill-dir>/scripts/test_once.py run \
  --suite full-ut \
  --command 'your full-suite command'
```

This shares the result but cannot enable automatic hook rewriting until the repository is configured.

## Configure finer granularity

Use repeated `init --input PATH` options for the actual test files, transitive
business-code dependencies, shared code, setup hooks, fixtures, configuration,
and lockfiles. Each path is a literal repository-relative file or directory,
and must exist at initialization. Directories include tracked and non-ignored
untracked files recursively. Explicit files are included even if ignored;
`--extra-input` adds ignored files or globs. Use a separate named command for
each independently reusable test file or group.

```bash
python3 <skill-dir>/scripts/test_once.py init \
  --suite auth-file --command 'pnpm exec vitest run tests/auth.test.ts' \
  --input tests/auth.test.ts --input src --input tests/setup.ts \
  --input vitest.config.ts --input package.json --input pnpm-lock.yaml
python3 <skill-dir>/scripts/test_once.py run --suite auth-file
```

Derive paths from the repository; the example is not a universal dependency
list. Unknown dependency closure requires broader inputs or whole-repository
mode. Scoped identities ignore HEAD and staging changes: Git metadata and
external-state dependencies need explicit fingerprints. Scoped symlinks and
submodules are unsupported. This feature does not automatically infer imports
or split a full-suite command. `status --json` exposes the input roots and file
count. Omitting `inputs` preserves the existing whole-repository behavior.

## Inspect or invalidate

```bash
python3 <skill-dir>/scripts/test_once.py status --suite full-ut --json
python3 <skill-dir>/scripts/test_once.py stats --suite full-ut --json
python3 <skill-dir>/scripts/test_once.py invalidate --suite full-ut
```

`stats` reports local aggregate run requests, cache hits, and avoided test executions. `nominal_test_seconds_avoided` is the sum of original passing-run durations and is test-work accounting, not a wall-time claim. `calibrated_wall_seconds_saved` is unavailable until the current exact result has an explicit warm baseline; schema-1 statistics migrate as uncalibrated history.

To measure native runner caches for the current exact result, explicitly rerun the full suite once:

```bash
python3 <skill-dir>/scripts/test_once.py calibrate --suite full-ut
```

`calibrate` requires an existing reusable pass, intentionally starts the full test command once, and stores its warm duration only if it passes without a source change. It is optional measurement work, not required for reuse. Subsequent hits report calibrated wall-time savings while earlier or uncalibrated hits remain clearly separated.

`invalidate` moves only the current key's result into the cache trash directory so it remains recoverable.
