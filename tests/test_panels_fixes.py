"""Regression tests for panel audit fixes.

Covers: markup injection in config labels, ANSI/control-code injection in
logs and cron panels, raw epoch rendering in kanban, log-stream fallback in
the logs compact view, skills detail visible-window cap, gateway drain
timestamp formatting, timestamp overflow guards, detail table row caps,
blank placeholder fallbacks, and the version_behind fallback.
"""

from __future__ import annotations

import time

from hermesd.models import (
    BackgroundProcessInfo,
    ConfigSummary,
    CronJob,
    CronState,
    CuratorRun,
    DashboardState,
    GatewayState,
    KanbanState,
    KanbanTaskSummary,
    LogLine,
    LogState,
    LogStream,
    ModelCacheSummary,
    OperationsState,
    ProfilesState,
    ProfileSummary,
    SessionInfo,
    SkillInfo,
    SkillsMemory,
)
from hermesd.panels.config_panel import render_config
from hermesd.panels.cron import render_cron
from hermesd.panels.curator_panel import render_curator
from hermesd.panels.gateway import render_gateway
from hermesd.panels.kanban import render_kanban
from hermesd.panels.logs import render_logs
from hermesd.panels.operations import render_operations
from hermesd.panels.overview import render_overview
from hermesd.panels.profiles import render_profiles
from hermesd.panels.sessions import render_sessions
from hermesd.panels.tokens import render_tokens
from hermesd.panels.tools import render_tools
from hermesd.theme import Theme
from tests.conftest import render_to_str

CLEAR_SCREEN = "\x1b[2J"


# --- Fix 1: config_panel markup injection via _code_execution_label/_moa_label ---


def test_config_detail_escapes_code_execution_mode() -> None:
    state = DashboardState(config=ConfigSummary(code_execution_mode="yolo [/] inj"))
    rendered = render_to_str(render_config(state, Theme(), detail=True))
    assert "yolo [/] inj" in rendered


def test_config_detail_escapes_moa_label_values() -> None:
    state = DashboardState(
        config=ConfigSummary(
            moa_active_preset="preset [/] x",
            moa_preset_count=2,
            moa_aggregator_label="agg [/] y",
        )
    )
    rendered = render_to_str(render_config(state, Theme(), detail=True))
    assert "preset [/] x" in rendered
    assert "agg [/] y" in rendered


# --- Fix 2: ANSI/control-code injection in logs and cron ---


def test_logs_strip_ansi_control_sequences() -> None:
    line = LogLine(
        timestamp="2026-06-14 00:00:00",
        level="INFO",
        message=f"boom {CLEAR_SCREEN} clear",
    )
    state = DashboardState(logs=LogState(agent_lines=[line]))
    for detail in (False, True):
        rendered = render_to_str(render_logs(state, Theme(), detail=detail))
        assert CLEAR_SCREEN not in rendered
        assert "clear" in rendered


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


# --- Fix 3: kanban Completed column renders an age label, not a raw epoch ---


def test_kanban_completed_at_renders_age_label() -> None:
    completed = int(time.time()) - 120
    task = KanbanTaskSummary(task_id="t1", status="done", completed_at=completed)
    state = DashboardState(kanban=KanbanState(db_present=True, recent_tasks=[task]))
    rendered = render_to_str(render_kanban(state, Theme(), detail=True))
    assert str(completed) not in rendered
    assert "2m" in rendered


# --- Fix 4: logs compact falls back to the first non-empty stream ---


def test_logs_compact_falls_back_to_gateway_stream() -> None:
    state = DashboardState(logs=LogState(gateway_lines=[LogLine(message="gateway hello")]))
    rendered = render_to_str(render_logs(state, Theme()))
    assert "gateway hello" in rendered
    assert "No log lines" not in rendered


def test_logs_compact_falls_back_when_named_streams_present() -> None:
    state = DashboardState(
        logs=LogState(
            streams=[
                LogStream(name="agent"),
                LogStream(name="cron", lines=[LogLine(message="cron tick")]),
            ]
        )
    )
    rendered = render_to_str(render_logs(state, Theme()))
    assert "cron tick" in rendered
    assert "No log lines" not in rendered


def test_logs_compact_still_prefers_agent_stream() -> None:
    state = DashboardState(
        logs=LogState(
            agent_lines=[LogLine(message="agent hello")],
            gateway_lines=[LogLine(message="gateway hello")],
        )
    )
    rendered = render_to_str(render_logs(state, Theme()))
    assert "agent hello" in rendered
    assert "gateway hello" not in rendered


def test_logs_compact_empty_state_still_shows_placeholder() -> None:
    rendered = render_to_str(render_logs(DashboardState(), Theme()))
    assert "No log lines" in rendered


# --- Fix 6: skills detail visible-window cap ---


def _skills_state(count: int) -> DashboardState:
    return DashboardState(
        skills_memory=SkillsMemory(
            skill_count=count,
            skill_categories=1,
            skills=[SkillInfo(name=f"skill-{i:02d}", category="dev") for i in range(count)],
        )
    )


def test_skills_detail_caps_visible_window() -> None:
    rendered = render_to_str(render_overview(_skills_state(40), Theme(), detail=True))
    assert "[1-20/40]" in rendered
    assert "skill-39" not in rendered


def test_skills_detail_scroll_pages_the_window() -> None:
    rendered = render_to_str(
        render_overview(_skills_state(40), Theme(), detail=True, scroll_offset=20)
    )
    assert "[21-40/40]" in rendered
    assert "skill-39" in rendered


def test_skills_detail_small_list_has_no_hint() -> None:
    rendered = render_to_str(render_overview(_skills_state(5), Theme(), detail=True))
    assert "skill-04" in rendered
    assert "/5]" not in rendered


# --- Fix 7: gateway drain_requested_at formatted like updated_at ---


def test_gateway_drain_requested_at_is_formatted() -> None:
    state = DashboardState(
        gateway=GatewayState(
            running=True,
            pid=1,
            drain_active=True,
            drain_requested_at="2026-06-14T10:11:12Z",
        )
    )
    rendered = render_to_str(render_gateway(state, Theme(), detail=True))
    assert "2026-06-14 10:11:12" in rendered
    assert "2026-06-14T10:11:12Z" not in rendered


# --- Fix 8: timestamp overflow guards ---


def test_profiles_detail_handles_absurd_log_mtime() -> None:
    state = DashboardState(
        profiles=ProfilesState(
            profile_count=1,
            profiles=[ProfileSummary(name="ops", latest_log_mtime=1e18)],
        )
    )
    rendered = render_to_str(render_profiles(state, Theme(), detail=True))
    assert "—" in rendered


def test_tools_detail_handles_absurd_started_at() -> None:
    state = DashboardState(
        background_processes=[
            BackgroundProcessInfo(session_id="s", command="make", started_at=1e18)
        ]
    )
    rendered = render_to_str(render_tools(state, Theme(), detail=True))
    assert "—" in rendered


def test_operations_detail_handles_absurd_mtime() -> None:
    state = DashboardState(
        operations=OperationsState(
            model_caches=[ModelCacheSummary(name="models_dev_cache", mtime=float("inf"))]
        )
    )
    rendered = render_to_str(render_operations(state, Theme(), detail=True))
    assert "—" in rendered


# --- Fix 9: detail table row caps ---


def test_sessions_detail_caps_table_with_footer() -> None:
    sessions = [SessionInfo(session_id=f"sess_{i:04d}", started_at=float(i)) for i in range(60)]
    rendered = render_to_str(
        render_sessions(DashboardState(sessions=sessions), Theme(), detail=True)
    )
    assert "… and 10 more" in rendered


def test_sessions_detail_no_footer_when_under_cap() -> None:
    sessions = [SessionInfo(session_id=f"sess_{i:04d}") for i in range(3)]
    rendered = render_to_str(
        render_sessions(DashboardState(sessions=sessions), Theme(), detail=True)
    )
    assert "… and" not in rendered


def test_tokens_detail_caps_session_table_with_footer() -> None:
    sessions = [SessionInfo(session_id=f"sess_{i:04d}") for i in range(55)]
    rendered = render_to_str(render_tokens(DashboardState(sessions=sessions), Theme(), detail=True))
    assert "… and 5 more" in rendered


def test_tokens_detail_no_footer_when_under_cap() -> None:
    sessions = [SessionInfo(session_id=f"sess_{i:04d}") for i in range(3)]
    rendered = render_to_str(render_tokens(DashboardState(sessions=sessions), Theme(), detail=True))
    assert "… and" not in rendered


# --- Fix 10: blank placeholders in cron and curator compact views ---


def test_cron_compact_blank_job_name_falls_back_to_dash() -> None:
    job = CronJob(job_id="", name="", schedule_display="* * *")
    state = DashboardState(cron=CronState(job_count=1, jobs=[job]))
    rendered = render_to_str(render_cron(state, Theme()))
    assert "—" in rendered


def test_curator_compact_blank_stamp_falls_back_to_dash() -> None:
    state = DashboardState(curator=CuratorRun(run_present=True, stamp=""))
    rendered = render_to_str(render_curator(state, Theme()))
    assert "Last run:" in rendered
    assert "—" in rendered


# --- Fix 11: gateway panel renders version_behind as a fallback ---


def test_gateway_version_behind_fallback_when_updates_behind_absent() -> None:
    state = DashboardState(
        gateway=GatewayState(running=True, pid=1, hermes_version="1.2.3", updates_behind=0),
        version_behind=5,
    )
    compact = render_to_str(render_gateway(state, Theme()))
    detail = render_to_str(render_gateway(state, Theme(), detail=True))
    assert "(5 behind)" in compact
    assert "5 commits behind" in detail
    assert "up to date" not in detail


def test_gateway_updates_behind_takes_precedence_over_version_behind() -> None:
    state = DashboardState(
        gateway=GatewayState(running=True, pid=1, hermes_version="1.2.3", updates_behind=2),
        version_behind=5,
    )
    compact = render_to_str(render_gateway(state, Theme()))
    detail = render_to_str(render_gateway(state, Theme(), detail=True))
    assert "(2 behind)" in compact
    assert "2 commits behind" in detail
    assert "5 commits behind" not in detail


def test_skills_detail_scroll_clamps_to_full_window() -> None:
    """Scrolling past the end must clamp to a full window, not a 1-row stub."""
    rendered = render_to_str(
        render_overview(_skills_state(40), Theme(), detail=True, scroll_offset=39)
    )
    assert "[21-40/40]" in rendered
    assert "skill-20" in rendered


def test_detail_max_scroll_offset_skills_accounts_for_window() -> None:
    from hermesd.app import _SKILLS_PANEL_NUM, _detail_max_scroll_offset

    state = _skills_state(30)
    assert _detail_max_scroll_offset(_SKILLS_PANEL_NUM, state, "", "") == 10
