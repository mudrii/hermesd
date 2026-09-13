"""Collection of kanban board state, task counts, and multi-board discovery."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

import hermesd.collect.kanban as kanban_module
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
        ("stale-task", "Stale task", "done", 1, 1),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, claim_expires) VALUES (?, ?, ?, ?, ?)",
        ("live-task", "Live task", "done", 1, int(time.time()) + 3600),
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
        ("stale-task", "Stale task", "done", 1, now - 600),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, last_heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("live-task", "Live task", "done", 1, now - 10),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.kanban.boards[0].stale_claim_count == 1
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


def test_kanban_task_links_read_error_fails_source_and_keeps_last_good(
    hermes_home: Path, monkeypatch
):
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

        real_query_rows = kanban_module._query_rows

        def flaky_query_rows(conn, sql, *args):
            if "FROM task_links" in sql:
                raise sqlite3.OperationalError("simulated task_links read failure")
            return real_query_rows(conn, sql, *args)

        monkeypatch.setattr(kanban_module, "_query_rows", flaky_query_rows)
        second = c.collect()
        assert "kanban" in second.health.failed_sources
        assert second.kanban == first.kanban

        monkeypatch.setattr(kanban_module, "_query_rows", real_query_rows)
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


def test_kanban_enriched_tasks_read_error_fails_source_and_keeps_last_good(
    hermes_home: Path, monkeypatch
):
    """A failing enriched-tasks read is a source failure, not an empty list."""
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

        real_query_rows = kanban_module._query_rows

        def flaky_query_rows(conn, sql, *args):
            if "completed_at IS NOT NULL" in sql:
                raise sqlite3.OperationalError("simulated enriched-tasks read failure")
            return real_query_rows(conn, sql, *args)

        monkeypatch.setattr(kanban_module, "_query_rows", flaky_query_rows)
        second = c.collect()
        assert "kanban" in second.health.failed_sources
        assert second.kanban == first.kanban

        monkeypatch.setattr(kanban_module, "_query_rows", real_query_rows)
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
    """Per-sub backlog is max(task_events.id) - last_event_id; platforms roll up
    case-insensitively, matching notifier routing."""
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


def test_kanban_notify_read_error_restores_only_notify_fields(hermes_home: Path, monkeypatch):
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

        real_query_rows = kanban_module._query_rows

        def flaky_query_rows(conn, sql, *args):
            if "FROM kanban_notify_subs" in sql:
                raise sqlite3.OperationalError("simulated notify-sub read failure")
            return real_query_rows(conn, sql, *args)

        monkeypatch.setattr(kanban_module, "_query_rows", flaky_query_rows)
        second = c.collect()
        assert "kanban_notify" in second.health.failed_sources
        assert second.kanban.notify_sub_count == 1
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
    assert kanban_module._breaker_limit(0, 0) == 2
    assert kanban_module._breaker_limit(0, 3) == 3
    assert kanban_module._breaker_limit(1, 3) == 1


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
