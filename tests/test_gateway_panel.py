"""Tests for the [1] Gateway & Platforms panel."""

from __future__ import annotations

import pytest

from hermesd.models import (
    DashboardState,
    DeliveryObligationSummary,
    GatewayLoopHealth,
    GatewayState,
    PlatformOwnership,
    PlatformStatus,
)
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


HOSTILE = "[/] boom [red]x[/red] \x1b[2Jclear"


def _liveness_state(**overrides) -> DashboardState:
    gateway = GatewayState(
        running=True,
        pid=4242,
        state="running",
        heartbeat_age_seconds=30.0,
        loop_health=GatewayLoopHealth.TICKING,
        lifecycle_phase="exited",
        last_exit_code=3,
        last_exit_reason="SIGTERM received",
        code_sha="abcdef0123456789abcdef",
        code_version="2026.9.1",
        config_generation_short="0123456789ab",
        session_store_status="ready",
        gateway_incarnation_count=4,
        gateway_restarts_24h=2,
        current_incarnation_uptime_seconds=7200.0,
        platforms=[
            PlatformStatus(
                name="discord",
                state="error",
                needs_attention=True,
                retrying_since_age_seconds=600.0,
                error_code="AUTH",
            )
        ],
    )
    return DashboardState(gateway=gateway.model_copy(update=overrides))


def test_gateway_compact_shows_loop_status_and_warnings() -> None:
    state = _liveness_state(
        config_stale=True,
        runtime_code_skew=True,
        runtime_code_skew_source="fleet",
        last_update_outcome="failed",
        update_receipt_unfinished=True,
        pending_delivery_count=2,
        failed_delivery_count=1,
    )
    rendered = render_to_str(render_gateway(state, Theme()), no_color=True)

    assert "loop:ticking" in rendered
    assert "config changed, restart needed" in rendered
    assert "update unfinished" in rendered
    assert "code skew" in rendered
    assert "2 pending" in rendered
    assert "1 failed" in rendered


def test_gateway_compact_does_not_call_a_successful_update_failed() -> None:
    """Upstream writes outcome="success"; only an unfinished run deserves a warning."""
    state = _liveness_state(last_update_outcome="success", update_receipt_unfinished=False)
    rendered = render_to_str(render_gateway(state, Theme()), no_color=True)

    assert "update failed" not in rendered
    assert "update unfinished" not in rendered


def test_gateway_compact_omits_warnings_when_healthy() -> None:
    rendered = render_to_str(render_gateway(_liveness_state(), Theme()), no_color=True)

    assert "loop:ticking" in rendered
    assert "restart needed" not in rendered
    assert "code skew" not in rendered
    assert "Deliveries" not in rendered


def test_gateway_detail_labels_skew_from_recorded_fleet() -> None:
    state = _liveness_state(
        last_update_outcome="success",
        runtime_code_skew=True,
        runtime_code_skew_source="fleet",
        update_fleet_runtime_count=2,
        update_fleet_states={"current": 1, "stale": 1},
    )
    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=160, no_color=True)

    assert "recorded post-restart fleet" in rendered
    assert "pre-update plan" not in rendered
    assert "Recorded fleet: 2" in rendered
    assert "current 1" in rendered
    assert "stale 1" in rendered


def test_gateway_detail_labels_skew_from_unfinished_plan() -> None:
    state = _liveness_state(
        last_update_outcome="partial",
        update_receipt_unfinished=True,
        runtime_code_skew=True,
        runtime_code_skew_source="plan",
    )
    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=160, no_color=True)

    assert "update never finished" in rendered
    assert "pre-update plan" in rendered
    assert "recorded post-restart fleet" not in rendered


def test_gateway_detail_notes_when_skew_is_unassessable() -> None:
    """A receipt with no fleet matrix and a clean finish proves nothing about skew."""
    state = _liveness_state(
        last_update_outcome="success",
        runtime_code_skew=False,
        runtime_code_skew_source="",
    )
    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=160, no_color=True)

    assert "skew not assessable" in rendered


def test_gateway_detail_escapes_hostile_fleet_state_names() -> None:
    state = _liveness_state(
        last_update_outcome="success",
        update_fleet_runtime_count=1,
        update_fleet_states={HOSTILE: 1},
    )
    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    assert "\x1b[2J" not in rendered


def test_gateway_detail_shows_liveness_lifecycle_updates_and_deliveries() -> None:
    state = _liveness_state(
        config_stale=True,
        unclean_previous_exit=True,
        exit_reason="crashed on boot",
        last_update_outcome="failed",
        update_receipt_unfinished=True,
        last_update_finished_age_seconds=600.0,
        last_update_from_version="2026.8.1",
        last_update_to_version="2026.9.1",
        last_update_failed_step="install",
        runtime_code_skew=True,
        runtime_code_skew_source="fleet",
        pending_delivery_count=2,
        failed_delivery_count=1,
        pending_deliveries=[
            DeliveryObligationSummary(
                platform="telegram",
                state="pending",
                attempts=2,
                age_seconds=300.0,
                last_error="network unreachable",
            )
        ],
    )
    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=160, no_color=True)

    assert "Liveness" in rendered
    assert "ticking" in rendered
    assert "30s ago" in rendered
    assert "incarnations 4" in rendered
    assert "restarts 24h 2" in rendered
    assert "uptime 2h" in rendered
    assert "abcdef012345" in rendered
    assert "abcdef0123456789" not in rendered
    assert "2026.9.1" in rendered
    assert "ready" in rendered
    assert "config changed, restart needed" in rendered
    assert "Lifecycle" in rendered
    assert "SIGTERM received" in rendered
    assert "crashed on boot" in rendered
    assert "without recording an exit" in rendered
    assert "Updates" in rendered
    assert "2026.8.1" in rendered
    assert "Failed step: install" in rendered
    assert "runtime code skew" in rendered
    assert "Delivery Obligations" in rendered
    assert "network unreachable" in rendered
    assert "! needs attention" in rendered
    assert "10m" in rendered


def test_gateway_detail_empty_state_uses_placeholders() -> None:
    rendered = render_to_str(render_gateway(DashboardState(), Theme(), detail=True), no_color=True)

    assert "loop" not in rendered.lower().split("liveness")[0]
    assert "unknown" in rendered
    assert "—" in rendered
    assert "Updates" not in rendered
    assert "Delivery Obligations" not in rendered


def test_gateway_compact_empty_state_renders() -> None:
    rendered = render_to_str(render_gateway(DashboardState(), Theme()), no_color=True)

    assert "loop:unknown" in rendered


@pytest.mark.parametrize("detail", [False, True])
def test_gateway_survives_markup_hostile_liveness_values(detail: bool) -> None:
    state = _liveness_state(
        exit_reason=HOSTILE,
        last_exit_reason=HOSTILE,
        lifecycle_phase=HOSTILE,
        code_version=HOSTILE,
        session_store_status=HOSTILE,
        config_generation_short=HOSTILE,
        last_update_outcome=HOSTILE,
        last_update_failed_step=HOSTILE,
        last_update_from_version=HOSTILE,
        config_stale=True,
        pending_delivery_count=1,
        pending_deliveries=[
            DeliveryObligationSummary(
                platform=HOSTILE,
                state=HOSTILE,
                attempts=1,
                age_seconds=1.0,
                last_error=HOSTILE,
            )
        ],
    )

    rendered = render_to_str(
        render_gateway(state, Theme(), detail=detail), width=200, no_color=True
    )

    # Compact shows only fixed warning text; detail is where the hostile
    # free-text fields land, and they must survive as literal characters.
    if detail:
        assert "[/] boom" in rendered
    assert "\x1b[2J" not in rendered


def test_gateway_detail_renders_day_scale_durations() -> None:
    state = _liveness_state(current_incarnation_uptime_seconds=200_000.0)
    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=160, no_color=True)

    assert "uptime 2d" in rendered


# F05 — writer ownership is rendered separately from freshness.


def _ownership_state(*ownerships: PlatformOwnership) -> DashboardState:
    return DashboardState(
        gateway=GatewayState(
            running=True,
            pid=4242,
            platforms=[
                PlatformStatus(name=f"platform-{index}", state="connected", ownership=ownership)
                for index, ownership in enumerate(ownerships)
            ],
        )
    )


def test_gateway_detail_marks_a_preserved_platform_record() -> None:
    state = _ownership_state(PlatformOwnership.PRESERVED)

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=160, no_color=True)

    assert "Owner" in rendered
    assert "preserved" in rendered


def test_gateway_detail_labels_a_current_platform_record() -> None:
    state = _ownership_state(PlatformOwnership.CURRENT)

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=160, no_color=True)

    assert "current" in rendered
    assert "preserved" not in rendered


def test_gateway_detail_does_not_claim_ownership_it_cannot_verify() -> None:
    state = _ownership_state(PlatformOwnership.UNVERIFIABLE)

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=160, no_color=True)

    assert "current" not in rendered
    assert "preserved" not in rendered


def test_gateway_compact_warns_about_preserved_platform_records() -> None:
    state = _ownership_state(PlatformOwnership.CURRENT, PlatformOwnership.PRESERVED)

    rendered = render_to_str(render_gateway(state, Theme()), no_color=True)

    assert "1 platform record(s) outlived their writer" in rendered


def test_gateway_compact_stays_quiet_when_ownership_is_current_or_unknown() -> None:
    state = _ownership_state(PlatformOwnership.CURRENT, PlatformOwnership.UNVERIFIABLE)

    rendered = render_to_str(render_gateway(state, Theme()), no_color=True)

    assert "outlived their writer" not in rendered


@pytest.mark.parametrize("detail", [False, True])
def test_gateway_never_renders_raw_writer_identity_stamps(detail: bool) -> None:
    """Upstream strips writer_pid/writer_start_time from its public status endpoint
    as process recon. hermesd keeps them in state because the ownership verdict is
    computed from them, but only the verdict is ever rendered."""
    state = DashboardState(
        gateway=GatewayState(
            running=True,
            pid=4242,
            platforms=[
                PlatformStatus(
                    name="telegram",
                    state="connected",
                    writer_pid=987654,
                    writer_start_time=178921152673,
                    ownership=PlatformOwnership.PRESERVED,
                )
            ],
        )
    )

    rendered = render_to_str(
        render_gateway(state, Theme(), detail=detail), width=200, no_color=True
    )

    assert "987654" not in rendered
    assert "178921152673" not in rendered
    if detail:
        assert "preserved" in rendered
    else:
        assert "outlived their writer" in rendered
