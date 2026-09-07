"""Verification evidence, goals, projects, MoA and curator readers."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sqlite3
from pathlib import Path
from typing import Any

from hermesd.collect.common import (
    _as_dict,
    _as_list,
    _coerce_int,
    _read_tail_text,
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
