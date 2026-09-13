"""Tests for the [1] Gateway & Platforms panel."""

from __future__ import annotations

import pytest

from hermesd.models import (
    DashboardState,
    DeliveryObligationSummary,
    ForensicFile,
    GatewayLoopHealth,
    GatewayState,
    MigrationProfileRecord,
    MigrationState,
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


# F10 — shared-listener routing: profile column, served-record labelling, ingress.


def _routing_state(**gateway_overrides) -> DashboardState:
    gateway = GatewayState(running=True, pid=4242, state="running")
    return DashboardState(gateway=gateway.model_copy(update=gateway_overrides))


def test_gateway_detail_shows_a_profile_column_for_namespaced_platforms() -> None:
    state = _routing_state(
        platforms=[
            PlatformStatus(name="telegram", profile="dev", state="connected"),
            PlatformStatus(name="telegram", state="connected"),
        ]
    )

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    assert "Profile" in rendered
    assert "dev" in rendered


def test_gateway_compact_labels_a_served_profile_platform() -> None:
    """A plain platform name would be ambiguous once two profiles serve the same platform."""
    state = _routing_state(
        platforms=[PlatformStatus(name="telegram", profile="dev", state="connected")]
    )

    rendered = render_to_str(render_gateway(state, Theme()), no_color=True)

    assert "dev/telegram:" in rendered


def test_gateway_detail_lists_recorded_ingress_urls_without_claiming_a_probe() -> None:
    state = _routing_state(
        platforms=[
            PlatformStatus(
                name="telegram",
                profile="dev",
                state="connected",
                ingress_url="https://gw.example/p/dev/telegram/webhook",
            )
        ]
    )

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    assert "Shared-Listener Ingress" in rendered
    assert "https://gw.example/p/dev/telegram/webhook" in rendered
    assert "recorded by the gateway — hermesd never requests these URLs" in rendered
    # The section must not read as an endpoint-availability claim.
    for claim in ("reachable", "responding", "available", "probe"):
        assert claim not in rendered.lower()


def test_gateway_detail_marks_a_path_only_ingress_record() -> None:
    """A bare path is what upstream records when no default listener was live yet."""
    state = _routing_state(
        platforms=[
            PlatformStatus(
                name="telegram",
                profile="dev",
                state="connected",
                ingress_url="/p/dev/telegram/webhook",
            )
        ]
    )

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    assert "/p/dev/telegram/webhook" in rendered
    assert "no live listener" in rendered


def test_gateway_detail_omits_the_ingress_section_without_a_recorded_url() -> None:
    state = _routing_state(platforms=[PlatformStatus(name="telegram", state="connected")])

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    assert "Shared-Listener Ingress" not in rendered


def test_gateway_detail_reports_a_live_served_record() -> None:
    state = _routing_state(served_profiles=["default", "coding"], served_profiles_recorded=True)

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    assert "Served Profiles: default, coding" in rendered
    assert "gateway not live" not in rendered


def test_gateway_detail_reports_an_authoritative_empty_served_set() -> None:
    """An empty list from a live gateway means "serves nobody else", not "unknown"."""
    state = _routing_state(served_profiles=[], served_profiles_recorded=True)

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    assert "none (the live gateway serves no other profile)" in rendered


def test_gateway_detail_labels_a_served_record_that_outlived_its_writer() -> None:
    state = _routing_state(
        running=False,
        state="stopped",
        served_profiles=["default", "coding"],
        served_profiles_recorded=False,
    )

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    assert "Served Profiles (record, writer not current): default, coding" in rendered


def test_gateway_detail_stays_quiet_without_any_served_record() -> None:
    state = _routing_state(served_profiles=[], served_profiles_recorded=False)

    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    assert "Served Profiles" not in rendered


# F19 — a migration manifest is progress evidence, never proof of success.


def _migration_record(profile: str, *, served: bool, home: str = "/h/.hermes") -> object:
    return MigrationProfileRecord(profile=profile, home=home, service_kind="launchd", served=served)


def _migration_dashboard(**overrides) -> DashboardState:
    """A verified multiplex topology, with any clause knocked out by ``overrides``."""
    migration = MigrationState(
        manifest_present=True,
        manifest_parsed=True,
        manifest_schema_valid=True,
        manifest_version=1,
        migrated_at="2026-09-13T00:52:11+0200",
        migrated_at_age_seconds=10_800.0,
        flag_was=False,
        default_profile=MigrationProfileRecord(
            profile="default", home="/h/.hermes", service_kind="launchd", served=True
        ),
        secondaries=[
            _migration_record("dev", served=True),
            _migration_record("coding", served=True, home="/h/.hermes/profiles/coding"),
        ],
        secondary_count=2,
        multiplex_flag_on=True,
        default_gateway_live=True,
        served_recorded=True,
    )
    gateway = GatewayState(
        running=True,
        pid=4242,
        state="running",
        served_profiles=["default", "dev", "coding"],
        served_profiles_recorded=True,
    )
    return DashboardState(gateway=gateway, migration=migration.model_copy(update=overrides))


def _render_migration(state: DashboardState, *, detail: bool = True) -> str:
    return render_to_str(render_gateway(state, Theme(), detail=detail), width=220, no_color=True)


def test_gateway_detail_reports_a_verified_multiplex_topology() -> None:
    rendered = _render_migration(_migration_dashboard())

    assert "Multiplex Migration" in rendered
    assert "multiplexed (verified)" in rendered
    assert "as recorded in config" in rendered


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"served_recorded": False}, "no live gateway recorded a served-profile set"),
        ({"default_gateway_live": False}, "the default gateway is not live"),
        ({"multiplex_flag_on": False}, "gateway.multiplex_profiles is off as recorded in config"),
        ({"manifest_parsed": False}, "gateway_migration.json is present but unreadable"),
        ({"manifest_schema_valid": False}, "manifest schema is malformed or unsupported"),
    ],
)
def test_gateway_detail_names_the_missing_evidence(
    overrides: dict[str, object], expected: str
) -> None:
    """Unverified says *what* could not be verified, never that the migration failed."""
    rendered = _render_migration(_migration_dashboard(**overrides))

    assert "migration unverified" in rendered
    assert expected in rendered
    assert "multiplexed (verified)" not in rendered


def test_gateway_detail_names_the_profiles_the_live_set_does_not_cover() -> None:
    state = _migration_dashboard(
        served_recorded=True,
        secondaries=[
            _migration_record("dev", served=True),
            _migration_record("coding", served=False),
        ],
    )

    rendered = _render_migration(state)

    assert "migration unverified" in rendered
    assert "not in the live served set: coding" in rendered
    assert "dev" in rendered


def test_gateway_detail_reports_a_truncated_secondary_list() -> None:
    state = _migration_dashboard(
        secondary_count=60,
        secondaries_truncated=True,
        secondaries=[_migration_record("dev", served=True)],
    )

    rendered = _render_migration(state)

    assert "migration unverified" in rendered
    assert "only the first 1 of 60 recorded secondaries were retained" in rendered


@pytest.mark.parametrize("detail", [False, True])
def test_the_panel_never_claims_a_migration_finished(detail: bool) -> None:
    """Banned wording: the manifest is written before verification ever runs."""
    verified = _render_migration(_migration_dashboard(), detail=detail)
    unverified = _render_migration(_migration_dashboard(served_recorded=False), detail=detail)

    for rendered in (verified, unverified):
        lowered = rendered.lower()
        assert "migrated" not in lowered
        assert "migration complete" not in lowered
        assert "migration succeeded" not in lowered


def test_gateway_detail_labels_the_manifest_stamp_as_a_start() -> None:
    rendered = _render_migration(_migration_dashboard())

    assert "Started: 2026-09-13T00:52:11+0200" in rendered
    assert "local time" in rendered
    assert "3h ago" in rendered


def test_gateway_detail_does_not_render_an_unparseable_stamp_as_an_age() -> None:
    state = _migration_dashboard(migrated_at="yesterday", migrated_at_age_seconds=None)

    rendered = _render_migration(state)

    assert "Started: yesterday" in rendered
    assert "could not be parsed" in rendered


def test_gateway_detail_omits_the_start_line_without_a_recorded_stamp() -> None:
    """A manifest with no ``migrated_at`` must not invent one, or an age for it."""
    state = _migration_dashboard(migrated_at="", migrated_at_age_seconds=None)

    rendered = _render_migration(state)

    assert "Started:" not in rendered
    assert "Config flag:" in rendered


def test_gateway_detail_separates_recorded_intent_from_progress() -> None:
    """The flag flip and the restart are intermediate progress, not the verdict."""
    rendered = _render_migration(_migration_dashboard())

    assert "Progress:" in rendered
    assert "flag flipped" in rendered
    assert "default gateway live" in rendered
    assert "served record live" in rendered
    assert "every recorded profile is in the live served set" in rendered


def test_gateway_detail_lists_the_recorded_profiles_and_their_coverage() -> None:
    state = _migration_dashboard(
        secondaries=[
            _migration_record("dev", served=True),
            _migration_record("coding", served=False, home="/h/.hermes/profiles/coding"),
        ],
    )

    rendered = _render_migration(state)

    assert "Recorded profiles" in rendered
    assert "/h/.hermes/profiles/coding" in rendered
    assert "launchd" in rendered
    assert "not served" in rendered


def test_gateway_detail_does_not_claim_coverage_it_has_no_record_for() -> None:
    """Without a live served record the coverage cell is unknown, not "not served"."""
    state = _migration_dashboard(
        served_recorded=False,
        default_profile=MigrationProfileRecord(profile="default", served=False),
        secondaries=[_migration_record("dev", served=False)],
    )

    rendered = _render_migration(state)

    assert "not served" not in rendered


def test_gateway_detail_shows_multiplex_config_without_a_manifest() -> None:
    """A hand-configured multiplexer was never migrated: no manifest, no verdict."""
    state = _migration_dashboard(manifest_present=False, manifest_parsed=False)

    rendered = _render_migration(state)

    assert "Multiplex Migration" in rendered
    assert "no migration manifest" in rendered
    assert "migration unverified" not in rendered
    assert "multiplexed (verified)" not in rendered


def test_gateway_detail_omits_the_section_without_a_manifest_or_a_flag() -> None:
    state = _migration_dashboard(
        manifest_present=False, manifest_parsed=False, multiplex_flag_on=False
    )

    rendered = _render_migration(state)

    assert "Multiplex Migration" not in rendered


def test_gateway_compact_warns_about_an_unverified_migration() -> None:
    rendered = _render_migration(_migration_dashboard(served_recorded=False), detail=False)

    assert "migration unverified" in rendered


def test_gateway_compact_stays_quiet_about_a_verified_migration() -> None:
    rendered = _render_migration(_migration_dashboard(), detail=False)

    assert "migration" not in rendered.lower()


def test_gateway_compact_stays_quiet_without_a_manifest() -> None:
    state = _migration_dashboard(manifest_present=False, manifest_parsed=False)

    rendered = _render_migration(state, detail=False)

    assert "migration" not in rendered.lower()


def test_gateway_detail_survives_markup_hostile_migration_values() -> None:
    state = _migration_dashboard(
        migrated_at=HOSTILE,
        default_profile=MigrationProfileRecord(profile=HOSTILE, home=HOSTILE, served=True),
        secondaries=[_migration_record(HOSTILE, served=True, home=HOSTILE)],
    )

    rendered = _render_migration(state)

    assert "\x1b[2J" not in rendered
    assert "[/] boom" in rendered


def test_gateway_compact_renders_alive_and_legacy_loop_badges() -> None:
    alive = _liveness_state(loop_health=GatewayLoopHealth.ALIVE, loop_tick_armed=True)
    rendered = render_to_str(render_gateway(alive, Theme()), width=200, no_color=True)
    assert "loop:alive" in rendered.replace(" ", "")

    legacy = _liveness_state(
        loop_health=GatewayLoopHealth.LEGACY,
        loop_tick_armed=None,
        heartbeat_age_seconds=400.0,
    )
    rendered = render_to_str(render_gateway(legacy, Theme()), width=200, no_color=True)
    assert "legacy" in rendered


def test_gateway_detail_explains_the_loop_witness() -> None:
    state = _liveness_state(loop_health=GatewayLoopHealth.ALIVE, loop_tick_armed=True)
    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)
    assert "witness answered" in rendered

    legacy = _liveness_state(loop_health=GatewayLoopHealth.LEGACY, loop_tick_armed=None)
    rendered = render_to_str(render_gateway(legacy, Theme(), detail=True), width=200, no_color=True)
    assert "legacy heartbeat" in rendered
    assert "staleness alone" in rendered


def test_gateway_renders_lifecycle_carry_flags() -> None:
    state = _liveness_state(prior_unclean_exit=True, prior_suspected_oom=True)
    compact = render_to_str(render_gateway(state, Theme()), width=200, no_color=True)
    detail = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)
    assert "previous exit unclean" in compact
    assert "suspected OOM" in compact
    assert "previous exit unclean" in detail
    assert "suspected OOM" in detail


def test_gateway_renders_restart_storm_line_and_backoff() -> None:
    state = _liveness_state(
        gateway_starts_recorded=True,
        gateway_starts_window=2,
        gateway_starts_1h=9,
        restart_storm_cap=5,
        restart_storm_window_seconds=120.0,
        seconds_since_last_gateway_start=45.0,
    )
    detail = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)
    assert "2m 2/5" in detail
    assert "1h 9" in detail
    assert "last start 45s ago" in detail

    storm = _liveness_state(
        gateway_starts_recorded=True,
        gateway_starts_window=6,
        gateway_starts_1h=11,
        restart_storm_cap=5,
        restart_storm_window_seconds=120.0,
        seconds_since_last_gateway_start=12.0,
        in_respawn_backoff=True,
    )
    compact = render_to_str(render_gateway(storm, Theme()), width=200, no_color=True)
    detail = render_to_str(render_gateway(storm, Theme(), detail=True), width=200, no_color=True)
    assert "respawn backoff" in compact
    assert "2m 6/5" in detail
    assert "respawn backoff" in detail


def test_gateway_compact_hides_restart_line_when_no_ledger() -> None:
    state = _liveness_state(gateway_starts_recorded=False)
    rendered = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)
    assert "Starts:" not in rendered


def test_gateway_renders_dashboard_client_attachment() -> None:
    attached = _liveness_state(
        dashboard_client_attached=True,
        dashboard_client_last_frame_age_seconds=8.0,
    )
    compact = render_to_str(render_gateway(attached, Theme()), width=200, no_color=True)
    detail = render_to_str(render_gateway(attached, Theme(), detail=True), width=200, no_color=True)
    assert "web client" in compact
    assert "web dashboard client attached" in detail
    assert "last frame 8s ago" in detail

    never = _liveness_state(
        dashboard_client_attached=False,
        dashboard_client_last_frame_age_seconds=None,
    )
    detail = render_to_str(render_gateway(never, Theme(), detail=True), width=200, no_color=True)
    assert "no dashboard client marker" in detail


def test_gateway_renders_exit_diagnostics_ledger() -> None:
    state = _liveness_state(
        exit_diag_recorded=True,
        exit_diag_last_tag="gateway.asyncio_main_return",
        exit_diag_last_age_seconds=90.0,
        exit_diag_unclean_24h=2,
        exit_diag_size_bytes=2048,
    )
    detail = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)
    assert "Exit diagnostics:" in detail
    assert "gateway.asyncio_main_return" in detail
    assert "unclean exits 24h: 2" in detail
    assert "oversized" not in detail

    oversized = _liveness_state(
        exit_diag_recorded=True,
        exit_diag_last_tag="t",
        exit_diag_size_bytes=3 * 1024 * 1024,
        exit_diag_oversized=True,
    )
    detail = render_to_str(
        render_gateway(oversized, Theme(), detail=True), width=200, no_color=True
    )
    assert "nothing prunes it" in detail
    compact = render_to_str(render_gateway(oversized, Theme()), no_color=True)
    assert "exit-diag log oversized" in compact


def test_gateway_renders_missing_exit_ledger_as_no_evidence() -> None:
    state = _liveness_state(exit_diag_recorded=False)
    detail = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)
    assert "no exit-diag ledger" in detail


def test_gateway_renders_forensic_companion_files() -> None:
    state = _liveness_state(
        exit_diag_recorded=True,
        forensic_files=[
            ForensicFile(name="gateway_faulthandler.log", size_bytes=4096, age_seconds=3600.0),
        ],
    )
    detail = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)
    assert "gateway_faulthandler.log" in detail
    assert "growth, not absence, is the signal" in detail


def test_gateway_renders_shared_listener_mirrors() -> None:
    state = _liveness_state(
        platforms=[
            PlatformStatus(
                name="api_server",
                state="connected",
                mirror_urls={"dev": "http://127.0.0.1:8088/p/dev/v1"},
            )
        ]
    )
    detail = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)
    assert "shared listener" in detail
    assert "dev" in detail
    assert "http://127.0.0.1:8088/p/dev/v1" in detail

    plain = _liveness_state(platforms=[PlatformStatus(name="telegram", state="connected")])
    detail = render_to_str(render_gateway(plain, Theme(), detail=True), width=200, no_color=True)
    assert "shared listener" not in detail


def test_gateway_detail_puts_each_liveness_diagnostic_on_its_own_line() -> None:
    """Witness, Starts, Web client, Exit diagnostics and Event logs get a line each.

    Each helper returns a bare labelled ``Text`` and the renderer appends them
    in sequence, so without a leading break the detail view ran them together:
    ``… witness answered  Starts: … Web client: … Exit diagnostics: …`` on one
    line, which reads as a single claim instead of five separate facts.
    """
    state = _liveness_state(
        loop_health=GatewayLoopHealth.ALIVE,
        gateway_starts_recorded=True,
        gateway_starts_window=2,
        restart_storm_cap=5,
        seconds_since_last_gateway_start=120.0,
        dashboard_client_attached=True,
        dashboard_client_last_frame_age_seconds=5.0,
        exit_diag_recorded=True,
        exit_diag_last_tag="gateway.start",
        exit_diag_last_age_seconds=14400.0,
        forensic_files=[
            ForensicFile(name="gateway_faulthandler.log", size_bytes=4096, age_seconds=3600.0),
        ],
    )
    detail = render_to_str(render_gateway(state, Theme(), detail=True), width=200, no_color=True)

    labels = ["Witness:", "Starts:", "Web client:", "Exit diagnostics:", "Event logs:"]
    lines = detail.splitlines()
    for label in labels:
        matching = [line for line in lines if label in line]
        assert matching, label
        # Each of these facts owns its line: no other diagnostic shares it.
        assert all(sum(other in line for other in labels) == 1 for line in matching), label
    # The ledger's newest record is not necessarily an exit: say what it is.
    exit_line = next(line for line in lines if "Exit diagnostics:" in line)
    assert "last record" in exit_line
