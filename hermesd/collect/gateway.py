"""Gateway liveness: heartbeat, lifecycle, config generation, updates, ledgers.

Every reader here is pure: it takes already-loaded JSON (via the collector's
last-good file cache), an injected clock, and — where liveness depends on the
host — an injected ``pid_exists``. Nothing in this module opens a file for
writing or imports hermes-agent.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermesd.collect.common import (
    _age_seconds,
    _as_dict,
    _as_list,
    _coerce_float,
    _coerce_int,
    _iso_to_epoch,
)
from hermesd.collect.sqlite_util import (
    _count_by,
    _query_rows,
    _table_count_or_zero,
    _table_exists,
)
from hermesd.file_cache import JsonMapping
from hermesd.models import (
    ConfigSourceStamp,
    DeliveryObligationSummary,
    GatewayLoopHealth,
    PlatformStatus,
)

# The gateway watchdog rewrites state/gateway.heartbeat every 30s: three missed
# writes is stale, ten is a wedged event loop.
_HEARTBEAT_TICKING_SECONDS = 90.0
_HEARTBEAT_STALE_SECONDS = 300.0
_DAY_SECONDS = 86400.0
# Undelivered obligations still in flight; "delivered" is done and "failed" is
# counted separately.
_PENDING_DELIVERY_STATES = ("pending", "attempting")
_DELIVERY_ERROR_EXCERPT_CHARS = 80
# Bound on the restart history scanned per pass; state.db is large and only the
# newest incarnations matter for uptime and the 24h restart count.
_INCARNATION_SCAN_LIMIT = 500
_OPEN_DELIVERY_LIMIT = 5
# A recorded start_time is only usable as wall-clock when it lands inside this
# window of now. The live gateway_state.json carries a monotonic-clock value
# (178874708938, i.e. the year 7638), which must never be read as an epoch.
_PLAUSIBLE_EPOCH_WINDOW_SECONDS = 50 * 365 * _DAY_SECONDS


def _optional_int(value: object) -> int | None:
    """Coerce to int, preserving a genuine null (an exit code that never happened)."""
    return None if value is None else _coerce_int(value)


def _platform_status(name: str, info: dict[str, Any], now: float) -> PlatformStatus:
    retrying_since = str(info.get("retrying_since") or "")
    return PlatformStatus(
        name=name,
        state=str(info.get("state") or "unknown"),
        updated_at=str(info.get("updated_at") or ""),
        error_code=str(info.get("error_code") or ""),
        error_message=str(info.get("error_message") or ""),
        needs_attention=bool(info.get("needs_attention")),
        retrying_since=retrying_since,
        retrying_since_age_seconds=_age_seconds(_iso_to_epoch(retrying_since), now),
    )


def _heartbeat_liveness(
    data: JsonMapping,
    file_mtime: float | None,
    now: float,
    *,
    running: bool,
) -> tuple[float | None, GatewayLoopHealth]:
    """Event-loop liveness from the watchdog heartbeat; file mtime is the fallback clock."""
    stamp = _iso_to_epoch(data.get("updated_at")) if data else None
    age = _age_seconds(stamp if stamp is not None else file_mtime, now)
    if age is None:
        return None, GatewayLoopHealth.UNKNOWN
    if age <= _HEARTBEAT_TICKING_SECONDS:
        return age, GatewayLoopHealth.TICKING
    if age <= _HEARTBEAT_STALE_SECONDS:
        return age, GatewayLoopHealth.STALE
    # A long-silent heartbeat is only "wedged" while the gateway claims to run;
    # otherwise it is just an old file left by a stopped gateway.
    return age, GatewayLoopHealth.WEDGED if running else GatewayLoopHealth.STALE


@dataclass(frozen=True, slots=True)
class _LifecycleStatus:
    phase: str = ""
    last_exit_code: int | None = None
    last_exit_reason: str = ""
    unclean_previous_exit: bool = False


def _lifecycle_status(data: JsonMapping, pid_exists: Callable[[int], bool]) -> _LifecycleStatus:
    """A ``running`` phase whose pid is gone means the previous life never wrote an exit."""
    if not data:
        return _LifecycleStatus()
    phase = str(data.get("phase") or "")
    pid = _coerce_int(data.get("pid"))
    return _LifecycleStatus(
        phase=phase,
        last_exit_code=_optional_int(data.get("exit_code")),
        last_exit_reason=str(data.get("exit_reason") or ""),
        unclean_previous_exit=phase == "running" and pid > 0 and not pid_exists(pid),
    )


@dataclass(frozen=True, slots=True)
class _ConfigGeneration:
    """The gateway's recorded config fingerprint and source stamps.

    Informational only: no current hermes-agent writes ``config_generation``,
    so the recorded per-source mtimes are orphan data and are never compared
    against the live files (see ``_config_stale``).
    """

    fingerprint: str = ""
    short: str = ""
    sources: list[ConfigSourceStamp] = field(default_factory=list)


def _config_generation(data: JsonMapping) -> _ConfigGeneration:
    """Config files the running gateway recorded that it loaded."""
    raw = _as_dict(data.get("config_generation"))
    if not raw:
        return _ConfigGeneration()
    sources = [
        ConfigSourceStamp(
            name=str(info.get("name") or ""),
            path=str(info.get("path") or ""),
            exists=bool(info.get("exists")),
            mtime_ns=_coerce_int(info.get("mtime_ns")),
            size=_coerce_int(info.get("size")),
        )
        for entry in _as_list(raw.get("sources"))
        if (info := _as_dict(entry))
    ]
    return _ConfigGeneration(
        fingerprint=str(raw.get("fingerprint") or ""),
        short=str(raw.get("short") or ""),
        sources=sources,
    )


def _plausible_epoch(value: object, now: float) -> float | None:
    """Wall-clock epoch for `value`, or None when it cannot be one.

    Accepts an ISO-8601 string or a numeric epoch, and rejects anything more
    than _PLAUSIBLE_EPOCH_WINDOW_SECONDS from `now` — hermes-agent records a
    monotonic clock reading under the same ``start_time`` key.
    """
    epoch = _iso_to_epoch(value)
    if epoch is None:
        if isinstance(value, str) or value is None:
            return None
        epoch = _coerce_float(value)
    if epoch <= 0 or abs(epoch - now) > _PLAUSIBLE_EPOCH_WINDOW_SECONDS:
        return None
    return epoch


def _gateway_start_epoch(
    heartbeat: JsonMapping,
    lifecycle: JsonMapping,
    state: JsonMapping,
    now: float,
) -> float | None:
    """When the running gateway started, from the first source carrying a usable stamp."""
    for data in (heartbeat, lifecycle, state):
        epoch = _plausible_epoch(data.get("start_time"), now)
        if epoch is not None:
            return epoch
    return None


def _config_stale(config_path: Path, root: Path, start_epoch: float | None) -> bool:
    """True when config.yaml was written after the running gateway started.

    ``config_path`` is stat'd through symlinks on purpose: dotfiles setups link
    ``~/.hermes/config.yaml`` at a repository copy, and that is still the file
    the gateway loads. Only the lexical location is confined to `root`.
    """
    if start_epoch is None:
        return False
    if not config_path.is_relative_to(root):
        return False
    try:
        return config_path.stat().st_mtime > start_epoch
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class _UpdateReceipt:
    outcome: str = ""
    finished_age_seconds: float | None = None
    from_version: str = ""
    to_version: str = ""
    failed_step: str = ""
    runtime_code_skew: bool = False


def _update_receipt_status(data: JsonMapping, now: float, code_sha: str) -> _UpdateReceipt:
    if not data:
        return _UpdateReceipt()
    plan = _as_dict(data.get("plan"))
    return _UpdateReceipt(
        outcome=str(data.get("outcome") or ""),
        finished_age_seconds=_age_seconds(_iso_to_epoch(data.get("finished_at")), now),
        from_version=str(_as_dict(data.get("pre_update")).get("version") or ""),
        to_version=str(_as_dict(data.get("post_update")).get("version") or ""),
        failed_step=_first_failed_step(data.get("steps")),
        runtime_code_skew=_runtime_code_skew(plan.get("runtimes"), code_sha),
    )


def _first_failed_step(steps: object) -> str:
    """Name of the first step that recorded ok=false; a missing ok is not a failure."""
    for entry in _as_list(steps):
        info = _as_dict(entry)
        if "ok" in info and not info["ok"]:
            return str(info.get("name") or "")
    return ""


def _runtime_code_skew(runtimes: object, code_sha: str) -> bool:
    """A runtime pinned to a different non-empty sha than the gateway is skewed."""
    if not code_sha:
        return False
    return any(
        (sha := str(_as_dict(entry).get("code_sha") or "")) and sha != code_sha
        for entry in _as_list(runtimes)
    )


@dataclass(frozen=True, slots=True)
class _GatewayLedgerRows:
    """Raw state.db ledger data; ages are derived per tick from the live clock."""

    incarnation_count: int = 0
    incarnation_starts: list[float] = field(default_factory=list)
    delivery_counts: dict[str, int] = field(default_factory=dict)
    delivery_rows: list[dict[str, Any]] = field(default_factory=list)


def _read_gateway_ledger_rows(conn: sqlite3.Connection) -> _GatewayLedgerRows:
    """Read the restart history and delivery obligations. Both tables may be absent."""
    return _GatewayLedgerRows(
        incarnation_count=_table_count_or_zero(conn, "gateway_heartbeats"),
        incarnation_starts=_read_incarnation_starts(conn),
        delivery_counts=_read_delivery_counts(conn),
        delivery_rows=_read_open_delivery_rows(conn),
    )


def _read_incarnation_starts(conn: sqlite3.Connection) -> list[float]:
    if not _table_exists(conn, "gateway_heartbeats"):
        return []
    rows = _query_rows(
        conn,
        "SELECT started_at FROM gateway_heartbeats "
        f"ORDER BY started_at DESC LIMIT {_INCARNATION_SCAN_LIMIT}",
    )
    starts = [_coerce_float(row.get("started_at") or 0.0) for row in rows]
    return [start for start in starts if start > 0]


def _read_delivery_counts(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "delivery_obligations"):
        return {}
    return _count_by(conn, "SELECT state, COUNT(*) FROM delivery_obligations GROUP BY state")


def _read_open_delivery_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Newest undelivered obligations. ``content`` is deliberately never selected."""
    if not _table_exists(conn, "delivery_obligations"):
        return []
    return _query_rows(
        conn,
        "SELECT platform, state, attempts, created_at, updated_at, last_error "
        "FROM delivery_obligations "
        "WHERE state IS NULL OR state <> 'delivered' "
        f"ORDER BY COALESCE(updated_at, created_at, 0) DESC LIMIT {_OPEN_DELIVERY_LIMIT}",
    )


def _gateway_ledger_fields(rows: _GatewayLedgerRows, now: float) -> dict[str, Any]:
    starts = rows.incarnation_starts
    counts = rows.delivery_counts
    return {
        "gateway_incarnation_count": rows.incarnation_count,
        "gateway_restarts_24h": sum(1 for start in starts if now - start <= _DAY_SECONDS),
        "current_incarnation_uptime_seconds": _age_seconds(max(starts) if starts else None, now),
        "pending_delivery_count": sum(counts.get(state) or 0 for state in _PENDING_DELIVERY_STATES),
        "failed_delivery_count": counts.get("failed") or 0,
        "pending_deliveries": [_delivery_summary(row, now) for row in rows.delivery_rows],
    }


def _delivery_summary(row: dict[str, Any], now: float) -> DeliveryObligationSummary:
    timestamp = _coerce_float(row.get("updated_at") or row.get("created_at") or 0.0)
    return DeliveryObligationSummary(
        platform=str(row.get("platform") or ""),
        state=str(row.get("state") or ""),
        attempts=_coerce_int(row.get("attempts") or 0),
        age_seconds=_age_seconds(timestamp or None, now),
        last_error=_error_excerpt(row.get("last_error") or ""),
    )


def _error_excerpt(value: object) -> str:
    """Collapse whitespace and cap an untrusted error string to a cell-sized excerpt."""
    return " ".join(str(value).split())[:_DELIVERY_ERROR_EXCERPT_CHARS]
