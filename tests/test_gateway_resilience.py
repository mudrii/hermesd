"""Gateway error paths: a corrupt state file must not blank the last-good gateway state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermesd.collector import Collector
from tests.conftest import create_state_db_tables


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


def _write_running_gateway(home: Path) -> None:
    (home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 4242,
                "gateway_state": "running",
                "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
            }
        )
    )


def _state_dir(home: Path) -> Path:
    state_dir = home / "state"
    state_dir.mkdir(exist_ok=True)
    return state_dir


def test_corrupt_heartbeat_keeps_last_good_and_marks_source_failed(hermes_home: Path):
    _write_running_gateway(hermes_home)
    heartbeat = _state_dir(hermes_home) / "gateway.heartbeat"
    heartbeat.write_text(json.dumps({"pid": 4242, "updated_at": "2026-09-07T00:00:00+00:00"}))

    c = Collector(hermes_home, pid_exists=lambda pid: True)
    first = c.collect()
    assert first.gateway.loop_health.value != "unknown"
    heartbeat.write_text("{not json")
    second = c.collect()

    assert second.gateway.loop_health == first.gateway.loop_health
    assert second.gateway.heartbeat_age_seconds == first.gateway.heartbeat_age_seconds
    assert "gateway_heartbeat" in second.health.failed_sources
    c.close()


def test_corrupt_lifecycle_keeps_last_good_and_marks_source_failed(hermes_home: Path):
    _write_running_gateway(hermes_home)
    lifecycle = _state_dir(hermes_home) / "gateway.lifecycle.json"
    lifecycle.write_text(
        json.dumps({"phase": "exited", "pid": 4242, "exit_code": 7, "exit_reason": "sigkill"})
    )

    c = Collector(hermes_home, pid_exists=lambda pid: True)
    first = c.collect()
    assert first.gateway.last_exit_code == 7
    lifecycle.write_text("[]")
    second = c.collect()

    assert second.gateway.last_exit_code == 7
    assert second.gateway.last_exit_reason == "sigkill"
    assert "gateway_lifecycle" in second.health.failed_sources
    c.close()


def test_corrupt_update_receipt_keeps_last_good_and_marks_source_failed(hermes_home: Path):
    _write_running_gateway(hermes_home)
    receipts = hermes_home / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    receipt = receipts / "latest.json"
    receipt.write_text(json.dumps({"outcome": "ok", "post_update": {"version": "2026.9.1"}}))

    c = Collector(hermes_home, pid_exists=lambda pid: True)
    first = c.collect()
    assert first.gateway.last_update_outcome == "ok"
    receipt.write_text("{{{")
    second = c.collect()

    assert second.gateway.last_update_outcome == "ok"
    assert second.gateway.last_update_to_version == "2026.9.1"
    assert "update_receipt" in second.health.failed_sources
    c.close()


def test_corrupt_state_db_keeps_last_good_gateway_ledgers(hermes_home: Path):
    _write_running_gateway(hermes_home)
    state_db = hermes_home / "state.db"
    conn = sqlite3.connect(str(state_db))
    create_state_db_tables(conn)
    conn.executescript(
        "CREATE TABLE gateway_heartbeats ("
        " backend_id TEXT PRIMARY KEY, pid INTEGER, started_at REAL,"
        " last_heartbeat REAL, profile TEXT, host TEXT);"
        "INSERT INTO gateway_heartbeats VALUES ('b1', 1, 1000.0, 1001.0, 'root', 'host');"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, pid_exists=lambda pid: True)
    first = c.collect()
    assert first.gateway.gateway_incarnation_count == 1

    state_db.write_bytes(b"not a sqlite database")
    second = c.collect()

    assert second.gateway.gateway_incarnation_count == 1
    assert "gateway_ledgers" in second.health.failed_sources
    c.close()


def test_gateway_liveness_defaults_when_no_liveness_files_exist(hermes_home: Path):
    """Missing liveness files must not fail a source or blank the panel."""
    _write_running_gateway(hermes_home)
    c = Collector(hermes_home, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()

    assert state.gateway.running is True
    assert state.gateway.loop_health.value == "unknown"
    assert "gateway_heartbeat" not in state.health.failed_sources
    assert "gateway_lifecycle" not in state.health.failed_sources
    assert "update_receipt" not in state.health.failed_sources
