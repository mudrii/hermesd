"""Critical-rule guard: the collector must never write to ~/.hermes/.

These tests snapshot a recursive manifest of every entry under a populated
~/.hermes (relative path, type, link target, size, mtime), exercise the
collector's full read paths, then re-snapshot and assert the manifest is
byte-for-byte identical. Any entry added, removed, or modified by a read would
fail the read-only invariant.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermesd.app import DashboardApp
from hermesd.collector import Collector


def _manifest(root: Path) -> dict[str, tuple[str, str, int, int]]:
    """Map every entry's relative path to (kind, link_target, size_bytes, mtime_ns)."""
    manifest: dict[str, tuple[str, str, int, int]] = {}
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        if path.is_symlink():
            kind = "symlink"
            link_target = str(path.readlink())
        elif path.is_dir():
            kind = "dir"
            link_target = ""
        elif path.is_file():
            kind = "file"
            link_target = ""
        else:
            kind = "other"
            link_target = ""
        manifest[str(path.relative_to(root))] = (
            kind,
            link_target,
            stat.st_size,
            stat.st_mtime_ns,
        )
    return manifest


def test_collector_does_not_write_to_hermes_home(populated_hermes_home: Path):
    before = _manifest(populated_hermes_home)

    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    c.collect()
    c.collect()  # a second poll must not mutate anything either
    c.close()

    assert _manifest(populated_hermes_home) == before


def test_snapshot_read_paths_do_not_write_to_hermes_home(populated_hermes_home: Path):
    before = _manifest(populated_hermes_home)

    app = DashboardApp(populated_hermes_home, refresh_rate=5, no_color=True)
    app.render_snapshot()
    app.render_snapshot_text(10)
    app.render_snapshot_json()
    app.close()

    assert _manifest(populated_hermes_home) == before


def test_response_store_read_paths_do_not_write_to_hermes_home(hermes_home: Path):
    db_path = hermes_home / "response_store.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "PRAGMA journal_mode=WAL;"
        "CREATE TABLE conversations (id TEXT PRIMARY KEY);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY);"
        "INSERT INTO conversations VALUES ('c1');"
        "INSERT INTO responses VALUES ('r1');"
    )
    conn.commit()
    conn.close()
    before = _manifest(hermes_home)

    c = Collector(hermes_home)
    c.collect()
    c.close()
    app = DashboardApp(hermes_home, refresh_rate=5, no_color=True)
    app.render_snapshot_text(12)
    app.close()

    assert _manifest(hermes_home) == before
