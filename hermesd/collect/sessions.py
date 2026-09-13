"""Session, token and cost analytics derived from the sessions table."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hermesd.collect.common import (
    _age_seconds,
    _as_list,
    _coerce_bool,
    _coerce_float,
    _coerce_int,
    _iso_to_epoch,
)
from hermesd.collect.operations import _json_object_capped
from hermesd.collect.redaction import (
    _redact_bare_credentials,
    _redact_secret_url,
    _redact_text_fields,
)
from hermesd.collect.sqlite_util import (
    _count_rows,
    _query_rows,
    _table_count_or_zero,
    _table_exists,
)
from hermesd.file_cache import _read_capped
from hermesd.models import (
    AUTHORITATIVE_COST_STATUSES,
    BackgroundProcessInfo,
    ConversationGeneration,
    GatewayHygieneState,
    GatewayRouteState,
    ProcessLiveness,
    SessionLease,
    SessionLeaseKind,
    TokenBreakdown,
    TokenSummary,
    TokenWindowSummary,
)


def _summarize_tokens(
    rows: list[dict[str, Any]],
    started_at_min: float | None = None,
) -> TokenSummary:
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    reasoning_tokens = 0
    total_cost_usd = 0.0
    contributing_rows = 0
    reported_rows = 0
    for row in rows:
        # SQLite is untyped: coerce before comparing, or a text epoch raises.
        started_at = _coerce_float(row.get("started_at"))
        if started_at_min is not None and started_at < started_at_min:
            continue
        contributing_rows += 1
        if _session_cost_is_reported(row):
            reported_rows += 1
        input_tokens += _coerce_int(row.get("input_tokens"))
        output_tokens += _coerce_int(row.get("output_tokens"))
        cache_read_tokens += _coerce_int(row.get("cache_read_tokens"))
        cache_write_tokens += _coerce_int(row.get("cache_write_tokens"))
        reasoning_tokens += _coerce_int(row.get("reasoning_tokens"))
        total_cost_usd += _resolved_session_cost(row)
    return TokenSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        total_cost_usd=total_cost_usd,
        cost_is_estimated=contributing_rows == 0 or reported_rows < contributing_rows,
    )


def _summarize_window(
    label: str,
    rows: list[dict[str, Any]],
    days: int,
    *,
    now: float | None = None,
) -> TokenWindowSummary:
    cutoff = (now if now is not None else time.time()) - days * 86400
    filtered = [row for row in rows if _coerce_float(row.get("started_at")) >= cutoff]
    totals = _summarize_tokens(filtered)
    prompt_tokens = totals.input_tokens + totals.cache_read_tokens
    cache_ratio = totals.cache_read_tokens / prompt_tokens if prompt_tokens > 0 else 0.0
    return TokenWindowSummary(
        label=label,
        session_count=len(filtered),
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        total_cost_usd=totals.total_cost_usd,
        cache_ratio=cache_ratio,
    )


def _count_cost_statuses(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("cost_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _summarize_breakdown(rows: list[dict[str, Any]], key_name: str) -> list[TokenBreakdown]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        # Breakdown labels are rendered and serialized; URL-shaped values such
        # as billing_base_url may carry userinfo or secret query params.
        label = _redact_secret_url(str(row.get(key_name) or "unknown"))
        grouped.setdefault(label, []).append(row)

    summaries = []
    for label, group in grouped.items():
        totals = _summarize_tokens(group)
        summaries.append(
            TokenBreakdown(
                label=label,
                session_count=len(group),
                input_tokens=totals.input_tokens,
                output_tokens=totals.output_tokens,
                cache_read_tokens=totals.cache_read_tokens,
                total_cost_usd=totals.total_cost_usd,
            )
        )
    return sorted(
        summaries,
        key=lambda summary: (-summary.total_cost_usd, -summary.input_tokens, summary.label),
    )


# Approximate fallback cost per 1M tokens (USD), used only when a session row
# carries no provider-reported cost. Not billing authority: provider-reported
# costs always win (see _resolved_session_cost). Figures are list prices for the
# GPT-4o / Claude Sonnet tier as of 2026-07; re-check when that tier reprices.
_COST_PER_M = {
    "input": 2.50,  # GPT-4o / Claude Sonnet class
    "output": 10.00,
    "cache_read": 0.30,  # typical prompt caching discount
    "cache_write": 3.125,  # input x 1.25, typical cache write premium
    "reasoning": 10.00,
}


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    reasoning_tokens: int,
    cache_write_tokens: int = 0,
) -> float:
    input_tokens = _bounded_token_count(input_tokens)
    output_tokens = _bounded_token_count(output_tokens)
    cache_read_tokens = _bounded_token_count(cache_read_tokens)
    cache_write_tokens = _bounded_token_count(cache_write_tokens)
    reasoning_tokens = _bounded_token_count(reasoning_tokens)
    return (
        input_tokens * _COST_PER_M["input"]
        + output_tokens * _COST_PER_M["output"]
        + cache_read_tokens * _COST_PER_M["cache_read"]
        + cache_write_tokens * _COST_PER_M["cache_write"]
        + reasoning_tokens * _COST_PER_M["reasoning"]
    ) / 1_000_000


def _session_cost_is_reported(row: dict[str, Any]) -> bool:
    # A non-zero actual_cost_usd (hermes-agent 0.21) is provider-billed and
    # authoritative regardless of cost_status.
    if _coerce_float(row.get("actual_cost_usd")) > 0:
        return True
    return str(row.get("cost_status") or "") in AUTHORITATIVE_COST_STATUSES and (
        row.get("actual_cost_usd") is not None or row.get("estimated_cost_usd") is not None
    )


def _resolved_session_cost(row: dict[str, Any]) -> float:
    actual_cost = _coerce_float(row.get("actual_cost_usd"))
    if actual_cost > 0:
        return actual_cost
    raw_cost = row.get("estimated_cost_usd")
    cost = _coerce_float(raw_cost)
    if _session_cost_is_reported(row):
        # An explicit authoritative zero wins over a positive estimate; older
        # rows without actual_cost_usd store their reported cost in raw_cost.
        if row.get("actual_cost_usd") is not None:
            return actual_cost
        return cost
    if cost:
        return cost
    return _estimate_cost(
        _coerce_int(row.get("input_tokens")),
        _coerce_int(row.get("output_tokens")),
        _coerce_int(row.get("cache_read_tokens")),
        _coerce_int(row.get("reasoning_tokens")),
        _coerce_int(row.get("cache_write_tokens")),
    )


def _bounded_token_count(value: int) -> int:
    if value <= 0:
        return 0
    return min(value, 10**15)


def _context_limit_for(context_lengths: Mapping[str, int], model: str, base_url: str) -> int:
    normalized_base = base_url.rstrip("/")
    exact = context_lengths.get(f"{model}@{normalized_base}")
    if exact is not None:
        return exact

    origin = _url_origin(normalized_base)
    if not origin:
        return 0
    for key, value in sorted(context_lengths.items()):
        cached_model, sep, cached_base = key.partition("@")
        if sep and cached_model == model and _url_origin(cached_base) == origin:
            return value
    return 0


def _url_origin(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _tool_names_from_entries(value: object, *, allow_bare_names: bool = False) -> set[str]:
    """Extract tool names from a banner/session ``tools`` list of any shape.

    ``allow_bare_names`` keeps the legacy session-file shape (a plain list of
    tool names) working; the banner snapshot always uses mappings.
    """
    names: set[str] = set()
    for item in _as_list(value):
        raw: object
        if isinstance(item, dict):
            function = item.get("function")
            raw = function.get("name") if isinstance(function, dict) else None
            if not raw:
                raw = item.get("name")
        else:
            raw = item if allow_bare_names else None
        if isinstance(raw, str) and raw:
            names.add(raw)
    return names


def _read_session_tools(path: Path) -> object:
    """Return the raw ``tools`` value of a session file without caching it.

    The read is capped at the shared parsed-file byte limit: session files hold
    full transcripts and can be tens of MB, so an oversize file raises OSError
    instead of being parsed whole. A session file the index names but that no
    longer exists simply has no tools. A file that exists but cannot be decoded
    raises instead: silently treating it as empty shrinks the reported tool
    inventory, where raising fails the tools source and keeps the last-good
    inventory — the same handling a symlinked session file already gets.
    """
    try:
        data = json.loads(_read_capped(path))
    except FileNotFoundError:
        return None
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OSError(f"Unreadable session file: {path.name}") from exc
    return data.get("tools") if isinstance(data, dict) else None


def _background_process_from_ledger(
    entry: Mapping[str, Any],
    pid_exists: Callable[[int], bool],
) -> BackgroundProcessInfo:
    pid = _coerce_int(entry.get("pid"))
    purpose = str(entry.get("purpose") or "")
    return BackgroundProcessInfo(
        session_id=str(entry.get("session_id") or "") or purpose or f"pid:{pid}",
        command=str(entry.get("argv") or ""),
        pid=pid,
        # The ledger records the install root a process runs from; it is the
        # closest thing it carries to a working directory.
        cwd=str(entry.get("install") or ""),
        started_at=(
            _coerce_float(entry.get("create_time")) or _coerce_float(entry.get("registered_at"))
        ),
        purpose=purpose,
        port=_coerce_int(entry.get("port")),
        profile=str(entry.get("profile") or ""),
        alive=bool(pid) and pid_exists(pid),
    )


# ── Session coordination sources (state.db side of panel 2) ─────────────────

# Rows per coordination table held in the state.db readout. The live tables are
# tiny (one row per active conversation/lock upstream), so these caps only
# guard a runaway database; the *_total counts stay exact regardless.
_COORDINATION_ROW_LIMIT = 40
_HYGIENE_ROW_LIMIT = 50
_ROUTE_ROW_LIMIT = 50
_GENERATION_ROW_LIMIT = 10

# Holder strings embed the owning process: "pid=N:tid=N:agent=hex:nonce=hex8"
# (agent/conversation_compression.py:1485). Same parse upstream uses when it
# decides a holder is reclaimable (hermes_state.py:124).
_HOLDER_PID_RE = re.compile(r"(?:^|:)pid=(\d+)(?::|$)")


def _holder_pid(holder: str) -> int:
    """Local pid embedded in a lease holder string; 0 when there is none.

    A holder without a parseable ``pid=`` (or with a non-positive one) is
    *unstructured*: upstream keeps its lease until TTL expiry instead of
    reclaiming on a kernel guess (``hermes_state.py:119-143``).
    """
    match = _HOLDER_PID_RE.search(holder or "")
    return int(match.group(1)) if match else 0


def _holder_liveness(pid: int, pid_exists: Callable[[int], bool]) -> ProcessLiveness:
    """Upstream's reclaim conservatism, mirrored: only kernel proof the pid is
    gone marks a holder dead; a reused pid reads as alive (the wrong-alive
    answer self-heals at TTL, the wrong-dead answer would fork a lineage)."""
    if pid <= 0:
        return ProcessLiveness.UNVERIFIABLE
    return ProcessLiveness.LIVE if pid_exists(pid) else ProcessLiveness.DEAD


@dataclass(frozen=True, slots=True)
class _SessionCoordinationRows:
    """Raw state.db coordination rows, before clock/pid enrichment.

    Cached against state.db's mtime like every readout component, so it holds
    only database values — never an age, an expired verdict, or a liveness
    check (see ``StateDbRead`` in ``collect/operations.py``).
    """

    lease_rows: tuple[dict[str, Any], ...] = ()
    lock_rows: tuple[dict[str, Any], ...] = ()
    lease_total: int = 0
    hygiene_rows: tuple[dict[str, Any], ...] = ()
    hygiene_total: int = 0
    routing_rows: tuple[dict[str, Any], ...] = ()
    routing_total: int = 0
    generation_rows: tuple[dict[str, Any], ...] = ()
    generation_chat_total: int = 0
    generation_reset_total: int = 0


def _read_session_coordination_rows(conn: Any) -> _SessionCoordinationRows:
    """Every coordination table hermesd reads from state.db, absent-tolerant.

    The shapes mirror upstream ``hermes_state_common.py``: ``session_turn_leases``
    / ``compression_locks`` (``:506-518``, writers
    ``hermes_state_compression.py:433-605``), ``gateway_routing`` (``:447-457``,
    payload written by ``gateway/session.py:535-545``), ``gateway_hygiene_state``
    (``:459-465``, writer ``hermes_state_gateway.py:513-535``) and
    ``conversation_generations`` (``:482-487``, bumped by
    ``hermes_state_messages.py:30-34``) — all through
    ``get_hermes_home()/"state.db"`` (``hermes_state.py:160,178``), i.e. the
    selected profile's store.

    Tables predate nothing: agents older than the lease/hygiene/routing
    features simply have no table, which reads as empty — the same contract as
    the operations and gateway-ledger readers. Once a table exists, read errors
    propagate so the owning source fails to its last-good value.
    """
    lease_rows: tuple[dict[str, Any], ...] = ()
    lock_rows: tuple[dict[str, Any], ...] = ()
    lease_total = 0
    if _table_exists(conn, "session_turn_leases"):
        # Soonest expiry first: the row about to lapse is the one worth watching.
        lease_rows = tuple(
            _query_rows(
                conn,
                "SELECT conversation_id, holder, acquired_at, expires_at FROM session_turn_leases "
                f"ORDER BY COALESCE(expires_at, 0) LIMIT {_COORDINATION_ROW_LIMIT}",
            )
        )
        lease_total += _table_count_or_zero(conn, "session_turn_leases")
    if _table_exists(conn, "compression_locks"):
        lock_rows = tuple(
            _query_rows(
                conn,
                "SELECT session_id, holder, acquired_at, expires_at FROM compression_locks "
                f"ORDER BY COALESCE(expires_at, 0) LIMIT {_COORDINATION_ROW_LIMIT}",
            )
        )
        lease_total += _table_count_or_zero(conn, "compression_locks")
    hygiene_rows: tuple[dict[str, Any], ...] = ()
    hygiene_total = 0
    if _table_exists(conn, "gateway_hygiene_state"):
        # A 0-streak row is cleared state upstream keeps only transiently; it is
        # not a warning and never reaches the panel. The total counts exactly the
        # rows the list is filtered to, so a capped list can never be mistaken
        # for the count.
        hygiene_rows = tuple(
            _query_rows(
                conn,
                "SELECT session_key, failure_streak FROM gateway_hygiene_state "
                "WHERE COALESCE(failure_streak, 0) > 0 "
                f"ORDER BY COALESCE(failure_streak, 0) DESC LIMIT {_HYGIENE_ROW_LIMIT}",
            )
        )
        hygiene_total = _count_rows(
            conn,
            "SELECT COUNT(*) FROM gateway_hygiene_state WHERE COALESCE(failure_streak, 0) > 0",
        )
    routing_rows: tuple[dict[str, Any], ...] = ()
    routing_total = 0
    if _table_exists(conn, "gateway_routing"):
        routing_rows = tuple(
            _query_rows(
                conn,
                "SELECT scope, session_key, entry_json, updated_at FROM gateway_routing "
                f"ORDER BY COALESCE(updated_at, 0) DESC LIMIT {_ROUTE_ROW_LIMIT}",
            )
        )
        routing_total = _table_count_or_zero(conn, "gateway_routing")
    generation_rows: tuple[dict[str, Any], ...] = ()
    generation_chat_total = 0
    generation_reset_total = 0
    if _table_exists(conn, "conversation_generations"):
        generation_rows = tuple(
            _query_rows(
                conn,
                "SELECT source, session_key, generation FROM conversation_generations "
                f"ORDER BY COALESCE(generation, 0) DESC, session_key LIMIT {_GENERATION_ROW_LIMIT}",
            )
        )
        generation_chat_total = _table_count_or_zero(conn, "conversation_generations")
        generation_reset_total = _count_rows(
            conn, "SELECT COALESCE(SUM(COALESCE(generation, 0)), 0) FROM conversation_generations"
        )
    return _SessionCoordinationRows(
        lease_rows=lease_rows,
        lock_rows=lock_rows,
        lease_total=lease_total,
        hygiene_rows=hygiene_rows,
        hygiene_total=hygiene_total,
        routing_rows=routing_rows,
        routing_total=routing_total,
        generation_rows=generation_rows,
        generation_chat_total=generation_chat_total,
        generation_reset_total=generation_reset_total,
    )


def _session_lease(
    row: dict[str, Any],
    kind: SessionLeaseKind,
    *,
    now: float,
    pid_exists: Callable[[int], bool],
) -> SessionLease:
    """One coordination row, enriched against the injected clock and pid probe.

    ``expired`` uses the same boundary upstream's claimers do
    (``hermes_state_compression.py:460-463``: reclaim when
    ``expires_at <= now`` or the holder's pid is dead). An expired lease whose
    holder still matches is *revived* rather than stolen
    (``:433-439``), so expiry alone is benign — the orphan verdict needs the
    dead-pid half.
    """
    holder = str(row.get("holder") or "")
    pid = _holder_pid(holder)
    acquired_at = _coerce_float(row.get("acquired_at")) or None
    expires_at = _coerce_float(row.get("expires_at")) or None
    return SessionLease(
        kind=kind,
        key=str(row.get("conversation_id") or row.get("session_id") or ""),
        holder=holder,
        pid=pid,
        held_seconds=_age_seconds(acquired_at, now),
        expires_in_seconds=(expires_at - now) if expires_at is not None else None,
        expired=expires_at is not None and expires_at <= now,
        liveness=_holder_liveness(pid, pid_exists),
    )


def _session_lease_fields(
    rows: _SessionCoordinationRows,
    *,
    now: float,
    pid_exists: Callable[[int], bool],
) -> dict[str, Any]:
    leases = [
        _session_lease(row, SessionLeaseKind.TURN_LEASE, now=now, pid_exists=pid_exists)
        for row in rows.lease_rows
    ]
    leases.extend(
        _session_lease(row, SessionLeaseKind.COMPRESSION_LOCK, now=now, pid_exists=pid_exists)
        for row in rows.lock_rows
    )
    return {"leases": leases, "lease_total": rows.lease_total}


# The hygiene cooldown ladder: multipliers over the 300s base cooldown, clamped
# at one hour (gateway/run.py:101-103,147-149). Streak 3+ is effectively
# "pre-turn compaction off" for that chat.
_HYGIENE_SUSPENSION_STREAK = 3


def _hygiene_fields(
    rows: tuple[dict[str, Any], ...],
    session_rows: list[dict[str, Any]],
    total: int = 0,
) -> dict[str, Any]:
    """Pair each streak with the chat's recorded compression failure, if any.

    The join runs over the raw session rows (rotation-stable ``session_key``
    column, written by the gateway repair paths); a chat with no session row or
    no recorded error simply carries an empty reason.
    """
    errors_by_key: dict[str, str] = {}
    for row in session_rows:
        key = str(row.get("session_key") or "")
        error = str(row.get("compression_failure_error") or "")
        if key and error:
            errors_by_key.setdefault(key, error)
    hygiene = [
        GatewayHygieneState(
            session_key=str(row.get("session_key") or ""),
            failure_streak=_coerce_int(row.get("failure_streak")),
            suspended=_coerce_int(row.get("failure_streak")) >= _HYGIENE_SUSPENSION_STREAK,
            compression_failure_error=errors_by_key.get(str(row.get("session_key") or ""), ""),
        )
        for row in rows
    ]
    return {"hygiene": hygiene, "hygiene_total": total}


def _route_free_text(value: object) -> str:
    """Redact one remote-controlled ``entry_json`` string.

    Bare credential shapes are scrubbed first (a chat display name carries no
    ``key = value`` label), then the field-oriented pass runs so multi-word
    values under a secret key still collapse.
    """
    return _redact_text_fields(_redact_bare_credentials(str(value or "")))


def _gateway_route(
    row: dict[str, Any],
    *,
    now: float,
    known_session_ids: frozenset[str] | set[str],
) -> GatewayRouteState:
    """Decode one routing row's ``entry_json`` into display state.

    The payload is ``SessionEntry.to_dict()`` (``gateway/session.py:535-545``):
    an unbounded free-text map that also carries token counters and Slack
    watermarks. It is decoded through the bounded JSON reader and only the
    state flags below survive; ``display_name`` — attacker-controlled chat
    metadata — is redacted and clipped before it reaches the model. Timestamps
    are ISO strings in the entry; the table's ``updated_at`` REAL backs the
    entry up when its own stamp is unusable.
    """
    entry = _json_object_capped(row.get("entry_json")) or {}
    session_id = str(entry.get("session_id") or "")
    updated_at = _iso_to_epoch(entry.get("updated_at"))
    if updated_at is None:
        updated_at = _coerce_float(row.get("updated_at")) or None
    turn_token = str(entry.get("active_turn_token") or "")
    turn_started = _iso_to_epoch(entry.get("active_turn_started_at"))
    return GatewayRouteState(
        session_key=str(row.get("session_key") or ""),
        session_id=session_id,
        platform=str(entry.get("platform") or ""),
        chat_type=str(entry.get("chat_type") or ""),
        display_name=_route_free_text(entry.get("display_name"))[:40],
        updated_at_age_seconds=_age_seconds(updated_at, now),
        suspended=_coerce_bool(entry.get("suspended")),
        resume_pending=_coerce_bool(entry.get("resume_pending")),
        resume_reason=_route_free_text(entry.get("resume_reason")),
        was_auto_reset=_coerce_bool(entry.get("was_auto_reset")),
        auto_reset_reason=_route_free_text(entry.get("auto_reset_reason")),
        # The durable executing-turn marker: its start age is only meaningful
        # while a token exists (gateway/session.py:515-518).
        turn_age_seconds=_age_seconds(turn_started, now) if turn_token else None,
        dangling=bool(session_id) and session_id not in known_session_ids,
    )


def _gateway_route_fields(
    rows: tuple[dict[str, Any], ...],
    *,
    route_total: int,
    now: float,
    known_session_ids: frozenset[str] | set[str],
) -> dict[str, Any]:
    routes = [_gateway_route(row, now=now, known_session_ids=known_session_ids) for row in rows]
    return {"routes": routes, "route_total": route_total}


def _generation_fields(rows: _SessionCoordinationRows) -> dict[str, Any]:
    generations = [
        ConversationGeneration(
            source=str(row.get("source") or ""),
            session_key=str(row.get("session_key") or ""),
            generation=_coerce_int(row.get("generation")),
        )
        for row in rows.generation_rows
    ]
    return {
        "generations": generations,
        "generation_chat_total": rows.generation_chat_total,
        "generation_reset_total": rows.generation_reset_total,
    }
