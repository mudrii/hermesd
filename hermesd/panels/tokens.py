from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import (
    AUTHORITATIVE_COST_STATUSES,
    DashboardState,
    ModelUsage,
    TokenAnalytics,
    TokenBreakdown,
)
from hermesd.panels.formatting import (
    escape_terminal_text as escape,
)
from hermesd.panels.formatting import (
    fmt_tokens,
    fmt_usd,
    section_heading,
)
from hermesd.theme import Theme

_DETAIL_MAX_SESSION_ROWS = 50
_COMPACT_TOP_MODELS = 3


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
    _append_top_models(lines, state, theme)

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[3] Tokens / Cost[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    analytics = state.token_analytics
    # Aggregate tables mix estimated and reported sessions; reuse the
    # summary-level flag the compact view uses.
    aggregate_estimated = state.tokens_total.cost_is_estimated
    sections: list[RenderableType] = []

    if analytics.cost_status_counts:
        sections.append(section_heading("Cost Status", theme, leading_blank=False))
        sections.append(_cost_status_line(analytics, theme))

    if analytics.by_endpoint:
        sections.append(section_heading("By Endpoint", theme))
        sections.append(_render_breakdown_table(analytics.by_endpoint, theme, aggregate_estimated))

    if analytics.windows:
        sections.append(section_heading("Recent Windows", theme))
        sections.append(_windows_table(analytics, theme, aggregate_estimated))

    if _usage_rows_available(state):
        sections.append(section_heading("By Model", theme))
        sections.extend(_model_usage_section(analytics, theme))
    elif analytics.by_model:
        sections.append(section_heading("By Model", theme))
        sections.append(_render_breakdown_table(analytics.by_model, theme, aggregate_estimated))

    if analytics.by_provider:
        sections.append(section_heading("By Provider", theme))
        sections.append(_render_breakdown_table(analytics.by_provider, theme, aggregate_estimated))

    sections.append(section_heading("Sessions", theme))
    sections.append(_sessions_table(state, theme))
    if len(state.sessions) > _DETAIL_MAX_SESSION_ROWS:
        sections.append(
            Text(
                f"  … and {len(state.sessions) - _DETAIL_MAX_SESSION_ROWS} more\n",
                style=theme.banner_dim,
            )
        )

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[3] Tokens / Cost[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _usage_rows_available(state: DashboardState) -> bool:
    """True when the per-model usage table backs the model breakdown."""
    analytics = state.token_analytics
    return analytics.usage_source == "session_model_usage" and bool(analytics.model_usage_all)


def _usage_cost_cell(usage: ModelUsage) -> str:
    """Provider-billed cost when known, otherwise the estimate with an `est.` marker."""
    if usage.has_actual_cost:
        return fmt_usd(usage.actual_cost_usd)
    return f"{fmt_usd(usage.estimated_cost_usd)} est."


def _append_top_models(lines: Text, state: DashboardState, theme: Theme) -> None:
    if not _usage_rows_available(state):
        return
    top = [usage for usage in state.token_analytics.model_usage_all if not usage.task][
        :_COMPACT_TOP_MODELS
    ]
    if not top:
        return
    lines.append("\n  Models", style=theme.ui_label)
    for index, usage in enumerate(top):
        lines.append("  " if index else " ", style=theme.banner_dim)
        lines.append(escape(usage.model or "unknown"), style=theme.banner_text)
        lines.append(f" {_usage_cost_cell(usage)}", style=theme.ui_accent)


def _model_usage_section(analytics: TokenAnalytics, theme: Theme) -> list[RenderableType]:
    """Per-model usage table plus the 24h/7d window summary line."""
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Model", style=theme.ui_label)
    table.add_column("Provider", style=theme.banner_dim)
    table.add_column("Calls", justify="right", style=theme.banner_text)
    table.add_column("In", justify="right", style=theme.banner_text)
    table.add_column("Out", justify="right", style=theme.banner_text)
    table.add_column("Cache-R", justify="right", style=theme.banner_text)
    table.add_column("Cost", justify="right", style=theme.ui_accent)
    aux: list[ModelUsage] = []
    for usage in analytics.model_usage_all:
        if usage.task:
            aux.append(usage)
            continue
        table.add_row(
            escape(usage.model or "unknown"),
            escape(usage.provider) if usage.provider else "—",
            str(usage.api_calls),
            fmt_tokens(usage.input_tokens),
            fmt_tokens(usage.output_tokens),
            fmt_tokens(usage.cache_read_tokens),
            _usage_cost_cell(usage),
        )
    if aux:
        table.add_row(*_aux_subtotal_row(aux))
    return [table, _usage_windows_line(analytics, theme)]


def _aux_subtotal_row(aux: list[ModelUsage]) -> tuple[str, ...]:
    """One `aux` row summing the task-tagged (auxiliary) model usage."""
    actual = sum(usage.actual_cost_usd for usage in aux)
    estimated = sum(usage.estimated_cost_usd for usage in aux)
    return (
        f"aux ({len(aux)})",
        "—",
        str(sum(usage.api_calls for usage in aux)),
        fmt_tokens(sum(usage.input_tokens for usage in aux)),
        fmt_tokens(sum(usage.output_tokens for usage in aux)),
        fmt_tokens(sum(usage.cache_read_tokens for usage in aux)),
        fmt_usd(actual) if actual > 0 else f"{fmt_usd(estimated)} est.",
    )


def _usage_windows_line(analytics: TokenAnalytics, theme: Theme) -> Text:
    line = Text("  ")
    for label, rows in (("24h", analytics.model_usage_24h), ("7d", analytics.model_usage_7d)):
        tokens = sum(usage.input_tokens + usage.output_tokens for usage in rows)
        line.append(f"{label} ", style=theme.ui_label)
        line.append(f"{len(rows)} models  {fmt_tokens(tokens)} tok   ", style=theme.banner_text)
    return line


def _sessions_table(state: DashboardState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 2))
    table.add_column("Session", style=theme.session_label)
    table.add_column("In", justify="right", style=theme.banner_text)
    table.add_column("Out", justify="right", style=theme.banner_text)
    table.add_column("Cache-R", justify="right", style=theme.banner_text)
    table.add_column("Cache-W", justify="right", style=theme.banner_text)
    table.add_column("Reason", justify="right", style=theme.banner_text)
    table.add_column("Cost", justify="right", style=theme.ui_accent)

    for s in state.sessions[:_DETAIL_MAX_SESSION_ROWS]:
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
    return table


def _cost_status_line(analytics: TokenAnalytics, theme: Theme) -> Text:
    ordered = sorted(
        analytics.cost_status_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )
    line = Text("  ")
    for index, (status, count) in enumerate(ordered):
        if index:
            line.append("  ·  ", style=theme.banner_dim)
        line.append(escape(status), style=theme.ui_label)
        line.append(f" {count}", style=theme.banner_text)
    return line


def _windows_table(analytics: TokenAnalytics, theme: Theme, estimated: bool) -> Table:
    windows = Table(box=None, show_header=True, padding=(0, 2))
    windows.add_column("Window", style=theme.ui_label)
    windows.add_column("Sessions", justify="right", style=theme.banner_text)
    windows.add_column("In", justify="right", style=theme.banner_text)
    windows.add_column("Out", justify="right", style=theme.banner_text)
    windows.add_column("Cache %", justify="right", style=theme.banner_text)
    windows.add_column("Cost", justify="right", style=theme.ui_accent)
    for window in analytics.windows:
        windows.add_row(
            escape(window.label),
            str(window.session_count),
            fmt_tokens(window.input_tokens),
            fmt_tokens(window.output_tokens),
            f"{window.cache_ratio * 100:.0f}%",
            _fmt_cost(window.total_cost_usd, estimated=estimated),
        )
    return windows


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
