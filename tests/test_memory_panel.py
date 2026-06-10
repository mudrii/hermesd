from __future__ import annotations

import re

from hermesd.models import DashboardState, MemoryOverview
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_memory_panel_compact():
    state = DashboardState(
        memory=MemoryOverview(
            provider="supermemory",
            memory_file_count=3,
            soul_excerpt="Remember the operator's habits.",
        )
    )
    panel = render_panel(10, state, Theme(), detail=False)
    text = render_to_str(panel, width=100, no_color=True)
    assert "Memory" in text
    assert "supermemory" in text
    assert re.search(r"Files:\s+3\b", text)


def test_memory_panel_compact_soul_present():
    state = DashboardState(
        memory=MemoryOverview(soul_size_bytes=128, soul_excerpt="Remember the operator.")
    )
    panel = render_panel(10, state, Theme(), detail=False)
    text = render_to_str(panel, width=100, no_color=True)
    assert "SOUL: present" in text


def test_memory_panel_compact_soul_empty_file():
    state = DashboardState(memory=MemoryOverview(soul_size_bytes=2, soul_excerpt=""))
    panel = render_panel(10, state, Theme(), detail=False)
    text = render_to_str(panel, width=100, no_color=True)
    assert "SOUL: empty" in text


def test_memory_panel_compact_soul_missing():
    state = DashboardState(memory=MemoryOverview(soul_size_bytes=0, soul_excerpt=""))
    panel = render_panel(10, state, Theme(), detail=False)
    text = render_to_str(panel, width=100, no_color=True)
    assert "SOUL: none" in text


def test_memory_panel_detail():
    state = DashboardState(
        memory=MemoryOverview(
            provider="supermemory",
            memory_file_count=3,
            memory_word_count=42,
            user_word_count=17,
            soul_size_bytes=128,
            soul_excerpt="Remember the operator's habits.",
            memory_files=["MEMORY.md", "USER.md", "notes.md"],
        )
    )
    panel = render_panel(10, state, Theme(), detail=True)
    text = render_to_str(panel, width=100, no_color=True)
    assert "Provider" in text
    assert "MEMORY.md" in text
    assert "SOUL Excerpt" in text
    assert "42 words" in text


def test_memory_panel_detail_soul_missing():
    state = DashboardState(memory=MemoryOverview(soul_size_bytes=0, soul_excerpt=""))
    panel = render_panel(10, state, Theme(), detail=True)
    text = render_to_str(panel, width=100, no_color=True)
    assert "missing" in text
    assert "SOUL Excerpt" not in text


def test_memory_panel_detail_soul_empty_file():
    state = DashboardState(memory=MemoryOverview(soul_size_bytes=2, soul_excerpt=""))
    panel = render_panel(10, state, Theme(), detail=True)
    text = render_to_str(panel, width=100, no_color=True)
    assert "2 bytes (empty)" in text
