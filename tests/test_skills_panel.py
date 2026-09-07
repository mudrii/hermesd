"""Tests for [7] Skills / Integrations panel."""

from __future__ import annotations

import re

from hermesd.models import (
    CredentialPoolEntry,
    DashboardState,
    HookInfo,
    MCPServerInfo,
    PluginInfo,
    ProviderInfo,
    SkillInfo,
    SkillsMemory,
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
                    enabled=True,
                )
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
    text = render_to_str(panel, width=100, no_color=True)
    assert "Hooks" in text
    assert "startup-check" in text
    assert "Plugins" in text
    assert "weather" in text
    assert "Dashboard" in text
    assert "MCP Servers" in text
    assert "playwright" in text
    assert "BOOT.md" in text


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
