"""Usage analytics derived from session rows: daily series, sources, top sessions, repos."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from hermesd.collect.sessions import _usage_analytics
from hermesd.collector import Collector
from tests.conftest import create_state_db_tables

# 2026-09-23 18:30 UTC is 2026-09-24 02:30 in UTC+8: the local day differs from
# the UTC day, so a UTC bucketing bug shows up as an off-by-one day.
_NOW = datetime(2026, 9, 23, 18, 30, tzinfo=UTC).timestamp()
_HOUR = 3600.0
_DAY = 86400.0


@pytest.fixture
def fixed_utc_plus_8(monkeypatch: pytest.MonkeyPatch):
    """Pin local time to a tzdata-free POSIX zone (UTC+8) and restore it afterwards."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is unavailable on this platform")
    monkeypatch.setenv("TZ", "XXX-08")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _row(session_id: str, started_at: float, **fields: Any) -> dict[str, Any]:
    return {"id": session_id, "started_at": started_at, **fields}


def test_daily_series_covers_fourteen_local_days_oldest_first(fixed_utc_plus_8: None):
    analytics = _usage_analytics([], now=_NOW)

    days = [entry.day for entry in analytics.daily]
    assert len(days) == 14
    assert days[-1] == "2026-09-24"  # local today, not the UTC date
    assert days[0] == "2026-09-11"
    assert all(entry.sessions == 0 for entry in analytics.daily)


def test_daily_series_buckets_by_local_start_day(fixed_utc_plus_8: None):
    # 2026-09-23 17:00 UTC is 2026-09-24 01:00 local: today, although UTC says yesterday.
    local_today = datetime(2026, 9, 23, 17, 0, tzinfo=UTC).timestamp()
    rows = [
        _row("a", local_today, input_tokens=100, output_tokens=10, api_call_count=3),
        _row("b", local_today - _DAY, input_tokens=5, output_tokens=1, api_call_count=None),
        # 14 local days back is outside the window.
        _row("old", local_today - 14 * _DAY, input_tokens=999),
    ]

    by_day = {entry.day: entry for entry in _usage_analytics(rows, now=_NOW).daily}

    assert by_day["2026-09-24"].sessions == 1
    assert by_day["2026-09-24"].input_tokens == 100
    assert by_day["2026-09-24"].output_tokens == 10
    assert by_day["2026-09-24"].api_calls == 3
    assert by_day["2026-09-23"].sessions == 1
    assert by_day["2026-09-23"].api_calls == 0
    assert sum(entry.input_tokens for entry in by_day.values()) == 105


def test_daily_cost_prefers_actual_then_estimate_and_flags_estimates(fixed_utc_plus_8: None):
    rows = [
        _row("billed", _NOW - _HOUR, actual_cost_usd=2.0, estimated_cost_usd=9.0),
        _row("guess", _NOW - _HOUR, estimated_cost_usd=0.5),
    ]

    today = _usage_analytics(rows, now=_NOW).daily[-1]

    assert today.total_cost_usd == pytest.approx(2.5)
    assert today.cost_is_estimated is True

    only_billed = _usage_analytics(rows[:1], now=_NOW).daily[-1]
    assert only_billed.total_cost_usd == pytest.approx(2.0)
    assert only_billed.cost_is_estimated is False


def test_source_breakdown_for_24h_and_7d_windows():
    rows = [
        _row("t1", _NOW - _HOUR, source="telegram", input_tokens=10, estimated_cost_usd=1.0),
        _row("c1", _NOW - 2 * _DAY, source="cron", input_tokens=20, estimated_cost_usd=0.2),
        _row("c2", _NOW - 3 * _DAY, source="cron", input_tokens=30, estimated_cost_usd=0.3),
        _row("x", _NOW - 8 * _DAY, source="cli", input_tokens=40),
        _row("n", _NOW - _HOUR, source=None, input_tokens=None),
    ]

    analytics = _usage_analytics(rows, now=_NOW)

    day = {entry.label: entry for entry in analytics.by_source_24h}
    week = {entry.label: entry for entry in analytics.by_source_7d}
    assert set(day) == {"telegram", "unknown"}
    assert set(week) == {"telegram", "cron", "unknown"}
    assert week["cron"].session_count == 2
    assert week["cron"].input_tokens == 50
    assert week["cron"].total_cost_usd == pytest.approx(0.5)


def test_top_sessions_rank_seven_day_sessions_by_cost_then_tokens():
    rows = [
        _row(f"s{i}", _NOW - i * _HOUR, estimated_cost_usd=float(i), input_tokens=i)
        for i in range(1, 8)
    ]
    rows.append(_row("old", _NOW - 8 * _DAY, estimated_cost_usd=100.0))
    rows.append(
        _row(
            "tie",
            _NOW - _HOUR,
            estimated_cost_usd=7.0,
            input_tokens=500,
            output_tokens=1,
            title="Stored title",
            display_name="Shown name",
            source="telegram",
            model="gpt-5.4",
        )
    )

    top = _usage_analytics(rows, now=_NOW).top_sessions_7d

    assert [entry.session_id for entry in top] == ["tie", "s7", "s6", "s5", "s4"]
    assert top[0].title == "Shown name"
    assert top[0].source == "telegram"
    assert top[0].model == "gpt-5.4"
    assert top[0].input_tokens == 500
    assert top[0].output_tokens == 1
    assert top[0].total_cost_usd == pytest.approx(7.0)
    assert top[1].title == ""


def test_top_session_falls_back_to_stored_title():
    rows = [_row("a", _NOW - _HOUR, title="Stored title", estimated_cost_usd=1.0)]
    assert _usage_analytics(rows, now=_NOW).top_sessions_7d[0].title == "Stored title"


def test_hourly_activity_counts_last_seven_days_by_local_start_hour(fixed_utc_plus_8: None):
    # 2026-09-23 17:00 UTC is 01:00 local.
    one_am = datetime(2026, 9, 23, 17, 0, tzinfo=UTC).timestamp()
    rows = [
        _row("a", one_am),
        _row("b", one_am - _DAY),
        _row("c", one_am - 2 * _DAY + 2 * _HOUR),
        _row("old", one_am - 8 * _DAY),
        _row("junk", "not-a-time"),
    ]

    hourly = _usage_analytics(rows, now=_NOW).hourly_sessions_7d

    assert len(hourly) == 24
    assert hourly[1] == 2
    assert hourly[3] == 1
    assert sum(hourly) == 3


def test_repo_activity_counts_seven_and_thirty_day_sessions():
    rows = [
        _row("a", _NOW - _HOUR, git_repo_root="/src/hermesd"),
        _row("b", _NOW - 10 * _DAY, git_repo_root="/src/hermesd"),
        _row("c", _NOW - 2 * _DAY, git_repo_root="/src/other"),
        _row("d", _NOW - 2 * _DAY, git_repo_root="/src/other"),
        _row("e", _NOW - 40 * _DAY, git_repo_root="/src/ancient"),
        _row("f", _NOW - _HOUR, git_repo_root=None),
        _row("g", _NOW - _HOUR, git_repo_root=""),
    ]

    repos = _usage_analytics(rows, now=_NOW).repos

    assert [(r.repo_root, r.sessions_7d, r.sessions_30d) for r in repos] == [
        ("/src/other", 2, 2),
        ("/src/hermesd", 1, 2),
    ]


def test_repo_activity_is_capped():
    rows = [_row(f"s{i}", _NOW - _HOUR, git_repo_root=f"/src/r{i:02d}") for i in range(15)]
    assert len(_usage_analytics(rows, now=_NOW).repos) == 10


def _write_db(path: Path, *, v021: bool, rows: list[tuple[Any, ...]], extra: str = "") -> None:
    conn = sqlite3.connect(str(path))
    create_state_db_tables(conn, include_schema_version=False, include_v021_columns=v021)
    if extra:
        conn.executescript(extra)
    conn.executemany(
        "INSERT INTO sessions (id, source, started_at, input_tokens) VALUES (?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()


def test_collector_reads_repo_and_transport_profile_columns(hermes_home: Path):
    db_path = hermes_home / "state.db"
    _write_db(
        db_path,
        v021=True,
        rows=[("s1", "telegram", _NOW - _HOUR, 10)],
        extra=(
            "ALTER TABLE sessions ADD COLUMN git_repo_root TEXT;"
            "ALTER TABLE sessions ADD COLUMN transport_profile TEXT;"
        ),
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE sessions SET git_repo_root = '/src/hermesd', transport_profile = 'work-bot'"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.sessions[0].transport_profile == "work-bot"
    assert [r.repo_root for r in state.usage_analytics.repos] == ["/src/hermesd"]
    assert state.usage_analytics.by_source_24h[0].label == "telegram"
    assert len(state.usage_analytics.daily) == 14
    assert "usage_analytics" not in state.health.failed_sources
    payload = state.model_dump(mode="json")
    assert payload["usage_analytics"]["repos"][0]["repo_root"] == "/src/hermesd"
    assert payload["sessions"][0]["transport_profile"] == "work-bot"


def test_collector_tolerates_databases_without_repo_or_transport_columns(hermes_home: Path):
    _write_db(hermes_home / "state.db", v021=False, rows=[("s1", "cli", _NOW - _HOUR, 10)])

    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.sessions[0].transport_profile == ""
    assert state.usage_analytics.repos == []
    assert state.usage_analytics.top_sessions_7d[0].session_id == "s1"
    assert "usage_analytics" not in state.health.failed_sources


def test_usage_analytics_keeps_last_good_when_session_rows_fail(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_db(hermes_home / "state.db", v021=False, rows=[("s1", "cli", _NOW - _HOUR, 10)])
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        good = c.collect().usage_analytics

        def boom() -> list[dict[str, object]]:
            raise RuntimeError("sessions unavailable")

        monkeypatch.setattr(c._db, "read_sessions", boom)
        degraded = c.collect()
    finally:
        c.close()

    assert "usage_analytics" in degraded.health.failed_sources
    assert degraded.usage_analytics == good


def test_out_of_range_start_times_are_skipped_not_fatal():
    rows = [_row("far", 1e20, input_tokens=5), _row("ok", _NOW - _HOUR, input_tokens=1)]

    analytics = _usage_analytics(rows, now=_NOW)

    assert sum(analytics.hourly_sessions_7d) == 1
    assert sum(entry.input_tokens for entry in analytics.daily) == 1
