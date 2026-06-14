from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import CuratorRun, DashboardState
from hermesd.theme import Theme

_TITLE = "\\[13] Curator"
_SUMMARY_MAX_CHARS = 600


def render_curator(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _delta_label(cur: CuratorRun) -> str:
    sign = "+" if cur.count_delta > 0 else ""
    return f"{cur.count_before} → {cur.count_after} ({sign}{cur.count_delta})"


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    cur = state.curator
    lines = Text()
    if not cur.run_present:
        lines.append("  No curation runs", style=theme.banner_dim)
    else:
        lines.append("  Last run: ", style=theme.ui_label)
        lines.append(f"{escape(cur.stamp)}\n", style=theme.banner_text)
        lines.append("  Skills: ", style=theme.ui_label)
        lines.append(f"{_delta_label(cur)}\n", style=theme.banner_text)
        lines.append("  Archived ", style=theme.ui_label)
        lines.append(str(cur.archived_count), style=theme.banner_text)
        lines.append("  Pruned ", style=theme.ui_label)
        lines.append(str(cur.pruned_count), style=theme.banner_text)
        if cur.llm_error:
            lines.append("  ⚠ error", style=theme.ui_warn)
    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]{_TITLE}[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    cur = state.curator
    if not cur.run_present:
        return Panel(
            Text("  No curation runs found", style=theme.banner_dim),
            title=f"[{theme.panel_title_style}]{_TITLE}[/]",
            title_align="left",
            border_style=theme.panel_border_style,
            box=rich.box.HORIZONTALS,
            padding=(1, 2),
        )

    summary = Table(box=None, show_header=False, padding=(0, 2))
    summary.add_column("Key", style=theme.ui_label)
    summary.add_column("Value", style=theme.banner_text)
    summary.add_row("Run", escape(cur.stamp))
    if cur.started_at:
        summary.add_row("Started", escape(cur.started_at))
    summary.add_row("Duration", f"{cur.duration_seconds:.1f}s")
    summary.add_row("Model", escape(cur.model) if cur.model else "—")
    summary.add_row("Provider", escape(cur.provider) if cur.provider else "—")
    summary.add_row("Skills", _delta_label(cur))
    summary.add_row("Archived", str(cur.archived_count))
    summary.add_row("Added", str(cur.added_count))
    summary.add_row("Pruned", str(cur.pruned_count))
    summary.add_row("Consolidated", str(cur.consolidated_count))
    summary.add_row("Tool Calls", str(cur.tool_calls_total))

    sections: list[RenderableType] = [summary]

    if cur.tool_call_counts:
        sections.append(Text("\nTool Calls\n", style=f"bold {theme.ui_label}"))
        tools = Table(box=None, show_header=True, padding=(0, 2))
        tools.add_column("Tool", style=theme.ui_label)
        tools.add_column("Calls", justify="right", style=theme.banner_text)
        for tool, count in sorted(cur.tool_call_counts.items()):
            tools.add_row(escape(tool), str(count))
        sections.append(tools)

    if cur.state_transitions:
        sections.append(Text("\nState Transitions\n", style=f"bold {theme.ui_label}"))
        transitions = Text()
        for transition in cur.state_transitions[:10]:
            transitions.append(f"  {escape(transition)}\n", style=theme.banner_dim)
        sections.append(transitions)

    if cur.llm_error:
        sections.append(Text("\nError\n", style=f"bold {theme.ui_error}"))
        sections.append(Text(f"  {escape(cur.llm_error)}", style=theme.ui_error))
    elif cur.llm_summary:
        sections.append(Text("\nSummary\n", style=f"bold {theme.ui_label}"))
        summary_text = cur.llm_summary[:_SUMMARY_MAX_CHARS]
        if len(cur.llm_summary) > _SUMMARY_MAX_CHARS:
            summary_text += "…"
        sections.append(Text(escape(summary_text), style=theme.banner_dim))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]{_TITLE}[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )
