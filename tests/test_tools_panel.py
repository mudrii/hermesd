"""Tests for [4] Tools panel — two-table detail layout."""

from __future__ import annotations

from hermesd.models import DashboardState, ToolStats
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str


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
    text = render_to_str(panel, width=100)
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
    text = render_to_str(panel, width=100)
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
    text = render_to_str(panel, width=100)
    assert "No tool call data" in text
    assert "No active session" in text


def test_tools_detail_watch_and_checkpoint_fallback_labels():
    from hermesd.models import BackgroundProcessInfo, CheckpointInfo

    state = DashboardState(
        background_processes=[
            BackgroundProcessInfo(session_id="proc_plain", command="sleep 60"),
            BackgroundProcessInfo(
                session_id="proc_watched",
                command="tail -f log",
                watch_patterns=["ERROR"],
                watcher_interval=0,
            ),
        ],
        checkpoints=[CheckpointInfo(repo_id="repo_no_ts", workdir_name="proj")],
    )
    panel = render_panel(4, state, Theme(), detail=True)
    text = render_to_str(panel, width=100)
    # No watch patterns and no checkpoint timestamp render as dashes;
    # patterns without an interval render as a bare count.
    lines = text.splitlines()
    no_watch_line = next(line for line in lines if "proc_plain" in line)
    assert "—" in no_watch_line
    watch_line = next(line for line in lines if "proc_watched" in line)
    assert "1" in watch_line
    assert "@" not in watch_line
    checkpoint_line = next(line for line in lines if "proj" in line)
    assert "—" in checkpoint_line


def test_tools_compact_shows_summary():
    state = DashboardState(
        tool_stats=[ToolStats(name="cli:abc", call_count=10)],
        total_tool_calls=10,
        available_tools=29,
    )
    panel = render_panel(4, state, Theme(), detail=False)
    text = render_to_str(panel, width=100)
    assert "29 available" in text
    assert "10 calls" in text
