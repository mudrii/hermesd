from __future__ import annotations

import rich.box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_gateway(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    gw = state.gateway
    lines = Text()

    if gw.running:
        lines.append("  ● ", style=f"bold {theme.ui_ok}")
        lines.append("Running", style=theme.banner_text)
        lines.append("  PID:", style=theme.ui_label)
        lines.append(f"{gw.pid}", style=theme.ui_accent)
    else:
        lines.append("  ● ", style=f"bold {theme.ui_error}")
        lines.append("Stopped", style=theme.banner_text)

    if gw.hermes_version:
        lines.append(f"  v{gw.hermes_version}", style=theme.banner_dim)
        if gw.updates_behind > 0:
            lines.append(f" ({gw.updates_behind} behind)", style=theme.ui_warn)

    if gw.platforms:
        lines.append("    ")
        for p in gw.platforms:
            dot_color = theme.ui_ok if p.state == "connected" else theme.ui_error
            lines.append(f"{p.name}:", style=theme.ui_label)
            lines.append(" ● ", style=f"bold {dot_color}")
            lines.append(" ")

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[1] Gateway & Platforms[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    gw = state.gateway

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Platform", style=theme.ui_label)
    table.add_column("Status", style=theme.banner_text)
    table.add_column("Updated", style=theme.banner_dim)

    for p in gw.platforms:
        dot_color = theme.ui_ok if p.state == "connected" else theme.ui_error
        status = Text()
        status.append("● ", style=f"bold {dot_color}")
        status.append(p.state)
        table.add_row(p.name, status, p.updated_at[:19] if p.updated_at else "—")

    header = Text()
    if gw.running:
        header.append("● ", style=f"bold {theme.ui_ok}")
        header.append(f"Running  PID:{gw.pid}", style=theme.banner_text)
    else:
        header.append("● ", style=f"bold {theme.ui_error}")
        header.append("Stopped", style=theme.banner_text)
    if gw.hermes_version:
        header.append(f"\n  Hermes v{gw.hermes_version}", style=theme.ui_accent)
        if gw.updates_behind > 0:
            header.append(
                f"  ({gw.updates_behind} commits behind — run 'hermes update')", style=theme.ui_warn
            )
        else:
            header.append("  (up to date)", style=theme.ui_ok)
    header.append("\n\n")

    from rich.console import Group

    content = Group(header, table)

    return Panel(
        content,
        title=f"[{theme.panel_title_style}]\\[1] Gateway & Platforms[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )
