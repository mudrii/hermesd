"""Collection of kanban board state, task counts, and multi-board discovery."""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

import hermesd.collect.kanban as kanban_module
import hermesd.collect.sqlite_util as sqlite_util_module
from hermesd.collect.kanban import _read_recent_enriched_tasks
from hermesd.collector import (
    Collector,
    _read_kanban_state,
)
from hermesd.models import KanbanState
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import create_kanban_db_tables, render_to_str


def test_collect_kanban_discovers_multi_board_summaries(hermes_home: Path):
    root_db = hermes_home / "kanban.db"
    conn = sqlite3.connect(str(root_db))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        ("root-task", "Root task", "todo", 1),
    )
    conn.commit()
    conn.close()

    boards_dir = hermes_home / "kanban" / "boards"
    alpha_dir = boards_dir / "alpha"
    beta_dir = boards_dir / "beta"
    alpha_dir.mkdir(parents=True)
    beta_dir.mkdir()
    (hermes_home / "kanban" / "current").write_text("alpha\n")

    for board_dir, task_id in (
        (alpha_dir, "alpha-task"),
        (beta_dir, "beta-task"),
    ):
        conn = sqlite3.connect(str(board_dir / "kanban.db"))
        create_kanban_db_tables(conn)
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
            (task_id, f"{task_id} title", "todo", 1),
        )
        conn.commit()
        conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.kanban.board_count == 3
    assert state.kanban.current_board == "alpha"
    boards = {board.slug: board for board in state.kanban.boards}
    assert boards["root"].task_count == 1
    assert boards["alpha"].current is True
    assert boards["alpha"].task_count == 1
    assert boards["beta"].task_count == 1
    c.close()


def test_collect_kanban_board_problem_and_block_kind_counts(hermes_home: Path):
    board_dir = hermes_home / "kanban" / "boards" / "alpha"
    board_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(board_dir / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute("ALTER TABLE tasks ADD COLUMN block_kind TEXT")
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, block_kind) VALUES (?, ?, ?, ?, ?)",
        ("alpha-task", "Alpha task", "blocked", 1, "needs_input"),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    board = state.kanban.boards[0]
    assert board.slug == "alpha"
    assert board.problem_count == 1
    assert board.block_kind_counts == {"needs_input": 1}
    c.close()


def test_collect_kanban_stale_claim_counts_use_configured_ttl(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(yaml.dump({"kanban": {"claim_ttl_seconds": 120}}))
    board_dir = hermes_home / "kanban" / "boards" / "beta"
    board_dir.mkdir(parents=True)
    stale_heartbeat = int(time.time()) - 180
    conn = sqlite3.connect(str(board_dir / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, last_heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("beta-task", "Beta task", "running", 1, stale_heartbeat),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.kanban.claim_ttl_seconds == 120
    assert state.kanban.stale_claim_count == 0
    boards = {board.slug: board for board in state.kanban.boards}
    assert boards["beta"].stale_claim_count == 1
    c.close()


def test_collect_kanban_stale_claims_zero_when_schema_has_no_claim_columns(hermes_home: Path):
    """A pre-claims tasks schema has no stale-claim signal: count is 0."""
    board_dir = hermes_home / "kanban" / "boards" / "alpha"
    board_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(board_dir / "kanban.db"))
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        ("alpha-task", "Alpha task", "done", 1),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.kanban.stale_claim_count == 0
    assert state.kanban.boards[0].task_count == 1
    assert state.kanban.boards[0].stale_claim_count == 0
    c.close()


def test_collect_kanban_stale_claims_counted_with_only_claim_expires_column(hermes_home: Path):
    board_dir = hermes_home / "kanban" / "boards" / "alpha"
    board_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(board_dir / "kanban.db"))
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at INTEGER NOT NULL, claim_expires INTEGER)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, claim_expires) VALUES (?, ?, ?, ?, ?)",
        ("stale-task", "Stale task", "running", 1, 1),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, claim_expires) VALUES (?, ?, ?, ?, ?)",
        ("live-task", "Live task", "running", 1, int(time.time()) + 3600),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.kanban.boards[0].stale_claim_count == 1
    c.close()


def test_collect_kanban_stale_claims_counted_with_only_heartbeat_column(hermes_home: Path):
    board_dir = hermes_home / "kanban" / "boards" / "alpha"
    board_dir.mkdir(parents=True)
    now = int(time.time())
    conn = sqlite3.connect(str(board_dir / "kanban.db"))
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at INTEGER NOT NULL, last_heartbeat_at INTEGER)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, last_heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("stale-task", "Stale task", "running", 1, now - 600),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, last_heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("live-task", "Live task", "running", 1, now - 10),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.kanban.boards[0].stale_claim_count == 1
    c.close()


def test_collect_kanban_stale_claims_ignore_finished_tasks(hermes_home: Path):
    """Upstream complete_task clears claim_expires but leaves last_heartbeat_at,
    so a finished task's old heartbeat is history, not an abandoned claim."""
    board_dir = hermes_home / "kanban" / "boards" / "alpha"
    board_dir.mkdir(parents=True)
    now = int(time.time())
    conn = sqlite3.connect(str(board_dir / "kanban.db"))
    create_kanban_db_tables(conn)
    for task_id, status in (("done-task", "done"), ("todo-task", "todo"), ("run-task", "claimed")):
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, last_heartbeat_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, task_id, status, 1, now - 3600),
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.kanban.boards[0].stale_claim_count == 1


def test_collect_kanban_board_visibility_renders_from_collected_state(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(yaml.dump({"kanban": {"claim_ttl_seconds": 120}}))
    board_dir = hermes_home / "kanban" / "boards" / "alpha"
    board_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(board_dir / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute("ALTER TABLE tasks ADD COLUMN block_kind TEXT")
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, claim_expires, block_kind) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("alpha-task", "Alpha task", "blocked", 1, 1, "needs_input"),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    text = render_to_str(render_panel(11, state, Theme(), detail=True), width=120, no_color=True)

    assert "Boards" in text
    assert "alpha" in text
    assert "needs_input:1" in text
    assert "Stale" in text
    c.close()


def test_collect_kanban_preserves_board_when_board_db_corrupts(hermes_home: Path):
    boards_dir = hermes_home / "kanban" / "boards"
    board_dir = boards_dir / "alpha"
    board_dir.mkdir(parents=True)
    (hermes_home / "kanban" / "current").write_text("alpha\n")
    db_path = board_dir / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        ("alpha-task", "Alpha task", "todo", 1),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    assert first.kanban.board_count == 1

    db_path.write_bytes(b"not a sqlite database")
    second = c.collect()

    assert second.kanban.board_count == 1
    assert second.kanban.boards == first.kanban.boards
    assert "kanban" in second.health.failed_sources
    c.close()


def test_collect_kanban_preserves_last_good_when_current_board_becomes_unsafe(
    hermes_home: Path, tmp_path: Path
):
    board_dir = hermes_home / "kanban" / "boards" / "alpha"
    board_dir.mkdir(parents=True)
    current = hermes_home / "kanban" / "current"
    current.write_text("alpha\n")
    conn = sqlite3.connect(str(board_dir / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    assert first.kanban.current_board == "alpha"

    outside_current = tmp_path / "current"
    outside_current.write_text("beta\n")
    current.unlink()
    current.symlink_to(outside_current)
    second = c.collect()

    assert second.kanban == first.kanban
    assert "kanban" in second.health.failed_sources
    c.close()


def test_collect_kanban_corrupt_db_preserves_last_good(populated_hermes_home: Path):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()
    assert state1.kanban.task_count == 3

    (populated_hermes_home / "kanban.db").write_bytes(b"this is not a sqlite database")
    state2 = c.collect()

    assert state2.kanban == state1.kanban
    assert "kanban" in state2.health.failed_sources
    c.close()


def test_collect_kanban_disappearing_db_preserves_last_good(populated_hermes_home: Path):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()
    assert state1.kanban.task_count == 3

    (populated_hermes_home / "kanban.db").unlink()
    state2 = c.collect()

    assert state2.kanban == state1.kanban
    assert "kanban" in state2.health.failed_sources
    c.close()


def test_collect_kanban_unsafe_symlink_replacement_preserves_last_good(
    populated_hermes_home: Path, tmp_path: Path
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()
    assert state1.kanban.task_count == 3

    outside_db = tmp_path / "kanban.db"
    sqlite3.connect(str(outside_db)).close()
    db_path = populated_hermes_home / "kanban.db"
    db_path.unlink()
    db_path.symlink_to(outside_db)
    state2 = c.collect()

    assert state2.kanban == state1.kanban
    assert "kanban" in state2.health.failed_sources
    c.close()


def test_collect_kanban_corrupt_db_without_history_uses_default(hermes_home: Path):
    (hermes_home / "kanban.db").write_bytes(b"garbage bytes")
    c = Collector(hermes_home)
    state = c.collect()
    assert "kanban" in state.health.failed_sources
    assert state.kanban.task_count == 0
    c.close()


def test_read_kanban_state_reads_wal_database(hermes_home: Path):
    db_path = hermes_home / "kanban.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_kanban_db_tables(writer)
    writer.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures) "
        "VALUES ('t_wal', 'WAL task', 'in_progress', ?, 0)",
        (int(time.time()),),
    )
    writer.commit()
    assert db_path.with_name("kanban.db-wal").exists()

    state = _read_kanban_state(db_path, KanbanState(db_present=True), now=time.time())
    writer.close()

    assert state.task_count == 1
    assert state.active_tasks[0].task_id == "t_wal"
    assert state.status_counts == {"in_progress": 1}


def test_collect_kanban_sidecar_free_wal_board_reads_without_writing_sidecars(
    hermes_home: Path,
):
    """A checkpointed WAL board — writer closed, -wal/-shm removed — is the
    normal idle state: SQLite deletes the sidecars when the last writer exits.

    Reading it must go through the immutable route. A plain ``mode=ro`` open of
    a sidecar-free WAL database makes SQLite recreate ``-wal``/``-shm`` beside
    the monitored file (a write into the Hermes home) and fails outright on
    SQLite builds that refuse read-only WAL opens, so every kanban figure must
    still read correctly with no sidecar appearing.
    """
    db_path = hermes_home / "kanban.db"
    writer = sqlite3.connect(str(db_path))
    create_kanban_db_tables(writer)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('t_idle', 'Idle WAL task', 'review', 1)"
    )
    writer.execute(
        "INSERT INTO task_events (task_id, kind, created_at) VALUES ('t_idle', 'status', 1)"
    )
    _insert_notify_sub(writer, "t_idle", "discord", last_event_id=0)
    writer.commit()
    writer.close()
    # Closing the last writer checkpointed and removed the sidecars — the state
    # the kept-open-writer WAL tests never exercise.
    assert not db_path.with_name("kanban.db-wal").exists()
    assert not db_path.with_name("kanban.db-shm").exists()

    board_dir = hermes_home / "kanban" / "boards" / "idle"
    board_dir.mkdir(parents=True)
    board_db = board_dir / "kanban.db"
    board_writer = sqlite3.connect(str(board_db))
    create_kanban_db_tables(board_writer)
    assert board_writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    board_writer.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('b_idle', 'Board task', 'todo', 1)"
    )
    board_writer.commit()
    board_writer.close()
    assert not board_db.with_name("kanban.db-wal").exists()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    # No sidecars may appear beside either monitored database.
    assert not db_path.with_name("kanban.db-wal").exists()
    assert not db_path.with_name("kanban.db-shm").exists()
    assert not board_db.with_name("kanban.db-wal").exists()
    assert not board_db.with_name("kanban.db-shm").exists()
    assert "kanban" not in state.health.failed_sources
    assert "kanban_notify" not in state.health.failed_sources
    assert state.kanban.task_count == 1
    assert state.kanban.status_counts == {"review": 1}
    assert state.kanban.notify_sub_count == 1
    boards = {board.slug: board for board in state.kanban.boards}
    assert boards["idle"].task_count == 1


def test_deleted_board_releases_its_wal_snapshot(hermes_home: Path):
    """A board removed from disk must not leave its temp WAL copy behind."""
    board_dir = hermes_home / "kanban" / "boards" / "gone"
    board_dir.mkdir(parents=True)
    board_db = board_dir / "kanban.db"
    writer = sqlite3.connect(str(board_db))
    create_kanban_db_tables(writer)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t', 'Task', 'todo', 1)"
    )
    writer.commit()

    c = Collector(hermes_home)
    try:
        c.collect()
        snapshot = c._kanban_snapshots[board_db]
        assert snapshot[2] is not None
        snapshot_dir = Path(snapshot[2].name)
        assert snapshot_dir.exists()

        writer.close()
        shutil.rmtree(board_dir)
        c.collect()

        assert board_db not in c._kanban_snapshots
        assert not snapshot_dir.exists()
    finally:
        c.close()


def test_collect_kanban_null_columns_coerced(populated_hermes_home: Path):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()
    assert "kanban" not in state.health.failed_sources

    tasks = {task.task_id: task for task in state.kanban.active_tasks}
    null_task = tasks["t_null"]
    assert null_task.assignee == ""
    assert null_task.last_failure_error == ""
    assert null_task.priority == 0
    assert null_task.worker_pid == 0
    assert null_task.session_id == ""
    assert null_task.model_override == ""
    assert null_task.branch_name == ""
    assert state.kanban.assignee_counts.get("unassigned") == 1
    # The fixture kanban.db has no task_links/task_attachments tables, so
    # _table_count_or_zero must tolerate the absent tables and report zero.
    assert state.kanban.link_count == 0
    assert state.kanban.attachment_count == 0

    runs = {run.run_id: run for run in state.kanban.recent_runs}
    null_run = runs[2]
    assert null_run.profile == ""
    assert null_run.outcome == ""
    assert null_run.error == ""
    assert null_run.summary == ""
    assert null_run.worker_pid == 0
    c.close()


def test_collect_kanban_enrichment_fields_and_link_attachment_counts(hermes_home: Path):
    """Live tasks carry workspace_path/goal_mode/current_step_key; link/attachment
    tables are counted when present."""
    db_path = hermes_home / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    create_kanban_db_tables(conn)
    conn.executescript(
        "ALTER TABLE tasks ADD COLUMN workspace_path TEXT;"
        "ALTER TABLE tasks ADD COLUMN goal_mode TEXT;"
        "ALTER TABLE tasks ADD COLUMN current_step_key TEXT;"
        "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);"
        "CREATE TABLE task_attachments (id INTEGER PRIMARY KEY, task_id TEXT);"
    )
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, current_run_id, completed_at, "
        "branch_name, workspace_path, goal_mode, current_step_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_goal",
            "Decompose milestone",
            "in_progress",
            now - 100,
            5,
            now,
            "feature/goal",
            "/work/repo",
            "autonomous",
            "step-3",
        ),
    )
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_goal', 't_child')")
    conn.execute("INSERT INTO task_attachments (task_id) VALUES ('t_goal')")
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    assert "kanban" not in state.health.failed_sources
    assert state.kanban.link_count == 1
    assert state.kanban.attachment_count == 1
    task = {t.task_id: t for t in state.kanban.active_tasks}["t_goal"]
    assert task.completed_at == now
    assert task.workspace_path == "/work/repo"
    assert task.goal_mode == "autonomous"
    assert task.current_step_key == "step-3"
    assert task.branch_name == "feature/goal"
    c.close()


def test_collect_kanban_ignores_symlinked_db_outside_home(hermes_home: Path, tmp_path: Path):
    outside_db = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(outside_db))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        ("outside_task", "Outside task", "in_progress", int(time.time())),
    )
    conn.commit()
    conn.close()
    (hermes_home / "kanban.db").symlink_to(outside_db)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.kanban.db_present is True
    assert state.kanban.task_count == 0
    assert state.kanban.active_tasks == []
    c.close()


def test_collect_kanban_enrichment_renders_from_collected_state(hermes_home: Path):
    db_path = hermes_home / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    create_kanban_db_tables(conn)
    conn.executescript(
        "ALTER TABLE tasks ADD COLUMN workspace_path TEXT;"
        "ALTER TABLE tasks ADD COLUMN goal_mode TEXT;"
        "ALTER TABLE tasks ADD COLUMN current_step_key TEXT;"
        "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);"
        "CREATE TABLE task_attachments (id INTEGER PRIMARY KEY, task_id TEXT);"
    )
    now = 1_775_791_440
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, completed_at, "
        "branch_name, workspace_path, goal_mode, current_step_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_done",
            "Completed milestone",
            "done",
            now - 100,
            now,
            "feature/done",
            "/work/done",
            "guided",
            "final",
        ),
    )
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_done', 't_child')")
    conn.execute("INSERT INTO task_attachments (task_id) VALUES ('t_done')")
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    text = render_to_str(render_panel(11, state, Theme(), detail=True), width=140)

    assert "t_done" in text
    assert "/work/done" in text
    assert "guided" in text
    assert "final" in text
    assert "t_child" in text
    c.close()


def test_kanban_skips_board_db_missing_tasks_table(hermes_home: Path, sample_kanban_db: Path):
    boards_dir = hermes_home / "kanban" / "boards"
    healthy_dir = boards_dir / "healthy"
    healthy_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(healthy_dir / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        ("t1", "healthy task", "todo", 1),
    )
    conn.commit()
    conn.close()

    broken_dir = boards_dir / "broken"
    broken_dir.mkdir(parents=True)
    # A valid SQLite file with no tasks table -> OperationalError on read.
    sqlite3.connect(str(broken_dir / "kanban.db")).close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    slugs = [board.slug for board in state.kanban.boards]
    assert "root" in slugs
    assert "healthy" in slugs
    assert "broken" not in slugs
    assert "kanban" in state.health.failed_sources
    healthy = next(board for board in state.kanban.boards if board.slug == "healthy")
    assert healthy.task_count == 1


def test_kanban_skips_board_when_wal_snapshot_fails(
    hermes_home: Path, sample_kanban_db: Path, monkeypatch
):
    """A WAL sidecar vanishing mid-copy (OSError from the snapshot) must skip
    only that board, not fail the whole kanban source."""
    import hermesd.db as db_module

    boards_dir = hermes_home / "kanban" / "boards"
    healthy_dir = boards_dir / "healthy"
    healthy_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(healthy_dir / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
        ("t1", "healthy task", "todo", 1),
    )
    conn.commit()
    conn.close()

    broken_dir = boards_dir / "broken"
    broken_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(broken_dir / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.commit()
    # A -wal sidecar forces the snapshot-copy path in _connect_readonly_sqlite.
    (broken_dir / "kanban.db-wal").write_bytes(b"wal")
    conn.close()

    real_copy2 = db_module.shutil.copy2

    def flaky_copy2(src, dst, *args, **kwargs):
        if "broken" in str(src):
            raise FileNotFoundError("wal vanished mid-copy")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(db_module.shutil, "copy2", flaky_copy2)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    slugs = [board.slug for board in state.kanban.boards]
    assert "healthy" in slugs
    assert "broken" not in slugs


# ---------------------------------------------------------------------------
# Optional-table reads: absent is empty, read errors fail the source
# ---------------------------------------------------------------------------


def test_kanban_task_links_missing_table_is_an_empty_success(hermes_home: Path):
    """A pre-links schema has no task_links table: empty result, healthy source."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "kanban" not in state.health.failed_sources
    assert state.kanban.db_present is True
    assert state.kanban.task_links == []
    assert state.kanban.link_count == 0


def test_kanban_task_links_read_error_fails_source_and_keeps_last_good(hermes_home: Path):
    """A failing task_links read is a source failure, not an empty list."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_parent', 't_child')")
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert len(first.kanban.task_links) == 1
        assert "kanban" not in first.health.failed_sources

        # A real hostile schema: the links table keeps its name but loses the
        # column the reader selects, so the read fails instead of the table
        # reading as pre-links (which is a documented, healthy degradation).
        broken = sqlite3.connect(str(hermes_home / "kanban.db"))
        broken.execute("DROP TABLE task_links")
        broken.execute("CREATE TABLE task_links (parent_id TEXT)")
        broken.commit()
        broken.close()
        second = c.collect()
        assert "kanban" in second.health.failed_sources
        assert second.kanban == first.kanban

        restored = sqlite3.connect(str(hermes_home / "kanban.db"))
        restored.execute("ALTER TABLE task_links ADD COLUMN child_id TEXT")
        restored.execute("INSERT INTO task_links VALUES ('t_parent', 't_child')")
        restored.commit()
        restored.close()
        third = c.collect()
        assert "kanban" not in third.health.failed_sources
        assert len(third.kanban.task_links) == 1
    finally:
        c.close()


def test_kanban_pre_enrichment_schema_has_no_recent_tasks(hermes_home: Path):
    """A tasks table without the enrichment columns yields an empty recent list."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Task', 'done', 1)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "kanban" not in state.health.failed_sources
    assert state.kanban.recent_tasks == []


def test_kanban_read_error_fails_source_and_keeps_last_good(hermes_home: Path):
    """A failing read is a source failure, not an empty board.

    The failure is real — the tasks table is dropped and later restored — so the
    pin covers the reader's own error handling rather than a patched query
    helper. The enriched-tasks read's degraded shapes (no completed_at, no
    enrichment columns at all) have their own unit tests below.
    """
    now = int(time.time())
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute("ALTER TABLE tasks ADD COLUMN workspace_path TEXT")
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, completed_at, workspace_path) "
        "VALUES ('t_done', 'Done task', 'done', ?, ?, '/work/repo')",
        (now - 100, now),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert [task.task_id for task in first.kanban.recent_tasks] == ["t_done"]
        assert "kanban" not in first.health.failed_sources

        broken = sqlite3.connect(str(hermes_home / "kanban.db"))
        broken.execute("DROP TABLE tasks")
        broken.commit()
        broken.close()
        second = c.collect()
        assert "kanban" in second.health.failed_sources
        assert second.kanban == first.kanban

        restored = sqlite3.connect(str(hermes_home / "kanban.db"))
        # Only the dropped table comes back; the sibling tables were never
        # touched, and the schema helper is not re-runnable.
        restored.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, "
            "created_at INTEGER, completed_at INTEGER, workspace_path TEXT, "
            "assignee TEXT, priority INTEGER, consecutive_failures INTEGER, "
            "last_failure_error TEXT, last_heartbeat_at INTEGER, started_at INTEGER, "
            "current_run_id INTEGER, worker_pid INTEGER)"
        )
        restored.execute(
            "INSERT INTO tasks (id, title, status, created_at, completed_at, workspace_path) "
            "VALUES ('t_done', 'Done task', 'done', ?, ?, '/work/repo')",
            (now - 100, now),
        )
        restored.commit()
        restored.close()
        third = c.collect()
        assert "kanban" not in third.health.failed_sources
        assert [task.task_id for task in third.kanban.recent_tasks] == ["t_done"]
    finally:
        c.close()


def test_read_recent_enriched_tasks_without_completed_at_column():
    """completed_at is optional: the condition and its sort key drop out."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id TEXT, status TEXT, workspace_path TEXT, "
        "last_heartbeat_at INTEGER, started_at INTEGER, created_at INTEGER)"
    )
    conn.execute("INSERT INTO tasks VALUES ('t1', 'done', '/work/repo', 3, 2, 1)")

    rows = _read_recent_enriched_tasks(conn)
    conn.close()

    assert [row["id"] for row in rows] == ["t1"]


def test_read_recent_enriched_tasks_without_any_enrichment_columns_returns_empty():
    """A tasks table with no enrichment columns has no enriched rows to show."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id TEXT, status TEXT, last_heartbeat_at INTEGER, "
        "started_at INTEGER, created_at INTEGER)"
    )
    conn.execute("INSERT INTO tasks VALUES ('t1', 'done', 3, 2, 1)")

    assert _read_recent_enriched_tasks(conn) == []
    conn.close()


_PRAGMA_DENYING_AUTHORIZER = lambda action, *_: (  # noqa: E731
    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA else sqlite3.SQLITE_OK
)


def test_stale_claim_count_propagates_pragma_failure():
    """A denied PRAGMA inside the column-aware claim query must propagate."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        create_kanban_db_tables(conn)
        conn.set_authorizer(_PRAGMA_DENYING_AUTHORIZER)
        with pytest.raises(sqlite3.DatabaseError):
            kanban_module._stale_claim_count_from_tasks(conn, 300, time.time())
    finally:
        conn.close()


def test_recent_enriched_tasks_propagates_pragma_failure():
    """A denied PRAGMA inside the enrichment column probes must propagate."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        create_kanban_db_tables(conn)
        conn.set_authorizer(_PRAGMA_DENYING_AUTHORIZER)
        with pytest.raises(sqlite3.DatabaseError):
            _read_recent_enriched_tasks(conn)
    finally:
        conn.close()


def test_kanban_pragma_failure_fails_source_and_keeps_last_good(hermes_home: Path, monkeypatch):
    """A real PRAGMA denial on the kanban connection fails the kanban source."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Task', 'todo', 1)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.kanban.task_count == 1
        assert "kanban" not in first.health.failed_sources

        real_connect = kanban_module._connect_readonly_sqlite

        @contextlib.contextmanager
        def pragma_denying_connect(db_path):
            with real_connect(db_path) as conn:
                conn.set_authorizer(_PRAGMA_DENYING_AUTHORIZER)
                yield conn

        monkeypatch.setattr(kanban_module, "_connect_readonly_sqlite", pragma_denying_connect)
        second = c.collect()
        assert "kanban" in second.health.failed_sources
        assert second.kanban == first.kanban

        monkeypatch.setattr(kanban_module, "_connect_readonly_sqlite", real_connect)
        third = c.collect()
        assert "kanban" not in third.health.failed_sources
        assert third.kanban.task_count == 1
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Notify subscriptions: the kanban_notify source over kanban_notify_subs
# ---------------------------------------------------------------------------


def _insert_notify_sub(
    conn: sqlite3.Connection,
    task_id: str,
    platform: str,
    *,
    last_event_id: int = 0,
    notifier_profile: str | None = None,
    delivery_mode: str = "notify",
    chat_id: str = "chat-1",
    thread_id: str = "",
) -> None:
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, "
        "notifier_profile, delivery_mode, created_at, last_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, platform, chat_id, thread_id, notifier_profile, delivery_mode, 1, last_event_id),
    )


def test_collect_kanban_notify_backlog_counts_and_platform_rollup(hermes_home: Path):
    """Per-sub backlog counts this task's events newer than the cursor; platforms
    roll up case-insensitively, matching notifier routing."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Watched', 'review', 1)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t2', 'Quiet', 'done', 1)"
    )
    for _ in range(5):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) VALUES ('t1', 'status', 1)"
        )
    for _ in range(2):
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) VALUES ('t2', 'status', 1)"
        )
    _insert_notify_sub(conn, "t1", "Discord", last_event_id=3)
    _insert_notify_sub(conn, "t1", "discord", chat_id="chat-2", last_event_id=5)
    _insert_notify_sub(conn, "t2", "Slack", last_event_id=7)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "kanban_notify" not in state.health.failed_sources
    assert state.kanban.notify_sub_count == 3
    assert state.kanban.notify_platform_counts == {"discord": 2, "slack": 1}
    assert state.kanban.notify_backlog_total == 2
    assert state.kanban.notify_max_backlog == 2
    backlog = state.kanban.notify_backlog_subs
    # The row keeps the platform as stored; only the rollup lowercases.
    assert [(sub.task_id, sub.platform) for sub in backlog] == [("t1", "Discord")]
    assert backlog[0].last_event_id == 3
    assert backlog[0].max_event_id == 5
    assert backlog[0].backlog == 2


def test_collect_kanban_notify_backlog_counts_only_this_tasks_unseen_events(hermes_home: Path):
    """task_events.id is a global autoincrement, so other tasks' interleaved
    events must not inflate the backlog: the subscription's unseen events are
    this task's rows with id > last_event_id (kanban_db_notify.py:310-337)."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Watched', 'review', 1)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t2', 'Noisy', 'todo', 1)"
    )
    conn.execute(
        "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (1, 't1', 'status', 1)"
    )
    for event_id in range(2, 22):
        conn.execute(
            "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (?, 't2', 'status', 1)",
            (event_id,),
        )
    conn.execute(
        "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (22, 't1', 'status', 1)"
    )
    _insert_notify_sub(conn, "t1", "discord", last_event_id=1)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "kanban_notify" not in state.health.failed_sources
    assert state.kanban.notify_backlog_total == 1
    assert state.kanban.notify_max_backlog == 1
    subs = state.kanban.notify_backlog_subs
    # Newest stays the raw newest event id (22); Backlog is the unseen count (1).
    assert [(sub.task_id, sub.last_event_id, sub.max_event_id, sub.backlog) for sub in subs] == [
        ("t1", 1, 22, 1)
    ]
    compact = render_to_str(render_panel(11, state, Theme()), width=100, no_color=True)
    assert "Backlog: 1" in compact
    assert "Backlog: 21" not in compact
    detail = render_to_str(render_panel(11, state, Theme(), detail=True), width=160, no_color=True)
    assert "1 unseen (max 1)" in detail
    assert "21 unseen" not in detail


def test_collect_kanban_notify_backlog_zero_when_cursor_is_past_newest_event(hermes_home: Path):
    """A cursor past this task's newest event reads as an empty backlog and
    never goes negative; a sibling sub with real unseen events still counts."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    for task_id in ("t1", "t2"):
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, 'Task', 'todo', 1)",
            (task_id,),
        )
    conn.execute(
        "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (1, 't1', 'status', 1)"
    )
    conn.execute(
        "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (2, 't1', 'status', 1)"
    )
    for event_id in range(3, 6):
        conn.execute(
            "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (?, 't2', 'status', 1)",
            (event_id,),
        )
    _insert_notify_sub(conn, "t1", "discord", last_event_id=9)
    _insert_notify_sub(conn, "t2", "slack", last_event_id=2)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "kanban_notify" not in state.health.failed_sources
    assert state.kanban.notify_sub_count == 2
    assert state.kanban.notify_backlog_total == 3
    assert state.kanban.notify_max_backlog == 3
    assert [(sub.task_id, sub.backlog) for sub in state.kanban.notify_backlog_subs] == [("t2", 3)]


def test_collect_kanban_notify_backlog_zero_for_task_without_events(hermes_home: Path):
    """A subscribed task with no events reads as zero, not as another task's
    global event-id gap."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    for task_id in ("t_quiet", "t_noisy"):
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, 'Task', 'todo', 1)",
            (task_id,),
        )
    for event_id in range(1, 21):
        conn.execute(
            "INSERT INTO task_events (id, task_id, kind, created_at) "
            "VALUES (?, 't_noisy', 'status', 1)",
            (event_id,),
        )
    _insert_notify_sub(conn, "t_quiet", "discord", last_event_id=0)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "kanban_notify" not in state.health.failed_sources
    assert state.kanban.notify_sub_count == 1
    assert state.kanban.notify_backlog_total == 0
    assert state.kanban.notify_max_backlog == 0
    assert state.kanban.notify_backlog_subs == []


def test_collect_kanban_notify_orphan_profiles(hermes_home: Path):
    """A sub whose notifier_profile has no profiles/ directory is orphaned;
    "default" names the root home upstream and "" names legacy unowned rows."""
    (hermes_home / "profiles" / "ops").mkdir(parents=True)
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    _insert_notify_sub(conn, "t1", "discord", notifier_profile="ghost")
    _insert_notify_sub(conn, "t1", "slack", notifier_profile="ops")
    _insert_notify_sub(conn, "t2", "api_server", notifier_profile="default")
    _insert_notify_sub(conn, "t2", "telegram", notifier_profile=None)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "kanban_notify" not in state.health.failed_sources
    assert state.kanban.notify_orphan_profile_count == 1
    assert state.kanban.notify_orphan_profiles == ["ghost"]


def test_kanban_notify_missing_table_reads_healthy_empty(hermes_home: Path):
    """A kanban.db without kanban_notify_subs is healthy with empty notify data."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "kanban_notify" not in state.health.failed_sources
    assert state.kanban.notify_sub_count == 0
    assert state.kanban.notify_platform_counts == {}
    assert state.kanban.notify_backlog_subs == []
    assert state.kanban.notify_orphan_profiles == []


def test_kanban_notify_read_error_restores_only_notify_fields(hermes_home: Path):
    """A failing subscription read degrades only the notify fields; the board
    itself keeps its fresh values and the notify fields keep their last good."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Task', 'todo', 1)"
    )
    _insert_notify_sub(conn, "t1", "discord", last_event_id=2)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.kanban.task_count == 1
        assert first.kanban.notify_sub_count == 1

        conn = sqlite3.connect(str(hermes_home / "kanban.db"))
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES ('t_new', 'New', 'todo', 1)"
        )
        conn.commit()
        conn.close()

        # A real hostile schema: task_events without the columns the notify
        # reader's correlated subqueries need. The board's own COUNT(*) over the
        # table still works, so exactly one reader fails — which a patched query
        # helper could only pretend to do.
        broken = sqlite3.connect(str(hermes_home / "kanban.db"))
        broken.execute("DROP TABLE task_events")
        broken.execute("CREATE TABLE task_events (kind TEXT, created_at INTEGER)")
        broken.execute("INSERT INTO task_events VALUES ('status', 1)")
        broken.commit()
        broken.close()
        second = c.collect()
        assert "kanban_notify" in second.health.failed_sources
        # Every field the source owns is restored, not just the headline count:
        # a truncated fallback list would silently blank the rest of the panel.
        assert second.kanban.notify_sub_count == first.kanban.notify_sub_count == 1
        assert second.kanban.notify_platform_counts == first.kanban.notify_platform_counts
        assert second.kanban.notify_backlog_total == first.kanban.notify_backlog_total
        assert second.kanban.notify_max_backlog == first.kanban.notify_max_backlog
        assert second.kanban.notify_backlog_subs == first.kanban.notify_backlog_subs
        assert second.kanban.notify_orphan_profile_count == first.kanban.notify_orphan_profile_count
        assert second.kanban.notify_orphan_profiles == first.kanban.notify_orphan_profiles
        assert second.kanban.task_count == 2
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Completion contracts and the failure circuit breaker
# ---------------------------------------------------------------------------


def test_collect_kanban_completion_contract_and_breaker_state(hermes_home: Path):
    """Review tasks expose their completion contract; breaker trips follow
    upstream's threshold order: task max_retries > config > default."""
    (hermes_home / "config.yaml").write_text(yaml.dump({"kanban": {"failure_limit": 3}}))
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, completion_contract) "
        "VALUES ('t_rev', 'Review task', 'review', 1, 'owner/repo')"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, completion_contract) "
        "VALUES ('t_pr', 'PR task', 'review', 1, 'https://github.com/owner/repo/pull/7')"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures) "
        "VALUES ('t_trip', 'Tripped', 'blocked', 1, 3)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures, max_retries) "
        "VALUES ('t_override', 'Override', 'in_progress', 1, 1, 1)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures) "
        "VALUES ('t_safe', 'Under limit', 'in_progress', 1, 2)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "kanban" not in state.health.failed_sources
    tasks = {
        task.task_id: task
        for task in [
            *state.kanban.active_tasks,
            *state.kanban.problem_tasks,
            *state.kanban.recent_tasks,
        ]
    }
    review = tasks["t_rev"]
    assert review.completion_contract == "owner/repo"
    assert review.breaker_limit == 3
    assert review.breaker_tripped is False
    assert tasks["t_pr"].completion_contract == "https://github.com/owner/repo/pull/7"
    tripped = tasks["t_trip"]
    assert tripped.breaker_limit == 3
    assert tripped.breaker_tripped is True
    override = tasks["t_override"]
    assert override.breaker_limit == 1
    assert override.breaker_tripped is True
    assert tasks["t_safe"].breaker_tripped is False
    # A review task with only a contract still surfaces through the
    # enrichment read so its contract can be displayed.
    assert "t_rev" in {task.task_id for task in state.kanban.recent_tasks}


def test_kanban_completion_contract_url_credentials_are_redacted(hermes_home: Path):
    """The contract is a URL and reaches both panel and snapshot, so redact it.

    Upstream's ``validate_contract`` only accepts ``OWNER/REPO`` or a clean
    ``https://github.com/OWNER/REPO/pull/N`` (``hermes_cli/kanban_pr_acceptance.py:13-21``),
    so a credentialed URL needs a direct SQL write — defense in depth, at the
    same collection seam every other URL reader redacts at.
    """
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, completion_contract) "
        "VALUES ('t_leak', 'Leaky', 'review', 1, 'https://user:tok@github.com/o/r/pull/1')"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    task = next(
        task
        for task in [
            *state.kanban.active_tasks,
            *state.kanban.problem_tasks,
            *state.kanban.recent_tasks,
        ]
        if task.task_id == "t_leak"
    )
    assert task.completion_contract == "https://[REDACTED]@github.com/o/r/pull/1"
    assert "user:tok@" not in state.model_dump_json()


def test_collect_kanban_breaker_trips_at_default_limit_without_config(hermes_home: Path):
    """With no kanban.failure_limit config the default trip count is 2
    (upstream DEFAULT_FAILURE_LIMIT)."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures) "
        "VALUES ('t_trip', 'Tripped', 'blocked', 1, 2)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    task = state.kanban.problem_tasks[0]
    assert task.breaker_limit == 2
    assert task.breaker_tripped is True


def test_kanban_breaker_limit_prefers_task_override_then_config_then_default():
    assert kanban_module._breaker_limit(None, 0) == 2
    assert kanban_module._breaker_limit(None, 3) == 3
    assert kanban_module._breaker_limit(1, 3) == 1
    # 0 is a real override upstream ("trip immediately"), not an unset marker.
    assert kanban_module._breaker_limit(0, 3) == 0
    assert kanban_module._breaker_limit(0, 0) == 0
    # A corrupt negative column clamps to the same immediate trip.
    assert kanban_module._breaker_limit(-1, 3) == 0


def test_collect_kanban_breaker_trips_immediately_when_max_retries_is_zero(hermes_home: Path):
    """max_retries = 0 is an explicit per-task override that trips the breaker
    on the first failure (kanban_db_dispatch.py:1027-1034); it must not fall
    back to the config or default limit."""
    (hermes_home / "config.yaml").write_text(yaml.dump({"kanban": {"failure_limit": 5}}))
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures, max_retries) "
        "VALUES ('t_zero', 'Zero retries', 'in_progress', 1, 1, 0)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    tasks = {
        task.task_id: task
        for task in [
            *state.kanban.active_tasks,
            *state.kanban.problem_tasks,
            *state.kanban.recent_tasks,
        ]
    }
    task = tasks["t_zero"]
    assert task.max_retries == 0
    assert task.breaker_limit == 0
    assert task.breaker_tripped is True


def test_collect_kanban_breaker_needs_a_failure_before_it_can_trip(hermes_home: Path):
    """A healthy task with ``max_retries = 0`` is not a tripped breaker.

    Upstream only compares against the limit after a failure increments the
    counter (``kanban_db_dispatch.py:1025-1033``), so the effective floor is one
    failure. Comparing the raw counter against a 0 limit reported a fresh,
    never-failed task as ``0/0 breaker tripped`` in alert style.
    """
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures, max_retries) "
        "VALUES ('t_fresh', 'Never failed', 'in_progress', 1, 0, 0)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures, max_retries) "
        "VALUES ('t_failed', 'Failed once', 'in_progress', 1, 1, 0)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    tasks = {
        task.task_id: task
        for task in [
            *state.kanban.active_tasks,
            *state.kanban.problem_tasks,
            *state.kanban.recent_tasks,
        ]
    }
    # The zero limit still reaches the panel; only the trip verdict waits for
    # the failure upstream requires.
    assert tasks["t_fresh"].breaker_limit == 0
    assert tasks["t_fresh"].breaker_tripped is False
    assert tasks["t_failed"].breaker_tripped is True


def test_collect_kanban_breaker_null_max_retries_uses_config_limit(hermes_home: Path):
    """A NULL max_retries is the only unset marker: it falls through to
    kanban.failure_limit (the default-2 path is covered without config)."""
    (hermes_home / "config.yaml").write_text(yaml.dump({"kanban": {"failure_limit": 5}}))
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures) "
        "VALUES ('t_null', 'No override', 'in_progress', 1, 4)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    task = state.kanban.active_tasks[0]
    assert task.max_retries is None
    assert task.breaker_limit == 5
    assert task.breaker_tripped is False


def test_kanban_max_retries_preserves_null_versus_zero(hermes_home: Path):
    """NULL is "no override"; a stored 0 is an explicit trip-on-first-failure one.

    Upstream keeps the two apart at the row and dispatch layers — ``_opt_int``
    passes 0 through (``kanban_db.py:1892-1894``) and the override is selected
    with ``is not None`` (``kanban_db_dispatch.py:1027-1032``) — and the
    snapshot is the only place the raw column is visible, so collapsing NULL to
    0 there erased the distinction the collector's breaker logic relies on.
    """
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures) "
        "VALUES ('t_null', 'No override', 'in_progress', 1, 0)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures, max_retries) "
        "VALUES ('t_zero', 'Zero override', 'in_progress', 1, 0, 0)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    tasks = {task.task_id: task for task in state.kanban.active_tasks}
    assert tasks["t_null"].max_retries is None
    assert tasks["t_zero"].max_retries == 0
    # Both fall back to the default limit, but only 0 is an override.
    assert tasks["t_null"].breaker_limit == 2
    assert tasks["t_zero"].breaker_limit == 0


def test_collect_kanban_breaker_task_max_retries_overrides_config_limit(hermes_home: Path):
    """A positive per-task max_retries wins over the config failure limit."""
    (hermes_home / "config.yaml").write_text(yaml.dump({"kanban": {"failure_limit": 5}}))
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures, max_retries) "
        "VALUES ('t_three', 'Three retries', 'in_progress', 1, 3, 3)"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    task = state.kanban.active_tasks[0]
    assert task.max_retries == 3
    assert task.breaker_limit == 3
    assert task.breaker_tripped is True


def test_read_kanban_notify_fields_without_required_columns_returns_empty():
    """A kanban_notify_subs table missing its cursor columns has nothing to read."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT)")
    try:
        assert (
            kanban_module._read_kanban_notify_fields(conn, known_profiles=frozenset({"ops"})) == {}
        )
    finally:
        conn.close()


def test_read_kanban_notify_fields_tolerates_missing_optional_columns():
    """platform and notifier_profile are optional: a table without them still
    yields the counts and backlog, with no platform rollup and no orphans."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kanban_notify_subs (task_id TEXT, last_event_id INTEGER);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT);
        INSERT INTO kanban_notify_subs VALUES ('t1', 0);
        INSERT INTO task_events (task_id) VALUES ('t1');
        """
    )
    try:
        fields = kanban_module._read_kanban_notify_fields(conn, known_profiles=frozenset({"ops"}))
    finally:
        conn.close()

    assert fields["notify_sub_count"] == 1
    assert fields["notify_platform_counts"] == {}
    assert fields["notify_backlog_total"] == 1
    assert [sub.task_id for sub in fields["notify_backlog_subs"]] == ["t1"]
    assert fields["notify_orphan_profile_count"] == 0


def test_kanban_notify_orphan_list_is_capped_while_the_count_is_exact(hermes_home: Path):
    """Eight orphaned profiles must not all be listed, but all must be counted."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Task', 'todo', 1)"
    )
    for index in range(8):
        _insert_notify_sub(
            conn, "t1", "discord", chat_id=f"chat-{index}", notifier_profile=f"ghost-{index}"
        )
    conn.commit()
    conn.close()
    profiles = hermes_home / "profiles"
    profiles.mkdir(exist_ok=True)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.kanban.notify_orphan_profile_count == 8
    assert len(state.kanban.notify_orphan_profiles) == 5
    assert state.kanban.notify_orphan_profiles[0] == "ghost-0"


def test_kanban_notify_read_is_capped_while_the_totals_stay_exact(hermes_home: Path):
    """The subscription scan reads a bounded row list; every total stays exact.

    ``kanban_notify_subs`` has no natural bound — the gateway garbage-collects
    stale rows, but a long-lived board can hold one row per watcher — and each
    row costs two correlated ``task_events`` subqueries. Every sibling query in
    the module caps its read with ``LIMIT``; this one did not, so the panel
    materialized the whole table to display ten rows. The counts are taken from
    aggregates so the cap cannot make a total wrong.
    """
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    conn.row_factory = sqlite3.Row
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Watched', 'review', 1)"
    )
    for event_id in range(1, 11):
        conn.execute(
            "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (?, 't1', 'status', 1)",
            (event_id,),
        )
    # Distinct backlogs: the cursor walks 0..10 across forty watchers.
    for index in range(40):
        _insert_notify_sub(
            conn,
            "t1",
            "discord" if index % 2 == 0 else "slack",
            chat_id=f"chat-{index}",
            last_event_id=index % 11,
        )
    conn.commit()

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    fields = kanban_module._read_kanban_notify_fields(conn, known_profiles=frozenset())
    conn.set_trace_callback(None)
    conn.close()

    expected_backlogs = [max(0, 10 - (index % 11)) for index in range(40)]
    assert fields["notify_sub_count"] == 40
    assert fields["notify_platform_counts"] == {"discord": 20, "slack": 20}
    assert fields["notify_backlog_total"] == sum(expected_backlogs)
    assert fields["notify_max_backlog"] == 10
    backlog = fields["notify_backlog_subs"]
    assert len(backlog) == 10  # literal: _NOTIFY_BACKLOG_SUB_LIMIT
    assert [sub.backlog for sub in backlog] == sorted(expected_backlogs, reverse=True)[:10]
    assert backlog[0].max_event_id == 10

    # The unbounded row read is gone: every statement that selects subscription
    # rows bounds itself, so the table can grow without growing the read.
    listings = [
        sql for sql in statements if "FROM kanban_notify_subs" in sql and "SELECT s." in sql
    ]
    assert listings
    assert all("LIMIT" in sql.upper() for sql in listings)


def test_kanban_db_wal_is_copied_once_per_refresh(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Every kanban reader shares one WAL snapshot per refresh.

    The board state, the per-board summaries and the notify subscriptions all
    read kanban.db, and each reader used to snapshot the WAL database on its
    own — up to three full copies of a possibly large file per refresh, which is
    the cost commit 040769e removed for state.db.
    """
    db_path = hermes_home / "kanban.db"
    writer = sqlite3.connect(str(db_path))
    create_kanban_db_tables(writer)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Watched', 'review', 1)"
    )
    writer.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t1', 'status', 1)")
    _insert_notify_sub(writer, "t1", "discord", last_event_id=0)
    writer.commit()
    # The connection stays open so the -wal sidecar exists for the whole collect.

    copies: list[Path] = []
    original = sqlite_util_module.snapshot_wal_database

    def counting(source: Path, *, prefix: str):
        copies.append(source)
        return original(source, prefix=prefix)

    monkeypatch.setattr(sqlite_util_module, "snapshot_wal_database", counting)
    try:
        c = Collector(hermes_home)
        try:
            state = c.collect()
        finally:
            c.close()
    finally:
        writer.close()

    assert [path.name for path in copies] == ["kanban.db"]
    # Both readers saw the snapshot's contents, not an empty or stale store.
    assert state.kanban.task_count == 1
    assert state.kanban.notify_sub_count == 1
    assert "kanban" not in state.health.failed_sources
    assert "kanban_notify" not in state.health.failed_sources


def test_kanban_notify_backlog_sub_count_is_exact_while_the_list_is_capped(
    hermes_home: Path,
):
    """The sub-with-backlog count is its own aggregate over the whole table, so
    the worst-ten list can be labelled "showing 10 of N" even when the table
    holds more backlogged subscriptions than the cap shows."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    conn.row_factory = sqlite3.Row
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Watched', 'review', 1)"
    )
    for event_id in range(1, 11):
        conn.execute(
            "INSERT INTO task_events (id, task_id, kind, created_at) VALUES (?, 't1', 'status', 1)",
            (event_id,),
        )
    # Every fourth watcher is caught up (cursor at the newest event); the rest
    # hold a real backlog.
    for index in range(40):
        _insert_notify_sub(
            conn,
            "t1",
            "discord",
            chat_id=f"chat-{index}",
            last_event_id=10 if index % 4 == 0 else index % 5,
        )
    conn.commit()

    fields = kanban_module._read_kanban_notify_fields(conn, known_profiles=frozenset())
    conn.close()

    assert fields["notify_sub_count"] == 40
    assert fields["notify_backlog_sub_count"] == 30
    assert fields["notify_backlog_total"] == sum(
        10 - (index % 5) for index in range(40) if index % 4
    )
    assert len(fields["notify_backlog_subs"]) == 10


def test_kanban_notify_backlog_sub_count_zero_without_task_events(hermes_home: Path):
    """Without a task_events table every cursor reads as caught up: the backlog
    sub count is 0, not an error or a guess from the sub count."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE kanban_notify_subs (
            task_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            notifier_profile TEXT,
            delivery_mode TEXT NOT NULL DEFAULT 'notify',
            created_at INTEGER NOT NULL,
            last_event_id INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, platform, chat_id, thread_id)
        );
        """
    )
    _insert_notify_sub(conn, "t1", "discord", last_event_id=7)
    conn.commit()

    fields = kanban_module._read_kanban_notify_fields(conn, known_profiles=frozenset())
    conn.close()

    assert fields["notify_sub_count"] == 1
    assert fields["notify_backlog_sub_count"] == 0
    assert fields["notify_backlog_subs"] == []


def test_kanban_notify_platform_rollup_is_capped_and_flagged(hermes_home: Path):
    """The platform rollup joins into one detail row, so it is bounded like the
    other displayed lists: the busiest platforms are kept and the cut is
    flagged instead of silently hiding every other platform."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    conn.row_factory = sqlite3.Row
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Watched', 'review', 1)"
    )
    platforms = {
        "discord": 30,
        "slack": 20,
        "telegram": 10,
        "imessage": 5,
        "sms": 4,
        "signal": 3,
        "whatsapp": 2,
        "irc": 1,
    }
    for platform, count in platforms.items():
        for index in range(count):
            _insert_notify_sub(conn, "t1", platform, chat_id=f"{platform}-{index}")
    conn.commit()

    fields = kanban_module._read_kanban_notify_fields(conn, known_profiles=frozenset())
    conn.close()

    assert fields["notify_sub_count"] == 75
    assert fields["notify_platforms_truncated"] is True
    # The busiest platforms survive the cap; counts stay exact.
    assert fields["notify_platform_counts"] == {
        "discord": 30,
        "slack": 20,
        "telegram": 10,
        "imessage": 5,
        "sms": 4,
        "signal": 3,
    }


def test_kanban_notify_platform_rollup_untruncated_under_the_cap(hermes_home: Path):
    """Under the cap the rollup is complete and the flag stays False."""
    conn = sqlite3.connect(str(hermes_home / "kanban.db"))
    conn.row_factory = sqlite3.Row
    create_kanban_db_tables(conn)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES ('t1', 'Watched', 'review', 1)"
    )
    _insert_notify_sub(conn, "t1", "discord")
    _insert_notify_sub(conn, "t1", "slack", chat_id="chat-2")
    conn.commit()

    fields = kanban_module._read_kanban_notify_fields(conn, known_profiles=frozenset())
    conn.close()

    assert fields["notify_platform_counts"] == {"discord": 1, "slack": 1}
    assert fields["notify_platforms_truncated"] is False
