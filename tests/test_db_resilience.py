"""Tests for DB resilience: cache preservation, reconnection, WAL handling."""
import json
import sqlite3
import time
from pathlib import Path

from hermesd.db import HermesDB


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
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
        "INSERT INTO sessions (id, source, started_at, message_count, tool_call_count, "
        "input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("s1", "cli", now, 10, 5, 5000, 3000),
    )
    conn.commit()
    conn.close()


def test_cache_preserved_on_query_error(tmp_path):
    """Cache must not be wiped when a query fails."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()
    assert len(sessions) == 1

    # Corrupt the connection to simulate an error
    db._conn.close()
    db._conn = None

    # Should return cached data, not empty
    sessions2 = db.read_sessions()
    assert len(sessions2) == 1
    assert sessions2[0]["id"] == "s1"
    db.close()


def test_cache_preserved_for_token_totals(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    totals = db.read_token_totals()
    assert totals["input_tokens"] == 5000

    db._conn.close()
    db._conn = None

    totals2 = db.read_token_totals()
    assert totals2["input_tokens"] == 5000
    db.close()


def test_cache_preserved_for_tool_stats(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    stats = db.read_tool_stats()
    # Empty tool_name in messages, so returns []
    assert isinstance(stats, list)

    db._conn.close()
    db._conn = None

    stats2 = db.read_tool_stats()
    assert stats2 == stats
    db.close()


def test_reconnect_after_consecutive_errors(tmp_path):
    """After 3 consecutive errors, DB should attempt reconnection."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)

    # First successful read
    sessions = db.read_sessions()
    assert len(sessions) == 1

    # Break the connection
    db._conn.close()
    db._conn = None

    # Force 3 errors (ensure_connection will reconnect each time)
    for _ in range(4):
        db.read_sessions()

    # After reconnection attempts, should get data back
    # (ensure_connection re-opens the file)
    sessions2 = db.read_sessions()
    assert len(sessions2) == 1
    db.close()


def test_ensure_connection_reopens(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    assert db._conn is not None

    db._conn.close()
    db._conn = None

    # _ensure_connection should reopen
    result = db._ensure_connection()
    assert result is True
    assert db._conn is not None
    db.close()


def test_missing_db_returns_empty_not_crash(tmp_path):
    db = HermesDB(tmp_path / "nonexistent.db")
    assert db.read_sessions() == []
    assert db.read_tool_stats() == []
    totals = db.read_token_totals()
    assert totals["input_tokens"] == 0
    db.close()


def test_concurrent_writer_does_not_wipe_cache(tmp_path):
    """Simulate hermes-agent writing while we read."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)

    sessions1 = db.read_sessions()
    assert len(sessions1) == 1

    # Simulate external write (new session added)
    writer = sqlite3.connect(str(db_path))
    writer.execute(
        "INSERT INTO sessions (id, source, started_at, message_count) VALUES (?, ?, ?, ?)",
        ("s2", "telegram", time.time(), 5),
    )
    writer.commit()
    writer.close()

    # Next read should pick up the change (data_version changed)
    sessions2 = db.read_sessions()
    assert len(sessions2) == 2
    db.close()
