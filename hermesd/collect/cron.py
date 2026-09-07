"""Cron job output discovery, excerpts, execution history and ticker health."""

from __future__ import annotations

import contextlib
import datetime
import json
import math
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hermesd.collect.common import (
    _EXCERPT_MAX_CHARS,
    _as_dict,
    _coerce_float,
    _coerce_int,
    _mtime,
    _path_resolves_under,
    _read_tail_text,
    _read_text_capped,
    _safe_mtime,
)
from hermesd.collect.redaction import _redact_secret_text
from hermesd.collect.sqlite_util import (
    _connect_readonly_sqlite,
    _count_rows_or_zero,
    _query_rows,
    _table_exists,
)
from hermesd.models import (
    CronExecution,
    CronExecutionsState,
    CronIncident,
    CronJobExecutionStats,
    CronTickerHealth,
    LogLine,
)

# executions.db grows without bound (1k rows on a month-old home), so every
# read is capped: the newest _EXECUTIONS_SCAN_LIMIT rows feed the 24h counters
# and the newest _EXECUTIONS_RECENT_LIMIT of those feed the detail list.
_EXECUTIONS_SCAN_LIMIT = 500
_EXECUTIONS_RECENT_LIMIT = 10
_EXECUTIONS_WINDOW_SECONDS = 24 * 60 * 60.0
_INCIDENTS_LIMIT = 5

# The ticker fires every 60s. Two missed beats means stale; a heartbeat that
# keeps arriving while last_success falls ten beats behind means failing.
_TICKER_HEARTBEAT_STALE_SECONDS = 120.0
_TICKER_LAST_SUCCESS_STALE_SECONDS = 600.0


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
    for path in job_output_dir.iterdir():
        try:
            if not path.is_symlink() and path.is_file():
                files.append(path)
        except OSError:
            continue
    if not files:
        return None
    return max(files, key=_safe_mtime)


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
        lines = _read_tail_text(latest, max_bytes).splitlines()
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
    for job_dir in output_root.iterdir():
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        for path in job_dir.iterdir():
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_file = path
                latest_mtime = mtime
    if latest_file is None:
        return []
    try:
        lines = _read_tail_text(latest_file, max_bytes).splitlines()[-max_lines:]
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
            with contextlib.suppress(json.JSONDecodeError):
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


def _parse_iso_epoch(value: str) -> float | None:
    """Epoch seconds for an ISO-8601 stamp, assuming UTC when no offset is given."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.timestamp()


def _age_since(epoch: float | None, now: float) -> float | None:
    """Non-negative age of `epoch` at `now`, or None when the stamp is unusable."""
    if epoch is None or not math.isfinite(epoch):
        return None
    return max(0.0, now - epoch)


def _cron_error_excerpt(error: str) -> str:
    """First non-blank line of an execution/incident error, redacted and capped."""
    for line in error.splitlines():
        stripped = line.strip()
        if stripped:
            return _redact_secret_text(stripped)[:_EXCERPT_MAX_CHARS]
    return ""


def _execution_duration(started_at: str, finished_at: str) -> float | None:
    started = _parse_iso_epoch(started_at)
    finished = _parse_iso_epoch(finished_at)
    if started is None or finished is None:
        return None
    return max(0.0, finished - started)


def _claimed_sort_key(row: dict[str, Any]) -> float:
    """Claim epoch for newest-first ordering; unparseable stamps sort last."""
    claimed = _parse_iso_epoch(str(row.get("claimed_at") or ""))
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
        started_age_seconds=_age_since(_parse_iso_epoch(started_at), now),
        duration_seconds=_execution_duration(started_at, str(row.get("finished_at") or "")),
        error_excerpt=_cron_error_excerpt(str(row.get("error") or "")),
    )


def _job_execution_stats(
    rows: list[dict[str, Any]],
    *,
    now: float,
) -> list[CronJobExecutionStats]:
    """Per-job 24h status counters and last-run detail from newest-first rows."""
    window_start = now - _EXECUTIONS_WINDOW_SECONDS
    stats: dict[str, CronJobExecutionStats] = {}
    for row in rows:
        job_id = str(row.get("job_id") or "")
        status = str(row.get("status") or "")
        if job_id not in stats:
            started_at = str(row.get("started_at") or "")
            stats[job_id] = CronJobExecutionStats(
                job_id=job_id,
                last_status=status,
                last_duration_seconds=_execution_duration(
                    started_at, str(row.get("finished_at") or "")
                ),
                last_error_excerpt=_cron_error_excerpt(str(row.get("error") or "")),
            )
        claimed = _parse_iso_epoch(str(row.get("claimed_at") or ""))
        if claimed is None or claimed < window_start:
            continue
        entry = stats[job_id]
        if status == "completed":
            entry.completed_24h += 1
        elif status == "failed":
            entry.failed_24h += 1
        elif status in {"running", "claimed"}:
            entry.running_24h += 1
    return list(stats.values())


def _recent_execution_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The newest _EXECUTIONS_SCAN_LIMIT executions, or [] when the table is absent."""
    if not _table_exists(conn, "executions"):
        return []
    with contextlib.suppress(sqlite3.Error):
        # The LIMIT is a module-level int constant, never caller-supplied text.
        return _query_rows(
            conn,
            "SELECT id, job_id, status, claimed_at, started_at, finished_at, error "
            "FROM executions ORDER BY claimed_at DESC, id DESC "
            f"LIMIT {_EXECUTIONS_SCAN_LIMIT}",
        )
    return []


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
        first_seen_age_seconds=_age_since(
            _parse_iso_epoch(str(row.get("first_seen_at") or "")), now
        ),
        last_seen_age_seconds=_age_since(_parse_iso_epoch(str(row.get("last_seen_at") or "")), now),
        error_excerpt=_cron_error_excerpt(str(row.get("error") or "")),
    )


def _read_cron_incidents(
    conn: sqlite3.Connection,
    job_names: Mapping[str, str],
    *,
    now: float,
) -> tuple[int, int, list[CronIncident]]:
    """Open/unacked incident counts plus the latest few open incidents."""
    if not _table_exists(conn, "cron_incidents"):
        return 0, 0, []
    open_clause = "WHERE COALESCE(state, '') != 'closed' AND closed_at IS NULL"
    open_count = _count_rows_or_zero(conn, f"SELECT COUNT(*) FROM cron_incidents {open_clause}")
    unacked_count = _count_rows_or_zero(
        conn,
        f"SELECT COUNT(*) FROM cron_incidents {open_clause} AND acked_at IS NULL",
    )
    rows: list[dict[str, Any]] = []
    with contextlib.suppress(sqlite3.Error):
        rows = _query_rows(
            conn,
            "SELECT id, job_id, state, failure_type, first_seen_at, last_seen_at, error "
            f"FROM cron_incidents {open_clause} ORDER BY last_seen_at DESC, id DESC "
            f"LIMIT {_INCIDENTS_LIMIT}",
        )
    return open_count, unacked_count, [_incident_from_row(row, job_names, now=now) for row in rows]


def _read_cron_executions_state(
    db_path: Path,
    job_names: Mapping[str, str],
    *,
    now: float,
) -> CronExecutionsState:
    """Bounded read of cron/executions.db: 24h counters, recent runs, incidents."""
    if db_path.is_symlink() or not db_path.is_file():
        return CronExecutionsState()
    with _connect_readonly_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ordered = sorted(_recent_execution_rows(conn), key=_claimed_sort_key, reverse=True)
        open_count, unacked_count, incidents = _read_cron_incidents(conn, job_names, now=now)
        return CronExecutionsState(
            db_present=True,
            job_stats=_job_execution_stats(ordered, now=now),
            recent=[
                _execution_from_row(row, job_names, now=now)
                for row in ordered[:_EXECUTIONS_RECENT_LIMIT]
            ],
            open_incident_count=open_count,
            unacked_incident_count=unacked_count,
            open_incidents=incidents,
        )


def _cron_ticker_epoch(path: Path) -> float | None:
    """The single epoch float held in a ticker stamp file, or None if unusable."""
    raw = _read_text_capped(path).strip()
    if not raw:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _cron_ticker_ages(cron_dir: Path, *, now: float) -> tuple[float | None, float | None]:
    """Ages of the ticker heartbeat and last-success stamps, clamped at zero."""
    return (
        _age_since(_cron_ticker_epoch(cron_dir / "ticker_heartbeat"), now),
        _age_since(_cron_ticker_epoch(cron_dir / "ticker_last_success"), now),
    )


def _cron_ticker_health(
    heartbeat_age: float | None,
    last_success_age: float | None,
) -> CronTickerHealth:
    """Ticker health from the two stamp ages; UNKNOWN when no heartbeat exists."""
    if heartbeat_age is None:
        return CronTickerHealth.UNKNOWN
    if heartbeat_age > _TICKER_HEARTBEAT_STALE_SECONDS:
        return CronTickerHealth.STALE
    if last_success_age is None or last_success_age > _TICKER_LAST_SUCCESS_STALE_SECONDS:
        return CronTickerHealth.FAILING
    return CronTickerHealth.OK


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


def _cron_job_repeat(job: dict[str, Any]) -> tuple[int | None, int]:
    """`repeat` times (None means unlimited) and completed count."""
    repeat = _as_dict(job.get("repeat"))
    raw_times = repeat.get("times")
    times = None if raw_times is None else _coerce_int(raw_times)
    return times, _coerce_int(repeat.get("completed"))
