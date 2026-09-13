"""Collection of gateway state, PID and launchd detection, channels,
background processes, and the spawn ledger."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from hermesd.collect.gateway import (
    _INCARNATION_SCAN_LIMIT,
    _OPEN_DELIVERY_LIMIT,
    _listener_mirror_urls,
)
from hermesd.collector import (
    Collector,
    _is_dashboard_process,
    _pid_exists,
)
from hermesd.models import DashboardState, GatewayLoopHealth, PlatformOwnership, PlatformStatus
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
        "loop_tick_socket": True,
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


def _gateway_detail(state: DashboardState) -> str:
    return render_to_str(render_panel(1, state, Theme(), detail=True), width=200, no_color=True)


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
        # Armed witness but no node to probe (the default probe finds none in a
        # bare fixture home): upstream's classify calls that ambiguity, and a
        # WEDGED verdict now requires sustained witness silence instead.
        (301.0, "running", GatewayLoopHealth.UNKNOWN),
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


def test_symlinked_gateway_state_outside_home_is_not_followed(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "gateway_state.json"
    outside.write_text(json.dumps({"pid": 4242, "gateway_state": "running", "platforms": {}}))
    (hermes_home / "gateway_state.json").symlink_to(outside)

    gateway = _collect(hermes_home).gateway

    assert gateway.state == "unknown"
    assert gateway.running is False
    assert gateway.pid == 0


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
        "outcome": "success",
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

    assert gateway.last_update_outcome == "success"
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
            fleet=[
                {"profile": "default", "code_sha": "bbbb", "state": "current"},
                {"profile": "coding", "code_sha": "cccc", "state": "stale"},
            ]
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is True


# F18: plan.runtimes[].code_sha is captured *before* the pull, so a finished
# update's plan always looks stale. The post-restart fleet matrix is the
# authoritative evidence; the plan only explains a run that never finished.


def test_update_receipt_finished_with_current_fleet_ignores_stale_plan(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(
            outcome="success",
            exit_code=0,
            plan={"runtimes": [{"kind": "gateway", "code_sha": "aaaa"}]},
            fleet=[{"profile": "default", "pid": 7, "code_sha": "bbbb", "state": "current"}],
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is False
    assert gateway.runtime_code_skew_source == "fleet"
    assert gateway.update_receipt_unfinished is False


def test_update_receipt_fleet_mismatch_wins_over_matching_plan(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(
            outcome="success",
            exit_code=0,
            plan={"runtimes": [{"kind": "gateway", "code_sha": "bbbb"}]},
            fleet=[{"profile": "default", "pid": 7, "code_sha": "cccc", "state": "stale"}],
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is True
    assert gateway.runtime_code_skew_source == "fleet"


def test_update_receipt_fleet_explicit_stale_state_without_sha_is_skew(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(fleet=[{"profile": "default", "pid": 7, "code_sha": None, "state": "stale"}]),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is True


def test_update_receipt_finished_without_fleet_does_not_use_pre_update_plan(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(
            outcome="success",
            exit_code=0,
            plan={"runtimes": [{"kind": "gateway", "code_sha": "cccc"}]},
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is False
    assert gateway.runtime_code_skew_source == ""
    assert gateway.update_receipt_unfinished is False


@pytest.mark.parametrize(
    "extra",
    [
        {"outcome": "failed"},
        {"outcome": "partial"},
        {"outcome": "running"},
        {"exit_code": 1},
        {"gateway_restart": {"incomplete": True}},
        {"outcome": "refused", "stop_reason": "update_contract refusal"},
    ],
)
def test_update_receipt_unfinished_falls_back_to_plan_evidence(
    hermes_home: Path, extra: dict[str, object]
):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(plan={"runtimes": [{"kind": "gateway", "code_sha": "cccc"}]}, **extra),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.update_receipt_unfinished is True
    assert gateway.runtime_code_skew is True
    assert gateway.runtime_code_skew_source == "plan"


def test_update_receipt_success_with_stop_reason_is_not_unfinished(hermes_home: Path):
    """Upstream stamps stop_reason on clean receipts too; it must not look unfinished."""
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(
            outcome="success",
            exit_code=0,
            stop_reason="completed at command boundary",
            plan={"runtimes": [{"kind": "gateway", "code_sha": "cccc"}]},
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.update_receipt_unfinished is False
    assert gateway.runtime_code_skew is False


def test_update_receipt_skew_unassessable_without_gateway_sha(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="")
    _write_receipt(
        hermes_home,
        _receipt(fleet=[{"profile": "default", "pid": 7, "code_sha": "cccc", "state": "stale"}]),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is False
    assert gateway.runtime_code_skew_source == ""


def test_update_receipt_records_fleet_state_counts(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(
            fleet=[
                {"profile": "default", "code_sha": "bbbb", "state": "current"},
                {"profile": "coding", "code_sha": "aaaa", "state": "stale"},
                {"profile": "ops", "code_sha": None, "state": "unknown"},
                {"profile": "web", "code_sha": None, "state": "down"},
            ]
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.update_fleet_runtime_count == 4
    assert gateway.update_fleet_states == {"current": 1, "stale": 1, "unknown": 1, "down": 1}


def test_update_receipt_fleet_row_beyond_state_kinds_is_still_scanned(hermes_home: Path):
    """Bounding the state vocabulary must not bound the skew scan."""
    _write_gateway_state(hermes_home, code_sha="bbbb")
    fleet: list[dict[str, object]] = [
        {"profile": f"p{i}", "code_sha": "bbbb", "state": f"state{i}"} for i in range(200)
    ]
    fleet[-1] = {"profile": "p199", "code_sha": "aaaa", "state": "stale"}
    _write_receipt(hermes_home, _receipt(fleet=fleet))

    gateway = _collect(hermes_home).gateway

    assert gateway.update_fleet_runtime_count == 200
    assert gateway.runtime_code_skew is True
    assert len(gateway.update_fleet_states) <= 8


def test_update_receipt_ignores_wrong_typed_fleet(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(outcome="failed", fleet="nope", plan={"runtimes": [{"code_sha": "cccc"}]}),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.update_fleet_runtime_count == 0
    assert gateway.update_receipt_unfinished is True
    assert gateway.runtime_code_skew_source == "plan"
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


# --------------------------------------------------------------------------
# K. F05 — per-platform writer ownership
#
# gateway_state.json re-stamps top-level pid/start_time on every write, so those
# identify the *most recent* writer. Each platform entry carries the identity of
# the process that wrote it, and ownership is exact (pid, start_time) equality:
# a preserved entry outlived the gateway life that recorded it.
# --------------------------------------------------------------------------


def _platform_entry(**extra: object) -> dict[str, object]:
    entry: dict[str, object] = {"state": "connected", "updated_at": ""}
    entry.update(extra)
    return entry


def _platforms_by_name(state) -> dict[str, object]:
    return {platform.name: platform for platform in state.gateway.platforms}


def test_platform_written_by_the_current_gateway_is_current_owner(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        pid=4242,
        start_time=111,
        platforms={
            "telegram": _platform_entry(writer_pid=4242, writer_start_time=111),
        },
    )

    platform = _platforms_by_name(_collect(hermes_home))["telegram"]

    assert platform.ownership is PlatformOwnership.CURRENT
    assert platform.writer_pid == 4242
    assert platform.writer_start_time == 111


def test_platform_surviving_a_gateway_restart_is_preserved(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        pid=4242,
        start_time=222,
        platforms={
            "telegram": _platform_entry(writer_pid=4242, writer_start_time=111),
        },
    )

    platform = _platforms_by_name(_collect(hermes_home))["telegram"]

    assert platform.ownership is PlatformOwnership.PRESERVED


def test_reused_pid_with_a_different_start_time_is_preserved_not_current(hermes_home: Path):
    """PID equality alone is not identity: the start-time stamp is what separates them."""
    _write_gateway_state(
        hermes_home,
        pid=4242,
        start_time=999,
        platforms={
            "telegram": _platform_entry(writer_pid=4242, writer_start_time=111),
        },
    )

    assert (
        _platforms_by_name(_collect(hermes_home))["telegram"].ownership
        is PlatformOwnership.PRESERVED
    )


def test_platform_from_a_different_process_is_preserved(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        pid=4242,
        start_time=111,
        platforms={
            "telegram": _platform_entry(writer_pid=999, writer_start_time=111),
        },
    )

    assert (
        _platforms_by_name(_collect(hermes_home))["telegram"].ownership
        is PlatformOwnership.PRESERVED
    )


def test_platform_without_writer_provenance_is_unverifiable(hermes_home: Path):
    """A gateway predating writer stamps records no provenance; that is not ownership."""
    _write_gateway_state(
        hermes_home,
        pid=4242,
        start_time=111,
        platforms={"telegram": _platform_entry()},
    )

    platform = _platforms_by_name(_collect(hermes_home))["telegram"]

    assert platform.ownership is PlatformOwnership.UNVERIFIABLE
    assert platform.writer_pid is None
    assert platform.writer_start_time is None


@pytest.mark.parametrize(
    "entry",
    [
        _platform_entry(writer_pid=4242),
        _platform_entry(writer_start_time=111),
        _platform_entry(writer_pid=4242, writer_start_time=None),
        _platform_entry(writer_pid=None, writer_start_time=111),
    ],
)
def test_partial_writer_provenance_is_unverifiable(hermes_home: Path, entry: dict[str, object]):
    _write_gateway_state(hermes_home, pid=4242, start_time=111, platforms={"telegram": entry})

    assert (
        _platforms_by_name(_collect(hermes_home))["telegram"].ownership
        is PlatformOwnership.UNVERIFIABLE
    )


@pytest.mark.parametrize(
    "top_level",
    [{"pid": 4242}, {"pid": None, "start_time": 111}, {"pid": None}, {"start_time": None}],
)
def test_missing_record_identity_leaves_platforms_unverifiable(
    hermes_home: Path, top_level: dict[str, object]
):
    _write_gateway_state(
        hermes_home,
        **top_level,
        platforms={"telegram": _platform_entry(writer_pid=4242, writer_start_time=111)},
    )

    assert (
        _platforms_by_name(_collect(hermes_home))["telegram"].ownership
        is PlatformOwnership.UNVERIFIABLE
    )


def test_ownership_is_evaluated_separately_from_heartbeat_freshness(hermes_home: Path):
    """A ticking loop says nothing about whether a platform record is still owned."""
    _write_gateway_state(
        hermes_home,
        pid=4242,
        start_time=222,
        platforms={
            "telegram": _platform_entry(writer_pid=4242, writer_start_time=111),
            "discord": _platform_entry(writer_pid=4242, writer_start_time=222),
        },
    )
    _write_heartbeat(hermes_home, pid=4242)

    state = _collect(hermes_home)
    platforms = _platforms_by_name(state)

    assert state.gateway.loop_health is GatewayLoopHealth.TICKING
    assert platforms["telegram"].ownership is PlatformOwnership.PRESERVED
    assert platforms["discord"].ownership is PlatformOwnership.CURRENT


def test_mixed_owner_platforms_are_reported_per_entry(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        pid=4242,
        start_time=222,
        platforms={
            "telegram": _platform_entry(writer_pid=4242, writer_start_time=222),
            "discord": _platform_entry(writer_pid=4242, writer_start_time=111),
            "slack": _platform_entry(),
        },
    )

    platforms = _platforms_by_name(_collect(hermes_home))

    assert platforms["telegram"].ownership is PlatformOwnership.CURRENT
    assert platforms["discord"].ownership is PlatformOwnership.PRESERVED
    assert platforms["slack"].ownership is PlatformOwnership.UNVERIFIABLE


def test_platform_ownership_defaults_to_unverifiable():
    """A PlatformStatus built without provenance must not claim ownership."""
    assert PlatformStatus(name="telegram").ownership is PlatformOwnership.UNVERIFIABLE


# --------------------------------------------------------------------------
# F10 — shared-listener routing: <profile>:<platform> keys, the served-profile
# tri-state, and the recorded ingress URL
# --------------------------------------------------------------------------


def _collect_dead(home: Path):
    """Collect with no live gateway pid: recorded data is preserved, not live."""
    return _collect(home, live_pid=0)


def test_namespaced_platform_key_is_split_into_profile_and_platform(hermes_home: Path):
    """``run_adapters.py:1048`` keys a served profile's adapter ``<profile>:<platform>``."""
    _write_gateway_state(hermes_home, platforms={"dev:telegram": _platform_entry()})

    platform = _collect(hermes_home).gateway.platforms[0]

    assert platform.profile == "dev"
    assert platform.name == "telegram"


def test_plain_platform_key_has_no_profile(hermes_home: Path):
    _write_gateway_state(hermes_home, platforms={"telegram": _platform_entry()})

    platform = _collect(hermes_home).gateway.platforms[0]

    assert platform.profile == ""
    assert platform.name == "telegram"


@pytest.mark.parametrize(
    "key",
    [
        "DEV:telegram",  # uppercase profile
        "dev:Telegram",  # uppercase platform
        "dev:telegram:extra",  # two separators
        ":telegram",  # empty profile
        "dev:",  # empty platform
        "_dev:telegram",  # leading underscore
        "dev:_telegram",  # leading underscore
        "dev telegram:web",  # space in the profile segment
        f"{'d' * 65}:telegram",  # profile past the 64-char bound
        f"dev:{'t' * 65}",  # platform past the 64-char bound
    ],
)
def test_a_key_that_fails_the_grammar_stays_opaque(hermes_home: Path, key: str):
    """Upstream validates the key grammar unconditionally (``web_routers/status.py:122-136``).

    A namespaced key that fails it must never be split: splitting would project an
    arbitrary key from a process-local JSON file onto a profile name.
    """
    _write_gateway_state(hermes_home, platforms={key: _platform_entry()})

    platform = _collect(hermes_home).gateway.platforms[0]

    assert platform.profile == ""
    assert platform.name == key


def test_valid_grammar_boundaries_split(hermes_home: Path):
    """One char, 64 chars, digits, hyphens and underscores are all inside the grammar."""
    expected = {
        ("a", "b"),
        ("0-dev_1", "foo-bar_2"),
        ("d" * 64, "t" * 64),
    }
    _write_gateway_state(
        hermes_home,
        platforms={
            "a:b": _platform_entry(),
            "0-dev_1:foo-bar_2": _platform_entry(),
            f"{'d' * 64}:{'t' * 64}": _platform_entry(),
        },
    )

    collected = {(p.profile, p.name) for p in _collect(hermes_home).gateway.platforms}

    assert collected == expected


def test_served_profiles_absent_is_not_an_empty_record(hermes_home: Path):
    """Absent and explicitly empty must stay distinguishable (multiplex_served.py:28-38)."""
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.served_profiles == []
    assert gateway.served_profiles_recorded is False


def test_an_explicitly_empty_served_profiles_list_is_an_authoritative_record(hermes_home: Path):
    """An empty list from a live gateway means "serves nobody else", not "no record"."""
    _write_gateway_state(hermes_home, served_profiles=[])

    gateway = _collect(hermes_home).gateway

    assert gateway.served_profiles == []
    assert gateway.served_profiles_recorded is True


@pytest.mark.parametrize("value", ["coding", {"profile": "coding"}, 3, None, True])
def test_a_non_list_served_profiles_value_is_not_a_record(hermes_home: Path, value: object):
    _write_gateway_state(hermes_home, served_profiles=value)

    gateway = _collect(hermes_home).gateway

    assert gateway.served_profiles == []
    assert gateway.served_profiles_recorded is False


def test_served_profiles_of_a_live_gateway_are_recorded(hermes_home: Path):
    _write_gateway_state(hermes_home, served_profiles=["default", "coding"])

    gateway = _collect(hermes_home).gateway

    assert gateway.served_profiles == ["default", "coding"]
    assert gateway.served_profiles_recorded is True


def test_served_profiles_of_a_dead_gateway_are_preserved_but_not_a_live_record(
    hermes_home: Path,
):
    """Upstream gates the record on ``live_default_gateway_pid()``: a dead pid forces None.

    hermesd keeps the recorded names — they are still what the file says — but marks
    the record as not live, so a preserved record is never presented as current.
    """
    _write_gateway_state(hermes_home, served_profiles=["default", "coding"])

    gateway = _collect_dead(hermes_home).gateway

    assert gateway.running is False
    assert gateway.served_profiles == ["default", "coding"]
    assert gateway.served_profiles_recorded is False


INGRESS_URL = "https://gw.example/p/dev/telegram/webhook"


def test_ingress_url_is_recorded_for_a_served_profile_platform(hermes_home: Path):
    _write_gateway_state(
        hermes_home, platforms={"dev:telegram": _platform_entry(ingress_url=INGRESS_URL)}
    )

    platform = _collect(hermes_home).gateway.platforms[0]

    assert platform.ingress_url == INGRESS_URL
    assert platform.ingress_url_is_path_only is False


def test_a_bare_ingress_path_is_recorded_verbatim(hermes_home: Path):
    """``publish_shared_ingress`` records a bare path when no default listener is live."""
    _write_gateway_state(
        hermes_home,
        platforms={"dev:telegram": _platform_entry(ingress_url="/p/dev/telegram/webhook")},
    )

    platform = _collect(hermes_home).gateway.platforms[0]

    assert platform.ingress_url == "/p/dev/telegram/webhook"
    assert platform.ingress_url_is_path_only is True


@pytest.mark.parametrize("state", ["fatal", "disconnected", "stopped"])
def test_ingress_url_is_suppressed_for_an_adapter_state_upstream_skips(
    hermes_home: Path, state: str
):
    """``served_profile_ingress_urls`` skips fatal/disconnected/stopped entries."""
    _write_gateway_state(
        hermes_home,
        platforms={"dev:telegram": _platform_entry(state=state, ingress_url=INGRESS_URL)},
    )

    platform = _collect(hermes_home).gateway.platforms[0]

    assert platform.state == state
    assert platform.ingress_url == ""


def test_ingress_url_is_suppressed_when_the_gateway_is_not_live(hermes_home: Path):
    """No live default gateway pid means no ingress, upstream (multiplex_served.py:46-70)."""
    _write_gateway_state(
        hermes_home, platforms={"dev:telegram": _platform_entry(ingress_url=INGRESS_URL)}
    )

    platform = _collect_dead(hermes_home).gateway.platforms[0]

    assert platform.ingress_url == ""


@pytest.mark.parametrize("value", ["", None, 0, False])
def test_a_falsy_ingress_url_is_not_recorded(hermes_home: Path, value: object):
    _write_gateway_state(
        hermes_home, platforms={"dev:telegram": _platform_entry(ingress_url=value)}
    )

    assert _collect(hermes_home).gateway.platforms[0].ingress_url == ""


def test_an_ingress_url_on_a_plain_platform_key_is_still_recorded(hermes_home: Path):
    """``web_routers/messaging.py:204,263`` reads ingress_url for plain keys too."""
    _write_gateway_state(
        hermes_home, platforms={"telegram": _platform_entry(ingress_url=INGRESS_URL)}
    )

    platform = _collect(hermes_home).gateway.platforms[0]

    assert platform.profile == ""
    assert platform.ingress_url == INGRESS_URL


def test_a_credential_bearing_ingress_url_never_reaches_dashboard_state(hermes_home: Path):
    """Redaction happens at the data boundary, so the raw URL is never in the model."""
    secret_url = "https://bot:hunter2@gw.example/p/dev/telegram/webhook?token=s3cr3t&chat=42"
    _write_gateway_state(
        hermes_home, platforms={"dev:telegram": _platform_entry(ingress_url=secret_url)}
    )

    state = _collect(hermes_home)
    dumped = json.dumps(state.model_dump(mode="json"))

    assert "hunter2" not in dumped
    assert "s3cr3t" not in dumped
    assert secret_url not in dumped
    platform = state.gateway.platforms[0]
    assert "[REDACTED]@gw.example" in platform.ingress_url
    assert "token=[REDACTED]" in platform.ingress_url
    assert "chat=42" in platform.ingress_url


def test_a_malformed_ingress_url_fails_closed(hermes_home: Path):
    """A URL urlsplit cannot parse must not pass its credentials through."""
    _write_gateway_state(
        hermes_home,
        platforms={
            "dev:telegram": _platform_entry(ingress_url="https://user:pw@[::1/p/dev?api_key=abc")
        },
    )

    state = _collect(hermes_home)
    dumped = json.dumps(state.model_dump(mode="json"))

    assert "user:pw" not in dumped
    assert "api_key=abc" not in dumped
    assert state.gateway.platforms[0].ingress_url == (
        "https://[REDACTED]@[::1/p/dev?api_key=[REDACTED]"
    )


# --------------------------------------------------------------------------
# J. loop-tick witness probe (item: authoritative loop health)
# --------------------------------------------------------------------------


def _write_heartbeat_v2(home: Path, payload: dict[str, object]) -> Path:
    """Write an exact heartbeat payload (the legacy helper always arms a witness)."""
    state_dir = home / "state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / "gateway.heartbeat"
    path.write_text(json.dumps(payload))
    return path


def _collect_probed(home: Path, probe: object, *, live_pid: int = 4242) -> object:
    collector = Collector(
        home,
        pid_exists=lambda pid: pid == live_pid,
        clock=_clock,
        loop_tick_probe=probe,  # type: ignore[arg-type]
    )
    try:
        return collector.collect()
    finally:
        collector.close()


def _armed_heartbeat(age: float, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "pid": 4242,
        "updated_at": _iso(NOW - age),
        "monotonic": 1234.5,
        "start_time": NOW - 5000,
        "loop_tick_socket": True,
        "loop_tick_tcp_port": None,
    }
    payload.update(extra)
    return payload


def test_loop_tick_probe_answer_says_alive_even_when_heartbeat_is_stale(hermes_home: Path):
    """A witness answer is direct loop evidence: a stalled heartbeat write is not a wedge."""
    probes: list[tuple[int, int | None]] = []

    def probe(pid: int, tcp_port: int | None) -> bool | None:
        probes.append((pid, tcp_port))
        return True

    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0))

    gateway = _collect_probed(hermes_home, probe).gateway  # type: ignore[attr-defined]

    assert gateway.loop_health is GatewayLoopHealth.ALIVE
    assert gateway.loop_tick_armed is True
    assert probes == [(4242, None)]


def test_loop_tick_fresh_heartbeat_with_silent_socket_is_unknown(hermes_home: Path):
    """Fresh heartbeat but a silent witness: an off-loop write can land after a freeze."""
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(30.0))

    gateway = _collect_probed(hermes_home, lambda pid, tcp_port: False).gateway  # type: ignore[attr-defined]

    assert gateway.loop_health is GatewayLoopHealth.UNKNOWN


def test_loop_tick_sustained_silence_escalates_to_wedged(hermes_home: Path):
    """One silent probe is never evidence; escalation needs silence across refreshes."""
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0))

    collector = Collector(
        hermes_home,
        pid_exists=lambda pid: pid == 4242,
        clock=_clock,
        loop_tick_probe=lambda pid, tcp_port: False,  # type: ignore[arg-type,return-value]
    )
    try:
        first = collector.collect().gateway
        second = collector.collect().gateway
        third = collector.collect().gateway
    finally:
        collector.close()

    assert first.loop_health is GatewayLoopHealth.STALE
    assert second.loop_health is GatewayLoopHealth.STALE
    assert third.loop_health is GatewayLoopHealth.WEDGED


def test_loop_tick_legacy_heartbeat_staleness_alone_is_proof(hermes_home: Path):
    """A payload without the witness key is an on-loop writer: stale means wedged."""
    legacy = {
        key: value for key, value in _armed_heartbeat(400.0).items() if key != "loop_tick_socket"
    }
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, legacy)

    gateway = _collect_probed(hermes_home, lambda pid, tcp_port: None).gateway  # type: ignore[attr-defined]

    assert gateway.loop_health is GatewayLoopHealth.LEGACY
    assert gateway.loop_tick_armed is None


def test_loop_tick_witness_absent_node_is_ambiguity_never_a_wedge(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0))

    collector = Collector(
        hermes_home,
        pid_exists=lambda pid: pid == 4242,
        clock=_clock,
        loop_tick_probe=lambda pid, tcp_port: None,  # type: ignore[arg-type,return-value]
    )
    try:
        for _ in range(5):
            gateway = collector.collect().gateway
    finally:
        collector.close()

    assert gateway.loop_health is GatewayLoopHealth.UNKNOWN


def test_loop_tick_disarmed_witness_never_escalates(hermes_home: Path):
    """loop_tick_socket: false means the bind failed upstream: staleness is not proof."""
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0, loop_tick_socket=False))

    collector = Collector(
        hermes_home,
        pid_exists=lambda pid: pid == 4242,
        clock=_clock,
        loop_tick_probe=lambda pid, tcp_port: False,  # type: ignore[arg-type,return-value]
    )
    try:
        for _ in range(5):
            gateway = collector.collect().gateway
    finally:
        collector.close()

    assert gateway.loop_health is GatewayLoopHealth.UNKNOWN
    assert gateway.loop_tick_armed is False


def test_loop_tick_tcp_port_is_probed_instead_of_the_socket(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0, loop_tick_tcp_port=5555))

    gateway = _collect_probed(hermes_home, lambda pid, tcp_port: tcp_port == 5555).gateway  # type: ignore[attr-defined]

    assert gateway.loop_health is GatewayLoopHealth.ALIVE


def test_loop_tick_heartbeat_pid_mismatch_is_not_evidence(hermes_home: Path):
    """The socket node is PID-suffixed: never probe a witness another process owns."""
    probes: list[tuple[int, int | None]] = []

    def probe(pid: int, tcp_port: int | None) -> bool | None:
        probes.append((pid, tcp_port))
        return True

    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0, pid=999999))

    gateway = _collect_probed(hermes_home, probe).gateway  # type: ignore[attr-defined]

    assert probes == []
    # Without witness evidence the age-only verdict stands.
    assert gateway.loop_health is GatewayLoopHealth.WEDGED
    assert gateway.loop_tick_armed is None


def test_loop_tick_stopped_gateway_is_never_probed(hermes_home: Path):
    probes: list[tuple[int, int | None]] = []

    def probe(pid: int, tcp_port: int | None) -> bool | None:
        probes.append((pid, tcp_port))
        return True

    _write_gateway_state(hermes_home, gateway_state="stopped")
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0))

    gateway = _collect_probed(hermes_home, probe).gateway  # type: ignore[attr-defined]

    assert probes == []
    assert gateway.loop_health is GatewayLoopHealth.STALE


def test_loop_tick_strikes_reset_after_a_witness_answer(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0))
    answers: list[bool | None] = [False, False, True, False, False]

    def probe(pid: int, tcp_port: int | None) -> bool | None:
        return answers.pop(0) if answers else False

    collector = Collector(
        hermes_home,
        pid_exists=lambda pid: pid == 4242,
        clock=_clock,
        loop_tick_probe=probe,  # type: ignore[arg-type]
    )
    try:
        first = collector.collect().gateway
        second = collector.collect().gateway
        third = collector.collect().gateway
        fourth = collector.collect().gateway
        fifth = collector.collect().gateway
    finally:
        collector.close()

    assert first.loop_health is GatewayLoopHealth.STALE
    assert second.loop_health is GatewayLoopHealth.STALE
    assert third.loop_health is GatewayLoopHealth.ALIVE
    # The strike counter reset on the answer: two fresh silences are not a wedge.
    assert fourth.loop_health is GatewayLoopHealth.STALE
    assert fifth.loop_health is GatewayLoopHealth.STALE


def _serve_one_byte(listener: socket.socket) -> None:
    """Accept one connection and send the witness byte, like _tick_socket_handler."""
    conn, _ = listener.accept()
    with conn:
        conn.sendall(b"1")


def _short_socket_dir() -> Iterator[Path]:
    """AF_UNIX node paths are capped at ~104 chars on macOS: keep the dir short."""
    for parent in (None, "/tmp"):
        path = Path(tempfile.mkdtemp(prefix="hermesd-lt-", dir=parent))
        try:
            probe = path / "probe.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(probe))
            listener.close()
            probe.unlink(missing_ok=True)
            return path
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
    pytest.skip("no usable short AF_UNIX temp directory")


@pytest.mark.parametrize("use_tcp", [False, True])
def test_default_loop_tick_probe_answers_from_a_real_witness(hermes_home: Path, use_tcp: bool):
    import socket
    import threading

    from hermesd.collect.gateway import _default_loop_tick_probe

    if use_tcp:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        tcp_port: int | None = listener.getsockname()[1]
        home = hermes_home
    else:
        home = _short_socket_dir()
        (home / "state").mkdir()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(home / "state" / "gateway.loop-tick.4242.sock"))
        listener.listen(1)
        tcp_port = None
    try:
        watcher = threading.Thread(target=_serve_one_byte, args=(listener,), daemon=True)
        watcher.start()
        assert _default_loop_tick_probe(4242, tcp_port, home) is True
        watcher.join(timeout=5)
        # No node / invalid port / silent witness are all "no evidence" or "silent".
        assert _default_loop_tick_probe(999999, None, home) is None
        assert _default_loop_tick_probe(4242, 0, home) is None
        assert _default_loop_tick_probe(4242, 70000, home) is None
        assert _default_loop_tick_probe(4242, None, hermes_home) is None
    finally:
        listener.close()
        if not use_tcp:
            shutil.rmtree(home, ignore_errors=True)


def test_default_loop_tick_probe_silent_witness_is_false(hermes_home: Path):
    import socket

    from hermesd.collect.gateway import _default_loop_tick_probe

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        # A listening witness that never answers reads as silent, not absent.
        assert _default_loop_tick_probe(4242, port, hermes_home) is False
    finally:
        listener.close()


def test_default_loop_tick_probe_never_writes_to_the_home(hermes_home: Path):
    from hermesd.collect.gateway import _default_loop_tick_probe

    before = sorted(str(p) for p in hermes_home.rglob("*"))
    _default_loop_tick_probe(4242, None, hermes_home)
    after = sorted(str(p) for p in hermes_home.rglob("*"))

    assert before == after


# --------------------------------------------------------------------------
# K. lifecycle OOM + unclean-exit carry flags
# --------------------------------------------------------------------------


def test_lifecycle_carries_prior_unclean_exit_and_oom_flags(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_lifecycle(
        hermes_home,
        phase="running",
        pid=4242,
        prior_unclean_exit=True,
        prior_suspected_oom=True,
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.prior_unclean_exit is True
    assert gateway.prior_suspected_oom is True


def test_lifecycle_oom_flags_absent_on_clean_records(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_lifecycle(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.prior_unclean_exit is False
    assert gateway.prior_suspected_oom is False


def test_lifecycle_oom_flags_ignore_non_boolean_junk(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_lifecycle(
        hermes_home,
        phase="running",
        pid=4242,
        prior_unclean_exit="yes",
        prior_suspected_oom=1,
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.prior_unclean_exit is False
    assert gateway.prior_suspected_oom is False


# --------------------------------------------------------------------------
# L. restart-storm ledger (gateway-starts.log)
# --------------------------------------------------------------------------


def _write_starts_log(home: Path, epochs: list[float]) -> Path:
    path = home / "gateway-starts.log"
    path.write_text("".join(f"{epoch!r}\n" for epoch in epochs))
    return path


def test_restart_storm_counts_window_and_cap(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_starts_log(
        hermes_home,
        [NOW - 30, NOW - 60, NOW - 400, NOW - 3600, NOW - 7200, NOW - 100000],
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_starts_recorded is True
    assert gateway.gateway_starts_window == 2
    assert gateway.gateway_starts_1h == 4
    assert gateway.seconds_since_last_gateway_start == pytest.approx(30.0)
    assert gateway.in_respawn_backoff is False
    assert gateway.restart_storm_cap == 5


def test_restart_storm_backoff_when_cap_is_exceeded(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_starts_log(hermes_home, [NOW - 10 * i for i in range(1, 7)])

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_starts_window == 6
    assert gateway.in_respawn_backoff is True


def test_restart_storm_absent_file_is_not_evidence_of_zero_restarts(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_starts_recorded is False
    assert gateway.gateway_starts_window == 0
    assert gateway.in_respawn_backoff is False


def test_restart_storm_garbage_and_future_lines_are_ignored(hermes_home: Path):
    _write_gateway_state(hermes_home)
    path = hermes_home / "gateway-starts.log"
    path.write_text(f"{NOW - 30!r}\nnot-a-float\n{NOW + 5000!r}\n{NOW - 60!r}\n\n")

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_starts_recorded is True
    assert gateway.gateway_starts_window == 2
    assert gateway.seconds_since_last_gateway_start == pytest.approx(30.0)


def test_restart_storm_empty_file_records_nothing(hermes_home: Path):
    """Upstream appends ``now`` before its atomic replace, so an empty ledger is not a ledger."""
    _write_gateway_state(hermes_home)
    (hermes_home / "gateway-starts.log").write_text("")

    state = _collect(hermes_home)

    assert state.gateway.gateway_starts_recorded is False
    assert state.gateway.gateway_starts_window == 0
    assert state.gateway.seconds_since_last_gateway_start is None
    assert "Starts:" not in _gateway_detail(state)


@pytest.mark.parametrize("content", ["   \n\n", "not-a-float\n# junk\n\n"])
def test_restart_storm_unparseable_file_records_nothing(hermes_home: Path, content: str):
    _write_gateway_state(hermes_home)
    (hermes_home / "gateway-starts.log").write_text(content)

    state = _collect(hermes_home)

    assert state.gateway.gateway_starts_recorded is False
    assert state.gateway.gateway_starts_window == 0
    assert state.gateway.in_respawn_backoff is False
    assert "Starts:" not in _gateway_detail(state)


def test_restart_storm_one_valid_epoch_beside_junk_still_records(hermes_home: Path):
    _write_gateway_state(hermes_home)
    (hermes_home / "gateway-starts.log").write_text(f"not-a-float\n{NOW - 30!r}\n\n")

    state = _collect(hermes_home)

    assert state.gateway.gateway_starts_recorded is True
    assert state.gateway.gateway_starts_window == 1
    assert state.gateway.gateway_starts_1h == 1
    assert state.gateway.seconds_since_last_gateway_start == pytest.approx(30.0)
    assert state.gateway.restart_storm_cap == 5
    assert "Starts:" in _gateway_detail(state)


# --------------------------------------------------------------------------
# M. dashboard client attachment marker
# --------------------------------------------------------------------------


def _touch_client_heartbeat(home: Path, age: float | None) -> Path | None:
    path = home / "state" / "dashboard_clients.heartbeat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (NOW - age, NOW - age) if age is not None else None)
    return path


def test_dashboard_client_attached_when_marker_is_fresh(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _touch_client_heartbeat(hermes_home, 10.0)

    gateway = _collect(hermes_home).gateway

    assert gateway.dashboard_client_attached is True
    assert gateway.dashboard_client_last_frame_age_seconds == pytest.approx(10.0)


def test_dashboard_client_stale_marker_keeps_age_but_not_attached(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _touch_client_heartbeat(hermes_home, 600.0)

    gateway = _collect(hermes_home).gateway

    assert gateway.dashboard_client_attached is False
    assert gateway.dashboard_client_last_frame_age_seconds == pytest.approx(600.0)


def test_dashboard_client_missing_marker_means_never(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.dashboard_client_attached is False
    assert gateway.dashboard_client_last_frame_age_seconds is None


def test_dashboard_client_future_mtime_clamps_to_now(hermes_home: Path):
    _write_gateway_state(hermes_home)
    path = hermes_home / "state" / "dashboard_clients.heartbeat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (NOW + 5000, NOW + 5000))

    gateway = _collect(hermes_home).gateway

    assert gateway.dashboard_client_last_frame_age_seconds == 0.0
    assert gateway.dashboard_client_attached is True


# --------------------------------------------------------------------------
# N. exit diagnostics ledger (logs/gateway-exit-diag.log + companions)
# --------------------------------------------------------------------------


def _write_exit_diag(home: Path, records: list[dict[str, object]]) -> Path:
    logs = home / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / "gateway-exit-diag.log"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def test_exit_diag_last_tag_age_and_unclean_count(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_exit_diag(
        hermes_home,
        [
            {"ts": _iso(NOW - 7200), "tag": "gateway.previous_unclean_exit", "pid": 9},
            {"ts": _iso(NOW - 60), "tag": "gateway.asyncio_main_return", "pid": 10},
            {"ts": _iso(NOW - 30), "tag": "gateway.previous_unclean_exit", "pid": 11},
        ],
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.exit_diag_recorded is True
    assert gateway.exit_diag_last_tag == "gateway.previous_unclean_exit"
    assert gateway.exit_diag_last_age_seconds == pytest.approx(30.0)
    assert gateway.exit_diag_unclean_24h == 2
    assert gateway.exit_diag_size_bytes > 0
    assert gateway.exit_diag_oversized is False


def test_exit_diag_absent_file_is_not_evidence_of_clean_exits(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.exit_diag_recorded is False
    assert gateway.exit_diag_last_tag == ""
    assert gateway.exit_diag_unclean_24h == 0


def test_exit_diag_oversized_file_warns(hermes_home: Path):
    _write_gateway_state(hermes_home)
    path = hermes_home / "logs" / "gateway-exit-diag.log"
    path.write_text("x" * (2 * 1024 * 1024 + 1))

    gateway = _collect(hermes_home).gateway

    assert gateway.exit_diag_oversized is True
    assert gateway.exit_diag_size_bytes == 2 * 1024 * 1024 + 1


def test_exit_diag_tail_reads_only_the_cap(hermes_home: Path):
    _write_gateway_state(hermes_home)
    path = _write_exit_diag(
        hermes_home,
        [
            {"ts": _iso(NOW - 60), "tag": "gateway.previous_unclean_exit", "pid": 9},
        ],
    )
    path.write_text(
        path.read_text()
        + json.dumps({"ts": _iso(NOW - 30), "tag": "gateway.asyncio_main_return", "pid": 10})
        + "\n"
    )

    collector = Collector(
        hermes_home, pid_exists=lambda pid: pid == 4242, clock=_clock, log_tail_bytes=96
    )
    try:
        gateway = collector.collect().gateway
    finally:
        collector.close()

    # The unclean record fell out of the tiny tail window: only the visible
    # records are counted, never the whole unbounded file.
    assert gateway.exit_diag_last_tag == "gateway.asyncio_main_return"
    assert gateway.exit_diag_unclean_24h == 0


def test_exit_diag_junk_lines_are_ignored(hermes_home: Path):
    _write_gateway_state(hermes_home)
    logs = hermes_home / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "gateway-exit-diag.log").write_text(
        "not json\n"
        + json.dumps({"ts": "junk", "tag": "gateway.asyncio_main_return"})
        + "\n"
        + json.dumps({"ts": _iso(NOW - 45), "tag": "x" * 500, "traceback": "secret"})
        + "\n"
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.exit_diag_last_tag == "x" * 120
    assert gateway.exit_diag_last_age_seconds == pytest.approx(45.0)
    assert "secret" not in json.dumps(gateway.model_dump(mode="json"))


def test_forensic_companion_files_reported_when_present(hermes_home: Path):
    _write_gateway_state(hermes_home)
    logs = hermes_home / "logs"
    logs.mkdir(exist_ok=True)
    shutdown = logs / "gateway-shutdown-diag.log"
    shutdown.write_text("signal block")
    os.utime(shutdown, (NOW - 600, NOW - 600))
    fault = logs / "gateway_faulthandler.log"
    fault.write_text("z" * 2048)
    os.utime(fault, (NOW - 7200, NOW - 7200))

    gateway = _collect(hermes_home).gateway

    names = {entry.name: entry for entry in gateway.forensic_files}
    assert set(names) == {"gateway-shutdown-diag.log", "gateway_faulthandler.log"}
    assert names["gateway-shutdown-diag.log"].size_bytes == len("signal block")
    assert names["gateway-shutdown-diag.log"].age_seconds == pytest.approx(600.0)
    assert names["gateway_faulthandler.log"].size_bytes == 2048
    assert "launchd-reload.log" not in names


# --------------------------------------------------------------------------
# O. shared-listener mirror URLs (listener_base on default-profile entries)
# --------------------------------------------------------------------------


def test_listener_base_mirrors_are_synthesized_for_served_profiles(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        served_profiles=["dev", "default"],
        platforms={
            "api_server": {"state": "connected", "listener_base": "http://127.0.0.1:8088"},
            "webhook": {"state": "connected", "listener_base": "http://127.0.0.1:8089"},
            "telegram": {"state": "connected", "listener_base": "http://127.0.0.1:9000"},
        },
    )

    platforms = {p.name: p for p in _collect(hermes_home).gateway.platforms}

    assert platforms["api_server"].mirror_urls == {"dev": "http://127.0.0.1:8088/p/dev/v1"}
    assert platforms["webhook"].mirror_urls == {
        "dev": "http://127.0.0.1:8089/p/dev/webhooks/<route>"
    }
    # Not a port binder: never mirrored.
    assert platforms["telegram"].mirror_urls == {}


def test_listener_base_mirrors_need_a_live_writer_and_a_serving_state(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        served_profiles=["dev"],
        pid=999_999_999,
        platforms={
            "api_server": {"state": "connected", "listener_base": "http://127.0.0.1:8088"},
        },
    )
    assert _collect(hermes_home).gateway.platforms[0].mirror_urls == {}

    _write_gateway_state(
        hermes_home,
        served_profiles=["dev"],
        platforms={
            "api_server": {"state": "fatal", "listener_base": "http://127.0.0.1:8088"},
        },
    )
    assert _collect(hermes_home).gateway.platforms[0].mirror_urls == {}

    _write_gateway_state(
        hermes_home,
        platforms={
            "api_server": {"state": "connected", "listener_base": "http://127.0.0.1:8088"},
        },
    )
    assert _collect(hermes_home).gateway.platforms[0].mirror_urls == {}


def test_listener_base_mirrors_are_redacted(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        served_profiles=["dev"],
        platforms={
            "api_server": {
                "state": "connected",
                "listener_base": "http://bob:hunter2@127.0.0.1:8088",
            },
        },
    )

    platform = _collect(hermes_home).gateway.platforms[0]

    assert "hunter2" not in json.dumps(platform.model_dump(mode="json"))
    assert platform.mirror_urls["dev"].startswith("http://[REDACTED]@127.0.0.1:8088/p/dev/v1")


@pytest.mark.parametrize("state", ["starting", "paused", "unknown"])
def test_listener_base_mirrors_require_a_serving_state(hermes_home: Path, state: str):
    """Upstream mirrors only a serving default entry (gateway/status.py:962-966)."""
    _write_gateway_state(
        hermes_home,
        served_profiles=["dev"],
        platforms={
            "api_server": {"state": state, "listener_base": "http://127.0.0.1:8088"},
        },
    )

    assert _collect(hermes_home).gateway.platforms[0].mirror_urls == {}


def test_listener_base_mirrors_absent_when_state_is_missing(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        served_profiles=["dev"],
        platforms={"api_server": {"listener_base": "http://127.0.0.1:8088"}},
    )

    platform = _collect(hermes_home).gateway.platforms[0]

    assert platform.state == "unknown"
    assert platform.mirror_urls == {}


@pytest.mark.parametrize("state", ["connected", "connecting", "retrying"])
def test_listener_base_mirrors_are_synthesized_for_every_serving_state(
    hermes_home: Path, state: str
):
    _write_gateway_state(
        hermes_home,
        served_profiles=["dev"],
        platforms={
            "api_server": {"state": state, "listener_base": "http://127.0.0.1:8088"},
        },
    )

    assert _collect(hermes_home).gateway.platforms[0].mirror_urls == {
        "dev": "http://127.0.0.1:8088/p/dev/v1"
    }


def test_listener_mirror_urls_need_a_live_writer():
    assert (
        _listener_mirror_urls(
            "api_server",
            {"listener_base": "https://x.test"},
            "connected",
            record_current=False,
            served_profiles=["coding"],
        )
        == {}
    )


def test_listener_mirror_urls_skip_the_default_profile():
    assert _listener_mirror_urls(
        "api_server",
        {"listener_base": "https://x.test"},
        "connected",
        record_current=True,
        served_profiles=["default", "coding"],
    ) == {"coding": "https://x.test/p/coding/v1"}


def _write_respawn_config(home: Path, **respawn: object) -> None:
    body = ["gateway:", "  respawn_storm:"]
    for key, value in respawn.items():
        body.append(f"    {key}: {value!r}" if isinstance(value, str) else f"    {key}: {value}")
    (home / "config.yaml").write_text("\n".join(body) + "\n")


def test_restart_storm_uses_the_configured_cap(hermes_home: Path):
    """Upstream's effective cap is ``gateway.respawn_storm.max_starts``, not 5.

    ``_respawn_storm_backoff`` reads the value from ``load_config()``
    (``hermes_cli/gateway.py:4673-4685``), so a raised cap means no backoff
    where the hardcoded default would have cried storm — and a lowered one means
    a real storm the default would have missed.
    """
    _write_gateway_state(hermes_home)
    _write_respawn_config(hermes_home, max_starts=10)
    _write_starts_log(hermes_home, [NOW - 10 * i for i in range(1, 7)])

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_starts_window == 6
    assert gateway.restart_storm_cap == 10
    assert gateway.in_respawn_backoff is False

    _write_respawn_config(hermes_home, max_starts=2)
    gateway = _collect(hermes_home).gateway
    assert gateway.restart_storm_cap == 2
    assert gateway.in_respawn_backoff is True


def test_restart_storm_uses_the_configured_window(hermes_home: Path):
    """A 300 s window counts starts the hardcoded 120 s window would drop."""
    _write_gateway_state(hermes_home)
    _write_respawn_config(hermes_home, window_seconds=300)
    _write_starts_log(hermes_home, [NOW - 60, NOW - 150, NOW - 290, NOW - 400])

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_storm_window_seconds == pytest.approx(300.0)
    assert gateway.gateway_starts_window == 3
    assert gateway.gateway_starts_1h == 4
    assert gateway.in_respawn_backoff is False


def test_restart_storm_disabled_writer_makes_no_claims(hermes_home: Path):
    """``max_starts <= 0`` disables the writer upstream, so the ledger is stale
    by construction and hermesd must not report a storm verdict from it."""
    _write_gateway_state(hermes_home)
    _write_respawn_config(hermes_home, max_starts=0)
    _write_starts_log(hermes_home, [NOW - 10 for _ in range(9)])

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_starts_recorded is False
    assert gateway.in_respawn_backoff is False


def test_respawn_storm_policy_reads_only_upstream_shapes():
    """booleans and junk are not ints upstream: mirror the isinstance guards."""
    from hermesd.collect.gateway import _respawn_storm_policy

    assert _respawn_storm_policy({}) == (5, 120.0)
    assert _respawn_storm_policy({"gateway": {"respawn_storm": {"max_starts": 8}}}) == (8, 120.0)
    assert _respawn_storm_policy(
        {"gateway": {"respawn_storm": {"max_starts": True, "window_seconds": "300"}}}
    ) == (5, 120.0)
    assert _respawn_storm_policy({"gateway": {"respawn_storm": {"window_seconds": 300.5}}}) == (
        5,
        300.5,
    )


# --------------------------------------------------------------------------
# O. regression pins for review-reported mutation survivors
# --------------------------------------------------------------------------


def test_loop_tick_probe_uses_only_the_first_byte(hermes_home: Path):
    """The protocol is one byte: a witness that appends anything still answers.

    Pins ``recv(1)`` (``hermes_cli/gateway.py:363-376``): a probe that drained
    the socket would misread a well-behaved handler that writes ``b"1\\n"``.
    """
    import socket
    import threading

    from hermesd.collect.gateway import _default_loop_tick_probe

    home = _short_socket_dir()
    (home / "state").mkdir()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(home / "state" / "gateway.loop-tick.4242.sock"))
    listener.listen(1)

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.sendall(b"1\n")

    watcher = threading.Thread(target=serve, daemon=True)
    watcher.start()
    try:
        assert _default_loop_tick_probe(4242, None, home) is True
        watcher.join(timeout=5)
    finally:
        listener.close()
        shutil.rmtree(home, ignore_errors=True)


def test_exit_diag_size_warning_boundary(hermes_home: Path):
    """Oversized is False below the 2 MiB cap and True above it."""
    from hermesd.collect.gateway import _EXIT_DIAG_SIZE_WARN_BYTES

    _write_gateway_state(hermes_home)
    logs = hermes_home / "logs"
    logs.mkdir(exist_ok=True)
    ledger = logs / "gateway-exit-diag.log"

    padding = '{"tag": "padding"}\n'
    one_mib = padding * (1024 * 1024 // len(padding))
    ledger.write_text(one_mib)
    gateway = _collect(hermes_home).gateway
    assert gateway.exit_diag_size_bytes == len(one_mib)
    assert gateway.exit_diag_oversized is False

    over = one_mib + "x" * (_EXIT_DIAG_SIZE_WARN_BYTES - len(one_mib) + 1)
    ledger.write_text(over)
    gateway = _collect(hermes_home).gateway
    assert gateway.exit_diag_oversized is True


def test_exit_diag_unclean_count_window_is_24h(hermes_home: Path):
    """An unclean exit older than a day is history, not a current signal."""
    _write_gateway_state(hermes_home)
    _write_exit_diag(
        hermes_home,
        [
            {"ts": _iso(NOW - 30 * 3600), "tag": "gateway.previous_unclean_exit", "pid": 9},
            {"ts": _iso(NOW - 2 * 3600), "tag": "gateway.previous_unclean_exit", "pid": 10},
            {"ts": _iso(NOW - 3600), "tag": "gateway.previous_unclean_exit", "pid": 11},
        ],
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.exit_diag_unclean_24h == 2


def test_forensic_companions_are_stat_only(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Companion logs are reported by size alone; their contents are never read."""
    _write_gateway_state(hermes_home)
    logs = hermes_home / "logs"
    logs.mkdir(exist_ok=True)
    companions = ("gateway-shutdown-diag.log", "gateway_faulthandler.log", "launchd-reload.log")
    for name in companions:
        (logs / name).write_text("SECRET-COMPANION-BODY")

    import hermesd.collect.gateway as gateway_module

    read_paths: list[str] = []
    for helper in ("_read_text_capped", "_read_tail_text"):
        real = getattr(gateway_module, helper)

        def spy(path: Path, *args: object, _real: object = real, **kwargs: object) -> object:
            read_paths.append(Path(path).name)
            return _real(path, *args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(gateway_module, helper, spy)

    gateway = _collect(hermes_home).gateway

    assert {f.name for f in gateway.forensic_files} == set(companions)
    assert all(size > 0 for size in (f.size_bytes for f in gateway.forensic_files))
    assert read_paths == [] or not set(read_paths) & set(companions)


def test_restart_storm_exactly_at_the_cap_is_not_backoff(hermes_home: Path):
    """Upstream backs off only *past* the cap (``len(recent) <= max_starts``)."""
    _write_gateway_state(hermes_home)
    _write_starts_log(hermes_home, [NOW - 10 * index for index in range(1, 6)])

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_starts_window == 5
    assert gateway.in_respawn_backoff is False


def test_mirror_roster_is_bounded(hermes_home: Path):
    """The synthesized mirror list honors its profile bound.

    The bound is asserted as a literal: importing the constant under test into
    the expectation would make the assertion move with the mutation.
    """
    _write_gateway_state(hermes_home)
    profiles = [f"p{index:02d}" for index in range(20)]
    state_path = hermes_home / "gateway_state.json"
    payload = json.loads(state_path.read_text())
    payload["served_profiles"] = profiles
    payload["platforms"] = {
        "api_server": {"state": "connected", "listener_base": "http://127.0.0.1:8088"}
    }
    state_path.write_text(json.dumps(payload))

    gateway = _collect(hermes_home).gateway

    platform = next(p for p in gateway.platforms if p.name == "api_server")
    assert len(platform.mirror_urls) == 16
    assert "p00" in platform.mirror_urls
    assert "p15" in platform.mirror_urls
    assert "p16" not in platform.mirror_urls


def test_restart_storm_keeps_defaults_when_config_is_unreadable(hermes_home: Path):
    """A torn config.yaml must not take the storm verdict down with it.

    The policy read is an enrichment of the ledger read: with no usable config
    the upstream defaults apply and the source stays healthy, rather than the
    whole restart-storm source failing over an unrelated file.
    """
    _write_gateway_state(hermes_home)
    _write_starts_log(hermes_home, [NOW - 10 * index for index in range(1, 7)])
    (hermes_home / "config.yaml").write_text("[1, 2, 3]")

    state = _collect(hermes_home)

    assert "gateway_restart_storm" not in state.health.failed_sources
    gateway = state.gateway
    assert gateway.restart_storm_cap == 5
    assert gateway.restart_storm_window_seconds == pytest.approx(120.0)
    assert gateway.gateway_starts_window == 6
    assert gateway.in_respawn_backoff is True


def test_gateway_reads_stringified_state_flags_strictly(hermes_home: Path):
    """A stringified ``"false"`` in gateway_state.json is not truth.

    The record is written by the live gateway, so a string where a boolean
    belongs is corruption; reading it truthily would raise a "needs attention"
    flag on a healthy platform and claim a config file exists that does not.
    """
    (hermes_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 4242,
                "start_time": NOW - 5000,
                "kind": "hermes-gateway",
                "gateway_state": "running",
                "platforms": {
                    "telegram": {"state": "connected", "needs_attention": "false"},
                },
                "config_generation": {
                    "fingerprint": "fp",
                    "short": "abc123",
                    "sources": [
                        {
                            "name": "config.yaml",
                            "path": "/tmp/config.yaml",
                            "exists": "false",
                            "mtime_ns": 1,
                            "size": 2,
                        }
                    ],
                },
            }
        )
    )

    gateway = _collect(hermes_home).gateway

    platform = next(p for p in gateway.platforms if p.name == "telegram")
    assert platform.needs_attention is False
    assert gateway.config_sources[0].exists is False
