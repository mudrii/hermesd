from __future__ import annotations

from typing import TypedDict

import rich.box
from rich.panel import Panel
from rich.text import Text

from hermesd.models import (
    DashboardState,
    LogHealthCounter,
    LogLine,
    LogStreamHealth,
    SourceScope,
)
from hermesd.panels.formatting import sanitize_terminal_text
from hermesd.theme import Theme


class LogFilterCriteria(TypedDict):
    fields: dict[str, list[str]]
    terms: list[str]


_LOG_LEVEL_RANK = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "critical": 50,
}
_DETAIL_VISIBLE_LOG_LINES = 10
_MIN_DETAIL_VISIBLE_LOG_LINES = 3
# Rows the detail spends around its log lines: panel borders and padding (4),
# tab bar, scope line, position line and the blank row below it (4).
_DETAIL_CHROME_ROWS = 8


def _resolve_log_view(
    state: DashboardState, sub_view: str, filter_query: str
) -> tuple[dict[str, list[LogLine]], str, list[LogLine], list[LogLine]]:
    """Resolve the active sub-view plus its unfiltered and filtered lines."""
    log_map = _log_stream_map(state)
    if sub_view not in log_map:
        sub_view = next(iter(log_map), "agent")
    unfiltered = log_map.get(sub_view, state.logs.agent_lines)
    return log_map, sub_view, unfiltered, _filter_log_lines(unfiltered, filter_query)


def _visible_line_count(detail_height: int | None, filter_query: str, extra_rows: int = 0) -> int:
    """Log lines that fit in ``detail_height`` rows (fixed default when unknown)."""
    if detail_height is None:
        return _DETAIL_VISIBLE_LOG_LINES
    chrome = _DETAIL_CHROME_ROWS + (1 if filter_query else 0) + extra_rows
    return max(_MIN_DETAIL_VISIBLE_LOG_LINES, detail_height - chrome)


def max_detail_scroll_offset(
    state: DashboardState, sub_view: str, filter_query: str, detail_height: int | None = None
) -> int:
    """Effective max scroll offset for the logs detail view."""
    _, sub_view, _, lines = _resolve_log_view(state, sub_view, filter_query)
    extra = 1 if _stream_health(state, sub_view) is not None else 0
    return max(0, len(lines) - _visible_line_count(detail_height, filter_query, extra))


def _stream_health(state: DashboardState, sub_view: str) -> LogStreamHealth | None:
    return next((entry for entry in state.logs.health if entry.stream == sub_view), None)


def log_health_counter_label(counter: LogHealthCounter) -> str:
    """``label 2/1h · 5/24h`` plus the undated backfill count when there is one."""
    label = f"{counter.label} {counter.last_1h}/1h · {counter.last_24h}/24h"
    if counter.undated:
        label += f" (+{counter.undated} undated)"
    return label


def log_health_summary(health: LogStreamHealth) -> str:
    """Every counter of one scanned stream, with the catch-up state."""
    summary = " · ".join(log_health_counter_label(counter) for counter in health.counters)
    if health.backlog_bytes:
        summary += f" (catching up: {health.backlog_bytes:,} bytes left)"
    return summary


def render_logs(
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    sub_view: str = "agent",
    scroll_offset: int = 0,
    filter_query: str = "",
    detail_height: int | None = None,
) -> Panel:
    if detail:
        return _render_detail(state, theme, sub_view, scroll_offset, filter_query, detail_height)
    return _render_compact(state, theme)


def _log_line_text(line: LogLine, theme: Theme) -> Text:
    t = Text()
    if line.timestamp:
        t.append(f"  {sanitize_terminal_text(line.timestamp)} ", style=theme.banner_dim)
    level_colors = {
        "INFO": theme.ui_ok,
        "WARNING": theme.ui_warn,
        "ERROR": theme.ui_error,
    }
    color = level_colors.get(line.level, theme.banner_text)
    if line.component:
        t.append(f"{sanitize_terminal_text(line.component)} ", style=theme.ui_label)
    if line.level:
        t.append(f"{sanitize_terminal_text(line.level):<5} ", style=color)
    if line.session_id:
        t.append(f"[{sanitize_terminal_text(line.session_id)}] ", style=theme.ui_accent)
    t.append(sanitize_terminal_text(line.message), style=theme.banner_text)
    return t


def _compact_lines(state: DashboardState) -> list[LogLine]:
    """Recent agent-stream lines, falling back to the first non-empty stream."""
    log_map = _log_stream_map(state)
    lines = log_map.get("agent") or next(
        (stream_lines for stream_lines in log_map.values() if stream_lines), []
    )
    return lines[-5:]


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    lines = Text()
    recent_lines = _compact_lines(state)
    if not recent_lines:
        lines.append("  No log lines", style=theme.banner_dim)
    for log_line in recent_lines:
        lines.append_text(_log_line_text(log_line, theme))
        lines.append("\n")

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[8] Logs[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(
    state: DashboardState,
    theme: Theme,
    sub_view: str,
    scroll_offset: int,
    filter_query: str,
    detail_height: int | None,
) -> Panel:
    log_map, sub_view, unfiltered_lines, log_lines = _resolve_log_view(
        state, sub_view, filter_query
    )
    health = _stream_health(state, sub_view)
    total = len(log_lines)
    window = _visible_line_count(detail_height, filter_query, 1 if health is not None else 0)
    max_offset = max(0, total - window)
    # Clamp both ends: a negative offset would slice from the end of the list
    # and render an empty page with a negative line counter.
    offset = max(0, min(scroll_offset, max_offset))
    visible_lines = log_lines[offset : offset + window]

    lines = Text()
    tab_bar = Text()
    for name in log_map:
        if name == sub_view:
            tab_bar.append(f" [{name}] ", style=f"bold {theme.ui_accent}")
        else:
            tab_bar.append(f"  {name}  ", style=theme.banner_dim)
    lines.append_text(tab_bar)
    lines.append("\n")
    lines.append(" Scope: ", style=theme.ui_label)
    lines.append(_scope_label(state, sub_view), style=theme.ui_accent)
    if health is not None:
        lines.append("\n")
        lines.append(" Health: ", style=theme.ui_label)
        lines.append(sanitize_terminal_text(log_health_summary(health)), style=theme.banner_text)
    if filter_query:
        lines.append("\n")
        lines.append(" Filter: ", style=theme.ui_label)
        lines.append(sanitize_terminal_text(filter_query), style=theme.ui_accent)
        lines.append(
            f"  ({len(log_lines)}/{len(unfiltered_lines)} matches)",
            style=theme.banner_dim,
        )
    if total:
        visible_end = min(total, offset + len(visible_lines))
        lines.append("\n")
        lines.append(f" [{offset + 1}-{visible_end}/{total}] ", style=theme.ui_label)
        if offset > 0:
            lines.append("↑ ", style=theme.ui_accent)
        if total > 1:
            lines.append("j/k scroll", style=theme.banner_dim)
        lines.append("\n\n")
    else:
        lines.append("\n\n")

    if not visible_lines:
        empty_message = "  No matching log lines" if filter_query else "  No log lines"
        lines.append(empty_message, style=theme.banner_dim)

    for log_line in visible_lines:
        lines.append_text(_log_line_text(log_line, theme))
        lines.append("\n")

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[8] Logs[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _filter_log_lines(log_lines: list[LogLine], filter_query: str) -> list[LogLine]:
    criteria = _parse_log_filter(filter_query)
    if not criteria["terms"] and not criteria["fields"]:
        return log_lines
    return [line for line in log_lines if _log_line_matches(line, criteria)]


def _log_line_matches(line: LogLine, criteria: LogFilterCriteria) -> bool:
    fields = criteria["fields"]
    for field_name, expected_values in fields.items():
        for expected in expected_values:
            value = str(expected).lower()
            if field_name == "level" and value not in line.level.lower():
                return False
            if field_name == "minlevel":
                threshold = _log_level_rank(value)
                if threshold == 0:
                    # Unknown minlevel value: treat the filter as inactive
                    # instead of hiding every line.
                    continue
                if _log_level_rank(line.level) < threshold:
                    return False
            if field_name == "component" and value not in line.component.lower():
                return False
            if field_name == "session" and value not in line.session_id.lower():
                return False

    haystack = (
        f"{line.timestamp} {line.component} {line.level} {line.session_id} {line.message}".lower()
    )
    terms = criteria["terms"]
    return all(term in haystack for term in terms)


def _parse_log_filter(filter_query: str) -> LogFilterCriteria:
    fields: dict[str, list[str]] = {}
    terms: list[str] = []
    for token in filter_query.split():
        if ":" not in token:
            terms.append(token.lower())
            continue
        key, value = token.split(":", 1)
        key = key.lower().strip()
        value = value.strip().lower()
        if key in {"level", "component", "session", "minlevel"}:
            fields.setdefault(key, []).append(value)
        elif key == "text":
            if value:
                terms.append(value)
        else:
            terms.append(token.lower())
    return {"fields": fields, "terms": terms}


def _log_level_rank(level: str) -> int:
    return _LOG_LEVEL_RANK.get(level.lower(), 0)


def _log_stream_map(state: DashboardState) -> dict[str, list[LogLine]]:
    if state.logs.streams:
        return {stream.name: stream.lines for stream in state.logs.streams}
    return {
        "agent": state.logs.agent_lines,
        "gateway": state.logs.gateway_lines,
        "errors": state.logs.error_lines,
        "cron": state.logs.cron_lines,
    }


def _scope_label(state: DashboardState, sub_view: str) -> str:
    """Which Hermes home the selected stream is resolved against.

    Streams of the same base name exist in both scopes (``logs/agent.log`` and
    ``profiles/<name>/logs/agent.log``), and ``LogStream.path`` carries only the
    file name, so the scope is what tells an operator which copy they read.
    See ``.codex/rules/source-ownership.md``.
    """
    for stream in state.logs.streams:
        if stream.name == sub_view:
            return stream.scope.value
    return SourceScope.ROOT.value
