"""Tests for the [2] Sessions panel."""

from __future__ import annotations

from hermesd.models import DashboardState, SessionInfo
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
