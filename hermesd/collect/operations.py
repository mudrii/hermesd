"""Verification evidence, goals, projects, MoA and curator readers."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any, NamedTuple

from hermesd.collect.common import (
    _as_dict,
    _as_list,
    _coerce_float,
    _coerce_int,
    _file_size,
    _mtime,
    _path_resolves_under,
    _read_tail_text,
    _safe_child_path,
)
from hermesd.collect.kanban import _kanban_board_present
from hermesd.collect.sqlite_util import (
    _count_rows_or_zero,
    _query_rows,
    _table_count,
    _table_count_or_zero,
    _table_exists,
)
from hermesd.models import (
    CuratorRun,
    DelegationInfo,
    DiscoveredRepoSummary,
    GoalSummary,
    OperationsState,
    ProjectSummary,
    VerificationEventSummary,
    VerificationRootSummary,
)
from hermesd.paths import HermesPaths


def _read_verification_evidence(
    conn: sqlite3.Connection,
    operations: OperationsState,
) -> OperationsState:
    return operations.model_copy(
        update={
            "verification_db_present": True,
            "verification_event_count": _table_count(conn, "verification_events"),
            "verification_failed_count": _verification_failed_count(conn),
            "verification_state_count": _table_count_or_zero(conn, "verification_state"),
            "verification_latest_events": _read_verification_events(conn),
            "verification_roots": _read_verification_roots(conn),
        }
    )


def _read_verification_events(conn: sqlite3.Connection) -> list[VerificationEventSummary]:
    rows = _query_rows(
        conn,
        "SELECT id, created_at, session_id, root, command, canonical_command, "
        "kind, scope, status, exit_code, output_summary "
        "FROM verification_events ORDER BY id DESC LIMIT 8",
    )
    return [
        VerificationEventSummary(
            event_id=_coerce_int(row.get("id")),
            created_at=str(row.get("created_at") or ""),
            session_id=str(row.get("session_id") or ""),
            root=str(row.get("root") or ""),
            command=str(row.get("command") or ""),
            canonical_command=str(row.get("canonical_command") or ""),
            kind=str(row.get("kind") or ""),
            scope=str(row.get("scope") or ""),
            status=str(row.get("status") or ""),
            exit_code=_coerce_int(row.get("exit_code")),
            output_summary=str(row.get("output_summary") or ""),
        )
        for row in rows
    ]


def _read_verification_roots(conn: sqlite3.Connection) -> list[VerificationRootSummary]:
    if not _table_exists(conn, "verification_state"):
        return []
    rows = _query_rows(
        conn,
        "SELECT session_id, root, last_event_id, last_edit_at, changed_paths_json "
        "FROM verification_state "
        "ORDER BY COALESCE(last_edit_at, '') DESC, COALESCE(last_event_id, 0) DESC "
        "LIMIT 8",
    )
    return [
        VerificationRootSummary(
            session_id=str(row.get("session_id") or ""),
            root=str(row.get("root") or ""),
            last_event_id=_coerce_int(row.get("last_event_id")),
            last_edit_at=str(row.get("last_edit_at") or ""),
            changed_path_count=_json_list_count(row.get("changed_paths_json")),
        )
        for row in rows
    ]


def _verification_failed_count(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM verification_events WHERE status != 'passed'")
    row = cur.fetchone()
    return int(row[0] or 0) if row is not None else 0


def _goal_state_update(conn: sqlite3.Connection) -> dict[str, Any]:
    goals = _read_goal_summaries(conn)
    return {
        "goal_count": len(goals),
        "active_goal_count": sum(1 for goal in goals if goal.status == "active"),
        "waiting_goal_count": sum(
            1 for goal in goals if goal.waiting_on_pid or goal.waiting_on_session
        ),
        "goals": goals,
    }


_DELEGATION_TERMINAL_STATES = ("completed", "error", "failed", "cancelled")
_DELEGATION_FAILED_STATES = ("error", "failed")
_JSON_COLUMN_MAX_BYTES = 4096
_DELEGATION_TEXT_MAX_CHARS = 80
_BOUNDED_SCAN_LIMIT = 200
_STATE_META_MAINTENANCE_KEYS = (
    "last_auto_prune",
    "last_auto_archive",
    "db_file_generation",
    "fts_storage_version",
)


class StateDbRead(NamedTuple):
    """Everything one state.db open yields, before clock/pid enrichment.

    Cached against state.db's mtime by the collector, so it must hold only
    values that change when the database changes — never a wall-clock age or a
    liveness check.
    """

    goal_update: dict[str, Any]
    delegation_rows: list[dict[str, Any]]
    delegation_counts: dict[str, int]
    meta: dict[str, str]
    schema_version: int


def _read_state_db(conn: sqlite3.Connection) -> StateDbRead:
    return StateDbRead(
        goal_update=_goal_state_update(conn),
        delegation_rows=_delegation_rows(conn),
        delegation_counts=_delegation_counts(conn),
        meta=_state_meta_entries(conn),
        schema_version=_read_schema_version(conn),
    )


def _state_db_update(
    read: StateDbRead,
    *,
    now: float,
    pid_exists: Callable[[int], bool],
) -> dict[str, Any]:
    """Turn a cached state.db read into a fresh OperationsState update."""
    update: dict[str, Any] = dict(read.goal_update)
    update.update(read.delegation_counts)
    update["delegations"] = [
        _delegation_from_row(row, now, pid_exists) for row in read.delegation_rows
    ]
    update["state_db_schema_version"] = read.schema_version
    update["state_db_file_generation"] = read.meta.get("db_file_generation", "")
    update["state_db_fts_storage_version"] = read.meta.get("fts_storage_version", "")
    update["last_auto_prune_age_seconds"] = _epoch_age_seconds(
        read.meta.get("last_auto_prune", ""), now
    )
    update["last_auto_archive_age_seconds"] = _epoch_age_seconds(
        read.meta.get("last_auto_archive", ""), now
    )
    return update


def _delegation_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The 10 newest async_delegations rows; the table is absent on old agents."""
    if not _table_exists(conn, "async_delegations"):
        return []
    with contextlib.suppress(sqlite3.Error):
        return _query_rows(
            conn,
            "SELECT * FROM async_delegations ORDER BY dispatched_at DESC LIMIT 10",
        )
    return []


def _delegation_counts(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "async_delegations"):
        return {}
    terminal = ", ".join(f"'{state}'" for state in _DELEGATION_TERMINAL_STATES)
    failed = ", ".join(f"'{state}'" for state in _DELEGATION_FAILED_STATES)
    return {
        "delegation_count": _table_count_or_zero(conn, "async_delegations"),
        "delegation_running_count": _count_rows_or_zero(
            conn,
            f"SELECT COUNT(*) FROM async_delegations WHERE COALESCE(state, '') NOT IN ({terminal})",
        ),
        "delegation_failed_count": _count_rows_or_zero(
            conn,
            f"SELECT COUNT(*) FROM async_delegations WHERE COALESCE(state, '') IN ({failed})",
        ),
        "delegation_undelivered_count": _count_rows_or_zero(
            conn,
            "SELECT COUNT(*) FROM async_delegations "
            "WHERE COALESCE(delivery_state, '') != 'delivered' "
            "AND COALESCE(state, '') = 'completed'",
        ),
    }


def _delegation_from_row(
    row: dict[str, Any],
    now: float,
    pid_exists: Callable[[int], bool],
) -> DelegationInfo:
    dispatched_at = _coerce_float(row.get("dispatched_at"))
    completed_at = _coerce_float(row.get("completed_at")) or None
    task = _json_object_capped(row.get("task_json"))
    result = _first_delegation_result(row.get("result_json"))
    owner_pid = _coerce_int(row.get("owner_pid"))
    return DelegationInfo(
        delegation_id=str(row.get("delegation_id") or ""),
        origin_session=str(row.get("origin_session") or row.get("origin_session_id") or ""),
        state=str(row.get("state") or ""),
        delivery_state=str(row.get("delivery_state") or ""),
        delivery_attempts=_coerce_int(row.get("delivery_attempts")),
        dispatched_at=dispatched_at,
        completed_at=completed_at,
        duration_seconds=_delegation_duration(dispatched_at, completed_at, now),
        goal=_clip_single_line(str(task.get("goal") or "")),
        result_status=str(result.get("status") or ""),
        error_excerpt=_clip_single_line(
            str(result.get("error") or "") or str(result.get("summary") or "")
        ),
        owner_alive=bool(owner_pid) and pid_exists(owner_pid),
    )


def _delegation_duration(
    dispatched_at: float,
    completed_at: float | None,
    now: float,
) -> float | None:
    if dispatched_at <= 0:
        return None
    end = completed_at if completed_at is not None else now
    return max(0.0, end - dispatched_at)


def _first_delegation_result(raw: object) -> dict[str, Any]:
    results = _as_list(_json_object_capped(raw).get("results"))
    if results and isinstance(results[0], dict):
        return results[0]
    return {}


def _json_object_capped(raw: object) -> dict[str, Any]:
    """Decode a JSON object column, refusing payloads over 4 KiB."""
    if not isinstance(raw, str) or not raw:
        return {}
    if len(raw.encode("utf-8", errors="replace")) > _JSON_COLUMN_MAX_BYTES:
        return {}
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            return decoded
    return {}


def _clip_single_line(value: str) -> str:
    return " ".join(value.split())[:_DELEGATION_TEXT_MAX_CHARS]


def _state_meta_entries(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, "state_meta"):
        return {}
    keys = ", ".join(f"'{key}'" for key in _STATE_META_MAINTENANCE_KEYS)
    with contextlib.suppress(sqlite3.Error):
        rows = _query_rows(
            conn,
            f"SELECT key, value FROM state_meta WHERE key IN ({keys}) LIMIT 8",
        )
        return {str(row.get("key") or ""): str(row.get("value") or "") for row in rows}
    return {}


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Max integer in schema_version's version-like column, 0 when absent."""
    if not _table_exists(conn, "schema_version"):
        return 0
    best = 0
    with contextlib.suppress(sqlite3.Error):
        columns = [str(row[1] or "") for row in conn.execute("PRAGMA table_info(schema_version)")]
        for column in columns:
            if "version" not in column.lower():
                continue
            # Identifier comes from PRAGMA output, not from user input.
            row = conn.execute(
                f'SELECT MAX(CAST("{column}" AS INTEGER)) FROM schema_version'
            ).fetchone()
            best = max(best, int(row[0] or 0) if row is not None else 0)
    return best


def _epoch_age_seconds(raw: str, now: float) -> float | None:
    epoch = _coerce_float(raw)
    if epoch <= 0:
        return None
    return max(0.0, now - epoch)


def _iso_age_seconds(raw: str, now: float) -> float | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, now - parsed.timestamp())


def _count_delegation_live_logs(live_root: Path, home: Path) -> int:
    """Count live subagent transcripts, scanning at most 200 directory entries."""
    if not _safe_child_path(live_root, home) or not live_root.is_dir():
        return 0
    count = 0
    scanned = 0
    with contextlib.suppress(OSError):
        for run_dir in islice(live_root.iterdir(), _BOUNDED_SCAN_LIMIT):
            scanned += 1
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            if not _path_resolves_under(run_dir, home):
                continue
            for log in islice(run_dir.glob("task-*.log"), _BOUNDED_SCAN_LIMIT - scanned):
                if log.is_symlink() or not log.is_file():
                    continue
                count += 1
                scanned += 1
            if scanned >= _BOUNDED_SCAN_LIMIT:
                break
    return count


def _read_state_snapshots(root: Path, home: Path, *, now: float) -> dict[str, Any]:
    """Stat state-snapshots/ one level deep, capped at 200 entries."""
    count = 0
    total_bytes = 0
    newest: float | None = None
    if _safe_child_path(root, home) and root.is_dir():
        with contextlib.suppress(OSError):
            for entry in islice(root.iterdir(), _BOUNDED_SCAN_LIMIT):
                if entry.is_symlink() or not _path_resolves_under(entry, home):
                    continue
                if entry.is_dir():
                    total_bytes += _immediate_file_bytes(entry)
                elif entry.is_file():
                    total_bytes += _file_size(entry)
                else:
                    continue
                count += 1
                mtime = _mtime(entry)
                if mtime is not None and (newest is None or mtime > newest):
                    newest = mtime
    return {
        "snapshot_count": count,
        "snapshot_total_bytes": total_bytes,
        "newest_snapshot_age_seconds": max(0.0, now - newest) if newest is not None else None,
    }


def _immediate_file_bytes(directory: Path) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for child in islice(directory.iterdir(), _BOUNDED_SCAN_LIMIT):
            if child.is_file() and not child.is_symlink():
                total += _file_size(child)
    return total


def _read_goal_summaries(conn: sqlite3.Connection) -> list[GoalSummary]:
    if not _table_exists(conn, "state_meta"):
        return []
    rows = _query_rows(
        conn,
        "SELECT key, value FROM state_meta WHERE key LIKE 'goal:%' ORDER BY key LIMIT 8",
    )
    goals: list[GoalSummary] = []
    for row in rows:
        raw_value = str(row.get("value") or "")
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(raw_value)
            if isinstance(data, dict):
                goals.append(_goal_summary_from_row(str(row.get("key") or ""), data))
    return goals


def _goal_summary_from_row(key: str, data: dict[str, Any]) -> GoalSummary:
    contract = _as_dict(data.get("contract"))
    return GoalSummary(
        session_id=key.removeprefix("goal:"),
        goal=str(data.get("goal") or ""),
        status=str(data.get("status") or ""),
        turns_used=_coerce_int(data.get("turns_used")),
        max_turns=_coerce_int(data.get("max_turns")),
        has_contract=any(value not in (None, "", [], {}) for value in contract.values()),
        waiting_on_pid=_coerce_int(data.get("waiting_on_pid")),
        waiting_on_session=str(data.get("waiting_on_session") or ""),
        waiting_reason=str(data.get("waiting_reason") or ""),
        subgoal_count=len(_as_list(data.get("subgoals"))),
    )


def _read_projects_state(
    conn: sqlite3.Connection,
    operations: OperationsState,
    paths: HermesPaths,
) -> OperationsState:
    return operations.model_copy(
        update={
            "projects_db_present": True,
            "project_count": _table_count(conn, "projects"),
            "project_archived_count": _count_rows_or_zero(
                conn,
                "SELECT COUNT(*) FROM projects WHERE archived != 0",
            ),
            "project_folder_count": _table_count_or_zero(conn, "project_folders"),
            "discovered_repo_count": _table_count_or_zero(conn, "discovered_repos"),
            "project_missing_primary_path_count": _count_rows_or_zero(
                conn,
                "SELECT COUNT(*) FROM projects WHERE COALESCE(primary_path, '') = ''",
            ),
            "projects": _read_project_summaries(conn, operations.verification_roots, paths),
            "discovered_repos": _read_discovered_repos(conn),
        }
    )


def _read_project_summaries(
    conn: sqlite3.Connection,
    verification_roots: list[VerificationRootSummary],
    paths: HermesPaths,
) -> list[ProjectSummary]:
    with contextlib.suppress(sqlite3.Error):
        rows = _query_rows(
            conn,
            "SELECT slug, name, board_slug, primary_path, archived "
            "FROM projects ORDER BY archived ASC, created_at DESC, slug ASC LIMIT 8",
        )
        return [
            ProjectSummary(
                slug=str(row.get("slug") or ""),
                name=str(row.get("name") or ""),
                board_slug=str(row.get("board_slug") or ""),
                primary_path=str(row.get("primary_path") or ""),
                archived=bool(row.get("archived")),
                verification_root_count=_project_verification_root_count(
                    str(row.get("primary_path") or ""),
                    verification_roots,
                ),
                kanban_board_present=_kanban_board_present(
                    paths,
                    str(row.get("board_slug") or ""),
                ),
            )
            for row in rows
        ]
    return []


def _read_discovered_repos(conn: sqlite3.Connection) -> list[DiscoveredRepoSummary]:
    if not _table_exists(conn, "discovered_repos"):
        return []
    with contextlib.suppress(sqlite3.Error):
        rows = _query_rows(
            conn,
            "SELECT root, label, last_seen FROM discovered_repos "
            "ORDER BY COALESCE(last_seen, '') DESC, root ASC LIMIT 5",
        )
        return [
            DiscoveredRepoSummary(
                root=str(row.get("root") or ""),
                label=str(row.get("label") or ""),
                last_seen=str(row.get("last_seen") or ""),
            )
            for row in rows
        ]
    return []


def _project_verification_root_count(
    primary_path: str,
    verification_roots: list[VerificationRootSummary],
) -> int:
    if not primary_path:
        return 0
    return sum(
        1 for root in verification_roots if _same_path_or_descendant(root.root, primary_path)
    )


def _same_path_or_descendant(candidate: str, parent: str) -> bool:
    candidate_path = candidate.rstrip(os.sep)
    parent_path = parent.rstrip(os.sep)
    if not candidate_path or not parent_path:
        return False
    return candidate_path == parent_path or candidate_path.startswith(parent_path + os.sep)


def _json_list_count(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    with contextlib.suppress(json.JSONDecodeError):
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return len(decoded)
    return 0


def _curator_with_scheduler_state(
    run: CuratorRun,
    state: dict[str, Any],
    curator_cfg: dict[str, Any],
) -> CuratorRun:
    if not state and not curator_cfg:
        return run
    return run.model_copy(
        update={
            "scheduler_state_present": bool(state),
            "scheduler_paused": bool(state.get("paused")),
            "scheduler_run_count": _coerce_int(state.get("run_count")),
            "scheduler_last_run_at": str(state.get("last_run_at") or ""),
            "scheduler_last_report_path": str(state.get("last_report_path") or ""),
            "consolidate_enabled": bool(curator_cfg.get("consolidate")),
        }
    )


def _state_transition_label(entry: dict[str, Any]) -> str:
    from_state = str(entry.get("from") or entry.get("from_state") or "")
    to_state = str(entry.get("to") or entry.get("to_state") or "")
    at = str(entry.get("at") or entry.get("timestamp") or entry.get("created_at") or "")
    if from_state or to_state:
        label = f"{from_state or 'unknown'} -> {to_state or 'unknown'}"
    else:
        label = str(entry.get("state") or "")
    return f"{label} @ {at}" if at and label else label


def _moa_latest_record_summary(path: Path, max_bytes: int) -> tuple[str, list[str]]:
    with contextlib.suppress(OSError):
        for line in reversed(_read_tail_text(path, max_bytes).splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(stripped)
                if isinstance(data, dict):
                    keys = sorted(str(key) for key in data)[:8]
                    labels = [
                        str(data.get(field) or "")
                        for field in ("event", "type", "status", "phase", "preset")
                        if data.get(field)
                    ]
                    return (" ".join(labels[:3]) or "json record", keys)
    return "", []


def _model_cache_counts(data: dict[str, Any]) -> tuple[int, int]:
    if not data:
        return 0, 0
    model_count = 0
    for provider_data in data.values():
        provider = _as_dict(provider_data)
        models = provider.get("models")
        if isinstance(models, dict | list):
            model_count += len(models)
    return len(data), model_count


def _is_dashboard_process(command: str) -> bool:
    if "hermes dashboard" in command:
        return True
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return any(Path(part).name == "hermesd" for part in parts)
