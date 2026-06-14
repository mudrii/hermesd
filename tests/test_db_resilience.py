"""Tests for DB resilience: cache preservation, reconnection, WAL handling."""

from __future__ import annotations

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


def test_missing_db_returns_empty_not_crash(tmp_path):
    db = HermesDB(tmp_path / "nonexistent.db")
    assert db.read_sessions() == []
    assert db.read_tool_stats() == []
    db.close()


def test_unopenable_db_backs_off_two_reads_before_reconnect(tmp_path, monkeypatch):
    """A failed connect must back off for two reads before retrying, not retry every read."""
    db_dir = tmp_path / "state.db"
    db_dir.mkdir()  # a directory at the db path makes sqlite3.connect fail

    connect_calls = 0
    original_connect = sqlite3.connect

    def counting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    db = HermesDB(db_dir)  # connect attempt #1 fails
    assert connect_calls == 1
    assert db.read_sessions() == []  # backoff read 1: no reconnect
    assert db.read_sessions() == []  # backoff read 2: no reconnect
    assert connect_calls == 1
    assert db.read_sessions() == []  # backoff exhausted: reconnect attempt #2
    assert connect_calls == 2
    db.close()


def test_wal_snapshot_copy_failure_then_recovery(tmp_path):
    """If snapshotting a WAL db fails, reads stay safe and recover once readable again."""
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', 1.0)")
    writer.commit()
    assert db_path.with_name("state.db-wal").exists()

    db_path.chmod(0o000)  # snapshot copy raises PermissionError
    try:
        db = HermesDB(db_path)
        assert db.read_sessions() == []  # no crash, no data yet (backoff read 1)
        assert db.read_sessions() == []  # backoff read 2
    finally:
        db_path.chmod(0o644)

    # Backoff exhausted; next read reconnects and sees the data.
    assert [row["id"] for row in db.read_sessions()] == ["s1"]
    db.close()
    writer.close()


def test_db_file_deleted_marks_all_cached_reads_stale(tmp_path):
    """Losing the DB file flags every cached read surface stale while preserving data."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()
    count = db.read_session_count()
    stats = db.read_tool_stats()
    found = db.search_session_ids_by_message("used a tool")
    assert sessions and count == 1 and stats and found == {"s1"}

    db._conn.close()
    db._conn = None
    db_path.unlink()

    assert db.read_sessions() == sessions
    assert db.read_session_count() == count
    assert db.read_tool_stats() == stats
    assert db.search_session_ids_by_message("used a tool") == found
    assert db.last_read_sessions_stale is True
    assert db.last_read_session_count_stale is True
    assert db.last_read_tool_stats_stale is True
    assert db.last_message_search_stale is True
    # A different query has no cached answer: empty, not stale garbage.
    assert db.search_session_ids_by_message("never seen") == set()
    db.close()


def test_open_connection_serves_cached_sessions_when_source_file_disappears(tmp_path):
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()

    db_path.unlink()

    assert db.read_sessions() == sessions
    assert db.last_read_sessions_stale is False
    db.close()


def test_externally_closed_connection_recovers(tmp_path):
    """If the handle dies under us, reads serve cache, then reconnect restores live data."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()
    assert len(sessions) == 1

    db._conn.close()  # simulate the handle dying without our knowledge

    # Errors on the dead handle serve cached data; the third triggers reconnect.
    for _ in range(3):
        assert db.read_sessions() == sessions
    assert db.read_sessions() == sessions
    assert db.last_read_sessions_stale is False
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


def test_repeated_message_search_serves_cache_without_requery(tmp_path):
    """An unchanged repeat search hits the version cache and clears the stale flag."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)

    first = db.search_session_ids_by_message("used a tool")
    assert first == {"s1"}
    assert db.last_message_search_stale is False

    # No write between calls, so data_version is unchanged: the second call must
    # return the cached result set object and (re)assert a non-stale read.
    second = db.search_session_ids_by_message("used a tool")
    assert second == {"s1"}
    assert second is first  # same cached object, not a fresh query
    assert db.last_message_search_stale is False
    db.close()


def test_message_search_error_serves_last_good_and_flags_stale(tmp_path):
    """A failed search returns the last-good result set and marks the read stale."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)

    found = db.search_session_ids_by_message("used a tool")
    assert found == {"s1"}
    assert db.last_message_search_stale is False

    # Kill the live handle so the next query raises mid-search; the data_version
    # probe also fails, so we fall through to the query and into the error path.
    db._conn.close()

    after_error = db.search_session_ids_by_message("used a tool")
    assert after_error == found  # cache preserved, not blanked
    assert db.last_message_search_stale is True
    db.close()


def test_message_search_fts_check_failure_degrades_without_crashing(tmp_path):
    """If probing for the FTS table raises, search degrades gracefully (no crash)."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)

    # Kill the handle before any search runs, so FTS availability is still
    # undetermined: the sqlite_master probe inside _messages_fts_enabled raises
    # and must be swallowed (FTS treated as unavailable) rather than crashing.
    db._conn.close()

    assert db._messages_fts_available is None
    result = db.search_session_ids_by_message("used a tool")
    assert result == set()  # no cache yet, query failed -> empty, not a crash
    assert db._messages_fts_available is False
    db.close()


def test_message_search_reconnect_failure_resets_error_count(tmp_path):
    """Three consecutive search errors trigger a reconnect; a failed one resets the count."""
    db_path = tmp_path / "state.db"
    _create_db(db_path)
    db = HermesDB(db_path)

    found = db.search_session_ids_by_message("used a tool")
    assert found == {"s1"}

    # Kill the handle and remove the file so the reconnect attempt also fails.
    db._conn.close()
    db_path.unlink()

    # Each call serves cache while counting errors; the third hits the reconnect
    # threshold, and because the file is gone the reconnect yields no connection,
    # resetting the consecutive-error counter back to zero.
    for _ in range(2):
        assert db.search_session_ids_by_message("used a tool") == found
    assert db._consecutive_errors == 2
    assert db.search_session_ids_by_message("used a tool") == found
    assert db._conn is None
    assert db._consecutive_errors == 0
    assert db.last_message_search_stale is True
    db.close()
