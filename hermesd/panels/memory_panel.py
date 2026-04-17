from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_memory(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    memory = state.memory
    lines = Text()
    lines.append("  Provider: ", style=theme.ui_label)
    lines.append(memory.provider or "builtin", style=theme.banner_text)
    lines.append("\n")
    lines.append("  Files: ", style=theme.ui_label)
    lines.append(f"{memory.memory_file_count}", style=theme.ui_accent)
    lines.append("\n")
    lines.append("  SOUL: ", style=theme.ui_label)
    lines.append("present" if memory.soul_excerpt else "none", style=theme.banner_text)

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[10] Memory[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    memory = state.memory
    sections: list[RenderableType] = []

    summary = Table(box=None, show_header=False, padding=(0, 2))
    summary.add_column("Key", style=theme.ui_label, min_width=14)
    summary.add_column("Value", style=theme.banner_text, ratio=1)
    summary.add_row("Provider", memory.provider or "builtin")
    summary.add_row("Memory Files", str(memory.memory_file_count))
    summary.add_row("MEMORY.md", f"{memory.memory_word_count} words")
    summary.add_row("USER.md", f"{memory.user_word_count} words")
    summary.add_row("SOUL.md", _soul_summary(memory.soul_size_bytes, memory.soul_excerpt))
    sections.append(summary)

    if memory.memory_files:
        files_header = Text()
        files_header.append("\nFiles\n", style=f"bold {theme.ui_label}")
        sections.append(files_header)

        files_table = Table(box=None, show_header=True, padding=(0, 1))
        files_table.add_column("Name", style=theme.ui_accent)
        files_table.add_column("Role", style=theme.banner_text)
        for name in memory.memory_files:
            files_table.add_row(name, _file_role(name))
        sections.append(files_table)

    if memory.soul_excerpt:
        soul_header = Text()
        soul_header.append("\nSOUL Excerpt\n", style=f"bold {theme.ui_label}")
        sections.append(soul_header)
        sections.append(Text(f"  {memory.soul_excerpt}\n", style=theme.banner_dim))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[10] Memory[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _file_role(name: str) -> str:
    lowered = name.lower()
    if lowered == "memory.md":
        return "Long-term memory"
    if lowered == "user.md":
        return "User profile"
    return "Memory artifact"


def _soul_summary(size_bytes: int, excerpt: str) -> str:
    if size_bytes <= 0:
        return "missing"
    if excerpt:
        return f"{size_bytes} bytes"
    return f"{size_bytes} bytes (empty)"
