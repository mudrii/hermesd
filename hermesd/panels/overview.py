from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_overview(
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    scroll_offset: int = 0,
) -> Panel:
    if detail:
        return _render_detail(state, theme, scroll_offset)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    sm = state.skills_memory
    lines = Text()
    lines.append("  Skills: ", style=theme.ui_label)
    lines.append(f"{sm.skill_count}", style=theme.ui_accent)
    lines.append(f" ({sm.skill_categories} cat)\n", style=theme.banner_dim)
    lines.append("  Creds: ", style=theme.ui_label)
    lines.append(f"{len(sm.credential_pools)} pools\n", style=theme.banner_text)
    lines.append("  Integrations: ", style=theme.ui_label)
    lines.append(f"{len(sm.plugins)} plug  {len(sm.mcp_servers)} mcp\n", style=theme.banner_text)
    for p in sm.providers[:4]:
        sym = "✓" if p.is_active else "✗"
        color = theme.ui_ok
        lines.append(f"  {sym} ", style=color)
        lines.append(f"{p.name} ", style=theme.banner_text)

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[7] Skills / Integrations[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme, scroll_offset: int) -> Panel:
    sm = state.skills_memory
    sections: list[RenderableType] = []

    # Table 1: Providers
    prov_header = Text()
    prov_header.append("Providers\n", style=f"bold {theme.ui_label}")
    sections.append(prov_header)

    prov_table = Table(box=None, show_header=False, padding=(0, 2))
    prov_table.add_column("Status", width=3)
    prov_table.add_column("Name", style=theme.banner_text)
    for p in sm.providers:
        sym = (
            Text("●", style=f"bold {theme.ui_ok}")
            if p.is_active
            else Text("○", style=theme.banner_dim)
        )
        prov_table.add_row(sym, p.name)
    sections.append(prov_table)

    if sm.credential_pools:
        pool_header = Text()
        pool_header.append("\nCredential Pools\n", style=f"bold {theme.ui_label}")
        sections.append(pool_header)

        pool_table = Table(box=None, show_header=True, padding=(0, 1))
        pool_table.add_column("Provider", style=theme.ui_accent, min_width=16)
        pool_table.add_column("Label", style=theme.banner_text, min_width=18)
        pool_table.add_column("Auth", style=theme.banner_text, min_width=8)
        pool_table.add_column("Source", style=theme.banner_dim, min_width=12)
        pool_table.add_column("Token", style=theme.banner_text, width=5)
        pool_table.add_column("Status", style=theme.banner_text, min_width=12)
        pool_table.add_column("Req", justify="right", min_width=3)
        pool_table.add_column("Cooldown", style=theme.banner_dim, min_width=8)
        pool_table.add_column("Prio", justify="right", min_width=4)
        for entry in sm.credential_pools:
            pool_table.add_row(
                entry.name,
                entry.label,
                entry.auth_type,
                entry.source,
                "Yes" if entry.token_present else "No",
                entry.last_status,
                str(entry.request_count),
                entry.cooldown_remaining,
                str(entry.priority) if entry.priority else "—",
            )
        sections.append(pool_table)

    if sm.hooks:
        hooks_header = Text()
        hooks_header.append("\nHooks\n", style=f"bold {theme.ui_label}")
        sections.append(hooks_header)

        hooks_table = Table(box=None, show_header=True, padding=(0, 1))
        hooks_table.add_column("Name", style=theme.ui_accent, min_width=16)
        hooks_table.add_column("Events", style=theme.banner_text, ratio=1)
        hooks_table.add_column("Description", style=theme.banner_dim, ratio=1)
        for hook in sm.hooks:
            hooks_table.add_row(
                hook.name,
                ", ".join(hook.events),
                hook.description,
            )
        sections.append(hooks_table)

    if sm.plugins:
        plugins_header = Text()
        plugins_header.append("\nPlugins\n", style=f"bold {theme.ui_label}")
        sections.append(plugins_header)

        plugins_table = Table(box=None, show_header=True, padding=(0, 1))
        plugins_table.add_column("Name", style=theme.ui_accent, min_width=16)
        plugins_table.add_column("Version", style=theme.banner_text, min_width=8)
        plugins_table.add_column("Enabled", style=theme.banner_text, min_width=7)
        plugins_table.add_column("Dashboard", style=theme.banner_text, min_width=9)
        plugins_table.add_column("Hooks", justify="right", min_width=5)
        plugins_table.add_column("Tools", justify="right", min_width=5)
        plugins_table.add_column("Description", style=theme.banner_dim, ratio=1)
        for plugin in sm.plugins:
            plugins_table.add_row(
                plugin.name,
                plugin.version,
                "Yes" if plugin.enabled else "No",
                "Yes" if plugin.dashboard_enabled else "No",
                str(plugin.hook_count),
                str(plugin.tool_count),
                plugin.description,
            )
        sections.append(plugins_table)

    if sm.mcp_servers:
        mcp_header = Text()
        mcp_header.append("\nMCP Servers\n", style=f"bold {theme.ui_label}")
        sections.append(mcp_header)

        mcp_table = Table(box=None, show_header=True, padding=(0, 1))
        mcp_table.add_column("Name", style=theme.ui_accent, min_width=16)
        mcp_table.add_column("Enabled", style=theme.banner_text, min_width=7)
        mcp_table.add_column("Transport", style=theme.banner_text, min_width=9)
        mcp_table.add_column("Target", style=theme.banner_text, ratio=1)
        mcp_table.add_column("Tools", style=theme.banner_dim, ratio=1)
        for server in sm.mcp_servers:
            mcp_table.add_row(
                server.name,
                "Yes" if server.enabled else "No",
                server.transport or "—",
                server.target or "—",
                server.tool_filter or "—",
            )
        sections.append(mcp_table)

    if sm.boot_md_present:
        boot_text = Text()
        boot_text.append("\nBOOT.md\n", style=f"bold {theme.ui_label}")
        boot_text.append("  Present", style=theme.banner_text)
        sections.append(boot_text)

    # Table 2: Skills with scroll + description
    if sm.skills:
        # Build flat skill list grouped by category
        rows: list[tuple[str, str, str]] = []
        by_cat: dict[str, list[tuple[str, str]]] = {}
        for s in sm.skills:
            cat = s.category or "other"
            by_cat.setdefault(cat, []).append((s.name, s.description))
        for cat in sorted(by_cat):
            for i, (name, desc) in enumerate(sorted(by_cat[cat])):
                short = name.removeprefix(f"{cat}-") if name.startswith(f"{cat}-") else name
                cat_label = cat if i == 0 else ""
                rows.append((cat_label, short, desc))

        total = len(rows)
        # Apply scroll — clamp offset
        offset = min(scroll_offset, max(0, total - 1))
        visible = rows[offset:]

        skills_header = Text()
        scroll_hint = (
            f" [{offset + 1}-{min(offset + len(visible), total)}/{total}]" if total > 20 else ""
        )
        skills_header.append(
            f"\nSkills ({sm.skill_count} in {sm.skill_categories} categories){scroll_hint}  ",
            style=f"bold {theme.ui_label}",
        )
        if offset > 0:
            skills_header.append("↑ ", style=theme.ui_accent)
        if offset + len(visible) < total:
            skills_header.append("j/k scroll", style=theme.banner_dim)
        skills_header.append("\n")
        sections.append(skills_header)

        skills_table = Table(box=None, show_header=True, padding=(0, 1))
        skills_table.add_column("Category", style=theme.ui_accent, min_width=12)
        skills_table.add_column("Skill", style=theme.banner_text, min_width=16)
        skills_table.add_column("Description", style=theme.banner_dim, ratio=1)

        # Highlight the first visible row as "selected"
        for idx, (cat_label, short, desc) in enumerate(visible):
            if idx == 0 and offset > 0:
                # First visible row is the "selected" one
                style = f"bold {theme.ui_accent}"
                skills_table.add_row(
                    Text(cat_label, style=style),
                    Text(short, style=style),
                    Text(desc or "—", style=theme.banner_text),
                )
            else:
                skills_table.add_row(cat_label, short, desc or "—")

        sections.append(skills_table)

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[7] Skills / Integrations[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )
