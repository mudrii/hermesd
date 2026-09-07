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

import hermesd.collector as collector_module
from hermesd.collector import (
    Collector,
    _git_checkpoint_summary,
)
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import (
    _init_shadow_checkpoint_repo,
    _skip_if_root,
    _unreadable,
    create_kanban_db_tables,
    create_state_db_tables,
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


def _count_sqlite_connects(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    original = collector_module._connect_readonly_sqlite
    seen: list[Path] = []

    def counting(db_path: Path):
        seen.append(Path(db_path))
        return original(db_path)

    monkeypatch.setattr(collector_module, "_connect_readonly_sqlite", counting)
    return seen


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

    assert [p.name for p in connects].count("state.db") == 1


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

    assert [p.name for p in connects].count("state.db") == 2


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
