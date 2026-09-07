"""Collection of verification evidence, projects, goals, git checkpoints,
MoA traces, and PR monitors."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

import hermesd.collect.sqlite_util as sqlite_util_module
import hermesd.collector as collector_module
import hermesd.db as db_module
from hermesd.collector import (
    Collector,
    _git_checkpoint_summary,
)
from hermesd.models import PRMonitorSummary
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import (
    _init_shadow_checkpoint_repo,
    _skip_if_root,
    _unreadable,
    create_async_delegations_table,
    create_kanban_db_tables,
    create_state_db_tables,
    create_state_meta_table,
    insert_delegation,
    render_to_str,
)


def create_verification_evidence_db_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE verification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_id TEXT NOT NULL,
            cwd TEXT NOT NULL,
            root TEXT NOT NULL,
            command TEXT NOT NULL,
            canonical_command TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            output_summary TEXT NOT NULL
        );
        CREATE TABLE verification_state (
            session_id TEXT NOT NULL,
            root TEXT NOT NULL,
            last_event_id INTEGER,
            last_edit_at TEXT,
            changed_paths_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (session_id, root)
        );
        """
    )


def insert_verification_event(
    conn: sqlite3.Connection,
    *,
    event_id: int = 1,
    status: str = "passed",
    command: str = "uv run pytest",
    canonical_command: str = "pytest",
    kind: str = "test",
    scope: str = "full",
    output_summary: str = "12 passed",
) -> None:
    conn.execute(
        """
        INSERT INTO verification_events VALUES (
            ?, '2026-07-10T10:00:00Z', 'sess-a', '/repo', '/repo',
            ?, ?, ?, ?, ?, 0, ?
        )
        """,
        (event_id, command, canonical_command, kind, scope, status, output_summary),
    )


def create_projects_db_tables(conn: sqlite3.Connection, *, optional: bool = True) -> None:
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            icon TEXT,
            color TEXT,
            board_slug TEXT,
            primary_path TEXT,
            created_at TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    if optional:
        conn.executescript(
            """
            CREATE TABLE project_folders (
                project_id TEXT NOT NULL,
                path TEXT NOT NULL,
                label TEXT,
                is_primary INTEGER NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL
            );
            CREATE TABLE discovered_repos (
                root TEXT PRIMARY KEY,
                label TEXT,
                last_seen TEXT NOT NULL
            );
            """
        )


def test_collect_operations_handles_empty_caches_and_corrupt_pr_monitor(hermes_home: Path):
    """An existing-but-empty model cache counts zero; a corrupt pr-monitor file is skipped."""
    (hermes_home / "models_dev_cache.json").write_text("{}")
    (hermes_home / "pr-monitor-corrupt.json").write_text("{not valid json")

    c = Collector(hermes_home)
    state = c.collect()

    assert "operations" not in state.health.failed_sources
    caches = {cache.name: cache for cache in state.operations.model_caches}
    assert caches["models_dev_cache.json"].provider_count == 0
    assert caches["models_dev_cache.json"].model_count == 0
    assert state.operations.pr_monitors == []
    c.close()


def test_collect_pr_monitor_reads_live_key_shape(hermes_home: Path):
    """Live pr-monitor JSON uses tracked_numbers/prs/author_prs/checked_at."""
    (hermes_home / "pr-monitor-acme-widget.json").write_text(
        json.dumps(
            {
                "repo": "acme/widget",
                "checked_at": "2026-06-14T09:00:00Z",
                "tracked_numbers": [10, 11, 12],
                "prs": {"10": {}, "11": {}},
                "author_prs": {"99": {}},
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    monitor = next(m for m in state.operations.pr_monitors if m.repo == "acme/widget")
    assert monitor.checked_at == "2026-06-14T09:00:00Z"
    assert monitor.tracked_count == 3
    assert monitor.monitored_count == 2
    assert monitor.author_pr_count == 1
    c.close()


def test_collect_verification_evidence_counts_latest_events_and_pending_roots(hermes_home: Path):
    db_path = hermes_home / "verification_evidence.db"
    conn = sqlite3.connect(str(db_path))
    create_verification_evidence_db_tables(conn)
    insert_verification_event(
        conn,
        event_id=1,
        command="uv run pytest tests/test_api.py",
        canonical_command="pytest",
        kind="test",
        scope="targeted",
        status="passed",
        output_summary="12 passed",
    )
    insert_verification_event(
        conn,
        event_id=2,
        command="uv run ruff check .",
        canonical_command="ruff check",
        kind="lint",
        scope="full",
        status="failed",
        output_summary="F401 unused import",
    )
    conn.execute(
        """
        INSERT INTO verification_state VALUES (
            'sess-a', '/repo', 2, '2026-07-10T10:06:00Z',
            '["hermesd/collector.py", "tests/test_collector_extended.py"]'
        )
        """
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    ops = state.operations

    assert ops.verification_db_present is True
    assert ops.verification_event_count == 2
    assert ops.verification_failed_count == 1
    assert ops.verification_state_count == 1
    latest = ops.verification_latest_events[0]
    assert latest.event_id == 2
    assert latest.root == "/repo"
    assert latest.command == "uv run ruff check ."
    assert latest.canonical_command == "ruff check"
    assert latest.kind == "lint"
    assert latest.scope == "full"
    assert latest.status == "failed"
    assert latest.output_summary == "F401 unused import"
    root = ops.verification_roots[0]
    assert root.session_id == "sess-a"
    assert root.root == "/repo"
    assert root.last_event_id == 2
    assert root.last_edit_at == "2026-07-10T10:06:00Z"
    assert root.changed_path_count == 2
    assert "operations" not in state.health.failed_sources
    c.close()


def test_collect_verification_evidence_preserves_last_good_on_corrupt_db(hermes_home: Path):
    db_path = hermes_home / "verification_evidence.db"
    conn = sqlite3.connect(str(db_path))
    create_verification_evidence_db_tables(conn)
    insert_verification_event(conn)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    db_path.write_bytes(b"not a sqlite database")
    second = c.collect()

    assert second.operations.verification_db_present is True
    assert second.operations.verification_event_count == first.operations.verification_event_count
    assert (
        second.operations.verification_latest_events == first.operations.verification_latest_events
    )
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_verification_evidence_preserves_last_good_on_unsafe_symlink(
    hermes_home: Path, tmp_path: Path
):
    db_path = hermes_home / "verification_evidence.db"
    conn = sqlite3.connect(str(db_path))
    create_verification_evidence_db_tables(conn)
    insert_verification_event(conn)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    assert first.operations.verification_event_count == 1

    outside_db = tmp_path / "verification_evidence.db"
    sqlite3.connect(str(outside_db)).close()
    db_path.unlink()
    db_path.symlink_to(outside_db)
    second = c.collect()

    assert second.operations == first.operations
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_verification_evidence_preserves_last_good_on_older_schema(
    hermes_home: Path,
):
    db_path = hermes_home / "verification_evidence.db"
    conn = sqlite3.connect(str(db_path))
    create_verification_evidence_db_tables(conn)
    insert_verification_event(conn)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    assert first.operations.verification_event_count == 1

    db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE verification_state (
            session_id TEXT NOT NULL,
            root TEXT NOT NULL,
            last_event_id INTEGER,
            last_edit_at TEXT,
            changed_paths_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (session_id, root)
        )
        """
    )
    conn.commit()
    conn.close()
    second = c.collect()

    assert second.operations == first.operations
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_moa_config_and_trace_inventory(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump(
            {
                "moa": {
                    "default_preset": "council",
                    "active_preset": "council",
                    "save_traces": True,
                    "trace_dir": "",
                    "presets": {
                        "council": {
                            "reference_models": [
                                {"provider": "openai-codex", "model": "gpt-5.5"},
                                {"provider": "openrouter", "model": "deepseek-v4"},
                            ],
                            "aggregator": {"provider": "openrouter", "model": "claude-opus"},
                            "enabled": True,
                        }
                    },
                }
            }
        )
    )
    trace_dir = hermes_home / "moa-traces"
    trace_dir.mkdir()
    (trace_dir / "sess-moa.jsonl").write_text(
        json.dumps({"session_id": "sess-moa", "preset": "council", "status": "ok"}) + "\n"
    )

    c = Collector(hermes_home)
    state = c.collect()

    assert state.config.moa_default_preset == "council"
    assert state.config.moa_active_preset == "council"
    assert state.config.moa_save_traces is True
    assert state.config.moa_preset_count == 1
    assert state.config.moa_reference_model_count == 2
    assert state.config.moa_aggregator_label == "openrouter/claude-opus"
    assert state.operations.moa_trace_count == 1
    assert state.operations.moa_trace_newest_session_id == "sess-moa"
    assert state.operations.moa_trace_size_bytes > 0
    assert state.operations.moa_trace_latest_record_summary == "ok council"
    assert state.operations.moa_trace_latest_record_keys == ["preset", "session_id", "status"]
    c.close()


def test_collect_projects_db_summary(hermes_home: Path):
    db_path = hermes_home / "projects.db"
    conn = sqlite3.connect(str(db_path))
    create_projects_db_tables(conn)
    conn.executescript(
        """
        INSERT INTO projects VALUES
            ('p1', 'hermesd', 'hermesd', '', '', '', 'main', '/repo/hermesd',
             '2026-07-10T00:00:00Z', 0),
            ('p2', 'archive', 'archive', '', '', '', '', '/repo/archive',
             '2026-07-09T00:00:00Z', 1),
            ('p3', 'missing', 'missing', '', '', '', '', '',
             '2026-07-08T00:00:00Z', 0);
        INSERT INTO project_folders VALUES
            ('p1', '/repo/hermesd', 'primary', 1, '2026-07-10T00:00:00Z'),
            ('p2', '/repo/archive', 'primary', 1, '2026-07-09T00:00:00Z');
        INSERT INTO discovered_repos VALUES
            ('/repo/hermes-agent', 'Hermes Agent', '2026-07-11T00:00:00Z'),
            ('/repo/hermesd', 'hermesd', '2026-07-12T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    ops = state.operations

    assert ops.projects_db_present is True
    assert ops.project_count == 3
    assert ops.project_archived_count == 1
    assert ops.project_folder_count == 2
    assert ops.discovered_repo_count == 2
    assert ops.project_missing_primary_path_count == 1
    assert ops.projects[0].slug == "hermesd"
    assert ops.projects[0].board_slug == "main"
    assert ops.discovered_repos[0].root == "/repo/hermesd"
    c.close()


def test_project_correlations_require_safe_boards_and_path_boundaries(
    hermes_home: Path, tmp_path: Path
):
    verification_db = hermes_home / "verification_evidence.db"
    conn = sqlite3.connect(str(verification_db))
    create_verification_evidence_db_tables(conn)
    conn.executescript(
        """
        INSERT INTO verification_state VALUES
            ('sess-a', '/repo/app', 1, '2026-07-10T10:00:00Z', '[]'),
            ('sess-b', '/repo/application', 2, '2026-07-10T10:01:00Z', '[]');
        """
    )
    conn.commit()
    conn.close()

    outside_db = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(outside_db))
    create_kanban_db_tables(conn)
    conn.close()
    (hermes_home / "kanban.db").symlink_to(outside_db)

    projects_db = hermes_home / "projects.db"
    conn = sqlite3.connect(str(projects_db))
    create_projects_db_tables(conn, optional=False)
    conn.executescript(
        """
        INSERT INTO projects VALUES (
            'p1', 'app', 'App', '', '', '', 'root', '/repo/app',
            '2026-07-10T00:00:00Z', 0
        );
        """
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.operations.projects[0].verification_root_count == 1
    assert state.operations.projects[0].kanban_board_present is False
    c.close()


def test_collect_moa_traces_preserves_last_good_when_trace_dir_disappears(
    hermes_home: Path,
):
    (hermes_home / "config.yaml").write_text(yaml.dump({"moa": {"save_traces": True}}))
    trace_dir = hermes_home / "moa-traces"
    trace_dir.mkdir()
    (trace_dir / "sess-moa.jsonl").write_text(json.dumps({"status": "ok"}) + "\n")

    c = Collector(hermes_home)
    first = c.collect()
    assert first.operations.moa_trace_count == 1

    shutil.rmtree(trace_dir)
    second = c.collect()

    assert second.operations == first.operations
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_moa_traces_preserves_last_good_when_trace_dir_becomes_unsafe_symlink(
    hermes_home: Path, tmp_path: Path
):
    (hermes_home / "config.yaml").write_text(yaml.dump({"moa": {"save_traces": True}}))
    trace_dir = hermes_home / "moa-traces"
    trace_dir.mkdir()
    (trace_dir / "sess-moa.jsonl").write_text(json.dumps({"status": "ok"}) + "\n")

    c = Collector(hermes_home)
    first = c.collect()
    assert first.operations.moa_trace_count == 1

    outside_trace_dir = tmp_path / "moa-traces"
    outside_trace_dir.mkdir()
    shutil.rmtree(trace_dir)
    trace_dir.symlink_to(outside_trace_dir)
    second = c.collect()

    assert second.operations == first.operations
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_projects_preserves_last_good_when_projects_db_corrupts(hermes_home: Path):
    db_path = hermes_home / "projects.db"
    conn = sqlite3.connect(str(db_path))
    create_projects_db_tables(conn, optional=False)
    conn.executescript(
        """
        INSERT INTO projects VALUES (
            'p1', 'hermesd', 'hermesd', '', '', '', '', '/repo/hermesd',
            '2026-07-10T00:00:00Z', 0
        );
        """
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    assert first.operations.project_count == 1

    db_path.write_bytes(b"not a sqlite database")
    second = c.collect()

    assert second.operations == first.operations
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_projects_preserves_last_good_when_projects_db_becomes_unsafe_symlink(
    hermes_home: Path, tmp_path: Path
):
    db_path = hermes_home / "projects.db"
    conn = sqlite3.connect(str(db_path))
    create_projects_db_tables(conn, optional=False)
    conn.execute(
        """
        INSERT INTO projects VALUES (
            'p1', 'hermesd', 'hermesd', '', '', '', '', '/repo/hermesd',
            '2026-07-10T00:00:00Z', 0
        )
        """
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    assert first.operations.project_count == 1

    outside_db = tmp_path / "projects.db"
    sqlite3.connect(str(outside_db)).close()
    db_path.unlink()
    db_path.symlink_to(outside_db)
    second = c.collect()

    assert second.operations == first.operations
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_projects_tolerates_missing_optional_tables(hermes_home: Path):
    db_path = hermes_home / "projects.db"
    conn = sqlite3.connect(str(db_path))
    create_projects_db_tables(conn, optional=False)
    conn.execute(
        """
        INSERT INTO projects VALUES (
            'p1', 'hermesd', 'hermesd', '', '', '', '', '/repo/hermesd',
            '2026-07-10T00:00:00Z', 0
        )
        """
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.operations.project_count == 1
    assert state.operations.project_folder_count == 0
    assert state.operations.discovered_repo_count == 0
    assert state.operations.discovered_repos == []
    assert "operations" not in state.health.failed_sources
    c.close()


def test_collect_operations_visibility_renders_from_collected_state(hermes_home: Path):
    verification_db = hermes_home / "verification_evidence.db"
    conn = sqlite3.connect(str(verification_db))
    create_verification_evidence_db_tables(conn)
    insert_verification_event(
        conn,
        status="failed",
        command="uv run ruff check .",
        canonical_command="ruff check",
        kind="lint",
        scope="full",
        output_summary="F401 unused import",
    )
    conn.execute(
        """
        INSERT INTO verification_state VALUES (
            'sess-a', '/repo/hermesd', 1, '2026-07-10T10:06:00Z',
            '["hermesd/collector.py"]'
        )
        """
    )
    conn.commit()
    conn.close()

    state_db = hermes_home / "state.db"
    conn = sqlite3.connect(str(state_db))
    create_state_db_tables(conn)
    conn.executescript(
        """
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO state_meta VALUES (
            'goal:sess-goal',
            '{"goal":"Ship visibility","status":"active","turns_used":1,"max_turns":3,
              "contract":{"outcome":"green tests"}}'
        );
        """
    )
    conn.commit()
    conn.close()

    trace_dir = hermes_home / "moa-traces"
    trace_dir.mkdir()
    (trace_dir / "sess-moa.jsonl").write_text(json.dumps({"status": "ok"}) + "\n")

    projects_db = hermes_home / "projects.db"
    conn = sqlite3.connect(str(projects_db))
    create_projects_db_tables(conn)
    conn.executescript(
        """
        INSERT INTO projects VALUES (
            'p1', 'hermesd', 'hermesd', '', '', '', '', '/repo/hermesd',
            '2026-07-10T00:00:00Z', 0
        );
        INSERT INTO discovered_repos VALUES ('/repo/hermesd', 'hermesd', '2026-07-12T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    text = render_to_str(render_panel(12, state, Theme(), detail=True), width=120, no_color=True)

    assert "Verification Evidence" in text
    assert "ruff check" in text
    assert "Ship visibility" in text
    assert "MoA Traces" in text
    assert "Newest Discovered Repos" in text
    c.close()


def test_collect_goal_state_from_state_meta(hermes_home: Path):
    state_db = hermes_home / "state.db"
    conn = sqlite3.connect(str(state_db))
    create_state_db_tables(conn)
    conn.executescript(
        """
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO state_meta VALUES (
            'goal:sess-goal',
            '{"goal":"Ship visibility","status":"active","turns_used":3,"max_turns":8,
              "waiting_on_pid":4242,"waiting_reason":"tests running",
              "subgoals":["finish docs"],"contract":{"outcome":"green tests"}}'
        );
        """
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.operations.goal_count == 1
    assert state.operations.active_goal_count == 1
    assert state.operations.waiting_goal_count == 1
    goal = state.operations.goals[0]
    assert goal.session_id == "sess-goal"
    assert goal.goal == "Ship visibility"
    assert goal.status == "active"
    assert goal.turns_used == 3
    assert goal.max_turns == 8
    assert goal.waiting_on_pid == 4242
    assert goal.waiting_reason == "tests running"
    assert goal.subgoal_count == 1
    assert goal.has_contract is True
    c.close()


def test_collect_goal_state_preserves_last_good_on_corrupt_state_db(hermes_home: Path):
    state_db = hermes_home / "state.db"
    conn = sqlite3.connect(str(state_db))
    create_state_db_tables(conn)
    conn.executescript(
        """
        CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO state_meta VALUES (
            'goal:sess-goal',
            '{"goal":"Ship visibility","status":"active","contract":{"outcome":"green tests"}}'
        );
        """
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    first = c.collect()
    assert first.operations.goal_count == 1

    state_db.write_bytes(b"not a sqlite database")
    second = c.collect()

    assert second.operations == first.operations
    assert "operations" in second.health.failed_sources
    c.close()


def test_collect_pr_monitor_reads_underscore_and_subdir_families(hermes_home: Path):
    """pr_monitor_*.json (underscore) and pr_monitor/*.json (subdir) are also read."""
    (hermes_home / "pr_monitor_state.json").write_text(
        json.dumps(
            {
                "repo": "underscore/flat",
                "checked_at": "2026-06-14T01:00:00Z",
                "prs": {"1": {}},
                "tracked_numbers": [1],
            }
        )
    )
    subdir = hermes_home / "pr_monitor"
    subdir.mkdir()
    (subdir / "state.json").write_text(
        json.dumps({"repo": "subdir/under", "checked_at": "2026-06-14T02:00:00Z", "prs": {"7": {}}})
    )
    hyphen_subdir = hermes_home / "pr-monitor"
    hyphen_subdir.mkdir()
    (hyphen_subdir / "widget-prs.json").write_text(
        json.dumps(
            {"repo": "subdir/hyphen", "checked_at": "2026-06-14T03:00:00Z", "prs": {"9": {}}}
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    repos = {m.repo for m in state.operations.pr_monitors}
    assert {"underscore/flat", "subdir/under", "subdir/hyphen"} <= repos
    c.close()


def test_collect_pr_monitor_ignores_symlinked_files_outside_home(hermes_home: Path, tmp_path: Path):
    outside_monitor = tmp_path / "outside-pr.json"
    outside_monitor.write_text(
        json.dumps(
            {
                "repo": "outside/repo",
                "checked_at": "2026-06-14T01:00:00Z",
                "prs": {"1": {}},
            }
        )
    )
    (hermes_home / "pr-monitor-outside.json").symlink_to(outside_monitor)

    c = Collector(hermes_home)
    state = c.collect()

    assert [monitor.repo for monitor in state.operations.pr_monitors] == []
    c.close()


def test_collect_pr_monitor_dedupes_same_repo_keeping_newest(hermes_home: Path):
    """The same repo across multiple monitor files collapses to the newest checked_at."""
    (hermes_home / "pr-monitor-acme-widget.json").write_text(
        json.dumps({"repo": "acme/widget", "checked_at": "2026-06-10T00:00:00Z", "prs": {"1": {}}})
    )
    (hermes_home / "pr_monitor_acme_widget_state.json").write_text(
        json.dumps(
            {
                "repo": "acme/widget",
                "checked_at": "2026-06-14T00:00:00Z",
                "prs": {"1": {}, "2": {}, "3": {}},
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    acme = [m for m in state.operations.pr_monitors if m.repo == "acme/widget"]
    assert len(acme) == 1
    assert acme[0].checked_at == "2026-06-14T00:00:00Z"
    assert acme[0].monitored_count == 3
    c.close()


def test_collect_operations_reads_camelcase_desktop_build_stamp(hermes_home: Path):
    """Live desktop-build-stamp.json uses camelCase builtAt/contentHash/sourceMode."""
    (hermes_home / "desktop-build-stamp.json").write_text(
        json.dumps(
            {
                "builtAt": "2026-06-14T08:00:00Z",
                "contentHash": "abcdef1234567890deadbeef",
                "sourceMode": "release",
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.operations.desktop_build_stamp == "2026-06-14T08:00:00Z"
    c.close()


def test_collect_operations_falls_back_to_content_hash_when_no_built_at(hermes_home: Path):
    """With only contentHash present, a truncated hash represents the build."""
    (hermes_home / "desktop-build-stamp.json").write_text(
        json.dumps({"contentHash": "abcdef1234567890deadbeef"})
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.operations.desktop_build_stamp == "abcdef123456"
    c.close()


def test_collect_checkpoints_ignores_symlinked_repo_dirs(hermes_home: Path, tmp_path: Path):
    checkpoints_dir = hermes_home / "checkpoints"
    checkpoints_dir.mkdir()
    external_repo = tmp_path / "external-repo"
    external_repo.mkdir()
    (external_repo / "HERMES_WORKDIR").write_text(str(tmp_path / "outside-workdir"))
    try:
        (checkpoints_dir / "escaped").symlink_to(external_repo, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are not supported here: {exc}")

    c = Collector(hermes_home)
    state = c.collect()

    assert state.checkpoints == []
    c.close()


def test_git_checkpoint_summary_parses_latest_checkpoint(monkeypatch):
    def fake_run(*args, **kwargs):
        if "rev-list" in args[0]:
            return CompletedProcess(args[0], 0, stdout="3\n")
        return CompletedProcess(args[0], 0, stdout="1712345678\tcheckpoint reason\n")

    monkeypatch.setattr("hermesd.collector.subprocess.run", fake_run)

    commit_count, timestamp, reason = _git_checkpoint_summary(Path("/tmp/repo.git"))

    assert commit_count == 3
    assert timestamp == 1712345678.0
    assert reason == "checkpoint reason"


def test_git_checkpoint_summary_empty_repo_skips_log(monkeypatch):
    def fake_run(*args, **kwargs):
        return CompletedProcess(args[0], 0, stdout="0\n")

    monkeypatch.setattr("hermesd.collector.subprocess.run", fake_run)

    assert _git_checkpoint_summary(Path("/tmp/repo.git")) == (0, None, "")


def test_git_checkpoint_summary_handles_missing_git(monkeypatch):
    def fake_run(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr("hermesd.collector.subprocess.run", fake_run)
    assert _git_checkpoint_summary(Path("/tmp/repo.git")) == (0, None, "")


def test_git_checkpoint_summary_log_failure_keeps_commit_count(monkeypatch):
    def fake_run(*args, **kwargs):
        if "rev-list" in args[0]:
            return CompletedProcess(args[0], 0, stdout="3\n")
        raise OSError("git log failed")

    monkeypatch.setattr("hermesd.collector.subprocess.run", fake_run)
    assert _git_checkpoint_summary(Path("/tmp/repo.git")) == (3, None, "")


def test_git_checkpoint_summary_log_nonzero_exit_keeps_commit_count(monkeypatch):
    def fake_run(*args, **kwargs):
        if "rev-list" in args[0]:
            return CompletedProcess(args[0], 0, stdout="3\n")
        return CompletedProcess(args[0], 128, stdout="")

    monkeypatch.setattr("hermesd.collector.subprocess.run", fake_run)
    assert _git_checkpoint_summary(Path("/tmp/repo.git")) == (3, None, "")


def test_git_checkpoint_summary_log_without_tab_returns_raw_reason(monkeypatch):
    def fake_run(*args, **kwargs):
        if "rev-list" in args[0]:
            return CompletedProcess(args[0], 0, stdout="3\n")
        return CompletedProcess(args[0], 0, stdout="no tab here\n")

    monkeypatch.setattr("hermesd.collector.subprocess.run", fake_run)
    assert _git_checkpoint_summary(Path("/tmp/repo.git")) == (3, None, "no tab here")


def test_collect_pr_monitor_repoless_files_stay_distinct(hermes_home: Path):
    """Monitor files without a repo key are kept separate, keyed by filename."""
    (hermes_home / "pr-monitor-one.json").write_text(
        json.dumps({"checked_at": "2026-06-14T01:00:00Z", "prs": {"1": {}}})
    )
    (hermes_home / "pr-monitor-two.json").write_text(
        json.dumps({"checked_at": "2026-06-14T02:00:00Z", "prs": {"2": {}, "3": {}}})
    )
    c = Collector(hermes_home)
    state = c.collect()
    repoless = [m for m in state.operations.pr_monitors if not m.repo]
    assert len(repoless) == 2
    assert {m.filename for m in repoless} == {"pr-monitor-one.json", "pr-monitor-two.json"}
    c.close()


def _commit_raw_subject(repo_dir: Path, subject: bytes) -> None:
    """Append a commit whose subject is stored verbatim (no re-encoding).

    Modern git re-encodes `git commit -m` input to UTF-8, so the only way to
    get raw non-UTF-8 bytes into a commit object is the hash-object plumbing.
    """
    tree = subprocess.run(
        ["git", "--git-dir", str(repo_dir), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    parent = subprocess.run(
        ["git", "--git-dir", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    body = (
        (
            f"tree {tree}\nparent {parent}\n"
            "author Hermesd Tests <tests@example.com> 1788608000 +0000\n"
            "committer Hermesd Tests <tests@example.com> 1788608000 +0000\n"
            "\n"
        ).encode()
        + subject
        + b"\n"
    )
    commit = (
        subprocess.run(
            ["git", "--git-dir", str(repo_dir), "hash-object", "-t", "commit", "-w", "--stdin"],
            check=True,
            capture_output=True,
            input=body,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        ["git", "--git-dir", str(repo_dir), "update-ref", "HEAD", commit],
        check=True,
        capture_output=True,
    )


def test_git_checkpoint_summary_tolerates_non_utf8_subject(tmp_path: Path):
    repo_dir = tmp_path / "checkpoints" / "abc123def4567890"
    workdir = tmp_path / "workspaces" / "project-alpha"
    _init_shadow_checkpoint_repo(repo_dir, workdir, ["initial checkpoint"])
    _commit_raw_subject(repo_dir, b"non-utf8 subject \xff\xfe")

    commit_count, _timestamp, _reason = _git_checkpoint_summary(repo_dir)

    assert commit_count == 2


@_skip_if_root
def test_checkpoint_unreadable_workdir_file_blanks_workdir(hermes_home: Path):
    repo_dir = hermes_home / "checkpoints" / "deadbeefcafe0001"
    repo_dir.mkdir(parents=True)
    workdir_file = repo_dir / "HERMES_WORKDIR"
    workdir_file.write_text("/tmp/project")
    os.chmod(workdir_file, 0o000)
    try:
        if not _unreadable(workdir_file):
            pytest.skip("filesystem allowed read despite chmod 000")
        c = Collector(hermes_home)
        try:
            state = c.collect()
            assert "checkpoints" not in state.health.failed_sources
            cp = next(c for c in state.checkpoints if c.repo_id == "deadbeefcafe0001")
            # Unreadable HERMES_WORKDIR falls back to "" rather than crashing.
            assert cp.workdir == ""
            assert cp.workdir_name == ""
        finally:
            c.close()
    finally:
        os.chmod(workdir_file, 0o644)


def test_pr_monitors_read_cron_state_directory(hermes_home: Path):
    state_dir = hermes_home / "cron" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "pr_monitor.json").write_text(
        json.dumps(
            {
                "repo": "NousResearch/hermes-agent",
                "checked_at": "2026-09-01T00:00:00Z",
                "prs": {"1": {}, "2": {}, "3": {}},
                "tracked_numbers": [1, 2, 3],
            }
        )
    )
    (state_dir / "pr_monitor.json.bak").write_text(json.dumps({"repo": "stale/backup"}))
    (state_dir / "pr_monitor.json.lock").write_text("")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    monitors = state.operations.pr_monitors
    assert [m.filename for m in monitors] == ["pr_monitor.json"]
    assert monitors[0].repo == "NousResearch/hermes-agent"
    assert monitors[0].monitored_count == 3
    assert monitors[0].tracked_count == 3


def _write_pr_keyed_monitor(hermes_home: Path, payload: object) -> None:
    state_dir = hermes_home / "cron" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "pr_monitor.json").write_text(json.dumps(payload))


def _pr_keyed_monitor(hermes_home: Path, payload: object) -> PRMonitorSummary:
    _write_pr_keyed_monitor(hermes_home, payload)
    c = Collector(hermes_home)
    try:
        monitors = c.collect().operations.pr_monitors
    finally:
        c.close()
    assert len(monitors) == 1
    return monitors[0]


_LIVE_PR_KEYED_PAYLOAD = {
    "25137": {
        "number": 25137,
        "title": "fix: persist CLI model runtime state",
        "state": "CLOSED",
        "mergeable": "UNKNOWN",
        "reviewDecision": "",
        "updatedAt": "2026-07-20T09:26:06Z",
    },
    "57327": {
        "number": 57327,
        "title": "fix(auth): shared provider alias normalization",
        "state": "OPEN",
        "mergeable": "CONFLICTING",
        "reviewDecision": "",
        "updatedAt": "2026-07-15T15:22:51Z",
    },
}


def test_pr_monitor_reads_live_pr_keyed_shape(hermes_home: Path):
    """The live cron/state/pr_monitor.json is a dict keyed by PR number."""
    monitor = _pr_keyed_monitor(hermes_home, _LIVE_PR_KEYED_PAYLOAD)

    assert monitor.monitored_count == 2
    assert monitor.tracked_count == 2
    assert monitor.open_count == 1
    assert monitor.conflicting_count == 1
    assert monitor.checked_at == "2026-07-20T09:26:06Z"
    assert monitor.repo == ""


def test_pr_monitor_pr_keyed_shape_tolerates_missing_fields(hermes_home: Path):
    monitor = _pr_keyed_monitor(hermes_home, {"1": {}, "2": {"state": "open"}})

    assert monitor.monitored_count == 2
    assert monitor.tracked_count == 2
    assert monitor.open_count == 1
    assert monitor.conflicting_count == 0
    assert monitor.checked_at == ""


def test_pr_monitor_mixed_keys_are_not_treated_as_pr_keyed(hermes_home: Path):
    """A repo/prs document keeps the legacy reading even with digit siblings."""
    monitor = _pr_keyed_monitor(
        hermes_home,
        {"repo": "acme/widget", "checked_at": "2026-01-01T00:00:00Z", "prs": {"7": {}}},
    )

    assert monitor.repo == "acme/widget"
    assert monitor.monitored_count == 1
    assert monitor.open_count == 0
    assert monitor.conflicting_count == 0


def test_pr_monitor_digit_keys_with_non_dict_values_use_legacy_reading(hermes_home: Path):
    monitor = _pr_keyed_monitor(hermes_home, {"1": "OPEN", "2": "MERGED"})

    assert monitor.monitored_count == 0
    assert monitor.open_count == 0


def _count_sqlite_connects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every real SQLite open, so a second state.db copy cannot hide."""
    original = sqlite3.connect
    seen: list[str] = []

    def counting(target, *args, **kwargs):
        seen.append(str(target))
        return original(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting)
    return seen


def _state_db_opens(connects: list[str]) -> int:
    return sum(1 for target in connects if "state.db" in target)


def test_goal_state_is_cached_while_state_db_is_unchanged(
    hermes_home: Path, sample_db: Path, monkeypatch: pytest.MonkeyPatch
):
    connects = _count_sqlite_connects(monkeypatch)

    c = Collector(hermes_home)
    try:
        c.collect()
        c.collect()
    finally:
        c.close()

    assert _state_db_opens(connects) == 1


def test_goal_state_recomputed_when_state_db_changes(
    hermes_home: Path, sample_db: Path, monkeypatch: pytest.MonkeyPatch
):
    connects = _count_sqlite_connects(monkeypatch)

    c = Collector(hermes_home)
    try:
        c.collect()
        bumped = sample_db.stat().st_mtime + 10
        os.utime(sample_db, (bumped, bumped))
        c.collect()
    finally:
        c.close()

    assert _state_db_opens(connects) == 2


def _count_git_summaries(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    original = collector_module._git_checkpoint_summary
    seen: list[Path] = []

    def counting(repo_dir: Path):
        seen.append(repo_dir)
        return original(repo_dir)

    monkeypatch.setattr(collector_module, "_git_checkpoint_summary", counting)
    return seen


def test_git_checkpoint_summary_is_cached_while_refs_are_unchanged(
    hermes_home: Path, sample_checkpoints: Path, monkeypatch: pytest.MonkeyPatch
):
    summaries = _count_git_summaries(monkeypatch)

    c = Collector(hermes_home)
    try:
        first = c.collect()
        second = c.collect()
    finally:
        c.close()

    assert len(summaries) == 1
    assert first.checkpoints[0].commit_count == 2
    assert second.checkpoints[0].commit_count == 2
    assert second.checkpoints[0].last_reason == first.checkpoints[0].last_reason


def test_git_checkpoint_summary_recomputed_after_new_commit(
    hermes_home: Path, sample_checkpoints: Path, monkeypatch: pytest.MonkeyPatch
):
    summaries = _count_git_summaries(monkeypatch)
    repo_dir = next(p for p in sample_checkpoints.iterdir() if p.is_dir())
    workdir = Path((repo_dir / "HERMES_WORKDIR").read_text().strip())

    c = Collector(hermes_home)
    try:
        first = c.collect()
        _commit(repo_dir, workdir, "Third checkpoint")
        second = c.collect()
    finally:
        c.close()

    assert len(summaries) == 2
    assert first.checkpoints[0].commit_count == 2
    assert second.checkpoints[0].commit_count == 3
    assert second.checkpoints[0].last_reason == "Third checkpoint"


def _commit(repo_dir: Path, workdir: Path, message: str) -> None:
    (workdir / "tracked.txt").write_text(f"{message}\n")
    git = ["git", "--git-dir", str(repo_dir), "--work-tree", str(workdir)]
    subprocess.run([*git, "add", "tracked.txt"], check=True, capture_output=True, text=True)
    subprocess.run([*git, "commit", "-m", message], check=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Async delegations, state.db maintenance, snapshot backups, web UI stamp
# ---------------------------------------------------------------------------

_FIXED_NOW = 1_800_000_000.0


def _fixed_clock() -> float:
    return _FIXED_NOW


def _collect_ops(home: Path, **kwargs: object):
    """One collect pass with a fixed clock, closing the collector afterwards."""
    c = Collector(home, clock=_fixed_clock, **kwargs)  # type: ignore[arg-type]
    try:
        return c.collect()
    finally:
        c.close()


def _open_state_db(home: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(home / "state.db"))


def test_delegations_happy_path(hermes_home: Path, sample_db: Path):
    ops = _collect_ops(hermes_home, pid_exists=lambda pid: pid == 4242).operations
    assert ops.delegation_count == 3
    assert ops.delegation_running_count == 1
    assert ops.delegation_failed_count == 1
    assert ops.delegation_undelivered_count == 0
    assert [d.delegation_id for d in ops.delegations] == [
        "deleg_running",
        "deleg_failed",
        "deleg_done",
    ]
    running = ops.delegations[0]
    assert running.state == "running"
    assert running.goal == "crawl the docs"
    assert running.owner_alive is True
    failed = ops.delegations[1]
    assert failed.result_status == "error"
    assert failed.error_excerpt == "boom in the worker"
    assert failed.delivery_attempts == 3
    assert failed.duration_seconds is not None and failed.duration_seconds > 0


def test_delegation_owner_alive_uses_injected_pid_exists(hermes_home: Path, sample_db: Path):
    ops = _collect_ops(hermes_home, pid_exists=lambda pid: False).operations
    assert all(not d.owner_alive for d in ops.delegations)


def test_delegation_table_absent_leaves_counts_at_zero(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_state_db_tables(conn)
    conn.commit()
    conn.close()
    ops = _collect_ops(hermes_home).operations
    assert ops.delegation_count == 0
    assert ops.delegations == []


def test_delegation_null_columns_coerce_to_defaults(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_async_delegations_table(conn)
    conn.execute(
        "INSERT INTO async_delegations "
        "(delegation_id, state, dispatched_at, updated_at, delivery_state) "
        "VALUES (?, ?, ?, ?, ?)",
        ("deleg_null", "queued", 0.0, 0.0, "pending"),
    )
    conn.commit()
    conn.close()
    entry = _collect_ops(hermes_home).operations.delegations[0]
    assert entry.origin_session == ""
    assert entry.delivery_attempts == 0
    assert entry.goal == ""
    assert entry.result_status == ""
    assert entry.error_excerpt == ""
    assert entry.completed_at is None
    assert entry.duration_seconds is None
    assert entry.owner_alive is False


def test_delegation_garbage_json_columns_are_ignored(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_async_delegations_table(conn)
    insert_delegation(conn, "deleg_garbage", task_json="{not json", result_json="[[[")
    conn.commit()
    conn.close()
    entry = _collect_ops(hermes_home).operations.delegations[0]
    assert entry.goal == ""
    assert entry.result_status == ""


def test_delegation_oversized_json_columns_are_capped(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_async_delegations_table(conn)
    insert_delegation(
        conn,
        "deleg_big",
        task_json=json.dumps({"goal": "x" * 70_000}),
        result_json=json.dumps({"results": [{"status": "ok", "error": "y" * 70_000}]}),
    )
    conn.commit()
    conn.close()
    entry = _collect_ops(hermes_home).operations.delegations[0]
    assert entry.goal == ""
    assert entry.result_status == ""


def test_delegation_json_columns_under_the_cap_are_decoded(hermes_home: Path):
    """The 4 KiB cap blanked realistic delegation payloads; 64 KiB does not."""
    conn = _open_state_db(hermes_home)
    create_async_delegations_table(conn)
    insert_delegation(
        conn,
        "deleg_medium",
        task_json=json.dumps({"goal": "ship it", "context": "c" * 8000}),
        result_json=json.dumps({"results": [{"status": "ok", "summary": "s" * 8000}]}),
    )
    conn.commit()
    conn.close()
    entry = _collect_ops(hermes_home).operations.delegations[0]
    assert entry.goal == "ship it"
    assert entry.result_status == "ok"


def test_delegation_text_fields_clip_to_80_chars(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_async_delegations_table(conn)
    insert_delegation(
        conn,
        "deleg_long",
        task_json=json.dumps({"goal": "g" * 300}),
        result_json=json.dumps({"results": [{"status": "error", "error": "e" * 300}]}),
    )
    conn.commit()
    conn.close()
    entry = _collect_ops(hermes_home).operations.delegations[0]
    assert len(entry.goal) == 80
    assert len(entry.error_excerpt) == 80


def test_delegation_error_excerpt_falls_back_to_summary(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_async_delegations_table(conn)
    insert_delegation(
        conn,
        "deleg_summary",
        result_json=json.dumps({"results": [{"status": "ok", "summary": "all\n  done"}]}),
    )
    conn.commit()
    conn.close()
    entry = _collect_ops(hermes_home).operations.delegations[0]
    assert entry.error_excerpt == "all done"


def test_delegation_detail_rows_capped_at_ten_newest(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_async_delegations_table(conn)
    for index in range(15):
        insert_delegation(conn, f"deleg_{index:02d}", dispatched_at=1000.0 + index)
    conn.commit()
    conn.close()
    ops = _collect_ops(hermes_home).operations
    assert ops.delegation_count == 15
    assert len(ops.delegations) == 10
    assert ops.delegations[0].delegation_id == "deleg_14"


def test_delegation_undelivered_counts_only_completed_rows(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_async_delegations_table(conn)
    insert_delegation(conn, "a", state="completed", delivery_state="pending")
    insert_delegation(conn, "b", state="completed", delivery_state="delivered")
    insert_delegation(conn, "c", state="error", delivery_state="pending")
    conn.commit()
    conn.close()
    assert _collect_ops(hermes_home).operations.delegation_undelivered_count == 1


def test_delegation_live_logs_counted(
    hermes_home: Path, sample_db: Path, sample_delegation_live_logs: Path
):
    assert _collect_ops(hermes_home).operations.delegation_live_log_count == 2


def test_delegation_live_log_dir_absent(hermes_home: Path, sample_db: Path):
    assert _collect_ops(hermes_home).operations.delegation_live_log_count == 0


def test_delegation_live_log_symlinked_dir_is_ignored(
    hermes_home: Path, sample_db: Path, tmp_path: Path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "task-0.log").write_text("nope\n")
    live = hermes_home / "cache" / "delegation" / "live"
    live.mkdir(parents=True)
    (live / "deleg_evil").symlink_to(outside, target_is_directory=True)
    assert _collect_ops(hermes_home).operations.delegation_live_log_count == 0


def test_delegation_live_log_scan_is_bounded(hermes_home: Path, sample_db: Path):
    live = hermes_home / "cache" / "delegation" / "live"
    live.mkdir(parents=True)
    for index in range(260):
        run_dir = live / f"deleg_{index:04d}"
        run_dir.mkdir()
        (run_dir / "task-0.log").write_text("x\n")
    count = _collect_ops(hermes_home).operations.delegation_live_log_count
    assert 0 < count <= 200


def test_delegation_live_run_dir_escaping_home_is_not_counted(
    hermes_home: Path, sample_db: Path, tmp_path: Path, monkeypatch
):
    """A run directory that resolves outside the home is skipped, contained ones are not."""
    live = hermes_home / "cache" / "delegation" / "live"
    live.mkdir(parents=True)
    inside = live / "deleg_inside"
    inside.mkdir()
    (inside / "task-0.log").write_text("kept\n")

    escaped = live / "deleg_escaped"
    escaped.mkdir()
    (escaped / "task-0.log").write_text("dropped\n")
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    real_resolve = Path.resolve

    def escaping_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        # Simulate a directory swapped for an out-of-home target between the
        # listing and the containment check.
        if self == escaped:
            return outside
        return real_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", escaping_resolve)

    ops = _collect_ops(hermes_home).operations

    assert ops.delegation_live_log_count == 1


def test_unknown_table_name_is_refused_by_the_allow_list():
    """The identifier interpolation guard must reject anything off the allow-list."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE projects (id TEXT)")
        assert sqlite_util_module._table_count(conn, "projects") == 0
        with pytest.raises(ValueError, match="unknown table name"):
            sqlite_util_module._table_count(conn, "projects; DROP TABLE projects")
        with pytest.raises(ValueError, match="unknown table name"):
            sqlite_util_module._column_exists(conn, "sessions", "id")
    finally:
        conn.close()


def test_sqlite_helpers_degrade_to_defaults_on_a_dead_connection():
    """A connection closed underneath the readers yields zeros, not an exception."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE projects (id TEXT)")
    conn.close()

    assert sqlite_util_module._table_count_or_zero(conn, "projects") == 0
    assert sqlite_util_module._column_exists(conn, "projects", "id") is False
    assert sqlite_util_module._count_rows_or_zero(conn, "SELECT COUNT(*) FROM projects") == 0


def test_state_snapshots_root_symlinked_outside_home_is_ignored(
    hermes_home: Path, sample_db: Path, tmp_path: Path
):
    """A state-snapshots/ symlink escaping the home must contribute no sizes."""
    outside = tmp_path / "snaps"
    outside.mkdir()
    snapshot = outside / "20260907-143350-pre-update"
    snapshot.mkdir()
    (snapshot / "state.db").write_bytes(b"x" * 2048)
    (hermes_home / "state-snapshots").symlink_to(outside, target_is_directory=True)

    ops = _collect_ops(hermes_home).operations

    assert ops.snapshot_count == 0
    assert ops.snapshot_total_bytes == 0
    assert ops.newest_snapshot_age_seconds is None


def test_delegation_live_root_symlinked_outside_home_is_ignored(
    hermes_home: Path, sample_db: Path, tmp_path: Path
):
    """A cache/delegation/live symlink escaping the home must count no transcripts."""
    outside = tmp_path / "live"
    outside.mkdir()
    run_dir = outside / "deleg_done"
    run_dir.mkdir()
    (run_dir / "task-0.log").write_text("subagent transcript\n")
    cache = hermes_home / "cache" / "delegation"
    cache.mkdir(parents=True)
    (cache / "live").symlink_to(outside, target_is_directory=True)

    ops = _collect_ops(hermes_home).operations

    assert ops.delegation_live_log_count == 0
    # The rest of the operations source still collects.
    assert ops.delegation_count == 3


def test_state_db_maintenance_happy_path(hermes_home: Path, sample_db: Path):
    # sample_db seeds state_meta from the wall clock; re-anchor the maintenance
    # epochs to _FIXED_NOW so the ages stay positive whatever today's date is.
    conn = _open_state_db(hermes_home)
    conn.execute(
        "UPDATE state_meta SET value = ? WHERE key = 'last_auto_prune'", (_FIXED_NOW - 7200,)
    )
    conn.execute(
        "UPDATE state_meta SET value = ? WHERE key = 'last_auto_archive'", (_FIXED_NOW - 86400,)
    )
    conn.commit()
    conn.close()

    ops = _collect_ops(hermes_home).operations
    assert ops.state_db_schema_version == 6
    assert ops.state_db_file_generation == "3"
    assert ops.state_db_fts_storage_version == "2"
    assert ops.last_auto_prune_age_seconds == pytest.approx(7200.0)
    assert ops.last_auto_archive_age_seconds == pytest.approx(86400.0)
    assert ops.state_db_size_bytes > 0
    assert ops.state_db_wal_size_bytes == 0


def test_state_db_maintenance_tolerates_missing_tables(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_state_db_tables(conn, include_schema_version=False)
    conn.commit()
    conn.close()
    ops = _collect_ops(hermes_home).operations
    assert ops.state_db_schema_version == 0
    assert ops.last_auto_prune_age_seconds is None
    assert ops.last_auto_archive_age_seconds is None
    assert ops.state_db_file_generation == ""


def test_state_db_maintenance_null_and_garbage_meta_values(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_state_db_tables(conn, include_schema_version=False)
    create_state_meta_table(conn, {"db_file_generation": "7"})
    conn.execute("INSERT INTO state_meta VALUES ('last_auto_prune', NULL)")
    conn.execute("INSERT INTO state_meta VALUES ('last_auto_archive', 'not-a-number')")
    conn.commit()
    conn.close()
    ops = _collect_ops(hermes_home).operations
    assert ops.last_auto_prune_age_seconds is None
    assert ops.last_auto_archive_age_seconds is None
    assert ops.state_db_file_generation == "7"


def test_state_db_schema_version_takes_max_of_version_column(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_state_db_tables(conn, include_schema_version=False)
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER, applied_at TEXT);"
        "INSERT INTO schema_version VALUES (4, 'a');"
        "INSERT INTO schema_version VALUES (9, 'b');"
    )
    conn.commit()
    conn.close()
    assert _collect_ops(hermes_home).operations.state_db_schema_version == 9


def test_state_db_wal_size_reported(hermes_home: Path, sample_db: Path):
    conn = _open_state_db(hermes_home)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE wal_probe (x INTEGER)")
    conn.commit()
    try:
        assert _collect_ops(hermes_home).operations.state_db_wal_size_bytes > 0
    finally:
        conn.close()


def test_state_snapshots_happy_path(
    hermes_home: Path, sample_db: Path, sample_state_snapshots: Path
):
    ops = _collect_ops(hermes_home).operations
    assert ops.snapshot_count == 2
    assert ops.snapshot_total_bytes == 2048 + 512 + 4096
    assert ops.newest_snapshot_age_seconds is not None


def test_state_snapshots_directory_absent(hermes_home: Path, sample_db: Path):
    ops = _collect_ops(hermes_home).operations
    assert ops.snapshot_count == 0
    assert ops.snapshot_total_bytes == 0
    assert ops.newest_snapshot_age_seconds is None


def test_state_snapshots_symlinked_entries_ignored(
    hermes_home: Path, sample_db: Path, tmp_path: Path
):
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"q" * 9999)
    root = hermes_home / "state-snapshots"
    root.mkdir()
    (root / "state-linked.db").symlink_to(outside)
    ops = _collect_ops(hermes_home).operations
    assert ops.snapshot_count == 0
    assert ops.snapshot_total_bytes == 0


def test_state_snapshots_scan_is_bounded(hermes_home: Path, sample_db: Path):
    root = hermes_home / "state-snapshots"
    root.mkdir()
    for index in range(260):
        (root / f"state-{index:04d}.db").write_bytes(b"z")
    assert _collect_ops(hermes_home).operations.snapshot_count == 200


def test_state_snapshot_failure_is_its_own_source_and_keeps_operations(
    hermes_home: Path, sample_db: Path, monkeypatch: pytest.MonkeyPatch
):
    def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("snapshot scan exploded")

    monkeypatch.setattr(collector_module, "_read_state_snapshots", boom)
    state = _collect_ops(hermes_home)
    assert "state_snapshots" in state.health.failed_sources
    assert state.operations.delegation_count == 3


def test_web_ui_build_stamp_read(hermes_home: Path, sample_web_ui_stamp: Path):
    ops = _collect_ops(hermes_home).operations
    assert ops.web_ui_build_hash == "314422207985"
    assert ops.web_ui_built_age_seconds is not None


def test_web_ui_build_stamp_absent(hermes_home: Path):
    ops = _collect_ops(hermes_home).operations
    assert ops.web_ui_build_hash == ""
    assert ops.web_ui_built_age_seconds is None


def test_web_ui_build_stamp_garbage_timestamp(hermes_home: Path):
    (hermes_home / "web-ui-build-stamp.json").write_text(
        json.dumps({"contentHash": "abcdef0123456789", "builtAt": "not-a-date"})
    )
    ops = _collect_ops(hermes_home).operations
    assert ops.web_ui_build_hash == "abcdef012345"
    assert ops.web_ui_built_age_seconds is None


def test_background_processes_from_spawn_ledger_carry_liveness(
    hermes_home: Path, sample_spawn_ledger: Path
):
    processes = _collect_ops(hermes_home, pid_exists=lambda pid: pid == 4242)
    by_purpose = {p.purpose: p for p in processes.background_processes}
    dashboard = by_purpose["dashboard"]
    assert dashboard.port == 9119
    assert dashboard.profile == "coding"
    assert dashboard.alive is True
    helper = by_purpose["mcp-helper"]
    assert helper.port == 0
    assert helper.profile == ""
    assert helper.alive is False


def test_background_processes_from_legacy_registry_carry_liveness(
    hermes_home: Path, sample_processes: Path
):
    processes = _collect_ops(hermes_home, pid_exists=lambda pid: pid == 4242)
    by_id = {p.session_id: p for p in processes.background_processes}
    assert by_id["proc_alpha"].alive is True
    assert by_id["proc_beta"].alive is False
    assert by_id["proc_alpha"].purpose == ""
    assert by_id["proc_alpha"].port == 0


def test_web_ui_build_stamp_naive_timestamp_is_treated_as_utc(hermes_home: Path):
    (hermes_home / "web-ui-build-stamp.json").write_text(
        json.dumps({"contentHash": "0123456789abcdef", "builtAt": "2026-09-07T14:08:09"})
    )
    ops = _collect_ops(hermes_home).operations
    assert ops.web_ui_built_age_seconds is not None
    assert ops.web_ui_built_age_seconds > 0


def test_delegation_live_log_symlinked_file_is_ignored(
    hermes_home: Path, sample_db: Path, tmp_path: Path
):
    outside = tmp_path / "outside.log"
    outside.write_text("nope\n")
    run_dir = hermes_home / "cache" / "delegation" / "live" / "deleg_a"
    run_dir.mkdir(parents=True)
    (run_dir / "task-0.log").symlink_to(outside)
    assert _collect_ops(hermes_home).operations.delegation_live_log_count == 0


def _write_blocked_scripts(home: Path, names_to_age: dict[str, float]) -> Path:
    root = home / "cache" / "blocked-scripts"
    root.mkdir(parents=True, exist_ok=True)
    for name, age in names_to_age.items():
        path = root / name
        path.write_text("#!/bin/sh\nrm -rf /  # secret payload\n")
        os.utime(path, (_FIXED_NOW - age, _FIXED_NOW - age))
    return root


def test_blocked_scripts_counted_with_newest_names(hermes_home: Path, sample_db: Path):
    _write_blocked_scripts(
        hermes_home,
        {
            "blocked-1788689099-90533172.sh": 7200.0,
            "blocked-1788794341-ed4ffaed.sh": 600.0,
            "blocked-1788794977-dcea0931.sh": 60.0,
            "blocked-old.sh": 90000.0,
        },
    )

    ops = _collect_ops(hermes_home).operations

    assert ops.blocked_script_count == 4
    assert ops.newest_blocked_script_age_seconds == 60.0
    assert ops.blocked_script_names == [
        "blocked-1788794977-dcea0931.sh",
        "blocked-1788794341-ed4ffaed.sh",
        "blocked-1788689099-90533172.sh",
    ]


def test_blocked_scripts_never_read_file_contents(hermes_home: Path, sample_db: Path):
    _write_blocked_scripts(hermes_home, {"blocked-a.sh": 10.0})

    payload = json.dumps(_collect_ops(hermes_home).operations.model_dump(mode="json"))

    assert "secret payload" not in payload
    assert "rm -rf" not in payload


def test_blocked_scripts_names_are_sanitized_and_capped(hermes_home: Path, sample_db: Path):
    _write_blocked_scripts(hermes_home, {"[bold]blocked-" + "x" * 60 + ".sh": 5.0})

    names = _collect_ops(hermes_home).operations.blocked_script_names

    assert len(names[0]) <= 40
    assert "\x1b" not in names[0]


def test_blocked_scripts_absent_directory_is_zero(hermes_home: Path, sample_db: Path):
    ops = _collect_ops(hermes_home).operations

    assert ops.blocked_script_count == 0
    assert ops.newest_blocked_script_age_seconds is None
    assert ops.blocked_script_names == []


def test_blocked_scripts_age_is_clamped_at_zero(hermes_home: Path, sample_db: Path):
    _write_blocked_scripts(hermes_home, {"blocked-future.sh": -600.0})

    assert _collect_ops(hermes_home).operations.newest_blocked_script_age_seconds == 0.0


def test_blocked_scripts_scan_is_bounded(hermes_home: Path, sample_db: Path):
    _write_blocked_scripts(hermes_home, {f"blocked-{i:04d}.sh": float(i) for i in range(260)})

    assert _collect_ops(hermes_home).operations.blocked_script_count == 200


def test_blocked_scripts_symlinked_entries_ignored(
    hermes_home: Path, sample_db: Path, tmp_path: Path
):
    outside = tmp_path / "outside.sh"
    outside.write_text("nope\n")
    root = hermes_home / "cache" / "blocked-scripts"
    root.mkdir(parents=True)
    (root / "blocked-linked.sh").symlink_to(outside)

    assert _collect_ops(hermes_home).operations.blocked_script_count == 0


def test_blocked_scripts_unreadable_dir_keeps_last_good_and_names_source(
    hermes_home: Path, sample_db: Path
):
    _skip_if_root()
    root = _write_blocked_scripts(hermes_home, {"blocked-a.sh": 30.0})
    c = Collector(hermes_home, clock=_fixed_clock)
    try:
        first = c.collect()
        assert first.operations.blocked_script_count == 1

        os.chmod(root, 0o000)
        if not _unreadable(root / "blocked-a.sh"):
            pytest.skip("filesystem ignores directory permissions")
        second = c.collect()
    finally:
        os.chmod(root, 0o755)
        c.close()

    assert "blocked_scripts" in second.health.failed_sources
    assert second.operations.blocked_script_count == 1
    assert second.operations.blocked_script_names == ["blocked-a.sh"]
    assert second.operations.delegation_count == 3


def test_oversized_goal_json_is_refused(hermes_home: Path):
    """Goal records share the delegation JSON cap instead of decoding unbounded."""
    conn = _open_state_db(hermes_home)
    create_state_db_tables(conn)
    create_state_meta_table(
        conn,
        {
            "goal:sess-small": json.dumps({"goal": "ship it", "status": "active"}),
            "goal:sess-huge": json.dumps({"goal": "x" * 70_000, "status": "active"}),
        },
    )
    conn.commit()
    conn.close()

    ops = _collect_ops(hermes_home).operations

    assert [goal.session_id for goal in ops.goals] == ["sess-small"]
    assert ops.goal_count == 1


def test_goal_json_under_the_cap_is_decoded(hermes_home: Path):
    conn = _open_state_db(hermes_home)
    create_state_db_tables(conn)
    create_state_meta_table(
        conn,
        {"goal:sess-a": json.dumps({"goal": "ship it", "notes": "n" * 8000, "status": "active"})},
    )
    conn.commit()
    conn.close()

    ops = _collect_ops(hermes_home).operations

    assert [goal.goal for goal in ops.goals] == ["ship it"]


def test_state_db_wal_is_snapshotted_once_per_change(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """HermesDB and the operations readout must share one WAL snapshot per tick."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    create_state_db_tables(conn)
    create_state_meta_table(conn, {"goal:sess-a": json.dumps({"goal": "g", "status": "active"})})
    conn.commit()

    snapshots: list[Path] = []
    original = db_module.snapshot_wal_database

    def counting(target: Path, *, prefix: str):
        snapshots.append(Path(target))
        return original(target, prefix=prefix)

    monkeypatch.setattr(db_module, "snapshot_wal_database", counting)
    monkeypatch.setattr(sqlite_util_module, "snapshot_wal_database", counting)

    c = Collector(hermes_home)
    try:
        assert c.collect().operations.goal_count == 1
        after_first = len(snapshots)
        c.collect()
        assert len(snapshots) == after_first

        conn.execute(
            "INSERT INTO state_meta VALUES (?, ?)",
            ("goal:sess-b", json.dumps({"goal": "g2", "status": "active"})),
        )
        conn.commit()
        assert c.collect().operations.goal_count == 2
    finally:
        c.close()
        conn.close()

    assert [path.name for path in snapshots] == ["state.db", "state.db"]


def test_corrupt_state_db_keeps_last_good_operations_rows(hermes_home: Path, sample_db: Path):
    """The shared readout raises; every state.db-backed source falls back."""
    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.operations.delegation_count > 0

        sample_db.write_bytes(b"not a sqlite database")
        second = c.collect()
    finally:
        c.close()

    assert second.operations.delegations == first.operations.delegations
    assert second.operations.delegation_count == first.operations.delegation_count
    assert "operations" in second.health.failed_sources


def test_symlinked_build_stamps_outside_home_read_as_absent(hermes_home: Path, tmp_path: Path):
    outside_desktop = tmp_path / "desktop-build-stamp.json"
    outside_desktop.write_text(json.dumps({"version": "9.9.9"}))
    outside_web = tmp_path / "web-ui-build-stamp.json"
    outside_web.write_text(json.dumps({"contentHash": "deadbeefcafe", "builtAt": "2026-01-01Z"}))
    (hermes_home / "desktop-build-stamp.json").symlink_to(outside_desktop)
    (hermes_home / "web-ui-build-stamp.json").symlink_to(outside_web)

    state = _collect_ops(hermes_home)

    assert state.operations.desktop_build_stamp == ""
    assert state.operations.web_ui_build_hash == ""
    assert state.operations.web_ui_built_age_seconds is None
    assert "operations" not in state.health.failed_sources
