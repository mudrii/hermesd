from __future__ import annotations

import rich.box
from rich.console import Group
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
    sections = []

    # Table 1: Tool call stats
    calls_header = Text()
    calls_header.append(f"Tool Calls ({state.total_tool_calls} total)\n", style=f"bold {theme.ui_label}")
    sections.append(calls_header)

    if state.tool_stats:
        calls_table = Table(box=None, show_header=True, padding=(0, 2))
        calls_table.add_column("Session", style=theme.ui_label)
        calls_table.add_column("Calls", justify="right", style=theme.ui_accent)
        for ts in state.tool_stats:
            calls_table.add_row(ts.name, str(ts.call_count))
        sections.append(calls_table)
    else:
        sections.append(Text("  No tool call data\n", style=theme.banner_dim))

    # Table 2: Available tools
    sections.append(Text("\n"))
    tools_header = Text()
    tools_header.append(f"Available Tools ({state.available_tools})\n", style=f"bold {theme.ui_label}")
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

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[4] Tools[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )
