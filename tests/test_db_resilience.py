"""Tests for DB resilience: cache preservation, reconnection, WAL handling."""

import sqlite3
import time
from pathlib import Path

from hermesd.db import HermesDB
from tests.conftest import create_state_db_tables


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    create_state_db_tables(conn, include_schema_version=False)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, message_count, tool_call_count, "
        "input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("s1", "cli", now, 10, 5, 5000, 3000),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        ("s1", "assistant", "used a tool", "shell_exec", now),
    )
    conn.commit()
    conn.close()


def test_cache_preserved_on_query_error(tmp_path, monkeypatch):
    """Cache must not be wiped when a query fails."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()
    assert len(sessions) == 1

    def fail_read(conn: sqlite3.Connection) -> list[dict[str, object]]:
        raise sqlite3.OperationalError("query failed")

    monkeypatch.setattr(db, "_current_version", lambda: 999)
    monkeypatch.setattr(db, "_read_all_sessions", fail_read)

    # Should return cached data, not empty
    sessions2 = db.read_sessions()
    assert len(sessions2) == 1
    assert sessions2[0]["id"] == "s1"
    db.close()


def test_read_sessions_stale_flag_clears_on_recovered_cache_hit(tmp_path, monkeypatch):
    """A transient read error must not keep later cached reads marked stale."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()
    cached_version = db._cached_sessions_version

    def fail_read(conn: sqlite3.Connection) -> list[dict[str, object]]:
        raise sqlite3.OperationalError("query failed")

    monkeypatch.setattr(db, "_current_version", lambda: None)
    monkeypatch.setattr(db, "_read_all_sessions", fail_read)

    assert db.read_sessions() == sessions
    assert db.last_read_sessions_stale is True

    monkeypatch.setattr(db, "_current_version", lambda: cached_version)

    assert db.read_sessions() == sessions
    assert db.last_read_sessions_stale is False
    db.close()


def test_cache_preserved_for_tool_stats(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    stats = db.read_tool_stats()
    assert stats == [{"tool_name": "shell_exec", "call_count": 1}]

    def fail_read(conn: sqlite3.Connection) -> list[dict[str, object]]:
        raise sqlite3.OperationalError("query failed")

    monkeypatch.setattr(db, "_current_version", lambda: 999)
    monkeypatch.setattr(db, "_read_tool_stats", fail_read)

    stats2 = db.read_tool_stats()
    assert stats2 == stats
    db.close()


def test_reconnect_after_consecutive_errors(tmp_path, monkeypatch):
    """After 3 consecutive errors, DB should attempt reconnection."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)

    # First successful read
    sessions = db.read_sessions()
    assert len(sessions) == 1

    connect_calls = 0

    original_connect = sqlite3.connect

    def counting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    def fail_read(conn: sqlite3.Connection) -> list[dict[str, object]]:
        raise sqlite3.OperationalError("query failed")

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    monkeypatch.setattr(db, "_current_version", lambda: 999)
    monkeypatch.setattr(db, "_read_all_sessions", fail_read)

    # Force 3 query errors; the third should trigger a real reconnect attempt.
    for _ in range(3):
        db.read_sessions()

    assert connect_calls == 1
    assert db.read_sessions() == sessions
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
    assert result is not None
    assert db._conn is not None
    db.close()


def test_missing_db_returns_empty_not_crash(tmp_path):
    db = HermesDB(tmp_path / "nonexistent.db")
    assert db.read_sessions() == []
    assert db.read_tool_stats() == []
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
