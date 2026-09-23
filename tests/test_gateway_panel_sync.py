"""Gateway panel rendering for readers kept in step with upstream (HEAD 38c289c014)."""

from __future__ import annotations

from hermesd.models import DashboardState, GatewayState
from hermesd.panels.gateway import render_gateway
from hermesd.theme import Theme
from tests.conftest import render_to_str

HOSTILE = "[/] boom [red]x[/red] \x1b[2Jclear"


def _render(gateway: GatewayState, *, detail: bool, **state: object) -> str:
    dashboard = DashboardState(gateway=gateway, **state)  # type: ignore[arg-type]
    return render_to_str(
        render_gateway(dashboard, Theme(), detail=detail), width=200, no_color=True
    )


# --------------------------------------------------------------------------
# degraded / watchdog exit
# --------------------------------------------------------------------------


def test_degraded_gateway_renders_as_a_warning_not_running_or_stopped() -> None:
    gateway = GatewayState(running=True, pid=7, state="degraded")

    for detail in (False, True):
        rendered = _render(gateway, detail=detail)
        assert "Degraded" in rendered
        assert "Running" not in rendered
        assert "Stopped" not in rendered
        assert "PID:7" in rendered


def test_degraded_detail_explains_the_parked_platform() -> None:
    rendered = _render(GatewayState(running=True, pid=7, state="degraded"), detail=True)

    assert "serving; a configured platform is parked or retrying" in rendered


def test_running_gateway_still_renders_running() -> None:
    rendered = _render(GatewayState(running=True, pid=7, state="running"), detail=False)

    assert "Running" in rendered
    assert "Degraded" not in rendered


def test_watchdog_exit_is_named_in_compact_and_detail() -> None:
    gateway = GatewayState(
        running=False, state="degraded", watchdog_exit_reason="loop_liveness_watchdog"
    )

    compact = _render(gateway, detail=False)
    detail = _render(gateway, detail=True)

    assert "Stopped" in compact
    assert "watchdog exit" in compact
    assert "watchdog exit: loop_liveness_watchdog" in detail
    assert "event loop stopped dispatching" in detail
