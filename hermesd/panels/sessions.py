from __future__ import annotations

import rich.box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState
from hermesd.theme import Theme


def render_sessions(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    active = [s for s in state.sessions if s.is_active]
    total_msgs = sum(s.message_count for s in state.sessions)
    total_tc = sum(s.tool_call_count for s in state.sessions)
    lines = Text()
    lines.append(f"  {len(active)} active", style=f"bold {theme.ui_ok}")
    lines.append(f" / {len(state.sessions)} total", style=theme.banner_dim)
    lines.append(f"   {total_msgs} msgs  {total_tc} tools\n", style=theme.banner_text)
    for s in state.sessions[:4]:
        sid_short = s.session_id[-6:] if len(s.session_id) > 6 else s.session_id
        lines.append(f"  #{sid_short}", style=theme.session_label)
        lines.append(f" {s.source:<4}", style=theme.ui_label)
        if s.is_active:
            lines.append(" ● ", style=f"bold {theme.ui_ok}")
        else:
            lines.append("   ")
        lines.append(f"{s.message_count:>3}m", style=theme.banner_text)
        lines.append("\n")

    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[2] Sessions[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("ID", style=theme.session_label)
    table.add_column("Source", style=theme.ui_label)
    table.add_column("Model", style=theme.banner_text)
    table.add_column("Msgs", justify="right", style=theme.ui_accent)
    table.add_column("Tools", justify="right", style=theme.ui_accent)
    table.add_column("In Tok", justify="right", style=theme.banner_text)
    table.add_column("Out Tok", justify="right", style=theme.banner_text)
    table.add_column("Cost", justify="right", style=theme.ui_accent)

    for s in state.sessions:
        active = Text("● ", style=f"bold {theme.ui_ok}") if s.is_active else Text("  ")
        sid = Text()
        sid.append_text(active)
        sid.append(s.session_id[-8:])
        table.add_row(
            sid, s.source, s.model or "—",
            str(s.message_count), str(s.tool_call_count),
            _fmt_tokens(s.input_tokens), _fmt_tokens(s.output_tokens),
            f"${s.estimated_cost_usd:.2f}",
        )

    return Panel(
        table,
        title=f"[{theme.panel_title_style}]\\[2] Sessions[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
