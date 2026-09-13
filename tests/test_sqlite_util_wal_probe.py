"""The ``-wal`` sidecar probe must fail the source, not silently downgrade.

``_connect_readonly_sqlite`` chooses between a temporary WAL snapshot and a direct
``mode=ro&immutable=1`` open. If the sidecar probe swallows an ``EACCES`` — which
``Path.exists()`` does from Python 3.14 onward, when ``_ignore_error`` was widened
— an unreadable sidecar reads as *absent*, and the connection silently falls
through to ``immutable=1``. That serves checkpoint-lagging data as though it were
current, which is the false-empty class of failure this project treats as a source
error. ``_exists_strict`` exists for exactly this reason.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermesd.collect.sqlite_util import _connect_readonly_sqlite


def test_unreadable_wal_sidecar_propagates_instead_of_downgrading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    wal_path = tmp_path / "state.db-wal"
    db_path.write_bytes(b"")
    wal_path.write_bytes(b"")

    real_stat = Path.stat
    real_exists = Path.exists

    def denied_stat(self, *args, **kwargs):
        if self == wal_path:
            raise PermissionError(13, "Permission denied")
        return real_stat(self, *args, **kwargs)

    # Simulate the Python 3.14 behaviour: exists() swallows EACCES and says False.
    def swallowed_exists(self, *args, **kwargs):
        if self == wal_path:
            return False
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    monkeypatch.setattr(Path, "exists", swallowed_exists)

    with pytest.raises(PermissionError), _connect_readonly_sqlite(db_path) as conn:
        conn.execute("SELECT 1")


def test_absent_wal_sidecar_still_uses_the_immutable_open(tmp_path: Path) -> None:
    """A genuinely absent sidecar is not an error — that is the normal read path."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.execute("INSERT INTO t VALUES (7)")
        conn.commit()
    finally:
        conn.close()
    assert not db_path.with_name("state.db-wal").exists()

    with _connect_readonly_sqlite(db_path) as ro:
        assert ro.execute("SELECT a FROM t").fetchone()[0] == 7


def test_dangling_wal_symlink_is_refused_before_immutable_open(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE t (a INTEGER)")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO t VALUES (7)")
        writer.commit()
        wal_path = db_path.with_name("state.db-wal")
        assert wal_path.exists()
        wal_path.unlink()
        wal_path.symlink_to(tmp_path / "missing-wal-target")

        with (
            pytest.raises(OSError, match="unsafe SQLite WAL sidecar"),
            _connect_readonly_sqlite(db_path) as ro,
        ):
            ro.execute("SELECT COUNT(*) FROM t").fetchone()
    finally:
        writer.close()
