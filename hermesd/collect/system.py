"""Process liveness, git checkpoint summaries and runtime activity age."""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import functools
import os
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from hermesd.collect.common import (
    _age_seconds,
    _coerce_float,
    _coerce_int,
    _mtime,
    _mtime_ns,
    _optional_epoch,
)
from hermesd.models import ProcessLiveness
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
# Seconds a `ps` start-time probe may run. Like git, it is on the collect tick.
_PS_START_TIMEOUT_SECONDS = 2
# `ps -o lstart=` reports whole seconds while the registry records fractional
# epoch seconds, so identity is compared within this window. Two seconds is far
# tighter than the pid wraparound a genuine reuse would require.
_PROCESS_START_TOLERANCE_SECONDS = 2.0
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"
# macOS sysctl({CTL_KERN, KERN_PROC, KERN_PROC_PID, pid}) returns one
# ``struct kinfo_proc``; its first member, ``kp_proc.p_un.__p_starttime``, is a
# ``struct timeval`` (int64 seconds, int32 microseconds). psutil reads the same
# field. A kernel whose struct outgrows this buffer answers ENOMEM, which reads
# as "unobserved" and falls back to ps.
_KINFO_PROC_SIZE = 648
_CTL_KERN, _KERN_PROC, _KERN_PROC_PID = 1, 14, 1
_TIMEVAL = struct.Struct("=qi")


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


def _proc_boot_epoch() -> float | None:
    """Boot time from ``/proc/stat``, or None off Linux / on a read failure."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    return None


def _proc_start_times(pids: Sequence[int]) -> dict[int, float]:
    """Linux start times as epoch seconds, from ``/proc/<pid>/stat`` field 22."""
    boot = _proc_boot_epoch()
    if boot is None:
        return {}
    try:
        tick = os.sysconf("SC_CLK_TCK")
    except (AttributeError, OSError, ValueError):
        return {}
    if tick <= 0:
        return {}
    observed: dict[int, float] = {}
    for pid in pids:
        with contextlib.suppress(OSError, ValueError, IndexError):
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # Field 2 (comm) may contain spaces and parentheses, so anchor on the
            # final ')' and count from field 3: starttime is field 22 → index 19.
            fields = stat[stat.rindex(")") + 1 :].split()
            observed[pid] = boot + int(fields[19]) / tick
    return observed


def _parse_lstart(value: str) -> float | None:
    """Parse ``ps -o lstart=``, which prints local time without a zone.

    ``mktime`` resolves it in the current local zone, so a process started inside a
    DST-ambiguous hour can resolve an hour off and read as a different process.
    That is a one-hour window once a year, and it fails toward a false ``dead`` on
    a single entry — never toward a false ``live``.
    """
    try:
        return time.mktime(time.strptime(value, _LSTART_FORMAT))
    except ValueError:
        return None


def _ps_start_times(pids: Sequence[int]) -> dict[int, float]:
    """Start times for every pid from a single bounded ``ps`` call.

    One subprocess per surface per refresh tick would be far too expensive, so the
    whole pid set is asked at once. A non-zero exit is normal once some pids have
    already exited, so stdout is parsed regardless and absent pids just drop out.
    """
    try:
        completed = subprocess.run(
            ["ps", "-p", ",".join(str(pid) for pid in pids), "-o", "pid=,lstart="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PS_START_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return {}
    observed: dict[int, float] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid = _coerce_int(parts[0])
        epoch = _parse_lstart(parts[1].strip())
        if pid > 0 and epoch is not None:
            observed[pid] = epoch
    return observed


@functools.cache
def _darwin_libc() -> ctypes.CDLL | None:
    """libc for sysctl, loaded once; None when it cannot be loaded."""
    path = ctypes.util.find_library("c")
    if path is None:
        return None
    try:
        return ctypes.CDLL(path, use_errno=True)
    except OSError:
        return None


def _darwin_start_times(pids: Sequence[int]) -> dict[int, float | None]:
    """macOS start times straight from the kernel, without spawning ``ps``.

    A pid maps to its epoch start time, or to None when the kernel reports no
    such process (a zero-length reply). A pid missing from the result could not
    be probed at all (sysctl error, out-of-range pid) and is left to ``ps``.
    Off macOS, or when libc cannot be loaded, the result is empty.
    """
    if sys.platform != "darwin":
        return {}
    libc = _darwin_libc()
    if libc is None:
        return {}
    observed: dict[int, float | None] = {}
    for pid in pids:
        if pid <= 0 or pid > _MAX_PID:
            continue
        mib = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid)
        buffer = ctypes.create_string_buffer(_KINFO_PROC_SIZE)
        size = ctypes.c_size_t(_KINFO_PROC_SIZE)
        if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
            continue
        if size.value == 0:
            observed[pid] = None
            continue
        if size.value < _TIMEVAL.size:
            continue
        seconds, micros = _TIMEVAL.unpack_from(buffer.raw, 0)
        if seconds > 0 and 0 <= micros < 1_000_000:
            observed[pid] = seconds + micros / 1_000_000
    return observed


def _observed_process_start_times(pids: Sequence[int]) -> dict[int, float]:
    """Observed start time per pid as epoch seconds, best effort.

    A pid that cannot be observed is absent from the result, which callers must
    read as *unverifiable* — never as dead. hermesd has no psutil dependency:
    Linux reads ``/proc``, macOS asks the kernel via sysctl, and only pids
    neither could answer fall back to one ``ps`` call.
    """
    unique = sorted({pid for pid in pids if pid > 0})
    if not unique:
        return {}
    observed = _proc_start_times(unique)
    missing = [pid for pid in unique if pid not in observed]
    if missing:
        kernel = _darwin_start_times(missing)
        observed.update({pid: start for pid, start in kernel.items() if start is not None})
        # A pid the kernel says does not exist needs no ps round-trip either.
        missing = [pid for pid in missing if pid not in kernel]
    if missing:
        observed.update(_ps_start_times(missing))
    return observed


def _surface_liveness(
    pid: int,
    recorded_start: float | None,
    observed: Mapping[int, float],
    pid_exists: Callable[[int], bool],
) -> ProcessLiveness:
    """Three-state liveness for one active-session registry entry.

    A pid that merely exists is not the recorded process, because pids are reused:
    only a matching start time proves identity. A start time that was never
    recorded, or that this host cannot observe, leaves the entry unverifiable
    rather than silently counting it as live.
    """
    if pid <= 0 or not pid_exists(pid):
        return ProcessLiveness.DEAD
    if recorded_start is None:
        return ProcessLiveness.UNVERIFIABLE
    seen = observed.get(pid)
    if seen is None:
        return ProcessLiveness.UNVERIFIABLE
    if abs(seen - recorded_start) <= _PROCESS_START_TOLERANCE_SECONDS:
        return ProcessLiveness.LIVE
    # The pid exists but belongs to a different process: the recorded one is gone.
    return ProcessLiveness.DEAD


def _lease_age_seconds(raw: object, now: float) -> float | None:
    """Age of an active-session lease stamp, or None when it is not an epoch.

    ``started_at``/``updated_at`` are ``time.time()`` floats upstream
    (``_lease_entry``, ``hermes_cli/active_sessions.py:426-433``). Anything else
    in that slot — an ISO string, ``null``, a non-positive number — reads as "no
    usable stamp", never as January 1970 and never as an age of ``now``.
    """
    return _age_seconds(_optional_epoch(raw), now)


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
