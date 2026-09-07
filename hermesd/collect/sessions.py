"""Session, token and cost analytics derived from the sessions table."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hermesd.collect.common import _as_list, _coerce_float, _coerce_int
from hermesd.models import (
    AUTHORITATIVE_COST_STATUSES,
    BackgroundProcessInfo,
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
        started_at = row.get("started_at") or 0.0
        if started_at_min is not None and started_at < started_at_min:
            continue
        contributing_rows += 1
        if _session_cost_is_reported(row):
            reported_rows += 1
        input_tokens += row.get("input_tokens") or 0
        output_tokens += row.get("output_tokens") or 0
        cache_read_tokens += row.get("cache_read_tokens") or 0
        cache_write_tokens += row.get("cache_write_tokens") or 0
        reasoning_tokens += row.get("reasoning_tokens") or 0
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
    filtered = [row for row in rows if (row.get("started_at") or 0.0) >= cutoff]
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
        label = str(row.get(key_name) or "unknown")
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
    return (
        str(row.get("cost_status") or "") in AUTHORITATIVE_COST_STATUSES
        and row.get("estimated_cost_usd") is not None
    )


def _resolved_session_cost(row: dict[str, Any]) -> float:
    actual_cost = _coerce_float(row.get("actual_cost_usd"))
    if actual_cost > 0:
        return actual_cost
    raw_cost = row.get("estimated_cost_usd")
    cost = _coerce_float(raw_cost)
    if _session_cost_is_reported(row):
        return cost
    if cost:
        return cost
    return _estimate_cost(
        row.get("input_tokens") or 0,
        row.get("output_tokens") or 0,
        row.get("cache_read_tokens") or 0,
        row.get("reasoning_tokens") or 0,
        row.get("cache_write_tokens") or 0,
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

    A session file the index names but that no longer exists simply has no
    tools. A file that exists but cannot be decoded raises instead: silently
    treating it as empty shrinks the reported tool inventory, where raising
    fails the tools source and keeps the last-good inventory — the same
    handling a symlinked session file already gets.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
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
