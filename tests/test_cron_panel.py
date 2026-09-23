"""Tests for [6] Cron panel — job listing, compact and detail views."""

from __future__ import annotations

from hermesd.models import (
    CronExecution,
    CronExecutionsState,
    CronFireClaimState,
    CronIncident,
    CronJob,
    CronJobExecutionStats,
    CronJobUsage,
    CronModelSource,
    CronState,
    CronTickerHealth,
    CronUsageState,
    DashboardState,
)
from hermesd.panels import render_panel
from hermesd.panels.cron import _last_status_cell, render_cron
from hermesd.theme import Theme
from tests.conftest import render_to_str

CLEAR_SCREEN = "\x1b[2J"


def test_cron_compact_shows_job_count():
    state = DashboardState(
        cron=CronState(
            last_tick_ago_seconds=10.0,
            job_count=2,
            error_count=0,
            jobs=[
                CronJob(
                    name="daily-report",
                    schedule_display="every day at 9am",
                    state="scheduled",
                    enabled=True,
                ),
                CronJob(
                    name="cleanup", schedule_display="every 6h", state="scheduled", enabled=True
                ),
            ],
        ),
    )
    panel = render_panel(6, state, Theme(), detail=False)
    text = render_to_str(panel, width=100, no_color=True)
    assert "Jobs: 2" in text
    assert "daily-report" in text
    assert "every day at 9am" in text


def test_cron_compact_shows_errors():
    state = DashboardState(
        cron=CronState(
            job_count=1,
            error_count=1,
            jobs=[
                CronJob(name="broken", state="error", last_status="error"),
            ],
        ),
    )
    panel = render_panel(6, state, Theme(), detail=False)
    text = render_to_str(panel, width=100, no_color=True)
    assert "Errors: 1" in text


def test_cron_detail_shows_job_table():
    state = DashboardState(
        cron=CronState(
            last_tick_ago_seconds=5.0,
            job_count=2,
            max_parallel_jobs=3,
            wrap_response=True,
            jobs=[
                CronJob(
                    job_id="abc123",
                    name="meeting-reminder",
                    schedule_display="once in 2m",
                    state="scheduled",
                    enabled=True,
                    next_run_at="2026-04-09T18:21:49+08:00",
                ),
                CronJob(
                    job_id="def456",
                    name="daily-backup",
                    schedule_display="every day at 3am",
                    state="scheduled",
                    enabled=False,
                    last_status="ok",
                ),
            ],
        ),
    )
    panel = render_panel(6, state, Theme(), detail=True)
    text = render_to_str(panel, width=100, no_color=True)
    assert "meeting-reminder" in text
    assert "once in 2m" in text
    assert "daily-backup" in text
    assert "2026-04-09 18:21:49" in text
    assert "max_parallel=3" in text
    assert "wrap_response=yes" in text


def test_cron_detail_empty():
    state = DashboardState(cron=CronState())
    panel = render_panel(6, state, Theme(), detail=True)
    text = render_to_str(panel, width=100, no_color=True)
    assert "No cron jobs configured" in text


def test_cron_detail_shows_delivery_and_output_metadata():
    state = DashboardState(
        cron=CronState(
            job_count=1,
            jobs=[
                CronJob(
                    job_id="j1",
                    name="meeting-reminder",
                    schedule_display="once in 2m",
                    state="scheduled",
                    enabled=True,
                    deliver="telegram:My Group",
                    delivery_target_label="telegram:My Group",
                    latest_output_path="cron/output/j1/2026-04-09.md",
                    latest_output_excerpt="No changes to report.",
                    silent_run=True,
                )
            ],
        ),
    )
    panel = render_panel(6, state, Theme(), detail=True)
    text = render_to_str(panel, width=100, no_color=True)
    assert "telegram:My Group" in text
    assert "[SILENT]" in text
    assert "cron/output/j1/2026-04-09.md" in text
    assert "No changes to report." in text


def test_cron_detail_strips_ansi_control_sequences() -> None:
    job = CronJob(
        job_id="job1",
        name="nightly",
        latest_output_excerpt=f"out {CLEAR_SCREEN} done",
    )
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    rendered = render_to_str(render_cron(state, Theme(), detail=True))
    assert CLEAR_SCREEN not in rendered
    assert "done" in rendered


def test_cron_compact_strips_ansi_control_sequences() -> None:
    job = CronJob(job_id="job1", name=f"ni{CLEAR_SCREEN}ghtly", schedule_display="* * *")
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    rendered = render_to_str(render_cron(state, Theme()))
    assert CLEAR_SCREEN not in rendered
    assert "nightly" in rendered


def test_cron_compact_blank_job_name_falls_back_to_dash() -> None:
    job = CronJob(job_id="", name="", schedule_display="* * *")
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    rendered = render_to_str(render_cron(state, Theme()))
    assert "—" in rendered


MARKUP_BOMB = "[bold red]owned[/]"


def _executions_state(**kwargs) -> CronExecutionsState:
    return CronExecutionsState(**kwargs)


def test_cron_compact_shows_ticker_health() -> None:
    state = DashboardState(
        cron=CronState(
            ticker_health=CronTickerHealth.FAILING,
            ticker_heartbeat_age_seconds=12.0,
            ticker_last_success_age_seconds=900.0,
        )
    )
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    assert "Ticker: failing" in text


def test_cron_compact_ticker_unknown_renders_placeholder() -> None:
    state = DashboardState(cron=CronState())
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    # Never blank: an unknown ticker still reports a word, not an empty cell.
    assert "Ticker: unknown" in text


def test_cron_compact_shows_failure_streak_and_paused_markers() -> None:
    state = DashboardState(
        cron=CronState(
            job_count=2,
            jobs=[
                CronJob(job_id="a", name="flaky", schedule_display="every 5m", failure_streak=3),
                CronJob(
                    job_id="b",
                    name="held",
                    schedule_display="every 1h",
                    paused=True,
                    paused_reason="manual",
                ),
            ],
        )
    )
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    assert "✗3" in text
    assert "⏸" in text


def test_cron_compact_omits_markers_for_healthy_jobs() -> None:
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="a", name="fine", schedule_display="1h")])
    )
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    assert "✗" not in text
    assert "⏸" not in text


def test_cron_compact_shows_open_incident_counts() -> None:
    state = DashboardState(
        cron_executions=_executions_state(open_incident_count=2, unacked_incident_count=1)
    )
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    assert "Incidents: 2 open" in text
    assert "1 unacked" in text


def test_cron_compact_hides_incident_line_when_none_are_open() -> None:
    state = DashboardState(cron_executions=_executions_state(db_present=True))
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    assert "Incidents" not in text


def test_cron_detail_shows_ticker_ages() -> None:
    state = DashboardState(
        cron=CronState(
            ticker_health=CronTickerHealth.OK,
            ticker_heartbeat_age_seconds=15.0,
            ticker_last_success_age_seconds=120.0,
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=120, no_color=True)
    assert "Ticker: ok" in text
    assert "beat 15s" in text
    assert "success 2m" in text


def test_cron_detail_ticker_ages_fall_back_to_dashes() -> None:
    state = DashboardState(cron=CronState(ticker_health=CronTickerHealth.UNKNOWN))
    text = render_to_str(render_cron(state, Theme(), detail=True), width=120, no_color=True)
    assert "beat —" in text
    assert "success —" in text


def test_cron_detail_shows_per_job_24h_counters() -> None:
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(
            db_present=True,
            job_stats=[
                CronJobExecutionStats(
                    job_id="job-a",
                    completed_24h=7,
                    failed_24h=2,
                    running_24h=1,
                    last_status="failed",
                    last_duration_seconds=45.0,
                    last_error_excerpt="boom",
                )
            ],
        ),
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)
    assert "7✓" in text
    assert "2✗" in text
    assert "1▶" in text


def test_cron_detail_shows_unknown_outcomes_as_their_own_bucket() -> None:
    """F07: an unresolved outcome must be visible, not folded into ✓ or ✗."""
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(
            db_present=True,
            job_stats=[
                CronJobExecutionStats(
                    job_id="job-a", total_24h=4, completed_24h=1, failed_24h=2, unknown_24h=1
                )
            ],
        ),
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)

    assert "1✓" in text
    assert "2✗" in text
    assert "1?" in text


def test_cron_detail_unknown_only_window_is_not_rendered_as_a_dash() -> None:
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(
            db_present=True,
            job_stats=[CronJobExecutionStats(job_id="job-a", total_24h=3, unknown_24h=3)],
        ),
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)

    assert "3?" in text


# F06 — a completed execution is not a delivered notification.


def test_cron_detail_separates_delivery_outcome_from_execution_status() -> None:
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(
            db_present=True,
            job_stats=[
                CronJobExecutionStats(
                    job_id="job-a",
                    total_24h=4,
                    completed_24h=4,
                    delivery_tracked=True,
                    delivery_outcomes_24h={"delivered": 1, "suppressed": 2},
                    delivery_unrecorded_24h=1,
                )
            ],
            recent=[
                CronExecution(
                    execution_id="e1",
                    job_id="job-a",
                    job_name="alpha",
                    status="completed",
                    delivery_outcome="suppressed",
                )
            ],
        ),
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=160, no_color=True)

    assert "Delivery Outcomes" in text
    assert "delivered 1" in text
    assert "suppressed 2" in text
    assert "unrecorded 1" in text
    assert "suppressed" in text


def test_cron_detail_marks_a_pending_handoff() -> None:
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(
            db_present=True,
            job_stats=[
                CronJobExecutionStats(
                    job_id="job-a",
                    total_24h=1,
                    completed_24h=1,
                    delivery_tracked=True,
                    delivery_outcomes_24h={"delivered": 1},
                    handoff_pending_24h=1,
                )
            ],
            recent=[
                CronExecution(
                    execution_id="e1",
                    job_id="job-a",
                    job_name="alpha",
                    status="completed",
                    delivery_outcome="delivered",
                    handoff_pending=True,
                )
            ],
        ),
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=160, no_color=True)

    assert "handoff pending 1" in text
    assert "handoff" in text


def test_cron_detail_omits_delivery_when_the_schema_does_not_track_it() -> None:
    """An older agent records no outcome; a blank column must not read as delivered."""
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(
            db_present=True,
            job_stats=[CronJobExecutionStats(job_id="job-a", total_24h=2, completed_24h=2)],
            recent=[
                CronExecution(
                    execution_id="e1", job_id="job-a", job_name="alpha", status="completed"
                )
            ],
        ),
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=160, no_color=True)

    assert "Delivery Outcomes" not in text
    assert "delivered" not in text


# F08 — aggregates cover *recorded* attempts, not every attempt that happened.


def _retention_state(**kwargs) -> DashboardState:
    fields = {
        "db_present": True,
        "retained_total_count": 40,
        "retained_terminal_count": 40,
        "retention_cap": 1000,
        "oldest_claimed_age_seconds": 7200.0,
        "newest_claimed_age_seconds": 60.0,
        "recent": [
            CronExecution(execution_id="e1", job_id="job-a", job_name="alpha", status="completed")
        ],
    }
    fields.update(kwargs)
    return DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(**fields),
    )


def test_cron_detail_labels_counts_as_recorded_attempts() -> None:
    text = render_to_str(
        render_cron(_retention_state(), Theme(), detail=True), width=160, no_color=True
    )

    assert "Recorded attempts" in text
    assert "40 retained" in text
    assert "40 terminal" in text


def test_cron_detail_shows_the_observed_history_span() -> None:
    text = render_to_str(
        render_cron(_retention_state(), Theme(), detail=True), width=160, no_color=True
    )

    assert "spanning" in text


def test_cron_detail_warns_when_history_reaches_the_retention_cap() -> None:
    state = _retention_state(
        retained_total_count=1000, retained_terminal_count=1000, at_retention_cap=True
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=160, no_color=True)

    assert "1000-record retention cap" in text
    assert "recorded attempts only" in text


def test_cron_retention_warning_does_not_claim_the_window_is_incomplete() -> None:
    """Reaching the cap is not evidence about any particular 24h window."""
    state = _retention_state(
        retained_total_count=1000, retained_terminal_count=1000, at_retention_cap=True
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=400, no_color=True)

    assert "does not by itself make the 24h window incomplete" in text


def test_cron_detail_omits_the_cap_warning_below_the_cap() -> None:
    text = render_to_str(
        render_cron(_retention_state(), Theme(), detail=True), width=160, no_color=True
    )

    assert "retention cap" not in text


def test_cron_detail_omits_recorded_attempts_without_a_database() -> None:
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(),
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=160, no_color=True)

    assert "Recorded attempts" not in text


def test_cron_detail_job_without_execution_history_shows_dash() -> None:
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(db_present=True),
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)
    assert "alpha" in text
    assert "—" in text


def test_cron_detail_shows_recent_executions() -> None:
    state = DashboardState(
        cron_executions=_executions_state(
            db_present=True,
            recent=[
                CronExecution(
                    execution_id="e1",
                    job_id="job-a",
                    job_name="Alpha Report",
                    status="failed",
                    started_age_seconds=90.0,
                    duration_seconds=42.0,
                    error_excerpt="connection refused",
                ),
                CronExecution(
                    execution_id="e2",
                    job_id="job-b",
                    job_name="job-b",
                    status="running",
                ),
            ],
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)
    assert "Recent Executions" in text
    assert "Alpha Report" in text
    assert "connection refused" in text
    assert "1m" in text
    assert "42s" in text
    # A running execution has neither duration nor error: both render as "—".
    assert "job-b" in text
    assert "running" in text


def test_cron_detail_shows_open_incidents() -> None:
    state = DashboardState(
        cron_executions=_executions_state(
            db_present=True,
            open_incident_count=1,
            unacked_incident_count=1,
            open_incidents=[
                CronIncident(
                    incident_id="i1",
                    job_id="job-a",
                    job_name="Alpha Report",
                    state="detected",
                    failure_type="timeout",
                    first_seen_age_seconds=7200.0,
                    last_seen_age_seconds=600.0,
                    error_excerpt="Script exited with code 1",
                )
            ],
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)
    assert "Open Incidents" in text
    assert "detected" in text
    assert "timeout" in text
    assert "Script exited with code 1" in text
    assert "2h" in text
    assert "10m" in text


def test_cron_detail_without_execution_history_shows_no_data() -> None:
    state = DashboardState(cron=CronState(), cron_executions=_executions_state())
    text = render_to_str(render_cron(state, Theme(), detail=True), width=120, no_color=True)
    # Never blank the panel: an empty history says so explicitly.
    assert "No execution history" in text


def test_cron_detail_shows_job_flag_fields() -> None:
    state = DashboardState(
        cron=CronState(
            job_count=1,
            jobs=[
                CronJob(
                    job_id="job-a",
                    name="alpha",
                    failure_streak=4,
                    paused=True,
                    paused_reason="operator hold",
                    last_delivery_error="telegram 429",
                    dispatch_lateness_seconds=46.8,
                    dispatch_kind="late",
                    repeat_times=10,
                    repeat_completed=4,
                    no_agent=True,
                )
            ],
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)
    assert "streak 4" in text
    assert "operator hold" in text
    assert "telegram 429" in text
    assert "late 46.8s" in text
    assert "repeat 4/10" in text
    assert "no-agent" in text


def test_cron_detail_escapes_markup_in_executions_and_incidents() -> None:
    state = DashboardState(
        cron_executions=_executions_state(
            db_present=True,
            recent=[
                CronExecution(
                    execution_id="e1",
                    job_id="job-a",
                    job_name=f"{MARKUP_BOMB}{CLEAR_SCREEN}",
                    status="failed",
                    error_excerpt=f"{MARKUP_BOMB}{CLEAR_SCREEN}fail",
                )
            ],
            open_incident_count=1,
            open_incidents=[
                CronIncident(
                    incident_id="i1",
                    job_id="job-a",
                    job_name=MARKUP_BOMB,
                    state=f"det{CLEAR_SCREEN}ected",
                    failure_type=MARKUP_BOMB,
                    error_excerpt=f"{MARKUP_BOMB} boom",
                )
            ],
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=160, no_color=True)
    assert CLEAR_SCREEN not in text
    # The markup is shown literally instead of being interpreted as a style.
    assert "[bold red]owned[/]" in text
    assert "detected" in text


def test_cron_compact_escapes_markup_in_paused_and_streak_row() -> None:
    state = DashboardState(
        cron=CronState(
            job_count=1,
            jobs=[
                CronJob(
                    job_id="job-a",
                    name=f"{MARKUP_BOMB}{CLEAR_SCREEN}",
                    schedule_display=MARKUP_BOMB,
                    failure_streak=2,
                    paused=True,
                    paused_reason="hold",
                )
            ],
        )
    )
    text = render_to_str(render_cron(state, Theme()), width=160, no_color=True)
    assert CLEAR_SCREEN not in text
    assert "✗2" in text
    assert "⏸" in text


def test_cron_detail_mixed_history_dashes_only_the_job_without_stats() -> None:
    state = DashboardState(
        cron=CronState(
            job_count=2,
            jobs=[
                CronJob(job_id="job-a", name="alpha"),
                CronJob(job_id="job-b", name="beta"),
            ],
        ),
        cron_executions=CronExecutionsState(
            db_present=True,
            job_stats=[CronJobExecutionStats(job_id="job-a", completed_24h=3)],
        ),
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)
    assert "3✓" in text
    # beta has no rows in executions.db at all, so its window cell is a dash.
    assert "beta" in text
    assert "—" in text


def test_cron_detail_job_with_only_stale_history_shows_dash_counters() -> None:
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=CronExecutionsState(
            db_present=True,
            # Rows exist but every one of them fell outside the 24h window.
            job_stats=[CronJobExecutionStats(job_id="job-a", last_status="completed")],
        ),
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)
    assert "✓" not in text
    assert "—" in text


def test_cron_compact_paused_glyph_shows_without_a_reason() -> None:
    """A job paused with a null reason still earns the ⏸ marker."""
    state = DashboardState(
        cron=CronState(
            job_count=1,
            jobs=[CronJob(job_id="a", name="held", schedule_display="every 1h", paused=True)],
        )
    )
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    assert "⏸" in text


def test_cron_compact_paused_reason_without_flag_shows_no_glyph() -> None:
    """The marker keys off `paused`, which the collector derives; not the reason text."""
    state = DashboardState(
        cron=CronState(
            job_count=1,
            jobs=[CronJob(job_id="a", name="held", schedule_display="1h", paused_reason="hold")],
        )
    )
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    assert "⏸" not in text


def test_cron_detail_shows_paused_without_a_reason() -> None:
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="a", name="held", paused=True)])
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=140, no_color=True)
    assert "paused" in text


def _error_column_state(last_error: str) -> DashboardState:
    return DashboardState(
        cron=CronState(
            job_count=1, jobs=[CronJob(job_id="job-a", name="alpha", last_error=last_error)]
        ),
        cron_executions=_executions_state(
            db_present=True,
            job_stats=[
                CronJobExecutionStats(
                    job_id="job-a", last_status="failed", last_error_excerpt="executions.db boom"
                )
            ],
        ),
    )


def test_cron_detail_falls_back_to_execution_error_excerpt() -> None:
    """executions.db keeps the error after jobs.json clears last_error."""
    text = render_to_str(
        render_cron(_error_column_state(""), Theme(), detail=True), width=160, no_color=True
    )

    assert "executions.db boom" in text


def test_cron_detail_prefers_jobs_json_last_error() -> None:
    text = render_to_str(
        render_cron(_error_column_state("jobs.json boom"), Theme(), detail=True),
        width=160,
        no_color=True,
    )

    assert "jobs.json boom" in text
    assert "executions.db boom" not in text


def test_cron_detail_escapes_execution_error_excerpt() -> None:
    state = _error_column_state("")
    state.cron_executions.job_stats[0].last_error_excerpt = "[bold red]evil\x1b[2J"

    text = render_to_str(render_cron(state, Theme(), detail=True), width=160, no_color=True)

    assert "[bold red]evil" in text
    assert "\x1b[2J" not in text


# --- Missed-run catch-up and ticker-error diagnostics ------------------------


def _catch_up_detail(c: CronState, width: int = 140) -> str:
    return render_to_str(
        render_cron(DashboardState(cron=c), Theme(), detail=True), width=width, no_color=True
    )


def _catch_up_compact(c: CronState, width: int = 120) -> str:
    return render_to_str(render_cron(DashboardState(cron=c), Theme()), width=width, no_color=True)


def test_cron_detail_shows_the_catch_up_policy_and_a_recorded_counter() -> None:
    text = _catch_up_detail(
        CronState(
            catch_up_missed=True,
            catch_up_missed_set=True,
            catch_up_occurrences=7,
            catch_up_occurrences_recorded=True,
        )
    )

    assert "Missed-Run Catch-Up" in text
    assert "catch_up_missed: true" in text
    assert "7 recorded" in text
    # The counter is monotonic and carries no timestamp, so it must not read as a rate.
    assert "not a rate" in text


def test_cron_detail_labels_an_unset_catch_up_policy_as_the_upstream_default() -> None:
    text = _catch_up_detail(CronState())

    assert "not set in config.yaml" in text
    assert "upstream default" in text
    assert "no counter observed" in text


def test_cron_detail_flags_an_explicitly_disabled_catch_up_policy() -> None:
    text = _catch_up_detail(CronState(catch_up_missed=False, catch_up_missed_set=True))

    assert "catch_up_missed: false" in text
    assert "missed runs are skipped" in text
    # The silent-skip consequence: the counter does not move while runs are dropped.
    assert "stays flat" in text


def test_cron_detail_reports_an_unobserved_counter_without_claiming_zero() -> None:
    text = _catch_up_detail(CronState())

    assert "no counter observed" in text
    assert "0 recorded" not in text


def test_cron_detail_reports_a_recorded_zero_catch_up_count() -> None:
    text = _catch_up_detail(CronState(catch_up_occurrences=0, catch_up_occurrences_recorded=True))

    assert "0 recorded" in text
    assert "no counter observed" not in text


def test_cron_detail_shows_the_recorded_ticker_error_with_its_age() -> None:
    text = _catch_up_detail(
        CronState(ticker_last_error="RuntimeError: boom", ticker_last_error_age_seconds=45.0)
    )

    assert "Last tick error" in text
    assert "45s ago" in text
    assert "RuntimeError: boom" in text


def test_cron_detail_ticker_error_without_a_parseable_stamp_says_the_age_is_unknown() -> None:
    text = _catch_up_detail(CronState(ticker_last_error="RuntimeError: boom"))

    assert "RuntimeError: boom" in text
    assert "unknown time" in text
    assert "— ago" not in text


def test_cron_detail_states_that_absence_is_not_proof_of_health() -> None:
    """Both markers are deleted/best-effort, so a clean panel must not read as clean cron."""
    text = _catch_up_detail(CronState())

    assert "Absence is not proof" in text
    assert "deleted on the next clean tick" in text


def test_cron_detail_strips_terminal_controls_from_the_ticker_error() -> None:
    text = _catch_up_detail(
        CronState(
            ticker_last_error=f"{MARKUP_BOMB}{CLEAR_SCREEN}boom",
            ticker_last_error_age_seconds=1.0,
        )
    )

    assert CLEAR_SCREEN not in text
    assert "[bold red]owned" in text


def test_cron_detail_distinguishes_catch_up_from_late_in_the_flags_line() -> None:
    """Upstream labels a beyond-grace dispatch differently (hermes_cli/cron.py:119)."""
    state = DashboardState(
        cron=CronState(
            job_count=2,
            jobs=[
                CronJob(
                    job_id="job-a",
                    name="alpha",
                    dispatch_lateness_seconds=3600.0,
                    dispatch_kind="catch_up",
                ),
                CronJob(
                    job_id="job-b",
                    name="beta",
                    dispatch_lateness_seconds=46.8,
                    dispatch_kind="late",
                ),
            ],
        )
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=160, no_color=True)

    assert "catch-up after missed fire 3600.0s" in text
    assert "late 46.8s" in text
    assert "catch_up 3600.0s" not in text


def test_cron_detail_passes_an_unseen_dispatch_kind_through_sanitized() -> None:
    job = CronJob(
        job_id="job-a",
        name="alpha",
        dispatch_lateness_seconds=5.0,
        dispatch_kind=f"{CLEAR_SCREEN}future_kind",
    )

    text = _catch_up_detail(CronState(job_count=1, jobs=[job]), width=160)

    assert CLEAR_SCREEN not in text
    assert "future_kind 5.0s" in text


def test_cron_compact_warns_when_catch_up_is_disabled() -> None:
    quiet = _catch_up_compact(CronState()).splitlines()
    warned = _catch_up_compact(
        CronState(catch_up_missed=False, catch_up_missed_set=True)
    ).splitlines()

    assert len(warned) == len(quiet) + 1
    assert any("Catch-up off" in line and "missed runs are skipped" in line for line in warned)


def test_cron_compact_warns_on_a_recorded_ticker_error() -> None:
    quiet = _catch_up_compact(CronState()).splitlines()
    warned = _catch_up_compact(
        CronState(ticker_last_error="RuntimeError: boom", ticker_last_error_age_seconds=12.0)
    ).splitlines()

    assert len(warned) == len(quiet) + 1
    assert any("Tick error" in line and "12s ago" in line for line in warned)


def test_cron_compact_omits_the_tick_error_age_when_the_stamp_was_unparseable() -> None:
    text = _catch_up_compact(CronState(ticker_last_error="RuntimeError: boom"))

    assert "Tick error" in text
    assert "— ago" not in text


def test_cron_compact_spends_no_line_on_a_healthy_catch_up_counter() -> None:
    """A non-zero lifetime counter is history, not a current problem."""
    quiet = _catch_up_compact(CronState())
    counted = _catch_up_compact(
        CronState(
            catch_up_occurrences=7,
            catch_up_occurrences_recorded=True,
            catch_up_missed=True,
            catch_up_missed_set=True,
        )
    )

    assert quiet.splitlines() == counted.splitlines()


def test_cron_delivery_line_sanitizes_instead_of_escaping() -> None:
    """``Text.append`` skips markup parsing, so escaping leaks a literal backslash.

    Delivery outcome strings come from the database and are preserved verbatim,
    so one can contain ``[``. Control bytes must still be stripped.
    """
    state = DashboardState(
        cron=CronState(job_count=1, jobs=[CronJob(job_id="job-a", name="alpha")]),
        cron_executions=_executions_state(
            db_present=True,
            job_stats=[
                CronJobExecutionStats(
                    job_id="job-a",
                    total_24h=1,
                    completed_24h=1,
                    delivery_tracked=True,
                    delivery_outcomes_24h={"[bold]teleported\x1b[2J": 1},
                )
            ],
        ),
    )

    text = render_to_str(render_cron(state, Theme(), detail=True), width=200, no_color=True)

    assert "\\[bold]" not in text
    assert "[bold]teleported 1" in text
    assert "\x1b[2J" not in text


def test_cron_detail_labels_delivery_queued_as_unverified() -> None:
    """``delivery_queued`` is its own vocabulary: the notice left but was never
    verified, so resending would duplicate it (``cron/scheduler.py:2717-2723``)."""
    job = CronJob(job_id="j1", name="nightly", last_status="delivery_queued")
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    text = render_to_str(render_cron(state, Theme(), detail=True), no_color=True)
    assert "delivery_queued" in text
    assert "do not resend" in text


def test_cron_detail_labels_blocked_config_as_preflight_block() -> None:
    """``blocked_config`` is a preflight block with an alert-once flag, not a
    plain run error (``cron/scheduler.py:1438-1473``, ``:2726-2727``)."""
    job = CronJob(
        job_id="j1",
        name="nightly",
        last_status="blocked_config",
        preflight_alerted=True,
    )
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    text = render_to_str(render_cron(state, Theme(), detail=True), no_color=True)
    assert "preflight block" in text
    assert "alert sent" in text


def test_cron_last_status_cell_keeps_delivery_and_config_out_of_error() -> None:
    theme = Theme()
    error_cell = _last_status_cell(CronJob(last_status="error"), theme)
    queued_cell = _last_status_cell(CronJob(last_status="delivery_queued"), theme)
    blocked_cell = _last_status_cell(CronJob(last_status="blocked_config"), theme)
    failed_cell = _last_status_cell(CronJob(last_status="delivery_failed"), theme)
    ok_cell = _last_status_cell(CronJob(last_status="ok"), theme)
    empty_cell = _last_status_cell(CronJob(), theme)
    assert error_cell.style == theme.ui_error
    assert queued_cell.style == theme.ui_warn
    assert blocked_cell.style == theme.ui_warn
    assert failed_cell.style == theme.ui_warn
    assert ok_cell.style == theme.banner_text
    assert empty_cell.plain == "—"


def test_cron_detail_shows_fire_claim_states() -> None:
    running = CronJob(
        job_id="j1",
        name="running-job",
        fire_claim_state=CronFireClaimState.RUNNING,
        fire_claim_age_seconds=45.0,
    )
    abandoned = CronJob(
        job_id="j2",
        name="dead-job",
        fire_claim_state=CronFireClaimState.ABANDONED_RUN,
        fire_claim_age_seconds=900.0,
    )
    state = DashboardState(cron=CronState(job_count=2, jobs=[running, abandoned]))
    text = render_to_str(render_cron(state, Theme(), detail=True), no_color=True)
    assert "running now" in text
    assert "abandoned run" in text


def test_cron_detail_shows_pending_slot_and_last_fire_error() -> None:
    job = CronJob(
        job_id="j1",
        name="webhook-job",
        pending_slot_scheduled_at="2026-09-07T22:29:36+08:00",
        pending_slot_age_seconds=90.0,
        last_fire_error="loopback forward refused",
        last_fire_error_age_seconds=120.0,
    )
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    text = render_to_str(render_cron(state, Theme(), detail=True), no_color=True)
    assert "pending slot" in text
    assert "2026-09-07 22:29:36" in text
    assert "loopback forward refused" in text


def test_cron_detail_last_fire_error_omits_age_when_stamp_missing() -> None:
    job = CronJob(job_id="j1", name="webhook-job", last_fire_error="loopback refused")
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    text = render_to_str(render_cron(state, Theme(), detail=True), no_color=True)
    assert "loopback refused" in text
    assert "unknown time" in text


def test_cron_compact_marks_running_abandoned_and_pending_slot_jobs() -> None:
    """The compact list shows only the first two jobs, so each marker gets a pass."""
    running = CronJob(name="running-job", fire_claim_state=CronFireClaimState.RUNNING)
    abandoned = CronJob(name="dead-job", fire_claim_state=CronFireClaimState.ABANDONED_RUN)
    pending = CronJob(name="stuck-job", pending_slot_scheduled_at="2026-09-07T22:29:36+08:00")
    first = render_to_str(
        render_cron(
            DashboardState(cron=CronState(job_count=3, jobs=[running, abandoned])), Theme()
        ),
        no_color=True,
    )
    second = render_to_str(
        render_cron(DashboardState(cron=CronState(job_count=3, jobs=[pending, running])), Theme()),
        no_color=True,
    )
    assert "▶" in first
    assert "✗run" in first
    assert "⧗" in second
    assert "▶" in second


def test_cron_compact_warns_when_a_fire_could_not_be_forwarded() -> None:
    job = CronJob(name="webhook-job", last_fire_error="loopback refused")
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    text = render_to_str(render_cron(state, Theme()), no_color=True)
    assert "Fire forward failed" in text


def test_cron_compact_hides_fire_forward_line_when_none() -> None:
    state = DashboardState(cron=CronState(job_count=1, jobs=[CronJob(name="ok-job")]))
    text = render_to_str(render_cron(state, Theme()), no_color=True)
    assert "Fire forward" not in text


def test_cron_detail_labels_the_effective_model_by_its_source() -> None:
    """A pin is labelled pinned; an unpinned job says which config axis it
    follows at fire time (``cron/scheduler.py:1561-1590``)."""
    jobs = [
        CronJob(
            job_id="j1",
            name="pinned-job",
            model="gpt-9",
            provider="openai",
            effective_model="gpt-9",
            model_source=CronModelSource.PINNED,
        ),
        CronJob(
            job_id="j2",
            name="fleet-job",
            effective_model="fleet",
            model_source=CronModelSource.CRON_DEFAULT,
        ),
        CronJob(
            job_id="j3",
            name="main-job",
            effective_model="grok",
            model_source=CronModelSource.MAIN_MODEL,
        ),
    ]
    state = DashboardState(cron=CronState(job_count=3, jobs=jobs))
    text = render_to_str(render_cron(state, Theme(), detail=True), width=200, no_color=True)
    assert "model gpt-9 via openai (pinned)" in text
    assert "model fleet (cron.model default)" in text
    assert "model grok (follows main model)" in text
    assert "snapshot" not in text


def test_cron_detail_labels_detected_incident_as_no_delivered_failure_ping() -> None:
    """An open incident still in ``detected`` has no delivered failure ping on
    record: upstream flips it to ``alerted`` only when a failure ping actually
    leaves the process (``cron/incidents.py:1-9``, ``cron/scheduler.py:2745-2746``),
    but it also leaves the row in ``detected`` when the failure notice was only
    queued (``cron/scheduler.py:2516-2528``), so the label records the missing
    delivery without claiming the alert path itself is broken. ``acked_at`` is
    only ever set together with ``closed_at`` (``cron/incidents.py:186-195``),
    so an open incident can never be acknowledged."""
    detected = CronIncident(
        incident_id="i1",
        job_id="j1",
        job_name="nightly",
        state="detected",
        failure_type="timeout",
        first_seen_age_seconds=7200.0,
        last_seen_age_seconds=7200.0,
        error_excerpt="boom",
    )
    state = DashboardState(
        cron_executions=CronExecutionsState(
            open_incident_count=1,
            unacked_incident_count=1,
            open_incidents=[detected],
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), no_color=True)
    assert "no delivered failure ping" in text
    # The softened copy must not regress into absolute claims: a queued notice
    # leaves the row in detected with no delivery breakage on record.
    assert "broken alert path" not in text
    assert "ever reached the operator" not in text


def test_cron_detail_incident_note_explains_ack_semantics() -> None:
    detected = CronIncident(incident_id="i1", job_id="j1", job_name="nightly", state="detected")
    state = DashboardState(
        cron_executions=CronExecutionsState(
            open_incident_count=1, unacked_incident_count=1, open_incidents=[detected]
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), no_color=True)
    assert "acked_at" in text
    assert "closed_at" in text
    # The queued-notice caveat is what keeps a detected row from reading as
    # proof the delivery path is broken.
    assert "queued" in text


def test_cron_detail_keeps_alerted_incident_label_and_unknown_states_verbatim() -> None:
    alerted = CronIncident(incident_id="i1", job_id="j1", job_name="a", state="alerted")
    exotic = CronIncident(incident_id="i2", job_id="j2", job_name="b", state="quarantined")
    state = DashboardState(
        cron_executions=CronExecutionsState(
            open_incident_count=2,
            unacked_incident_count=2,
            open_incidents=[alerted, exotic],
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), no_color=True)
    # 'alerted' stays labelled; an unseen state is shown verbatim, never folded
    # into 'no delivered failure ping'.
    assert "alerted" in text
    assert "quarantined" in text


def test_cron_compact_fire_forward_line_carries_the_newest_age() -> None:
    """An ageless warning reads as current; the newest miss age bounds it."""
    state = DashboardState(
        cron=CronState(
            jobs=[
                CronJob(
                    job_id="j1",
                    name="old",
                    last_fire_error="loopback refused",
                    last_fire_error_age_seconds=86400.0,
                ),
                CronJob(
                    job_id="j2",
                    name="recent",
                    last_fire_error="loopback refused",
                    last_fire_error_age_seconds=120.0,
                ),
            ]
        )
    )
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    assert "Fire forward failed on 2 job(s)" in text
    assert "newest 2m ago" in text


def test_cron_compact_hides_unacked_when_it_cannot_differ() -> None:
    """``acked_at`` is written only with ``closed_at``, so open == unacked in this
    schema (``cron/incidents.py:172-203``); the qualifier is only worth a line
    when a foreign schema diverges from that."""
    state = DashboardState(
        cron_executions=_executions_state(open_incident_count=2, unacked_incident_count=2)
    )
    text = render_to_str(render_cron(state, Theme()), width=100, no_color=True)
    assert "Incidents: 2 open" in text
    assert "unacked" not in text


def test_cron_detail_shows_last_alert_age_and_resolved_counts() -> None:
    """``alerted_at`` is restamped on every delivered ping
    (``cron/incidents.py:196-212``), so it renders as the last alert's age; the
    auto-``resolved`` rows are summarised apart from the open ones."""
    incident = CronIncident(
        incident_id="i1",
        job_id="j1",
        job_name="nightly",
        state="alerted",
        failure_type="timeout",
        first_seen_age_seconds=7200.0,
        last_seen_age_seconds=600.0,
        alerted_age_seconds=300.0,
        error_excerpt="boom",
    )
    state = DashboardState(
        cron_executions=CronExecutionsState(
            db_present=True,
            open_incident_count=1,
            unacked_incident_count=1,
            open_incidents=[incident],
            resolved_incident_count=4,
            resolved_24h_count=2,
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=200, no_color=True)
    assert "Last alert" in text
    assert "5m ago" in text
    assert "Resolved incidents: 4 (2 in the last 24h" in text


def test_cron_detail_shows_resolved_counts_without_open_incidents() -> None:
    state = DashboardState(
        cron_executions=CronExecutionsState(
            db_present=True, resolved_incident_count=1, resolved_24h_count=1
        )
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=200, no_color=True)
    assert "Resolved incidents: 1 (1 in the last 24h" in text


def test_cron_panel_labels_a_quota_held_job() -> None:
    job = CronJob(job_id="j1", name="held-job", quota_hold_until="2026-09-24T09:00:00+00:00")
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    detail = render_to_str(render_cron(state, Theme(), detail=True), width=200, no_color=True)
    assert "held until" in detail
    assert "(provider usage window)" in detail
    compact = render_to_str(render_cron(state, Theme()), no_color=True)
    assert "held" in compact


def test_cron_detail_lists_recent_failures_with_their_errors() -> None:
    failure = CronExecution(
        execution_id="e1",
        job_id="j1",
        job_name="nightly",
        status="failed",
        started_age_seconds=7200.0,
        error_excerpt="Script execution failed: [Errno 2] missing",
    )
    state = DashboardState(
        cron_executions=CronExecutionsState(db_present=True, recent_failures=[failure])
    )
    text = render_to_str(render_cron(state, Theme(), detail=True), width=200, no_color=True)
    assert "Recent Failures" in text
    assert "Script execution failed: [Errno 2] missing" in text
    assert "2h ago" in text


def test_cron_panel_shows_usage_audit_token_rollup() -> None:
    usage = CronUsageState(
        present=True,
        tokens_24h=12_500,
        tokens_7d=80_000,
        fires_7d=40,
        jobs=[
            CronJobUsage(
                job_id="j1",
                job_name="nightly",
                fires_24h=4,
                tokens_24h=12_500,
                fires_7d=40,
                tokens_7d=80_000,
                errors_7d=2,
                last_fire_age_seconds=120.0,
                last_total_tokens=3_100,
                last_model="grok-4.6",
                last_duration_seconds=12.5,
                last_error_excerpt="provider 429",
            )
        ],
        window_truncated=True,
    )
    state = DashboardState(cron_usage=usage)
    detail = render_to_str(render_cron(state, Theme(), detail=True), width=220, no_color=True)
    assert "Token Usage" in detail
    assert "nightly" in detail
    assert "24h 4 fires 12.5K" in detail
    assert "7d 40 fires 80.0K" in detail
    assert "2 errors" in detail
    assert "last 2m ago 3.1K grok-4.6 12.5s" in detail
    assert "provider 429" in detail
    assert "lower bound" in detail
    compact = render_to_str(render_cron(state, Theme()), no_color=True)
    assert "Tokens 24h: 12.5K" in compact


def test_cron_panel_hides_token_usage_without_an_audit_ledger() -> None:
    state = DashboardState()
    detail = render_to_str(render_cron(state, Theme(), detail=True), width=220, no_color=True)
    assert "Token Usage" not in detail
    assert "Tokens 24h" not in render_to_str(render_cron(state, Theme()), no_color=True)
