"""Tests for the [2] Sessions panel."""

from __future__ import annotations

import pytest

import hermesd.panels.sessions as sessions_module
from hermesd.models import ActiveSurface, DashboardState, SessionInfo
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
