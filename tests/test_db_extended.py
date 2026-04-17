import sqlite3
import threading
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


def test_db_serializes_cross_thread_reads(hermes_home):
    db = HermesDB(hermes_home / "state.db")
    entered = threading.Event()
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
            results["search"] = db.search_session_ids_by_message("sess_001")
        except BaseException as exc:  # pragma: no cover - exercised on failure
            errors.append(exc)

    first = threading.Thread(target=run_read)
    second = threading.Thread(target=run_search)
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join()
    second.join()

    assert errors == []
    assert results["sessions"] == [{"id": "sess_001"}]
    assert results["search"] == {"sess_001"}
    db.close()
