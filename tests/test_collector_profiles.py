"""Profile-scoped collection: discovery, path containment, session counts,
and soul excerpts."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from hermesd.collector import (
    Collector,
    _read_soul_excerpt,
)
from tests.conftest import (
    _assert_cached_until_changed,
    _count_opens,
    _skip_if_root,
    _unreadable,
    create_state_db_tables,
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
