"""Collection of gateway state, PID and launchd detection, channels,
background processes, and the spawn ledger."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from hermesd.collector import (
    Collector,
    _is_dashboard_process,
    _pid_exists,
)
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str


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
