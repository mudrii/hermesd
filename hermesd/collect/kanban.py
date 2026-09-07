"""Kanban board SQL readers and per-board discovery."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any

from hermesd.collect.common import _coerce_int, _path_resolves_under
from hermesd.collect.sqlite_util import (
    _column_exists,
    _connect_readonly_sqlite,
    _count_by,
    _count_rows_or_zero,
    _query_rows,
    _table_count,
    _table_count_or_zero,
    _table_exists,
)
from hermesd.models import (
    KanbanBoardSummary,
    KanbanRunSummary,
    KanbanState,
    KanbanTaskLink,
    KanbanTaskSummary,
)
from hermesd.paths import HermesPaths

# Fallback for kanban.claim_ttl_seconds: a task claim older than this with no
# heartbeat is treated as abandoned. Mirrors hermes-agent's own default.
_DEFAULT_CLAIM_TTL_SECONDS = 300


def _kanban_claim_ttl_seconds(cfg: dict[str, Any]) -> int:
    for key in ("claim_ttl_seconds", "worker_claim_ttl_seconds", "claim_timeout_seconds"):
        value = _coerce_int(cfg.get(key))
        if value > 0:
            return value
    return _DEFAULT_CLAIM_TTL_SECONDS


def _read_kanban_state(db_path: Path, base_state: KanbanState, *, now: float) -> KanbanState:
    with _connect_readonly_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        status_counts = _count_by(conn, "SELECT status, COUNT(*) FROM tasks GROUP BY status")
        assignee_counts = _count_by(
            conn,
            "SELECT COALESCE(NULLIF(assignee, ''), 'unassigned'), COUNT(*) "
            "FROM tasks GROUP BY COALESCE(NULLIF(assignee, ''), 'unassigned')",
        )
        active_rows = _query_rows(
            conn,
            "SELECT * FROM tasks "
            "WHERE status IN ('in_progress', 'running', 'claimed') "
            "OR current_run_id IS NOT NULL OR worker_pid IS NOT NULL "
            "ORDER BY COALESCE(last_heartbeat_at, started_at, created_at, 0) DESC LIMIT 10",
        )
        problem_rows = _query_rows(
            conn,
            "SELECT * FROM tasks "
            "WHERE status IN ('blocked', 'failed', 'error') "
            "OR consecutive_failures > 0 OR COALESCE(last_failure_error, '') != '' "
            "ORDER BY COALESCE(last_heartbeat_at, started_at, created_at, 0) DESC LIMIT 10",
        )
        run_rows = _query_rows(
            conn,
            "SELECT * FROM task_runs ORDER BY started_at DESC, id DESC LIMIT 10",
        )
        recent_task_rows = _read_recent_enriched_tasks(conn)
        return base_state.model_copy(
            update={
                "db_present": True,
                "task_count": _table_count(conn, "tasks"),
                "run_count": _table_count(conn, "task_runs"),
                "event_count": _table_count(conn, "task_events"),
                "comment_count": _table_count(conn, "task_comments"),
                "link_count": _table_count_or_zero(conn, "task_links"),
                "attachment_count": _table_count_or_zero(conn, "task_attachments"),
                "stale_claim_count": _stale_claim_count_from_tasks(
                    conn,
                    base_state.claim_ttl_seconds,
                    now,
                ),
                "status_counts": status_counts,
                "assignee_counts": assignee_counts,
                "active_tasks": [_kanban_task_from_row(row) for row in active_rows],
                "problem_tasks": [_kanban_task_from_row(row) for row in problem_rows],
                "recent_tasks": [_kanban_task_from_row(row) for row in recent_task_rows],
                "task_links": _read_task_links(conn),
                "recent_runs": [_kanban_run_from_row(row) for row in run_rows],
            }
        )


def _read_kanban_board_summary(
    db_path: Path,
    *,
    slug: str,
    current: bool,
    claim_ttl_seconds: int,
    now: float,
) -> KanbanBoardSummary:
    with _connect_readonly_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return KanbanBoardSummary(
            slug=slug,
            current=current,
            task_count=_table_count(conn, "tasks"),
            run_count=_table_count_or_zero(conn, "task_runs"),
            problem_count=_count_rows_or_zero(
                conn,
                "SELECT COUNT(*) FROM tasks WHERE status IN ('blocked', 'failed', 'error')",
            ),
            stale_claim_count=_stale_claim_count_from_tasks(conn, claim_ttl_seconds, now),
            block_kind_counts=(
                _count_by(
                    conn,
                    "SELECT block_kind, COUNT(*) FROM tasks "
                    "WHERE COALESCE(block_kind, '') != '' GROUP BY block_kind",
                )
                if _column_exists(conn, "tasks", "block_kind")
                else {}
            ),
        )


def _stale_claim_count_from_tasks(
    conn: sqlite3.Connection, claim_ttl_seconds: int, now_epoch: float
) -> int:
    if not _table_exists(conn, "tasks"):
        return 0
    now = int(now_epoch)
    ttl = claim_ttl_seconds if claim_ttl_seconds > 0 else _DEFAULT_CLAIM_TTL_SECONDS
    conditions = []
    if _column_exists(conn, "tasks", "claim_expires"):
        conditions.append(f"COALESCE(claim_expires, 0) > 0 AND claim_expires < {now}")
    if _column_exists(conn, "tasks", "last_heartbeat_at"):
        conditions.append(f"COALESCE(last_heartbeat_at, 0) > 0 AND last_heartbeat_at < {now - ttl}")
    if not conditions:
        return 0
    return _count_rows_or_zero(
        conn,
        "SELECT COUNT(*) FROM tasks WHERE "
        + " OR ".join(f"({condition})" for condition in conditions),
    )


def _read_task_links(conn: sqlite3.Connection) -> list[KanbanTaskLink]:
    with contextlib.suppress(sqlite3.Error):
        rows = _query_rows(
            conn,
            "SELECT parent_id, child_id FROM task_links "
            "ORDER BY COALESCE(parent_id, ''), COALESCE(child_id, '') LIMIT 20",
        )
        return [
            KanbanTaskLink(
                parent_id=str(row.get("parent_id") or ""),
                child_id=str(row.get("child_id") or ""),
            )
            for row in rows
        ]
    return []


def _read_recent_enriched_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    with contextlib.suppress(sqlite3.Error):
        return _query_rows(
            conn,
            "SELECT * FROM tasks "
            "WHERE completed_at IS NOT NULL OR COALESCE(workspace_path, '') != '' "
            "OR COALESCE(goal_mode, '') != '' OR COALESCE(current_step_key, '') != '' "
            "OR COALESCE(branch_name, '') != '' "
            "ORDER BY COALESCE(completed_at, last_heartbeat_at, started_at, created_at, 0) "
            "DESC LIMIT 10",
        )
    return []


def _kanban_task_from_row(row: dict[str, Any]) -> KanbanTaskSummary:
    return KanbanTaskSummary(
        task_id=str(row.get("id") or ""),
        title=str(row.get("title") or ""),
        assignee=str(row.get("assignee") or ""),
        status=str(row.get("status") or ""),
        priority=_coerce_int(row.get("priority")),
        consecutive_failures=_coerce_int(row.get("consecutive_failures")),
        worker_pid=_coerce_int(row.get("worker_pid")),
        session_id=str(row.get("session_id") or ""),
        last_failure_error=str(row.get("last_failure_error") or ""),
        last_heartbeat_at=_coerce_int(row.get("last_heartbeat_at")),
        claim_expires=_coerce_int(row.get("claim_expires")),
        current_run_id=_coerce_int(row.get("current_run_id")),
        model_override=str(row.get("model_override") or ""),
        branch_name=str(row.get("branch_name") or ""),
        skills=str(row.get("skills") or ""),
        completed_at=_coerce_int(row.get("completed_at")),
        workspace_path=str(row.get("workspace_path") or ""),
        goal_mode=str(row.get("goal_mode") or ""),
        current_step_key=str(row.get("current_step_key") or ""),
    )


def _kanban_run_from_row(row: dict[str, Any]) -> KanbanRunSummary:
    return KanbanRunSummary(
        run_id=_coerce_int(row.get("id")),
        task_id=str(row.get("task_id") or ""),
        profile=str(row.get("profile") or ""),
        status=str(row.get("status") or ""),
        outcome=str(row.get("outcome") or ""),
        worker_pid=_coerce_int(row.get("worker_pid")),
        started_at=_coerce_int(row.get("started_at")),
        ended_at=_coerce_int(row.get("ended_at")),
        error=str(row.get("error") or ""),
        summary=str(row.get("summary") or ""),
    )


def _kanban_board_present(paths: HermesPaths, board_slug: str) -> bool:
    if not board_slug:
        return False
    if board_slug in {"root", "default"}:
        path = paths.shared_path("kanban.db")
        return (
            path.exists() and not path.is_symlink() and _path_resolves_under(path, paths.root_home)
        )
    path = paths.shared_path("kanban", "boards", board_slug, "kanban.db")
    return path.exists() and not path.is_symlink() and _path_resolves_under(path, paths.root_home)
