from __future__ import annotations

import rich.box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState, SessionInfo
from hermesd.panels.formatting import fmt_tokens
from hermesd.theme import Theme


def render_sessions(
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    filter_query: str = "",
    session_sort: str = "recent",
) -> Panel:
    if detail:
        return _render_detail(state, theme, filter_query, session_sort)
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


def _render_detail(
    state: DashboardState,
    theme: Theme,
    filter_query: str,
    session_sort: str,
) -> Panel:
    sessions = _sort_sessions(_filter_sessions(state.sessions, filter_query), session_sort)
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("ID", style=theme.session_label)
    table.add_column("Source", style=theme.ui_label)
    table.add_column("Model", style=theme.banner_text)
    table.add_column("Parent", style=theme.banner_dim)
    table.add_column("Provider", style=theme.banner_text)
    table.add_column("Cost Status", style=theme.banner_dim)
    table.add_column("Pricing", style=theme.banner_dim)
    table.add_column("Msgs", justify="right", style=theme.ui_accent)
    table.add_column("Tools", justify="right", style=theme.ui_accent)
    table.add_column("In Tok", justify="right", style=theme.banner_text)
    table.add_column("Out Tok", justify="right", style=theme.banner_text)
    table.add_column("Cost", justify="right", style=theme.ui_accent)

    for s in sessions:
        active = Text("● ", style=f"bold {theme.ui_ok}") if s.is_active else Text("  ")
        sid = Text()
        sid.append_text(active)
        sid.append(s.session_id[-8:])
        table.add_row(
            sid,
            s.source,
            s.model or "—",
            s.parent_session_id[-8:] if s.parent_session_id else "—",
            s.billing_provider or "—",
            s.cost_status or "—",
            s.pricing_version or "—",
            str(s.message_count),
            str(s.tool_call_count),
            fmt_tokens(s.input_tokens),
            fmt_tokens(s.output_tokens),
            f"${s.estimated_cost_usd:.2f}",
        )

    header = Text()
    if filter_query:
        header.append("Filter: ", style=theme.ui_label)
        header.append(filter_query, style=theme.ui_accent)
        header.append(f"  ({len(sessions)}/{len(state.sessions)} matches)", style=theme.banner_dim)
    if filter_query or session_sort != "recent":
        header.append("  ", style=theme.banner_dim)
    if session_sort != "recent":
        header.append("Sort: ", style=theme.ui_label)
        header.append(session_sort, style=theme.ui_accent)
    if filter_query or session_sort != "recent":
        header.append("\n\n", style=theme.banner_dim)

    return Panel(
        Group(
            header,
            table if sessions else Text("  No matching sessions\n", style=theme.banner_dim),
        ),
        title=f"[{theme.panel_title_style}]\\[2] Sessions[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _filter_sessions(sessions: list[SessionInfo], filter_query: str) -> list[SessionInfo]:
    criteria = _parse_session_filter(filter_query)
    if not criteria["terms"] and not criteria["fields"]:
        return sessions
    return [session for session in sessions if _session_matches(session, criteria)]


def _session_matches(session: SessionInfo, criteria: dict[str, object]) -> bool:
    fields = criteria["fields"]
    assert isinstance(fields, dict)
    for field_name, expected in fields.items():
        if not _match_session_field(session, field_name, expected):
            return False

    haystack = " ".join(
        [
            session.session_id,
            session.source,
            session.model,
            session.parent_session_id,
            session.billing_provider,
            session.cost_status,
            session.pricing_version,
            session.title or "",
        ]
    ).lower()
    terms = criteria["terms"]
    assert isinstance(terms, list)
    return all(term in haystack for term in terms)


def _match_session_field(session: SessionInfo, field_name: str, expected: object) -> bool:
    value = str(expected).lower()
    if field_name == "active":
        is_active = value in {"1", "true", "yes", "active"}
        return session.is_active is is_active
    field_map = {
        "id": session.session_id,
        "source": session.source,
        "model": session.model,
        "parent": session.parent_session_id,
        "provider": session.billing_provider,
        "status": session.cost_status,
        "pricing": session.pricing_version,
        "title": session.title or "",
    }
    return value in field_map.get(field_name, "").lower()


def _parse_session_filter(filter_query: str) -> dict[str, object]:
    fields: dict[str, str] = {}
    terms: list[str] = []
    for token in filter_query.split():
        if ":" not in token:
            terms.append(token.lower())
            continue
        key, value = token.split(":", 1)
        key = key.lower().strip()
        value = value.strip().lower()
        if key in {
            "id",
            "source",
            "model",
            "parent",
            "provider",
            "status",
            "pricing",
            "title",
            "active",
        }:
            fields[key] = value
        elif key == "text":
            if value:
                terms.append(value)
        else:
            terms.append(token.lower())
    return {"fields": fields, "terms": terms}


def _sort_sessions(sessions: list[SessionInfo], session_sort: str) -> list[SessionInfo]:
    if session_sort == "cost":
        return sorted(
            sessions,
            key=lambda session: (
                session.estimated_cost_usd,
                session.started_at,
                session.session_id,
            ),
            reverse=True,
        )
    if session_sort == "tokens":
        return sorted(
            sessions,
            key=lambda session: (
                session.input_tokens
                + session.output_tokens
                + session.cache_read_tokens
                + session.reasoning_tokens,
                session.started_at,
                session.session_id,
            ),
            reverse=True,
        )
    return sorted(sessions, key=lambda session: session.started_at, reverse=True)
