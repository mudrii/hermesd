"""Collection of session rows, token totals, cost estimation, and token analytics."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

import hermesd.collect.sqlite_util as sqlite_util_module
from hermesd.collect.redaction import _redact_bare_credentials
from hermesd.collector import (
    _ACTIVE_SURFACE_LIMIT,
    Collector,
    _estimate_cost,
    _resolved_session_cost,
    _summarize_breakdown,
    _summarize_tokens,
    _today_epoch,
)
from hermesd.models import (
    ProcessLiveness,
    SessionCoordinationState,
    SessionLeaseKind,
)
from hermesd.panels.tokens import render_tokens
from hermesd.theme import Theme
from tests.conftest import (
    create_session_coordination_tables,
    create_state_db_tables,
    insert_compression_lock,
    insert_gateway_route,
    insert_model_usage,
    insert_turn_lease,
)


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
        "INSERT INTO sessions ("
        "id, source, model, started_at, message_count, tool_call_count, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, "
        "estimated_cost_usd"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "sess_old",
            "cli",
            "gpt-5.4",
            yesterday,
            10,
            5,
            5000,
            3000,
            1000,
            500,
            0,
            0.10,
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


def test_collect_response_store_missing_tables_counts_zero(hermes_home: Path):
    """A response_store.db without the tables yet legitimately reports zero."""
    db_path = hermes_home / "response_store.db"
    sqlite3.connect(str(db_path)).close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.operations.response_store_present is True
    assert state.operations.conversation_count == 0
    assert state.operations.response_count == 0
    assert "operations" not in state.health.failed_sources
    c.close()


def test_collect_response_store_count_error_preserves_last_good(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """A transient sqlite error must fail the source to last-good, not report 0."""
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
    assert first.operations.conversation_count == 2

    def _locked(conn: sqlite3.Connection, table_name: str) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite_util_module, "_table_count", _locked)
    second = c.collect()

    assert "operations" in second.health.failed_sources
    assert second.operations.response_store_present is True
    assert second.operations.conversation_count == first.operations.conversation_count
    assert second.operations.response_count == first.operations.response_count
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


def test_collect_response_store_refuses_symlinked_wal_and_keeps_last_good(
    hermes_home: Path, tmp_path: Path
):
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
    c = Collector(hermes_home)
    first = c.collect()
    assert first.operations.response_store_present is True
    assert first.operations.conversation_count == 1
    assert first.operations.response_count == 1

    outside_wal = tmp_path / "outside-response-store-wal"
    outside_wal.write_bytes(b"not a sqlite wal")
    (hermes_home / "response_store.db-wal").symlink_to(outside_wal)
    second = c.collect()

    assert second.operations.response_store_present is True
    assert second.operations.conversation_count == 1
    assert second.operations.response_count == 1
    assert "operations" in second.health.failed_sources
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


def test_billing_base_url_credentials_redacted_from_state_and_snapshot(hermes_home: Path):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, billing_base_url) VALUES (?, ?, ?, ?)",
        (
            "cred_sess",
            "cli",
            time.time(),
            "https://user:glpat-abc123@billing.example.com/v1?token=sk-secret-123&safe=1",
        ),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    session = state.sessions[0]
    assert session.billing_base_url == (
        "https://[REDACTED]@billing.example.com/v1?token=[REDACTED]&safe=1"
    )
    endpoint_labels = [breakdown.label for breakdown in state.token_analytics.by_endpoint]
    assert "https://[REDACTED]@billing.example.com/v1?token=[REDACTED]&safe=1" in endpoint_labels

    snapshot = json.dumps(state.model_dump(mode="json"))
    assert "glpat-abc123" not in snapshot
    assert "sk-secret-123" not in snapshot
    assert "billing.example.com" in snapshot
    c.close()


def test_billing_base_url_non_url_value_passes_through_unchanged(hermes_home: Path):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, billing_base_url) VALUES (?, ?, ?, ?)",
        ("plain_sess", "cli", time.time(), "internal-billing"),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].billing_base_url == "internal-billing"
    labels = [breakdown.label for breakdown in state.token_analytics.by_endpoint]
    assert "internal-billing" in labels
    c.close()


def test_billing_base_url_invalid_port_does_not_raise(hermes_home: Path):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, billing_base_url) VALUES (?, ?, ?, ?)",
        (
            "badport_sess",
            "cli",
            time.time(),
            "https://user:glpat-abc123@billing.example.com:bad/v1?token=sk-secret-123",
        ),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.sessions[0].billing_base_url == (
        "https://[REDACTED]@billing.example.com/v1?token=[REDACTED]"
    )
    snapshot = json.dumps(state.model_dump(mode="json"))
    assert "glpat-abc123" not in snapshot
    assert "sk-secret-123" not in snapshot
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


@pytest.mark.parametrize("cost_status", ["included", "exact", "reported"])
@pytest.mark.parametrize(("actual_cost", "expected"), [(0.0, 0.0), (None, 2.0)])
def test_authoritative_cost_agrees_across_sessions_and_model_usage(
    hermes_home: Path, cost_status: str, actual_cost: float | None, expected: float
):
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    create_state_db_tables(conn, include_schema_version=False, include_v021_columns=True)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, input_tokens, "
        "estimated_cost_usd, actual_cost_usd, cost_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("s1", "cli", "model", now, 100, 2.0, actual_cost, cost_status),
    )
    insert_model_usage(
        conn,
        "s1",
        "model",
        input_tokens=100,
        estimated_cost_usd=2.0,
        actual_cost_usd=actual_cost,
        cost_status=cost_status,
        last_seen=now,
    )
    conn.commit()
    conn.close()

    collector = Collector(hermes_home)
    try:
        state = collector.collect()
        assert state.sessions[0].estimated_cost_usd == expected
        assert state.tokens_total.total_cost_usd == expected
        assert state.tokens_total.cost_is_estimated is False
        usage = state.token_analytics.model_usage_all[0]
        assert usage.reported_cost_usd == expected
        assert usage.estimated_only_cost_usd == 0.0
        assert usage.reported_row_count == usage.row_count == 1
        console = Console(file=StringIO(), record=True, width=180)
        console.print(render_tokens(state, Theme(), detail=True))
        rendered = console.export_text()
        assert f"${expected:.2f}" in rendered
        assert "~$" not in rendered
    finally:
        collector.close()


@pytest.mark.parametrize("cost_status", ["included", "exact", "reported"])
def test_authoritative_actual_zero_without_estimate_is_reported(cost_status: str):
    row = {
        "actual_cost_usd": 0.0,
        "estimated_cost_usd": None,
        "cost_status": cost_status,
        "input_tokens": 100_000,
    }
    assert _resolved_session_cost(row) == 0.0
    totals = _summarize_tokens([row])
    assert totals.total_cost_usd == 0.0
    assert totals.cost_is_estimated is False


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


def test_summarize_tokens_coerces_text_columns_to_zero():
    # SQLite columns are untyped: a TEXT token value must not TypeError the +=.
    rows = [
        {
            "input_tokens": "abc",
            "output_tokens": "12",
            "cache_read_tokens": "",
            "cache_write_tokens": None,
            "reasoning_tokens": "1.5",
            "estimated_cost_usd": 0.0,
            "cost_status": "exact",
        }
    ]

    totals = _summarize_tokens(rows)

    assert totals.input_tokens == 0
    assert totals.output_tokens == 12
    assert totals.cache_read_tokens == 0
    assert totals.cache_write_tokens == 0
    assert totals.reasoning_tokens == 0


def test_resolved_session_cost_coerces_text_token_columns():
    row = {
        "estimated_cost_usd": None,
        "cost_status": "estimated",
        "input_tokens": "abc",
        "output_tokens": None,
        "cache_read_tokens": "",
        "cache_write_tokens": "xyz",
        "reasoning_tokens": float("inf"),
    }

    assert _resolved_session_cost(row) == 0.0


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


def _write_v021_session_db(hermes_home: Path, *, v021: bool) -> None:
    """A state.db with (or without) the hermes-agent 0.21 session columns."""
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    create_state_db_tables(conn, include_schema_version=False, include_v021_columns=v021)
    now = time.time()
    if v021:
        conn.execute(
            "INSERT INTO sessions ("
            "id, source, model, started_at, message_count, input_tokens, output_tokens, "
            "estimated_cost_usd, actual_cost_usd, cost_status, cost_source, git_branch, "
            "chat_type, display_name, title_source, profile_name, pinned, last_activity_at, "
            "last_activity_description, compression_failure_error, title"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sess_new",
                "cli",
                "gpt-5.4",
                now - 7200,
                12,
                100,
                50,
                0.90,
                0.55,
                "exact",
                "provider",
                "feat/usage",
                "direct",
                "Usage work",
                "llm",
                "coding",
                1,
                now - 30,
                "edited db.py",
                "compression failed: context too large",
                "raw title",
            ),
        )
        insert_model_usage(
            conn,
            "sess_new",
            "gpt-5.4",
            provider="openai",
            api_call_count=7,
            input_tokens=100,
            output_tokens=50,
            estimated_cost_usd=0.9,
            actual_cost_usd=0.55,
            last_seen=now - 30,
        )
        insert_model_usage(
            conn,
            "sess_new",
            "gpt-5.4-mini",
            provider="openai",
            task="title",
            api_call_count=1,
            input_tokens=10,
            estimated_cost_usd=0.01,
            actual_cost_usd=0.0,
            last_seen=now - 86400 * 3,
        )
    else:
        conn.execute(
            "INSERT INTO sessions (id, source, model, started_at, title) VALUES (?,?,?,?,?)",
            ("sess_legacy", "cli", "gpt-5.4", now - 7200, "raw title"),
        )
    conn.commit()
    conn.close()


def _collect_once(hermes_home: Path, **kwargs: object):
    collector = Collector(hermes_home, **kwargs)  # type: ignore[arg-type]
    try:
        return collector.collect()
    finally:
        collector.close()


def test_collector_maps_new_session_columns(hermes_home: Path) -> None:
    _write_v021_session_db(hermes_home, v021=True)

    state = _collect_once(hermes_home)

    session = state.sessions[0]
    assert session.git_branch == "feat/usage"
    assert session.chat_type == "direct"
    assert session.display_name == "Usage work"
    assert session.title_source == "llm"
    assert session.profile_name == "coding"
    assert session.pinned is True
    assert session.last_activity_at > 0
    assert session.last_activity_description == "edited db.py"
    assert session.actual_cost_usd == pytest.approx(0.55)
    assert session.cost_source == "provider"
    assert session.compression_failure_error.startswith("compression failed")
    # The provider-billed cost wins over the estimate.
    assert session.estimated_cost_usd == pytest.approx(0.55)
    assert state.tokens_total.cost_is_estimated is False


# --------------------------------------------------------------------------
# compression recovery state (the durable half of the anti-thrash guard)
#
# `compression_failure_cooldown_until`, `compression_fallback_streak`,
# `compression_ineffective_count` and `compression_recovery_deadline` are
# sessions-table columns (hermes_state_common.py:375-379). They are counters and
# timestamps only — no conversation content is read to produce them.
# --------------------------------------------------------------------------

_COMPRESSION_NOW = 1_800_000_000.0


def _write_compression_db(
    hermes_home: Path,
    *,
    cooldown_until: object = None,
    fallback_streak: object = None,
    ineffective_count: object = None,
    recovery_deadline: object = None,
    with_columns: bool = True,
    v021: bool = True,
) -> None:
    """A state.db holding one session with the given compression-recovery values."""
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    create_state_db_tables(
        conn,
        include_schema_version=False,
        include_v021_columns=v021,
        include_compression_columns=with_columns,
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        ("sess_compression", "cli", _COMPRESSION_NOW - 600),
    )
    if with_columns and v021:
        # The two counters are NOT NULL DEFAULT 0 upstream, so a None here means
        # "leave the default" rather than "store NULL".
        conn.execute(
            "UPDATE sessions SET compression_failure_cooldown_until = ?, "
            "compression_fallback_streak = ?, compression_ineffective_count = ?, "
            "compression_recovery_deadline = ? WHERE id = ?",
            (
                cooldown_until,
                0 if fallback_streak is None else fallback_streak,
                0 if ineffective_count is None else ineffective_count,
                recovery_deadline,
                "sess_compression",
            ),
        )
    conn.commit()
    conn.close()


def _compression_session(hermes_home: Path, **kwargs: object):
    _write_compression_db(hermes_home, **kwargs)  # type: ignore[arg-type]
    state = _collect_once(hermes_home, clock=lambda: _COMPRESSION_NOW)
    assert "sessions" not in state.health.failed_sources
    return state.sessions[0]


def test_collector_maps_compression_recovery_columns(hermes_home: Path) -> None:
    session = _compression_session(
        hermes_home,
        cooldown_until=_COMPRESSION_NOW + 45,
        fallback_streak=2,
        ineffective_count=3,
        recovery_deadline=_COMPRESSION_NOW + 240,
    )

    assert session.compression_failure_cooldown_until == pytest.approx(_COMPRESSION_NOW + 45)
    assert session.compression_fallback_streak == 2
    assert session.compression_ineffective_count == 3
    assert session.compression_recovery_deadline == pytest.approx(_COMPRESSION_NOW + 240)


def test_collector_defaults_compression_columns_when_they_are_absent(
    hermes_home: Path,
) -> None:
    """An early-0.21 database has the error column but none of the four new ones."""
    session = _compression_session(hermes_home, with_columns=False)

    assert session.compression_failure_cooldown_until is None
    assert session.compression_fallback_streak == 0
    assert session.compression_ineffective_count == 0
    assert session.compression_recovery_deadline is None
    assert session.compression_recovery_active(_COMPRESSION_NOW) is False


def test_collector_defaults_compression_columns_on_a_legacy_schema(
    hermes_home: Path,
) -> None:
    session = _compression_session(hermes_home, v021=False, with_columns=False)

    assert session.compression_fallback_streak == 0
    assert session.compression_recovery_deadline is None


def test_compression_recovery_deadline_zero_is_disarmed_not_an_epoch(
    hermes_home: Path,
) -> None:
    """Upstream stores 0 for "not armed"; surfacing it as an epoch reads as 1970."""
    session = _compression_session(hermes_home, recovery_deadline=0.0)

    assert session.compression_recovery_deadline is None
    assert session.compression_recovery_remaining(_COMPRESSION_NOW) is None
    assert session.compression_recovery_active(_COMPRESSION_NOW) is False


def test_compression_cooldown_zero_is_disarmed(hermes_home: Path) -> None:
    session = _compression_session(hermes_home, cooldown_until=0)

    assert session.compression_failure_cooldown_until is None
    assert session.compression_cooldown_remaining(_COMPRESSION_NOW) is None


def test_null_compression_columns_are_not_active(hermes_home: Path) -> None:
    session = _compression_session(hermes_home)

    assert session.compression_failure_cooldown_until is None
    assert session.compression_recovery_deadline is None
    assert session.compression_fallback_streak == 0
    assert session.compression_ineffective_count == 0
    assert session.compression_recovery_active(_COMPRESSION_NOW) is False


@pytest.mark.parametrize(
    ("cooldown_until", "recovery_deadline"),
    [
        pytest.param("not-a-number", "also-not", id="text"),
        pytest.param("", "", id="empty-text"),
        pytest.param(-5.0, -1.0, id="negative"),
        pytest.param(float("inf"), float("nan"), id="non-finite"),
    ],
)
def test_malformed_compression_values_coerce_without_failing_the_source(
    hermes_home: Path, cooldown_until: object, recovery_deadline: object
) -> None:
    """SQLite columns are untyped: a TEXT value in a REAL column must not blank
    the whole `sessions` source."""
    session = _compression_session(
        hermes_home,
        cooldown_until=cooldown_until,
        recovery_deadline=recovery_deadline,
        fallback_streak="7",
        ineffective_count="bogus",
    )

    assert session.compression_fallback_streak == 7
    assert session.compression_ineffective_count == 0
    assert session.compression_recovery_active(_COMPRESSION_NOW) is False


def test_expired_compression_cooldown_is_not_active(hermes_home: Path) -> None:
    session = _compression_session(hermes_home, cooldown_until=_COMPRESSION_NOW - 1)

    assert session.compression_failure_cooldown_until == pytest.approx(_COMPRESSION_NOW - 1)
    assert session.compression_cooldown_remaining(_COMPRESSION_NOW) is None
    assert session.compression_recovery_active(_COMPRESSION_NOW) is False


def test_live_compression_cooldown_is_active_with_remaining(hermes_home: Path) -> None:
    session = _compression_session(hermes_home, cooldown_until=_COMPRESSION_NOW + 45)

    assert session.compression_cooldown_remaining(_COMPRESSION_NOW) == pytest.approx(45.0)
    assert session.compression_recovery_active(_COMPRESSION_NOW) is True


def test_cooldown_exactly_at_now_is_expired(hermes_home: Path) -> None:
    """Upstream keeps a cooldown only while `cooldown_until > now`
    (hermes_state_compression.py:329-335)."""
    session = _compression_session(hermes_home, cooldown_until=_COMPRESSION_NOW)

    assert session.compression_cooldown_remaining(_COMPRESSION_NOW) is None


def test_future_recovery_deadline_is_active_and_a_past_one_is_not(
    hermes_home: Path,
) -> None:
    armed = _compression_session(hermes_home, recovery_deadline=_COMPRESSION_NOW + 300)
    assert armed.compression_recovery_remaining(_COMPRESSION_NOW) == pytest.approx(300.0)
    assert armed.compression_recovery_active(_COMPRESSION_NOW) is True

    hermes_home.joinpath("state.db").unlink()
    elapsed = _compression_session(hermes_home, recovery_deadline=_COMPRESSION_NOW - 300)
    assert elapsed.compression_recovery_remaining(_COMPRESSION_NOW) is None
    assert elapsed.compression_recovery_active(_COMPRESSION_NOW) is False


def test_recovery_deadline_counts_down_against_the_injected_clock(
    hermes_home: Path,
) -> None:
    """The same stored row is active at one clock reading and expired at another."""
    _write_compression_db(hermes_home, recovery_deadline=_COMPRESSION_NOW + 100)

    early = Collector(hermes_home, clock=lambda: _COMPRESSION_NOW)
    try:
        session = early.collect().sessions[0]
    finally:
        early.close()
    assert session.compression_recovery_active(_COMPRESSION_NOW) is True
    # The stored epoch does not move; only the clock does.
    assert session.compression_recovery_active(_COMPRESSION_NOW + 101) is False


def test_either_signal_alone_makes_recovery_active(hermes_home: Path) -> None:
    cooldown_only = _compression_session(hermes_home, cooldown_until=_COMPRESSION_NOW + 10)
    assert cooldown_only.compression_recovery_active(_COMPRESSION_NOW) is True

    hermes_home.joinpath("state.db").unlink()
    deadline_only = _compression_session(hermes_home, recovery_deadline=_COMPRESSION_NOW + 10)
    assert deadline_only.compression_recovery_active(_COMPRESSION_NOW) is True


def test_compression_counters_are_active_without_a_live_timer(hermes_home: Path) -> None:
    """A tripped strike count with no armed clock is state, not active recovery."""
    session = _compression_session(
        hermes_home, fallback_streak=4, ineffective_count=2, recovery_deadline=0
    )

    assert session.compression_fallback_streak == 4
    assert session.compression_ineffective_count == 2
    assert session.compression_recovery_active(_COMPRESSION_NOW) is False


def test_collector_model_usage_windows(hermes_home: Path) -> None:
    _write_v021_session_db(hermes_home, v021=True)

    analytics = _collect_once(hermes_home).token_analytics

    assert analytics.usage_source == "session_model_usage"
    assert [row.model for row in analytics.model_usage_all] == ["gpt-5.4", "gpt-5.4-mini"]
    assert [row.model for row in analytics.model_usage_24h] == ["gpt-5.4"]
    assert [row.model for row in analytics.model_usage_7d] == ["gpt-5.4", "gpt-5.4-mini"]
    main = analytics.model_usage_all[0]
    assert main.provider == "openai"
    assert main.api_calls == 7
    assert main.has_actual_cost is True
    assert main.actual_cost_usd == pytest.approx(0.55)
    aux = analytics.model_usage_all[1]
    assert aux.task == "title"
    assert aux.has_actual_cost is False


def test_legacy_db_without_new_columns_keeps_session_defaults(hermes_home: Path) -> None:
    _write_v021_session_db(hermes_home, v021=False)

    state = _collect_once(hermes_home)

    session = state.sessions[0]
    assert session.session_id == "sess_legacy"
    assert session.git_branch == ""
    assert session.display_name == ""
    assert session.pinned is False
    assert session.actual_cost_usd == 0.0
    # No usage table: the panel keeps the per-session model breakdown.
    assert state.token_analytics.usage_source == "sessions"
    assert state.token_analytics.model_usage_all == []
    assert state.health.failed_sources == []


def test_wrong_typed_session_columns_coerce_instead_of_raising(hermes_home: Path) -> None:
    """SQLite is untyped: 'yes' in pinned and 'soon' in last_activity_at must not crash."""
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    create_state_db_tables(conn, include_schema_version=False, include_v021_columns=True)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, message_count, pinned, "
        "last_activity_at) VALUES (?,?,?,?,?,?)",
        ("sess_typo", "cli", "yesterday", 3, "yes", "soon"),
    )
    conn.commit()
    conn.close()

    state = _collect_once(hermes_home)

    assert state.health.failed_sources == []
    session = state.sessions[0]
    assert session.session_id == "sess_typo"
    assert session.pinned is True
    assert session.last_activity_at == 0.0
    assert session.started_at == 0.0


def test_sessions_table_without_id_column_keeps_source_alive(hermes_home: Path) -> None:
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    conn.execute("CREATE TABLE sessions (source TEXT, model TEXT, started_at REAL)")
    conn.execute("INSERT INTO sessions (source, model, started_at) VALUES ('cli', 'gpt-5.4', 1)")
    conn.commit()
    conn.close()

    state = _collect_once(hermes_home)

    assert "sessions" not in state.health.failed_sources
    assert [session.session_id for session in state.sessions] == [""]


def _write_active_sessions(hermes_home: Path, payload: object) -> None:
    runtime = hermes_home / "runtime"
    runtime.mkdir(exist_ok=True)
    path = runtime / "active_sessions.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def test_active_surfaces_reported_with_liveness(hermes_home: Path) -> None:
    _write_v021_session_db(hermes_home, v021=True)
    _write_active_sessions(
        hermes_home,
        {
            "entries": [
                {
                    "session_id": "sess_new",
                    "surface": "cli",
                    "pid": 111,
                    "process_start_time": 1.0,
                    "started_at": "2026-09-07T10:00:00+00:00",
                },
                {
                    "session_id": "sess_new",
                    "surface": "telegram",
                    "pid": 222,
                    "process_start_time": 2.0,
                    "started_at": "2026-09-07T10:05:00+00:00",
                },
            ]
        },
    )

    state = _collect_once(hermes_home, pid_exists=lambda pid: pid == 111)

    assert state.active_surface_count == 2
    surfaces = {surface.surface: surface for surface in state.active_surfaces}
    assert surfaces["cli"].alive is True
    assert surfaces["cli"].session_id == "sess_new"
    assert surfaces["telegram"].alive is False
    assert surfaces["telegram"].pid == 222


@pytest.mark.parametrize(
    "payload",
    ["not json at all", {"entries": "nope"}, {}, {"entries": [{"surface": "cli"}]}],
    ids=["garbage", "wrong-type", "empty", "no-session-id"],
)
def test_active_surfaces_tolerates_bad_payloads(hermes_home: Path, payload: object) -> None:
    _write_v021_session_db(hermes_home, v021=True)
    _write_active_sessions(hermes_home, payload)

    state = _collect_once(hermes_home)

    assert state.active_surfaces == []
    assert state.active_surface_count == 0
    assert "active_sessions" not in state.health.failed_sources


def test_active_surfaces_missing_file_is_empty(hermes_home: Path) -> None:
    _write_v021_session_db(hermes_home, v021=True)

    state = _collect_once(hermes_home)

    assert state.active_surfaces == []
    assert state.active_surface_count == 0


def test_active_surfaces_are_bounded_and_cost_one_liveness_probe_each(hermes_home: Path) -> None:
    """A 500-entry runtime file must not turn into 500 liveness syscalls per refresh."""
    _write_v021_session_db(hermes_home, v021=True)
    _write_active_sessions(
        hermes_home,
        {
            "entries": [
                {"session_id": f"sess_{index:04d}", "surface": "cli", "pid": 1000 + index}
                for index in range(500)
            ]
        },
    )
    probed: list[int] = []

    state = _collect_once(hermes_home, pid_exists=lambda pid: probed.append(pid) or False)

    assert len(state.active_surfaces) == _ACTIVE_SURFACE_LIMIT
    assert state.active_surface_count == 500
    assert state.active_surfaces_truncated is True
    assert len(probed) == _ACTIVE_SURFACE_LIMIT
    # The cap keeps the head of the file, in file order.
    assert state.active_surfaces[0].session_id == "sess_0000"
    assert state.active_surfaces[-1].session_id == f"sess_{_ACTIVE_SURFACE_LIMIT - 1:04d}"


@pytest.mark.parametrize(
    ("raw_pid", "expected"),
    [("abc", 0), (-1, -1), (2**40, 2**40), (None, 0), (3.7, 3), ("", 0)],
    ids=["garbage-str", "negative", "huge", "null", "float", "empty-str"],
)
def test_active_surface_pid_coercion_never_raises(
    hermes_home: Path, raw_pid: object, expected: int
) -> None:
    """Any pid shape must coerce; an implausible pid is simply never alive."""
    _write_v021_session_db(hermes_home, v021=True)
    _write_active_sessions(
        hermes_home,
        {"entries": [{"session_id": "sess_new", "surface": "cli", "pid": raw_pid}]},
    )

    # A deterministic liveness stub: implausible pids must stay dead without
    # consulting the host process table (a small pid like 3 exists on some
    # runners, which made the real check flaky).
    state = _collect_once(hermes_home, pid_exists=lambda pid: False)

    surface = state.active_surfaces[0]
    assert surface.pid == expected
    assert surface.alive is False
    assert "active_sessions" not in state.health.failed_sources


def test_context_length_cache_edit_refreshes_session_context_limit(hermes_home: Path):
    """Editing context_length_cache.yaml must not wait on a DB data_version change."""
    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/v1"
    )
    cache_path = hermes_home / "context_length_cache.yaml"
    cache_path.write_text("context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 100\n")
    c = Collector(hermes_home)
    try:
        assert c.collect().sessions[0].context_limit == 100
        # No DB write, same day: only the config file changed.
        cache_path.write_text("context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 200\n")
        assert c.collect().sessions[0].context_limit == 200
    finally:
        c.close()


def test_context_length_transient_read_retries_without_another_edit(hermes_home: Path, monkeypatch):
    from hermesd import file_cache as cache_module

    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/v1"
    )
    path = hermes_home / "context_length_cache.yaml"
    path.write_text("context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 100\n")
    collector = Collector(hermes_home)
    try:
        assert collector.collect().sessions[0].context_limit == 100
        path.write_text("context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 200\n")
        with monkeypatch.context() as transient:

            def fail_read(path):
                raise PermissionError("temporary YAML read failure")

            transient.setattr(cache_module, "_load_yaml", fail_read)
            stale = collector.collect()
        recovered = collector.collect()
        assert stale.sessions[0].context_limit == 100
        assert "session_models" in stale.health.failed_sources
        assert recovered.sessions[0].context_limit == 200
        assert "session_models" not in recovered.health.failed_sources
    finally:
        collector.close()


def test_context_length_edit_does_not_invalidate_unrelated_derived_entries(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/v1"
    )
    cache_path = hermes_home / "context_length_cache.yaml"
    cache_path.write_text("context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 100\n")
    c = Collector(hermes_home)
    calls = {"sessions": 0, "tokens_total": 0}
    original_sessions = c._collect_sessions
    original_total = c._collect_tokens_total

    def counted_sessions(rows: list | None = None) -> list:
        calls["sessions"] += 1
        return original_sessions(rows)

    def counted_total(rows: list | None = None):
        calls["tokens_total"] += 1
        return original_total(rows)

    try:
        c.collect()
        monkeypatch.setattr(c, "_collect_sessions", counted_sessions)
        monkeypatch.setattr(c, "_collect_tokens_total", counted_total)
        cache_path.write_text("context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 200\n")
        c.collect()
        assert calls["sessions"] == 1
        assert calls["tokens_total"] == 0
    finally:
        c.close()


def test_session_aging_out_of_7d_window_drops_out_within_the_same_day(hermes_home: Path):
    """The sliding 7d cutoff moves with the clock even when rows and date don't."""
    base = _today_epoch(time.time()) + 43200  # local noon: +61s stays same-day
    fake_now = [base]
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, input_tokens) VALUES (?, ?, ?, ?)",
        ("s1", "cli", base - 7 * 86400 + 30, 100),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: fake_now[0])
    try:
        first = c.collect()
        windows = {window.label: window for window in first.token_analytics.windows}
        assert windows["7d"].session_count == 1

        fake_now[0] = base + 61
        second = c.collect()
        windows = {window.label: window for window in second.token_analytics.windows}
        assert windows["7d"].session_count == 0
        assert windows["30d"].session_count == 1
    finally:
        c.close()


def test_frozen_clock_collects_are_deterministic(hermes_home: Path):
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
        first = c.collect()
        second = c.collect()
        assert first.token_analytics == second.token_analytics
        assert first.tokens_total == second.tokens_total
        assert first.sessions == second.sessions
    finally:
        c.close()


def test_collect_model_usage_populates_cost_split_fields(hermes_home: Path):
    """The collector threads the per-group actual/estimated split into ModelUsage."""
    now = time.time()
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    create_state_db_tables(conn, include_schema_version=False, include_v021_columns=True)
    insert_model_usage(
        conn,
        "s1",
        "gpt-5.4",
        provider="openai",
        input_tokens=100,
        estimated_cost_usd=1.5,
        actual_cost_usd=1.0,
        last_seen=now,
    )
    insert_model_usage(
        conn,
        "s2",
        "gpt-5.4",
        provider="openai",
        input_tokens=50,
        estimated_cost_usd=2.0,
        actual_cost_usd=None,
        last_seen=now,
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
        assert state.token_analytics.usage_source == "session_model_usage"
        usage = next(
            entry for entry in state.token_analytics.model_usage_all if entry.model == "gpt-5.4"
        )
        assert usage.row_count == 2
        assert usage.reported_row_count == 1
        assert usage.reported_cost_usd == pytest.approx(1.0)
        assert usage.estimated_only_cost_usd == pytest.approx(2.0)
        assert usage.has_actual_cost is True
    finally:
        c.close()


# ── Item 6: turn leases and compression locks ───────────────────────────────

_COORD_NOW = 1_800_000_000.0
_HOLDER_FMT = "pid={pid}:tid={tid}:agent={agent}:nonce={nonce}"


def _make_coordination_db(hermes_home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(hermes_home / "state.db")
    create_state_db_tables(conn, include_schema_version=False, include_session_key=True)
    create_session_coordination_tables(conn)
    return conn


def test_turn_leases_and_compression_locks_surface_with_liveness(hermes_home: Path) -> None:
    conn = _make_coordination_db(hermes_home)
    insert_turn_lease(
        conn,
        "conv-root",
        _HOLDER_FMT.format(pid=101, tid=7, agent="1f", nonce="abcd1234"),
        _COORD_NOW - 60,
        _COORD_NOW + 240,
    )
    insert_compression_lock(
        conn,
        "sess-lock",
        _HOLDER_FMT.format(pid=102, tid=7, agent="2a", nonce="beefcafe"),
        _COORD_NOW - 120,
        _COORD_NOW - 30,
    )
    conn.commit()
    conn.close()
    c = Collector(
        hermes_home,
        clock=lambda: _COORD_NOW,
        pid_exists=lambda pid: pid == 101,
    )
    state = c.collect()
    c.close()
    coord = state.session_coordination
    assert coord.lease_total == 2
    by_kind = {lease.kind: lease for lease in coord.leases}
    turn = by_kind[SessionLeaseKind.TURN_LEASE]
    assert turn.key == "conv-root"
    assert turn.holder.endswith("abcd1234")
    assert turn.pid == 101
    assert turn.liveness is ProcessLiveness.LIVE
    assert turn.held_seconds == 60
    assert turn.expires_in_seconds == 240
    assert turn.expired is False
    assert turn.orphaned is False
    lock = by_kind[SessionLeaseKind.COMPRESSION_LOCK]
    assert lock.key == "sess-lock"
    assert lock.pid == 102
    assert lock.liveness is ProcessLiveness.DEAD
    assert lock.expired is True
    assert lock.orphaned is True
    assert lock.held_seconds == 120
    assert lock.expires_in_seconds == -30


def test_expired_lease_with_live_holder_is_not_orphaned(hermes_home: Path) -> None:
    """An expired lease whose holder still matches is revived upstream, not
    stolen (hermes_state_compression.py:433-439), so expiry alone is benign."""
    conn = _make_coordination_db(hermes_home)
    insert_compression_lock(
        conn,
        "sess-lock",
        _HOLDER_FMT.format(pid=101, tid=7, agent="2a", nonce="beefcafe"),
        _COORD_NOW - 600,
        _COORD_NOW - 10,
    )
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (lock,) = state.session_coordination.leases
    assert lock.expired is True
    assert lock.liveness is ProcessLiveness.LIVE
    assert lock.orphaned is False


def test_holder_without_parseable_pid_is_unverifiable_not_orphaned(hermes_home: Path) -> None:
    """Upstream reclaims on kernel proof only (hermes_state.py:119-143): a holder
    with no local pid keeps its lease until TTL and never reads as orphaned."""
    conn = _make_coordination_db(hermes_home)
    insert_turn_lease(conn, "conv-legacy", "legacy-holder", _COORD_NOW - 10, _COORD_NOW + 290)
    conn.commit()
    conn.close()
    probed: list[int] = []
    c = Collector(
        hermes_home,
        clock=lambda: _COORD_NOW,
        pid_exists=lambda pid: probed.append(pid) or True,
    )
    state = c.collect()
    c.close()
    (lease,) = state.session_coordination.leases
    assert lease.pid == 0
    assert lease.liveness is ProcessLiveness.UNVERIFIABLE
    assert lease.orphaned is False
    assert probed == []  # a pid-less holder is never probed


def test_absent_coordination_tables_read_as_empty(hermes_home: Path) -> None:
    conn = sqlite3.connect(hermes_home / "state.db")
    create_state_db_tables(conn, include_schema_version=False)
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    assert state.session_coordination == SessionCoordinationState()
    assert "session_leases" not in state.health.failed_sources
    assert "gateway_hygiene" not in state.health.failed_sources
    assert "gateway_routes" not in state.health.failed_sources
    assert "generation_churn" not in state.health.failed_sources


def test_junk_lease_columns_coerce_without_failing_the_source(hermes_home: Path) -> None:
    """The writer's columns are NOT NULL, but legacy tooling can still put text
    in epoch columns: coercion must degrade the row, never the source."""
    conn = _make_coordination_db(hermes_home)
    conn.execute("INSERT INTO session_turn_leases VALUES ('conv-null', '', 'not-a-number', 'x')")
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (lease,) = state.session_coordination.leases
    assert lease.holder == ""
    assert lease.pid == 0
    assert lease.held_seconds is None
    assert lease.expires_in_seconds is None
    assert lease.expired is False


# ── Item 7: hygiene failure streaks ─────────────────────────────────────────


def test_hygiene_rows_join_session_error_and_mark_suspension(hermes_home: Path) -> None:
    conn = sqlite3.connect(hermes_home / "state.db")
    create_state_db_tables(
        conn, include_schema_version=False, include_v021_columns=True, include_session_key=True
    )
    create_session_coordination_tables(conn)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, session_key, compression_failure_error) "
        "VALUES ('sess_h', 'gateway', ?, 'telegram:42:7', 'summary model timeout')",
        (_COORD_NOW - 30,),
    )
    conn.execute("INSERT INTO gateway_hygiene_state VALUES ('telegram:42:7', 4)")
    conn.execute("INSERT INTO gateway_hygiene_state VALUES ('discord:9:1', 2)")
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    hygiene = {row.session_key: row for row in state.session_coordination.hygiene}
    assert hygiene["telegram:42:7"].failure_streak == 4
    assert hygiene["telegram:42:7"].suspended is True
    assert hygiene["telegram:42:7"].compression_failure_error == "summary model timeout"
    assert hygiene["discord:9:1"].failure_streak == 2
    assert hygiene["discord:9:1"].suspended is False
    assert hygiene["discord:9:1"].compression_failure_error == ""


def test_hygiene_rows_join_the_error_of_a_hidden_session(hermes_home: Path) -> None:
    """Bot Mode chats are born hidden, yet their hygiene streak is still live.

    The join must read the unfiltered sessions table, like the route targets
    do, or a hidden chat's recorded compression failure is lost.
    """
    conn = sqlite3.connect(hermes_home / "state.db")
    create_state_db_tables(
        conn, include_schema_version=False, include_v021_columns=True, include_session_key=True
    )
    create_session_coordination_tables(conn)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, session_key, compression_failure_error, "
        "hidden) VALUES ('sess_bot', 'gateway', ?, 'telegram:77:1', 'summary model timeout', 1)",
        (_COORD_NOW - 30,),
    )
    conn.execute("INSERT INTO gateway_hygiene_state VALUES ('telegram:77:1', 3)")
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    assert state.sessions == []
    (row,) = state.session_coordination.hygiene
    assert row.compression_failure_error == "summary model timeout"


def test_zero_streak_hygiene_rows_are_not_reported(hermes_home: Path) -> None:
    conn = _make_coordination_db(hermes_home)
    conn.execute("INSERT INTO gateway_hygiene_state VALUES ('telegram:42:7', 0)")
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    assert state.session_coordination.hygiene == []


def test_streak_three_is_the_suspension_threshold(hermes_home: Path) -> None:
    conn = _make_coordination_db(hermes_home)
    conn.execute("INSERT INTO gateway_hygiene_state VALUES ('k2', 3)")
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (row,) = state.session_coordination.hygiene
    assert row.suspended is True


# ── Item 8: routing entry state flags ───────────────────────────────────────


def _iso(offset_seconds: float) -> str:
    """ISO stamp relative to the frozen coordination clock."""
    return datetime.fromtimestamp(_COORD_NOW - offset_seconds, tz=UTC).isoformat()


def _route_entry(**overrides: object) -> dict:
    entry: dict = {
        "session_key": "telegram:42:7",
        "session_id": "sess_r",
        "created_at": _iso(9600),
        "updated_at": _iso(30),
        "display_name": "dev chat",
        "platform": "telegram",
        "chat_type": "group",
        "metadata": {"watermark": 5},
        "suspended": False,
        "resume_pending": False,
        "resume_reason": None,
        "was_auto_reset": False,
        "auto_reset_reason": None,
        "active_turn_token": None,
        "active_turn_started_at": None,
        "input_tokens": 10,
        "total_tokens": 12,
    }
    entry.update(overrides)
    return entry


def _insert_route_with_session(hermes_home: Path, entry: dict) -> None:
    conn = _make_coordination_db(hermes_home)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('sess_r', 'gateway', ?)",
        (_COORD_NOW - 60,),
    )
    conn.execute(
        "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("/sessions/dir", "telegram:42:7", json.dumps(entry), _COORD_NOW - 30),
    )
    conn.commit()
    conn.close()


def test_gateway_route_decodes_entry_state(hermes_home: Path) -> None:
    _insert_route_with_session(
        hermes_home,
        _route_entry(
            updated_at=_iso(30),
            resume_pending=True,
            resume_reason="restart_timeout",
            was_auto_reset=True,
            auto_reset_reason="idle",
            active_turn_token="tok-1",
            active_turn_started_at=_iso(700),
        ),
    )
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert state.session_coordination.route_total == 1
    assert route.session_key == "telegram:42:7"
    assert route.session_id == "sess_r"
    assert route.platform == "telegram"
    assert route.chat_type == "group"
    assert route.display_name == "dev chat"
    assert route.updated_at_age_seconds == 30
    assert route.resume_pending is True
    assert route.resume_reason == "restart_timeout"
    assert route.was_auto_reset is True
    assert route.auto_reset_reason == "idle"
    assert route.turn_age_seconds == 700
    assert route.turn_never_unwound is True
    assert route.needs_user_message is True
    assert route.dangling is False
    # entry_json carries token counters and Slack watermarks; none of that
    # reaches the model.
    assert "watermark" not in str(route.model_dump())


def test_gateway_route_turn_under_grace_is_not_a_crash_marker(hermes_home: Path) -> None:
    _insert_route_with_session(
        hermes_home,
        _route_entry(
            active_turn_token="tok-1",
            active_turn_started_at=_iso(60),
        ),
    )
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert route.turn_age_seconds == 60
    assert route.turn_never_unwound is False


def test_gateway_route_without_token_has_no_turn_age(hermes_home: Path) -> None:
    _insert_route_with_session(hermes_home, _route_entry())
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert route.turn_age_seconds is None
    assert route.turn_never_unwound is False


def test_gateway_route_with_unknown_session_is_dangling(hermes_home: Path) -> None:
    _insert_route_with_session(hermes_home, _route_entry(session_id="ghost-session"))
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert route.dangling is True


def test_gateway_route_to_hidden_session_is_not_dangling(hermes_home: Path) -> None:
    """Hidden means "out of the default listing", not "gone".

    ``hermes_state_sessions.py:898-900`` hides a session from the listing while
    keeping it resumable, and ``get_session`` (:737-746) looks it up by id with
    no hidden filter; canonical bot chats are born hidden. Resolving routes
    against the visible listing reported those targets as pointing at a
    nonexistent session.
    """
    _insert_route_with_session(hermes_home, _route_entry(session_id="sess_r"))
    conn = sqlite3.connect(hermes_home / "state.db")
    # The v0.21 column the listing filter keys on; the minimal fixture schema
    # does not carry it.
    conn.execute("ALTER TABLE sessions ADD COLUMN hidden INTEGER DEFAULT 0")
    conn.execute("UPDATE sessions SET hidden = 1 WHERE id = 'sess_r'")
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    try:
        state = c.collect()
    finally:
        c.close()

    (route,) = state.session_coordination.routes
    assert route.dangling is False
    # The listing still omits it: the two questions have different answers.
    assert [session.session_id for session in state.sessions] == []


def test_gateway_route_display_name_is_redacted(hermes_home: Path) -> None:
    _insert_route_with_session(
        hermes_home, _route_entry(display_name="bot api_key: sk-live-abc123")
    )
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert "sk-live-abc123" not in route.display_name
    assert "[REDACTED]" in route.display_name


def test_gateway_route_display_name_scrubs_bare_credential(hermes_home: Path) -> None:
    """A chat display name is remote-controlled free text: a bare token carries
    no ``key = value`` label for the field redactor to key on."""
    secret = "sk-live-abcdefghijklmnop"
    _insert_route_with_session(hermes_home, _route_entry(display_name=f"Ops bot {secret}"))
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert route.display_name == "Ops bot [REDACTED]"
    assert secret not in json.dumps(state.model_dump(mode="json"))


def test_gateway_route_display_name_scrubs_jwt_and_github_token(hermes_home: Path) -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )
    pat = "ghp_abcdefghijklmnopqrst"
    _insert_route_with_session(hermes_home, _route_entry(display_name=f"a {jwt} b {pat}"))
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert route.display_name == "a [REDACTED] b [REDACTED]"


def test_gateway_route_display_name_without_credentials_is_unchanged(hermes_home: Path) -> None:
    _insert_route_with_session(hermes_home, _route_entry(display_name="Bob (ops)"))
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert route.display_name == "Bob (ops)"


def test_gateway_route_reasons_scrub_bare_credentials(hermes_home: Path) -> None:
    """The resume/auto-reset reasons reach the panel and the JSON snapshot too."""
    _insert_route_with_session(
        hermes_home,
        _route_entry(
            resume_pending=True,
            resume_reason="auth failed for sk-live-abcdefghijklmnop",
            was_auto_reset=True,
            auto_reset_reason="xoxb-abcdefghijklmnopqrst",
        ),
    )
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert route.resume_reason == "auth failed for [REDACTED]"
    assert route.auto_reset_reason == "[REDACTED]"


def test_redact_bare_credentials_scrubs_known_prefixes() -> None:
    for value in (
        "sk-abcdefghijklmnop",
        "pk-abcdefghijklmnop",
        "rk-abcdefghijklmnop",
        "ghp_abcdefghijklmnop",
        "gho_abcdefghijklmnop",
        "ghs_abcdefghijklmnop",
        "github_pat_abcdefghijklmnop",
        "xoxb-abcdefghijklmnop",
        "xoxp-abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    ):
        assert _redact_bare_credentials(f"name {value} tail") == "name [REDACTED] tail"
    # Too short to be a real credential, and ordinary words, stay visible.
    for value in (
        "sk-short",
        "ghp_short",
        "Bob (ops)",
        "task-list",
        "eyJhbGci",
        "risk-reward",
        "x" * 30_000,
    ):
        assert _redact_bare_credentials(value) == value


def test_gateway_route_junk_entry_json_degrades_to_key_only(hermes_home: Path) -> None:
    conn = _make_coordination_db(hermes_home)
    conn.execute(
        "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) "
        "VALUES ('/sessions/dir', 'telegram:42:7', 'not-json{{', ?)",
        (_COORD_NOW - 30,),
    )
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    (route,) = state.session_coordination.routes
    assert route.session_key == "telegram:42:7"
    assert route.session_id == ""
    assert route.dangling is False
    assert route.turn_age_seconds is None
    assert "gateway_routes" not in state.health.failed_sources


# ── Item 9: reset churn counter ─────────────────────────────────────────────


def test_generation_churn_top_chats_and_lifetime_total(hermes_home: Path) -> None:
    conn = _make_coordination_db(hermes_home)
    conn.executemany(
        "INSERT INTO conversation_generations VALUES (?,?,?)",
        [("cli", "k1", 5), ("cli", "k2", 9), ("telegram", "k3", 2)],
    )
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    coord = state.session_coordination
    assert [(g.session_key, g.generation) for g in coord.generations] == [
        ("k2", 9),
        ("k1", 5),
        ("k3", 2),
    ]
    assert coord.generation_chat_total == 3
    assert coord.generation_reset_total == 16
    assert coord.generation_count_shrank is False


def test_generation_churn_flags_a_shrinking_table(hermes_home: Path) -> None:
    """conversation_generations is never pruned upstream
    (hermes_state_common.py:460-487): a shrink between refreshes means
    something broke the no-prune invariant, so it surfaces as a warning.

    The warning latches on the high-water count: re-reading the same shrunken
    table must not clear it after one refresh, because the operator's only cue
    would then depend on catching the exact refresh that saw the drop.
    """
    conn = _make_coordination_db(hermes_home)
    conn.executemany(
        "INSERT INTO conversation_generations VALUES (?,?,?)",
        [("cli", "k1", 5), ("cli", "k2", 9), ("cli", "k3", 2)],
    )
    conn.commit()
    conn.close()
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    assert state.session_coordination.generation_chat_total == 3
    assert state.session_coordination.generation_count_shrank is False
    conn = sqlite3.connect(hermes_home / "state.db")
    conn.executemany(
        "DELETE FROM conversation_generations WHERE session_key = ?", [("k2",), ("k3",)]
    )
    conn.commit()
    conn.close()
    state = c.collect()
    assert state.session_coordination.generation_chat_total == 1
    assert state.session_coordination.generation_count_shrank is True
    # Still shrunken on the next refresh: the warning stands.
    state = c.collect()
    assert state.session_coordination.generation_count_shrank is True
    # A partial recovery below the pre-shrink count is still a shrink.
    conn = sqlite3.connect(hermes_home / "state.db")
    conn.execute("INSERT INTO conversation_generations VALUES ('cli','k4',2)")
    conn.commit()
    conn.close()
    state = c.collect()
    assert state.session_coordination.generation_chat_total == 2
    assert state.session_coordination.generation_count_shrank is True
    # Recovering to the pre-shrink count clears it.
    conn = sqlite3.connect(hermes_home / "state.db")
    conn.execute("INSERT INTO conversation_generations VALUES ('cli','k5',2)")
    conn.commit()
    conn.close()
    state = c.collect()
    c.close()
    assert state.session_coordination.generation_chat_total == 3
    assert state.session_coordination.generation_count_shrank is False


# ── Item 22 (sessions half): terminal breadcrumbs ───────────────────────────


def _write_breadcrumb(directory: Path, name: str, payload: object) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(json.dumps(payload))


def test_terminal_breadcrumbs_report_recent_cli_terminals(hermes_home: Path) -> None:
    directory = hermes_home / "terminal-sessions"
    _write_breadcrumb(
        directory,
        "tty-dev-pts-3",
        {"session_id": "sess_t", "cwd": "/repo/checkout", "ts": _COORD_NOW - 7200},
    )
    _write_breadcrumb(
        directory,
        "tmux_pane-2",
        {"session_id": "sess_old", "cwd": "/old", "ts": _COORD_NOW - 3 * 86400},
    )
    # The writer's intermediate temp file and junk must never surface.
    (directory / ".tty-dev-pts-3.tmp").write_text("{")
    (directory / "tty-broken").write_text("not json")
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    readout = state.terminal_sessions
    assert readout.count == 1
    (row,) = readout.sessions
    assert row.terminal == "tty-dev-pts-3"
    assert row.session_id == "sess_t"
    assert row.cwd == "/repo/checkout"
    assert row.age_seconds == 7200


def test_terminal_breadcrumbs_absent_directory_is_empty(hermes_home: Path) -> None:
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    assert state.terminal_sessions.count == 0
    assert state.terminal_sessions.sessions == []


def test_terminal_breadcrumbs_deeply_nested_json_reads_as_junk(hermes_home: Path) -> None:
    """A nesting bomb is junk, not a failed source.

    ``json.loads`` refuses nesting deep enough to exhaust the decoder with
    ``RecursionError`` rather than a ``JSONDecodeError``, so the guard has to
    name it: otherwise the terminal-sessions source lands in failed_sources
    over one hostile breadcrumb.
    """
    directory = hermes_home / "terminal-sessions"
    directory.mkdir()
    (directory / "tty-bomb").write_text('{"ts": ' + "[" * 3000 + "]" * 3000 + "}")
    _write_breadcrumb(
        directory,
        "tty-dev-pts-3",
        {"session_id": "sess_t", "cwd": "/repo", "ts": _COORD_NOW - 60},
    )

    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "terminal_sessions" not in state.health.failed_sources
    assert state.terminal_sessions.count == 1


def test_terminal_breadcrumb_rows_are_bounded(hermes_home: Path) -> None:
    directory = hermes_home / "terminal-sessions"
    for i in range(15):
        _write_breadcrumb(
            directory,
            f"tty-dev-pts-{i}",
            {"session_id": f"s{i}", "cwd": "/r", "ts": _COORD_NOW - 60 * i},
        )
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    assert state.terminal_sessions.count == 15
    assert len(state.terminal_sessions.sessions) == 12


def test_terminal_breadcrumb_rows_keep_the_newest_not_the_first_named(hermes_home: Path) -> None:
    """The bounded row list is the most recent terminals, newest first, even
    when the newest breadcrumbs sort last by file name."""
    directory = hermes_home / "terminal-sessions"
    for i in range(15):
        _write_breadcrumb(
            directory,
            f"tty-{i:02d}",
            {"session_id": f"s{i}", "cwd": "/r", "ts": _COORD_NOW - 60 * (15 - i)},
        )
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    assert [row.session_id for row in state.terminal_sessions.sessions] == [
        f"s{i}" for i in range(14, 2, -1)
    ]


# ── Item 10: joinable session chip ──────────────────────────────────────────


def test_active_surface_joinable_chip_from_shared_runtime_url(hermes_home: Path) -> None:
    registry = hermes_home / "runtime" / "active_sessions.json"
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "session_id": "sess_join",
                        "surface": "cli",
                        "pid": 4242,
                        "lease_id": "lease-1",
                        "metadata": {"shared_runtime_url": "http://127.0.0.1:8123"},
                    },
                    {
                        "session_id": "sess_plain",
                        "surface": "gateway:telegram",
                        "pid": 4242,
                        "lease_id": "lease-2",
                        "metadata": {"live_session_id": "sess_plain"},
                    },
                ]
            }
        )
    )
    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: True)
    state = c.collect()
    c.close()
    by_session = {s.session_id: s for s in state.active_surfaces}
    assert by_session["sess_join"].joinable is True
    assert by_session["sess_plain"].joinable is False
    # The advertised URL must never be stored, let alone rendered.
    assert "127.0.0.1" not in str(state.active_surfaces)
    assert "8123" not in str(state.active_surfaces)


def test_hygiene_rows_report_an_exact_total_beyond_the_capped_list(hermes_home: Path) -> None:
    """A capped row list must never be presented as the count.

    ``SessionCoordinationState``'s own rule is that totals are exact while the
    lists are capped, and the compact marker used ``len()`` of the capped list.
    """
    conn = _make_coordination_db(hermes_home)
    for index in range(55):
        conn.execute(
            "INSERT INTO gateway_hygiene_state (session_key, failure_streak) VALUES (?, ?)",
            (f"telegram:{index}", index % 6 + 1),
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: _COORD_NOW)
    try:
        coord = c.collect().session_coordination
    finally:
        c.close()

    assert len(coord.hygiene) == 50
    assert coord.hygiene_total == 55


def test_route_total_is_reported_beside_capped_route_rows(hermes_home: Path) -> None:
    conn = _make_coordination_db(hermes_home)
    for index in range(55):
        insert_gateway_route(
            conn,
            f"telegram:{index}",
            {"session_id": "missing", "platform": "telegram", "suspended": True},
            _COORD_NOW - 30,
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: False)
    try:
        coord = c.collect().session_coordination
    finally:
        c.close()

    assert len(coord.routes) == 50
    assert coord.route_total == 55


def test_lease_expiry_boundary_is_inclusive(hermes_home: Path) -> None:
    """``expires_at <= now`` is already expired when the holder is alive.

    Upstream's reclaim boundary is inclusive for turn leases
    (``hermes_state_compression.py:519-536``); an exclusive comparison would
    report a lease live for one more refresh than the writer honours.
    """
    conn = _make_coordination_db(hermes_home)
    insert_turn_lease(
        conn,
        "conv-edge",
        _HOLDER_FMT.format(pid=101, tid=7, agent="1f", nonce="abcd1234"),
        _COORD_NOW - 300,
        _COORD_NOW,
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: pid == 101)
    try:
        lease = c.collect().session_coordination.leases[0]
    finally:
        c.close()

    assert lease.expires_in_seconds == 0.0
    assert lease.expired is True
    assert lease.liveness is ProcessLiveness.LIVE


def test_lease_list_is_capped_while_the_total_stays_exact(hermes_home: Path) -> None:
    conn = _make_coordination_db(hermes_home)
    for index in range(45):
        insert_turn_lease(
            conn,
            f"conv-{index}",
            _HOLDER_FMT.format(pid=101, tid=index, agent="1f", nonce="abcd1234"),
            _COORD_NOW - 10,
            _COORD_NOW + 290,
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: pid == 101)
    try:
        coord = c.collect().session_coordination
    finally:
        c.close()

    assert len(coord.leases) == 40
    assert coord.lease_total == 45


def test_gateway_route_suspended_flag_survives_the_decode_path(hermes_home: Path) -> None:
    """``suspended`` must be read from entry_json, not only painted by the panel."""
    conn = _make_coordination_db(hermes_home)
    insert_gateway_route(
        conn,
        "telegram:404",
        {
            "session_id": "sess-1",
            "platform": "telegram",
            "chat_type": "private",
            "display_name": "Ops",
            "suspended": True,
            "resume_pending": False,
            "was_auto_reset": False,
        },
        _COORD_NOW - 30,
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, session_key) VALUES (?,?,?,?)",
        ("sess-1", "telegram", _COORD_NOW - 1000, "telegram:404"),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: False)
    try:
        route = c.collect().session_coordination.routes[0]
    finally:
        c.close()

    assert route.suspended is True
    assert route.needs_user_message is True
    assert route.dangling is False


def test_gateway_route_display_name_is_clipped(hermes_home: Path) -> None:
    """A remote display name is clipped, not merely redacted."""
    conn = _make_coordination_db(hermes_home)
    insert_gateway_route(
        conn,
        "telegram:505",
        {"session_id": "sess-1", "platform": "telegram", "display_name": "N" * 200},
        _COORD_NOW - 30,
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: False)
    try:
        route = c.collect().session_coordination.routes[0]
    finally:
        c.close()

    assert len(route.display_name) == 40
    assert route.display_name == "N" * 40


def test_gateway_route_reasons_are_clipped(hermes_home: Path) -> None:
    """Both reasons are chat-controlled free text and need a bound, not just redaction.

    ``entry_json`` is capped at 64 KiB, so a single route could put two
    multi-thousand-character strings into the panel's flags cell and the JSON
    snapshot. Redaction runs first, so a credential cannot survive as a prefix.
    """
    conn = _make_coordination_db(hermes_home)
    insert_gateway_route(
        conn,
        "telegram:506",
        {
            "session_id": "sess-1",
            "platform": "telegram",
            "resume_pending": True,
            "resume_reason": "R" * 5000,
            "was_auto_reset": True,
            # A credential just past the clip boundary: clipping before
            # redacting would leave a recognisable token prefix in the cell.
            "auto_reset_reason": "A" * 30 + "sk-live-abcdefghijklmnop",
        },
        _COORD_NOW - 30,
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: _COORD_NOW, pid_exists=lambda pid: False)
    try:
        route = c.collect().session_coordination.routes[0]
    finally:
        c.close()

    assert route.resume_reason == "R" * 40
    assert route.auto_reset_reason == "A" * 30 + "[REDACTED]"
    assert "sk-live" not in route.auto_reset_reason
