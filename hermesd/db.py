from __future__ import annotations

import contextlib
import shutil
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")
_RECONNECT_ERROR_THRESHOLD = 3
_CONNECT_BACKOFF_READS = 2


class HermesDB:
    def __init__(self, db_path: Path):
        self._path = db_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._current_data_version: int | None = None
        self._cached_sessions: list[dict[str, Any]] = []
        self._cached_sessions_version: int | None = None
        self._cached_sessions_initialized = False
        self._cached_session_count = 0
        self._cached_session_count_version: int | None = None
        self._cached_session_count_initialized = False
        self._cached_tool_stats: list[dict[str, Any]] = []
        self._cached_tool_stats_version: int | None = None
        self._cached_tool_stats_initialized = False
        self._last_read_sessions_stale = False
        self._last_read_session_count_stale = False
        self._last_read_tool_stats_stale = False
        self._cached_message_search_query: str = ""
        self._cached_message_search_results: set[str] = set()
        self._cached_message_search_version: int | None = None
        self._cached_message_search_initialized = False
        self._last_message_search_stale = False
        self._cache_hits = 0
        self._uri = ""
        self._consecutive_errors = 0
        self._connect_backoff_reads = 0
        self._connected_mtime_ns: int | None = None
        self._messages_fts_supports_session_id: bool | None = None
        self._messages_fts_available: bool | None = None
        self._snapshot_dir: tempfile.TemporaryDirectory[str] | None = None
        self._connect()

    def _connect(self) -> None:
        self._close_connection()
        if not self._path.exists():
            self._connected_mtime_ns = None
            return
        try:
            db_path, uri_params = self._open_target()
            self._uri = f"{db_path.resolve().as_uri()}?{uri_params}"
            self._conn = sqlite3.connect(self._uri, uri=True, timeout=2, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._current_data_version = None
            self._cached_sessions_version = None
            self._cached_session_count_version = None
            self._cached_tool_stats_version = None
            self._cached_message_search_version = None
            self._consecutive_errors = 0
            self._connect_backoff_reads = 0
            self._connected_mtime_ns = self._source_mtime_ns()
            self._messages_fts_supports_session_id = None
            self._messages_fts_available = None
        except (OSError, sqlite3.OperationalError):
            self._close_connection()
            self._connected_mtime_ns = None
            self._connect_backoff_reads = _CONNECT_BACKOFF_READS
            self._mark_cached_reads_stale()

    def _close_connection(self) -> None:
        if self._conn:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None
        if self._snapshot_dir is not None:
            self._snapshot_dir.cleanup()
            self._snapshot_dir = None

    def _open_target(self) -> tuple[Path, str]:
        if not self._path.with_name(f"{self._path.name}-wal").exists():
            return self._path, "mode=ro&immutable=1"
        return self._snapshot_wal_database(), "mode=ro"

    def _snapshot_wal_database(self) -> Path:
        snapshot_dir = tempfile.TemporaryDirectory(prefix="hermesd-state-")
        snapshot_root = Path(snapshot_dir.name)
        snapshot_db = snapshot_root / self._path.name
        try:
            shutil.copy2(self._path, snapshot_db)
            for suffix in ("-wal", "-shm"):
                source = self._path.with_name(f"{self._path.name}{suffix}")
                if source.exists():
                    shutil.copy2(source, snapshot_root / source.name)
        except OSError:
            snapshot_dir.cleanup()
            raise
        self._snapshot_dir = snapshot_dir
        return snapshot_db

    def _source_mtime_ns(self) -> int | None:
        mtimes = []
        for path in (self._path, self._path.with_name(f"{self._path.name}-wal")):
            try:
                mtimes.append(path.stat().st_mtime_ns)
            except OSError:
                continue
        return max(mtimes) if mtimes else None

    def _source_changed(self) -> bool:
        current_mtime = self._source_mtime_ns()
        return current_mtime is not None and current_mtime != self._connected_mtime_ns

    def _mark_cached_reads_stale(self) -> None:
        if self._cached_sessions_initialized:
            self._last_read_sessions_stale = True
        if self._cached_session_count_initialized:
            self._last_read_session_count_stale = True
        if self._cached_tool_stats_initialized:
            self._last_read_tool_stats_stale = True
        if self._cached_message_search_initialized:
            self._last_message_search_stale = True

    def _ensure_connection(self) -> sqlite3.Connection | None:
        if self._conn and self._source_changed():
            self._connect()
        if self._conn:
            return self._conn
        if self._connect_backoff_reads > 0:
            self._connect_backoff_reads -= 1
            return None
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
            self._cached_sessions = self._read_cached(
                cached_value=self._cached_sessions,
                get_cached_version=lambda: self._cached_sessions_version,
                set_cached_version=lambda version: setattr(
                    self,
                    "_cached_sessions_version",
                    version,
                ),
                set_stale=lambda stale: setattr(self, "_last_read_sessions_stale", stale),
                mark_initialized=lambda: setattr(self, "_cached_sessions_initialized", True),
                reader=self._read_all_sessions,
            )
            return self._cached_sessions

    @property
    def last_read_sessions_stale(self) -> bool:
        return self._last_read_sessions_stale

    @property
    def last_read_session_count_stale(self) -> bool:
        return self._last_read_session_count_stale

    @property
    def last_read_tool_stats_stale(self) -> bool:
        return self._last_read_tool_stats_stale

    @property
    def last_message_search_stale(self) -> bool:
        return self._last_message_search_stale

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
            self._cached_session_count = self._read_cached(
                cached_value=self._cached_session_count,
                get_cached_version=lambda: self._cached_session_count_version,
                set_cached_version=lambda version: setattr(
                    self,
                    "_cached_session_count_version",
                    version,
                ),
                set_stale=lambda stale: setattr(self, "_last_read_session_count_stale", stale),
                mark_initialized=lambda: setattr(
                    self,
                    "_cached_session_count_initialized",
                    True,
                ),
                reader=self._read_session_count,
            )
            return self._cached_session_count

    def _read_session_count(self, conn: sqlite3.Connection) -> int:
        cur = conn.execute("SELECT COUNT(*) FROM sessions")
        row = cur.fetchone()
        return int(row[0]) if row is not None else 0

    def read_tool_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            self._cached_tool_stats = self._read_cached(
                cached_value=self._cached_tool_stats,
                get_cached_version=lambda: self._cached_tool_stats_version,
                set_cached_version=lambda version: setattr(
                    self,
                    "_cached_tool_stats_version",
                    version,
                ),
                set_stale=lambda stale: setattr(self, "_last_read_tool_stats_stale", stale),
                mark_initialized=lambda: setattr(self, "_cached_tool_stats_initialized", True),
                reader=self._read_tool_stats,
            )
            return self._cached_tool_stats

    def _read_tool_stats(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        cur = conn.execute(
            "SELECT tool_name, COUNT(*) as call_count "
            "FROM messages WHERE tool_name IS NOT NULL "
            "GROUP BY tool_name ORDER BY call_count DESC"
        )
        return [dict(row) for row in cur.fetchall()]

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
                self._last_message_search_stale = False
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
                self._cached_message_search_initialized = True
                if version is not None:
                    self._cached_message_search_version = version
                self._consecutive_errors = 0
                self._last_message_search_stale = False
            except sqlite3.Error:
                self._last_message_search_stale = self._cached_message_search_initialized
                self._record_read_error()
            return self._cached_message_search_results

    def _read_cached(
        self,
        *,
        cached_value: T,
        get_cached_version: Callable[[], int | None],
        set_cached_version: Callable[[int], None],
        set_stale: Callable[[bool], None],
        mark_initialized: Callable[[], None],
        reader: Callable[[sqlite3.Connection], T],
    ) -> T:
        conn = self._ensure_connection()
        if conn is None:
            return cached_value
        version = self._current_version()
        if version is not None and get_cached_version() == version:
            set_stale(False)
            return cached_value
        try:
            value = reader(conn)
        except sqlite3.Error:
            set_stale(True)
            self._record_read_error()
            return cached_value
        if version is not None:
            set_cached_version(version)
        self._consecutive_errors = 0
        set_stale(False)
        mark_initialized()
        return value

    def _record_read_error(self) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= _RECONNECT_ERROR_THRESHOLD:
            self._connect()
            if self._conn is None:
                self._consecutive_errors = 0

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
        fts_query = _quote_fts_query(query)
        if self._messages_fts_supports_session_id is None:
            cur = conn.execute("PRAGMA table_info(messages_fts)")
            columns = {str(row[1]) for row in cur.fetchall()}
            self._messages_fts_supports_session_id = "session_id" in columns
        if self._messages_fts_supports_session_id:
            cur = conn.execute(
                "SELECT DISTINCT session_id FROM messages_fts WHERE messages_fts MATCH ?",
                (fts_query,),
            )
        else:
            cur = conn.execute(
                "SELECT DISTINCT messages.session_id "
                "FROM messages_fts "
                "JOIN messages ON messages.id = messages_fts.rowid "
                "WHERE messages_fts MATCH ?",
                (fts_query,),
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
            self._close_connection()


def _quote_fts_query(query: str) -> str:
    escaped = query.replace('"', '""')
    return f'"{escaped}"'
