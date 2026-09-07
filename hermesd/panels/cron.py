from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import CronJob, CronState, DashboardState
from hermesd.panels.formatting import (
    escape_terminal_text as escape,
)
from hermesd.panels.formatting import (
    fmt_iso_timestamp,
    sanitize_terminal_text,
    section_heading,
)
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
        tick_age = max(0, int(c.last_tick_ago_seconds))
        lines.append(f"{tick_age}s ago\n", style=theme.ui_accent)
    else:
        lines.append("  Last tick: ", style=theme.ui_label)
        lines.append("—\n", style=theme.banner_dim)
    lines.append("  Jobs: ", style=theme.ui_label)
    lines.append(f"{c.job_count}", style=theme.banner_text)
    lines.append("  Errors: ", style=theme.ui_label)
    err_color = theme.ui_error if c.error_count > 0 else theme.banner_text
    lines.append(f"{c.error_count}\n", style=err_color)
    lines.append("  Parallel: ", style=theme.ui_label)
    lines.append(str(c.max_parallel_jobs or "—"), style=theme.banner_text)

    if c.jobs:
        lines.append("\n")
        for j in c.jobs[:2]:
            sym = "●" if j.enabled else "○"
            color = theme.ui_ok if j.state == "scheduled" else theme.banner_dim
            lines.append(f"  {sym} ", style=color)
            lines.append(
                sanitize_terminal_text(j.name or j.job_id[:8] or "—"), style=theme.banner_text
            )
            lines.append(f" {sanitize_terminal_text(j.schedule_display)}", style=theme.banner_dim)

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
    sections: list[RenderableType] = [_cron_header(c, theme)]

    if c.jobs:
        sections.append(_jobs_table(c, theme))
        if any(j.next_run_at or j.latest_output_excerpt or j.silent_run for j in c.jobs):
            sections.append(section_heading("Latest Output", theme))
            sections.extend(_latest_output_line(j, theme) for j in c.jobs)
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


def _cron_header(c: CronState, theme: Theme) -> Text:
    header = Text()
    if c.last_tick_ago_seconds is not None:
        tick_age = max(0, int(c.last_tick_ago_seconds))
        header.append(f"Last tick: {tick_age}s ago", style=theme.ui_accent)
    else:
        header.append("Last tick: —", style=theme.banner_dim)
    header.append(f"   Jobs: {c.job_count}   Errors: {c.error_count}\n\n", style=theme.banner_text)
    header.append(
        f"Config: max_parallel={c.max_parallel_jobs or '—'} "
        f"wrap_response={'yes' if c.wrap_response else 'no'}\n\n",
        style=theme.banner_dim,
    )
    header.append(
        f"Provider: provider={escape(c.provider)}",
        style=theme.banner_dim,
    )
    if c.suggestion_count:
        header.append(f"  suggestions={c.suggestion_count}", style=theme.banner_text)
    if c.provider == "chronos" or c.chronos_configured:
        header.append(f"  chronos {_chronos_label(c)}", style=theme.banner_text)
    header.append("\n\n")
    return header


def _chronos_label(c: CronState) -> str:
    configured = "configured" if c.chronos_configured else "partial"
    parts = []
    if c.chronos_portal_configured:
        parts.append("portal")
    if c.chronos_callback_configured:
        parts.append("callback")
    if c.chronos_audience_configured:
        parts.append("audience")
    if c.chronos_jwks_configured:
        parts.append("jwks")
    suffix = f" ({', '.join(parts)})" if parts else ""
    return f"{configured}{suffix}"


def _jobs_table(c: CronState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("", width=2)
    table.add_column("Name", style=theme.banner_text)
    table.add_column("Schedule", style=theme.banner_dim)
    table.add_column("Deliver", style=theme.ui_label)
    table.add_column("State", style=theme.ui_label)
    table.add_column("Last", style=theme.banner_text)
    table.add_column("Error", style=theme.ui_error)

    for j in c.jobs:
        sym = (
            Text("●", style=f"bold {theme.ui_ok}")
            if j.enabled
            else Text("○", style=theme.banner_dim)
        )
        state_color = theme.ui_ok if j.state == "scheduled" else theme.ui_warn
        last = j.last_status or "—"
        last_style = theme.ui_error if last == "error" else theme.banner_text
        table.add_row(
            sym,
            escape(j.name or j.job_id[:8]),
            escape(j.schedule_display),
            escape(j.delivery_target_label or j.deliver or "—"),
            Text(sanitize_terminal_text(j.state), style=state_color),
            Text(sanitize_terminal_text(last), style=last_style),
            escape(j.last_error[:80]) if j.last_error else "—",
        )
    return table


def _latest_output_line(j: CronJob, theme: Theme) -> Text:
    line = Text()
    line.append(
        f"  {sanitize_terminal_text(j.name or j.job_id[:8] or '—')}: ",
        style=theme.ui_label,
    )
    if j.next_run_at:
        line.append(
            f"next {sanitize_terminal_text(fmt_iso_timestamp(j.next_run_at))}  ",
            style=theme.banner_dim,
        )
    if j.silent_run:
        line.append("[SILENT] ", style=theme.ui_warn)
    if j.latest_output_path:
        line.append(f"{sanitize_terminal_text(j.latest_output_path)}  ", style=theme.banner_dim)
    line.append(
        sanitize_terminal_text(j.latest_output_excerpt) or "—",
        style=theme.banner_text,
    )
    line.append("\n")
    return line
