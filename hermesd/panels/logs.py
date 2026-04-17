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
) -> Panel:
    if detail:
        return _render_detail(state, theme, sub_view, scroll_offset)
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
    if line.level:
        t.append(f"{line.level:<5} ", style=color)
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
) -> Panel:
    log_map = {
        "agent": state.logs.agent_lines,
        "gateway": state.logs.gateway_lines,
        "errors": state.logs.error_lines,
    }
    log_lines = log_map.get(sub_view, state.logs.agent_lines)
    total = len(log_lines)
    offset = min(scroll_offset, max(0, total - 1))
    visible_lines = log_lines[offset:]

    lines = Text()
    tab_bar = Text()
    for name in ("agent", "gateway", "errors"):
        if name == sub_view:
            tab_bar.append(f" [{name}] ", style=f"bold {theme.ui_accent}")
        else:
            tab_bar.append(f"  {name}  ", style=theme.banner_dim)
    lines.append_text(tab_bar)
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
