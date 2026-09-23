"""Edge cases of the ops-health readers and their panel labels."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import hermesd.collect.logs as logs_module
import hermesd.collect.operations as operations_module
import hermesd.collect.system as system_module
from hermesd.collect.logs import LOG_HEALTH_SPECS, IncrementalLogScanner
from hermesd.models import (
    CacheDirUsage,
    DashboardState,
    DiskUsageState,
    LogFileUsage,
    LogHealthCounter,
    OperationsState,
    PendingActionSubsystem,
    StateSnapshotSummary,
    WorkerIdentity,
)
from hermesd.panels import render_panel
from hermesd.panels.logs import log_health_counter_label
from hermesd.theme import Theme
from tests.conftest import render_to_str

_SPECS = dict(LOG_HEALTH_SPECS)


def _scanner(filename: str) -> IncrementalLogScanner:
    return IncrementalLogScanner(_SPECS[filename])


def test_invalid_local_stamp_is_undated():
    assert logs_module._local_stamp_epoch("2026-13-45 99:99:99") is None


def test_unrepresentable_local_stamp_is_undated(monkeypatch: pytest.MonkeyPatch):
    def overflow(_parsed: object) -> float:
        raise OverflowError("mktime argument out of range")

    monkeypatch.setattr(logs_module.time, "mktime", overflow)
    assert logs_module._local_stamp_epoch("2026-09-23 10:00:00") is None


def test_undated_gateway_signatures_and_non_matching_lines(tmp_path: Path):
    log = tmp_path / "gateway.error.log"
    log.write_text(
        "ModuleNotFoundError: No module named 'x'\n"
        "ERROR no-colon-here\n"
        "Some Error happened without a colon\n"
        "=====not a banner=====\n"
        "2026-99-99 99:99:99,000 ERROR gateway.run: bad stamp\n"
    )
    health = _scanner("gateway.error.log").scan(log, tmp_path, time.time())
    assert health is not None
    counter = health.counters[0]
    assert counter.undated == 2
    assert {top.signature: top.undated for top in health.top} == {
        "ModuleNotFoundError: No module named 'x'": 1,
        "gateway.run: bad stamp": 1,
    }


def test_mcp_non_banner_lines_are_ignored(tmp_path: Path):
    log = tmp_path / "mcp-stderr.log"
    log.write_text("===== not a real banner =====\nsomething_else.py: error: nope\n")
    health = _scanner("mcp-stderr.log").scan(log, tmp_path, time.time())
    assert health is not None
    assert all(counter.last_24h == 0 and counter.undated == 0 for counter in health.counters)


def test_signature_table_evicts_the_stalest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(logs_module, "_LOG_HEALTH_MAX_SIGNATURES", 2)
    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
    log = tmp_path / "gateway.error.log"
    log.write_text(
        "".join(f"{stamp},000 ERROR mod.{name}: failed\n" for name in ("alpha", "beta", "gamma"))
    )
    health = _scanner("gateway.error.log").scan(log, tmp_path, now)
    assert health is not None
    assert len(health.top) == 2
    assert health.counters[0].last_24h == 3


def test_overlong_partial_line_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(logs_module, "_LOG_HEALTH_MAX_CARRY_BYTES", 8)
    log = tmp_path / "workspace.log"
    log.write_text("x" * 50)
    scanner = _scanner("workspace.log")
    scanner.scan(log, tmp_path, time.time())
    with log.open("a") as handle:
        handle.write("\n ELIFECYCLE  Command failed\n" + "y" * 50)
    health = scanner.scan(log, tmp_path, time.time())
    assert health is not None
    assert health.counters[0].undated + health.counters[0].last_24h == 1


def test_scanning_a_missing_log_reports_nothing(tmp_path: Path):
    assert _scanner("workspace.log").scan(tmp_path / "absent.log", tmp_path, time.time()) is None


def test_tree_walk_of_a_missing_dir_is_empty(tmp_path: Path):
    assert operations_module._bounded_tree_bytes(tmp_path / "absent") == (0, False)
    cache: operations_module.TreeSizeCache = {}
    assert operations_module._cached_tree_bytes(
        tmp_path / "absent", now=time.time(), cache=cache
    ) == (0, False, False)


def test_tree_walk_skips_special_files(tmp_path: Path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.bin").write_bytes(b"12345")
    os.mkfifo(root / "pipe")
    assert operations_module._bounded_tree_bytes(root) == (5, False)


def test_state_snapshot_special_entries_are_skipped(hermes_home: Path):
    root = hermes_home / "state-snapshots"
    root.mkdir()
    os.mkfifo(root / "pipe")
    (root / "x.db").write_bytes(b"1")
    snapshots = operations_module._read_state_snapshots(root, hermes_home, now=time.time())
    assert snapshots["snapshot_count"] == 1


def test_journal_mode_of_an_unreadable_path(tmp_path: Path):
    mode, error = operations_module._read_journal_mode(tmp_path)
    assert mode == ""
    assert error


def test_unknown_journal_version_byte(tmp_path: Path):
    db = tmp_path / "x.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x10\x00" + bytes([7, 7]) + b"\x00" * 80)
    assert operations_module._read_journal_mode(db) == ("", "unrecognized file-format version 7")


def test_fingerprint_edge_values(monkeypatch: pytest.MonkeyPatch):
    assert system_module._fingerprint_start_epoch("boot|-5") is None
    monkeypatch.setattr(system_module, "_proc_boot_epoch", lambda: None)
    assert system_module._fingerprint_start_epoch("boot|12345") is None
    monkeypatch.setattr(system_module, "_proc_boot_epoch", lambda: 1.0)

    def no_sysconf(_name: str) -> int:
        raise ValueError("unsupported")

    monkeypatch.setattr(system_module.os, "sysconf", no_sysconf)
    assert system_module._fingerprint_start_epoch("boot|12345") is None


def test_unverified_fingerprint_verdicts():
    verdict = system_module._kanban_worker_identity
    assert verdict(0, "unverified", {}, lambda pid: True) is WorkerIdentity.NONE
    assert verdict(9, "unverified", {}, lambda pid: False) is WorkerIdentity.DEAD
    assert verdict(9, "unverified", {}, lambda pid: True) is WorkerIdentity.UNVERIFIED


def test_undated_counter_label():
    counter = LogHealthCounter(key="k", label="crashes", last_1h=1, last_24h=2, undated=3)
    assert log_health_counter_label(counter) == "crashes 1/1h · 2/24h (+3 undated)"


def test_disk_and_pending_labels_render():
    state = DashboardState(
        operations=OperationsState(
            pending_actions=[
                PendingActionSubsystem(
                    subsystem="skills", count=2, oldest_age_seconds=60.0, unreadable_count=1
                )
            ],
            pending_action_total=2,
            snapshot_count=1,
            snapshots=[
                StateSnapshotSummary(
                    name="snap",
                    manifest_present=True,
                    file_count=3,
                    oversized_skipped=["response_store.db"],
                )
            ],
        ),
        disk=DiskUsageState(
            log_files=[LogFileUsage(name=f"f{i}.log", size_bytes=10) for i in range(2)],
            log_file_count=5,
            checkpoints_enabled=True,
            checkpoints_bytes=600 * 1024 * 1024,
            checkpoints_over_cap=True,
            cache_hogs=[CacheDirUsage(name="campaign", size_bytes=2 << 30, size_truncated=True)],
            state_db_wal_bytes=20 * 1024 * 1024,
            state_db_wal_verdict="note",
            pending_walks=2,
        ),
    )
    detail = render_to_str(render_panel(12, state, Theme(), detail=True), width=200, no_color=True)
    compact = render_to_str(render_panel(12, state, Theme()), width=160, no_color=True)
    assert "(1 unreadable)" in detail
    assert "oversized skipped: response_store.db" in detail
    assert "at or above the cap" in detail
    assert "cache/campaign" in detail
    assert "≥2.1G" in detail
    assert "normal for active sessions" in detail
    assert "2 dir(s) still being sized" in detail
    assert "showing 2 of 5" in detail
    assert "checkpoints over 500 MB cap" in compact
    assert "1 cache dir(s) ≥ 1 GiB" in compact
