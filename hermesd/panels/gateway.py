from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState, GatewayState
from hermesd.panels.formatting import (
    escape_terminal_text as escape,
)
from hermesd.panels.formatting import (
    fmt_iso_timestamp,
    sanitize_terminal_text,
    section_heading,
)
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
        lines.append(f"  v{sanitize_terminal_text(gw.hermes_version)}", style=theme.banner_dim)
        behind = gw.updates_behind or state.version_behind
        if behind > 0:
            lines.append(f" ({behind} behind)", style=theme.ui_warn)

    if gw.platforms:
        lines.append("    ")
        for p in gw.platforms:
            dot_color = theme.ui_ok if p.state == "connected" else theme.ui_error
            lines.append(f"{sanitize_terminal_text(p.name)}:", style=theme.ui_label)
            lines.append(" ● ", style=f"bold {dot_color}")
            if p.error_message or p.error_code:
                lines.append("⚠ ", style=theme.ui_warn)
            lines.append(" ")
    if state.channels.platform_count:
        lines.append("\n  Directory: ", style=theme.ui_label)
        lines.append(f"{state.channels.platform_count} platforms", style=theme.banner_text)

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[1] Gateway & Platforms[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    sections: list[RenderableType] = [
        _status_header(state, theme),
        _platforms_table(state.gateway, theme),
    ]

    if state.channels.platforms:
        sections.append(section_heading("Channel Directory", theme))
        sections.append(_channel_table(state, theme))

    if state.channels.alias_count:
        sections.append(_alias_text(state, theme))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[1] Gateway & Platforms[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _platforms_table(gw: GatewayState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Platform", style=theme.ui_label)
    table.add_column("Status", style=theme.banner_text)
    table.add_column("Updated", style=theme.banner_dim)
    table.add_column("Error", style=theme.ui_error)

    for p in gw.platforms:
        dot_color = theme.ui_ok if p.state == "connected" else theme.ui_error
        status = Text()
        status.append("● ", style=f"bold {dot_color}")
        status.append(sanitize_terminal_text(p.state))
        error_parts = [p.error_code, p.error_message]
        error = " / ".join(escape(part) for part in error_parts if part) or "—"
        table.add_row(escape(p.name), status, escape(fmt_iso_timestamp(p.updated_at)), error)
    return table


def _status_header(state: DashboardState, theme: Theme) -> Text:
    gw = state.gateway
    header = Text()
    if gw.running:
        header.append("● ", style=f"bold {theme.ui_ok}")
        header.append(f"Running  PID:{gw.pid}", style=theme.banner_text)
    else:
        header.append("● ", style=f"bold {theme.ui_error}")
        header.append("Stopped", style=theme.banner_text)
    if gw.hermes_version:
        header.append(
            f"\n  Hermes v{sanitize_terminal_text(gw.hermes_version)}",
            style=theme.ui_accent,
        )
        behind = gw.updates_behind or state.version_behind
        if behind > 0:
            header.append(f"  ({behind} commits behind — run 'hermes update')", style=theme.ui_warn)
        else:
            header.append("  (up to date)", style=theme.ui_ok)
    if gw.active_agents:
        header.append(f"\n  {gw.active_agents} active agents", style=theme.ui_accent)
    if gw.busy:
        header.append("  busy", style=theme.ui_warn)
    if gw.drainable:
        header.append("  drainable", style=theme.banner_dim)
    if gw.restart_requested:
        header.append("  ⚠ restart requested", style=theme.ui_warn)
    if gw.drain_active:
        _append_drain(header, gw, theme)
    if gw.served_profiles:
        header.append("\n  Served Profiles: ", style=theme.ui_label)
        header.append(escape(", ".join(gw.served_profiles)), style=theme.banner_text)
    if gw.scale_to_zero_idle_timeout_minutes:
        relay = " relay-only" if gw.scale_to_zero_relay_only else ""
        header.append(
            f"\n  Scale-to-zero: {gw.scale_to_zero_idle_timeout_minutes}m idle{relay}",
            style=theme.banner_dim,
        )
    header.append("\n\n")
    return header


def _append_drain(header: Text, gw: GatewayState, theme: Theme) -> None:
    principal = f" by {escape(gw.drain_principal)}" if gw.drain_principal else ""
    requested_at = (
        f" at {escape(fmt_iso_timestamp(gw.drain_requested_at))}" if gw.drain_requested_at else ""
    )
    suppress = " suppress-notify" if gw.drain_suppress_notification else ""
    header.append(
        f"\n  external drain{principal}{requested_at}{suppress}",
        style=theme.ui_warn,
    )


def _channel_table(state: DashboardState, theme: Theme) -> Table:
    channel_table = Table(box=None, show_header=True, padding=(0, 2))
    channel_table.add_column("Platform", style=theme.ui_label)
    channel_table.add_column("Entries", justify="right", style=theme.ui_accent)
    channel_table.add_column("Family", style=theme.banner_text)
    channel_table.add_column("States", style=theme.banner_text)
    channel_table.add_column("Capabilities", style=theme.banner_dim)
    for platform in state.channels.platforms:
        states = escape(", ".join(platform.states)) if platform.states else "—"
        capabilities = escape(", ".join(platform.capabilities)) if platform.capabilities else "—"
        name_label = (
            f"{platform.name} (missing directory)"
            if platform.missing_from_directory
            else platform.name
        )
        name = Text(
            sanitize_terminal_text(name_label),
            style=theme.ui_ok if platform.connected else theme.ui_label,
        )
        channel_table.add_row(
            name,
            str(platform.entry_count),
            escape(platform.family_label) if platform.family_label else "—",
            states,
            capabilities,
        )
    return channel_table


def _alias_text(state: DashboardState, theme: Theme) -> Text:
    alias_text = Text()
    alias_text.append("\nChannel Aliases\n", style=f"bold {theme.ui_label}")
    alias_text.append(
        f"  {state.channels.alias_count} aliases across "
        f"{state.channels.alias_platform_count} platforms",
        style=theme.banner_text,
    )
    if state.channels.stale_alias_count:
        alias_text.append(
            f"  {state.channels.stale_alias_count} stale",
            style=theme.ui_warn,
        )
    alias_text.append("\n")
    return alias_text
