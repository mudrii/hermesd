"""Tests for [6] Cron panel — job listing, compact and detail views."""

from __future__ import annotations

from hermesd.models import (
    CronExecution,
    CronExecutionsState,
    CronIncident,
    CronJob,
    CronJobExecutionStats,
    CronState,
    CronTickerHealth,
    DashboardState,
)
from hermesd.panels import render_panel
from hermesd.panels.cron import render_cron
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
