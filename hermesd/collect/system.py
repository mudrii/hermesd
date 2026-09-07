"""Process liveness, git checkpoint summaries and runtime activity age."""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

from hermesd.collect.common import _coerce_float, _coerce_int, _mtime, _mtime_ns
from hermesd.paths import HermesPaths

# Seconds a `git` call may run before it is abandoned. Two git subprocesses
# per checkpoint repo run on every collect tick, so a hung repo must not
# stall the pass.
_GIT_SUBPROCESS_TIMEOUT_SECONDS = 2
# An agent that touched state.db, the session index, the agent log or the
# gateway state within this window counts as running even with no live
# session and no gateway process.
_RECENT_ACTIVITY_WINDOW_SECONDS = 300.0
# os.kill() passes the pid to the C API as a pid_t, so anything wider raises
# OverflowError. A JSON file under ~/.hermes can hold an arbitrary integer, and
# no such value can name a live process anyway.
_MAX_PID = 2**31 - 1


def _pid_exists(pid: int) -> bool:
    if pid <= 0 or pid > _MAX_PID:
        # os.kill(0, 0) targets this process group and os.kill(-1, 0) every
        # process the user owns; neither is a liveness check.
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def _git_ref_signature(repo_dir: Path) -> tuple[int, ...]:
    """Cheap fingerprint of a bare repo's ref state (HEAD, refs tree, packed-refs)."""
    refs_dir = repo_dir / "refs"
    latest_ref = _mtime_ns(refs_dir)
    with contextlib.suppress(OSError):
        for root, dir_names, file_names in os.walk(refs_dir):
            root_path = Path(root)
            for name in (*dir_names, *file_names):
                latest_ref = max(latest_ref, _mtime_ns(root_path / name))
    return (
        _mtime_ns(repo_dir / "HEAD"),
        _mtime_ns(repo_dir / "packed-refs"),
        latest_ref,
    )


def _git_checkpoint_summary(repo_dir: Path) -> tuple[int, float | None, str]:
    commit_count = 0
    try:
        count_result = subprocess.run(
            ["git", "--git-dir", str(repo_dir), "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return 0, None, ""

    if count_result.returncode == 0:
        commit_count = _coerce_int(count_result.stdout.strip())
    if commit_count <= 0:
        return 0, None, ""

    try:
        log_result = subprocess.run(
            ["git", "--git-dir", str(repo_dir), "log", "-1", "--format=%ct%x09%s", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        # A non-UTF-8 commit subject must not fail the whole checkpoints source.
        return commit_count, None, ""

    if log_result.returncode != 0:
        return commit_count, None, ""

    raw = log_result.stdout.strip()
    if "\t" not in raw:
        return commit_count, None, raw

    ts_text, reason = raw.split("\t", 1)
    timestamp = _coerce_float(ts_text)
    return commit_count, (timestamp or None), reason


def _latest_runtime_activity_age(paths: HermesPaths, now: float) -> float | None:
    candidates = [
        paths.profile_path("state.db"),
        paths.profile_path("sessions", "sessions.json"),
        paths.profile_path("logs", "agent.log"),
        paths.shared_path("gateway_state.json"),
    ]
    latest = max((_mtime(path) or 0.0) for path in candidates)
    if latest <= 0.0:
        return None
    return max(0.0, now - latest)
