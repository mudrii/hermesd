"""Tests for the [3] Tokens / Cost panel."""

from __future__ import annotations

from hermesd.models import DashboardState, SessionInfo
from hermesd.panels.tokens import render_tokens
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_tokens_detail_caps_session_table_with_footer() -> None:
    sessions = [SessionInfo(session_id=f"sess_{i:04d}") for i in range(55)]
    rendered = render_to_str(render_tokens(DashboardState(sessions=sessions), Theme(), detail=True))
    assert "… and 5 more" in rendered


def test_tokens_detail_no_footer_when_under_cap() -> None:
    sessions = [SessionInfo(session_id=f"sess_{i:04d}") for i in range(3)]
    rendered = render_to_str(render_tokens(DashboardState(sessions=sessions), Theme(), detail=True))
    assert "… and" not in rendered
