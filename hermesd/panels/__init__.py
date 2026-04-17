from __future__ import annotations

from collections.abc import Callable

from rich.panel import Panel
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme

PanelRenderer = Callable[[DashboardState, Theme, bool, str, int], Panel]


def _render_gateway_panel(
    state: DashboardState,
    theme: Theme,
    detail: bool,
    log_sub_view: str,
    scroll_offset: int,
) -> Panel:
    from hermesd.panels.gateway import render_gateway

    return render_gateway(state, theme, detail=detail)


def _render_sessions_panel(
    state: DashboardState,
    theme: Theme,
    detail: bool,
    log_sub_view: str,
    scroll_offset: int,
) -> Panel:
    from hermesd.panels.sessions import render_sessions

    return render_sessions(state, theme, detail=detail)


def _render_tokens_panel(
    state: DashboardState,
    theme: Theme,
    detail: bool,
    log_sub_view: str,
    scroll_offset: int,
) -> Panel:
    from hermesd.panels.tokens import render_tokens

    return render_tokens(state, theme, detail=detail)


def _render_tools_panel(
    state: DashboardState,
    theme: Theme,
    detail: bool,
    log_sub_view: str,
    scroll_offset: int,
) -> Panel:
    from hermesd.panels.tools import render_tools

    return render_tools(state, theme, detail=detail)


def _render_config_panel(
    state: DashboardState,
    theme: Theme,
    detail: bool,
    log_sub_view: str,
    scroll_offset: int,
) -> Panel:
    from hermesd.panels.config_panel import render_config

    return render_config(state, theme, detail=detail)


def _render_cron_panel(
    state: DashboardState,
    theme: Theme,
    detail: bool,
    log_sub_view: str,
    scroll_offset: int,
) -> Panel:
    from hermesd.panels.cron import render_cron

    return render_cron(state, theme, detail=detail)


def _render_overview_panel(
    state: DashboardState,
    theme: Theme,
    detail: bool,
    log_sub_view: str,
    scroll_offset: int,
) -> Panel:
    from hermesd.panels.overview import render_overview

    return render_overview(state, theme, detail=detail, scroll_offset=scroll_offset)


def _render_logs_panel(
    state: DashboardState,
    theme: Theme,
    detail: bool,
    log_sub_view: str,
    scroll_offset: int,
) -> Panel:
    from hermesd.panels.logs import render_logs

    return render_logs(
        state,
        theme,
        detail=detail,
        sub_view=log_sub_view,
        scroll_offset=scroll_offset,
    )


PANEL_NAMES = {
    1: "Gateway & Platforms",
    2: "Sessions",
    3: "Tokens / Cost",
    4: "Tools",
    5: "Config",
    6: "Cron",
    7: "Skills / Providers",
    8: "Logs",
}

# Deferred imports keep panel modules unloaded until they are actually rendered.
_RENDERERS: dict[int, PanelRenderer] = {
    1: _render_gateway_panel,
    2: _render_sessions_panel,
    3: _render_tokens_panel,
    4: _render_tools_panel,
    5: _render_config_panel,
    6: _render_cron_panel,
    7: _render_overview_panel,
    8: _render_logs_panel,
}


def render_panel(
    panel_num: int,
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    log_sub_view: str = "agent",
    scroll_offset: int = 0,
) -> Panel:
    renderer = _RENDERERS.get(panel_num)
    if renderer is None:
        return Panel(Text("Unknown panel"), title="?", border_style=theme.panel_border_style)
    return renderer(state, theme, detail, log_sub_view, scroll_offset)
