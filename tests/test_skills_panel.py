"""Tests for [7] Skills/Providers panel — two-table detail layout."""
from rich.console import Console

from hermesd.models import DashboardState, ProviderInfo, SkillInfo, SkillsMemory
from hermesd.theme import Theme
from hermesd.panels import render_panel


def _render_to_str(panel) -> str:
    console = Console(width=100, force_terminal=True)
    with console.capture() as cap:
        console.print(panel)
    return cap.get()


def test_skills_detail_shows_providers_table():
    state = DashboardState(
        skills_memory=SkillsMemory(
            providers=[
                ProviderInfo(name="openai-codex", is_active=True),
                ProviderInfo(name="anthropic", is_active=False),
            ],
            memory_file_count=3,
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "Providers" in text
    assert "openai-codex" in text
    assert "anthropic" in text
    assert "Memory files: 3" in text


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
    text = _render_to_str(panel)
    assert "Skills (4 in 2 categories)" in text
    assert "dev" in text
    assert "lint" in text
    assert "research" in text
    assert "arxiv" in text


def test_skills_detail_no_skills():
    state = DashboardState(
        skills_memory=SkillsMemory(
            providers=[ProviderInfo(name="anthropic")],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "Providers" in text
    assert "anthropic" in text
    # No skills section when empty


def test_skills_compact_shows_summary():
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=77,
            skill_categories=39,
            memory_file_count=2,
            providers=[ProviderInfo(name="openai-codex", is_active=True)],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "77" in text
    assert "39 cat" in text
    assert "2 files" in text


def test_skills_detail_shows_description_column():
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=2,
            skill_categories=1,
            providers=[],
            skills=[
                SkillInfo(name="apple-notes", category="apple",
                          description="Manage Apple Notes via memo CLI"),
                SkillInfo(name="apple-reminders", category="apple",
                          description="Manage Apple Reminders via remindctl"),
            ],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "Description" in text
    assert "Manage Apple Notes" in text
    assert "Manage Apple Reminders" in text


def test_skills_detail_scroll_offset():
    skills = [
        SkillInfo(name=f"skill-{i}", category="cat", description=f"Desc {i}")
        for i in range(20)
    ]
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=20, skill_categories=1, providers=[], skills=skills,
        ),
    )
    # Scroll to offset 5
    panel = render_panel(7, state, Theme(), detail=True, scroll_offset=5)
    text = _render_to_str(panel)
    # Should show "↑" indicator for scrolled content
    assert "↑" in text
    # First visible should be skill-5
    assert "skill-5" in text


def test_skills_detail_scroll_offset_zero():
    skills = [
        SkillInfo(name=f"skill-{i}", category="cat", description=f"Desc {i}")
        for i in range(5)
    ]
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=5, skill_categories=1, providers=[], skills=skills,
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True, scroll_offset=0)
    text = _render_to_str(panel)
    assert "skill-0" in text
