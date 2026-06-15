from __future__ import annotations

import time

import rich.box
from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_operations(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    ops = state.operations
    model_count = sum(cache.model_count for cache in ops.model_caches)
    lines = Text()
    lines.append("  Dashboard: ", style=theme.ui_label)
    lines.append(f"{ops.dashboard_process_count} proc\n", style=theme.banner_text)
    lines.append("  Model Caches: ", style=theme.ui_label)
    lines.append(f"{len(ops.model_caches)} files  {model_count} models\n", style=theme.banner_text)
    lines.append("  PR Monitors: ", style=theme.ui_label)
    lines.append(str(len(ops.pr_monitors)), style=theme.banner_text)
    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[12] Operations[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    ops = state.operations
    sections: list[RenderableType] = []

    summary = Table(box=None, show_header=False, padding=(0, 2))
    summary.add_column("Key", style=theme.ui_label)
    summary.add_column("Value", style=theme.banner_text)
    summary.add_row("Dashboard Processes", str(ops.dashboard_process_count))
    summary.add_row(
        "Desktop Build", escape(ops.desktop_build_stamp) if ops.desktop_build_stamp else "—"
    )
    if ops.response_store_present:
        summary.add_row(
            "Response Store",
            f"{ops.conversation_count} conversations  {ops.response_count} responses  "
            f"{_size_label(ops.response_store_size_bytes)}",
        )
    sections.append(summary)

    if ops.model_caches:
        sections.append(Text("\nModel Caches\n", style=f"bold {theme.ui_label}"))
        cache_table = Table(box=None, show_header=True, padding=(0, 1))
        cache_table.add_column("File", style=theme.ui_accent)
        cache_table.add_column("Providers", justify="right", style=theme.banner_text)
        cache_table.add_column("Models", justify="right", style=theme.banner_text)
        cache_table.add_column("Size", justify="right", style=theme.banner_dim)
        cache_table.add_column("Age", style=theme.banner_dim)
        for cache in ops.model_caches:
            cache_table.add_row(
                escape(cache.name),
                str(cache.provider_count),
                str(cache.model_count),
                _size_label(cache.size_bytes),
                _age_label(cache.mtime),
            )
        sections.append(cache_table)

    if ops.pr_monitors:
        sections.append(Text("\nPR Monitors\n", style=f"bold {theme.ui_label}"))
        pr_table = Table(box=None, show_header=True, padding=(0, 1))
        pr_table.add_column("File", style=theme.ui_accent)
        pr_table.add_column("Repo", style=theme.banner_text)
        pr_table.add_column("Checked", style=theme.banner_dim)
        pr_table.add_column("Monitored", justify="right", style=theme.banner_text)
        pr_table.add_column("Tracked", justify="right", style=theme.banner_text)
        pr_table.add_column("Author", justify="right", style=theme.banner_text)
        for monitor in ops.pr_monitors:
            pr_table.add_row(
                escape(monitor.filename),
                escape(monitor.repo) if monitor.repo else "—",
                escape(monitor.checked_at) if monitor.checked_at else "—",
                str(monitor.monitored_count),
                str(monitor.tracked_count),
                str(monitor.author_pr_count),
            )
        sections.append(pr_table)

    if (
        not ops.model_caches
        and not ops.pr_monitors
        and ops.dashboard_process_count == 0
        and not ops.response_store_present
    ):
        sections.append(Text("\n  No operations artifacts found\n", style=theme.banner_dim))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[12] Operations[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _size_label(size_bytes: int) -> str:
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.1f}M"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.1f}K"
    return str(size_bytes)


def _age_label(timestamp: float | None) -> str:
    if timestamp is None:
        return "—"
    age = max(0, int(time.time() - timestamp))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    return f"{age // 3600}h"
