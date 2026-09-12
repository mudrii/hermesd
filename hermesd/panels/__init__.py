from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rich.panel import Panel
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


@dataclass(frozen=True, slots=True)
class PanelRenderContext:
    state: DashboardState
    theme: Theme
    detail: bool = False
    log_sub_view: str = "agent"
    scroll_offset: int = 0
    profile_view_index: int = 0
    filter_query: str = ""
    session_sort: str = "recent"
    session_message_match_ids: set[str] | None = None


PanelRenderer = Callable[[PanelRenderContext], Panel]


def _render_gateway_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.gateway import render_gateway

    return render_gateway(ctx.state, ctx.theme, detail=ctx.detail)


def _render_sessions_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.sessions import render_sessions

    return render_sessions(
        ctx.state,
        ctx.theme,
        detail=ctx.detail,
        filter_query=ctx.filter_query,
        session_sort=ctx.session_sort,
        message_match_ids=ctx.session_message_match_ids,
    )


def _render_tokens_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.tokens import render_tokens

    return render_tokens(ctx.state, ctx.theme, detail=ctx.detail)


def _render_tools_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.tools import render_tools

    return render_tools(ctx.state, ctx.theme, detail=ctx.detail)


def _render_config_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.config_panel import render_config

    return render_config(ctx.state, ctx.theme, detail=ctx.detail)


def _render_cron_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.cron import render_cron

    return render_cron(ctx.state, ctx.theme, detail=ctx.detail)


def _render_skills_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.overview import render_overview

    return render_overview(
        ctx.state,
        ctx.theme,
        detail=ctx.detail,
        scroll_offset=ctx.scroll_offset,
    )


def _render_logs_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.logs import render_logs

    return render_logs(
        ctx.state,
        ctx.theme,
        detail=ctx.detail,
        sub_view=ctx.log_sub_view,
        scroll_offset=ctx.scroll_offset,
        filter_query=ctx.filter_query,
    )


def _render_profiles_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.profiles import render_profiles

    return render_profiles(
        ctx.state,
        ctx.theme,
        detail=ctx.detail,
        profile_view_index=ctx.profile_view_index,
    )


def _render_memory_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.memory_panel import render_memory

    return render_memory(ctx.state, ctx.theme, detail=ctx.detail)


def _render_kanban_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.kanban import render_kanban

    return render_kanban(ctx.state, ctx.theme, detail=ctx.detail)


def _render_operations_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.operations import render_operations

    return render_operations(ctx.state, ctx.theme, detail=ctx.detail)


def _render_curator_panel(ctx: PanelRenderContext) -> Panel:
    from hermesd.panels.curator_panel import render_curator

    return render_curator(ctx.state, ctx.theme, detail=ctx.detail)


PANEL_NAMES = {
    1: "Gateway & Platforms",
    2: "Sessions",
    3: "Tokens / Cost",
    4: "Tools",
    5: "Config",
    6: "Cron",
    7: "Skills / Integrations",
    8: "Logs",
    9: "Profiles",
    10: "Memory",
    11: "Kanban",
    12: "Operations",
    13: "Curator",
}

# Deferred imports keep panel modules unloaded until they are actually rendered.
_RENDERERS: dict[int, PanelRenderer] = {
    1: _render_gateway_panel,
    2: _render_sessions_panel,
    3: _render_tokens_panel,
    4: _render_tools_panel,
    5: _render_config_panel,
    6: _render_cron_panel,
    7: _render_skills_panel,
    8: _render_logs_panel,
    9: _render_profiles_panel,
    10: _render_memory_panel,
    11: _render_kanban_panel,
    12: _render_operations_panel,
    13: _render_curator_panel,
}


def render_panel(
    panel_num: int,
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    log_sub_view: str = "agent",
    scroll_offset: int = 0,
    profile_view_index: int = 0,
    filter_query: str = "",
    session_sort: str = "recent",
    session_message_match_ids: set[str] | None = None,
) -> Panel:
    renderer = _RENDERERS.get(panel_num)
    if renderer is None:
        return Panel(Text("Unknown panel"), title="?", border_style=theme.panel_border_style)
    context = PanelRenderContext(
        state=state,
        theme=theme,
        detail=detail,
        log_sub_view=log_sub_view,
        scroll_offset=scroll_offset,
        profile_view_index=profile_view_index,
        filter_query=filter_query,
        session_sort=session_sort,
        session_message_match_ids=session_message_match_ids,
    )
    return renderer(context)
