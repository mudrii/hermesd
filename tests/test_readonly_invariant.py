"""Critical-rule guard: the collector must never write to ~/.hermes/.

These tests snapshot a recursive manifest of every file under a populated
~/.hermes (relative path + size + mtime), exercise the collector's full read
paths, then re-snapshot and assert the manifest is byte-for-byte identical. Any
file added, removed, or modified by a read would fail the read-only invariant.
"""

from __future__ import annotations

from pathlib import Path

from hermesd.app import DashboardApp
from hermesd.collector import Collector


def _manifest(root: Path) -> dict[str, tuple[int, int]]:
    """Map every file's path (relative to root) to (size_bytes, mtime_ns)."""
    manifest: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            manifest[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
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
