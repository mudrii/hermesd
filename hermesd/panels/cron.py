from __future__ import annotations

import rich.box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_cron(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    c = state.cron
    lines = Text()
    if c.last_tick_ago_seconds is not None:
        lines.append("  Last tick: ", style=theme.ui_label)
        lines.append(f"{int(c.last_tick_ago_seconds)}s ago\n", style=theme.ui_accent)
    else:
        lines.append("  Last tick: ", style=theme.ui_label)
        lines.append("—\n", style=theme.banner_dim)
    lines.append("  Jobs: ", style=theme.ui_label)
    lines.append(f"{c.job_count}", style=theme.banner_text)
    lines.append("  Errors: ", style=theme.ui_label)
    err_color = theme.ui_error if c.error_count > 0 else theme.banner_text
    lines.append(f"{c.error_count}", style=err_color)

    if c.jobs:
        lines.append("\n")
        for j in c.jobs[:2]:
            sym = "●" if j.enabled else "○"
            color = theme.ui_ok if j.state == "scheduled" else theme.banner_dim
            lines.append(f"  {sym} ", style=color)
            lines.append(f"{j.name or j.job_id[:8]}", style=theme.banner_text)
            lines.append(f" {j.schedule_display}", style=theme.banner_dim)

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[6] Cron[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    c = state.cron
    sections = []

    header = Text()
    if c.last_tick_ago_seconds is not None:
        header.append(f"Last tick: {int(c.last_tick_ago_seconds)}s ago", style=theme.ui_accent)
    else:
        header.append("Last tick: —", style=theme.banner_dim)
    header.append(f"   Jobs: {c.job_count}   Errors: {c.error_count}\n\n", style=theme.banner_text)
    sections.append(header)

    if c.jobs:
        table = Table(box=None, show_header=True, padding=(0, 2))
        table.add_column("", width=2)
        table.add_column("Name", style=theme.banner_text)
        table.add_column("Schedule", style=theme.banner_dim)
        table.add_column("State", style=theme.ui_label)
        table.add_column("Next Run", style=theme.banner_dim)
        table.add_column("Last", style=theme.banner_text)

        for j in c.jobs:
            sym = Text("●", style=f"bold {theme.ui_ok}") if j.enabled else Text("○", style=theme.banner_dim)
            state_color = theme.ui_ok if j.state == "scheduled" else theme.ui_warn
            next_run = j.next_run_at[:19] if j.next_run_at else "—"
            last = j.last_status or "—"
            last_style = theme.ui_error if last == "error" else theme.banner_text
            table.add_row(
                sym,
                j.name or j.job_id[:8],
                j.schedule_display,
                Text(j.state, style=state_color),
                next_run,
                Text(last, style=last_style),
            )
        sections.append(table)
    else:
        sections.append(Text("  No cron jobs configured\n", style=theme.banner_dim))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[6] Cron[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )
