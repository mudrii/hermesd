"""Tests for [4] Tools panel — two-table detail layout."""

from __future__ import annotations

from hermesd.models import (
    BackgroundProcessInfo,
    DashboardState,
    ToolsetAvailability,
    ToolStats,
)
from hermesd.panels import render_panel
from hermesd.panels.tools import render_tools
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


def test_tools_detail_handles_absurd_started_at() -> None:
    state = DashboardState(
        background_processes=[
            BackgroundProcessInfo(session_id="s", command="make", started_at=1e18)
        ]
    )
    rendered = render_to_str(render_tools(state, Theme(), detail=True))
    assert "—" in rendered


def _process(**fields: object) -> BackgroundProcessInfo:
    base: dict[str, object] = {
        "session_id": "proc_alpha",
        "command": "pytest -q",
        "pid": 4242,
        "purpose": "dashboard",
        "port": 9119,
        "profile": "coding",
        "alive": True,
    }
    base.update(fields)
    return BackgroundProcessInfo(**base)  # type: ignore[arg-type]


def test_tools_detail_renders_purpose_port_and_profile_columns():
    state = DashboardState(background_processes=[_process()])
    text = render_to_str(render_tools(state, Theme(), detail=True), width=180)
    assert "Purpose" in text
    assert "Port" in text
    assert "Profile" in text
    row = next(line for line in text.splitlines() if "proc_alpha" in line)
    assert "dashboard" in row
    assert "9119" in row
    assert "coding" in row


def test_tools_detail_uses_placeholders_for_missing_process_metadata():
    state = DashboardState(
        background_processes=[_process(purpose="", port=0, profile="", pid=0, alive=False)]
    )
    text = render_to_str(render_tools(state, Theme(), detail=True), width=180)
    row = next(line for line in text.splitlines() if "proc_alpha" in line)
    assert row.count("—") >= 3


def test_tools_detail_marks_dead_processes():
    state = DashboardState(
        background_processes=[
            _process(session_id="proc_live", alive=True),
            _process(session_id="proc_dead", pid=4343, alive=False),
        ]
    )
    text = render_to_str(render_tools(state, Theme(), detail=True), width=180)
    live_row = next(line for line in text.splitlines() if "proc_live" in line)
    dead_row = next(line for line in text.splitlines() if "proc_dead" in line)
    assert "✗" not in live_row
    assert "4343 ✗" in dead_row


def test_tools_detail_escapes_markup_hostile_process_metadata():
    state = DashboardState(
        background_processes=[
            _process(
                purpose="[bold]mcp\x1b[2J-helper[/bold]",
                profile="\x1b]8;;http://evil\x07[red]p[/red]",
                command="run [blink]x[/blink]",
            )
        ]
    )
    text = render_to_str(render_tools(state, Theme(), detail=True), width=200)
    assert "\x1b[2J" not in text
    assert "http://evil" not in text
    assert "[bold]mcp" in text
    assert "[blink]x" in text


def test_tools_detail_empty_process_table_still_renders():
    state = DashboardState(background_processes=[])
    text = render_to_str(render_tools(state, Theme(), detail=True), width=180)
    assert "No running background processes" in text


def _availability_state(**fields: object) -> DashboardState:
    return DashboardState(toolset_availability=ToolsetAvailability(**fields))


def test_tools_detail_shows_toolset_availability_line():
    state = _availability_state(
        enabled_toolsets=["file", "web", "terminal"],
        unavailable_toolsets=["bfl", "browser", "kanban"],
        lazy_tool_count=8,
        disabled_tool_count=1,
    )

    text = render_to_str(render_tools(state, Theme(), detail=True), width=180, no_color=True)

    assert "Toolsets: 3 enabled" in text
    assert "unavailable: bfl, browser, kanban" in text
    assert "8 lazy" in text
    assert "1 disabled" in text


def test_tools_detail_omits_unavailable_when_all_toolsets_load():
    state = _availability_state(enabled_toolsets=["file", "web"])

    text = render_to_str(render_tools(state, Theme(), detail=True), width=180, no_color=True)

    assert "Toolsets: 2 enabled" in text
    assert "unavailable" not in text


def test_tools_detail_omits_toolset_line_without_a_snapshot():
    text = render_to_str(render_tools(DashboardState(), Theme(), detail=True), width=180)

    assert "Toolsets:" not in text


def test_tools_compact_marks_unavailable_toolsets():
    state = _availability_state(enabled_toolsets=["file"], unavailable_toolsets=["kanban"])

    text = render_to_str(render_tools(state, Theme()), width=100, no_color=True)

    assert "1 toolset unavailable" in text


def test_tools_compact_has_no_marker_when_all_toolsets_load():
    state = _availability_state(enabled_toolsets=["file"])

    text = render_to_str(render_tools(state, Theme()), width=100, no_color=True)

    assert "unavailable" not in text


def test_tools_detail_escapes_markup_hostile_toolset_names():
    state = _availability_state(
        enabled_toolsets=["ok"], unavailable_toolsets=["[bold red]evil\x1b[2J", "[/]x"]
    )

    text = render_to_str(render_tools(state, Theme(), detail=True), width=180, no_color=True)

    assert "[bold red]evil" in text
    assert "[/]x" in text
    assert "\x1b[2J" not in text
