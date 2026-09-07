"""DB error paths: every test injects a failure and asserts the last-good data survives it."""

from __future__ import annotations

import sqlite3

import pytest

from hermesd.db import _RECONNECT_ERROR_THRESHOLD, HermesDB
from tests.conftest import (
    create_state_db_tables,
    create_state_db_with_session,
)


def test_db_file_deleted_marks_all_cached_reads_stale(tmp_path):
    """Losing the DB file flags every cached read surface stale while preserving data."""
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
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


def test_open_connection_marks_cached_sessions_stale_when_source_file_disappears(tmp_path):
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()

    db_path.unlink()

    assert db.read_sessions() == sessions
    assert db.last_read_sessions_stale is True
    db.close()


def test_externally_closed_connection_recovers(tmp_path):
    """If the handle dies under us, reads serve cache, then reconnect restores live data."""
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()
    assert len(sessions) == 1

    db._conn.close()  # simulate the handle dying without our knowledge

    # Errors on the dead handle serve cached data; reaching the threshold
    # triggers a reconnect on the next read.
    for _ in range(_RECONNECT_ERROR_THRESHOLD):
        assert db.read_sessions() == sessions
    assert db.read_sessions() == sessions
    assert db.last_read_sessions_stale is False
    db.close()


def test_exclusive_writer_lock_never_blanks_cached_sessions(tmp_path):
    """A writer holding BEGIN EXCLUSIVE must never turn a populated read into an empty one."""
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()
    assert len(sessions) == 1

    writer = sqlite3.connect(str(db_path), timeout=2, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        # Either the read wins outright or it serves the last-good rows; it must
        # not blank the display and must not raise.
        assert db.read_sessions() == sessions
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    # Once the lock is released the reader is healthy again.
    assert db.read_sessions() == sessions
    assert db.last_read_sessions_stale is False
    db.close()


@pytest.mark.parametrize(
    ("label", "payload"),
    [("zero_byte", b""), ("corrupt_header", b"garbage" * 100)],
)
def test_damaged_db_file_preserves_last_good_sessions(tmp_path, label, payload):
    """Truncating or corrupting state.db keeps the last-good rows and flags them stale."""
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
    db = HermesDB(db_path)
    sessions = db.read_sessions()
    assert len(sessions) == 1

    db_path.write_bytes(payload)

    assert db.read_sessions() == sessions
    assert db.last_read_sessions_stale is True
    db.close()


def test_truncated_wal_sidecar_preserves_last_good_sessions(tmp_path):
    """A half-written -wal sidecar must not raise or blank the session list."""
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', 1.0)")
    writer.commit()
    # Checkpoint so the row lives in the main db; the sidecar stays in place.
    writer.execute("PRAGMA wal_checkpoint(FULL)")
    wal_path = db_path.with_name("state.db-wal")
    assert wal_path.exists()

    db = HermesDB(db_path)
    sessions = db.read_sessions()
    assert [row["id"] for row in sessions] == ["s1"]

    with wal_path.open("r+b") as handle:
        handle.truncate(10)

    assert db.read_sessions() == sessions
    db.close()
    writer.close()


def test_message_search_error_serves_last_good_and_flags_stale(tmp_path):
    """A failed search returns the last-good result set and marks the read stale."""
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
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


def test_message_search_error_for_different_query_returns_empty_not_previous_result(tmp_path):
    """A failed search must not reuse a cached result for a different query."""
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
    db = HermesDB(db_path)

    found = db.search_session_ids_by_message("used a tool")
    assert found == {"s1"}
    assert db.last_message_search_stale is False

    db._conn.close()

    after_error = db.search_session_ids_by_message("never seen")
    assert after_error == set()
    assert db.last_message_search_stale is True
    db.close()


def test_message_search_reconnect_failure_resets_error_count(tmp_path):
    """Three consecutive search errors trigger a reconnect; a failed one resets the count."""
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
    db = HermesDB(db_path)

    found = db.search_session_ids_by_message("used a tool")
    assert found == {"s1"}

    # Kill the handle so search errors accumulate against the unchanged source.
    db._conn.close()

    # Each call serves cache while counting errors. Remove the source before the
    # third call so the reconnect attempt fails and resets the error count.
    for _ in range(2):
        assert db.search_session_ids_by_message("used a tool") == found
    assert db._consecutive_errors == 2
    db_path.unlink()
    assert db.search_session_ids_by_message("used a tool") == found
    assert db._conn is None
    assert db._consecutive_errors == 0
    assert db.last_message_search_stale is True
    db.close()
