"""Tests for [6] Cron panel — job listing, compact and detail views."""
import json
from pathlib import Path

from rich.console import Console

from hermesd.collector import Collector
from hermesd.models import CronJob, CronState, DashboardState
from hermesd.panels import render_panel
from hermesd.theme import Theme


def _render_to_str(panel) -> str:
    console = Console(width=100, force_terminal=True, no_color=True)
    with console.capture() as cap:
        console.print(panel)
    return cap.get()


def test_cron_compact_shows_job_count():
    state = DashboardState(
        cron=CronState(
            last_tick_ago_seconds=10.0,
            job_count=2,
            error_count=0,
            jobs=[
                CronJob(name="daily-report", schedule_display="every day at 9am", state="scheduled", enabled=True),
                CronJob(name="cleanup", schedule_display="every 6h", state="scheduled", enabled=True),
            ],
        ),
    )
    panel = render_panel(6, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "Jobs: 2" in text
    assert "daily-report" in text
    assert "every day at 9am" in text


def test_cron_compact_shows_errors():
    state = DashboardState(
        cron=CronState(job_count=1, error_count=1, jobs=[
            CronJob(name="broken", state="error", last_status="error"),
        ]),
    )
    panel = render_panel(6, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "Errors: 1" in text


def test_cron_detail_shows_job_table():
    state = DashboardState(
        cron=CronState(
            last_tick_ago_seconds=5.0,
            job_count=2,
            jobs=[
                CronJob(
                    job_id="abc123", name="meeting-reminder",
                    schedule_display="once in 2m", state="scheduled",
                    enabled=True, next_run_at="2026-04-09T18:21:49+08:00",
                ),
                CronJob(
                    job_id="def456", name="daily-backup",
                    schedule_display="every day at 3am", state="scheduled",
                    enabled=False, last_status="ok",
                ),
            ],
        ),
    )
    panel = render_panel(6, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "meeting-reminder" in text
    assert "once in 2m" in text
    assert "daily-backup" in text
    assert "2026-04-09T18:21:49" in text


def test_cron_detail_empty():
    state = DashboardState(cron=CronState())
    panel = render_panel(6, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "No cron jobs configured" in text


def test_collector_reads_jobs_json(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(json.dumps({
        "jobs": [
            {
                "id": "j1", "name": "test-cron",
                "schedule_display": "every 10m", "state": "scheduled",
                "enabled": True, "next_run_at": "2026-04-09T19:00:00",
                "last_status": None, "last_error": None,
            },
            {
                "id": "j2", "name": "failed-job",
                "schedule_display": "every 1h", "state": "error",
                "enabled": True, "last_status": "error",
                "last_error": "timeout",
            },
        ],
    }))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.cron.job_count == 2
    assert state.cron.error_count == 1
    assert state.cron.jobs[0].name == "test-cron"
    assert state.cron.jobs[1].name == "failed-job"
    c.close()


def test_collector_no_jobs_json(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.cron.job_count == 0
    assert state.cron.jobs == []
    c.close()
