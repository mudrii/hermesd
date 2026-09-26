from __future__ import annotations

import time
from datetime import datetime

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.panels.formatting import escape_terminal_text as escape
from hermesd.panels.formatting import fmt_bytes, sanitize_terminal_text
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
    lines.append(sanitize_terminal_text(state.profile_mode_label), style=theme.ui_accent)
    lines.append("\n")
    lines.append("  Profiles: ", style=theme.ui_label)
    lines.append(f"{state.profiles.profile_count} discovered", style=theme.banner_text)
    duplicates = len(state.profiles.duplicate_platform_credentials)
    if duplicates:
        noun = "credential" if duplicates == 1 else "credentials"
        lines.append(f"\n  ⚠ {duplicates} platform {noun} in several profiles", style=theme.ui_warn)
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
    header.append(sanitize_terminal_text(state.profile_mode_label), style=theme.ui_accent)
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
    table.add_column("Files", style=theme.banner_dim)

    for index, profile in enumerate(profiles):
        marker = "▶" if index == viewed_index else ""
        table.add_row(
            Text(marker, style=f"bold {theme.ui_accent}"),
            escape(profile.name),
            str(profile.session_count),
            str(profile.skill_count),
            fmt_bytes(profile.db_size_bytes),
            _format_timestamp(profile.latest_log_mtime),
            _files_label(profile.config_present, profile.env_present),
        )

    excerpt = Text()
    excerpt.append("\nViewed profile: ", style=theme.ui_label)
    excerpt.append(sanitize_terminal_text(viewed_profile.name), style=theme.ui_accent)
    excerpt.append("\n")
    if viewed_profile.soul_excerpt:
        excerpt.append("SOUL: ", style=theme.ui_label)
        excerpt.append(sanitize_terminal_text(viewed_profile.soul_excerpt), style=theme.banner_text)
    else:
        excerpt.append("SOUL: ", style=theme.ui_label)
        excerpt.append("—", style=theme.banner_dim)

    sections: list[RenderableType] = [header, table, excerpt]
    if state.profiles.duplicate_platform_credentials:
        sections.append(_duplicate_credentials_text(state, theme))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[9] Profiles[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _files_label(config_present: bool, env_present: bool) -> str:
    """The per-profile file checks ``hermes doctor`` prints (doctor_state.py:560-565)."""
    if config_present and env_present:
        return "config+.env"
    problems = []
    if not config_present:
        problems.append("⚠ missing config")
    if not env_present:
        problems.append("no .env")
    return ", ".join(problems)


def _duplicate_credentials_text(state: DashboardState, theme: Theme) -> Text:
    """Platform credential key names set in more than one profile's .env."""
    text = Text()
    text.append("\nShared platform credentials\n", style=f"bold {theme.ui_warn}")
    for duplicate in state.profiles.duplicate_platform_credentials:
        text.append(
            sanitize_terminal_text(
                f"  {duplicate.key} ({duplicate.platform}): {', '.join(duplicate.profiles)}"
            )
            + "\n",
            style=theme.ui_warn,
        )
    text.append(
        "  Key names only, values not compared: one bot token can serve only one gateway,"
        " so a token that really is shared parks the second profile's adapter.\n",
        style=theme.banner_dim,
    )
    return text


def _format_timestamp(value: float | None) -> str:
    if value is None or value <= 0:
        return "—"
    try:
        formatted = datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"
    if value > time.time() + 60:
        return f"{formatted} (future)"
    return formatted
