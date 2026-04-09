from __future__ import annotations

from rich.panel import Panel
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_panel(
    panel_num: int,
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    log_sub_view: str = "agent",
    scroll_offset: int = 0,
) -> Panel:
    from hermesd.panels.gateway import render_gateway
    from hermesd.panels.sessions import render_sessions
    from hermesd.panels.tokens import render_tokens
    from hermesd.panels.tools import render_tools
    from hermesd.panels.config_panel import render_config
    from hermesd.panels.cron import render_cron
    from hermesd.panels.overview import render_overview
    from hermesd.panels.logs import render_logs

    renderers = {
        1: render_gateway,
        2: render_sessions,
        3: render_tokens,
        4: render_tools,
        5: render_config,
        6: render_cron,
        7: render_overview,
        8: render_logs,
    }
    renderer = renderers.get(panel_num)
    if renderer:
        if panel_num == 8:
            return renderer(state, theme, detail=detail, sub_view=log_sub_view)
        if panel_num == 7:
            return renderer(state, theme, detail=detail, scroll_offset=scroll_offset)
        return renderer(state, theme, detail=detail)
    return Panel(Text("Unknown panel"), title="?", border_style=theme.panel_border_style)


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
