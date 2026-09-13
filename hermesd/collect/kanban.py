"""Kanban board SQL readers and per-board discovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from hermesd.collect.common import _coerce_int, _path_resolves_under
from hermesd.collect.sqlite_util import (
    _column_exists,
    _connect_readonly_sqlite,
    _count_by,
    _count_rows,
    _query_rows,
    _select_columns,
    _table_columns,
    _table_count,
    _table_count_or_zero,
    _table_exists,
)
from hermesd.models import (
    KanbanBoardSummary,
    KanbanNotifySubSummary,
    KanbanRunSummary,
    KanbanState,
    KanbanTaskLink,
    KanbanTaskSummary,
)
from hermesd.paths import HermesPaths

# Fallback for kanban.claim_ttl_seconds: a task claim older than this with no
# heartbeat is treated as abandoned. Mirrors hermes-agent's own default.
_DEFAULT_CLAIM_TTL_SECONDS = 300

# Failure circuit breaker: the trip threshold order is per-task max_retries >
# the dispatcher's kanban.failure_limit config > DEFAULT_FAILURE_LIMIT = 2
# (hermes_cli/kanban_db_dispatch.py:33 and _record_task_failure :986-1013,
# recompute_ready hermes_cli/kanban_db.py:2012-2050). A task's max_retries is
# an explicit override whenever the column is NOT NULL — upstream passes 0
# through its int coercion (hermes_cli/kanban_db.py:1892-1894) and switches on
# ``task_override is not None`` (kanban_db_dispatch.py:1027-1032), so a stored
# 0 is "trip on the first failure" rather than NULL's "fall through to the
# config value". The CLI itself refuses ``--max-retries 0``
# (hermes_cli/kanban.py:359-361), so such a row is a legacy or direct-DB
# write; it is still read as the override it says it is.
_DEFAULT_FAILURE_LIMIT = 2


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
                "active_tasks": [
                    _kanban_task_from_row(row, failure_limit=base_state.failure_limit)
                    for row in active_rows
                ],
                "problem_tasks": [
                    _kanban_task_from_row(row, failure_limit=base_state.failure_limit)
                    for row in problem_rows
                ],
                "recent_tasks": [
                    _kanban_task_from_row(row, failure_limit=base_state.failure_limit)
                    for row in recent_task_rows
                ],
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
            problem_count=_count_rows(
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
    return _count_rows(
        conn,
        "SELECT COUNT(*) FROM tasks WHERE "
        + " OR ".join(f"({condition})" for condition in conditions),
    )


def _read_task_links(conn: sqlite3.Connection) -> list[KanbanTaskLink]:
    """Parent/child links, [] on a pre-links schema; read errors propagate so the
    kanban source fails to its last-good value instead of reporting false empty."""
    if not _table_exists(conn, "task_links"):
        return []
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


# Text columns that postdate the original kanban tasks schema; each one is
# optional, so only columns confirmed present may appear in the query.
# completion_contract is included so a review task gated on a completion
# contract surfaces even though it matches no other enrichment predicate.
_ENRICHED_TASK_TEXT_COLUMNS = (
    "workspace_path",
    "goal_mode",
    "current_step_key",
    "branch_name",
    "completion_contract",
)


def _optional_int(value: object) -> int | None:
    """Coerce to int, preserving a genuine null (an unset max_retries column)."""
    return None if value is None else _coerce_int(value)


def _breaker_limit(max_retries: int | None, failure_limit: int) -> int:
    """Upstream trip threshold: task override, then config, then the default.

    ``max_retries`` is the raw column, so 0 survives as an immediate-trip
    override instead of being mistaken for NULL. The threshold is only ever
    reached *after* a failure, so the trip test below floors it at one
    (``kanban_db_dispatch.py:1025-1033`` increments before comparing).
    """
    if max_retries is not None:
        return max(0, max_retries)
    if failure_limit > 0:
        return failure_limit
    return _DEFAULT_FAILURE_LIMIT


def _read_recent_enriched_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Recent tasks with completion/workspace enrichment, [] when no enrichment
    column exists; read errors propagate to the kanban source's last-good fallback.

    The heartbeat/start/created sort keys are guaranteed present: the active-
    and problem-task reads above this call already reference them unguarded.
    """
    conditions = []
    order_columns = []
    if _column_exists(conn, "tasks", "completed_at"):
        conditions.append("completed_at IS NOT NULL")
        order_columns.append("completed_at")
    conditions.extend(
        f"COALESCE({column}, '') != ''"
        for column in _ENRICHED_TASK_TEXT_COLUMNS
        if _column_exists(conn, "tasks", column)
    )
    if not conditions:
        return []
    order_columns.extend(("last_heartbeat_at", "started_at", "created_at"))
    # Column names are module-level constants, never caller-supplied text.
    return _query_rows(
        conn,
        "SELECT * FROM tasks WHERE "
        + " OR ".join(conditions)
        + f" ORDER BY COALESCE({', '.join(order_columns)}, 0) DESC LIMIT 10",
    )


def _kanban_task_from_row(row: dict[str, Any], *, failure_limit: int = 0) -> KanbanTaskSummary:
    consecutive_failures = _coerce_int(row.get("consecutive_failures"))
    max_retries = _optional_int(row.get("max_retries"))
    breaker_limit = _breaker_limit(max_retries, failure_limit)
    return KanbanTaskSummary(
        task_id=str(row.get("id") or ""),
        title=str(row.get("title") or ""),
        assignee=str(row.get("assignee") or ""),
        status=str(row.get("status") or ""),
        priority=_coerce_int(row.get("priority")),
        consecutive_failures=consecutive_failures,
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
        completion_contract=str(row.get("completion_contract") or ""),
        max_retries=max_retries or 0,
        breaker_limit=breaker_limit,
        # A breaker trips on failures: upstream increments the counter before it
        # compares, so a fresh task under a 0 limit is not "0/0 tripped".
        breaker_tripped=consecutive_failures >= max(breaker_limit, 1),
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


# --- Notify subscriptions ---------------------------------------------------
#
# kanban_notify_subs is written by add_notify_sub (:67-131) and its
# unseen-event cursor is claimed/advanced/rewound by
# claim/advance/rewind_notify_cursor (:340-371, :380-395, :409-429); the
# gateway kanban-notifier is the consumer (gateway/kanban_watchers_notifier.py).
# The table lives in the same root-anchored kanban.db as the board itself —
# kanban_home() = get_default_hermes_root(), "Shared across profiles BY
# DESIGN" (hermes_cli/kanban_db.py:382-401) — so this reader is ROOT-scoped
# like the kanban source it complements.

_NOTIFY_BACKLOG_SUB_LIMIT = 10
_NOTIFY_ORPHAN_PROFILE_LIMIT = 5

# "default" is what upstream get_active_profile_name() reports for the root
# home (hermes_cli/profiles.py:1368-1382); it owns no profiles/ directory, so
# a sub stamped with it is not orphaned. ""/NULL stamps are legacy unowned
# rows that the dispatch owner covers (include_unowned, :119-146).
_DEFAULT_PROFILE_NAME = "default"


def _kanban_notify_from_row(row: dict[str, Any]) -> KanbanNotifySubSummary:
    last_event_id = _coerce_int(row.get("last_event_id"))
    max_event_id = _coerce_int(row.get("max_event_id"))
    return KanbanNotifySubSummary(
        task_id=str(row.get("task_id") or ""),
        platform=str(row.get("platform") or ""),
        notifier_profile=str(row.get("notifier_profile") or ""),
        delivery_mode=str(row.get("delivery_mode") or ""),
        last_event_id=last_event_id,
        max_event_id=max_event_id,
        backlog=max(0, _coerce_int(row.get("unseen_event_count"))),
    )


def _read_kanban_notify_fields(
    conn: sqlite3.Connection, *, known_profiles: frozenset[str] | None
) -> dict[str, Any]:
    """Notify-subscription health from an open kanban.db connection.

    ``known_profiles`` is the set of profile names under the root ``profiles/``
    store, or None when that store could not be read safely and orphan
    detection must stay silent rather than report every stamped sub orphaned.

    The table is unbounded (one row per watcher), so the row list is capped
    like every sibling query in this module and nothing that claims to be a
    total is derived from it: the counts, the platform rollup, the backlog sum
    and peak, and the distinct notifier profiles all come from their own
    aggregates over the whole table.
    """
    if not _table_exists(conn, "kanban_notify_subs"):
        return {}
    wanted = _select_columns(
        _table_columns(conn, "kanban_notify_subs"),
        ("task_id", "platform", "notifier_profile", "delivery_mode", "last_event_id"),
    )
    if not {"task_id", "last_event_id"}.issubset(wanted):
        return {}
    # Column order is not stable across migrated databases, so the select list
    # is built from confirmed names only (see sqlite_util._table_columns).
    select_list = ", ".join(f"s.{name} AS {name}" for name in wanted)
    # max_event_id is the newest event id of THIS task (the panel's "Newest"
    # column); unseen_event_count is the subscription's own backlog. task_events.id
    # is a global autoincrement, so the gap between the two is not the backlog:
    # upstream selects this task's rows with id > cursor
    # (kanban_db_notify.py:310-337).
    has_events = _table_exists(conn, "task_events")
    max_event_expr = (
        "COALESCE((SELECT MAX(e.id) FROM task_events e WHERE e.task_id = s.task_id), 0)"
        if has_events
        else "0"
    )
    unseen_expr = (
        "COALESCE((SELECT COUNT(*) FROM task_events e WHERE e.task_id = s.task_id "
        "AND e.id > s.last_event_id), 0)"
        if has_events
        else "0"
    )
    # Totals first, from aggregates over the whole table.
    sub_count = _count_rows(conn, "SELECT COUNT(*) FROM kanban_notify_subs")
    platform_counts: dict[str, int] = {}
    for row in _query_rows(
        conn, "SELECT platform, COUNT(*) AS subs FROM kanban_notify_subs GROUP BY platform"
    ):
        # Rolled up in Python so a non-ASCII platform lowercases the way
        # Python does, not the way SQLite's ASCII-only LOWER does.
        platform = str(row.get("platform") or "")
        key = platform.lower() if platform else "unknown"
        platform_counts[key] = platform_counts.get(key, 0) + _coerce_int(row.get("subs"))
    backlog_totals = (
        _query_rows(
            conn,
            "SELECT COALESCE(SUM(u), 0) AS total, COALESCE(MAX(u), 0) AS peak "
            f"FROM (SELECT {unseen_expr} AS u FROM kanban_notify_subs s)",
        )
        or [{}]
    )[0]
    orphan_names: set[str] = set()
    if known_profiles is not None:
        # Distinct stamps only: profile names are a handful, not one per sub.
        for row in _query_rows(conn, "SELECT DISTINCT notifier_profile FROM kanban_notify_subs"):
            profile = str(row.get("notifier_profile") or "").strip()
            if profile and profile != _DEFAULT_PROFILE_NAME and profile not in known_profiles:
                orphan_names.add(profile)
    # The displayed slice: the worst backlogs, capped. Only these rows carry the
    # per-row correlated reads, so the cost is bounded by the cap, not the table.
    backlog_subs = [
        _kanban_notify_from_row(row)
        for row in _query_rows(
            conn,
            f"SELECT {select_list}, {max_event_expr} AS max_event_id, "
            f"{unseen_expr} AS unseen_event_count "
            f"FROM kanban_notify_subs s WHERE {unseen_expr} > 0 "
            f"ORDER BY unseen_event_count DESC, s.task_id, s.platform "
            f"LIMIT {_NOTIFY_BACKLOG_SUB_LIMIT}",
        )
    ]
    orphans = sorted(orphan_names)
    return {
        "notify_sub_count": sub_count,
        "notify_platform_counts": platform_counts,
        "notify_backlog_total": _coerce_int(backlog_totals.get("total")),
        "notify_max_backlog": _coerce_int(backlog_totals.get("peak")),
        "notify_backlog_subs": backlog_subs,
        "notify_orphan_profile_count": len(orphans),
        "notify_orphan_profiles": orphans[:_NOTIFY_ORPHAN_PROFILE_LIMIT],
    }


def _read_kanban_notify(db_path: Path, *, known_profiles: frozenset[str] | None) -> dict[str, Any]:
    """kanban_notify_subs reads; {} on a pre-subs schema, errors propagate so
    the kanban_notify source fails to its last-good value."""
    with _connect_readonly_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return _read_kanban_notify_fields(conn, known_profiles=known_profiles)
