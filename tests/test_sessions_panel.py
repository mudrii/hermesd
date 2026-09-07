"""Tests for the [2] Sessions panel."""

from __future__ import annotations

import time

import pytest

from hermesd.models import ActiveSurface, DashboardState, SessionInfo
from hermesd.panels.sessions import render_sessions
from hermesd.theme import Theme
from tests.conftest import render_to_str


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


def _session(**overrides: object) -> SessionInfo:
    base: dict[str, object] = {
        "session_id": "sess_abcdef12",
        "source": "cli",
        "model": "gpt-5.4",
        "message_count": 12,
        "tool_call_count": 4,
        "input_tokens": 1000,
        "output_tokens": 500,
        "estimated_cost_usd": 0.9,
        "started_at": time.time() - 7200,
        "is_active": True,
    }
    base.update(overrides)
    return SessionInfo(**base)  # type: ignore[arg-type]


def _rich_state() -> DashboardState:
    return DashboardState(
        sessions=[
            _session(
                display_name="Dashboard work",
                title="raw title",
                title_source="llm",
                git_branch="feat/session-model-usage",
                profile_name="coding",
                pinned=True,
                chat_type="direct",
                last_activity_at=time.time() - 45,
                last_activity_description="edited db.py",
                actual_cost_usd=0.55,
                cost_source="provider",
                api_call_count=9,
                cwd="/tmp/project",
                compression_failure_error="compression failed: context too large",
            )
        ],
        active_surfaces=[
            ActiveSurface(session_id="sess_abcdef12", surface="cli", pid=111, alive=True),
            ActiveSurface(session_id="sess_abcdef12", surface="telegram", pid=222, alive=False),
        ],
        active_surface_count=2,
    )


@pytest.mark.parametrize("detail", [False, True])
def test_sessions_panel_renders_empty_state(detail: bool) -> None:
    rendered = render_to_str(render_sessions(DashboardState(), Theme(), detail=detail))
    assert "Sessions" in rendered


def test_compact_shows_live_surface_count() -> None:
    rendered = render_to_str(render_sessions(_rich_state(), Theme()), width=100)

    assert "2 live" in rendered


def test_compact_omits_live_marker_without_surfaces() -> None:
    rendered = render_to_str(
        render_sessions(DashboardState(sessions=[_session()]), Theme()), width=100
    )

    assert "live" not in rendered


def test_detail_prefers_display_name_and_marks_pinned() -> None:
    rendered = render_to_str(render_sessions(_rich_state(), Theme(), detail=True), width=200)

    assert "Dashboard work" in rendered
    assert "raw title" not in rendered
    assert "📌" in rendered


def test_detail_shows_branch_profile_activity_and_actual_cost() -> None:
    rendered = render_to_str(render_sessions(_rich_state(), Theme(), detail=True), width=200)

    assert "feat/session-model-usage" in rendered
    assert "coding" in rendered
    assert "$0.55" in rendered
    # Age comes from last_activity_at (45s ago), not started_at (2h ago).
    assert "45s" in rendered


def test_detail_falls_back_to_started_at_for_age() -> None:
    state = DashboardState(sessions=[_session(started_at=time.time() - 120, git_branch="main")])

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "2m" in rendered


def test_detail_truncates_long_branch_name() -> None:
    state = DashboardState(sessions=[_session(git_branch="feature/" + "b" * 60)])

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "b" * 40 not in rendered
    assert "feature/" in rendered


def test_detail_lists_live_surfaces() -> None:
    rendered = render_to_str(render_sessions(_rich_state(), Theme(), detail=True), width=200)

    assert "Live Surfaces" in rendered
    assert "telegram" in rendered
    assert "222" in rendered


def test_detail_limits_surfaces_to_filtered_sessions() -> None:
    state = _rich_state()
    state.sessions.append(_session(session_id="sess_other", source="telegram"))

    rendered = render_to_str(
        render_sessions(state, Theme(), detail=True, filter_query="id:sess_other"),
        width=200,
    )

    assert "Live Surfaces" not in rendered


def test_detail_warns_about_compression_failures() -> None:
    rendered = render_to_str(render_sessions(_rich_state(), Theme(), detail=True), width=200)

    assert "compression failed" in rendered


def test_detail_truncates_long_compression_error() -> None:
    state = DashboardState(sessions=[_session(compression_failure_error="x" * 200)])

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "x" * 61 not in rendered
    assert "x" * 40 in rendered


def test_detail_escapes_markup_hostile_free_text() -> None:
    state = DashboardState(
        sessions=[
            _session(
                display_name="[/] name [x]\x1b[2Jhere",
                git_branch="[/] branch [x]",
                compression_failure_error="[/] boom [x]\x1b[2J",
            )
        ]
    )

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "[/]" in rendered
    assert "[x]" in rendered
    assert "\x1b[2J" not in rendered
