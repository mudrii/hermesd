from __future__ import annotations

import json
from pathlib import Path

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
    "llm_summary": "## Summary\nprocessed the candidates",
    "llm_error": None,
}


def test_collect_curator_reads_newest_run(hermes_home: Path):
    _write_curator_run(hermes_home, "20260601-100000", {"model": "stale", "counts": {"before": 1}})
    _write_curator_run(hermes_home, "20260610-133539", _LIVE_SHAPE)

    state = Collector(hermes_home).collect()
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
    assert cur.llm_error == ""


def test_collect_curator_absent_is_empty(hermes_home: Path):
    state = Collector(hermes_home).collect()
    assert state.curator.run_present is False
    assert state.curator.model == ""
    assert "curator" not in state.health.failed_sources


def test_collect_curator_empty_dir_is_empty(hermes_home: Path):
    (hermes_home / "logs" / "curator").mkdir(parents=True)
    state = Collector(hermes_home).collect()
    assert state.curator.run_present is False
