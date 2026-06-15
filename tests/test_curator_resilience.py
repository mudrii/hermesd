from __future__ import annotations

import json
import os
from pathlib import Path

from hermesd.collector import Collector


def _write_curator_run(home: Path, stamp: str, payload: dict) -> Path:
    run_dir = home / "logs" / "curator" / stamp
    run_dir.mkdir(parents=True)
    run_json = run_dir / "run.json"
    run_json.write_text(json.dumps(payload))
    return run_json


def test_curator_preserves_last_good_on_corruption(hermes_home: Path):
    run_json = _write_curator_run(
        hermes_home,
        "20260610-133539",
        {"model": "MiniMax-M3", "provider": "minimax", "counts": {"before": 8, "after": 5}},
    )
    c = Collector(hermes_home)
    first = c.collect()
    assert first.curator.model == "MiniMax-M3"

    run_json.write_text("{not valid json")
    os.utime(run_json, None)
    second = c.collect()

    # Cache-preservation: corrupt run.json keeps the last-good curator data.
    assert second.curator.model == "MiniMax-M3"
    assert second.curator.count_before == 8
    c.close()
