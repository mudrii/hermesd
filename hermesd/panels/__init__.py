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
    from hermesd.panels.config_panel import render_config
    from hermesd.panels.cron import render_cron
    from hermesd.panels.gateway import render_gateway
    from hermesd.panels.logs import render_logs
    from hermesd.panels.overview import render_overview
    from hermesd.panels.sessions import render_sessions
    from hermesd.panels.tokens import render_tokens
    from hermesd.panels.tools import render_tools

    if panel_num == 1:
        return render_gateway(state, theme, detail=detail)
    if panel_num == 2:
        return render_sessions(state, theme, detail=detail)
    if panel_num == 3:
        return render_tokens(state, theme, detail=detail)
    if panel_num == 4:
        return render_tools(state, theme, detail=detail)
    if panel_num == 5:
        return render_config(state, theme, detail=detail)
    if panel_num == 6:
        return render_cron(state, theme, detail=detail)
    if panel_num == 7:
        return render_overview(state, theme, detail=detail, scroll_offset=scroll_offset)
    if panel_num == 8:
        return render_logs(state, theme, detail=detail, sub_view=log_sub_view)
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
