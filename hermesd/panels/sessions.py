from __future__ import annotations

from typing import TypedDict

import rich.box
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState, SessionInfo
from hermesd.panels.formatting import fmt_tokens, fmt_usd
from hermesd.theme import Theme


class SessionFilterCriteria(TypedDict):
    fields: dict[str, list[str]]
    terms: list[str]


_EXACT_SESSION_FILTER_FIELDS = {
    "id",
    "source",
    "parent",
    "provider",
    "status",
    "pricing",
    "archived",
    "handoff",
}
_ACTIVE_TRUE_VALUES = {"1", "true", "yes", "active"}
_ACTIVE_FALSE_VALUES = {"0", "false", "no", "inactive"}


def render_sessions(
    state: DashboardState,
    theme: Theme,
    detail: bool = False,
    filter_query: str = "",
    session_sort: str = "recent",
    message_match_ids: set[str] | None = None,
) -> Panel:
    if detail:
        return _render_detail(state, theme, filter_query, session_sort, message_match_ids)
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
    message_match_ids: set[str] | None,
) -> Panel:
    sessions = _sort_sessions(
        _filter_sessions(state.sessions, filter_query, message_match_ids), session_sort
    )
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
            escape(s.source),
            escape(s.model) if s.model else "—",
            escape(s.parent_session_id[-8:]) if s.parent_session_id else "—",
            escape(s.billing_provider) if s.billing_provider else "—",
            escape(s.cost_status) if s.cost_status else "—",
            escape(s.pricing_version) if s.pricing_version else "—",
            str(s.message_count),
            str(s.tool_call_count),
            fmt_tokens(s.input_tokens),
            fmt_tokens(s.output_tokens),
            fmt_usd(s.estimated_cost_usd),
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

    sections = [
        header,
        table if sessions else Text("  No matching sessions\n", style=theme.banner_dim),
    ]
    runtime_table = _runtime_table(sessions, theme)
    if runtime_table is not None:
        sections.append(Text("\nRuntime\n", style=f"bold {theme.ui_label}"))
        sections.append(runtime_table)

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[2] Sessions[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _filter_sessions(
    sessions: list[SessionInfo],
    filter_query: str,
    message_match_ids: set[str] | None = None,
) -> list[SessionInfo]:
    criteria = _parse_session_filter(filter_query)
    if not criteria["terms"] and not criteria["fields"]:
        return sessions
    return [
        session
        for session in sessions
        if _session_matches(session, criteria, message_match_ids or set())
    ]


def _session_matches(
    session: SessionInfo,
    criteria: SessionFilterCriteria,
    message_match_ids: set[str],
) -> bool:
    fields = criteria["fields"]
    for field_name, expected_values in fields.items():
        for expected in expected_values:
            if not _match_session_field(session, field_name, expected, message_match_ids):
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
            session.cwd,
            session.handoff_state,
            session.handoff_platform,
            session.handoff_error,
            session.title or "",
        ]
    ).lower()
    terms = criteria["terms"]
    return all(term in haystack for term in terms)


def _match_session_field(
    session: SessionInfo,
    field_name: str,
    expected: object,
    message_match_ids: set[str],
) -> bool:
    value = str(expected).lower()
    if field_name == "active":
        if value not in _ACTIVE_TRUE_VALUES | _ACTIVE_FALSE_VALUES:
            return False
        is_active = value in _ACTIVE_TRUE_VALUES
        return session.is_active is is_active
    if field_name == "message":
        return session.session_id in message_match_ids
    if field_name == "archived":
        if value not in _ACTIVE_TRUE_VALUES | _ACTIVE_FALSE_VALUES:
            return False
        expected_archived = value in _ACTIVE_TRUE_VALUES
        return session.archived is expected_archived
    field_map = {
        "id": session.session_id,
        "source": session.source,
        "model": session.model,
        "parent": session.parent_session_id,
        "provider": session.billing_provider,
        "status": session.cost_status,
        "pricing": session.pricing_version,
        "cwd": session.cwd,
        "handoff": session.handoff_state,
        "platform": session.handoff_platform,
        "title": session.title or "",
    }
    actual = field_map.get(field_name, "").lower()
    if field_name in _EXACT_SESSION_FILTER_FIELDS:
        return actual == value
    return value in actual


def _parse_session_filter(filter_query: str) -> SessionFilterCriteria:
    fields: dict[str, list[str]] = {}
    terms: list[str] = []
    for token in filter_query.split():
        if ":" not in token:
            terms.append(token.lower())
            continue
        key, value = token.split(":", 1)
        key = key.lower().strip()
        value = value.strip().lower()
        if key in {"message", "msg"}:
            # With repeated message:/msg: tokens the last occurrence wins
            # everywhere, mirroring extract_message_search_query (which feeds
            # the message-search worker with the same final value).
            fields["message"] = [value]
        elif key in {
            "id",
            "source",
            "model",
            "parent",
            "provider",
            "status",
            "pricing",
            "title",
            "cwd",
            "archived",
            "handoff",
            "platform",
            "active",
        }:
            fields.setdefault(key, []).append(value)
        elif key == "text":
            if value:
                terms.append(value)
        else:
            terms.append(token.lower())
    return {"fields": fields, "terms": terms}


def extract_message_search_query(filter_query: str) -> str:
    latest_value = ""
    for token in filter_query.split():
        if ":" not in token:
            continue
        key, raw_value = token.split(":", 1)
        if key.lower().strip() in {"message", "msg"} and raw_value.strip():
            latest_value = raw_value.strip()
    return latest_value


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
    return sorted(
        sessions,
        key=lambda session: (session.started_at, session.session_id),
        reverse=True,
    )


def _cwd_label(cwd: str) -> str:
    if not cwd:
        return "—"
    return cwd.rstrip("/").split("/")[-1] or cwd


def _runtime_table(sessions: list[SessionInfo], theme: Theme) -> Table | None:
    runtime_sessions = [
        session
        for session in sessions[:10]
        if session.api_call_count
        or session.cwd
        or session.archived
        or session.rewind_count
        or session.handoff_state
        or session.handoff_platform
        or session.handoff_error
    ]
    if not runtime_sessions:
        return None
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("ID", style=theme.session_label)
    table.add_column("API", justify="right", style=theme.ui_accent)
    table.add_column("CWD", style=theme.banner_text)
    table.add_column("Flags", style=theme.banner_dim)
    table.add_column("Handoff", style=theme.banner_text)
    for session in runtime_sessions:
        flags = []
        if session.archived:
            flags.append("archived")
        if session.rewind_count:
            flags.append(f"rewind:{session.rewind_count}")
        handoff = " ".join(
            part
            for part in [
                session.handoff_state,
                session.handoff_platform,
                session.handoff_error[:40],
            ]
            if part
        )
        table.add_row(
            escape(session.session_id[-8:]),
            str(session.api_call_count),
            escape(_cwd_label(session.cwd)),
            ", ".join(flags) if flags else "—",
            escape(handoff) if handoff else "—",
        )
    return table
