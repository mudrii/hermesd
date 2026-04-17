from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
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
    lines.append(f"{gateway_count}/{len(c.tool_gateway_routes)}", style=theme.banner_text)

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

    table.add_row("Model", c.model or "—")
    table.add_row("Provider", c.provider or "—")
    table.add_row("Personality", c.personality or "—")
    table.add_row("Max Turns", str(c.max_turns))
    table.add_row("Reasoning", c.reasoning_effort or "—")
    table.add_row("Compression", str(c.compression_threshold))
    table.add_row("Redact Secrets", "✓" if c.security_redact else "✗")
    table.add_row("Approvals", c.approvals_mode or "—")
    table.add_row("Provider Routing", c.provider_routing_summary or "—")
    table.add_row("Smart Routing", "✓" if c.smart_model_routing_enabled else "✗")
    table.add_row("Cheap Model", c.smart_model_routing_cheap_model or "—")
    table.add_row("Fallback Model", c.fallback_model_label or "—")
    table.add_row("Dashboard Theme", c.dashboard_theme or "—")
    table.add_row("Session Reset", c.session_reset_mode or "—")
    table.add_row("Memory Provider", c.memory_provider or "—")
    sections.append(table)

    if c.tool_gateway_routes:
        sections.append(Text("\nTool Gateway\n", style=f"bold {theme.ui_label}"))

        gateway_table = Table(box=None, show_header=False, padding=(0, 2))
        gateway_table.add_column("Key", style=theme.ui_label)
        gateway_table.add_column("Value", style=theme.ui_accent)
        gateway_table.add_row("Domain", c.tool_gateway_domain or "—")
        gateway_table.add_row("Scheme", c.tool_gateway_scheme or "—")
        gateway_table.add_row("Firecrawl", c.firecrawl_gateway_url or "—")
        sections.append(gateway_table)

        routes_table = Table(box=None, show_header=True, padding=(0, 2))
        routes_table.add_column("Tool", style=theme.ui_label)
        routes_table.add_column("Mode", style=theme.banner_text)
        routes_table.add_column("Token", style=theme.ui_accent)
        for route in c.tool_gateway_routes:
            routes_table.add_row(
                route.tool,
                route.mode,
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
