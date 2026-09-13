"""Hosted-room coordination monitoring from the ROOT-scoped ``shared-state.db``.

Upstream keeps hosted-room coordination in its own database and deliberately
*not* in the master ``state.db``: ``gateway/hosted_rooms.py:398-414`` resolves
even a profile gateway to the shared ROOT ``shared-state.db``, because pointing
profile gateways at the session store makes every profile process a long-lived
writer on it. The live ``state.db`` nonetheless still carries empty legacy
``hosted_room*`` tables, so "which file did the reader open?" is a real question
here, and several tests below exist only to pin that answer.

The other half of the file is the content-free contract: grants, link catalogs
and target URLs, event payloads and actors, revoked-grant scope keys and the
``hosted_room_policy_transcript*`` conversation tables are never read into a
model, so a canary placed in each of them must not reach the JSON snapshot.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

import hermesd.collect.hosted_rooms as hosted_rooms_module
from hermesd.collector import Collector
from hermesd.models import HostedRoomEventKind, HostedRoomState, HostedRoomSummary

_NOW = 1_800_000_000.0
_DAY = 86_400.0

# Verbatim from gateway/hosted_rooms.py:87-148 (_SCHEMA_DDL), minus the
# hosted_room_policy_* tables hermesd must never open.
HOSTED_ROOM_DDL = """
CREATE TABLE hosted_rooms (
    room_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    members_json TEXT NOT NULL,
    authority_gateway_id TEXT NOT NULL,
    authority_epoch INTEGER NOT NULL DEFAULT 1 CHECK (authority_epoch >= 1),
    next_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
    event_bytes INTEGER NOT NULL DEFAULT 0 CHECK (event_bytes >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    disbanded_at REAL
);
CREATE TABLE hosted_room_events (
    room_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    authority_epoch INTEGER CHECK (authority_epoch IS NULL OR authority_epoch >= 1),
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (room_id, seq),
    UNIQUE (room_id, event_id)
);
CREATE TABLE hosted_room_retired_ids (
    room_id TEXT PRIMARY KEY,
    retired_at REAL NOT NULL
);
CREATE TABLE hosted_room_links (
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    target_url TEXT NOT NULL,
    target_profile TEXT NOT NULL,
    grant TEXT NOT NULL,
    catalog_json TEXT NOT NULL,
    cancellation_scope_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    transport_security TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    updated_at REAL NOT NULL,
    PRIMARY KEY (room_id, member_id)
);
CREATE TABLE hosted_room_remote_runs (
    room_id TEXT NOT NULL,
    home_install_id TEXT NOT NULL,
    authority_gateway_id TEXT NOT NULL,
    authority_epoch INTEGER NOT NULL CHECK (authority_epoch >= 1),
    member_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    execution_generation INTEGER NOT NULL CHECK (execution_generation >= 1),
    target_install_id TEXT NOT NULL,
    target_profile TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (
        room_id, home_install_id, authority_gateway_id, authority_epoch,
        member_id, target_install_id, target_profile, task_id,
        execution_generation
    )
);
CREATE TABLE hosted_room_revoked_grants (
    scope_key TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    revoked_before REAL NOT NULL
);
CREATE TABLE hosted_room_peer_reservations (
    room_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    target_profile TEXT NOT NULL,
    authority_gateway_id TEXT NOT NULL,
    authority_epoch INTEGER NOT NULL CHECK (authority_epoch >= 1),
    expires_at REAL NOT NULL,
    revoked_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (room_id, member_id, target_profile)
);
CREATE INDEX idx_hosted_room_events_cursor ON hosted_room_events(room_id, seq);
"""

# The conversation-content tables hermesd must never open. Present in the live
# shared-state.db; created here only so a canary can be planted in them.
HOSTED_ROOM_POLICY_DDL = """
CREATE TABLE hosted_room_policy_transcript (
    room_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (room_id, seq)
);
CREATE TABLE hosted_room_policy_transcript_state (
    room_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (room_id)
);
"""


def create_shared_state_db(
    conn: sqlite3.Connection,
    *,
    with_event_bytes: bool = True,
    with_policy_tables: bool = False,
    journal_mode: str = "DELETE",
) -> None:
    """Create the hosted-room schema.

    ``journal_mode=DELETE`` keeps the read on the ``immutable=1`` branch of
    ``_connect_readonly_sqlite``; pass ``WAL`` to exercise the snapshot branch.
    """
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    ddl = HOSTED_ROOM_DDL
    if not with_event_bytes:
        # Draft builds predate the event_bytes migration
        # (_LEGACY_COLUMN_DDL, gateway/hosted_rooms.py:340-348).
        ddl = ddl.replace(
            "    event_bytes INTEGER NOT NULL DEFAULT 0 CHECK (event_bytes >= 0),\n", ""
        )
        assert "event_bytes" not in ddl
    if with_policy_tables:
        ddl += HOSTED_ROOM_POLICY_DDL
    conn.executescript(ddl)


def insert_hosted_room(
    conn: sqlite3.Connection,
    room_id: str,
    *,
    name: str = "room",
    member_count: int = 2,
    authority_epoch: int = 1,
    next_seq: int = 1,
    event_bytes: int = 0,
    revision: int = 1,
    created_at: float = _NOW - _DAY,
    updated_at: float = _NOW - 60.0,
    disbanded_at: float | None = None,
) -> None:
    """Insert one hosted_rooms row, addressing every column by name."""
    columns = [
        "room_id",
        "name",
        "members_json",
        "authority_gateway_id",
        "authority_epoch",
        "next_seq",
        "revision",
        "created_at",
        "updated_at",
        "disbanded_at",
    ]
    values: list[object] = [
        room_id,
        name,
        json.dumps([f"member-{index}" for index in range(member_count)]),
        "install:gateway",
        authority_epoch,
        next_seq,
        revision,
        created_at,
        updated_at,
        disbanded_at,
    ]
    table_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(hosted_rooms)")}
    if "event_bytes" in table_columns:
        columns.insert(6, "event_bytes")
        values.insert(6, event_bytes)
    conn.execute(
        f"INSERT INTO hosted_rooms ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        tuple(values),
    )


def insert_hosted_room_event(
    conn: sqlite3.Connection,
    room_id: str,
    seq: int,
    kind: str,
    *,
    payload: str = "{}",
    actor: str = '{"kind":"system","id":"system"}',
    created_at: float = _NOW - 120.0,
) -> None:
    conn.execute(
        "INSERT INTO hosted_room_events "
        "(room_id, seq, event_id, kind, actor_json, authority_epoch, payload_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (room_id, seq, f"evt-{room_id}-{seq}", kind, actor, 1, payload, created_at),
    )


def insert_peer_reservation(
    conn: sqlite3.Connection,
    room_id: str,
    member_id: str,
    *,
    expires_at: float,
    revoked_at: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO hosted_room_peer_reservations "
        "(room_id, member_id, target_profile, authority_gateway_id, authority_epoch, "
        " expires_at, revoked_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (room_id, member_id, "coding", "install:gateway", 1, expires_at, revoked_at, _NOW, _NOW),
    )


def insert_room_link(
    conn: sqlite3.Connection,
    room_id: str,
    member_id: str,
    *,
    target_url: str = "https://user:CANARY_GRANT_URL@peer.invalid/rpc",
    grant: str = "CANARY_GRANT_TOKEN",
    catalog_json: str = '{"tools":["CANARY_CATALOG"]}',
) -> None:
    conn.execute(
        "INSERT INTO hosted_room_links "
        "(room_id, member_id, target_url, target_profile, grant, catalog_json, "
        " cancellation_scope_id, trace_id, transport_security, status, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            room_id,
            member_id,
            target_url,
            "coding",
            grant,
            catalog_json,
            "scope-1",
            "trace-1",
            "mtls",
            "ready",
            _NOW - 30.0,
        ),
    )


def _build_populated_db(db_path: Path, *, with_policy_tables: bool = True) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        create_shared_state_db(conn, with_policy_tables=with_policy_tables)
        insert_hosted_room(conn, "room-active", name="Active Room", next_seq=6, event_bytes=4096)
        insert_hosted_room(
            conn,
            "room-disbanded",
            name="Disbanded Room",
            member_count=3,
            next_seq=2,
            authority_epoch=4,
            revision=7,
            updated_at=_NOW - 3 * _DAY,
            disbanded_at=_NOW - 2 * _DAY,
        )
        # Member *identities* are a canary: only len(members_json) may leave.
        conn.execute(
            "UPDATE hosted_rooms SET members_json=? WHERE room_id='room-disbanded'",
            (json.dumps(["CANARY_MEMBER_ID", "CANARY_MEMBER_TWO", "third"]),),
        )
        insert_hosted_room_event(conn, "room-active", 1, "room.created")
        insert_hosted_room_event(conn, "room-active", 2, "message.user", created_at=_NOW - 90.0)
        insert_hosted_room_event(
            conn,
            "room-active",
            3,
            "turn.settled",
            payload='{"text":"CANARY_EVENT_PAYLOAD"}',
            actor='{"kind":"member","id":"CANARY_ACTOR_ID"}',
        )
        # Not in the closed vocabulary: must land in the unknown count, never
        # reach a model as a raw string.
        insert_hosted_room_event(conn, "room-active", 4, "CANARY_EVENT_KIND")
        conn.execute(
            "INSERT INTO hosted_room_retired_ids (room_id, retired_at) VALUES (?,?)",
            ("room-gone", _NOW - 5 * _DAY),
        )
        insert_room_link(conn, "room-active", "member-a")
        conn.execute(
            "INSERT INTO hosted_room_remote_runs "
            "(room_id, home_install_id, authority_gateway_id, authority_epoch, member_id, "
            " task_id, execution_generation, target_install_id, target_profile, run_id, "
            " session_id, created_at, updated_at) "
            "VALUES ('room-active','install:home','install:gateway',1,'member-a','task-1',1,"
            " 'install:peer','coding','run-1','sess-1',?,?)",
            (_NOW - 4 * _DAY, _NOW - 300.0),
        )
        conn.execute(
            "INSERT INTO hosted_room_revoked_grants (scope_key, expires_at, revoked_before) "
            "VALUES ('CANARY_SCOPE_KEY', ?, ?)",
            (_NOW + _DAY, _NOW - _DAY),
        )
        insert_peer_reservation(conn, "room-active", "member-a", expires_at=_NOW + 600.0)
        insert_peer_reservation(conn, "room-active", "member-b", expires_at=_NOW - 600.0)
        insert_peer_reservation(
            conn,
            "room-active",
            "member-c",
            expires_at=_NOW + 600.0,
            revoked_at=_NOW - 60.0,
        )
        if with_policy_tables:
            conn.execute(
                "INSERT INTO hosted_room_policy_transcript (room_id, seq, body) "
                "VALUES ('room-active', 1, 'CANARY_TRANSCRIPT_BODY')"
            )
            conn.execute(
                "INSERT INTO hosted_room_policy_transcript_state (room_id, body) "
                "VALUES ('room-active', 'CANARY_TRANSCRIPT_STATE')"
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def shared_state_db(hermes_home: Path) -> Iterator[Path]:
    """A populated ROOT ``shared-state.db``, closed before the collector runs."""
    db_path = hermes_home / "shared-state.db"
    _build_populated_db(db_path)
    yield db_path


def _collect(home: Path, **kwargs: object):
    c = Collector(home, clock=lambda: _NOW, **kwargs)  # type: ignore[arg-type]
    try:
        return c.collect()
    finally:
        c.close()


def _dump(state: object) -> str:
    return json.dumps(state.model_dump(mode="json"))  # type: ignore[attr-defined]


def _traced_statements(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every ``(connection target, SQL statement)`` pair issued during the test.

    ``set_trace_callback`` yields the expanded statement text, so a bound
    identifier shows up as the literal SQLite actually parsed.
    """
    statements: list[tuple[str, str]] = []
    real_connect = sqlite3.connect

    def tracing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        target = str(args[0]) if args else str(kwargs.get("database") or "")
        conn.set_trace_callback(lambda sql: statements.append((target, sql)))
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracing_connect)
    return statements


# --- scoping: which file, and whose file ------------------------------------


def test_hosted_rooms_absent_database_is_not_a_failed_source(hermes_home: Path):
    state = _collect(hermes_home)

    assert state.operations.hosted_rooms == HostedRoomState()
    assert state.operations.hosted_rooms.db_present is False
    assert "hosted_rooms" not in state.health.failed_sources


def test_hosted_rooms_reader_targets_shared_state_db_not_the_master_session_store(
    hermes_home: Path,
):
    """The live ``state.db`` carries empty legacy hosted_room* tables.

    Reading them would be reading a dead table, so a populated ``state.db`` and
    no ``shared-state.db`` must report an absent database.
    """
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    try:
        create_shared_state_db(conn, with_policy_tables=False)
        insert_hosted_room(conn, "room-in-state-db", name="WRONG STORE")
        insert_hosted_room_event(conn, "room-in-state-db", 1, "room.created")
        conn.commit()
    finally:
        conn.close()

    state = _collect(hermes_home)

    hosted = state.operations.hosted_rooms
    assert hosted.db_present is False
    assert hosted.active_room_count == 0
    assert hosted.event_count == 0
    assert "room-in-state-db" not in _dump(state)
    assert "hosted_rooms" not in state.health.failed_sources


def test_hosted_rooms_reader_issues_no_query_against_the_master_session_store(
    hermes_home: Path, shared_state_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """SQL-level pin on the same fact: the room queries run on shared-state.db."""
    statements = _traced_statements(monkeypatch)

    _collect(hermes_home)

    hosted = [(target, sql) for target, sql in statements if "hosted_room" in sql]
    assert hosted, "expected the reader to issue hosted_room* queries"
    for target, sql in hosted:
        assert "shared-state.db" in target, f"hosted_room query on {target!r}: {sql}"


def test_hosted_rooms_prefers_shared_state_db_over_a_populated_state_db(
    hermes_home: Path, shared_state_db: Path
):
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    try:
        create_shared_state_db(conn, with_policy_tables=False)
        insert_hosted_room(conn, "room-in-state-db", name="WRONG STORE")
        conn.commit()
    finally:
        conn.close()

    state = _collect(hermes_home)

    room_ids = [room.room_id for room in state.operations.hosted_rooms.rooms]
    assert "room-in-state-db" not in room_ids
    assert "room-active" in room_ids
    assert "room-in-state-db" not in _dump(state)


def test_hosted_rooms_is_root_scoped_under_a_profile(profiled_hermes_home: Path):
    """Upstream resolves even a profile gateway to the ROOT shared-state.db."""
    for home, room_id, name in (
        (profiled_hermes_home / "profiles" / "coding", "profile-room", "PROFILE STORE"),
        (profiled_hermes_home, "root-room", "ROOT STORE"),
    ):
        conn = sqlite3.connect(str(home / "shared-state.db"))
        try:
            create_shared_state_db(conn, with_policy_tables=False)
            insert_hosted_room(conn, room_id, name=name)
            conn.commit()
        finally:
            conn.close()

    state = _collect(profiled_hermes_home, profile_name="coding")

    hosted = state.operations.hosted_rooms
    assert [room.room_id for room in hosted.rooms] == ["root-room"]
    assert "profile-room" not in _dump(state)
    assert "hosted_rooms" not in state.health.failed_sources


# --- counts and bounded summaries -------------------------------------------


def test_hosted_rooms_counts_active_and_disbanded_rooms(hermes_home: Path, shared_state_db: Path):
    hosted = _collect(hermes_home).operations.hosted_rooms

    assert hosted.db_present is True
    assert hosted.db_size_bytes > 0
    assert hosted.active_room_count == 1
    assert hosted.disbanded_room_count == 1
    assert hosted.room_count == 2


def test_hosted_room_summaries_carry_counts_and_epochs_only(
    hermes_home: Path, shared_state_db: Path
):
    hosted = _collect(hermes_home).operations.hosted_rooms
    rooms = {room.room_id: room for room in hosted.rooms}

    active = rooms["room-active"]
    assert active.name == "Active Room"
    assert active.member_count == 2
    assert active.authority_epoch == 1
    assert active.next_seq == 6
    # latest_seq is derived, never stored: upstream keeps next_seq one ahead.
    assert active.latest_seq == 5
    assert active.event_bytes == 4096
    assert active.revision == 1
    assert active.created_at_age_seconds == pytest.approx(_DAY)
    assert active.updated_at_age_seconds == pytest.approx(60.0)
    assert active.disbanded is False
    assert active.disbanded_at_age_seconds is None

    disbanded = rooms["room-disbanded"]
    assert disbanded.member_count == 3
    assert disbanded.authority_epoch == 4
    assert disbanded.revision == 7
    assert disbanded.disbanded is True
    assert disbanded.disbanded_at_age_seconds == pytest.approx(2 * _DAY)


def test_hosted_rooms_event_kind_histogram_is_allowlisted(hermes_home: Path, shared_state_db: Path):
    state = _collect(hermes_home)
    hosted = state.operations.hosted_rooms

    assert hosted.event_count == 4
    assert hosted.event_kind_counts == {
        HostedRoomEventKind.ROOM_CREATED.value: 1,
        HostedRoomEventKind.MESSAGE_USER.value: 1,
        HostedRoomEventKind.TURN_SETTLED.value: 1,
    }
    assert hosted.unknown_event_kind_count == 1
    assert hosted.newest_event_age_seconds == pytest.approx(90.0)
    assert hosted.accounted_event_bytes == 4096
    assert "CANARY_EVENT_KIND" not in _dump(state)


def test_hosted_rooms_counts_side_tables_and_peer_reservation_liveness(
    hermes_home: Path, shared_state_db: Path
):
    hosted = _collect(hermes_home).operations.hosted_rooms

    assert hosted.retired_id_count == 1
    assert hosted.newest_retired_id_age_seconds == pytest.approx(5 * _DAY)
    assert hosted.link_count == 1
    assert hosted.remote_run_count == 1
    assert hosted.newest_remote_run_age_seconds == pytest.approx(300.0)
    assert hosted.revoked_grant_count == 1
    assert hosted.live_peer_reservation_count == 1
    assert hosted.expired_peer_reservation_count == 1
    assert hosted.revoked_peer_reservation_count == 1
    assert hosted.peer_reservation_count == 3


def test_hosted_rooms_tolerates_a_legacy_schema_without_event_bytes(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "shared-state.db"))
    try:
        create_shared_state_db(conn, with_event_bytes=False, with_policy_tables=False)
        insert_hosted_room(conn, "room-legacy", next_seq=3)
        conn.commit()
    finally:
        conn.close()

    state = _collect(hermes_home)
    hosted = state.operations.hosted_rooms

    assert hosted.db_present is True
    assert hosted.active_room_count == 1
    assert hosted.rooms[0].latest_seq == 2
    assert hosted.rooms[0].event_bytes == 0
    assert hosted.accounted_event_bytes == 0
    assert "hosted_rooms" not in state.health.failed_sources


def test_hosted_rooms_tolerates_a_database_without_the_room_tables(hermes_home: Path):
    """An empty shared-state.db (no hosted_room* DDL) is present but silent."""
    conn = sqlite3.connect(str(hermes_home / "shared-state.db"))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()

    hosted = _collect(hermes_home).operations.hosted_rooms

    assert hosted.db_present is True
    assert hosted.room_count == 0
    assert hosted.event_count == 0
    assert hosted.peer_reservation_count == 0


def test_hosted_room_list_is_bounded_but_the_count_is_not(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "shared-state.db"))
    try:
        create_shared_state_db(conn, with_policy_tables=False)
        for index in range(12):
            insert_hosted_room(conn, f"room-{index:02d}", updated_at=_NOW - index)
        conn.commit()
    finally:
        conn.close()

    hosted = _collect(hermes_home).operations.hosted_rooms

    assert hosted.room_count == 12
    assert hosted.active_room_count == 12
    assert len(hosted.rooms) == hosted_rooms_module.MAX_HOSTED_ROOM_LIST
    assert hosted.rooms_truncated is True
    # Newest first: the most recently updated rooms survive the display cap.
    assert [room.room_id for room in hosted.rooms] == [f"room-{index:02d}" for index in range(8)]


def test_hosted_rooms_truncation_flag_is_false_under_the_cap(
    hermes_home: Path, shared_state_db: Path
):
    assert _collect(hermes_home).operations.hosted_rooms.rooms_truncated is False


def test_hosted_rooms_reads_a_wal_database_through_a_snapshot(hermes_home: Path):
    """The live runs store has a -wal sidecar; the room store may gain one too."""
    db_path = hermes_home / "shared-state.db"
    writer = sqlite3.connect(str(db_path))
    try:
        create_shared_state_db(writer, with_policy_tables=False, journal_mode="WAL")
        insert_hosted_room(writer, "room-wal", name="WAL Room", next_seq=4)
        writer.commit()
        assert db_path.with_name("shared-state.db-wal").exists()
        state = _collect(hermes_home)
    finally:
        writer.close()

    hosted = state.operations.hosted_rooms
    assert hosted.db_present is True
    assert [room.room_id for room in hosted.rooms] == ["room-wal"]
    assert hosted.rooms[0].latest_seq == 3
    # The snapshot path copies into a temp dir; nothing extra lands in the home.
    assert set(hermes_home.glob("shared-state.db*")) <= {
        db_path,
        db_path.with_name("shared-state.db-shm"),
        db_path.with_name("shared-state.db-wal"),
    }


def test_hosted_rooms_null_columns_coerce_to_defaults(hermes_home: Path):
    """A permissive schema still yields zeros and empty strings, never None."""
    conn = sqlite3.connect(str(hermes_home / "shared-state.db"))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            """
            CREATE TABLE hosted_rooms (
                room_id TEXT, name TEXT, members_json TEXT, authority_gateway_id TEXT,
                authority_epoch INTEGER, next_seq INTEGER, event_bytes INTEGER,
                revision INTEGER, created_at REAL, updated_at REAL, disbanded_at REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO hosted_rooms VALUES "
            "('room-null', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    state = _collect(hermes_home)
    hosted = state.operations.hosted_rooms
    room = hosted.rooms[0]

    assert room.name == ""
    assert room.member_count == 0
    assert room.authority_epoch == 0
    assert room.next_seq == 0
    assert room.latest_seq == 0
    assert room.event_bytes == 0
    assert room.created_at_age_seconds is None
    assert hosted.active_room_count == 1
    assert "hosted_rooms" not in state.health.failed_sources


def test_hosted_rooms_oversized_members_json_is_refused_not_parsed(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "shared-state.db"))
    try:
        create_shared_state_db(conn, with_policy_tables=False)
        conn.execute(
            "INSERT INTO hosted_rooms "
            "(room_id, name, members_json, authority_gateway_id, authority_epoch, next_seq, "
            " event_bytes, revision, created_at, updated_at, disbanded_at) "
            "VALUES ('room-big', 'big', ?, 'install:g', 1, 1, 0, 1, ?, ?, NULL)",
            (json.dumps(["CANARY_BIG_MEMBER"] * 40_000), _NOW, _NOW),
        )
        conn.commit()
    finally:
        conn.close()

    state = _collect(hermes_home)

    assert state.operations.hosted_rooms.rooms[0].member_count == 0
    assert "CANARY_BIG_MEMBER" not in _dump(state)


def test_hosted_rooms_room_name_is_length_capped(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "shared-state.db"))
    try:
        create_shared_state_db(conn, with_policy_tables=False)
        insert_hosted_room(conn, "room-long", name="n" * 5_000)
        conn.commit()
    finally:
        conn.close()

    room = _collect(hermes_home).operations.hosted_rooms.rooms[0]

    assert len(room.name) == hosted_rooms_module.MAX_HOSTED_ROOM_TEXT_CHARS


# --- resilience: absent vs lost after a good read ----------------------------


def test_hosted_rooms_preserves_last_good_when_the_database_disappears(
    hermes_home: Path, shared_state_db: Path
):
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        assert first.operations.hosted_rooms.active_room_count == 1
        shared_state_db.unlink()
        second = c.collect()
    finally:
        c.close()

    assert second.operations.hosted_rooms == first.operations.hosted_rooms
    assert second.operations.hosted_rooms.db_present is True
    assert "hosted_rooms" in second.health.failed_sources


def test_hosted_rooms_preserves_last_good_when_the_database_becomes_an_unsafe_symlink(
    hermes_home: Path, shared_state_db: Path, tmp_path: Path
):
    outside = tmp_path / "outside-shared-state.db"
    conn = sqlite3.connect(str(outside))
    try:
        create_shared_state_db(conn, with_policy_tables=False)
        conn.commit()
    finally:
        conn.close()

    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        assert first.operations.hosted_rooms.db_present is True
        shared_state_db.unlink()
        shared_state_db.symlink_to(outside)
        second = c.collect()
    finally:
        c.close()

    assert second.operations.hosted_rooms == first.operations.hosted_rooms
    assert "hosted_rooms" in second.health.failed_sources


def test_hosted_rooms_preserves_last_good_when_the_database_corrupts(
    hermes_home: Path, shared_state_db: Path
):
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        assert first.operations.hosted_rooms.active_room_count == 1
        shared_state_db.write_bytes(b"not a sqlite database")
        second = c.collect()
    finally:
        c.close()

    assert second.operations.hosted_rooms == first.operations.hosted_rooms
    assert "hosted_rooms" in second.health.failed_sources


def test_hosted_rooms_failure_does_not_blank_the_rest_of_the_operations_panel(
    hermes_home: Path, shared_state_db: Path, sample_db: Path
):
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        assert first.operations.state_db_schema_version == 6
        shared_state_db.write_bytes(b"not a sqlite database")
        second = c.collect()
    finally:
        c.close()

    assert second.operations.state_db_schema_version == 6
    assert second.operations.hosted_rooms == first.operations.hosted_rooms
    assert "hosted_rooms" in second.health.failed_sources
    assert "operations" not in second.health.failed_sources


def test_hosted_rooms_recovers_after_a_transient_failure(hermes_home: Path, shared_state_db: Path):
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        shared_state_db.write_bytes(b"not a sqlite database")
        broken = c.collect()
        assert "hosted_rooms" in broken.health.failed_sources
        shared_state_db.unlink()
        conn = sqlite3.connect(str(shared_state_db))
        try:
            create_shared_state_db(conn, with_policy_tables=False)
            insert_hosted_room(conn, "room-recovered")
            conn.commit()
        finally:
            conn.close()
        third = c.collect()
    finally:
        c.close()

    assert "hosted_rooms" not in third.health.failed_sources
    assert [room.room_id for room in third.operations.hosted_rooms.rooms] == ["room-recovered"]
    assert third.operations.hosted_rooms.active_room_count == 1
    assert first.operations.hosted_rooms.active_room_count == 1


# --- the content-free contract ----------------------------------------------


_EXCLUDED_COLUMN_CANARIES = (
    "CANARY_GRANT_TOKEN",
    "CANARY_GRANT_URL",
    "CANARY_CATALOG",
    "CANARY_EVENT_PAYLOAD",
    "CANARY_ACTOR_ID",
    "CANARY_SCOPE_KEY",
    "CANARY_TRANSCRIPT_BODY",
    "CANARY_TRANSCRIPT_STATE",
    "CANARY_EVENT_KIND",
    "CANARY_MEMBER_ID",
    "CANARY_MEMBER_TWO",
)


# Field-name tokens that would let a model carry room content rather than a
# count of it.
_FORBIDDEN_FIELD_TOKENS = (
    "grant",
    "catalog",
    "payload",
    "actor",
    "scope_key",
    "target_url",
    "url",
    "transcript",
    "members_json",
    "body",
    "text",
    "content",
    "member_ids",
)


def test_hosted_room_models_have_no_field_that_can_carry_excluded_content():
    """Structural half of the contract: the field names themselves rule it out.

    A name may mention an excluded concept only to *count* it — ``int`` or a
    ``dict[str, int]`` histogram. Nothing string-typed, list-typed or optional
    may carry one, which is what makes the behavioural canary test below a
    consequence of the model rather than a lucky reader.
    """
    count_only = (int, dict[str, int])
    offenders: list[str] = []
    for model in (HostedRoomState, HostedRoomSummary):
        for name, field in model.model_fields.items():
            if not any(token in name.lower() for token in _FORBIDDEN_FIELD_TOKENS):
                continue
            if field.annotation not in count_only:
                offenders.append(f"{model.__name__}.{name}: {field.annotation}")
        # Every dumped key must exist in model_fields or be a derived count.
        for name, value in model().model_dump(mode="json").items():
            if any(token in name.lower() for token in _FORBIDDEN_FIELD_TOKENS) and not isinstance(
                value, int
            ):
                offenders.append(f"{model.__name__}.{name} dumped {type(value).__name__}")

    assert not offenders, f"hosted-room model fields could carry content: {offenders}"


def test_hosted_rooms_never_surfaces_excluded_column_values(
    hermes_home: Path, shared_state_db: Path
):
    """Behavioural half: a canary in every excluded column stays in the file."""
    dump = _dump(_collect(hermes_home))

    for canary in _EXCLUDED_COLUMN_CANARIES:
        assert canary not in dump


def test_hosted_rooms_issues_no_query_that_can_return_excluded_content(
    hermes_home: Path, shared_state_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """Statement-level pin: the reader never even asks for the content columns.

    ``members_json`` is the one deliberate exception — its *length* is the
    member count — so it is covered by the canary test above instead.
    """
    statements = _traced_statements(monkeypatch)

    _collect(hermes_home)

    hosted = [sql for _, sql in statements if "hosted_room" in sql]
    assert hosted, "expected the reader to issue hosted_room* queries"
    for sql in hosted:
        lowered = sql.lower()
        assert "policy" not in lowered, sql
        assert "payload_json" not in lowered, sql
        assert "actor_json" not in lowered, sql
        assert "catalog_json" not in lowered, sql
        assert "target_url" not in lowered, sql
        assert "scope_key" not in lowered, sql
        # `hosted_room_revoked_grants` is counted, so \bgrant\b (not "grants").
        assert re.search(r"\bgrant\b", lowered) is None, sql


def test_hosted_rooms_members_json_that_is_not_a_collection_counts_zero(hermes_home: Path):
    """A scalar or malformed members_json is reported as uncounted, never guessed."""
    conn = sqlite3.connect(str(hermes_home / "shared-state.db"))
    try:
        create_shared_state_db(conn, with_policy_tables=False)
        for index, raw in enumerate(('"scalar"', "not json at all", "7", "null")):
            conn.execute(
                "INSERT INTO hosted_rooms "
                "(room_id, name, members_json, authority_gateway_id, authority_epoch, "
                " next_seq, event_bytes, revision, created_at, updated_at, disbanded_at) "
                "VALUES (?, 'n', ?, 'install:g', 1, 1, 0, 1, ?, ?, NULL)",
                (f"room-{index}", raw, _NOW, _NOW),
            )
        conn.commit()
    finally:
        conn.close()

    state = _collect(hermes_home)
    hosted = state.operations.hosted_rooms

    assert hosted.active_room_count == 4
    assert [room.member_count for room in hosted.rooms] == [0, 0, 0, 0]
    assert "hosted_rooms" not in state.health.failed_sources


def test_hosted_rooms_members_json_as_a_mapping_is_counted(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "shared-state.db"))
    try:
        create_shared_state_db(conn, with_policy_tables=False)
        conn.execute(
            "INSERT INTO hosted_rooms "
            "(room_id, name, members_json, authority_gateway_id, authority_epoch, next_seq, "
            " event_bytes, revision, created_at, updated_at, disbanded_at) "
            "VALUES ('room-map', 'n', ?, 'install:g', 1, 1, 0, 1, ?, ?, NULL)",
            (json.dumps({"a": 1, "b": 2, "c": 3}), _NOW, _NOW),
        )
        conn.commit()
    finally:
        conn.close()

    assert _collect(hermes_home).operations.hosted_rooms.rooms[0].member_count == 3


def test_hosted_rooms_table_without_a_room_id_fails_the_source(
    hermes_home: Path, shared_state_db: Path
):
    """A room table hermesd cannot summarise must not read as an empty room list."""
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        assert first.operations.hosted_rooms.active_room_count == 1
        shared_state_db.unlink()
        conn = sqlite3.connect(str(shared_state_db))
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("CREATE TABLE hosted_rooms (something_else TEXT)")
            conn.execute("INSERT INTO hosted_rooms VALUES ('x')")
            conn.commit()
        finally:
            conn.close()
        second = c.collect()
    finally:
        c.close()

    assert second.operations.hosted_rooms == first.operations.hosted_rooms
    assert "hosted_rooms" in second.health.failed_sources
