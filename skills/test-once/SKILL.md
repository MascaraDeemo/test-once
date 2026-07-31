---
name: test-once
description: Reuse a passing repository-wide unit-test or integration-test result across Codex development, review, and parallel sessions with an exact source snapshot, command, and environment fingerprint plus single-flight locking. Use whenever Codex is about to run an expensive full test suite, verify whether a full suite already passed, configure shared test caching for a large repository, or avoid duplicate test runs at the same revision. Do not use for focused or targeted tests.
---

# Test Once

Run the bundled `scripts/test_once.py` by resolving it relative to this `SKILL.md`. Prefer an absolute script path. The plugin's session hook normally supplies the exact installed command automatically.

## Apply the policy

- Treat only a configured repository-wide suite as cacheable. Run focused tests directly.
- Accept a cache hit as current full-suite evidence only when the runner reports `TEST-ONCE HIT: PASS`; the runner has already matched the source snapshot, command, platform, selected environment, and toolchain fingerprint.
- Never infer a full-suite pass from `HEAD` alone. Dirty tracked files and untracked files are included in the source fingerprint.
- Cache only successful runs. Let failed runs execute again.
- If the runner reports that the source changed during testing, do not claim success for the current state; stabilize the worktree and rerun.
- Do not bypass the runner for a command listed in `.codex/test-once.json`. The `PreToolUse` hook rewrites exact configured commands as a backstop.
- Treat repository configuration as executable code. Review `.codex/test-once.json` before running a suite from an untrusted repository.

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

This writes `.codex/test-once.json`. Commit that file when the policy should apply to every local Codex session for the repository.

For a TypeScript repository, use its real full-suite command:

```bash
python3 <skill-dir>/scripts/test_once.py init \
  --suite full-ut \
  --command 'pnpm test' \
  --fingerprint-command auto \
  --env NODE_OPTIONS
```

Use repeated `--match` values for exact alternate spellings that should be intercepted. Matching is token-based and intentionally not substring- or regex-based. A leading `rtk` or `rtk proxy` wrapper is ignored during matching; `rtk` is optional.

Add `--env NAME` for environment variables that affect test behavior and `--extra-input PATH_OR_GLOB` for ignored files that affect tests. Use `--ttl-seconds N` only when the suite depends on time-varying external state.

For a one-off, unconfigured run:

```bash
python3 <skill-dir>/scripts/test_once.py run \
  --suite full-ut \
  --command 'your full-suite command'
```

This shares the result but cannot enable automatic hook rewriting until the repository is configured.

## Inspect or invalidate

```bash
python3 <skill-dir>/scripts/test_once.py status --suite full-ut --json
python3 <skill-dir>/scripts/test_once.py stats --suite full-ut --json
python3 <skill-dir>/scripts/test_once.py invalidate --suite full-ut
```

`stats` reports local aggregate run requests, cache hits, avoided test executions, avoided test duration, and estimated wall time saved. It does not count `status` checks.

`invalidate` moves only the current key's result into the cache trash directory so it remains recoverable.
