from __future__ import annotations

import time

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.panels.formatting import escape_terminal_text as escape
from hermesd.panels.formatting import sanitize_terminal_text, section_heading
from hermesd.theme import Theme


def render_tools(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    lines = Text()
    lines.append(f"  {state.available_tools} available", style=theme.banner_text)
    lines.append(f"  {state.total_tool_calls} calls\n", style=theme.ui_accent)
    lines.append("  Background: ", style=theme.ui_label)
    lines.append(f"{len(state.background_processes)} proc\n", style=theme.banner_text)
    lines.append("  Checkpoints: ", style=theme.ui_label)
    lines.append(f"{len(state.checkpoints)} repo\n", style=theme.banner_text)
    unavailable = len(state.toolset_availability.unavailable_toolsets)
    if unavailable:
        plural = "" if unavailable == 1 else "s"
        lines.append(f"  ⚠ {unavailable} toolset{plural} unavailable\n", style=theme.ui_warn)
    for ts in state.tool_stats[:3]:
        lines.append(f"  {sanitize_terminal_text(ts.name)}", style=theme.ui_label)
        lines.append(f" ({ts.call_count})\n", style=theme.banner_dim)

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[4] Tools[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    sections: list[RenderableType] = [
        *_tool_calls_section(state, theme),
        Text("\n"),
        *_available_tools_section(state, theme),
        *_toolset_availability_lines(state, theme),
        Text("\n"),
        *_background_processes_section(state, theme),
        Text("\n"),
        *_checkpoints_section(state, theme),
    ]

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[4] Tools[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _tool_calls_section(state: DashboardState, theme: Theme) -> list[RenderableType]:
    header = section_heading(
        f"Tool Calls ({state.total_tool_calls} total)", theme, leading_blank=False
    )
    if not state.tool_stats:
        return [header, Text("  No tool call data\n", style=theme.banner_dim)]
    calls_table = Table(box=None, show_header=True, padding=(0, 2))
    calls_table.add_column("Name", style=theme.ui_label)
    calls_table.add_column("Calls", justify="right", style=theme.ui_accent)
    for ts in state.tool_stats:
        calls_table.add_row(escape(ts.name), str(ts.call_count))
    return [header, calls_table]


def _available_tools_section(state: DashboardState, theme: Theme) -> list[RenderableType]:
    header = section_heading(
        f"Available Tools ({state.available_tools})", theme, leading_blank=False
    )
    names = state.available_tool_names
    if not names:
        return [header, Text("  No active session with tools\n", style=theme.banner_dim)]
    tools_table = Table(box=None, show_header=False, padding=(0, 2))
    tools_table.add_column("Tool", style=theme.banner_text)
    tools_table.add_column("Tool", style=theme.banner_text)
    tools_table.add_column("Tool", style=theme.banner_text)
    for i in range(0, len(names), 3):
        row = [escape(names[i]) if i < len(names) else ""]
        row.append(escape(names[i + 1]) if i + 1 < len(names) else "")
        row.append(escape(names[i + 2]) if i + 2 < len(names) else "")
        tools_table.add_row(*row)
    return [header, tools_table]


def _toolset_availability_lines(state: DashboardState, theme: Theme) -> list[RenderableType]:
    """Toolset load status from the banner snapshot; empty without a snapshot."""
    availability = state.toolset_availability
    if not availability.enabled_toolsets and not availability.unavailable_toolsets:
        return []
    line = Text("\n  Toolsets: ", style=theme.ui_label)
    line.append(f"{len(availability.enabled_toolsets)} enabled", style=theme.banner_text)
    if availability.unavailable_toolsets:
        names = ", ".join(sanitize_terminal_text(n) for n in availability.unavailable_toolsets)
        line.append(f" · unavailable: {names}", style=theme.ui_warn)
    extras = [
        f"{availability.lazy_tool_count} lazy" if availability.lazy_tool_count else "",
        f"{availability.disabled_tool_count} disabled" if availability.disabled_tool_count else "",
    ]
    configured = [part for part in extras if part]
    if configured:
        line.append(f" · {' · '.join(configured)}", style=theme.banner_dim)
    line.append("\n")
    return [line]


def _background_processes_section(state: DashboardState, theme: Theme) -> list[RenderableType]:
    header = section_heading(
        f"Background Processes ({len(state.background_processes)})", theme, leading_blank=False
    )
    if not state.background_processes:
        return [header, Text("  No running background processes\n", style=theme.banner_dim)]
    process_table = Table(box=None, show_header=True, padding=(0, 1))
    process_table.add_column("Session", style=theme.ui_label, min_width=14)
    process_table.add_column("PID", justify="right", style=theme.ui_accent, min_width=5)
    process_table.add_column("Purpose", style=theme.banner_text, min_width=8)
    process_table.add_column("Port", justify="right", style=theme.banner_dim, min_width=5)
    process_table.add_column("Profile", style=theme.banner_dim, min_width=7)
    process_table.add_column("Notify", style=theme.banner_text, min_width=6)
    process_table.add_column("Watch", style=theme.banner_dim, min_width=8)
    process_table.add_column("Started", style=theme.banner_dim, min_width=8)
    process_table.add_column("Command", style=theme.banner_text, ratio=1)
    for process in state.background_processes:
        process_table.add_row(
            escape(process.session_id),
            _pid_label(process.pid, process.alive),
            escape(process.purpose) or "—",
            str(process.port) if process.port else "—",
            escape(process.profile) or "—",
            "Yes" if process.notify_on_complete else "No",
            _watch_summary(process.watch_patterns, process.watcher_interval),
            _started_label(process.started_at),
            escape(process.command),
        )
    return [header, process_table]


def _checkpoints_section(state: DashboardState, theme: Theme) -> list[RenderableType]:
    header = section_heading(f"Checkpoints ({len(state.checkpoints)})", theme, leading_blank=False)
    if not state.checkpoints:
        return [header, Text("  No filesystem checkpoints\n", style=theme.banner_dim)]
    checkpoint_table = Table(box=None, show_header=True, padding=(0, 1))
    checkpoint_table.add_column("Workdir", style=theme.ui_label, min_width=16)
    checkpoint_table.add_column("Commits", justify="right", style=theme.ui_accent, min_width=7)
    checkpoint_table.add_column("Latest", style=theme.banner_text, ratio=1)
    checkpoint_table.add_column("When", style=theme.banner_dim, min_width=8)
    for checkpoint in state.checkpoints:
        checkpoint_table.add_row(
            escape(checkpoint.workdir_name or checkpoint.repo_id),
            str(checkpoint.commit_count),
            escape(checkpoint.last_reason) if checkpoint.last_reason else "—",
            _started_label(checkpoint.last_checkpoint_at or 0.0),
        )
    return [header, checkpoint_table]


def _pid_label(pid: int, alive: bool) -> str:
    """PID cell, marked with ✗ when the recorded pid is gone."""
    if not pid:
        return "—"
    return str(pid) if alive else f"{pid} ✗"


def _watch_summary(patterns: list[str], watcher_interval: int) -> str:
    if not patterns:
        return "—"
    count = len(patterns)
    if watcher_interval > 0:
        return f"{count} @{watcher_interval}s"
    return str(count)


def _started_label(started_at: float) -> str:
    if started_at <= 0:
        return "—"
    try:
        return time.strftime("%H:%M:%S", time.localtime(started_at))
    except (OverflowError, OSError, ValueError):
        return "—"
