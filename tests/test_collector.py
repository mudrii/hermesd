from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermesd.collector import Collector, _CollectionHealth
from hermesd.models import DashboardState
from tests.conftest import create_state_db_tables


def test_collect_full(populated_hermes_home: Path):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()
    assert isinstance(state, DashboardState)
    assert state.health.failed_sources == []
    assert state.health.ok_sources == state.health.total_sources
    assert state.runtime.agent_running is True
    assert state.gateway.running is True
    assert state.gateway.pid == 12345
    assert len(state.gateway.platforms) == 2
    assert len(state.sessions) == 2
    assert state.sessions[0].is_active is True  # ended_at is NULL
    assert state.tokens_total.input_tokens > 0
    assert state.config.model == "gpt-5.4"
    assert state.config.provider == "openai-codex"
    assert state.config.tool_search_enabled == "auto"
    assert state.config.kanban_auto_decompose is True
    assert state.config.dashboard_basic_auth_configured is True
    assert state.skills_memory.skill_count == 15
    assert state.channels.platform_count == 3
    assert state.kanban.task_count == 3
    assert state.operations.dashboard_process_count == 0
    assert len(state.operations.model_caches) == 2
    assert len(state.logs.agent_lines) > 0
    assert state.active_skin == "default"
    c.close()


def test_collect_missing_files(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    assert state.runtime.agent_running is False
    assert state.runtime.banner == "AGENT OFFLINE"
    assert state.sessions == []
    assert state.config.model == ""
    c.close()


def test_collect_gateway_not_running(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 99999,
                "gateway_state": "stopped",
                "platforms": {},
                "updated_at": "",
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    assert state.gateway.state == "stopped"
    c.close()


def test_collect_cron_tick(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    state = c.collect()
    assert state.cron.last_tick_ago_seconds is not None
    assert state.cron.last_tick_ago_seconds >= 0
    c.close()


def test_collect_providers(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    state = c.collect()
    assert len(state.skills_memory.providers) >= 1
    names = [p.name for p in state.skills_memory.providers]
    assert "openai-codex" in names
    c.close()


def test_collect_credential_pools_redacts_secrets(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    state = c.collect()
    pools = {pool.name: pool for pool in state.skills_memory.credential_pools}
    assert pools["openai-codex"].label == "Primary Codex"
    assert pools["openai-codex"].auth_type == "oauth"
    assert pools["openai-codex"].token_present is True
    assert pools["openai-codex"].request_count == 42
    assert pools["anthropic"].source == "env:ANTHROPIC_API_KEY"
    assert pools["anthropic"].last_status == "rate_limited"
    assert pools["anthropic"].cooldown_remaining == "58m"
    assert pools["anthropic"].token_present is True
    assert "sk-live-secret" not in repr(state.skills_memory.credential_pools)
    assert "sk-ant-secret" not in repr(state.skills_memory.credential_pools)
    c.close()


def test_collect_background_processes(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    state = c.collect()
    assert len(state.background_processes) == 2
    assert state.background_processes[0].session_id == "proc_alpha"
    assert state.background_processes[0].notify_on_complete is True
    assert state.background_processes[0].watcher_platform == "telegram"
    assert state.background_processes[0].watcher_user_name == "Operator"
    assert state.background_processes[0].watch_patterns == ["ERROR", "listening on port"]
    assert state.background_processes[1].command == "npm run dev"
    c.close()


def test_collect_integrations(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    state = c.collect()

    assert state.skills_memory.boot_md_present is True
    assert len(state.skills_memory.hooks) == 2
    assert state.skills_memory.hooks[0].name == "session-audit"
    assert state.skills_memory.hooks[1].events == ["gateway:startup", "agent:start"]

    plugins = {plugin.name: plugin for plugin in state.skills_memory.plugins}
    assert plugins["weather"].version == "1.2.3"
    assert plugins["weather"].dashboard_enabled is True
    assert plugins["weather"].enabled is True
    assert plugins["weather"].tool_count == 2
    assert plugins["disabled-plugin"].enabled is False

    mcp_servers = {server.name: server for server in state.skills_memory.mcp_servers}
    assert mcp_servers["playwright"].enabled is True
    assert mcp_servers["playwright"].transport == "command"
    assert "browser_navigate" in mcp_servers["playwright"].tool_filter
    assert mcp_servers["sheets"].enabled is False
    assert mcp_servers["sheets"].transport == "url"
    c.close()


def test_collect_memory_overview(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    state = c.collect()
    assert state.memory.provider == "supermemory"
    assert state.memory.memory_file_count >= 1
    assert "MEMORY.md" in state.memory.memory_files or state.memory.memory_files
    c.close()


def test_collect_checkpoints(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    state = c.collect()

    assert len(state.checkpoints) == 1
    checkpoint = state.checkpoints[0]
    assert checkpoint.repo_id == "abc123def4567890"
    assert checkpoint.commit_count == 2
    assert checkpoint.last_reason == "Refine config panel"
    assert checkpoint.workdir.endswith("project-alpha")
    assert checkpoint.workdir_name == "project-alpha"
    assert checkpoint.last_checkpoint_at is not None
    c.close()


def test_collect_sessions_with_null_columns(hermes_home: Path):
    """Sessions with NULL model, source, and token columns must not crash."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, source_required=False)
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at) VALUES (?, NULL, NULL, ?)",
        ("sess_null", time.time()),
    )
    conn.commit()
    conn.close()
    c = Collector(hermes_home)
    state = c.collect()
    assert len(state.sessions) == 1
    s = state.sessions[0]
    assert s.session_id == "sess_null"
    assert s.source == ""
    assert s.model == ""
    assert s.parent_session_id == ""
    assert s.billing_provider == ""
    assert s.cost_status == ""
    assert s.pricing_version == ""
    assert s.message_count == 0
    assert s.tool_call_count == 0
    assert s.input_tokens == 0
    assert s.output_tokens == 0
    assert s.cache_read_tokens == 0
    assert s.cache_write_tokens == 0
    assert s.reasoning_tokens == 0
    assert s.estimated_cost_usd == 0.0
    assert s.api_call_count == 0
    assert s.cwd == ""
    assert s.archived is False
    assert s.rewind_count == 0
    assert s.handoff_state == ""
    assert s.title is None
    c.close()


def test_collect_sessions_reads_newer_runtime_columns(hermes_home: Path):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn)
    conn.executescript(
        """
        ALTER TABLE sessions ADD COLUMN api_call_count INTEGER DEFAULT 0;
        ALTER TABLE sessions ADD COLUMN cwd TEXT;
        ALTER TABLE sessions ADD COLUMN rewind_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE sessions ADD COLUMN handoff_state TEXT;
        ALTER TABLE sessions ADD COLUMN handoff_platform TEXT;
        ALTER TABLE sessions ADD COLUMN handoff_error TEXT;
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, api_call_count, cwd, rewind_count, "
        "archived, handoff_state, handoff_platform, handoff_error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "sess_new",
            "cli",
            time.time(),
            8,
            "/tmp/project",
            2,
            1,
            "pending",
            "telegram",
            "delivery failed",
        ),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    session = state.sessions[0]

    assert session.api_call_count == 8
    assert session.cwd == "/tmp/project"
    assert session.rewind_count == 2
    assert session.archived is True
    assert session.is_active is False
    assert session.handoff_state == "pending"
    assert session.handoff_platform == "telegram"
    assert session.handoff_error == "delivery failed"
    c.close()


def test_collect_kanban_state(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    state = c.collect()

    assert state.kanban.db_present is True
    assert state.kanban.task_count == 3
    assert state.kanban.run_count == 2
    assert state.kanban.event_count == 1
    assert state.kanban.comment_count == 1
    assert state.kanban.status_counts == {"blocked": 1, "in_progress": 2}
    assert state.kanban.active_tasks[0].task_id == "t_active"
    assert state.kanban.problem_tasks[0].task_id == "t_blocked"
    assert state.kanban.problem_tasks[0].last_failure_error == "missing credentials"
    c.close()


def test_collect_channels_and_operations(populated_hermes_home: Path):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()

    assert state.channels.updated_at == "2026-06-04T17:14:31Z"
    assert state.channels.platform_count == 3
    platforms = {platform.name: platform for platform in state.channels.platforms}
    assert platforms["telegram"].entry_count == 1
    assert platforms["telegram"].connected is True
    assert platforms["feishu"].capabilities == ["meeting invites"]

    caches = {cache.name: cache for cache in state.operations.model_caches}
    assert caches["models_dev_cache.json"].provider_count == 2
    assert caches["models_dev_cache.json"].model_count == 3
    assert state.operations.pr_monitors[0].repo == "NousResearch/hermes-agent"
    assert state.operations.pr_monitors[0].monitored_count == 2
    c.close()


def test_collect_does_not_mutate_hermes_home(populated_hermes_home: Path):
    before = _file_mtimes(populated_hermes_home)
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)

    try:
        for _ in range(3):
            c.collect()
    finally:
        c.close()

    assert _file_mtimes(populated_hermes_home) == before


def test_collect_mtime_cache(populated_hermes_home: Path, monkeypatch):
    sessions_dir = populated_hermes_home / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    (sessions_dir / "sessions.json").write_text(json.dumps({"one": {"session_id": "one"}}))
    (sessions_dir / "session_one.json").write_text(
        json.dumps({"session_id": "one", "tools": [{"name": "cached_tool"}]})
    )
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    read_count = 0
    original_read = c._read_json_cached

    def counting_read(path: Path):
        nonlocal read_count
        read_count += 1
        return original_read(path)

    monkeypatch.setattr(c, "_read_json_cached", counting_read)
    try:
        tools1 = c._collect_available_tools()
        reads_after_first_call = read_count
        tools2 = c._collect_available_tools()
    finally:
        c.close()

    assert tools2 == tools1
    assert read_count == reads_after_first_call


def _file_mtimes(root: Path) -> dict[Path, int]:
    return {
        path.relative_to(root): path.stat().st_mtime_ns
        for path in root.rglob("*")
        if path.is_file()
    }


def test_collection_health_uses_default_when_fallback_also_fails():
    health = _CollectionHealth()

    def fail() -> str:
        raise RuntimeError("unavailable")

    assert health.collect(fail, "source", fail, lambda: "default") == "default"
    assert health.failed_sources == ["source"]
    assert "RuntimeError: unavailable" in health.errors["source"]
    assert health.total_sources == 1


def test_collect_recent_activity_suppresses_offline_banner(hermes_home: Path, sample_db: Path):
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": 0, "gateway_state": "stopped", "platforms": {}})
    )
    now = time.time()
    os.utime(sample_db, (now, now))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    assert state.runtime.agent_running is True
    assert state.runtime.banner == ""
    c.close()


def test_collect_preserves_last_good_source_on_failure(populated_hermes_home: Path, monkeypatch):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()

    def boom(*args: object, **kwargs: object) -> list:
        raise RuntimeError("tool stats unavailable")

    monkeypatch.setattr(c, "_collect_tool_stats", boom)
    state2 = c.collect()

    assert state2.tool_stats == state1.tool_stats
    assert "tool_stats" in state2.health.failed_sources
    assert "RuntimeError: tool stats unavailable" in state2.health.errors["tool_stats"]
    assert state2.health.ok_sources == state2.health.total_sources - 1
    c.close()


def test_collect_reads_session_rows_once_per_cycle(hermes_home: Path):
    class CountingDB:
        def __init__(self) -> None:
            self.session_reads = 0

        def read_sessions(self) -> list[dict[str, object]]:
            self.session_reads += 1
            return [
                {
                    "id": "sess_once",
                    "source": "cli",
                    "model": "gpt-5.4",
                    "parent_session_id": None,
                    "billing_provider": "openai",
                    "cost_status": "",
                    "pricing_version": "",
                    "message_count": 2,
                    "tool_call_count": 3,
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_tokens": 25,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "estimated_cost_usd": 0.01,
                    "started_at": time.time(),
                    "ended_at": None,
                    "title": "Session once",
                }
            ]

        def read_tool_stats(self) -> list[dict[str, object]]:
            return []

        @property
        def last_read_sessions_stale(self) -> bool:
            return False

        @property
        def last_read_tool_stats_stale(self) -> bool:
            return False

        def close(self) -> None:
            pass

    db = CountingDB()
    c = Collector(hermes_home, db_factory=lambda path: db)

    state = c.collect()

    assert db.session_reads == 1
    assert state.sessions[0].session_id == "sess_once"
    assert state.tokens_total.input_tokens == 100
    assert state.total_tool_calls == 3
    assert state.tool_stats[0].name == "cli:s_once"
    c.close()


def test_collect_marks_session_derived_sources_failed_when_session_read_fails(
    populated_hermes_home: Path,
    monkeypatch,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()

    def boom() -> list[dict[str, object]]:
        raise RuntimeError("sessions unavailable")

    monkeypatch.setattr(c._db, "read_sessions", boom)
    monkeypatch.setattr(c._db, "read_tool_stats", lambda: [])

    state2 = c.collect()

    assert state2.sessions == state1.sessions
    assert state2.tokens_total == state1.tokens_total
    assert set(state2.health.failed_sources) >= {
        "sessions",
        "tokens_today",
        "tokens_total",
        "token_analytics",
        "tool_stats",
        "tool_call_total",
    }
    c.close()


def test_collect_marks_session_derived_sources_failed_when_db_returns_stale_cache(
    populated_hermes_home: Path,
    monkeypatch,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()

    def fail_read(conn: sqlite3.Connection) -> list[dict[str, object]]:
        raise sqlite3.OperationalError("db unavailable")

    monkeypatch.setattr(c._db, "_current_version", lambda: 999)
    monkeypatch.setattr(c._db, "_read_all_sessions", fail_read)
    monkeypatch.setattr(c._db, "read_tool_stats", lambda: [])

    state2 = c.collect()

    assert state2.sessions == state1.sessions
    assert set(state2.health.failed_sources) >= {
        "sessions",
        "tokens_today",
        "tokens_total",
        "token_analytics",
        "tool_stats",
        "tool_call_total",
    }
    assert (
        state2.health.errors["sessions"] == "read_sessions returned cached rows after sqlite error"
    )
    c.close()


def test_collect_does_not_raise_on_unvalidatable_session_row(hermes_home: Path):
    """A row pydantic cannot coerce (NULL id) must not raise out of collect()."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, source_required=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (NULL, 'cli', ?)",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert state.sessions == []
    assert "session_models" in state.health.failed_sources
    c.close()


def test_collect_preserves_last_good_sessions_on_validation_error(hermes_home: Path):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, source_required=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('sess_good', 'cli', ?)",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state1 = c.collect()
    assert [s.session_id for s in state1.sessions] == ["sess_good"]

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (NULL, 'cli', ?)",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    state2 = c.collect()

    assert state2.sessions == state1.sessions
    assert "session_models" in state2.health.failed_sources
    assert state2.config == state1.config  # other sources still populate
    c.close()


def test_collect_runtime_status_failure_preserves_last_good(
    populated_hermes_home: Path,
    monkeypatch,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()

    def boom(*args: object, **kwargs: object):
        raise RuntimeError("runtime status unavailable")

    monkeypatch.setattr(c, "_collect_runtime_status", boom)
    state2 = c.collect()

    assert state2.runtime == state1.runtime
    assert "runtime" in state2.health.failed_sources
    assert state2.health.ok_sources == state2.health.total_sources - 1
    c.close()


def test_collect_tool_stats_marked_failed_when_db_returns_stale_cache(
    populated_hermes_home: Path,
    monkeypatch,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()
    assert state1.tool_stats

    def fail_read(conn: sqlite3.Connection) -> list[dict[str, object]]:
        raise sqlite3.OperationalError("db unavailable")

    monkeypatch.setattr(c._db, "_current_version", lambda: 999)
    monkeypatch.setattr(c._db, "_read_tool_stats", fail_read)

    state2 = c.collect()

    assert state2.tool_stats == state1.tool_stats
    assert "tool_stats" in state2.health.failed_sources
    assert "tool stats are stale" in state2.health.errors["tool_stats"]
    c.close()


def test_collect_tool_stats_handles_null_session_id(hermes_home: Path):
    """A NULL session id must not fail the tool_stats fallback path."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, source_required=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, tool_call_count) VALUES (NULL, 'cli', ?, 5)",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()

    assert "tool_stats" not in state.health.failed_sources
    assert [t.name for t in state.tool_stats] == ["cli:?"]
    assert state.tool_stats[0].call_count == 5
    c.close()


def test_search_session_ids_does_not_block_during_slow_collect(
    populated_hermes_home: Path,
    monkeypatch,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    c.collect()

    entered = threading.Event()
    release = threading.Event()

    def slow_checkpoints():
        entered.set()
        release.wait(timeout=10)
        return []

    monkeypatch.setattr(c, "_collect_checkpoints", slow_checkpoints)
    collect_thread = threading.Thread(target=c.collect, daemon=True)
    collect_thread.start()

    results: list[set[str]] = []

    def search() -> None:
        results.append(c.search_session_ids_by_message("response"))

    try:
        assert entered.wait(timeout=10)
        search_thread = threading.Thread(target=search, daemon=True)
        search_thread.start()
        search_thread.join(timeout=5)
        assert not search_thread.is_alive(), "search blocked behind slow collect"
        assert results and "sess_001" in results[0]
    finally:
        release.set()
        collect_thread.join(timeout=10)
    c.close()


def test_search_session_ids_raises_when_db_returns_stale_cache(hermes_home: Path):
    """A stale (cached-after-error) search result must surface as an error, not silently."""

    class StaleSearchDB:
        def __init__(self, db_path: Path) -> None:
            self.db_path = db_path

        def search_session_ids_by_message(self, query: str) -> set[str]:
            return {"sess_cached"}

        @property
        def last_message_search_stale(self) -> bool:
            return True

        def close(self) -> None:
            return None

    c = Collector(hermes_home, db_factory=StaleSearchDB)
    try:
        with pytest.raises(RuntimeError, match="message search returned cached rows"):
            c.search_session_ids_by_message("anything")
    finally:
        c.close()


def test_collect_reuses_session_summaries_when_rows_unchanged(
    populated_hermes_home: Path,
    monkeypatch,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    session_builds = 0
    analytics_builds = 0
    original_sessions = c._collect_sessions
    original_analytics = c._collect_token_analytics

    def counting_sessions(rows=None):
        nonlocal session_builds
        session_builds += 1
        return original_sessions(rows)

    def counting_analytics(rows=None):
        nonlocal analytics_builds
        analytics_builds += 1
        return original_analytics(rows)

    monkeypatch.setattr(c, "_collect_sessions", counting_sessions)
    monkeypatch.setattr(c, "_collect_token_analytics", counting_analytics)

    state1 = c.collect()
    state2 = c.collect()

    assert session_builds == 1
    assert analytics_builds == 1
    assert state2.sessions == state1.sessions
    assert state2.tokens_today == state1.tokens_today
    assert state2.tokens_total == state1.tokens_total
    assert state2.token_analytics == state1.token_analytics
    assert state2.total_tool_calls == state1.total_tool_calls
    c.close()


def test_collect_recomputes_session_summaries_when_rows_change(
    hermes_home: Path,
    sample_db: Path,
):
    c = Collector(hermes_home)
    state1 = c.collect()
    assert len(state1.sessions) == 2

    conn = sqlite3.connect(str(sample_db))
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, input_tokens) "
        "VALUES ('sess_003', 'cli', ?, 777)",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    state2 = c.collect()

    assert len(state2.sessions) == 3
    assert state2.tokens_total.input_tokens == state1.tokens_total.input_tokens + 777
    c.close()


def test_collect_recomputes_session_summaries_when_date_changes(
    populated_hermes_home: Path,
    monkeypatch,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    builds = 0
    original = c._collect_tokens_today

    def counting(rows=None):
        nonlocal builds
        builds += 1
        return original(rows)

    monkeypatch.setattr(c, "_collect_tokens_today", counting)

    c.collect()
    assert builds == 1
    monkeypatch.setattr("hermesd.collector._local_date", lambda: "1999-01-01")
    c.collect()
    assert builds == 2
    c.close()


def test_collect_preserves_token_analytics_on_mid_collection_failure(
    populated_hermes_home: Path,
    monkeypatch,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()

    def boom(rows=None):
        raise RuntimeError("analytics unavailable")

    monkeypatch.setattr(c, "_collect_token_analytics", boom)

    # Change the rows so the derived-summary cache cannot mask the failure.
    conn = sqlite3.connect(str(populated_hermes_home / "state.db"))
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES ('sess_extra', 'cli', ?)",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    state2 = c.collect()

    assert state2.token_analytics == state1.token_analytics
    assert "token_analytics" in state2.health.failed_sources
    c.close()
