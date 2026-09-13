"""Hosted-room coordination, read from the ROOT-scoped ``shared-state.db``.

``gateway/hosted_rooms.py:398-414`` (``default_db_path``) resolves the
hosted-room database to ``<root>/shared-state.db`` **even for a profile
gateway**, and its docstring is explicit about why: hosted-room coordination is
the only thing that module owns, and pointing profile gateways at the master
``state.db`` makes every profile process a long-lived writer on the session
store — the multi-writer corruption vector upstream pinned with its own test
(``tests/gateway/test_hosted_rooms.py:1344-1364``). So this reader is
``shared_path("shared-state.db")`` and never ``profile_path``, and never
``state.db``.

That last point is not hypothetical: the live ``~/.hermes/state.db`` still
carries ``hosted_rooms``, ``hosted_room_events`` and every sibling table with
zero rows and no DDL in ``hermes_state_common.py``. They are legacy leftovers,
and reading them would be reading a dead table.

**Content-free by construction.** The columns this module must never select are
``hosted_room_links.grant``/``catalog_json``/``target_url`` (a target URL may
embed credentials), ``hosted_room_events.payload_json``/``actor_json``,
``hosted_room_revoked_grants.scope_key``, and anything at all from the
``hosted_room_policy_transcript*`` tables, which are conversation content. What
does leave the file: counts, ``room_id``, ``name``, timestamps, epochs,
revisions, ``event_bytes``, the length of ``members_json`` (never its contents),
and a histogram over the closed ``kind`` vocabulary.

Every list here is bounded, every count is a whole-table aggregate, and read
errors propagate so the ``hosted_rooms`` source falls back to its last-good
value instead of reporting a false zero.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import Any

from hermesd.collect.common import _age_seconds, _coerce_int, _optional_epoch
from hermesd.collect.sqlite_util import (
    _count_by,
    _count_rows,
    _query_rows,
    _scalar_epoch,
    _select_columns,
    _table_columns,
    _table_count,
    _table_count_or_zero,
    _table_exists,
)
from hermesd.models import (
    KNOWN_HOSTED_ROOM_EVENT_KINDS,
    HostedRoomState,
    HostedRoomSummary,
)

# Display cap on the per-room list. The counts beside it are whole-table
# aggregates, so capping the list never changes a number.
MAX_HOSTED_ROOM_LIST = 8
# Upstream's own ceilings on the two strings it stores in hosted_rooms
# (MAX_ROOM_NAME_CHARS, MAX_ACTOR_ID_CHARS — gateway/hosted_rooms.py:25-29).
MAX_HOSTED_ROOM_TEXT_CHARS = 200
# MAX_MEMBERS_JSON_BYTES (gateway/hosted_rooms.py:31): past this the column is
# not parsed at all, so a runaway blob costs a length check and nothing more.
_MEMBERS_JSON_MAX_BYTES = 128 * 1024

# Selected by name, in this order, and only when PRAGMA says the column exists:
# authority_epoch and event_bytes were added by _LEGACY_COLUMN_DDL
# (gateway/hosted_rooms.py:340-348), so a draft-build database lacks them.
_ROOM_COLUMNS = (
    "room_id",
    "name",
    "members_json",
    "authority_epoch",
    "next_seq",
    "event_bytes",
    "revision",
    "created_at",
    "updated_at",
    "disbanded_at",
)


def _read_hosted_rooms(
    conn: sqlite3.Connection, *, now: float, db_size_bytes: int
) -> HostedRoomState:
    """Every hosted-room number hermesd is allowed to know, from one open."""
    return HostedRoomState(
        db_present=True,
        db_size_bytes=db_size_bytes,
        **_room_fields(conn, now=now),
        **_event_fields(conn, now=now),
        **_side_table_fields(conn, now=now),
        **_peer_reservation_fields(conn, now=now),
    )


def _room_fields(conn: sqlite3.Connection, *, now: float) -> dict[str, Any]:
    if not _table_exists(conn, "hosted_rooms"):
        return {}
    columns = _table_columns(conn, "hosted_rooms")
    selected = _select_columns(columns, _ROOM_COLUMNS)
    if "room_id" not in selected:
        # A hosted_rooms table without its primary key is not a table hermesd
        # can summarise. Raise rather than report an empty room list beside a
        # non-zero count: the source keeps its last-good value instead.
        raise RuntimeError("hosted_rooms has no room_id column")
    rows = _query_rows(
        conn,
        f"SELECT {', '.join(selected)} FROM hosted_rooms "
        "ORDER BY COALESCE(updated_at, 0) DESC, room_id ASC LIMIT ?",
        (MAX_HOSTED_ROOM_LIST + 1,),
    )
    return {
        # Upstream's active-room predicate is `disbanded_at IS NULL`
        # (gateway/hosted_rooms.py:867). Spelled as a COALESCE comparison so a
        # stored 0 cannot count as a tombstone here while reading as
        # not-disbanded in HostedRoomSummary.disbanded.
        "active_room_count": _count_rows(
            conn, "SELECT COUNT(*) FROM hosted_rooms WHERE COALESCE(disbanded_at, 0) <= 0"
        ),
        "disbanded_room_count": _count_rows(
            conn, "SELECT COUNT(*) FROM hosted_rooms WHERE COALESCE(disbanded_at, 0) > 0"
        ),
        "accounted_event_bytes": (
            _count_rows(conn, "SELECT COALESCE(SUM(event_bytes), 0) FROM hosted_rooms")
            if "event_bytes" in columns
            else 0
        ),
        "rooms": [_room_summary(row, now=now) for row in rows[:MAX_HOSTED_ROOM_LIST]],
        "rooms_truncated": len(rows) > MAX_HOSTED_ROOM_LIST,
    }


def _room_summary(row: dict[str, Any], *, now: float) -> HostedRoomSummary:
    return HostedRoomSummary(
        room_id=_capped_text(row.get("room_id")),
        name=_capped_text(row.get("name")),
        member_count=_member_count(row.get("members_json")),
        authority_epoch=_coerce_int(row.get("authority_epoch")),
        next_seq=_coerce_int(row.get("next_seq")),
        event_bytes=_coerce_int(row.get("event_bytes")),
        revision=_coerce_int(row.get("revision")),
        created_at_age_seconds=_age_seconds(_optional_epoch(row.get("created_at")), now),
        updated_at_age_seconds=_age_seconds(_optional_epoch(row.get("updated_at")), now),
        disbanded_at_age_seconds=_age_seconds(_optional_epoch(row.get("disbanded_at")), now),
    )


def _event_fields(conn: sqlite3.Connection, *, now: float) -> dict[str, Any]:
    if not _table_exists(conn, "hosted_room_events"):
        return {}
    placeholders = ", ".join("?" * len(KNOWN_HOSTED_ROOM_EVENT_KINDS))
    return {
        "event_count": _table_count(conn, "hosted_room_events"),
        # Bounded to the closed vocabulary by the WHERE clause, so the dict can
        # hold at most 17 entries and no unrecognized string is ever selected.
        # The complement is derived on the model as unknown_event_kind_count.
        "event_kind_counts": _count_by(
            conn,
            "SELECT kind, COUNT(*) FROM hosted_room_events "
            f"WHERE kind IN ({placeholders}) GROUP BY kind",
            KNOWN_HOSTED_ROOM_EVENT_KINDS,
        ),
        "newest_event_age_seconds": _age_seconds(
            _scalar_epoch(conn, "SELECT MAX(created_at) FROM hosted_room_events"), now
        ),
    }


def _side_table_fields(conn: sqlite3.Connection, *, now: float) -> dict[str, Any]:
    """Counts for the four tables whose every non-key column is off-limits.

    ``hosted_room_links`` is the sharpest case: target_url may embed a
    credential, ``grant`` is one, and ``catalog_json`` is a tool listing. Only
    the row count is read. ``hosted_room_revoked_grants`` is counted for the
    same reason — its primary key *is* the scope key.
    """
    fields: dict[str, Any] = {
        "retired_id_count": _table_count_or_zero(conn, "hosted_room_retired_ids"),
        "link_count": _table_count_or_zero(conn, "hosted_room_links"),
        "remote_run_count": _table_count_or_zero(conn, "hosted_room_remote_runs"),
        "revoked_grant_count": _table_count_or_zero(conn, "hosted_room_revoked_grants"),
    }
    if _table_exists(conn, "hosted_room_retired_ids"):
        fields["newest_retired_id_age_seconds"] = _age_seconds(
            _scalar_epoch(conn, "SELECT MAX(retired_at) FROM hosted_room_retired_ids"), now
        )
    if _table_exists(conn, "hosted_room_remote_runs"):
        fields["newest_remote_run_age_seconds"] = _age_seconds(
            _scalar_epoch(conn, "SELECT MAX(updated_at) FROM hosted_room_remote_runs"), now
        )
    return fields


def _peer_reservation_fields(conn: sqlite3.Connection, *, now: float) -> dict[str, Any]:
    """Live vs expired vs revoked peer reservations.

    ``expires_at``/``revoked_at`` are base-DDL columns with no upstream
    migration, so they are not gated on PRAGMA: a database missing one is not
    degraded to a false zero, it fails the source to its last-good value.
    Revoked wins over expired, so the three buckets partition the table and
    ``HostedRoomState.peer_reservation_count`` can be derived from them.
    """
    if not _table_exists(conn, "hosted_room_peer_reservations"):
        return {}
    live = (
        "SELECT COUNT(*) FROM hosted_room_peer_reservations "
        "WHERE COALESCE(revoked_at, 0) <= 0 AND COALESCE(expires_at, 0) > ?"
    )
    expired = (
        "SELECT COUNT(*) FROM hosted_room_peer_reservations "
        "WHERE COALESCE(revoked_at, 0) <= 0 AND COALESCE(expires_at, 0) <= ?"
    )
    return {
        "live_peer_reservation_count": _count_rows(conn, live, (now,)),
        "expired_peer_reservation_count": _count_rows(conn, expired, (now,)),
        "revoked_peer_reservation_count": _count_rows(
            conn,
            "SELECT COUNT(*) FROM hosted_room_peer_reservations WHERE COALESCE(revoked_at, 0) > 0",
        ),
    }


def _member_count(raw: object) -> int:
    """The *length* of ``members_json``; its contents never leave this function.

    Refuses the column outright past upstream's own byte ceiling rather than
    parsing a runaway blob, and returns 0 for anything that is not a JSON
    list/object — an unparseable member list is reported as "no members
    counted", never as a guess.
    """
    if not isinstance(raw, str) or not raw:
        return 0
    if len(raw.encode("utf-8", errors="replace")) > _MEMBERS_JSON_MAX_BYTES:
        return 0
    with contextlib.suppress(json.JSONDecodeError, ValueError, RecursionError):
        decoded = json.loads(raw)
        if isinstance(decoded, list | dict):
            return len(decoded)
    return 0


def _capped_text(value: object) -> str:
    """Printable, length-capped text safe to hand to a panel.

    Control characters are stripped here, in the collector: a panel escapes
    markup but must not be the place an escape sequence is neutralised.
    """
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if char.isprintable())[:MAX_HOSTED_ROOM_TEXT_CHARS]
