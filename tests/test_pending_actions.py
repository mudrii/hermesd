"""Pending operator actions staged under ``pending/<subsystem>/<id>.json``.

Upstream's write-approval gate stages memory/skill writes for review as
``{"id", "subsystem", "action", "summary", "origin", "created_at", "payload"}``
(``tools/write_approval.py:64-86``). hermesd reports count and oldest age per
subsystem and never reads the payload into state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import hermesd.collector as collector_module
from hermesd.collector import Collector
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str

_NOW = 1_790_000_000.0


def _collect(home: Path, **kwargs: object):
    collector = Collector(home, clock=lambda: _NOW, **kwargs)  # type: ignore[arg-type]
    try:
        return collector.collect()
    finally:
        collector.close()


def _stage(home: Path, subsystem: str, pending_id: str, created_at: object, **extra: object):
    directory = home / "pending" / subsystem
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "id": pending_id,
        "subsystem": subsystem,
        "action": "add",
        "summary": "apply 1 op(s)",
        "origin": "background_review",
        "created_at": created_at,
        "payload": {"content": "PAYLOAD-SENTINEL secret body"},
    }
    record.update(extra)
    path = directory / f"{pending_id}.json"
    path.write_text(json.dumps(record))
    return path


def test_no_pending_dir_reports_nothing(hermes_home: Path):
    ops = _collect(hermes_home).operations
    assert ops.pending_action_total == 0
    assert ops.pending_actions == []


def test_counts_and_oldest_age_per_subsystem(hermes_home: Path):
    _stage(hermes_home, "memory", "aaaa1111", _NOW - 7200)
    _stage(hermes_home, "memory", "bbbb2222", _NOW - 60)
    _stage(hermes_home, "skills", "cccc3333", _NOW - 300)
    state = _collect(hermes_home)
    ops = state.operations
    assert ops.pending_action_total == 3
    by_name = {entry.subsystem: entry for entry in ops.pending_actions}
    assert by_name["memory"].count == 2
    assert by_name["memory"].oldest_age_seconds == pytest.approx(7200.0)
    assert by_name["skills"].count == 1
    assert by_name["skills"].oldest_age_seconds == pytest.approx(300.0)
    assert "PAYLOAD-SENTINEL" not in state.model_dump_json()
    assert "pending_actions" not in state.health.failed_sources


def test_unreadable_record_counts_and_ages_by_mtime(hermes_home: Path):
    path = _stage(hermes_home, "memory", "torn0000", _NOW)
    path.write_text("{torn")
    os.utime(path, (_NOW - 900, _NOW - 900))
    entry = _collect(hermes_home).operations.pending_actions[0]
    assert entry.count == 1
    assert entry.unreadable_count == 1
    assert entry.oldest_age_seconds == pytest.approx(900.0)


def test_null_created_at_falls_back_to_mtime(hermes_home: Path):
    path = _stage(hermes_home, "skills", "null0000", None)
    os.utime(path, (_NOW - 120, _NOW - 120))
    entry = _collect(hermes_home).operations.pending_actions[0]
    assert entry.unreadable_count == 0
    assert entry.oldest_age_seconds == pytest.approx(120.0)


def test_symlinked_records_and_dirs_are_skipped(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.json").write_text(json.dumps({"created_at": _NOW - 5}))
    pending = hermes_home / "pending"
    pending.mkdir()
    (pending / "evil").symlink_to(outside, target_is_directory=True)
    (pending / "memory").mkdir()
    (pending / "memory" / "link.json").symlink_to(outside / "x.json")
    ops = _collect(hermes_home).operations
    assert ops.pending_action_total == 0


def test_profile_scoped_pending_dir(profiled_hermes_home: Path):
    _stage(profiled_hermes_home, "memory", "root0000", _NOW - 10)
    _stage(profiled_hermes_home / "profiles" / "coding", "skills", "prof0000", _NOW - 20)
    ops = _collect(profiled_hermes_home, profile_name="coding").operations
    assert [entry.subsystem for entry in ops.pending_actions] == ["skills"]


def test_records_are_parsed_once_per_signature(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    _stage(hermes_home, "memory", "aaaa1111", _NOW - 10)
    reads: list[Path] = []
    original = collector_module._pending_record_created_at

    def counting(path: Path, home: Path):
        reads.append(path)
        return original(path, home)

    monkeypatch.setattr(collector_module, "_pending_record_created_at", counting)
    collector = Collector(hermes_home, clock=lambda: _NOW)
    try:
        collector.collect()
        collector.collect()
    finally:
        collector.close()
    assert len(reads) == 1


def test_scan_failure_keeps_last_good(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    _stage(hermes_home, "memory", "aaaa1111", _NOW - 10)
    collector = Collector(hermes_home, clock=lambda: _NOW)
    try:
        before = collector.collect()

        def boom(*args: object, **kwargs: object):
            raise OSError("scan failed")

        monkeypatch.setattr(collector_module, "_read_pending_actions", boom)
        after = collector.collect()
    finally:
        collector.close()
    assert before.operations.pending_action_total == 1
    assert "pending_actions" in after.health.failed_sources
    assert after.operations.pending_action_total == 1


def test_operations_panel_shows_pending_actions(hermes_home: Path):
    _stage(hermes_home, "memory", "aaaa1111", _NOW - 7200)
    _stage(hermes_home, "skil[bold]ls", "bbbb2222", _NOW - 60)
    state = _collect(hermes_home)
    compact = render_to_str(render_panel(12, state, Theme()), width=100, no_color=True)
    detail = render_to_str(render_panel(12, state, Theme(), detail=True), width=160, no_color=True)
    assert "Pending review: 2 (oldest 2h)" in compact
    assert "memory: 1 · oldest 2h" in detail
    assert "skil[bold]ls: 1" in detail
