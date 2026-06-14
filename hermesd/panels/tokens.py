from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import AUTHORITATIVE_COST_STATUSES, DashboardState, TokenBreakdown
from hermesd.panels.formatting import fmt_tokens, fmt_usd
from hermesd.theme import Theme


def _fmt_cost(value: float, *, estimated: bool) -> str:
    """Format a USD cost, prefixing '~' when the figure is an estimate.

    Routes through fmt_usd so negatives render as -$x.xx (not $-x.xx).
    """
    return f"~{fmt_usd(value)}" if estimated else fmt_usd(value)


def render_tokens(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    t = state.tokens_today
    total = state.tokens_total
    lines = Text()
    lines.append("  Today", style=theme.ui_label)
    lines.append(
        f"  In:{fmt_tokens(t.input_tokens):>6}  Out:{fmt_tokens(t.output_tokens):>6}\n",
        style=theme.ui_accent,
    )
    lines.append("       ", style=theme.ui_label)
    lines.append(f"  Cache-R:{fmt_tokens(t.cache_read_tokens):>6}\n", style=theme.banner_text)
    lines.append("  Cost", style=theme.ui_label)
    lines.append(
        f"   Today:{_fmt_cost(t.total_cost_usd, estimated=t.cost_is_estimated)}\n",
        style=theme.ui_accent,
    )
    lines.append("       ", style=theme.ui_label)
    lines.append(
        f"   Total:{_fmt_cost(total.total_cost_usd, estimated=total.cost_is_estimated)}",
        style=theme.banner_dim,
    )

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[3] Tokens / Cost[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    sections: list[RenderableType] = []

    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Session", style=theme.session_label)
    table.add_column("In", justify="right", style=theme.banner_text)
    table.add_column("Out", justify="right", style=theme.banner_text)
    table.add_column("Cache-R", justify="right", style=theme.banner_text)
    table.add_column("Cache-W", justify="right", style=theme.banner_text)
    table.add_column("Reason", justify="right", style=theme.banner_text)
    table.add_column("Cost", justify="right", style=theme.ui_accent)

    for s in state.sessions:
        estimated = s.cost_status not in AUTHORITATIVE_COST_STATUSES
        table.add_row(
            escape(s.session_id[-8:]),
            fmt_tokens(s.input_tokens),
            fmt_tokens(s.output_tokens),
            fmt_tokens(s.cache_read_tokens),
            fmt_tokens(s.cache_write_tokens),
            fmt_tokens(s.reasoning_tokens),
            _fmt_cost(s.estimated_cost_usd, estimated=estimated),
        )

    # Aggregate tables mix estimated and reported sessions; reuse the
    # summary-level flag the compact view uses.
    aggregate_estimated = state.tokens_total.cost_is_estimated

    if state.token_analytics.cost_status_counts:
        sections.append(Text("Cost Status\n", style=f"bold {theme.ui_label}"))
        ordered = sorted(
            state.token_analytics.cost_status_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
        line = Text("  ")
        for index, (status, count) in enumerate(ordered):
            if index:
                line.append("  ·  ", style=theme.banner_dim)
            line.append(escape(status), style=theme.ui_label)
            line.append(f" {count}", style=theme.banner_text)
        sections.append(line)

    if state.token_analytics.by_endpoint:
        sections.append(Text("\nBy Endpoint\n", style=f"bold {theme.ui_label}"))
        sections.append(
            _render_breakdown_table(state.token_analytics.by_endpoint, theme, aggregate_estimated)
        )

    if state.token_analytics.windows:
        sections.append(Text("\nRecent Windows\n", style=f"bold {theme.ui_label}"))
        windows = Table(box=None, show_header=True, padding=(0, 2))
        windows.add_column("Window", style=theme.ui_label)
        windows.add_column("Sessions", justify="right", style=theme.banner_text)
        windows.add_column("In", justify="right", style=theme.banner_text)
        windows.add_column("Out", justify="right", style=theme.banner_text)
        windows.add_column("Cache %", justify="right", style=theme.banner_text)
        windows.add_column("Cost", justify="right", style=theme.ui_accent)
        for window in state.token_analytics.windows:
            windows.add_row(
                escape(window.label),
                str(window.session_count),
                fmt_tokens(window.input_tokens),
                fmt_tokens(window.output_tokens),
                f"{window.cache_ratio * 100:.0f}%",
                _fmt_cost(window.total_cost_usd, estimated=aggregate_estimated),
            )
        sections.append(windows)

    if state.token_analytics.by_model:
        sections.append(Text("\nBy Model\n", style=f"bold {theme.ui_label}"))
        sections.append(
            _render_breakdown_table(state.token_analytics.by_model, theme, aggregate_estimated)
        )

    if state.token_analytics.by_provider:
        sections.append(Text("\nBy Provider\n", style=f"bold {theme.ui_label}"))
        sections.append(
            _render_breakdown_table(state.token_analytics.by_provider, theme, aggregate_estimated)
        )

    sections.append(Text("\nSessions\n", style=f"bold {theme.ui_label}"))
    sections.append(table)

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[3] Tokens / Cost[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _render_breakdown_table(entries: list[TokenBreakdown], theme: Theme, estimated: bool) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Label", style=theme.ui_label)
    table.add_column("Sessions", justify="right", style=theme.banner_text)
    table.add_column("In", justify="right", style=theme.banner_text)
    table.add_column("Out", justify="right", style=theme.banner_text)
    table.add_column("Cache-R", justify="right", style=theme.banner_text)
    table.add_column("Cost", justify="right", style=theme.ui_accent)
    for entry in entries:
        table.add_row(
            escape(entry.label),
            str(entry.session_count),
            fmt_tokens(entry.input_tokens),
            fmt_tokens(entry.output_tokens),
            fmt_tokens(entry.cache_read_tokens),
            _fmt_cost(entry.total_cost_usd, estimated=estimated),
        )
    return table
