from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import ConfigBackupGroup, ConfigSummary, DashboardState
from hermesd.panels.formatting import escape_terminal_text as escape
from hermesd.panels.formatting import fmt_age_seconds, sanitize_terminal_text, section_heading
from hermesd.theme import Theme


def render_config(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    c = state.config
    gateway_count = sum(1 for route in c.tool_gateway_routes if route.mode == "gateway")
    corrupt_count = _corrupt_backup_count(c)
    lines = Text()
    lines.append("  Model: ", style=theme.ui_label)
    lines.append(f"{sanitize_terminal_text(c.model) or '—'}\n", style=theme.ui_accent)
    lines.append("  Provider: ", style=theme.ui_label)
    lines.append(f"{sanitize_terminal_text(c.provider) or '—'}\n", style=theme.ui_accent)
    lines.append("  Personality: ", style=theme.ui_label)
    lines.append(f"{sanitize_terminal_text(c.personality) or '—'}\n", style=theme.ui_accent)
    lines.append("  Compress: ", style=theme.ui_label)
    lines.append(f"{c.compression_threshold}\n", style=theme.banner_text)
    lines.append("  Gateway Tools: ", style=theme.ui_label)
    lines.append(f"{gateway_count}/{len(c.tool_gateway_routes)}\n", style=theme.banner_text)
    lines.append("  Tool Search: ", style=theme.ui_label)
    lines.append(f"{sanitize_terminal_text(c.tool_search_enabled) or '—'}\n", style=theme.ui_accent)
    lines.append("  Integrations: ", style=theme.ui_label)
    lines.append(
        f"mcp {c.mcp_server_count} · plugins {c.plugin_enabled_count} "
        f"· goals {c.goals_max_turns or '—'}\n",
        style=theme.banner_text,
    )
    lines.append("  Backups: ", style=theme.ui_label)
    if not c.config_backups_present:
        lines.append("—", style=theme.banner_dim)
    else:
        lines.append(_newest_good_backup_stamp(c) or "no good copy", style=theme.banner_text)
        if corrupt_count:
            lines.append(f" · corrupt {corrupt_count}", style=theme.ui_error)

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
    sections: list[RenderableType] = [_settings_table(c, theme)]
    sections.extend(_session_capacity_section(c, theme))
    sections.extend(_kv_section("Agent limits", _agent_limit_rows(c), theme))
    sections.extend(_kv_section("Integrations", _integration_rows(c), theme))
    sections.extend(_backup_section(c, theme))

    if c.tool_gateway_routes:
        sections.append(section_heading("Tool Gateway (dashboard-local env)", theme))
        sections.append(_tool_gateway_table(c, theme))
        sections.append(_tool_gateway_routes_table(c, theme))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[5] Config[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _settings_table(c: ConfigSummary, theme: Theme) -> Table:
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.ui_accent)

    table.add_row("Model", escape(c.model) if c.model else "—")
    table.add_row("Provider", escape(c.provider) if c.provider else "—")
    table.add_row("Personality", escape(c.personality) if c.personality else "—")
    table.add_row("Max Turns", str(c.max_turns))
    table.add_row("Reasoning", escape(c.reasoning_effort) if c.reasoning_effort else "—")
    table.add_row("Compression", str(c.compression_threshold))
    table.add_row("Redact Secrets", "✓" if c.security_redact else "✗")
    table.add_row("Approvals", escape(c.approvals_mode) if c.approvals_mode else "—")
    table.add_row(
        "Provider Routing",
        escape(c.provider_routing_summary) if c.provider_routing_summary else "—",
    )
    table.add_row("Smart Routing", "✓" if c.smart_model_routing_enabled else "✗")
    table.add_row(
        "Cheap Model",
        escape(c.smart_model_routing_cheap_model) if c.smart_model_routing_cheap_model else "—",
    )
    table.add_row(
        "Fallback Model", escape(c.fallback_model_label) if c.fallback_model_label else "—"
    )
    table.add_row("Dashboard Theme", escape(c.dashboard_theme) if c.dashboard_theme else "—")
    table.add_row("Dashboard Auth", escape(_dashboard_auth_label(c)))
    table.add_row(
        "Dashboard URL", escape(c.dashboard_public_url) if c.dashboard_public_url else "—"
    )
    table.add_row("Session Reset", escape(c.session_reset_mode) if c.session_reset_mode else "—")
    table.add_row("Memory Provider", escape(c.memory_provider) if c.memory_provider else "—")
    table.add_row("Tool Search", escape(_tool_search_label(c)))
    table.add_row("Toolsets", escape(", ".join(c.toolsets)) if c.toolsets else "—")
    table.add_row("Code Execution", escape(_code_execution_label(c)))
    table.add_row("Kanban Dispatch", _kanban_config_label(c))
    table.add_row("Gateway Media", _gateway_media_label(c))
    table.add_row("MoA", escape(_moa_label(c)))
    table.add_row("Auxiliary Slots", str(len(c.auxiliary_slots)))
    return table


def _kv_section(title: str, rows: list[tuple[str, str]], theme: Theme) -> list[RenderableType]:
    """A labelled key/value block; "—" when nothing non-default is configured."""
    heading = section_heading(title, theme)
    if not rows:
        return [heading, Text("  —", style=theme.banner_dim)]
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.ui_accent)
    for key, value in rows:
        table.add_row(key, escape(value))
    return [heading, table]


# Both session caps are always listed, configured or not: an absent row would be
# indistinguishable from a limit hermesd failed to read, and the two keys govern
# different resources, so neither may be shown under the other's name.
_CAPACITY_NOTE_LINES = (
    "max_concurrent_sessions: a cross-process lease cap, checked when a surface attaches.",
    "max_live_sessions: a soft LRU cap on the gateway's in-memory sessions; evicts detached.",
    "Neither is a count of running turns, and neither stands in for the other.",
)


def _session_capacity_section(c: ConfigSummary, theme: Theme) -> list[RenderableType]:
    """The two session caps, each named for the resource it actually governs."""
    note = Text("\n".join(f"  {line}" for line in _CAPACITY_NOTE_LINES), style=theme.banner_dim)
    return [
        *_kv_section("Session Capacity", _session_capacity_rows(c), theme),
        note,
    ]


def _session_capacity_rows(c: ConfigSummary) -> list[tuple[str, str]]:
    return [
        ("Active-Session Lease Cap", _concurrent_cap_label(c)),
        ("In-Memory Live-Session Cap", _live_cap_label(c)),
    ]


def _concurrent_cap_label(c: ConfigSummary) -> str:
    """Upstream enforces capacity only when an operator asked for one."""
    if not c.active_session_cap_configured:
        return "not configured (unbounded)"
    return f"{c.max_concurrent_sessions} (max_concurrent_sessions)"


def _live_cap_label(c: ConfigSummary) -> str:
    """0/unset disables the LRU evictor, so it reads as not configured."""
    if not c.live_session_cap_configured:
        return "not configured (LRU eviction off)"
    return f"{c.max_live_sessions} (max_live_sessions)"


def _agent_limit_rows(c: ConfigSummary) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    delegation = [
        f"children {c.delegation_max_concurrent_children}"
        if c.delegation_max_concurrent_children
        else "",
        f"depth {c.delegation_max_spawn_depth}" if c.delegation_max_spawn_depth else "",
        "orchestrator" if c.delegation_orchestrator_enabled else "",
    ]
    configured_delegation = [part for part in delegation if part]
    if configured_delegation:
        rows.append(("Delegation", " · ".join(configured_delegation)))
    if c.goals_max_turns:
        rows.append(("Goals", f"max turns {c.goals_max_turns}"))
    guardrails = [
        "warn" if c.tool_loop_warnings_enabled else "",
        "hard-stop" if c.tool_loop_hard_stop_enabled else "",
    ]
    configured_guardrails = [part for part in guardrails if part]
    if configured_guardrails:
        rows.append(("Tool Loop Guard", " · ".join(configured_guardrails)))
    # max_live_sessions is a session cap, not an agent limit: it lives in the
    # Session Capacity section beside the lease cap it must not be confused with.
    if c.streaming_enabled:
        rows.append(("Streaming", "on"))
    if c.logging_level:
        rows.append(("Log Level", c.logging_level))
    return rows


def _integration_rows(c: ConfigSummary) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if c.mcp_server_count:
        names = ", ".join(c.mcp_server_names)
        rows.append(
            ("MCP Servers", f"{c.mcp_server_count} ({names})" if names else str(c.mcp_server_count))
        )
    if c.plugin_enabled_count or c.plugin_disabled_count:
        rows.append(
            (
                "Plugins",
                f"{c.plugin_enabled_count} enabled · {c.plugin_disabled_count} disabled",
            )
        )
    updates = [
        "check" if c.updates_check else "",
        f"backup {c.updates_pre_update_backup}" if c.updates_pre_update_backup else "",
        f"keep {c.updates_backup_keep}" if c.updates_backup_keep else "",
    ]
    configured_updates = [part for part in updates if part]
    if configured_updates:
        rows.append(("Updates", " · ".join(configured_updates)))
    if c.network_proxy_configured:
        rows.append(("Network Proxy", "configured"))
    return rows


# Backup audit-trail caveats: a "good" copy is written only when the file's
# bytes change, so its stamp dates the *config*, not the reader; and the stamp
# is the writer's local time (time.strftime), so ages follow the local clock.
_BACKUP_NOTE_LINES = (
    "A good copy lands only when config.yaml's bytes change — an old stamp is an",
    "unchanged config, not a stale one. Stamps are the writer's local time.",
)

_AUDIT_TRAIL_LIMIT = 4


def _corrupt_backup_count(c: ConfigSummary) -> int:
    for group in c.config_backup_groups:
        if group.kind == "corrupt":
            return group.count
    return 0


def _newest_good_backup_stamp(c: ConfigSummary) -> str:
    for group in c.config_backup_groups:
        if group.kind == "good":
            return group.newest_stamp
    return ""


def _backup_section(c: ConfigSummary, theme: Theme) -> list[RenderableType]:
    """The backups/config/ audit trail: last-changed date, corrupt alert, stamps."""
    heading = section_heading("Config Backups", theme)
    if not c.config_backups_present:
        return [heading, Text("  no backups directory observed", style=theme.banner_dim)]

    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.ui_accent)
    newest_good = _newest_good_backup_stamp(c)
    newest_good_age = _newest_good_backup_age(c)
    if newest_good:
        changed = newest_good
        if newest_good_age is not None:
            changed = f"{newest_good} ({fmt_age_seconds(int(newest_good_age))} ago)"
        table.add_row("Last changed", escape(changed))
    else:
        table.add_row("Last changed", "no good copy")
    corrupt_count = _corrupt_backup_count(c)
    table.add_row("Corrupt snapshots", str(corrupt_count) if corrupt_count else "none")
    audit = [group for group in c.config_backup_groups if group.kind not in ("good", "corrupt")]
    if audit:
        table.add_row(
            "Audit trail",
            escape(_audit_trail_label(audit[:_AUDIT_TRAIL_LIMIT], len(audit))),
        )
    if c.config_backup_groups_truncated:
        table.add_row("Groups", "truncated — the scan hit its entry budget")
    note = Text("\n".join(f"  {line}" for line in _BACKUP_NOTE_LINES), style=theme.banner_dim)
    return [heading, table, note]


def _newest_good_backup_age(c: ConfigSummary) -> float | None:
    for group in c.config_backup_groups:
        if group.kind == "good":
            return group.newest_age_seconds
    return None


def _audit_trail_label(groups: list[ConfigBackupGroup], total: int) -> str:
    parts = [
        f"{group.reason} x{group.count}"
        + (f" (newest {group.newest_stamp})" if group.newest_stamp else "")
        for group in groups
    ]
    hidden = total - len(groups)
    if hidden > 0:
        parts.append(f"(+{hidden} more)")
    return " · ".join(parts)


def _tool_gateway_table(c: ConfigSummary, theme: Theme) -> Table:
    gateway_table = Table(box=None, show_header=False, padding=(0, 2))
    gateway_table.add_column("Key", style=theme.ui_label)
    gateway_table.add_column("Value", style=theme.ui_accent)
    gateway_table.add_row("Domain", escape(c.tool_gateway_domain) if c.tool_gateway_domain else "—")
    gateway_table.add_row("Scheme", escape(c.tool_gateway_scheme) if c.tool_gateway_scheme else "—")
    gateway_table.add_row(
        "Firecrawl", escape(c.firecrawl_gateway_url) if c.firecrawl_gateway_url else "—"
    )
    return gateway_table


def _tool_gateway_routes_table(c: ConfigSummary, theme: Theme) -> Table:
    routes_table = Table(box=None, show_header=True, padding=(0, 2))
    routes_table.add_column("Tool", style=theme.ui_label)
    routes_table.add_column("Mode", style=theme.banner_text)
    routes_table.add_column("Token", style=theme.ui_accent)
    for route in c.tool_gateway_routes:
        routes_table.add_row(
            escape(route.tool),
            escape(route.mode),
            "Yes" if route.token_present else "No",
        )
    return routes_table


def _dashboard_auth_label(config: ConfigSummary) -> str:
    provider = config.dashboard_auth_provider or "—"
    if config.dashboard_basic_auth_configured:
        return f"{provider} basic-configured"
    return provider


def _tool_search_label(config: ConfigSummary) -> str:
    if not config.tool_search_enabled:
        return "—"
    return (
        f"{config.tool_search_enabled} "
        f"threshold={config.tool_search_threshold_pct}% "
        f"limit={config.tool_search_default_limit}/{config.tool_search_max_limit}"
    )


def _code_execution_label(config: ConfigSummary) -> str:
    if not config.code_execution_mode:
        return "—"
    parts = [config.code_execution_mode]
    if config.code_execution_timeout:
        parts.append(f"{config.code_execution_timeout}s")
    if config.code_execution_max_tool_calls:
        parts.append(f"{config.code_execution_max_tool_calls} calls")
    return " ".join(parts)


def _kanban_config_label(config: ConfigSummary) -> str:
    dispatch = "gateway" if config.kanban_dispatch_in_gateway else "manual"
    parts = [dispatch]
    if config.kanban_dispatch_interval_seconds:
        parts.append(f"{config.kanban_dispatch_interval_seconds}s")
    if config.kanban_failure_limit:
        parts.append(f"fail={config.kanban_failure_limit}")
    if config.kanban_auto_decompose:
        parts.append("auto-decompose")
    return " ".join(parts)


def _gateway_media_label(config: ConfigSummary) -> str:
    parts = ["strict" if config.gateway_strict_media_delivery else "relaxed"]
    if config.gateway_trust_recent_files:
        parts.append(f"trust-recent={config.gateway_trust_recent_files_seconds}s")
    return " ".join(parts)


def _moa_label(config: ConfigSummary) -> str:
    if not config.moa_default_preset and not config.moa_preset_count:
        return "—"
    preset = config.moa_active_preset or config.moa_default_preset or "—"
    parts = [
        f"{preset}",
        f"{config.moa_preset_count} presets",
        f"{config.moa_reference_model_count} refs",
    ]
    if config.moa_aggregator_label:
        parts.append(config.moa_aggregator_label)
    parts.append("traces on" if config.moa_save_traces else "traces off")
    return " ".join(parts)
