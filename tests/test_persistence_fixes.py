from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

import yaml

from hermesd.collector import Collector
from hermesd.db import HermesDB
from hermesd.file_cache import LastGoodFileCache
from hermesd.models import DashboardState, RuntimeStatus
from tests.conftest import create_state_db_tables


def _clone_stat(result: os.stat_result, *, st_mtime: float, st_mtime_ns: int) -> os.stat_result:
    """Rebuild a stat result with a controlled mtime granularity.

    Simulates a filesystem whose float st_mtime has 1-second granularity while
    st_mtime_ns still distinguishes successive writes.
    """
    return os.stat_result(
        (
            result.st_mode,
            result.st_ino,
            result.st_dev,
            result.st_nlink,
            result.st_uid,
            result.st_gid,
            result.st_size,
            result.st_atime,
            st_mtime,
            result.st_ctime,
        ),
        {
            "st_atime_ns": result.st_atime_ns,
            "st_mtime_ns": st_mtime_ns,
            "st_ctime_ns": result.st_ctime_ns,
        },
    )


def _pin_coarse_mtime(monkeypatch, path: Path, reference: os.stat_result) -> None:
    """Force path.stat() to report reference's float mtime but a newer st_mtime_ns."""
    real_stat = Path.stat

    def fake_stat(self: Path, *args, **kwargs) -> os.stat_result:
        result = real_stat(self, *args, **kwargs)
        if self == path:
            return _clone_stat(
                result,
                st_mtime=reference.st_mtime,
                st_mtime_ns=reference.st_mtime_ns + 1_000_000,
            )
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)


def test_malformed_json_fixed_within_same_second_is_reloaded(tmp_path, monkeypatch):
    """A same-second fix to a malformed file must not be hidden by bad-mtime caching."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text("{ not valid json")
    assert cache.read_json_mapping(path) == {}
    reference = path.stat()

    path.write_text(json.dumps({"fixed": 1}))
    _pin_coarse_mtime(monkeypatch, path, reference)

    assert cache.read_json_mapping(path) == {"fixed": 1}


def test_malformed_yaml_fixed_within_same_second_is_reloaded(tmp_path, monkeypatch):
    cache = LastGoodFileCache()
    path = tmp_path / "config.yaml"
    path.write_text(":\n  - [unclosed")
    assert cache.read_yaml_mapping(path) == {}
    reference = path.stat()

    path.write_text(yaml.safe_dump({"fixed": 1}))
    _pin_coarse_mtime(monkeypatch, path, reference)

    assert cache.read_yaml_mapping(path) == {"fixed": 1}


def test_valid_json_changed_within_same_second_is_reloaded(tmp_path, monkeypatch):
    """Same-second valid-content changes must invalidate the cached value too."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"v": 1}))
    assert cache.read_json_mapping(path) == {"v": 1}
    reference = path.stat()

    path.write_text(json.dumps({"v": 2}))
    _pin_coarse_mtime(monkeypatch, path, reference)

    assert cache.read_json_mapping(path) == {"v": 2}


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
    import threading
    import time

    db_file = tmp_path / "state.db"
    seed = sqlite3.connect(db_file)
    seed.execute("CREATE TABLE t (x INTEGER)")
    seed.close()

    db = HermesDB(db_file)
    assert db._conn is not None
    started = threading.Event()
    outcome: dict[str, str] = {}

    def run_query() -> None:
        started.set()
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
    time.sleep(0.1)  # let the recursive CTE get going
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
    """interrupt() on a closed HermesDB must not raise."""
    db_file = tmp_path / "state.db"
    seed = sqlite3.connect(db_file)
    seed.execute("CREATE TABLE t (x INTEGER)")
    seed.close()

    db = HermesDB(db_file)
    db.close()
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
