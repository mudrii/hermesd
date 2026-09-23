"""Disk & retention readout: log sizes/growth, stores vs caps, WAL and journal modes."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

import hermesd.collect.operations as operations_module
import hermesd.collector as collector_module
from hermesd.collector import Collector
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str

_MIB = 1024 * 1024
_NOW = 1_790_000_000.0


class _Clock:
    def __init__(self, now: float = _NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size)


def _collect(home: Path, clock: _Clock | None = None, **kwargs: object):
    collector = Collector(home, clock=clock or _Clock(), **kwargs)  # type: ignore[arg-type]
    try:
        return collector.collect()
    finally:
        collector.close()


def _sqlite_header(mode_byte: int) -> bytes:
    return b"SQLite format 3\x00" + b"\x10\x00" + bytes([mode_byte, mode_byte]) + b"\x00" * 80


def test_unrotated_log_over_10mb_is_flagged(hermes_home: Path):
    """Upstream rotates only agent/errors/gateway/gui.log (hermes_logging.py:241-244)."""
    logs = hermes_home / "logs"
    _sparse(logs / "gateway.error.log", 11 * _MIB)
    _sparse(logs / "agent.log", 11 * _MIB)
    _sparse(logs / "mcp-stderr.log", 2 * _MIB)
    disk = _collect(hermes_home).disk
    by_name = {entry.name: entry for entry in disk.log_files}
    assert by_name["gateway.error.log"].unrotated_oversize is True
    assert by_name["gateway.error.log"].rotated_upstream is False
    assert by_name["agent.log"].unrotated_oversize is False
    assert by_name["agent.log"].rotated_upstream is True
    assert by_name["mcp-stderr.log"].unrotated_oversize is False
    assert disk.unrotated_oversize_count == 1
    assert disk.logs_dir_bytes >= 24 * _MIB
    # Largest first.
    assert disk.log_files[0].size_bytes >= disk.log_files[-1].size_bytes


def test_log_growth_rate_over_observed_window(hermes_home: Path):
    log = hermes_home / "logs" / "workspace.log"
    _sparse(log, 1000)
    clock = _Clock()
    collector = Collector(hermes_home, clock=clock)
    try:
        first = collector.collect().disk
        assert {e.name: e for e in first.log_files}["workspace.log"].growth_bytes_per_hour is None
        clock.now += 1800
        _sparse(log, 1000 + 50_000)
        second = collector.collect().disk
        entry = {e.name: e for e in second.log_files}["workspace.log"]
        assert entry.growth_bytes_per_hour == pytest.approx(100_000.0)
        clock.now += 120
        _sparse(log, 10)  # truncated / rotated: the window restarts
        third = collector.collect().disk
        assert {e.name: e for e in third.log_files}["workspace.log"].growth_bytes_per_hour is None
    finally:
        collector.close()


def test_sessions_and_checkpoint_store_against_cap(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"checkpoints": {"enabled": True, "max_total_size_mb": 1}})
    )
    _sparse(hermes_home / "sessions" / "session_a.json", 3000)
    _sparse(hermes_home / "sessions" / "nested" / "b.json", 2000)
    _sparse(hermes_home / "checkpoints" / "repo1" / "objects" / "pack.pack", 2 * _MIB)
    disk = _collect(hermes_home).disk
    assert disk.sessions_bytes == 5000
    assert disk.checkpoints_bytes == 2 * _MIB
    assert disk.checkpoints_enabled is True
    assert disk.checkpoints_cap_mb == 1
    assert disk.checkpoints_over_cap is True


def test_checkpoint_cap_defaults_and_disabled_store(hermes_home: Path):
    _sparse(hermes_home / "checkpoints" / "repo1" / "pack", 600 * _MIB)
    disk = _collect(hermes_home).disk
    # checkpoints.enabled defaults to false upstream: no over-cap verdict.
    assert disk.checkpoints_enabled is False
    assert disk.checkpoints_cap_mb == 500
    assert disk.checkpoints_over_cap is False


def test_cache_hogs_and_scratch(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    """cache/* dirs outside the pruned scratch/terminal at >= 1 GiB are hogs
    (hermes_cli/doctor_state.py:168-190)."""
    monkeypatch.setattr(operations_module, "_CACHE_HOG_MIN_BYTES", 1000)
    cache = hermes_home / "cache"
    _sparse(cache / "scratch" / "task" / "big.bin", 5000)
    _sparse(cache / "terminal" / "out.log", 5000)
    _sparse(cache / "campaign" / "tree" / "a.bin", 4000)
    _sparse(cache / "images" / "small.png", 10)
    (cache / "banner_snapshot.json").write_text("{}")
    clock = _Clock()
    collector = Collector(hermes_home, clock=clock)
    try:
        # The per-refresh walk budget spreads the cache/* walks over passes.
        disk = collector.collect().disk
        while disk.pending_walks:
            clock.now += 1
            disk = collector.collect().disk
    finally:
        collector.close()
    assert disk.scratch_bytes == 5000
    assert [(hog.name, hog.size_bytes) for hog in disk.cache_hogs] == [("campaign", 4000)]


@pytest.mark.parametrize(
    ("wal_mib", "verdict"),
    [(0, "ok"), (11, "note"), (51, "warn")],
)
def test_state_db_wal_thresholds(hermes_home: Path, sample_db: Path, wal_mib: int, verdict: str):
    """doctor: > 10 MB is info, > 50 MB warns (hermes_cli/doctor_state.py:354-390)."""
    if wal_mib:
        _sparse(hermes_home / "state.db-wal", wal_mib * _MIB)
    disk = _collect(hermes_home).disk
    assert disk.state_db_wal_verdict == verdict
    assert disk.state_db_wal_bytes == wal_mib * _MIB


def test_journal_modes_from_header_bytes(hermes_home: Path):
    """Header byte 18: 2 = WAL, 1 = rollback (hermes_cli/doctor_platform.py:64-81)."""
    (hermes_home / "projects.db").write_bytes(_sqlite_header(2))
    (hermes_home / "response_store.db").write_bytes(_sqlite_header(1))
    (hermes_home / "verification_evidence.db").write_bytes(b"not a database at all, sorry!")
    (hermes_home / "memory_store.db").write_bytes(b"")
    board = hermes_home / "kanban" / "boards" / "ops"
    board.mkdir(parents=True)
    conn = sqlite3.connect(str(board / "kanban.db"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    disk = _collect(hermes_home).disk
    modes = {db.name: db for db in disk.databases}
    assert modes["projects.db"].journal_mode == "wal"
    assert modes["response_store.db"].journal_mode == "rollback"
    assert modes["verification_evidence.db"].journal_mode == ""
    assert modes["verification_evidence.db"].error == "file is not a database"
    assert modes["memory_store.db"].error == "file is empty"
    assert modes["kanban/boards/ops/kanban.db"].journal_mode == "wal"
    assert "state.db" not in modes  # absent files are not listed


def test_journal_mode_read_never_creates_sidecars(hermes_home: Path):
    (hermes_home / "projects.db").write_bytes(_sqlite_header(2))
    _collect(hermes_home)
    assert not (hermes_home / "projects.db-wal").exists()
    assert not (hermes_home / "projects.db-shm").exists()


def test_tree_walks_are_cached_between_refreshes(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _sparse(hermes_home / "sessions" / "a.json", 10)
    walked: list[str] = []
    original = operations_module._bounded_tree_bytes

    def counting(path: Path, *args: object, **kwargs: object):
        walked.append(Path(path).name)
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(operations_module, "_bounded_tree_bytes", counting)
    clock = _Clock()
    collector = Collector(hermes_home, clock=clock)
    try:
        collector.collect()
        first = walked.count("sessions")
        _sparse(hermes_home / "sessions" / "b.json", 10)  # top mtime changes
        clock.now += 5
        collector.collect()
        assert walked.count("sessions") == first  # within the minimum interval
        clock.now += 3600
        state = collector.collect()
        assert walked.count("sessions") == first + 1
        assert state.disk.sessions_bytes == 20
    finally:
        collector.close()


def test_walk_budget_defers_extra_directories(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(operations_module, "_DISK_WALKS_PER_PASS", 1)
    _sparse(hermes_home / "sessions" / "a.json", 10)
    _sparse(hermes_home / "checkpoints" / "r" / "p", 10)
    clock = _Clock()
    collector = Collector(hermes_home, clock=clock)
    try:
        first = collector.collect().disk
        assert first.pending_walks >= 1
        for _ in range(6):
            clock.now += 1
            last = collector.collect().disk
        assert last.pending_walks == 0
        assert last.sessions_bytes == 10
        assert last.checkpoints_bytes == 10
    finally:
        collector.close()


def test_disk_scan_failure_keeps_last_good(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    _sparse(hermes_home / "logs" / "gateway.error.log", 11 * _MIB)
    collector = Collector(hermes_home, clock=_Clock())
    try:
        before = collector.collect()

        def boom(*args: object, **kwargs: object):
            raise OSError("scan failed")

        monkeypatch.setattr(collector_module, "_read_disk_usage", boom)
        after = collector.collect()
    finally:
        collector.close()
    assert before.disk.unrotated_oversize_count == 1
    assert "disk_usage" in after.health.failed_sources
    assert after.disk.unrotated_oversize_count == 1


def test_symlinked_store_dirs_are_not_walked(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    _sparse(outside / "big.bin", 5000)
    shutil.rmtree(hermes_home / "sessions", ignore_errors=True)
    (hermes_home / "sessions").symlink_to(outside, target_is_directory=True)
    (hermes_home / "logs" / "escape.log").symlink_to(outside / "big.bin")
    disk = _collect(hermes_home).disk
    assert disk.sessions_bytes == 0
    assert "escape.log" not in {entry.name for entry in disk.log_files}


def test_disk_section_renders_in_operations_panel(hermes_home: Path, sample_db: Path):
    _sparse(hermes_home / "logs" / "gateway.error.log", 39 * _MIB)
    _sparse(hermes_home / "state.db-wal", 51 * _MIB)
    state = _collect(hermes_home)
    compact = render_to_str(render_panel(12, state, Theme()), width=100, no_color=True)
    detail = render_to_str(render_panel(12, state, Theme(), detail=True), width=180, no_color=True)
    assert "1 unrotated log > 10 MB" in compact
    assert "WAL 53.5M (> 50 MB)" in compact
    assert "Disk & Retention" in detail
    assert "gateway.error.log" in detail
    assert "unrotated" in detail
    assert "state.db: rollback" in detail


def test_disk_usage_is_in_json_snapshot(hermes_home: Path):
    _sparse(hermes_home / "logs" / "gateway.error.log", 10)
    dumped = _collect(hermes_home).model_dump(mode="json")
    assert dumped["disk"]["log_files"][0]["name"] == "gateway.error.log"


def test_logs_dir_listing_is_bounded(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(operations_module, "_MAX_LOG_FILE_ROWS", 2)
    for index in range(5):
        _sparse(hermes_home / "logs" / f"f{index}.log", 100 * (index + 1))
    disk = _collect(hermes_home).disk
    assert [entry.name for entry in disk.log_files] == ["f4.log", "f3.log"]
    assert disk.log_file_count == 5


def test_walker_tolerates_entries_vanishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "tree"
    _sparse(root / "keep.bin", 7)
    _sparse(root / "gone.bin", 9)
    real_scandir = os.scandir

    class _Vanishing:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self._entry = entry
            self.path = entry.path
            self.name = entry.name

        def is_symlink(self) -> bool:
            return self._entry.is_symlink()

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

        def is_file(self, *, follow_symlinks: bool = True) -> bool:
            return self._entry.is_file(follow_symlinks=follow_symlinks)

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            if self.name == "gone.bin":
                raise FileNotFoundError(self.path)
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class _Scan:
        def __init__(self, path: str) -> None:
            self._it = real_scandir(path)

        def __enter__(self):
            return (_Vanishing(entry) for entry in self._it)

        def __exit__(self, *exc: object) -> None:
            self._it.close()

    monkeypatch.setattr(operations_module.os, "scandir", _Scan)
    assert operations_module._bounded_tree_bytes(root) == (7, False)
