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
            _duration_label(p.retrying_since_age_seconds),
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
    entries = [platform for platform in gw.platforms if platform.ingress_url]
    if not entries:
        return []
    header = Text()
    header.append("\nShared-Listener Ingress\n", style=f"bold {theme.ui_label}")
    header.append(
        "  recorded by the gateway — hermesd never requests these URLs\n",
        style=theme.banner_dim,
    )
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
    sections: list[RenderableType] = [header, table]
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
        names = escape(", ".join(gw.served_profiles))
        header.append("\n  Served Profiles: ", style=theme.ui_label)
        header.append(
            names or "none (the live gateway serves no other profile)",
            style=theme.banner_text if names else theme.banner_dim,
        )
        return
    if gw.served_profiles:
        header.append("\n  Served Profiles (record, gateway not live): ", style=theme.ui_warn)
        header.append(escape(", ".join(gw.served_profiles)), style=theme.banner_dim)


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
    if not (gw.last_update_outcome or gw.runtime_code_skew or gw.runtime_code_skew_source):
        return []
    text = Text()
    text.append("\nUpdates\n", style=f"bold {theme.ui_label}")
    text.append("  Outcome: ", style=theme.ui_label)
    text.append(
        _or_dash(gw.last_update_outcome),
        style=theme.ui_warn if gw.update_receipt_unfinished else theme.ui_ok,
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
    _append_fleet_evidence(text, gw, theme)
    _append_skew_verdict(text, gw, theme)
    return [text]


def _append_fleet_evidence(text: Text, gw: GatewayState, theme: Theme) -> None:
    """The receipt's fleet matrix is a snapshot taken at update time, not a live probe."""
    if not gw.update_fleet_runtime_count:
        return
    states = "  ".join(
        f"{escape(state)} {count}" for state, count in sorted(gw.update_fleet_states.items())
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
        names = ", ".join(escape(name) for name in mig.unserved_profiles)
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
        text.append(escape(mig.migrated_at), style=theme.banner_text)
        # The only local-time stamp hermesd reads; every other source writes UTC.
        suffix = (
            " (local time; the recorded stamp could not be parsed)"
            if mig.migrated_at_age_seconds is None
            else f" (local time, {_duration_label(mig.migrated_at_age_seconds)} ago)"
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
    text.append(escape(" · ".join(parts)), style=theme.banner_dim)


def _append_migration_roster(text: Text, mig: MigrationState, theme: Theme) -> None:
    """How many profiles the manifest recorded, and how many hermesd retained."""
    retained = (
        f"{len(mig.secondaries)} of {mig.secondary_count} secondaries retained"
        if mig.secondaries_truncated
        else f"{mig.secondary_count} secondaries"
    )
    style = theme.ui_warn if mig.secondaries_truncated else theme.banner_dim
    text.append(f"\n  Recorded profiles: {escape(retained)} (+ default)\n", style=style)


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
