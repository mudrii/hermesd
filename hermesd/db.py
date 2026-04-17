from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path
from typing import Any


class HermesDB:
    def __init__(self, db_path: Path):
        self._path = db_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._current_data_version: int | None = None
        self._cached_sessions: list[dict[str, Any]] = []
        self._cached_sessions_version: int | None = None
        self._cached_session_count = 0
        self._cached_session_count_version: int | None = None
        self._cached_tool_stats: list[dict[str, Any]] = []
        self._cached_tool_stats_version: int | None = None
        self._cached_message_search_query: str = ""
        self._cached_message_search_results: set[str] = set()
        self._cached_message_search_version: int | None = None
        self._cache_hits = 0
        self._uri = ""
        self._consecutive_errors = 0
        self._messages_fts_supports_session_id: bool | None = None
        self._messages_fts_available: bool | None = None
        self._connect()

    def _connect(self) -> None:
        if self._conn:
            with contextlib.suppress(sqlite3.Error, sqlite3.ProgrammingError):
                self._conn.close()
            self._conn = None
        if not self._path.exists():
            return
        self._uri = f"file:{self._path}?mode=ro"
        try:
            self._conn = sqlite3.connect(self._uri, uri=True, timeout=2, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._current_data_version = None
            self._consecutive_errors = 0
        except sqlite3.OperationalError:
            self._conn = None

    def _ensure_connection(self) -> sqlite3.Connection | None:
        if self._conn:
            return self._conn
        self._connect()
        return self._conn

    def _current_version(self) -> int | None:
        if not self._conn:
            return None
        try:
            cur = self._conn.execute("PRAGMA data_version")
            row = cur.fetchone()
            if row is None:
                return self._current_data_version
            version = int(row[0])
            if version == self._current_data_version:
                self._cache_hits += 1
                return self._current_data_version
            self._current_data_version = version
            return version
        except sqlite3.Error:
            return None

    def read_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_connection()
            if conn is None:
                return self._cached_sessions
            version = self._current_version()
            if (
                version is not None
                and self._cached_sessions_version == version
                and self._cached_sessions
            ):
                return self._cached_sessions
            try:
                self._cached_sessions = self._read_all_sessions(conn)
                if version is not None:
                    self._cached_sessions_version = version
                self._consecutive_errors = 0
            except sqlite3.Error:
                self._consecutive_errors += 1
                if self._consecutive_errors >= 3:
                    self._connect()
            return self._cached_sessions

    def _read_all_sessions(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        cur = conn.execute(
            "SELECT id, source, user_id, model, parent_session_id, started_at, ended_at, "
            "end_reason, message_count, tool_call_count, "
            "input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, reasoning_tokens, "
            "estimated_cost_usd, actual_cost_usd, "
            "billing_provider, cost_status, pricing_version, title "
            "FROM sessions ORDER BY started_at DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    def read_session_count(self) -> int:
        with self._lock:
            conn = self._ensure_connection()
            if conn is None:
                return self._cached_session_count
            version = self._current_version()
            if version is not None and self._cached_session_count_version == version:
                return self._cached_session_count
            try:
                cur = conn.execute("SELECT COUNT(*) FROM sessions")
                row = cur.fetchone()
                self._cached_session_count = int(row[0]) if row is not None else 0
                if version is not None:
                    self._cached_session_count_version = version
                self._consecutive_errors = 0
            except sqlite3.Error:
                self._consecutive_errors += 1
                if self._consecutive_errors >= 3:
                    self._connect()
            return self._cached_session_count

    def read_tool_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_connection()
            if conn is None:
                return self._cached_tool_stats
            version = self._current_version()
            if (
                version is not None
                and self._cached_tool_stats_version == version
                and self._cached_tool_stats
            ):
                return self._cached_tool_stats
            try:
                cur = conn.execute(
                    "SELECT tool_name, COUNT(*) as call_count "
                    "FROM messages WHERE tool_name IS NOT NULL "
                    "GROUP BY tool_name ORDER BY call_count DESC"
                )
                self._cached_tool_stats = [dict(row) for row in cur.fetchall()]
                if version is not None:
                    self._cached_tool_stats_version = version
                self._consecutive_errors = 0
            except sqlite3.Error:
                self._consecutive_errors += 1
                if self._consecutive_errors >= 3:
                    self._connect()
            return self._cached_tool_stats

    def search_session_ids_by_message(self, query: str) -> set[str]:
        normalized = query.strip()
        if not normalized:
            return set()
        with self._lock:
            conn = self._ensure_connection()
            if conn is None:
                if self._cached_message_search_query == normalized:
                    return self._cached_message_search_results
                return set()
            version = self._current_version()
            if (
                version is not None
                and self._cached_message_search_version == version
                and self._cached_message_search_query == normalized
            ):
                return self._cached_message_search_results
            try:
                if self._messages_fts_enabled(conn):
                    try:
                        session_ids = self._search_session_ids_by_fts(conn, normalized)
                    except sqlite3.Error:
                        session_ids = self._search_session_ids_by_like(conn, normalized)
                    else:
                        if not session_ids:
                            session_ids = self._search_session_ids_by_like(conn, normalized)
                else:
                    session_ids = self._search_session_ids_by_like(conn, normalized)
                self._cached_message_search_query = normalized
                self._cached_message_search_results = session_ids
                if version is not None:
                    self._cached_message_search_version = version
                self._consecutive_errors = 0
            except sqlite3.Error:
                self._consecutive_errors += 1
                if self._consecutive_errors >= 3:
                    self._connect()
            return self._cached_message_search_results

    def _messages_fts_enabled(self, conn: sqlite3.Connection) -> bool:
        if self._messages_fts_available is not None:
            return self._messages_fts_available
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
            )
            self._messages_fts_available = cur.fetchone() is not None
        except sqlite3.Error:
            self._messages_fts_available = False
        return self._messages_fts_available

    def _search_session_ids_by_fts(self, conn: sqlite3.Connection, query: str) -> set[str]:
        if self._messages_fts_supports_session_id is None:
            cur = conn.execute("PRAGMA table_info(messages_fts)")
            columns = {str(row[1]) for row in cur.fetchall()}
            self._messages_fts_supports_session_id = "session_id" in columns
        if self._messages_fts_supports_session_id:
            cur = conn.execute(
                "SELECT DISTINCT session_id FROM messages_fts WHERE messages_fts MATCH ?",
                (query,),
            )
        else:
            cur = conn.execute(
                "SELECT DISTINCT messages.session_id "
                "FROM messages_fts "
                "JOIN messages ON messages.id = messages_fts.rowid "
                "WHERE messages_fts MATCH ?",
                (query,),
            )
        return {str(row[0]) for row in cur.fetchall() if row[0]}

    def _search_session_ids_by_like(self, conn: sqlite3.Connection, query: str) -> set[str]:
        pattern = f"%{query.lower()}%"
        cur = conn.execute(
            "SELECT DISTINCT session_id "
            "FROM messages "
            "WHERE LOWER(COALESCE(content, '')) LIKE ? "
            "OR LOWER(COALESCE(tool_name, '')) LIKE ?",
            (pattern, pattern),
        )
        return {str(row[0]) for row in cur.fetchall() if row[0]}

    def close(self) -> None:
        with self._lock:
            if self._conn:
                with contextlib.suppress(sqlite3.ProgrammingError):
                    self._conn.close()
                self._conn = None
