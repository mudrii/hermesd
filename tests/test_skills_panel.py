"""Tests for [7] Skills / Integrations panel."""

from __future__ import annotations

import re

from rich.console import Console

from hermesd.models import (
    ConfigSummary,
    CredentialPoolEntry,
    DashboardState,
    HookInfo,
    MCPCacheEntry,
    MCPCacheEntryState,
    MCPSchemaCache,
    MCPServerInfo,
    PluginActivation,
    PluginInfo,
    ProviderInfo,
    SkillInfo,
    SkillsMemory,
    SkillsPromptSnapshot,
)
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import build_skills_state, render_to_str


def test_skills_detail_shows_providers_table():
    state = DashboardState(
        skills_memory=SkillsMemory(
            providers=[
                ProviderInfo(name="openai-codex", is_active=True),
                ProviderInfo(name="anthropic", is_active=False),
            ],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = render_to_str(panel, width=200, no_color=True)
    assert "Providers" in text
    assert "openai-codex" in text
    assert "anthropic" in text


def test_skills_detail_shows_skills_by_category():
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=4,
            skill_categories=2,
            providers=[],
            skills=[
                SkillInfo(name="dev-lint", category="dev"),
                SkillInfo(name="dev-test", category="dev"),
                SkillInfo(name="research-arxiv", category="research"),
                SkillInfo(name="research-papers", category="research"),
            ],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = render_to_str(panel, width=200, no_color=True)
    assert "Skills (4 in 2 categories)" in text
    assert "dev" in text
    assert "lint" in text
    assert "research" in text
    assert "arxiv" in text


def test_skills_detail_shows_credential_pools_without_secrets():
    state = DashboardState(
        skills_memory=SkillsMemory(
            providers=[ProviderInfo(name="openai-codex", is_active=True)],
            credential_pools=[
                CredentialPoolEntry(
                    name="openai-codex",
                    label="Primary Codex",
                    auth_type="oauth",
                    source="codex",
                    last_status="ok",
                    request_count=42,
                    priority=1,
                    token_present=True,
                ),
                CredentialPoolEntry(
                    name="anthropic",
                    label="Fallback Anthropic",
                    auth_type="api_key",
                    source="env:ANTHROPIC_API_KEY",
                    last_status="rate_limited",
                    request_count=3,
                    cooldown_remaining="58m",
                    priority=2,
                    token_present=True,
                ),
            ],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = render_to_str(panel, width=100, no_color=True)
    assert "Credential Pools" in text
    assert "Primary Codex" in text
    assert "Fallback Anthropic" in text
    assert "rate_limited" in text
    assert "58m" in text
    assert "Yes" in text
    assert "sk-live-secret" not in text


def test_skills_detail_uses_dash_for_missing_priority():
    state = DashboardState(
        skills_memory=SkillsMemory(
            providers=[ProviderInfo(name="openai-codex", is_active=True)],
            credential_pools=[
                CredentialPoolEntry(
                    name="openai-codex",
                    label="Primary Codex",
                    auth_type="oauth",
                    source="codex",
                    last_status="ok",
                    request_count=42,
                    cooldown_remaining="ready",
                    priority=0,
                    token_present=True,
                ),
            ],
        ),
    )

    panel = render_panel(7, state, Theme(), detail=True)
    text = render_to_str(panel, width=140, no_color=True)
    line = next(
        rendered_line for rendered_line in text.splitlines() if "Primary Codex" in rendered_line
    )

    assert "—" in line


def test_skills_detail_no_skills():
    state = DashboardState(
        skills_memory=SkillsMemory(
            providers=[ProviderInfo(name="anthropic")],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = render_to_str(panel, width=200, no_color=True)
    assert "Providers" in text
    assert "anthropic" in text
    # No skills section when empty


def test_skills_compact_shows_summary():
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=77,
            skill_categories=39,
            providers=[ProviderInfo(name="openai-codex", is_active=True)],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=False)
    text = render_to_str(panel, width=100, no_color=True)
    assert re.search(r"Skills:\s+77\s+\(39 cat\)", text)
    assert "openai-codex" in text


def test_skills_detail_shows_description_column():
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=2,
            skill_categories=1,
            providers=[],
            skills=[
                SkillInfo(
                    name="apple-notes",
                    category="apple",
                    description="Manage Apple Notes via memo CLI",
                ),
                SkillInfo(
                    name="apple-reminders",
                    category="apple",
                    description="Manage Apple Reminders via remindctl",
                ),
            ],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = render_to_str(panel, width=100, no_color=True)
    assert "Description" in text
    assert "Manage Apple Notes" in text
    assert "Manage Apple Reminders" in text


def test_skills_detail_shows_integrations_sections():
    state = DashboardState(
        skills_memory=SkillsMemory(
            providers=[ProviderInfo(name="openai-codex", is_active=True)],
            hooks=[
                HookInfo(
                    name="startup-check",
                    description="Run startup validation",
                    events=["gateway:startup", "agent:start"],
                )
            ],
            plugins=[
                PluginInfo(
                    name="weather",
                    version="1.2.3",
                    description="Weather tools and alerts",
                    tool_count=2,
                    hook_count=1,
                    dashboard_enabled=True,
                    activation=PluginActivation.ENABLED,
                ),
                PluginInfo(
                    name="notes",
                    activation=PluginActivation.NOT_ENABLED,
                    activation_reason="not enabled in config",
                ),
                PluginInfo(
                    name="memx",
                    kind="exclusive",
                    activation=PluginActivation.CATEGORY_OWNED,
                    activation_reason="exclusive plugin — activate via memory.provider config",
                ),
            ],
            mcp_servers=[
                MCPServerInfo(
                    name="playwright",
                    enabled=True,
                    transport="command",
                    target="npx @playwright/mcp@latest",
                    tool_filter="browser_navigate,browser_screenshot",
                )
            ],
            boot_md_present=True,
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = render_to_str(panel, width=200, no_color=True)
    assert "Hooks" in text
    assert "startup-check" in text
    assert "Plugins" in text
    assert "weather" in text
    assert "Dashboard" in text
    assert "MCP Servers" in text
    assert "playwright" in text
    assert "BOOT.md" in text


def test_skills_detail_shows_configured_activation_not_a_yes_no_flag():
    """F01: a discovered plugin is not enabled just because it is on disk."""
    state = DashboardState(
        skills_memory=SkillsMemory(
            plugins=[
                PluginInfo(name="weather", activation=PluginActivation.ENABLED),
                PluginInfo(name="notes", activation=PluginActivation.NOT_ENABLED),
                PluginInfo(name="blocked", activation=PluginActivation.DISABLED),
                PluginInfo(name="memx", activation=PluginActivation.CATEGORY_OWNED),
                PluginInfo(name="nemo_relay", activation=PluginActivation.REMOVED),
                PluginInfo(name="broken", activation=PluginActivation.UNKNOWN),
            ]
        )
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "Activation" in text
    assert "not enabled" in text
    assert "disabled" in text
    assert "category" in text
    assert "removed" in text
    assert "unknown" in text
    assert "Enabled" not in text.split("MCP Servers")[0]


def test_skills_detail_scroll_offset():
    state = build_skills_state(
        30, category="cat", name_template="skill-{i}", description_template="Desc {i}"
    )
    # Scroll to offset 5 (the window is 20 rows, so 30 skills leave room)
    panel = render_panel(7, state, Theme(), detail=True, scroll_offset=5)
    text = render_to_str(panel, width=100, no_color=True)
    # Should show "↑" indicator for scrolled content
    assert "↑" in text
    # Names sort lexicographically, so offset 5 starts at "skill-13";
    # the first rows (skill-0, skill-1, skill-10...) are scrolled off.
    assert "skill-13" in text
    assert "skill-0" not in text
    assert "skill-12" not in text


def test_skills_detail_small_list_shows_every_row_and_no_hint():
    """A list shorter than the window renders whole, with no scroll hint."""
    state = build_skills_state(5)
    panel = render_panel(7, state, Theme(), detail=True, scroll_offset=0)
    text = render_to_str(panel, width=100, no_color=True)
    assert "skill-00" in text
    assert "skill-04" in text
    assert "/5]" not in text


def test_skills_detail_caps_visible_window():
    state = build_skills_state(40)
    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=100, no_color=True)
    assert "[1-20/40]" in text
    assert "skill-39" not in text


def test_skills_detail_scroll_pages_the_window():
    state = build_skills_state(40)
    text = render_to_str(
        render_panel(7, state, Theme(), detail=True, scroll_offset=20), width=100, no_color=True
    )
    assert "[21-40/40]" in text
    assert "skill-39" in text


def test_skills_detail_scroll_clamps_to_full_window():
    """Scrolling past the end must clamp to a full window, not a 1-row stub."""
    state = build_skills_state(40)
    text = render_to_str(
        render_panel(7, state, Theme(), detail=True, scroll_offset=39), width=100, no_color=True
    )
    assert "[21-40/40]" in text
    assert "skill-20" in text


def test_detail_max_scroll_offset_skills_accounts_for_window():
    from hermesd.app import _SKILLS_PANEL_NUM, _detail_max_scroll_offset

    state = build_skills_state(30)
    assert _detail_max_scroll_offset(_SKILLS_PANEL_NUM, state, "", "") == 10


def test_skills_detail_uses_dash_for_empty_descriptions_after_scrolling():
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=2,
            skill_categories=1,
            providers=[],
            skills=[
                SkillInfo(name="cat-skill-0", category="cat", description="Visible"),
                SkillInfo(name="cat-skill-1", category="cat", description=""),
            ],
        ),
    )

    panel = render_panel(7, state, Theme(), detail=True, scroll_offset=1)
    text = render_to_str(panel, width=100, no_color=True)

    assert "—" in text


def test_skills_detail_does_not_truncate_long_descriptions():
    long_description = "A" * 81 + "tail"
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=1,
            skill_categories=1,
            providers=[],
            skills=[SkillInfo(name="cat-skill", category="cat", description=long_description)],
        ),
    )

    panel = render_panel(7, state, Theme(), detail=True)
    text = render_to_str(panel, width=200, no_color=True)

    assert long_description in text


def _export_skills_detail_text(state: DashboardState, scroll_offset: int) -> str:
    """Render the skills detail view through a recording console and export it."""
    console = Console(width=120, height=120, record=True)
    console.print(render_panel(7, state, Theme(), detail=True, scroll_offset=scroll_offset))
    return console.export_text()


def test_skills_detail_negative_scroll_offset_renders_from_the_top():
    state = build_skills_state(40, description_template="description {i}")

    negative = _export_skills_detail_text(state, -5)

    # A negative offset must clamp to the top, not slice from the end.
    assert negative == _export_skills_detail_text(state, 0)
    assert "skill-00" in negative
    assert "skill-39" not in negative


def _mcp_state(**kwargs) -> DashboardState:
    return DashboardState(
        config=ConfigSummary(
            mcp_server_count=kwargs.pop("configured_count", 0),
            mcp_server_names=kwargs.pop("configured_names", []),
        ),
        mcp_cache=MCPSchemaCache(**kwargs.pop("cache", {})),
        skills_prompt=SkillsPromptSnapshot(**kwargs.pop("prompt", {})),
        skills_memory=SkillsMemory(**kwargs),
    )


def test_skills_detail_shows_mcp_cache_section():
    state = _mcp_state(
        configured_count=3,
        configured_names=["notion", "playwright", "sheets"],
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 2,
            "mcp_cached_server_names": ["playwright", "sheets"],
            "mcp_schema_cache_age_seconds": 300.0,
            "mcp_uncached_server_count": 1,
            "mcp_uncached_server_names": ["notion"],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "MCP" in text
    assert "playwright" in text
    assert "sheets" in text
    assert "5m" in text
    assert "notion" in text


def test_skills_detail_says_no_cache_entry_instead_of_never_connected():
    """Cache absence is an observation about this read, not a connection history."""
    state = _mcp_state(
        configured_count=2,
        configured_names=["notion", "playwright"],
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["playwright"],
            "mcp_uncached_server_count": 1,
            "mcp_uncached_server_names": ["notion"],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "No cache entry" in text
    assert "notion" in text
    assert "Never connected" not in text
    assert "never connected" not in text.lower()


def test_skills_detail_flags_uncached_names_omitted_by_the_display_cap():
    """A bounded list must not silently look complete (the F02 truncation bug)."""
    names = [f"srv-{index:02d}" for index in range(20)]
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["alpha"],
            "mcp_uncached_server_count": 25,
            "mcp_uncached_server_names": names,
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "(+5 more)" in text


def test_skills_detail_reports_absent_cache_file():
    text = render_to_str(
        render_panel(7, _mcp_state(), Theme(), detail=True), width=200, no_color=True
    )

    assert "MCP" in text
    assert "no cache file observed" in text


def test_skills_detail_shows_dash_when_cache_age_is_unknown():
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["playwright"],
            "mcp_schema_cache_age_seconds": None,
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "Cache age" in text
    assert "—" in text


def test_skills_detail_shows_prompted_skill_line():
    state = _mcp_state(prompt={"prompted_skill_count": 3, "prompt_snapshot_age_seconds": 7200.0})

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "Prompted skills: 3 (snapshot 2h ago)" in text


def test_skills_detail_without_cache_or_snapshot_renders_placeholder():
    text = render_to_str(
        render_panel(7, _mcp_state(), Theme(), detail=True), width=200, no_color=True
    )

    assert "MCP" in text
    assert "no cache file observed" in text
    assert "Prompted skills" not in text


def test_skills_compact_shows_cached_mcp_count_when_present():
    state = _mcp_state(cache={"mcp_cached_server_count": 2})

    text = render_to_str(render_panel(7, state, Theme(), detail=False), width=200, no_color=True)

    assert "mcp 2 cached" in text


def test_skills_compact_hides_cached_mcp_count_when_zero():
    text = render_to_str(
        render_panel(7, _mcp_state(), Theme(), detail=False), width=200, no_color=True
    )

    assert "cached" not in text


def test_skills_detail_escapes_markup_hostile_cached_names():
    state = _mcp_state(
        configured_count=1,
        configured_names=["[bold]never-cached\x1b[2J"],
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["[red]evil\x1b]0;pwn\x07"],
            "mcp_schema_cache_age_seconds": 60.0,
            "mcp_uncached_server_count": 1,
            "mcp_uncached_server_names": ["[bold]never-cached\x1b[2J"],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "[red]evil" in text
    assert "[bold]never-cached" in text
    assert "\x1b]0;pwn" not in text
    assert "\x1b[2J" not in text


# --------------------------------------------------------------------------
# MCP schema-cache entry validity
# --------------------------------------------------------------------------


def _cache_entry(**overrides: object) -> MCPCacheEntry:
    entry: dict[str, object] = {"name": "codegraph", "fingerprint": "7df47d93"}
    entry.update(overrides)
    return MCPCacheEntry(**entry)  # type: ignore[arg-type]


def _flat(text: str) -> str:
    """Collapse rendered whitespace so a wrapped prose note can be matched."""
    return " ".join(text.split())


def test_skills_detail_lists_entry_validity_counts():
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 3,
            "mcp_cached_server_names": ["a", "b", "c"],
            "mcp_valid_entry_count": 1,
            "mcp_expired_entry_count": 1,
            "mcp_unassessable_entry_count": 1,
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "Entry validity" in text
    assert "1 valid" in text
    assert "1 expired" in text
    assert "1 unassessable" in text


def test_skills_detail_shows_dash_when_no_entry_could_be_assessed():
    state = _mcp_state(
        cache={"mcp_cache_present": True, "mcp_cached_server_count": 0},
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "Entry validity" in text


def test_skills_detail_renders_the_zero_ttl_expired_entry():
    """The live-home shape: one entry, ``ttl_ms: 0``, so it is never served."""
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["codegraph"],
            "mcp_expired_entry_count": 1,
            "mcp_entries": [
                _cache_entry(
                    state=MCPCacheEntryState.EXPIRED,
                    ttl_ms=0.0,
                    age_seconds=42158.0,
                )
            ],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "codegraph" in text
    assert "expired" in text
    assert "0ms ttl elapsed 11h ago" in text
    assert "fp 7df47d93" in text


def test_skills_detail_renders_a_valid_entry_with_ttl_remaining():
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["srv"],
            "mcp_valid_entry_count": 1,
            "mcp_entries": [
                _cache_entry(
                    name="srv",
                    state=MCPCacheEntryState.VALID,
                    ttl_ms=600_000.0,
                    age_seconds=30.0,
                    remaining_seconds=570.0,
                )
            ],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "9m of 10m ttl left" in text


def test_skills_detail_says_a_valid_entry_without_ttl_never_expires():
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["srv"],
            "mcp_valid_entry_count": 1,
            "mcp_entries": [_cache_entry(name="srv", state=MCPCacheEntryState.VALID)],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "no ttl recorded" in text
    assert "never expires" in text


def test_skills_detail_says_a_ttl_without_written_at_never_expires():
    """Upstream needs both numbers to expire an entry, so this one stays valid."""
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["srv"],
            "mcp_valid_entry_count": 1,
            "mcp_entries": [
                _cache_entry(
                    name="srv",
                    state=MCPCacheEntryState.VALID,
                    ttl_ms=1.0,
                    age_seconds=None,
                    remaining_seconds=None,
                )
            ],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "no written_at recorded — never expires" in text


def test_skills_detail_renders_a_sub_second_ttl_in_milliseconds():
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["srv"],
            "mcp_valid_entry_count": 1,
            "mcp_entries": [
                _cache_entry(
                    name="srv",
                    state=MCPCacheEntryState.VALID,
                    ttl_ms=250.0,
                    age_seconds=0.1,
                    remaining_seconds=0.15,
                )
            ],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "150ms of 250ms ttl left" in text


def test_skills_detail_renders_an_unassessable_entry_reason():
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["srv"],
            "mcp_unassessable_entry_count": 1,
            "mcp_entries": [
                _cache_entry(
                    name="srv",
                    state=MCPCacheEntryState.UNASSESSABLE,
                    reason="no usable fingerprint recorded",
                    fingerprint="",
                )
            ],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "unassessable" in text
    assert "no usable fingerprint recorded" in text
    assert "fp " not in text


def test_skills_detail_validity_never_claims_credentials_or_connectivity():
    """A matching TTL says nothing about auth or reachability, and an expired
    entry is not an error — so the section must not imply either."""
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 2,
            "mcp_cached_server_names": ["ok", "srv"],
            "mcp_valid_entry_count": 1,
            "mcp_expired_entry_count": 1,
            "mcp_entries": [
                _cache_entry(name="ok", state=MCPCacheEntryState.VALID),
                _cache_entry(
                    name="srv",
                    state=MCPCacheEntryState.EXPIRED,
                    ttl_ms=0.0,
                    age_seconds=10.0,
                ),
            ],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)
    # The note is prose and wraps, so compare against a whitespace-flat copy.
    lowered = _flat(text).lower()

    assert "does not prove credentials work" in lowered
    assert "not an error" in lowered
    assert "re-probes" in lowered
    assert "mismatch" not in lowered
    assert "unreachable" not in lowered
    assert "authenticated" not in lowered


def test_skills_detail_says_the_fingerprint_is_the_cache_own_record():
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["srv"],
            "mcp_valid_entry_count": 1,
            "mcp_entries": [_cache_entry(name="srv", state=MCPCacheEntryState.VALID)],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "fingerprint shown is the cache's own record" in _flat(text)


def test_skills_detail_bounds_the_entry_list_without_hiding_the_count():
    entries = [
        _cache_entry(name=f"srv-{index:02d}", state=MCPCacheEntryState.EXPIRED, ttl_ms=0.0)
        for index in range(20)
    ]
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 25,
            "mcp_cached_server_names": [f"srv-{index:02d}" for index in range(20)],
            "mcp_expired_entry_count": 25,
            "mcp_entries": entries,
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    # The rendered list is capped, but the row above still reports all 25.
    assert "25 expired" in text
    assert "(+5 more)" in text
    assert text.count("expired —") == 20


def test_skills_compact_marks_expired_cache_entries():
    state = _mcp_state(
        cache={"mcp_cached_server_count": 2, "mcp_expired_entry_count": 1},
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=False), width=200, no_color=True)

    assert "mcp 2 cached (1 expired)" in text


def test_skills_compact_marks_unassessable_cache_entries():
    state = _mcp_state(
        cache={"mcp_cached_server_count": 3, "mcp_unassessable_entry_count": 2},
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=False), width=200, no_color=True)

    assert "mcp 3 cached (2 unassessable)" in text


def test_skills_compact_leaves_an_all_valid_cache_unannotated():
    state = _mcp_state(
        cache={"mcp_cached_server_count": 2, "mcp_valid_entry_count": 2},
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=False), width=200, no_color=True)

    assert "mcp 2 cached" in text
    assert "(" not in text.split("Schema cache:")[1]


def test_skills_detail_escapes_markup_hostile_cache_entries():
    state = _mcp_state(
        cache={
            "mcp_cache_present": True,
            "mcp_cached_server_count": 1,
            "mcp_cached_server_names": ["[red]srv\x1b]0;pwn\x07"],
            "mcp_unassessable_entry_count": 1,
            "mcp_entries": [
                _cache_entry(
                    name="[red]srv\x1b]0;pwn\x07",
                    state=MCPCacheEntryState.UNASSESSABLE,
                    reason="[/] reason [x]\x1b[2J",
                    fingerprint="[/]abcde",
                )
            ],
        },
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=200, no_color=True)

    assert "[red]srv" in text
    assert "[/] reason [x]" in text
    assert "[/]abcde" in text
    assert "\x1b]0;pwn" not in text
    assert "\x1b[2J" not in text


# --------------------------------------------------------------------------
# plugin provenance and declarations
# --------------------------------------------------------------------------

CATALOG_SHA = "a" * 40
INSTALLED_SHA = "b" * 40


def _plugins_state(*plugins: PluginInfo, **skills_memory: object) -> DashboardState:
    return DashboardState(skills_memory=SkillsMemory(plugins=list(plugins), **skills_memory))


def _plugins_text(*plugins: PluginInfo, **skills_memory: object) -> str:
    return render_to_str(
        render_panel(7, _plugins_state(*plugins, **skills_memory), Theme(), detail=True),
        width=200,
        no_color=True,
    )


def _section(text: str) -> str:
    """The Plugins table only, so an assertion cannot be satisfied by another section."""
    return text.split("Plugins", 1)[1].split("MCP Servers", 1)[0]


def test_skills_detail_has_a_provenance_column():
    text = _plugins_text(PluginInfo(name="weather", catalog_sha=CATALOG_SHA))

    assert "Provenance" in text


def test_a_catalog_install_renders_upstreams_own_annotation():
    text = _section(
        _plugins_text(PluginInfo(name="weather", catalog_sha=CATALOG_SHA, catalog_tier="official"))
    )

    assert f"catalog:official@{CATALOG_SHA[:8]}" in text


def test_a_catalog_tier_is_defaulted_at_render_time_not_in_the_data():
    """catalog_annotation falls back to 'community'; the model keeps what was written."""
    text = _section(_plugins_text(PluginInfo(name="weather", catalog_sha=CATALOG_SHA)))

    assert f"catalog:community@{CATALOG_SHA[:8]}" in text


def test_a_catalog_sidecar_with_no_usable_sha_still_names_the_install():
    text = _section(
        _plugins_text(PluginInfo(name="weather", catalog_name="weather", catalog_tier="official"))
    )

    assert "catalog:official" in text
    assert "catalog:official@" not in text


def test_an_unpinned_git_install_renders_its_installed_head():
    text = _section(_plugins_text(PluginInfo(name="weather", installed_revision=INSTALLED_SHA)))

    assert f"git@{INSTALLED_SHA[:8]}" in text
    assert "pinned" not in text


def test_a_ref_install_renders_as_pinned():
    text = _section(
        _plugins_text(
            PluginInfo(
                name="weather",
                installed_revision=INSTALLED_SHA,
                pinned_revision=INSTALLED_SHA,
            )
        )
    )

    assert f"git pinned@{INSTALLED_SHA[:8]}" in text


def test_agreeing_catalog_and_installed_revisions_are_not_rendered_twice():
    text = _section(
        _plugins_text(
            PluginInfo(
                name="weather",
                catalog_sha=CATALOG_SHA,
                catalog_tier="official",
                installed_revision=CATALOG_SHA,
            )
        )
    )

    assert f"catalog:official@{CATALOG_SHA[:8]}" in text
    assert text.count(CATALOG_SHA[:8]) == 1
    assert "drift" not in text


def test_a_ref_install_off_the_catalog_pin_shows_both_shas_and_a_drift_marker():
    """The disagreement is the signal: neither SHA may be dropped to tidy the cell."""
    text = _section(
        _plugins_text(
            PluginInfo(
                name="weather",
                catalog_sha=CATALOG_SHA,
                catalog_tier="official",
                installed_revision=INSTALLED_SHA,
                pinned_revision=INSTALLED_SHA,
            )
        )
    )

    assert f"catalog:official@{CATALOG_SHA[:8]}" in text
    assert f"git pinned@{INSTALLED_SHA[:8]}" in text
    assert "⚠" in text
    assert "drift" in text


def test_a_plugin_with_no_provenance_at_all_renders_a_dash():
    text = _section(_plugins_text(PluginInfo(name="weather")))

    assert "—" in text


def test_declared_version_gates_and_capabilities_are_labelled_as_declarations():
    text = _plugins_text(
        PluginInfo(
            name="weather",
            requires_hermes=">=0.19",
            declared_capabilities=["tools.override", "llm.model_override"],
            declared_capability_count=2,
        )
    )

    assert "Declares" in text
    assert ">=0.19" in text
    assert "caps:2" in text


def test_the_declared_capability_count_survives_a_truncated_list():
    """The cell reports what was declared, not what the display kept."""
    text = _section(
        _plugins_text(
            PluginInfo(
                name="weather",
                declared_capabilities=[f"cap-{i}" for i in range(8)],
                declared_capability_count=40,
            )
        )
    )

    assert "caps:40" in text


def test_a_plugin_declaring_nothing_renders_no_declaration_tokens():
    row = next(
        line
        for line in _section(_plugins_text(PluginInfo(name="weather"))).splitlines()
        if "weather" in line
    )

    assert "caps:" not in row
    assert ">=" not in row
    # Both the Declares and the Provenance cell fall back to a dash.
    assert row.count("—") == 2


def test_the_panel_says_provenance_is_not_proof_a_plugin_loads():
    text = _plugins_text(PluginInfo(name="weather", catalog_sha=CATALOG_SHA))

    assert "never imports plugin code" in text
    assert "declares" in text.lower() or "declaration" in text.lower()


def test_a_conflicting_manifest_pair_is_reported_not_silently_resolved():
    text = _plugins_text(
        PluginInfo(
            name="weather",
            manifest_file="plugin.yaml",
            manifest_shadowed=["plugin.yml", "plugin.json"],
        )
    )

    assert "weather" in text
    assert "plugin.yaml" in text
    assert "plugin.yml" in text
    assert "plugin.json" in text
    assert "1 plugin carries" in text


def test_conflicting_manifests_are_listed_with_their_true_count_past_the_cap():
    plugins = [
        PluginInfo(
            name=f"plug-{i}",
            manifest_file="plugin.yaml",
            manifest_shadowed=["plugin.json"],
        )
        for i in range(6)
    ]

    text = _plugins_text(*plugins)
    note = text.split("conflicting manifests", 1)[1]

    assert "6 plugins" in text
    assert "plug-0" in note
    assert "(+3 more)" in note
    # The omitted names must not appear in the note, which would read as complete.
    assert "plug-5" not in note


def test_a_truncated_plugin_scan_is_reported():
    text = _plugins_text(PluginInfo(name="weather"), plugin_scan_truncated=True)

    assert "truncated" in text


def test_an_untruncated_scan_says_nothing_about_truncation():
    text = _plugins_text(PluginInfo(name="weather"))

    assert "truncated" not in text


def test_skills_compact_marks_a_truncated_plugin_count():
    """The compact count is a retained count, so the cap has to be visible there too."""
    state = _plugins_state(PluginInfo(name="weather"), plugin_scan_truncated=True)

    text = render_to_str(render_panel(7, state, Theme(), detail=False), width=200, no_color=True)

    assert "1+ plug" in text


def test_skills_compact_leaves_an_untruncated_plugin_count_bare():
    state = _plugins_state(PluginInfo(name="weather"))

    text = render_to_str(render_panel(7, state, Theme(), detail=False), width=200, no_color=True)

    assert "1 plug" in text


def test_provenance_cells_are_markup_escaped():
    """The tier and the shadowed-manifest names come from files, so both are escaped."""
    text = _plugins_text(
        PluginInfo(
            name="weather",
            catalog_sha=CATALOG_SHA,
            catalog_tier="[red]y\x1b]0;pwn\x07",
            manifest_file="plugin.yaml",
            manifest_shadowed=["[bold]plugin.json\x1b[2J"],
            requires_hermes="[blink]>=0.19",
        )
    )

    assert "[red]y" in text
    assert "[bold]plugin.json" in text
    assert "[blink]>=0.19" in text
    assert "\x1b[2J" not in text
    assert "\x1b]0;pwn" not in text


def test_plugins_conflict_note_sanitizes_instead_of_escaping():
    """``Text.append`` skips markup parsing, so escaping leaks a literal backslash.

    The existing markup test asserts ``"[bold]plugin.json" in text``, which passes
    either way because ``\\[bold]plugin.json`` contains that substring — the
    backslash is the actual failure signature. Control bytes must still be
    stripped, which is what sanitizing (not escaping) does here.
    """
    text = render_to_str(
        render_panel(
            7,
            DashboardState(
                skills_memory=SkillsMemory(
                    plugins=[
                        PluginInfo(
                            name="[bold]tracer\x1b[2J",
                            manifest_file="plugin.yaml\x1b[2J",
                            manifest_shadowed=["[bold]plugin.yml"],
                            activation=PluginActivation.NOT_ENABLED,
                        )
                    ]
                )
            ),
            Theme(),
            detail=True,
        ),
        width=200,
        no_color=True,
    )

    assert "\\[bold]" not in text
    assert "[bold]tracer: plugin.yaml used" in text
    assert "[bold]plugin.yml ignored" in text
    assert "\x1b[2J" not in text
