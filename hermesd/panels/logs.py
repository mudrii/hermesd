from __future__ import annotations

import rich.box
from rich.panel import Panel
from rich.text import Text

from hermesd.models import DashboardState, LogLine
from hermesd.theme import Theme


def render_logs(
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    sub_view: str = "agent",
    scroll_offset: int = 0,
    filter_query: str = "",
) -> Panel:
    if detail:
        return _render_detail(state, theme, sub_view, scroll_offset, filter_query)
    return _render_compact(state, theme)


def _log_line_text(line: LogLine, theme: Theme) -> Text:
    t = Text()
    if line.timestamp:
        t.append(f"  {line.timestamp} ", style=theme.banner_dim)
    level_colors = {
        "INFO": theme.ui_ok,
        "WARNING": theme.ui_warn,
        "ERROR": theme.ui_error,
    }
    color = level_colors.get(line.level, theme.banner_text)
    if line.component:
        t.append(f"{line.component} ", style=theme.ui_label)
    if line.level:
        t.append(f"{line.level:<5} ", style=color)
    if line.session_id:
        t.append(f"[{line.session_id}] ", style=theme.ui_accent)
    t.append(line.message, style=theme.banner_text)
    return t


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    lines = Text()
    recent_lines = state.logs.agent_lines[-5:]
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
) -> Panel:
    log_map = {
        "agent": state.logs.agent_lines,
        "gateway": state.logs.gateway_lines,
        "errors": state.logs.error_lines,
        "cron": state.logs.cron_lines,
    }
    unfiltered_lines = log_map.get(sub_view, state.logs.agent_lines)
    log_lines = _filter_log_lines(unfiltered_lines, filter_query)
    total = len(log_lines)
    offset = min(scroll_offset, max(0, total - 1))
    visible_lines = log_lines[offset:]

    lines = Text()
    tab_bar = Text()
    for name in ("agent", "gateway", "errors", "cron"):
        if name == sub_view:
            tab_bar.append(f" [{name}] ", style=f"bold {theme.ui_accent}")
        else:
            tab_bar.append(f"  {name}  ", style=theme.banner_dim)
    lines.append_text(tab_bar)
    if filter_query:
        lines.append("\n")
        lines.append(" Filter: ", style=theme.ui_label)
        lines.append(filter_query, style=theme.ui_accent)
        lines.append(
            f"  ({len(log_lines)}/{len(unfiltered_lines)} matches)",
            style=theme.banner_dim,
        )
    if total:
        lines.append("\n")
        lines.append(f" [{offset + 1}-{total}/{total}] ", style=theme.ui_label)
        if offset > 0:
            lines.append("↑ ", style=theme.ui_accent)
        if total > 1:
            lines.append("j/k scroll", style=theme.banner_dim)
        lines.append("\n\n")
    else:
        lines.append("\n\n")

    if not visible_lines:
        lines.append("  No log lines", style=theme.banner_dim)

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


def _log_line_matches(line: LogLine, criteria: dict[str, object]) -> bool:
    fields = criteria["fields"]
    assert isinstance(fields, dict)
    for field_name, expected in fields.items():
        value = str(expected).lower()
        if field_name == "level" and value not in line.level.lower():
            return False
        if field_name == "component" and value not in line.component.lower():
            return False
        if field_name == "session" and value not in line.session_id.lower():
            return False

    haystack = (
        f"{line.timestamp} {line.component} {line.level} {line.session_id} {line.message}".lower()
    )
    terms = criteria["terms"]
    assert isinstance(terms, list)
    return all(term in haystack for term in terms)


def _parse_log_filter(filter_query: str) -> dict[str, object]:
    fields: dict[str, str] = {}
    terms: list[str] = []
    for token in filter_query.split():
        if ":" not in token:
            terms.append(token.lower())
            continue
        key, value = token.split(":", 1)
        key = key.lower().strip()
        value = value.strip().lower()
        if key in {"level", "component", "session"}:
            fields[key] = value
        elif key == "text":
            if value:
                terms.append(value)
        else:
            terms.append(token.lower())
    return {"fields": fields, "terms": terms}
