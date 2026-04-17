"""Tests for [4] Tools panel — two-table detail layout."""

from rich.console import Console

from hermesd.models import DashboardState, ToolStats
from hermesd.panels import render_panel
from hermesd.theme import Theme


def _render_to_str(panel) -> str:
    console = Console(width=100, force_terminal=True)
    with console.capture() as cap:
        console.print(panel)
    return cap.get()


def test_tools_detail_shows_calls_table():
    state = DashboardState(
        tool_stats=[
            ToolStats(name="cli:abc123", call_count=23),
            ToolStats(name="telegram:def456", call_count=7),
        ],
        total_tool_calls=30,
        available_tools=10,
        available_tool_names=["terminal", "web_search", "read_file"],
    )
    panel = render_panel(4, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "Tool Calls" in text
    assert "30 total" in text
    assert "cli:abc123" in text
    assert "23" in text
    assert "Tool" in text
    assert "Session" not in text


def test_tools_detail_shows_available_table():
    state = DashboardState(
        tool_stats=[],
        total_tool_calls=0,
        available_tools=3,
        available_tool_names=["terminal", "web_search", "read_file"],
    )
    panel = render_panel(4, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "Available Tools" in text
    assert "terminal" in text
    assert "web_search" in text
    assert "read_file" in text


def test_tools_detail_empty_stats_shows_message():
    state = DashboardState(
        tool_stats=[],
        total_tool_calls=0,
        available_tools=0,
        available_tool_names=[],
    )
    panel = render_panel(4, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "No tool call data" in text
    assert "No active session" in text


def test_tools_compact_shows_summary():
    state = DashboardState(
        tool_stats=[ToolStats(name="cli:abc", call_count=10)],
        total_tool_calls=10,
        available_tools=29,
    )
    panel = render_panel(4, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "29 available" in text
    assert "10 calls" in text
