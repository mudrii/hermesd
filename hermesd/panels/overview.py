from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import (
    DashboardState,
    MCPCacheEntry,
    MCPCacheEntryState,
    MCPSchemaCache,
    PluginActivation,
    PluginInfo,
    SkillsMemory,
)
from hermesd.panels.formatting import escape_terminal_text as escape
from hermesd.panels.formatting import (
    fmt_age_seconds,
    sanitize_terminal_text,
    section_heading,
)
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
    expand_skills: bool = False,
) -> Panel:
    if detail:
        return _render_detail(state, theme, scroll_offset, expand_skills)
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
    # A trailing "+" marks a walk that hit its directory budget: the number is
    # what hermesd retained, not what is on disk.
    plugin_count = f"{len(sm.plugins)}+" if sm.plugin_scan_truncated else f"{len(sm.plugins)}"
    desktop_count = (
        f"{len(sm.desktop_plugins)}+"
        if sm.desktop_plugin_scan_truncated
        else f"{len(sm.desktop_plugins)}"
    )
    lines.append(
        f"{plugin_count} plug (agent)  {desktop_count} plug (desktop)  {len(sm.mcp_servers)} mcp\n",
        style=theme.banner_text,
    )
    if sm.plugin_catalog_update_count or sm.plugin_catalog_removed_count:
        lines.append("  Catalog: ", style=theme.ui_label)
        catalog_style = theme.ui_warn if sm.plugin_catalog_removed_count else theme.banner_text
        parts = []
        if sm.plugin_catalog_update_count:
            parts.append(f"{sm.plugin_catalog_update_count} updates")
        if sm.plugin_catalog_removed_count:
            parts.append(f"{sm.plugin_catalog_removed_count} removed")
        lines.append(" · ".join(parts), style=catalog_style)
        lines.append("\n", style=theme.banner_text)
    if state.mcp_cache.mcp_cached_server_count:
        lines.append("  Schema cache: ", style=theme.ui_label)
        lines.append(
            f"mcp {state.mcp_cache.mcp_cached_server_count} cached"
            f"{_cache_validity_suffix(state.mcp_cache)}\n",
            style=theme.banner_text,
        )
    for p in sm.providers[:4]:
        sym = "✓" if p.is_active else "✗"
        color = theme.ui_ok if p.is_active else theme.banner_dim
        lines.append(f"  {sym} ", style=color)
        lines.append(f"{sanitize_terminal_text(p.name)} ", style=theme.banner_text)
        if p.free_tier:
            lines.append("Nous free tier", style=theme.ui_ok)

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[7] Skills / Integrations[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(
    state: DashboardState,
    theme: Theme,
    scroll_offset: int,
    expand_skills: bool,
) -> Panel:
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
        sections.append(_plugins_note(sm, theme))

    if sm.desktop_plugins or sm.desktop_plugin_scan_truncated:
        sections.append(section_heading("Desktop Plugins", theme))
        sections.append(_desktop_plugins_table(sm, theme))
        sections.append(_desktop_plugins_note(sm, theme))

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
        sections.extend(_skills_sections(sm, theme, scroll_offset, expand_skills=expand_skills))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[7] Skills / Integrations[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _mcp_cache_section(state: DashboardState, theme: Theme) -> list[RenderableType]:
    """MCP schema cache block: cached names, age, per-entry validity, and
    servers with no entry."""
    cache = state.mcp_cache
    heading = section_heading("MCP", theme)
    if not cache.mcp_cache_present:
        return [heading, Text("  no cache file observed", style=theme.banner_dim)]

    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.ui_accent)
    names = ", ".join(cache.mcp_cached_server_names)
    table.add_row(
        "Cached servers",
        escape(f"{cache.mcp_cached_server_count} ({names})") if names else "—",
    )
    table.add_row("Cache age", _age_label(cache.mcp_schema_cache_age_seconds))
    table.add_row("Entry validity", _validity_summary(cache))
    # An absent entry says nothing about whether the server ever connected: the
    # cache can be cleared, invalidated, or written under another profile.
    table.add_row(
        "No cache entry",
        _bounded_name_list(cache.mcp_uncached_server_count, cache.mcp_uncached_server_names),
    )
    sections: list[RenderableType] = [heading, table]
    if cache.mcp_entries:
        sections.append(_mcp_entry_lines(cache, theme))
    sections.append(Text(f"  {_MCP_VALIDITY_NOTE}", style=theme.banner_dim))
    return sections


# Rendered on every pass, like the plugins note: the validity column is a TTL
# verdict about a cache file, and reading it as a health verdict about the
# server is exactly the mistake worth pre-empting.
_MCP_VALIDITY_NOTE = (
    "Entry validity is the schema-cache TTL rule only: a valid entry does not "
    "prove credentials work or that the server is reachable, and an expired one "
    "is not an error — the next call re-probes it. The fingerprint shown is the "
    "cache's own record, not a comparison hermesd performed."
)


def _cache_validity_suffix(cache: MCPSchemaCache) -> str:
    """Compact-view marker for the entries that are not being served as-is.

    An all-valid cache stays unannotated: the compact line has room for a
    problem, not for a restatement of the count beside it.
    """
    flags = (
        (cache.mcp_expired_entry_count, "expired"),
        (cache.mcp_unassessable_entry_count, "unassessable"),
    )
    parts = [f"{count} {label}" for count, label in flags if count]
    return f" ({', '.join(parts)})" if parts else ""


def _validity_summary(cache: MCPSchemaCache) -> str:
    """Rollup over every entry in the file, not over the bounded rendered list."""
    counts = (
        (cache.mcp_valid_entry_count, "valid"),
        (cache.mcp_expired_entry_count, "expired"),
        (cache.mcp_unassessable_entry_count, "unassessable"),
    )
    parts = [f"{count} {label}" for count, label in counts if count]
    return escape(" · ".join(parts)) if parts else "—"


def _mcp_entry_lines(cache: MCPSchemaCache, theme: Theme) -> Text:
    """One line per retained entry, plus a marker when the list was bounded."""
    lines = Text()
    for entry in cache.mcp_entries:
        lines.append(f"  {sanitize_terminal_text(entry.name)}  ", style=theme.ui_label)
        lines.append(entry.state.value, style=_entry_style(entry.state, theme))
        lines.append(f" — {sanitize_terminal_text(_entry_detail(entry))}", style=theme.banner_dim)
        if entry.fingerprint:
            lines.append(
                f"  fp {sanitize_terminal_text(entry.fingerprint)}", style=theme.banner_dim
            )
        lines.append("\n")
    hidden = cache.mcp_cached_server_count - len(cache.mcp_entries)
    if hidden > 0:
        lines.append(f"  (+{hidden} more)\n", style=theme.banner_dim)
    return lines


def _entry_style(state: MCPCacheEntryState, theme: Theme) -> str:
    """Expired is deliberately *not* an error colour: it only means the next
    call re-probes the server instead of using the cache."""
    if state is MCPCacheEntryState.VALID:
        return theme.ui_ok
    if state is MCPCacheEntryState.EXPIRED:
        return theme.banner_dim
    return theme.ui_warn


def _entry_detail(entry: MCPCacheEntry) -> str:
    """The TTL story behind an entry's state, in the entry's own numbers."""
    if entry.state is MCPCacheEntryState.UNASSESSABLE:
        return entry.reason or "entry could not be assessed"
    ttl = _duration_label(entry.ttl_ms / 1000.0) if entry.ttl_ms is not None else ""
    if entry.state is MCPCacheEntryState.EXPIRED:
        head = f"{ttl} ttl elapsed" if ttl else "ttl elapsed"
        elapsed = _age_label(entry.age_seconds) if entry.age_seconds is not None else ""
        return f"{head} {elapsed} ago" if elapsed else head
    if not ttl:
        return "no ttl recorded — never expires"
    if entry.remaining_seconds is None:
        return "no written_at recorded — never expires"
    return f"{_duration_label(entry.remaining_seconds)} of {ttl} ttl left"


def _duration_label(seconds: float) -> str:
    """A TTL or a remaining window; sub-second values keep their milliseconds."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return fmt_age_seconds(int(seconds))


def _bounded_name_list(count: int, names: list[str]) -> str:
    """Render a display-bounded name list without hiding that it was bounded."""
    if not names:
        return "—"
    hidden = count - len(names)
    suffix = f" (+{hidden} more)" if hidden > 0 else ""
    return escape(f"{', '.join(names)}{suffix}")


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
    prov_table.add_column("Identity", style=theme.ui_ok)
    for p in sm.providers:
        sym = (
            Text("●", style=f"bold {theme.ui_ok}")
            if p.is_active
            else Text("○", style=theme.banner_dim)
        )
        prov_table.add_row(sym, escape(p.name), "Nous free tier" if p.free_tier else "")
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
    plugins_table.add_column("Activation", style=theme.banner_text, min_width=11)
    plugins_table.add_column("Declares", style=theme.banner_text, min_width=8)
    plugins_table.add_column("Provenance", style=theme.banner_text, min_width=18)
    plugins_table.add_column("Catalog", style=theme.banner_text, min_width=10)
    plugins_table.add_column("Dashboard", style=theme.banner_text, min_width=9)
    plugins_table.add_column("Hooks", justify="right", min_width=5)
    plugins_table.add_column("Tools", justify="right", min_width=5)
    plugins_table.add_column("Description", style=theme.banner_dim, ratio=1)
    for plugin in sm.plugins:
        plugins_table.add_row(
            escape(plugin.name),
            escape(plugin.version),
            _activation_label(plugin.activation),
            _declares_label(plugin),
            _provenance_label(plugin),
            _catalog_state_label(plugin),
            "Yes" if plugin.dashboard_enabled else "No",
            str(plugin.hook_count),
            str(plugin.tool_count),
            escape(plugin.description),
        )
    return plugins_table


def _catalog_state_label(plugin: PluginInfo) -> str:
    """The live catalog's verdict for this install, when the cache allows one.

    Removal outranks an available update: a plugin on the kill list should not
    be updated at all. ``unmanaged`` is a fact about the files (no provenance
    sidecar recorded anything), not an error state.
    """
    if plugin.catalog_removed:
        return "removed"
    if plugin.catalog_update_available:
        return "update available"
    if plugin.unmanaged:
        return "unmanaged"
    return "—"


def _desktop_plugins_table(sm: SkillsMemory, theme: Theme) -> Table:
    plugins_table = Table(box=None, show_header=True, padding=(0, 1))
    plugins_table.add_column("Folder", style=theme.ui_accent, min_width=16)
    plugins_table.add_column("Entry", style=theme.banner_text)
    for plugin in sm.desktop_plugins:
        plugins_table.add_row(
            escape(sanitize_terminal_text(plugin.name)),
            "plugin.js present",
        )
    return plugins_table


def _desktop_plugins_note(sm: SkillsMemory, theme: Theme) -> Text:
    note = Text(
        "\nInventory only: hermesd does not read or execute plugin.js and cannot observe "
        "enabled or loaded state.\n",
        style=theme.banner_dim,
    )
    if sm.desktop_plugin_scan_truncated:
        note.append(
            "  ⚠ desktop plugin list truncated: the directory scan hit its budget\n",
            style=theme.banner_dim,
        )
    return note


# Activation is read from config.yaml and the manifest: it says what hermes-agent
# would do, and a plugin that clears the gate can still fail to import.
_ACTIVATION_LABELS = {
    PluginActivation.ENABLED: "enabled",
    PluginActivation.DISABLED: "disabled",
    PluginActivation.NOT_ENABLED: "not enabled",
    PluginActivation.CATEGORY_OWNED: "category",
    PluginActivation.REMOVED: "removed",
    PluginActivation.UNKNOWN: "unknown",
}


def _activation_label(activation: PluginActivation) -> str:
    return escape(_ACTIVATION_LABELS.get(activation, str(activation)))


def _declares_label(plugin: PluginInfo) -> str:
    """What the manifest *claims* — never what hermesd observed.

    The capability count is the full declared one, not the length of the
    display-bounded list, so capping that list can never change what the panel
    reports. Upstream still gates every declared capability behind an explicit
    grant, and evaluates ``requires_hermes`` against the running version at load
    time; neither is something hermesd can see.
    """
    tokens: list[str] = []
    if plugin.requires_hermes:
        tokens.append(plugin.requires_hermes)
    if plugin.declared_capability_count:
        tokens.append(f"caps:{plugin.declared_capability_count}")
    return escape(" ".join(tokens)) if tokens else "—"


def _provenance_label(plugin: PluginInfo) -> str:
    """Upstream's own annotation shapes, with a disagreement made visible.

    ``catalog:<tier>@<sha8>`` is ``plugins_cmd_catalog.catalog_annotation``
    (``:88-93``) and ``git pinned@<sha8>`` is ``plugins_cmd._pin_annotation``
    (``:457-459``). They stay separate tokens because they are separate claims:
    the catalog sha is the commit that was *reviewed*, the git sha the commit that
    was *installed*, and ``install_catalog_entry`` writes the sidecar from the
    catalog entry even when ``--ref`` installed something else (``:108-118``).

    When the two agree the second token adds nothing and is dropped; when they
    differ both are shown, because that difference is the only evidence available
    that the code on disk is not the code that was reviewed. Picking one would
    hide exactly the case worth seeing.
    """
    tokens: list[str] = []
    if plugin.catalog_name or plugin.catalog_sha:
        tier = plugin.catalog_tier or "community"
        tokens.append(
            f"catalog:{tier}@{plugin.catalog_sha[:8]}" if plugin.catalog_sha else f"catalog:{tier}"
        )
    if plugin.installed_revision and (plugin.provenance_drift or not tokens):
        verb = "git pinned@" if plugin.pinned else "git@"
        tokens.append(f"{verb}{plugin.installed_revision[:8]}")
    if plugin.provenance_drift:
        tokens.append("⚠ drift")
    return escape(" ".join(tokens)) if tokens else "—"


# Provenance and activation are both records read off disk, so this is rendered on
# every pass rather than only when something looks wrong: the strongest claim
# hermesd can make about a plugin is what its files say.
_PROVENANCE_NOTE = (
    "Activation, provenance and declarations are read from files — hermesd never "
    "imports plugin code, so none of them proves a plugin loads."
)
# Bound on the conflicting-manifest list only. The plugins themselves are all in
# the table above; this note names a few and states the true count.
_CONFLICT_NOTE_LIMIT = 3


# Catalog-cache note: an absent cache must not read as "everything current" —
# the drift/removal checks simply made no claims that pass.
_CATALOG_NOTE_LIMIT = 3


def _plugins_note(sm: SkillsMemory, theme: Theme) -> Text:
    """What the plugins table cannot fit in a cell."""
    note = Text()
    note.append(f"\n{_PROVENANCE_NOTE}\n", style=theme.banner_dim)
    _append_catalog_note(sm, note, theme)
    if sm.plugin_scan_truncated:
        note.append(
            "  ⚠ plugin list truncated: the directory scan hit its budget, so the"
            " table above is bounded, not a complete inventory\n",
            style=theme.banner_dim,
        )
    conflicts = [plugin for plugin in sm.plugins if plugin.manifest_shadowed]
    if conflicts:
        subject = (
            "1 plugin carries a conflicting manifest"
            if len(conflicts) == 1
            else f"{len(conflicts)} plugins carry conflicting manifests"
        )
        note.append(
            f"  ⚠ {subject} (plugin.yaml outranks plugin.yml outranks plugin.json):\n",
            style=theme.banner_dim,
        )
        for plugin in conflicts[:_CONFLICT_NOTE_LIMIT]:
            ignored = ", ".join(plugin.manifest_shadowed)
            # Text.append does not parse markup, so these are sanitized (control
            # bytes stripped) rather than escaped — escaping would render the
            # backslash literally. Table cells above are markup-parsed and do
            # need escape().
            note.append(
                f"    {sanitize_terminal_text(plugin.name)}:"
                f" {sanitize_terminal_text(plugin.manifest_file)} used;"
                f" {sanitize_terminal_text(ignored)} ignored\n",
                style=theme.banner_dim,
            )
        if len(conflicts) > _CONFLICT_NOTE_LIMIT:
            note.append(
                f"    (+{len(conflicts) - _CONFLICT_NOTE_LIMIT} more)\n", style=theme.banner_dim
            )
    return note


def _append_catalog_note(sm: SkillsMemory, note: Text, theme: Theme) -> None:
    """Catalog drift/removal counts and the cache state behind them."""
    if not sm.plugin_catalog_cache_present:
        note.append(
            "  no catalog cache observed — update/removal checks unavailable\n",
            style=theme.banner_dim,
        )
        return
    age = sm.plugin_catalog_cache_age_seconds
    cache_line = "catalog cache observed" if age is None else f"catalog cache {_age_label(age)} old"
    note.append(f"  {cache_line}: ", style=theme.banner_dim)
    parts = []
    if sm.plugin_catalog_update_count:
        parts.append(f"{sm.plugin_catalog_update_count} update(s) available")
    if sm.plugin_catalog_removed_count:
        parts.append(f"{sm.plugin_catalog_removed_count} removed from catalog")
    note.append(
        " · ".join(parts) + "\n" if parts else "plugins match the catalog\n",
        style=theme.banner_dim,
    )
    removed = [plugin for plugin in sm.plugins if plugin.catalog_removed]
    for plugin in removed[:_CATALOG_NOTE_LIMIT]:
        reason = plugin.catalog_removed_reason or "no reason recorded"
        note.append(
            f"    {sanitize_terminal_text(plugin.name)}: {sanitize_terminal_text(reason)}\n",
            style=theme.banner_dim,
        )
    if len(removed) > _CATALOG_NOTE_LIMIT:
        note.append(f"    (+{len(removed) - _CATALOG_NOTE_LIMIT} more)\n", style=theme.banner_dim)


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


def _skills_sections(
    sm: SkillsMemory,
    theme: Theme,
    scroll_offset: int,
    *,
    expand_skills: bool = False,
) -> list[RenderableType]:
    rows = _skill_rows(sm)
    total = len(rows)
    if expand_skills:
        offset = 0
        visible = rows
    else:
        # Apply scroll — clamp both ends so the rendered page stays a full
        # window; a negative offset would slice from the end and render nothing.
        offset = max(0, min(scroll_offset, max(0, total - _DETAIL_VISIBLE_SKILL_ROWS)))
        visible = rows[offset : offset + _DETAIL_VISIBLE_SKILL_ROWS]
    return [
        _skills_header(sm, theme, offset, len(visible), total),
        _skills_table(theme, visible, offset),
    ]
