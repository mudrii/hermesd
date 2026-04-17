from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any


class HermesDB:
    def __init__(self, db_path: Path):
        self._path = db_path
        self._conn: sqlite3.Connection | None = None
        self._last_data_version: int | None = None
        self._cached_sessions: list[dict[str, Any]] = []
        self._cached_tool_stats: list[dict[str, Any]] = []
        self._cached_token_totals: dict[str, Any] = {}
        self._cache_hits = 0
        self._uri = ""
        self._consecutive_errors = 0
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
            self._last_data_version = None
            self._consecutive_errors = 0
        except sqlite3.OperationalError:
            self._conn = None

    def _ensure_connection(self) -> sqlite3.Connection | None:
        if self._conn:
            return self._conn
        self._connect()
        return self._conn

    def _data_version_changed(self) -> bool:
        if not self._conn:
            return False
        try:
            cur = self._conn.execute("PRAGMA data_version")
            version = cur.fetchone()[0]
            if version == self._last_data_version:
                self._cache_hits += 1
                return False
            self._last_data_version = version
            return True
        except sqlite3.Error:
            return True

    def read_sessions(self) -> list[dict[str, Any]]:
        conn = self._ensure_connection()
        if conn is None:
            return self._cached_sessions
        if not self._data_version_changed() and self._cached_sessions:
            return self._cached_sessions
        try:
            cur = conn.execute(
                "SELECT id, source, user_id, model, parent_session_id, started_at, ended_at, "
                "end_reason, message_count, tool_call_count, "
                "input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, reasoning_tokens, "
                "estimated_cost_usd, actual_cost_usd, "
                "billing_provider, cost_status, pricing_version, title "
                "FROM sessions ORDER BY started_at DESC"
            )
            self._cached_sessions = [dict(row) for row in cur.fetchall()]
            self._consecutive_errors = 0
        except sqlite3.Error:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                self._connect()
        return self._cached_sessions

    def read_tool_stats(self) -> list[dict[str, Any]]:
        conn = self._ensure_connection()
        if conn is None:
            return self._cached_tool_stats
        if not self._data_version_changed() and self._cached_tool_stats:
            return self._cached_tool_stats
        try:
            cur = conn.execute(
                "SELECT tool_name, COUNT(*) as call_count "
                "FROM messages WHERE tool_name IS NOT NULL "
                "GROUP BY tool_name ORDER BY call_count DESC"
            )
            self._cached_tool_stats = [dict(row) for row in cur.fetchall()]
            self._consecutive_errors = 0
        except sqlite3.Error:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                self._connect()
        return self._cached_tool_stats

    def read_token_totals(self) -> dict[str, Any]:
        empty = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_cost_usd": 0.0,
        }
        conn = self._ensure_connection()
        if conn is None:
            return self._cached_token_totals or empty
        if not self._data_version_changed() and self._cached_token_totals:
            return self._cached_token_totals
        try:
            cur = conn.execute(
                "SELECT "
                "COALESCE(SUM(input_tokens), 0) as input_tokens, "
                "COALESCE(SUM(output_tokens), 0) as output_tokens, "
                "COALESCE(SUM(cache_read_tokens), 0) as cache_read_tokens, "
                "COALESCE(SUM(cache_write_tokens), 0) as cache_write_tokens, "
                "COALESCE(SUM(reasoning_tokens), 0) as reasoning_tokens, "
                "COALESCE(SUM(estimated_cost_usd), 0.0) as total_cost_usd "
                "FROM sessions"
            )
            self._cached_token_totals = dict(cur.fetchone())
            self._consecutive_errors = 0
        except sqlite3.Error:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                self._connect()
        return self._cached_token_totals or empty

    def close(self) -> None:
        if self._conn:
            with contextlib.suppress(sqlite3.ProgrammingError):
                self._conn.close()
            self._conn = None
