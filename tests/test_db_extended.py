"""Read-only SQLite reader: schema variants, search, WAL snapshots,
connection lifecycle, and interrupt handling."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import hermesd.db as db_module
from hermesd.collector import Collector
from hermesd.db import _LIKE_SEARCH_LIMIT, HermesDB
from hermesd.models import DashboardState, RuntimeStatus
from tests.conftest import (
    _skip_if_root,
    create_state_db_tables,
    create_state_db_with_session,
    insert_model_usage,
)


def _create_hermes_agent_db(path: Path) -> None:
    """Create a newer hermes-agent schema with hidden/last_activity_at/active."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            model TEXT,
            started_at REAL NOT NULL,
            last_activity_at REAL,
            hidden INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            active INTEGER DEFAULT 1
        );
        """
    )
    conn.commit()
    conn.close()


def test_read_sessions_excludes_hidden_rows(tmp_path: Path):
    """hermes-agent marks soft-deleted sessions hidden=1; they must not be shown."""
    db_path = tmp_path / "state.db"
    _create_hermes_agent_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at, hidden)
            VALUES ('sess_visible', 'cli', 1.0, 0);
        INSERT INTO sessions (id, source, started_at, hidden)
            VALUES ('sess_hidden', 'cli', 2.0, 1);
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_null', 'cli', 3.0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert {row["id"] for row in db.read_sessions()} == {"sess_visible", "sess_null"}
    db.close()


def test_read_session_count_excludes_hidden_rows(tmp_path: Path):
    """The session count must agree with the filtered session list."""
    db_path = tmp_path / "state.db"
    _create_hermes_agent_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at, hidden)
            VALUES ('sess_visible', 'cli', 1.0, 0);
        INSERT INTO sessions (id, source, started_at, hidden)
            VALUES ('sess_hidden', 'cli', 2.0, 1);
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_null', 'cli', 3.0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.read_session_count() == 2
    assert db.read_session_count() == len(db.read_sessions())
    db.close()


def test_read_session_count_legacy_schema_counts_every_row(hermes_home):
    """An older DB without a hidden column counts all sessions."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_a', 'cli', 1.0);
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_b', 'cli', 2.0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.read_session_count() == 2
    db.close()


def test_read_sessions_orders_by_last_activity_at(tmp_path: Path):
    """A revived old session sorts ahead of a newer one with no later activity."""
    db_path = tmp_path / "state.db"
    _create_hermes_agent_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at, last_activity_at)
            VALUES ('sess_revived', 'cli', 1.0, 100.0);
        INSERT INTO sessions (id, source, started_at, last_activity_at)
            VALUES ('sess_recent', 'cli', 50.0, NULL);
        INSERT INTO sessions (id, source, started_at, last_activity_at)
            VALUES ('sess_old', 'cli', 2.0, 3.0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert [row["id"] for row in db.read_sessions()] == [
        "sess_revived",
        "sess_recent",
        "sess_old",
    ]
    db.close()


def test_read_sessions_legacy_schema_orders_by_started_at(hermes_home):
    """An older DB without hidden/last_activity_at still reads and sorts correctly."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_a', 'cli', 1.0);
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_b', 'cli', 9.0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert [row["id"] for row in db.read_sessions()] == ["sess_b", "sess_a"]
    db.close()


def test_tool_stats_exclude_inactive_messages(tmp_path: Path):
    """Compacted-away messages (active=0) must not inflate tool call counts."""
    db_path = tmp_path / "state.db"
    _create_hermes_agent_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_1', 'cli', 1.0);
        INSERT INTO messages (session_id, role, tool_name, timestamp, active)
            VALUES ('sess_1', 'assistant', 'shell_exec', 1.0, 1);
        INSERT INTO messages (session_id, role, tool_name, timestamp, active)
            VALUES ('sess_1', 'assistant', 'shell_exec', 2.0, 0);
        INSERT INTO messages (session_id, role, tool_name, timestamp, active)
            VALUES ('sess_1', 'assistant', 'browser_open', 3.0, 0);
        INSERT INTO messages (session_id, role, tool_name, timestamp)
            VALUES ('sess_1', 'assistant', 'shell_exec', 4.0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    stats = db.read_tool_stats()
    assert stats == [{"tool_name": "shell_exec", "call_count": 2}]
    db.close()


def test_tool_stats_legacy_schema_counts_every_row(hermes_home):
    """Without an active column every tool row still counts."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_1', 'cli', 1.0);
        INSERT INTO messages (session_id, role, tool_name, timestamp)
            VALUES ('sess_1', 'assistant', 'shell_exec', 1.0);
        INSERT INTO messages (session_id, role, tool_name, timestamp)
            VALUES ('sess_1', 'assistant', 'shell_exec', 2.0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.read_tool_stats() == [{"tool_name": "shell_exec", "call_count": 2}]
    db.close()


def test_repeated_message_search_serves_cache_without_requery(tmp_path: Path):
    """An unchanged repeat search hits the version cache and clears the stale flag."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', 1.0);
        INSERT INTO messages (session_id, role, content, tool_name, timestamp)
            VALUES ('s1', 'assistant', 'used a tool', 'shell_exec', 1.0);
    """)
    conn.commit()
    conn.close()
    db = HermesDB(db_path)

    first = db.search_session_ids_by_message("used a tool")
    assert first == {"s1"}
    assert db.last_message_search_stale is False

    # No write between calls, so data_version is unchanged: the second call must
    # return the same observable result and (re)assert a non-stale read.
    second = db.search_session_ids_by_message("used a tool")
    assert second == {"s1"}
    assert db.last_message_search_stale is False
    db.close()


def test_read_sessions(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    assert len(sessions) == 2
    assert {s["id"] for s in sessions} == {"sess_001", "sess_002"}
    db.close()


def test_read_sessions_returns_dicts(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    s = sessions[0]
    assert "id" in s
    assert "source" in s
    assert "message_count" in s
    assert "input_tokens" in s
    db.close()


def test_tool_stats_with_data(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    stats = db.read_tool_stats()
    assert len(stats) >= 1
    assert stats[0]["tool_name"] == "shell_exec"
    assert stats[0]["call_count"] == 3
    db.close()


def test_sessions_empty_db(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.close()
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    assert sessions == []
    db.close()


def test_read_sessions_excludes_private_payload_columns(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions ("
        "id, source, started_at, message_count, input_tokens, "
        "system_prompt, model_config, billing_base_url"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "sess_private",
            "cli",
            1.0,
            3,
            42,
            "very large prompt",
            '{"secret": "shape"}',
            "https://billing.example.test/private",
        ),
    )
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    row = db.read_sessions()[0]

    assert row["id"] == "sess_private"
    assert row["message_count"] == 3
    assert row["input_tokens"] == 42
    assert "system_prompt" not in row
    assert "model_config" not in row
    assert row["billing_base_url"] == "https://billing.example.test/private"
    db.close()


def test_read_only_uri_is_immutable_and_does_not_create_sidecars(tmp_path: Path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_001', 'cli', 1.0)")
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.read_sessions()[0]["id"] == "sess_001"
    assert db._uri.endswith("?mode=ro&immutable=1")  # rollback-journal path unchanged
    assert not db_path.with_name("state.db-wal").exists()
    assert not db_path.with_name("state.db-shm").exists()
    db.close()


def test_wal_snapshot_reads_uncheckpointed_data_without_home_sidecars(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_db = source_dir / "state.db"
    writer = sqlite3.connect(str(source_db))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_wal', 'cli', 1.0)")
    writer.commit()

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    db_path = hermes_home / "state.db"
    shutil.copy2(source_db, db_path)
    shutil.copy2(source_db.with_name("state.db-wal"), db_path.with_name("state.db-wal"))
    assert not db_path.with_name("state.db-shm").exists()

    db = HermesDB(db_path)
    try:
        assert [row["id"] for row in db.read_sessions()] == ["sess_wal"]
        assert not db_path.with_name("state.db-shm").exists()
    finally:
        db.close()
        writer.close()


def test_open_db_reader_refreshes_after_later_wal_commit(tmp_path: Path):
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_1', 'cli', 1.0)")
    writer.commit()

    db = HermesDB(db_path)
    try:
        assert [row["id"] for row in db.read_sessions()] == ["sess_1"]

        writer.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES ('sess_2', 'cli', 2.0)"
        )
        writer.commit()

        assert {row["id"] for row in db.read_sessions()} == {"sess_1", "sess_2"}
    finally:
        db.close()
        writer.close()


def test_wal_snapshot_is_reused_until_source_changes(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_1', 'cli', 1.0)")
    writer.commit()

    snapshots: list[Path] = []
    original = db_module.snapshot_wal_database

    def counting(target: Path, *, prefix: str):
        snapshots.append(Path(target))
        return original(target, prefix=prefix)

    monkeypatch.setattr(db_module, "snapshot_wal_database", counting)

    db = HermesDB(db_path)
    try:
        assert [row["id"] for row in db.read_sessions()] == ["sess_1"]
        assert db.read_session_count() == 1
        assert snapshots == [db_path]
        assert [row["id"] for row in db.read_sessions()] == ["sess_1"]
        assert snapshots == [db_path]

        writer.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES ('sess_2', 'cli', 2.0)"
        )
        writer.commit()

        assert {row["id"] for row in db.read_sessions()} == {"sess_1", "sess_2"}
    finally:
        db.close()
        writer.close()

    assert snapshots == [db_path, db_path]


def test_wal_snapshot_open_failure_marks_stale_and_recovers(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_fb', 'cli', 1.0)")
    writer.commit()
    assert db_path.with_name("state.db-shm").exists()

    snapshots: list[Path] = []
    original_snapshot = db_module.snapshot_wal_database

    def counting_snapshot(target: Path, *, prefix: str):
        snapshots.append(Path(target))
        return original_snapshot(target, prefix=prefix)

    monkeypatch.setattr(db_module, "snapshot_wal_database", counting_snapshot)

    open_attempts = 0
    original_connect = sqlite3.connect

    def blocking_connect(target: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal open_attempts
        open_attempts += 1
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", blocking_connect)

    db = HermesDB(db_path)
    try:
        assert db.read_sessions() == []
        assert db.last_read_sessions_stale is True
        assert db._snapshot_dir is None
        monkeypatch.setattr(sqlite3, "connect", original_connect)
        db._connect()
        assert [row["id"] for row in db.read_sessions()] == ["sess_fb"]
        assert db.last_read_sessions_stale is False
    finally:
        db.close()
        writer.close()

    assert open_attempts == 1
    assert snapshots == [db_path, db_path]


_LIVE_WRITER_SCRIPT = """
import sqlite3, sys, time
from pathlib import Path

db_path, ready_path = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db_path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL)")
conn.execute("INSERT INTO sessions VALUES ('sess_live', 'cli', 1.0)")
conn.commit()
Path(ready_path).write_text("ready")
time.sleep(120)
"""


def _spawn_live_writer(db_path: Path, ready_path: Path) -> subprocess.Popen[bytes]:
    """Hold a WAL writer open in a separate process so it keeps the -shm alive."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _LIVE_WRITER_SCRIPT, str(db_path), str(ready_path)]
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready_path.exists():
            return proc
        if proc.poll() is not None:
            raise RuntimeError("writer subprocess exited before signaling readiness")
        time.sleep(0.05)
    proc.kill()
    proc.wait()
    raise RuntimeError("writer subprocess did not signal readiness in time")


def test_cross_process_wal_writer_leaves_shm_untouched(tmp_path: Path):
    """With a live cross-process writer, live reads must not write wal-index
    read-marks (SQLite coordination bookkeeping) into the -shm sidecar."""
    db_path = tmp_path / "state.db"
    ready_path = tmp_path / "writer-ready"
    proc = _spawn_live_writer(db_path, ready_path)
    try:
        shm_path = db_path.with_name("state.db-shm")
        assert shm_path.exists()
        bytes_before = shm_path.read_bytes()
        mtime_before = shm_path.stat().st_mtime_ns

        db = HermesDB(db_path)
        try:
            assert db._snapshot_dir is not None
            assert [row["id"] for row in db.read_sessions()] == ["sess_live"]
            assert db.read_session_count() == 1
        finally:
            db.close()

        assert shm_path.read_bytes() == bytes_before
        assert shm_path.stat().st_mtime_ns == mtime_before
    finally:
        proc.terminate()
        proc.wait()


def test_stale_shm_without_writer_reads_and_leaves_shm_untouched(tmp_path: Path):
    """A stale -shm without a live writer must still read without mutation."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_db = source_dir / "state.db"
    writer = sqlite3.connect(str(source_db))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_wal', 'cli', 1.0)")
    writer.commit()

    db_path = tmp_path / "state.db"
    shutil.copy2(source_db, db_path)
    for suffix in ("-wal", "-shm"):
        shutil.copy2(
            source_db.with_name(f"state.db{suffix}"),
            db_path.with_name(f"state.db{suffix}"),
        )
    shm_path = db_path.with_name("state.db-shm")
    bytes_before = shm_path.read_bytes()
    mtime_before = shm_path.stat().st_mtime_ns

    db = HermesDB(db_path)
    try:
        assert [row["id"] for row in db.read_sessions()] == ["sess_wal"]
    finally:
        db.close()
        writer.close()

    assert shm_path.read_bytes() == bytes_before
    assert shm_path.stat().st_mtime_ns == mtime_before


def test_in_process_wal_writer_leaves_source_bytes_untouched(tmp_path: Path):
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_1', 'cli', 1.0)")
    writer.commit()
    assert db_path.with_name("state.db-shm").exists()

    source_paths = [db_path, db_path.with_name("state.db-wal"), db_path.with_name("state.db-shm")]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_paths}

    db = HermesDB(db_path)
    try:
        assert [row["id"] for row in db.read_sessions()] == ["sess_1"]
        assert db.read_session_count() == 1
        db.close()
        assert {
            path: (path.read_bytes(), path.stat().st_mtime_ns) for path in source_paths
        } == before
    finally:
        db.close()
        writer.close()


def test_wal_snapshot_probe_failure_marks_stale_and_recovers(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_pr', 'cli', 1.0)")
    writer.commit()
    assert db_path.with_name("state.db-shm").exists()

    snapshots: list[Path] = []
    original_snapshot = db_module.snapshot_wal_database

    def counting_snapshot(target: Path, *, prefix: str):
        snapshots.append(Path(target))
        return original_snapshot(target, prefix=prefix)

    monkeypatch.setattr(db_module, "snapshot_wal_database", counting_snapshot)

    original_connect = sqlite3.connect

    class _CantInitConnection:
        """Connect 'succeeds', but the first real read raises READONLY_CANTINIT."""

        def __init__(self, real: sqlite3.Connection):
            self._real = real

        def execute(self, *args: object, **kwargs: object):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        def close(self) -> None:
            self._real.close()

    def fake_connect(target: object, *args: object, **kwargs: object):
        conn = original_connect(target, *args, **kwargs)  # type: ignore[arg-type]
        return _CantInitConnection(conn)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)

    db = HermesDB(db_path)
    try:
        assert db.read_sessions() == []
        assert db.last_read_sessions_stale is True
        assert db._snapshot_dir is None
        monkeypatch.setattr(sqlite3, "connect", original_connect)
        db._connect()
        assert [row["id"] for row in db.read_sessions()] == ["sess_pr"]
        assert db.last_read_sessions_stale is False
    finally:
        db.close()
        writer.close()

    assert snapshots == [db_path, db_path]


def test_wal_without_shm_uses_snapshot_and_creates_no_sidecars(tmp_path: Path, monkeypatch):
    """Without -shm, read-only WAL recovery would write to the db dir: use the snapshot."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_db = source_dir / "state.db"
    writer = sqlite3.connect(str(source_db))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_wal', 'cli', 1.0)")
    writer.commit()

    db_path = tmp_path / "state.db"
    shutil.copy2(source_db, db_path)
    shutil.copy2(source_db.with_name("state.db-wal"), db_path.with_name("state.db-wal"))
    assert not db_path.with_name("state.db-shm").exists()

    snapshots: list[Path] = []
    original = db_module.snapshot_wal_database

    def counting(target: Path, *, prefix: str):
        snapshots.append(Path(target))
        return original(target, prefix=prefix)

    monkeypatch.setattr(db_module, "snapshot_wal_database", counting)

    db = HermesDB(db_path)
    try:
        assert [row["id"] for row in db.read_sessions()] == ["sess_wal"]
        assert not db_path.with_name("state.db-shm").exists()
    finally:
        db.close()
        writer.close()

    assert snapshots == [db_path]


def test_wal_sidecar_probe_uses_exists_strict(tmp_path: Path, monkeypatch):
    """The -wal probe must go through _exists_strict: on Python 3.14 Path.exists()
    reads EACCES as absence, silently routing to immutable=1 checkpoint-lagged data."""
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_1', 'cli', 1.0)")
    writer.commit()
    wal_path = db_path.with_name("state.db-wal")
    assert wal_path.exists()

    probed: list[Path] = []
    real = db_module._exists_strict

    def spy(path: Path) -> bool:
        probed.append(path)
        return real(path)

    monkeypatch.setattr(db_module, "_exists_strict", spy)

    db = HermesDB(db_path)
    try:
        assert [row["id"] for row in db.read_sessions()] == ["sess_1"]
    finally:
        db.close()
        writer.close()

    assert wal_path in probed


@pytest.mark.parametrize("dangling", [False, True])
def test_wal_snapshot_refusal_keeps_last_good_sessions(tmp_path: Path, dangling: bool):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_db = source_dir / "state.db"
    writer = sqlite3.connect(str(source_db))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('sess_wal', 'cli', 1.0)")
    writer.commit()

    db_path = tmp_path / "state.db"
    wal_path = db_path.with_name("state.db-wal")
    shutil.copy2(source_db, db_path)
    shutil.copy2(source_db.with_name("state.db-wal"), wal_path)

    db = HermesDB(db_path, allowed_root=tmp_path)
    try:
        assert [row["id"] for row in db.read_sessions()] == ["sess_wal"]
        assert db.last_read_sessions_stale is False

        outside_wal = tmp_path.parent / f"{tmp_path.name}-outside-wal"
        if not dangling:
            shutil.copy2(wal_path, outside_wal)
        wal_path.unlink()
        wal_path.symlink_to(outside_wal)
        db._connect()

        assert [row["id"] for row in db.read_sessions()] == ["sess_wal"]
        assert db.last_read_sessions_stale is True
        assert db._snapshot_dir is None
    finally:
        db.close()
        writer.close()


@pytest.mark.parametrize("dangling", [False, True])
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_wal_snapshot_refuses_unsafe_sidecars_and_cleans_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, dangling: bool
):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"database")
    db_path.with_name("state.db-wal").write_bytes(b"wal")
    outside = tmp_path.parent / f"{tmp_path.name}{suffix}"
    if not dangling:
        outside.write_bytes(b"outside")
    unsafe_sidecar = db_path.with_name(f"state.db{suffix}")
    if unsafe_sidecar.exists():
        unsafe_sidecar.unlink()
    unsafe_sidecar.symlink_to(outside)
    created = []
    real_temporary_directory = db_module.tempfile.TemporaryDirectory

    def tracking_temporary_directory(*args: object, **kwargs: object):
        directory = real_temporary_directory(*args, **kwargs)
        created.append(directory)
        return directory

    monkeypatch.setattr(db_module.tempfile, "TemporaryDirectory", tracking_temporary_directory)

    with pytest.raises(OSError, match="unsafe SQLite sidecar"):
        db_module.snapshot_wal_database(db_path, prefix="hermesd-test-")

    assert len(created) == 1
    assert not Path(created[0].name).exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_wal_snapshot_propagates_sidecar_probe_failures_and_cleans_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"database")
    db_path.with_name("state.db-wal").write_bytes(b"wal")
    denied_sidecar = db_path.with_name(f"state.db{suffix}")
    if suffix == "-shm":
        denied_sidecar.write_bytes(b"shm")
    created = []
    real_temporary_directory = db_module.tempfile.TemporaryDirectory
    real_exists_strict = db_module._exists_strict

    def tracking_temporary_directory(*args: object, **kwargs: object):
        directory = real_temporary_directory(*args, **kwargs)
        created.append(directory)
        return directory

    def denied_probe(path: Path) -> bool:
        if path == denied_sidecar:
            raise PermissionError("sidecar temporarily unreadable")
        return real_exists_strict(path)

    monkeypatch.setattr(db_module.tempfile, "TemporaryDirectory", tracking_temporary_directory)
    monkeypatch.setattr(db_module, "_exists_strict", denied_probe)

    with pytest.raises(PermissionError, match="temporarily unreadable"):
        db_module.snapshot_wal_database(db_path, prefix="hermesd-test-")

    assert len(created) == 1
    assert not Path(created[0].name).exists()


def test_read_only_uri_handles_uri_metacharacters(tmp_path: Path):
    hermes_home = tmp_path / "hermes?demo#home"
    hermes_home.mkdir()
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_uri', 'cli', 1.0);
    """)
    conn.close()

    db = HermesDB(db_path)
    sessions = db.read_sessions()
    assert [row["id"] for row in sessions] == ["sess_uri"]
    db.close()


def test_close_idempotent():
    db = HermesDB(Path("/nonexistent/state.db"))
    db.close()
    db.close()
    # Post-close reads stay safe: cached (empty) data, no exception.
    assert db.read_sessions() == []


def test_read_after_close_returns_cached(sample_db, hermes_home):
    """After close, read returns last cached data (stale is better than empty)."""
    db = HermesDB(hermes_home / "state.db")
    sessions_before = db.read_sessions()
    assert len(sessions_before) == 2
    db.close()
    sessions_after = db.read_sessions()
    assert sessions_after == sessions_before
    # The post-close read transparently reopens a connection; close it too.
    db.close()


def test_search_session_ids_by_message_like(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    session_ids = db.search_session_ids_by_message("response 0")
    assert session_ids == {"sess_001"}
    db.close()


def test_search_like_escapes_percent_wildcard(hermes_home):
    """A literal % in the query must not act as a LIKE wildcard."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_pct', 'cli', 1.0);
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_plain', 'cli', 2.0);
        INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('sess_pct', 'assistant', 'progress 100% complete', 1.0);
        INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('sess_plain', 'assistant', 'progress 100x complete', 2.0);
    """)
    conn.close()
    db = HermesDB(db_path)
    assert db.search_session_ids_by_message("100%") == {"sess_pct"}
    db.close()


def test_search_like_escapes_underscore_wildcard(hermes_home):
    """A literal _ in the query must not act as a LIKE single-char wildcard."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_us', 'cli', 1.0);
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_nous', 'cli', 2.0);
        INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('sess_us', 'assistant', 'run a_b now', 1.0);
        INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('sess_nous', 'assistant', 'run axb now', 2.0);
    """)
    conn.close()
    db = HermesDB(db_path)
    assert db.search_session_ids_by_message("a_b") == {"sess_us"}
    db.close()


def test_search_blank_query_returns_empty(sample_db, hermes_home):
    """Empty or whitespace-only queries return no sessions without touching the DB."""
    db = HermesDB(hermes_home / "state.db")
    assert db.search_session_ids_by_message("") == set()
    assert db.search_session_ids_by_message("   ") == set()
    db.close()


def test_search_two_distinct_queries_reuse_fts_detection(sample_db, hermes_home):
    """Consecutive distinct queries both resolve correctly (FTS detection memoized)."""
    db = HermesDB(hermes_home / "state.db")
    assert db.search_session_ids_by_message("response 0") == {"sess_001"}
    assert db.search_session_ids_by_message("no such phrase anywhere") == set()
    db.close()


def test_read_sessions_refreshes_updated_older_rows(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    older_session = next(row for row in sessions if row["id"] == "sess_001")
    assert older_session["message_count"] == 77

    conn = sqlite3.connect(str(sample_db))
    conn.execute("UPDATE sessions SET message_count = 999 WHERE id = ?", ("sess_001",))
    conn.commit()
    conn.close()

    refreshed_sessions = db.read_sessions()
    refreshed = next(row for row in refreshed_sessions if row["id"] == "sess_001")
    assert refreshed["message_count"] == 999
    db.close()


def test_read_tool_stats_refreshes_after_sessions_read(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    db.read_sessions()
    original_stats = db.read_tool_stats()
    assert original_stats[0]["tool_name"] == "shell_exec"

    conn = sqlite3.connect(str(sample_db))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("sess_002", "assistant", "new tool", "browser_open", 1.0),
    )
    conn.commit()
    conn.close()

    db.read_sessions()
    refreshed_stats = db.read_tool_stats()
    names = {row["tool_name"] for row in refreshed_stats}
    assert "browser_open" in names
    db.close()


def test_read_session_count_refreshes_after_write(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    assert db.read_session_count() == 2

    conn = sqlite3.connect(str(sample_db))
    conn.execute(
        "INSERT INTO sessions ("
        "id, source, model, started_at, message_count, estimated_cost_usd, title"
        ") VALUES (?,?,?,?,?,?,?)",
        (
            "sess_003",
            "cli",
            "gpt-5.4",
            time.time(),
            1,
            0.0,
            "new title",
        ),
    )
    conn.commit()
    conn.close()

    assert db.read_session_count() == 3
    db.close()


def test_search_session_ids_by_message_refreshes_same_query_after_write(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    session_ids = db.search_session_ids_by_message("shared term")
    assert session_ids == set()

    conn = sqlite3.connect(str(sample_db))
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("sess_001", "assistant", "shared term", None, 2.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)",
        ("sess_002", "assistant", "shared term", None, 3.0),
    )
    conn.commit()
    conn.close()

    refreshed_session_ids = db.search_session_ids_by_message("shared term")
    assert refreshed_session_ids == {"sess_001", "sess_002"}
    db.close()


def test_search_session_ids_by_message_falls_back_to_like_when_fts_misses(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions VALUES (
            'sess_001', 'cli', NULL, 'gpt-5.4',
            NULL, NULL, NULL,
            0, NULL, NULL,
            1, 0,
            0, 0,
            0, 0,
            0, 'openai',
            NULL, NULL,
            0.0, NULL,
            NULL, NULL, NULL,
            'title'
        );
        INSERT INTO messages (
            session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp,
            token_count, finish_reason, reasoning, reasoning_details, codex_reasoning_items
        ) VALUES (
            'sess_001', 'user', 'foo:bar', NULL, NULL, NULL, 0,
            NULL, NULL, NULL, NULL, NULL
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, tool_name, session_id UNINDEXED, content='messages', content_rowid='id'
        );
        INSERT INTO messages_fts (rowid, content, tool_name, session_id)
        SELECT id, content, tool_name, session_id FROM messages;
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    session_ids = db.search_session_ids_by_message("foo:")
    assert session_ids == {"sess_001"}
    db.close()


def test_search_falls_back_to_like_when_fts_returns_no_rows(hermes_home):
    """A mid-token substring misses in FTS but must still be found via LIKE."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_sub', 'cli', 1.0);
        INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('sess_sub', 'user', 'foo:bar', 1.0);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, tool_name, session_id UNINDEXED, content='messages', content_rowid='id'
        );
        INSERT INTO messages_fts (rowid, content, tool_name, session_id)
        SELECT id, content, tool_name, session_id FROM messages;
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    # "oo:ba" is not a token prefix, so the quoted FTS phrase finds nothing;
    # the LIKE fallback matches the substring.
    assert db.search_session_ids_by_message("oo:ba") == {"sess_sub"}
    db.close()


def test_search_like_result_is_bounded_by_a_limit(hermes_home):
    """The LIKE scan must return a bounded result set, not every matching session."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    for index in range(_LIKE_SEARCH_LIMIT + 25):
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, 'cli', ?)",
            (f"sess_{index:04d}", float(index)),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (f"sess_{index:04d}", "user", "needle text", float(index)),
        )
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert len(db.search_session_ids_by_message("needle")) == _LIKE_SEARCH_LIMIT
    db.close()


def test_search_excludes_inactive_messages_via_fts(tmp_path: Path):
    """A hit that only exists in a compacted-away message must not surface its session."""
    db_path = tmp_path / "state.db"
    _create_hermes_agent_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_live', 'cli', 1.0);
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_compacted', 'cli', 2.0);
        INSERT INTO messages (session_id, role, content, timestamp, active)
            VALUES ('sess_live', 'user', 'needle text', 1.0, 1);
        INSERT INTO messages (session_id, role, content, timestamp, active)
            VALUES ('sess_compacted', 'user', 'needle text', 2.0, 0);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            content, tool_name, session_id UNINDEXED, content='messages', content_rowid='id'
        );
        INSERT INTO messages_fts (rowid, content, tool_name, session_id)
        SELECT id, content, tool_name, session_id FROM messages;
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.search_session_ids_by_message("needle") == {"sess_live"}
    db.close()


def test_search_excludes_inactive_messages_via_like(tmp_path: Path):
    """The LIKE path applies the same active filter as the FTS path."""
    db_path = tmp_path / "state.db"
    _create_hermes_agent_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_live', 'cli', 1.0);
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_compacted', 'cli', 2.0);
        INSERT INTO messages (session_id, role, content, timestamp, active)
            VALUES ('sess_live', 'user', 'needle text', 1.0, 1);
        INSERT INTO messages (session_id, role, content, timestamp, active)
            VALUES ('sess_compacted', 'user', 'needle text', 2.0, 0);
        INSERT INTO messages (session_id, role, tool_name, timestamp, active)
            VALUES ('sess_compacted', 'assistant', 'needle_tool', 3.0, 0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.search_session_ids_by_message("needle") == {"sess_live"}
    db.close()


def test_search_only_inactive_match_returns_no_session(tmp_path: Path):
    """When every match is inactive, the search finds nothing at all."""
    db_path = tmp_path / "state.db"
    _create_hermes_agent_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_compacted', 'cli', 1.0);
        INSERT INTO messages (session_id, role, content, timestamp, active)
            VALUES ('sess_compacted', 'user', 'needle text', 1.0, 0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.search_session_ids_by_message("needle") == set()
    db.close()


def test_search_legacy_schema_matches_without_active_column(hermes_home):
    """An older DB has no active column, so every message stays searchable."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_a', 'cli', 1.0);
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_b', 'cli', 2.0);
        INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('sess_a', 'user', 'needle text', 1.0);
        INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('sess_b', 'user', 'needle text', 2.0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.search_session_ids_by_message("needle") == {"sess_a", "sess_b"}
    db.close()


def test_search_falls_back_to_like_when_fts_table_is_absent(hermes_home):
    """Without an FTS index, substring search still works through LIKE."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript("""
        INSERT INTO sessions (id, source, started_at) VALUES ('sess_sub', 'cli', 1.0);
        INSERT INTO messages (session_id, role, content, timestamp)
            VALUES ('sess_sub', 'user', 'foo:bar', 1.0);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    assert db.search_session_ids_by_message("oo:ba") == {"sess_sub"}
    db.close()


def test_search_session_ids_by_message_fts_without_session_id_joins_messages(hermes_home):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_name TEXT,
            timestamp REAL
        );
        INSERT INTO messages (session_id, role, content, tool_name, timestamp)
        VALUES ('sess_join', 'assistant', 'needle text', NULL, 1.0);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name);
        INSERT INTO messages_fts (rowid, content, tool_name)
        VALUES (1, 'needle text', NULL);
    """)
    conn.commit()
    conn.close()

    db = HermesDB(db_path)
    session_ids = db.search_session_ids_by_message("needle")

    assert session_ids == {"sess_join"}
    db.close()


def test_search_session_ids_by_message_falls_back_to_like_when_fts_raises(
    sample_db,
    hermes_home,
    monkeypatch,
):
    db = HermesDB(hermes_home / "state.db")

    def fail_fts(conn: sqlite3.Connection, query: str) -> set[str]:
        raise sqlite3.OperationalError("fts failed")

    monkeypatch.setattr(db, "_messages_fts_enabled", lambda conn: True)
    monkeypatch.setattr(db, "_search_session_ids_by_fts", fail_fts)

    assert db.search_session_ids_by_message("response 0") == {"sess_001"}
    db.close()


def test_db_has_no_dead_cache_hits_counter(sample_db):
    """The _cache_hits counter was incremented but never read; it must be gone."""
    db = HermesDB(sample_db)
    db.read_sessions()
    db.read_sessions()
    assert not hasattr(db, "_cache_hits")
    db.close()


def test_profile_db_rejects_symlink_swapped_target_outside_allowed_root(tmp_path, hermes_home):
    """A profile state.db swapped for an outside symlink after startup is rejected.

    The open must fall back to last-good cached data instead of reading the
    attacker-controlled database.
    """
    profile_home = hermes_home / "profiles" / "coding"
    profile_home.mkdir(parents=True)
    db_path = profile_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('legit', 'cli', 1.0)")
    conn.commit()
    conn.close()

    db = HermesDB(db_path, allowed_root=hermes_home)
    assert [row["id"] for row in db.read_sessions()] == ["legit"]

    outside = tmp_path / "outside.db"
    conn = sqlite3.connect(str(outside))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('evil', 'cli', 2.0)")
    conn.commit()
    conn.close()

    db_path.unlink()
    db_path.symlink_to(outside)
    # Guarantee the source-change check notices the swap and reconnects.
    later = db_path.stat().st_mtime + 10
    os.utime(outside, (later, later))

    sessions = db.read_sessions()
    assert all(row["id"] != "evil" for row in sessions)
    assert [row["id"] for row in sessions] == ["legit"]
    db.close()


def test_profile_db_accepts_legitimate_path_under_allowed_root(hermes_home):
    """The resolve-under check must not break normal profile databases."""
    profile_home = hermes_home / "profiles" / "coding"
    profile_home.mkdir(parents=True)
    db_path = profile_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('ok', 'cli', 1.0)")
    conn.commit()
    conn.close()

    db = HermesDB(db_path, allowed_root=hermes_home)
    assert [row["id"] for row in db.read_sessions()] == ["ok"]
    db.close()


def test_runtime_status_defaults_to_not_running():
    """An uncollected runtime status must read as unknown/offline, not running."""
    assert RuntimeStatus().agent_running is False
    assert DashboardState().runtime.agent_running is False


def test_first_collect_with_failed_runtime_source_reports_agent_not_running(
    populated_hermes_home, monkeypatch
):
    """First collect with a failing runtime source (no last-good state) is not 'running'."""
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)

    def boom(*args: object, **kwargs: object) -> RuntimeStatus:
        raise RuntimeError("runtime source down")

    monkeypatch.setattr(Collector, "_collect_runtime_status", boom)
    state = c.collect()
    assert "runtime" in state.health.failed_sources
    assert state.runtime.agent_running is False
    c.close()


def test_interrupt_aborts_long_running_query(tmp_path):
    """HermesDB.interrupt() must abort an in-flight query from another thread."""
    db_file = tmp_path / "state.db"
    seed = sqlite3.connect(db_file)
    seed.execute("CREATE TABLE t (x INTEGER)")
    seed.close()

    db = HermesDB(db_file)
    assert db._conn is not None
    started = threading.Event()
    outcome: dict[str, str] = {}

    # SQLite calls the progress handler from inside the running statement, so it
    # is the earliest point at which interrupt() is guaranteed to have something
    # to abort. Signalling before .execute() would race the query start.
    db._conn.set_progress_handler(lambda: started.set(), 1000)

    def run_query() -> None:
        try:
            db._conn.execute(
                "WITH RECURSIVE cnt(x) AS ("
                "SELECT 1 UNION ALL SELECT x + 1 FROM cnt LIMIT 1000000000"
                ") SELECT count(*) FROM cnt"
            ).fetchall()
            outcome["result"] = "completed"
        except sqlite3.OperationalError:
            outcome["result"] = "interrupted"

    thread = threading.Thread(target=run_query, daemon=True)
    thread.start()
    assert started.wait(5)
    db.interrupt()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome.get("result") == "interrupted"
    db.close()


def test_interrupt_does_not_wait_for_message_search_lock(tmp_path, monkeypatch):
    """interrupt() must reach SQLite while message search owns the query lock."""
    db_file = tmp_path / "state.db"
    seed = sqlite3.connect(db_file)
    seed.execute("CREATE TABLE messages (session_id TEXT, content TEXT)")
    seed.close()

    db = HermesDB(db_file)
    search_started = threading.Event()
    release_search = threading.Event()
    interrupt_returned = threading.Event()

    monkeypatch.setattr(db, "_messages_fts_enabled", lambda conn: False)

    def slow_search(conn: sqlite3.Connection, query: str) -> set[str]:
        search_started.set()
        release_search.wait(timeout=5)
        return set()

    monkeypatch.setattr(db, "_search_session_ids_by_like", slow_search)
    search_thread = threading.Thread(target=db.search_session_ids_by_message, args=("needle",))
    search_thread.start()
    assert search_started.wait(timeout=5)

    interrupt_thread = threading.Thread(
        target=lambda: (db.interrupt(), interrupt_returned.set()),
    )
    interrupt_thread.start()
    try:
        assert interrupt_returned.wait(timeout=0.5)
    finally:
        release_search.set()
        search_thread.join(timeout=5)
        interrupt_thread.join(timeout=5)
        db.close()


def test_interrupt_after_close_is_noop(tmp_path):
    """interrupt() on a closed HermesDB must not raise or reopen the connection."""
    db_file = tmp_path / "state.db"
    seed = sqlite3.connect(db_file)
    create_state_db_tables(seed, include_schema_version=False)
    seed.execute("INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', 1.0)")
    seed.commit()
    seed.close()

    db = HermesDB(db_file)
    assert db.read_session_count() == 1
    assert [row["id"] for row in db.read_sessions()] == ["s1"]

    db.close()
    db.interrupt()

    # A second session lands on disk after the close. A closed HermesDB must not
    # reconnect to pick it up; it serves the last-good cache instead.
    seed = sqlite3.connect(db_file)
    seed.execute("INSERT INTO sessions (id, source, started_at) VALUES ('s2', 'cli', 2.0)")
    seed.commit()
    seed.close()

    assert db.read_session_count() == 1
    assert [row["id"] for row in db.read_sessions()] == ["s1"]
    db.interrupt()


def test_collector_wires_allowed_root_into_default_db(tmp_path, monkeypatch):
    """Collector must construct HermesDB with allowed_root=root_home so profile
    db targets are re-validated against symlink swaps on every (re)connect."""
    seen: dict[str, object] = {}

    def factory(path: Path, **kwargs: object) -> HermesDB:
        seen.update(kwargs)
        return HermesDB(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("hermesd.collector.HermesDB", factory)
    collector = Collector(tmp_path)  # default db_factory exercises the wiring
    try:
        assert seen.get("allowed_root") == tmp_path
    finally:
        collector.close()


def test_missing_db_returns_empty_not_crash(tmp_path):
    db = HermesDB(tmp_path / "nonexistent.db")
    assert db.read_sessions() == []
    assert db.read_tool_stats() == []
    db.close()


def test_unopenable_db_backs_off_two_reads_before_reconnect(tmp_path, monkeypatch):
    """A failed connect must back off for two reads before retrying, not retry every read."""
    db_dir = tmp_path / "state.db"
    db_dir.mkdir()  # a directory at the db path makes sqlite3.connect fail

    connect_calls = 0
    original_connect = sqlite3.connect

    def counting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)

    db = HermesDB(db_dir)  # connect attempt #1 fails
    assert connect_calls == 1
    assert db.read_sessions() == []  # backoff read 1: no reconnect
    assert db.read_sessions() == []  # backoff read 2: no reconnect
    assert connect_calls == 1
    assert db.read_sessions() == []  # backoff exhausted: reconnect attempt #2
    assert connect_calls == 2
    db.close()


@_skip_if_root
def test_wal_snapshot_copy_failure_then_recovery(tmp_path):
    """If snapshotting a WAL db fails, reads stay safe and recover once readable again."""
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(writer, include_schema_version=False)
    writer.execute("INSERT INTO sessions (id, source, started_at) VALUES ('s1', 'cli', 1.0)")
    writer.commit()
    assert db_path.with_name("state.db-wal").exists()

    db_path.chmod(0o000)  # snapshot copy raises PermissionError
    try:
        db = HermesDB(db_path)
        assert db.read_sessions() == []  # no crash, no data yet (backoff read 1)
        assert db.read_sessions() == []  # backoff read 2
    finally:
        db_path.chmod(0o644)

    # Backoff exhausted; next read reconnects and sees the data.
    assert [row["id"] for row in db.read_sessions()] == ["s1"]
    db.close()
    writer.close()


def test_concurrent_writer_insert_is_visible_on_next_read(tmp_path):
    """Simulate hermes-agent writing while we read."""
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
    db = HermesDB(db_path)

    sessions1 = db.read_sessions()
    assert len(sessions1) == 1

    # Simulate external write (new session added)
    writer = sqlite3.connect(str(db_path))
    writer.execute(
        "INSERT INTO sessions (id, source, started_at, message_count) VALUES (?, ?, ?, ?)",
        ("s2", "telegram", time.time(), 5),
    )
    writer.commit()
    writer.close()

    # Next read should pick up the change (data_version changed)
    sessions2 = db.read_sessions()
    assert len(sessions2) == 2
    db.close()


def test_message_search_fts_check_failure_degrades_without_crashing(tmp_path):
    """If probing for the FTS table raises, search degrades gracefully (no crash)."""
    db_path = tmp_path / "state.db"
    create_state_db_with_session(db_path)
    db = HermesDB(db_path)

    # Kill the handle before any search runs, so FTS availability is still
    # undetermined: the sqlite_master probe inside _messages_fts_enabled raises
    # and must be swallowed (FTS treated as unavailable) rather than crashing.
    db._conn.close()

    assert db._messages_fts_available is None
    result = db.search_session_ids_by_message("used a tool")
    assert result == set()  # no cache yet, query failed -> empty, not a crash
    assert db._messages_fts_available is False
    db.close()


def _model_usage_db(
    hermes_home: Path,
    rows: list[dict[str, object]],
    *,
    table: bool = True,
) -> Path:
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False, include_v021_columns=table)
    for row in rows:
        insert_model_usage(conn, **row)  # type: ignore[arg-type]
    conn.commit()
    conn.close()
    return db_path


def test_read_model_usage_aggregates_by_model_provider_task(hermes_home):
    now = 1_800_000_000.0
    _model_usage_db(
        hermes_home,
        [
            {
                "session_id": "s1",
                "model": "gpt-5.4",
                "provider": "openai",
                "api_call_count": 3,
                "input_tokens": 100,
                "output_tokens": 20,
                "estimated_cost_usd": 0.5,
                "actual_cost_usd": 0.4,
                "last_seen": now - 10,
            },
            {
                "session_id": "s2",
                "model": "gpt-5.4",
                "provider": "openai",
                "api_call_count": 2,
                "input_tokens": 50,
                "output_tokens": 10,
                "estimated_cost_usd": 0.25,
                "actual_cost_usd": 0.2,
                "last_seen": now - 5,
            },
            {
                "session_id": "s3",
                "model": "gpt-5.4",
                "provider": "openai",
                "task": "title",
                "api_call_count": 1,
                "input_tokens": 5,
                "last_seen": now - 5,
            },
        ],
    )
    db = HermesDB(hermes_home / "state.db")
    try:
        usage = db.read_model_usage(now)

        assert [row["model"] for row in usage["all"]] == ["gpt-5.4", "gpt-5.4"]
        main = usage["all"][0]
        assert main["task"] == ""
        assert main["api_calls"] == 5
        assert main["input_tokens"] == 150
        assert main["output_tokens"] == 30
        assert main["estimated_cost_usd"] == pytest.approx(0.75)
        assert main["actual_cost_usd"] == pytest.approx(0.6)
        assert main["last_seen"] == now - 5
        # Auxiliary (task-tagged) work groups separately.
        assert usage["all"][1]["task"] == "title"
    finally:
        db.close()


def test_read_model_usage_tolerates_null_token_columns(hermes_home):
    now = 1_800_000_000.0
    _model_usage_db(
        hermes_home,
        [
            {
                "session_id": "s1",
                "model": "m",
                "input_tokens": None,
                "output_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "reasoning_tokens": None,
                "estimated_cost_usd": None,
                "actual_cost_usd": None,
                "last_seen": now,
            }
        ],
    )
    db = HermesDB(hermes_home / "state.db")
    try:
        row = db.read_model_usage(now)["all"][0]

        # SUM() over NULL columns is NULL: readers must apply `or 0`.
        assert row["input_tokens"] is None
        assert (row["input_tokens"] or 0) == 0
    finally:
        db.close()


def test_read_model_usage_missing_table_returns_empty(hermes_home):
    _model_usage_db(hermes_home, [], table=False)
    db = HermesDB(hermes_home / "state.db")
    try:
        usage = db.read_model_usage(1_800_000_000.0)

        assert usage == {"all": [], "24h": [], "7d": []}
        assert db.last_read_model_usage_stale is False
    finally:
        db.close()


def test_read_model_usage_windows_filter_by_last_seen(hermes_home):
    now = 1_800_000_000.0
    _model_usage_db(
        hermes_home,
        [
            {"session_id": "s1", "model": "recent", "input_tokens": 10, "last_seen": now - 3600},
            {"session_id": "s2", "model": "mid", "input_tokens": 10, "last_seen": now - 86400 * 3},
            {"session_id": "s3", "model": "old", "input_tokens": 10, "last_seen": now - 86400 * 30},
        ],
    )
    db = HermesDB(hermes_home / "state.db")
    try:
        usage = db.read_model_usage(now)

        assert {row["model"] for row in usage["all"]} == {"recent", "mid", "old"}
        assert {row["model"] for row in usage["24h"]} == {"recent"}
        assert {row["model"] for row in usage["7d"]} == {"recent", "mid"}
    finally:
        db.close()


def test_read_model_usage_limits_rows(hermes_home):
    now = 1_800_000_000.0
    _model_usage_db(
        hermes_home,
        [
            {
                "session_id": f"s{index}",
                "model": f"model-{index:03d}",
                "input_tokens": index + 1,
                "last_seen": now,
            }
            for index in range(60)
        ],
    )
    db = HermesDB(hermes_home / "state.db")
    try:
        usage = db.read_model_usage(now)

        assert len(usage["all"]) == 50
        # Ordered by total tokens descending.
        assert usage["all"][0]["model"] == "model-059"
    finally:
        db.close()


def test_read_model_usage_serves_cache_on_unchanged_data_version(hermes_home, monkeypatch):
    now = 1_800_000_000.0
    _model_usage_db(
        hermes_home,
        [{"session_id": "s1", "model": "m", "input_tokens": 5, "last_seen": now}],
    )
    db = HermesDB(hermes_home / "state.db")
    try:
        first = db.read_model_usage(now)

        def unexpected(conn, cutoff):
            raise AssertionError("cached read must not re-query")

        monkeypatch.setattr(db, "_aggregate_model_usage", unexpected)

        assert db.read_model_usage(now) is first
    finally:
        db.close()


def test_read_model_usage_error_serves_last_good(hermes_home, monkeypatch):
    now = 1_800_000_000.0
    _model_usage_db(
        hermes_home,
        [{"session_id": "s1", "model": "m", "input_tokens": 5, "last_seen": now}],
    )
    db = HermesDB(hermes_home / "state.db")
    try:
        good = db.read_model_usage(now)
        assert good["all"]

        def boom(conn, cutoff):
            raise sqlite3.OperationalError("boom")

        monkeypatch.setattr(db, "_aggregate_model_usage", boom)
        # A later "now" moves the window cutoffs, forcing a re-read.
        degraded = db.read_model_usage(now + 7200)

        assert degraded["all"] == good["all"]
        assert db.last_read_model_usage_stale is True
    finally:
        db.close()


def test_read_model_usage_reports_mixed_actual_estimated_groups(hermes_home):
    """Per-group completeness: billed vs estimate-only rows are counted apart."""
    now = 1_800_000_000.0
    _model_usage_db(
        hermes_home,
        [
            # Same group: one billed row (also carrying an estimate) and one
            # estimate-only row.
            {
                "session_id": "s1",
                "model": "gpt-5.4",
                "provider": "openai",
                "input_tokens": 100,
                "estimated_cost_usd": 0.9,
                "actual_cost_usd": 0.55,
                "last_seen": now - 10,
            },
            {
                "session_id": "s2",
                "model": "gpt-5.4",
                "provider": "openai",
                "input_tokens": 50,
                "estimated_cost_usd": 0.30,
                "actual_cost_usd": None,
                "last_seen": now - 5,
            },
            # Estimate-only group.
            {
                "session_id": "s3",
                "model": "claude-opus",
                "provider": "anthropic",
                "input_tokens": 40,
                "estimated_cost_usd": 0.40,
                "actual_cost_usd": None,
                "last_seen": now - 5,
            },
            # Subscription-included row: authoritatively $0.00, not an estimate.
            {
                "session_id": "s4",
                "model": "gpt-5.4-mini",
                "provider": "openai",
                "input_tokens": 10,
                "estimated_cost_usd": 0.01,
                "actual_cost_usd": 0.0,
                "cost_status": "included",
                "last_seen": now - 5,
            },
        ],
    )
    db = HermesDB(hermes_home / "state.db")
    try:
        rows = {row["model"]: row for row in db.read_model_usage(now)["all"]}

        mixed = rows["gpt-5.4"]
        assert mixed["row_count"] == 2
        assert mixed["reported_row_count"] == 1
        assert mixed["reported_cost_usd"] == pytest.approx(0.55)
        # The billed row's own estimate column is not double-counted.
        assert mixed["estimated_only_cost_usd"] == pytest.approx(0.30)

        estimated = rows["claude-opus"]
        assert estimated["row_count"] == 1
        assert estimated["reported_row_count"] == 0
        assert estimated["reported_cost_usd"] == 0
        assert estimated["estimated_only_cost_usd"] == pytest.approx(0.40)

        included = rows["gpt-5.4-mini"]
        assert included["row_count"] == 1
        assert included["reported_row_count"] == 1
        assert included["reported_cost_usd"] == 0.0
        assert included["estimated_only_cost_usd"] == 0
    finally:
        db.close()
