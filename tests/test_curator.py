from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermesd.collector import Collector


def _write_curator_run(home: Path, stamp: str, payload: dict) -> Path:
    run_dir = home / "logs" / "curator" / stamp
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(payload))
    return run_dir


_LIVE_SHAPE = {
    "started_at": "2026-06-10T13:35:39+00:00",
    "duration_seconds": 597.0,
    "model": "MiniMax-M3",
    "provider": "minimax",
    "counts": {
        "before": 8,
        "after": 5,
        "delta": -3,
        "archived_this_run": 3,
        "added_this_run": 0,
        "pruned_this_run": 3,
        "consolidated_this_run": 0,
        "tool_calls_total": 67,
    },
    "tool_call_counts": {"read_file": 12, "list_dir": 3},
    "state_transitions": [
        {"from": "collecting", "to": "summarizing", "at": "2026-06-10T13:40:00+00:00"}
    ],
    "llm_summary": "## Summary\nprocessed the candidates",
    "llm_error": None,
}


def test_collect_curator_reads_newest_run(hermes_home: Path):
    _write_curator_run(hermes_home, "20260601-100000", {"model": "stale", "counts": {"before": 1}})
    _write_curator_run(hermes_home, "20260610-133539", _LIVE_SHAPE)

    c = Collector(hermes_home)
    state = c.collect()
    cur = state.curator
    assert cur.run_present is True
    assert cur.stamp == "20260610-133539"
    assert cur.model == "MiniMax-M3"
    assert cur.provider == "minimax"
    assert cur.duration_seconds == 597.0
    assert cur.count_before == 8
    assert cur.count_after == 5
    assert cur.count_delta == -3
    assert cur.archived_count == 3
    assert cur.pruned_count == 3
    assert cur.consolidated_count == 0
    assert cur.tool_calls_total == 67
    assert cur.tool_call_counts == {"read_file": 12, "list_dir": 3}
    assert cur.state_transitions == ["collecting -> summarizing @ 2026-06-10T13:40:00+00:00"]
    assert cur.llm_error == ""
    c.close()


def test_collect_curator_absent_is_empty(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.curator.run_present is False
    assert state.curator.model == ""
    assert "curator" not in state.health.failed_sources
    c.close()


def test_collect_curator_empty_dir_is_empty(hermes_home: Path):
    (hermes_home / "logs" / "curator").mkdir(parents=True)
    c = Collector(hermes_home)
    state = c.collect()
    assert state.curator.run_present is False
    c.close()


def test_collect_curator_run_dir_without_run_json_is_empty(hermes_home: Path):
    # A run directory exists but run.json is missing (e.g. interrupted run) —
    # degrade to empty, not a failed source.
    (hermes_home / "logs" / "curator" / "20260610-133539").mkdir(parents=True)
    c = Collector(hermes_home)
    state = c.collect()
    assert state.curator.run_present is False
    assert "curator" not in state.health.failed_sources
    c.close()


def test_collect_curator_skips_symlinked_run_dir(hermes_home: Path):
    curator_dir = hermes_home / "logs" / "curator"
    curator_dir.mkdir(parents=True)
    outside = hermes_home / "outside_run"
    outside.mkdir()
    (outside / "run.json").write_text(json.dumps({"model": "leaked", "run_present": True}))
    try:
        (curator_dir / "20260610-133539").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks not supported here")
    c = Collector(hermes_home)
    state = c.collect()
    assert state.curator.run_present is False
    assert state.curator.model == ""
    assert "curator" not in state.health.failed_sources
    c.close()


def test_collect_curator_skips_symlinked_root_dir(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "curator"
    run_dir = outside / "20260610-133539"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"model": "leaked"}))
    logs_dir = hermes_home / "logs"
    (logs_dir / "curator").symlink_to(outside, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.curator.run_present is False
    assert state.curator.model == ""
    assert "curator" not in state.health.failed_sources
    c.close()
