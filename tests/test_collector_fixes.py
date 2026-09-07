"""Regression tests for collector audit fixes.

Each test maps to a finding from the collector audit:
1. CronJob string-field coercion (numeric JSON values crash the cron source).
2. Per-board tolerance in _with_kanban_boards (one corrupt board kills all).
3. Cron-excerpt cache consulted before the file read; stale hit when mtime
   is unavailable.
4. (cache_write cost estimation lives in tests/test_cost_estimation.py)
5. Available-tools cache keyed only on the sessions.json index mtime.
6. _summarize_window bypasses the injectable clock.
7. Negative last_tick_ago_seconds when .tick.lock mtime is in the future.
8. Git checkpoint subprocess decode failures escape the handled exceptions.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermesd.collector import Collector, _git_checkpoint_summary
from tests.conftest import (
    _init_shadow_checkpoint_repo,
    create_kanban_db_tables,
    create_state_db_tables,
)

# --- 1. CronJob string-field coercion ----------------------------------------


def test_cron_jobs_with_numeric_fields_are_coerced_to_strings(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": 123,
                        "name": 456,
                        "schedule_display": 789,
                        "state": 1,
                        "enabled": True,
                        "deliver": "",
                        "next_run_at": 42,
                        "last_status": 7,
                    }
                ]
            }
        )
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.job_count == 1
    job = state.cron.jobs[0]
    assert job.job_id == "123"
    assert job.name == "456"
    assert job.schedule_display == "789"
    assert job.state == "1"
    assert job.next_run_at == "42"
    assert job.last_status == "7"


# --- 2. Kanban per-board tolerance --------------------------------------------


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


# --- 3. Cron-excerpt cache ordering -------------------------------------------


def test_cron_excerpt_cache_skips_file_read_when_mtime_unchanged(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    output_root = hermes_home / "cron" / "output"
    job_dir = output_root / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "out.txt").write_text("first output\n")

    import hermesd.collector as collector_module

    real_excerpt = collector_module._latest_cron_output_excerpt
    reads: list[int] = []

    def counting_excerpt(*args, **kwargs):
        reads.append(1)
        return real_excerpt(*args, **kwargs)

    monkeypatch.setattr(collector_module, "_latest_cron_output_excerpt", counting_excerpt)

    c = Collector(hermes_home)
    try:
        first = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
        second = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
    finally:
        c.close()

    assert first[0] == "first output"
    assert second == first
    assert len(reads) == 1  # cache hit must not re-read the file tail


def test_cron_excerpt_refreshes_when_mtime_unavailable(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    output_root = hermes_home / "cron" / "output"
    job_dir = output_root / "job-1"
    job_dir.mkdir(parents=True)
    output_file = job_dir / "out.txt"
    output_file.write_text("old output\n")

    monkeypatch.setattr("hermesd.collector._mtime", lambda path: None)

    c = Collector(hermes_home)
    try:
        first = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
        assert first[0] == "old output"
        output_file.write_text("new output\n")
        second = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
    finally:
        c.close()

    assert second[0] == "new output"


# --- 5. Available-tools cache staleness ----------------------------------------


def test_available_tools_refresh_when_session_file_changes(hermes_home: Path):
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    session_file = sessions_dir / "session_s1.json"
    session_file.write_text(json.dumps({"session_id": "s1", "tools": [{"name": "web_search"}]}))

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.available_tool_names == ["web_search"]

        # Change tool content inside the per-session file only; the index
        # (sessions.json) is untouched, so its mtime stays the same.
        session_file.write_text(json.dumps({"session_id": "s1", "tools": [{"name": "shell_exec"}]}))
        bumped = session_file.stat().st_mtime + 10
        os.utime(session_file, (bumped, bumped))

        second = c.collect()
    finally:
        c.close()

    assert second.available_tool_names == ["shell_exec"]


def test_available_tools_cache_binds_each_mtime_to_its_session_file(hermes_home: Path):
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(
        json.dumps({"a": {"session_id": "s1"}, "b": {"session_id": "s2"}})
    )
    first_file = sessions_dir / "session_s1.json"
    second_file = sessions_dir / "session_s2.json"
    first_file.write_text(json.dumps({"tools": [{"name": "first_old"}]}))
    second_file.write_text(json.dumps({"tools": [{"name": "second_old"}]}))
    first_mtime = 1_700_000_000
    second_mtime = first_mtime + 10
    os.utime(first_file, (first_mtime, first_mtime))
    os.utime(second_file, (second_mtime, second_mtime))

    c = Collector(hermes_home)
    try:
        assert c.collect().available_tool_names == ["first_old", "second_old"]

        first_file.write_text(json.dumps({"tools": [{"name": "first_new"}]}))
        second_file.write_text(json.dumps({"tools": [{"name": "second_new"}]}))
        os.utime(first_file, (second_mtime, second_mtime))
        os.utime(second_file, (first_mtime, first_mtime))

        refreshed = c.collect()
    finally:
        c.close()

    assert refreshed.available_tool_names == ["first_new", "second_new"]


def test_available_tools_rejects_session_id_path_traversal(hermes_home: Path):
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(
        json.dumps({"escaped": {"session_id": "x/../../secret"}})
    )
    (sessions_dir / "session_x").mkdir()
    (hermes_home / "secret.json").write_text(
        json.dumps({"tools": [{"name": "outside_secret_tool"}]})
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "outside_secret_tool" not in state.available_tool_names


def test_available_tools_rejects_symlinked_session_file(hermes_home: Path, tmp_path: Path):
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"tools": [{"name": "outside_secret_tool"}]}))
    (sessions_dir / "session_s1.json").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "outside_secret_tool" not in state.available_tool_names


def test_available_tools_rejects_symlinked_sessions_index(hermes_home: Path, tmp_path: Path):
    sessions_dir = hermes_home / "sessions"
    sessions_index = sessions_dir / "sessions.json"
    sessions_index.unlink(missing_ok=True)
    outside = tmp_path / "outside-index.json"
    outside.write_text(json.dumps({"a": {"session_id": "s1"}}))
    sessions_index.symlink_to(outside)
    (sessions_dir / "session_s1.json").write_text(
        json.dumps({"tools": [{"name": "outside_index_tool"}]})
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "outside_index_tool" not in state.available_tool_names
    assert "tools_index" in state.health.failed_sources


def test_available_tools_preserves_last_good_after_session_file_becomes_symlink(
    hermes_home: Path, tmp_path: Path
):
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    session_file = sessions_dir / "session_s1.json"
    session_file.write_text(json.dumps({"tools": [{"name": "safe_tool"}]}))
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"tools": [{"name": "outside_secret_tool"}]}))

    c = Collector(hermes_home)
    first = c.collect()
    assert first.available_tool_names == ["safe_tool"]
    session_file.unlink()
    session_file.symlink_to(outside)
    try:
        second = c.collect()
    finally:
        c.close()

    assert second.available_tool_names == ["safe_tool"]
    assert "outside_secret_tool" not in second.available_tool_names
    assert "tools_index" in second.health.failed_sources


def test_available_tools_cache_uses_nanosecond_file_signature(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    session_file = sessions_dir / "session_s1.json"
    session_file.write_text(json.dumps({"tools": [{"name": "old_tool"}]}))

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.available_tool_names == ["old_tool"]
        old_stat = session_file.stat()
        session_file.write_text(json.dumps({"tools": [{"name": "new_tool"}]}))
        os.utime(session_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1))
        monkeypatch.setattr("hermesd.collector._mtime", lambda path: 100.0)
        second = c.collect()
    finally:
        c.close()

    assert second.available_tool_names == ["new_tool"]


def test_cron_excerpt_cache_binds_signature_to_filename_with_equal_mtime(hermes_home: Path):
    output_root = hermes_home / "cron" / "output"
    job_dir = output_root / "job-1"
    job_dir.mkdir(parents=True)
    first_file = job_dir / "a.txt"
    first_file.write_text("first\n")
    os.utime(first_file, (100, 100))

    c = Collector(hermes_home)
    try:
        assert c._latest_cron_output_excerpt(output_root, "job-1", 32768)[0] == "first"
        first_file.unlink()
        second_file = job_dir / "b.txt"
        second_file.write_text("second\n")
        os.utime(second_file, (100, 100))
        second = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
    finally:
        c.close()

    assert second[0] == "second"


# --- 6. _summarize_window clock seam -------------------------------------------


def test_token_analytics_windows_use_injected_clock(hermes_home: Path):
    fake_now = 1_000_000.0
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, input_tokens) VALUES (?, ?, ?, ?)",
        ("s1", "cli", fake_now - 10 * 86400, 100),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: fake_now)
    try:
        state = c.collect()
    finally:
        c.close()

    windows = {window.label: window for window in state.token_analytics.windows}
    assert windows["7d"].session_count == 0
    assert windows["30d"].session_count == 1


# --- 7. Negative last_tick_ago_seconds ------------------------------------------


def test_cron_last_tick_ago_clamped_to_zero_for_future_mtime(hermes_home: Path):
    tick = hermes_home / "cron" / ".tick.lock"
    tick.write_text("")
    future = 2_000_000_000.0
    os.utime(tick, (future, future))

    c = Collector(hermes_home, clock=lambda: future - 100.0)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.last_tick_ago_seconds == 0.0


# --- 8. Git checkpoint subprocess decode ----------------------------------------


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


def test_cron_excerpt_cache_key_matches_read_content(hermes_home: Path, monkeypatch):
    """The cache key must be the mtime of the file actually read, not of an
    earlier independent directory scan (a newer output can land between them)."""
    c = Collector(hermes_home)
    try:
        calls: list[int] = []
        monkeypatch.setattr(
            "hermesd.collector._latest_cron_output_file",
            lambda *a, **k: Path("/fake/out.log"),
        )
        # Scan 1 reports the old mtime...
        monkeypatch.setattr("hermesd.collector._mtime", lambda p: 100.0)

        def fake_excerpt(*a, **k):
            calls.append(1)
            # ...but the read itself sees the newer file that just landed.
            return ("newer content", False, "out.log", 200.0)

        monkeypatch.setattr("hermesd.collector._latest_cron_output_excerpt", fake_excerpt)

        first = c._latest_cron_output_excerpt(Path("/root"), "job1", 1024)
        assert first[0] == "newer content"
        assert len(calls) == 1

        # Next refresh: scan 1 now sees the newer file -> must be a cache HIT.
        monkeypatch.setattr("hermesd.collector._mtime", lambda p: 200.0)
        second = c._latest_cron_output_excerpt(Path("/root"), "job1", 1024)
        assert second[0] == "newer content"
        assert len(calls) == 1  # no re-read: key is the content's own mtime
    finally:
        c.close()


def test_available_tools_refresh_when_non_max_session_file_changes(hermes_home: Path):
    """An edit to a session file whose mtime stays below the max must still
    invalidate the tools cache (a max-only key would miss it)."""
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(
        json.dumps({"a": {"session_id": "s1"}, "b": {"session_id": "s2"}})
    )
    file1 = sessions_dir / "session_s1.json"
    file2 = sessions_dir / "session_s2.json"
    file1.write_text(json.dumps({"session_id": "s1", "tools": [{"name": "web_search"}]}))
    file2.write_text(json.dumps({"session_id": "s2", "tools": [{"name": "shell_exec"}]}))
    os.utime(file1, (1000, 1000))
    os.utime(file2, (2000, 2000))  # file2 holds the max mtime

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.available_tool_names == ["shell_exec", "web_search"]

        # Edit file1; its mtime changes but stays below the max (2000).
        file1.write_text(json.dumps({"session_id": "s1", "tools": [{"name": "new_tool"}]}))
        os.utime(file1, (1500, 1500))

        second = c.collect()
    finally:
        c.close()

    assert second.available_tool_names == ["new_tool", "shell_exec"]
