"""Collection of session rows, token totals, cost estimation, and token analytics."""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

import pytest

from hermesd.collector import (
    Collector,
    _estimate_cost,
    _resolved_session_cost,
    _summarize_breakdown,
    _summarize_tokens,
    _today_epoch,
)
from tests.conftest import create_state_db_tables


def test_today_epoch_is_midnight():
    import datetime

    epoch = _today_epoch(time.time())
    dt = datetime.datetime.fromtimestamp(epoch)
    assert dt.hour == 0
    assert dt.minute == 0
    assert dt.second == 0


def test_collect_tokens_today_filters_by_date(
    hermes_home: Path, sample_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """Only sessions started today count toward today's tokens."""
    # Pin the "today" cutoff to two hours ago so the assertion is deterministic
    # regardless of wall-clock time: sample_db's sessions (≤1h old) count toward
    # today, sess_old (2 days old) does not — with no midnight-boundary flake.
    monkeypatch.setattr("hermesd.collector._today_epoch", lambda _now: time.time() - 7200)
    conn = sqlite3.connect(str(sample_db))
    yesterday = time.time() - 86400 * 2
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "sess_old",
            "cli",
            None,
            "gpt-5.4",
            None,
            None,
            None,
            yesterday,
            None,
            None,
            10,
            5,
            5000,
            3000,
            1000,
            500,
            0,
            None,
            None,
            None,
            0.10,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    conn.commit()
    conn.close()
    c = Collector(hermes_home)
    state = c.collect()
    # sample_db: sess_001 (12_400 in) + sess_002 (9_100 in) started today.
    # sess_old (5_000 in) started two days ago, so it counts toward the total
    # but not toward today.
    assert state.tokens_today.input_tokens == 21_500
    assert state.tokens_total.input_tokens == 26_500
    assert state.tokens_today.input_tokens < state.tokens_total.input_tokens
    c.close()


def test_collect_tool_stats(hermes_home: Path, sample_db: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert isinstance(state.tool_stats, list)
    names = [t.name for t in state.tool_stats]
    assert "shell_exec" in names
    c.close()


def test_collect_total_tool_calls(hermes_home: Path, sample_db: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.total_tool_calls == 51 + 14
    c.close()


def _insert_session_with_endpoint(db_path: Path, model: str, base_url: str) -> None:
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, model, billing_base_url) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ctx_sess", "cli", time.time(), model, base_url),
    )
    conn.commit()
    conn.close()


def test_collect_session_context_limit_joins_on_model_and_base_url(hermes_home: Path):
    """SessionInfo.context_limit joins model@billing_base_url against the cache."""
    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/v1"
    )
    (hermes_home / "context_length_cache.yaml").write_text(
        "context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 1048576\n"
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].context_limit == 1048576
    c.close()


def test_collect_session_context_limit_normalizes_trailing_slash(hermes_home: Path):
    """A trailing slash on the session base_url still matches the cache key."""
    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/v1/"
    )
    (hermes_home / "context_length_cache.yaml").write_text(
        "context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 1048576\n"
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].context_limit == 1048576
    c.close()


def test_collect_session_context_limit_falls_back_to_same_origin_endpoint_variant(
    hermes_home: Path,
):
    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/anthropic"
    )
    (hermes_home / "context_length_cache.yaml").write_text(
        "context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 1048576\n"
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].context_limit == 1048576
    c.close()


def test_collect_session_context_limit_missing_cache_is_zero(hermes_home: Path):
    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/v1"
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].context_limit == 0
    assert "sessions" not in state.health.failed_sources
    c.close()


def test_collect_response_store_counts(hermes_home: Path):
    """response_store.db conversation/response counts surface in Operations."""
    db_path = hermes_home / "response_store.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY);"
        "INSERT INTO conversations VALUES ('c1'), ('c2');"
        "INSERT INTO responses VALUES ('r1'), ('r2'), ('r3');"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    ops = state.operations
    assert ops.response_store_present is True
    assert ops.conversation_count == 2
    assert ops.response_count == 3
    assert ops.response_store_size_bytes > 0
    assert "operations" not in state.health.failed_sources
    c.close()


def test_collect_response_store_absent_is_zero(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.operations.response_store_present is False
    assert state.operations.conversation_count == 0
    c.close()


def test_collect_response_store_preserves_last_good_on_corrupt_db(hermes_home: Path):
    db_path = hermes_home / "response_store.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY);"
        "INSERT INTO conversations VALUES ('c1'), ('c2');"
        "INSERT INTO responses VALUES ('r1'), ('r2'), ('r3');"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    db_path.write_bytes(b"not a sqlite database")
    second = c.collect()

    assert second.operations.response_store_present is True
    assert second.operations.conversation_count == first.operations.conversation_count
    assert second.operations.response_count == first.operations.response_count
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_response_store_preserves_last_good_when_db_disappears(hermes_home: Path):
    db_path = hermes_home / "response_store.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY);"
        "INSERT INTO conversations VALUES ('c1'), ('c2');"
        "INSERT INTO responses VALUES ('r1'), ('r2'), ('r3');"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    db_path.unlink()
    second = c.collect()

    assert second.operations.response_store_present is True
    assert second.operations.conversation_count == first.operations.conversation_count
    assert second.operations.response_count == first.operations.response_count
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_response_store_preserves_last_good_when_db_becomes_unsafe_symlink(
    hermes_home: Path, tmp_path: Path
):
    db_path = hermes_home / "response_store.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY);"
        "INSERT INTO conversations VALUES ('c1'), ('c2');"
        "INSERT INTO responses VALUES ('r1'), ('r2'), ('r3');"
    )
    conn.commit()
    conn.close()
    outside_db = tmp_path / "response_store.db"
    sqlite3.connect(str(outside_db)).close()

    c = Collector(hermes_home)
    first = c.collect()
    db_path.unlink()
    db_path.symlink_to(outside_db)
    second = c.collect()

    assert second.operations.response_store_present is True
    assert second.operations.conversation_count == first.operations.conversation_count
    assert second.operations.response_count == first.operations.response_count
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_response_store_ignores_symlinked_db_outside_home(
    hermes_home: Path, tmp_path: Path
):
    outside_db = tmp_path / "response_store.db"
    conn = sqlite3.connect(str(outside_db))
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY);"
        "INSERT INTO conversations VALUES ('outside');"
        "INSERT INTO responses VALUES ('outside-response');"
    )
    conn.commit()
    conn.close()
    (hermes_home / "response_store.db").symlink_to(outside_db)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.operations.response_store_present is False
    assert state.operations.conversation_count == 0
    assert "operations" not in state.health.failed_sources
    c.close()


def test_collect_response_store_ignores_symlinked_wal_sidecar(hermes_home: Path, tmp_path: Path):
    db_path = hermes_home / "response_store.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY);"
        "INSERT INTO conversations VALUES ('c1');"
        "INSERT INTO responses VALUES ('r1');"
    )
    conn.commit()
    conn.close()
    outside_wal = tmp_path / "outside-response-store-wal"
    outside_wal.write_bytes(b"not a sqlite wal")
    (hermes_home / "response_store.db-wal").symlink_to(outside_wal)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.operations.response_store_present is True
    assert state.operations.conversation_count == 1
    assert state.operations.response_count == 1
    assert "operations" not in state.health.failed_sources
    c.close()


def test_session_active_detection(hermes_home: Path, sample_db: Path):
    """Sessions with ended_at=NULL should be marked active."""
    c = Collector(hermes_home)
    state = c.collect()
    # Both sample sessions have ended_at=NULL. Assert the count first so the
    # all() below can't pass vacuously on an empty session list.
    assert len(state.sessions) == 2
    assert all(s.is_active for s in state.sessions)
    c.close()


def test_session_ended_detection(hermes_home: Path):
    """Sessions with ended_at set should not be marked active."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, ?)",
        ("sess_ended", "cli", now - 3600, now - 1800),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, NULL)",
        ("sess_active", "cli", now - 600),
    )
    conn.commit()
    conn.close()
    c = Collector(hermes_home)
    state = c.collect()
    by_id = {s.session_id: s for s in state.sessions}
    assert by_id["sess_ended"].is_active is False
    assert by_id["sess_active"].is_active is True
    c.close()


def test_token_analytics_windows_use_injected_clock(hermes_home: Path):
    fake_now = 1_000_000.0
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, input_tokens) VALUES (?, ?, ?, ?)",
        ("s1", "cli", fake_now - 10 * 86400, 100),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: fake_now)
    try:
        state = c.collect()
    finally:
        c.close()

    windows = {window.label: window for window in state.token_analytics.windows}
    assert windows["7d"].session_count == 0
    assert windows["30d"].session_count == 1


def test_context_lengths_key_without_separator_is_skipped(hermes_home: Path):
    # context_length_cache.yaml keys are "model@base_url"; a key with no "@" is
    # malformed and must be skipped (the partition sep check), while a valid key
    # is normalized through. Drive it via a real collect() and assert sessions
    # still populate (no crash from the malformed entry).
    import yaml

    (hermes_home / "context_length_cache.yaml").write_text(
        yaml.dump(
            {
                "context_lengths": {
                    "no_separator_key": 12345,
                    "gpt-5.4@https://api.example.com/": 200000,  # normalized
                }
            }
        )
    )
    c = Collector(hermes_home)
    try:
        # No exception; the malformed key is silently dropped. We exercise the
        # private reader directly to assert the normalization contract.
        lengths = c._read_context_lengths()
        assert "no_separator_key" not in lengths
        assert lengths["gpt-5.4@https://api.example.com"] == 200000
    finally:
        c.close()


def _make_db(hermes_home: Path) -> None:
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, NULL)",
        ("active_cli", "cli", now - 100),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, ?)",
        ("ended_cli", "cli", now - 3600, now - 1800),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, NULL)",
        ("active_telegram", "telegram", now - 50),
    )
    conn.commit()
    conn.close()


def test_active_sessions_detected(hermes_home: Path):
    _make_db(hermes_home)
    c = Collector(hermes_home)
    state = c.collect()
    by_id = {s.session_id: s for s in state.sessions}
    assert by_id["active_cli"].is_active is True
    assert by_id["active_telegram"].is_active is True
    assert by_id["ended_cli"].is_active is False
    c.close()


def test_active_count_in_compact_panel(hermes_home: Path):
    _make_db(hermes_home)
    from rich.console import Console

    from hermesd.collector import Collector
    from hermesd.panels import render_panel
    from hermesd.theme import Theme

    c = Collector(hermes_home)
    state = c.collect()
    panel = render_panel(2, state, Theme(), detail=False)
    console = Console(width=80, force_terminal=True, no_color=True)
    with console.capture() as cap:
        console.print(panel)
    text = cap.get()
    assert "2 active" in text
    assert "3 total" in text
    c.close()


def test_null_columns_in_session(hermes_home: Path):
    """All nullable columns as NULL must not crash."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False, source_required=False)
    conn.execute(
        "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
        ("null_sess", time.time()),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    s = state.sessions[0]
    assert s.session_id == "null_sess"
    assert s.source == ""
    assert s.model == ""
    assert s.message_count == 0
    assert s.input_tokens == 0
    assert s.estimated_cost_usd == 0.0
    assert s.billing_provider == ""
    assert s.cost_status == ""
    assert s.pricing_version == ""
    assert s.is_active is True  # ended_at is NULL
    c.close()


def test_session_schema_fields_mapped(hermes_home: Path):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions ("
        "id, source, started_at, billing_provider, cost_status, pricing_version, "
        "end_reason, billing_base_url, billing_mode"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "schema_sess",
            "cli",
            time.time(),
            "openai-codex",
            "reported",
            "2026-04",
            "cron_complete",
            "https://api.kimi.test/v1",
            "subscription_included",
        ),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    session = state.sessions[0]
    assert session.billing_provider == "openai-codex"
    assert session.cost_status == "reported"
    assert session.pricing_version == "2026-04"
    assert session.end_reason == "cron_complete"
    assert session.billing_base_url == "https://api.kimi.test/v1"
    assert session.billing_mode == "subscription_included"
    c.close()


def test_session_parent_session_id_mapped(hermes_home: Path):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, parent_session_id) VALUES (?, ?, ?, ?)",
        ("child_sess", "cli", time.time(), "parent_sess"),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].parent_session_id == "parent_sess"
    c.close()


def test_estimate_cost_basic():
    # 1M input tokens at $2.50/M = $2.50
    cost = _estimate_cost(1_000_000, 0, 0, 0)
    assert abs(cost - 2.50) < 0.01


def test_estimate_cost_clamps_negative_and_huge_token_counts():
    assert _estimate_cost(-100, -100, -100, -100) == 0.0
    huge_cost = _estimate_cost(10**400, 0, 0, 0)
    assert math.isfinite(huge_cost)
    assert huge_cost == 2_500_000_000.0


def test_estimate_cost_output():
    # 1M output tokens at $10/M = $10
    cost = _estimate_cost(0, 1_000_000, 0, 0)
    assert abs(cost - 10.00) < 0.01


def test_estimate_cost_cache_read():
    # 1M cache read tokens at $0.30/M = $0.30
    cost = _estimate_cost(0, 0, 1_000_000, 0)
    assert abs(cost - 0.30) < 0.01


def test_estimate_cost_reasoning():
    # 1M reasoning tokens at $10/M = $10
    cost = _estimate_cost(0, 0, 0, 1_000_000)
    assert abs(cost - 10.00) < 0.01


def test_estimate_cost_mixed():
    # Real-world scenario: 100K in, 5K out, 50K cache
    cost = _estimate_cost(100_000, 5_000, 50_000, 0)
    expected = 100_000 * 2.50 / 1e6 + 5_000 * 10.00 / 1e6 + 50_000 * 0.30 / 1e6
    assert abs(cost - expected) < 0.001


def test_estimate_cost_zero():
    cost = _estimate_cost(0, 0, 0, 0)
    assert cost == 0.0


def test_estimate_cost_cache_write():
    # 1M cache write tokens at $3.125/M (input rate x 1.25) = $3.125
    cost = _estimate_cost(0, 0, 0, 0, 1_000_000)
    assert abs(cost - 3.125) < 0.01


def test_estimate_cost_cache_write_clamps_negative_and_huge_counts():
    assert _estimate_cost(0, 0, 0, 0, -100) == 0.0
    huge_cost = _estimate_cost(0, 0, 0, 0, 10**400)
    assert math.isfinite(huge_cost)
    assert huge_cost == 3_125_000_000.0


def test_resolved_session_cost_includes_cache_write_tokens():
    row = {
        "estimated_cost_usd": None,
        "cost_status": "estimated",
        "input_tokens": 100_000,
        "output_tokens": 5_000,
        "cache_read_tokens": 50_000,
        "cache_write_tokens": 20_000,
        "reasoning_tokens": 1_000,
    }
    # (100_000*2.50 + 5_000*10 + 50_000*0.30 + 1_000*10 + 20_000*3.125) / 1e6
    expected = (100_000 * 2.50 + 5_000 * 10 + 50_000 * 0.30 + 1_000 * 10 + 20_000 * 3.125) / 1e6
    assert _resolved_session_cost(row) == pytest.approx(expected)


def test_collector_estimates_cache_write_tokens_when_cost_is_null(hermes_home: Path):
    """Cache-write tokens must contribute to the estimated fallback cost."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, cache_write_tokens, "
        "estimated_cost_usd) VALUES (?, ?, ?, ?, NULL)",
        ("s1", "cli", now, 1_000_000),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    # 1M cache-write tokens at $3.125/M = $3.125.
    assert state.tokens_total.total_cost_usd == pytest.approx(3.125)
    c.close()


def test_resolved_session_cost_preserves_reported_zero_cost():
    row = {
        "estimated_cost_usd": 0.0,
        "cost_status": "reported",
        "input_tokens": 100_000,
        "output_tokens": 5_000,
        "cache_read_tokens": 50_000,
        "reasoning_tokens": 1_000,
    }

    assert _resolved_session_cost(row) == 0.0


def test_resolved_session_cost_treats_included_zero_as_authoritative():
    # subscription_included rows store cost 0.0 and must NOT be re-estimated
    # from tokens — $0.00 is the genuine, provider-authoritative cost.
    row = {
        "estimated_cost_usd": 0.0,
        "cost_status": "included",
        "input_tokens": 100_000,
        "output_tokens": 5_000,
        "cache_read_tokens": 50_000,
        "reasoning_tokens": 1_000,
    }
    assert _resolved_session_cost(row) == 0.0


def test_summarize_tokens_all_included_rows_clears_estimated_flag():
    rows = [
        {"input_tokens": 10, "estimated_cost_usd": 0.0, "cost_status": "included"},
        {"input_tokens": 20, "estimated_cost_usd": 0.0, "cost_status": "exact"},
    ]
    assert _summarize_tokens(rows).cost_is_estimated is False


@pytest.mark.parametrize(
    ("cost_status", "estimated_cost", "expected"),
    [
        # Hand-computed from _COST_PER_M (input 2.50, output 10, cache_read 0.30,
        # reasoning 10 per 1M): (100_000*2.50 + 5_000*10 + 50_000*0.30 + 1_000*10)
        # / 1e6 = 325_000 / 1e6 = 0.325. Pinned as a literal so a regression in
        # _estimate_cost can't silently agree with itself.
        ("reported", None, 0.325),
        ("reported", 5.0, 5.0),
        ("included", 0.0, 0.0),
        ("exact", 4.2, 4.2),
        ("estimated", 0.0, 0.325),
        ("estimated", 1.5, 1.5),
    ],
)
def test_resolved_session_cost_corners(
    cost_status: str,
    estimated_cost: float | None,
    expected: float,
):
    row = {
        "estimated_cost_usd": estimated_cost,
        "cost_status": cost_status,
        "input_tokens": 100_000,
        "output_tokens": 5_000,
        "cache_read_tokens": 50_000,
        "reasoning_tokens": 1_000,
    }

    assert _resolved_session_cost(row) == expected


def test_summarize_tokens_coerces_null_columns_to_zero():
    rows = [
        {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
            "reasoning_tokens": None,
            "estimated_cost_usd": None,
            "cost_status": None,
            "started_at": None,
        }
    ]

    totals = _summarize_tokens(rows)

    assert totals.input_tokens == 0
    assert totals.output_tokens == 0
    assert totals.cache_read_tokens == 0
    assert totals.cache_write_tokens == 0
    assert totals.reasoning_tokens == 0
    assert totals.total_cost_usd == 0.0


def test_summarize_tokens_excludes_rows_before_started_at_min():
    rows = [
        {"input_tokens": 10, "started_at": 50.0},  # before cutoff — excluded
        {"input_tokens": 20, "started_at": 150.0},  # at/after cutoff — counted
    ]

    totals = _summarize_tokens(rows, started_at_min=100.0)

    assert totals.input_tokens == 20


def test_summarize_tokens_accumulates_fields_and_resolves_cost_per_row():
    rows = [
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 2,
            "cache_write_tokens": 1,
            "reasoning_tokens": 3,
            "estimated_cost_usd": 1.5,
            "cost_status": "reported",  # reported cost preserved verbatim
        },
        {
            "input_tokens": 20,
            "output_tokens": 7,
            "cache_read_tokens": 4,
            "cache_write_tokens": 0,
            "reasoning_tokens": 6,
            "estimated_cost_usd": None,  # falls back to estimate
            "cost_status": "estimated",
        },
    ]

    totals = _summarize_tokens(rows)

    assert totals.input_tokens == 30
    assert totals.output_tokens == 12
    assert totals.cache_read_tokens == 6
    assert totals.cache_write_tokens == 1
    assert totals.reasoning_tokens == 9
    expected_cost = 1.5 + _estimate_cost(20, 7, 4, 6)
    assert abs(totals.total_cost_usd - expected_cost) < 1e-9


def test_summarize_tokens_all_reported_rows_clears_estimated_flag():
    rows = [
        {"input_tokens": 10, "estimated_cost_usd": 1.0, "cost_status": "reported"},
        {"input_tokens": 20, "estimated_cost_usd": 0.0, "cost_status": "reported"},
    ]

    assert _summarize_tokens(rows).cost_is_estimated is False


def test_summarize_tokens_mixed_cost_status_stays_estimated():
    rows = [
        {"input_tokens": 10, "estimated_cost_usd": 1.0, "cost_status": "reported"},
        {"input_tokens": 20, "estimated_cost_usd": 0.5, "cost_status": "estimated"},
    ]

    assert _summarize_tokens(rows).cost_is_estimated is True


def test_summarize_tokens_empty_rows_keep_estimated_default():
    assert _summarize_tokens([]).cost_is_estimated is True


def test_summarize_tokens_reported_status_with_null_cost_stays_estimated():
    # cost_status says "reported" but the stored cost is NULL, so the
    # resolved cost falls back to an estimate — the flag must reflect that.
    rows = [{"input_tokens": 10, "estimated_cost_usd": None, "cost_status": "reported"}]

    assert _summarize_tokens(rows).cost_is_estimated is True


def test_summarize_breakdown_sorts_equal_labels_ascending():
    rows = [
        {
            "model": "zed",
            "input_tokens": 100,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "estimated_cost_usd": 1.0,
        },
        {
            "model": "alpha",
            "input_tokens": 100,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "estimated_cost_usd": 1.0,
        },
    ]

    labels = [summary.label for summary in _summarize_breakdown(rows, key_name="model")]
    assert labels == ["alpha", "zed"]


def test_collector_uses_db_cost_when_cost_is_non_zero(hermes_home: Path, sample_db: Path):
    """When estimated_cost_usd is non-zero, collector should use the stored value."""
    # sample_db has sessions with cost=0.42 and cost=0.31 and tokens
    c = Collector(hermes_home)
    state = c.collect()
    assert state.tokens_total.total_cost_usd == 0.73
    c.close()


def test_collector_estimates_when_cost_is_null(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    """When all costs are NULL, collector estimates from tokens."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    # Pin the "today" cutoff just before the session so it deterministically
    # counts toward today's total (no midnight-boundary flake).
    monkeypatch.setattr("hermesd.collector._today_epoch", lambda _now: now - 1)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, input_tokens, output_tokens, "
        "cache_read_tokens, estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?, NULL)",
        ("s1", "cli", now, 100_000, 5_000, 50_000),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    # Hand-computed: 100_000*2.50 + 5_000*10 + 50_000*0.30 + 0*10 = 315_000;
    # /1e6 = 0.315. The single session started "now", so it counts toward both
    # totals.
    assert state.tokens_total.total_cost_usd == pytest.approx(0.315)
    assert state.tokens_today.total_cost_usd == pytest.approx(0.315)
    c.close()


def test_collector_estimates_missing_session_cost_in_detail_rows(hermes_home: Path):
    """Per-session rows should estimate cost when the DB cost column is NULL."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, input_tokens, output_tokens, "
        "cache_read_tokens, estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?, NULL)",
        ("s1", "cli", now, 100_000, 5_000, 50_000),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    assert len(state.sessions) == 1
    # Hand-computed: 100_000*2.50 + 5_000*10 + 50_000*0.30 = 315_000; /1e6 = 0.315.
    assert state.sessions[0].estimated_cost_usd == pytest.approx(0.315)
    c.close()


def test_collector_total_cost_estimates_missing_sessions_when_db_has_mixed_costs(hermes_home: Path):
    """Total cost should include estimated fallback for sessions with missing DB cost."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, input_tokens, output_tokens, "
        "cache_read_tokens, estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("s_reported", "cli", now, 10_000, 2_000, 1_000, 0.42),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, input_tokens, output_tokens, "
        "cache_read_tokens, estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?, NULL)",
        ("s_missing", "cli", now, 100_000, 5_000, 50_000),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    expected_missing = _estimate_cost(100_000, 5_000, 50_000, 0)
    assert len(state.sessions) == 2
    assert state.tokens_total.total_cost_usd == pytest.approx(0.42 + expected_missing)
    c.close()


def test_collector_total_cost_flag_is_authoritative_when_all_rows_are_authoritative(
    hermes_home: Path,
):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    rows = [
        ("included", 0.0),
        ("exact", 4.2),
        ("reported", 0.3),
    ]
    for idx, (status, cost) in enumerate(rows):
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, input_tokens, cost_status, "
            "estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (f"s{idx}", "cli", now, 1000, status, cost),
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.tokens_total.cost_is_estimated is False
    c.close()


def test_collector_total_cost_flag_stays_estimated_for_mixed_authority(
    hermes_home: Path,
):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    rows = [
        ("reported", 0.3),
        ("estimated", None),
    ]
    for idx, (status, cost) in enumerate(rows):
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, input_tokens, cost_status, "
            "estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (f"s{idx}", "cli", now, 1000, status, cost),
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.tokens_total.cost_is_estimated is True
    c.close()


def test_collector_token_analytics_counts_cost_status(hermes_home: Path):
    """cost_status distribution is summarized for the reconciliation view."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    statuses = ["unknown", "unknown", "included", "estimated", None]
    for idx, status in enumerate(statuses):
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, cost_status) VALUES (?, ?, ?, ?)",
            (f"s{idx}", "cli", now, status),
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    counts = state.token_analytics.cost_status_counts
    assert counts["unknown"] == 3  # two explicit + one NULL coerced to "unknown"
    assert counts["included"] == 1
    assert counts["estimated"] == 1
    c.close()


def test_collector_token_analytics_breaks_down_by_endpoint(hermes_home: Path):
    """Spend/tokens aggregate per billing_base_url, finer than provider."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    rows = [
        ("e1", "https://api.kimi.test/v1", 100_000, 0.40),
        ("e2", "https://api.kimi.test/v1", 50_000, 0.10),
        ("e3", "https://api.minimax.io/v1", 20_000, 0.05),
    ]
    for sid, base_url, in_tok, cost in rows:
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, input_tokens, "
            "billing_base_url, cost_status, estimated_cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, "cli", "m", now, in_tok, base_url, "reported", cost),
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    endpoints = {entry.label: entry for entry in state.token_analytics.by_endpoint}
    assert endpoints["https://api.kimi.test/v1"].session_count == 2
    assert endpoints["https://api.kimi.test/v1"].input_tokens == 150_000
    assert abs(endpoints["https://api.kimi.test/v1"].total_cost_usd - 0.50) < 1e-9
    assert endpoints["https://api.minimax.io/v1"].session_count == 1
    c.close()


def test_collector_builds_token_analytics_windows_and_breakdowns(hermes_home: Path):
    """Analytics should summarize recent windows plus model/provider breakdowns."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens, "
        "cache_read_tokens, billing_provider, estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "s_recent_a",
            "cli",
            "gpt-5.4",
            now - 2 * 86400,
            100_000,
            5_000,
            50_000,
            "openai-codex",
            0.42,
        ),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens, "
        "cache_read_tokens, billing_provider, estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "s_recent_b",
            "cli",
            "claude-sonnet",
            now - 10 * 86400,
            80_000,
            6_000,
            20_000,
            "anthropic",
            0.31,
        ),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens, "
        "cache_read_tokens, billing_provider, estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("s_old", "cli", "gpt-5.4", now - 45 * 86400, 60_000, 4_000, 10_000, "openai-codex", 0.21),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    windows = {window.label: window for window in state.token_analytics.windows}
    assert windows["7d"].session_count == 1
    assert windows["7d"].input_tokens == 100_000
    assert windows["30d"].session_count == 2
    assert windows["30d"].input_tokens == 180_000

    models = {entry.label: entry for entry in state.token_analytics.by_model}
    assert models["gpt-5.4"].session_count == 2
    assert models["gpt-5.4"].input_tokens == 160_000
    providers = {entry.label: entry for entry in state.token_analytics.by_provider}
    assert providers["openai-codex"].session_count == 2
    assert providers["anthropic"].session_count == 1
    c.close()


def test_collector_token_window_cache_ratio_uses_prompt_and_cache_tokens(
    hermes_home: Path,
):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    rows = [
        ("cached", 100, 300),
        ("zero_prompt", 0, 50),
    ]
    for sid, input_tokens, cache_read_tokens in rows:
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, input_tokens, cache_read_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, "cli", now, input_tokens, cache_read_tokens),
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    windows = {window.label: window for window in state.token_analytics.windows}
    assert windows["7d"].cache_ratio == pytest.approx(350 / 450)
    assert windows["30d"].cache_ratio == pytest.approx(350 / 450)
    c.close()
