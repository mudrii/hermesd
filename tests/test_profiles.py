from pathlib import Path

import pytest

from hermesd.__main__ import parse_args, resolve_profile_name
from hermesd.app import DashboardApp
from hermesd.collector import Collector


def test_parse_args_profile_default_none():
    args = parse_args([])
    assert args.profile is None


def test_parse_args_accepts_profile_flag():
    args = parse_args(["--profile", "coding"])
    assert args.profile == "coding"


def test_resolve_profile_name_uses_env(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "research")
    args = parse_args([])
    assert resolve_profile_name(args) == "research"


def test_resolve_profile_name_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "research")
    args = parse_args(["--profile", "coding"])
    assert resolve_profile_name(args) == "coding"


def test_default_collector_ignores_active_profile_file(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home)
    state = c.collect()
    assert state.selected_profile is None
    assert state.profile_mode_label == "root"
    assert state.sessions[0].session_id == "root_session"
    assert state.available_tool_names == ["root_tool"]
    assert state.logs.agent_lines[0].message == "root agent log"
    c.close()


def test_profiled_collector_reads_profile_scoped_runtime_data(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home, profile_name="coding")
    state = c.collect()
    assert state.selected_profile == "coding"
    assert state.profile_mode_label == "profile:coding"
    assert state.sessions[0].session_id == "profile_session"
    assert state.available_tool_names == ["profile_tool"]
    assert state.logs.agent_lines[0].message == "profile agent log"
    c.close()


def test_profiled_collector_keeps_shared_root_config_and_auth(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home, profile_name="coding")
    state = c.collect()
    assert state.config.model == "root-model"
    assert state.config.provider == "root-provider"
    assert [provider.name for provider in state.skills_memory.providers] == [
        "backup-provider",
        "root-provider",
    ]
    c.close()


def test_profiled_collector_reads_profile_scoped_skills(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home, profile_name="coding")
    state = c.collect()
    assert state.skills_memory.skill_count == 1
    assert state.skills_memory.skills[0].name == "profile-skill"
    c.close()


def test_collector_rejects_missing_profile(profiled_hermes_home: Path):
    with pytest.raises(ValueError, match="Profile 'missing' does not exist"):
        Collector(profiled_hermes_home, profile_name="missing")


def test_dashboard_header_shows_profile_mode_label(profiled_hermes_home: Path):
    app = DashboardApp(profiled_hermes_home, profile_name="coding")
    state = app._collector.collect()
    header = app._build_header(state)
    assert "profile:coding" in header.plain
    app.close()


def test_dashboard_header_shows_root_mode_label(profiled_hermes_home: Path):
    app = DashboardApp(profiled_hermes_home)
    state = app._collector.collect()
    header = app._build_header(state)
    assert "root" in header.plain
    app.close()
