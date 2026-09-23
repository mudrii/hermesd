"""Gateway panel rendering for readers kept in step with upstream (HEAD 38c289c014)."""

from __future__ import annotations

from hermesd.models import (
    DashboardState,
    DeadTargetSummary,
    GatewayBackendGroup,
    GatewayState,
    MigrationProfileRecord,
    MigrationState,
    ServeRestartObligation,
    UpdateReceiptSummary,
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


# --------------------------------------------------------------------------
# update receipt additions
# --------------------------------------------------------------------------


def test_updates_section_renders_the_newer_receipt_keys() -> None:
    gateway = _LIVE.model_copy(
        update={
            "last_update_outcome": "success",
            "runtime_code_skew_source": "fleet",
            "update_fleet_runtime_count": 2,
            "update_fleet_states": {"current": 1, "external": 1},
            "update_fleet_external_roots": ["/opt/" + HOSTILE],
            "update_post_swap_pid": 92904,
            "update_pending_manual_serve_count": 2,
            "update_settled_from_live_fleet_age_seconds": 120.0,
            "update_runtime_outcomes": {"restarted": 2, "unaccounted": 1},
            "update_skip_count": 4,
            "update_skip_names": ["desktop_serve", "npm", HOSTILE],
        }
    )

    detail = _render(gateway, detail=True)
    compact = _render(gateway, detail=False)

    assert "External checkouts (not skew): /opt/[/] boom" in detail
    assert "\x1b[2J" not in detail
    assert "finished by post-swap pid 92904" in detail
    assert "2 manual serve restart(s) still owed" in detail
    assert "settled from the live fleet 2m ago" in detail
    assert "Runtime restarts: restarted 2  unaccounted 1" in detail
    assert "Skipped: desktop_serve, npm, [/] boom" in detail
    assert "(+1 more)" in detail
    assert "⚠ 2 manual serve restart(s) owed" in compact


def test_updates_section_omits_absent_receipt_keys() -> None:
    gateway = _LIVE.model_copy(update={"last_update_outcome": "success"})

    detail = _render(gateway, detail=True)

    for absent in ("External checkouts", "post-swap", "manual serve", "settled", "Skipped"):
        assert absent not in detail


# --------------------------------------------------------------------------
# heartbeat memory
# --------------------------------------------------------------------------


def _memory(pressure: str) -> GatewayState:
    return _LIVE.model_copy(
        update={
            "memory_rss_kib": 512 * 1024,
            "memory_total_kib": 8 * 1024 * 1024,
            "memory_available_kib": 100 * 1024,
            "memory_swap_used_kib": 2048,
            "memory_pressure": pressure,
        }
    )


def test_memory_line_renders_in_detail() -> None:
    detail = _render(_memory("elevated"), detail=True)

    assert (
        "Memory: gateway RSS 512.0 MB  available 100.0 MB of 8.0 GB  swap 2.0 MB  "
        "pressure elevated" in detail
    )


def test_high_memory_pressure_is_a_compact_hint() -> None:
    assert "⚠ memory pressure critical" in _render(_memory("critical"), detail=False)
    assert "⚠ memory pressure elevated" in _render(_memory("elevated"), detail=False)
    for quiet in ("ok", "unknown"):
        assert "memory" not in _render(_memory(quiet), detail=False)


def test_memory_line_is_omitted_without_a_sample() -> None:
    assert "Memory:" not in _render(_LIVE, detail=True)


# --------------------------------------------------------------------------
# dead targets and the restart-loop breaker
# --------------------------------------------------------------------------


def test_dead_targets_render_in_detail_and_compact() -> None:
    gateway = _LIVE.model_copy(
        update={
            "dead_target_count": 3,
            "dead_target_platforms": {"telegram": 2, HOSTILE: 1},
            "dead_targets": [
                DeadTargetSummary(
                    platform="telegram", reason="forbidden " + HOSTILE, age_seconds=60
                ),
                DeadTargetSummary(platform="discord", reason="", age_seconds=None),
            ],
        }
    )

    detail = _render(gateway, detail=True)
    compact = _render(gateway, detail=False)

    assert "Dead delivery targets: 3" in detail
    assert "telegram 2" in detail
    assert "telegram  forbidden [/] boom" in detail
    assert "1m ago" in detail
    assert "\x1b[2J" not in detail
    assert "⚠ 3 dead delivery target(s)" in compact


def test_restart_loop_chain_renders_and_trips() -> None:
    quiet = _LIVE.model_copy(
        update={
            "restart_loop_boots_recorded": 2,
            "restart_loop_chain": 2,
            "restart_loop_max_restarts": 3,
            "restart_loop_chain_gap_seconds": 300.0,
            "restart_loop_last_boot_age_seconds": 30.0,
        }
    )
    tripped = quiet.model_copy(update={"restart_loop_chain": 3, "restart_loop_tripped": True})

    quiet_detail = _render(quiet, detail=True)
    tripped_detail = _render(tripped, detail=True)

    assert "Restart-loop breaker: chain 2/3 (gaps ≤ 5m)  last boot 30s ago" in quiet_detail
    assert "TRIPPED" not in quiet_detail
    assert "restart loop" not in _render(quiet, detail=False)
    assert "⚠ TRIPPED — the next restart-interrupted boot skips auto-resume" in tripped_detail
    assert "⚠ restart loop" in _render(tripped, detail=False)


def test_restart_loop_and_dead_targets_are_silent_when_absent() -> None:
    detail = _render(_LIVE, detail=True)

    assert "Restart-loop breaker" not in detail
    assert "Dead delivery targets" not in detail


# --------------------------------------------------------------------------
# backend heartbeat groups
# --------------------------------------------------------------------------


def test_backend_groups_render_by_profile_and_host() -> None:
    gateway = _LIVE.model_copy(
        update={
            "gateway_backend_groups": [
                GatewayBackendGroup(
                    profile="default",
                    host="pro32",
                    backends=55,
                    last_heartbeat_age_seconds=30.0,
                    live=True,
                ),
                GatewayBackendGroup(
                    profile=HOSTILE, host="", backends=1, last_heartbeat_age_seconds=None
                ),
            ],
            "gateway_backend_groups_truncated": True,
        }
    )

    detail = _render(gateway, detail=True)

    assert "Backends: default@pro32 55 live (beat 30s ago)" in detail
    assert "[/] boom [red]x[/red] clear@— 1 (beat — ago)" in detail
    assert "\x1b[2J" not in detail
    assert "more groups not shown" in detail


def test_backend_groups_line_is_omitted_when_empty() -> None:
    assert "Backends:" not in _render(_LIVE, detail=True)


# --------------------------------------------------------------------------
# restart / update backlog
# --------------------------------------------------------------------------


def test_restart_notice_and_serve_obligations_render() -> None:
    gateway = _LIVE.model_copy(
        update={
            "restart_notice_pending": True,
            "restart_notice_requested_age_seconds": 600.0,
            "restart_notice_via_service": True,
            "restart_notice_delivered_count": 1,
            "serve_restart_pending_count": 2,
            "serve_restart_stale_count": 1,
            "serve_restart_pending": [
                ServeRestartObligation(kind="serve", profile=HOSTILE, pid=501, verified=True),
                ServeRestartObligation(kind="dashboard", profile="dev", pid=502),
            ],
        }
    )

    detail = _render(gateway, detail=True)
    compact = _render(gateway, detail=False)

    assert "Restart Backlog" in detail
    assert (
        "Planned restart 10m ago (via service): back-online notice still owed to home"
        " channels (1 delivered)" in detail
    )
    assert "Manual serve restarts owed: 2  (1 stale record(s) for processes already gone)" in detail
    assert "serve [/] boom [red]x[/red] clear pid 501" in detail
    assert "dashboard dev pid 502 (identity unverified)" in detail
    assert "\x1b[2J" not in detail
    assert "⚠ restart notice owed" in compact
    assert "⚠ 2 manual serve restart(s) pending" in compact


def test_update_failure_history_renders_in_the_updates_section() -> None:
    gateway = _LIVE.model_copy(
        update={
            "update_history_scanned": 10,
            "update_history_failed": 2,
            "update_failures": [
                UpdateReceiptSummary(outcome="partial", finished_age_seconds=3600.0),
                UpdateReceiptSummary(
                    outcome=HOSTILE, finished_age_seconds=None, failed_step="pull"
                ),
            ],
        }
    )

    detail = _render(gateway, detail=True)

    assert "Updates" in detail
    assert "Recent runs: 2 of the last 10 did not finish cleanly" in detail
    assert "partial  1h ago" in detail
    assert "[/] boom [red]x[/red] clear  — ago  failed step pull" in detail


def test_restart_backlog_is_omitted_when_nothing_is_owed() -> None:
    detail = _render(_LIVE, detail=True)

    assert "Restart Backlog" not in detail
    assert "Recent runs" not in detail


def test_compact_warnings_fit_the_overview_row_ahead_of_the_directory_count() -> None:
    """The wide overview gives the Gateway row two content lines: warnings win."""
    from rich.console import Console

    from hermesd.models import ChannelDirectoryState

    dashboard = DashboardState(
        gateway=GatewayState(
            running=True,
            pid=4242,
            state="running",
            multiplex_standalone_reason="profiles default and dev share TELEGRAM_BOT_TOKEN",
        ),
        channels=ChannelDirectoryState(platform_count=2),
    )
    console = Console(width=200, no_color=True)
    lines = console.render_lines(
        render_gateway(dashboard, Theme(), detail=False),
        console.options.update(height=4),
        pad=False,
    )
    visible = "\n".join("".join(segment.text for segment in line) for line in lines[:4])

    assert "standalone (not multiplexing)" in visible
