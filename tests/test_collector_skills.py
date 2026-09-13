"""Collection of skills, memory files, hooks, plugins, and curator runs."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pytest
import yaml

from hermesd.collector import (
    Collector,
    _count_skills,
    _word_count,
)
from hermesd.models import MCPCacheEntryState
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import (
    _assert_cached_until_changed,
    _count_opens,
    _skip_if_root,
    _unreadable,
    iso_ago,
    render_to_str,
)


def test_collect_curator_scheduler_and_consolidate_config(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(yaml.dump({"curator": {"consolidate": True}}))
    (hermes_home / "skills" / ".curator_state").write_text(
        json.dumps(
            {
                "paused": True,
                "run_count": 7,
                "last_run_at": "2026-07-10T10:00:00Z",
                "last_report_path": "logs/curator/2026/run.md",
            }
        )
    )

    c = Collector(hermes_home)
    state = c.collect()

    assert state.curator.scheduler_state_present is True
    assert state.curator.scheduler_paused is True
    assert state.curator.scheduler_run_count == 7
    assert state.curator.consolidate_enabled is True
    c.close()


def test_collect_curator_scheduler_preserves_last_good_on_malformed_state(
    hermes_home: Path,
):
    state_path = hermes_home / "skills" / ".curator_state"
    state_path.write_text(json.dumps({"paused": True, "run_count": 7}))

    c = Collector(hermes_home)
    first = c.collect()
    assert first.curator.scheduler_run_count == 7

    state_path.write_text("{not valid json")
    second = c.collect()

    assert second.curator.scheduler_run_count == 7
    assert "curator" not in second.health.failed_sources
    c.close()


def test_learned_skill_directory_that_is_a_symlink_is_skipped(hermes_home: Path, tmp_path: Path):
    """A learned/<skill> symlink must not be counted, however it resolves."""
    outside = tmp_path / "smuggled-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: smuggled\npinned: true\ncreated_by: agent\n---\n")
    learned = hermes_home / "skills" / "learned"
    learned.mkdir()
    real = learned / "dev-lint"
    real.mkdir()
    (real / "SKILL.md").write_text("---\nname: dev-lint\n---\n")
    (learned / "smuggled").symlink_to(outside, target_is_directory=True)
    # A plain file in learned/ is not a skill directory either.
    (learned / "README.md").write_text("notes\n")

    c = Collector(hermes_home)
    try:
        memory = c.collect().memory
    finally:
        c.close()

    assert memory.learned_skill_count == 1
    assert memory.pinned_skill_count == 0
    assert memory.agent_created_skill_count == 0


def test_collect_memory_learning_summary(hermes_home: Path):
    (hermes_home / "skills" / ".usage.json").write_text(
        json.dumps(
            {
                "dev-lint": {"use_count": 4},
                "research": {"use_count": 1, "pinned": True, "created_by": "agent"},
                "profile-helper": {"use_count": 2, "profile_skill": True},
            }
        )
    )
    learned = hermes_home / "skills" / "learned"
    learned.mkdir()
    skill_dir = learned / "dev-lint"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: dev-lint\npinned: true\ncreated_by: agent\n---\nBody\n"
    )
    (hermes_home / "memories" / "MEMORY.md").write_text("## First\nBody\n## Second\nBody\n")
    (hermes_home / "memories" / "USER.md").write_text("## Profile\nBody\n")

    c = Collector(hermes_home)
    state = c.collect()

    assert state.memory.skill_usage_count == 3
    assert state.memory.learned_skill_count == 3
    assert state.memory.pinned_skill_count == 2
    assert state.memory.agent_created_skill_count == 2
    assert state.memory.memory_card_count == 3
    c.close()


def test_collect_skills_and_memory_visibility_render_from_collected_state(hermes_home: Path):
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "credential_pool": {
                    "vertex": [
                        {
                            "label": "Vertex",
                            "auth_type": "oauth",
                            "source": "adc",
                            "last_status": "ok",
                            "access_expires_at": "2026-07-11T12:00:00Z",
                            "last_refresh": "2026-07-11T11:00:00Z",
                        }
                    ]
                }
            }
        )
    )
    (hermes_home / "skills" / ".usage.json").write_text(
        json.dumps({"dev-lint": {"use_count": 4, "pinned": True}})
    )
    (hermes_home / "memories" / "MEMORY.md").write_text("## First\nBody\n")

    c = Collector(hermes_home)
    state = c.collect()
    skills_text = render_to_str(render_panel(7, state, Theme(), detail=True), width=160)
    memory_text = render_to_str(render_panel(10, state, Theme(), detail=True), width=120)

    assert "2026-07-11T12:00:00Z" in skills_text
    assert "2026-07-11T11:00:00Z" in skills_text
    assert "Learning" in memory_text
    assert "1 used skills" in memory_text
    c.close()


def test_collect_memory_learning_summary_preserves_usage_on_malformed_json(
    hermes_home: Path,
):
    usage_path = hermes_home / "skills" / ".usage.json"
    usage_path.write_text(json.dumps({"dev-lint": {"use_count": 4, "pinned": True}}))

    c = Collector(hermes_home)
    first = c.collect()
    assert first.memory.skill_usage_count == 1
    assert first.memory.pinned_skill_count == 1

    usage_path.write_text("{not valid json")
    second = c.collect()

    assert second.memory.skill_usage_count == 1
    assert second.memory.pinned_skill_count == 1
    assert "memory" not in second.health.failed_sources
    c.close()


def test_collect_ignores_stray_entries_in_scanned_directories(populated_hermes_home: Path):
    """Stray files / incomplete dirs in skills, hooks, plugins, and checkpoints are skipped."""
    home = populated_hermes_home
    # skills: a stray file at category level, a dot-dir, and a file inside a category
    (home / "skills" / "README.md").write_text("not a category")
    (home / "skills" / ".hidden").mkdir()
    (home / "skills" / "dev" / "notes.txt").write_text("not a skill dir")
    # hooks: a stray file and a hook dir without handler.py
    (home / "hooks" / "stray.txt").write_text("not a hook")
    no_handler = home / "hooks" / "no-handler"
    no_handler.mkdir()
    (no_handler / "HOOK.yaml").write_text("name: no-handler\nevents: not-a-list\n")
    # plugins: a stray file and a plugin dir without plugin.yaml
    (home / "plugins" / "stray.txt").write_text("not a plugin")
    (home / "plugins" / "no-manifest").mkdir()
    # checkpoints: a stray file
    (home / "checkpoints" / "stray.txt").write_text("not a repo")

    c = Collector(home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()

    assert state.health.failed_sources == []
    assert state.skills_memory.skill_count == 15
    assert len(state.skills_memory.hooks) == 2
    assert {p.name for p in state.skills_memory.plugins} == {"weather", "disabled-plugin"}
    assert [cp.repo_id for cp in state.checkpoints] == ["abc123def4567890"]
    c.close()


def test_collect_hook_with_non_list_events_gets_empty_events(hermes_home: Path):
    hook_dir = hermes_home / "hooks" / "odd-events"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("name: odd-events\nevents: not-a-list\n")
    (hook_dir / "handler.py").write_text("def handle(event_type, context):\n    return None\n")

    c = Collector(hermes_home)
    state = c.collect()
    assert len(state.skills_memory.hooks) == 1
    assert state.skills_memory.hooks[0].events == []
    c.close()


def test_collect_skills_no_manifest(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.skills_memory.skill_count == 0
    assert state.skills_memory.skill_categories == 0
    c.close()


def test_collect_memory_files(hermes_home: Path):
    (hermes_home / "memories" / "MEMORY.md").write_text("test")
    (hermes_home / "memories" / "USER.md").write_text("test")
    c = Collector(hermes_home)
    state = c.collect()
    assert state.skills_memory.memory_file_count == 2
    c.close()


def test_memory_files_exclude_lock_files_and_dotfiles(hermes_home: Path):
    """The agent's MEMORY.md.lock / USER.md.lock siblings are not memory files."""
    memories = hermes_home / "memories"
    (memories / "MEMORY.md").write_text("test")
    (memories / "USER.md").write_text("test")
    (memories / "MEMORY.md.lock").write_text("")
    (memories / "USER.md.lock").write_text("")
    (memories / ".DS_Store").write_text("")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.skills_memory.memory_file_count == 2
    assert state.memory.memory_file_count == 2
    assert state.memory.memory_files == ["MEMORY.md", "USER.md"]


def test_read_skill_description_parses_yaml_frontmatter(hermes_home: Path):
    skill_md = hermes_home / "skills" / "dev" / "lint" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        """---
description: |
  Use: lint tools
  Keep style clean
---

Body
"""
    )

    c = Collector(hermes_home)
    assert c._read_skill_description("dev", "lint") == "Use: lint tools\nKeep style clean"
    c.close()


@pytest.mark.parametrize(
    ("skill_md_content", "reason"),
    [
        (None, "missing SKILL.md"),
        ("---\nname: lint\n---\nBody\n", "frontmatter without description"),
        ("---\ndescription: [unclosed\n---\nBody\n", "malformed YAML frontmatter"),
        ("No frontmatter at all\n", "no frontmatter delimiter"),
    ],
)
def test_skill_without_usable_frontmatter_gets_empty_description(
    hermes_home: Path,
    skill_md_content: str | None,
    reason: str,
):
    skill_dir = hermes_home / "skills" / "dev" / "lint"
    skill_dir.mkdir(parents=True, exist_ok=True)
    if skill_md_content is not None:
        (skill_dir / "SKILL.md").write_text(skill_md_content)

    c = Collector(hermes_home)
    state = c.collect()
    assert "skills" not in state.health.failed_sources
    assert state.skills_memory.skill_count == 1, reason
    assert state.skills_memory.skills[0].description == "", reason
    c.close()


def test_curator_run_json_symlink_returns_empty_without_failing_source(hermes_home: Path):
    run_dir = hermes_home / "logs" / "curator" / "20260610-133539"
    run_dir.mkdir(parents=True)
    real = hermes_home / "logs" / "curator" / "real_run.json"
    real.write_text(json.dumps({"model": "MiniMax-M3", "counts": {"before": 8}}))
    (run_dir / "run.json").symlink_to(real)

    c = Collector(hermes_home)
    try:
        state = c.collect()
        # Symlinked run.json is rejected by the read-only hardening: empty run,
        # and curator is NOT marked failed (it is a clean empty result).
        assert state.curator.run_present is False
        assert state.curator.model == ""
        assert "curator" not in state.health.failed_sources
    finally:
        c.close()


@_skip_if_root
def test_word_count_oserror_returns_zero(tmp_path: Path):
    f = tmp_path / "BOOT.md"
    f.write_text("one two three")
    os.chmod(f, 0o000)
    try:
        if not _unreadable(f):
            pytest.skip("filesystem allowed read despite chmod 000")
        assert _word_count(f) == 0
    finally:
        os.chmod(f, 0o644)


def test_count_skills_ignores_dotdirs_and_files(tmp_path: Path):
    skills = tmp_path / "skills"
    real = skills / "dev"
    real.mkdir(parents=True)
    (real / "skill-a").mkdir()
    (real / "skill-b").mkdir()
    (real / "README.md").write_text("not a skill dir")  # non-dir child: not counted
    (skills / ".cache").mkdir()  # dotdir category: skipped
    (skills / "loose.txt").write_text("x")  # non-dir category: skipped
    assert _count_skills(skills) == 2
    assert _count_skills(tmp_path / "missing") == 0


def test_memory_files_symlinked_outside_hermes_home_are_ignored(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside-memory.md"
    outside.write_text("## Card\nsecret words from outside the hermes home\n")
    (hermes_home / "memories" / "MEMORY.md").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "memory" not in state.health.failed_sources
    assert state.memory.memory_word_count == 0
    assert state.memory.memory_card_count == 0


def test_large_memory_file_is_read_under_the_byte_cap(hermes_home: Path):
    (hermes_home / "memories" / "MEMORY.md").write_text("## Card\n" + "word " * 400_000)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "memory" not in state.health.failed_sources
    assert 0 < state.memory.memory_word_count < 400_000
    assert state.memory.memory_card_count == 1


def test_skill_description_is_cached_until_skill_md_changes(
    hermes_home: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
):
    skill_md = hermes_home / "skills" / "coding" / "tdd" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\ndescription: write the test first\n---\n")
    opens = _count_opens(monkeypatch, skill_md)

    _assert_cached_until_changed(
        collector,
        opens,
        skill_md,
        lambda: skill_md.write_text("---\ndescription: red green refactor\n---\n"),
    )
    assert collector.collect().skills_memory.skills[0].description == "red green refactor"


def test_memory_md_is_cached_until_it_changes(
    hermes_home: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
):
    memory_md = hermes_home / "memories" / "MEMORY.md"
    memory_md.parent.mkdir(parents=True, exist_ok=True)
    memory_md.write_text("## one\nalpha beta\n")
    opens = _count_opens(monkeypatch, memory_md)

    _assert_cached_until_changed(
        collector,
        opens,
        memory_md,
        lambda: memory_md.write_text("## one\n## two\nalpha beta gamma delta\n"),
    )
    memory = collector.collect().memory
    assert memory.memory_word_count == 8
    assert memory.memory_card_count == 2


def test_user_md_is_cached_until_it_changes(
    hermes_home: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
):
    user_md = hermes_home / "memories" / "USER.md"
    user_md.parent.mkdir(parents=True, exist_ok=True)
    user_md.write_text("## profile\nprefers terse answers\n")
    opens = _count_opens(monkeypatch, user_md)

    _assert_cached_until_changed(
        collector,
        opens,
        user_md,
        lambda: user_md.write_text("## profile\n## tone\nprefers terse answers always\n"),
    )
    assert collector.collect().memory.user_word_count == 8


def test_learned_skill_frontmatter_is_cached_until_it_changes(
    hermes_home: Path, collector: Collector, monkeypatch: pytest.MonkeyPatch
):
    skill_md = hermes_home / "skills" / "learned" / "grep-first" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\ncreated_by: user\n---\n")
    opens = _count_opens(monkeypatch, skill_md)

    _assert_cached_until_changed(
        collector,
        opens,
        skill_md,
        lambda: skill_md.write_text("---\ncreated_by: agent\npinned: true\n---\n"),
    )
    memory = collector.collect().memory
    assert memory.pinned_skill_count == 1
    assert memory.agent_created_skill_count == 1


def _write_curator_run(home: Path, stamp: str, payload: dict) -> Path:
    run_dir = home / "logs" / "curator" / stamp
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(payload))
    return run_dir


_LIVE_SHAPE = {
    "started_at": "2026-06-10T13:35:39+00:00",
    "duration_seconds": 597.0,
    "model": "MiniMax-M3",
    "provider": "minimax",
    "counts": {
        "before": 8,
        "after": 5,
        "delta": -3,
        "archived_this_run": 3,
        "added_this_run": 0,
        "pruned_this_run": 3,
        "consolidated_this_run": 0,
        "tool_calls_total": 67,
    },
    "tool_call_counts": {"read_file": 12, "list_dir": 3},
    "state_transitions": [
        {"from": "collecting", "to": "summarizing", "at": "2026-06-10T13:40:00+00:00"}
    ],
    "llm_summary": "## Summary\nprocessed the candidates",
    "llm_error": None,
}


def test_collect_curator_reads_newest_run(hermes_home: Path):
    _write_curator_run(hermes_home, "20260601-100000", {"model": "stale", "counts": {"before": 1}})
    _write_curator_run(hermes_home, "20260610-133539", _LIVE_SHAPE)

    c = Collector(hermes_home)
    state = c.collect()
    cur = state.curator
    assert cur.run_present is True
    assert cur.stamp == "20260610-133539"
    assert cur.model == "MiniMax-M3"
    assert cur.provider == "minimax"
    assert cur.duration_seconds == 597.0
    assert cur.count_before == 8
    assert cur.count_after == 5
    assert cur.count_delta == -3
    assert cur.archived_count == 3
    assert cur.pruned_count == 3
    assert cur.consolidated_count == 0
    assert cur.tool_calls_total == 67
    assert cur.tool_call_counts == {"read_file": 12, "list_dir": 3}
    assert cur.state_transitions == ["collecting -> summarizing @ 2026-06-10T13:40:00+00:00"]
    assert cur.llm_error == ""
    c.close()


def test_collect_curator_state_transition_state_only_shape(hermes_home: Path):
    # Alternate producer shape: a transition entry with only a `state` key (no
    # from/to) is labelled by that state, with the timestamp appended.
    run = _LIVE_SHAPE | {
        "state_transitions": [
            {"state": "idle", "at": "2026-06-10T13:41:00+00:00"},
            {"state": "done"},
        ]
    }
    _write_curator_run(hermes_home, "20260610-133539", run)

    c = Collector(hermes_home)
    state = c.collect()
    c.close()
    assert state.curator.state_transitions == [
        "idle @ 2026-06-10T13:41:00+00:00",
        "done",
    ]


def test_collect_curator_added_and_consolidated_counts_render(hermes_home: Path):
    run = _LIVE_SHAPE | {
        "counts": _LIVE_SHAPE["counts"]
        | {
            "added_this_run": 17,
            "consolidated_this_run": 19,
        }
    }
    _write_curator_run(hermes_home, "20260610-133539", run)

    c = Collector(hermes_home)
    state = c.collect()
    c.close()

    text = render_to_str(render_panel(13, state, Theme(), detail=True), width=120, no_color=True)
    assert re.search(r"Added\s+17", text)
    assert re.search(r"Consolidated\s+19", text)


def test_collect_curator_absent_is_empty(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.curator.run_present is False
    assert state.curator.model == ""
    assert "curator" not in state.health.failed_sources
    c.close()


def test_collect_curator_empty_dir_is_empty(hermes_home: Path):
    (hermes_home / "logs" / "curator").mkdir(parents=True)
    c = Collector(hermes_home)
    state = c.collect()
    assert state.curator.run_present is False
    c.close()


def test_collect_curator_run_dir_without_run_json_is_empty(hermes_home: Path):
    # A run directory exists but run.json is missing (e.g. interrupted run) —
    # degrade to empty, not a failed source.
    (hermes_home / "logs" / "curator" / "20260610-133539").mkdir(parents=True)
    c = Collector(hermes_home)
    state = c.collect()
    assert state.curator.run_present is False
    assert "curator" not in state.health.failed_sources
    c.close()


def test_collect_curator_skips_symlinked_run_dir(hermes_home: Path):
    curator_dir = hermes_home / "logs" / "curator"
    curator_dir.mkdir(parents=True)
    outside = hermes_home / "outside_run"
    outside.mkdir()
    (outside / "run.json").write_text(json.dumps({"model": "leaked", "run_present": True}))
    try:
        (curator_dir / "20260610-133539").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks not supported here")
    c = Collector(hermes_home)
    state = c.collect()
    assert state.curator.run_present is False
    assert state.curator.model == ""
    assert "curator" not in state.health.failed_sources
    c.close()


def test_collect_curator_skips_symlinked_root_dir(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "curator"
    run_dir = outside / "20260610-133539"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"model": "leaked"}))
    logs_dir = hermes_home / "logs"
    (logs_dir / "curator").symlink_to(outside, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.curator.run_present is False
    assert state.curator.model == ""
    assert "curator" not in state.health.failed_sources
    c.close()


def _collect_state(home: Path, clock_value: float | None = None):
    kwargs = {} if clock_value is None else {"clock": lambda: clock_value}
    collector = Collector(home, **kwargs)
    try:
        return collector.collect()
    finally:
        collector.close()


def _write_mcp_cache(home: Path, payload: object) -> Path:
    path = home / "cache" / "mcp_schema_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def test_mcp_cache_present(hermes_home: Path, sample_mcp_schema_cache: Path):
    mtime = sample_mcp_schema_cache.stat().st_mtime
    state = _collect_state(hermes_home, clock_value=mtime + 300)

    cache = state.mcp_cache
    assert cache.mcp_cached_server_count == 2
    assert cache.mcp_cached_server_names == ["playwright", "sheets"]
    assert cache.mcp_schema_cache_age_seconds == 300.0
    assert "sk-should-never-render" not in json.dumps(state.model_dump(mode="json"))


def test_mcp_cache_absent(hermes_home: Path):
    cache = _collect_state(hermes_home).mcp_cache
    assert cache.mcp_cached_server_count == 0
    assert cache.mcp_cached_server_names == []
    assert cache.mcp_schema_cache_age_seconds is None


def test_mcp_cache_garbage_is_ignored(hermes_home: Path):
    _write_mcp_cache(hermes_home, "{not json at all")

    cache = _collect_state(hermes_home).mcp_cache
    assert cache.mcp_cached_server_count == 0
    assert cache.mcp_cached_server_names == []


def test_mcp_cache_wrong_shape_is_ignored(hermes_home: Path):
    _write_mcp_cache(hermes_home, ["playwright", "sheets"])

    assert _collect_state(hermes_home).mcp_cache.mcp_cached_server_count == 0


def test_mcp_cache_names_capped_and_sorted(hermes_home: Path):
    _write_mcp_cache(hermes_home, {f"srv-{index:02d}": {} for index in range(25)})

    cache = _collect_state(hermes_home).mcp_cache
    assert cache.mcp_cached_server_count == 25
    assert cache.mcp_cached_server_names == sorted(cache.mcp_cached_server_names)
    assert len(cache.mcp_cached_server_names) == 20


def test_mcp_cache_symlinked_file_is_ignored(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside_mcp.json"
    outside.write_text(json.dumps({"escaped": {}}))
    cache_dir = hermes_home / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "mcp_schema_cache.json").symlink_to(outside)

    cache = _collect_state(hermes_home).mcp_cache
    assert cache.mcp_cached_server_count == 0
    assert cache.mcp_cached_server_names == []


def test_mcp_cache_age_is_clamped_to_zero(hermes_home: Path):
    path = _write_mcp_cache(hermes_home, {"playwright": {}})
    mtime = path.stat().st_mtime

    assert _collect_state(hermes_home, mtime - 5000).mcp_cache.mcp_schema_cache_age_seconds == 0.0


# F02/F03: cache membership is a fact about the complete name sets on both
# sides. Bounding the *rendered* lists must never change which servers count as
# having an entry, or the 21st configured server reads as uncached purely
# because it fell off a display cap.


def _write_configured_mcp_servers(home: Path, names: list[str]) -> None:
    import yaml

    home.joinpath("config.yaml").write_text(
        yaml.dump({"mcp_servers": {name: {"enabled": True} for name in names}})
    )


def test_mcp_cache_membership_survives_the_display_cap(hermes_home: Path):
    names = [f"srv-{index:02d}" for index in range(25)]
    _write_configured_mcp_servers(hermes_home, names)
    _write_mcp_cache(hermes_home, {name: {} for name in names})

    cache = _collect_state(hermes_home).mcp_cache

    assert cache.mcp_cached_server_count == 25
    assert len(cache.mcp_cached_server_names) == 20
    assert cache.mcp_uncached_server_count == 0
    assert cache.mcp_uncached_server_names == []


def test_mcp_cache_reports_configured_server_with_no_entry(hermes_home: Path):
    _write_configured_mcp_servers(hermes_home, ["alpha", "beta", "gamma"])
    _write_mcp_cache(hermes_home, {"alpha": {}, "beta": {}})

    cache = _collect_state(hermes_home).mcp_cache

    assert cache.mcp_uncached_server_count == 1
    assert cache.mcp_uncached_server_names == ["gamma"]


def test_mcp_cache_uncached_count_is_complete_when_names_are_capped(hermes_home: Path):
    configured = [f"srv-{index:02d}" for index in range(30)]
    _write_configured_mcp_servers(hermes_home, configured)
    _write_mcp_cache(hermes_home, {"srv-00": {}})

    cache = _collect_state(hermes_home).mcp_cache

    assert cache.mcp_uncached_server_count == 29
    assert len(cache.mcp_uncached_server_names) == 20


def test_mcp_cache_uncached_ignores_servers_that_are_not_configured(hermes_home: Path):
    """A cached server absent from config is not an uncached server."""
    _write_configured_mcp_servers(hermes_home, ["alpha"])
    _write_mcp_cache(hermes_home, {"alpha": {}, "unconfigured": {}})

    cache = _collect_state(hermes_home).mcp_cache

    assert cache.mcp_cached_server_count == 2
    assert cache.mcp_uncached_server_count == 0


def test_mcp_cache_present_flag_distinguishes_absent_from_empty(hermes_home: Path):
    _write_configured_mcp_servers(hermes_home, ["alpha"])

    assert _collect_state(hermes_home).mcp_cache.mcp_cache_present is False

    _write_mcp_cache(hermes_home, {})
    cache = _collect_state(hermes_home).mcp_cache

    assert cache.mcp_cache_present is True
    assert cache.mcp_cached_server_count == 0
    assert cache.mcp_uncached_server_count == 1


def test_mcp_cache_symlink_is_not_present(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside_mcp.json"
    outside.write_text(json.dumps({"escaped": {}}))
    cache_dir = hermes_home / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "mcp_schema_cache.json").symlink_to(outside)

    assert _collect_state(hermes_home).mcp_cache.mcp_cache_present is False


def test_mcp_cache_without_config_reports_no_uncached(hermes_home: Path):
    """No config.yaml means no configured set to compare against, not all-uncached."""
    _write_mcp_cache(hermes_home, {"playwright": {}})

    cache = _collect_state(hermes_home).mcp_cache

    assert cache.mcp_cached_server_count == 1
    assert cache.mcp_uncached_server_count == 0


# --------------------------------------------------------------------------
# per-entry validity
#
# Every expectation below mirrors `get_cached_entry`
# (hermes-agent tools/mcp_schema_cache.py:59-73) exactly: an entry with no
# numeric TTL never expires, `ttl_ms: 0` is always expired because age >= 0
# always holds, and expiry is measured from the entry's own `written_at` —
# never from the cache file's mtime.
# --------------------------------------------------------------------------

_NOW = 1_800_000_000.0


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "fingerprint": "0123456789abcdef",
        "tools": [],
        "utility_tools": [],
    }
    entry.update(overrides)
    return entry


def _entry_no_fingerprint() -> dict[str, object]:
    entry = _entry()
    del entry["fingerprint"]
    return entry


def _single_entry_cache(home: Path, entry: object, *, mtime_offset: float = 0.0) -> None:
    path = _write_mcp_cache(home, {"srv": entry})
    if mtime_offset:
        stamp = _NOW + mtime_offset
        os.utime(path, (stamp, stamp))


def _entry_state(home: Path, entry: object, *, mtime_offset: float = 0.0):
    _single_entry_cache(home, entry, mtime_offset=mtime_offset)
    cache = _collect_state(home, _NOW).mcp_cache
    assert cache.mcp_cached_server_count == 1
    return cache.mcp_entries[0]


def test_mcp_cache_entry_without_ttl_never_expires(hermes_home: Path):
    """No numeric `ttl_ms` means upstream serves the entry forever."""
    entry = _entry_state(hermes_home, _entry(written_at=_NOW - 86400 * 365))

    assert entry.state is MCPCacheEntryState.VALID
    assert entry.ttl_ms is None
    assert entry.remaining_seconds is None
    assert entry.age_seconds == pytest.approx(86400 * 365)


def test_mcp_cache_entry_with_zero_ttl_is_always_expired(hermes_home: Path):
    """`ttl_ms: 0` expires immediately: `(now - written_at) * 1000 >= 0` always holds."""
    entry = _entry_state(hermes_home, _entry(ttl_ms=0, written_at=_NOW))

    assert entry.state is MCPCacheEntryState.EXPIRED
    assert entry.ttl_ms == 0.0
    assert entry.remaining_seconds is None


def test_mcp_cache_entry_zero_ttl_is_expired_even_when_just_written(hermes_home: Path):
    """The live-home shape: a cache that looks populated but is never served."""
    entry = _entry_state(hermes_home, _entry(ttl_ms=0, written_at=_NOW - 0.0001))

    assert entry.state is MCPCacheEntryState.EXPIRED


def test_mcp_cache_entry_inside_ttl_is_valid_with_remaining(hermes_home: Path):
    entry = _entry_state(hermes_home, _entry(ttl_ms=60_000, written_at=_NOW - 30))

    assert entry.state is MCPCacheEntryState.VALID
    assert entry.ttl_ms == 60_000.0
    assert entry.remaining_seconds == pytest.approx(30.0)


def test_mcp_cache_entry_past_ttl_is_expired(hermes_home: Path):
    entry = _entry_state(hermes_home, _entry(ttl_ms=60_000, written_at=_NOW - 120))

    assert entry.state is MCPCacheEntryState.EXPIRED
    assert entry.age_seconds == pytest.approx(120.0)
    assert entry.remaining_seconds is None


def test_mcp_cache_entry_at_the_exact_ttl_boundary_is_expired(hermes_home: Path):
    """Upstream uses `>=`, so age == ttl is already a miss."""
    entry = _entry_state(hermes_home, _entry(ttl_ms=60_000, written_at=_NOW - 60))

    assert entry.state is MCPCacheEntryState.EXPIRED


def test_mcp_cache_entry_a_millisecond_inside_ttl_is_valid(hermes_home: Path):
    entry = _entry_state(hermes_home, _entry(ttl_ms=60_000, written_at=_NOW - 59.999))

    assert entry.state is MCPCacheEntryState.VALID


def test_mcp_cache_entry_ttl_without_written_at_never_expires(hermes_home: Path):
    """Upstream needs BOTH numbers to expire an entry; one alone is not enough."""
    entry = _entry_state(hermes_home, _entry(ttl_ms=1))

    assert entry.state is MCPCacheEntryState.VALID
    assert entry.age_seconds is None
    assert entry.remaining_seconds is None


def test_mcp_cache_entry_text_ttl_is_not_a_number(hermes_home: Path):
    """A string TTL is not numeric, so upstream never expires the entry."""
    entry = _entry_state(hermes_home, _entry(ttl_ms="60000", written_at=_NOW - 3600))

    assert entry.state is MCPCacheEntryState.VALID
    assert entry.ttl_ms is None


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        pytest.param(_entry(fingerprint=None), "no usable fingerprint recorded", id="null"),
        pytest.param(_entry_no_fingerprint(), "no usable fingerprint recorded", id="absent"),
        pytest.param(_entry(fingerprint=""), "no usable fingerprint recorded", id="empty"),
        pytest.param(_entry(fingerprint=1234), "no usable fingerprint recorded", id="non-string"),
        pytest.param(["not", "a", "mapping"], "entry is not a mapping", id="list"),
        pytest.param("a string", "entry is not a mapping", id="string"),
        pytest.param(None, "entry is not a mapping", id="null-entry"),
    ],
)
def test_mcp_cache_entry_is_unassessable(hermes_home: Path, entry: object, reason: str):
    assessed = _entry_state(hermes_home, entry)

    assert assessed.state is MCPCacheEntryState.UNASSESSABLE
    assert assessed.reason == reason


def test_mcp_cache_entry_uses_written_at_not_the_file_mtime(hermes_home: Path):
    """A freshly rewritten file can still hold an entry whose own TTL ran out."""
    stale = _entry_state(
        hermes_home, _entry(ttl_ms=60_000, written_at=_NOW - 3600), mtime_offset=-1.0
    )

    assert stale.state is MCPCacheEntryState.EXPIRED


def test_mcp_cache_entry_survives_an_old_file_mtime(hermes_home: Path):
    """The converse: an ancient mtime must not expire a recently written entry."""
    fresh = _entry_state(
        hermes_home, _entry(ttl_ms=600_000, written_at=_NOW - 30), mtime_offset=-86400.0
    )

    assert fresh.state is MCPCacheEntryState.VALID
    assert fresh.remaining_seconds == pytest.approx(570.0)


def test_mcp_cache_entry_records_a_bounded_fingerprint_prefix(hermes_home: Path):
    entry = _entry_state(hermes_home, _entry(fingerprint="0123456789abcdef"))

    assert entry.fingerprint == "01234567"


def test_mcp_cache_never_claims_a_configuration_mismatch_it_cannot_prove(hermes_home: Path):
    """hermesd reads raw config.yaml; upstream fingerprints the env-expanded,
    default-merged, managed-scope-merged config it actually connected with
    (`hermes_cli/config.py:1965-1968`, `tools/mcp_tool_config.py:244-259,322-345`),
    so a differing fingerprint has causes hermesd cannot separate."""
    _write_configured_mcp_servers(hermes_home, ["srv"])
    _write_mcp_cache(hermes_home, {"srv": _entry(fingerprint="ffffffffffffffff")})

    cache = _collect_state(hermes_home, _NOW).mcp_cache

    assert "configuration-mismatched" not in {state.value for state in MCPCacheEntryState}
    assert cache.mcp_entries[0].state is MCPCacheEntryState.VALID
    assert cache.mcp_entries[0].fingerprint == "ffffffff"


def test_mcp_cache_validity_counts_survive_the_display_cap(hermes_home: Path):
    """Truncating the rendered entry list must never change a computed count."""
    entries: dict[str, object] = {}
    for index in range(25):
        entries[f"srv-{index:02d}"] = _entry(ttl_ms=0, written_at=_NOW)
    for index in range(4):
        entries[f"ok-{index}"] = _entry(ttl_ms=600_000, written_at=_NOW)
    entries["broken"] = ["not a mapping"]
    _write_mcp_cache(hermes_home, entries)

    cache = _collect_state(hermes_home, _NOW).mcp_cache

    assert cache.mcp_cached_server_count == 30
    assert cache.mcp_expired_entry_count == 25
    assert cache.mcp_valid_entry_count == 4
    assert cache.mcp_unassessable_entry_count == 1
    assert len(cache.mcp_entries) == 20
    assert [entry.name for entry in cache.mcp_entries] == sorted(
        entry.name for entry in cache.mcp_entries
    )


def test_mcp_cache_validity_counts_are_zero_when_the_file_is_absent(hermes_home: Path):
    cache = _collect_state(hermes_home, _NOW).mcp_cache

    assert cache.mcp_cache_present is False
    assert cache.mcp_entries == []
    assert (cache.mcp_valid_entry_count, cache.mcp_expired_entry_count) == (0, 0)
    assert cache.mcp_unassessable_entry_count == 0


def test_mcp_cache_never_copies_tool_payloads_into_state(hermes_home: Path):
    """`tools` / `utility_tools` / `inputSchema` are opaque, unbounded and can
    carry credentials, so nothing from them may reach the serialized state."""
    _write_mcp_cache(
        hermes_home,
        {
            "srv": _entry(
                ttl_ms=60_000,
                written_at=_NOW,
                tools=[
                    {
                        "name": "sk-tool-name-should-never-render",
                        "description": "sk-tool-description-should-never-render",
                        "inputSchema": {
                            "properties": {"token": {"default": "sk-schema-should-never-render"}}
                        },
                        "annotations": {"readOnlyHint": False},
                    }
                ],
                utility_tools=[
                    {
                        "schema": {"name": "sk-utility-should-never-render"},
                        "handler_key": "sk-handler-should-never-render",
                    }
                ],
                cache_scope="sk-scope-should-never-render",
            )
        },
    )

    state = _collect_state(hermes_home, _NOW)
    dumped = json.dumps(state.model_dump(mode="json"))

    for canary in (
        "sk-tool-name-should-never-render",
        "sk-tool-description-should-never-render",
        "sk-schema-should-never-render",
        "sk-utility-should-never-render",
        "sk-handler-should-never-render",
        "sk-scope-should-never-render",
        "inputSchema",
        "utility_tools",
    ):
        assert canary not in dumped


def test_skills_prompt_snapshot_present(hermes_home: Path, sample_skills_prompt_snapshot: Path):
    mtime = sample_skills_prompt_snapshot.stat().st_mtime
    snapshot = _collect_state(hermes_home, clock_value=mtime + 7200).skills_prompt

    assert snapshot.prompted_skill_count == 3
    assert snapshot.prompt_snapshot_age_seconds == 7200.0


def test_skills_prompt_snapshot_absent(hermes_home: Path):
    snapshot = _collect_state(hermes_home).skills_prompt
    assert snapshot.prompted_skill_count == 0
    assert snapshot.prompt_snapshot_age_seconds is None


def test_skills_prompt_snapshot_garbage_is_ignored(hermes_home: Path):
    (hermes_home / ".skills_prompt_snapshot.json").write_text("]]]not json")

    assert _collect_state(hermes_home).skills_prompt.prompted_skill_count == 0


def test_skills_prompt_snapshot_wrong_skills_shape(hermes_home: Path):
    (hermes_home / ".skills_prompt_snapshot.json").write_text(
        json.dumps({"version": 1, "skills": {"dev-lint": {}}})
    )

    assert _collect_state(hermes_home).skills_prompt.prompted_skill_count == 0


def test_skills_prompt_snapshot_symlinked_file_is_ignored(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside_snapshot.json"
    outside.write_text(json.dumps({"skills": [{"name": "escaped"}]}))
    (hermes_home / ".skills_prompt_snapshot.json").symlink_to(outside)

    assert _collect_state(hermes_home).skills_prompt.prompted_skill_count == 0


def test_skills_prompt_snapshot_age_is_clamped_to_zero(hermes_home: Path):
    path = hermes_home / ".skills_prompt_snapshot.json"
    path.write_text(json.dumps({"skills": [{"name": "dev-lint"}]}))
    mtime = path.stat().st_mtime

    snapshot = _collect_state(hermes_home, clock_value=mtime - 900).skills_prompt
    assert snapshot.prompt_snapshot_age_seconds == 0.0


def test_mcp_cache_and_prompt_sources_are_registered(hermes_home: Path):
    state = _collect_state(hermes_home)

    assert state.health.total_sources > 0
    assert "mcp_cache" not in state.health.failed_sources
    assert "skills_prompt" not in state.health.failed_sources


def _write_usage(home: Path, records: dict[str, dict[str, object]]) -> Path:
    usage = home / "skills" / ".usage.json"
    usage.write_text(json.dumps(records))
    return usage


def test_curator_threshold_defaults_without_config(hermes_home: Path):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.curator.stale_after_days == 14
    assert state.curator.archive_after_days == 30
    assert state.curator.thresholds_customized is False


def test_curator_threshold_config_overrides(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"curator": {"stale_after_days": 7, "archive_after_days": 21}})
    )
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.curator.stale_after_days == 7
    assert state.curator.archive_after_days == 21
    assert state.curator.thresholds_customized is True


def test_curator_threshold_junk_override_falls_back_to_default(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(yaml.dump({"curator": {"stale_after_days": "soon"}}))
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.curator.stale_after_days == 14
    assert state.curator.archive_after_days == 30
    assert state.curator.thresholds_customized is True


def test_curator_usage_hygiene_counts(hermes_home: Path):
    now = time.time()
    _write_usage(
        hermes_home,
        {
            # Patch fully re-used: not pending.
            "dev-lint": {
                "state": "active",
                "pinned": True,
                "patch_generation": 3,
                "last_reused_patch_generation": 3,
                "last_used_at": iso_ago(3600, now=now),
            },
            # Patched but never re-used: the loop hermesd should surface.
            "research": {
                "state": "active",
                "patch_generation": 2,
                "last_reused_patch_generation": 0,
                "last_patched_at": iso_ago(1 * 86400, now=now),
            },
            # Stale by its own state, close to the archive threshold.
            "old-habit": {
                "state": "stale",
                "last_used_at": iso_ago(20 * 86400, now=now),
            },
            "archived-one": {
                "state": "archived",
                "archived_at": iso_ago(40 * 86400, now=now),
                "last_viewed_at": iso_ago(45 * 86400, now=now),
            },
            # Never used, never viewed, never patched: created_at is excluded
            # upstream, so no activity window may be derived from it.
            "never-used": {"state": "active", "created_at": iso_ago(2 * 86400, now=now)},
            "weird-state": {"state": "mystery"},
        },
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    cur = state.curator
    assert cur.managed_skill_count == 6
    assert cur.patch_pending_reuse_count == 1
    assert cur.state_active_count == 3
    assert cur.state_stale_count == 1
    assert cur.state_archived_count == 1
    assert cur.state_unknown_count == 1
    assert cur.pinned_count == 1
    windows = {window.name: window for window in cur.skill_windows}
    assert windows["research"].patch_pending_reuse is True
    assert windows["research"].days_until_stale == pytest.approx(13.0, abs=0.01)
    assert windows["research"].days_until_archive == pytest.approx(29.0, abs=0.01)
    assert windows["dev-lint"].days_until_stale == pytest.approx(13.96, abs=0.01)
    assert windows["dev-lint"].pinned is True
    assert windows["never-used"].last_activity_age_seconds is None
    assert windows["never-used"].days_until_stale is None
    assert windows["never-used"].days_until_archive is None
    assert "curator" not in state.health.failed_sources


def test_curator_usage_windows_are_soonest_first_and_bounded(hermes_home: Path):
    now = time.time()
    records = {}
    for index in range(25):  # literal: _SKILL_WINDOW_LIMIT is 20 (bounded windows)
        age_days = index + 1  # later index = older = sooner to go stale
        records[f"skill-{index:02d}"] = {
            "state": "active",
            "last_used_at": iso_ago(age_days * 86400, now=now),
        }
    _write_usage(hermes_home, records)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    cur = state.curator
    assert cur.managed_skill_count == 25
    assert len(cur.skill_windows) == 20
    stale_days = [window.days_until_stale for window in cur.skill_windows]
    assert stale_days == sorted(stale_days)
    # The soonest skills survive the cap; the youngest are the ones cut.
    assert cur.skill_windows[0].days_until_stale == pytest.approx(14 - 25, abs=0.01)
    assert cur.skill_windows[-1].days_until_stale == pytest.approx(14 - 6, abs=0.01)


def test_curator_hygiene_without_usage_file_is_a_healthy_zero(hermes_home: Path):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.curator.managed_skill_count == 0
    assert state.curator.skill_windows == []
    assert state.curator.patch_pending_reuse_count == 0
    assert "curator" not in state.health.failed_sources


def test_curator_hygiene_survives_malformed_usage_json(hermes_home: Path):
    usage = _write_usage(hermes_home, {"dev-lint": {"state": "active", "patch_generation": 2}})
    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.curator.patch_pending_reuse_count == 1

        usage.write_text("{not valid json")
        second = c.collect()
    finally:
        c.close()

    assert second.curator.patch_pending_reuse_count == 1
    assert "curator" not in second.health.failed_sources


def test_curator_hygiene_ignores_non_mapping_records(hermes_home: Path):
    usage = hermes_home / "skills" / ".usage.json"
    usage.write_text(json.dumps({"dev-lint": "just a string", "other": {"state": "stale"}}))
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.curator.managed_skill_count == 1
    assert state.curator.state_stale_count == 1


def test_usage_pinned_flag_is_read_strictly(hermes_home: Path):
    """`skills/.usage.json` is a state payload: `"false"` is not pinned.

    Upstream writes a real boolean (`set_pinned` stores `bool(pinned)`), so a
    string is corruption; `bool("false")` would report a skill as protected from
    curation when it is not. The human-authored *frontmatter* flag is left
    truthy on purpose — that one is a setting, read the way config values are.
    """
    _write_usage(
        hermes_home,
        {
            "quoted-false": {"state": "active", "pinned": "false"},
            "quoted-zero": {"state": "active", "pinned": "0"},
            "really-pinned": {"state": "active", "pinned": True},
        },
    )

    c = Collector(hermes_home)
    try:
        cur = c.collect().curator
    finally:
        c.close()

    assert cur.managed_skill_count == 3
    assert cur.pinned_count == 1
    windows = {window.name: window for window in cur.skill_windows}
    assert windows["really-pinned"].pinned is True
    assert windows["quoted-false"].pinned is False
    assert windows["quoted-zero"].pinned is False


def test_curator_run_record_keeps_the_usage_rollup_and_thresholds(hermes_home: Path):
    """A populated run must not drop the usage rollup or the thresholds.

    Three files describe one curation pass: ``run.json`` holds the transition
    counts, ``skills/.usage.json`` holds the state and patch-reuse rollup, and
    ``config.yaml`` holds the thresholds. Building the populated run from a
    fresh ``CuratorRun`` discarded the last two, so panel 13 showed no hygiene
    section at all on a home whose usage file manages dozens of skills.
    """
    now = time.time()
    _write_curator_run(hermes_home, "20260610-133539", _LIVE_SHAPE)
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"curator": {"stale_after_days": 7, "archive_after_days": 9}})
    )
    _write_usage(
        hermes_home,
        {
            "research": {
                "state": "active",
                "patch_generation": 3,
                "last_reused_patch_generation": 1,
                "last_used_at": iso_ago(86400, now=now),
                "pinned": True,
            },
            "old-habit": {"state": "stale", "last_used_at": iso_ago(8 * 86400, now=now)},
            "archived-one": {"state": "archived"},
        },
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    cur = state.curator
    # The run report still wins for the fields it owns ...
    assert cur.run_present is True
    assert cur.model == "MiniMax-M3"
    assert cur.count_before == 8
    # ... and everything the usage file and config own survives it.
    assert cur.managed_skill_count == 3
    assert cur.state_active_count == 1
    assert cur.state_stale_count == 1
    assert cur.state_archived_count == 1
    assert cur.patch_pending_reuse_count == 1
    assert cur.pinned_count == 1
    assert cur.stale_after_days == 7
    assert cur.archive_after_days == 9
    assert cur.thresholds_customized is True
    windows = {window.name: window for window in cur.skill_windows}
    assert windows["old-habit"].days_until_stale == pytest.approx(-1.0, abs=0.01)

    text = render_to_str(render_panel(13, state, Theme(), detail=True), width=120, no_color=True)
    assert re.search(r"Managed\s+3", text)
    assert "stale after 7d · archive after 9d" in text
    assert "curator.stale_after_days" in text
