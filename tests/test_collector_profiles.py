"""Profile-scoped collection: discovery, path containment, session counts,
soul excerpts, per-source scope ownership, and the rule document that pins it."""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermesd.collector import (
    Collector,
    _read_soul_excerpt,
)
from hermesd.models import GatewayLoopHealth, LogStream, OperationsState, SourceScope
from hermesd.paths import HermesPaths
from tests.conftest import (
    _assert_cached_until_changed,
    _count_opens,
    _skip_if_root,
    _unreadable,
    _write_minimal_state_db,
    create_kanban_db_tables,
    create_session_coordination_tables,
    create_state_db_tables,
    insert_turn_lease,
)
from tests.test_collector_operations import (
    create_projects_db_tables,
    create_verification_evidence_db_tables,
)


def test_summarize_profile_uses_session_count_reader(hermes_home: Path):
    profile_home = hermes_home / "profiles" / "coding"
    profile_home.mkdir(parents=True)
    (profile_home / "state.db").touch()

    class FakeHermesDB:
        def __init__(self, db_path: Path):
            self.db_path = db_path

        def read_session_count(self) -> int:
            return 7

        def read_sessions(self) -> list[dict[str, object]]:
            raise AssertionError("read_sessions() should not be used for profile counts")

        def close(self) -> None:
            return None

    collector = Collector(hermes_home, db_factory=FakeHermesDB)
    summary = collector._summarize_profile("coding", profile_home)

    assert summary.session_count == 7
    collector.close()


def test_summarize_profile_caches_session_count_by_db_mtime(hermes_home: Path):
    profile_home = hermes_home / "profiles" / "coding"
    profile_home.mkdir(parents=True)
    db_file = profile_home / "state.db"
    db_file.touch()

    constructed: list[Path] = []

    class CountingFakeDB:
        def __init__(self, db_path: Path):
            constructed.append(db_path)

        def read_session_count(self) -> int:
            return 7

        def close(self) -> None:
            return None

    c = Collector(hermes_home, db_factory=CountingFakeDB)
    baseline = len(constructed)  # Collector.__init__ constructs the root DB

    first = c._summarize_profile("coding", profile_home)
    second = c._summarize_profile("coding", profile_home)
    assert first.session_count == 7
    assert second.session_count == 7
    assert len(constructed) == baseline + 1  # unchanged mtime -> no new DB open

    bumped = time.time() + 10
    os.utime(db_file, (bumped, bumped))
    third = c._summarize_profile("coding", profile_home)
    assert third.session_count == 7
    assert len(constructed) == baseline + 2

    # WAL-only write: main db mtime unchanged, -wal mtime bumps -> must invalidate.
    wal_file = profile_home / "state.db-wal"
    wal_file.touch()
    wal_bumped = time.time() + 20
    os.utime(wal_file, (wal_bumped, wal_bumped))
    fourth = c._summarize_profile("coding", profile_home)
    assert fourth.session_count == 7
    assert len(constructed) == baseline + 3
    c.close()


def test_collect_profiles_preserves_last_good_when_profile_db_read_fails(hermes_home: Path):
    profile_home = hermes_home / "profiles" / "coding"
    profile_home.mkdir(parents=True)
    db_file = profile_home / "state.db"
    db_file.touch()

    class FlakyFakeDB:
        fail = False

        def __init__(self, db_path: Path):
            self.db_path = db_path

        def read_sessions(self) -> list[dict[str, object]]:
            return []

        def read_tool_stats(self) -> list[dict[str, object]]:
            return []

        def read_model_usage(self, now: float) -> dict[str, list[dict[str, object]]]:
            return {"all": [], "24h": [], "7d": []}

        def read_session_count(self) -> int:
            if FlakyFakeDB.fail:
                raise RuntimeError("profile db unavailable")
            return 7

        @property
        def last_read_sessions_stale(self) -> bool:
            return False

        @property
        def last_read_tool_stats_stale(self) -> bool:
            return False

        @property
        def last_read_model_usage_stale(self) -> bool:
            return False

        def close(self) -> None:
            return None

    c = Collector(hermes_home, db_factory=FlakyFakeDB)
    state1 = c.collect()
    assert state1.profiles.profiles[0].session_count == 7

    FlakyFakeDB.fail = True
    bumped = time.time() + 10
    os.utime(db_file, (bumped, bumped))
    state2 = c.collect()

    assert state2.profiles == state1.profiles
    assert "profiles" in state2.health.failed_sources
    c.close()


def test_collect_profiles_preserves_last_good_when_real_profile_db_becomes_unreadable(
    profiled_hermes_home: Path,
):
    c = Collector(profiled_hermes_home)
    state1 = c.collect()
    assert state1.profiles.profiles[0].session_count == 1

    profile_db = profiled_hermes_home / "profiles" / "coding" / "state.db"
    profile_db.write_bytes(b"not a sqlite database")
    state2 = c.collect()

    assert state2.profiles == state1.profiles
    assert "profiles" in state2.health.failed_sources
    c.close()


def test_collect_profiles_preserves_cached_count_when_profile_db_disappears(
    profiled_hermes_home: Path,
):
    c = Collector(profiled_hermes_home)
    state1 = c.collect()
    assert state1.profiles.profiles[0].session_count == 1

    profile_db = profiled_hermes_home / "profiles" / "coding" / "state.db"
    profile_db.unlink()
    state2 = c.collect()

    assert state2.profiles.profiles[0].session_count == 1
    c.close()


def test_summarize_profile_ignores_symlinked_children_outside_profile(
    hermes_home: Path, tmp_path: Path
):
    profile_home = hermes_home / "profiles" / "coding"
    profile_home.mkdir(parents=True)
    outside_db = tmp_path / "state.db"
    outside_db.touch()
    outside_soul = tmp_path / "SOUL.md"
    outside_soul.write_text("outside soul\n")
    (profile_home / "state.db").symlink_to(outside_db)
    (profile_home / "SOUL.md").symlink_to(outside_soul)

    c = Collector(hermes_home)
    summary = c._summarize_profile("coding", profile_home)

    assert summary.session_count == 0
    assert summary.db_size_bytes == 0
    assert summary.soul_excerpt == ""
    c.close()


def test_collect_profiles_preserves_last_good_when_profile_child_becomes_unsafe_symlink(
    profiled_hermes_home: Path, tmp_path: Path
):
    profile_home = profiled_hermes_home / "profiles" / "coding"
    outside_soul = tmp_path / "SOUL.md"
    outside_soul.write_text("outside soul\n")

    c = Collector(profiled_hermes_home)
    first = c.collect()
    assert first.profiles.profiles[0].session_count == 1

    soul_path = profile_home / "SOUL.md"
    soul_path.write_text("safe soul\n")
    second = c.collect()
    assert second.profiles.profiles[0].soul_excerpt == "safe soul"

    soul_path.unlink()
    soul_path.symlink_to(outside_soul)
    third = c.collect()

    assert third.profiles == second.profiles
    assert "profiles" in third.health.failed_sources
    c.close()


def test_collect_profiles_empty_when_no_profiles_dir(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.profiles.profile_count == 0
    assert state.profiles.profiles == []
    c.close()


def test_collect_profiles_lists_profile_directories(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home)
    state = c.collect()
    assert state.profiles.profile_count == 1
    profile = state.profiles.profiles[0]
    assert profile.name == "coding"
    assert profile.session_count == 1
    assert profile.skill_count == 1
    assert profile.db_size_bytes > 0
    assert profile.soul_excerpt == ""
    assert profile.latest_log_mtime is not None
    c.close()


def test_collect_profiles_updates_session_count_after_profile_db_changes(
    profiled_hermes_home: Path,
):
    c = Collector(profiled_hermes_home)
    first = c.collect()
    assert first.sessions[0].session_id == "root_session"
    assert first.profiles.profiles[0].session_count == 1

    profile_db = profiled_hermes_home / "profiles" / "coding" / "state.db"
    conn = sqlite3.connect(str(profile_db))
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        ("profile_session_2", "profile", time.time()),
    )
    conn.commit()
    conn.close()

    second = c.collect()

    assert second.sessions[0].session_id == "root_session"
    assert second.profiles.profiles[0].session_count == 2
    c.close()


def test_collect_profiles_updates_session_count_after_profile_wal_commit(
    profiled_hermes_home: Path,
):
    profile_db = profiled_hermes_home / "profiles" / "coding" / "state.db"
    writer = sqlite3.connect(str(profile_db))
    writer.execute("PRAGMA journal_mode=WAL")

    c = Collector(profiled_hermes_home)
    try:
        first = c.collect()
        assert first.profiles.profiles[0].session_count == 1

        writer.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("profile_session_2", "profile", time.time()),
        )
        writer.commit()

        second = c.collect()
        assert second.sessions[0].session_id == "root_session"
        assert second.profiles.profiles[0].session_count == 2
    finally:
        c.close()
        writer.close()


def test_collect_profiles_reads_soul_excerpt_when_present(profiled_hermes_home: Path):
    soul = profiled_hermes_home / "profiles" / "coding" / "SOUL.md"
    soul.write_text("Profile soul line one\nProfile soul line two\n")
    c = Collector(profiled_hermes_home)
    state = c.collect()
    assert state.profiles.profiles[0].soul_excerpt == "Profile soul line one"
    c.close()


@_skip_if_root
def test_read_soul_excerpt_oserror_returns_empty(tmp_path: Path):
    f = tmp_path / "SOUL.md"
    f.write_text("Remember the operator.")
    os.chmod(f, 0o000)
    try:
        if not _unreadable(f):
            pytest.skip("filesystem allowed read despite chmod 000")
        assert _read_soul_excerpt(f) == ""
    finally:
        os.chmod(f, 0o644)


def test_read_soul_excerpt_whitespace_only_returns_empty(tmp_path: Path):
    # File is present and readable but every line is blank: the loop finds no
    # non-empty line and falls through to the empty-string result.
    f = tmp_path / "SOUL.md"
    f.write_text("\n   \n\t\n")
    assert _read_soul_excerpt(f) == ""


def test_soul_excerpt_symlinked_outside_hermes_home_is_ignored(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside-soul.md"
    outside.write_text("secret soul line\n")
    (hermes_home / "SOUL.md").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.memory.soul_excerpt == ""


def test_soul_md_is_cached_until_it_changes(
    hermes_home: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
):
    soul = hermes_home / "SOUL.md"
    soul.write_text("curious and precise\n")
    opens = _count_opens(monkeypatch, soul)

    _assert_cached_until_changed(
        collector,
        opens,
        soul,
        lambda: soul.write_text("terse and exact\n"),
    )
    assert collector.collect().memory.soul_excerpt == "terse and exact"


def test_default_collector_ignores_active_profile_file(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home)
    state = c.collect()
    assert state.selected_profile is None
    assert state.profile_mode_label == "root"
    assert state.sessions[0].session_id == "root_session"
    assert state.available_tool_names == ["root_tool"]
    assert state.logs.agent_lines[0].message == "root agent log"
    c.close()


def test_profiled_collector_reads_profile_scoped_runtime_data(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home, profile_name="coding")
    state = c.collect()
    assert state.selected_profile == "coding"
    assert state.profile_mode_label == "profile:coding"
    assert state.sessions[0].session_id == "profile_session"
    assert state.available_tool_names == ["profile_tool"]
    assert state.logs.agent_lines[0].message == "profile agent log"
    c.close()


def test_profiled_collector_rejects_profile_root_swapped_to_outside(
    profiled_hermes_home: Path, tmp_path: Path
):
    c = Collector(profiled_hermes_home, profile_name="coding")
    first = c.collect()
    assert first.available_tool_names == ["profile_tool"]

    profile_home = profiled_hermes_home / "profiles" / "coding"
    original_profile = profiled_hermes_home / "profiles" / "coding-original"
    profile_home.rename(original_profile)
    outside = tmp_path / "outside-profile"
    sessions = outside / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "sessions.json").write_text('{"x": {"session_id": "outside"}}')
    (sessions / "session_outside.json").write_text('{"tools": [{"name": "outside_secret_tool"}]}')
    profile_home.symlink_to(outside, target_is_directory=True)

    try:
        second = c.collect()
    finally:
        c.close()

    assert second.available_tool_names == ["profile_tool"]
    assert "outside_secret_tool" not in second.available_tool_names
    assert "tools_index" in second.health.failed_sources


def test_plugin_catalog_cache_is_read_from_the_shared_root_under_a_profile(
    profiled_hermes_home: Path,
):
    """The live-catalog cache is ROOT like the plugins/ directory it describes.

    Comparing a root-installed plugin's sidecar sha against a profile-local
    cache would manufacture drift, so both sides of the comparison must come
    from one home.
    """
    root_cache = profiled_hermes_home / "cache"
    root_cache.mkdir(exist_ok=True)
    (root_cache / "plugin-catalog.json").write_text(
        json.dumps(
            {
                "entries": [{"name": "root-weather", "sha": "a" * 40}],
                "removed": [{"name": "root-evil", "reason": "malicious"}],
            }
        )
    )
    profile_cache = profiled_hermes_home / "profiles" / "coding" / "cache"
    profile_cache.mkdir(parents=True)
    (profile_cache / "plugin-catalog.json").write_text(json.dumps({"entries": [], "removed": []}))
    plugin_dir = profiled_hermes_home / "plugins" / "root-weather"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text("name: root-weather\n")
    (plugin_dir / ".hermes-catalog.json").write_text(
        json.dumps({"catalog_name": "root-weather", "sha": "b" * 40})
    )

    c = Collector(profiled_hermes_home, profile_name="coding")
    try:
        state = c.collect()
    finally:
        c.close()

    sm = state.skills_memory
    assert sm.plugin_catalog_cache_present is True
    assert sm.plugin_catalog_update_count == 1
    assert sm.plugin_catalog_removed_count == 0
    assert sm.plugins[0].catalog_update_available is True
    c.close()


def test_profiled_collector_keeps_config_backups_on_the_shared_root(profiled_hermes_home: Path):
    """backups/config/ inherits the config.yaml decision.

    The copies are point-in-time snapshots of the same root config.yaml hermesd
    reads, so reporting a profile-local backups directory would date a config
    the dashboard never displays.
    """
    root_backups = profiled_hermes_home / "backups" / "config"
    root_backups.mkdir(parents=True)
    (root_backups / "config.yaml.good.20260907-143000").write_text("model: root\n")
    profile_backups = profiled_hermes_home / "profiles" / "coding" / "backups" / "config"
    profile_backups.mkdir(parents=True)
    (profile_backups / "config.yaml.good.20260908-150000").write_text("model: profile\n")

    c = Collector(profiled_hermes_home, profile_name="coding")
    try:
        state = c.collect()
    finally:
        c.close()

    good = next(group for group in state.config.config_backup_groups if group.kind == "good")
    assert good.newest_stamp == "20260907-143000"
    assert good.count == 1
    assert state.config.config_backups_present is True
    c.close()


def test_profiled_collector_keeps_shared_root_config_and_auth(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home, profile_name="coding")
    state = c.collect()
    assert state.config.model == "root-model"
    assert state.config.provider == "root-provider"
    assert [provider.name for provider in state.skills_memory.providers] == [
        "backup-provider",
        "root-provider",
    ]
    c.close()


def test_profiled_collector_reads_profile_scoped_skills(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home, profile_name="coding")
    state = c.collect()
    assert state.skills_memory.skill_count == 1
    assert state.skills_memory.skills[0].name == "profile-skill"
    c.close()


def test_collector_rejects_missing_profile(profiled_hermes_home: Path):
    with pytest.raises(ValueError, match="Profile 'missing' does not exist"):
        Collector(profiled_hermes_home, profile_name="missing")


@pytest.mark.parametrize("profile_name", ["../coding", "coding/../root", ".", "..", ""])
def test_collector_rejects_invalid_profile_names(profiled_hermes_home: Path, profile_name: str):
    with pytest.raises(ValueError, match="Invalid profile name"):
        Collector(profiled_hermes_home, profile_name=profile_name)


def test_default_collector_ignores_discovered_symlinked_profile_outside_home(
    hermes_home: Path,
):
    outside = hermes_home.parent / "outside-profile"
    outside.mkdir()
    conn = sqlite3.connect(str(outside / "state.db"))
    create_state_db_tables(conn)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('outside_session', 'cli', 1)"
    )
    conn.commit()
    conn.close()
    (outside / "SOUL.md").write_text("outside profile soul")
    profiles_dir = hermes_home / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "escaped").symlink_to(outside, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.profiles.profiles == []
    c.close()


def test_default_collector_ignores_symlinked_profiles_root_outside_home(
    hermes_home: Path,
):
    outside_profiles = hermes_home.parent / "outside-profiles"
    outside = outside_profiles / "escaped"
    outside.mkdir(parents=True)
    conn = sqlite3.connect(str(outside / "state.db"))
    create_state_db_tables(conn)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('outside_session', 'cli', 1)"
    )
    conn.commit()
    conn.close()
    (outside / "SOUL.md").write_text("outside profile soul")
    (hermes_home / "profiles").symlink_to(outside_profiles, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.profiles.profiles == []
    c.close()


def test_collector_rejects_profile_traversal_outside_profiles_dir(hermes_home: Path):
    outside = hermes_home.parent / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="Invalid profile name"):
        Collector(hermes_home, profile_name="../../outside")


# ---------------------------------------------------------------------------
# Source-scope ownership: which resolver owns which on-disk source.
# See .codex/rules/source-ownership.md — the document is checked against the
# code by test_source_ownership_doc_matches_resolver_call_sites below.
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_ROOT = _PROJECT_ROOT / "hermesd"
_RULE_FILE = _PROJECT_ROOT / ".codex" / "rules" / "source-ownership.md"
_RESOLVERS = frozenset({"shared_path", "profile_path"})
_SCOPES = frozenset({"ROOT", "PROFILE", "MIXED"})
_DERIVED_MARKER = "(derived)"
# state.db is resolved exactly once, in Collector.__init__, and every later read
# goes through the HermesDB it built there. The two sources that consume those
# rows therefore need __init__ in their walk to see the resolver call at all.
_EXTRA_ENTRYPOINTS: dict[str, tuple[str, ...]] = {
    "sessions": ("Collector.__init__",),
    "session_models": ("Collector.__init__",),
}


def _write_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"2026-04-09 15:41:58,123 - hermes - INFO - {message}\n")


def _make_projects_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        create_projects_db_tables(conn, optional=False)
        conn.execute(
            "INSERT INTO projects VALUES ("
            "'p1', 'other', 'Other', '', '', '', '', '/repo/other',"
            " '2026-07-10T00:00:00Z', 0)"
        )
        conn.commit()
    finally:
        conn.close()


def _make_verification_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        create_verification_evidence_db_tables(conn)
        conn.commit()
    finally:
        conn.close()


def test_profiled_collector_labels_log_stream_scope(profiled_hermes_home: Path):
    profile_logs = profiled_hermes_home / "profiles" / "coding" / "logs"
    root_logs = profiled_hermes_home / "logs"
    _write_log(profile_logs / "gateway.log", "profile gateway log")
    _write_log(profile_logs / "errors.log", "profile error log")
    _write_log(root_logs / "desktop.log", "root desktop log")
    _write_log(root_logs / "gui.log", "root gui log")
    # Same base name in both scopes: the profile-owned stream must stay
    # profile-scoped and must be the one that was read.
    _write_log(root_logs / "gateway.log", "root gateway log")
    cron_output = profiled_hermes_home / "cron" / "output" / "job-1"
    cron_output.mkdir(parents=True)
    (cron_output / "latest.md").write_text("root cron output\n")

    c = Collector(profiled_hermes_home, profile_name="coding")
    try:
        state = c.collect()
    finally:
        c.close()

    scopes = {stream.name: stream.scope for stream in state.logs.streams}
    assert scopes["agent"] is SourceScope.PROFILE
    assert scopes["gateway"] is SourceScope.PROFILE
    assert scopes["errors"] is SourceScope.PROFILE
    assert scopes["desktop"] is SourceScope.ROOT
    assert scopes["gui"] is SourceScope.ROOT
    assert scopes["cron"] is SourceScope.ROOT
    gateway = next(stream for stream in state.logs.streams if stream.name == "gateway")
    assert gateway.lines[0].message == "profile gateway log"


def test_root_collector_keeps_ownership_labels_on_log_streams(profiled_hermes_home: Path):
    """Scope names the resolver that owns a stream, not the dir it resolved to.

    In root mode ``profile_home is root_home``, so a profile-owned stream reads
    the root copy — but it must still be labeled PROFILE, otherwise the label
    would stop telling an operator where the file lives once a profile is
    selected.
    """
    _write_log(profiled_hermes_home / "logs" / "desktop.log", "root desktop log")

    c = Collector(profiled_hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    scopes = {stream.name: stream.scope for stream in state.logs.streams}
    assert scopes["agent"] is SourceScope.PROFILE
    assert scopes["desktop"] is SourceScope.ROOT
    assert state.logs.agent_lines[0].message == "root agent log"


def test_log_stream_scope_defaults_to_root():
    assert LogStream(name="agent").scope is SourceScope.ROOT


def test_profiled_collector_reads_root_scoped_cron_kanban_and_gateway(
    profiled_hermes_home: Path,
):
    """Root-owned sources keep reading the root copy while a profile is selected.

    Extends the config.yaml/auth.json pin to the three root-owned sources an
    operator is most likely to mistake for profile data: cron (upstream scopes
    it per profile), kanban (root BY DESIGN upstream) and the gateway pid/state
    record (root-anchored for the multiplexed default gateway).
    """
    home = profiled_hermes_home
    profile_home = home / "profiles" / "coding"

    (home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "root_job", "name": "root-job"}]})
    )
    profile_cron = profile_home / "cron"
    profile_cron.mkdir(parents=True)
    (profile_cron / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "profile_job", "name": "profile-job"}]})
    )

    root_kanban = sqlite3.connect(str(home / "kanban.db"))
    create_kanban_db_tables(root_kanban)
    root_kanban.execute(
        "INSERT INTO tasks (id, title, status, created_at, started_at) VALUES (?, ?, ?, ?, ?)",
        ("t_root", "root task", "in_progress", int(time.time()), int(time.time())),
    )
    root_kanban.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at, last_event_id) "
        "VALUES ('t_root', 'discord', 'chat-root', 1, 0)"
    )
    root_kanban.commit()
    root_kanban.close()
    profile_kanban = sqlite3.connect(str(profile_home / "kanban.db"))
    create_kanban_db_tables(profile_kanban)
    profile_kanban.execute(
        "INSERT INTO tasks (id, title, status, created_at, started_at) VALUES (?, ?, ?, ?, ?)",
        ("t_profile", "profile task", "in_progress", int(time.time()), int(time.time())),
    )
    profile_kanban.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at, last_event_id) "
        "VALUES ('t_profile', 'slack', 'chat-profile', 1, 0)"
    )
    profile_kanban.commit()
    profile_kanban.close()

    (profile_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 999,
                "gateway_state": "stopped",
                "platforms": {"discord": {"state": "connected", "updated_at": ""}},
            }
        )
    )

    c = Collector(home, profile_name="coding")
    try:
        state = c.collect()
    finally:
        c.close()

    assert [job.name for job in state.cron.jobs] == ["root-job"]
    assert state.cron.job_count == 1
    assert state.kanban.db_present is True
    assert state.kanban.task_count == 1
    assert [task.task_id for task in state.kanban.active_tasks] == ["t_root"]
    # kanban_notify_subs ride the same root store: the root sub is reported and
    # the profile copy next to it is invisible, exactly like the board itself.
    assert state.kanban.notify_sub_count == 1
    assert state.kanban.notify_platform_counts == {"discord": 1}
    # gateway_state.json comes from the root copy written by the fixture.
    assert state.gateway.pid == 12345
    assert state.gateway.state == "running"
    assert [platform.name for platform in state.gateway.platforms] == ["telegram"]
    # The profile-scoped runtime data in the same home is still profile-scoped.
    assert state.sessions[0].session_id == "profile_session"


def test_profiled_collector_reads_the_root_migration_manifest(
    profiled_hermes_home: Path,
):
    """The migration manifest belongs to the DEFAULT home, never to a secondary's.

    Upstream anchors it at ``get_default_hermes_root()``
    (``gateway_migrate.py:144-146`` + ``_manifest_path`` ``:467-468``), and a served
    profile owns no ``gateway_state.json`` of its own, so under ``--profile coding``
    the root manifest is the only one that can exist. A copy dropped inside the
    profile home must be ignored rather than read.
    """
    home = profiled_hermes_home
    profile_home = home / "profiles" / "coding"
    manifest = {
        "version": 1,
        "migrated_at": "2026-09-13T00:52:11+0200",
        "flag_was": False,
        "default": {"profile": "default", "home": str(home), "pid": 12345, "service": None},
        "secondaries": [
            {"profile": "coding", "home": str(profile_home), "pid": None, "service": None}
        ],
    }
    (home / "gateway_migration.json").write_text(json.dumps(manifest))
    (profile_home / "gateway_migration.json").write_text(
        json.dumps({**manifest, "migrated_at": "2020-01-01T00:00:00+0000"})
    )
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n")
    (home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 12345,
                "gateway_state": "running",
                "served_profiles": ["default", "coding"],
                "platforms": {},
            }
        )
    )

    c = Collector(home, profile_name="coding", pid_exists=lambda pid: pid == 12345)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.migration.manifest_present is True
    assert state.migration.migrated_at == "2026-09-13T00:52:11+0200"
    assert state.migration.multiplex_flag_on is True
    assert state.migration.migration_verified is True
    assert "migration" not in state.health.failed_sources


def test_profiled_collector_ignores_a_profile_local_migration_manifest(
    profiled_hermes_home: Path,
):
    """Only the root manifest counts: a profile-local copy is not a migration record."""
    home = profiled_hermes_home
    profile_home = home / "profiles" / "coding"
    (profile_home / "gateway_migration.json").write_text(
        json.dumps({"version": 1, "migrated_at": "2026-09-13T00:52:11+0200", "flag_was": False})
    )

    c = Collector(home, profile_name="coding", pid_exists=lambda pid: pid == 12345)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.migration.manifest_present is False
    assert state.migration.migration_verified is False
    assert "migration" not in state.health.failed_sources


def test_profiled_collector_does_not_read_root_scoped_profile_sources(
    profiled_hermes_home: Path,
):
    """A profile-owned source must not fall back to the root copy."""
    home = profiled_hermes_home
    (home / "memories" / "ROOT.md").write_text("root memory\n")
    (home / "checkpoints").mkdir()
    (home / "runtime").mkdir()
    (home / "runtime" / "active_sessions.json").write_text(
        json.dumps({"entries": [{"session_id": "root_surface", "surface": "cli", "pid": 0}]})
    )
    profile_runtime = home / "profiles" / "coding" / "runtime"
    profile_runtime.mkdir(parents=True)
    (profile_runtime / "active_sessions.json").write_text(
        json.dumps({"entries": [{"session_id": "profile_surface", "surface": "cli", "pid": 0}]})
    )

    c = Collector(home, profile_name="coding")
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.memory.memory_files == ["PROFILE.md"]
    assert state.memory.memory_file_count == 1
    assert state.checkpoints == []
    assert [surface.session_id for surface in state.active_surfaces] == ["profile_surface"]
    assert [skill.name for skill in state.skills_memory.skills] == ["profile-skill"]
    assert state.logs.agent_lines[0].message == "profile agent log"


class _CrossProfilePaths(HermesPaths):
    """Resolver that hands back a *sibling* profile's paths.

    Stands in for the failure mode the confinement check exists to catch: a
    ``profile_path()`` result that is still under ``root_home`` but belongs to
    another profile. Confining against ``root_home`` accepts it; confining
    against ``profile_home`` does not.
    """

    def profile_path(self, *parts: str) -> Path:
        return self.root_home.joinpath("profiles", "other", *parts)


def _make_sibling_profile(home: Path) -> Path:
    other = home / "profiles" / "other"
    other.mkdir(parents=True)
    _make_projects_db(other / "projects.db")
    _make_verification_db(other / "verification_evidence.db")
    _write_minimal_state_db(other / "state.db", "other_session", "other")
    return other


def test_profiled_operations_readers_confine_to_selected_profile_home(
    profiled_hermes_home: Path,
):
    """The three profile_path()-resolved operations readers confine to profile_home."""
    _make_sibling_profile(profiled_hermes_home)
    c = Collector(profiled_hermes_home, profile_name="coding")
    try:
        c._paths = _CrossProfilePaths(profiled_hermes_home, "coding")
        operations = OperationsState()
        assert c._with_projects(operations).projects_db_present is False
        assert c._with_verification_evidence(operations).verification_db_present is False
        assert c._read_state_db() is None
    finally:
        c.close()


def test_profiled_db_recovery_reads_the_selected_profile_home(
    profiled_hermes_home: Path,
):
    """state.db's recovery artifacts are profile-scoped, like the database itself.

    Upstream repairs ``get_hermes_home()/"state.db"`` (``hermes_state.py:160``,
    repair invoked at ``:535``) and writes every artifact as a sibling of it, so a
    ledger at the root is the root home's evidence and must not be reported for a
    selected profile — and vice versa.
    """
    ledger = json.dumps({"failed_attempts": 3, "last_attempt": "2026-09-12T21:48:03"})
    (profiled_hermes_home / "state.db.repair-attempts.json").write_text(ledger)
    profile_home = profiled_hermes_home / "profiles" / "coding"
    (profile_home / "state.db.repair-attempts.json").write_text(
        json.dumps({"failed_attempts": 1, "last_attempt": "2026-09-12T21:48:03"})
    )

    profiled = Collector(profiled_hermes_home, profile_name="coding")
    try:
        profiled_recovery = profiled._with_db_recovery(OperationsState()).db_recovery
    finally:
        profiled.close()

    rooted = Collector(profiled_hermes_home)
    try:
        root_recovery = rooted._with_db_recovery(OperationsState()).db_recovery
    finally:
        rooted.close()

    assert profiled_recovery.repair_ledger_present is True
    assert profiled_recovery.failed_attempts == 1
    assert profiled_recovery.repair_budget_exhausted is False
    assert root_recovery.failed_attempts == 3
    assert root_recovery.repair_budget_exhausted is True


def test_profiled_db_recovery_refuses_a_cross_profile_symlink(
    profiled_hermes_home: Path,
):
    """A ledger symlinked in from a sibling profile is refused, not followed."""
    other = _make_sibling_profile(profiled_hermes_home)
    (other / "state.db.repair-attempts.json").write_text(
        json.dumps({"failed_attempts": 3, "last_attempt": "2026-09-12T21:48:03"})
    )
    (profiled_hermes_home / "profiles" / "coding" / "state.db.repair-attempts.json").symlink_to(
        other / "state.db.repair-attempts.json"
    )

    c = Collector(profiled_hermes_home, profile_name="coding")
    try:
        recovery = c._with_db_recovery(OperationsState()).db_recovery
    finally:
        c.close()

    assert recovery.repair_ledger_present is False
    assert recovery.failed_attempts == 0


def test_process_receipts_are_profile_scoped_under_a_profile(
    profiled_hermes_home: Path,
):
    """Receipts follow upstream's get_hermes_home() anchor: PROFILE, not ROOT.

    The writer resolves ``get_hermes_home()/"logs"/"process-results"``
    (``tools/process_registry_results.py:30,58``) — the same home as the
    registry checkpoint the ownership table already records. Under a selected
    profile, root-mode receipts are invisible, exactly like every PROFILE row.
    """
    import time as _time

    def _write_receipt(base: Path, process_id: str) -> None:
        receipts_dir = base / "logs" / "process-results"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        (receipts_dir / f"{process_id}.json").write_text(
            json.dumps(
                {
                    "id": process_id,
                    "command": "sleep 5",
                    "exit_code": 0,
                    "started_at": _time.time() - 60,
                }
            )
        )

    profile_home = profiled_hermes_home / "profiles" / "coding"
    _write_receipt(profiled_hermes_home, "proc_root_receipt")
    _write_receipt(profile_home, "proc_profile_receipt")

    profiled = Collector(profiled_hermes_home, profile_name="coding")
    try:
        profiled_receipts = profiled.collect().operations.process_receipts
    finally:
        profiled.close()
    assert profiled_receipts.dir_present is True
    assert [r.process_id for r in profiled_receipts.receipts] == ["proc_profile_receipt"]

    rooted = Collector(profiled_hermes_home)
    try:
        root_receipts = rooted.collect().operations.process_receipts
    finally:
        rooted.close()
    assert [r.process_id for r in root_receipts.receipts] == ["proc_root_receipt"]


def test_delegation_live_is_root_scoped_under_a_profile(profiled_hermes_home: Path):
    """Behaviour pin: live manifests read the ROOT cache/delegation/live copy.

    hermesd keeps the whole delegation cluster (count inside `operations`, the
    `delegation_live` source here) on the root resolver, an open divergence from
    upstream's profile-safe ``live_transcript_root()``
    (``tools/delegation_live_log.py:40-43``). The pin records what the code does
    so a change is deliberate, not an endorsement.
    """

    def _write_manifest(base: Path, delegation_id: str) -> None:
        run_dir = base / "cache" / "delegation" / "live" / delegation_id
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "delegation_id": delegation_id,
                    "task_count": 1,
                    "model": "m",
                    "provider": "p",
                    "tasks": [{"index": 0, "goal": "g", "status": "running"}],
                }
            )
        )

    profile_home = profiled_hermes_home / "profiles" / "coding"
    _write_manifest(profiled_hermes_home, "deleg_root01")
    _write_manifest(profile_home, "deleg_prof01")

    c = Collector(profiled_hermes_home, profile_name="coding")
    try:
        state = c.collect()
    finally:
        c.close()
    assert "delegation_live" not in state.health.failed_sources
    assert [m.delegation_id for m in state.operations.delegation_live_manifests] == ["deleg_root01"]
    assert state.operations.delegation_live_manifest_count == 1


def test_root_mode_operations_confinement_is_unchanged(hermes_home: Path):
    """Root mode confines against root_home because profile_home *is* root_home."""
    assert HermesPaths(hermes_home).profile_home == hermes_home
    _make_projects_db(hermes_home / "projects.db")
    _make_verification_db(hermes_home / "verification_evidence.db")

    c = Collector(hermes_home)
    try:
        operations = OperationsState()
        assert c._with_projects(operations).projects_db_present is True
        assert c._with_verification_evidence(operations).verification_db_present is True
    finally:
        c.close()


def test_profiled_collector_rejects_cross_profile_symlinked_projects_db(
    profiled_hermes_home: Path,
):
    """A symlink into a sibling profile is refused, not followed."""
    other = _make_sibling_profile(profiled_hermes_home)
    linked = profiled_hermes_home / "profiles" / "coding" / "projects.db"
    linked.symlink_to(other / "projects.db")

    c = Collector(profiled_hermes_home, profile_name="coding")
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.operations.projects_db_present is False
    assert state.operations.project_count == 0


def test_profiled_collector_reports_cross_profile_symlink_as_a_lost_source(
    profiled_hermes_home: Path,
):
    """Once read successfully, a cross-profile swap fails the source (last-good kept)."""
    coding = profiled_hermes_home / "profiles" / "coding"
    _make_projects_db(coding / "projects.db")
    other = _make_sibling_profile(profiled_hermes_home)

    c = Collector(profiled_hermes_home, profile_name="coding")
    try:
        first = c.collect()
        assert first.operations.projects_db_present is True

        good_db = coding / "projects.db"
        good_db.unlink()
        good_db.symlink_to(other / "projects.db")
        second = c.collect()
    finally:
        c.close()

    assert second.operations == first.operations
    assert "operations" in second.health.failed_sources


# ---------------------------------------------------------------------------
# Doc-is-truth: the ownership table must match the resolver call sites.
# ---------------------------------------------------------------------------


def _parsed_modules() -> dict[str, ast.AST]:
    sources = [_PACKAGE_ROOT / "collector.py", *sorted((_PACKAGE_ROOT / "collect").glob("*.py"))]
    assert sources, "expected to find collector sources to scan"
    return {
        str(path.relative_to(_PROJECT_ROOT)): ast.parse(path.read_text(), filename=str(path))
        for path in sources
    }


def _function_index(trees: dict[str, ast.AST]) -> dict[str, list[ast.AST]]:
    """Callable name -> body.

    Module-level functions are indexed bare; methods are indexed *only* as
    ``Class.method``. Keeping the two apart is what stops an attribute call on
    some other object (``health.collect(...)``) from resolving to an unrelated
    method (``Collector.collect``) and dragging the whole class into the walk.
    """
    index: dict[str, list[ast.AST]] = {}
    for tree in trees.values():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(node.name, []).append(node)
            elif isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index.setdefault(f"{node.name}.{child.name}", []).append(child)
    return index


def _resolve(index: dict[str, list[ast.AST]], name: str) -> list[ast.AST]:
    if name.startswith("self."):
        suffix = f".{name.removeprefix('self.')}"
        return [node for key, nodes in index.items() if key.endswith(suffix) for node in nodes]
    return index.get(name, [])


def _called_names(node: ast.AST) -> set[str]:
    """Callees this index can resolve: bare names and ``self.<method>``."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            names.add(f"self.{func.attr}")
    return names


def _resolver_kinds(index: dict[str, list[ast.AST]], entrypoints: set[str]) -> set[str]:
    """Which resolvers are reachable from these entrypoints (transitively)."""
    seen: set[int] = set()
    kinds: set[str] = set()
    pending = sorted(entrypoints)
    while pending:
        for node in _resolve(index, pending.pop()):
            if id(node) in seen:
                continue
            seen.add(id(node))
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr in _RESOLVERS
                ):
                    kinds.add(child.func.attr)
            pending.extend(_called_names(node))
    return kinds


def _collected_sources(trees: dict[str, ast.AST]) -> dict[str, set[str]]:
    """Every ``source_name`` that can land in ``health.failed_sources`` -> entrypoints."""
    sources: dict[str, set[str]] = {}
    collector_tree = trees["hermesd/collector.py"]
    for node in ast.walk(collector_tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "_SourceSpec" or len(node.args) < 3:
            continue
        name_node = node.args[1]
        if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)):
            continue
        entrypoints = sources.setdefault(name_node.value, set())
        for child in ast.walk(node.args[2]):
            if not (isinstance(child, ast.Attribute) and child.attr.startswith("_")):
                continue
            if isinstance(child.value, ast.Name) and child.value.id == "self":
                entrypoints.add(f"self.{child.attr}")

    index = _function_index(trees)
    for name, nodes in index.items():
        for node in nodes:
            for child in ast.walk(node):
                # health.collect(fallback, "<source_name>", fn, default)
                if not (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)):
                    continue
                if child.func.attr != "collect" or len(child.args) < 3:
                    continue
                literal = child.args[1]
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                    sources.setdefault(literal.value, set()).add(name)
    return sources


def _table_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and set(cells[0]) <= {"-", ":", " "}:
            continue
        rows.append(cells)
    return rows


def _documented_scopes(text: str) -> dict[str, tuple[str, bool]]:
    """source_name -> (scope class, is-derived) from the ownership table."""
    documented: dict[str, tuple[str, bool]] = {}
    for cells in _table_rows(text):
        if len(cells) < 2:
            continue
        name = cells[0].strip("`")
        if not name or not name.replace("_", "").isalnum() or not name.islower():
            continue
        scope_cell = cells[1]
        scope = scope_cell.split()[0].strip("`") if scope_cell.split() else ""
        if scope not in _SCOPES:
            continue
        assert name not in documented, f"{name} is documented twice in {_RULE_FILE.name}"
        documented[name] = (scope, _DERIVED_MARKER in scope_cell)
    return documented


def _documented_env_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for cells in _table_rows(text):
        if not cells:
            continue
        name = cells[0].strip("`")
        if name and name.isupper() and name.replace("_", "").isalnum():
            keys.add(name)
    return keys


def _collector_env_keys(trees: dict[str, ast.AST]) -> set[str]:
    keys: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "get" or not node.args:
                continue
            holder = node.func.value
            if not (isinstance(holder, ast.Attribute) and holder.attr == "_env"):
                continue
            literal = node.args[0]
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                keys.add(literal.value)
    return keys


def test_source_ownership_doc_matches_resolver_call_sites():
    trees = _parsed_modules()
    index = _function_index(trees)
    documented = _documented_scopes(_RULE_FILE.read_text())
    assert documented, f"no ownership rows parsed from {_RULE_FILE}"

    problems: list[str] = []
    for source_name, entrypoints in sorted(_collected_sources(trees).items()):
        if source_name not in documented:
            problems.append(f"{source_name}: collected but not documented")
            continue
        scope, derived = documented[source_name]
        kinds = _resolver_kinds(
            index, set(entrypoints) | set(_EXTRA_ENTRYPOINTS.get(source_name, ()))
        )
        if not kinds:
            # No path resolution of its own: the row must say so, and name the
            # scope it inherits (the session rows it consumes).
            if not derived:
                problems.append(
                    f"{source_name}: resolves nothing but is not marked {_DERIVED_MARKER}"
                )
            continue
        if derived:
            problems.append(f"{source_name}: marked {_DERIVED_MARKER} but calls {sorted(kinds)}")
            continue
        expected = {
            frozenset({"shared_path"}): "ROOT",
            frozenset({"profile_path"}): "PROFILE",
            frozenset(_RESOLVERS): "MIXED",
        }[frozenset(kinds)]
        if scope != expected:
            problems.append(
                f"{source_name}: documented {scope} but calls {sorted(kinds)} -> {expected}"
            )

    stale = sorted(set(documented) - set(_collected_sources(trees)))
    problems.extend(f"{name}: documented but no longer collected" for name in stale)
    assert problems == [], "source-ownership.md disagrees with the code:\n" + "\n".join(problems)


def test_source_ownership_doc_covers_every_process_env_read():
    trees = _parsed_modules()
    documented = _documented_env_keys(_RULE_FILE.read_text())
    assert _collector_env_keys(trees) <= documented, (
        "PROCESS-ENV keys missing from "
        f"{_RULE_FILE.name}: {sorted(_collector_env_keys(trees) - documented)}"
    )


def test_gateway_launch_files_are_root_scoped_under_a_profile(
    profiled_hermes_home: Path,
):
    """gateway-starts.log and the dashboard-client marker join the root launch files.

    Upstream resolves all three through ``get_hermes_home()`` (PROFILE), but they
    describe the ROOT gateway's launch, the same process whose heartbeat and
    lifecycle sentinel hermesd already reads from the root. The storm ledger sits
    beside them so the restart count and the liveness clock describe one process;
    the dashboard-client marker belongs to the web dashboard that gateway serves.
    """
    home = profiled_hermes_home
    profile_home = home / "profiles" / "coding"

    now = time.time()
    root_state = home / "state"
    root_state.mkdir(exist_ok=True)
    (home / "gateway-starts.log").write_text(f"{now - 1800.0!r}\n")
    (root_state / "dashboard_clients.heartbeat").touch()
    os.utime(root_state / "dashboard_clients.heartbeat", (now - 120.0, now - 120.0))
    (root_state / "gateway.heartbeat").write_text(
        json.dumps({"pid": 12345, "updated_at": "2027-01-15T00:00:00+00:00"})
    )
    (home / "logs").mkdir(exist_ok=True)
    (home / "logs" / "gateway-exit-diag.log").write_text(
        json.dumps({"ts": "2027-01-15T00:00:00+00:00", "tag": "gateway.asyncio_main_return"}) + "\n"
    )
    (profile_home / "logs").mkdir(parents=True, exist_ok=True)
    (profile_home / "logs" / "gateway-exit-diag.log").write_text(
        json.dumps({"ts": "2027-01-15T00:00:00+00:00", "tag": "profile_only_tag"}) + "\n"
    )

    profile_state = profile_home / "state"
    profile_state.mkdir(parents=True)
    (profile_home / "gateway-starts.log").write_text(f"{now - 30.0!r}\n" * 9)
    (profile_state / "dashboard_clients.heartbeat").touch()

    c = Collector(home, profile_name="coding")
    try:
        gateway = c.collect().gateway
    finally:
        c.close()

    # The root copy: one start, marker mtime at 2027-01-15. The nine profile
    # entries (which would trip the storm cap) and the profile marker are ignored.
    assert gateway.gateway_starts_recorded is True
    assert gateway.gateway_starts_window == 0
    assert gateway.gateway_starts_1h == 1
    # The root marker was aged 120 s and the profile marker touched now: only a
    # root read can call this detached (the age alone is non-None either way).
    assert gateway.dashboard_client_last_frame_age_seconds is not None
    assert gateway.dashboard_client_attached is False
    assert gateway.exit_diag_recorded is True
    assert gateway.exit_diag_last_tag == "gateway.asyncio_main_return"


def test_profiled_collector_reads_session_coordination_from_the_profile_db(
    profiled_hermes_home: Path,
) -> None:
    """Leases, hygiene, routing and generation rows are PROFILE state.db data
    (upstream opens ``get_hermes_home()/"state.db"``, ``hermes_state.py:160,178``),
    and terminal breadcrumbs are PROFILE files
    (``hermes_cli/terminal_breadcrumbs.py:26-28``): a profiled collector must see
    the selected profile's rows and never the root's."""
    now = time.time()

    def seed(db_path: Path, marker: str) -> None:
        conn = sqlite3.connect(db_path)
        create_session_coordination_tables(conn)
        insert_turn_lease(
            conn, f"{marker}-conv", f"pid={111}:tid=1:agent=a:nonce=b", now, now + 300
        )
        conn.execute("INSERT INTO gateway_hygiene_state VALUES (?, 4)", (f"{marker}:42:7",))
        conn.execute(
            "INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at) VALUES (?,?,?,?)",
            ("/sessions/dir", f"{marker}:42:7", json.dumps({"session_id": f"{marker}-sess"}), now),
        )
        conn.execute(
            "INSERT INTO conversation_generations VALUES ('cli', ?, 5)", (f"{marker}-key",)
        )
        conn.commit()
        conn.close()

    seed(profiled_hermes_home / "state.db", "profile")
    seed(profiled_hermes_home / "profiles" / "coding" / "state.db", "coding")

    def seed_terminals(home: Path, marker: str) -> None:
        directory = home / "terminal-sessions"
        directory.mkdir(exist_ok=True)
        (directory / "tty-dev-pts-1").write_text(
            json.dumps({"session_id": f"{marker}-tty", "cwd": f"/{marker}", "ts": now})
        )

    seed_terminals(profiled_hermes_home, "root")
    seed_terminals(profiled_hermes_home / "profiles" / "coding", "profile")

    profiled = Collector(profiled_hermes_home, profile_name="coding")
    state = profiled.collect()
    profiled.close()
    assert [lease.key for lease in state.session_coordination.leases] == ["coding-conv"]
    assert [row.session_key for row in state.session_coordination.hygiene] == ["coding:42:7"]
    assert [route.session_id for route in state.session_coordination.routes] == ["coding-sess"]
    assert [gen.session_key for gen in state.session_coordination.generations] == ["coding-key"]
    assert [row.cwd for row in state.terminal_sessions.sessions] == ["/profile"]

    root = Collector(profiled_hermes_home)
    state = root.collect()
    root.close()
    assert [lease.key for lease in state.session_coordination.leases] == ["profile-conv"]
    assert [row.cwd for row in state.terminal_sessions.sessions] == ["/root"]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def test_loop_tick_witness_is_probed_from_the_root_home_under_a_profile(
    profiled_hermes_home: Path,
):
    """The witness belongs to the root gateway, node and heartbeat alike.

    Upstream arms the witness on the heartbeat the gateway writes
    (``gateway/shutdown_watchdog.py:169``) and a served profile owns no gateway
    of its own, so under ``--profile`` the plan must come from the root
    heartbeat and the node must resolve under the root home. A profile read would
    silently interrogate a node nobody writes and report "no witness" forever.
    """
    home = profiled_hermes_home
    profile_home = home / "profiles" / "coding"
    now = time.time()

    root_state = home / "state"
    root_state.mkdir(exist_ok=True)
    (root_state / "gateway.heartbeat").write_text(
        json.dumps(
            {
                "pid": 4242,
                "updated_at": _iso(now - 400),
                "loop_tick_socket": True,
                "loop_tick_tcp_port": None,
            }
        )
    )
    # A conflicting profile-local heartbeat: fresh, and owned by a different pid
    # so a profile read would answer "not my witness" instead.
    profile_state = profile_home / "state"
    profile_state.mkdir(parents=True, exist_ok=True)
    (profile_state / "gateway.heartbeat").write_text(
        json.dumps(
            {
                "pid": 9999,
                "updated_at": _iso(now),
                "loop_tick_socket": True,
                "loop_tick_tcp_port": None,
            }
        )
    )
    (home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 4242,
                "start_time": now - 5000,
                "kind": "hermes-gateway",
                "gateway_state": "running",
                "platforms": {},
            }
        )
    )

    probed: list[tuple[int, int | None]] = []

    def probe(pid: int, tcp_port: int | None) -> bool:
        probed.append((pid, tcp_port))
        return True

    c = Collector(
        home,
        profile_name="coding",
        pid_exists=lambda pid: pid == 4242,
        loop_tick_probe=probe,
    )
    try:
        gateway = c.collect().gateway
    finally:
        c.close()

    assert probed == [(4242, None)]
    assert gateway.loop_tick_armed is True
    assert gateway.loop_health is GatewayLoopHealth.ALIVE


def test_probed_loop_tick_resolves_the_node_under_the_root_home(
    profiled_hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The AF_UNIX node path is built from the root home, not the profile's.

    ``_default_loop_tick_probe`` derives ``state/gateway.loop-tick.<pid>.sock``
    from the home it is handed; upstream arms it under the *root* gateway's home
    (``gateway/shutdown_watchdog.py:169``), so under ``--profile`` a profile home
    here would interrogate a node nobody writes.
    """
    import hermesd.collector as collector_module

    home = profiled_hermes_home
    seen: list[Path] = []

    def fake_probe(pid: int, tcp_port: int | None, probe_home: Path, **kwargs: object) -> bool:
        seen.append(probe_home)
        return True

    monkeypatch.setattr(collector_module, "_default_loop_tick_probe", fake_probe)
    c = Collector(home, profile_name="coding")
    try:
        assert c._probed_loop_tick(4242, None) is True
    finally:
        c.close()

    assert seen == [home]
    assert seen[0] != home / "profiles" / "coding"


def _defined_test_names() -> set[str]:
    root = Path(__file__).resolve().parent
    names: set[str] = set()
    for path in root.glob("test_*.py"):
        names.update(
            re.findall(r"^def (test_[A-Za-z0-9_]+)", path.read_text(encoding="utf-8"), re.M)
        )
    return names


def test_source_ownership_rows_cite_upstream_and_resolve_their_pins():
    """Rule 3 and the pin column are enforced, not just documented.

    ``_documented_scopes`` only reads the scope cell, so a row could cite no
    upstream file at all, and a "pinned by" cell could name a test that does not
    exist — which is exactly how the `gateway_loop_tick` row came to credit a
    test that never armed a witness. Divergence rows are exempt only from having
    a pin at all (many pre-existing rows are explicitly `UNPINNED`); any name
    they *do* cite has to exist.
    """
    defined = _defined_test_names()
    problems: list[str] = []
    for cells in _table_rows(_RULE_FILE.read_text()):
        if len(cells) < 8:
            continue
        name = cells[0].strip("`")
        if not name or not name.replace("_", "").isalnum() or not name.islower():
            continue
        scope_cell = cells[1]
        scope = scope_cell.split()[0].strip("`") if scope_cell.split() else ""
        if scope not in _SCOPES:
            continue
        if not cells[5].strip():
            problems.append(f"{name}: no upstream citation")
        for cited in re.findall(r"`(test_[A-Za-z0-9_]+)`", cells[7]):
            if cited not in defined:
                problems.append(f"{name}: pin {cited} is not defined in tests/")
    assert problems == [], "source-ownership.md rows are incomplete:\n" + "\n".join(problems)
