"""Tests for [6] Cron panel — job listing, compact and detail views."""

from __future__ import annotations

from hermesd.models import CronJob, CronState, DashboardState
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
