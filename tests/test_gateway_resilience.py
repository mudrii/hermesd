"""Tests for gateway PID detection including launchd fallback."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from hermesd.collector import Collector


def test_gateway_uses_gateway_pid_file_fallback(hermes_home: Path):
    """When gateway_state.json PID is dead, fall back to gateway.pid."""
    my_pid = os.getpid()
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": 999999999, "gateway_state": "running",
        "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
    }))
    pid_file = hermes_home / "gateway.pid"
    pid_file.write_text(json.dumps({"pid": my_pid, "kind": "hermes-gateway"}))

    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == my_pid
    c.close()


def test_gateway_both_pids_dead(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": 999999999, "gateway_state": "running",
        "platforms": {},
    }))
    pid_file = hermes_home / "gateway.pid"
    pid_file.write_text(json.dumps({"pid": 999999998}))

    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    c.close()


def test_gateway_no_pid_file(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": 999999999, "gateway_state": "running",
        "platforms": {},
    }))
    # No gateway.pid file

    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    c.close()


def test_gateway_pid_file_malformed(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": 999999999, "gateway_state": "running",
        "platforms": {},
    }))
    pid_file = hermes_home / "gateway.pid"
    pid_file.write_text("not valid json{{{")

    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    c.close()


def test_gateway_state_stopped_does_not_check_pid(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": 999999999, "gateway_state": "stopped",
        "platforms": {},
    }))

    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    assert state.gateway.state == "stopped"
    c.close()


def test_gateway_live_pid_in_state(hermes_home: Path):
    """When gateway_state.json PID is alive, use it directly."""
    my_pid = os.getpid()
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": my_pid, "gateway_state": "running",
        "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
    }))

    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == my_pid
    c.close()


def test_gateway_shows_version(hermes_home: Path):
    my_pid = os.getpid()
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": my_pid, "gateway_state": "running", "platforms": {},
    }))
    agent_dir = hermes_home / "hermes-agent"
    agent_dir.mkdir()
    (agent_dir / "pyproject.toml").write_text('[project]\nversion = "0.8.0"\n')
    (hermes_home / ".update_check").write_text(json.dumps({"behind": 5}))

    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.hermes_version == "0.8.0"
    assert state.gateway.updates_behind == 5
    c.close()


def test_gateway_version_up_to_date(hermes_home: Path):
    my_pid = os.getpid()
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": my_pid, "gateway_state": "running", "platforms": {},
    }))
    agent_dir = hermes_home / "hermes-agent"
    agent_dir.mkdir()
    (agent_dir / "pyproject.toml").write_text('[project]\nversion = "0.8.0"\n')

    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.hermes_version == "0.8.0"
    assert state.gateway.updates_behind == 0
    c.close()
