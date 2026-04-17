from __future__ import annotations

import time

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
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
    for ts in state.tool_stats[:3]:
        lines.append(f"  {ts.name}", style=theme.ui_label)
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
    sections: list[RenderableType] = []

    # Table 1: Tool call stats
    calls_header = Text()
    calls_header.append(
        f"Tool Calls ({state.total_tool_calls} total)\n", style=f"bold {theme.ui_label}"
    )
    sections.append(calls_header)

    if state.tool_stats:
        calls_table = Table(box=None, show_header=True, padding=(0, 2))
        calls_table.add_column("Name", style=theme.ui_label)
        calls_table.add_column("Calls", justify="right", style=theme.ui_accent)
        for ts in state.tool_stats:
            calls_table.add_row(ts.name, str(ts.call_count))
        sections.append(calls_table)
    else:
        sections.append(Text("  No tool call data\n", style=theme.banner_dim))

    # Table 2: Available tools
    sections.append(Text("\n"))
    tools_header = Text()
    tools_header.append(
        f"Available Tools ({state.available_tools})\n", style=f"bold {theme.ui_label}"
    )
    sections.append(tools_header)

    if state.available_tool_names:
        tools_table = Table(box=None, show_header=False, padding=(0, 2))
        tools_table.add_column("Tool", style=theme.banner_text)
        tools_table.add_column("Tool", style=theme.banner_text)
        tools_table.add_column("Tool", style=theme.banner_text)
        names = state.available_tool_names
        for i in range(0, len(names), 3):
            row = [names[i] if i < len(names) else ""]
            row.append(names[i + 1] if i + 1 < len(names) else "")
            row.append(names[i + 2] if i + 2 < len(names) else "")
            tools_table.add_row(*row)
        sections.append(tools_table)
    else:
        sections.append(Text("  No active session with tools\n", style=theme.banner_dim))

    sections.append(Text("\n"))
    process_header = Text()
    process_header.append(
        f"Background Processes ({len(state.background_processes)})\n",
        style=f"bold {theme.ui_label}",
    )
    sections.append(process_header)

    if state.background_processes:
        process_table = Table(box=None, show_header=True, padding=(0, 1))
        process_table.add_column("Session", style=theme.ui_label, min_width=14)
        process_table.add_column("PID", justify="right", style=theme.ui_accent, min_width=5)
        process_table.add_column("Notify", style=theme.banner_text, min_width=6)
        process_table.add_column("Watch", style=theme.banner_dim, min_width=8)
        process_table.add_column("Started", style=theme.banner_dim, min_width=8)
        process_table.add_column("Command", style=theme.banner_text, ratio=1)
        for process in state.background_processes:
            process_table.add_row(
                process.session_id,
                str(process.pid) if process.pid else "—",
                "Yes" if process.notify_on_complete else "No",
                _watch_summary(process.watch_patterns, process.watcher_interval),
                _started_label(process.started_at),
                process.command,
            )
        sections.append(process_table)
    else:
        sections.append(Text("  No running background processes\n", style=theme.banner_dim))

    sections.append(Text("\n"))
    checkpoint_header = Text()
    checkpoint_header.append(
        f"Checkpoints ({len(state.checkpoints)})\n",
        style=f"bold {theme.ui_label}",
    )
    sections.append(checkpoint_header)

    if state.checkpoints:
        checkpoint_table = Table(box=None, show_header=True, padding=(0, 1))
        checkpoint_table.add_column("Workdir", style=theme.ui_label, min_width=16)
        checkpoint_table.add_column("Commits", justify="right", style=theme.ui_accent, min_width=7)
        checkpoint_table.add_column("Latest", style=theme.banner_text, ratio=1)
        checkpoint_table.add_column("When", style=theme.banner_dim, min_width=8)
        for checkpoint in state.checkpoints:
            checkpoint_table.add_row(
                checkpoint.workdir_name or checkpoint.repo_id,
                str(checkpoint.commit_count),
                checkpoint.last_reason or "—",
                _started_label(checkpoint.last_checkpoint_at or 0.0),
            )
        sections.append(checkpoint_table)
    else:
        sections.append(Text("  No filesystem checkpoints\n", style=theme.banner_dim))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[4] Tools[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


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
    return time.strftime("%H:%M:%S", time.localtime(started_at))
