"""Cron job output discovery, excerpts, execution history and ticker health."""

from __future__ import annotations

import contextlib
import functools
import heapq
import json
import math
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from hermesd.collect.common import (
    _EXCERPT_MAX_CHARS,
    _MAX_TEXT_READ_BYTES,
    _age_seconds,
    _as_dict,
    _coerce_bool,
    _coerce_float,
    _coerce_int,
    _exists_strict,
    _file_size,
    _iso_to_epoch,
    _mtime,
    _open_regular_file,
    _optional_int,
    _path_resolves_under,
    _read_tail_text,
    _read_text_capped,
    _safe_child_path,
    _safe_mtime,
)
from hermesd.collect.logs import _MAX_LOG_LINE_CHARS
from hermesd.collect.redaction import _redact_secret_text
from hermesd.collect.sqlite_util import (
    _column_exists,
    _connect_readonly_sqlite,
    _count_rows,
    _query_rows,
    _table_exists,
)
from hermesd.models import (
    CronBotChatReceipt,
    CronBotChatState,
    CronDeliveryFailure,
    CronDeliveryQueueState,
    CronExecution,
    CronExecutionsState,
    CronFireClaimState,
    CronIncident,
    CronJobExecutionStats,
    CronJobUsage,
    CronModelSource,
    CronTickerHealth,
    CronUsageState,
    LogLine,
)

# executions.db grows without bound (1k rows on a month-old home). The 24h
# counters aggregate the full window in SQL and each job's last run comes from
# a per-job chronological ranking, so a busy job cannot crowd another job
# out of either; only the displayed history is fetched, capped at
# _EXECUTIONS_RECENT_LIMIT rows.
_EXECUTIONS_RECENT_LIMIT = 10
_EXECUTIONS_WINDOW_SECONDS = 24 * 60 * 60.0
_INCIDENTS_LIMIT = 5
_RECENT_FAILURES_LIMIT = 5

# The ticker fires every 60s. Two missed beats means stale; a heartbeat that
# keeps arriving while last_success falls ten beats behind means failing.
_TICKER_HEARTBEAT_STALE_SECONDS = 120.0
_TICKER_LAST_SUCCESS_STALE_SECONDS = 600.0

# Markers in the cron store written by upstream's best-effort ``_write_marker``
# (``cron/jobs.py:1129-1136``), which swallows every exception — so a marker can
# be missing for purely benign reasons and absence must never read as "no
# failures". Both resolve through ``_current_cron_store()`` upstream, i.e. the
# profile-local store; hermesd reads them from the root store with the rest of
# ``cron`` (see ``.codex/rules/source-ownership.md``).
_CATCH_UP_OCCURRENCES_MARKER = "catch_up_occurrences"
_TICKER_ERROR_MARKER = "ticker_last_error"
# ``ticker_last_error`` is ``f"{time.time()}\n{message}\n"``; upstream refuses a
# file with fewer than two lines because a torn write can leave the stamp alone
# (``get_ticker_last_error``, ``cron/jobs.py:1212-1220``).
_TICKER_ERROR_MIN_LINES = 2
# ``config.yaml`` key deciding whether a recurring run missed beyond the grace
# window is caught up or silently skipped (``cron/jobs.py:2908``).
_CATCH_UP_MISSED_KEY = "catch_up_missed"
# Per-run output files are named ``%Y-%m-%d_%H-%M-%S.md``, and upstream itself
# orders them newest-first by reverse name when pruning to ``output_retention``
# (50 by default; non-positive disables pruning, ``cron/jobs.py:2040-2087``).
# Each refresh therefore lists a bounded number of entries and stats only the
# lexically newest few, instead of stat'ing an unpruned directory whole.
_CRON_OUTPUT_LIST_LIMIT = 10_000
# Memoized form of the shared ISO parser for the executions.db SQL function.
_memo_iso_to_epoch = functools.lru_cache(maxsize=8192)(_iso_to_epoch)
_CRON_OUTPUT_STAT_LIMIT = 50


def _truncate_lines(text: str) -> list[str]:
    """Split cron output into lines, each capped like a log line.

    A single unbounded line would otherwise be scanned whole by the secret
    redactor and the [SILENT] probe on every refresh.
    """
    return [line[:_MAX_LOG_LINE_CHARS] for line in text.splitlines()]


def _latest_cron_output_file(
    output_root: Path,
    job_id: str,
    *,
    stop_at: Path | None = None,
) -> Path | None:
    """Locate the newest cron output file for job_id without reading it."""
    if not job_id:
        return None
    job_output_dir = output_root / job_id
    output_root_escaped = stop_at is not None and not _path_resolves_under(output_root, stop_at)
    job_output_dir_escaped = not _path_resolves_under(job_output_dir, output_root)
    if output_root_escaped or job_output_dir_escaped or not job_output_dir.is_dir():
        return None
    files = []
    for path in _newest_named_outputs(job_output_dir):
        try:
            if not path.is_symlink() and path.is_file():
                files.append(path)
        except OSError:
            continue
    if not files:
        return None
    return max(files, key=_safe_mtime)


def _newest_named_outputs(job_output_dir: Path) -> list[Path]:
    """The lexically newest entries of one job's output dir; [] when unreadable.

    Only these are stat'ed. An unreadable directory reads as no output, which
    the Collector turns into its cached excerpt rather than failing every job.
    """
    try:
        return heapq.nlargest(
            _CRON_OUTPUT_STAT_LIMIT,
            islice(job_output_dir.iterdir(), _CRON_OUTPUT_LIST_LIMIT),
            key=lambda path: path.name,
        )
    except OSError:
        return []


def _latest_cron_output_excerpt(
    output_root: Path,
    job_id: str,
    max_bytes: int,
    *,
    stop_at: Path | None = None,
) -> tuple[str, bool, str, float | None]:
    latest = _latest_cron_output_file(output_root, job_id, stop_at=stop_at)
    if latest is None:
        return "", False, "", None
    latest_mtime = _mtime(latest)
    try:
        lines = _truncate_lines(_read_tail_text(latest, max_bytes))
    except OSError:
        return "", False, "", None
    silent = any("[SILENT]" in line.upper() for line in lines)
    for line in lines:
        stripped = line.strip()
        if stripped and "[SILENT]" not in stripped.upper():
            return stripped[:_EXCERPT_MAX_CHARS], silent, latest.name, latest_mtime
    return "", silent, latest.name, latest_mtime


def _tail_latest_cron_output(
    output_root: Path,
    max_lines: int,
    max_bytes: int,
    *,
    stop_at: Path | None = None,
) -> list[LogLine]:
    output_root_escaped = stop_at is not None and not _path_resolves_under(output_root, stop_at)
    if output_root_escaped or not output_root.is_dir():
        return []
    latest_file: Path | None = None
    latest_mtime = 0.0
    try:
        job_dirs = list(islice(output_root.iterdir(), _CRON_OUTPUT_LIST_LIMIT))
    except OSError:
        return []
    for job_dir in job_dirs:
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        for path in _newest_named_outputs(job_dir):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
            except OSError:
                continue
            mtime = _safe_mtime(path)
            if mtime > latest_mtime:
                latest_file = path
                latest_mtime = mtime
    if latest_file is None:
        return []
    try:
        lines = _truncate_lines(_read_tail_text(latest_file, max_bytes))[-max_lines:]
    except OSError:
        return []
    return [LogLine(message=_redact_secret_text(line.strip())) for line in lines if line.strip()]


def _cron_suggestion_count(cron_dir: Path) -> int:
    candidates = [
        cron_dir / "suggestions.json",
        cron_dir / "cron_suggestions.json",
        cron_dir / "suggestions",
    ]
    total = 0
    for path in candidates:
        if path.is_symlink() or not _path_resolves_under(path, cron_dir.parent):
            continue
        if path.is_file():
            # Nesting deep enough to exhaust the decoder is just more junk.
            with contextlib.suppress(json.JSONDecodeError, RecursionError):
                data = json.loads(_read_text_capped(path, cron_dir.parent))
                total += _suggestion_count_from_data(data)
        elif path.is_dir():
            with contextlib.suppress(OSError):
                total += sum(
                    1
                    for child in path.iterdir()
                    if child.is_file()
                    and not child.is_symlink()
                    and child.suffix.lower() in {".json", ".yaml", ".yml", ".md"}
                    and _path_resolves_under(child, cron_dir.parent)
                )
    return total


def _suggestion_count_from_data(data: object) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("suggestions", "items", "jobs"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
        return len(data)
    return 0


def _chronos_configured(cfg: dict[str, Any]) -> bool:
    return bool(
        cfg.get("portal_url")
        and cfg.get("callback_url")
        and cfg.get("expected_audience")
        and cfg.get("nas_jwks_url")
    )


def _delivery_target_label(directory: dict[str, Any], deliver: str) -> str:
    if not deliver:
        return ""
    if deliver in {"local", "origin"}:
        return deliver
    if ":" not in deliver:
        return deliver

    platform, target = deliver.split(":", 1)
    entries = _as_dict(directory.get("platforms")).get(platform)
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("name") or "") == target:
                return f"{platform}:{target}"
    return deliver


def _cron_error_excerpt(error: str) -> str:
    """First non-blank line of an execution/incident error, redacted and capped."""
    for line in error.splitlines():
        stripped = line.strip()
        if stripped:
            return _redact_secret_text(stripped)[:_EXCERPT_MAX_CHARS]
    return ""


def _execution_duration(started_at: str, finished_at: str) -> float | None:
    started = _iso_to_epoch(started_at)
    finished = _iso_to_epoch(finished_at)
    if started is None or finished is None:
        return None
    return max(0.0, finished - started)


def _claimed_sort_key(row: dict[str, Any]) -> float:
    """Claim epoch for newest-first ordering; unparseable stamps sort last."""
    claimed = _memo_iso_to_epoch(str(row.get("claimed_at") or ""))
    return -math.inf if claimed is None else claimed


def _execution_from_row(
    row: dict[str, Any],
    job_names: Mapping[str, str],
    *,
    now: float,
) -> CronExecution:
    job_id = str(row.get("job_id") or "")
    started_at = str(row.get("started_at") or "")
    return CronExecution(
        execution_id=str(row.get("id") or ""),
        job_id=job_id,
        job_name=job_names.get(job_id) or job_id,
        status=str(row.get("status") or ""),
        started_age_seconds=_age_seconds(_iso_to_epoch(started_at), now),
        duration_seconds=_execution_duration(started_at, str(row.get("finished_at") or "")),
        error_excerpt=_cron_error_excerpt(str(row.get("error") or "")),
        delivery_outcome=str(row.get("delivery_outcome") or ""),
        scheduled_instant=str(row.get("scheduled_instant") or ""),
        # Machine-written column: a text 'false' survives INTEGER affinity
        # and bool() would call the handoff pending.
        handoff_pending=_coerce_bool(row.get("handoff_pending")),
    )


def _job_execution_stats(
    window_rows: list[dict[str, Any]],
    last_rows: list[dict[str, Any]],
    delivery_rows: list[dict[str, Any]],
    *,
    delivery_tracked: bool,
) -> list[CronJobExecutionStats]:
    """Per-job 24h counters from SQL aggregates plus last-run detail.

    `last_rows` carries each job's newest execution from the full table, so a
    busy job cannot crowd another job's last-run status off the panel.
    `delivery_rows` keeps delivery in its own counters, because a completed
    execution whose notification was suppressed was not delivered.
    """
    stats: dict[str, CronJobExecutionStats] = {}
    for row in sorted(last_rows, key=_claimed_sort_key, reverse=True):
        job_id = str(row.get("job_id") or "")
        started_at = str(row.get("started_at") or "")
        stats[job_id] = CronJobExecutionStats(
            job_id=job_id,
            last_status=str(row.get("status") or ""),
            last_duration_seconds=_execution_duration(
                started_at, str(row.get("finished_at") or "")
            ),
            last_error_excerpt=_cron_error_excerpt(str(row.get("error") or "")),
        )
    for row in window_rows:
        job_id = str(row.get("job_id") or "")
        if job_id not in stats:
            stats[job_id] = CronJobExecutionStats(job_id=job_id)
        entry = stats[job_id]
        entry.total_24h = int(row.get("total_24h") or 0)
        entry.completed_24h = int(row.get("completed_24h") or 0)
        entry.failed_24h = int(row.get("failed_24h") or 0)
        entry.running_24h = int(row.get("running_24h") or 0)
        entry.unknown_24h = int(row.get("unknown_24h") or 0)
    for row in delivery_rows:
        job_id = str(row.get("job_id") or "")
        if job_id not in stats:
            stats[job_id] = CronJobExecutionStats(job_id=job_id)
        entry = stats[job_id]
        entry.handoff_pending_24h += int(row.get("handoff_pending") or 0)
        outcome = str(row.get("delivery_outcome") or "")
        count = int(row.get("row_count") or 0)
        if not outcome:
            entry.delivery_unrecorded_24h += count
        elif (
            outcome in entry.delivery_outcomes_24h
            or len(entry.delivery_outcomes_24h) < _DELIVERY_OUTCOME_KIND_LIMIT
        ):
            outcomes = entry.delivery_outcomes_24h
            outcomes[outcome] = outcomes.get(outcome, 0) + count
    result = list(stats.values())
    for entry in result:
        entry.delivery_tracked = delivery_tracked
    return result


# Columns every executions read relies on; an executions table without them is
# a foreign schema (older/newer agent), which degrades to empty instead of
# failing the source.
_EXECUTIONS_REQUIRED_COLUMNS = (
    "id",
    "job_id",
    "status",
    "claimed_at",
    "started_at",
    "finished_at",
    "error",
)

# The status vocabulary hermes-agent writes (its own schema CHECK constraint).
# Anything outside it — including a NULL or a value added by a newer agent — is
# counted as unknown rather than dropped or guessed at.
_EXECUTIONS_KNOWN_STATUSES = ("completed", "failed", "running", "claimed")

# Delivery, handoff and scheduled-occurrence columns were added to the executions
# schema after the base ones. An older agent has none of them, so every read that
# touches them is column-aware and degrades to "not recorded" rather than failing.
_EXECUTIONS_OPTIONAL_COLUMNS = ("delivery_outcome", "handoff_pending", "scheduled_instant")
# Bound on the distinct delivery outcomes retained per job; the count of rows is
# never bounded, only the vocabulary, so an untrusted value cannot grow the map.
_DELIVERY_OUTCOME_KIND_LIMIT = 8

# Upstream's MAX_TERMINAL_EXECUTIONS: terminal history is pruned to this many
# records, keeping the newest by finished_at. In-flight rows are never pruned.
_MAX_TERMINAL_EXECUTIONS = 1000
# The statuses upstream treats as terminal for that pruning. 'unknown' counts,
# which is why it has to be a reported bucket rather than a discarded one.
_EXECUTIONS_TERMINAL_STATUSES = ("completed", "failed", "unknown")


def _executions_columns(conn: sqlite3.Connection) -> set[str]:
    """Column names of the executions table, introspected once per read pass.

    An absent table legitimately yields no columns; a present-but-unreadable one
    raises so the source fails to its last-good value instead of silently
    degrading every column-aware query to empty results.
    """
    if not _table_exists(conn, "executions"):
        return set()
    return {str(row[1] or "") for row in conn.execute("PRAGMA table_info(executions)")}


def _executions_schema_compatible(columns: set[str]) -> bool:
    """True when every column the base reads rely on is present."""
    return set(_EXECUTIONS_REQUIRED_COLUMNS).issubset(columns)


def _execution_select_columns(columns: set[str]) -> str:
    """The execution columns to select, adding the newer optional ones present."""
    selected = list(_EXECUTIONS_REQUIRED_COLUMNS)
    selected.extend(name for name in _EXECUTIONS_OPTIONAL_COLUMNS if name in columns)
    return ", ".join(selected)


def _execution_rows(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    *,
    columns: set[str],
) -> list[dict[str, Any]]:
    if not _executions_schema_compatible(columns):
        return []
    # Keep SQL ordering and cutoffs identical to the shared ISO parser, including
    # offsets, naive UTC timestamps, fractional seconds, and malformed values.
    # The UDF defeats any index on claimed_at (full scan per poll); acceptable at
    # the expected executions.db scale. Three queries per poll parse the same
    # unchanging claimed_at strings, so the parser is memoized.
    conn.create_function("hermes_epoch", 1, _memo_iso_to_epoch, deterministic=True)
    return _query_rows(conn, sql, params)


def _recent_execution_rows(conn: sqlite3.Connection, *, columns: set[str]) -> list[dict[str, Any]]:
    """The newest _EXECUTIONS_RECENT_LIMIT executions, [] on a missing/foreign schema.

    Ordered in SQL by the same parser as every other cutoff (``hermes_epoch``),
    newest claim first with ``id`` breaking ties; unparseable stamps sort last.

    Operational read errors propagate so the cron_executions source fails to
    its last-good value instead of reporting a false empty history.
    """
    # The LIMIT is a module-level int constant, never caller-supplied text.
    return _execution_rows(
        conn,
        f"SELECT {_execution_select_columns(columns)} "
        "FROM executions ORDER BY hermes_epoch(claimed_at) DESC, id DESC "
        f"LIMIT {_EXECUTIONS_RECENT_LIMIT}",
        columns=columns,
    )


def _recent_failure_rows(conn: sqlite3.Connection, *, columns: set[str]) -> list[dict[str, Any]]:
    """The newest _RECENT_FAILURES_LIMIT failed executions, newest claim first."""
    # The LIMIT is a module-level int constant, never caller-supplied text.
    return _execution_rows(
        conn,
        f"SELECT {_execution_select_columns(columns)} "
        "FROM executions WHERE status = 'failed' "
        "ORDER BY hermes_epoch(claimed_at) DESC, id DESC "
        f"LIMIT {_RECENT_FAILURES_LIMIT}",
        columns=columns,
    )


def _execution_window_rows(
    conn: sqlite3.Connection, *, now: float, columns: set[str]
) -> list[dict[str, Any]]:
    """Aggregate the complete 24h window, returning one row per job.

    ``unknown_24h`` is the complement of the three recognized buckets, so an
    ``unknown`` status — a real upstream terminal state — or any value hermesd has
    not seen is reported instead of vanishing from the summary. ``total_24h`` is
    the denominator the buckets must reconcile against.
    """
    known = ", ".join(f"'{status}'" for status in _EXECUTIONS_KNOWN_STATUSES)
    return _execution_rows(
        conn,
        "SELECT COALESCE(job_id, '') AS job_id, "
        "COUNT(*) AS total_24h, "
        "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_24h, "
        "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_24h, "
        "SUM(CASE WHEN status IN ('running', 'claimed') THEN 1 ELSE 0 END) AS running_24h, "
        f"SUM(CASE WHEN status IS NULL OR status NOT IN ({known}) "
        "THEN 1 ELSE 0 END) AS unknown_24h "
        "FROM executions WHERE hermes_epoch(claimed_at) BETWEEN ? AND ? "
        "GROUP BY COALESCE(job_id, '')",
        (now - _EXECUTIONS_WINDOW_SECONDS, now),
        columns=columns,
    )


def _execution_delivery_rows(
    conn: sqlite3.Connection, *, now: float, columns: set[str]
) -> list[dict[str, Any]]:
    """Per-job delivery outcomes and pending handoffs inside the 24h window.

    Delivery is a separate question from execution: a run can complete and still
    have its notification suppressed, so a completed counter must never be read
    as "delivered". A schema without either column yields no rows, which the
    caller reports as untracked rather than as a window of unrecorded deliveries.
    """
    has_outcome = "delivery_outcome" in columns
    has_handoff = "handoff_pending" in columns
    if not has_outcome and not has_handoff:
        return []
    outcome = "COALESCE(delivery_outcome, '')" if has_outcome else "''"
    handoff = "SUM(COALESCE(handoff_pending, 0))" if has_handoff else "0"
    return _execution_rows(
        conn,
        "SELECT COALESCE(job_id, '') AS job_id, "
        f"{outcome} AS delivery_outcome, "
        f"{handoff} AS handoff_pending, "
        "COUNT(*) AS row_count "
        "FROM executions WHERE hermes_epoch(claimed_at) BETWEEN ? AND ? "
        f"GROUP BY COALESCE(job_id, ''), {outcome}",
        (now - _EXECUTIONS_WINDOW_SECONDS, now),
        columns=columns,
    )


def _execution_retention_fields(
    conn: sqlite3.Connection, *, now: float, columns: set[str]
) -> dict[str, Any]:
    """Recorded-attempt counts plus the observed span of retained history.

    Upstream prunes terminal executions to ``_MAX_TERMINAL_EXECUTIONS`` records,
    so these qualify every aggregate as *recorded* attempts. Reaching the cap
    proves older terminal rows were dropped; it does not prove any particular 24h
    window is incomplete, and staying under it does not prove full coverage.
    """
    terminal_statuses = ", ".join(f"'{status}'" for status in _EXECUTIONS_TERMINAL_STATUSES)
    rows = _execution_rows(
        conn,
        "SELECT COUNT(*) AS total, "
        f"SUM(CASE WHEN status IN ({terminal_statuses}) THEN 1 ELSE 0 END) AS terminal, "
        "MIN(hermes_epoch(claimed_at)) AS oldest_epoch, "
        "MAX(hermes_epoch(claimed_at)) AS newest_epoch "
        "FROM executions",
        columns=columns,
    )
    if not rows:
        return {}
    row = rows[0]
    terminal = int(row.get("terminal") or 0)
    return {
        "retained_total_count": int(row.get("total") or 0),
        "retained_terminal_count": terminal,
        "retention_cap": _MAX_TERMINAL_EXECUTIONS,
        "at_retention_cap": terminal >= _MAX_TERMINAL_EXECUTIONS,
        "oldest_claimed_age_seconds": _age_seconds(row.get("oldest_epoch"), now),
        "newest_claimed_age_seconds": _age_seconds(row.get("newest_epoch"), now),
    }


def _last_execution_rows(conn: sqlite3.Connection, *, columns: set[str]) -> list[dict[str, Any]]:
    """Each job's newest execution (claimed_at, then id), one row per job."""
    select = _execution_select_columns(columns)
    return _execution_rows(
        conn,
        f"SELECT {select} FROM ("
        f"SELECT {select}, "
        "ROW_NUMBER() OVER (PARTITION BY COALESCE(job_id, '') "
        "ORDER BY hermes_epoch(claimed_at) DESC, id DESC) AS run_rank "
        "FROM executions) WHERE run_rank = 1",
        columns=columns,
    )


def _incident_from_row(
    row: dict[str, Any],
    job_names: Mapping[str, str],
    *,
    now: float,
) -> CronIncident:
    job_id = str(row.get("job_id") or "")
    return CronIncident(
        incident_id=str(row.get("id") or ""),
        job_id=job_id,
        job_name=job_names.get(job_id) or job_id,
        state=str(row.get("state") or ""),
        failure_type=str(row.get("failure_type") or ""),
        first_seen_age_seconds=_age_seconds(
            _iso_to_epoch(str(row.get("first_seen_at") or "")), now
        ),
        last_seen_age_seconds=_age_seconds(_iso_to_epoch(str(row.get("last_seen_at") or "")), now),
        alerted_age_seconds=_age_seconds(_iso_to_epoch(str(row.get("alerted_at") or "")), now),
        error_excerpt=_cron_error_excerpt(str(row.get("error") or "")),
    )


def _read_cron_incidents(
    conn: sqlite3.Connection,
    job_names: Mapping[str, str],
    *,
    now: float,
) -> dict[str, Any]:
    """Open/unacked/resolved incident counts plus the latest few open incidents.

    A missing cron_incidents table (older agents) reads as zeros; operational
    read errors propagate so the source fails to its last-good value.
    """
    if not _table_exists(conn, "cron_incidents"):
        return {}
    # Lifecycle is detected -> alerted -> resolved | closed
    # (``INCIDENT_STATES``, ``cron/incidents.py:32``). ``resolved`` is the
    # automatic transition after a successful run and stamps ``closed_at``
    # (``:233-248``); a repeat of the same error re-opens it as ``detected``
    # (``:151-181``). ``closed`` is the operator's ack, and ``acked_at`` is
    # written only by that transition, together with ``closed_at`` (``:196-230``):
    # upstream has no acknowledge-without-close. So for data this schema
    # produces, every open incident is unacked — the separate counter is kept
    # because a foreign/newer schema may diverge. Open is filtered on state as
    # well as ``closed_at`` so a resolved row can never read as open.
    open_clause = "WHERE COALESCE(state, '') NOT IN ('closed', 'resolved') AND closed_at IS NULL"
    open_count = _count_rows(conn, f"SELECT COUNT(*) FROM cron_incidents {open_clause}")
    unacked_count = _count_rows(
        conn,
        f"SELECT COUNT(*) FROM cron_incidents {open_clause} AND acked_at IS NULL",
    )
    # ``alerted_at`` was added in place for older ledgers (``:83,90``).
    has_alerted = _column_exists(conn, "cron_incidents", "alerted_at")
    alerted = "alerted_at" if has_alerted else "NULL AS alerted_at"
    rows = _query_rows(
        conn,
        f"SELECT id, job_id, state, failure_type, first_seen_at, last_seen_at, {alerted}, error "
        f"FROM cron_incidents {open_clause} ORDER BY last_seen_at DESC, id DESC "
        f"LIMIT {_INCIDENTS_LIMIT}",
    )
    conn.create_function("hermes_epoch", 1, _memo_iso_to_epoch, deterministic=True)
    resolved = _query_rows(
        conn,
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN hermes_epoch(closed_at) >= ? THEN 1 ELSE 0 END) AS recent "
        "FROM cron_incidents WHERE state = 'resolved'",
        (now - _EXECUTIONS_WINDOW_SECONDS,),
    )[0]
    return {
        "open_incident_count": open_count,
        "unacked_incident_count": unacked_count,
        "open_incidents": [_incident_from_row(row, job_names, now=now) for row in rows],
        "resolved_incident_count": int(resolved.get("total") or 0),
        "resolved_24h_count": int(resolved.get("recent") or 0),
    }


def _read_cron_executions_state(
    db_path: Path,
    job_names: Mapping[str, str],
    *,
    now: float,
    root: Path,
) -> CronExecutionsState:
    """Cron/executions.db: full-window 24h counters, capped recent runs, incidents.

    `root` confines the database: checking only ``db_path.is_symlink()`` misses
    a symlinked ``cron/`` directory pointing outside the Hermes home.
    """
    if not _safe_child_path(db_path, root) or not db_path.is_file():
        return CronExecutionsState()
    with _connect_readonly_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = _executions_columns(conn)
        recent_rows = _recent_execution_rows(conn, columns=columns)
        failure_rows = _recent_failure_rows(conn, columns=columns)
        window_rows = _execution_window_rows(conn, now=now, columns=columns)
        delivery_rows = _execution_delivery_rows(conn, now=now, columns=columns)
        last_rows = _last_execution_rows(conn, columns=columns)
        incidents = _read_cron_incidents(conn, job_names, now=now)
        return CronExecutionsState(
            db_present=True,
            job_stats=_job_execution_stats(
                window_rows,
                last_rows,
                delivery_rows,
                delivery_tracked="delivery_outcome" in columns,
            ),
            recent=[_execution_from_row(row, job_names, now=now) for row in recent_rows],
            recent_failures=[_execution_from_row(row, job_names, now=now) for row in failure_rows],
            **incidents,
            **_execution_retention_fields(conn, now=now, columns=columns),
        )


def _epoch_from_text(raw: str) -> float | None:
    """A finite epoch float from marker text, or None when it holds anything else."""
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _cron_ticker_epoch(path: Path, root: Path) -> float | None:
    """The single epoch float held in a ticker stamp file, or None if unusable."""
    return _epoch_from_text(_read_text_capped(path, root))


def _cron_marker_present(cron_dir: Path, name: str, root: Path) -> bool:
    """Whether ``cron/<name>`` is a confined, non-symlinked marker that exists.

    ``_exists_strict`` runs first so a permission error on the cron directory
    propagates and fails the source to its last-good value, instead of reading as
    "the marker is absent" — which for these two files would be a claim about
    scheduling health hermesd has no evidence for. ``_read_text_capped`` swallows
    OSError, and on Python 3.14 so do ``Path.exists()``/``is_symlink()``, so the
    ordering is what makes propagation interpreter-independent.
    """
    path = cron_dir / name
    return _exists_strict(path) and _safe_child_path(path, root)


def _read_cron_marker_text_strict(path: Path, root: Path) -> str:
    """Read a confined marker without treating an I/O failure as empty text."""
    if not _safe_child_path(path, root):
        return ""
    with path.open("rb") as handle:
        return handle.read(_MAX_TEXT_READ_BYTES).decode("utf-8", errors="replace")


def _cron_catch_up_occurrences(cron_dir: Path, root: Path) -> tuple[int, bool]:
    """``cron/catch_up_occurrences`` plus whether a count was actually observed.

    Upstream's ``get_catch_up_occurrence_count`` returns ``0`` for a missing file
    and for a genuine zero alike (``cron/jobs.py:1186-1193``), which is exactly
    the distinction an operator needs: the write is best effort and is skipped
    entirely while catch-up is disabled (``cron/jobs.py:2908-2918``), so an
    absent marker is not evidence that no occurrence was missed. The counter is
    monotonic with no timestamp and is never reset, so it is reported as a
    lifetime total — never as a rate and never as an age.
    """
    if not _cron_marker_present(cron_dir, _CATCH_UP_OCCURRENCES_MARKER, root):
        return 0, False
    raw = _read_cron_marker_text_strict(cron_dir / _CATCH_UP_OCCURRENCES_MARKER, root).strip()
    try:
        # Upstream clamps with max(0, ...); a negative marker is still a marker.
        return max(0, int(raw)), True
    except ValueError:
        # Present but holding no count. Reporting that as "0 recorded" would
        # claim evidence hermesd does not have, so it reads as unobserved.
        return 0, False


def _cron_ticker_last_error(
    cron_dir: Path,
    *,
    now: float,
    root: Path,
) -> tuple[str, float | None]:
    """The redacted message and age from ``cron/ticker_last_error``.

    ``clear_ticker_error`` unlinks the marker on the next clean tick
    (``cron/jobs.py:1206-1209``), so an empty result means *no failure recorded
    right now* and never *scheduling has not failed*. Fewer than two lines and a
    blank message are both refused exactly as upstream refuses them
    (``cron/jobs.py:1212-1220``); the line guard is upstream-faithful defence
    that the blank-message check also covers, since ``lines[1:]`` is empty for any
    input shorter than two lines. The message is ``f"{type(e).__name__}: {e}"`` —
    an arbitrary exception string that can embed paths, tokens or URLs — so it is
    redacted and capped here, at the data boundary, never in a panel.
    """
    if not _cron_marker_present(cron_dir, _TICKER_ERROR_MARKER, root):
        return "", None
    lines = _read_cron_marker_text_strict(cron_dir / _TICKER_ERROR_MARKER, root).splitlines()
    if len(lines) < _TICKER_ERROR_MIN_LINES:
        return "", None
    message = _cron_error_excerpt("\n".join(lines[1:]))
    if not message:
        return "", None
    return message, _age_seconds(_epoch_from_text(lines[0]), now)


def _cron_catch_up_policy(cron_cfg: Mapping[str, Any]) -> tuple[bool, bool]:
    """``cron.catch_up_missed`` as configured, and whether the key was set at all.

    Upstream reads it as ``_cron_config_number("catch_up_missed", True, lambda
    value: value is not False)`` (``cron/jobs.py:2908`` + ``:2615-2624``): the
    cast is an *identity* check, so only a literal ``False`` disables catch-up,
    while ``None``, ``0``, ``"no"``, a missing key and an unreadable config all
    leave it enabled. A naive ``bool(value)`` reports the opposite for every one
    of those. When it is off, ``_fast_forward_missed_recurring`` re-anchors the
    schedule *without* calling ``record_catch_up_occurrence``, so missed runs are
    dropped silently and the counter stays flat — which is why the configured
    value is surfaced next to the observed one.
    """
    if _CATCH_UP_MISSED_KEY not in cron_cfg:
        return True, False
    return cron_cfg[_CATCH_UP_MISSED_KEY] is not False, True


def _cron_ticker_ages(
    cron_dir: Path,
    *,
    now: float,
    root: Path,
) -> tuple[float | None, float | None]:
    """Ages of the ticker heartbeat and last-success stamps, clamped at zero."""
    return (
        _age_seconds(_cron_ticker_epoch(cron_dir / "ticker_heartbeat", root), now),
        _age_seconds(_cron_ticker_epoch(cron_dir / "ticker_last_success", root), now),
    )


def _cron_ticker_health(
    heartbeat_age: float | None,
    last_success_age: float | None,
    *,
    ticker_error_recorded: bool = False,
) -> CronTickerHealth:
    """Ticker health from the two stamp ages; UNKNOWN when no heartbeat exists.

    A recorded ``ticker_last_error`` escalates OK to FAILING: upstream only
    unlinks that marker on a clean tick (``cron/scheduler_provider.py:438,447``),
    so a marker that is still there means the last tick failed — even inside
    hermesd's 600s success window, which is 3x looser than upstream's own ~200s
    ``STALE_AFTER`` (``hermes_cli/cron.py:342``). Without this the panel would
    print ``ok`` next to the failure it is displaying. It never invents a
    heartbeat (UNKNOWN stays UNKNOWN) and never downgrades STALE, which is
    already the stronger claim.
    """
    if heartbeat_age is None:
        return CronTickerHealth.UNKNOWN
    if heartbeat_age > _TICKER_HEARTBEAT_STALE_SECONDS:
        return CronTickerHealth.STALE
    if last_success_age is None or last_success_age > _TICKER_LAST_SUCCESS_STALE_SECONDS:
        return CronTickerHealth.FAILING
    if ticker_error_recorded:
        return CronTickerHealth.FAILING
    return CronTickerHealth.OK


# ``fire_claim`` lease: upstream sets FIRE_CLAIM_TTL_SECONDS = 300 with a 60 s
# heartbeat (``cron/jobs.py:889-892``) and refreshes it from the run thread
# (``heartbeat_fire_claim``, ``cron/jobs.py:2600-2608``).
_FIRE_CLAIM_TTL_SECONDS = 300.0
# ``pending_slot`` is honoured upstream only for recurring schedules
# (``unclaimed_pending_slot``, ``cron/occurrences.py:53-70``).
_PENDING_SLOT_SCHEDULE_KINDS = frozenset({"cron", "interval"})


def _claim_owner_pid(claim: dict[str, Any], hostname: str) -> int | None:
    """The claim owner's pid when ``by`` names THIS host, else None (fail safe).

    Upstream stamps ``by`` as ``f"{_machine_id()}:{uuid4().hex}"``
    (``cron/jobs.py:2588``) and only proves death for a same-host pid
    (``_claim_owner_is_dead``, ``:2070-2086``): a foreign host, an explicit
    ``HERMES_MACHINE_ID`` or an unparseable owner never shortens the TTL.
    """
    parts = str(claim.get("by") or "").split(":")
    # isdigit() alone admits "²" and other digits int() refuses.
    pid_text = parts[1] if len(parts) >= 2 else ""
    if not (pid_text.isascii() and pid_text.isdigit()) or parts[0] != hostname:
        return None
    return int(parts[1])


def _cron_job_fire_claim(
    job: dict[str, Any],
    *,
    now: float,
    pid_exists: Callable[[int], bool] | None = None,
    hostname: str,
) -> tuple[float | None, CronFireClaimState | None]:
    """Fire-claim age and derived liveness, or (None, None) for no usable claim.

    ``hostname`` is required: the claim's ``by`` prefix has to be matched
    against the name the claim writer actually stamped, and only the Collector
    (which receives it injected) knows that name. Guessing it here with
    ``socket.gethostname()`` would silently reclassify same-host owners as
    foreign on any host whose name the guess misses.

    Upstream's own liveness window is ``0 <= age < FIRE_CLAIM_TTL_SECONDS``
    (``_claim_is_live``, ``cron/jobs.py:2087-2098``), so a claim exactly at the
    TTL is already stale: the run it leased died before its first 60 s heartbeat
    could be replaced. A same-host owner pid that has exited releases the claim
    even earlier (``_claim_owner_is_dead``, ``:2070-2086``), which is what stops
    a killed ``hermes cron run`` from blocking the next manual run for the whole
    window. A future-dated stamp (clock/TZ skew) is stale upstream too, but
    rendering that as "abandoned run" would claim evidence hermesd does not
    have, so it reports no state.
    """
    claim = _as_dict(job.get("fire_claim"))
    claimed_at = _iso_to_epoch(str(claim.get("at") or ""))
    if claimed_at is None:
        return None, None
    age = now - claimed_at
    if age < 0:
        return 0.0, None
    owner_dead = False
    if pid_exists is not None:
        owner_pid = _claim_owner_pid(claim, hostname)
        if owner_pid is not None:
            owner_dead = not pid_exists(owner_pid)
    state = (
        CronFireClaimState.RUNNING
        if age < _FIRE_CLAIM_TTL_SECONDS and not owner_dead
        else CronFireClaimState.ABANDONED_RUN
    )
    return age, state


def _cron_job_pending_slot(job: dict[str, Any], *, now: float) -> tuple[str, float | None]:
    """Pending-slot instant and stamp age, or ("", None) when it is no slot.

    ``pending_slot`` is the durable record of the window between a tick advancing
    a recurring job's ``next_run_at`` and the fire claim being taken
    (``cron/occurrences.py:38-49``) — an occurrence that may never have run.
    Upstream honours the stamp only for recurring schedules and only when its
    instant parses (``:53-70``); its remaining guards need process-liveness and
    machine-id knowledge hermesd does not have, so the badge says only that a
    slot was recorded, never that a run is due.
    """
    kind = str(_as_dict(job.get("schedule")).get("kind") or "")
    if kind not in _PENDING_SLOT_SCHEDULE_KINDS:
        return "", None
    slot = _as_dict(job.get("pending_slot"))
    scheduled_at = str(slot.get("scheduled_at") or "")
    if not scheduled_at or _iso_to_epoch(scheduled_at) is None:
        return "", None
    stamp_age = _age_seconds(_iso_to_epoch(str(slot.get("at") or "")), now)
    return scheduled_at, stamp_age


def _cron_job_fire_error(job: dict[str, Any], *, now: float) -> tuple[str, float | None]:
    """Redacted ``last_fire_error`` detail and its age, or ("", None).

    ``note_fire_forward_failure`` records that a scheduled fire could not be
    handed to the runner — the only trace of a dashboard fire webhook miss, since
    no execution row is written and ``last_error`` stays null
    (``cron/jobs.py:2200-2212``). It is popped only when a run *succeeds*
    (``cron/jobs.py:2228-2232``), so a repeatedly failing job keeps the original
    text: the recorded age beside it is part of the message, not decoration. The detail is arbitrary webhook text (URLs, tokens), so it goes
    through the same redacting excerpt helper as every other free-text error.
    """
    err = _as_dict(job.get("last_fire_error"))
    detail = _cron_error_excerpt(str(err.get("detail") or ""))
    if not detail:
        return "", None
    return detail, _age_seconds(_iso_to_epoch(str(err.get("at") or "")), now)


def _cron_job_dispatch(job: dict[str, Any]) -> tuple[float | None, str]:
    """`last_dispatch` lateness and kind, or (None, "") when the key is absent."""
    dispatch = _as_dict(job.get("last_dispatch"))
    raw_lateness = dispatch.get("lateness_seconds")
    lateness = None if raw_lateness is None else _coerce_float(raw_lateness)
    return lateness, str(dispatch.get("kind") or "")


def _cron_job_paused(job: dict[str, Any]) -> tuple[bool, str]:
    """Paused flag and reason; a `paused_at` stamp alone is enough to be paused."""
    reason = str(job.get("paused_reason") or "").strip()
    raw_paused_at = job.get("paused_at")
    paused_at_set = raw_paused_at is not None and str(raw_paused_at).strip() != ""
    return paused_at_set or bool(reason), reason


def _cron_job_model(
    job: dict[str, Any], cfg: Mapping[str, Any]
) -> tuple[str, CronModelSource | None]:
    """The model the next fire resolves to and the axis it came from.

    Mirrors ``_load_cron_job_config`` (``cron/scheduler.py:1561-1590``): a
    per-job model is the pin; otherwise ``cron.model`` (the fleet default), then
    the main ``model:`` (shorthand string, or the dict's ``default``/``model``/
    ``name``). ``pinned`` is not stored (``cron/jobs.py:1900-1914``) and the
    retired ``model_snapshot`` keys are ignored upstream, so they are here too.
    The ``HERMES_MODEL`` env fallback is the scheduler's process env, which
    hermesd cannot see, so an otherwise unconfigured job reads as unresolved.
    """
    if _coerce_bool(job.get("no_agent")):
        return "", None
    pinned = str(job.get("model") or "").strip()
    if pinned:
        return pinned, CronModelSource.PINNED
    fleet = str(_as_dict(cfg.get("cron")).get("model") or "").strip()
    if fleet:
        return fleet, CronModelSource.CRON_DEFAULT
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, dict):
        main = model_cfg.get("default") or model_cfg.get("model") or model_cfg.get("name")
    else:
        main = model_cfg
    main_text = str(main or "").strip()
    return (main_text, CronModelSource.MAIN_MODEL) if main_text else ("", None)


def _cron_job_quota_hold(job: dict[str, Any], *, now: float) -> str:
    """The ``quota_hold_until`` instant while the hold is active, else "".

    Upstream's ``hold_active`` (``cron/quota_hold.py:58-64``) treats an expired
    or unparseable marker as inert, so neither is reported.
    """
    until = str(job.get("quota_hold_until") or "")
    until_epoch = _iso_to_epoch(until)
    return until if until_epoch is not None and until_epoch > now else ""


def _cron_job_repeat(job: dict[str, Any]) -> tuple[int | None, int]:
    """`repeat` times (None means unlimited) and completed count."""
    repeat = _as_dict(job.get("repeat"))
    times = _optional_int(repeat.get("times"))
    return times, _coerce_int(repeat.get("completed"))


# ``cron/usage_audit.jsonl`` is appended once per fire and never pruned upstream
# (``_write_usage_audit``, ``cron/scheduler.py:1196-1216``). A line is ~350
# bytes, so this tail holds a few thousand fires; the parse is cached by file
# signature and only the windowing reruns each refresh.
_USAGE_AUDIT_TAIL_BYTES = 1024 * 1024
_USAGE_WINDOW_7D_SECONDS = 7 * 24 * 60 * 60.0
_USAGE_JOBS_LIMIT = 20


@dataclass(frozen=True, slots=True)
class _UsageRecord:
    """One parsed audit line; the error is already a redacted excerpt."""

    epoch: float
    job_id: str
    total_tokens: int | None
    model: str
    duration_seconds: float | None
    error_excerpt: str


@dataclass(frozen=True, slots=True)
class _UsageAudit:
    records: tuple[_UsageRecord, ...]
    cut: bool
    unparseable: int


def _usage_record(data: dict[str, Any]) -> _UsageRecord | None:
    epoch = _iso_to_epoch(str(data.get("ts") or ""))
    if epoch is None:
        return None
    tokens = data.get("total_tokens")
    duration_ms = data.get("duration_ms")
    return _UsageRecord(
        epoch=epoch,
        job_id=str(data.get("job_id") or ""),
        total_tokens=None if tokens is None else _coerce_int(tokens),
        model=str(data.get("model") or "")[:_EXCERPT_MAX_CHARS],
        duration_seconds=None if duration_ms is None else _coerce_float(duration_ms) / 1000.0,
        error_excerpt=_cron_error_excerpt(str(data.get("error") or "")),
    )


def _read_usage_audit_records(path: Path) -> _UsageAudit:
    """Parse the capped tail of ``cron/usage_audit.jsonl``.

    Torn and foreign lines are counted, never fatal; an I/O error propagates so
    the source keeps its last-good value.
    """
    max_bytes = _USAGE_AUDIT_TAIL_BYTES
    cut = _file_size(path) > max_bytes
    records: list[_UsageRecord] = []
    unparseable = 0
    for line in _read_tail_text(path, max_bytes).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        record = None
        with contextlib.suppress(json.JSONDecodeError, RecursionError):
            data = json.loads(stripped)
            if isinstance(data, dict):
                record = _usage_record(data)
        if record is None:
            unparseable += 1
        else:
            records.append(record)
    return _UsageAudit(records=tuple(records), cut=cut, unparseable=unparseable)


def _cron_usage_state(
    audit: _UsageAudit, job_names: Mapping[str, str], *, now: float
) -> CronUsageState:
    """Roll the parsed audit up into 24h/7d per-job windows at ``now``."""
    day_start = now - _EXECUTIONS_WINDOW_SECONDS
    week_start = now - _USAGE_WINDOW_7D_SECONDS
    by_job: dict[str, CronJobUsage] = {}
    last_epoch: dict[str, float] = {}
    for record in audit.records:
        if record.epoch < week_start or record.epoch > now:
            continue
        entry = by_job.setdefault(
            record.job_id,
            CronJobUsage(
                job_id=record.job_id, job_name=job_names.get(record.job_id) or record.job_id
            ),
        )
        tokens = record.total_tokens or 0
        entry.fires_7d += 1
        entry.tokens_7d += tokens
        entry.errors_7d += 1 if record.error_excerpt else 0
        if record.epoch >= day_start:
            entry.fires_24h += 1
            entry.tokens_24h += tokens
        if record.epoch >= last_epoch.get(record.job_id, -math.inf):
            last_epoch[record.job_id] = record.epoch
            entry.last_fire_age_seconds = _age_seconds(record.epoch, now)
            entry.last_total_tokens = record.total_tokens
            entry.last_model = record.model
            entry.last_duration_seconds = record.duration_seconds
            entry.last_error_excerpt = record.error_excerpt
    jobs = sorted(by_job.values(), key=lambda job: (-job.tokens_7d, -job.fires_7d, job.job_id))
    oldest = min((record.epoch for record in audit.records), default=None)
    return CronUsageState(
        present=True,
        jobs=jobs[:_USAGE_JOBS_LIMIT],
        tokens_24h=sum(job.tokens_24h for job in jobs),
        tokens_7d=sum(job.tokens_7d for job in jobs),
        fires_7d=sum(job.fires_7d for job in jobs),
        window_truncated=audit.cut and (oldest is None or oldest > week_start),
        unparseable_lines=audit.unparseable,
    )


# ``cron/deliveries.db`` statuses (``cron/delivery_queue.py:108-122``). The
# vocabulary is CHECK-constrained upstream; the kind cap only bounds a foreign
# schema's values.
_DELIVERY_PENDING_STATUSES = ("pending", "delivering")
_DELIVERY_FAILED_STATUSES = ("failed", "unknown")
_DELIVERY_STATUS_KIND_LIMIT = 8
_DELIVERY_FAILURES_LIMIT = 5


def _read_cron_delivery_queue(db_path: Path, *, now: float) -> CronDeliveryQueueState:
    """Pending/failed counts, oldest pending age and the newest failures.

    ``content`` and ``job_json`` hold the message payload and are never read.
    The caller has already confined ``db_path``; read errors propagate so the
    source keeps its last-good value.
    """
    with _connect_readonly_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "deliveries"):
            return CronDeliveryQueueState(db_present=True)
        conn.create_function("hermes_epoch", 1, _memo_iso_to_epoch, deterministic=True)
        counts = {
            str(row.get("status") or ""): int(row.get("n") or 0)
            for row in _query_rows(
                conn,
                "SELECT COALESCE(status, '') AS status, COUNT(*) AS n FROM deliveries "
                f"GROUP BY COALESCE(status, '') ORDER BY n DESC LIMIT {_DELIVERY_STATUS_KIND_LIMIT}",
            )
        }
        pending = ", ".join(f"'{status}'" for status in _DELIVERY_PENDING_STATUSES)
        failed = ", ".join(f"'{status}'" for status in _DELIVERY_FAILED_STATUSES)
        oldest = _query_rows(
            conn,
            f"SELECT MIN(hermes_epoch(created_at)) AS oldest FROM deliveries "
            f"WHERE status IN ({pending})",
        )[0].get("oldest")
        failed_24h = _count_rows(
            conn,
            f"SELECT COUNT(*) FROM deliveries WHERE status IN ({failed}) "
            "AND hermes_epoch(finished_at) >= ?",
            (now - _EXECUTIONS_WINDOW_SECONDS,),
        )
        for_failure = (
            "for_failure"
            if _column_exists(conn, "deliveries", "for_failure")
            else "0 AS for_failure"
        )
        rows = _query_rows(
            conn,
            f"SELECT execution_id, status, {for_failure}, finished_at, error FROM deliveries "
            f"WHERE status IN ({failed}) "
            "ORDER BY hermes_epoch(finished_at) DESC, execution_id DESC "
            f"LIMIT {_DELIVERY_FAILURES_LIMIT}",
        )
    return CronDeliveryQueueState(
        db_present=True,
        status_counts=dict(sorted(counts.items())),
        pending_count=sum(counts.get(status, 0) for status in _DELIVERY_PENDING_STATUSES),
        oldest_pending_age_seconds=_age_seconds(oldest, now),
        failed_24h=failed_24h,
        recent_failures=[
            CronDeliveryFailure(
                execution_id=str(row.get("execution_id") or "")[:_EXCERPT_MAX_CHARS],
                status=str(row.get("status") or ""),
                for_failure=_coerce_bool(row.get("for_failure")),
                finished_age_seconds=_age_seconds(
                    _iso_to_epoch(str(row.get("finished_at") or "")), now
                ),
                error_excerpt=_cron_error_excerpt(str(row.get("error") or "")),
            )
            for row in rows
        ],
    )


# ``cron/bot_chat_pending/`` is never pruned upstream, so the listing is capped;
# a receipt embeds the whole cron output as ``content``, so one past the byte
# cap is counted unreadable instead of parsed.
_BOT_CHAT_SCAN_LIMIT = 2000
_BOT_CHAT_RECORD_MAX_BYTES = 1024 * 1024
_BOT_CHAT_UNSETTLED_STATUSES = ("queued", "claimed")
_BOT_CHAT_ATTENTION_STATUSES = ("ambiguous", "claimed")
_BOT_CHAT_ATTENTION_LIMIT = 5
_BOT_CHAT_STATUS_KIND_LIMIT = 8

_BotChatSignature = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _BotChatReceipt:
    """The fields hermesd keeps from one receipt; ``content`` is dropped."""

    receipt_id: str
    status: str
    job_name: str
    for_failure: bool
    error_excerpt: str


def _read_bot_chat_receipt(path: Path) -> _BotChatReceipt | None:
    """Parse one receipt, or None when it is oversize, torn or not an object.

    Upstream keeps unreadable receipts as evidence and skips them
    (``_records``, ``cron/bot_chat_delivery.py:38-55``); so does hermesd.
    """
    with _open_regular_file(path) as handle:
        raw = handle.read(_BOT_CHAT_RECORD_MAX_BYTES + 1)
    if len(raw) > _BOT_CHAT_RECORD_MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    job = _as_dict(data.get("job"))
    return _BotChatReceipt(
        receipt_id=str(data.get("id") or path.stem)[:_EXCERPT_MAX_CHARS],
        status=str(data.get("status") or "")[:_EXCERPT_MAX_CHARS],
        job_name=str(job.get("name") or job.get("id") or "")[:_EXCERPT_MAX_CHARS],
        for_failure=_coerce_bool(data.get("for_failure")),
        error_excerpt=_cron_error_excerpt(str(data.get("error") or "")),
    )


def _read_cron_bot_chat(
    root_dir: Path,
    *,
    now: float,
    cache: dict[str, tuple[_BotChatSignature, _BotChatReceipt | None]],
) -> CronBotChatState:
    """Status counts, the unsettled backlog and receipts needing attention.

    ``cache`` maps a receipt name to its last parse, keyed by (mtime_ns, size,
    inode), and is rewritten to the files seen this scan. The caller confines
    ``root_dir``; a listing error propagates so the source keeps its last-good.
    """
    entries = list(islice(root_dir.iterdir(), _BOT_CHAT_SCAN_LIMIT + 1))
    truncated = len(entries) > _BOT_CHAT_SCAN_LIMIT
    counts: dict[str, int] = {}
    unreadable = 0
    oldest_unsettled: float | None = None
    attention: list[tuple[float, _BotChatReceipt]] = []
    seen: dict[str, tuple[_BotChatSignature, _BotChatReceipt | None]] = {}
    for path in entries[:_BOT_CHAT_SCAN_LIMIT]:
        if path.suffix != ".json":
            continue
        try:
            stat = path.lstat()
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            cached = cache.get(path.name)
            if cached is not None and cached[0] == signature:
                receipt = cached[1]
            else:
                receipt = None if path.is_symlink() else _read_bot_chat_receipt(path)
        except OSError:
            unreadable += 1
            continue
        seen[path.name] = (signature, receipt)
        if receipt is None:
            unreadable += 1
            continue
        if receipt.status in counts or len(counts) < _BOT_CHAT_STATUS_KIND_LIMIT:
            counts[receipt.status] = counts.get(receipt.status, 0) + 1
        age = max(0.0, now - stat.st_mtime)
        if receipt.status in _BOT_CHAT_UNSETTLED_STATUSES:
            oldest_unsettled = age if oldest_unsettled is None else max(oldest_unsettled, age)
        if receipt.status in _BOT_CHAT_ATTENTION_STATUSES:
            attention.append((age, receipt))
    cache.clear()
    cache.update(seen)
    attention.sort(key=lambda item: (item[0], item[1].receipt_id))
    return CronBotChatState(
        present=True,
        status_counts=dict(sorted(counts.items())),
        unsettled_count=sum(counts.get(status, 0) for status in _BOT_CHAT_UNSETTLED_STATUSES),
        oldest_unsettled_age_seconds=oldest_unsettled,
        attention=[
            CronBotChatReceipt(
                receipt_id=receipt.receipt_id,
                job_name=receipt.job_name,
                status=receipt.status,
                for_failure=receipt.for_failure,
                age_seconds=age,
                error_excerpt=receipt.error_excerpt,
            )
            for age, receipt in attention[:_BOT_CHAT_ATTENTION_LIMIT]
        ],
        unreadable_count=unreadable,
        scan_truncated=truncated,
    )
