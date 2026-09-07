from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import (
    CronExecutionsState,
    CronJob,
    CronJobExecutionStats,
    CronState,
    CronTickerHealth,
    DashboardState,
)
from hermesd.panels.formatting import (
    escape_terminal_text as escape,
)
from hermesd.panels.formatting import (
    fmt_age_seconds,
    fmt_iso_timestamp,
    sanitize_terminal_text,
    section_heading,
)
from hermesd.theme import Theme

_TICKER_STYLES = {
    CronTickerHealth.OK: "ui_ok",
    CronTickerHealth.FAILING: "ui_warn",
    CronTickerHealth.STALE: "ui_error",
    CronTickerHealth.UNKNOWN: "banner_dim",
}


def _fmt_age(age: float | None) -> str:
    """Compact age label, or the panel's placeholder when the age is unknown."""
    if age is None:
        return "—"
    return fmt_age_seconds(max(0, int(age)))


def _job_markers(job: CronJob) -> str:
    """Short compact-row glyphs for a failure streak and a paused job."""
    markers = []
    if job.failure_streak > 0:
        markers.append(f"✗{job.failure_streak}")
    if job.paused:
        markers.append("⏸")
    return " ".join(markers)


def render_cron(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    c = state.cron
    executions = state.cron_executions
    lines = Text()
    if c.last_tick_ago_seconds is not None:
        lines.append("  Last tick: ", style=theme.ui_label)
        tick_age = max(0, int(c.last_tick_ago_seconds))
        lines.append(f"{tick_age}s ago\n", style=theme.ui_accent)
    else:
        lines.append("  Last tick: ", style=theme.ui_label)
        lines.append("—\n", style=theme.banner_dim)
    lines.append("  Ticker: ", style=theme.ui_label)
    lines.append(
        f"{c.ticker_health.value}\n",
        style=getattr(theme, _TICKER_STYLES[c.ticker_health]),
    )
    if executions.open_incident_count:
        lines.append("  Incidents: ", style=theme.ui_label)
        lines.append(
            f"{executions.open_incident_count} open "
            f"({executions.unacked_incident_count} unacked)\n",
            style=theme.ui_error,
        )
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
            markers = _job_markers(j)
            if markers:
                lines.append(f" {markers}", style=theme.ui_warn)
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
    executions = state.cron_executions
    sections: list[RenderableType] = [_cron_header(c, theme)]

    if c.jobs:
        stats_by_job = {stats.job_id: stats for stats in executions.job_stats}
        sections.append(_jobs_table(c, stats_by_job, theme))
        flag_lines = [line for line in (_job_flags_line(j, theme) for j in c.jobs) if line]
        if flag_lines:
            sections.append(section_heading("Job Detail", theme))
            sections.extend(flag_lines)
        if any(j.next_run_at or j.latest_output_excerpt or j.silent_run for j in c.jobs):
            sections.append(section_heading("Latest Output", theme))
            sections.extend(_latest_output_line(j, theme) for j in c.jobs)
    else:
        sections.append(Text("  No cron jobs configured\n", style=theme.banner_dim))

    sections.extend(_executions_sections(executions, theme))

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
    header.append("Ticker: ", style=theme.ui_label)
    header.append(c.ticker_health.value, style=getattr(theme, _TICKER_STYLES[c.ticker_health]))
    header.append(
        f"  (beat {_fmt_age(c.ticker_heartbeat_age_seconds)}, "
        f"success {_fmt_age(c.ticker_last_success_age_seconds)})\n\n",
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


def _window_counters(stats: CronJobExecutionStats | None) -> str:
    """`7✓ 2✗ 1▶` summary of a job's last 24 hours, or the placeholder."""
    if stats is None:
        return "—"
    parts = []
    if stats.completed_24h:
        parts.append(f"{stats.completed_24h}✓")
    if stats.failed_24h:
        parts.append(f"{stats.failed_24h}✗")
    if stats.running_24h:
        parts.append(f"{stats.running_24h}▶")
    return " ".join(parts) if parts else "—"


def _jobs_table(
    c: CronState,
    stats_by_job: dict[str, CronJobExecutionStats],
    theme: Theme,
) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("", width=2)
    table.add_column("Name", style=theme.banner_text)
    table.add_column("Schedule", style=theme.banner_dim)
    table.add_column("Deliver", style=theme.ui_label)
    table.add_column("State", style=theme.ui_label)
    table.add_column("Last", style=theme.banner_text)
    # Only widen the row when executions.db actually has history to show.
    show_window = bool(stats_by_job)
    if show_window:
        table.add_column("24h", style=theme.banner_dim)
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
        window = [_window_counters(stats_by_job.get(j.job_id))] if show_window else []
        table.add_row(
            sym,
            escape(j.name or j.job_id[:8]),
            escape(j.schedule_display),
            escape(j.delivery_target_label or j.deliver or "—"),
            Text(sanitize_terminal_text(j.state), style=state_color),
            Text(sanitize_terminal_text(last), style=last_style),
            *window,
            _job_error_label(j, stats_by_job.get(j.job_id)),
        )
    return table


def _job_error_label(job: CronJob, stats: CronJobExecutionStats | None) -> str:
    """jobs.json ``last_error``, else the executions.db excerpt for the job."""
    error = job.last_error or (stats.last_error_excerpt if stats is not None else "")
    return escape(error[:80]) if error else "—"


def _job_flags_line(j: CronJob, theme: Theme) -> Text | None:
    """One line of the jobs.json fields too narrow to earn a table column."""
    parts = []
    if j.failure_streak:
        parts.append(f"streak {j.failure_streak}")
    if j.paused:
        reason = sanitize_terminal_text(j.paused_reason)
        parts.append(f"paused: {reason}" if reason else "paused")
    if j.last_delivery_error:
        parts.append(f"delivery: {sanitize_terminal_text(j.last_delivery_error[:80])}")
    if j.dispatch_lateness_seconds is not None:
        kind = sanitize_terminal_text(j.dispatch_kind) or "dispatch"
        parts.append(f"{kind} {j.dispatch_lateness_seconds:.1f}s")
    if j.repeat_completed or j.repeat_times is not None:
        parts.append(f"repeat {j.repeat_completed}/{j.repeat_times or '∞'}")
    if j.no_agent:
        parts.append("no-agent")
    if not parts:
        return None
    line = Text()
    line.append(
        f"  {sanitize_terminal_text(j.name or j.job_id[:8] or '—')}: ",
        style=theme.ui_label,
    )
    line.append("  ".join(parts) + "\n", style=theme.banner_text)
    return line


def _executions_sections(executions: CronExecutionsState, theme: Theme) -> list[RenderableType]:
    """Recent-execution and open-incident tables, or a single 'no data' line."""
    if not executions.recent and not executions.open_incidents:
        return [Text("\n  No execution history\n", style=theme.banner_dim)]
    sections: list[RenderableType] = []
    if executions.recent:
        sections.append(section_heading("Recent Executions", theme))
        sections.append(_recent_executions_table(executions, theme))
    if executions.open_incidents:
        sections.append(
            section_heading(
                f"Open Incidents ({executions.open_incident_count}, "
                f"{executions.unacked_incident_count} unacked)",
                theme,
            )
        )
        sections.append(_incidents_table(executions, theme))
    return sections


def _recent_executions_table(executions: CronExecutionsState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Job", style=theme.banner_text)
    table.add_column("Status", style=theme.ui_label)
    table.add_column("Started", style=theme.banner_dim)
    table.add_column("Duration", style=theme.banner_dim)
    table.add_column("Error", style=theme.ui_error)

    for run in executions.recent:
        status_color = theme.ui_error if run.status == "failed" else theme.banner_text
        table.add_row(
            escape(run.job_name or run.job_id or "—"),
            Text(sanitize_terminal_text(run.status) or "—", style=status_color),
            _fmt_age(run.started_age_seconds),
            _fmt_age(run.duration_seconds),
            escape(run.error_excerpt) if run.error_excerpt else "—",
        )
    return table


def _incidents_table(executions: CronExecutionsState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Job", style=theme.banner_text)
    table.add_column("State", style=theme.ui_warn)
    table.add_column("Type", style=theme.ui_label)
    table.add_column("First", style=theme.banner_dim)
    table.add_column("Last", style=theme.banner_dim)
    table.add_column("Error", style=theme.ui_error)

    for incident in executions.open_incidents:
        table.add_row(
            escape(incident.job_name or incident.job_id or "—"),
            escape(incident.state) or "—",
            escape(incident.failure_type) or "—",
            _fmt_age(incident.first_seen_age_seconds),
            _fmt_age(incident.last_seen_age_seconds),
            escape(incident.error_excerpt) if incident.error_excerpt else "—",
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
