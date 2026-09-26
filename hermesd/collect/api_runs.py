"""Retained API run reservations, read from ``runs_idempotency.db``.

``gateway/platforms/api_server_run_idempotency.py:67`` resolves the store as
``get_hermes_home()/"runs_idempotency.db"``, so it is PROFILE-scoped — the
opposite of the ROOT-scoped ``shared-state.db`` the hosted-room reader uses.

**This table is a replay window, not an activity ledger.** ``reserve`` and
``lookup`` each call ``_prune_stale_terminal_locked`` (``:168-186``), which
deletes an aged row *only once its stored run status is terminal*
(``TERMINAL_STATUSES``, ``:17``), and long room runs push ``retention_until``
out (``extend_retention``, ``:205-214``; ``api_server_runs.py:56-61,222-232``).
Zero rows therefore says nothing about whether the API was used.

**The file may not be the store in use.** When it cannot be opened, upstream
logs "Run idempotency storage is unavailable; falling back to process memory,
so replay will not survive a restart" and connects ``":memory:"``, setting
``_db_path = None`` so ``durable`` is False (``:63-84``). That capability is
advertised only over HTTP (``api_server.py:2276``), never written to disk, so
hermesd cannot detect the fallback: an absent or stale file is reported as
absent or stale and nothing stronger.

**Content-free by construction.** ``fingerprint``, ``idempotency_key`` and
``scope`` are never selected — the tenant scope appears only as
``COUNT(DISTINCT scope)``. ``status_json`` holds "fingerprints and public run
status … never request bodies or credentials" per the class docstring
(``:52-55``), and hermesd parses it no further than one allowlisted status word.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from hermesd.collect.common import (
    _age_seconds,
    _coerce_int,
    _json_object_capped,
    _optional_epoch,
    _printable_capped,
)
from hermesd.collect.sqlite_util import (
    _count_rows,
    _query_rows,
    _scalar_epoch,
    _select_columns,
    _table_columns,
    _table_count,
    _table_exists,
)
from hermesd.models import ApiRunReservation, ApiRunReservationsState, ApiRunStatus

# Display cap on the reservation list. Every count beside it is a whole-table
# aggregate, so capping the list never changes a number.
MAX_API_RUN_LIST = 8
# Upstream run ids are short opaque strings; a longer value in an untrusted file
# is truncated rather than rendered.
MAX_API_RUN_TEXT_CHARS = 128
# status_json carries a fingerprint and public run status, so it is small. A row
# over this cap is not parsed at all and its status reads as unknown.
_STATUS_JSON_MAX_BYTES = 16 * 1024

# Selected by name and only when present: owner_pid, owner_started,
# retention_until and acknowledged_at were all added by _MIGRATIONS
# (api_server_run_idempotency.py:29-34), so a first-schema database lacks them.
# scope, idempotency_key and fingerprint are deliberately absent from this list.
_RUN_COLUMNS = (
    "run_id",
    "status_json",
    "owner_pid",
    "owner_started",
    "retention_until",
    "acknowledged_at",
    "created_at",
    "updated_at",
)

_RECOGNIZED_STATUSES = frozenset(
    status.value for status in ApiRunStatus if status is not ApiRunStatus.UNKNOWN
)


def _read_api_runs(
    conn: sqlite3.Connection,
    *,
    now: float,
    db_size_bytes: int,
    pid_exists: Callable[[int], bool],
) -> ApiRunReservationsState:
    """Every retained-reservation number hermesd is allowed to know."""
    if not _table_exists(conn, "run_idempotency"):
        return ApiRunReservationsState(db_present=True, db_size_bytes=db_size_bytes)
    columns = _table_columns(conn, "run_idempotency")
    rows = _reservation_rows(conn, columns)
    return ApiRunReservationsState(
        db_present=True,
        db_size_bytes=db_size_bytes,
        reservation_count=_table_count(conn, "run_idempotency"),
        # The tenant scope is counted, never carried.
        scope_count=(
            _count_rows(conn, "SELECT COUNT(DISTINCT scope) FROM run_idempotency")
            if "scope" in columns
            else 0
        ),
        reservations=[
            _reservation(row, now=now, pid_exists=pid_exists) for row in rows[:MAX_API_RUN_LIST]
        ],
        reservations_truncated=len(rows) > MAX_API_RUN_LIST,
        acknowledged_count=_gated_count(
            conn,
            columns,
            "acknowledged_at",
            "SELECT COUNT(*) FROM run_idempotency WHERE COALESCE(acknowledged_at, 0) > 0",
        ),
        owner_recorded_count=_gated_count(
            conn,
            columns,
            "owner_pid",
            "SELECT COUNT(*) FROM run_idempotency WHERE COALESCE(owner_pid, 0) > 0",
        ),
        # Rows past retention_until can remain until a later request triggers
        # upstream's opportunistic pruning.
        retention_expired_count=_gated_count(
            conn,
            columns,
            "retention_until",
            "SELECT COUNT(*) FROM run_idempotency "
            "WHERE COALESCE(retention_until, 0) > 0 AND retention_until <= ?",
            (now,),
        ),
        newest_age_seconds=_age_seconds(
            _scalar_epoch(conn, "SELECT MAX(updated_at) FROM run_idempotency"), now
        ),
        oldest_age_seconds=_age_seconds(
            _scalar_epoch(conn, "SELECT MIN(created_at) FROM run_idempotency"), now
        ),
    )


def _reservation_rows(conn: sqlite3.Connection, columns: frozenset[str]) -> list[dict[str, Any]]:
    """The newest rows, one past the display cap so truncation is detectable."""
    selected = _select_columns(columns, _RUN_COLUMNS)
    if "run_id" not in selected:
        return []
    return _query_rows(
        conn,
        f"SELECT {', '.join(selected)} FROM run_idempotency "
        "ORDER BY COALESCE(updated_at, 0) DESC, run_id ASC LIMIT ?",
        (MAX_API_RUN_LIST + 1,),
    )


def _reservation(
    row: dict[str, Any],
    *,
    now: float,
    pid_exists: Callable[[int], bool],
) -> ApiRunReservation:
    owner_pid = _coerce_int(row.get("owner_pid"))
    return ApiRunReservation(
        run_id=_printable_capped(row.get("run_id"), MAX_API_RUN_TEXT_CHARS),
        status=_allowlisted_status(row.get("status_json")),
        created_at_age_seconds=_age_seconds(_optional_epoch(row.get("created_at")), now),
        updated_at_age_seconds=_age_seconds(_optional_epoch(row.get("updated_at")), now),
        retention_remaining_seconds=_retention_remaining(row.get("retention_until"), now),
        acknowledged=_optional_epoch(row.get("acknowledged_at")) is not None,
        owner_pid=owner_pid,
        # owner_started is /proc ticks on Linux and psutil centiseconds
        # elsewhere (gateway/status.get_process_start_time,
        # api_server_runs.py:81-87), so it is reduced to "an identity was
        # recorded" and never compared against a hermesd timestamp.
        owner_started_recorded=_coerce_int(row.get("owner_started")) > 0,
        owner_alive=bool(owner_pid) and pid_exists(owner_pid),
    )


def _allowlisted_status(raw: object) -> ApiRunStatus:
    """The one word taken out of ``status_json``.

    An unrecognized status is reported as ``UNKNOWN`` rather than dropped or
    guessed: the store outlives this enumeration, and silently hiding a row
    would understate what the gateway is holding.
    """
    data = _json_object_capped(raw, _STATUS_JSON_MAX_BYTES)
    if data is None:
        return ApiRunStatus.UNKNOWN
    status = data.get("status")
    if isinstance(status, str) and status in _RECOGNIZED_STATUSES:
        return ApiRunStatus(status)
    return ApiRunStatus.UNKNOWN


def _retention_remaining(raw: object, now: float) -> float | None:
    """Seconds until upstream may prune this row, negative when already past.

    None means the row carries no explicit deadline, so upstream falls back to
    ``updated_at + RETENTION_SECONDS``.
    """
    deadline = _optional_epoch(raw)
    if deadline is None:
        return None
    return deadline - now


def _gated_count(
    conn: sqlite3.Connection,
    columns: frozenset[str],
    column: str,
    sql: str,
    params: tuple[Any, ...] = (),
) -> int:
    """A count over a column added by migration, 0 when the column is absent."""
    if column not in columns:
        return 0
    return _count_rows(conn, sql, params)
