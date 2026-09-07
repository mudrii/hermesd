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
# Caps the LIKE fallback result set. The scan itself is unbounded on a miss;
# this bounds what a pathological match can hand back to the UI.
_LIKE_SEARCH_LIMIT = 500


class HermesDB:
    def __init__(self, db_path: Path, allowed_root: Path | None = None):
        self._path = db_path
        # When set, _open_target re-validates on every (re)connect that the db
        # path is not a symlink and still resolves under this root, closing the
        # profile symlink TOCTOU window left by startup-only validation.
        self._allowed_root = allowed_root
        # Guards the SQLite connection lifecycle and serializes reads against
        # the cached last-good result/version state below.
        self._lock = threading.RLock()
        # Guards only the connection reference so interrupt() can snapshot it
        # without waiting for a long query holding _lock.
        self._connection_ref_lock = threading.Lock()
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
        self._uri = ""
        self._consecutive_errors = 0
        self._connect_backoff_reads = 0
        self._connected_mtime_ns: int | None = None
        self._messages_fts_supports_session_id: bool | None = None
        self._messages_fts_available: bool | None = None
        self._session_column_names: set[str] | None = None
        self._message_column_names: set[str] | None = None
        self._snapshot_dir: tempfile.TemporaryDirectory[str] | None = None
        self._closed = False
        self._connect()

    def _connect(self) -> None:
        if self._closed:
            return
        self._close_connection()
        if not self._path.exists():
            self._connected_mtime_ns = None
            self._consecutive_errors = 0
            self._mark_cached_reads_stale()
            return
        try:
            db_path, uri_params = self._open_target()
            self._uri = f"{db_path.resolve().as_uri()}?{uri_params}"
            conn = sqlite3.connect(self._uri, uri=True, timeout=2, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            with self._connection_ref_lock:
                self._conn = conn
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
            self._session_column_names = None
            self._message_column_names = None
        except (OSError, sqlite3.OperationalError):
            self._close_connection()
            self._connected_mtime_ns = None
            self._consecutive_errors = 0
            self._connect_backoff_reads = _CONNECT_BACKOFF_READS
            self._mark_cached_reads_stale()

    def _close_connection(self) -> None:
        with self._connection_ref_lock:
            conn = self._conn
            self._conn = None
        if conn:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        if self._snapshot_dir is not None:
            self._snapshot_dir.cleanup()
            self._snapshot_dir = None

    def _open_target(self) -> tuple[Path, str]:
        if self._allowed_root is not None and not _safe_sidecar_path(
            self._path, self._allowed_root
        ):
            raise OSError(f"Refusing to open database outside allowed root: {self._path}")
        if not self._path.with_name(f"{self._path.name}-wal").exists():
            return self._path, "mode=ro&immutable=1"
        return self._snapshot_wal_database(), "mode=ro"

    def _snapshot_wal_database(self) -> Path:
        snapshot_dir, snapshot_db = snapshot_wal_database(self._path, prefix="hermesd-state-")
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
        return current_mtime is None or current_mtime != self._connected_mtime_ns

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
        if self._closed:
            return None
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

    # The last_read_*_stale properties below are advisory flags (the UI only
    # uses them to annotate possibly-stale data). They are read outside the
    # collector's critical sections, so snapshot them under the lock to keep
    # cross-thread reads formally synchronized rather than relying on CPython
    # attribute-load atomicity.
    @property
    def last_read_sessions_stale(self) -> bool:
        with self._lock:
            return self._last_read_sessions_stale

    @property
    def last_read_session_count_stale(self) -> bool:
        with self._lock:
            return self._last_read_session_count_stale

    @property
    def last_read_tool_stats_stale(self) -> bool:
        with self._lock:
            return self._last_read_tool_stats_stale

    @property
    def last_message_search_stale(self) -> bool:
        with self._lock:
            return self._last_message_search_stale

    def _read_all_sessions(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        columns = ", ".join(self._session_columns(conn))
        # last_activity_at only exists on newer hermes-agent schemas; older
        # databases must keep the original started_at ordering.
        order_by = (
            "COALESCE(last_activity_at, started_at)"
            if "last_activity_at" in self._session_column_set(conn)
            else "started_at"
        )
        where = self._hidden_session_filter(conn)
        cur = conn.execute(f"SELECT {columns} FROM sessions{where} ORDER BY {order_by} DESC")
        return [dict(row) for row in cur.fetchall()]

    def _hidden_session_filter(self, conn: sqlite3.Connection) -> str:
        """Return the WHERE clause hiding soft-deleted sessions, or '' on old schemas."""
        if "hidden" not in self._session_column_set(conn):
            return ""
        return " WHERE COALESCE(hidden, 0) = 0"

    def _session_column_set(self, conn: sqlite3.Connection) -> set[str]:
        if self._session_column_names is None:
            cur = conn.execute("PRAGMA table_info(sessions)")
            self._session_column_names = {str(row["name"]) for row in cur.fetchall()}
        return self._session_column_names

    def _session_columns(self, conn: sqlite3.Connection) -> list[str]:
        wanted_columns = [
            "id",
            "source",
            "model",
            "parent_session_id",
            "started_at",
            "ended_at",
            "message_count",
            "tool_call_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "billing_provider",
            "billing_base_url",
            "billing_mode",
            "end_reason",
            "estimated_cost_usd",
            "cost_status",
            "pricing_version",
            "title",
            "api_call_count",
            "cwd",
            "rewind_count",
            "archived",
            "handoff_state",
            "handoff_platform",
            "handoff_error",
        ]
        available = self._session_column_set(conn)
        return [column for column in wanted_columns if column in available]

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
        # Must agree with read_sessions: hidden rows are not shown, so not counted.
        cur = conn.execute(f"SELECT COUNT(*) FROM sessions{self._hidden_session_filter(conn)}")
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
        # messages.active exists only on newer hermes-agent schemas; 0 marks a
        # message compacted out of the live transcript, which must not be counted.
        active_filter = (
            " AND COALESCE(active, 1) = 1" if self._filters_inactive_messages(conn) else ""
        )
        cur = conn.execute(
            "SELECT tool_name, COUNT(*) as call_count "
            f"FROM messages WHERE tool_name IS NOT NULL{active_filter} "
            "GROUP BY tool_name ORDER BY call_count DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    def _message_columns(self, conn: sqlite3.Connection) -> set[str]:
        if self._message_column_names is None:
            cur = conn.execute("PRAGMA table_info(messages)")
            self._message_column_names = {str(row["name"]) for row in cur.fetchall()}
        return self._message_column_names

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
                        # FTS matches whole tokens, so a mid-token substring
                        # legitimately misses; LIKE is what finds it.
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
                if self._cached_message_search_query != normalized:
                    return set()
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
        active_only = self._filters_inactive_messages(conn)
        if self._messages_fts_supports_session_id and not active_only:
            sql = "SELECT DISTINCT session_id FROM messages_fts WHERE messages_fts MATCH ?"
        else:
            # Joining messages on rowid is required to read session_id when the
            # FTS table lacks it, and to apply the active filter when it has it.
            active_filter = " AND COALESCE(messages.active, 1) = 1" if active_only else ""
            sql = (
                "SELECT DISTINCT messages.session_id "
                "FROM messages_fts "
                "JOIN messages ON messages.id = messages_fts.rowid "
                f"WHERE messages_fts MATCH ?{active_filter}"
            )
        cur = conn.execute(sql, (fts_query,))
        return {str(row[0]) for row in cur.fetchall() if row[0]}

    def _filters_inactive_messages(self, conn: sqlite3.Connection) -> bool:
        """True when messages.active exists, marking rows compacted out of the transcript."""
        return "active" in self._message_columns(conn)

    def _search_session_ids_by_like(self, conn: sqlite3.Connection, query: str) -> set[str]:
        pattern = f"%{_escape_like_pattern(query.lower())}%"
        # The OR pair must stay parenthesised: AND binds tighter, so an unbracketed
        # active filter would only constrain the tool_name branch.
        active_filter = (
            " AND COALESCE(active, 1) = 1" if self._filters_inactive_messages(conn) else ""
        )
        cur = conn.execute(
            "SELECT DISTINCT session_id "
            "FROM messages "
            "WHERE (LOWER(COALESCE(content, '')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(tool_name, '')) LIKE ? ESCAPE '\\')"
            f"{active_filter} "
            "LIMIT ?",
            (pattern, pattern, _LIKE_SEARCH_LIMIT),
        )
        return {str(row[0]) for row in cur.fetchall() if row[0]}

    def interrupt(self) -> None:
        """Abort any in-flight query on the connection (safe to call cross-thread)."""
        with self._connection_ref_lock:
            conn = self._conn
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.interrupt()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_connection()


def _escape_like_pattern(query: str) -> str:
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def snapshot_wal_database(
    db_path: Path, *, prefix: str
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    """Copy a WAL-mode database and its sidecars into a fresh temp dir.

    Returns the TemporaryDirectory (caller owns cleanup) and the snapshot db
    path. Sidecars are copied only when they safely resolve under db_path's
    directory.
    """
    snapshot_dir = tempfile.TemporaryDirectory(prefix=prefix)
    snapshot_root = Path(snapshot_dir.name)
    snapshot_db = snapshot_root / db_path.name
    try:
        shutil.copy2(db_path, snapshot_db)
        for suffix in ("-wal", "-shm"):
            source = db_path.with_name(f"{db_path.name}{suffix}")
            if source.exists() and _safe_sidecar_path(source, db_path.parent):
                shutil.copy2(source, snapshot_root / source.name)
    except OSError:
        snapshot_dir.cleanup()
        raise
    return snapshot_dir, snapshot_db


def _safe_sidecar_path(path: Path, root: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)


def _quote_fts_query(query: str) -> str:
    escaped = query.replace('"', '""')
    return f'"{escaped}"'
