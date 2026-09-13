"""Retained API run reservations from the PROFILE-scoped ``runs_idempotency.db``.

Two facts about this store drive every assertion below.

*It is a replay window, not an activity ledger.* ``reserve``/``lookup`` prune
aged rows only once their stored run status is terminal
(``_prune_stale_terminal_locked``, ``api_server_run_idempotency.py:168-186``),
and long room runs extend ``retention_until`` — so zero rows says nothing about
whether the API was used.

*The file may not be the store the gateway is using.* When it cannot be opened,
upstream logs and falls back to ``":memory:"`` (``:63-84``), which makes
``durable`` False; that capability is only ever served over HTTP
(``api_server.py:2276``), never written to disk. hermesd therefore cannot
distinguish "no reservations retained" from "reservations are in process memory".

It is PROFILE-scoped (``get_hermes_home()/"runs_idempotency.db"``, ``:67``) —
the opposite of the ROOT-scoped ``shared-state.db`` the hosted-room reader uses.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import hermesd.collect.api_runs as api_runs_module
from hermesd.collector import Collector
from hermesd.models import API_RUN_RETENTION_SECONDS, ApiRunReservation, ApiRunReservationsState

_NOW = 1_800_000_000.0
_HOUR = 3_600.0

# Verbatim from api_server_run_idempotency.py:86-105.
RUN_IDEMPOTENCY_DDL = """
CREATE TABLE run_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status_json TEXT NOT NULL,
    owner_pid INTEGER NOT NULL DEFAULT 0,
    owner_started INTEGER NOT NULL DEFAULT 0,
    retention_until REAL NOT NULL DEFAULT 0,
    acknowledged_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);
CREATE UNIQUE INDEX run_idempotency_run_id ON run_idempotency(run_id);
"""

# The first shipped schema, before _MIGRATIONS (:29-34) added the ownership and
# retention columns.
LEGACY_RUN_IDEMPOTENCY_DDL = """
CREATE TABLE run_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);
"""


def create_runs_db(conn: sqlite3.Connection, *, legacy: bool = False) -> None:
    """Create run_idempotency. DELETE journal keeps reads on immutable=1."""
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(LEGACY_RUN_IDEMPOTENCY_DDL if legacy else RUN_IDEMPOTENCY_DDL)


def insert_reservation(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    scope: str = "CANARY_SCOPE_VALUE",
    idempotency_key: str = "CANARY_IDEMPOTENCY_KEY",
    fingerprint: str = "CANARY_FINGERPRINT",
    status: str = "running",
    status_json: str | None = None,
    owner_pid: int = 0,
    owner_started: int = 0,
    retention_until: float = 0.0,
    acknowledged_at: float | None = None,
    created_at: float = _NOW - _HOUR,
    updated_at: float = _NOW - 120.0,
) -> None:
    """Insert one row, addressing every column by name.

    Column *order* is deliberately not relied on: this branch already found the
    cron ``executions`` table with two different orders in two profiles.
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(run_idempotency)")}
    row: dict[str, object] = {
        "scope": scope,
        "idempotency_key": idempotency_key,
        "fingerprint": fingerprint,
        "run_id": run_id,
        "status_json": (status_json if status_json is not None else json.dumps({"status": status})),
        "created_at": created_at,
        "updated_at": updated_at,
    }
    for name, value in (
        ("owner_pid", owner_pid),
        ("owner_started", owner_started),
        ("retention_until", retention_until),
        ("acknowledged_at", acknowledged_at),
    ):
        if name in columns:
            row[name] = value
    conn.execute(
        f"INSERT INTO run_idempotency ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


def _build_populated_db(db_path: Path, *, legacy: bool = False) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        create_runs_db(conn, legacy=legacy)
        insert_reservation(
            conn,
            "run-live",
            scope="tenant-a",
            idempotency_key="key-a",
            fingerprint="fp-a",
            status="running",
            owner_pid=4242,
            owner_started=1775791399,
            retention_until=_NOW + 12 * _HOUR,
            created_at=_NOW - 2 * _HOUR,
            updated_at=_NOW - 60.0,
        )
        insert_reservation(
            conn,
            "run-terminal",
            scope="tenant-a",
            idempotency_key="key-b",
            fingerprint="fp-b",
            status="completed",
            owner_pid=0,
            acknowledged_at=_NOW - 3 * _HOUR,
            created_at=_NOW - 30 * _HOUR,
            updated_at=_NOW - 29 * _HOUR,
        )
        insert_reservation(
            conn,
            "run-unknown-status",
            scope="tenant-b",
            idempotency_key="key-c",
            fingerprint="fp-c",
            status_json='{"status":"CANARY_UNRECOGNISED_STATUS"}',
            created_at=_NOW - 5 * _HOUR,
            updated_at=_NOW - 4 * _HOUR,
        )
        insert_reservation(
            conn,
            "run-awaiting-approval",
            scope="tenant-b",
            idempotency_key="key-d",
            fingerprint="fp-d",
            status="waiting_for_approval",
            retention_until=_NOW - 300.0,
            created_at=_NOW - 6 * _HOUR,
            updated_at=_NOW - 5 * _HOUR,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def runs_db(hermes_home: Path) -> Path:
    db_path = hermes_home / "runs_idempotency.db"
    _build_populated_db(db_path)
    return db_path


def _collect(home: Path, **kwargs: object):
    c = Collector(home, clock=lambda: _NOW, **kwargs)  # type: ignore[arg-type]
    try:
        return c.collect()
    finally:
        c.close()


def _dump(state: object) -> str:
    return json.dumps(state.model_dump(mode="json"))  # type: ignore[attr-defined]


# --- absence is not idleness -------------------------------------------------


def test_api_runs_absent_database_is_not_a_failed_source(hermes_home: Path):
    state = _collect(hermes_home)

    assert state.operations.api_runs == ApiRunReservationsState()
    assert state.operations.api_runs.db_present is False
    assert "api_runs" not in state.health.failed_sources


def test_api_runs_empty_store_still_reports_presence(hermes_home: Path):
    """The live store has zero rows; presence and emptiness are different facts."""
    conn = sqlite3.connect(str(hermes_home / "runs_idempotency.db"))
    try:
        create_runs_db(conn)
        conn.commit()
    finally:
        conn.close()

    api_runs = _collect(hermes_home).operations.api_runs

    assert api_runs.db_present is True
    assert api_runs.reservation_count == 0
    assert api_runs.reservations == []
    assert api_runs.reservations_truncated is False


# --- scoping -----------------------------------------------------------------


def test_api_runs_is_profile_scoped_under_a_profile(profiled_hermes_home: Path):
    """``get_hermes_home()/"runs_idempotency.db"`` — the opposite of F20's root."""
    profile_home = profiled_hermes_home / "profiles" / "coding"
    _build_populated_db(profile_home / "runs_idempotency.db")
    conn = sqlite3.connect(str(profiled_hermes_home / "runs_idempotency.db"))
    try:
        create_runs_db(conn)
        insert_reservation(conn, "run-root-canary", scope="root-scope")
        conn.commit()
    finally:
        conn.close()

    state = _collect(profiled_hermes_home, profile_name="coding")

    api_runs = state.operations.api_runs
    run_ids = [reservation.run_id for reservation in api_runs.reservations]
    assert "run-root-canary" not in run_ids
    assert "run-live" in run_ids
    assert "run-root-canary" not in _dump(state)
    assert "api_runs" not in state.health.failed_sources


def test_api_runs_reads_the_root_store_when_no_profile_is_selected(
    profiled_hermes_home: Path,
):
    _build_populated_db(profiled_hermes_home / "runs_idempotency.db")
    profile_home = profiled_hermes_home / "profiles" / "coding"
    conn = sqlite3.connect(str(profile_home / "runs_idempotency.db"))
    try:
        create_runs_db(conn)
        insert_reservation(conn, "run-profile-canary", scope="profile-scope")
        conn.commit()
    finally:
        conn.close()

    state = _collect(profiled_hermes_home)

    run_ids = [reservation.run_id for reservation in state.operations.api_runs.reservations]
    assert run_ids and "run-profile-canary" not in run_ids
    assert "run-profile-canary" not in _dump(state)


# --- the retained-reservation view -------------------------------------------


def test_api_runs_counts_and_ages_come_from_the_injected_clock(hermes_home: Path, runs_db: Path):
    api_runs = _collect(hermes_home).operations.api_runs

    assert api_runs.db_present is True
    assert api_runs.db_size_bytes > 0
    assert api_runs.reservation_count == 4
    assert api_runs.scope_count == 2
    assert api_runs.newest_age_seconds == pytest.approx(60.0)
    assert api_runs.oldest_age_seconds == pytest.approx(30 * _HOUR)
    assert api_runs.acknowledged_count == 1
    assert api_runs.owner_recorded_count == 1
    # One row is past retention_until and awaits a later request-triggered
    # pruning pass.
    assert api_runs.retention_expired_count == 1


def test_api_run_status_is_allowlisted_and_unknown_survives(hermes_home: Path, runs_db: Path):
    state = _collect(hermes_home)
    by_run = {item.run_id: item for item in state.operations.api_runs.reservations}

    assert by_run["run-live"].status == "running"
    assert by_run["run-live"].terminal is False
    assert by_run["run-terminal"].status == "completed"
    assert by_run["run-terminal"].terminal is True
    assert by_run["run-awaiting-approval"].status == "waiting_for_approval"
    # Unrecognised is reported as unknown, never dropped and never guessed.
    assert by_run["run-unknown-status"].status == "unknown"
    assert by_run["run-unknown-status"].terminal is False
    assert "CANARY_UNRECOGNISED_STATUS" not in _dump(state)


def test_api_run_reservation_carries_ownership_without_a_timestamp(
    hermes_home: Path, runs_db: Path
):
    by_run = {item.run_id: item for item in _collect(hermes_home).operations.api_runs.reservations}

    live = by_run["run-live"]
    assert live.owner_pid == 4242
    assert live.owner_pid_present is True
    # owner_started is platform-dependent units (Linux /proc ticks elsewhere
    # psutil centiseconds), so it is an identity bit, never a timestamp.
    assert live.owner_started_recorded is True
    assert live.retention_remaining_seconds == pytest.approx(12 * _HOUR)
    assert live.acknowledged is False
    assert live.created_at_age_seconds == pytest.approx(2 * _HOUR)
    assert live.updated_at_age_seconds == pytest.approx(60.0)

    terminal = by_run["run-terminal"]
    assert terminal.owner_pid == 0
    assert terminal.owner_pid_present is False
    assert terminal.owner_started_recorded is False
    assert terminal.retention_remaining_seconds is None
    assert terminal.acknowledged is True


def test_api_run_owner_liveness_uses_the_injected_probe(hermes_home: Path, runs_db: Path):
    alive = _collect(hermes_home, pid_exists=lambda pid: pid == 4242).operations.api_runs
    dead = _collect(hermes_home, pid_exists=lambda pid: False).operations.api_runs

    alive_by_run = {item.run_id: item for item in alive.reservations}
    dead_by_run = {item.run_id: item for item in dead.reservations}
    assert alive_by_run["run-live"].owner_alive is True
    assert dead_by_run["run-live"].owner_alive is False
    assert alive_by_run["run-terminal"].owner_alive is False


def test_api_runs_tolerates_the_pre_migration_schema(hermes_home: Path):
    _build_populated_db(hermes_home / "runs_idempotency.db", legacy=True)

    state = _collect(hermes_home)
    api_runs = state.operations.api_runs

    assert api_runs.db_present is True
    assert api_runs.reservation_count == 4
    assert api_runs.acknowledged_count == 0
    assert api_runs.owner_recorded_count == 0
    assert api_runs.retention_expired_count == 0
    assert all(item.owner_pid == 0 for item in api_runs.reservations)
    assert all(item.retention_remaining_seconds is None for item in api_runs.reservations)
    assert "api_runs" not in state.health.failed_sources


def test_api_runs_tolerates_a_database_without_the_table(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "runs_idempotency.db"))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()

    api_runs = _collect(hermes_home).operations.api_runs

    assert api_runs.db_present is True
    assert api_runs.reservation_count == 0
    assert api_runs.scope_count == 0


def test_api_run_list_is_bounded_but_the_count_is_not(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "runs_idempotency.db"))
    try:
        create_runs_db(conn)
        for index in range(12):
            insert_reservation(
                conn,
                f"run-{index:02d}",
                idempotency_key=f"key-{index}",
                updated_at=_NOW - index,
                created_at=_NOW - index,
            )
        conn.commit()
    finally:
        conn.close()

    api_runs = _collect(hermes_home).operations.api_runs

    assert api_runs.reservation_count == 12
    assert len(api_runs.reservations) == api_runs_module.MAX_API_RUN_LIST
    assert api_runs.reservations_truncated is True
    assert [item.run_id for item in api_runs.reservations] == [
        f"run-{index:02d}" for index in range(8)
    ]


def test_api_runs_reads_a_wal_database_through_a_snapshot(hermes_home: Path):
    """The live runs_idempotency.db has a -wal sidecar, so this is the real path."""
    db_path = hermes_home / "runs_idempotency.db"
    writer = sqlite3.connect(str(db_path))
    try:
        create_runs_db(writer)
        writer.execute("PRAGMA journal_mode=WAL")
        insert_reservation(writer, "run-wal", status="queued")
        writer.commit()
        assert db_path.with_name("runs_idempotency.db-wal").exists()
        state = _collect(hermes_home)
    finally:
        writer.close()

    api_runs = state.operations.api_runs
    assert api_runs.db_present is True
    assert [item.run_id for item in api_runs.reservations] == ["run-wal"]
    # The snapshot path copies into a temp dir; nothing extra lands in the home.
    assert set(hermes_home.glob("runs_idempotency.db*")) <= {
        db_path,
        db_path.with_name("runs_idempotency.db-shm"),
        db_path.with_name("runs_idempotency.db-wal"),
    }


def test_api_runs_dangling_wal_keeps_last_good_instead_of_reporting_zero(
    hermes_home: Path, tmp_path: Path
):
    source_db = tmp_path / "source-runs.db"
    writer = sqlite3.connect(str(source_db))
    try:
        create_runs_db(writer)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        insert_reservation(writer, "run-wal", status="queued")
        writer.commit()

        db_path = hermes_home / "runs_idempotency.db"
        wal_path = db_path.with_name("runs_idempotency.db-wal")
        shutil.copy2(source_db, db_path)
        shutil.copy2(source_db.with_name("source-runs.db-wal"), wal_path)
        c = Collector(hermes_home, clock=lambda: _NOW)
        try:
            first = c.collect()
            assert first.operations.api_runs.reservation_count == 1
            assert [item.run_id for item in first.operations.api_runs.reservations] == ["run-wal"]

            wal_path.unlink()
            wal_path.symlink_to(tmp_path / "missing-runs-wal")
            second = c.collect()
        finally:
            c.close()
    finally:
        writer.close()

    assert second.operations.api_runs == first.operations.api_runs
    assert "api_runs" in second.health.failed_sources


def test_api_run_null_and_garbage_status_json_reads_as_unknown(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "runs_idempotency.db"))
    try:
        create_runs_db(conn)
        insert_reservation(conn, "run-garbage", status_json="not json at all")
        insert_reservation(conn, "run-list", status_json='["completed"]', idempotency_key="k2")
        insert_reservation(conn, "run-no-status", status_json='{"other":1}', idempotency_key="k3")
        insert_reservation(
            conn,
            "run-huge",
            status_json=json.dumps({"status": "completed", "pad": "P" * 200_000}),
            idempotency_key="k4",
        )
        conn.commit()
    finally:
        conn.close()

    by_run = {item.run_id: item for item in _collect(hermes_home).operations.api_runs.reservations}

    assert by_run["run-garbage"].status == "unknown"
    assert by_run["run-list"].status == "unknown"
    assert by_run["run-no-status"].status == "unknown"
    # Over the cap the column is not parsed at all: unknown, not "completed".
    assert by_run["run-huge"].status == "unknown"


def test_api_run_null_columns_coerce_to_defaults(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "runs_idempotency.db"))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            """
            CREATE TABLE run_idempotency (
                scope TEXT, idempotency_key TEXT, fingerprint TEXT, run_id TEXT,
                status_json TEXT, owner_pid INTEGER, owner_started INTEGER,
                retention_until REAL, acknowledged_at REAL, created_at REAL, updated_at REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO run_idempotency VALUES "
            "('s','k','f','run-null',NULL,NULL,NULL,NULL,NULL,NULL,NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    api_runs = _collect(hermes_home).operations.api_runs
    item = api_runs.reservations[0]

    assert item.run_id == "run-null"
    assert item.status == "unknown"
    assert item.owner_pid == 0
    assert item.owner_started_recorded is False
    assert item.retention_remaining_seconds is None
    assert item.acknowledged is False
    assert item.created_at_age_seconds is None
    assert api_runs.newest_age_seconds is None
    assert api_runs.oldest_age_seconds is None


def test_api_run_run_id_is_length_capped(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "runs_idempotency.db"))
    try:
        create_runs_db(conn)
        insert_reservation(conn, "r" * 5_000)
        conn.commit()
    finally:
        conn.close()

    item = _collect(hermes_home).operations.api_runs.reservations[0]

    assert len(item.run_id) == api_runs_module.MAX_API_RUN_TEXT_CHARS


# --- resilience --------------------------------------------------------------


def test_api_runs_preserves_last_good_when_the_database_disappears(
    hermes_home: Path, runs_db: Path
):
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        assert first.operations.api_runs.reservation_count == 4
        runs_db.unlink()
        second = c.collect()
    finally:
        c.close()

    assert second.operations.api_runs == first.operations.api_runs
    assert second.operations.api_runs.db_present is True
    assert "api_runs" in second.health.failed_sources


def test_api_runs_preserves_last_good_when_the_database_becomes_an_unsafe_symlink(
    hermes_home: Path, runs_db: Path, tmp_path: Path
):
    outside = tmp_path / "outside-runs.db"
    conn = sqlite3.connect(str(outside))
    try:
        create_runs_db(conn)
        conn.commit()
    finally:
        conn.close()

    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        assert first.operations.api_runs.db_present is True
        runs_db.unlink()
        runs_db.symlink_to(outside)
        second = c.collect()
    finally:
        c.close()

    assert second.operations.api_runs == first.operations.api_runs
    assert "api_runs" in second.health.failed_sources


def test_api_runs_preserves_last_good_when_the_database_corrupts(hermes_home: Path, runs_db: Path):
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        assert first.operations.api_runs.reservation_count == 4
        runs_db.write_bytes(b"not a sqlite database")
        second = c.collect()
    finally:
        c.close()

    assert second.operations.api_runs == first.operations.api_runs
    assert "api_runs" in second.health.failed_sources


def test_api_runs_failure_does_not_blank_the_rest_of_the_operations_panel(
    hermes_home: Path, runs_db: Path, sample_db: Path
):
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        assert first.operations.state_db_schema_version == 6
        runs_db.write_bytes(b"not a sqlite database")
        second = c.collect()
    finally:
        c.close()

    assert second.operations.state_db_schema_version == 6
    assert second.operations.api_runs == first.operations.api_runs
    assert "api_runs" in second.health.failed_sources
    assert "operations" not in second.health.failed_sources


def test_api_runs_recovers_after_a_transient_failure(hermes_home: Path, runs_db: Path):
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        runs_db.write_bytes(b"not a sqlite database")
        broken = c.collect()
        assert "api_runs" in broken.health.failed_sources
        runs_db.unlink()
        conn = sqlite3.connect(str(runs_db))
        try:
            create_runs_db(conn)
            insert_reservation(conn, "run-recovered")
            conn.commit()
        finally:
            conn.close()
        recovered = c.collect()
    finally:
        c.close()

    assert "api_runs" not in recovered.health.failed_sources
    assert [item.run_id for item in recovered.operations.api_runs.reservations] == ["run-recovered"]


# --- the content-free contract -----------------------------------------------


_CANARIES = (
    "CANARY_FINGERPRINT",
    "CANARY_IDEMPOTENCY_KEY",
    "CANARY_SCOPE_VALUE",
    "CANARY_UNRECOGNISED_STATUS",
    "fp-a",
    "fp-b",
    "key-a",
    "key-b",
    "tenant-a",
    "tenant-b",
)

_FORBIDDEN_FIELD_TOKENS = (
    "fingerprint",
    "idempotency",
    "scope_key",
    "status_json",
    "owner_started_at",
    "request",
    "body",
    "credential",
)


def test_api_run_models_have_no_field_that_can_carry_excluded_content():
    names: set[str] = set()
    for model in (ApiRunReservationsState, ApiRunReservation):
        names |= set(model.model_fields)
        names |= set(model().model_dump(mode="json"))

    offending = sorted(
        name for name in names if any(token in name.lower() for token in _FORBIDDEN_FIELD_TOKENS)
    )
    assert not offending, f"api-run model fields could carry content: {offending}"
    # `scope` survives only as a distinct-count, never as a value.
    assert "scope_count" in ApiRunReservationsState.model_fields
    assert not any(name == "scope" or name.endswith("_scope") for name in names)
    # owner_started is /proc ticks on Linux and psutil centiseconds elsewhere, so
    # it may only ever survive as the boolean "an identity was recorded" flag.
    assert "owner_started" not in ApiRunReservation.model_fields
    assert "owner_started_recorded" in ApiRunReservation.model_fields
    assert ApiRunReservation.model_fields["owner_started_recorded"].annotation is bool


def test_api_runs_never_surfaces_fingerprint_key_or_scope_values(hermes_home: Path, runs_db: Path):
    dump = _dump(_collect(hermes_home))

    for canary in _CANARIES:
        assert canary not in dump


def test_api_runs_issues_no_query_that_selects_an_excluded_column(
    hermes_home: Path, runs_db: Path, monkeypatch: pytest.MonkeyPatch
):
    statements: list[str] = []
    real_connect = sqlite3.connect

    def tracing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracing_connect)

    _collect(hermes_home)

    queries = [sql for sql in statements if "run_idempotency" in sql]
    assert queries, "expected the reader to query run_idempotency"
    for sql in queries:
        lowered = sql.lower()
        assert "select *" not in lowered, sql
        assert "fingerprint" not in lowered, sql
        assert "idempotency_key" not in lowered, sql
        # COUNT(DISTINCT scope) is the only place the tenant scope is touched.
        if "scope" in lowered:
            assert "distinct scope" in lowered, sql


def test_api_run_retention_constant_matches_upstream():
    assert API_RUN_RETENTION_SECONDS == 24 * 60 * 60


def test_api_runs_table_without_a_run_id_column_reports_no_reservations(hermes_home: Path):
    """Counts survive a table hermesd cannot list; the list degrades to empty."""
    conn = sqlite3.connect(str(hermes_home / "runs_idempotency.db"))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE run_idempotency (scope TEXT, created_at REAL, updated_at REAL)")
        conn.execute(
            "INSERT INTO run_idempotency VALUES ('tenant-a', ?, ?)", (_NOW - _HOUR, _NOW - 60.0)
        )
        conn.commit()
    finally:
        conn.close()

    state = _collect(hermes_home)
    api_runs = state.operations.api_runs

    assert api_runs.db_present is True
    assert api_runs.reservation_count == 1
    assert api_runs.scope_count == 1
    assert api_runs.reservations == []
    assert api_runs.reservations_truncated is False
    assert api_runs.newest_age_seconds == pytest.approx(60.0)
    assert "api_runs" not in state.health.failed_sources


def test_api_run_null_run_id_renders_as_empty_text(hermes_home: Path):
    conn = sqlite3.connect(str(hermes_home / "runs_idempotency.db"))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            """
            CREATE TABLE run_idempotency (
                scope TEXT, idempotency_key TEXT, fingerprint TEXT, run_id TEXT,
                status_json TEXT, owner_pid INTEGER, owner_started INTEGER,
                retention_until REAL, acknowledged_at REAL, created_at REAL, updated_at REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO run_idempotency VALUES "
            "('s','k','f',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    api_runs = _collect(hermes_home).operations.api_runs

    assert api_runs.reservation_count == 1
    assert api_runs.reservations[0].run_id == ""
