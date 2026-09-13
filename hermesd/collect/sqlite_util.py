"""Read-only SQLite helpers shared by the kanban and operations readers."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from hermesd.collect.common import _exists_strict, _optional_epoch
from hermesd.db import _SQLITE_TIMEOUT_SECONDS, snapshot_wal_database


def _snapshot_wal_if_present(
    db_path: Path,
) -> tuple[tempfile.TemporaryDirectory[str], Path] | None:
    """Copy db+sidecars into a temp dir when a WAL exists, else None.

    Separated from the connection so callers that read the same database
    several times in one refresh can share a single copy: kanban.db is read by
    the board, per-board and notify readers, and each used to snapshot the WAL
    on its own. The caller owns cleanup.
    """
    wal_path = db_path.with_name(f"{db_path.name}-wal")
    if wal_path.is_symlink():
        raise OSError(f"Refusing to open database with unsafe SQLite WAL sidecar: {wal_path}")
    # Strict, not Path.exists(): from Python 3.14 exists() also swallows EACCES, so
    # an unreadable sidecar would read as absent and this would silently fall
    # through to immutable=1 — serving checkpoint-lagging data as current.
    if not _exists_strict(wal_path):
        return None
    return snapshot_wal_database(db_path, prefix="hermesd-kanban-")


@contextlib.contextmanager
def _connect_resolved_sqlite(read_path: Path, *, immutable: bool) -> Iterator[sqlite3.Connection]:
    """Open a database path that is already resolved for reading.

    ``_snapshot_wal_if_present`` copies the ``-wal`` sidecar into the snapshot
    directory, so a snapshot still looks like a WAL database; running the
    snapshot check on it again would copy it a second time. Callers that hold
    the shared snapshot (one copy per refresh, several readers) open it here.
    """
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    conn = sqlite3.connect(
        f"{read_path.resolve().as_uri()}{suffix}",
        uri=True,
        timeout=_SQLITE_TIMEOUT_SECONDS,
    )
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _connect_readonly_sqlite(
    db_path: Path, *, resolved: bool = False
) -> Iterator[sqlite3.Connection]:
    """A read-only connection to ``db_path``, snapshotting its WAL when needed.

    ``resolved`` means the caller already holds a snapshot of the WAL database
    (``_snapshot_wal_if_present``) and several readers share it: the copy
    deliberately includes the ``-wal`` sidecar, so re-running the snapshot check
    on it would copy it again.
    """
    if resolved:
        with _connect_resolved_sqlite(db_path, immutable=False) as resolved_conn:
            yield resolved_conn
        return
    snapshot = _snapshot_wal_if_present(db_path)
    if snapshot is not None:
        snapshot_dir, snapshot_db = snapshot
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                f"{snapshot_db.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=_SQLITE_TIMEOUT_SECONDS,
            )
            yield conn
        finally:
            try:
                if conn is not None:
                    conn.close()
            finally:
                snapshot_dir.cleanup()
        return
    conn = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=_SQLITE_TIMEOUT_SECONDS,
    )
    try:
        yield conn
    finally:
        conn.close()


# Every table hermesd reads by name. `_table_count` and `_column_exists` splice
# the name straight into SQL because SQLite cannot bind an identifier, so an
# unlisted name is rejected instead of reaching the database.
_KNOWN_TABLES = frozenset(
    {
        "async_delegations",
        "compression_locks",
        "conversation_generations",
        "conversations",
        "cron_incidents",
        "delivery_obligations",
        "discovered_repos",
        "executions",
        "gateway_heartbeats",
        "gateway_hygiene_state",
        "gateway_routing",
        # shared-state.db (gateway/hosted_rooms.py:87-148). The
        # hosted_room_policy_* tables beside these are deliberately absent: they
        # hold conversation transcripts, which hermesd never reads.
        "hosted_room_events",
        "hosted_room_links",
        "hosted_room_peer_reservations",
        "hosted_room_remote_runs",
        "hosted_room_retired_ids",
        "hosted_room_revoked_grants",
        "hosted_rooms",
        "kanban_notify_subs",
        "project_folders",
        "projects",
        "responses",
        # runs_idempotency.db (api_server_run_idempotency.py:86-99).
        "run_idempotency",
        "session_turn_leases",
        "task_attachments",
        "task_comments",
        "task_events",
        "task_links",
        "task_runs",
        "tasks",
        "verification_events",
        "verification_state",
    }
)


def _checked_table(table_name: str) -> str:
    """Return table_name if it is a known hermes table, else raise."""
    if table_name not in _KNOWN_TABLES:
        raise ValueError(f"unknown table name: {table_name!r}")
    return table_name


def _query_rows(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]


def _table_count(conn: sqlite3.Connection, table_name: str) -> int:
    # SQLite cannot bind an identifier, so the name is interpolated. It is
    # validated against _KNOWN_TABLES first; every caller passes a literal.
    cur = conn.execute(f"SELECT COUNT(*) FROM {_checked_table(table_name)}")
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def _table_count_or_zero(conn: sqlite3.Connection, table_name: str) -> int:
    """Row count, or 0 when the table is absent; read errors propagate so the
    caller's source fails to its last-good value instead of reporting a false
    zero."""
    _checked_table(table_name)
    if not _table_exists(conn, table_name):
        return 0
    return _table_count(conn, table_name)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    )
    return cur.fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    """Column membership from PRAGMA table_info; PRAGMA failures propagate.

    An absent table legitimately yields no rows (False); a present-but-
    unreadable table raises so the caller's source fails to its last-good
    value instead of silently degrading column-aware queries to empty results.
    """
    # Same identifier-interpolation carve-out as _table_count.
    rows = conn.execute(f"PRAGMA table_info({_checked_table(table_name)})").fetchall()
    return any(str(row[1] or "") == column_name for row in rows)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> frozenset[str]:
    """Every column of a table, read once per connection by callers.

    Column *order* is not stable across databases — this branch already found
    the cron ``executions`` table with two different orders in two profiles — so
    a reader that has to tolerate a schema migration resolves the names it wants
    against this set and selects them explicitly, rather than reading
    positionally or with ``SELECT *``.
    """
    # Same identifier-interpolation carve-out as _table_count.
    rows = conn.execute(f"PRAGMA table_info({_checked_table(table_name)})").fetchall()
    return frozenset(str(row[1] or "") for row in rows if str(row[1] or ""))


def _select_columns(columns: frozenset[str], wanted: tuple[str, ...]) -> tuple[str, ...]:
    """The wanted columns that actually exist, in the caller's order.

    Keeps a reader that must tolerate both an added and a dropped column off
    ``SELECT *``, whose result order is whatever the file happens to hold.
    """
    return tuple(name for name in wanted if name in columns)


def _count_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    """One integer from a single-value query over a table the caller confirmed exists.

    Named for its common case (``COUNT(*)``) but deliberately not restricted to
    it: it reads any single-column, single-row scalar, which is why the session
    coordination reader uses it for ``SUM(...)`` as well. Read errors propagate
    so the source fails to its last-good value.
    """
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return int(row[0] or 0) if row is not None else 0


def _count_by(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, int]:
    cur = conn.execute(sql, params)
    return {str(row[0] or "unknown"): int(row[1] or 0) for row in cur.fetchall()}


def _scalar_epoch(conn: sqlite3.Connection, sql: str) -> float | None:
    """One persisted epoch from a single-column aggregate query.

    NULL, 0 and anything non-finite all read as None, so an empty table yields
    "no timestamp" rather than January 1970. Read errors propagate.
    """
    row = conn.execute(sql).fetchone()
    return _optional_epoch(row[0]) if row is not None else None
