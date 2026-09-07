"""Collection of gateway state, PID and launchd detection, channels,
background processes, and the spawn ledger."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from hermesd.collect.gateway import (
    _INCARNATION_SCAN_LIMIT,
    _OPEN_DELIVERY_LIMIT,
)
from hermesd.collector import (
    Collector,
    _is_dashboard_process,
    _pid_exists,
)
from hermesd.models import GatewayLoopHealth
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import create_state_db_tables, render_to_str


def test_collect_gateway_preserves_last_good_mapping_on_non_mapping_json(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 12345,
                "gateway_state": "running",
                "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
            }
        )
    )

    c = Collector(hermes_home, pid_exists=lambda pid: pid == 12345)
    first = c.collect()
    assert first.gateway.running is True
    assert first.gateway.pid == 12345

    gw.write_text(json.dumps(["not", "a", "mapping"]))
    second = c.collect()
    assert second.gateway.running is True
    assert second.gateway.pid == 12345
    c.close()


def test_collect_gateway_lifecycle_drain_and_served_profiles(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"scale_to_zero": {"idle_timeout_minutes": 15, "relay_only": True}})
    )
    (hermes_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 12345,
                "gateway_state": "running",
                "active_agents": 2,
                "served_profiles": ["root", "coding"],
                "platforms": {},
            }
        )
    )
    (hermes_home / ".drain_request.json").write_text(
        json.dumps(
            {
                "requested_at": "2026-07-10T10:00:00Z",
                "principal": "nas",
                "suppress_notification": True,
            }
        )
    )

    c = Collector(hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()
    gw = state.gateway

    assert gw.running is True
    assert gw.busy is True
    assert gw.drainable is False
    assert gw.drain_active is True
    assert gw.drain_principal == "nas"
    assert gw.drain_suppress_notification is True
    assert gw.served_profiles == ["root", "coding"]
    assert gw.scale_to_zero_idle_timeout_minutes == 15
    assert gw.scale_to_zero_relay_only is True
    c.close()


def test_collect_gateway_derives_relay_only_from_connected_platforms(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"scale_to_zero": {"idle_timeout_minutes": 10}})
    )
    (hermes_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 12345,
                "gateway_state": "running",
                "platforms": {"raft": {"state": "connected"}},
            }
        )
    )

    c = Collector(hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()

    assert state.gateway.scale_to_zero_idle_timeout_minutes == 10
    assert state.gateway.scale_to_zero_relay_only is True
    c.close()


def test_collect_gateway_treats_absent_connected_platforms_as_relay_only(
    hermes_home: Path,
):
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"scale_to_zero": {"idle_timeout_minutes": 10}})
    )
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": 12345, "gateway_state": "running", "platforms": {}})
    )

    c = Collector(hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()

    assert state.gateway.scale_to_zero_relay_only is True
    c.close()


def test_collect_gateway_idle_state_is_drainable(hermes_home: Path):
    (hermes_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 12345,
                "gateway_state": "running",
                "active_agents": 0,
                "platforms": {},
            }
        )
    )

    c = Collector(hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()

    assert state.gateway.running is True
    assert state.gateway.busy is False
    assert state.gateway.drainable is True
    assert state.gateway.drain_active is False
    c.close()


def test_collect_channels_flags_alias_staleness_families_and_missing_directory(
    hermes_home: Path,
):
    (hermes_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "platforms": {
                    "telegram": {"state": "connected"},
                    "whatsapp_cloud": {"state": "disconnected"},
                }
            }
        )
    )
    (hermes_home / "channel_directory.json").write_text(
        json.dumps({"platforms": {"telegram": [{"state": "connected"}]}})
    )
    (hermes_home / "channel_aliases.json").write_text(
        json.dumps(
            {
                "telegram": {
                    "123": {"label": "Ops", "stale": True},
                    "456": {"label": "Builds"},
                }
            }
        )
    )

    c = Collector(hermes_home)
    state = c.collect()

    assert state.channels.alias_count == 2
    assert state.channels.stale_alias_count == 1
    assert state.channels.missing_directory_platforms == ["whatsapp_cloud"]
    platforms = {platform.name: platform for platform in state.channels.platforms}
    assert platforms["telegram"].family_label == "Telegram"
    assert platforms["whatsapp_cloud"].family_label == "WhatsApp Cloud"
    assert platforms["whatsapp_cloud"].missing_from_directory is True
    c.close()


def test_collect_channel_aliases_preserve_last_good_on_malformed_json(hermes_home: Path):
    alias_path = hermes_home / "channel_aliases.json"
    alias_path.write_text(json.dumps({"telegram": {"123": "Ops"}}))

    c = Collector(hermes_home)
    first = c.collect()
    assert first.channels.alias_count == 1

    alias_path.write_text("{not valid json")
    second = c.collect()

    assert second.channels.alias_count == 1
    assert "channels" not in second.health.failed_sources
    c.close()


def test_collect_gateway_channel_visibility_renders_from_collected_state(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"scale_to_zero": {"idle_timeout_minutes": 10}})
    )
    (hermes_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 12345,
                "gateway_state": "running",
                "active_agents": 0,
                "served_profiles": ["root"],
                "platforms": {"raft": {"state": "connected"}},
            }
        )
    )
    (hermes_home / "channel_aliases.json").write_text(
        json.dumps({"raft": {"room-1": {"label": "Ops", "stale": True}}})
    )

    c = Collector(hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()
    text = render_to_str(render_panel(1, state, Theme(), detail=True), width=120, no_color=True)

    assert "Scale-to-zero: 10m idle relay-only" in text
    assert "Served Profiles" in text
    assert "root" in text
    assert "1 stale" in text
    c.close()


def test_gateway_pid_not_running(hermes_home: Path):
    """Gateway state says running but PID doesn't exist and no gateway.pid."""
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "gateway_state": "running",
                "platforms": {},
                "updated_at": "",
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    assert state.gateway.state == "running"
    c.close()


def test_gateway_invalid_pid_type_does_not_make_state_stale(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": "not-a-pid",
                "gateway_state": "running",
                "platforms": {},
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.is_stale is False
    assert state.gateway.running is False
    assert state.gateway.pid == 0
    c.close()


def test_gateway_ignores_non_mapping_platform_entries(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 0,
                "gateway_state": "stopped",
                "platforms": {
                    "telegram": {"state": "connected", "updated_at": "2026-04-08T17:42:57+00:00"},
                    "broken": ["not", "a", "mapping"],
                },
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.is_stale is False
    assert len(state.gateway.platforms) == 1
    assert state.gateway.platforms[0].name == "telegram"
    c.close()


def test_gateway_null_platform_fields_degrade_to_strings(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 0,
                "gateway_state": "stopped",
                "platforms": {"telegram": {"state": None, "updated_at": None}},
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.platforms[0].state == "unknown"
    assert state.gateway.platforms[0].updated_at == ""
    assert "gateway" not in state.health.failed_sources
    c.close()


def test_gateway_surfaces_platform_errors_and_agent_counts(hermes_home: Path):
    """Live gateway_state.json carries per-platform error_message/error_code and
    top-level active_agents/restart_requested; hermesd surfaces them."""
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 0,
                "gateway_state": "stopped",
                "active_agents": 3,
                "restart_requested": True,
                "platforms": {
                    "discord": {
                        "state": "disconnected",
                        "updated_at": "2026-04-08T10:00:00+00:00",
                        "error_code": "reconnect_failed",
                        "error_message": "failed to reconnect",
                    },
                    "telegram": {
                        "state": "connected",
                        "updated_at": "2026-04-08T17:42:57+00:00",
                    },
                },
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    by_name = {p.name: p for p in state.gateway.platforms}
    assert by_name["discord"].error_message == "failed to reconnect"
    assert by_name["discord"].error_code == "reconnect_failed"
    assert by_name["telegram"].error_message == ""
    assert state.gateway.active_agents == 3
    assert state.gateway.restart_requested is True
    c.close()


def test_gateway_stale_pid_with_launchd_pid(hermes_home: Path):
    """gateway_state.json has stale PID but gateway.pid has live PID."""
    import os

    my_pid = os.getpid()  # use our own PID as a "live" process
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "gateway_state": "running",
                "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
                "updated_at": "",
            }
        )
    )
    pid_file = hermes_home / "gateway.pid"
    pid_file.write_text(json.dumps({"pid": my_pid, "kind": "hermes-gateway"}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == my_pid
    c.close()


def test_gateway_running_without_recorded_pid_uses_launchd_pid(hermes_home: Path):
    """gateway_state.json says running with no PID; gateway.pid supplies the live one."""
    my_pid = os.getpid()
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"gateway_state": "running", "platforms": {}})
    )
    (hermes_home / "gateway.pid").write_text(json.dumps({"pid": my_pid}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == my_pid
    c.close()


def test_gateway_pid_file_with_plain_integer_content(hermes_home: Path):
    """gateway.pid may contain a bare integer instead of a JSON object."""
    my_pid = os.getpid()
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": 999999999, "gateway_state": "running", "platforms": {}})
    )
    (hermes_home / "gateway.pid").write_text(str(my_pid))
    c = Collector(hermes_home, pid_exists=lambda pid: pid != 999999999)
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == my_pid
    c.close()


def test_pid_exists_returns_false_for_missing_process(monkeypatch):
    def fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", fake_kill)
    assert _pid_exists(12345) is False


def test_pid_exists_returns_true_for_permission_error(monkeypatch):
    def fake_kill(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", fake_kill)
    assert _pid_exists(12345) is True


def _ledger_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "pid": 4242,
        "create_time": 1775791440.0,
        "purpose": "mcp-helper",
        "install": "/opt/hermes",
        "spawner_pid": 1,
        "spawner_create": 0.0,
        "registered_at": 1775791441.0,
        "argv": "python -m hermes.mcp_helper",
        "host": "127.0.0.1",
        "port": None,
        "profile": "coding",
    }
    entry.update(overrides)
    return entry


def test_background_processes_read_spawn_ledger(hermes_home: Path, sample_processes: Path):
    (hermes_home / "spawn-ledger.json").write_text(
        json.dumps(
            [
                _ledger_entry(),
                _ledger_entry(
                    pid="4343",
                    purpose="serve",
                    argv="hermes serve",
                    port=8080,
                    profile="",
                    create_time=None,
                ),
            ]
        )
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    processes = state.background_processes
    assert [p.pid for p in processes] == [4242, 4343]
    # processes.json is dead once the ledger exists.
    assert all(p.session_id not in {"proc_alpha", "proc_beta"} for p in processes)
    assert processes[0].purpose == "mcp-helper"
    assert processes[0].command == "python -m hermes.mcp_helper"
    assert processes[0].started_at == 1775791440.0
    assert processes[0].profile == "coding"
    assert processes[0].session_id
    assert processes[1].port == 8080
    assert processes[1].started_at == 1775791441.0


def test_background_processes_fall_back_when_ledger_is_malformed(
    hermes_home: Path, sample_processes: Path
):
    (hermes_home / "spawn-ledger.json").write_text(json.dumps(["not-a-mapping", 7]))

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert {p.session_id for p in state.background_processes} == {"proc_alpha", "proc_beta"}


def test_background_processes_fall_back_when_ledger_is_empty(
    hermes_home: Path, sample_processes: Path
):
    (hermes_home / "spawn-ledger.json").write_text("[]")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert {p.session_id for p in state.background_processes} == {"proc_alpha", "proc_beta"}


def test_background_processes_empty_without_ledger_or_processes_json(hermes_home: Path):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.background_processes == []


@pytest.mark.parametrize("pid", [-1, 0])
def test_gateway_non_positive_pid_is_treated_as_absent(hermes_home: Path, pid: int):
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": pid, "gateway_state": "running", "platforms": {}})
    )
    probed: list[int] = []

    def spy(candidate: int) -> bool:
        probed.append(candidate)
        return True

    c = Collector(hermes_home, pid_exists=spy)
    try:
        state = c.collect()
    finally:
        c.close()

    assert probed == []
    assert state.gateway.pid == 0
    assert state.gateway.running is False


@pytest.mark.parametrize("pid", [-1, 0])
def test_pid_exists_rejects_non_positive_pids(pid: int):
    assert _pid_exists(pid) is False


def test_gateway_pid_file_with_non_positive_pid_is_ignored(hermes_home: Path):
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"gateway_state": "running", "platforms": {}})
    )
    (hermes_home / "gateway.pid").write_text("-1")
    probed: list[int] = []

    def spy(candidate: int) -> bool:
        probed.append(candidate)
        return True

    c = Collector(hermes_home, pid_exists=spy)
    try:
        state = c.collect()
    finally:
        c.close()

    assert probed == []
    assert state.gateway.pid == 0


def test_is_dashboard_process_matches_phrase_and_bad_quoting():
    # "hermes dashboard" phrase short-circuits to True.
    assert _is_dashboard_process("python -m hermes dashboard") is True
    # Unbalanced quote makes shlex.split raise ValueError -> str.split fallback
    # still finds the hermesd entrypoint.
    assert _is_dashboard_process('/usr/bin/hermesd --flag "unterminated') is True
    assert _is_dashboard_process('/usr/bin/other "unterminated') is False


def test_gateway_uses_gateway_pid_file_fallback(hermes_home: Path):
    """When gateway_state.json PID is dead, fall back to gateway.pid."""
    live_pid = 4242
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "gateway_state": "running",
                "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
            }
        )
    )
    pid_file = hermes_home / "gateway.pid"
    pid_file.write_text(json.dumps({"pid": live_pid, "kind": "hermes-gateway"}))

    c = Collector(hermes_home, pid_exists=lambda pid: pid == live_pid)
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == live_pid
    c.close()


def test_gateway_both_pids_dead(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "gateway_state": "running",
                "platforms": {},
            }
        )
    )
    pid_file = hermes_home / "gateway.pid"
    pid_file.write_text(json.dumps({"pid": 999999998}))

    c = Collector(hermes_home, pid_exists=lambda pid: False)
    state = c.collect()
    assert state.gateway.running is False
    c.close()


def test_gateway_no_pid_file(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "gateway_state": "running",
                "platforms": {},
            }
        )
    )
    # No gateway.pid file

    c = Collector(hermes_home, pid_exists=lambda pid: False)
    state = c.collect()
    assert state.gateway.running is False
    c.close()


def test_gateway_pid_file_malformed(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "gateway_state": "running",
                "platforms": {},
            }
        )
    )
    pid_file = hermes_home / "gateway.pid"
    pid_file.write_text("not valid json{{{")

    c = Collector(hermes_home, pid_exists=lambda pid: False)
    state = c.collect()
    assert state.gateway.running is False
    c.close()


def test_gateway_state_stopped_does_not_check_pid(hermes_home: Path):
    def fail_if_called(pid: int) -> bool:
        raise AssertionError("stopped gateway must not check pid liveness")

    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "gateway_state": "stopped",
                "platforms": {},
            }
        )
    )

    c = Collector(hermes_home, pid_exists=fail_if_called)
    state = c.collect()
    assert state.gateway.running is False
    assert state.gateway.state == "stopped"
    c.close()


def test_gateway_live_pid_in_state(hermes_home: Path):
    """When gateway_state.json PID is alive, use it directly."""
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
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == live_pid
    c.close()


def test_gateway_shows_version(hermes_home: Path):
    live_pid = 4242
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": live_pid,
                "gateway_state": "running",
                "platforms": {},
            }
        )
    )
    agent_dir = hermes_home / "hermes-agent"
    agent_dir.mkdir()
    (agent_dir / "pyproject.toml").write_text('[project]\nversion = "0.8.0"\n')
    (hermes_home / ".update_check").write_text(json.dumps({"behind": 5}))

    c = Collector(hermes_home, pid_exists=lambda pid: pid == live_pid)
    state = c.collect()
    assert state.gateway.hermes_version == "0.8.0"
    assert state.gateway.updates_behind == 5
    c.close()


def test_gateway_version_up_to_date(hermes_home: Path):
    live_pid = 4242
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": live_pid,
                "gateway_state": "running",
                "platforms": {},
            }
        )
    )
    agent_dir = hermes_home / "hermes-agent"
    agent_dir.mkdir()
    (agent_dir / "pyproject.toml").write_text('[project]\nversion = "0.8.0"\n')

    c = Collector(hermes_home, pid_exists=lambda pid: pid == live_pid)
    state = c.collect()
    assert state.gateway.hermes_version == "0.8.0"
    assert state.gateway.updates_behind == 0
    c.close()


NOW = 1_800_000_000.0


def _iso(epoch: float, *, naive: bool = False) -> str:
    moment = datetime.fromtimestamp(epoch, tz=UTC)
    if naive:
        return moment.replace(tzinfo=None).isoformat()
    return moment.isoformat()


def _clock() -> float:
    return NOW


def _write_gateway_state(home: Path, **extra: object) -> None:
    payload: dict[str, object] = {
        "pid": 4242,
        "gateway_state": "running",
        "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
    }
    payload.update(extra)
    (home / "gateway_state.json").write_text(json.dumps(payload))


def _write_heartbeat(home: Path, **extra: object) -> Path:
    state_dir = home / "state"
    state_dir.mkdir(exist_ok=True)
    payload: dict[str, object] = {
        "pid": 4242,
        "updated_at": _iso(NOW - 30),
        "monotonic": 1234.5,
        "start_time": NOW - 5000,
        "loop_tick_socket": "/tmp/gw.sock",
        "loop_tick_tcp_port": None,
    }
    payload.update(extra)
    path = state_dir / "gateway.heartbeat"
    path.write_text(json.dumps(payload))
    return path


def _write_lifecycle(home: Path, **extra: object) -> Path:
    state_dir = home / "state"
    state_dir.mkdir(exist_ok=True)
    payload: dict[str, object] = {
        "phase": "exited",
        "pid": 4242,
        "start_time": NOW - 5000,
        "started_at": _iso(NOW - 5000),
        "exit_code": 3,
        "exit_reason": "SIGTERM received",
    }
    payload.update(extra)
    path = state_dir / "gateway.lifecycle.json"
    path.write_text(json.dumps(payload))
    return path


def _write_receipt(home: Path, payload: object) -> Path:
    receipts = home / "logs" / "update_receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    path = receipts / "latest.json"
    path.write_text(json.dumps(payload))
    return path


def _collect(home: Path, *, live_pid: int = 4242):
    collector = Collector(home, pid_exists=lambda pid: pid == live_pid, clock=_clock)
    try:
        return collector.collect()
    finally:
        collector.close()


# --------------------------------------------------------------------------
# A. heartbeat liveness
# --------------------------------------------------------------------------


def test_heartbeat_happy_path_is_ticking(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_heartbeat(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.heartbeat_age_seconds == pytest.approx(30.0)
    assert gateway.loop_health is GatewayLoopHealth.TICKING


def test_heartbeat_missing_file_is_unknown(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.heartbeat_age_seconds is None
    assert gateway.loop_health is GatewayLoopHealth.UNKNOWN


def test_heartbeat_naive_timestamp_is_treated_as_utc(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_heartbeat(hermes_home, updated_at=_iso(NOW - 45, naive=True))

    gateway = _collect(hermes_home).gateway

    assert gateway.heartbeat_age_seconds == pytest.approx(45.0)
    assert gateway.loop_health is GatewayLoopHealth.TICKING


def test_heartbeat_garbage_timestamp_falls_back_to_file_mtime(hermes_home: Path):
    _write_gateway_state(hermes_home)
    path = _write_heartbeat(hermes_home, updated_at="not-a-timestamp")
    os.utime(path, (NOW - 120, NOW - 120))

    gateway = _collect(hermes_home).gateway

    assert gateway.heartbeat_age_seconds == pytest.approx(120.0)
    assert gateway.loop_health is GatewayLoopHealth.STALE


def test_heartbeat_wrong_types_do_not_crash(hermes_home: Path):
    _write_gateway_state(hermes_home)
    path = _write_heartbeat(hermes_home, updated_at=17, pid="nope", loop_tick_tcp_port="x")
    os.utime(path, (NOW - 10, NOW - 10))

    gateway = _collect(hermes_home).gateway

    assert gateway.heartbeat_age_seconds == pytest.approx(10.0)
    assert gateway.loop_health is GatewayLoopHealth.TICKING


def test_heartbeat_future_timestamp_clamps_to_zero(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_heartbeat(hermes_home, updated_at=_iso(NOW + 5000))

    gateway = _collect(hermes_home).gateway

    assert gateway.heartbeat_age_seconds == 0.0
    assert gateway.loop_health is GatewayLoopHealth.TICKING


@pytest.mark.parametrize(
    ("age", "gateway_state", "expected"),
    [
        (90.0, "running", GatewayLoopHealth.TICKING),
        (91.0, "running", GatewayLoopHealth.STALE),
        (300.0, "running", GatewayLoopHealth.STALE),
        (301.0, "running", GatewayLoopHealth.WEDGED),
        (301.0, "stopped", GatewayLoopHealth.STALE),
    ],
)
def test_heartbeat_health_thresholds(
    hermes_home: Path,
    age: float,
    gateway_state: str,
    expected: GatewayLoopHealth,
):
    _write_gateway_state(hermes_home, gateway_state=gateway_state)
    _write_heartbeat(hermes_home, updated_at=_iso(NOW - age))

    gateway = _collect(hermes_home).gateway

    assert gateway.heartbeat_age_seconds == pytest.approx(age)
    assert gateway.loop_health is expected


def test_symlinked_heartbeat_file_is_ignored(hermes_home: Path, tmp_path: Path):
    _write_gateway_state(hermes_home)
    outside = tmp_path / "outside.heartbeat"
    outside.write_text(json.dumps({"updated_at": _iso(NOW - 5)}))
    state_dir = hermes_home / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "gateway.heartbeat").symlink_to(outside)

    gateway = _collect(hermes_home).gateway

    assert gateway.heartbeat_age_seconds is None
    assert gateway.loop_health is GatewayLoopHealth.UNKNOWN


# --------------------------------------------------------------------------
# B. lifecycle
# --------------------------------------------------------------------------


def test_lifecycle_exited_records_exit_code_and_reason(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_lifecycle(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.lifecycle_phase == "exited"
    assert gateway.last_exit_code == 3
    assert gateway.last_exit_reason == "SIGTERM received"
    assert gateway.unclean_previous_exit is False


def test_lifecycle_running_phase_with_dead_pid_is_unclean(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_lifecycle(
        hermes_home, phase="running", pid=999_999_999, exit_code=None, exit_reason=None
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.lifecycle_phase == "running"
    assert gateway.last_exit_code is None
    assert gateway.last_exit_reason == ""
    assert gateway.unclean_previous_exit is True


def test_lifecycle_running_phase_with_live_pid_is_clean(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_lifecycle(hermes_home, phase="running", pid=4242, exit_code=None)

    gateway = _collect(hermes_home).gateway

    assert gateway.unclean_previous_exit is False


def test_lifecycle_missing_file_keeps_defaults(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.lifecycle_phase == ""
    assert gateway.last_exit_code is None
    assert gateway.unclean_previous_exit is False


def test_lifecycle_wrong_types_do_not_crash(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_lifecycle(hermes_home, phase=7, pid="x", exit_code="not-int", exit_reason=[1, 2])

    gateway = _collect(hermes_home).gateway

    assert gateway.lifecycle_phase == "7"
    assert gateway.last_exit_code == 0
    assert gateway.unclean_previous_exit is False


# --------------------------------------------------------------------------
# C. gateway_state.json new keys
# --------------------------------------------------------------------------


def _config_generation(home: Path, *, mtime_ns: int) -> dict[str, object]:
    return {
        "fingerprint": "0123456789abcdef0123456789abcdef",
        "short": "0123456789ab",
        "sources": [
            {
                "name": "config.yaml",
                "path": str(home / "config.yaml"),
                "exists": True,
                "mtime_ns": mtime_ns,
                "size": 12,
            }
        ],
    }


def test_gateway_state_surfaces_code_and_session_store_keys(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        code_sha="abcdef0123456789abcdef",
        code_version="2026.9.1",
        session_store={"status": "ready"},
        exit_reason="",
        platforms={
            "telegram": {
                "state": "connected",
                "updated_at": "",
                "needs_attention": False,
                "retrying_since": None,
            },
            "discord": {
                "state": "error",
                "updated_at": "",
                "error_code": "AUTH",
                "error_message": "bad token",
                "needs_attention": True,
                "retrying_since": _iso(NOW - 600),
            },
        },
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.code_sha == "abcdef0123456789abcdef"
    assert gateway.code_version == "2026.9.1"
    assert gateway.session_store_status == "ready"
    platforms = {platform.name: platform for platform in gateway.platforms}
    assert platforms["telegram"].needs_attention is False
    assert platforms["discord"].needs_attention is True
    assert platforms["discord"].retrying_since_age_seconds == pytest.approx(600.0)
    assert platforms["telegram"].retrying_since_age_seconds is None


def test_gateway_state_exit_reason_is_surfaced(hermes_home: Path):
    _write_gateway_state(hermes_home, gateway_state="stopped", exit_reason="crashed on boot")

    gateway = _collect(hermes_home).gateway

    assert gateway.exit_reason == "crashed on boot"


def _write_config(path: Path, *, mtime: float) -> Path:
    path.write_text("model: {}\n")
    os.utime(path, (mtime, mtime))
    return path


def test_config_stale_when_config_is_newer_than_the_gateway_start(hermes_home: Path):
    _write_config(hermes_home / "config.yaml", mtime=NOW - 100)
    _write_gateway_state(hermes_home, config_generation=_config_generation(hermes_home, mtime_ns=1))
    _write_heartbeat(hermes_home, start_time=NOW - 5000)

    gateway = _collect(hermes_home).gateway

    assert gateway.config_stale is True
    # The recorded generation stays informational, never a staleness input.
    assert gateway.config_fingerprint == "0123456789abcdef0123456789abcdef"
    assert gateway.config_generation_short == "0123456789ab"
    assert [source.name for source in gateway.config_sources] == ["config.yaml"]


def test_config_not_stale_when_config_predates_the_gateway_start(hermes_home: Path):
    _write_config(hermes_home / "config.yaml", mtime=NOW - 9000)
    _write_gateway_state(hermes_home, config_generation=_config_generation(hermes_home, mtime_ns=1))
    _write_heartbeat(hermes_home, start_time=NOW - 5000)

    gateway = _collect(hermes_home).gateway

    assert gateway.config_stale is False


def test_config_stale_evaluates_a_symlinked_config(hermes_home: Path, tmp_path: Path):
    """Dotfiles setups link config.yaml at a repo copy; it is still the live file."""
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = _write_config(dotfiles / "config.yaml", mtime=NOW - 100)
    (hermes_home / "config.yaml").symlink_to(target)
    _write_gateway_state(hermes_home)
    _write_heartbeat(hermes_home, start_time=NOW - 5000)

    gateway = _collect(hermes_home).gateway

    assert gateway.config_stale is True


def test_config_stale_falls_back_to_the_lifecycle_start_time(hermes_home: Path):
    _write_config(hermes_home / "config.yaml", mtime=NOW - 100)
    _write_gateway_state(hermes_home)
    _write_lifecycle(hermes_home, start_time=NOW - 5000)

    gateway = _collect(hermes_home).gateway

    assert gateway.config_stale is True


def test_config_not_stale_without_any_known_start_time(hermes_home: Path):
    _write_config(hermes_home / "config.yaml", mtime=NOW - 100)
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.config_stale is False


def test_config_stale_ignores_a_monotonic_start_time(hermes_home: Path):
    """hermes-agent records a monotonic clock reading under the same key."""
    _write_config(hermes_home / "config.yaml", mtime=NOW - 100)
    _write_gateway_state(hermes_home, start_time=178874708938)

    gateway = _collect(hermes_home).gateway

    assert gateway.config_stale is False


def test_config_stale_ignores_sources_outside_hermes_home(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text("x: 1\n")
    _write_gateway_state(
        hermes_home,
        config_generation={
            "fingerprint": "f",
            "short": "f",
            "sources": [{"name": "outside", "path": str(outside), "mtime_ns": 1}],
        },
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.config_stale is False


def test_config_generation_wrong_types_do_not_crash(hermes_home: Path):
    _write_gateway_state(hermes_home, config_generation=["not", "a", "mapping"], session_store=7)

    gateway = _collect(hermes_home).gateway

    assert gateway.config_sources == []
    assert gateway.config_stale is False
    assert gateway.session_store_status == ""


# --------------------------------------------------------------------------
# D. update receipts
# --------------------------------------------------------------------------


def _receipt(**extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 1,
        "started_at": _iso(NOW - 900),
        "finished_at": _iso(NOW - 600),
        "argv": ["hermes", "update"],
        "pid": 999,
        "outcome": "ok",
        "pre_update": {"sha": "aaaa", "short_sha": "aaaa", "version": "2026.8.1"},
        "post_update": {"sha": "bbbb", "short_sha": "bbbb", "version": "2026.9.1"},
        "steps": [{"name": "pull", "ok": True, "detail": "", "at": _iso(NOW - 800)}],
        "plan": {"install_method": "uv", "runtimes": []},
    }
    payload.update(extra)
    return payload


def test_update_receipt_happy_path(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(hermes_home, _receipt())

    gateway = _collect(hermes_home).gateway

    assert gateway.last_update_outcome == "ok"
    assert gateway.last_update_finished_age_seconds == pytest.approx(600.0)
    assert gateway.last_update_from_version == "2026.8.1"
    assert gateway.last_update_to_version == "2026.9.1"
    assert gateway.last_update_failed_step == ""
    assert gateway.runtime_code_skew is False


def test_update_receipt_reports_first_failed_step(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_receipt(
        hermes_home,
        _receipt(
            outcome="failed",
            steps=[
                {"name": "pull", "ok": True},
                {"name": "install", "ok": False, "detail": "pip exploded"},
                {"name": "restart", "ok": False},
            ],
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.last_update_outcome == "failed"
    assert gateway.last_update_failed_step == "install"


def test_update_receipt_detects_runtime_code_skew(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(
            plan={
                "runtimes": [
                    {"kind": "gateway", "code_sha": "bbbb"},
                    {"kind": "worker", "code_sha": "cccc"},
                ]
            }
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is True


def test_update_receipt_skew_ignores_blank_shas(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="")
    _write_receipt(hermes_home, _receipt(plan={"runtimes": [{"code_sha": "cccc"}]}))

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is False


def test_update_receipt_tolerates_missing_and_wrong_typed_keys(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_receipt(hermes_home, {"outcome": 5, "steps": "nope", "plan": 12, "finished_at": "junk"})

    gateway = _collect(hermes_home).gateway

    assert gateway.last_update_outcome == "5"
    assert gateway.last_update_finished_age_seconds is None
    assert gateway.last_update_failed_step == ""
    assert gateway.runtime_code_skew is False


def test_update_receipt_missing_file_keeps_defaults(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.last_update_outcome == ""
    assert gateway.last_update_finished_age_seconds is None


def test_update_receipt_future_finish_clamps_to_zero(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_receipt(hermes_home, _receipt(finished_at=_iso(NOW + 3600)))

    gateway = _collect(hermes_home).gateway

    assert gateway.last_update_finished_age_seconds == 0.0


# --------------------------------------------------------------------------
# E/F. state.db ledgers
# --------------------------------------------------------------------------


def _create_ledger_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE gateway_heartbeats (
            backend_id TEXT PRIMARY KEY,
            pid INTEGER,
            started_at REAL,
            last_heartbeat REAL,
            profile TEXT,
            host TEXT
        );
        CREATE TABLE delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT,
            platform TEXT,
            chat_id TEXT,
            thread_id TEXT,
            content TEXT,
            state TEXT,
            attempts INTEGER,
            created_at REAL,
            updated_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            last_error TEXT,
            adapter_profile TEXT
        );
        """
    )


def _write_ledgers(home: Path) -> None:
    conn = sqlite3.connect(str(home / "state.db"))
    _create_ledger_tables(conn)
    conn.executemany(
        "INSERT INTO gateway_heartbeats VALUES (?,?,?,?,?,?)",
        [
            ("b1", 1, NOW - 3600, NOW - 10, "root", "host"),
            ("b2", 2, NOW - 90_000, NOW - 80_000, "root", "host"),
            ("b3", 3, None, None, None, None),
        ],
    )
    conn.executemany(
        "INSERT INTO delivery_obligations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "o1",
                "telegram:1",
                "telegram",
                "1",
                None,
                "SECRET-CONTENT-DO-NOT-SHOW",
                "pending",
                2,
                NOW - 400,
                NOW - 300,
                None,
                None,
                "network unreachable " + "x" * 200,
                "root",
            ),
            (
                "o2",
                "discord:2",
                "discord",
                "2",
                None,
                "SECRET-CONTENT-DO-NOT-SHOW",
                "failed",
                5,
                NOW - 900,
                NOW - 800,
                None,
                None,
                None,
                "root",
            ),
            (
                "o3",
                "discord:3",
                None,
                "3",
                None,
                "SECRET-CONTENT-DO-NOT-SHOW",
                "attempting",
                1,
                NOW - 100,
                None,
                None,
                None,
                None,
                None,
            ),
            (
                "o4",
                "discord:4",
                "discord",
                "4",
                None,
                "SECRET-CONTENT-DO-NOT-SHOW",
                "delivered",
                1,
                NOW - 50,
                NOW - 40,
                None,
                None,
                None,
                "root",
            ),
        ],
    )
    conn.commit()
    conn.close()


def test_gateway_ledgers_from_state_db(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_ledgers(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_incarnation_count == 3
    assert gateway.gateway_restarts_24h == 1
    assert gateway.current_incarnation_uptime_seconds == pytest.approx(3600.0)


def test_delivery_obligation_counts_and_excerpts(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_ledgers(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.pending_delivery_count == 2
    assert gateway.failed_delivery_count == 1
    assert len(gateway.pending_deliveries) == 3
    by_platform = {entry.platform: entry for entry in gateway.pending_deliveries}
    assert by_platform["telegram"].state == "pending"
    assert by_platform["telegram"].attempts == 2
    assert by_platform["telegram"].age_seconds == pytest.approx(300.0)
    assert len(by_platform["telegram"].last_error) <= 80
    assert all(
        "SECRET-CONTENT" not in entry.model_dump_json() for entry in gateway.pending_deliveries
    )


def test_open_delivery_list_is_capped_while_counts_stay_exact(hermes_home: Path):
    """Seven open obligations: only the newest five are listed, counts cover all seven."""
    _write_gateway_state(hermes_home)
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    _create_ledger_tables(conn)
    states = ["pending"] * 4 + ["attempting"] * 2 + ["failed"]
    conn.executemany(
        "INSERT INTO delivery_obligations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                f"o{index:02d}",
                f"telegram:{index}",
                f"platform-{index:02d}",
                str(index),
                None,
                "SECRET-CONTENT-DO-NOT-SHOW",
                state,
                index,
                NOW - 10_000 + index,
                NOW - 1_000 + index,
                None,
                None,
                None,
                "root",
            )
            for index, state in enumerate(states)
        ],
    )
    # A delivered row must never enter the open list or the pending counters.
    conn.execute(
        "INSERT INTO delivery_obligations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "o_done",
            "telegram:99",
            "platform-99",
            "99",
            None,
            "SECRET-CONTENT-DO-NOT-SHOW",
            "delivered",
            1,
            NOW - 5,
            NOW - 1,
            None,
            None,
            None,
            "root",
        ),
    )
    conn.commit()
    conn.close()

    gateway = _collect(hermes_home).gateway

    assert gateway.pending_delivery_count == 6
    assert gateway.failed_delivery_count == 1
    assert len(gateway.pending_deliveries) == _OPEN_DELIVERY_LIMIT
    # Newest updated_at first; the two oldest open rows are dropped by the cap.
    assert [entry.platform for entry in gateway.pending_deliveries] == [
        f"platform-{index:02d}" for index in range(6, 6 - _OPEN_DELIVERY_LIMIT, -1)
    ]
    assert all(
        "SECRET-CONTENT" not in entry.model_dump_json() for entry in gateway.pending_deliveries
    )


def test_incarnation_counts_stay_correct_beyond_the_scan_limit(hermes_home: Path):
    """600 heartbeat rows: the total is exact and the 24h restarts survive the scan cap."""
    _write_gateway_state(hermes_home)
    total_rows = 600
    recent_rows = 300
    assert recent_rows < _INCARNATION_SCAN_LIMIT < total_rows
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    _create_ledger_tables(conn)
    conn.executemany(
        "INSERT INTO gateway_heartbeats VALUES (?,?,?,?,?,?)",
        [
            (
                f"b{index:04d}",
                index + 1,
                # The newest `recent_rows` started inside 24h; the rest are far older.
                NOW - 60 - index if index < recent_rows else NOW - 400_000 - index,
                NOW - 10,
                "root",
                "host",
            )
            for index in range(total_rows)
        ],
    )
    conn.commit()
    conn.close()

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_incarnation_count == total_rows
    assert gateway.gateway_restarts_24h == recent_rows
    assert gateway.current_incarnation_uptime_seconds == pytest.approx(60.0)


def test_gateway_ledger_tables_absent_are_zero(hermes_home: Path):
    _write_gateway_state(hermes_home)
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    create_state_db_tables(conn)
    conn.commit()
    conn.close()

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_incarnation_count == 0
    assert gateway.gateway_restarts_24h == 0
    assert gateway.current_incarnation_uptime_seconds is None
    assert gateway.pending_delivery_count == 0
    assert gateway.pending_deliveries == []


def test_gateway_ledgers_without_state_db_are_zero(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_incarnation_count == 0
    assert gateway.pending_delivery_count == 0


def test_gateway_ledger_uptime_clamps_future_start(hermes_home: Path):
    _write_gateway_state(hermes_home)
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    _create_ledger_tables(conn)
    conn.execute(
        "INSERT INTO gateway_heartbeats VALUES (?,?,?,?,?,?)",
        ("b1", 1, NOW + 5000, NOW, "root", "host"),
    )
    conn.commit()
    conn.close()

    gateway = _collect(hermes_home).gateway

    assert gateway.current_incarnation_uptime_seconds == 0.0


def test_goal_state_still_collected_alongside_gateway_ledgers(hermes_home: Path):
    _write_gateway_state(hermes_home)
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    _create_ledger_tables(conn)
    conn.executescript(
        "CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        'INSERT INTO state_meta VALUES (\'goal:s1\', \'{"goal":"ship","status":"active"}\');'
    )
    conn.commit()
    conn.close()

    state = _collect(hermes_home)

    assert state.operations.goal_count == 1
    assert state.gateway.gateway_incarnation_count == 0


def test_state_db_is_read_once_per_unchanged_collect(hermes_home: Path, monkeypatch):
    _write_gateway_state(hermes_home)
    _write_ledgers(hermes_home)

    connects: list[str] = []
    original = sqlite3.connect

    def counting_connect(target, *args, **kwargs):
        if "state.db" in str(target):
            connects.append(str(target))
        return original(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    collector = Collector(hermes_home, pid_exists=lambda pid: True, clock=_clock)
    try:
        collector.collect()
        first = len(connects)
        collector.collect()
    finally:
        collector.close()

    assert first == 1
    assert len(connects) == 1


def test_ledger_ages_use_wall_clock_after_cached_read(hermes_home: Path):
    """A cached state.db readout must still age its rows against the live clock."""
    _write_gateway_state(hermes_home)
    _write_ledgers(hermes_home)
    now = [NOW]

    collector = Collector(hermes_home, pid_exists=lambda pid: True, clock=lambda: now[0])
    try:
        first = collector.collect()
        now[0] = NOW + 60
        second = collector.collect()
    finally:
        collector.close()

    assert first.gateway.current_incarnation_uptime_seconds == pytest.approx(3600.0)
    assert second.gateway.current_incarnation_uptime_seconds == pytest.approx(3660.0)


def test_collect_gateway_liveness_with_real_clock(hermes_home: Path):
    """Default (non-injected) clock path stays sane."""
    _write_gateway_state(hermes_home)
    _write_heartbeat(hermes_home, updated_at=_iso(time.time()))

    collector = Collector(hermes_home, pid_exists=lambda pid: True)
    try:
        gateway = collector.collect().gateway
    finally:
        collector.close()

    assert gateway.loop_health is GatewayLoopHealth.TICKING


def test_heartbeat_z_suffix_timestamp_is_parsed(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_heartbeat(hermes_home, updated_at="2027-01-15T09:00:00Z")
    stamp = datetime(2027, 1, 15, 9, 0, tzinfo=UTC).timestamp()
    collector = Collector(hermes_home, pid_exists=lambda pid: True, clock=lambda: stamp + 45)
    try:
        gateway = collector.collect().gateway
    finally:
        collector.close()

    assert gateway.heartbeat_age_seconds == pytest.approx(45.0)


def test_config_generation_skips_non_mapping_source_entries(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        config_generation={"fingerprint": "f", "short": "f", "sources": ["nope", {}]},
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.config_sources == []
    assert gateway.config_stale is False


def test_config_stale_ignores_unstattable_source(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        config_generation={
            "fingerprint": "f",
            "short": "f",
            "sources": [{"name": "gone", "path": str(hermes_home / "missing.yaml"), "mtime_ns": 5}],
        },
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.config_stale is False
    assert [source.name for source in gateway.config_sources] == ["gone"]


# --------------------------------------------------------------------------
# H. pid liveness bounds
# --------------------------------------------------------------------------


@pytest.mark.parametrize("pid", [0, -1, -4242, 2**31, 2**40, 2**63])
def test_pid_exists_rejects_out_of_range_pids(pid: int):
    """os.kill raises OverflowError above pid_t; that must not fail a source."""
    assert _pid_exists(pid) is False


def test_pid_exists_survives_os_kill_overflow(monkeypatch):
    def exploding_kill(pid: int, signal_number: int) -> None:
        raise OverflowError("Python int too large to convert to C int")

    monkeypatch.setattr(os, "kill", exploding_kill)

    assert _pid_exists(4242) is False


def test_huge_pids_across_runtime_files_keep_their_sources_healthy(hermes_home: Path):
    """A 2**40 pid in any liveness file used to fail the whole source."""
    huge = 2**40
    _write_gateway_state(hermes_home, pid=huge)
    _write_lifecycle(hermes_home, phase="running", pid=huge)
    runtime = hermes_home / "runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "active_sessions.json").write_text(
        json.dumps({"entries": [{"session_id": "s1", "surface": "cli", "pid": huge}]})
    )
    (hermes_home / "spawn-ledger.json").write_text(
        json.dumps([{"pid": huge, "command": "hermes serve", "purpose": "gateway"}])
    )

    collector = Collector(hermes_home, clock=_clock)
    try:
        state = collector.collect()
    finally:
        collector.close()

    for source in ("gateway", "gateway_lifecycle", "active_sessions", "background_processes"):
        assert source not in state.health.failed_sources
    assert state.gateway.running is False
    assert state.active_surfaces[0].alive is False
    assert state.background_processes[0].alive is False


def test_symlinked_lifecycle_outside_home_reads_as_absent(hermes_home: Path, tmp_path: Path):
    """A lifecycle file pointing outside ~/.hermes must contribute nothing."""
    outside = tmp_path / "gateway.lifecycle.json"
    outside.write_text(
        json.dumps(
            {
                "phase": "exited",
                "pid": 4242,
                "exit_code": 3,
                "exit_reason": "OUTSIDE-THE-HOME",
                "start_time": NOW - 5000,
            }
        )
    )
    state_dir = hermes_home / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "gateway.lifecycle.json").symlink_to(outside)
    _write_gateway_state(hermes_home)

    state = _collect(hermes_home)

    assert state.gateway.lifecycle_phase == ""
    assert state.gateway.last_exit_reason == ""
    assert state.gateway.last_exit_code is None
    assert "gateway_lifecycle" not in state.health.failed_sources


def test_symlinked_update_receipt_outside_home_reads_as_absent(hermes_home: Path, tmp_path: Path):
    """An update receipt pointing outside ~/.hermes must contribute nothing."""
    outside = tmp_path / "latest.json"
    outside.write_text(
        json.dumps(
            {
                "schema": 1,
                "outcome": "OUTSIDE-THE-HOME",
                "finished_at": _iso(NOW - 600),
                "pre_update": {"version": "1.0.0"},
                "post_update": {"version": "9.9.9"},
            }
        )
    )
    receipts = hermes_home / "logs" / "update_receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "latest.json").symlink_to(outside)
    _write_gateway_state(hermes_home)

    state = _collect(hermes_home)

    assert state.gateway.last_update_outcome == ""
    assert state.gateway.last_update_to_version == ""
    assert state.gateway.last_update_finished_age_seconds is None
    assert "update_receipt" not in state.health.failed_sources


def test_symlinked_active_sessions_outside_home_reads_as_absent(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "active_sessions.json"
    outside.write_text(json.dumps({"entries": [{"session_id": "s1", "pid": 1}]}))
    runtime = hermes_home / "runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "active_sessions.json").symlink_to(outside)
    _write_gateway_state(hermes_home)

    state = _collect(hermes_home)

    assert state.active_surfaces == []
    assert "active_sessions" not in state.health.failed_sources
