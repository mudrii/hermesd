"""F16 — active-session capacity policy and lease details.

Two different limits govern two different resources, and neither is a count of
running turns:

* ``max_concurrent_sessions`` is a cross-process **lease cap** checked when a
  surface attaches (``hermes_cli/active_sessions.py:47-61`` resolution,
  ``:31-44`` coercion, ``:524-532`` enforcement);
* ``max_live_sessions`` is a soft **LRU cap on the gateway's in-memory sessions**
  (``tui_gateway/session_reaper.py:237-247``, eviction at ``:250-265``).

The registry itself records leases, not processes: several entries can share one
pid, and only an identity-verified entry is executing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
import yaml

from hermesd.collector import Collector
from hermesd.models import ActiveSurface, ConfigSummary, DashboardState, ProcessLiveness
from hermesd.panels.config_panel import render_config
from hermesd.panels.sessions import render_sessions
from hermesd.theme import Theme
from tests.conftest import render_to_str

_NOW = 1_800_000_000.0


def _config(home: Path, payload: object) -> ConfigSummary:
    (home / "config.yaml").write_text(yaml.dump(payload))
    collector = Collector(home, clock=lambda: _NOW)
    try:
        return collector.collect().config
    finally:
        collector.close()


def _registry(home: Path, entries: list[dict]) -> None:
    runtime = home / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "active_sessions.json").write_text(json.dumps({"entries": entries}))


def _collect(
    home: Path,
    *,
    observed: Mapping[int, float] | None = None,
    live: frozenset[int] = frozenset(),
    clock: Callable[[], float] = lambda: _NOW,
) -> DashboardState:
    seen = dict(observed or {})

    def probe(_pids: Sequence[int]) -> dict[int, float]:
        return dict(seen)

    collector = Collector(
        home,
        clock=clock,
        pid_exists=lambda pid: pid in live,
        process_start_times=probe,
    )
    try:
        return collector.collect()
    finally:
        collector.close()


# --------------------------------------------------------------------------
# max_concurrent_sessions: the cross-process lease cap
# --------------------------------------------------------------------------


def test_top_level_concurrent_cap_is_read(hermes_home: Path) -> None:
    config = _config(hermes_home, {"max_concurrent_sessions": 4})

    assert config.max_concurrent_sessions == 4
    assert config.active_session_cap_configured is True


def test_concurrent_cap_falls_back_to_the_gateway_section(hermes_home: Path) -> None:
    config = _config(hermes_home, {"gateway": {"max_concurrent_sessions": 6}})

    assert config.max_concurrent_sessions == 6


def test_top_level_concurrent_cap_wins_over_the_gateway_section(hermes_home: Path) -> None:
    config = _config(
        hermes_home,
        {"max_concurrent_sessions": 2, "gateway": {"max_concurrent_sessions": 9}},
    )

    assert config.max_concurrent_sessions == 2


def test_absent_concurrent_cap_is_not_configured(hermes_home: Path) -> None:
    config = _config(hermes_home, {"model": {"default": "gpt-5.4"}})

    assert config.max_concurrent_sessions is None
    assert config.active_session_cap_configured is False


@pytest.mark.parametrize("value", [0, None, "nope", 2.5, True, -3, "", []])
def test_non_positive_or_invalid_concurrent_caps_are_not_configured(
    hermes_home: Path, value: object
) -> None:
    """Upstream coerces 0/null/invalid to *disabled*, which is "not configured"."""
    config = _config(hermes_home, {"max_concurrent_sessions": value})

    assert config.max_concurrent_sessions is None


def test_string_concurrent_cap_is_coerced(hermes_home: Path) -> None:
    assert _config(hermes_home, {"max_concurrent_sessions": " 7 "}).max_concurrent_sessions == 7


def test_integral_float_concurrent_cap_is_coerced(hermes_home: Path) -> None:
    assert _config(hermes_home, {"max_concurrent_sessions": 5.0}).max_concurrent_sessions == 5


def test_explicit_null_concurrent_cap_does_not_reach_the_gateway_section(
    hermes_home: Path,
) -> None:
    """Upstream resolves on key *presence*, so a top-level null shadows gateway.*.

    ``resolve_max_concurrent_sessions`` tests ``"max_concurrent_sessions" in
    config`` before falling back (``hermes_cli/active_sessions.py:50-58``).
    """
    config = _config(
        hermes_home,
        {"max_concurrent_sessions": None, "gateway": {"max_concurrent_sessions": 8}},
    )

    assert config.max_concurrent_sessions is None


# --------------------------------------------------------------------------
# max_live_sessions: the in-memory LRU cap
# --------------------------------------------------------------------------


def test_top_level_live_cap_is_read(hermes_home: Path) -> None:
    config = _config(hermes_home, {"max_live_sessions": 16})

    assert config.max_live_sessions == 16
    assert config.live_session_cap_configured is True


def test_live_cap_falls_back_to_the_gateway_section(hermes_home: Path) -> None:
    config = _config(hermes_home, {"gateway": {"max_live_sessions": 24}})

    assert config.max_live_sessions == 24


def test_null_live_cap_does_reach_the_gateway_section(hermes_home: Path) -> None:
    """Upstream's live-cap reader falls back on a *null value*, not key presence.

    ``_max_live_sessions`` re-reads ``gateway.max_live_sessions`` whenever the
    top-level value is None (``tui_gateway/session_reaper.py:240-244``), which is
    deliberately not the rule ``resolve_max_concurrent_sessions`` uses.
    """
    config = _config(hermes_home, {"max_live_sessions": None, "gateway": {"max_live_sessions": 12}})

    assert config.max_live_sessions == 12


def test_absent_live_cap_reads_as_disabled(hermes_home: Path) -> None:
    """``_load_cfg()`` skips the DEFAULT_CONFIG merge, so unset really is 0/off."""
    config = _config(hermes_home, {"model": {"default": "gpt-5.4"}})

    assert config.max_live_sessions == 0
    assert config.live_session_cap_configured is False


# --------------------------------------------------------------------------
# the two caps are different resources and must never be conflated
# --------------------------------------------------------------------------


def test_the_two_caps_are_read_from_their_own_keys(hermes_home: Path) -> None:
    """A value set under one key must not appear as the other cap."""
    config = _config(hermes_home, {"max_concurrent_sessions": 4})

    assert config.max_concurrent_sessions == 4
    assert config.max_live_sessions == 0


def test_the_live_cap_alone_does_not_configure_the_lease_cap(hermes_home: Path) -> None:
    config = _config(hermes_home, {"max_live_sessions": 16})

    assert config.max_live_sessions == 16
    assert config.max_concurrent_sessions is None
    assert config.active_session_cap_configured is False


def test_the_two_gateway_fallbacks_stay_apart(hermes_home: Path) -> None:
    config = _config(
        hermes_home,
        {"gateway": {"max_concurrent_sessions": 3, "max_live_sessions": 30}},
    )

    assert config.max_concurrent_sessions == 3
    assert config.max_live_sessions == 30


def test_bare_config_summary_configures_neither_cap() -> None:
    config = ConfigSummary()

    assert config.max_concurrent_sessions is None
    assert config.active_session_cap_configured is False
    assert config.live_session_cap_configured is False


# --------------------------------------------------------------------------
# lease metadata
# --------------------------------------------------------------------------


def test_collector_records_lease_metadata(hermes_home: Path) -> None:
    _registry(
        hermes_home,
        [
            {
                "lease_id": "3ce157931396483f8efa57f5a8951fec",
                "session_id": "s1",
                "surface": "desktop",
                "pid": 111,
                "process_start_time": _NOW - 100.0,
                "started_at": _NOW - 90.0,
                "updated_at": _NOW - 90.0,
                "track_liveness": True,
            }
        ],
    )

    surface = _collect(hermes_home, observed={111: _NOW - 100.0}, live=frozenset({111}))
    lease = surface.active_surfaces[0]

    assert lease.lease_id == "3ce157931396483f8efa57f5a8951fec"
    assert lease.started_at_age_seconds == pytest.approx(90.0)
    assert lease.updated_at_age_seconds == pytest.approx(90.0)
    assert lease.track_liveness is True
    assert lease.liveness is ProcessLiveness.LIVE


def test_lease_ages_follow_the_injected_clock(hermes_home: Path) -> None:
    """Ages are clock-relative and recomputed every pass, never cached."""
    _registry(
        hermes_home,
        [{"lease_id": "a", "session_id": "s1", "pid": 111, "started_at": _NOW - 10.0}],
    )

    first = _collect(hermes_home, clock=lambda: _NOW)
    later = _collect(hermes_home, clock=lambda: _NOW + 300.0)

    assert first.active_surfaces[0].started_at_age_seconds == pytest.approx(10.0)
    assert later.active_surfaces[0].started_at_age_seconds == pytest.approx(310.0)


def test_a_transferred_lease_is_marked_renewed(hermes_home: Path) -> None:
    """``updated_at`` only moves past acquisition when upstream transfers a lease."""
    _registry(
        hermes_home,
        [
            {
                "lease_id": "moved",
                "session_id": "s1",
                "pid": 111,
                "started_at": _NOW - 600.0,
                "updated_at": _NOW - 60.0,
            },
            {
                "lease_id": "still",
                "session_id": "s2",
                "pid": 111,
                "started_at": _NOW - 600.0,
                "updated_at": _NOW - 600.0,
            },
        ],
    )

    by_id = {s.lease_id: s for s in _collect(hermes_home).active_surfaces}

    assert by_id["moved"].lease_renewed is True
    assert by_id["still"].lease_renewed is False


def test_a_non_numeric_started_at_yields_no_age(hermes_home: Path) -> None:
    """An ISO string where upstream writes an epoch float is not an age."""
    _registry(
        hermes_home,
        [
            {
                "lease_id": "a",
                "session_id": "s1",
                "pid": 111,
                "started_at": "2026-09-07T10:00:00+00:00",
                "updated_at": None,
            }
        ],
    )

    lease = _collect(hermes_home).active_surfaces[0]

    assert lease.started_at_age_seconds is None
    assert lease.updated_at_age_seconds is None
    assert lease.lease_renewed is False


def test_absent_lease_fields_stay_empty(hermes_home: Path) -> None:
    _registry(hermes_home, [{"session_id": "s1", "surface": "cli", "pid": 111}])

    lease = _collect(hermes_home).active_surfaces[0]

    assert lease.lease_id == ""
    assert lease.track_liveness is False
    assert lease.started_at_age_seconds is None


def test_bare_active_surface_has_no_lease_metadata() -> None:
    surface = ActiveSurface()

    assert surface.lease_id == ""
    assert surface.track_liveness is False
    assert surface.started_at_age_seconds is None
    assert surface.updated_at_age_seconds is None
    assert surface.lease_renewed is False


# --------------------------------------------------------------------------
# three numbers that must never stand in for one another
# --------------------------------------------------------------------------


def test_verified_executing_differs_from_registry_occupancy(hermes_home: Path) -> None:
    """Two entries share one pid, one owner is gone and one identity is unprovable.

    Occupancy is 4 registry entries; verified executing activity is 2; the
    entries name 3 distinct pids. No one of those numbers may stand in for
    another, and the unverifiable entry is not counted as executing.
    """
    _registry(
        hermes_home,
        [
            {"session_id": "a", "pid": 111, "process_start_time": 1000.0},
            {"session_id": "b", "pid": 111, "process_start_time": 1000.0},
            {"session_id": "c", "pid": 222, "process_start_time": 1000.0},
            {"session_id": "d", "pid": 333, "process_start_time": 1000.0},
        ],
    )

    state = _collect(
        hermes_home,
        observed={111: 1000.0, 222: 9000.0},
        live=frozenset({111, 222, 333}),
    )
    by_state = {surface.session_id: surface.liveness for surface in state.active_surfaces}
    executing = sum(
        1 for surface in state.active_surfaces if surface.liveness is ProcessLiveness.LIVE
    )

    # a and b are the same verified process holding two leases.
    assert by_state == {
        "a": ProcessLiveness.LIVE,
        "b": ProcessLiveness.LIVE,
        # pid 222 exists but belongs to a different process now.
        "c": ProcessLiveness.DEAD,
        # pid 333 exists and its start time could not be observed here.
        "d": ProcessLiveness.UNVERIFIABLE,
    }
    assert state.active_surface_count == 4
    assert executing == 2
    assert len({surface.pid for surface in state.active_surfaces}) == 3


# --------------------------------------------------------------------------
# panels
# --------------------------------------------------------------------------


def _capacity_state(**config_fields: object) -> DashboardState:
    return DashboardState(
        collected_at=_NOW,
        config=ConfigSummary(**config_fields),  # type: ignore[arg-type]
        active_surface_count=3,
        active_surfaces=[
            ActiveSurface(
                session_id="sess_a",
                surface="cli",
                pid=111,
                lease_id="3ce157931396483f8efa57f5a8951fec",
                started_at_age_seconds=60.0,
                updated_at_age_seconds=60.0,
                liveness=ProcessLiveness.LIVE,
            ),
            ActiveSurface(
                session_id="sess_b",
                surface="cli",
                pid=111,
                lease_id="67658d79845f492883565f16ed542e8b",
                started_at_age_seconds=120.0,
                updated_at_age_seconds=30.0,
                track_liveness=True,
                liveness=ProcessLiveness.UNVERIFIABLE,
            ),
            ActiveSurface(
                session_id="sess_c",
                surface="desktop",
                pid=222,
                liveness=ProcessLiveness.DEAD,
            ),
        ],
    )


def test_compact_sessions_shows_the_configured_lease_cap() -> None:
    rendered = render_to_str(
        render_sessions(_capacity_state(max_concurrent_sessions=4), Theme()),
        width=120,
        no_color=True,
    )

    assert "3 surface(s)" in rendered
    assert "1 live" in rendered
    assert "cap 4" in rendered


def test_compact_sessions_says_when_no_cap_is_configured() -> None:
    rendered = render_to_str(render_sessions(_capacity_state(), Theme()), width=120, no_color=True)

    assert "3 surface(s)" in rendered
    assert "no cap" in rendered
    assert "cap 4" not in rendered


def test_compact_sessions_never_renders_the_live_session_cap() -> None:
    """``max_live_sessions`` governs a different resource and stays out of panel 2."""
    rendered = render_to_str(
        render_sessions(_capacity_state(max_live_sessions=16), Theme()),
        width=120,
        no_color=True,
    )

    assert "cap 16" not in rendered
    assert "no cap" in rendered


def test_compact_sessions_marks_a_registry_at_its_cap() -> None:
    """Occupancy reaching the cap is the condition upstream refuses a new lease at."""
    rendered = render_to_str(
        render_sessions(_capacity_state(max_concurrent_sessions=3), Theme()),
        width=120,
        no_color=True,
    )

    assert "cap 3" in rendered


def test_detail_sessions_marks_a_registry_at_its_cap() -> None:
    rendered = render_to_str(
        render_sessions(_capacity_state(max_concurrent_sessions=3), Theme(), detail=True),
        width=200,
        no_color=True,
    )

    assert "cap 3 leases" in rendered
    assert "3 registry entries" in rendered


def test_detail_sessions_separates_the_three_numbers() -> None:
    rendered = render_to_str(
        render_sessions(_capacity_state(max_concurrent_sessions=4), Theme(), detail=True),
        width=200,
        no_color=True,
    )

    assert "cap 4 leases" in rendered
    assert "3 registry entries" in rendered
    assert "1 verified executing" in rendered
    assert "1 unverified" in rendered
    assert "1 dead" in rendered
    assert "2 distinct pids" in rendered


def test_detail_sessions_says_when_no_cap_is_configured() -> None:
    rendered = render_to_str(
        render_sessions(_capacity_state(), Theme(), detail=True), width=200, no_color=True
    )

    assert "no active-session cap configured" in rendered
    assert "3 registry entries" in rendered
    assert "1 verified executing" in rendered


def test_detail_sessions_shows_lease_metadata() -> None:
    rendered = render_to_str(
        render_sessions(_capacity_state(), Theme(), detail=True), width=200, no_color=True
    )

    assert "3ce15793" in rendered
    assert "moved" in rendered
    assert "tracked" in rendered


def test_config_panel_names_both_caps_separately() -> None:
    rendered = render_to_str(
        render_config(
            DashboardState(config=ConfigSummary(max_concurrent_sessions=4, max_live_sessions=16)),
            Theme(),
            detail=True,
        ),
        width=200,
        no_color=True,
    )

    assert "Session Capacity" in rendered
    assert "Active-Session Lease Cap" in rendered
    assert "4 (max_concurrent_sessions)" in rendered
    assert "In-Memory Live-Session Cap" in rendered
    assert "16 (max_live_sessions)" in rendered


def test_config_panel_reports_an_unconfigured_lease_cap() -> None:
    rendered = render_to_str(
        render_config(DashboardState(config=ConfigSummary()), Theme(), detail=True),
        width=200,
        no_color=True,
    )

    assert "not configured (unbounded)" in rendered
    assert "LRU eviction off" in rendered


def test_config_panel_capacity_note_keeps_the_resources_apart() -> None:
    rendered = render_to_str(
        render_config(DashboardState(config=ConfigSummary()), Theme(), detail=True),
        width=200,
        no_color=True,
    )

    assert "cross-process lease cap" in rendered
    assert "in-memory sessions" in rendered


def test_active_surface_track_liveness_is_read_strictly(hermes_home: Path, tmp_path: Path):
    """``runtime/active_sessions.json`` is machine-written: `"false"` is not tracked."""
    _registry(
        hermes_home,
        [
            {
                "lease_id": "lease-quoted",
                "session_id": "s1",
                "surface": "desktop",
                "pid": 111,
                "process_start_time": _NOW - 100.0,
                "started_at": _NOW - 90.0,
                "updated_at": _NOW - 90.0,
                "track_liveness": "false",
            }
        ],
    )

    surface = _collect(hermes_home, observed={111: _NOW - 100.0}, live=frozenset({111}))
    lease = surface.active_surfaces[0]

    assert lease.track_liveness is False
