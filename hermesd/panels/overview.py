from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState, SkillsMemory
from hermesd.panels.formatting import escape_terminal_text as escape
from hermesd.panels.formatting import fmt_age_seconds, sanitize_terminal_text, section_heading
from hermesd.theme import Theme

_DETAIL_VISIBLE_SKILL_ROWS = 20


def max_skills_scroll_offset(state: DashboardState) -> int:
    """Largest scroll offset that still shows a full skills window."""
    return max(0, len(state.skills_memory.skills) - _DETAIL_VISIBLE_SKILL_ROWS)


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
    if state.mcp_cache.mcp_cached_server_count:
        lines.append("  Schema cache: ", style=theme.ui_label)
        lines.append(
            f"mcp {state.mcp_cache.mcp_cached_server_count} cached\n", style=theme.banner_text
        )
    for p in sm.providers[:4]:
        sym = "✓" if p.is_active else "✗"
        color = theme.ui_ok if p.is_active else theme.banner_dim
        lines.append(f"  {sym} ", style=color)
        lines.append(f"{sanitize_terminal_text(p.name)} ", style=theme.banner_text)

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
    sections: list[RenderableType] = [
        section_heading("Providers", theme, leading_blank=False),
        _providers_table(sm, theme),
    ]

    if sm.credential_pools:
        sections.append(section_heading("Credential Pools", theme))
        sections.append(_credential_pools_table(sm, theme))

    if sm.hooks:
        sections.append(section_heading("Hooks", theme))
        sections.append(_hooks_table(sm, theme))

    if sm.plugins:
        sections.append(section_heading("Plugins", theme))
        sections.append(_plugins_table(sm, theme))

    if sm.mcp_servers:
        sections.append(section_heading("MCP Servers", theme))
        sections.append(_mcp_servers_table(sm, theme))

    sections.extend(_mcp_cache_section(state, theme))

    if state.skills_prompt.prompted_skill_count:
        sections.append(_prompted_skills_text(state, theme))

    if sm.boot_md_present:
        boot_text = Text()
        boot_text.append("\nBOOT.md\n", style=f"bold {theme.ui_label}")
        boot_text.append("  Present", style=theme.banner_text)
        sections.append(boot_text)

    if sm.skills:
        sections.extend(_skills_sections(sm, theme, scroll_offset))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[7] Skills / Integrations[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _mcp_cache_section(state: DashboardState, theme: Theme) -> list[RenderableType]:
    """MCP schema cache block: cached names, age, and a never-connected hint."""
    cache = state.mcp_cache
    heading = section_heading("MCP", theme)
    if not cache.mcp_cached_server_count:
        return [heading, Text("  —", style=theme.banner_dim)]

    never_cached = [
        name for name in state.config.mcp_server_names if name not in cache.mcp_cached_server_names
    ]
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.ui_accent)
    names = ", ".join(cache.mcp_cached_server_names)
    table.add_row(
        "Cached servers",
        escape(f"{cache.mcp_cached_server_count} ({names})") if names else "—",
    )
    table.add_row("Cache age", _age_label(cache.mcp_schema_cache_age_seconds))
    table.add_row("Never connected", escape(", ".join(never_cached)) if never_cached else "—")
    return [heading, table]


def _prompted_skills_text(state: DashboardState, theme: Theme) -> Text:
    prompt = state.skills_prompt
    text = Text()
    text.append(
        f"\nPrompted skills: {prompt.prompted_skill_count} "
        f"(snapshot {_age_label(prompt.prompt_snapshot_age_seconds)} ago)",
        style=theme.banner_text,
    )
    return text


def _age_label(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "—"
    return fmt_age_seconds(max(0, int(age_seconds)))


def _providers_table(sm: SkillsMemory, theme: Theme) -> Table:
    prov_table = Table(box=None, show_header=False, padding=(0, 2))
    prov_table.add_column("Status", width=3)
    prov_table.add_column("Name", style=theme.banner_text)
    for p in sm.providers:
        sym = (
            Text("●", style=f"bold {theme.ui_ok}")
            if p.is_active
            else Text("○", style=theme.banner_dim)
        )
        prov_table.add_row(sym, escape(p.name))
    return prov_table


def _credential_pools_table(sm: SkillsMemory, theme: Theme) -> Table:
    pool_table = Table(box=None, show_header=True, padding=(0, 1))
    pool_table.add_column("Provider", style=theme.ui_accent, min_width=16)
    pool_table.add_column("Label", style=theme.banner_text, min_width=18)
    pool_table.add_column("Auth", style=theme.banner_text, min_width=8)
    pool_table.add_column("Source", style=theme.banner_dim, min_width=12)
    pool_table.add_column("Token", style=theme.banner_text, width=5)
    pool_table.add_column("Status", style=theme.banner_text, min_width=12)
    pool_table.add_column("Req", justify="right", min_width=3)
    pool_table.add_column("Cooldown", style=theme.banner_dim, min_width=8)
    pool_table.add_column("Expires", style=theme.banner_dim, min_width=8)
    pool_table.add_column("Refreshed", style=theme.banner_dim, min_width=8)
    pool_table.add_column("Prio", justify="right", min_width=4)
    for entry in sm.credential_pools:
        pool_table.add_row(
            escape(entry.name),
            escape(entry.label),
            escape(entry.auth_type),
            escape(entry.source),
            "Yes" if entry.token_present else "No",
            escape(entry.last_status),
            str(entry.request_count),
            escape(entry.cooldown_remaining),
            escape(entry.expires_at) if entry.expires_at else "—",
            escape(entry.last_refresh) if entry.last_refresh else "—",
            str(entry.priority) if entry.priority else "—",
        )
    return pool_table


def _hooks_table(sm: SkillsMemory, theme: Theme) -> Table:
    hooks_table = Table(box=None, show_header=True, padding=(0, 1))
    hooks_table.add_column("Name", style=theme.ui_accent, min_width=16)
    hooks_table.add_column("Events", style=theme.banner_text, ratio=1)
    hooks_table.add_column("Description", style=theme.banner_dim, ratio=1)
    for hook in sm.hooks:
        hooks_table.add_row(
            escape(hook.name),
            escape(", ".join(hook.events)),
            escape(hook.description),
        )
    return hooks_table


def _plugins_table(sm: SkillsMemory, theme: Theme) -> Table:
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
            escape(plugin.name),
            escape(plugin.version),
            "Yes" if plugin.enabled else "No",
            "Yes" if plugin.dashboard_enabled else "No",
            str(plugin.hook_count),
            str(plugin.tool_count),
            escape(plugin.description),
        )
    return plugins_table


def _mcp_servers_table(sm: SkillsMemory, theme: Theme) -> Table:
    mcp_table = Table(box=None, show_header=True, padding=(0, 1))
    mcp_table.add_column("Name", style=theme.ui_accent, min_width=16)
    mcp_table.add_column("Enabled", style=theme.banner_text, min_width=7)
    mcp_table.add_column("Transport", style=theme.banner_text, min_width=9)
    mcp_table.add_column("Target", style=theme.banner_text, ratio=1)
    mcp_table.add_column("Tools", style=theme.banner_dim, ratio=1)
    for server in sm.mcp_servers:
        mcp_table.add_row(
            escape(server.name),
            "Yes" if server.enabled else "No",
            escape(server.transport) if server.transport else "—",
            escape(server.target) if server.target else "—",
            escape(server.tool_filter) if server.tool_filter else "—",
        )
    return mcp_table


def _skill_rows(sm: SkillsMemory) -> list[tuple[str, str, str]]:
    """Flatten skills into (category label, short name, description) rows."""
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
    return rows


def _skills_header(sm: SkillsMemory, theme: Theme, offset: int, shown: int, total: int) -> Text:
    scroll_hint = (
        f" [{offset + 1}-{min(offset + shown, total)}/{total}]"
        if total > _DETAIL_VISIBLE_SKILL_ROWS
        else ""
    )
    header = Text()
    header.append(
        f"\nSkills ({sm.skill_count} in {sm.skill_categories} categories){scroll_hint}  ",
        style=f"bold {theme.ui_label}",
    )
    if offset > 0:
        header.append("↑ ", style=theme.ui_accent)
    header.append("\n")
    return header


def _skills_table(theme: Theme, visible: list[tuple[str, str, str]], offset: int) -> Table:
    skills_table = Table(box=None, show_header=True, padding=(0, 1))
    skills_table.add_column("Category", style=theme.ui_accent, min_width=12)
    skills_table.add_column("Skill", style=theme.banner_text, min_width=16)
    skills_table.add_column("Description", style=theme.banner_dim, ratio=1)

    # Highlight the first visible row as "selected"
    for idx, (cat_label, short, desc) in enumerate(visible):
        if idx == 0 and offset > 0:
            style = f"bold {theme.ui_accent}"
            skills_table.add_row(
                Text(sanitize_terminal_text(cat_label), style=style),
                Text(sanitize_terminal_text(short), style=style),
                Text(sanitize_terminal_text(desc) or "—", style=theme.banner_text),
            )
        else:
            skills_table.add_row(escape(cat_label), escape(short), escape(desc) if desc else "—")
    return skills_table


def _skills_sections(sm: SkillsMemory, theme: Theme, scroll_offset: int) -> list[RenderableType]:
    rows = _skill_rows(sm)
    total = len(rows)
    # Apply scroll — clamp both ends so the rendered page stays a full window;
    # a negative offset would slice from the end and render nothing.
    offset = max(0, min(scroll_offset, max(0, total - _DETAIL_VISIBLE_SKILL_ROWS)))
    visible = rows[offset : offset + _DETAIL_VISIBLE_SKILL_ROWS]
    return [
        _skills_header(sm, theme, offset, len(visible), total),
        _skills_table(theme, visible, offset),
    ]
