"""Collection of kanban board state, task counts, and multi-board discovery."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import yaml

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
        ("beta-task", "Beta task", "done", 1, stale_heartbeat),
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
