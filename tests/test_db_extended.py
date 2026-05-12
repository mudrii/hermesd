import shutil
import sqlite3
import threading
import time
from pathlib import Path

from hermesd.db import HermesDB
from tests.conftest import create_state_db_tables


def test_tool_stats_with_data(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    stats = db.read_tool_stats()
    assert len(stats) >= 1
    assert stats[0]["tool_name"] == "shell_exec"
    assert stats[0]["call_count"] == 3
    db.close()


def test_sessions_empty_db(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.close()
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    assert sessions == []
    db.close()


def test_read_only_uri_is_immutable_and_does_not_create_sidecars(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_001', 'cli', 1.0)")
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.read_sessions()[0]["id"] == "sess_001"
    assert "mode=ro&immutable=1" in db._uri
    assert not db_path.with_name("state.db-wal").exists()
    assert not db_path.with_name("state.db-shm").exists()
    db.close()


def test_wal_snapshot_reads_uncheckpointed_data_without_home_sidecars(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_db = source_dir / "state.db"
    writer = sqlite3.connect(str(source_db))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_wal', 'cli', 1.0)")
    writer.commit()

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    db_path = hermes_home / "state.db"
    shutil.copy2(source_db, db_path)
    shutil.copy2(source_db.with_name("state.db-wal"), db_path.with_name("state.db-wal"))
    assert not db_path.with_name("state.db-shm").exists()

    db = HermesDB(db_path)
    try:
        assert [row["id"] for row in db.read_sessions()] == ["sess_wal"]
        assert db._uri.endswith("?mode=ro")
        assert not db_path.with_name("state.db-shm").exists()
    finally:
        db.close()
        writer.close()


def test_empty_sessions_are_cached(hermes_home, monkeypatch):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.close()
    db = HermesDB(db_path)
    reads = 0
    original_read_all_sessions = db._read_all_sessions

    def counting_read_all_sessions(conn: sqlite3.Connection) -> list[dict[str, object]]:
        nonlocal reads
        reads += 1
        return original_read_all_sessions(conn)

    monkeypatch.setattr(db, "_read_all_sessions", counting_read_all_sessions)

    assert db.read_sessions() == []
    assert db.read_sessions() == []
    assert reads == 1
    db.close()


def test_empty_tool_stats_are_cached(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.close()
    db = HermesDB(db_path)
    reads = 0
    original_read_tool_stats = db._read_tool_stats

    def counting_read_tool_stats(conn: sqlite3.Connection) -> list[dict[str, object]]:
        nonlocal reads
        reads += 1
        return original_read_tool_stats(conn)

    db._read_tool_stats = counting_read_tool_stats  # type: ignore[assignment]
    assert db.read_tool_stats() == []
    assert db.read_tool_stats() == []
    assert reads == 1
    db.close()


def test_read_only_uri_handles_uri_metacharacters(tmp_path: Path):
    hermes_home = tmp_path / "hermes?demo#home"
    hermes_home.mkdir()
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_uri', 'cli', 1.0);
    """)
    conn.close()

    db = HermesDB(db_path)
    sessions = db.read_sessions()
    assert [row["id"] for row in sessions] == ["sess_uri"]
    assert "mode=ro&immutable=1" in db._uri
    assert "?" not in db._uri.removeprefix("file://").split("?mode=ro&immutable=1", 1)[0]
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


def test_search_session_ids_by_message_like(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    session_ids = db.search_session_ids_by_message("response 0")
    assert session_ids == {"sess_001"}
    db.close()


def test_read_sessions_refreshes_updated_older_rows(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    older_session = next(row for row in sessions if row["id"] == "sess_001")
    assert older_session["message_count"] == 77

    conn = sqlite3.connect(str(sample_db))
    conn.execute("UPDATE sessions SET message_count = 999 WHERE id = ?", ("sess_001",))
    conn.commit()
    conn.close()

    refreshed_sessions = db.read_sessions()
    refreshed = next(row for row in refreshed_sessions if row["id"] == "sess_001")
    assert refreshed["message_count"] == 999
    db.close()


def test_read_tool_stats_refreshes_after_sessions_read(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    db.read_sessions()
    original_stats = db.read_tool_stats()
    assert original_stats[0]["tool_name"] == "shell_exec"

    conn = sqlite3.connect(str(sample_db))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("sess_002", "assistant", "new tool", "browser_open", 1.0),
    )
    conn.commit()
    conn.close()

    db.read_sessions()
    refreshed_stats = db.read_tool_stats()
    names = {row["tool_name"] for row in refreshed_stats}
    assert "browser_open" in names
    db.close()


def test_read_session_count_refreshes_after_write(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    assert db.read_session_count() == 2

    conn = sqlite3.connect(str(sample_db))
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "sess_003",
            "cli",
            None,
            "gpt-5.4",
            None,
            None,
            None,
            time.time(),
            None,
            None,
            1,
            0,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            None,
            0.0,
            None,
            None,
            None,
            None,
            "new title",
        ),
    )
    conn.commit()
    conn.close()

    assert db.read_session_count() == 3
    db.close()


def test_read_session_count_cache_hit_skips_sql(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    reads = 0
    original_read_session_count = db._read_session_count

    def counting_read_session_count(conn: sqlite3.Connection) -> int:
        nonlocal reads
        reads += 1
        return original_read_session_count(conn)

    db._read_session_count = counting_read_session_count  # type: ignore[assignment]

    assert db.read_session_count() == 2
    assert db.read_session_count() == 2
    assert reads == 1
    db.close()


def test_read_sessions_reconnects_after_three_query_errors(sample_db, hermes_home, monkeypatch):
    db = HermesDB(hermes_home / "state.db")
    reconnects = 0
    original_connect = sqlite3.connect

    def fail_read(_conn: sqlite3.Connection) -> list[dict[str, object]]:
        raise sqlite3.OperationalError("db unavailable")

    def counting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal reconnects
        reconnects += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    monkeypatch.setattr(db, "_current_version", lambda: 1)
    monkeypatch.setattr(db, "_read_all_sessions", fail_read)

    assert db.read_sessions() == []
    assert db.read_sessions() == []
    assert reconnects == 0
    assert db.read_sessions() == []
    assert reconnects == 1
    db.close()


def test_search_session_ids_by_message_refreshes_same_query_after_write(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    session_ids = db.search_session_ids_by_message("shared term")
    assert session_ids == set()

    conn = sqlite3.connect(str(sample_db))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("sess_001", "assistant", "shared term", None, 2.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("sess_002", "assistant", "shared term", None, 3.0),
    )
    conn.commit()
    conn.close()

    refreshed_session_ids = db.search_session_ids_by_message("shared term")
    assert refreshed_session_ids == {"sess_001", "sess_002"}
    db.close()


def test_search_session_ids_by_message_falls_back_to_like_when_fts_misses(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions VALUES (
            'sess_001', 'cli', NULL, 'gpt-5.4',
            NULL, NULL, NULL,
            0, NULL, NULL,
            1, 0,
            0, 0,
            0, 0,
            0, 'openai',
            NULL, NULL,
            0.0, NULL,
            NULL, NULL, NULL,
            'title'
        );
        INSERT INTO messages (
            session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
            token_count, finish_reason, reasoning, reasoning_details, codex_reasoning_items
        ) VALUES (
            'sess_001', 'user', 'foo:bar', NULL, NULL, NULL, 0,
            NULL, NULL, NULL, NULL, NULL
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, tool_name, session_id UNINDEXED, content='messages', content_rowid='id'
        );
        INSERT INTO messages_fts (rowid, content, tool_name, session_id)
        SELECT id, content, tool_name, session_id FROM messages;
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    session_ids = db.search_session_ids_by_message("foo:")
    assert session_ids == {"sess_001"}
    db.close()


def test_search_session_ids_by_message_fts_without_session_id_joins_messages(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_name TEXT,
            timestamp REAL
        );
        INSERT INTO messages (session_id, role, content, tool_name, timestamp)
        VALUES ('sess_join', 'assistant', 'needle text', NULL, 1.0);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name);
        INSERT INTO messages_fts (rowid, content, tool_name)
        VALUES (1, 'needle text', NULL);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    session_ids = db.search_session_ids_by_message("needle")

    assert session_ids == {"sess_join"}
    db.close()


def test_search_session_ids_by_message_falls_back_to_like_when_fts_raises(
    sample_db,
    hermes_home,
    monkeypatch,
):
    db = HermesDB(hermes_home / "state.db")

    def fail_fts(conn: sqlite3.Connection, query: str) -> set[str]:
        raise sqlite3.OperationalError("fts failed")

    monkeypatch.setattr(db, "_messages_fts_enabled", lambda conn: True)
    monkeypatch.setattr(db, "_search_session_ids_by_fts", fail_fts)

    assert db.search_session_ids_by_message("response 0") == {"sess_001"}
    db.close()


def test_db_serializes_cross_thread_reads(hermes_home):
    db = HermesDB(hermes_home / "state.db")
    entered = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    active = threading.Lock()
    conn = object()
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def enter_critical() -> None:
        if not active.acquire(blocking=False):
            raise AssertionError("concurrent db access")
        entered.set()
        if not release.wait(timeout=1):
            active.release()
            raise AssertionError("timed out waiting for release")
        active.release()

    def fake_read_all_sessions(_conn: object) -> list[dict[str, object]]:
        enter_critical()
        return [{"id": "sess_001"}]

    def fake_search(_conn: object, query: str) -> set[str]:
        enter_critical()
        return {query}

    db._ensure_connection = lambda: conn  # type: ignore[assignment]
    db._current_version = lambda: 1  # type: ignore[assignment]
    db._messages_fts_enabled = lambda _conn: False  # type: ignore[assignment]
    db._read_all_sessions = fake_read_all_sessions  # type: ignore[assignment]
    db._search_session_ids_by_like = fake_search  # type: ignore[assignment]

    def run_read() -> None:
        try:
            results["sessions"] = db.read_sessions()
        except BaseException as exc:  # pragma: no cover - exercised on failure
            errors.append(exc)

    def run_search() -> None:
        try:
            second_started.set()
            results["search"] = db.search_session_ids_by_message("sess_001")
        except BaseException as exc:  # pragma: no cover - exercised on failure
            errors.append(exc)

    first = threading.Thread(target=run_read)
    second = threading.Thread(target=run_search)
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    assert second_started.wait(timeout=1)
    release.set()
    first.join()
    second.join()

    assert errors == []
    assert results["sessions"] == [{"id": "sess_001"}]
    assert results["search"] == {"sess_001"}
    db.close()
