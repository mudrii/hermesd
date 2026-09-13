"""Verification evidence, goals, projects, MoA and curator readers."""

from __future__ import annotations

import contextlib
import json
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
    _exists_strict,
    _iso_to_epoch,
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
    CuratorRun,
    DelegationInfo,
    DelegationLiveManifest,
    DelegationLiveTask,
    DiscoveredRepoSummary,
    GoalSummary,
    OperationsState,
    ProcessReceipt,
    ProcessReceiptsState,
    ProjectSummary,
    SkillCurationWindow,
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
# Cap on any JSON object column decoded whole (delegation task/result payloads
# and goal records). 64 KiB comfortably holds a real goal — which carries the
# full contract and subgoal list — while still refusing a runaway blob.
_JSON_COLUMN_MAX_BYTES = 64 * 1024
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
    terminal = ", ".join(f"'{state}'" for state in _DELEGATION_TERMINAL_STATES)
    failed = ", ".join(f"'{state}'" for state in _DELEGATION_FAILED_STATES)
    return {
        "delegation_count": _table_count_or_zero(conn, "async_delegations"),
        "delegation_running_count": _count_rows(
            conn,
            f"SELECT COUNT(*) FROM async_delegations WHERE COALESCE(state, '') NOT IN ({terminal})",
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
        goal=_clip_single_line(str(task.get("goal") or "")),
        result_status=str(result.get("status") or ""),
        error_excerpt=_clip_single_line(
            str(result.get("error") or "") or str(result.get("summary") or "")
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


def _json_object_capped(
    raw: object, max_bytes: int = _JSON_COLUMN_MAX_BYTES
) -> dict[str, Any] | None:
    """Decode a JSON object column, refusing payloads over ``max_bytes``.

    None means "no usable object" — absent, over the cap, malformed, or not a
    JSON object — which lets callers distinguish that from a genuine ``{}``.
    ``RecursionError`` joins the suppressed set because nesting deep enough to
    exhaust the decoder is just more junk: it must not fail the source.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if len(raw.encode("utf-8", errors="replace")) > max_bytes:
        return None
    with contextlib.suppress(json.JSONDecodeError, ValueError, RecursionError):
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            return decoded
    return None


def _clip_single_line(value: str) -> str:
    return " ".join(value.split())[:_DELEGATION_TEXT_MAX_CHARS]


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


def _read_delegation_live_manifests(live_root: Path, home: Path, *, now: float) -> dict[str, Any]:
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
    only the newest ``_MAX_LIVE_MANIFESTS`` directories are parsed into cards —
    so the panel can say "showing N of M" instead of silently truncating. A card
    materialises at most ``_MAX_LIVE_TASKS`` tasks, so at most that many
    ``task-<index>.log`` files are opened per card regardless of how many
    entries the manifest lists; ``running_task_count`` and ``tasks_truncated``
    are still computed from every raw entry, so those counts describe the whole
    batch rather than the displayed slice. A torn manifest still counts its
    delegation but yields no card; it must never fail the source.
    """
    empty = {"delegation_live_manifests": [], "delegation_live_manifest_count": 0}
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
    for mtime, run_dir in candidates:
        manifest = _live_manifest_from_dir(run_dir, home, mtime=mtime, now=now)
        if manifest is not None:
            manifests.append(manifest)
            if len(manifests) >= _MAX_LIVE_MANIFESTS:
                break
    return {
        "delegation_live_manifests": manifests,
        "delegation_live_manifest_count": count,
    }


def _live_manifest_from_dir(
    run_dir: Path, home: Path, *, mtime: float, now: float
) -> DelegationLiveManifest | None:
    """One delegation card, or None when the manifest is absent or unusable."""
    data = _json_object_capped(
        _read_text_capped(run_dir / "manifest.json", home),
        max_bytes=_LIVE_MANIFEST_MAX_BYTES,
    )
    if data is None:
        return None
    task_entries = _as_list(data.get("tasks"))
    # Only the displayed slice is materialised, so the per-task log tails stay
    # bounded by _MAX_LIVE_TASKS instead of the (unbounded) manifest size; the
    # counts below are still derived from every raw entry.
    entries = [_as_dict(entry) for entry in task_entries]
    tasks = [_live_task_from_entry(entry, run_dir, home) for entry in entries[:_MAX_LIVE_TASKS]]
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
        task_count=_coerce_int(data.get("task_count")) or len(entries),
        running_task_count=sum(
            1 for entry in entries if str(entry.get("status") or "") == "running"
        ),
        tasks=tasks,
        tasks_truncated=len(entries) > len(tasks),
    )


def _live_task_from_entry(entry: dict[str, Any], run_dir: Path, home: Path) -> DelegationLiveTask:
    index = _coerce_int(entry.get("index"))
    log_name = f"task-{index}.log"
    tail = _live_log_tail(run_dir / log_name, home)
    return DelegationLiveTask(
        index=index,
        goal=_redact_secret_text(_clip_single_line(str(entry.get("goal") or ""))),
        status=str(entry.get("status") or ""),
        exit_reason=str(entry.get("exit_reason") or ""),
        log_name=log_name if tail else "",
        log_tail=tail,
    )


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
    exit_code = _coerce_int(data.get("exit_code"))
    return ProcessReceipt(
        process_id=str(data.get("id") or path.stem),
        command=_redact_command_string(str(data.get("command") or "")),
        exit_code=exit_code if data.get("exit_code") is not None else None,
        completion_reason=str(data.get("completion_reason") or ""),
        termination_source=str(data.get("termination_source") or ""),
        started_age_seconds=_age_seconds(started_at if started_at > 0 else None, now),
        finished_age_seconds=_age_seconds(mtime, now),
        output_tail=_redact_secret_text(output[-_PROCESS_RECEIPT_TAIL_MAX_CHARS:]),
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
    return float(raw) * 3600.0


def _read_checkpoint_prune_marker(
    marker_path: Path,
    home: Path,
    *,
    now: float,
    interval_seconds: float = float(CHECKPOINT_PRUNE_INTERVAL_SECONDS),
) -> dict[str, Any]:
    """The checkpoint auto-prune wrapper's ``.last_prune`` marker.

    PROFILE scope: the marker lives in ``checkpoints/``, which upstream resolves
    through ``get_hermes_home()`` (``tools/checkpoint_manager.py:33,37-42``), and
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
    stamp = _coerce_float(_read_text_capped(marker_path, home).strip())
    age = _age_seconds(stamp if stamp > 0 else None, now)
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


def _read_state_snapshots(root: Path, home: Path, *, now: float) -> dict[str, Any]:
    """Stat state-snapshots/ one level deep, capped at 200 entries."""
    count = 0
    total_bytes = 0
    newest: float | None = None
    if _safe_child_path(root, home) and root.is_dir():
        for entry in islice(root.iterdir(), _BOUNDED_SCAN_LIMIT):
            if entry.is_symlink() or not _path_resolves_under(entry, home):
                continue
            if entry.is_dir():
                total_bytes += _immediate_file_bytes(entry)
            elif entry.is_file():
                total_bytes += entry.stat().st_size
            else:
                continue
            count += 1
            mtime = entry.stat().st_mtime
            if newest is None or mtime > newest:
                newest = mtime
    return {
        "snapshot_count": count,
        "snapshot_total_bytes": total_bytes,
        "newest_snapshot_age_seconds": max(0.0, now - newest) if newest is not None else None,
    }


def _immediate_file_bytes(directory: Path) -> int:
    total = 0
    for child in islice(directory.iterdir(), _BOUNDED_SCAN_LIMIT):
        if child.is_file() and not child.is_symlink():
            total += child.stat().st_size
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


# Curator transition thresholds, agent/curator.py:29 — 14 days to stale, 30 to
# archive — overridable per install via curator.stale_after_days /
# curator.archive_after_days (resolved by get_stale_after_days /
# get_archive_after_days, agent/curator.py:115-120).
_CURATOR_DEFAULT_STALE_AFTER_DAYS = 14
_CURATOR_DEFAULT_ARCHIVE_AFTER_DAYS = 30
# Display bound on the per-skill window table; the counts stay complete.
_SKILL_WINDOW_LIMIT = 20
_SECONDS_PER_DAY = 86400.0


def _curator_threshold_days(cfg: dict[str, Any], key: str, default: int) -> int:
    """``int(curator.<key>)`` with the default on any cast failure.

    Mirrors ``_config_number`` (``agent/curator.py:96-100``): an uncastable or
    absent value falls back to the default, while a present numeric value —
    including ``0`` — is kept exactly as the curator would keep it.
    """
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _curator_thresholds(cfg: dict[str, Any]) -> tuple[int, int, bool]:
    """Effective (stale, archive) day thresholds, and whether config overrode them."""
    customized = "stale_after_days" in cfg or "archive_after_days" in cfg
    stale = _curator_threshold_days(cfg, "stale_after_days", _CURATOR_DEFAULT_STALE_AFTER_DAYS)
    archive = _curator_threshold_days(
        cfg, "archive_after_days", _CURATOR_DEFAULT_ARCHIVE_AFTER_DAYS
    )
    return stale, archive, customized


def _usage_last_activity_epoch(record: dict[str, Any]) -> float | None:
    """Newest use/view/patch stamp as an epoch, or None when the skill never fired.

    ``created_at`` is deliberately left out — upstream excludes it so
    never-active skills stay distinguishable (``tools/skill_usage.py:106-111``).
    """
    stamps = [
        epoch
        for key in ("last_used_at", "last_viewed_at", "last_patched_at")
        if (epoch := _iso_to_epoch(record.get(key))) is not None
    ]
    return max(stamps) if stamps else None


def _skill_curation_hygiene(
    usage: dict[str, Any],
    *,
    now: float,
    stale_after_days: int,
    archive_after_days: int,
) -> dict[str, Any]:
    """Patch-reuse, state and threshold-window rollups over ``skills/.usage.json``.

    Records are what ``tools/skill_usage.py:330-340`` writes: ``state`` in
    {active, stale, archived} with ``pinned`` as a separate flag, and the patch
    loop tracked as ``patch_generation`` vs ``last_reused_patch_generation`` —
    a generation gap means the skill was patched but the patched version has
    not been re-used yet. Any other ``state`` value counts as unknown rather
    than being folded into a known bucket.
    """
    counts = {"active": 0, "stale": 0, "archived": 0}
    unknown = 0
    pinned = 0
    patch_pending = 0
    windows: list[SkillCurationWindow] = []
    for name, raw in sorted(usage.items()):
        record = raw if isinstance(raw, dict) else None
        if record is None:
            continue
        state = str(record.get("state") or "active")
        if state in counts:
            counts[state] += 1
        else:
            unknown += 1
        # ``.usage.json`` is a state payload with real booleans
        # (``set_pinned`` stores ``bool(pinned)``): a string is corruption.
        is_pinned = _coerce_bool(record.get("pinned"))
        if is_pinned:
            pinned += 1
        pending = _coerce_int(record.get("patch_generation")) > _coerce_int(
            record.get("last_reused_patch_generation")
        )
        if pending:
            patch_pending += 1
        activity = _usage_last_activity_epoch(record)
        age = _age_seconds(activity, now)
        window = SkillCurationWindow(
            name=str(name),
            state=state,
            pinned=is_pinned,
            patch_pending_reuse=pending,
            last_activity_age_seconds=age,
            days_until_stale=_days_remaining(age, stale_after_days),
            days_until_archive=_days_remaining(age, archive_after_days),
        )
        if len(windows) < _SKILL_WINDOW_LIMIT:
            windows.append(window)
        else:
            _replace_soonest_window(windows, window)
    windows.sort(key=_window_sort_key)
    return {
        "managed_skill_count": sum(counts.values()) + unknown,
        "patch_pending_reuse_count": patch_pending,
        "state_active_count": counts["active"],
        "state_stale_count": counts["stale"],
        "state_archived_count": counts["archived"],
        "state_unknown_count": unknown,
        "pinned_count": pinned,
        "skill_windows": windows,
    }


def _days_remaining(age_seconds: float | None, threshold_days: int) -> float | None:
    """Days left before a threshold, from the last activity; None with no activity."""
    if age_seconds is None:
        return None
    return threshold_days - age_seconds / _SECONDS_PER_DAY


def _window_sort_key(window: SkillCurationWindow) -> tuple[bool, float, str]:
    days = window.days_until_stale
    return (days is None, days if days is not None else 0.0, window.name)


def _replace_soonest_window(
    windows: list[SkillCurationWindow], candidate: SkillCurationWindow
) -> None:
    """Keep the soonest-deadline window when the display list is already full."""
    slowest_index = max(range(len(windows)), key=lambda idx: _window_sort_key(windows[idx]))
    if _window_sort_key(candidate) < _window_sort_key(windows[slowest_index]):
        windows[slowest_index] = candidate


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


def _is_dashboard_process(command: str) -> bool:
    if "hermes dashboard" in command:
        return True
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return any(Path(part).name == "hermesd" for part in parts)
