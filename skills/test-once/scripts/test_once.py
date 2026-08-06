#!/usr/bin/env python3
"""Single-flight, content-aware cache for expensive repository test suites."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import glob
import hashlib
import json
import math
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator


VERSION = "0.3.0"
CONFIG_SCHEMA = 1
CACHE_SCHEMA = 1
STATS_SCHEMA = 2
CONFIG_RELATIVE_PATH = Path(".test-once.json")
LEGACY_CONFIG_RELATIVE_PATHS = (Path(".codex") / "test-once.json",)
UNSTABLE_SOURCE_EXIT = 75
DEFAULT_ENVIRONMENT = (
    "CI",
    "CONDA_PREFIX",
    "GOARCH",
    "GOFLAGS",
    "GOOS",
    "GOPATH",
    "GOROOT",
    "JAVA_HOME",
    "LANG",
    "NODE_ENV",
    "PATH",
    "PYTHONPATH",
    "RUSTFLAGS",
    "SHELL",
    "TEST_ENV",
    "TZ",
    "VIRTUAL_ENV",
)


class TestOnceError(RuntimeError):
    """A user-actionable Test Once error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def run_capture(
    args: list[str], cwd: Path, *, check: bool = True, timeout: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TestOnceError(f"Failed to run {shlex.join(args)}: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise TestOnceError(
            f"{shlex.join(args)} failed with exit {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    return result


def discover_repo(start: Path | str) -> tuple[Path, Path]:
    start_path = Path(start).expanduser().resolve()
    root_result = run_capture(
        ["git", "-C", str(start_path), "rev-parse", "--show-toplevel"],
        start_path,
    )
    root = Path(os.fsdecode(root_result.stdout).strip()).resolve()
    common_result = run_capture(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"], root
    )
    common_raw = Path(os.fsdecode(common_result.stdout).strip())
    common = (
        (root / common_raw).resolve()
        if not common_raw.is_absolute()
        else common_raw.resolve()
    )
    return root, common


def validate_string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise TestOnceError(f"`{field}` must be an array of non-empty strings")
    return list(value)


def validate_config(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TestOnceError(f"{path} must contain a JSON object")
    if data.get("schema_version") != CONFIG_SCHEMA:
        raise TestOnceError(
            f"{path} uses unsupported schema_version {data.get('schema_version')!r}; "
            f"expected {CONFIG_SCHEMA}"
        )
    suites = data.get("suites")
    if not isinstance(suites, dict) or not suites:
        raise TestOnceError(f"{path} must define at least one suite")
    normalized: dict[str, Any] = {"schema_version": CONFIG_SCHEMA, "suites": {}}
    for name, raw in suites.items():
        if not isinstance(name, str) or not name:
            raise TestOnceError("Suite names must be non-empty strings")
        if not isinstance(raw, dict):
            raise TestOnceError(f"Suite `{name}` must be an object")
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise TestOnceError(f"Suite `{name}` must define a non-empty `command`")
        ttl = raw.get("ttl_seconds")
        if ttl is not None and (not isinstance(ttl, int) or ttl <= 0):
            raise TestOnceError(f"Suite `{name}` ttl_seconds must be a positive integer")
        fingerprint_command = raw.get("fingerprint_command", "auto")
        if not isinstance(fingerprint_command, str):
            raise TestOnceError(
                f"Suite `{name}` fingerprint_command must be a string"
            )
        normalized["suites"][name] = {
            "command": command.strip(),
            "match": validate_string_list(raw.get("match"), f"{name}.match"),
            "fingerprint_command": fingerprint_command.strip() or "none",
            "environment": validate_string_list(
                raw.get("environment"), f"{name}.environment"
            ),
            "extra_inputs": validate_string_list(
                raw.get("extra_inputs"), f"{name}.extra_inputs"
            ),
            "ttl_seconds": ttl,
        }
    return normalized


def read_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TestOnceError(f"Cannot read {path}: {exc}") from exc
    return validate_config(data, path)


def configuration_path(repo: Path) -> Path:
    canonical = repo / CONFIG_RELATIVE_PATH
    legacy = [repo / relative for relative in LEGACY_CONFIG_RELATIVE_PATHS]
    existing = [path for path in [canonical, *legacy] if path.is_file()]
    if not existing:
        accepted = ", ".join(
            str(path) for path in [CONFIG_RELATIVE_PATH, *LEGACY_CONFIG_RELATIVE_PATHS]
        )
        raise TestOnceError(
            f"No Test Once configuration ({accepted}) found under repository {repo}"
        )
    if canonical in existing and len(existing) > 1:
        canonical_config = read_config(canonical)
        for legacy_path in existing[1:]:
            if read_config(legacy_path) != canonical_config:
                raise TestOnceError(
                    "Conflicting Test Once configurations found at "
                    f"{canonical} and {legacy_path}; keep one authoritative file"
                )
        return canonical
    return existing[0]


def load_config(repo: Path) -> dict[str, Any]:
    return read_config(configuration_path(repo))


def command_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return [re.sub(r"\s+", " ", command.strip())]
    if tokens and Path(tokens[0]).name == "rtk":
        tokens = tokens[1:]
        if tokens and tokens[0] == "proxy":
            tokens = tokens[1:]
    return tokens


def normalize_command_for_match(command: str) -> str:
    return "\x1f".join(command_tokens(command))


def find_matching_suites(config: dict[str, Any], command: str) -> list[str]:
    wanted = normalize_command_for_match(command)
    matches: list[str] = []
    for name, suite in config["suites"].items():
        candidates = [suite["command"], *suite.get("match", [])]
        if any(normalize_command_for_match(item) == wanted for item in candidates):
            matches.append(name)
    return matches


def hash_file(hasher: Any, path: Path, logical_name: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        hasher.update(
            f"missing\0{logical_name}\0{exc}".encode("utf-8", "surrogateescape")
        )
        return
    hasher.update(logical_name.encode("utf-8", "surrogateescape"))
    hasher.update(b"\0")
    hasher.update(str(stat.S_IMODE(info.st_mode)).encode())
    hasher.update(b"\0")
    if stat.S_ISLNK(info.st_mode):
        hasher.update(b"symlink\0")
        hasher.update(os.readlink(path).encode("utf-8", "surrogateescape"))
    elif stat.S_ISREG(info.st_mode):
        hasher.update(b"file\0")
        hasher.update(str(info.st_size).encode())
        hasher.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
    else:
        hasher.update(f"special:{info.st_mode}:{info.st_size}".encode())
    hasher.update(b"\0")


def hash_git_diff(hasher: Any, repo: Path, head: str) -> None:
    process = subprocess.Popen(
        ["git", "-C", str(repo), "diff", "--binary", "--no-ext-diff", head, "--"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    while chunk := process.stdout.read(1024 * 1024):
        hasher.update(chunk)
    stderr = process.stderr.read() if process.stderr is not None else b""
    if process.wait() != 0:
        raise TestOnceError(
            "Unable to fingerprint Git diff: "
            + stderr.decode("utf-8", "replace").strip()
        )


def sparse_checkout_identity(repo: Path) -> str | None:
    enabled = run_capture(
        ["git", "-C", str(repo), "config", "--bool", "core.sparseCheckout"],
        repo,
        check=False,
    )
    if enabled.returncode != 0 or enabled.stdout.strip() != b"true":
        return None
    listing = run_capture(
        ["git", "-C", str(repo), "sparse-checkout", "list"],
        repo,
        check=False,
    )
    payload = listing.stdout if listing.returncode == 0 else b"<legacy-sparse-checkout>"
    return hashlib.sha256(payload).hexdigest()


def initialized_submodule_roots(repo: Path) -> list[Path]:
    if not (repo / ".gitmodules").is_file():
        return []
    result = run_capture(
        ["git", "-C", str(repo), "submodule", "foreach", "--quiet", "--recursive", "pwd"],
        repo,
        check=False,
    )
    if result.returncode != 0:
        return []
    roots: set[Path] = set()
    for raw in result.stdout.splitlines():
        if raw:
            candidate = Path(os.fsdecode(raw)).resolve()
            with contextlib.suppress(ValueError):
                candidate.relative_to(repo)
                roots.add(candidate)
    return sorted(roots, key=lambda item: str(item))


def hash_dirty_submodules(hasher: Any, repo: Path) -> None:
    for submodule in initialized_submodule_roots(repo):
        status_result = run_capture(
            [
                "git",
                "-C",
                str(submodule),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            submodule,
            check=False,
        )
        if status_result.returncode != 0 or not status_result.stdout:
            continue
        relative = submodule.relative_to(repo)
        hasher.update(f"submodule:{relative}\0".encode("utf-8", "surrogateescape"))
        hasher.update(status_result.stdout)
        hasher.update(b"\0")
        head_result = run_capture(
            ["git", "-C", str(submodule), "rev-parse", "--verify", "HEAD"],
            submodule,
            check=False,
        )
        if head_result.returncode == 0:
            submodule_head = os.fsdecode(head_result.stdout).strip()
            hasher.update(submodule_head.encode())
            hasher.update(b"\0")
            hash_git_diff(hasher, submodule, submodule_head)
        untracked_result = run_capture(
            [
                "git",
                "-C",
                str(submodule),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            submodule,
            check=False,
        )
        if untracked_result.returncode == 0:
            for raw_name in filter(None, untracked_result.stdout.split(b"\0")):
                name = os.fsdecode(raw_name)
                hash_file(
                    hasher,
                    submodule / name,
                    f"submodule-untracked:{relative}/{name}",
                )


def checked_extra_paths(repo: Path, patterns: list[str]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in patterns:
        pattern_path = Path(pattern)
        if pattern_path.is_absolute():
            raise TestOnceError(f"extra_inputs must be repository-relative: {pattern}")
        for raw in glob.glob(str(repo / pattern), recursive=True):
            candidate = Path(raw).resolve()
            try:
                candidate.relative_to(repo)
            except ValueError as exc:
                raise TestOnceError(
                    f"extra_inputs escapes the repository: {pattern}"
                ) from exc
            if candidate.is_file() or candidate.is_symlink():
                paths.add(candidate)
    return sorted(paths, key=lambda item: str(item))


def source_identity(repo: Path, suite: dict[str, Any]) -> dict[str, Any]:
    head_result = run_capture(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        repo,
        check=False,
    )
    head = (
        os.fsdecode(head_result.stdout).strip()
        if head_result.returncode == 0
        else "UNBORN"
    )
    status_result = run_capture(
        [
            "git",
            "-C",
            str(repo),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        repo,
    )
    dirty = bool(status_result.stdout)
    extra_paths = checked_extra_paths(repo, suite.get("extra_inputs", []))
    sparse = sparse_checkout_identity(repo)
    if not dirty and not extra_paths and sparse is None and head != "UNBORN":
        return {
            "head": head,
            "dirty": False,
            "fingerprint": f"git:{head}",
        }
    if not dirty and not extra_paths and sparse is not None and head != "UNBORN":
        return {
            "head": head,
            "dirty": False,
            "fingerprint": f"git:{head}:sparse:{sparse}",
        }

    hasher = hashlib.sha256()
    hasher.update(b"test-once-source-v1\0")
    hasher.update(head.encode())
    hasher.update(b"\0")
    hasher.update(status_result.stdout)
    hasher.update(b"\0")
    if sparse is not None:
        hasher.update(f"sparse:{sparse}\0".encode())
    if head != "UNBORN":
        hash_git_diff(hasher, repo, head)
    else:
        tracked_result = run_capture(
            [
                "git",
                "-C",
                str(repo),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            repo,
        )
        for raw_name in filter(None, tracked_result.stdout.split(b"\0")):
            name = os.fsdecode(raw_name)
            hash_file(hasher, repo / name, name)

    untracked_result = run_capture(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        repo,
    )
    for raw_name in filter(None, untracked_result.stdout.split(b"\0")):
        name = os.fsdecode(raw_name)
        hash_file(hasher, repo / name, f"untracked:{name}")
    hash_dirty_submodules(hasher, repo)
    for path in extra_paths:
        hash_file(hasher, path, f"extra:{path.relative_to(repo)}")
    digest = hasher.hexdigest()
    return {
        "head": head,
        "dirty": dirty,
        "fingerprint": f"snapshot:{head[:12]}:{digest}",
    }


def executable_identity(command: str, repo: Path) -> dict[str, Any]:
    tokens = command_tokens(command)
    while tokens and (
        tokens[0] == "env"
        or "=" in tokens[0]
        and not tokens[0].startswith(("./", "/"))
    ):
        tokens = tokens[1:]
    if not tokens:
        return {"token": None, "path": None}
    token = tokens[0]
    resolved = shutil.which(token)
    if resolved is None and ("/" in token or "\\" in token):
        candidate = (repo / token).resolve()
        if candidate.exists():
            resolved = str(candidate)
    result: dict[str, Any] = {"token": token, "path": resolved}
    if resolved:
        try:
            info = Path(resolved).stat()
            result.update({"size": info.st_size, "mtime_ns": info.st_mtime_ns})
        except OSError:
            pass
    return result


def auto_fingerprint_command(command: str) -> str | None:
    tokens = command_tokens(command)
    while tokens and (
        tokens[0] == "env"
        or "=" in tokens[0]
        and not tokens[0].startswith(("./", "/"))
    ):
        tokens = tokens[1:]
    if not tokens:
        return None
    tool = Path(tokens[0]).name
    if tool == "go":
        return "go version"
    if tool == "cargo":
        return "rustc --version && cargo --version"
    if (
        tool in {"python", "python3"}
        and len(tokens) >= 3
        and tokens[1:3] == ["-m", "pytest"]
    ):
        return f"{shlex.quote(tokens[0])} -m pytest --version"
    if tool in {"python", "python3", "pytest"}:
        return f"{shlex.quote(tokens[0])} --version"
    if tool == "npm":
        return "node --version && npm --version"
    if tool == "pnpm":
        return "node --version && pnpm --version"
    if tool == "yarn":
        return "node --version && yarn --version"
    if tool == "bun":
        return f"{shlex.quote(tokens[0])} --version"
    if tool in {"bazel", "bazelisk"}:
        return f"{shlex.quote(tokens[0])} version"
    if tool in {"mvn", "mvnw", "gradle", "gradlew", "make"}:
        return f"{shlex.quote(tokens[0])} --version"
    return None


def environment_identity(
    repo: Path, suite: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    names = sorted(set(DEFAULT_ENVIRONMENT) | set(suite.get("environment", [])))
    private_payload: dict[str, Any] = {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "environment": {name: os.environ.get(name) for name in names},
        "executable": executable_identity(suite["command"], repo),
    }
    requested = suite.get("fingerprint_command", "auto")
    fingerprint_command = (
        auto_fingerprint_command(suite["command"])
        if requested == "auto"
        else None
        if requested == "none"
        else requested
    )
    output_digest = None
    if fingerprint_command:
        try:
            result = subprocess.run(
                fingerprint_command,
                cwd=repo,
                shell=True,
                executable=os.environ.get("SHELL") or "/bin/sh",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TestOnceError(
                f"fingerprint_command failed: {fingerprint_command}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = result.stdout.decode("utf-8", "replace")[-2000:].strip()
            raise TestOnceError(
                f"fingerprint_command exited {result.returncode}: {fingerprint_command}"
                + (f"\n{detail}" if detail else "")
            )
        output_digest = hashlib.sha256(result.stdout).hexdigest()
        private_payload["fingerprint_command"] = fingerprint_command
        private_payload["fingerprint_output"] = output_digest
    public = {
        "platform": private_payload["platform"],
        "environment_names": names,
        "executable": private_payload["executable"],
        "fingerprint_command": fingerprint_command,
        "fingerprint_output_sha256": output_digest,
    }
    return sha256_json(private_payload), public


def cache_root() -> Path:
    override = os.environ.get("TEST_ONCE_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
        return base / "test-once"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "test-once"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "test-once"
    return Path.home() / ".cache" / "test-once"


def suite_slug(name: str) -> str:
    readable = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "suite"
    return f"{readable[:48]}-{hashlib.sha256(name.encode()).hexdigest()[:8]}"


def repository_id(repo_common: Path) -> str:
    return hashlib.sha256(str(repo_common).encode()).hexdigest()


def cache_layout(
    repo_common: Path,
    suite_name: str,
    suite: dict[str, Any],
    source: dict[str, Any],
    environment_digest: str,
) -> dict[str, Any]:
    repo_id = repository_id(repo_common)
    key_payload = {
        "cache_schema": CACHE_SCHEMA,
        "repo_id": repo_id,
        "suite": suite_name,
        "source": source["fingerprint"],
        "command": suite["command"],
        "environment": environment_digest,
    }
    key = sha256_json(key_payload)
    suite_dir = cache_root() / "repositories" / repo_id / suite_slug(suite_name)
    entry = suite_dir / key
    return {
        "repo_id": repo_id,
        "key": key,
        "suite_dir": suite_dir,
        "entry": entry,
        "result": entry / "result.json",
        "log": entry / "output.log",
        "lock": suite_dir / f"{key}.lock",
    }


def stats_layout(repo_common: Path) -> dict[str, Any]:
    repo_id = repository_id(repo_common)
    repo_dir = cache_root() / "repositories" / repo_id
    return {
        "repo_id": repo_id,
        "stats": repo_dir / "stats.json",
        "lock": repo_dir / "stats.lock",
    }


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


@contextlib.contextmanager
def exclusive_lock(path: Path, *, announce_wait: bool = True) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            waited = False
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if announce_wait and not waited:
                        print(
                            "TEST-ONCE WAIT: another session is running this exact suite key"
                        )
                        waited = True
                    time.sleep(0.2)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if announce_wait:
                    print(
                        "TEST-ONCE WAIT: another session is running this exact suite key"
                    )
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            with contextlib.suppress(OSError):
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def empty_suite_stats() -> dict[str, Any]:
    return {
        "run_requests": 0,
        "cache_hits": 0,
        "executed_runs": 0,
        "passed_runs": 0,
        "failed_runs": 0,
        "unstable_runs": 0,
        "calibrated_cache_hits": 0,
        "uncalibrated_cache_hits": 0,
        "executed_test_seconds": 0.0,
        "nominal_test_seconds_avoided": 0.0,
        "hit_resolution_seconds": 0.0,
        "calibrated_warm_seconds_avoided": 0.0,
        "calibrated_wall_seconds_saved": 0.0,
        "first_event_at": None,
        "last_event_at": None,
    }


def validate_suite_stats(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TestOnceError(f"{path} contains incompatible Test Once statistics")
    normalized = empty_suite_stats()
    integer_fields = (
        "run_requests",
        "cache_hits",
        "executed_runs",
        "passed_runs",
        "failed_runs",
        "unstable_runs",
        "calibrated_cache_hits",
        "uncalibrated_cache_hits",
    )
    float_fields = (
        "executed_test_seconds",
        "nominal_test_seconds_avoided",
        "hit_resolution_seconds",
        "calibrated_warm_seconds_avoided",
        "calibrated_wall_seconds_saved",
    )
    for field in integer_fields:
        raw = value.get(field)
        if (
            not isinstance(raw, int)
            or isinstance(raw, bool)
            or raw < 0
        ):
            raise TestOnceError(
                f"{path} contains incompatible Test Once statistics"
            )
        normalized[field] = raw
    for field in float_fields:
        raw = value.get(field)
        if (
            not isinstance(raw, (int, float))
            or isinstance(raw, bool)
            or not math.isfinite(float(raw))
            or raw < 0
        ):
            raise TestOnceError(
                f"{path} contains incompatible Test Once statistics"
            )
        normalized[field] = float(raw)
    for field in ("first_event_at", "last_event_at"):
        raw = value.get(field)
        if raw is not None and not isinstance(raw, str):
            raise TestOnceError(
                f"{path} contains incompatible Test Once statistics"
            )
        normalized[field] = raw
    if (
        normalized["run_requests"]
        != normalized["cache_hits"] + normalized["executed_runs"]
        or normalized["executed_runs"]
        != normalized["passed_runs"]
        + normalized["failed_runs"]
        + normalized["unstable_runs"]
        or normalized["cache_hits"]
        != normalized["calibrated_cache_hits"]
        + normalized["uncalibrated_cache_hits"]
    ):
        raise TestOnceError(f"{path} contains incompatible Test Once statistics")
    return normalized


def migrate_v1_suite_stats(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TestOnceError(f"{path} contains incompatible Test Once statistics")
    migrated = empty_suite_stats()
    integer_fields = (
        "run_requests",
        "cache_hits",
        "executed_runs",
        "passed_runs",
        "failed_runs",
        "unstable_runs",
    )
    float_fields = (
        "executed_test_seconds",
        "test_seconds_avoided",
        "hit_resolution_seconds",
        "estimated_wall_seconds_saved",
    )
    for field in integer_fields:
        raw = value.get(field)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise TestOnceError(
                f"{path} contains incompatible Test Once statistics"
            )
        migrated[field] = raw
    validated_floats: dict[str, float] = {}
    for field in float_fields:
        raw = value.get(field)
        if (
            not isinstance(raw, (int, float))
            or isinstance(raw, bool)
            or not math.isfinite(float(raw))
            or raw < 0
        ):
            raise TestOnceError(
                f"{path} contains incompatible Test Once statistics"
            )
        validated_floats[field] = float(raw)
    for field in ("first_event_at", "last_event_at"):
        raw = value.get(field)
        if raw is not None and not isinstance(raw, str):
            raise TestOnceError(
                f"{path} contains incompatible Test Once statistics"
            )
        migrated[field] = raw
    if (
        migrated["run_requests"]
        != migrated["cache_hits"] + migrated["executed_runs"]
        or migrated["executed_runs"]
        != migrated["passed_runs"]
        + migrated["failed_runs"]
        + migrated["unstable_runs"]
    ):
        raise TestOnceError(f"{path} contains incompatible Test Once statistics")
    migrated["executed_test_seconds"] = validated_floats[
        "executed_test_seconds"
    ]
    migrated["nominal_test_seconds_avoided"] = validated_floats[
        "test_seconds_avoided"
    ]
    migrated["hit_resolution_seconds"] = validated_floats[
        "hit_resolution_seconds"
    ]
    migrated["uncalibrated_cache_hits"] = migrated["cache_hits"]
    return migrated


def load_stats(path: Path, repo_id: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "stats_schema": STATS_SCHEMA,
            "repo_id": repo_id,
            "updated_at": None,
            "suites": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TestOnceError(f"Cannot read stats from {path}: {exc}") from exc
    stored_schema = (
        payload.get("stats_schema") if isinstance(payload, dict) else None
    )
    if (
        not isinstance(payload, dict)
        or stored_schema not in {1, STATS_SCHEMA}
        or payload.get("repo_id") != repo_id
        or not isinstance(payload.get("suites"), dict)
        or (
            payload.get("updated_at") is not None
            and not isinstance(payload.get("updated_at"), str)
        )
    ):
        raise TestOnceError(f"{path} contains incompatible Test Once statistics")
    normalized_suites: dict[str, Any] = {}
    for suite_name, raw in payload["suites"].items():
        if not isinstance(suite_name, str) or not suite_name:
            raise TestOnceError(
                f"{path} contains incompatible Test Once statistics"
            )
        normalized_suites[suite_name] = (
            migrate_v1_suite_stats(raw, path)
            if stored_schema == 1
            else validate_suite_stats(raw, path)
        )
    return {
        "stats_schema": STATS_SCHEMA,
        "repo_id": repo_id,
        "updated_at": payload.get("updated_at"),
        "suites": normalized_suites,
    }


def record_stats_event(
    repo_common: Path,
    suite_name: str,
    event: str,
    *,
    duration_seconds: float = 0.0,
    original_duration_seconds: float = 0.0,
    hit_resolution_seconds: float = 0.0,
    warm_baseline_seconds: float | None = None,
) -> None:
    layout = stats_layout(repo_common)
    try:
        with exclusive_lock(layout["lock"], announce_wait=False):
            payload = load_stats(layout["stats"], layout["repo_id"])
            suites = payload["suites"]
            current = dict(suites.get(suite_name, empty_suite_stats()))
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            if current["first_event_at"] is None:
                current["first_event_at"] = now
            current["last_event_at"] = now
            current["run_requests"] += 1
            if event == "hit":
                current["cache_hits"] += 1
                current["nominal_test_seconds_avoided"] += max(
                    float(original_duration_seconds), 0.0
                )
                current["hit_resolution_seconds"] += max(
                    float(hit_resolution_seconds), 0.0
                )
                if warm_baseline_seconds is None:
                    current["uncalibrated_cache_hits"] += 1
                else:
                    warm_seconds = max(float(warm_baseline_seconds), 0.0)
                    current["calibrated_cache_hits"] += 1
                    current["calibrated_warm_seconds_avoided"] += warm_seconds
                    current["calibrated_wall_seconds_saved"] += max(
                        warm_seconds - float(hit_resolution_seconds), 0.0
                    )
            elif event in {"passed", "failed", "unstable"}:
                current["executed_runs"] += 1
                current["executed_test_seconds"] += max(
                    float(duration_seconds), 0.0
                )
                current[f"{event}_runs"] += 1
            else:
                raise TestOnceError(f"Unsupported statistics event: {event}")
            suites[suite_name] = current
            payload["updated_at"] = now
            atomic_write_json(layout["stats"], payload)
    except (OSError, TestOnceError) as exc:
        print(f"test-once: warning: could not record statistics: {exc}", file=sys.stderr)


def stats_payload(repo_common: Path, suite_name: str) -> dict[str, Any]:
    layout = stats_layout(repo_common)
    stored = load_stats(layout["stats"], layout["repo_id"])
    current = dict(stored["suites"].get(suite_name, empty_suite_stats()))
    run_requests = int(current["run_requests"])
    cache_hits = int(current["cache_hits"])
    calibrated_cache_hits = int(current["calibrated_cache_hits"])
    return {
        "stats_schema": STATS_SCHEMA,
        "repo_id": layout["repo_id"],
        "suite": suite_name,
        **current,
        "runs_avoided": cache_hits,
        "cache_hit_rate": cache_hits / run_requests if run_requests else 0.0,
        "calibration_coverage": (
            calibrated_cache_hits / cache_hits if cache_hits else 0.0
        ),
        "calibrated_wall_seconds_saved": (
            current["calibrated_wall_seconds_saved"]
            if calibrated_cache_hits
            else None
        ),
        "stats_path": str(layout["stats"]),
    }


def read_result(path: Path, suite: dict[str, Any]) -> dict[str, Any] | None:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(result, dict)
        or result.get("cache_schema") != CACHE_SCHEMA
        or result.get("status") != "passed"
        or result.get("exit_code") != 0
        or not isinstance(result.get("suite"), str)
        or not isinstance(result.get("key"), str)
        or not isinstance(result.get("source"), dict)
        or not isinstance(result["source"].get("fingerprint"), str)
        or not isinstance(result.get("command"), str)
        or not isinstance(result.get("finished_at"), str)
        or not isinstance(result.get("duration_seconds"), (int, float))
    ):
        return None
    ttl = suite.get("ttl_seconds")
    if ttl is not None:
        finished = result.get("finished_at_epoch")
        if not isinstance(finished, (int, float)) or time.time() - finished > ttl:
            return None
    return result


def warm_baseline_seconds(result: dict[str, Any]) -> float | None:
    baseline = result.get("warm_baseline")
    if not isinstance(baseline, dict):
        return None
    duration = baseline.get("duration_seconds")
    source = result.get("source")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(float(duration))
        or duration < 0
        or not isinstance(source, dict)
        or baseline.get("source_fingerprint") != source.get("fingerprint")
        or not isinstance(baseline.get("recorded_at"), str)
        or not isinstance(baseline.get("log_file"), str)
    ):
        return None
    return float(duration)


def print_hit(result: dict[str, Any], result_path: Path, *, show_log: bool) -> None:
    log_path = result_path.parent / str(result.get("log_file", "output.log"))
    print("TEST-ONCE HIT: PASS")
    print("No test process was started; this exact passing result was reused.")
    print(f"suite: {result['suite']}")
    print(f"key: {result['key']}")
    print(f"source: {result['source']['fingerprint']}")
    print(f"command: {result['command']}")
    print(f"recorded_at: {result['finished_at']}")
    print(f"original_duration_seconds: {result['duration_seconds']:.3f}")
    print(f"log: {log_path}")
    if show_log and log_path.is_file():
        print("----- cached test output -----")
        sys.stdout.flush()
        with log_path.open("rb") as handle:
            shutil.copyfileobj(handle, sys.stdout.buffer)


def stream_test(command: str, repo: Path, log_path: Path) -> tuple[int, float]:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=repo,
        shell=True,
        executable=os.environ.get("SHELL") or "/bin/sh",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    try:
        with log_path.open("wb") as log:
            while chunk := process.stdout.read(64 * 1024):
                log.write(chunk)
                log.flush()
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        return process.wait(), time.monotonic() - started
    except KeyboardInterrupt:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGINT)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        raise


def resolve_suite(
    repo: Path, suite_name: str, command_override: str | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if command_override is not None:
        if not command_override.strip():
            raise TestOnceError("The test command cannot be empty")
        return (
            {
                "command": command_override.strip(),
                "match": [],
                "fingerprint_command": "auto",
                "environment": [],
                "extra_inputs": [],
                "ttl_seconds": None,
            },
            None,
        )
    config = load_config(repo)
    suite = config["suites"].get(suite_name)
    if suite is None:
        raise TestOnceError(f"Suite `{suite_name}` is not configured")
    return suite, config


def current_identity(
    repo: Path, common: Path, suite_name: str, suite: dict[str, Any]
) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any]]:
    source = source_identity(repo, suite)
    env_digest, env_public = environment_identity(repo, suite)
    layout = cache_layout(common, suite_name, suite, source, env_digest)
    return source, env_digest, env_public, layout


def command_run(args: argparse.Namespace) -> int:
    request_started = time.monotonic()
    repo, common = discover_repo(args.repo or Path.cwd())
    command_override = args.command
    if args.remainder:
        remainder = list(args.remainder)
        if remainder and remainder[0] == "--":
            remainder = remainder[1:]
        if not remainder:
            raise TestOnceError("Expected a command after `--`")
        if command_override:
            raise TestOnceError("Use either --command or a command after `--`, not both")
        command_override = shlex.join(remainder)
    suite, _ = resolve_suite(repo, args.suite, command_override)
    if not suite["command"]:
        raise TestOnceError("The test command cannot be empty")

    for _ in range(4):
        source, env_digest, env_public, layout = current_identity(
            repo, common, args.suite, suite
        )
        cached = read_result(layout["result"], suite)
        if cached is not None:
            after = source_identity(repo, suite)
            if after["fingerprint"] == source["fingerprint"]:
                record_stats_event(
                    common,
                    args.suite,
                    "hit",
                    original_duration_seconds=float(cached["duration_seconds"]),
                    hit_resolution_seconds=time.monotonic() - request_started,
                    warm_baseline_seconds=warm_baseline_seconds(cached),
                )
                print_hit(cached, layout["result"], show_log=args.show_log)
                return 0
            continue

        with exclusive_lock(layout["lock"]):
            locked_source = source_identity(repo, suite)
            if locked_source["fingerprint"] != source["fingerprint"]:
                continue
            cached = read_result(layout["result"], suite)
            if cached is not None:
                record_stats_event(
                    common,
                    args.suite,
                    "hit",
                    original_duration_seconds=float(cached["duration_seconds"]),
                    hit_resolution_seconds=time.monotonic() - request_started,
                    warm_baseline_seconds=warm_baseline_seconds(cached),
                )
                print_hit(cached, layout["result"], show_log=args.show_log)
                return 0

            layout["suite_dir"].mkdir(parents=True, exist_ok=True)
            fd, raw_log = tempfile.mkstemp(
                prefix=f".{layout['key']}.",
                suffix=".log.tmp",
                dir=layout["suite_dir"],
            )
            os.close(fd)
            temp_log = Path(raw_log)
            started_epoch = time.time()
            started_iso = dt.datetime.now(dt.timezone.utc).isoformat()
            print("TEST-ONCE MISS: running this exact suite key once")
            print(f"suite: {args.suite}")
            print(f"key: {layout['key']}")
            print(f"source: {source['fingerprint']}")
            print(f"command: {suite['command']}")
            sys.stdout.flush()
            try:
                exit_code, duration = stream_test(suite["command"], repo, temp_log)
                if exit_code != 0:
                    record_stats_event(
                        common,
                        args.suite,
                        "failed",
                        duration_seconds=duration,
                    )
                    print(
                        f"TEST-ONCE NOT STORED: test command exited {exit_code}; "
                        "failures are never reused."
                    )
                    return exit_code
                final_source = source_identity(repo, suite)
                if final_source["fingerprint"] != locked_source["fingerprint"]:
                    record_stats_event(
                        common,
                        args.suite,
                        "unstable",
                        duration_seconds=duration,
                    )
                    print(
                        "TEST-ONCE NOT STORED: repository source changed while tests "
                        f"were running (exit {UNSTABLE_SOURCE_EXIT}).",
                        file=sys.stderr,
                    )
                    return UNSTABLE_SOURCE_EXIT

                finished_epoch = time.time()
                finished_iso = dt.datetime.now(dt.timezone.utc).isoformat()
                layout["entry"].mkdir(parents=True, exist_ok=True)
                os.replace(temp_log, layout["log"])
                record = {
                    "cache_schema": CACHE_SCHEMA,
                    "status": "passed",
                    "exit_code": 0,
                    "suite": args.suite,
                    "key": layout["key"],
                    "repo_root": str(repo),
                    "repo_common_dir": str(common),
                    "source": locked_source,
                    "command": suite["command"],
                    "environment_fingerprint": env_digest,
                    "environment_details": env_public,
                    "started_at": started_iso,
                    "started_at_epoch": started_epoch,
                    "finished_at": finished_iso,
                    "finished_at_epoch": finished_epoch,
                    "duration_seconds": duration,
                    "log_file": "output.log",
                }
                atomic_write_json(layout["result"], record)
                record_stats_event(
                    common,
                    args.suite,
                    "passed",
                    duration_seconds=duration,
                )
                print("TEST-ONCE STORED: PASS")
                print(f"key: {layout['key']}")
                print(f"log: {layout['log']}")
                return 0
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temp_log.unlink()
    raise TestOnceError("Repository source kept changing while acquiring the cache lock")


def status_payload(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    repo, common = discover_repo(args.repo or Path.cwd())
    suite, _ = resolve_suite(repo, args.suite, args.command)
    source, env_digest, env_public, layout = current_identity(
        repo, common, args.suite, suite
    )
    result = read_result(layout["result"], suite)
    payload = {
        "suite": args.suite,
        "reusable": result is not None,
        "key": layout["key"],
        "source": source,
        "command": suite["command"],
        "environment_fingerprint": env_digest,
        "environment_details": env_public,
        "result_path": str(layout["result"]),
        "result": result,
    }
    if args.command is None:
        payload["config_path"] = str(configuration_path(repo))
    return payload, layout


def command_status(args: argparse.Namespace) -> int:
    payload, _ = status_payload(args)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif payload["reusable"]:
        print_hit(
            payload["result"], Path(payload["result_path"]), show_log=args.show_log
        )
    else:
        print("TEST-ONCE MISS: no reusable passing result for the current key")
        print(f"suite: {payload['suite']}")
        print(f"key: {payload['key']}")
        print(f"source: {payload['source']['fingerprint']}")
        print(f"command: {payload['command']}")
    return 0


def command_calibrate(args: argparse.Namespace) -> int:
    repo, common = discover_repo(args.repo or Path.cwd())
    suite, _ = resolve_suite(repo, args.suite, args.command)
    source, _, _, layout = current_identity(repo, common, args.suite, suite)
    with exclusive_lock(layout["lock"]):
        locked_source = source_identity(repo, suite)
        if locked_source["fingerprint"] != source["fingerprint"]:
            raise TestOnceError(
                "Repository source changed before calibration; retry when stable"
            )
        cached = read_result(layout["result"], suite)
        if cached is None:
            raise TestOnceError(
                "Calibration requires a reusable passing result for the current key; "
                "run the suite once first"
            )
        layout["suite_dir"].mkdir(parents=True, exist_ok=True)
        fd, raw_log = tempfile.mkstemp(
            prefix=f".{layout['key']}.",
            suffix=".calibration.log.tmp",
            dir=layout["suite_dir"],
        )
        os.close(fd)
        temp_log = Path(raw_log)
        print("TEST-ONCE CALIBRATION: intentionally rerunning the full suite once")
        print(f"suite: {args.suite}")
        print(f"key: {layout['key']}")
        print(f"source: {source['fingerprint']}")
        print(f"command: {suite['command']}")
        sys.stdout.flush()
        try:
            exit_code, duration = stream_test(suite["command"], repo, temp_log)
            if exit_code != 0:
                print(
                    "TEST-ONCE CALIBRATION NOT STORED: "
                    f"test command exited {exit_code}"
                )
                return exit_code
            final_source = source_identity(repo, suite)
            if final_source["fingerprint"] != locked_source["fingerprint"]:
                print(
                    "TEST-ONCE CALIBRATION NOT STORED: repository source changed "
                    f"while tests were running (exit {UNSTABLE_SOURCE_EXIT}).",
                    file=sys.stderr,
                )
                return UNSTABLE_SOURCE_EXIT
            calibration_log = layout["entry"] / "warm-baseline.log"
            os.replace(temp_log, calibration_log)
            recorded_at = dt.datetime.now(dt.timezone.utc).isoformat()
            updated = dict(cached)
            updated["warm_baseline"] = {
                "duration_seconds": duration,
                "recorded_at": recorded_at,
                "source_fingerprint": locked_source["fingerprint"],
                "log_file": calibration_log.name,
            }
            atomic_write_json(layout["result"], updated)
            print("TEST-ONCE CALIBRATED")
            print(f"warm_baseline_duration_seconds: {duration:.3f}")
            print(f"log: {calibration_log}")
            return 0
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp_log.unlink()


def command_init(args: argparse.Namespace) -> int:
    repo, _ = discover_repo(args.repo or Path.cwd())
    path = repo / CONFIG_RELATIVE_PATH
    legacy_paths = [
        repo / relative
        for relative in LEGACY_CONFIG_RELATIVE_PATHS
        if (repo / relative).is_file()
    ]
    if not args.command.strip():
        raise TestOnceError("The test command cannot be empty")
    if args.ttl_seconds is not None and args.ttl_seconds <= 0:
        raise TestOnceError("--ttl-seconds must be a positive integer")
    if path.is_file() and legacy_paths:
        configuration_path(repo)
    if path.exists():
        config = read_config(path)
    elif legacy_paths:
        config = read_config(legacy_paths[0])
    else:
        config = {"schema_version": CONFIG_SCHEMA, "suites": {}}
    if args.suite in config["suites"] and not args.force:
        raise TestOnceError(
            f"Suite `{args.suite}` already exists in {path}; use --force to replace it"
        )
    matches: list[str] = []
    for candidate in [args.command, *args.match]:
        if candidate not in matches:
            matches.append(candidate)
    config["suites"][args.suite] = {
        "command": args.command.strip(),
        "match": matches,
        "fingerprint_command": args.fingerprint_command,
        "environment": list(dict.fromkeys(args.env)),
        "extra_inputs": list(dict.fromkeys(args.extra_input)),
        "ttl_seconds": args.ttl_seconds,
    }
    atomic_write_json(path, config)
    for legacy_path in legacy_paths:
        try:
            legacy_path.unlink()
        except OSError as exc:
            raise TestOnceError(
                f"Configured {path} but could not remove legacy {legacy_path}: {exc}"
            ) from exc
    if legacy_paths:
        migrated = ", ".join(str(item) for item in legacy_paths)
        print(f"Migrated Test Once configuration from {migrated} to {path}")
    print(f"Configured Test Once suite `{args.suite}` in {path}")
    print(f"command: {args.command.strip()}")
    print(
        f"run: {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(Path(__file__).resolve()))} "
        f"--repo {shlex.quote(str(repo))} run --suite {shlex.quote(args.suite)}"
    )
    return 0


def command_stats(args: argparse.Namespace) -> int:
    _, common = discover_repo(args.repo or Path.cwd())
    payload = stats_payload(common, args.suite)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print("TEST-ONCE STATS")
    print(f"suite: {payload['suite']}")
    print(f"run_requests: {payload['run_requests']}")
    print(f"executed_runs: {payload['executed_runs']}")
    print(f"cache_hits: {payload['cache_hits']}")
    print(f"cache_hit_rate: {payload['cache_hit_rate']:.1%}")
    print(f"runs_avoided: {payload['runs_avoided']}")
    print(f"executed_test_seconds: {payload['executed_test_seconds']:.3f}")
    print(
        "nominal_test_seconds_avoided: "
        f"{payload['nominal_test_seconds_avoided']:.3f}"
    )
    print(f"calibrated_cache_hits: {payload['calibrated_cache_hits']}")
    print(f"uncalibrated_cache_hits: {payload['uncalibrated_cache_hits']}")
    print(
        "calibrated_warm_seconds_avoided: "
        f"{payload['calibrated_warm_seconds_avoided']:.3f}"
    )
    calibrated_saved = payload["calibrated_wall_seconds_saved"]
    calibrated_saved_text = (
        f"{calibrated_saved:.3f}"
        if calibrated_saved is not None
        else "unavailable"
    )
    print(f"calibrated_wall_seconds_saved: {calibrated_saved_text}")
    print(f"calibration_coverage: {payload['calibration_coverage']:.1%}")
    print(f"hit_resolution_seconds: {payload['hit_resolution_seconds']:.3f}")
    print(f"first_event_at: {payload['first_event_at'] or 'none'}")
    print(f"last_event_at: {payload['last_event_at'] or 'none'}")
    print(f"stats: {payload['stats_path']}")
    return 0


def command_invalidate(args: argparse.Namespace) -> int:
    payload, layout = status_payload(args)
    entry: Path = layout["entry"]
    if not entry.exists():
        print("TEST-ONCE INVALIDATE: no cached result exists for the current key")
        return 0
    with exclusive_lock(layout["lock"]):
        if not entry.exists():
            print("TEST-ONCE INVALIDATE: no cached result exists for the current key")
            return 0
        trash = (
            cache_root()
            / ".trash"
            / (
                f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                f"-{layout['key']}"
            )
        )
        trash.parent.mkdir(parents=True, exist_ok=True)
        os.replace(entry, trash)
    print(f"TEST-ONCE INVALIDATED: moved current result to {trash}")
    print(f"key: {payload['key']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an expensive repository test suite once per exact input fingerprint."
    )
    parser.add_argument("--repo", help="Repository path; defaults to the current directory")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    init_parser = subparsers.add_parser("init", help="Configure a repository suite")
    init_parser.add_argument("--suite", default="full-ut")
    init_parser.add_argument("--command", required=True)
    init_parser.add_argument("--match", action="append", default=[])
    init_parser.add_argument(
        "--fingerprint-command",
        default="auto",
        help="Shell command whose output identifies the toolchain; use `none` to disable",
    )
    init_parser.add_argument("--env", action="append", default=[])
    init_parser.add_argument("--extra-input", action="append", default=[])
    init_parser.add_argument("--ttl-seconds", type=int)
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(handler=command_init)

    run_parser = subparsers.add_parser("run", help="Run or reuse a suite")
    run_parser.add_argument("--suite", default="full-ut")
    run_parser.add_argument("--command")
    run_parser.add_argument("--show-log", action="store_true")
    run_parser.add_argument("remainder", nargs=argparse.REMAINDER)
    run_parser.set_defaults(handler=command_run)

    status_parser = subparsers.add_parser("status", help="Inspect the current cache key")
    status_parser.add_argument("--suite", default="full-ut")
    status_parser.add_argument("--command")
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument("--show-log", action="store_true")
    status_parser.set_defaults(handler=command_status)

    calibrate_parser = subparsers.add_parser(
        "calibrate",
        help="Intentionally rerun a passing suite once to measure its warm baseline",
    )
    calibrate_parser.add_argument("--suite", default="full-ut")
    calibrate_parser.add_argument("--command")
    calibrate_parser.set_defaults(handler=command_calibrate)

    stats_parser = subparsers.add_parser(
        "stats", help="Show locally aggregated run and savings statistics"
    )
    stats_parser.add_argument("--suite", default="full-ut")
    stats_parser.add_argument("--json", action="store_true")
    stats_parser.set_defaults(handler=command_stats)

    invalidate_parser = subparsers.add_parser(
        "invalidate", help="Move the current key's result into cache trash"
    )
    invalidate_parser.add_argument("--suite", default="full-ut")
    invalidate_parser.add_argument("--command")
    invalidate_parser.set_defaults(handler=command_invalidate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except TestOnceError as exc:
        print(f"test-once: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("test-once: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
