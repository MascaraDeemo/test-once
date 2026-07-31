# Test Once

Test Once prevents Codex development, review, and parallel sessions from repeatedly running the same expensive repository-wide test suite.

For an exact repository snapshot, test command, environment, platform, and toolchain fingerprint, the underlying suite runs at most once at a time. A passing result is then reused by other sessions. Failed runs are never cached.

Local aggregate statistics show how many test executions and seconds were avoided without recording source, commands, or environment values.

## Why the key is more than `HEAD`

A commit ID alone is not enough to prove that two test inputs are identical. Test Once also accounts for:

- dirty tracked files, staged changes, and untracked files;
- initialized submodules and sparse-checkout state;
- the configured full-suite command;
- operating system and architecture;
- selected environment variables and relevant default variables;
- the executable and toolchain version.

Clean Git worktrees that share the same common repository directory and commit share a result. Separate clones remain isolated.

## Requirements

- Codex with plugin lifecycle-hook support
- Git
- Python 3.10 or newer
- macOS or Linux

Windows is not supported in the current release; its locking and process-control paths have not been verified.

The runner is language-agnostic. Go, TypeScript, Rust, Python, Java, Bazel, and other projects work as long as the repository has a deterministic shell command for its full suite.

## Install from source

Until the plugin is accepted into the public Plugins Directory, install it through a personal marketplace:

```bash
git clone https://github.com/MascaraDeemo/test-once.git ~/plugins/test-once
```

Add this entry to the `plugins` array in `~/.agents/plugins/marketplace.json`:

```json
{
  "name": "test-once",
  "source": {
    "source": "local",
    "path": "./plugins/test-once"
  },
  "policy": {
    "installation": "AVAILABLE",
    "authentication": "ON_INSTALL"
  },
  "category": "Productivity"
}
```

Then install it:

```bash
codex plugin add test-once@personal
```

Start a new Codex task after installation and review the plugin hook when Codex asks you to trust it.

## Configure a repository

Use the runner bundled with the plugin. Replace `<plugin-root>` with the installed plugin directory.

For Go:

```bash
python3 <plugin-root>/skills/test-once/scripts/test_once.py init \
  --suite full-ut \
  --command 'go test ./...' \
  --fingerprint-command auto
```

For TypeScript:

```bash
python3 <plugin-root>/skills/test-once/scripts/test_once.py init \
  --suite full-ut \
  --command 'pnpm test' \
  --fingerprint-command auto \
  --env NODE_OPTIONS
```

Use the actual full-suite command from the repository's CI or documentation. Do not substitute a focused-test command.

The command creates `.codex/test-once.json`. Commit that file if every Codex session in the repository should use the same policy. Add repeated `--match` options for exact alternate spellings that the hook should intercept.

If a command is wrapped by `rtk` or `rtk proxy`, Test Once ignores that leading wrapper while matching. `rtk` is optional and is never required to run the plugin.

## Run, inspect, measure, and invalidate

```bash
python3 <plugin-root>/skills/test-once/scripts/test_once.py run --suite full-ut
python3 <plugin-root>/skills/test-once/scripts/test_once.py status --suite full-ut
python3 <plugin-root>/skills/test-once/scripts/test_once.py stats --suite full-ut
python3 <plugin-root>/skills/test-once/scripts/test_once.py invalidate --suite full-ut
```

On a miss, the runner acquires a per-key file lock and executes the suite. Concurrent sessions wait for that lock and then reuse the passing result. On a hit it prints:

```text
TEST-ONCE HIT: PASS
```

The result includes the exact key, source identity, command, timestamp, original duration, and retained log path.

Focused tests continue to run normally because the hook rewrites only exact commands configured for a full suite.

`stats` reports:

- `runs_avoided`: full-suite processes that did not need to start;
- `test_seconds_avoided`: sum of the original durations of reused passing runs;
- `estimated_wall_seconds_saved`: original durations minus cache lookup or single-flight waiting time, clamped at zero;
- execution counts for passed, failed, and source-unstable runs.

Only `run` requests affect statistics. `status` checks do not. Statistics are aggregated per repository and suite in the local Test Once cache.

## Configuration controls

- `--env NAME` includes an additional environment variable in the key.
- `--extra-input PATH_OR_GLOB` includes ignored or generated repository files that affect tests.
- `--ttl-seconds N` expires passing results that depend on time-varying external state.
- `--match COMMAND` adds an exact command spelling for hook interception.
- `--fingerprint-command COMMAND` fingerprints a custom toolchain; use `auto` for known tools or `none` only when toolchain changes cannot affect the suite.

The default auto-detection covers common Go, Node.js, Python, Rust, Bazel, Maven, Gradle, and Make toolchains.

## Safety and limits

- Test Once stores data only on the local machine and makes no network requests itself.
- Statistics contain aggregate counters, durations, timestamps, the suite name, and a hash of the local repository identity; they do not contain source, commands, or environment values.
- Test output logs may contain sensitive data. They remain in the local cache under `~/Library/Caches/test-once` on macOS, `$XDG_CACHE_HOME/test-once` when that variable is set on Linux, or `~/.cache/test-once` otherwise.
- `.codex/test-once.json` contains shell commands. Review it before using the plugin in an untrusted repository.
- Ignored files are not part of the source snapshot unless configured with `--extra-input`.
- A cached local pass is not a replacement for required CI, release checks, flaky-test detection, or tests that intentionally exercise changing external systems.
- Only passing results are reusable. A source change during execution prevents the result from being stored.

## Development

Run the integration suite:

```bash
python3 -m unittest discover -s tests -v
```

The tests exercise cache reuse, dirty snapshots, clean worktrees, single-flight concurrency, savings statistics, failed and unstable runs, corrupt-stat isolation, environment changes, TypeScript toolchain fingerprints, and hook rewriting.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
