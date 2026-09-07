"""Read-only SQLite helpers shared by the kanban and operations readers."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from hermesd.db import snapshot_wal_database


@contextlib.contextmanager
def _connect_readonly_sqlite(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn: sqlite3.Connection | None = None
    if db_path.with_name(f"{db_path.name}-wal").exists():
        snapshot_dir, snapshot_db = snapshot_wal_database(db_path, prefix="hermesd-kanban-")
        try:
            conn = sqlite3.connect(f"{snapshot_db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
            yield conn
            return
        finally:
            if conn is not None:
                conn.close()
            snapshot_dir.cleanup()
    conn = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=2,
    )
    try:
        yield conn
    finally:
        conn.close()


def _query_rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    cur = conn.execute(sql)
    return [dict(row) for row in cur.fetchall()]


def _table_count(conn: sqlite3.Connection, table_name: str) -> int:
    cur = conn.execute(f"SELECT COUNT(*) FROM {table_name}")
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def _table_count_or_zero(conn: sqlite3.Connection, table_name: str) -> int:
    with contextlib.suppress(sqlite3.Error):
        return _table_count(conn, table_name)
    return 0


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    )
    return cur.fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    with contextlib.suppress(sqlite3.Error):
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(str(row[1] or "") == column_name for row in rows)
    return False


def _count_rows_or_zero(conn: sqlite3.Connection, sql: str) -> int:
    with contextlib.suppress(sqlite3.Error):
        cur = conn.execute(sql)
        row = cur.fetchone()
        return int(row[0] or 0) if row is not None else 0
    return 0


def _count_by(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    cur = conn.execute(sql)
    return {str(row[0] or "unknown"): int(row[1] or 0) for row in cur.fetchall()}
