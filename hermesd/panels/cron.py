from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import (
    CronExecution,
    CronExecutionsState,
    CronFireClaimState,
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

# Upstream's own per-job wording (hermes_cli/cron.py:119). Rendering the raw
# `last_dispatch.kind` instead would show `catch_up` and `late` as the same
# severity, when one means "slipped a few minutes" and the other means
# "missed fires were accumulated and skipped, then ran once now".
_DISPATCH_KIND_LABELS = {"catch_up": "catch-up after missed fire", "late": "late"}

# jobs.json ``last_status`` is a five-value vocabulary (``cron/jobs.py:2214-2244``
# plus the explicit statuses the scheduler records: ``delivery_queued``
# ``cron/scheduler.py:2722-2723``, ``blocked_config`` ``:2726-2727``). Folding
# either into "error" would tell an operator to retry a run that does not need
# one — a queued notice left the process unverified (resending would duplicate
# it) and a preflight block needs a config fix, not a rerun.
_LAST_STATUS_LABELS = {
    "delivery_queued": "delivery_queued (unverified — do not resend)",
    "blocked_config": "blocked_config (preflight block)",
}
_LAST_STATUS_STYLES = {
    "error": "ui_error",
    "delivery_failed": "ui_warn",
    "delivery_queued": "ui_warn",
    "blocked_config": "ui_warn",
}

# Incident lifecycle is detected -> alerted -> closed (``cron/incidents.py:1-9``):
# ``alerted`` is set only when a failure ping actually left the process
# (``cron/scheduler.py:2745-2746``). An open row still in ``detected`` therefore
# records no *delivered* failure ping — but that alone does not prove the alert
# delivery path is broken, because upstream also leaves the row in ``detected``
# when the failure notice was only queued (``cron/scheduler.py:2516-2528``).
# And because ``acked_at`` is only ever written together with ``closed_at``
# (``cron/incidents.py:186-195``), it can never be acknowledged away. An unseen
# state is kept verbatim rather than folded in.
_INCIDENT_STATE_LABELS = {
    "detected": "no delivered failure ping",
    "alerted": "alerted",
}
_INCIDENT_STATE_STYLES = {
    "detected": "ui_error",
    "alerted": "ui_warn",
}
_INCIDENT_ACK_NOTE = (
    "  detected = no delivered failure ping recorded; upstream marks a row\n"
    "  alerted only when the ping actually leaves the process, and a notice can\n"
    "  also sit queued (cron/scheduler.py:2516-2528) without the row moving — a\n"
    "  detected row is an alert to chase, not proof the delivery path is broken.\n"
    "  acked_at is only ever set together with closed_at — open incidents can\n"
    "  never be acknowledged."
)

# Rendered on every detail pass: both catch-up markers are best effort and the
# error marker is deleted on recovery, so a quiet panel is not a healthy cron.
_CATCH_UP_ABSENCE_NOTE = (
    "Absence is not proof of health: the counter is written best effort and stays flat "
    "while catch-up is off, and ticker_last_error is deleted on the next clean tick."
)


def _fmt_age(age: float | None) -> str:
    """Compact age label, or the panel's placeholder when the age is unknown."""
    if age is None:
        return "—"
    return fmt_age_seconds(max(0, int(age)))


def _fmt_error_age(age: float | None) -> str:
    """Age phrase for a recorded failure; the stamp can be missing while the message is not."""
    return f"{_fmt_age(age)} ago" if age is not None else "at an unknown time"


def _job_markers(job: CronJob) -> str:
    """Short compact-row glyphs for a live/dead fire claim, a pending slot, a
    failure streak, and a paused job."""
    markers = []
    if job.fire_claim_state == CronFireClaimState.RUNNING:
        markers.append("▶")
    elif job.fire_claim_state == CronFireClaimState.ABANDONED_RUN:
        markers.append("✗run")
    if job.pending_slot_scheduled_at:
        markers.append("⧗")
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
    # Line budget: only current problems earn a compact line. A non-zero lifetime
    # catch-up counter is history, so it stays in the detail view.
    if c.catch_up_missed_disabled:
        lines.append("  ⚠ Catch-up off: missed runs are skipped\n", style=theme.ui_warn)
    if c.ticker_error_recorded:
        lines.append(
            f"  ⚠ Tick error {_fmt_error_age(c.ticker_last_error_age_seconds)}\n",
            style=theme.ui_error,
        )
    fire_failed = [j for j in c.jobs if j.last_fire_error]
    if fire_failed:
        newest_fire_error_age = min(
            (
                job.last_fire_error_age_seconds
                for job in fire_failed
                if job.last_fire_error_age_seconds is not None
            ),
            default=None,
        )
        lines.append(
            f"  ⚠ Fire forward failed on {len(fire_failed)} job(s)  "
            f"newest {_fmt_error_age(newest_fire_error_age)}\n",
            style=theme.ui_error,
        )
    if executions.open_incident_count:
        lines.append("  Incidents: ", style=theme.ui_label)
        # ``acked_at`` is written only together with ``closed_at`` upstream, so an
        # open incident is always unacked in this schema; the qualifier is only
        # rendered when it distinguishes something.
        if executions.unacked_incident_count != executions.open_incident_count:
            lines.append(
                f"{executions.open_incident_count} open "
                f"({executions.unacked_incident_count} unacked)\n",
                style=theme.ui_error,
            )
        else:
            lines.append(f"{executions.open_incident_count} open\n", style=theme.ui_error)
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
    sections.extend(_catch_up_section(c, theme))

    if c.jobs:
        stats_by_job = {stats.job_id: stats for stats in executions.job_stats}
        sections.append(_jobs_table(c, stats_by_job, theme))
        flag_lines = [line for line in (_job_flags_line(j, theme) for j in c.jobs) if line]
        if flag_lines:
            sections.append(section_heading("Job Detail", theme))
            sections.extend(flag_lines)
        delivery_lines = [
            line
            for line in (_job_delivery_line(j, stats_by_job.get(j.job_id), theme) for j in c.jobs)
            if line
        ]
        if delivery_lines:
            sections.append(section_heading("Delivery Outcomes (24h)", theme))
            sections.extend(delivery_lines)
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


def _catch_up_policy_label(c: CronState) -> str:
    """The configured `cron.catch_up_missed`, labelled with how it came to be set."""
    if not c.catch_up_missed_set:
        return "catch_up_missed not set in config.yaml (upstream default: catch up)"
    if c.catch_up_missed:
        return "catch_up_missed: true (catch up)"
    return "catch_up_missed: false (missed runs are skipped)"


def _catch_up_counter_label(c: CronState) -> str:
    """The observed lifetime counter, never rendered as a rate or an age.

    An unobserved marker is reported as unobserved: upstream collapses "absent"
    and "0" into the same zero, and the write is best effort, so a missing file
    must not become a claim that nothing was missed.
    """
    if not c.catch_up_occurrences_recorded:
        return "no counter observed"
    return f"{c.catch_up_occurrences} recorded (lifetime total, no timestamp — not a rate)"


def _catch_up_section(c: CronState, theme: Theme) -> list[RenderableType]:
    """Configured catch-up policy, the observed counter, and any recorded tick error."""
    body = Text("  ")
    body.append("Policy: ", style=theme.ui_label)
    body.append(_catch_up_policy_label(c), style=theme.banner_text)
    if c.catch_up_missed_disabled:
        body.append(
            "\n  ⚠ Missed recurring runs beyond the grace window are dropped, and the counter"
            " below stays flat while they are.",
            style=theme.ui_warn,
        )
    body.append("\n  ")
    body.append("Catch-ups: ", style=theme.ui_label)
    body.append(_catch_up_counter_label(c), style=theme.banner_text)
    if c.ticker_error_recorded:
        body.append("\n  ⚠ Last tick error ", style=theme.ui_error)
        body.append(_fmt_error_age(c.ticker_last_error_age_seconds), style=theme.ui_error)
        body.append(": ", style=theme.ui_error)
        # Redaction happened in the collector; this is the terminal-control half,
        # because the message is an arbitrary exception string.
        body.append(sanitize_terminal_text(c.ticker_last_error), style=theme.ui_error)
    body.append(f"\n  {_CATCH_UP_ABSENCE_NOTE}\n", style=theme.banner_dim)
    return [section_heading("Missed-Run Catch-Up", theme), body]


def _dispatch_flag(job: CronJob) -> str:
    """`last_dispatch` lateness with the kind labelled rather than passed through.

    An unseen kind is kept verbatim (sanitized) instead of bucketed, so a value a
    newer agent adds is displayed rather than silently reading as "late".
    """
    kind = sanitize_terminal_text(job.dispatch_kind)
    label = _DISPATCH_KIND_LABELS.get(kind) or kind or "dispatch"
    return f"{label} {job.dispatch_lateness_seconds:.1f}s"


def _window_counters(stats: CronJobExecutionStats | None) -> str:
    """`7✓ 2✗ 1▶ 1?` summary of a job's last 24 hours, or the placeholder.

    The `?` bucket is deliberately visible: an unresolved outcome is neither a
    success nor a failure, and hiding it would make the row look complete.
    """
    if stats is None:
        return "—"
    parts = []
    if stats.completed_24h:
        parts.append(f"{stats.completed_24h}✓")
    if stats.failed_24h:
        parts.append(f"{stats.failed_24h}✗")
    if stats.running_24h:
        parts.append(f"{stats.running_24h}▶")
    if stats.unknown_24h:
        parts.append(f"{stats.unknown_24h}?")
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
        window = [_window_counters(stats_by_job.get(j.job_id))] if show_window else []
        table.add_row(
            sym,
            escape(j.name or j.job_id[:8]),
            escape(j.schedule_display),
            escape(j.delivery_target_label or j.deliver or "—"),
            Text(sanitize_terminal_text(j.state), style=state_color),
            _last_status_cell(j, theme),
            *window,
            _job_error_label(j, stats_by_job.get(j.job_id)),
        )
    return table


def _last_status_cell(job: CronJob, theme: Theme) -> Text:
    """The ``last_status`` cell: delivery and config states keep their own words.

    A status a newer agent adds is displayed verbatim (sanitized) rather than
    bucketed into any known severity.
    """
    status = job.last_status or ""
    if not status:
        return Text("—", style=theme.banner_text)
    label = _LAST_STATUS_LABELS.get(status, status)
    style = getattr(theme, _LAST_STATUS_STYLES.get(status, "banner_text"))
    return Text(sanitize_terminal_text(label), style=style)


def _job_error_label(job: CronJob, stats: CronJobExecutionStats | None) -> str:
    """jobs.json ``last_error``, else the executions.db excerpt for the job."""
    error = job.last_error or (stats.last_error_excerpt if stats is not None else "")
    return escape(error[:80]) if error else "—"


def _job_flags_line(j: CronJob, theme: Theme) -> Text | None:
    """One line of the jobs.json fields too narrow to earn a table column."""
    parts = []
    if j.fire_claim_state == CronFireClaimState.RUNNING:
        parts.append(f"running now (claim {_fmt_age(j.fire_claim_age_seconds)} old)")
    elif j.fire_claim_state == CronFireClaimState.ABANDONED_RUN:
        # The claim outlived upstream's 300 s TTL without a run outcome: the
        # runner died mid-run and never cleared or heartbeated it.
        parts.append(f"abandoned run (claim {_fmt_age(j.fire_claim_age_seconds)} old)")
    if j.pending_slot_scheduled_at:
        stamp = ""
        if j.pending_slot_age_seconds is not None:
            stamp = f", stamped {_fmt_age(j.pending_slot_age_seconds)} ago"
        parts.append(
            f"pending slot {sanitize_terminal_text(fmt_iso_timestamp(j.pending_slot_scheduled_at))}"
            f"{stamp} — may never have run"
        )
    if j.last_fire_error:
        parts.append(
            f"fire forward failed {_fmt_error_age(j.last_fire_error_age_seconds)}: "
            f"{sanitize_terminal_text(j.last_fire_error)}"
        )
    if j.failure_streak:
        parts.append(f"streak {j.failure_streak}")
    if j.paused:
        reason = sanitize_terminal_text(j.paused_reason)
        parts.append(f"paused: {reason}" if reason else "paused")
    if j.last_delivery_error:
        parts.append(f"delivery: {sanitize_terminal_text(j.last_delivery_error[:80])}")
    if j.preflight_alerted:
        parts.append("config-block alert sent (alert-once)")
    if not j.model and j.model_snapshot:
        # An unpinned job runs whatever the snapshot resolved at creation; the
        # panel would otherwise let an operator assume a pin that is not there.
        parts.append(f"model {sanitize_terminal_text(j.model_snapshot)} (unpinned snapshot)")
    if not j.provider and j.provider_snapshot:
        parts.append(f"provider {sanitize_terminal_text(j.provider_snapshot)} (unpinned snapshot)")
    if j.dispatch_lateness_seconds is not None:
        parts.append(_dispatch_flag(j))
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
    retention = _retention_line(executions, theme)
    if retention is not None:
        sections.append(retention)
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
        sections.append(Text(f"{_INCIDENT_ACK_NOTE}\n", style=theme.banner_dim))
    return sections


def _retention_line(executions: CronExecutionsState, theme: Theme) -> Text | None:
    """Qualify the aggregates as *recorded* attempts, with the observed span.

    Upstream prunes terminal history to a fixed record cap. Reaching the cap
    proves older terminal runs were dropped; it does not prove any particular 24h
    window is incomplete, and staying under it does not prove full coverage. The
    wording states only what the retained table can support.
    """
    if not executions.db_present or not executions.retained_total_count:
        return None
    line = Text("\n  ")
    line.append("Recorded attempts: ", style=theme.ui_label)
    line.append(f"{executions.retained_total_count} retained", style=theme.banner_text)
    line.append(f" ({executions.retained_terminal_count} terminal)", style=theme.banner_dim)
    if executions.oldest_claimed_age_seconds is not None:
        line.append(
            f"  spanning {_fmt_age(executions.newest_claimed_age_seconds)}"
            f" to {_fmt_age(executions.oldest_claimed_age_seconds)} ago",
            style=theme.banner_dim,
        )
    if executions.at_retention_cap:
        line.append(
            f"\n  ⚠ at the {executions.retention_cap}-record retention cap — older terminal runs"
            " are pruned, so these counts cover recorded attempts only. A capped history does"
            " not by itself make the 24h window incomplete.\n",
            style=theme.ui_warn,
        )
    else:
        line.append("\n")
    return line


def _recent_executions_table(executions: CronExecutionsState, theme: Theme) -> Table:
    # An older schema records no delivery outcome at all. Only widen the table
    # when there is something to say, and never let an empty column imply that a
    # completed run was delivered.
    show_delivery = any(run.delivery_outcome or run.handoff_pending for run in executions.recent)
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Job", style=theme.banner_text)
    table.add_column("Status", style=theme.ui_label)
    if show_delivery:
        table.add_column("Delivery", style=theme.ui_label)
    table.add_column("Started", style=theme.banner_dim)
    table.add_column("Duration", style=theme.banner_dim)
    table.add_column("Error", style=theme.ui_error)

    for run in executions.recent:
        status_color = theme.ui_error if run.status == "failed" else theme.banner_text
        delivery = [_delivery_cell(run, theme)] if show_delivery else []
        table.add_row(
            escape(run.job_name or run.job_id or "—"),
            Text(sanitize_terminal_text(run.status) or "—", style=status_color),
            *delivery,
            _fmt_age(run.started_age_seconds),
            _fmt_age(run.duration_seconds),
            escape(run.error_excerpt) if run.error_excerpt else "—",
        )
    return table


def _delivery_cell(run: CronExecution, theme: Theme) -> Text:
    """One run's recorded delivery outcome, kept visibly apart from its status."""
    cell = Text()
    if run.delivery_outcome:
        cell.append(sanitize_terminal_text(run.delivery_outcome))
    else:
        cell.append("unrecorded", style=theme.banner_dim)
    if run.handoff_pending:
        cell.append(" ⤵ handoff", style=f"bold {theme.ui_warn}")
    return cell


def _job_delivery_line(
    j: CronJob, stats: CronJobExecutionStats | None, theme: Theme
) -> Text | None:
    """Per-job 24h delivery outcomes, separate from the execution counters.

    A completed execution is not a delivered notification: hermes-agent suppresses
    delivery for silent runs and records the outcome apart from the run status, so
    the two are never collapsed into one number.
    """
    if stats is None or not stats.delivery_tracked:
        return None
    parts = [f"{outcome} {count}" for outcome, count in sorted(stats.delivery_outcomes_24h.items())]
    if stats.delivery_unrecorded_24h:
        parts.append(f"unrecorded {stats.delivery_unrecorded_24h}")
    if stats.handoff_pending_24h:
        parts.append(f"handoff pending {stats.handoff_pending_24h}")
    if not parts:
        return None
    line = Text()
    line.append(
        f"  {sanitize_terminal_text(j.name or j.job_id[:8] or '—')}: ",
        style=theme.ui_label,
    )
    # Text.append does not parse markup, so the DB-sourced outcome names are
    # sanitized like the job name above rather than escaped — escaping would
    # render a literal backslash before any '['.
    line.append(
        "  ".join(sanitize_terminal_text(part) for part in parts) + "\n",
        style=theme.banner_text,
    )
    return line


def _incidents_table(executions: CronExecutionsState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Job", style=theme.banner_text)
    table.add_column("State", style=theme.ui_warn)
    table.add_column("Type", style=theme.ui_label)
    table.add_column("First", style=theme.banner_dim)
    table.add_column("Last", style=theme.banner_dim)
    table.add_column("Error", style=theme.ui_error)

    for incident in executions.open_incidents:
        state = sanitize_terminal_text(incident.state)
        table.add_row(
            escape(incident.job_name or incident.job_id or "—"),
            Text(
                _INCIDENT_STATE_LABELS.get(state, state) or "—",
                style=getattr(theme, _INCIDENT_STATE_STYLES.get(state, "banner_text")),
            ),
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
