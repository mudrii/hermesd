"""Gateway error paths: a corrupt state file must not blank the last-good gateway state."""

from __future__ import annotations

import json
from pathlib import Path

from hermesd.collector import Collector


def test_gateway_preserves_last_good_state_when_state_json_is_corrupt(hermes_home: Path):
    live_pid = 4242
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": live_pid,
                "gateway_state": "running",
                "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
            }
        )
    )

    c = Collector(hermes_home, pid_exists=lambda pid: pid == live_pid)
    first = c.collect()
    gw.write_text("{not valid json")
    second = c.collect()

    assert second.gateway.pid == first.gateway.pid
    assert second.gateway.running is True
    assert second.gateway.platforms[0].name == "telegram"
    c.close()
