"""Process start-time probes: /proc on Linux, a single ps call elsewhere."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermesd.collect import system

_PROC_STAT = "cpu  1 2 3\nbtime 1000\nprocesses 5\n"
# Field 2 (comm) holds spaces and a ')' to prove parsing anchors on the last ')'.
_PID_STAT = "42 (my (odd) proc) S " + " ".join(["0"] * 18) + " 500 0 0\n"


def _fake_proc(monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> None:
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        key = str(self)
        if key.startswith("/proc/"):
            if key in files:
                return files[key]
            raise FileNotFoundError(key)
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", fake_read_text)


def test_proc_start_times_reads_boot_time_and_starttime(monkeypatch: pytest.MonkeyPatch):
    _fake_proc(monkeypatch, {"/proc/stat": _PROC_STAT, "/proc/42/stat": _PID_STAT})
    monkeypatch.setattr(system.os, "sysconf", lambda name: 100)

    assert system._proc_boot_epoch() == 1000.0
    # starttime 500 ticks at 100 Hz after a 1000 s boot epoch; pid 7 is absent.
    assert system._proc_start_times([42, 7]) == {42: 1005.0}


def test_observed_start_times_skip_ps_when_proc_answers_everything(
    monkeypatch: pytest.MonkeyPatch,
):
    _fake_proc(monkeypatch, {"/proc/stat": _PROC_STAT, "/proc/42/stat": _PID_STAT})
    monkeypatch.setattr(system.os, "sysconf", lambda name: 100)

    def no_ps(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("ps must not run when /proc covered every pid")

    monkeypatch.setattr(system.subprocess, "run", no_ps)

    assert system._observed_process_start_times([42]) == {42: 1005.0}


def test_proc_boot_epoch_is_none_without_btime(monkeypatch: pytest.MonkeyPatch):
    _fake_proc(monkeypatch, {"/proc/stat": "cpu 1 2 3\n"})
    assert system._proc_boot_epoch() is None


@pytest.mark.parametrize("sysconf_result", [OSError("no"), ValueError("no"), 0])
def test_proc_start_times_empty_when_clock_tick_unusable(
    monkeypatch: pytest.MonkeyPatch, sysconf_result: object
):
    _fake_proc(monkeypatch, {"/proc/stat": _PROC_STAT, "/proc/42/stat": _PID_STAT})

    def fake_sysconf(name: str) -> int:
        if isinstance(sysconf_result, Exception):
            raise sysconf_result
        assert isinstance(sysconf_result, int)
        return sysconf_result

    monkeypatch.setattr(system.os, "sysconf", fake_sysconf)

    assert system._proc_start_times([42]) == {}


def test_ps_start_times_skips_malformed_and_non_positive_rows(monkeypatch: pytest.MonkeyPatch):
    stdout = (
        "garbage\n"
        "  0 Mon Jan  5 10:00:00 2026\n"
        "  -3 Mon Jan  5 10:00:00 2026\n"
        "  9 not a date\n"
        "  12 Mon Jan  5 10:00:00 2026\n"
    )

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout, "")

    monkeypatch.setattr(system.subprocess, "run", fake_run)

    observed = system._ps_start_times([0, 9, 12])

    assert list(observed) == [12]
    assert observed[12] == system._parse_lstart("Mon Jan  5 10:00:00 2026")


def _no_ps(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError("ps must not run when the kernel answered every pid")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sysctl KERN_PROC probe")
def test_darwin_start_times_match_this_process():
    observed = system._darwin_start_times([os.getpid()])

    started = observed[os.getpid()]
    assert started is not None
    # This interpreter started before now and (for a test run) within a day.
    assert time.time() - 86400 < started <= time.time()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sysctl KERN_PROC probe")
def test_darwin_start_times_report_a_missing_pid_as_absent_not_unknown():
    # 99998 is inside macOS's pid range but, absent a remarkable coincidence,
    # unused; the kernel answers "no such process" with a zero-length reply.
    if system._pid_exists(99998):
        pytest.skip("pid 99998 happens to be live")

    assert system._darwin_start_times([99998]) == {99998: None}


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sysctl KERN_PROC probe")
def test_observed_start_times_skip_ps_when_the_kernel_answers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(system.subprocess, "run", _no_ps)

    observed = system._observed_process_start_times([os.getpid(), 99998, 1_000_000])

    assert os.getpid() in observed
    # A dead or out-of-range pid is simply unobserved, and no longer drags a
    # whole-batch ps call (which rejects pids above 99999) into every pass.
    assert 99998 not in observed
    assert 1_000_000 not in observed


def test_darwin_start_times_are_empty_off_macos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(system.sys, "platform", "linux")

    assert system._darwin_start_times([os.getpid()]) == {}


def test_observed_start_times_fall_back_to_ps_when_sysctl_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(system, "_proc_start_times", lambda pids: {})
    monkeypatch.setattr(system, "_darwin_libc", lambda: None)
    monkeypatch.setattr(system.sys, "platform", "darwin")
    monkeypatch.setattr(system, "_ps_start_times", lambda pids: dict.fromkeys(pids, 7.0))

    assert system._observed_process_start_times([42]) == {42: 7.0}
