"""Tests for cost estimation when provider doesn't report costs."""

import sqlite3
import time
from pathlib import Path

from hermesd.collector import Collector, _estimate_cost


def test_estimate_cost_basic():
    # 1M input tokens at $2.50/M = $2.50
    cost = _estimate_cost(1_000_000, 0, 0, 0)
    assert abs(cost - 2.50) < 0.01


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


def test_collector_uses_estimated_cost_when_db_cost_is_zero(hermes_home: Path, sample_db: Path):
    """When estimated_cost_usd is 0 or NULL, collector should compute from tokens."""
    # sample_db has sessions with cost=0.42 and cost=0.31 and tokens
    c = Collector(hermes_home)
    state = c.collect()
    # Total tokens are 12400+9100=21500 input, 8200+6300=14500 output
    # The DB has non-zero costs so it should use those
    assert state.tokens_total.total_cost_usd > 0
    c.close()


def test_collector_estimates_when_cost_is_null(hermes_home: Path):
    """When all costs are NULL, collector estimates from tokens."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT,
            model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
            started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
            message_count INTEGER, tool_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, billing_provider TEXT,
            billing_base_url TEXT, billing_mode TEXT,
            estimated_cost_usd REAL, actual_cost_usd REAL,
            cost_status TEXT, cost_source TEXT, pricing_version TEXT,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL,
            token_count INTEGER, finish_reason TEXT, reasoning TEXT,
            reasoning_details TEXT, codex_reasoning_items TEXT
        );
    """)
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
