from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import (
    DashboardState,
    GatewayLoopHealth,
    GatewayState,
    MigrationProfileRecord,
    MigrationState,
    MigrationVerificationGap,
    PlatformOwnership,
    PlatformStatus,
)
from hermesd.panels.formatting import (
    escape_terminal_text as escape,
)
from hermesd.panels.formatting import (
    fmt_age_seconds,
    fmt_bytes,
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
        dot, label = _serving_badge(gw, theme)
        lines.append("  ● ", style=f"bold {dot}")
        lines.append(label, style=theme.ui_warn if gw.degraded else theme.banner_text)
        lines.append("  PID:", style=theme.ui_label)
        lines.append(f"{gw.pid}", style=theme.ui_accent)
    else:
        lines.append("  ● ", style=f"bold {theme.ui_error}")
        lines.append("Stopped", style=theme.banner_text)
        if gw.watchdog_exit_reason:
            lines.append("  ⚠ watchdog exit", style=theme.ui_error)
    lines.append("  loop:", style=theme.ui_label)
    lines.append(gw.loop_health.value, style=_loop_style(gw.loop_health, theme))
    if gw.dashboard_client_attached:
        lines.append("  ⌁ web client", style=theme.ui_accent)

    if gw.hermes_version:
        lines.append(f"  v{sanitize_terminal_text(gw.hermes_version)}", style=theme.banner_dim)
        behind = gw.updates_behind or state.version_behind
        if behind > 0:
            lines.append(f" ({behind} behind)", style=theme.ui_warn)

    if gw.platforms:
        lines.append("    ")
        for p in gw.platforms:
            dot_color = theme.ui_ok if p.state == "connected" else theme.ui_error
            lines.append(f"{sanitize_terminal_text(_platform_label(p))}:", style=theme.ui_label)
            lines.append(" ● ", style=f"bold {dot_color}")
            if p.error_message or p.error_code:
                lines.append("⚠ ", style=theme.ui_warn)
            if p.needs_attention:
                lines.append("! ", style=f"bold {theme.ui_warn}")
            lines.append(" ")
    if state.channels.platform_count:
        lines.append("\n  Directory: ", style=theme.ui_label)
        lines.append(f"{state.channels.platform_count} platforms", style=theme.banner_text)
    _append_compact_warnings(lines, state, theme)

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
    sections.extend(_ingress_section(state.gateway, theme))
    sections.extend(_updates_section(state.gateway, theme))
    sections.extend(_migration_section(state.migration, theme))
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


# Operator wording for the watchdog exit reasons, after upstream's own
# ``_WATCHDOG_EXIT_REASONS`` table (hermes_cli/gateway.py:4207-4214).
_WATCHDOG_EXIT_WORDING = {
    "loop_liveness_watchdog": (
        "event loop stopped dispatching; the liveness watchdog exited it for the supervisor"
        " to restart"
    ),
    "shutdown_watchdog": "shutdown drain wedged; the shutdown watchdog forced the exit",
}


def _serving_badge(gw: GatewayState, theme: Theme) -> tuple[str, str]:
    """Dot colour and label for a live gateway: ``degraded`` is serving, but a warning."""
    if gw.degraded:
        return theme.ui_warn, "Degraded"
    return theme.ui_ok, "Running"


def _platform_label(platform: PlatformStatus) -> str:
    """``<profile>/<platform>`` for a served profile's adapter, else the platform name.

    Two served profiles can both run the same platform on the shared listener, so a
    bare platform name would be ambiguous in the compact strip.
    """
    if platform.profile:
        return f"{platform.profile}/{platform.name}"
    return platform.name


def _platforms_table(gw: GatewayState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Platform", style=theme.ui_label)
    table.add_column("Profile", style=theme.banner_text)
    table.add_column("Status", style=theme.banner_text)
    table.add_column("Owner", style=theme.banner_dim)
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
            _or_dash(p.profile),
            status,
            _ownership_label(p.ownership, theme),
            escape(fmt_iso_timestamp(p.updated_at)),
            fmt_age_seconds(p.retrying_since_age_seconds),
            error,
        )
    return table


def _ingress_section(gw: GatewayState, theme: Theme) -> list[RenderableType]:
    """Recorded shared-listener ingress URLs for served profiles.

    These are what the gateway *recorded*, not endpoints hermesd tested: the panel
    says so explicitly, because a recorded URL that nothing answers would otherwise
    read as an availability claim. The collector has already suppressed every URL
    upstream suppresses (dead gateway, falsy value, fatal/disconnected/stopped
    adapter) and redacted credentials, so the panel renders the value as stored.
    """
    mirror_rows = [
        (profile, url, platform.name)
        for platform in gw.platforms
        for profile, url in platform.mirror_urls.items()
    ]
    entries = [platform for platform in gw.platforms if platform.ingress_url]
    if not entries and not mirror_rows:
        return []
    sections: list[RenderableType] = []
    if mirror_rows:
        mirrors = Text()
        mirrors.append(
            "\nInbound callback URLs on the shared listener\n", style=f"bold {theme.ui_label}"
        )
        mirrors.append(
            "  synthesized from the default profile's live listener — hermesd never requests these\n",
            style=theme.banner_dim,
        )
        for profile, url, platform_name in mirror_rows:
            mirrors.append(
                f"  {sanitize_terminal_text(platform_name)} / {sanitize_terminal_text(profile)}: ",
                style=theme.ui_label,
            )
            mirrors.append(f"{sanitize_terminal_text(url)}\n", style=theme.banner_text)
        if any(platform.mirror_urls_truncated for platform in gw.platforms):
            # Say the roster is a slice: a bounded list must not read as the
            # complete set of served profiles.
            mirrors.append(
                "  more served profiles than this bound shows (truncated)\n",
                style=theme.ui_warn,
            )
        sections.append(mirrors)
    if not entries:
        return sections
    header = Text()
    header.append("\nShared-Listener Ingress\n", style=f"bold {theme.ui_label}")
    header.append(
        "  recorded by the gateway — hermesd never requests these URLs\n",
        style=theme.banner_dim,
    )
    sections.append(header)
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Profile", style=theme.ui_label)
    table.add_column("Platform", style=theme.banner_text)
    table.add_column("Recorded URL", style=theme.banner_dim)
    for platform in entries:
        table.add_row(
            _or_dash(platform.profile),
            escape(platform.name),
            escape(platform.ingress_url),
        )
    sections.append(table)
    if any(platform.ingress_url_is_path_only for platform in entries):
        sections.append(
            Text(
                "  a bare path means the default profile had no live listener when it was recorded",
                style=theme.ui_warn,
            )
        )
    return sections


def _ownership_label(ownership: PlatformOwnership, theme: Theme) -> Text:
    """Who wrote this platform record, independent of how fresh it looks.

    A preserved entry outlived the gateway life that recorded it, so its state
    may describe a process that is gone; unverifiable means the record carries no
    writer identity to check (an older gateway, or a host that could not resolve a
    process start time), which is not the same as being current.
    """
    label = Text()
    if ownership is PlatformOwnership.PRESERVED:
        label.append("⚠ preserved", style=f"bold {theme.ui_warn}")
    elif ownership is PlatformOwnership.CURRENT:
        label.append("current", style=theme.banner_dim)
    else:
        label.append("—", style=theme.banner_dim)
    return label


def _status_header(state: DashboardState, theme: Theme) -> Text:
    gw = state.gateway
    header = Text()
    if gw.running:
        dot, label = _serving_badge(gw, theme)
        header.append("● ", style=f"bold {dot}")
        header.append(
            f"{label}  PID:{gw.pid}", style=theme.ui_warn if gw.degraded else theme.banner_text
        )
        if gw.degraded:
            header.append(
                "  serving; a configured platform is parked or retrying", style=theme.ui_warn
            )
    else:
        header.append("● ", style=f"bold {theme.ui_error}")
        header.append("Stopped", style=theme.banner_text)
        if gw.watchdog_exit_reason:
            header.append(
                f"  ⚠ watchdog exit: {sanitize_terminal_text(gw.watchdog_exit_reason)}",
                style=theme.ui_error,
            )
            header.append(
                f"\n    {_WATCHDOG_EXIT_WORDING.get(gw.watchdog_exit_reason, '')}",
                style=theme.banner_dim,
            )
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
    _append_served_profiles(header, gw, theme)
    if gw.scale_to_zero_idle_timeout_minutes:
        relay = " relay-only" if gw.scale_to_zero_relay_only else ""
        header.append(
            f"\n  Scale-to-zero: {gw.scale_to_zero_idle_timeout_minutes}m idle{relay}",
            style=theme.banner_dim,
        )
    return header


def _append_served_profiles(header: Text, gw: GatewayState, theme: Theme) -> None:
    """The served-profile record, labelled by whether a live gateway made it.

    A live empty list is authoritative — the gateway serves nobody else — while a
    list left behind by a gateway that is not live is preserved data rather than
    the current topology, and an absent key is no record at all and renders
    nothing. Collapsing the three would report "serves nobody else" for a gateway
    that never recorded anything.
    """
    if gw.served_profiles_recorded:
        names = sanitize_terminal_text(", ".join(gw.served_profiles))
        header.append("\n  Served Profiles: ", style=theme.ui_label)
        header.append(
            names or "none (the live gateway serves no other profile)",
            style=theme.banner_text if names else theme.banner_dim,
        )
        return
    if gw.served_profiles:
        header.append("\n  Served Profiles (record, writer not current): ", style=theme.ui_warn)
        header.append(sanitize_terminal_text(", ".join(gw.served_profiles)), style=theme.banner_dim)


def _loop_style(loop_health: GatewayLoopHealth, theme: Theme) -> str:
    return {
        GatewayLoopHealth.ALIVE: theme.ui_ok,
        GatewayLoopHealth.TICKING: theme.ui_ok,
        GatewayLoopHealth.STALE: theme.ui_warn,
        GatewayLoopHealth.WEDGED: theme.ui_error,
        # A legacy heartbeat that aged out is the same absence of on-loop evidence
        # as a wedge, just from a gateway too old to carry the witness key.
        GatewayLoopHealth.LEGACY: theme.ui_error,
        GatewayLoopHealth.UNKNOWN: theme.banner_dim,
    }[loop_health]


def _witness_label(gw: GatewayState, theme: Theme) -> Text:
    """Why the loop verdict is what it is, in one bounded line.

    The witness (``state/gateway.loop-tick.<pid>.sock`` or a 127.0.0.1 TCP port) is
    served by the gateway loop itself, so its answer is the only direct evidence
    that the loop dispatches; the heartbeat file is written off-loop and can go
    stale or fresh independently of the loop. A LEGACY verdict means the heartbeat
    predates the witness key entirely: the writer was on-loop, so staleness alone
    is proof the loop stopped.
    """
    label = Text("\n  Witness: ", style=theme.ui_label)
    if gw.loop_health is GatewayLoopHealth.ALIVE:
        label.append("loop-tick witness answered", style=theme.ui_ok)
    elif gw.loop_health is GatewayLoopHealth.WEDGED:
        label.append("witness silent across probes", style=theme.ui_error)
    elif gw.loop_health is GatewayLoopHealth.LEGACY:
        label.append(
            "legacy heartbeat — no witness key, staleness alone is evidence",
            style=theme.ui_error,
        )
    elif gw.loop_tick_armed is True:
        label.append("witness silent or no node (ambiguity)", style=theme.banner_dim)
    elif gw.loop_tick_armed is False:
        label.append("witness not armed (off-loop heartbeat only)", style=theme.banner_dim)
    else:
        label.append("—", style=theme.banner_dim)
    return label


def _restart_storm_text(gw: GatewayState, theme: Theme) -> Text:
    """Start-ledger facts; an absent ledger is never rendered as zero restarts.

    ``gateway-starts.log`` is written by the gateway itself as a ring of epochs.
    ``HERMES_GATEWAY_MAX_STARTS<=0`` disables the writer upstream, so a missing
    file proves nothing either way and the line is omitted.
    """
    text = Text()
    text.append("\n  Starts: ", style=theme.ui_label)
    window = fmt_age_seconds(gw.restart_storm_window_seconds)
    text.append(
        f"{window} {gw.gateway_starts_window}/{gw.restart_storm_cap}  1h {gw.gateway_starts_1h}",
        style=theme.banner_text,
    )
    last = gw.seconds_since_last_gateway_start
    if last is not None:
        text.append(f"  last start {fmt_age_seconds(last)} ago", style=theme.banner_dim)
    if gw.in_respawn_backoff:
        text.append(
            "  ⚠ respawn backoff (the supervisor pauses between restarts)",
            style=theme.ui_warn,
        )
    return text


def _dashboard_client_text(gw: GatewayState, theme: Theme) -> Text:
    """Web dashboard attachment from the marker file's mtime; absent means never."""
    text = Text("\n  Web client: ", style=theme.ui_label)
    age = gw.dashboard_client_last_frame_age_seconds
    if gw.dashboard_client_attached:
        text.append("web dashboard client attached", style=theme.ui_ok)
        if age is not None:
            text.append(f"  last frame {fmt_age_seconds(age)} ago", style=theme.banner_dim)
    elif age is not None:
        text.append(
            f"no client attached (last frame {fmt_age_seconds(age)} ago)",
            style=theme.banner_dim,
        )
    else:
        text.append("no dashboard client marker (never attached)", style=theme.banner_dim)
    return text


def _or_dash(value: str) -> str:
    """Markup-escaped value or a dash, for markup-parsed table cells."""
    return escape(value) if value else "—"


def _text_or_dash(value: str) -> str:
    """Sanitized value or a dash, for ``Text.append`` (no markup parsing)."""
    return sanitize_terminal_text(value) if value else "—"


def _append_compact_warnings(lines: Text, state: DashboardState, theme: Theme) -> None:
    gw = state.gateway
    warnings = []
    if gw.config_stale:
        warnings.append("⚠ config changed, restart needed")
    if gw.update_receipt_unfinished:
        warnings.append("⚠ update unfinished")
    if gw.runtime_code_skew:
        warnings.append("⚠ code skew")
    # A manifest proves an attempt began, never that it finished: warn whenever one
    # exists and the live artifacts do not verify the topology it aimed at.
    if state.migration.manifest_present and not state.migration.migration_verified:
        warnings.append("⚠ migration unverified")
    preserved = sum(1 for p in gw.platforms if p.ownership is PlatformOwnership.PRESERVED)
    if preserved:
        warnings.append(f"⚠ {preserved} platform record(s) outlived their writer")
    if gw.prior_unclean_exit:
        warnings.append("⚠ previous exit unclean")
    if gw.exit_diag_oversized:
        warnings.append("⚠ exit-diag log oversized")
    if gw.prior_suspected_oom:
        warnings.append("⚠ suspected OOM")
    if gw.in_respawn_backoff:
        warnings.append("⚠ respawn backoff")
    if warnings:
        lines.append("\n  " + "  ".join(warnings), style=theme.ui_warn)
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
        f"  heartbeat {fmt_age_seconds(gw.heartbeat_age_seconds)} ago"
        f"  incarnations {gw.gateway_incarnation_count}"
        f"  restarts 24h {gw.gateway_restarts_24h}"
        f"  uptime {fmt_age_seconds(gw.current_incarnation_uptime_seconds)}",
        style=theme.banner_dim,
    )
    text.append_text(_witness_label(gw, theme))
    if gw.gateway_starts_recorded:
        text.append_text(_restart_storm_text(gw, theme))
    text.append_text(_dashboard_client_text(gw, theme))
    text.append("\n  Code: ", style=theme.ui_label)
    text.append(_text_or_dash(gw.code_version), style=theme.banner_text)
    text.append(
        f"  sha {_text_or_dash(gw.code_sha[:12])}"
        f"  config {_text_or_dash(gw.config_generation_short)}"
        f"  session store {_text_or_dash(gw.session_store_status)}",
        style=theme.banner_dim,
    )
    if gw.config_stale:
        text.append("\n  ⚠ config changed, restart needed", style=theme.ui_warn)
    _append_lifecycle(text, gw, theme)
    text.append_text(_exit_diag_text(gw, theme))
    text.append_text(_forensic_files_text(gw, theme))
    text.append("\n")
    return text


def _append_lifecycle(text: Text, gw: GatewayState, theme: Theme) -> None:
    text.append("\n  Lifecycle: ", style=theme.ui_label)
    text.append(_text_or_dash(gw.lifecycle_phase), style=theme.banner_text)
    exit_code = "—" if gw.last_exit_code is None else str(gw.last_exit_code)
    text.append(
        f"  last exit {exit_code}  {_text_or_dash(gw.last_exit_reason)}", style=theme.banner_dim
    )
    if gw.exit_reason:
        text.append(
            f"\n  Exit reason: {sanitize_terminal_text(gw.exit_reason)}", style=theme.ui_warn
        )
    if gw.unclean_previous_exit:
        text.append(
            "\n  ⚠ previous gateway life ended without recording an exit",
            style=theme.ui_warn,
        )
    if gw.prior_unclean_exit or gw.prior_suspected_oom:
        # The flags below are evidence, not a verdict: `gateway --replace`
        # SIGTERMs the old process and SIGKILLs it ten seconds later
        # (gateway/run.py:4924-4926), so a takeover that outran the grace window
        # leaves the same record as a crash — upstream calls it a phantom
        # unclean death (:4699).
        text.append(
            "\n    a deliberate restart (gateway --replace) that SIGKILLed the old "
            "process looks the same",
            style=theme.banner_dim,
        )
    if gw.prior_unclean_exit:
        text.append("\n  ⚠ previous exit unclean", style=theme.ui_warn)
    if gw.prior_suspected_oom:
        text.append("\n  ⚠ suspected OOM", style=theme.ui_warn)


def _exit_diag_text(gw: GatewayState, theme: Theme) -> Text:
    """Crash forensics from the exit-diag ledger: tags and counts only.

    Upstream appends one record per ``asyncio.run()`` return path and one
    ``gateway.previous_unclean_exit`` per unclean boot, and never prunes the
    file — so size is health information, not bookkeeping. ``HERMES_GATEWAY_EXIT_DIAG=0``
    disables the writer, which is why an absent ledger reads as "no evidence"
    rather than "clean".
    """
    text = Text("\n  Exit diagnostics: ", style=theme.ui_label)
    if not gw.exit_diag_recorded:
        text.append("no exit-diag ledger (writer may be disabled)", style=theme.banner_dim)
        return text
    # The newest ledger record is whatever the gateway last wrote — a start
    # record on a healthy boot — so it is labelled as a record, not an exit.
    text.append("last record ", style=theme.ui_label)
    text.append(_text_or_dash(gw.exit_diag_last_tag), style=theme.banner_text)
    if gw.exit_diag_last_age_seconds is not None:
        text.append(
            f"  {fmt_age_seconds(gw.exit_diag_last_age_seconds)} ago",
            style=theme.banner_dim,
        )
    text.append(f"  unclean exits 24h: {gw.exit_diag_unclean_24h}", style=theme.banner_dim)
    text.append(f"  ledger {fmt_bytes(gw.exit_diag_size_bytes)}", style=theme.banner_dim)
    if gw.exit_diag_oversized:
        text.append(
            "  ⚠ past a few MB and nothing prunes it",
            style=theme.ui_warn,
        )
    return text


def _forensic_files_text(gw: GatewayState, theme: Theme) -> Text:
    """Event-only companion logs; growth — never absence — is the signal."""
    if not gw.forensic_files:
        return Text()
    text = Text("\n  Event logs: ", style=theme.ui_label)
    parts = [
        f"{sanitize_terminal_text(file.name)} ({fmt_bytes(file.size_bytes)}, "
        f"{fmt_age_seconds(file.age_seconds)} ago)"
        if file.age_seconds is not None
        else f"{sanitize_terminal_text(file.name)} ({fmt_bytes(file.size_bytes)})"
        for file in gw.forensic_files
    ]
    text.append("; ".join(parts), style=theme.banner_dim)
    text.append(
        "  growth, not absence, is the signal",
        style=theme.banner_dim,
    )
    return text


def _updates_section(gw: GatewayState, theme: Theme) -> list[RenderableType]:
    if not (gw.last_update_outcome or gw.runtime_code_skew or gw.runtime_code_skew_source):
        return []
    text = Text()
    text.append("\nUpdates\n", style=f"bold {theme.ui_label}")
    text.append("  Outcome: ", style=theme.ui_label)
    text.append(
        _text_or_dash(gw.last_update_outcome),
        style=theme.ui_warn if gw.update_receipt_unfinished else theme.ui_ok,
    )
    text.append(
        f"  finished {fmt_age_seconds(gw.last_update_finished_age_seconds)} ago",
        style=theme.banner_dim,
    )
    text.append("\n  Version: ", style=theme.ui_label)
    text.append(
        f"{_text_or_dash(gw.last_update_from_version)} → {_text_or_dash(gw.last_update_to_version)}",
        style=theme.banner_text,
    )
    if gw.last_update_failed_step:
        text.append(
            f"\n  Failed step: {sanitize_terminal_text(gw.last_update_failed_step)}",
            style=theme.ui_error,
        )
    _append_fleet_evidence(text, gw, theme)
    _append_skew_verdict(text, gw, theme)
    return [text]


def _append_fleet_evidence(text: Text, gw: GatewayState, theme: Theme) -> None:
    """The receipt's fleet matrix is a snapshot taken at update time, not a live probe."""
    if not gw.update_fleet_runtime_count:
        return
    states = "  ".join(
        f"{sanitize_terminal_text(state)} {count}"
        for state, count in sorted(gw.update_fleet_states.items())
    )
    text.append(f"\n  Recorded fleet: {gw.update_fleet_runtime_count}", style=theme.banner_dim)
    if states:
        text.append(f"  {states}", style=theme.banner_dim)


def _append_skew_verdict(text: Text, gw: GatewayState, theme: Theme) -> None:
    """Name the evidence behind a skew verdict; silence is not a clean bill of health."""
    if gw.runtime_code_skew:
        if gw.runtime_code_skew_source == "fleet":
            text.append(
                "\n  ⚠ runtime code skew — recorded post-restart fleet is on a different build",
                style=theme.ui_warn,
            )
        else:
            text.append(
                "\n  ⚠ update never finished — pre-update plan is on a different build",
                style=theme.ui_warn,
            )
    elif gw.last_update_outcome and not gw.runtime_code_skew_source:
        text.append("\n  code skew not assessable from this receipt", style=theme.banner_dim)


def _migration_section(mig: MigrationState, theme: Theme) -> list[RenderableType]:
    """The multiplex-migration manifest, kept strictly separate from a success claim.

    Upstream writes ``gateway_migration.json`` *before* it flips
    ``gateway.multiplex_profiles`` and restarts the default gateway, and never
    updates it afterwards, so the file records that an attempt began and nothing
    about how it ended. This section therefore shows three separate things: the
    recorded intent, the intermediate progress hermesd can re-read, and one verdict
    that is only ever "multiplexed (verified)" when the live artifacts cover the
    recorded set. The words "migrated" and "migration complete" are deliberately
    absent from every branch.
    """
    if not (mig.manifest_present or mig.multiplex_flag_on):
        return []
    text = Text()
    text.append("\nMultiplex Migration\n", style=f"bold {theme.ui_label}")
    _append_migration_verdict(text, mig, theme)
    _append_migration_intent(text, mig, theme)
    _append_migration_progress(text, mig, theme)
    if not mig.manifest_parsed:
        return [text]
    _append_migration_roster(text, mig, theme)
    return [text, _migration_table(mig, theme)]


def _append_migration_verdict(text: Text, mig: MigrationState, theme: Theme) -> None:
    """The one line allowed to describe the current topology."""
    if mig.migration_verified:
        text.append("  ● ", style=f"bold {theme.ui_ok}")
        text.append("multiplexed (verified)", style=theme.ui_ok)
        text.append(
            "\n    against gateway.multiplex_profiles as recorded in config and the"
            " live served-profile record\n",
            style=theme.banner_dim,
        )
        return
    if not mig.manifest_present:
        # No manifest: never migrated, or rolled back — the two are indistinguishable
        # because rollback deletes the file. Nothing here was ever an attempt.
        text.append(
            f"  multiplexing is on as recorded in config; {_migration_gap_sentence(mig)}\n",
            style=theme.banner_dim,
        )
        return
    text.append("  ⚠ ", style=f"bold {theme.ui_warn}")
    text.append("migration unverified", style=theme.ui_warn)
    text.append(
        f"\n    not verified: {_migration_gap_sentence(mig)}\n",
        style=theme.banner_dim,
    )


def _migration_gap_sentence(mig: MigrationState) -> str:
    """Name the missing evidence, never an outcome: hermesd cannot see the attempt."""
    gap = mig.verification_gap
    if gap is MigrationVerificationGap.MANIFEST_UNREADABLE:
        return "gateway_migration.json is present but unreadable"
    if gap is MigrationVerificationGap.MANIFEST_INVALID:
        return "the manifest schema is malformed or unsupported"
    if gap is MigrationVerificationGap.FLAG_OFF:
        return "gateway.multiplex_profiles is off as recorded in config"
    if gap is MigrationVerificationGap.GATEWAY_NOT_LIVE:
        return "the default gateway is not live"
    if gap is MigrationVerificationGap.SERVED_NOT_RECORDED:
        return "no live gateway recorded a served-profile set"
    if gap is MigrationVerificationGap.SECONDARIES_TRUNCATED:
        return (
            f"only the first {len(mig.secondaries)} of {mig.secondary_count} "
            "recorded secondaries were retained"
        )
    if gap is MigrationVerificationGap.PROFILES_UNSERVED:
        # Text.append does not parse markup, so sanitize the manifest-sourced
        # profile names instead of escaping them.
        names = ", ".join(sanitize_terminal_text(name) for name in mig.unserved_profiles)
        return f"not in the live served set: {names}"
    return "no migration manifest recorded"


def _append_migration_intent(text: Text, mig: MigrationState, theme: Theme) -> None:
    """What the manifest recorded, with ``migrated_at`` labelled as a start."""
    if not mig.manifest_parsed:
        text.append(
            "  Recorded: gateway_migration.json could not be parsed this pass",
            style=theme.ui_warn,
        )
        return
    if mig.migrated_at:
        text.append("\n  Started: ", style=theme.ui_label)
        # Manifest-sourced and appended to a Text, which does not parse markup.
        text.append(sanitize_terminal_text(mig.migrated_at), style=theme.banner_text)
        # The only local-time stamp hermesd reads; every other source writes UTC.
        suffix = (
            " (local time; the recorded stamp could not be parsed)"
            if mig.migrated_at_age_seconds is None
            else f" (local time, {fmt_age_seconds(mig.migrated_at_age_seconds)} ago)"
        )
        text.append(suffix, style=theme.banner_dim)
    text.append("\n  Config flag: ", style=theme.ui_label)
    text.append(
        f"gateway.multiplex_profiles {'on' if mig.multiplex_flag_on else 'off'}",
        style=theme.banner_text,
    )
    text.append(
        f" (manifest recorded {'on' if mig.flag_was else 'off'})"
        " — as recorded in config, an env override is invisible here",
        style=theme.banner_dim,
    )


def _append_migration_progress(text: Text, mig: MigrationState, theme: Theme) -> None:
    """Intermediate progress: every one of these is also true of a crashed attempt."""
    if not mig.manifest_parsed:
        return
    parts = [
        "flag flipped" if mig.flag_flipped else "flag unchanged since the attempt began",
        "default gateway live" if mig.default_gateway_live else "default gateway not live",
        "served record live" if mig.served_recorded else "no live served record",
    ]
    if mig.served_recorded:
        parts.append(
            "every recorded profile is in the live served set"
            if not mig.unserved_profiles
            else f"{len(mig.unserved_profiles)} recorded profile(s) not in the live served set"
        )
    text.append("\n  Progress: ", style=theme.ui_label)
    text.append(" · ".join(parts), style=theme.banner_dim)


def _append_migration_roster(text: Text, mig: MigrationState, theme: Theme) -> None:
    """How many profiles the manifest recorded, and how many hermesd retained."""
    retained = (
        f"{len(mig.secondaries)} of {mig.secondary_count} secondaries retained"
        if mig.secondaries_truncated
        else f"{mig.secondary_count} secondaries"
    )
    style = theme.ui_warn if mig.secondaries_truncated else theme.banner_dim
    text.append(f"\n  Recorded profiles: {retained} (+ default)\n", style=style)


def _migration_table(mig: MigrationState, theme: Theme) -> Table:
    """The manifest's roster, with each profile's coverage by the live served set."""
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Profile", style=theme.ui_label)
    table.add_column("Recorded home", style=theme.banner_dim)
    table.add_column("Service (recorded)", style=theme.banner_text)
    table.add_column("In live served set", style=theme.banner_text)
    for record in (mig.default_profile, *mig.secondaries):
        table.add_row(
            _or_dash(record.profile),
            _or_dash(record.home),
            escape(record.service_label),
            _coverage_label(record, mig.served_recorded, theme),
        )
    return table


def _coverage_label(record: MigrationProfileRecord, recorded: bool, theme: Theme) -> Text:
    """Coverage is unknown — not negative — when no live gateway recorded a set."""
    if not recorded:
        return Text("—", style=theme.banner_dim)
    if record.served:
        return Text("served", style=theme.ui_ok)
    return Text("not served", style=theme.ui_warn)


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
            fmt_age_seconds(entry.age_seconds),
            _or_dash(entry.last_error),
        )
    return [header, table]


def _append_drain(header: Text, gw: GatewayState, theme: Theme) -> None:
    principal = f" by {sanitize_terminal_text(gw.drain_principal)}" if gw.drain_principal else ""
    requested_at = (
        f" at {sanitize_terminal_text(fmt_iso_timestamp(gw.drain_requested_at))}"
        if gw.drain_requested_at
        else ""
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
