from __future__ import annotations

from datetime import datetime

import rich.box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_profiles(
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    profile_view_index: int = 0,
) -> Panel:
    if detail:
        return _render_detail(state, theme, profile_view_index)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    lines = Text()
    lines.append("  Source: ", style=theme.ui_label)
    lines.append(state.profile_mode_label, style=theme.ui_accent)
    lines.append("\n")
    lines.append("  Profiles: ", style=theme.ui_label)
    lines.append(f"{state.profiles.profile_count} discovered", style=theme.banner_text)
    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[9] Profiles[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme, profile_view_index: int) -> Panel:
    profiles = state.profiles.profiles
    if not profiles:
        empty = Text("  No profiles found", style=theme.banner_dim)
        return Panel(
            empty,
            title=f"[{theme.panel_title_style}]\\[9] Profiles[/]",
            title_align="left",
            border_style=theme.panel_border_style,
            box=rich.box.HORIZONTALS,
            padding=(1, 2),
        )

    viewed_index = profile_view_index % len(profiles)
    viewed_profile = profiles[viewed_index]

    header = Text()
    header.append("Selected source: ", style=theme.ui_label)
    header.append(state.profile_mode_label, style=theme.ui_accent)
    header.append("  ")
    header.append("p cycle", style=theme.banner_dim)
    header.append("\n")

    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("View", width=4)
    table.add_column("Name", style=theme.ui_accent)
    table.add_column("Sessions", justify="right", style=theme.banner_text)
    table.add_column("Skills", justify="right", style=theme.banner_text)
    table.add_column("DB Size", justify="right", style=theme.banner_text)
    table.add_column("Last Log", style=theme.banner_dim)

    for index, profile in enumerate(profiles):
        marker = "▶" if index == viewed_index else ""
        table.add_row(
            Text(marker, style=f"bold {theme.ui_accent}"),
            profile.name,
            str(profile.session_count),
            str(profile.skill_count),
            _format_size(profile.db_size_bytes),
            _format_timestamp(profile.latest_log_mtime),
        )

    excerpt = Text()
    excerpt.append("\nViewed profile: ", style=theme.ui_label)
    excerpt.append(viewed_profile.name, style=theme.ui_accent)
    excerpt.append("\n")
    if viewed_profile.soul_excerpt:
        excerpt.append("SOUL: ", style=theme.ui_label)
        excerpt.append(viewed_profile.soul_excerpt, style=theme.banner_text)
    else:
        excerpt.append("SOUL: ", style=theme.ui_label)
        excerpt.append("—", style=theme.banner_dim)

    return Panel(
        Group(header, table, excerpt),
        title=f"[{theme.panel_title_style}]\\[9] Profiles[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _format_timestamp(value: float | None) -> str:
    if value is None:
        return "—"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"
