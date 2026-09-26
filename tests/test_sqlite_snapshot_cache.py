"""WAL snapshots are reused until the source database changes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from hermesd.collect import sqlite_util


@pytest.fixture
def wal_db(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    db_path = tmp_path / "home" / "store.db"
    db_path.parent.mkdir()
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (v INTEGER)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    assert db_path.with_name("store.db-wal").exists()
    yield db_path, writer
    writer.close()
    sqlite_util.clear_snapshot_cache()


@pytest.fixture
def copies(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    made: list[Path] = []
    real = sqlite_util.snapshot_wal_database

    def counting(db_path: Path, **kwargs: object):
        made.append(db_path)
        return real(db_path, **kwargs)

    monkeypatch.setattr(sqlite_util, "snapshot_wal_database", counting)
    return made


def _read(db_path: Path) -> list[int]:
    with sqlite_util._connect_readonly_sqlite(db_path) as conn:
        return [row[0] for row in conn.execute("SELECT v FROM t ORDER BY v")]


def test_unchanged_wal_database_is_copied_once(wal_db, copies):
    db_path, _writer = wal_db

    assert _read(db_path) == [1]
    assert _read(db_path) == [1]

    assert len(copies) == 1


def test_a_write_invalidates_the_snapshot(wal_db, copies):
    db_path, writer = wal_db
    assert _read(db_path) == [1]

    writer.execute("INSERT INTO t VALUES (2)")
    writer.commit()

    assert _read(db_path) == [1, 2]
    assert len(copies) == 2


def test_a_replaced_snapshot_in_use_survives_until_released(wal_db, copies):
    db_path, writer = wal_db
    with sqlite_util._connect_readonly_sqlite(db_path) as old_conn:
        old_dir = Path(old_conn.execute("PRAGMA database_list").fetchone()[2]).parent
        writer.execute("INSERT INTO t VALUES (2)")
        writer.commit()
        assert _read(db_path) == [1, 2]
        # The superseded copy is still open here and must still be readable.
        assert old_dir.exists()
        assert [row[0] for row in old_conn.execute("SELECT v FROM t")] == [1]
    assert not old_dir.exists()


def test_clear_removes_idle_snapshots(wal_db, copies):
    db_path, _writer = wal_db
    with sqlite_util._connect_readonly_sqlite(db_path) as conn:
        snap_dir = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent

    sqlite_util.clear_snapshot_cache()

    assert not snap_dir.exists()
    assert _read(db_path) == [1]
    assert len(copies) == 2
