from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import CuratorRun, DashboardState
from hermesd.panels.formatting import escape_terminal_text as escape
from hermesd.panels.formatting import (
    fmt_age_seconds,
    sanitize_terminal_text,
    section_heading,
)
from hermesd.theme import Theme

_TITLE = "\\[13] Curator"
_SUMMARY_MAX_CHARS = 600


def render_curator(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _delta_label(cur: CuratorRun) -> str:
    sign = "+" if cur.count_delta > 0 else ""
    return f"{cur.count_before} → {cur.count_after} ({sign}{cur.count_delta})"


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    cur = state.curator
    lines = Text()
    if not cur.run_present:
        if cur.scheduler_state_present:
            lines.append("  Scheduler: ", style=theme.ui_label)
            lines.append("paused" if cur.scheduler_paused else "active", style=theme.banner_text)
            lines.append("\n  Runs: ", style=theme.ui_label)
            lines.append(str(cur.scheduler_run_count), style=theme.banner_text)
        else:
            lines.append("  No curation runs", style=theme.banner_dim)
    else:
        lines.append("  Last run: ", style=theme.ui_label)
        lines.append(
            f"{sanitize_terminal_text(cur.stamp) if cur.stamp else '—'}\n",
            style=theme.banner_text,
        )
        lines.append("  Skills: ", style=theme.ui_label)
        lines.append(f"{_delta_label(cur)}\n", style=theme.banner_text)
        lines.append("  Archived ", style=theme.ui_label)
        lines.append(str(cur.archived_count), style=theme.banner_text)
        lines.append("  Pruned ", style=theme.ui_label)
        lines.append(str(cur.pruned_count), style=theme.banner_text)
        if cur.llm_error:
            lines.append("  ⚠ error", style=theme.ui_warn)
    _append_hygiene_compact(cur, lines, theme)
    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]{_TITLE}[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    cur = state.curator
    if not cur.run_present:
        # Skill hygiene does not depend on a run having happened, so it renders
        # in the no-run shapes too.
        if cur.scheduler_state_present:
            body: RenderableType = _scheduler_group(cur, theme)
        else:
            body = Text("  No curation runs found", style=theme.banner_dim)
        sections: list[RenderableType] = [body]
        if cur.managed_skill_count:
            sections.append(section_heading("Skill Hygiene", theme))
            sections.append(_hygiene_table(cur, theme))
            sections.extend(_hygiene_notes(cur, theme))
        return Panel(
            Group(*sections),
            title=f"[{theme.panel_title_style}]{_TITLE}[/]",
            title_align="left",
            border_style=theme.panel_border_style,
            box=rich.box.HORIZONTALS,
            padding=(1, 2),
        )

    summary = Table(box=None, show_header=False, padding=(0, 2))
    summary.add_column("Key", style=theme.ui_label)
    summary.add_column("Value", style=theme.banner_text)
    summary.add_row("Run", escape(cur.stamp))
    if cur.started_at:
        summary.add_row("Started", escape(cur.started_at))
    summary.add_row("Duration", f"{cur.duration_seconds:.1f}s")
    summary.add_row("Model", escape(cur.model) if cur.model else "—")
    summary.add_row("Provider", escape(cur.provider) if cur.provider else "—")
    summary.add_row("Skills", _delta_label(cur))
    summary.add_row("Archived", str(cur.archived_count))
    summary.add_row("Added", str(cur.added_count))
    summary.add_row("Pruned", str(cur.pruned_count))
    summary.add_row("Consolidated", str(cur.consolidated_count))
    summary.add_row("Tool Calls", str(cur.tool_calls_total))

    sections = [summary]

    if cur.scheduler_state_present:
        sections.append(section_heading("Scheduler", theme))
        sections.append(_scheduler_table(cur, theme))

    if cur.managed_skill_count:
        sections.append(section_heading("Skill Hygiene", theme))
        sections.append(_hygiene_table(cur, theme))
        sections.extend(_hygiene_notes(cur, theme))

    if cur.tool_call_counts:
        sections.append(section_heading("Tool Calls", theme))
        tools = Table(box=None, show_header=True, padding=(0, 2))
        tools.add_column("Tool", style=theme.ui_label)
        tools.add_column("Calls", justify="right", style=theme.banner_text)
        for tool, count in sorted(cur.tool_call_counts.items()):
            tools.add_row(escape(tool), str(count))
        sections.append(tools)

    if cur.state_transitions:
        sections.append(section_heading("State Transitions", theme))
        transitions = Text()
        for transition in cur.state_transitions[:10]:
            transitions.append(f"  {sanitize_terminal_text(transition)}\n", style=theme.banner_dim)
        sections.append(transitions)

    if cur.llm_error:
        sections.append(Text("\nError\n", style=f"bold {theme.ui_error}"))
        sections.append(Text(f"  {sanitize_terminal_text(cur.llm_error)}", style=theme.ui_error))
    elif cur.llm_summary:
        sections.append(section_heading("Summary", theme))
        summary_text = cur.llm_summary[:_SUMMARY_MAX_CHARS]
        if len(cur.llm_summary) > _SUMMARY_MAX_CHARS:
            summary_text += "…"
        sections.append(Text(sanitize_terminal_text(summary_text), style=theme.banner_dim))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]{_TITLE}[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _scheduler_group(cur: CuratorRun, theme: Theme) -> Group:
    return Group(
        section_heading("Scheduler", theme, leading_blank=False),
        _scheduler_table(cur, theme),
    )


def _append_hygiene_compact(cur: CuratorRun, lines: Text, theme: Theme) -> None:
    """The patch-reuse and state rollup, in both compact shapes.

    A patched-but-never-re-used skill is the loop the curator cannot see from
    run reports alone, so it is the one count worth surfacing even when the
    panel has no run to show.
    """
    if not cur.managed_skill_count:
        return
    lines.append("\n  Patch reuse: ", style=theme.ui_label)
    if cur.patch_pending_reuse_count:
        lines.append(f"{cur.patch_pending_reuse_count} patched, not re-used", style=theme.ui_warn)
    else:
        lines.append("all re-used", style=theme.banner_text)
    lines.append("\n  States: ", style=theme.ui_label)
    lines.append(_state_counts_label(cur), style=theme.banner_text)
    if cur.pinned_count:
        lines.append(f" · {cur.pinned_count} pinned", style=theme.banner_text)


def _hygiene_table(cur: CuratorRun, theme: Theme) -> Table:
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.banner_text)
    table.add_row("Managed", str(cur.managed_skill_count))
    table.add_row("Patch reuse", _patch_reuse_label(cur))
    table.add_row("Pinned", str(cur.pinned_count))
    table.add_row("Thresholds", _threshold_label(cur))
    if cur.skill_windows:
        table.add_row("Windows", _state_counts_label(cur))
        table.add_row("", _window_table(cur, theme))
    return table


def _patch_reuse_label(cur: CuratorRun) -> str:
    if not cur.patch_pending_reuse_count:
        return "all re-used (patch_generation <= last_reused)"
    return (
        f"{cur.patch_pending_reuse_count} patched but not yet re-used "
        "(patch_generation > last_reused)"
    )


def _threshold_label(cur: CuratorRun) -> str:
    label = f"stale after {cur.stale_after_days}d · archive after {cur.archive_after_days}d"
    if cur.thresholds_customized:
        return f"{label} (curator.stale_after_days / curator.archive_after_days)"
    return f"{label} (defaults)"


def _state_counts_label(cur: CuratorRun) -> str:
    """The state rollup, in one place: the compact line and the table share it.

    ``unknown`` is appended only when it is non-zero, so a healthy home reads
    as three buckets rather than four with a permanent zero.
    """
    states = [
        f"{cur.state_active_count} active",
        f"{cur.state_stale_count} stale",
        f"{cur.state_archived_count} archived",
    ]
    if cur.state_unknown_count:
        states.append(f"{cur.state_unknown_count} unknown")
    return " · ".join(states)


def _window_table(cur: CuratorRun, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Skill", style=theme.ui_accent, min_width=14)
    table.add_column("State", style=theme.banner_text, min_width=9)
    table.add_column("Last activity", style=theme.banner_text, min_width=10)
    table.add_column("Until stale", style=theme.banner_text, min_width=9)
    table.add_column("Until archive", style=theme.banner_text, min_width=9)
    for window in cur.skill_windows:
        name = escape(window.name)
        if window.pinned:
            name += " 📌"
        table.add_row(
            name,
            escape(window.state),
            _activity_label(window.last_activity_age_seconds),
            _days_label(window.days_until_stale),
            _days_label(window.days_until_archive),
        )
    hidden = cur.managed_skill_count - len(cur.skill_windows)
    if hidden > 0:
        table.add_row(f"(+{hidden} more)", "", "", "", "")
    return table


def _activity_label(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "never"
    return fmt_age_seconds(max(0, int(age_seconds))) + " ago"


def _days_label(days: float | None) -> str:
    if days is None:
        return "—"
    if days <= 0:
        return "due"
    return f"{days:.0f}d"


def _hygiene_notes(cur: CuratorRun, theme: Theme) -> list[RenderableType]:
    note = Text(
        "\n  Windows run from each skill's last use, view or patch — created_at is"
        " excluded upstream, so a never-used skill shows no window.\n",
        style=theme.banner_dim,
    )
    return [note]


def _scheduler_table(cur: CuratorRun, theme: Theme) -> Table:
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.banner_text)
    table.add_row("State", "paused" if cur.scheduler_paused else "active")
    table.add_row("Run Count", str(cur.scheduler_run_count))
    table.add_row(
        "Last Run", escape(cur.scheduler_last_run_at) if cur.scheduler_last_run_at else "—"
    )
    table.add_row(
        "Last Report",
        escape(cur.scheduler_last_report_path) if cur.scheduler_last_report_path else "—",
    )
    table.add_row("Consolidate", "consolidate on" if cur.consolidate_enabled else "consolidate off")
    return table
