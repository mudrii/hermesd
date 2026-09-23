from __future__ import annotations

from typing import TypedDict

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import (
    ActiveSurface,
    DashboardState,
    GatewayHygieneState,
    GatewayRouteState,
    ProcessLiveness,
    SessionCoordinationState,
    SessionInfo,
    SessionLease,
    SessionLeaseKind,
    TerminalSessionReadout,
    UsageAnalytics,
)
from hermesd.panels.formatting import (
    IdentityMemo,
    fmt_age_seconds,
    fmt_tokens,
    fmt_usd,
    sanitize_terminal_text,
    section_heading,
)
from hermesd.panels.formatting import (
    escape_terminal_text as escape,
)
from hermesd.panels.tokens import sparkline
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
_DETAIL_MAX_SESSION_ROWS = 50
_DETAIL_MAX_SURFACE_ROWS = 20
# Display bound on an untrusted lease id: upstream mints a 32-hex uuid4, and the
# panel only needs enough of it to grep the registry with.
_LEASE_ID_CHARS = 8
_PIN_MARKER = "📌"
_MAX_NAME_CHARS = 30
_MAX_BRANCH_CHARS = 24
_MAX_ACTIVITY_CHARS = 40
_MAX_ERROR_CHARS = 60
# Bound on the sessions examined for compression warnings, matching the other
# per-section row caps in this panel.
_COMPRESSION_WARNING_ROWS = 10
# Coordination-section bounds. Coordination keys are rotation-stable chat keys
# (e.g. "telegram:<chat>:<thread>") — shortened like session ids, never shown
# whole for a stranger's chat.
_COORDINATION_KEY_CHARS = 14
_MAX_COORDINATION_ERROR_CHARS = 44
_MAX_ROUTE_FLAGS = 4
_RESET_CHURN_ROWS = 5
_TERMINAL_ROWS = 8

# Rendered under the Live Surfaces section: the numbers beside it are a lease
# count, a verified-activity count and a pid count, and reading any one of them
# as another is the mistake worth pre-empting.
_SURFACE_CAPACITY_NOTE = (
    "Leases, not processes: entries can share a pid, and only an "
    "identity-verified lease counts as executing."
)

# The filter+sort result is memoized on input identity between collects.
# Message-search results arrive as a new set object, so identity tracks
# content there too.
_detail_sessions_memo: IdentityMemo[list[SessionInfo]] = IdentityMemo()


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
    if state.active_surface_count > 0:
        # The registry count is entries, not running turns: only an identity check
        # makes a surface live, and an unverifiable one is reported as such.
        lines.append(f"  {state.active_surface_count} surface(s)", style=f"bold {theme.ui_accent}")
        live = sum(1 for s in state.active_surfaces if s.liveness is ProcessLiveness.LIVE)
        unverified = sum(
            1 for s in state.active_surfaces if s.liveness is ProcessLiveness.UNVERIFIABLE
        )
        if live:
            lines.append(f" {live} live", style=f"bold {theme.ui_ok}")
        if unverified:
            lines.append(f" {unverified} unverified", style=theme.ui_warn)
        joinable = sum(1 for s in state.active_surfaces if s.joinable)
        if joinable:
            lines.append(f" {joinable} joinable", style=f"bold {theme.ui_ok}")
        if state.active_surfaces_truncated:
            lines.append(f"  first {len(state.active_surfaces)} retained", style=theme.banner_dim)
        # Capacity is a third number, not a restatement of either above: the
        # configured cross-process lease cap. `max_live_sessions` caps a different
        # resource (the gateway's in-memory sessions) and is never rendered here.
        cap = state.config.max_concurrent_sessions
        if cap is None:
            lines.append("  no cap", style=theme.banner_dim)
        elif state.active_surface_count >= cap:
            lines.append(f"  cap {cap}", style=f"bold {theme.ui_warn}")
        else:
            lines.append(f"  cap {cap}", style=theme.banner_text)
    recovering = sum(1 for s in state.sessions if s.compression_recovery_active(state.collected_at))
    if recovering:
        # A live cooldown or an armed anti-thrash deadline: the compressor is
        # backing off on these sessions right now.
        lines.append(f"  ⚠ {recovering} compression recovery", style=f"bold {theme.ui_warn}")
    coord = state.session_coordination
    if coord.lease_total:
        # Exact totals; the expired/orphaned flags come from the retained rows
        # (capped upstream of the panel) and under-report only on a database
        # already large enough to scroll.
        lines.append(f"  {coord.lease_total} lease(s)", style=f"bold {theme.ui_accent}")
        expired = sum(1 for lease in coord.leases if lease.expired)
        orphaned = sum(1 for lease in coord.leases if lease.orphaned)
        if expired:
            # Qualified like the detail row: expiry alone is benign upstream.
            lines.append(f" {expired} expired (holder may revive)", style=theme.ui_warn)
        if orphaned:
            lines.append(f" {orphaned} orphaned", style=f"bold {theme.ui_error}")
    if coord.hygiene:
        # Per-chat session-hygiene failure streaks: compaction backing off. The
        # model's own rule: report the exact total, never the capped row count.
        lines.append(
            f"  ⚠ {coord.hygiene_total} hygiene cooldown(s)", style=f"bold {theme.ui_warn}"
        )
        suspended = sum(1 for row in coord.hygiene if row.suspended)
        if suspended:
            lines.append(f" · {suspended} compaction off", style=f"bold {theme.ui_error}")
    if coord.generation_count_shrank:
        # The never-prune invariant broke. Compact has no Reset Churn head, so
        # the flag rides its own marker rather than staying detail-only.
        lines.append("  ⚠ reset churn: generations table shrank", style=f"bold {theme.ui_error}")
    if state.terminal_sessions.count:
        lines.append(f"  {state.terminal_sessions.count} cli tty", style=theme.banner_dim)
    lines.append(f"   {total_msgs} msgs  {total_tc} tools\n", style=theme.banner_text)
    for s in state.sessions[:4]:
        sid_short = s.session_id[-6:] if len(s.session_id) > 6 else s.session_id
        lines.append(f"  #{sanitize_terminal_text(sid_short)}", style=theme.session_label)
        lines.append(f" {sanitize_terminal_text(s.source):<4}", style=theme.ui_label)
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
    sessions = _filtered_sorted_sessions(
        state.sessions, filter_query, session_sort, message_match_ids
    )

    sections: list[RenderableType] = [
        _detail_header(state, theme, sessions, filter_query, session_sort)
    ]
    activity_table = _activity_table(sessions, theme, now=state.collected_at)
    if activity_table is not None:
        sections.append(section_heading("Activity", theme, leading_blank=False))
        sections.append(activity_table)
    surfaces = _visible_surfaces(state, sessions, filter_query)
    surfaces_table = _surfaces_table(surfaces, theme)
    if state.active_surface_count or state.config.active_session_cap_configured:
        sections.append(section_heading("Live Surfaces", theme))
        sections.append(_capacity_line(state, surfaces, theme))
        if surfaces_table is not None:
            sections.append(surfaces_table)
        sections.append(Text(f"  {_SURFACE_CAPACITY_NOTE}", style=theme.banner_dim))
        if state.active_surfaces_truncated:
            sections.append(
                Text(
                    "  Registry rows were truncated; capacity uses the full registry count.",
                    style=theme.banner_dim,
                )
            )
    warnings = _compression_warnings(sessions, theme, now=state.collected_at)
    if warnings is not None:
        sections.append(section_heading("Warnings", theme))
        sections.append(warnings)
    sections.extend(_coordination_sections(state, theme))
    runtime_table = _runtime_table(sessions, theme)
    if runtime_table is not None:
        sections.append(section_heading("Runtime", theme, leading_blank=len(sections) > 1))
        sections.append(runtime_table)
    billing_table = _billing_table(sessions, theme)
    if billing_table is not None:
        sections.append(section_heading("Billing & Context", theme))
        sections.append(billing_table)
    sections.extend(_usage_pattern_sections(state.usage_analytics, theme))
    sections.append(section_heading("Sessions", theme))
    sections.append(
        _sessions_table(sessions[:_DETAIL_MAX_SESSION_ROWS], theme)
        if sessions
        else Text("  No matching sessions\n", style=theme.banner_dim)
    )
    if len(sessions) > _DETAIL_MAX_SESSION_ROWS:
        sections.append(
            Text(
                f"  … and {len(sessions) - _DETAIL_MAX_SESSION_ROWS} more\n",
                style=theme.banner_dim,
            )
        )

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[2] Sessions[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _detail_header(
    state: DashboardState,
    theme: Theme,
    sessions: list[SessionInfo],
    filter_query: str,
    session_sort: str,
) -> Text:
    header = Text()
    if filter_query:
        header.append("Filter: ", style=theme.ui_label)
        header.append(sanitize_terminal_text(filter_query), style=theme.ui_accent)
        header.append(f"  ({len(sessions)}/{len(state.sessions)} matches)", style=theme.banner_dim)
    if filter_query or session_sort != "recent":
        header.append("  ", style=theme.banner_dim)
    if session_sort != "recent":
        header.append("Sort: ", style=theme.ui_label)
        header.append(sanitize_terminal_text(session_sort), style=theme.ui_accent)
    if filter_query or session_sort != "recent":
        header.append("\n\n", style=theme.banner_dim)
    return header


def _sessions_table(sessions: list[SessionInfo], theme: Theme) -> Table:
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
        if s.pinned:
            sid.append(f"{_PIN_MARKER} ", style=theme.ui_accent)
        sid.append(sanitize_terminal_text(s.session_id[-8:]))
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
            fmt_usd(_display_cost(s)),
        )
    return table


def _filtered_sorted_sessions(
    sessions: list[SessionInfo],
    filter_query: str,
    session_sort: str,
    message_match_ids: set[str] | None,
) -> list[SessionInfo]:
    return _detail_sessions_memo.get(
        (sessions, filter_query, session_sort, message_match_ids),
        lambda: _sort_sessions(
            _filter_sessions(sessions, filter_query, message_match_ids), session_sort
        ),
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
            # With repeated message:/msg: tokens the last non-empty occurrence
            # wins everywhere, mirroring extract_message_search_query (which
            # feeds the message-search worker with the same final value). An
            # empty value is an unfinished filter, not "match nothing".
            if value:
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
                _display_cost(session),
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
                + session.cache_write_tokens
                + session.reasoning_tokens,
                session.started_at,
                session.session_id,
            ),
            reverse=True,
        )
    return sorted(
        sessions,
        key=lambda session: (_activity_at(session), session.session_id),
        reverse=True,
    )


def _cwd_label(cwd: str) -> str:
    if not cwd:
        return "—"
    return cwd.rstrip("/").split("/")[-1] or cwd


def _session_display_name(session: SessionInfo) -> str:
    """The 0.21 display_name when set, else the stored title."""
    return session.display_name or session.title or ""


def _display_cost(session: SessionInfo) -> float:
    """Provider-billed cost when known, otherwise the resolved estimate."""
    if session.actual_cost_usd > 0:
        return session.actual_cost_usd
    return session.estimated_cost_usd


def _activity_at(session: SessionInfo) -> float:
    return session.last_activity_at or session.started_at


def _age_label(timestamp: float, now: float) -> str:
    if timestamp <= 0:
        return "—"
    return fmt_age_seconds(now - timestamp)


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _activity_table(sessions: list[SessionInfo], theme: Theme, *, now: float) -> Table | None:
    """Names, branches, profiles and activity ages — hermes-agent 0.21 columns."""
    activity_sessions = [
        session
        for session in sessions[:10]
        if session.display_name
        or session.git_branch
        or session.profile_name
        or session.chat_type
        or session.pinned
        or session.last_activity_at
        or session.last_activity_description
        or session.transport_profile
    ]
    if not activity_sessions:
        return None
    # transport_profile is new and usually NULL: show the column only when a
    # conversation actually arrived through a named bot profile.
    show_via = any(session.transport_profile for session in activity_sessions)
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("ID", style=theme.session_label)
    table.add_column("Name", style=theme.banner_text)
    table.add_column("Branch", style=theme.banner_dim)
    table.add_column("Profile", style=theme.banner_dim)
    table.add_column("Chat", style=theme.banner_dim)
    if show_via:
        table.add_column("Via", style=theme.banner_dim)
    table.add_column("Age", justify="right", style=theme.ui_accent)
    table.add_column("Last Activity", style=theme.banner_text)
    for session in activity_sessions:
        pin = f"{_PIN_MARKER} " if session.pinned else ""
        name = _session_display_name(session)
        branch = session.git_branch
        via = [escape(session.transport_profile) if session.transport_profile else "—"]
        table.add_row(
            escape(f"{pin}{session.session_id[-8:]}"),
            escape(_truncate(name, _MAX_NAME_CHARS)) if name else "—",
            escape(_truncate(branch, _MAX_BRANCH_CHARS)) if branch else "—",
            escape(session.profile_name) if session.profile_name else "—",
            escape(session.chat_type) if session.chat_type else "—",
            *(via if show_via else []),
            _age_label(_activity_at(session), now),
            escape(_truncate(session.last_activity_description, _MAX_ACTIVITY_CHARS))
            if session.last_activity_description
            else "—",
        )
    return table


def _usage_pattern_sections(analytics: UsageAnalytics, theme: Theme) -> list[RenderableType]:
    """Repository and hour-of-day activity over all visible sessions (not the filter)."""
    sections: list[RenderableType] = []
    if analytics.repos:
        sections.append(section_heading("Repositories", theme))
        table = Table(box=None, show_header=True, padding=(0, 1))
        table.add_column("Repo", style=theme.ui_label)
        table.add_column("7d", justify="right", style=theme.ui_accent)
        table.add_column("30d", justify="right", style=theme.banner_text)
        for repo in analytics.repos:
            table.add_row(
                escape(_cwd_label(repo.repo_root)), str(repo.sessions_7d), str(repo.sessions_30d)
            )
        sections.append(table)
    hourly = analytics.hourly_sessions_7d
    if any(hourly):
        peak = max(range(len(hourly)), key=lambda hour: hourly[hour])
        sections.append(section_heading("Activity by Hour (7d, local)", theme))
        line = Text("  00h ", style=theme.banner_dim)
        line.append(sparkline(hourly), style=theme.ui_accent)
        line.append(" 23h", style=theme.banner_dim)
        line.append(f"   peak {peak:02d}:00 ({hourly[peak]})", style=theme.banner_text)
        sections.append(line)
    return sections


def _visible_surfaces(
    state: DashboardState,
    sessions: list[SessionInfo],
    filter_query: str,
) -> list[ActiveSurface]:
    """The leases this detail view lists.

    A filtered view lists only the surfaces of the sessions it shows; the
    unfiltered view lists every retained surface, including ones whose session
    row is not in the table. Registry occupancy and capacity remain global to the
    selected registry and are labelled separately from these visible rows.
    """
    if not filter_query:
        return state.active_surfaces
    shown = {session.session_id for session in sessions}
    return [surface for surface in state.active_surfaces if surface.session_id in shown]


def _capacity_line(state: DashboardState, surfaces: list[ActiveSurface], theme: Theme) -> Text:
    """Three numbers that must never stand in for one another.

    * the **configured** capacity — ``max_concurrent_sessions``, the cross-process
      lease cap upstream checks at acquisition, and absent unless an operator set
      it (its enforcement is "only when an operator asked for one");
    * the **observed registry occupancy** — how many leases this registry holds;
    * the **verified executing activity** — leases whose identity check passed.
      An unverifiable lease is never counted here.

    ``distinct pids`` is a fourth number again: several leases can share one
    process, so occupancy is not a process count.
    """
    displayed = surfaces[:_DETAIL_MAX_SURFACE_ROWS]
    executing = sum(1 for surface in displayed if surface.liveness is ProcessLiveness.LIVE)
    unverified = sum(1 for surface in displayed if surface.liveness is ProcessLiveness.UNVERIFIABLE)
    dead = sum(1 for surface in displayed if surface.liveness is ProcessLiveness.DEAD)
    pids = len({surface.pid for surface in displayed if surface.pid > 0})
    line = Text()
    cap = state.config.max_concurrent_sessions
    if cap is None:
        line.append("  no active-session cap configured", style=theme.banner_dim)
    else:
        style = f"bold {theme.ui_warn}" if state.active_surface_count >= cap else theme.ui_accent
        line.append(f"  cap {cap} leases", style=style)
    shown_label = f"{len(displayed)} shown"
    if state.active_surfaces_truncated:
        shown_label += f" from the first {len(state.active_surfaces)} retained"
    line.append(
        f" · {state.active_surface_count} registry entries · {shown_label}"
        f" · {executing} verified executing"
        f" · {unverified} unverified · {dead} dead · {pids} distinct pids",
        style=theme.banner_text,
    )
    return line


def _surfaces_table(surfaces: list[ActiveSurface], theme: Theme) -> Table | None:
    """Leases recorded in ``runtime/active_sessions.json``, with their metadata."""
    if not surfaces:
        return None
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Session", style=theme.session_label)
    table.add_column("Surface", style=theme.banner_text)
    table.add_column("PID", justify="right", style=theme.banner_dim)
    table.add_column("Lease", style=theme.banner_dim)
    table.add_column("Held", justify="right", style=theme.banner_dim)
    table.add_column("State", style=theme.banner_dim)
    for surface in surfaces[:_DETAIL_MAX_SURFACE_ROWS]:
        table.add_row(
            escape(surface.session_id[-8:]),
            escape(surface.surface) if surface.surface else "—",
            str(surface.pid),
            escape(surface.lease_id[:_LEASE_ID_CHARS]) if surface.lease_id else "—",
            fmt_age_seconds(surface.started_at_age_seconds),
            _surface_state_label(surface, theme),
        )
    return table


def _surface_state_label(surface: ActiveSurface, theme: Theme) -> Text:
    """Liveness plus the two lease facts that qualify it.

    ``moved`` marks a lease upstream transferred to another session id
    (``transfer_active_session`` refreshes ``updated_at`` and nothing else does),
    so its acquisition age is not the age of the session it now holds.
    ``tracked`` marks a desktop lease, for which upstream demands provable
    liveness instead of warning.
    """
    label = _liveness_label(surface.liveness, theme)
    if surface.lease_renewed:
        label.append(" moved ", style=theme.banner_dim)
        label.append(fmt_age_seconds(surface.updated_at_age_seconds), style=theme.ui_accent)
    if surface.track_liveness:
        label.append(" tracked", style=theme.banner_dim)
    if surface.joinable:
        # The lease advertises metadata.shared_runtime_url; the URL itself is
        # never shown — the chip is the whole message.
        label.append(" joinable", style=f"bold {theme.ui_ok}")
    return label


def _liveness_label(liveness: ProcessLiveness, theme: Theme) -> Text:
    """Identity-verified liveness; an unverified pid is never shown as live.

    "unverified" means the pid exists but its start time was never recorded or
    could not be observed here, so hermesd cannot tell it from a reused pid.
    """
    if liveness is ProcessLiveness.LIVE:
        return Text("live", style=f"bold {theme.ui_ok}")
    if liveness is ProcessLiveness.DEAD:
        return Text("dead", style=theme.ui_error)
    return Text("unverified", style=theme.ui_warn)


def _compression_warnings(sessions: list[SessionInfo], theme: Theme, *, now: float) -> Text | None:
    """Compression failure diagnostics plus the durable anti-thrash recovery state.

    Both halves come from counters and timestamps on the session row; hermesd
    never reads conversation content to produce them. ``now`` is
    ``state.collected_at`` — the injected clock — so a cooldown or recovery
    deadline that has already elapsed renders as nothing at all rather than as a
    warning about a timer that is no longer running.
    """
    recent = sessions[:_COMPRESSION_WARNING_ROWS]
    lines = Text()
    for session in recent:
        if not session.compression_failure_error:
            continue
        error = _truncate(session.compression_failure_error, _MAX_ERROR_CHARS)
        lines.append(f"  {sanitize_terminal_text(session.session_id[-8:])}  ", style=theme.ui_label)
        lines.append(f"{sanitize_terminal_text(error)}\n", style=theme.ui_warn)
    for session in recent:
        if not session.compression_recovery_active(now):
            continue
        lines.append(f"  {sanitize_terminal_text(session.session_id[-8:])}  ", style=theme.ui_label)
        lines.append("recovery — ", style=theme.ui_accent)
        lines.append(
            f"{sanitize_terminal_text(_recovery_detail(session, now))}\n", style=theme.ui_warn
        )
    if not lines.plain:
        return None
    lines.append(f"  {_COMPRESSION_WARNING_NOTE}", style=theme.banner_dim)
    return lines


# Rendered whenever the section has anything in it: the rows above are a
# cooldown the compressor imposed on itself, and reading them as a failure — or
# as a threshold hermesd measured — is the mistake worth pre-empting.
_COMPRESSION_WARNING_NOTE = (
    "Compression recovery state is counters and timestamps from the session row; "
    "hermesd never reads conversation content, and a live cooldown is the "
    "compressor's own back-off, not a failure."
)


def _recovery_detail(session: SessionInfo, now: float) -> str:
    """The live timers first, then the durable counters behind them."""
    parts: list[str] = []
    cooldown = session.compression_cooldown_remaining(now)
    if cooldown is not None:
        parts.append(f"cooldown {_duration_label(cooldown)} left")
    probe = session.compression_recovery_remaining(now)
    if probe is not None:
        parts.append(f"anti-thrash probe in {_duration_label(probe)}")
    if session.compression_fallback_streak:
        parts.append(f"fallback streak {session.compression_fallback_streak}")
    if session.compression_ineffective_count:
        parts.append(f"ineffective {session.compression_ineffective_count}")
    return ", ".join(parts)


def _duration_label(seconds: float) -> str:
    """A countdown window; sub-second values keep one decimal place."""
    if seconds < 1.0:
        return f"{seconds:.1f}s"
    return fmt_age_seconds(seconds)


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


def _billing_table(sessions: list[SessionInfo], theme: Theme) -> Table | None:
    billing_sessions = [
        session
        for session in sessions[:10]
        if session.end_reason
        or session.billing_base_url
        or session.billing_mode
        or session.context_limit
    ]
    if not billing_sessions:
        return None
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("ID", style=theme.session_label)
    table.add_column("End", style=theme.banner_dim)
    table.add_column("Endpoint", style=theme.banner_text)
    table.add_column("Mode", style=theme.banner_dim)
    # Lifetime tokens vs the model's context window, not live occupancy.
    table.add_column("Lifetime / Limit", justify="right", style=theme.banner_dim)
    for session in billing_sessions:
        table.add_row(
            escape(session.session_id[-8:]),
            escape(session.end_reason) if session.end_reason else "—",
            escape(session.billing_base_url) if session.billing_base_url else "—",
            escape(session.billing_mode) if session.billing_mode else "—",
            _lifetime_context_label(session),
        )
    return table


def _lifetime_context_label(session: SessionInfo) -> str:
    if not session.context_limit:
        return "—"
    lifetime_tokens = (
        session.input_tokens
        + session.output_tokens
        + session.cache_read_tokens
        + session.cache_write_tokens
        + session.reasoning_tokens
    )
    return f"{fmt_tokens(lifetime_tokens)} / {fmt_tokens(session.context_limit)}"


# ── Session coordination sections (leases, hygiene, routes, churn, ttys) ────


def _coordination_sections(state: DashboardState, theme: Theme) -> list[RenderableType]:
    """The state.db coordination sections under panel 2's detail view.

    Everything here is counters, timestamps and flags from the selected
    profile's ``state.db`` plus the terminal breadcrumb files — conversation
    content is never read. Each section renders only when its source has
    anything to say; an empty table reads as "nothing is stuck", which is the
    healthy state.
    """
    coord = state.session_coordination
    sections: list[RenderableType] = []
    if coord.leases:
        sections.append(section_heading("Turn Leases & Locks", theme))
        sections.append(_leases_table(coord.leases, theme))
        sections.append(Text(f"  {_LEASE_NOTE}", style=theme.banner_dim))
    if coord.hygiene:
        sections.append(section_heading("Hygiene Cooldowns", theme))
        caption = f"  {coord.hygiene_total} chat(s) with a failure streak"
        if coord.hygiene_total > len(coord.hygiene):
            caption += f" — showing the worst {len(coord.hygiene)}"
        sections.append(Text(caption, style=theme.banner_text))
        sections.append(_hygiene_table(coord.hygiene, theme))
        sections.append(Text(f"  {_HYGIENE_NOTE}", style=theme.banner_dim))
    if coord.routes:
        sections.append(section_heading("Chat Routes", theme))
        sections.append(_routes_table(coord.routes, theme))
        waiting = sum(1 for route in coord.routes if route.needs_user_message)
        dangling = sum(1 for route in coord.routes if route.dangling)
        if waiting or dangling:
            # Only a capped list needs the routing table's exact size beside it.
            total = coord.route_total if coord.route_total > len(coord.routes) else None
            sections.append(
                Text(
                    f"  {_route_counts_label(waiting, dangling, total)}\n", style=theme.banner_text
                )
            )
    # An emptied table is the worst case of the never-prune invariant break:
    # the chat total is 0 exactly when the shrink flag is set, so gating on the
    # total alone would hide the warning it exists to raise.
    if coord.generation_chat_total or coord.generation_count_shrank:
        sections.append(section_heading("Reset Churn", theme))
        sections.append(_reset_churn_section(coord, theme))
    term = state.terminal_sessions
    if term.count:
        sections.append(section_heading("CLI Terminals", theme))
        sections.append(_terminal_section(term, theme))
    return sections


_LEASE_NOTE = (
    "Turn leases key a conversation lineage; compression locks key one session "
    "and only block other compressions, never turns "
    "(hermes_state_compression.py:451-474). "
    "Upstream revives an expired lease whose holder still matches rather than "
    "stealing it, so expiry alone is benign — only expired rows with a dead "
    "holder (orphaned) are stuck. There is no background sweeper; rows clear "
    "when their holder releases them."
)

_HYGIENE_NOTE = (
    "Failure streaks are per rotation-stable chat key; cooldowns climb x1/x3/x9 "
    "over the default 300s base (config `hygiene_failure_cooldown_seconds`), "
    "clamped at 1h, so streak 3 suspends pre-turn compaction. A row clears only "
    "when compaction recovers."
)


def _ttl_label(lease: SessionLease) -> str:
    expires_in = lease.expires_in_seconds
    if expires_in is None:
        return "—"
    if expires_in >= 0:
        return f"{fmt_age_seconds(expires_in)} left"
    return f"expired {fmt_age_seconds(-expires_in)} ago"


def _lease_state_label(lease: SessionLease, theme: Theme) -> Text:
    """Orphaned first (the action-worthy case), then plain expiry — an expired
    lease whose holder still matches is revived upstream, not stolen."""
    if lease.orphaned:
        return Text("orphaned", style=f"bold {theme.ui_error}")
    if lease.expired:
        return Text("expired — holder may revive", style=theme.ui_warn)
    if lease.liveness is ProcessLiveness.LIVE:
        return Text("live", style=f"bold {theme.ui_ok}")
    return Text("unverified", style=theme.ui_warn)


def _leases_table(leases: list[SessionLease], theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Kind", style=theme.ui_label)
    table.add_column("Key", style=theme.session_label)
    table.add_column("PID", justify="right", style=theme.banner_dim)
    table.add_column("Held", justify="right", style=theme.banner_dim)
    table.add_column("TTL", justify="right", style=theme.banner_dim)
    table.add_column("State", style=theme.banner_dim)
    for lease in leases:
        table.add_row(
            "turn lease" if lease.kind is SessionLeaseKind.TURN_LEASE else "compress lock",
            escape(lease.key[-_COORDINATION_KEY_CHARS:]),
            str(lease.pid) if lease.pid else "—",
            fmt_age_seconds(lease.held_seconds),
            _ttl_label(lease),
            _lease_state_label(lease, theme),
        )
    return table


def _hygiene_effect_label(suspended: bool) -> str:
    """Effect wording from the model's derived flag, not a re-derived threshold.

    ``GatewayHygieneState.suspended`` is the reader's verdict
    (``failure_streak >= _HYGIENE_SUSPENSION_STREAK``); re-checking the streak
    here would duplicate the ladder and silently disagree with the model if the
    suspension point ever moves.
    """
    if suspended:
        return "compaction suspended, cooldown up to 1h"
    return "compaction cooldown backoff"


def _hygiene_table(rows: list[GatewayHygieneState], theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Chat", style=theme.session_label)
    table.add_column("Streak", justify="right", style=theme.ui_accent)
    table.add_column("Effect", style=theme.ui_warn)
    table.add_column("Last error", style=theme.banner_dim)
    for row in rows:
        table.add_row(
            escape(row.session_key[-_COORDINATION_KEY_CHARS:]),
            str(row.failure_streak),
            escape(_hygiene_effect_label(row.suspended)),
            escape(_truncate(row.compression_failure_error, _MAX_COORDINATION_ERROR_CHARS))
            if row.compression_failure_error
            else "—",
        )
    return table


def _route_flags(route: GatewayRouteState) -> str:
    """Severity-ordered: the cap keeps the most actionable flags, so a broken
    resume target or a stuck turn can never be truncated away by a longer
    list of historical markers."""
    flags: list[str] = []
    if route.dangling:
        flags.append("dangling")
    if route.turn_never_unwound:
        flags.append("turn never unwound")
    if route.suspended:
        flags.append("suspended")
    if route.resume_pending:
        flags.append(
            f"resume-pending ({route.resume_reason})" if route.resume_reason else "resume-pending"
        )
    if route.was_auto_reset:
        flags.append(
            f"auto-reset ({route.auto_reset_reason})" if route.auto_reset_reason else "auto-reset"
        )
    return ", ".join(flags[:_MAX_ROUTE_FLAGS]) if flags else "—"


def _routes_table(routes: list[GatewayRouteState], theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Chat", style=theme.session_label)
    table.add_column("Platform", style=theme.banner_text)
    table.add_column("Type", style=theme.banner_dim)
    table.add_column("Turn", justify="right", style=theme.banner_dim)
    table.add_column("Name", style=theme.banner_text)
    table.add_column("Flags", style=theme.ui_warn)
    for route in routes:
        table.add_row(
            escape(route.session_key[-_COORDINATION_KEY_CHARS:]),
            escape(route.platform) if route.platform else "—",
            escape(route.chat_type) if route.chat_type else "—",
            fmt_age_seconds(route.turn_age_seconds),
            escape(route.display_name) if route.display_name else "—",
            escape(_route_flags(route)),
        )
    return table


def _route_counts_label(waiting: int, dangling: int, total: int | None = None) -> str:
    """Counts from the retained rows, with the table's exact size when capped.

    ``total`` is only passed when the row list was truncated, so an uncapped
    panel keeps the plain wording rather than stating the obvious.
    """
    scope = f" of {total} routed chats" if total is not None else " chat(s)"
    parts: list[str] = []
    if waiting:
        parts.append(f"{waiting}{scope} need a user message to recover")
    if dangling:
        parts.append(f"{dangling}{scope} dangling — the gateway would resume a nonexistent session")
    return " · ".join(parts)


def _reset_churn_section(coord: SessionCoordinationState, theme: Theme) -> RenderableType:
    """Top chats by generation plus the lifetime reset total.

    ``conversation_generations`` is never pruned upstream, so the row count is
    a lifetime figure — a shrink between refreshes is an invariant break, not
    quiet tidying, and is called out as such.
    """
    lines = Text()
    lines.append(f"  lifetime resets {coord.generation_reset_total}", style=theme.ui_accent)
    lines.append(f" across {coord.generation_chat_total} chat(s)", style=theme.banner_text)
    if coord.generation_count_shrank:
        lines.append(
            " · table shrank between refreshes — an invariant break; upstream never prunes it",
            style=f"bold {theme.ui_error}",
        )
    lines.append("\n")
    if not coord.generations:
        return lines
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Chat", style=theme.session_label)
    table.add_column("Source", style=theme.banner_dim)
    table.add_column("Resets", justify="right", style=theme.ui_accent)
    for generation in coord.generations[:_RESET_CHURN_ROWS]:
        table.add_row(
            escape(generation.session_key[-_COORDINATION_KEY_CHARS:]),
            escape(generation.source) if generation.source else "—",
            str(generation.generation),
        )
    return Group(lines, table)


_TERMINAL_NOTE = (
    "file count is an upper bound — a breadcrumb proves a terminal recorded a "
    "session recently, not that the terminal is still alive, and stale files "
    "linger up to 30 days"
)


def _terminal_section(term: TerminalSessionReadout, theme: Theme) -> RenderableType:
    lines = Text()
    if term.truncated:
        # The directory exceeded the scan bound, so the figure is a floor.
        lines.append(
            f"  at least {term.count} open CLI terminals in the last 24 hours",
            style=theme.ui_accent,
        )
        lines.append(" (directory scan truncated)", style=theme.ui_warn)
    else:
        lines.append(
            f"  {term.count} open CLI terminals in the last 24 hours", style=theme.ui_accent
        )
    lines.append(f" — {_TERMINAL_NOTE}\n", style=theme.banner_dim)
    if not term.sessions:
        return lines
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Terminal", style=theme.session_label)
    table.add_column("Session", style=theme.banner_dim)
    table.add_column("CWD", style=theme.banner_text)
    table.add_column("Age", justify="right", style=theme.banner_dim)
    for row in term.sessions[:_TERMINAL_ROWS]:
        table.add_row(
            escape(row.terminal),
            escape(row.session_id[-8:]) if row.session_id else "—",
            escape(_cwd_label(row.cwd)) if row.cwd else "—",
            fmt_age_seconds(row.age_seconds),
        )
    return Group(lines, table)
