"""Gateway panel rendering for readers kept in step with upstream (HEAD 38c289c014)."""

from __future__ import annotations

from hermesd.models import (
    DashboardState,
    GatewayState,
    MigrationProfileRecord,
    MigrationState,
)
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


# --------------------------------------------------------------------------
# multiplex: standalone reason, retired opt-out, unfinished manifest
# --------------------------------------------------------------------------


def _migration(**fields: object) -> MigrationState:
    base: dict[str, object] = {
        "manifest_present": True,
        "manifest_parsed": True,
        "manifest_schema_valid": True,
        "manifest_version": 1,
        "default_profile": MigrationProfileRecord(profile="default", served=True),
        "secondaries": [MigrationProfileRecord(profile="dev", served=True)],
        "secondary_count": 1,
        "multiplex_flag_on": True,
        "default_gateway_live": True,
        "served_recorded": True,
    }
    base.update(fields)
    return MigrationState(**base)  # type: ignore[arg-type]


_LIVE = GatewayState(running=True, pid=7, state="running")


def test_unfinished_migration_names_the_resume_command() -> None:
    rendered = _render(_LIVE, detail=True, migration=_migration(served_recorded=False))

    assert "migration unfinished" in rendered
    assert "hermes gateway migrate --multiplex" in rendered
    assert "rollback" not in rendered.lower()
    assert "--standalone" not in rendered


def test_verified_topology_with_a_leftover_manifest_says_so() -> None:
    rendered = _render(_LIVE, detail=True, migration=_migration())

    assert "multiplexed (verified)" in rendered
    assert "manifest is still on disk" in rendered


def test_retired_false_flag_is_shown_as_ignored_not_as_an_opt_out() -> None:
    migration = MigrationState(multiplex_flag_retired_off=True)

    rendered = _render(_LIVE, detail=True, migration=migration)

    assert "Multiplex Migration" in rendered
    assert "retired / ignored" in rendered
    assert "multiplex_profiles off" not in rendered
    assert "could not be parsed" not in rendered


def test_unset_flag_is_shown_as_the_boot_time_default() -> None:
    rendered = _render(_LIVE, detail=True, migration=_migration(multiplex_flag_on=False))

    assert "gateway.multiplex_profiles unset" in rendered


def test_explicit_flag_is_shown_as_explicit() -> None:
    rendered = _render(_LIVE, detail=True, migration=_migration())

    assert "gateway.multiplex_profiles true (explicit)" in rendered


def test_standalone_reason_renders_in_detail_and_compact() -> None:
    gateway = _LIVE.model_copy(update={"multiplex_standalone_reason": "profile 'dev' " + HOSTILE})

    detail = _render(gateway, detail=True)
    compact = _render(gateway, detail=False)

    assert "Standalone: profile 'dev' [/] boom" in detail
    assert "\x1b[2J" not in detail
    assert "hermes gateway migrate --multiplex" in detail
    assert "⚠ standalone (not multiplexing)" in compact


def test_single_profile_standalone_reason_is_not_a_compact_warning() -> None:
    gateway = _LIVE.model_copy(
        update={"multiplex_standalone_reason": "only one profile exists (nothing to multiplex)"}
    )

    compact = _render(gateway, detail=False)
    detail = _render(gateway, detail=True)

    assert "standalone" not in compact
    assert "Standalone: only one profile exists" in detail
