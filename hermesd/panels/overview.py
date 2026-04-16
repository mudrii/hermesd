from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_overview(
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    scroll_offset: int = 0,
) -> Panel:
    if detail:
        return _render_detail(state, theme, scroll_offset)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    sm = state.skills_memory
    lines = Text()
    lines.append("  Skills: ", style=theme.ui_label)
    lines.append(f"{sm.skill_count}", style=theme.ui_accent)
    lines.append(f" ({sm.skill_categories} cat)\n", style=theme.banner_dim)
    lines.append("  Memory: ", style=theme.ui_label)
    lines.append(f"{sm.memory_file_count} files\n", style=theme.banner_text)
    for p in sm.providers[:4]:
        sym = "✓" if p.is_active else "✗"
        color = theme.ui_ok
        lines.append(f"  {sym} ", style=color)
        lines.append(f"{p.name} ", style=theme.banner_text)

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[7] Skills / Providers[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme, scroll_offset: int) -> Panel:
    sm = state.skills_memory
    sections: list[RenderableType] = []

    # Table 1: Providers
    prov_header = Text()
    prov_header.append("Providers\n", style=f"bold {theme.ui_label}")
    sections.append(prov_header)

    prov_table = Table(box=None, show_header=False, padding=(0, 2))
    prov_table.add_column("Status", width=3)
    prov_table.add_column("Name", style=theme.banner_text)
    for p in sm.providers:
        sym = (
            Text("●", style=f"bold {theme.ui_ok}")
            if p.is_active
            else Text("○", style=theme.banner_dim)
        )
        prov_table.add_row(sym, p.name)
    sections.append(prov_table)

    sections.append(Text(f"\n  Memory files: {sm.memory_file_count}\n", style=theme.banner_text))

    # Table 2: Skills with scroll + description
    if sm.skills:
        # Build flat skill list grouped by category
        rows: list[tuple[str, str, str]] = []
        by_cat: dict[str, list[tuple[str, str]]] = {}
        for s in sm.skills:
            cat = s.category or "other"
            by_cat.setdefault(cat, []).append((s.name, s.description))
        for cat in sorted(by_cat):
            for i, (name, desc) in enumerate(sorted(by_cat[cat])):
                short = name.removeprefix(f"{cat}-") if name.startswith(f"{cat}-") else name
                cat_label = cat if i == 0 else ""
                rows.append((cat_label, short, desc))

        total = len(rows)
        # Apply scroll — clamp offset
        offset = min(scroll_offset, max(0, total - 1))
        visible = rows[offset:]

        skills_header = Text()
        scroll_hint = (
            f" [{offset + 1}-{min(offset + len(visible), total)}/{total}]" if total > 20 else ""
        )
        skills_header.append(
            f"\nSkills ({sm.skill_count} in {sm.skill_categories} categories){scroll_hint}  ",
            style=f"bold {theme.ui_label}",
        )
        if offset > 0:
            skills_header.append("↑ ", style=theme.ui_accent)
        if offset + len(visible) < total:
            skills_header.append("j/k scroll", style=theme.banner_dim)
        skills_header.append("\n")
        sections.append(skills_header)

        skills_table = Table(box=None, show_header=True, padding=(0, 1))
        skills_table.add_column("Category", style=theme.ui_accent, min_width=12)
        skills_table.add_column("Skill", style=theme.banner_text, min_width=16)
        skills_table.add_column("Description", style=theme.banner_dim, ratio=1)

        # Highlight the first visible row as "selected"
        for idx, (cat_label, short, desc) in enumerate(visible):
            if idx == 0 and offset > 0:
                # First visible row is the "selected" one
                style = f"bold {theme.ui_accent}"
                skills_table.add_row(
                    Text(cat_label, style=style),
                    Text(short, style=style),
                    Text(desc[:80] if desc else "—", style=theme.banner_text),
                )
            else:
                skills_table.add_row(cat_label, short, desc[:80] if desc else "")

        sections.append(skills_table)

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[7] Skills / Providers[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )
