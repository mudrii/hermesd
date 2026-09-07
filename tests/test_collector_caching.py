"""Signature-keyed caching of derived file values.

Each test counts real `Path.open` calls on one source file (observable
behaviour) rather than reaching into the collector's cache dicts, following
tests/test_collector_coverage.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from hermesd.collector import Collector


def _count_opens(monkeypatch: pytest.MonkeyPatch, target: Path) -> list[Path]:
    opens: list[Path] = []
    real_open = Path.open

    def counting_open(self: Path, *args: object, **kwargs: object) -> object:
        if self == target:
            opens.append(self)
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", counting_open)
    return opens


@pytest.fixture
def collector(hermes_home: Path) -> Iterator[Collector]:
    c = Collector(hermes_home)
    try:
        yield c
    finally:
        c.close()


def _assert_cached_until_changed(
    collector: Collector,
    opens: list[Path],
    source: Path,
    rewrite: Callable[[], None],
) -> None:
    collector.collect()
    assert opens, f"cold collect never opened {source.name}"

    opens.clear()
    collector.collect()
    assert opens == [], f"unchanged {source.name} was re-read on the second collect"

    rewrite()
    opens.clear()
    collector.collect()
    assert opens, f"changed {source.name} was not re-read"


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
