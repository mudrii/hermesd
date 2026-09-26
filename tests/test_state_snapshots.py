"""State snapshot grouping, bounded recursive sizes and manifest.json verdicts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import hermesd.collect.operations as operations_module
from hermesd.collector import Collector
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str


def _write_snapshot_manifest(snapshot: Path, **overrides: object) -> None:
    """A manifest.json in upstream's shape (hermes_cli/backup.py:1271-1276)."""
    manifest: dict[str, object] = {
        "id": snapshot.name,
        "timestamp": snapshot.name[:15],
        "label": "pre-update",
        "file_count": 2,
        "total_size": 3000,
        "files": {"state.db": 2000, "cron/executions.db": 1000},
        "failed_dbs": [],
        "oversized_skipped": [],
    }
    manifest.update(overrides)
    (snapshot / "manifest.json").write_text(json.dumps(manifest))


def test_loose_db_sidecars_are_one_snapshot(hermes_home: Path):
    """A parked ``x.db`` with its ``-wal``/``-shm`` is one snapshot, not three."""
    root = hermes_home / "state-snapshots"
    root.mkdir()
    (root / "state-20260728-pre-fts-opt.db").write_bytes(b"d" * 1000)
    (root / "state-20260728-pre-fts-opt.db-wal").write_bytes(b"w" * 100)
    (root / "state-20260728-pre-fts-opt.db-shm").write_bytes(b"s" * 10)
    (root / "other.db-journal").write_bytes(b"j" * 5)

    snapshots = operations_module._read_state_snapshots(root, hermes_home, now=time.time())

    assert snapshots["snapshot_count"] == 2
    assert snapshots["snapshot_total_bytes"] == 1115
    by_name = {snap.name: snap for snap in snapshots["snapshots"]}
    assert by_name["state-20260728-pre-fts-opt.db"].size_bytes == 1110
    assert by_name["state-20260728-pre-fts-opt.db"].kind == "file"
    assert by_name["other.db"].size_bytes == 5


def test_dir_size_includes_subdirectories(hermes_home: Path):
    """A quick snapshot keeps ``cron/executions.db`` in a subdirectory
    (``_QUICK_STATE_FILES``, hermes_cli/backup.py:1121-1132)."""
    root = hermes_home / "state-snapshots"
    snapshot = root / "20260923-075337-pre-update"
    (snapshot / "cron").mkdir(parents=True)
    (snapshot / "state.db").write_bytes(b"x" * 2000)
    (snapshot / "cron" / "executions.db").write_bytes(b"c" * 1000)

    snapshots = operations_module._read_state_snapshots(root, hermes_home, now=time.time())

    assert snapshots["snapshot_total_bytes"] == 3000
    assert snapshots["snapshots"][0].size_bytes == 3000
    assert snapshots["snapshots"][0].kind == "dir"
    assert snapshots["snapshots"][0].manifest_present is False


def test_dir_size_skips_symlinks_inside_a_snapshot(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"o" * 5000)
    snapshot = hermes_home / "state-snapshots" / "20260923-075337-pre-update"
    snapshot.mkdir(parents=True)
    (snapshot / "state.db").write_bytes(b"x" * 10)
    (snapshot / "escape.db").symlink_to(outside)
    (snapshot / "escape-dir").symlink_to(tmp_path, target_is_directory=True)

    snapshots = operations_module._read_state_snapshots(
        hermes_home / "state-snapshots", hermes_home, now=time.time()
    )

    assert snapshots["snapshot_total_bytes"] == 10


def test_dir_walk_is_bounded(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(operations_module, "_TREE_WALK_MAX_ENTRIES", 3)
    snapshot = hermes_home / "state-snapshots" / "20260923-075337-pre-update"
    snapshot.mkdir(parents=True)
    for index in range(10):
        (snapshot / f"f{index}.db").write_bytes(b"x")

    snapshots = operations_module._read_state_snapshots(
        hermes_home / "state-snapshots", hermes_home, now=time.time()
    )

    assert snapshots["snapshots"][0].size_bytes == 3
    assert snapshots["snapshots"][0].size_truncated is True


def test_manifest_flags_failed_dbs(hermes_home: Path):
    root = hermes_home / "state-snapshots"
    good = root / "20260920-010000-pre-update"
    bad = root / "20260923-075337-pre-update"
    for snapshot in (good, bad):
        snapshot.mkdir(parents=True)
        (snapshot / "state.db").write_bytes(b"x" * 100)
    _write_snapshot_manifest(good)
    _write_snapshot_manifest(
        bad,
        label="pre-update",
        failed_dbs=["state.db", "kanban.db"],
        oversized_skipped=["response_store.db"],
        total_size=482_000_000,
        file_count=12,
    )
    os.utime(good, (1_000, 1_000))

    snapshots = operations_module._read_state_snapshots(root, hermes_home, now=time.time())

    assert snapshots["snapshot_failed_count"] == 1
    newest = snapshots["snapshots"][0]
    assert newest.name == bad.name
    assert newest.manifest_present is True
    assert newest.label == "pre-update"
    assert newest.failed_dbs == ["state.db", "kanban.db"]
    assert newest.oversized_skipped == ["response_store.db"]
    assert newest.manifest_total_size == 482_000_000
    assert newest.file_count == 12
    assert snapshots["snapshots"][1].failed_dbs == []


def test_manifest_null_and_junk_fields_are_tolerated(hermes_home: Path):
    snapshot = hermes_home / "state-snapshots" / "20260923-075337"
    snapshot.mkdir(parents=True)
    _write_snapshot_manifest(
        snapshot, label=None, failed_dbs=None, oversized_skipped="x", total_size=None
    )

    snapshots = operations_module._read_state_snapshots(
        hermes_home / "state-snapshots", hermes_home, now=time.time()
    )

    entry = snapshots["snapshots"][0]
    assert entry.manifest_present is True
    assert entry.label == ""
    assert entry.failed_dbs == []
    assert entry.oversized_skipped == []
    assert entry.manifest_total_size == 0


def test_torn_manifest_is_not_an_error(hermes_home: Path):
    root = hermes_home / "state-snapshots"
    snapshot = root / "20260923-075337-pre-update"
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text("{torn")

    snapshots = operations_module._read_state_snapshots(root, hermes_home, now=time.time())

    assert snapshots["snapshot_count"] == 1
    assert snapshots["snapshots"][0].manifest_present is False
    assert snapshots["snapshot_failed_count"] == 0


def test_dir_size_is_cached_by_signature(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    """An unchanged snapshot dir is not re-walked on every refresh."""
    root = hermes_home / "state-snapshots"
    snapshot = root / "20260923-075337-pre-update"
    (snapshot / "cron").mkdir(parents=True)
    (snapshot / "state.db").write_bytes(b"x" * 10)
    cache: dict[str, object] = {}
    walks: list[Path] = []
    original = operations_module._bounded_tree_bytes

    def counting(path: Path, *args: object, **kwargs: object):
        walks.append(path)
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(operations_module, "_bounded_tree_bytes", counting)
    now = time.time()
    operations_module._read_state_snapshots(root, hermes_home, now=now, size_cache=cache)
    operations_module._read_state_snapshots(root, hermes_home, now=now + 5, size_cache=cache)
    assert walks == [snapshot]
    (snapshot / "late.db").write_bytes(b"y")
    third = operations_module._read_state_snapshots(
        root, hermes_home, now=now + 10, size_cache=cache
    )
    assert walks == [snapshot, snapshot]
    assert third["snapshot_total_bytes"] == 11


def test_failed_dbs_render_in_operations_panel(hermes_home: Path, sample_db: Path):
    snapshot = hermes_home / "state-snapshots" / "20260923-075337-pre-update"
    snapshot.mkdir(parents=True)
    (snapshot / "state.db").write_bytes(b"x" * 100)
    _write_snapshot_manifest(snapshot, failed_dbs=["[bold]state.db"])
    collector = Collector(hermes_home)
    try:
        state = collector.collect()
    finally:
        collector.close()

    assert state.operations.snapshot_failed_count == 1
    detail = render_to_str(render_panel(12, state, Theme(), detail=True), width=160, no_color=True)
    compact = render_to_str(
        render_panel(12, state, Theme(), detail=False), width=100, no_color=True
    )
    assert "20260923-075337-pre-update" in detail
    assert "failed DBs: [bold]state.db" in detail
    assert "1 with failed DBs" in compact
