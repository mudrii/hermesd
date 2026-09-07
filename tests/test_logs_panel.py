"""Tests for the [8] Logs panel."""

from __future__ import annotations

from hermesd.models import DashboardState, LogLine, LogState, LogStream
from hermesd.panels.logs import render_logs
from hermesd.theme import Theme
from tests.conftest import render_to_str

CLEAR_SCREEN = "\x1b[2J"


def test_logs_strip_ansi_control_sequences() -> None:
    line = LogLine(
        timestamp="2026-06-14 00:00:00",
        level="INFO",
        message=f"boom {CLEAR_SCREEN} clear",
    )
    state = DashboardState(logs=LogState(agent_lines=[line]))
    for detail in (False, True):
        rendered = render_to_str(render_logs(state, Theme(), detail=detail))
        assert CLEAR_SCREEN not in rendered
        assert "clear" in rendered


def test_logs_compact_falls_back_to_gateway_stream() -> None:
    state = DashboardState(logs=LogState(gateway_lines=[LogLine(message="gateway hello")]))
    rendered = render_to_str(render_logs(state, Theme()))
    assert "gateway hello" in rendered
    assert "No log lines" not in rendered


def test_logs_compact_falls_back_when_named_streams_present() -> None:
    state = DashboardState(
        logs=LogState(
            streams=[
                LogStream(name="agent"),
                LogStream(name="cron", lines=[LogLine(message="cron tick")]),
            ]
        )
    )
    rendered = render_to_str(render_logs(state, Theme()))
    assert "cron tick" in rendered
    assert "No log lines" not in rendered


def test_logs_compact_still_prefers_agent_stream() -> None:
    state = DashboardState(
        logs=LogState(
            agent_lines=[LogLine(message="agent hello")],
            gateway_lines=[LogLine(message="gateway hello")],
        )
    )
    rendered = render_to_str(render_logs(state, Theme()))
    assert "agent hello" in rendered
    assert "gateway hello" not in rendered


def test_logs_compact_empty_state_still_shows_placeholder() -> None:
    rendered = render_to_str(render_logs(DashboardState(), Theme()))
    assert "No log lines" in rendered
