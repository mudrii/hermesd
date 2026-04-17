from rich.console import Console

from hermesd.models import DashboardState, MemoryOverview
from hermesd.panels import render_panel
from hermesd.theme import Theme


def _render_to_str(panel) -> str:
    console = Console(width=100, force_terminal=True)
    with console.capture() as cap:
        console.print(panel)
    return cap.get()


def test_memory_panel_compact():
    state = DashboardState(
        memory=MemoryOverview(
            provider="supermemory",
            memory_file_count=3,
            soul_excerpt="Remember the operator's habits.",
        )
    )
    panel = render_panel(10, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "Memory" in text
    assert "supermemory" in text
    assert "3" in text


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
    text = _render_to_str(panel)
    assert "Provider" in text
    assert "MEMORY.md" in text
    assert "SOUL Excerpt" in text
    assert "42 words" in text
