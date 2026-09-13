"""Skill, memory and SOUL filesystem readers."""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from hermesd.collect.common import (
    _EXCERPT_MAX_CHARS,
    _as_dict,
    _as_list,
    _coerce_bool,
    _read_text_capped,
)
from hermesd.models import (
    MCPCacheEntry,
    MCPCacheEntryState,
    MCPSchemaCache,
    SkillsPromptSnapshot,
    ToolsetAvailability,
)

# Upper bound on cached server names surfaced from the MCP schema cache.
_MAX_LISTED_NAMES = 20
# Display bound on the per-entry validity list, and on the recorded fingerprint
# prefix carried per entry. Neither bound feeds a count.
_MAX_CACHE_ENTRIES = 20
_FINGERPRINT_PREFIX_CHARS = 8
_NOT_A_MAPPING = "entry is not a mapping"
_NO_FINGERPRINT = "no usable fingerprint recorded"


def _toolset_availability(data: dict[str, Any]) -> ToolsetAvailability:
    """Summarize the ``availability`` block of ``cache/banner_snapshot.json``.

    ``unavailable_toolsets`` ships as ``{name, env_vars, tools}`` mappings on
    hermes-agent 0.21; plain name strings are accepted too. Any other shape
    contributes nothing rather than guessing.
    """
    availability = _as_dict(data.get("availability"))
    return ToolsetAvailability(
        enabled_toolsets=_name_list(data.get("enabled_toolsets")),
        unavailable_toolsets=_name_list(availability.get("unavailable_toolsets")),
        lazy_tool_count=len(_as_list(availability.get("lazy_tools"))),
        disabled_tool_count=len(_as_list(availability.get("disabled_tools"))),
    )


def _name_list(value: object) -> list[str]:
    """Sorted names from a list of strings or of ``{"name": ...}`` mappings."""
    names = set()
    for entry in _as_list(value):
        if isinstance(entry, str) and entry:
            names.add(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]:
            names.add(entry["name"])
    return sorted(names)


def _cache_number(value: object) -> float | None:
    """``float(value)`` when upstream would treat it as a number, else None.

    ``isinstance(True, int)`` holds, so a JSON ``true`` sitting in a numeric
    slot counts as a number here exactly as it does in ``get_cached_entry``
    (``tools/mcp_schema_cache.py:66-70``): mirroring the arithmetic means
    mirroring its type test, including the parts that look like bugs.
    """
    if isinstance(value, int | float):
        return float(value)
    return None


def _assess_mcp_cache_entry(name: str, raw: object, now: float) -> MCPCacheEntry:
    """Decide whether hermes-agent would serve this entry, from its own numbers.

    Reproduces ``get_cached_entry`` (``tools/mcp_schema_cache.py:59-73``) minus
    the fingerprint comparison hermesd cannot make — see
    ``MCPCacheEntryState``'s docstring for why the interpolated, default-merged
    config upstream hashed is out of reach. Expiry uses the entry's own
    ``written_at``, never the file mtime, and the elapsed-milliseconds
    expression is left **unclamped** so a future-dated ``written_at`` behaves as
    it does upstream; only the displayed age is clamped at zero.

    ``ttl_ms: 0`` is therefore always expired (``age >= 0`` holds for any real
    clock), and no numeric TTL means the entry never expires at all. Neither is
    an error: an expired entry just makes the next call re-probe the server.
    """
    if not isinstance(raw, dict):
        return MCPCacheEntry(name=name, reason=_NOT_A_MAPPING)
    recorded = raw.get("fingerprint")
    if not isinstance(recorded, str) or not recorded:
        return MCPCacheEntry(name=name, reason=_NO_FINGERPRINT)

    ttl_ms = _cache_number(raw.get("ttl_ms"))
    written_at = _cache_number(raw.get("written_at"))
    age_seconds = None if written_at is None else max(0.0, now - written_at)
    state = MCPCacheEntryState.VALID
    remaining: float | None = None
    # Upstream needs BOTH numbers before it can expire anything; one alone
    # leaves the entry permanently servable.
    if ttl_ms is not None and written_at is not None:
        elapsed_ms = (now - written_at) * 1000.0
        if elapsed_ms >= ttl_ms:
            state = MCPCacheEntryState.EXPIRED
        else:
            window = (ttl_ms - elapsed_ms) / 1000.0
            remaining = window if math.isfinite(window) else None
    return MCPCacheEntry(
        name=name,
        state=state,
        fingerprint=recorded[:_FINGERPRINT_PREFIX_CHARS],
        ttl_ms=ttl_ms,
        age_seconds=age_seconds,
        remaining_seconds=remaining,
    )


def _mcp_schema_cache_summary(
    data: dict[str, Any], age_seconds: float | None, configured: list[str], now: float
) -> MCPSchemaCache:
    """Summarize the MCP schema cache mapping; cached payloads stay opaque.

    Membership is decided from the complete name sets on both sides and only the
    rendered lists are bounded, so a configured server past the display cap is
    never misreported as having no cache entry. The validity rollups are counted
    over every entry in the file for the same reason: bounding ``mcp_entries``
    must never change what the counts say.
    """
    names = sorted(str(name) for name in data)
    cached = set(names)
    uncached = [name for name in configured if name not in cached]
    counts = dict.fromkeys(MCPCacheEntryState, 0)
    entries: list[MCPCacheEntry] = []
    for key, raw in sorted(data.items(), key=lambda item: str(item[0])):
        entry = _assess_mcp_cache_entry(str(key), raw, now)
        counts[entry.state] += 1
        if len(entries) < _MAX_CACHE_ENTRIES:
            entries.append(entry)
    return MCPSchemaCache(
        mcp_cache_present=True,
        mcp_cached_server_count=len(names),
        mcp_cached_server_names=names[:_MAX_LISTED_NAMES],
        mcp_schema_cache_age_seconds=age_seconds,
        mcp_uncached_server_count=len(uncached),
        mcp_uncached_server_names=uncached[:_MAX_LISTED_NAMES],
        mcp_valid_entry_count=counts[MCPCacheEntryState.VALID],
        mcp_expired_entry_count=counts[MCPCacheEntryState.EXPIRED],
        mcp_unassessable_entry_count=counts[MCPCacheEntryState.UNASSESSABLE],
        mcp_entries=entries,
    )


def _skills_prompt_summary(data: dict[str, Any], age_seconds: float | None) -> SkillsPromptSnapshot:
    """Summarize ``.skills_prompt_snapshot.json``: prompted skill count and age."""
    return SkillsPromptSnapshot(
        prompted_skill_count=len(_as_list(data.get("skills"))),
        prompt_snapshot_age_seconds=age_seconds,
    )


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
        # ``.usage.json`` is a state payload written with real booleans
        # (``set_pinned`` stores ``bool(pinned)``), so a stringified flag is
        # corruption, not truth.
        if _coerce_bool(metadata.get("pinned")):
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
            # Frontmatter is human-authored, like a config value: read the way
            # upstream reads its own settings, not with the state-payload rule.
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
        _coerce_bool(metadata.get("learned"))
        or _coerce_bool(metadata.get("agent_created"))
        or _coerce_bool(metadata.get("profile_skill"))
        or _coerce_bool(metadata.get("pinned"))
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
