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
        delegation_enabled=True,
        delegation_compression_threshold_tokens=60000,
        delegation_max_parallel=4,
        goals_enabled=True,
        goals_turn_budget=40,
        updates_channel="stable",
        updates_auto=True,
        mcp_server_count=2,
        mcp_server_names=["playwright", "sheets"],
        plugin_config_count=3,
        tool_loop_guardrails_enabled=True,
        tool_loop_max_repeats=3,
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
    assert "60000" in text
    assert "turn budget 40" in text
    assert "INFO" in text
    assert "playwright" in text
    assert "sheets" in text
    assert "stable" in text


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
    assert "goals on" in text


def test_config_compact_shows_goals_off_when_disabled() -> None:
    text = _render(ConfigSummary(), detail=False)

    assert "mcp 0" in text
    assert "goals off" in text


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
