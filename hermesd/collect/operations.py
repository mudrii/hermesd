"""Verification evidence, goals, delegations, process receipts, state snapshots,
projects and MoA readers."""

from __future__ import annotations

import contextlib
import functools
import json
import math
import os
import shlex
import sqlite3
from collections.abc import Callable, Mapping
from itertools import islice
from pathlib import Path
from typing import Any, NamedTuple

from hermesd.collect.common import (
    _MAX_TEXT_READ_BYTES,
    _age_seconds,
    _as_dict,
    _as_list,
    _coerce_bool,
    _coerce_float,
    _coerce_int,
    _excerpt,
    _exists_strict,
    _file_size,
    _iso_to_epoch,
    _json_object_capped,
    _mtime,
    _optional_int,
    _path_resolves_under,
    _read_tail_text,
    _read_text_capped,
    _safe_capped_file,
    _safe_child_path,
    _safe_mtime,
)
from hermesd.collect.kanban import _kanban_board_present
from hermesd.collect.redaction import _redact_command_string, _redact_secret_text
from hermesd.collect.sqlite_util import (
    _count_rows,
    _query_rows,
    _table_count,
    _table_count_or_zero,
    _table_exists,
)
from hermesd.models import (
    CHECKPOINT_PRUNE_INTERVAL_SECONDS,
    CacheDirUsage,
    DatabaseJournal,
    DelegationInfo,
    DelegationLiveManifest,
    DelegationLiveTask,
    DiscoveredRepoSummary,
    DiskUsageState,
    GoalSummary,
    LogFileUsage,
    OperationsState,
    PendingActionSubsystem,
    ProcessReceipt,
    ProcessReceiptsState,
    ProjectSummary,
    StateSnapshotSummary,
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


# Upstream's live set (``tools/async_delegation.py:68`` ``_LIVE_STATES``): a row
# in any other state is finished. Only ``running``/``finalizing`` are persisted
# (``:151``, recovery at ``:251``), ``stalling`` is in-memory, but all three are
# read as live so a future persisted ``stalling`` is not miscounted.
_DELEGATION_LIVE_STATES = ("running", "stalling", "finalizing")
# Every persisted non-success terminal state: the child's own ``error``/
# ``failed``/``timeout``/``interrupted`` status (``_persist_completion`` stores
# ``event["status"]``, ``:188-195``), the stall finalization ``stalled``
# (``:894,927``) and the abandoned-owner recovery ``unknown`` (``:274``).
_DELEGATION_FAILED_STATES = ("error", "failed", "timeout", "stalled", "unknown", "interrupted")
# Width of a delegation goal / error excerpt; the JSON payloads themselves are
# bounded by ``_json_object_capped``.
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
    """The 10 newest async_delegations rows; the table is absent on old agents.

    Once the table exists, read errors propagate so the operations source fails
    to its last-good value instead of reporting a false empty list.
    """
    if not _table_exists(conn, "async_delegations"):
        return []
    return _query_rows(
        conn,
        "SELECT * FROM async_delegations ORDER BY dispatched_at DESC LIMIT 10",
    )


def _delegation_counts(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "async_delegations"):
        return {}
    live = ", ".join(f"'{state}'" for state in _DELEGATION_LIVE_STATES)
    failed = ", ".join(f"'{state}'" for state in _DELEGATION_FAILED_STATES)
    return {
        "delegation_count": _table_count_or_zero(conn, "async_delegations"),
        "delegation_running_count": _count_rows(
            conn,
            f"SELECT COUNT(*) FROM async_delegations WHERE COALESCE(state, '') IN ({live})",
        ),
        "delegation_failed_count": _count_rows(
            conn,
            f"SELECT COUNT(*) FROM async_delegations WHERE COALESCE(state, '') IN ({failed})",
        ),
        "delegation_undelivered_count": _count_rows(
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
    task = _json_object_capped(row.get("task_json")) or {}
    result_object = _json_object_capped(row.get("result_json")) or {}
    result = _first_delegation_result(result_object)
    handed_off, orphaned, unread = _delegation_process_counts(result_object)
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
        goal=_excerpt(task.get("goal") or "", _DELEGATION_TEXT_MAX_CHARS),
        result_status=str(result.get("status") or ""),
        error_excerpt=_excerpt(
            str(result.get("error") or "") or str(result.get("summary") or ""),
            _DELEGATION_TEXT_MAX_CHARS,
        ),
        owner_alive=bool(owner_pid) and pid_exists(owner_pid),
        handed_off_count=handed_off,
        orphaned_count=orphaned,
        unread_completion_count=unread,
    )


def _delegation_process_counts(result: dict[str, Any]) -> tuple[int, int, int]:
    """Sum the per-child background-process accounting across one payload.

    Upstream stamps each finished child entry with ``handed_off_processes``,
    ``orphaned_processes`` (with ``runtime_seconds``) and
    ``unread_completions`` (with ``exit_code`` and an output tail) before the
    combined result is persisted (``tools/delegate_tool_child_run.py:744-762``,
    ``tools/async_delegation.py:198-225``). A still-running unit carries the
    same shape under ``partial: true`` (``record_unit_child``, ``:208-225``), so
    partial rows count identically. hermesd keeps the counts only: session ids,
    commands and output tails never leave the payload.
    """
    handed_off = orphaned = unread = 0
    for entry in _as_list(result.get("results")):
        data = _as_dict(entry)
        handed_off += len(_as_list(data.get("handed_off_processes")))
        orphaned += len(_as_list(data.get("orphaned_processes")))
        unread += len(_as_list(data.get("unread_completions")))
    return handed_off, orphaned, unread


def _delegation_duration(
    dispatched_at: float,
    completed_at: float | None,
    now: float,
) -> float | None:
    if dispatched_at <= 0:
        return None
    end = completed_at if completed_at is not None else now
    return max(0.0, end - dispatched_at)


def _first_delegation_result(result_object: dict[str, Any]) -> dict[str, Any]:
    results = _as_list(result_object.get("results"))
    if results and isinstance(results[0], dict):
        return results[0]
    return {}


def _state_meta_entries(conn: sqlite3.Connection) -> dict[str, str]:
    """Maintenance key/value pairs, {} on agents without state_meta.

    Once the table exists, read errors propagate so the operations source fails
    to its last-good value instead of reporting false empty metadata.
    """
    if not _table_exists(conn, "state_meta"):
        return {}
    keys = ", ".join(f"'{key}'" for key in _STATE_META_MAINTENANCE_KEYS)
    rows = _query_rows(
        conn,
        f"SELECT key, value FROM state_meta WHERE key IN ({keys}) LIMIT 8",
    )
    return {str(row.get("key") or ""): str(row.get("value") or "") for row in rows}


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Max integer in schema_version's version-like column, 0 when the table is
    absent; once the table exists, introspection/read errors propagate so the
    operations source fails to its last-good value instead of a false 0."""
    if not _table_exists(conn, "schema_version"):
        return 0
    best = 0
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
    return _age_seconds(epoch if epoch > 0 else None, now)


def _iso_age_seconds(raw: str, now: float) -> float | None:
    return _age_seconds(_iso_to_epoch(raw), now)


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


# Live-delegation manifest bounds. The manifest is a small dispatch-time
# document (one entry per child task), 64 KiB refuses a runaway blob while
# holding every realistic batch; the rendered card list is capped far below the
# bounded directory scan, and only the displayed tasks are tailed per card.
_LIVE_MANIFEST_MAX_BYTES = 64 * 1024
_MAX_LIVE_MANIFESTS = 5
_MAX_LIVE_TASKS = 8
_LIVE_TAIL_MAX_BYTES = 1024
_LIVE_TAIL_MAX_LINES = 4
_LIVE_TAIL_LINE_MAX_CHARS = 160


def _live_log_tail(log_path: Path, home: Path) -> list[str]:
    """The last few redacted lines of one task log; [] when absent or unsafe.

    The log is a regular file inside the run dir (checked, never taken from the
    manifest), capped by bytes and lines before redaction so a hostile tail
    cannot spend unbounded redaction work.
    """
    if log_path.is_symlink() or not _path_resolves_under(log_path, home):
        return []
    try:
        if not log_path.is_file() or log_path.stat().st_size == 0:
            return []
        text = _read_tail_text(log_path, _LIVE_TAIL_MAX_BYTES)
    except OSError:
        return []
    lines = [
        _redact_secret_text(line.strip())[:_LIVE_TAIL_LINE_MAX_CHARS]
        for line in text.splitlines()
        if line.strip()
    ]
    return lines[-_LIVE_TAIL_MAX_LINES:]


# Process receipt bounds. Upstream keeps 64 receipts of at most 200 KiB of
# output each (``tools/process_registry_results.py:19-24``, ``:54``); hermesd
# counts every receipt in the bounded scan but parses only the newest few, and
# refuses files over the shared text cap instead of reading them whole.
_MAX_PROCESS_RECEIPTS = 8
_PROCESS_RECEIPT_TAIL_MAX_CHARS = 400


def _read_delegation_live_manifests(
    live_root: Path,
    home: Path,
    *,
    now: float,
    log_tail: Callable[[Path, Path], list[str]] = _live_log_tail,
) -> dict[str, Any]:
    """Parse ``cache/delegation/live/<id>/manifest.json`` into per-delegation cards.

    Upstream writes the manifest at dispatch and amends per-task statuses after
    the batch joins (``tools/delegation_live_log.py:255-287``); task logs sit in
    the same directory. hermesd derives each task's log name from the task index
    (``task-<index>.log``) instead of trusting the manifest's stored path. Tails
    are redacted through hermesd's own layer even though upstream pre-redacts
    the file, because this is free text reaching a panel.

    The bounded scan mirrors ``_count_delegation_live_logs``: symlinked run dirs
    and any path that resolves outside ``home`` are skipped. The count is
    presence-based (every run dir holding a capped ``manifest.json``), while
    only the newest ``_MAX_LIVE_MANIFESTS`` readable directories become cards —
    so the panel can say "showing N of M" instead of silently truncating. Every
    bounded candidate is still validated for the unparsed count. A card
    materialises at most ``_MAX_LIVE_TASKS`` tasks, so at most that many
    ``task-<index>.log`` files are opened per card regardless of how many
    entries the manifest lists; ``running_task_count`` and ``tasks_truncated``
    are still computed from every raw entry, so those counts describe the whole
    batch rather than the displayed slice. A torn manifest still counts its
    delegation but yields no card; it must never fail the source.
    """
    empty = {
        "delegation_live_manifests": [],
        "delegation_live_manifest_count": 0,
        "delegation_live_unparsed_count": 0,
    }
    if not _safe_child_path(live_root, home) or not live_root.is_dir():
        return empty
    candidates: list[tuple[float, Path]] = []
    count = 0
    with contextlib.suppress(OSError):
        for run_dir in islice(live_root.iterdir(), _BOUNDED_SCAN_LIMIT):
            if run_dir.is_symlink() or not run_dir.is_dir():
                continue
            if not _path_resolves_under(run_dir, home):
                continue
            manifest_file = run_dir / "manifest.json"
            if (
                manifest_file.is_symlink()
                or not _safe_capped_file(manifest_file, home)
                or not _exists_strict(manifest_file)
            ):
                continue
            count += 1
            candidates.append((_safe_mtime(run_dir), run_dir))
    candidates.sort(key=lambda item: item[0], reverse=True)
    manifests: list[DelegationLiveManifest] = []
    unparsed = 0
    for mtime, run_dir in candidates:
        data = _live_manifest_data(run_dir, home)
        if data is None:
            # Counted by the presence-based scan, but no card: over the parse
            # cap, torn, or not JSON. Reported so the two numbers cannot
            # silently disagree.
            unparsed += 1
        elif len(manifests) < _MAX_LIVE_MANIFESTS:
            manifests.append(
                _live_manifest_from_data(
                    data,
                    run_dir,
                    home,
                    mtime=mtime,
                    now=now,
                    log_tail=log_tail,
                )
            )
    return {
        "delegation_live_manifests": manifests,
        "delegation_live_manifest_count": count,
        "delegation_live_unparsed_count": unparsed,
    }


def _live_manifest_data(run_dir: Path, home: Path) -> dict[str, Any] | None:
    return _json_object_capped(
        _read_text_capped(run_dir / "manifest.json", home),
        max_bytes=_LIVE_MANIFEST_MAX_BYTES,
    )


def _live_manifest_from_data(
    data: dict[str, Any],
    run_dir: Path,
    home: Path,
    *,
    mtime: float,
    now: float,
    log_tail: Callable[[Path, Path], list[str]] = _live_log_tail,
) -> DelegationLiveManifest:
    """Materialize one readable manifest as a delegation card."""
    task_entries = _as_list(data.get("tasks"))
    # Only the displayed slice is materialised, so the per-task log tails stay
    # bounded by _MAX_LIVE_TASKS instead of the (unbounded) manifest size; the
    # counts below are still derived from every raw entry.
    entries = [_as_dict(entry) for entry in task_entries]
    tasks = [
        _live_task_from_entry(entry, run_dir, home, log_tail=log_tail)
        for entry in entries[:_MAX_LIVE_TASKS]
    ]
    # The run dir IS the delegation id upstream (the writer names it so); the
    # manifest's own field is ignored, so a doctored id cannot mislabel a card.
    return DelegationLiveManifest(
        delegation_id=run_dir.name,
        model=str(data.get("model") or ""),
        provider=str(data.get("provider") or ""),
        started=str(data.get("started") or ""),
        completed=str(data.get("completed") or ""),
        manifest_present=True,
        dir_age_seconds=_age_seconds(mtime, now),
        # The card's total is its own entry list, not the manifest's
        # self-reported count: upstream writes ``len(task_list)`` there, so a
        # disagreement means a torn or doctored file, and the list is what the
        # truncation label compares against.
        task_count=len(entries),
        running_task_count=sum(
            1 for entry in entries if str(entry.get("status") or "") == "running"
        ),
        tasks=tasks,
        tasks_truncated=len(entries) > len(tasks),
    )


def _live_task_from_entry(
    entry: dict[str, Any],
    run_dir: Path,
    home: Path,
    *,
    log_tail: Callable[[Path, Path], list[str]] = _live_log_tail,
) -> DelegationLiveTask:
    index = _coerce_int(entry.get("index"))
    log_name = f"task-{index}.log"
    tail = log_tail(run_dir / log_name, home)
    return DelegationLiveTask(
        index=index,
        goal=_excerpt(entry.get("goal") or "", _DELEGATION_TEXT_MAX_CHARS),
        status=str(entry.get("status") or ""),
        exit_reason=str(entry.get("exit_reason") or ""),
        log_name=log_name if tail else "",
        log_tail=tail,
    )


def _read_process_receipts(receipts_dir: Path, home: Path, *, now: float) -> ProcessReceiptsState:
    """Bounded read of PROFILE ``logs/process-results/proc_*.json`` receipts.

    Upstream writes one 0600 JSON receipt per finished terminal process
    (``tools/process_registry_results.py:48-61``) and prunes to 7 days / 64
    files (``:28-45``). Everything here is presence-first: a missing directory,
    junk JSON and oversized files are counted but never raise, and only the
    newest ``_MAX_PROCESS_RECEIPTS`` parseable receipts are listed. Command and
    output are redacted again through hermesd's own layer before they reach a
    panel, though upstream already redacts both at write time (``:56-57``).
    """
    if (
        receipts_dir.is_symlink()
        or not _path_resolves_under(receipts_dir, home)
        or not receipts_dir.is_dir()
    ):
        return ProcessReceiptsState()
    candidates: list[tuple[float, Path]] = []
    count = 0
    newest: float | None = None
    with contextlib.suppress(OSError):
        for path in islice(receipts_dir.glob("proc_*.json"), _BOUNDED_SCAN_LIMIT):
            if path.is_symlink() or not _path_resolves_under(path, home):
                continue
            mtime = _safe_mtime(path)
            count += 1
            candidates.append((mtime, path))
            if newest is None or mtime > newest:
                newest = mtime
    candidates.sort(key=lambda item: item[0], reverse=True)
    receipts: list[ProcessReceipt] = []
    for mtime, path in candidates:
        if len(receipts) >= _MAX_PROCESS_RECEIPTS:
            break
        receipt = _process_receipt_from_file(path, home, mtime=mtime, now=now)
        if receipt is not None:
            receipts.append(receipt)
    return ProcessReceiptsState(
        dir_present=True,
        receipt_count=count,
        newest_receipt_age_seconds=_age_seconds(newest, now),
        receipts=receipts,
        receipts_truncated=count > len(receipts),
    )


def _process_receipt_from_file(
    path: Path, home: Path, *, mtime: float, now: float
) -> ProcessReceipt | None:
    """One parsed receipt, or None when the file is unreadable or over the cap.

    The text cap (256 KiB) doubles as the oversize refusal: upstream receipts
    can legitimately approach 200 KiB of output, so refusing at the shared cap
    keeps a worst-case file from being read whole while counting it above.
    """
    data = _json_object_capped(_read_text_capped(path, home), max_bytes=_MAX_TEXT_READ_BYTES)
    if data is None:
        return None
    output = str(data.get("output") or "")
    started_at = _coerce_float(data.get("started_at"))
    return ProcessReceipt(
        process_id=str(data.get("id") or path.stem),
        command=_redact_command_string(str(data.get("command") or "")),
        exit_code=_optional_int(data.get("exit_code")),
        completion_reason=str(data.get("completion_reason") or ""),
        termination_source=str(data.get("termination_source") or ""),
        started_age_seconds=_age_seconds(started_at if started_at > 0 else None, now),
        finished_age_seconds=_age_seconds(mtime, now),
        # Redact first, then bound: slicing ahead of the redactor can cut the
        # "Bearer "/"key=" marker off a credential and keep the token itself.
        output_tail=_redact_secret_text(output)[-_PROCESS_RECEIPT_TAIL_MAX_CHARS:],
    )


def _checkpoint_prune_interval_seconds(cfg: Mapping[str, Any]) -> float:
    """The wrapper's configured cadence in seconds (``min_interval_hours``).

    Upstream reads ``checkpoints.min_interval_hours`` before deciding whether a
    pass is due (``hermes_cli/cli.py:1133``, ``gateway/run.py:3671``) with a 24h
    default, so a marker age only means "overdue" relative to that policy. Only
    a positive real number counts; anything else keeps the default.
    """
    raw = _as_dict(cfg.get("checkpoints")).get("min_interval_hours")
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
        return float(CHECKPOINT_PRUNE_INTERVAL_SECONDS)
    try:
        interval_seconds = float(raw) * 3600.0
    except OverflowError:
        return float(CHECKPOINT_PRUNE_INTERVAL_SECONDS)
    if not math.isfinite(interval_seconds):
        return float(CHECKPOINT_PRUNE_INTERVAL_SECONDS)
    return interval_seconds


def _read_checkpoint_prune_marker(
    marker_path: Path,
    home: Path,
    *,
    now: float,
    interval_seconds: float = float(CHECKPOINT_PRUNE_INTERVAL_SECONDS),
) -> dict[str, Any]:
    """The checkpoint auto-prune wrapper's ``.last_prune`` marker.

    PROFILE scope: the marker lives in ``checkpoints/``, which upstream resolves
    through ``get_hermes_home()`` (``tools/checkpoint_manager.py:34,38-42``), and
    is written as a bare epoch after each wrapper pass (``:1106-1116``) with a
    default interval of 24h (``:1094``). A fresh marker proves the wrapper ran,
    not that pruning succeeded — per-repo failures are swallowed into the prune
    result — so nothing downstream may read this as a store-health verdict.
    Unreadable content falls back to the file mtime; an absent or unsafe marker
    is a healthy default.
    """
    update: dict[str, Any] = {
        "checkpoint_prune_marker_present": False,
        "checkpoint_prune_marker_age_seconds": None,
        "checkpoint_prune_interval_seconds": interval_seconds,
    }
    if (
        marker_path.is_symlink()
        or not _path_resolves_under(marker_path, home)
        or not _exists_strict(marker_path)
        or not marker_path.is_file()
    ):
        return update
    age = _epoch_age_seconds(_read_text_capped(marker_path, home).strip(), now)
    if age is None:
        age = _age_seconds(_safe_mtime(marker_path), now)
    update["checkpoint_prune_marker_present"] = True
    update["checkpoint_prune_marker_age_seconds"] = age
    return update


def _read_corrupt_ledger_marker(path: Path, home: Path, *, now: float) -> dict[str, Any]:
    """The ``spawn-ledger.json.corrupt`` parking bay, presence and mtime only.

    ROOT scope, pinned to the ledger itself: upstream parks an unparseable
    ``spawn-ledger.json`` beside it with ``os.replace``
    (``hermes_cli/process_identity.py:160-171``), and the ledger resolves through
    ``get_default_hermes_root()`` ("Machine-root ledger path",
    ``:27,128-137``). The parked file's contents are the corrupt bytes and are
    never read here — mtime is the only signal.
    """
    update: dict[str, Any] = {
        "spawn_ledger_corrupt_present": False,
        "spawn_ledger_corrupt_age_seconds": None,
    }
    if (
        path.is_symlink()
        or not _path_resolves_under(path, home)
        or not _exists_strict(path)
        or not path.is_file()
    ):
        return update
    update["spawn_ledger_corrupt_present"] = True
    update["spawn_ledger_corrupt_age_seconds"] = _age_seconds(_safe_mtime(path), now)
    return update


# Entries a single bounded tree walk visits before it reports a lower bound.
_TREE_WALK_MAX_ENTRIES = 5000
# A cached tree size is trusted for this long even when the top directory's
# signature is unchanged, because a nested write does not touch the top mtime.
_TREE_SIZE_CACHE_TTL_SECONDS = 600.0
# SQLite sidecars grouped with their database when parked loose.
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_MAX_SNAPSHOT_ROWS = 8
_SNAPSHOT_MANIFEST_MAX_BYTES = 64 * 1024
_SNAPSHOT_LIST_MAX_ITEMS = 8
_SNAPSHOT_TEXT_MAX_CHARS = 80

# (top-dir signature, computed-at, bytes, truncated) per walked directory.
TreeSizeCache = dict[str, tuple[tuple[int, int], float, int, bool]]


def _bounded_tree_bytes(directory: Path, max_entries: int | None = None) -> tuple[int, bool]:
    """Total regular-file bytes under ``directory``, never following symlinks.

    Visits at most ``max_entries`` entries (default ``_TREE_WALK_MAX_ENTRIES``)
    and returns ``(bytes, truncated)``; a truncated total is a lower bound. An
    entry that vanishes mid-walk contributes nothing, and an unreadable
    subdirectory is skipped rather than failing the whole walk.
    """
    limit = _TREE_WALK_MAX_ENTRIES if max_entries is None else max_entries
    total = 0
    visited = 0
    stack = [str(directory)]
    while stack:
        try:
            scan = os.scandir(stack.pop())
        except OSError:
            continue
        with scan as entries:
            for entry in entries:
                if visited >= limit:
                    return total, True
                visited += 1
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total, False


def _cached_tree_bytes(
    directory: Path,
    *,
    now: float,
    cache: TreeSizeCache | None,
    min_interval: float = 0.0,
    may_walk: bool = True,
) -> tuple[int, bool, bool] | None:
    """``_bounded_tree_bytes`` memoized per directory: ``(bytes, truncated, walked)``.

    A cached total is reused while it is younger than
    ``_TREE_SIZE_CACHE_TTL_SECONDS`` and either the directory's (mtime_ns,
    inode) is unchanged or the total is younger than ``min_interval``. With
    ``may_walk`` false a stale total is still returned (and None when there is
    none), so a caller can spread expensive walks across refreshes.
    """
    if cache is None:
        return (*_bounded_tree_bytes(directory), True)
    try:
        stat = directory.stat()
    except OSError:
        return 0, False, False
    signature = (stat.st_mtime_ns, stat.st_ino)
    key = str(directory)
    cached = cache.get(key)
    if cached is not None:
        age = now - cached[1]
        fresh = 0 <= age < _TREE_SIZE_CACHE_TTL_SECONDS and (
            cached[0] == signature or age < min_interval
        )
        if fresh or not may_walk:
            return cached[2], cached[3], False
    if not may_walk:
        return None
    size, truncated = _bounded_tree_bytes(directory)
    cache[key] = (signature, now, size, truncated)
    return size, truncated, True


def _snapshot_group_name(name: str) -> str:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _snapshot_text_list(value: object) -> list[str]:
    return [
        _excerpt(item, _SNAPSHOT_TEXT_MAX_CHARS)
        for item in _as_list(value)[:_SNAPSHOT_LIST_MAX_ITEMS]
        if isinstance(item, str) and item
    ]


def _snapshot_manifest_update(snapshot: Path, home: Path) -> dict[str, Any]:
    """Fields from one quick snapshot's ``manifest.json``; {} when absent or torn.

    Upstream writes it last into the staging dir before the rename
    (``hermes_cli/backup.py:1271-1276``). ``files`` is never read: the counts
    are enough, and the list names every copied file.
    """
    data = _json_object_capped(
        _read_text_capped(snapshot / "manifest.json", home),
        max_bytes=_SNAPSHOT_MANIFEST_MAX_BYTES,
    )
    if data is None:
        return {}
    return {
        "manifest_present": True,
        "label": _excerpt(data.get("label") or "", _SNAPSHOT_TEXT_MAX_CHARS),
        "file_count": _coerce_int(data.get("file_count")),
        "manifest_total_size": _coerce_int(data.get("total_size")),
        "failed_dbs": _snapshot_text_list(data.get("failed_dbs")),
        "oversized_skipped": _snapshot_text_list(data.get("oversized_skipped")),
    }


def _read_state_snapshots(
    root: Path,
    home: Path,
    *,
    now: float,
    size_cache: TreeSizeCache | None = None,
) -> dict[str, Any]:
    """Snapshots under ROOT ``state-snapshots/``, capped at 200 top entries.

    Directories are upstream quick snapshots, sized by a bounded recursive walk
    (a snapshot keeps ``cron/executions.db`` in a subdirectory) cached on the
    directory signature, with their ``manifest.json`` read for label and
    ``failed_dbs``. Loose files are parked databases, grouped with their SQLite
    sidecars so ``x.db``/``x.db-wal``/``x.db-shm`` count as one snapshot.
    """
    snapshots: list[StateSnapshotSummary] = []
    loose: dict[str, tuple[int, float | None]] = {}
    if _safe_child_path(root, home) and root.is_dir():
        for entry in islice(root.iterdir(), _BOUNDED_SCAN_LIMIT):
            if entry.is_symlink() or not _path_resolves_under(entry, home):
                continue
            if entry.is_dir():
                size, truncated, _ = _cached_tree_bytes(entry, now=now, cache=size_cache) or (
                    0,
                    False,
                    False,
                )
                snapshots.append(
                    StateSnapshotSummary(
                        name=entry.name,
                        kind="dir",
                        size_bytes=size,
                        size_truncated=truncated,
                        age_seconds=_age_seconds(_mtime(entry), now),
                        **_snapshot_manifest_update(entry, home),
                    )
                )
            elif entry.is_file():
                group = _snapshot_group_name(entry.name)
                size, newest = loose.get(group, (0, None))
                # An entry deleted since the type check has no mtime to offer.
                mtime = _mtime(entry)
                if mtime is not None and (newest is None or mtime > newest):
                    newest = mtime
                loose[group] = (size + _file_size(entry), newest)
    snapshots.extend(
        StateSnapshotSummary(
            name=name, kind="file", size_bytes=size, age_seconds=_age_seconds(mtime, now)
        )
        for name, (size, mtime) in loose.items()
    )
    snapshots.sort(key=lambda snap: snap.age_seconds if snap.age_seconds is not None else math.inf)
    ages = [snap.age_seconds for snap in snapshots if snap.age_seconds is not None]
    return {
        "snapshot_count": len(snapshots),
        "snapshot_total_bytes": sum(snap.size_bytes for snap in snapshots),
        "newest_snapshot_age_seconds": min(ages) if ages else None,
        "snapshots": snapshots[:_MAX_SNAPSHOT_ROWS],
        "snapshot_failed_count": sum(1 for snap in snapshots if snap.failed_dbs),
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
        # Same cap as the delegation payloads: a goal record is untrusted,
        # unbounded JSON sitting in a state_meta value column.
        data = _json_object_capped(row.get("value"))
        if data is not None:
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
            "project_archived_count": _count_rows(
                conn,
                "SELECT COUNT(*) FROM projects WHERE archived != 0",
            ),
            "project_folder_count": _table_count_or_zero(conn, "project_folders"),
            "discovered_repo_count": _table_count_or_zero(conn, "discovered_repos"),
            "project_missing_primary_path_count": _count_rows(
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
    """Newest project rows; read errors propagate so the operations source
    fails to its last-good value instead of reporting a false empty list.

    ``projects`` is the core table of projects.db: `_read_projects_state`
    already counts it unguarded before this runs, so an absent table is an
    error there, not an empty success here.
    """
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
            # Strict, like every other flag read from a DB row: the column's
            # INTEGER affinity converts a numeric spelling, but a text 'false'
            # survives as TEXT and bool() would call the project archived.
            archived=_coerce_bool(row.get("archived")),
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


def _read_discovered_repos(conn: sqlite3.Connection) -> list[DiscoveredRepoSummary]:
    """Newest discovered repos, [] on agents without the table; once it exists,
    read errors propagate to the operations source's last-good fallback."""
    if not _table_exists(conn, "discovered_repos"):
        return []
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
    """Length of a JSON array column; 0 for absent, malformed or absurdly nested
    text, which the decoder refuses with RecursionError rather than a parse error."""
    if not isinstance(value, str) or not value:
        return 0
    with contextlib.suppress(json.JSONDecodeError, RecursionError):
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return len(decoded)
    return 0


def _moa_latest_record_summary(path: Path, max_bytes: int) -> tuple[str, list[str]]:
    """Newest parseable JSON record's labels and keys; junk lines are skipped.

    A line nested deeply enough to exhaust the decoder raises RecursionError,
    which counts as junk here for the same reason a torn line does.
    """
    with contextlib.suppress(OSError):
        for line in reversed(_read_tail_text(path, max_bytes).splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            with contextlib.suppress(json.JSONDecodeError, RecursionError):
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


# Pure in the command line, and the same process table is re-checked on every
# refresh; shlex tokenizing dominated the operations source before this memo.
@functools.lru_cache(maxsize=1024)
def _is_dashboard_process(command: str) -> bool:
    if "hermes dashboard" in command:
        return True
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return any(Path(part).name == "hermesd" for part in parts)


# Subsystem directories and records per subsystem visited under pending/.
_MAX_PENDING_SUBSYSTEMS = 16


def _pending_record_created_at(path: Path, home: Path) -> float | None:
    """``created_at`` of one staged record, None when unreadable or absent.

    The record carries the full staged ``payload`` (``tools/write_approval.py:
    71-86``); it is parsed only to reach ``created_at`` and never retained.
    """
    data = _json_object_capped(_read_text_capped(path, home), max_bytes=_MAX_TEXT_READ_BYTES)
    if data is None:
        return None
    created_at = _coerce_float(data.get("created_at"))
    return created_at if created_at > 0 else math.nan


def _read_pending_actions(
    pending_root: Path,
    home: Path,
    *,
    now: float,
    created_at: Callable[[Path, Path], float | None] = _pending_record_created_at,
) -> dict[str, Any]:
    """Count and oldest age of staged writes per ``pending/<subsystem>/``.

    PROFILE scope: upstream stages under ``get_hermes_home()/"pending"``
    (``tools/write_approval.py:64-65``). Bounded to 16 subsystem dirs and 200
    records each; symlinked dirs and records are skipped. A record whose JSON
    cannot be read is counted as unreadable and aged by mtime, as is one with no
    usable ``created_at``.
    """
    subsystems: list[PendingActionSubsystem] = []
    if not _safe_child_path(pending_root, home) or not pending_root.is_dir():
        return {"pending_actions": [], "pending_action_total": 0}
    for directory in sorted(islice(pending_root.iterdir(), _MAX_PENDING_SUBSYSTEMS)):
        # The root is confined above, so a non-symlinked child dir stays under it.
        if directory.is_symlink() or not directory.is_dir():
            continue
        count = unreadable = 0
        oldest: float | None = None
        for record in islice(directory.glob("*.json"), _BOUNDED_SCAN_LIMIT):
            if record.is_symlink() or not record.is_file():
                continue
            count += 1
            stamp = created_at(record, home)
            if stamp is None:
                unreadable += 1
            if stamp is None or not math.isfinite(stamp):
                stamp = _mtime(record)
            if stamp is not None and (oldest is None or stamp < oldest):
                oldest = stamp
        if count:
            subsystems.append(
                PendingActionSubsystem(
                    subsystem=directory.name,
                    count=count,
                    oldest_age_seconds=_age_seconds(oldest, now),
                    unreadable_count=unreadable,
                )
            )
    return {
        "pending_actions": subsystems,
        "pending_action_total": sum(entry.count for entry in subsystems),
    }


# ── Disk & retention (source ``disk_usage``) ────────────────────────────────

_MIB = 1024 * 1024
# Files upstream attaches a RotatingFileHandler to (``hermes_logging.py:241-244``);
# their numbered backups (``agent.log.1``) are part of the same rotation.
_UPSTREAM_ROTATED_LOGS = frozenset({"agent.log", "errors.log", "gateway.log", "gui.log"})
_UNROTATED_LOG_WARN_BYTES = 10 * _MIB
_MAX_LOG_DIR_ENTRIES = 200
_MAX_LOG_FILE_ROWS = 12
# Growth samples: at most one a minute, an hour's worth kept per file.
_LOG_GROWTH_SAMPLE_SECONDS = 60.0
_LOG_GROWTH_WINDOW_SECONDS = 3600.0
# doctor_state.py:168-170 — cache/ entries this big outside the pruned dirs warn.
_CACHE_HOG_MIN_BYTES = 1 << 30
_PRUNED_CACHE_DIRS = frozenset({"scratch", "terminal"})
_MAX_CACHE_DIRS = 64
# doctor_state.py:354-390 — WAL info above 10 MB, warning above 50 MB.
_WAL_NOTE_BYTES = 10 * _MIB
_WAL_WARN_BYTES = 50 * _MIB
# Directory totals are re-walked at most this often even when their top
# mtime changes (sessions/ churns constantly), and at most this many
# directories are walked per refresh so one pass never pays for all of them.
_DISK_WALK_MIN_INTERVAL_SECONDS = 300.0
_DISK_WALKS_PER_PASS = 3
_SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
# The per-home databases doctor lists (``_QUICK_STATE_FILES`` ending in .db,
# ``hermes_cli/backup.py:1121-1132``, via ``doctor_platform.py:39-45``).
_KNOWN_DATABASES = (
    "state.db",
    "cron/executions.db",
    "gateway/discord_message_recovery.db",
    "projects.db",
    "response_store.db",
    "memory_store.db",
    "verification_evidence.db",
    "kanban.db",
)

# name -> [(observed-at, size)], oldest first.
LogGrowthSamples = dict[str, list[tuple[float, int]]]


def _read_journal_mode(path: Path) -> tuple[str, str]:
    """``(mode, error)`` from SQLite header byte 18: 2 = WAL, 1 = rollback.

    Mirrors ``hermes_cli/doctor_platform.py:64-81``: the file is opened for
    reading only and just its first 20 bytes are read — never through SQLite,
    which would create ``-wal``/``-shm`` sidecars beside the monitored file.
    hermesd holds no POSIX lock on these files (its own SQLite reads use
    ``immutable=1`` or a private snapshot), so closing this descriptor cannot
    drop one.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError as exc:
        return "", exc.strerror or type(exc).__name__
    if not header:
        return "", "file is empty"
    if len(header) < 20 or not header.startswith(_SQLITE_HEADER_MAGIC):
        return "", "file is not a database"
    mode = {2: "wal", 1: "rollback"}.get(header[18])
    if mode is None:
        return "", f"unrecognized file-format version {header[18]}"
    return mode, ""


def _log_growth_per_hour(
    samples: LogGrowthSamples, name: str, size: int, now: float
) -> float | None:
    """Record one size sample and return bytes/hour over the observed window."""
    history = samples.setdefault(name, [])
    if history and size < history[-1][1]:
        history.clear()  # truncated or rotated: the old window means nothing
    if not history or now - history[-1][0] >= _LOG_GROWTH_SAMPLE_SECONDS:
        history.append((now, size))
    while len(history) > 1 and now - history[0][0] > _LOG_GROWTH_WINDOW_SECONDS:
        history.pop(0)
    oldest_at, oldest_size = history[0]
    elapsed = now - oldest_at
    if elapsed < _LOG_GROWTH_SAMPLE_SECONDS:
        return None
    return (size - oldest_size) / elapsed * 3600.0


def _rotated_upstream(name: str) -> bool:
    base = name.rstrip("0123456789").removesuffix(".") if name[-1:].isdigit() else name
    return base in _UPSTREAM_ROTATED_LOGS


def _read_log_files(
    logs_dir: Path, home: Path, *, now: float, samples: LogGrowthSamples
) -> dict[str, Any]:
    files: list[LogFileUsage] = []
    total = 0
    if _safe_child_path(logs_dir, home) and logs_dir.is_dir():
        with os.scandir(logs_dir) as entries:
            for entry in islice(entries, _MAX_LOG_DIR_ENTRIES):
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                total += size
                rotated = _rotated_upstream(entry.name)
                files.append(
                    LogFileUsage(
                        name=entry.name,
                        size_bytes=size,
                        growth_bytes_per_hour=_log_growth_per_hour(samples, entry.name, size, now),
                        rotated_upstream=rotated,
                        unrotated_oversize=not rotated and size > _UNROTATED_LOG_WARN_BYTES,
                    )
                )
    seen = {entry.name for entry in files}
    for name in [name for name in samples if name not in seen]:
        del samples[name]
    files.sort(key=lambda entry: entry.size_bytes, reverse=True)
    return {
        "logs_dir_bytes": total,
        "log_file_count": len(files),
        "log_files": files[:_MAX_LOG_FILE_ROWS],
        "unrotated_oversize_count": sum(1 for entry in files if entry.unrotated_oversize),
    }


def _checkpoint_policy(cfg: Mapping[str, Any]) -> tuple[bool, int]:
    """``checkpoints.enabled`` (default false) and ``max_total_size_mb`` (default
    500), as ``checkpoint_footprint_notice`` reads them
    (``tools/checkpoint_manager.py:1213-1221``)."""
    section = _as_dict(cfg.get("checkpoints"))
    raw_cap = section.get("max_total_size_mb")
    cap = 500 if raw_cap is None else _coerce_int(raw_cap)
    return _coerce_bool(section.get("enabled")), max(0, cap)


def _wal_verdict(size: int) -> str:
    if size > _WAL_WARN_BYTES:
        return "warn"
    if size > _WAL_NOTE_BYTES:
        return "note"
    return "ok"


def _read_databases(
    home: Path, journal_mode: Callable[[Path], tuple[str, str]]
) -> list[DatabaseJournal]:
    candidates = [(name, home / name) for name in _KNOWN_DATABASES]
    boards = home / "kanban" / "boards"
    if _safe_child_path(boards, home) and boards.is_dir():
        for board in sorted(islice(boards.iterdir(), _BOUNDED_SCAN_LIMIT)):
            candidates.append((f"kanban/boards/{board.name}/kanban.db", board / "kanban.db"))
    databases: list[DatabaseJournal] = []
    for name, path in candidates:
        if path.is_symlink() or not _path_resolves_under(path, home) or not path.is_file():
            continue
        mode, error = journal_mode(path)
        databases.append(
            DatabaseJournal(name=name, size_bytes=_file_size(path), journal_mode=mode, error=error)
        )
    return databases


def _read_disk_usage(
    *,
    root_logs: Path,
    root_home: Path,
    profile_home: Path,
    cfg: Mapping[str, Any],
    now: float,
    tree_cache: TreeSizeCache,
    log_samples: LogGrowthSamples,
    journal_mode: Callable[[Path], tuple[str, str]] = _read_journal_mode,
) -> DiskUsageState:
    """Disk footprint and retention checks, mirroring ``hermes doctor``.

    ROOT ``logs/`` (every unrotated stream hermesd tails lives there); PROFILE
    ``sessions/``, ``checkpoints/``, ``cache/``, ``state.db-wal`` and the known
    databases (upstream resolves all of them through ``get_hermes_home()``).
    Directory walks are bounded, cached, re-walked at most every five minutes,
    and at most ``_DISK_WALKS_PER_PASS`` of them run in one refresh.
    """
    budget = _DISK_WALKS_PER_PASS
    pending = 0

    def walk(directory: Path) -> tuple[int, bool]:
        nonlocal budget, pending
        if not _safe_child_path(directory, profile_home) or not directory.is_dir():
            return 0, False
        result = _cached_tree_bytes(
            directory,
            now=now,
            cache=tree_cache,
            min_interval=_DISK_WALK_MIN_INTERVAL_SECONDS,
            may_walk=budget > 0,
        )
        if result is None:
            pending += 1
            return 0, False
        size, truncated, walked = result
        if walked:
            budget -= 1
        return size, truncated

    sessions_bytes, sessions_truncated = walk(profile_home / "sessions")
    checkpoints_bytes, checkpoints_truncated = walk(profile_home / "checkpoints")
    cache_dir = profile_home / "cache"
    scratch_bytes = 0
    scratch_truncated = False
    hogs: list[CacheDirUsage] = []
    if _safe_child_path(cache_dir, profile_home) and cache_dir.is_dir():
        for entry in sorted(islice(cache_dir.iterdir(), _MAX_CACHE_DIRS)):
            if entry.is_symlink() or not entry.is_dir():
                continue
            size, truncated = walk(entry)
            if entry.name == "scratch":
                scratch_bytes, scratch_truncated = size, truncated
            elif entry.name not in _PRUNED_CACHE_DIRS and size >= _CACHE_HOG_MIN_BYTES:
                hogs.append(
                    CacheDirUsage(name=entry.name, size_bytes=size, size_truncated=truncated)
                )
    hogs.sort(key=lambda hog: hog.size_bytes, reverse=True)
    enabled, cap_mb = _checkpoint_policy(cfg)
    wal_path = profile_home / "state.db-wal"
    wal_bytes = (
        _file_size(wal_path)
        if not wal_path.is_symlink() and _path_resolves_under(wal_path, profile_home)
        else 0
    )
    return DiskUsageState(
        **_read_log_files(root_logs, root_home, now=now, samples=log_samples),
        sessions_bytes=sessions_bytes,
        sessions_truncated=sessions_truncated,
        checkpoints_bytes=checkpoints_bytes,
        checkpoints_truncated=checkpoints_truncated,
        checkpoints_enabled=enabled,
        checkpoints_cap_mb=cap_mb,
        checkpoints_over_cap=enabled and cap_mb > 0 and checkpoints_bytes >= cap_mb * _MIB,
        scratch_bytes=scratch_bytes,
        scratch_truncated=scratch_truncated,
        cache_hogs=hogs,
        state_db_wal_bytes=wal_bytes,
        state_db_wal_verdict=_wal_verdict(wal_bytes),
        databases=_read_databases(profile_home, journal_mode),
        pending_walks=pending,
    )
