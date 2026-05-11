"""Tests for cost estimation when provider doesn't report costs."""

import sqlite3
import time
from pathlib import Path

import pytest

from hermesd.collector import (
    Collector,
    _estimate_cost,
    _resolved_session_cost,
    _summarize_breakdown,
)
from tests.conftest import create_state_db_tables


def test_estimate_cost_basic():
    # 1M input tokens at $2.50/M = $2.50
    cost = _estimate_cost(1_000_000, 0, 0, 0)
    assert abs(cost - 2.50) < 0.01


def test_estimate_cost_clamps_negative_and_huge_token_counts():
    assert _estimate_cost(-100, -100, -100, -100) == 0.0
    assert _estimate_cost(10**400, 0, 0, 0) > 0.0


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


@pytest.mark.parametrize(
    ("cost_status", "estimated_cost", "expected"),
    [
        ("reported", None, _estimate_cost(100_000, 5_000, 50_000, 1_000)),
        ("reported", 5.0, 5.0),
        ("estimated", 0.0, _estimate_cost(100_000, 5_000, 50_000, 1_000)),
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


def test_collector_estimates_when_cost_is_null(hermes_home: Path):
    """When all costs are NULL, collector estimates from tokens."""
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
    # Should have an estimated cost > 0
    assert state.tokens_total.total_cost_usd > 0
    assert state.tokens_today.total_cost_usd > 0
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
    assert state.sessions[0].estimated_cost_usd > 0
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
    assert state.tokens_total.total_cost_usd >= 0.42 + expected_missing
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
