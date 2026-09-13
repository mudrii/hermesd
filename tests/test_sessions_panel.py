"""Tests for the [2] Sessions panel."""

from __future__ import annotations

import pytest

import hermesd.panels.sessions as sessions_module
from hermesd.models import (
    ActiveSurface,
    ConversationGeneration,
    DashboardState,
    GatewayHygieneState,
    GatewayRouteState,
    ProcessLiveness,
    SessionCoordinationState,
    SessionInfo,
    SessionLease,
    SessionLeaseKind,
    TerminalBreadcrumb,
    TerminalSessionReadout,
)
from hermesd.panels.sessions import render_sessions
from hermesd.theme import Theme
from tests.conftest import render_to_str

# Frozen collection clock: session ages are rendered against state.collected_at,
# so tests pin both ends and never depend on wall-clock time.
_NOW = 1_800_000_000.0


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
        "started_at": _NOW - 7200,
        "is_active": True,
    }
    base.update(overrides)
    return SessionInfo(**base)  # type: ignore[arg-type]


def _rich_state() -> DashboardState:
    return DashboardState(
        collected_at=_NOW,
        sessions=[
            _session(
                display_name="Dashboard work",
                title="raw title",
                title_source="llm",
                git_branch="feat/session-model-usage",
                profile_name="coding",
                pinned=True,
                chat_type="direct",
                last_activity_at=_NOW - 45,
                last_activity_description="edited db.py",
                actual_cost_usd=0.55,
                cost_source="provider",
                api_call_count=9,
                cwd="/tmp/project",
                compression_failure_error="compression failed: context too large",
            )
        ],
        active_surfaces=[
            ActiveSurface(
                session_id="sess_abcdef12",
                surface="cli",
                pid=111,
                process_start_time=1000.0,
                liveness=ProcessLiveness.LIVE,
            ),
            ActiveSurface(
                session_id="sess_abcdef12",
                surface="telegram",
                pid=222,
                process_start_time=1000.0,
                liveness=ProcessLiveness.DEAD,
            ),
            ActiveSurface(
                session_id="sess_abcdef34",
                surface="cli",
                pid=333,
                liveness=ProcessLiveness.UNVERIFIABLE,
            ),
        ],
        active_surface_count=3,
    )


@pytest.mark.parametrize("detail", [False, True])
def test_sessions_panel_renders_empty_state(detail: bool) -> None:
    rendered = render_to_str(render_sessions(DashboardState(), Theme(), detail=detail))
    assert "Sessions" in rendered


def test_compact_shows_live_surface_count() -> None:
    """The registry count is entries; only identity-verified surfaces count as live."""
    rendered = render_to_str(render_sessions(_rich_state(), Theme()), width=100)

    assert "3 surface(s)" in rendered
    assert "1 live" in rendered
    assert "1 unverified" in rendered


def test_compact_does_not_call_unverified_surfaces_live() -> None:
    state = DashboardState(
        active_surfaces=[
            ActiveSurface(session_id="s1", surface="cli", pid=111),
            ActiveSurface(session_id="s2", surface="cli", pid=222),
        ],
        active_surface_count=2,
    )

    rendered = render_to_str(render_sessions(state, Theme()), width=100)

    assert "2 surface(s)" in rendered
    assert "2 unverified" in rendered
    assert "live" not in rendered


def test_detail_shows_the_three_liveness_states() -> None:
    rendered = render_to_str(render_sessions(_rich_state(), Theme(), detail=True), width=140)

    assert "live" in rendered
    assert "dead" in rendered
    assert "unverified" in rendered


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
    state = DashboardState(
        collected_at=_NOW, sessions=[_session(started_at=_NOW - 120, git_branch="main")]
    )

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

    assert "Live Surfaces" in rendered
    assert "3 registry entries" in rendered
    assert "0 shown" in rendered


def test_filtered_surface_rows_do_not_hide_registry_capacity_warning() -> None:
    state = _rich_state()
    state.config.max_concurrent_sessions = 3

    rendered = render_to_str(
        render_sessions(state, Theme(), detail=True, filter_query="id:sess_abcdef12"),
        width=200,
    )

    assert "cap 3 leases" in rendered
    assert "3 registry entries" in rendered
    assert "2 shown" in rendered


def test_detail_labels_a_truncated_active_surface_registry() -> None:
    state = _rich_state().model_copy(
        update={"active_surface_count": 201, "active_surfaces_truncated": True}
    )

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "201 registry entries" in rendered
    assert "3 shown from the first 3 retained" in rendered
    assert "capacity uses the full registry count" in rendered


def test_detail_warns_about_compression_failures() -> None:
    rendered = render_to_str(render_sessions(_rich_state(), Theme(), detail=True), width=200)

    assert "compression failed" in rendered


def test_detail_truncates_long_compression_error() -> None:
    state = DashboardState(sessions=[_session(compression_failure_error="x" * 200)])

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "x" * 61 not in rendered
    assert "x" * 40 in rendered


# --------------------------------------------------------------------------
# active compression recovery (the durable half of the anti-thrash guard)
# --------------------------------------------------------------------------


def _recovery_state(**overrides: object) -> DashboardState:
    """A state whose single session carries the given compression-recovery row."""
    return DashboardState(
        collected_at=_NOW,
        sessions=[_session(**overrides)],  # type: ignore[arg-type]
    )


def test_detail_warns_about_a_live_compression_cooldown() -> None:
    state = _recovery_state(compression_failure_cooldown_until=_NOW + 45)

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200, no_color=True)

    assert "recovery — cooldown 45s left" in rendered


def test_detail_warns_about_a_future_anti_thrash_recovery_deadline() -> None:
    state = _recovery_state(compression_recovery_deadline=_NOW + 240)

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200, no_color=True)

    assert "recovery — anti-thrash probe in 4m" in rendered


def test_detail_shows_both_timers_and_both_counters_on_one_line() -> None:
    state = _recovery_state(
        compression_failure_cooldown_until=_NOW + 45,
        compression_recovery_deadline=_NOW + 240,
        compression_fallback_streak=2,
        compression_ineffective_count=3,
    )

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=240, no_color=True)

    assert (
        "recovery — cooldown 45s left, anti-thrash probe in 4m, "
        "fallback streak 2, ineffective 3" in rendered
    )


def test_detail_renders_a_sub_second_cooldown_with_one_decimal() -> None:
    state = _recovery_state(compression_failure_cooldown_until=_NOW + 0.5)

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200, no_color=True)

    assert "recovery — cooldown 0.5s left" in rendered


def test_detail_omits_an_expired_compression_cooldown() -> None:
    """An expired cooldown is cleared state, not a warning."""
    state = _recovery_state(compression_failure_cooldown_until=_NOW - 1)

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "recovery" not in rendered
    assert "Warnings" not in rendered


def test_detail_omits_an_elapsed_recovery_deadline() -> None:
    state = _recovery_state(compression_recovery_deadline=_NOW - 300)

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "anti-thrash" not in rendered


def test_detail_treats_a_zero_recovery_deadline_as_disarmed_not_1970() -> None:
    """Upstream stores 0 for "not armed"; it must never render as an epoch."""
    state = _recovery_state(compression_recovery_deadline=0.0)

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "1970" not in rendered
    assert "anti-thrash" not in rendered
    assert "recovery" not in rendered


def test_detail_shows_counters_only_beside_a_live_timer() -> None:
    """A tripped strike count with no armed clock is state, not active recovery."""
    state = _recovery_state(compression_fallback_streak=4, compression_ineffective_count=2)

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "recovery" not in rendered
    assert "fallback streak" not in rendered


def test_detail_counts_down_against_the_collected_clock() -> None:
    """The same stored deadline is a warning at one clock reading and gone at another."""
    session = _session(compression_recovery_deadline=_NOW + 100)

    armed = render_to_str(
        render_sessions(DashboardState(collected_at=_NOW, sessions=[session]), Theme(), True),
        width=200,
    )
    elapsed = render_to_str(
        render_sessions(DashboardState(collected_at=_NOW + 101, sessions=[session]), Theme(), True),
        width=200,
    )

    assert "anti-thrash probe in 1m" in armed
    assert "anti-thrash" not in elapsed


def test_detail_says_compression_warnings_never_read_conversation_content() -> None:
    state = _recovery_state(compression_failure_cooldown_until=_NOW + 45)

    rendered = " ".join(
        render_to_str(render_sessions(state, Theme(), detail=True), width=200).split()
    ).lower()

    assert "never reads conversation content" in rendered
    assert "threshold" not in rendered


def test_detail_shows_no_compression_threshold() -> None:
    """A configured threshold is policy, not the effective runtime value, so the
    warnings section shows none."""
    state = _recovery_state(
        compression_failure_error="boom",
        compression_failure_cooldown_until=_NOW + 45,
    )

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200).lower()

    assert "threshold" not in rendered
    assert "0.86" not in rendered


def test_compact_marks_active_compression_recovery() -> None:
    state = _recovery_state(compression_failure_cooldown_until=_NOW + 45)

    rendered = render_to_str(render_sessions(state, Theme(), detail=False), width=200)

    assert "1 compression recovery" in rendered


def test_compact_omits_the_recovery_marker_when_nothing_is_active() -> None:
    state = _recovery_state(compression_recovery_deadline=_NOW - 5)

    rendered = render_to_str(render_sessions(state, Theme(), detail=False), width=200)

    assert "compression recovery" not in rendered


def test_detail_recovery_warning_sanitizes_the_session_id() -> None:
    state = DashboardState(
        collected_at=_NOW,
        sessions=[
            _session(
                session_id="sess_[/]x\x1b[2J",
                compression_recovery_deadline=_NOW + 60,
            )
        ],
    )

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "\x1b[2J" not in rendered
    assert "recovery —" in rendered


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


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        (0, "0s"),
        (30, "30s"),
        (59, "59s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86399, "23h"),
        (86400, "1d"),
        (3 * 86400, "3d"),
    ],
)
def test_detail_activity_age_label_tiers(age_seconds: int, expected: str) -> None:
    """Every `_age_label` tier, read off the Age column of the activity table."""
    state = DashboardState(
        collected_at=_NOW,
        sessions=[_session(git_branch="age-probe", last_activity_at=_NOW - age_seconds)],
    )

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200, no_color=True)
    row = next(line for line in rendered.splitlines() if "age-probe" in line)

    # Columns are ID, Name, Branch, Profile, Chat, Age, Last Activity.
    assert row.split()[-2] == expected


def test_detail_activity_age_label_is_dash_without_a_timestamp() -> None:
    state = DashboardState(
        collected_at=_NOW,
        sessions=[_session(git_branch="age-probe", started_at=0.0, last_activity_at=0.0)],
    )

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200, no_color=True)
    row = next(line for line in rendered.splitlines() if "age-probe" in line)

    assert row.split()[-2] == "—"


def test_detail_age_uses_collected_at_not_wall_clock() -> None:
    """Ages are measured against the injected collection clock."""
    state = DashboardState(
        collected_at=_NOW, sessions=[_session(last_activity_at=_NOW - 3600, git_branch="main")]
    )

    rendered = render_to_str(render_sessions(state, Theme(), detail=True), width=200)

    assert "1h" in rendered


def test_recent_sort_prefers_latest_activity_over_start_time() -> None:
    """A resumed old session sorts above a less-active newer one, matching the age column."""
    old_resumed = _session(
        session_id="sess_oldres", started_at=_NOW - 7200, last_activity_at=_NOW - 30
    )
    new_idle = _session(
        session_id="sess_newidl", started_at=_NOW - 300, last_activity_at=_NOW - 240
    )

    ordered = sessions_module._sort_sessions([new_idle, old_resumed], "recent")

    assert [session.session_id for session in ordered] == ["sess_oldres", "sess_newidl"]


def test_recent_sort_falls_back_to_started_at_without_activity() -> None:
    older = _session(session_id="sess_older", started_at=_NOW - 7200)
    newer = _session(session_id="sess_newer", started_at=_NOW - 300)

    ordered = sessions_module._sort_sessions([older, newer], "recent")

    assert [session.session_id for session in ordered] == ["sess_newer", "sess_older"]


def test_recent_sort_tiebreaks_on_session_id() -> None:
    sess_b = _session(session_id="sess_b", started_at=_NOW - 300, last_activity_at=_NOW - 60)
    sess_a = _session(session_id="sess_a", started_at=_NOW - 100, last_activity_at=_NOW - 60)

    ordered = sessions_module._sort_sessions([sess_a, sess_b], "recent")

    assert [session.session_id for session in ordered] == ["sess_b", "sess_a"]


def _counting_sort_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    real_sort = sessions_module._sort_sessions

    def counting_sort(sessions: list[SessionInfo], session_sort: str) -> list[SessionInfo]:
        calls[0] += 1
        return real_sort(sessions, session_sort)

    monkeypatch.setattr(sessions_module, "_sort_sessions", counting_sort)
    return calls


def test_detail_memoizes_filter_and_sort_for_unchanged_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2 Hz render loop must not re-filter/re-sort between collects."""
    state = DashboardState(sessions=[_session(session_id="sess_a"), _session(session_id="sess_b")])
    sort_calls = _counting_sort_spy(monkeypatch)

    render_sessions(state, Theme(), detail=True, filter_query="source:cli", session_sort="cost")
    render_sessions(state, Theme(), detail=True, filter_query="source:cli", session_sort="cost")

    assert sort_calls[0] == 1


def test_detail_recomputes_when_state_filter_or_sort_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = DashboardState(sessions=[_session(session_id="sess_a")])
    sort_calls = _counting_sort_spy(monkeypatch)

    render_sessions(state, Theme(), detail=True)
    render_sessions(state, Theme(), detail=True, filter_query="cli")
    render_sessions(state, Theme(), detail=True, filter_query="cli", session_sort="cost")
    # Same content in a new state object (a fresh collect) must recompute.
    render_sessions(DashboardState(sessions=[_session(session_id="sess_a")]), Theme(), detail=True)

    assert sort_calls[0] == 4


def test_detail_recomputes_when_message_match_ids_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = DashboardState(sessions=[_session(session_id="sess_a")])
    sort_calls = _counting_sort_spy(monkeypatch)
    match_ids = {"sess_a"}

    render_sessions(state, Theme(), detail=True, message_match_ids=match_ids)
    render_sessions(state, Theme(), detail=True, message_match_ids=match_ids)
    # A new set object signals a completed message search, even with equal contents.
    render_sessions(state, Theme(), detail=True, message_match_ids=set(match_ids))

    assert sort_calls[0] == 2


def _scroll_state(count: int) -> DashboardState:
    """Sessions sorted 'recent' render sess{count-1} first, sess0000 last."""
    sessions = [SessionInfo(session_id=f"sess{i:04d}", started_at=float(i)) for i in range(count)]
    return DashboardState(sessions=sessions)


def test_sessions_detail_keeps_all_rows_for_snapshot_and_viewport() -> None:
    rendered = render_to_str(render_sessions(_scroll_state(40), Theme(), detail=True))
    assert "sess0039" in rendered
    assert "sess0019" in rendered
    assert "sess0000" in rendered


def test_sessions_detail_under_window_shows_every_row_without_hint() -> None:
    rendered = render_to_str(render_sessions(_scroll_state(10), Theme(), detail=True))
    assert "sess0000" in rendered
    assert "sess0009" in rendered
    assert "j/k scroll" not in rendered
    assert "/10]" not in rendered


def test_sessions_detail_preserves_fifty_row_table_cap() -> None:
    rendered = render_to_str(render_sessions(_scroll_state(60), Theme(), detail=True))
    assert "… and 10 more" in rendered
    assert "sess0010" in rendered
    assert "sess0009" not in rendered


# ── Session coordination sections (items 6-10, 22) ──────────────────────────


def _lease(**overrides: object) -> SessionLease:
    base: dict[str, object] = {
        "kind": SessionLeaseKind.TURN_LEASE,
        "key": "conv-root-1234",
        "holder": "pid=101:tid=7:agent=1f:nonce=abcd1234",
        "pid": 101,
        "held_seconds": 60,
        "expires_in_seconds": 240,
        "liveness": ProcessLiveness.LIVE,
    }
    base.update(overrides)
    return SessionLease(**base)  # type: ignore[arg-type]


def _coordination_state(**overrides: object) -> DashboardState:
    base: dict[str, object] = {"collected_at": _NOW}
    base.update(overrides)
    return DashboardState(**base)  # type: ignore[arg-type]


def test_detail_labels_orphaned_and_expired_leases() -> None:
    leases = [
        _lease(),
        _lease(
            kind=SessionLeaseKind.COMPRESSION_LOCK,
            key="sess-lock",
            pid=102,
            liveness=ProcessLiveness.DEAD,
            expired=True,
            expires_in_seconds=-45,
        ),
    ]
    rendered = render_to_str(
        render_sessions(
            _coordination_state(
                session_coordination=SessionCoordinationState(leases=leases, lease_total=2)
            ),
            Theme(),
            detail=True,
        )
    )
    assert "Turn Leases" in rendered
    assert "orphaned" in rendered
    assert "expired" in rendered
    assert "revive" in rendered  # upstream revives before stealing


def test_detail_lease_note_explains_revivable_expiry() -> None:
    rendered = render_to_str(
        render_sessions(
            _coordination_state(
                session_coordination=SessionCoordinationState(
                    leases=[_lease(expired=True, expires_in_seconds=-30)], lease_total=1
                )
            ),
            Theme(),
            detail=True,
        )
    )
    assert "revive" in rendered


def test_compact_counts_leases_and_flags() -> None:
    coord = SessionCoordinationState(
        leases=[
            _lease(expired=True, expires_in_seconds=-120),
            _lease(
                kind=SessionLeaseKind.COMPRESSION_LOCK,
                pid=102,
                liveness=ProcessLiveness.DEAD,
                key="s",
            ),
        ],
        lease_total=2,
    )
    rendered = render_to_str(
        render_sessions(_coordination_state(session_coordination=coord), Theme())
    )
    assert "2 lease(s)" in rendered
    assert "1 expired" in rendered
    assert "1 orphaned" in rendered


def test_detail_hygiene_alert_row_for_suspended_compaction() -> None:
    coord = SessionCoordinationState(
        hygiene=[
            GatewayHygieneState(
                session_key="telegram:42:7",
                failure_streak=4,
                suspended=True,
                compression_failure_error="summary model timeout",
            )
        ]
    )
    rendered = render_to_str(
        render_sessions(_coordination_state(session_coordination=coord), Theme(), detail=True)
    )
    assert "telegram:42:7" in rendered
    assert "4" in rendered
    assert "compaction suspended" in rendered
    assert "1h" in rendered
    assert "summary model timeout" in rendered


def test_compact_shows_hygiene_badge_with_streak() -> None:
    coord = SessionCoordinationState(
        hygiene=[GatewayHygieneState(session_key="telegram:42:7", failure_streak=4, suspended=True)]
    )
    rendered = render_to_str(
        render_sessions(_coordination_state(session_coordination=coord), Theme())
    )
    assert "hygiene" in rendered
    assert "1" in rendered


def test_detail_route_table_flags_resume_dangling_and_stuck_turn() -> None:
    coord = SessionCoordinationState(
        routes=[
            GatewayRouteState(
                session_key="telegram:42:7",
                session_id="ghost",
                platform="telegram",
                chat_type="group",
                display_name="dev chat",
                turn_age_seconds=900,
                resume_pending=True,
                dangling=True,
            )
        ],
        route_total=1,
    )
    rendered = render_to_str(
        render_sessions(_coordination_state(session_coordination=coord), Theme(), detail=True)
    )
    assert "Chat Routes" in rendered
    assert "telegram:42:7" in rendered
    assert "dangling" in rendered
    assert "resume" in rendered
    assert "never unwound" in rendered
    assert "user message" in rendered


def test_detail_reset_churn_totals_and_shrink_warning() -> None:
    coord = SessionCoordinationState(
        generations=[ConversationGeneration(source="cli", session_key="k2", generation=9)],
        generation_chat_total=3,
        generation_reset_total=16,
        generation_count_shrank=True,
    )
    rendered = render_to_str(
        render_sessions(_coordination_state(session_coordination=coord), Theme(), detail=True)
    )
    assert "Reset Churn" in rendered
    assert "16" in rendered
    assert "3" in rendered
    assert "never prunes" in rendered or "never pruned" in rendered


def test_detail_terminal_copy_says_upper_bound() -> None:
    readout = TerminalSessionReadout(
        sessions=[
            TerminalBreadcrumb(
                terminal="tty-dev-pts-3",
                session_id="sess_t",
                cwd="/repo/checkout",
                age_seconds=7200,
            )
        ],
        count=1,
    )
    rendered = render_to_str(
        render_sessions(_coordination_state(terminal_sessions=readout), Theme(), detail=True)
    )
    assert "CLI Terminals" in rendered
    assert "24 hours" in rendered
    assert "upper bound" in rendered
    # cwd renders under the panel's last-segment convention (as the Runtime table)
    assert "checkout" in rendered
    assert "tty-dev-pts-3" in rendered
    assert "sess_t" in rendered


def test_detail_joinable_chip_on_surface_rows() -> None:
    surfaces = [
        ActiveSurface(
            session_id="sess_abcdef12",
            surface="cli",
            pid=111,
            liveness=ProcessLiveness.LIVE,
            joinable=True,
        )
    ]
    rendered = render_to_str(
        render_sessions(
            _coordination_state(active_surfaces=surfaces, active_surface_count=1),
            Theme(),
            detail=True,
        )
    )
    assert "joinable" in rendered


def test_compact_shows_joinable_count() -> None:
    surfaces = [
        ActiveSurface(
            session_id="sess_abcdef12",
            surface="cli",
            pid=111,
            liveness=ProcessLiveness.LIVE,
            joinable=True,
        ),
        ActiveSurface(
            session_id="sess_abcdef34",
            surface="gateway:telegram",
            pid=112,
            liveness=ProcessLiveness.LIVE,
        ),
    ]
    rendered = render_to_str(
        render_sessions(
            _coordination_state(active_surfaces=surfaces, active_surface_count=2),
            Theme(),
        )
    )
    assert "1 joinable" in rendered


def test_coordination_free_text_is_escaped() -> None:
    coord = SessionCoordinationState(
        hygiene=[
            GatewayHygieneState(
                session_key="[b]evil[/b]",
                failure_streak=5,
                suspended=True,
                compression_failure_error="[i]boom[/i]",
            )
        ],
        routes=[
            GatewayRouteState(
                session_key="[u]k[/u]",
                display_name="[red]name[/red]",
                dangling=True,
            )
        ],
    )
    rendered = render_to_str(
        render_sessions(_coordination_state(session_coordination=coord), Theme(), detail=True)
    )
    plain = rendered
    for hostile in ("[b]evil", "[i]boom", "[red]name"):
        assert hostile in plain  # escaped: markup text survives rendering


def test_detail_lease_dash_states_and_sub_threshold_streak() -> None:
    coord = SessionCoordinationState(
        leases=[
            _lease(
                pid=0,
                held_seconds=None,
                expires_in_seconds=None,
                liveness=ProcessLiveness.UNVERIFIABLE,
            )
        ],
        hygiene=[GatewayHygieneState(session_key="telegram:1:2", failure_streak=2)],
        routes=[GatewayRouteState(session_key="telegram:1:2", suspended=True)],
    )
    rendered = render_to_str(
        render_sessions(_coordination_state(session_coordination=coord), Theme(), detail=True)
    )
    assert "—" in rendered  # no usable hold/TTL stamps
    assert "unverified" in rendered
    assert "compaction cooldown backoff" in rendered
    assert "suspended" in rendered  # the route flag, not the streak effect


def test_reset_churn_and_terminals_render_heads_without_rows() -> None:
    coord = SessionCoordinationState(
        generations=[],
        generation_chat_total=5,
        generation_reset_total=21,
    )
    term = TerminalSessionReadout(sessions=[], count=2)
    rendered = render_to_str(
        render_sessions(
            _coordination_state(session_coordination=coord, terminal_sessions=term),
            Theme(),
            detail=True,
        )
    )
    assert "lifetime resets 21" in rendered
    assert "across 5 chat(s)" in rendered
    assert "2 open CLI terminals in the last 24 hours" in rendered
