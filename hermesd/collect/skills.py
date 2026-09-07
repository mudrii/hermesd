"""Skill, memory and SOUL filesystem readers."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from hermesd.collect.common import _EXCERPT_MAX_CHARS, _as_dict, _read_text_capped


def _count_skills(skills_dir: Path) -> int:
    if not skills_dir.is_dir():
        return 0
    count = 0
    for category_dir in skills_dir.iterdir():
        if not category_dir.is_dir() or category_dir.name.startswith("."):
            continue
        for skill_dir in category_dir.iterdir():
            if skill_dir.is_dir():
                count += 1
    return count


def _word_count(path: Path, root: Path | None = None) -> int:
    if not path.exists():
        return 0
    return len(_read_text_capped(path, root).split())


def _read_soul_excerpt(path: Path, root: Path | None = None) -> str:
    if not path.exists():
        return ""
    for line in _read_text_capped(path, root).splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:_EXCERPT_MAX_CHARS]
    return ""


# Signature of the SKILL.md frontmatter reader. The Collector passes a
# signature-cached reader so an unchanged SKILL.md is parsed once, not per tick.
_FrontmatterReader = Callable[[Path, Path | None], dict[str, Any]]


def _learning_summary(
    skills_dir: Path,
    usage: dict[str, Any],
    *,
    frontmatter: _FrontmatterReader | None = None,
) -> dict[str, int]:
    read_frontmatter = frontmatter if frontmatter is not None else _skill_frontmatter
    usage_metadata = {str(name): _as_dict(raw) for name, raw in usage.items()}
    learned = set(_learned_skill_names(skills_dir))
    pinned = set()
    agent_created = set()

    for name, metadata in usage_metadata.items():
        if _usage_indicates_learned(metadata):
            learned.add(name)
        if bool(metadata.get("pinned")):
            pinned.add(name)
        if str(metadata.get("created_by") or metadata.get("source") or "") == "agent":
            agent_created.add(name)

    learned_dir = skills_dir / "learned"
    if learned_dir.is_dir() and not learned_dir.is_symlink():
        for skill_dir in sorted(learned_dir.iterdir()):
            if not skill_dir.is_dir() or skill_dir.is_symlink():
                continue
            name = skill_dir.name
            metadata = read_frontmatter(skill_dir / "SKILL.md", skills_dir)
            learned.add(name)
            if bool(metadata.get("pinned")):
                pinned.add(name)
            if str(metadata.get("created_by") or metadata.get("source") or "") == "agent":
                agent_created.add(name)

    return {
        "used": len(usage_metadata),
        "learned": len(learned),
        "pinned": len(pinned),
        "agent": len(agent_created),
    }


def _usage_indicates_learned(metadata: dict[str, Any]) -> bool:
    return bool(
        metadata.get("learned")
        or metadata.get("agent_created")
        or metadata.get("profile_skill")
        or metadata.get("pinned")
        or str(metadata.get("created_by") or metadata.get("source") or "") == "agent"
    )


def _learned_skill_names(skills_dir: Path) -> list[str]:
    learned_dir = skills_dir / "learned"
    if not learned_dir.is_dir() or learned_dir.is_symlink():
        return []
    return [
        skill_dir.name
        for skill_dir in sorted(learned_dir.iterdir())
        if skill_dir.is_dir() and not skill_dir.is_symlink()
    ]


def _skill_frontmatter(path: Path, root: Path | None = None) -> dict[str, Any]:
    with contextlib.suppress(yaml.YAMLError):
        lines = _read_text_capped(path, root).splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        frontmatter: list[str] = []
        for line in lines[1:]:
            if line.strip() == "---":
                data = yaml.safe_load("\n".join(frontmatter)) or {}
                return data if isinstance(data, dict) else {}
            frontmatter.append(line)
    return {}


def _memory_card_count(path: Path, root: Path | None = None) -> int:
    return sum(1 for line in _read_text_capped(path, root).splitlines() if line.startswith("## "))


def _skill_description(path: Path, root: Path | None = None) -> str:
    """The `description` field of a SKILL.md frontmatter block, or ""."""
    description = _skill_frontmatter(path, root).get("description")
    return description if isinstance(description, str) else ""
