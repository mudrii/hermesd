from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState, GatewayLoopHealth, GatewayState
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
    lines.append("  loop:", style=theme.ui_label)
    lines.append(gw.loop_health.value, style=_loop_style(gw.loop_health, theme))

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
            if p.needs_attention:
                lines.append("! ", style=f"bold {theme.ui_warn}")
            lines.append(" ")
    if state.channels.platform_count:
        lines.append("\n  Directory: ", style=theme.ui_label)
        lines.append(f"{state.channels.platform_count} platforms", style=theme.banner_text)
    _append_compact_warnings(lines, gw, theme)

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
        _liveness_text(state.gateway, theme),
        _platforms_table(state.gateway, theme),
    ]
    sections.extend(_updates_section(state.gateway, theme))
    sections.extend(_deliveries_section(state.gateway, theme))

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
    table.add_column("Retrying", style=theme.ui_warn)
    table.add_column("Error", style=theme.ui_error)

    for p in gw.platforms:
        dot_color = theme.ui_ok if p.state == "connected" else theme.ui_error
        status = Text()
        status.append("● ", style=f"bold {dot_color}")
        status.append(sanitize_terminal_text(p.state))
        if p.needs_attention:
            status.append("  ! needs attention", style=f"bold {theme.ui_warn}")
        error_parts = [p.error_code, p.error_message]
        error = " / ".join(escape(part) for part in error_parts if part) or "—"
        table.add_row(
            escape(p.name),
            status,
            escape(fmt_iso_timestamp(p.updated_at)),
            _duration_label(p.retrying_since_age_seconds),
            error,
        )
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
    return header


def _loop_style(loop_health: GatewayLoopHealth, theme: Theme) -> str:
    return {
        GatewayLoopHealth.TICKING: theme.ui_ok,
        GatewayLoopHealth.STALE: theme.ui_warn,
        GatewayLoopHealth.WEDGED: theme.ui_error,
        GatewayLoopHealth.UNKNOWN: theme.banner_dim,
    }[loop_health]


def _duration_label(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h"
    return f"{total // 86400}d"


def _or_dash(value: str) -> str:
    return escape(value) if value else "—"


def _append_compact_warnings(lines: Text, gw: GatewayState, theme: Theme) -> None:
    warnings = []
    if gw.config_stale:
        warnings.append("⚠ config changed, restart needed")
    if gw.last_update_outcome and gw.last_update_outcome != "ok":
        warnings.append("⚠ update failed")
    if gw.runtime_code_skew:
        warnings.append("⚠ code skew")
    if warnings:
        lines.append("\n  " + escape("  ".join(warnings)), style=theme.ui_warn)
    if gw.pending_delivery_count or gw.failed_delivery_count:
        lines.append("\n  Deliveries: ", style=theme.ui_label)
        lines.append(f"{gw.pending_delivery_count} pending", style=theme.banner_text)
        lines.append(f"  {gw.failed_delivery_count} failed", style=theme.ui_error)


def _liveness_text(gw: GatewayState, theme: Theme) -> Text:
    text = Text()
    text.append("\nLiveness\n", style=f"bold {theme.ui_label}")
    text.append("  Loop: ", style=theme.ui_label)
    text.append(gw.loop_health.value, style=_loop_style(gw.loop_health, theme))
    text.append(
        f"  heartbeat {_duration_label(gw.heartbeat_age_seconds)} ago"
        f"  incarnations {gw.gateway_incarnation_count}"
        f"  restarts 24h {gw.gateway_restarts_24h}"
        f"  uptime {_duration_label(gw.current_incarnation_uptime_seconds)}",
        style=theme.banner_dim,
    )
    text.append("\n  Code: ", style=theme.ui_label)
    text.append(_or_dash(gw.code_version), style=theme.banner_text)
    text.append(
        f"  sha {_or_dash(gw.code_sha[:12])}"
        f"  config {_or_dash(gw.config_generation_short)}"
        f"  session store {_or_dash(gw.session_store_status)}",
        style=theme.banner_dim,
    )
    if gw.config_stale:
        text.append("\n  ⚠ config changed, restart needed", style=theme.ui_warn)
    _append_lifecycle(text, gw, theme)
    text.append("\n")
    return text


def _append_lifecycle(text: Text, gw: GatewayState, theme: Theme) -> None:
    text.append("\n  Lifecycle: ", style=theme.ui_label)
    text.append(_or_dash(gw.lifecycle_phase), style=theme.banner_text)
    exit_code = "—" if gw.last_exit_code is None else str(gw.last_exit_code)
    text.append(f"  last exit {exit_code}  {_or_dash(gw.last_exit_reason)}", style=theme.banner_dim)
    if gw.exit_reason:
        text.append(f"\n  Exit reason: {escape(gw.exit_reason)}", style=theme.ui_warn)
    if gw.unclean_previous_exit:
        text.append(
            "\n  ⚠ previous gateway life ended without recording an exit",
            style=theme.ui_warn,
        )


def _updates_section(gw: GatewayState, theme: Theme) -> list[RenderableType]:
    if not (gw.last_update_outcome or gw.runtime_code_skew):
        return []
    text = Text()
    text.append("\nUpdates\n", style=f"bold {theme.ui_label}")
    text.append("  Outcome: ", style=theme.ui_label)
    text.append(
        _or_dash(gw.last_update_outcome),
        style=theme.ui_ok if gw.last_update_outcome == "ok" else theme.ui_warn,
    )
    text.append(
        f"  finished {_duration_label(gw.last_update_finished_age_seconds)} ago",
        style=theme.banner_dim,
    )
    text.append("\n  Version: ", style=theme.ui_label)
    text.append(
        f"{_or_dash(gw.last_update_from_version)} → {_or_dash(gw.last_update_to_version)}",
        style=theme.banner_text,
    )
    if gw.last_update_failed_step:
        text.append(f"\n  Failed step: {escape(gw.last_update_failed_step)}", style=theme.ui_error)
    if gw.runtime_code_skew:
        text.append(
            "\n  ⚠ runtime code skew — a runtime is on a different build",
            style=theme.ui_warn,
        )
    return [text]


def _deliveries_section(gw: GatewayState, theme: Theme) -> list[RenderableType]:
    if not gw.pending_deliveries:
        return []
    header = Text()
    header.append("\nDelivery Obligations\n", style=f"bold {theme.ui_label}")
    header.append(
        f"  {gw.pending_delivery_count} pending  {gw.failed_delivery_count} failed\n",
        style=theme.banner_text,
    )
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Platform", style=theme.ui_label)
    table.add_column("State", style=theme.banner_text)
    table.add_column("Tries", justify="right", style=theme.ui_accent)
    table.add_column("Age", style=theme.banner_dim)
    table.add_column("Last Error", style=theme.ui_error)
    for entry in gw.pending_deliveries:
        table.add_row(
            _or_dash(entry.platform),
            _or_dash(entry.state),
            str(entry.attempts),
            _duration_label(entry.age_seconds),
            _or_dash(entry.last_error),
        )
    return [header, table]


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
