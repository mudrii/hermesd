"""Tests for the [1] Gateway & Platforms panel."""

from __future__ import annotations

from hermesd.models import DashboardState, GatewayState
from hermesd.panels.gateway import render_gateway
from hermesd.theme import Theme
from tests.conftest import render_to_str


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
