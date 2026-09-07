"""Tests for the [5] Config panel."""

from __future__ import annotations

from hermesd.models import ConfigSummary, DashboardState
from hermesd.panels.config_panel import render_config
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_config_detail_escapes_code_execution_mode() -> None:
    state = DashboardState(config=ConfigSummary(code_execution_mode="yolo [/] inj"))
    rendered = render_to_str(render_config(state, Theme(), detail=True))
    assert "yolo [/] inj" in rendered


def test_config_detail_escapes_moa_label_values() -> None:
    state = DashboardState(
        config=ConfigSummary(
            moa_active_preset="preset [/] x",
            moa_preset_count=2,
            moa_aggregator_label="agg [/] y",
        )
    )
    rendered = render_to_str(render_config(state, Theme(), detail=True))
    assert "preset [/] x" in rendered
    assert "agg [/] y" in rendered


def _agent_limits_config() -> ConfigSummary:
    return ConfigSummary(
        delegation_max_concurrent_children=10,
        delegation_max_spawn_depth=2,
        delegation_orchestrator_enabled=True,
        goals_max_turns=20,
        updates_check=True,
        updates_pre_update_backup="quick",
        updates_backup_keep=5,
        mcp_server_count=2,
        mcp_server_names=["playwright", "sheets"],
        plugin_enabled_count=3,
        plugin_disabled_count=1,
        tool_loop_warnings_enabled=True,
        tool_loop_hard_stop_enabled=True,
        max_live_sessions=8,
        streaming_enabled=True,
        logging_level="INFO",
        network_proxy_configured=True,
    )


def _render(config: ConfigSummary, detail: bool) -> str:
    state = DashboardState(config=config)
    return render_to_str(render_config(state, Theme(), detail=detail), width=200, no_color=True)


def test_config_detail_shows_agent_limits_and_integrations() -> None:
    text = _render(_agent_limits_config(), detail=True)

    assert "Agent limits" in text
    assert "Integrations" in text
    assert "children 10" in text
    assert "depth 2" in text
    assert "max turns 20" in text
    assert "warn" in text
    assert "hard-stop" in text
    assert "INFO" in text
    assert "playwright" in text
    assert "sheets" in text
    assert "quick" in text
    assert "keep 5" in text
    assert "3 enabled" in text
    assert "1 disabled" in text


def test_config_detail_empty_sections_render_placeholder() -> None:
    text = _render(ConfigSummary(), detail=True)

    assert "Agent limits" in text
    assert "Integrations" in text
    assert "—" in text
    assert "playwright" not in text


def test_config_compact_shows_integrations_line() -> None:
    text = _render(_agent_limits_config(), detail=False)

    assert "mcp 2" in text
    assert "plugins 3" in text
    assert "goals 20" in text


def test_config_compact_shows_goals_placeholder_when_unset() -> None:
    text = _render(ConfigSummary(), detail=False)

    assert "mcp 0" in text
    assert "goals —" in text


def test_config_detail_escapes_markup_hostile_server_names() -> None:
    config = ConfigSummary(
        mcp_server_count=1,
        mcp_server_names=["[bold red]evil\x1b[2J"],
        logging_level="[blink]DEBUG",
    )
    text = _render(config, detail=True)

    assert "[bold red]evil" in text
    assert "\x1b[2J" not in text
    assert "[blink]DEBUG" in text


def test_config_compact_never_blanks_with_hostile_names() -> None:
    config = ConfigSummary(mcp_server_count=1, mcp_server_names=["\x1b]0;pwn\x07x"])
    text = _render(config, detail=False)

    assert "Config" in text
    assert "\x1b]0;" not in text
