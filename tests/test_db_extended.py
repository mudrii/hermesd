import json
import sqlite3
import time
from pathlib import Path

from hermesd.db import HermesDB


def test_tool_stats_with_data(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    stats = db.read_tool_stats()
    assert len(stats) >= 1
    assert stats[0]["tool_name"] == "shell_exec"
    assert stats[0]["call_count"] == 3
    db.close()


def test_token_totals_empty_db(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT,
            model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
            started_at REAL, ended_at REAL, end_reason TEXT,
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
    conn.close()
    db = HermesDB(hermes_home / "state.db")
    totals = db.read_token_totals()
    assert totals["input_tokens"] == 0
    assert totals["total_cost_usd"] == 0.0
    db.close()


def test_sessions_empty_db(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT,
            model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
            started_at REAL, ended_at REAL, end_reason TEXT,
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
    conn.close()
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    assert sessions == []
    db.close()


def test_close_idempotent(hermes_home):
    db = HermesDB(Path("/nonexistent/state.db"))
    db.close()
    db.close()


def test_read_after_close_returns_cached(sample_db, hermes_home):
    """After close, read returns last cached data (stale is better than empty)."""
    db = HermesDB(hermes_home / "state.db")
    sessions_before = db.read_sessions()
    assert len(sessions_before) == 2
    db.close()
    sessions_after = db.read_sessions()
    assert sessions_after == sessions_before
