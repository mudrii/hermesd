from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import ConfigSummary, DashboardState
from hermesd.theme import Theme


def render_config(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    c = state.config
    gateway_count = sum(1 for route in c.tool_gateway_routes if route.mode == "gateway")
    lines = Text()
    lines.append("  Model: ", style=theme.ui_label)
    lines.append(f"{c.model or '—'}\n", style=theme.ui_accent)
    lines.append("  Provider: ", style=theme.ui_label)
    lines.append(f"{c.provider or '—'}\n", style=theme.ui_accent)
    lines.append("  Personality: ", style=theme.ui_label)
    lines.append(f"{c.personality or '—'}\n", style=theme.ui_accent)
    lines.append("  Compress: ", style=theme.ui_label)
    lines.append(f"{c.compression_threshold}\n", style=theme.banner_text)
    lines.append("  Gateway Tools: ", style=theme.ui_label)
    lines.append(f"{gateway_count}/{len(c.tool_gateway_routes)}\n", style=theme.banner_text)
    lines.append("  Tool Search: ", style=theme.ui_label)
    lines.append(c.tool_search_enabled or "—", style=theme.banner_text)

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
    sections: list[RenderableType] = []

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
    table.add_row("Code Execution", _code_execution_label(c))
    table.add_row("Kanban Dispatch", _kanban_config_label(c))
    table.add_row("Gateway Media", _gateway_media_label(c))
    table.add_row("Auxiliary Slots", str(len(c.auxiliary_slots)))
    sections.append(table)

    if c.tool_gateway_routes:
        sections.append(
            Text("\nTool Gateway (dashboard-local env)\n", style=f"bold {theme.ui_label}")
        )

        gateway_table = Table(box=None, show_header=False, padding=(0, 2))
        gateway_table.add_column("Key", style=theme.ui_label)
        gateway_table.add_column("Value", style=theme.ui_accent)
        gateway_table.add_row(
            "Domain", escape(c.tool_gateway_domain) if c.tool_gateway_domain else "—"
        )
        gateway_table.add_row(
            "Scheme", escape(c.tool_gateway_scheme) if c.tool_gateway_scheme else "—"
        )
        gateway_table.add_row(
            "Firecrawl", escape(c.firecrawl_gateway_url) if c.firecrawl_gateway_url else "—"
        )
        sections.append(gateway_table)

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
        sections.append(routes_table)

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[5] Config[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


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
