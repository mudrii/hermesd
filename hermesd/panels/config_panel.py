from __future__ import annotations

import rich.box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_config(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    c = state.config
    lines = Text()
    lines.append("  Model: ", style=theme.ui_label)
    lines.append(f"{c.model or '—'}\n", style=theme.ui_accent)
    lines.append("  Provider: ", style=theme.ui_label)
    lines.append(f"{c.provider or '—'}\n", style=theme.ui_accent)
    lines.append("  Personality: ", style=theme.ui_label)
    lines.append(f"{c.personality or '—'}\n", style=theme.ui_accent)
    lines.append("  Compress: ", style=theme.ui_label)
    lines.append(f"{c.compression_threshold}", style=theme.banner_text)

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[5] Config[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    c = state.config
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.ui_accent)

    table.add_row("Model", c.model or "—")
    table.add_row("Provider", c.provider or "—")
    table.add_row("Personality", c.personality or "—")
    table.add_row("Max Turns", str(c.max_turns))
    table.add_row("Reasoning", c.reasoning_effort or "—")
    table.add_row("Compression", str(c.compression_threshold))
    table.add_row("Redact Secrets", "✓" if c.security_redact else "✗")
    table.add_row("Approvals", c.approvals_mode or "—")

    return Panel(
        table,
        title=f"[{theme.panel_title_style}]\\[5] Config[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )
