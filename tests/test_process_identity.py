"""Process identity: kanban worker fingerprints and spawn-ledger spawners.

Kanban records ``worker_started_at`` = ``"<instantiation epoch>|<start time>"``
at spawn (``hermes_cli/kanban_db.py:897-904,1011-1015``,
``hermes_cli/kanban_db_dispatch.py:361-413``); the spawn ledger records each
helper's ``create_time`` and its ``spawner_pid``/``spawner_create``
(``hermes_cli/process_identity.py:174-191,280-302,325-332``). A live pid whose
start time disagrees is a reused pid, never the recorded process.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import hermesd.collect.system as system_module
import hermesd.collector as collector_module
from hermesd.collector import Collector
from hermesd.models import WorkerIdentity
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import create_kanban_db_tables, render_to_str

_NOW = 1_790_000_000.0
_START = 1_789_990_000.25


def _fingerprint(start: float) -> str:
    # psutil create_time() in centiseconds off Linux (gateway/status.py:458-468).
    return f"boot-abc:1|{round(start * 100)}"


def _make_kanban(home: Path, rows: list[tuple[str, int | None, object]]) -> None:
    conn = sqlite3.connect(str(home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute("ALTER TABLE tasks ADD COLUMN worker_started_at INTEGER")
    conn.execute("ALTER TABLE task_runs ADD COLUMN worker_started_at INTEGER")
    for index, (task_id, pid, started) in enumerate(rows):
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, worker_pid, worker_started_at) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (task_id, f"task {task_id}", 1_789_000_000 + index, pid, started),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, worker_pid, started_at, worker_started_at) "
            "VALUES (?, 'running', ?, ?, ?)",
            (task_id, pid, 1_789_000_000 + index, started),
        )
    conn.commit()
    conn.close()


def _collect(home: Path, *, alive: set[int], starts: dict[int, float]):
    collector = Collector(
        home,
        clock=lambda: _NOW,
        pid_exists=lambda pid: pid in alive,
        process_start_times=lambda pids: {pid: starts[pid] for pid in pids if pid in starts},
    )
    try:
        return collector.collect()
    finally:
        collector.close()


def test_kanban_worker_identity_verdicts(hermes_home: Path):
    _make_kanban(
        hermes_home,
        [
            ("t_live", 101, _fingerprint(_START)),
            ("t_reused", 102, _fingerprint(_START)),
            ("t_dead", 103, _fingerprint(_START)),
            ("t_unverified", 104, "unverified"),
            ("t_legacy", 105, None),
            ("t_old_int", 106, round(_START * 100)),
            ("t_unobservable", 107, _fingerprint(_START)),
        ],
    )
    state = _collect(
        hermes_home,
        alive={101, 102, 104, 105, 106, 107},
        starts={101: _START + 0.5, 102: _START + 3600.0, 104: _START, 105: _START, 106: _START},
    )
    verdicts = {task.task_id: task.worker_identity for task in state.kanban.active_tasks}
    assert verdicts == {
        "t_live": WorkerIdentity.LIVE,
        "t_reused": WorkerIdentity.REUSED,
        "t_dead": WorkerIdentity.DEAD,
        "t_unverified": WorkerIdentity.UNVERIFIED,
        "t_legacy": WorkerIdentity.LEGACY,
        "t_old_int": WorkerIdentity.LIVE,
        "t_unobservable": WorkerIdentity.UNVERIFIED,
    }
    runs = {run.task_id: run.worker_identity for run in state.kanban.recent_runs}
    assert runs["t_reused"] is WorkerIdentity.REUSED
    assert state.kanban.worker_pid_reused_count == 1
    assert "kanban_worker_identity" not in state.health.failed_sources


def test_task_without_worker_has_no_verdict(hermes_home: Path):
    _make_kanban(hermes_home, [("t_idle", None, None)])
    state = _collect(hermes_home, alive=set(), starts={})
    assert state.kanban.active_tasks[0].worker_identity is WorkerIdentity.NONE


def test_linux_tick_fingerprint_is_converted_with_boot_time(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(system_module, "_proc_boot_epoch", lambda: 1_789_000_000.0)
    monkeypatch.setattr(system_module.os, "sysconf", lambda name: 100)
    epoch = system_module._fingerprint_start_epoch("boot|250000")
    assert epoch == pytest.approx(1_789_002_500.0)


def test_unparseable_fingerprint_is_unverified():
    assert system_module._fingerprint_start_epoch("boot|not-a-number") is None
    assert system_module._fingerprint_start_epoch("") is None


def test_kanban_identity_failure_keeps_last_good(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _make_kanban(hermes_home, [("t_reused", 102, _fingerprint(_START))])
    alive = {102}
    starts = {102: _START + 3600.0}
    collector = Collector(
        hermes_home,
        clock=lambda: _NOW,
        pid_exists=lambda pid: pid in alive,
        process_start_times=lambda pids: {pid: starts[pid] for pid in pids if pid in starts},
    )
    try:
        before = collector.collect()

        def boom(*args: object, **kwargs: object):
            raise RuntimeError("probe failed")

        monkeypatch.setattr(collector_module, "_kanban_worker_identity", boom)
        after = collector.collect()
    finally:
        collector.close()
    assert before.kanban.worker_pid_reused_count == 1
    assert "kanban_worker_identity" in after.health.failed_sources
    assert after.kanban.worker_pid_reused_count == 1
    assert after.kanban.active_tasks[0].worker_identity is WorkerIdentity.REUSED


def _write_ledger(home: Path, entries: list[dict[str, object]]) -> None:
    (home / "spawn-ledger.json").write_text(json.dumps(entries))


def _ledger_entry(pid: int, spawner_pid: int, **extra: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "pid": pid,
        "create_time": _START,
        "purpose": "mcp-helper",
        "install": "cc907ccae096",
        "spawner_pid": spawner_pid,
        "spawner_create": _START - 1.2,
        "registered_at": _START + 0.1,
        "argv": "node codegraph.js serve --mcp",
        "host": "",
        "port": None,
        "profile": "",
    }
    entry.update(extra)
    return entry


def test_spawn_ledger_orphans_and_reused_pids(hermes_home: Path):
    _write_ledger(
        hermes_home,
        [
            _ledger_entry(201, 301),  # spawner alive and same incarnation
            _ledger_entry(202, 302),  # spawner gone -> orphaned helper
            _ledger_entry(203, 303),  # spawner pid reused -> orphaned helper
            _ledger_entry(204, 304),  # helper pid itself reused
            _ledger_entry(205, 0),  # no recorded spawner
            _ledger_entry(206, 306, spawner_create=None),  # spawner create unknown: pid only
        ],
    )
    state = _collect(
        hermes_home,
        alive={201, 202, 203, 204, 205, 206, 301, 303, 306},
        starts={
            201: _START,
            202: _START,
            203: _START,
            204: _START + 900.0,
            205: _START,
            206: _START,
            301: _START - 1.2,
            303: _START + 500.0,
            306: _START + 5000.0,
        },
    )
    by_pid = {process.pid: process for process in state.background_processes}
    assert by_pid[201].identity is WorkerIdentity.LIVE
    assert by_pid[201].spawner_pid == 301
    assert by_pid[201].orphaned is False
    assert by_pid[202].orphaned is True
    assert by_pid[203].orphaned is True
    assert by_pid[204].identity is WorkerIdentity.REUSED
    assert by_pid[204].orphaned is False  # the helper itself is gone
    assert by_pid[205].orphaned is False
    assert by_pid[206].orphaned is False
    assert "process_identity" not in state.health.failed_sources


def test_ledger_entry_without_a_pid_gets_no_verdict(hermes_home: Path):
    _write_ledger(
        hermes_home,
        [_ledger_entry(201, 301), {"pid": 0, "session_id": "pending-spawn", "purpose": "x"}],
    )
    state = _collect(hermes_home, alive={201, 301}, starts={201: _START, 301: _START - 1.2})
    by_session = {process.session_id: process for process in state.background_processes}
    assert by_session["pending-spawn"].identity is WorkerIdentity.NONE


def test_legacy_processes_json_entries_get_no_ledger_verdict(hermes_home: Path):
    (hermes_home / "processes.json").write_text(
        json.dumps([{"session_id": "s1", "command": "sleep 1", "pid": 401}])
    )
    state = _collect(hermes_home, alive={401}, starts={401: _START})
    process = state.background_processes[0]
    assert process.identity is WorkerIdentity.NONE
    assert process.orphaned is False


def test_orphans_and_reused_pids_render(hermes_home: Path):
    _write_ledger(hermes_home, [_ledger_entry(202, 302), _ledger_entry(204, 304)])
    _make_kanban(hermes_home, [("t_reused", 102, _fingerprint(_START))])
    state = _collect(
        hermes_home,
        alive={202, 204, 102},
        starts={202: _START, 204: _START + 900.0, 102: _START + 3600.0},
    )
    tools_detail = render_to_str(
        render_panel(4, state, Theme(), detail=True), width=200, no_color=True
    )
    tools_compact = render_to_str(render_panel(4, state, Theme()), width=100, no_color=True)
    assert "orphaned (spawner 302 gone)" in tools_detail
    assert "pid reused" in tools_detail
    assert "1 orphaned" in tools_compact
    kanban_detail = render_to_str(
        render_panel(11, state, Theme(), detail=True), width=200, no_color=True
    )
    kanban_compact = render_to_str(render_panel(11, state, Theme()), width=100, no_color=True)
    assert "102 reused" in kanban_detail
    assert "Reused worker PIDs: 1" in kanban_compact
