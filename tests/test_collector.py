"""Core collector: construction, collect() orchestration, health fallback,
available-tool discovery, and cross-cutting redaction helpers."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermesd.collector import (
    Collector,
    _coerce_float,
    _coerce_int,
    _CollectionHealth,
    _has_secret_material,
    _path_resolves_under,
    _redact_command_string,
    _redact_secret_args,
    _redact_secret_url,
)
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
    assert state.cron.max_parallel_jobs == 3
    assert state.cron.wrap_response is True
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


def test_collect_operations_counts_current_hermesd_background_process(
    populated_hermes_home: Path,
):
    processes = populated_hermes_home / "processes.json"
    rows = json.loads(processes.read_text())
    rows[1]["command"] = "uv run hermesd --snapshot"
    processes.write_text(json.dumps(rows))

    c = Collector(populated_hermes_home)
    state = c.collect()

    assert state.operations.dashboard_process_count == 1
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
    (populated_hermes_home / "desktop-build-stamp.json").write_text(
        json.dumps({"version": "desktop-2026.6.14"})
    )
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()

    assert state.channels.updated_at == "2026-06-04T17:14:31Z"
    assert state.channels.platform_count == 3
    platforms = {platform.name: platform for platform in state.channels.platforms}
    assert platforms["telegram"].entry_count == 1
    assert platforms["telegram"].connected is True
    assert platforms["feishu"].capabilities == ["meeting invites"]

    caches = {cache.name: cache for cache in state.operations.model_caches}
    assert state.operations.desktop_build_stamp == "desktop-2026.6.14"
    assert caches["models_dev_cache.json"].provider_count == 2
    assert caches["models_dev_cache.json"].model_count == 3
    assert state.operations.pr_monitors[0].repo == "NousResearch/hermes-agent"
    assert state.operations.pr_monitors[0].monitored_count == 2
    c.close()


def test_collection_health_uses_default_when_fallback_also_fails():
    health = _CollectionHealth()

    def fail() -> str:
        raise RuntimeError("unavailable")

    assert health.collect(fail, "source", fail, lambda: "default") == "default"
    assert health.failed_sources == ["source"]
    assert "RuntimeError: unavailable" in health.errors["source"]
    assert health.total_sources == 1


def test_collection_health_redacts_secret_material_from_errors():
    health = _CollectionHealth()

    def fail() -> str:
        raise RuntimeError("request failed token=secret-value")

    assert health.collect(lambda: "cached", "source", fail, lambda: "default") == "cached"
    assert "[REDACTED]" in health.errors["source"]
    assert "secret-value" not in health.errors["source"]


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

        def read_model_usage(self, now: float) -> dict[str, list[dict[str, object]]]:
            return {"all": [], "24h": [], "7d": []}

        @property
        def last_read_sessions_stale(self) -> bool:
            return False

        @property
        def last_read_tool_stats_stale(self) -> bool:
            return False

        @property
        def last_read_model_usage_stale(self) -> bool:
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


def test_collect_preserves_session_derived_state_when_real_db_becomes_unreadable(
    populated_hermes_home: Path,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()

    db_path = populated_hermes_home / "state.db"
    db_path.write_bytes(b"not a sqlite database")
    state2 = c.collect()

    assert state2.sessions == state1.sessions
    assert state2.tokens_total == state1.tokens_total
    assert state2.tokens_today == state1.tokens_today
    assert state2.token_analytics == state1.token_analytics
    assert state2.total_tool_calls == state1.total_tool_calls
    assert set(state2.health.failed_sources) >= {
        "sessions",
        "tokens_today",
        "tokens_total",
        "token_analytics",
        "tool_stats",
        "tool_call_total",
    }
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


def test_collect_recomputes_today_summaries_when_local_date_changes(
    hermes_home: Path,
    sample_db: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    today_context: dict[str, float | str] = {
        "date": "2026-06-14",
        "cutoff": time.time() - 7200,
    }
    monkeypatch.setattr(
        "hermesd.collector._local_date",
        lambda _now: str(today_context["date"]),
    )
    monkeypatch.setattr(
        "hermesd.collector._today_epoch",
        lambda _now: float(today_context["cutoff"]),
    )
    c = Collector(hermes_home)
    state1 = c.collect()
    assert state1.tokens_today.input_tokens == 21_500

    today_context["date"] = "2026-06-15"
    today_context["cutoff"] = time.time() + 60
    state2 = c.collect()

    assert state2.tokens_today.input_tokens == 0
    assert state2.tokens_total.input_tokens == state1.tokens_total.input_tokens
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


@pytest.mark.parametrize(
    ("relative_path", "state_value"),
    [
        ("processes.json", lambda state: state.background_processes),
        ("auth.json", lambda state: state.skills_memory.credential_pools),
        ("cron/jobs.json", lambda state: state.cron.jobs),
        (
            "channel_directory.json",
            lambda state: [job.delivery_target_label for job in state.cron.jobs],
        ),
        (".update_check", lambda state: state.version_behind),
        ("sessions/sessions.json", lambda state: state.available_tool_names),
    ],
)
def test_collect_preserves_last_good_json_sources_on_corruption(
    populated_hermes_home: Path,
    relative_path: str,
    state_value,
):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()
    expected = state_value(state1)

    path = populated_hermes_home / relative_path
    path.write_text("{not valid json")
    os.utime(path, None)
    state2 = c.collect()

    assert state_value(state2) == expected
    c.close()


def test_collect_after_close_raises(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    c.close()

    with pytest.raises(RuntimeError, match="collector is closed"):
        c.collect()


def test_coerce_float_handles_edge_cases():
    assert _coerce_float(None) == 0.0
    assert _coerce_float("not-a-number") == 0.0
    assert _coerce_float("nan") == 0.0
    assert _coerce_float(float("inf")) == 0.0
    assert _coerce_float("-1.25") == -1.25
    assert _coerce_float(True) == 1.0


def test_coerce_int_handles_bool_values():
    assert _coerce_int(True) == 1
    assert _coerce_int(False) == 0


def test_coerce_int_truncates_float_values():
    assert _coerce_int(3.9) == 3
    assert _coerce_int(-2.5) == -2


def test_coerce_int_handles_bytes_and_bytearray():
    assert _coerce_int(b"42") == 42
    assert _coerce_int(bytearray(b"7")) == 7
    assert _coerce_int(b"not-a-number") == 0


def test_redact_secret_args_handles_mixed_and_nested_values():
    redacted = _redact_secret_args(
        [
            "cmd",
            42,
            ["--api-key", "sk-secret"],
            {"token": "secret-token"},
            "https://example.com/path?token=secret&ok=yes",
        ]
    )

    text = " ".join(redacted)
    assert "sk-secret" not in text
    assert "secret-token" not in text
    assert "token=secret" not in text
    assert "--api-key [REDACTED]" in text
    assert "[REDACTED]" in text
    assert "ok=yes" in text


def test_redact_secret_url_hides_userinfo_and_secret_query_values():
    redacted = _redact_secret_url(
        "https://user:password@example.com:8443/mcp?token=abc123&safe=value"
    )

    assert redacted == "https://[REDACTED]@example.com:8443/mcp?token=[REDACTED]&safe=value"
    assert "password" not in redacted
    assert "abc123" not in redacted


def test_redact_secret_url_tolerates_invalid_port():
    redacted = _redact_secret_url("https://user:password@example.com:bad/mcp?token=abc123")

    assert redacted == "https://[REDACTED]@example.com/mcp?token=[REDACTED]"
    assert "password" not in redacted
    assert "abc123" not in redacted


def test_redact_secret_args_handles_aliases_headers_and_dicts():
    redacted = _redact_secret_args(
        [
            "--bearer",
            "secret-token",
            "-H",
            "Authorization: Bearer secret-token",
            "--client-secret=client-secret",
            {"Authorization": "Bearer secret-token"},
            ["--x-api-key", "secret-token"],
        ]
    )

    text = " ".join(redacted)
    assert "secret-token" not in text
    assert "client-secret=client-secret" not in text
    assert text.count("[REDACTED]") >= 5


def test_redact_secret_args_non_list_returns_empty():
    assert _redact_secret_args("--token secret") == []
    assert _redact_secret_args(None) == []


def test_has_secret_material_detects_nested_and_inline_secrets():
    assert _has_secret_material({"note": "Authorization: Bearer abc"}) is True
    assert _has_secret_material({"outer": {"token": "abc"}}) is True
    assert _has_secret_material({"items": [{"api_key": "abc"}]}) is True
    assert _has_secret_material({"items": ["bearer abc"]}) is True
    assert _has_secret_material({"items": [["x-api-key: abc"]]}) is True
    assert _has_secret_material({"plain": "value", "items": ["safe"]}) is False


def test_redact_command_string_with_unbalanced_quotes_falls_back_to_text_redaction():
    redacted = _redact_command_string("run --token=secret-value 'unbalanced")
    assert "secret-value" not in redacted
    assert "[REDACTED]" in redacted


def test_collect_available_tools_from_session_json(hermes_home: Path):
    sessions_json = hermes_home / "sessions" / "sessions.json"
    sessions_json.write_text(
        json.dumps(
            {
                "entry1": {"session_id": "s1"},
                "entry2": {"session_id": "s2"},
            }
        )
    )
    session_file = hermes_home / "sessions" / "session_s1.json"
    session_file.write_text(
        json.dumps(
            {
                "session_id": "s1",
                "tools": [{"name": "terminal"}, {"name": "web_search"}, {"name": "read_file"}],
            }
        )
    )
    second_session_file = hermes_home / "sessions" / "session_s2.json"
    second_session_file.write_text(
        json.dumps(
            {
                "session_id": "s2",
                "tools": [{"name": "read_file"}, {"name": "write_file"}, {"name": "fetch_url"}],
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.available_tools == 5
    assert state.available_tool_names == [
        "fetch_url",
        "read_file",
        "terminal",
        "web_search",
        "write_file",
    ]
    c.close()


def test_collect_available_tools_accepts_string_tool_entries(hermes_home: Path):
    """A session file may list tools as bare strings instead of objects."""
    (hermes_home / "sessions" / "sessions.json").write_text(
        json.dumps({"entry1": {"session_id": "s1"}})
    )
    (hermes_home / "sessions" / "session_s1.json").write_text(
        json.dumps({"session_id": "s1", "tools": ["terminal", "web_search"]})
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.available_tools == 2
    assert state.available_tool_names == ["terminal", "web_search"]
    c.close()


def test_collect_available_tools_no_sessions_json(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.available_tools == 0
    c.close()


def test_json_cache_returns_stale_on_read_error(hermes_home: Path):
    path = hermes_home / "test.json"
    path.write_text(json.dumps({"key": "value"}))
    c = Collector(hermes_home)
    data1 = c._read_json_cached(path)
    assert data1 == {"key": "value"}
    path.write_text("NOT VALID JSON{{{")
    data2 = c._read_json_cached(path)
    assert data2 == {"key": "value"}
    c.close()


def test_json_cache_returns_empty_mapping_on_missing_file(hermes_home: Path):
    c = Collector(hermes_home)
    data = c._read_json_cached(hermes_home / "nonexistent.json")
    assert data == {}
    c.close()


def _write_banner_snapshot(hermes_home: Path, payload: object) -> Path:
    cache_dir = hermes_home / "cache"
    cache_dir.mkdir(exist_ok=True)
    path = cache_dir / "banner_snapshot.json"
    path.write_text(json.dumps(payload))
    return path


def _write_legacy_sessions(hermes_home: Path, tool_name: str) -> None:
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    (sessions_dir / "session_s1.json").write_text(
        json.dumps({"session_id": "s1", "tools": [{"name": tool_name}]})
    )


def test_available_tools_read_banner_snapshot(hermes_home: Path):
    _write_banner_snapshot(
        hermes_home,
        {
            "fingerprint": "f1",
            "enabled_toolsets": ["core"],
            "tools": [
                {"type": "function", "function": {"name": "web_search"}},
                {"type": "function", "function": {"name": "shell_exec"}},
                {"type": "function", "function": {"name": "web_search"}},
            ],
        },
    )
    # The legacy index is now a stub that names no sessions.
    (hermes_home / "sessions" / "sessions.json").write_text(json.dumps({"_README": "legacy"}))

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == ["shell_exec", "web_search"]
    assert state.available_tools == 2
    assert "tools_index" not in state.health.failed_sources


def test_available_tools_prefer_banner_snapshot_over_session_files(hermes_home: Path):
    _write_banner_snapshot(
        hermes_home,
        {"tools": [{"type": "function", "function": {"name": "banner_tool"}}]},
    )
    _write_legacy_sessions(hermes_home, "legacy_tool")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == ["banner_tool"]


def test_available_tools_tolerate_malformed_banner_entries(hermes_home: Path):
    _write_banner_snapshot(
        hermes_home,
        {
            "tools": [
                "not-a-mapping",
                {"type": "function"},
                {"type": "function", "function": {"name": 42}},
                {"type": "function", "function": {"name": "good_tool"}},
            ]
        },
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == ["good_tool"]


def test_available_tools_fall_back_to_sessions_when_banner_has_no_names(hermes_home: Path):
    _write_banner_snapshot(hermes_home, {"tools": "not-a-list"})
    _write_legacy_sessions(hermes_home, "legacy_tool")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == ["legacy_tool"]


def test_available_tools_empty_without_banner_or_sessions(hermes_home: Path):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == []
    assert state.available_tools == 0


def test_session_tool_scan_does_not_retain_parsed_session_documents(hermes_home: Path):
    """The fallback scan caches extracted names, not whole session documents."""
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    session_file = sessions_dir / "session_s1.json"
    session_file.write_text(
        json.dumps({"session_id": "s1", "bulk": "x" * 4096, "tools": [{"name": "web_search"}]})
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
        # White-box on purpose: the defect is unbounded retention of parsed
        # session JSON inside LastGoodFileCache.
        cached_paths = set(c._file_cache._json_values)
    finally:
        c.close()

    assert state.available_tool_names == ["web_search"]
    assert str(session_file) not in cached_paths


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan"), "inf", "-inf", "nan"],
)
def test_coerce_int_rejects_non_finite_values(value: object):
    assert _coerce_int(value) == 0


def test_close_returns_promptly_and_skips_remaining_sources(hermes_home: Path, sample_db: Path):
    started = threading.Event()
    release = threading.Event()
    c = Collector(hermes_home)
    real_gateway = c._collect_gateway
    later_source_calls: list[int] = []
    real_config = c._collect_config

    def slow_gateway():
        started.set()
        release.wait(10)
        return real_gateway()

    def counting_config():
        later_source_calls.append(1)
        return real_config()

    c._collect_gateway = slow_gateway
    c._collect_config = counting_config

    collect_errors: list[BaseException] = []

    def run_collect() -> None:
        try:
            c.collect()
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            collect_errors.append(exc)

    closed = threading.Event()
    collector_thread = threading.Thread(target=run_collect)
    collector_thread.start()
    try:
        assert started.wait(10)
        closer = threading.Thread(target=lambda: (c.close(), closed.set()))
        closer.start()
        # Give close() time to reach the collect lock it cannot take yet.
        time.sleep(0.1)
        release.set()
        start = time.perf_counter()
        assert closed.wait(10)
        elapsed = time.perf_counter() - start
    finally:
        release.set()
        collector_thread.join(10)
        closer.join(10)

    assert collect_errors == []
    assert elapsed < 1.0
    assert later_source_calls == []


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


def test_available_tools_cache_hit_skips_reread(hermes_home: Path, monkeypatch):
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    session_file = sessions_dir / "session_s1.json"
    session_file.write_text(json.dumps({"session_id": "s1", "tools": [{"name": "web_search"}]}))

    # Count real file opens of the per-session file (observable behavior) rather
    # than wrapping a private collector method.
    opens: list[Path] = []
    real_open = Path.open

    def counting_open(self: Path, *args, **kwargs):
        if self == session_file:
            opens.append(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.available_tools == 1
        assert "web_search" in first.available_tool_names
        assert len(opens) >= 1  # cold collect opened the per-session file

        # Second collect with unchanged sessions.json mtime: the available-tools
        # branch returns the cached (count, names) without re-opening the
        # per-session file.
        opens.clear()
        second = c.collect()
        assert second.available_tools == 1
        assert second.available_tool_names == first.available_tool_names
        assert opens == []
    finally:
        c.close()


def test_path_resolves_under_false_when_resolve_raises(tmp_path: Path, monkeypatch):
    # On Linux/older CPython a symlink-loop makes Path.resolve raise OSError
    # (ELOOP) or RuntimeError; on macOS/CPython 3.13 resolve(strict=False) is
    # lexical and never raises. To prove the read-only guard's contract on every
    # platform we force the documented failure mode. This patches the stdlib
    # boundary (Path.resolve), not any collector internal.
    root = tmp_path / "home"
    root.mkdir()
    real_resolve = Path.resolve

    def boom(self, *args, **kwargs):
        if self.name == "escape":
            raise OSError("simulated ELOOP")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", boom)
    assert _path_resolves_under(tmp_path / "escape", root) is False


def test_path_resolves_under_true_for_real_child(tmp_path: Path):
    root = tmp_path / "home"
    child = root / "logs" / "agent.log"
    child.parent.mkdir(parents=True)
    child.write_text("x")
    assert _path_resolves_under(child, root) is True
    assert _path_resolves_under(tmp_path / "elsewhere", root) is False


_LIVE_AVAILABILITY_SNAPSHOT = {
    "enabled_toolsets": ["bfl", "browser", "clarify", "codegraph", "file"],
    "tools": [{"type": "function", "function": {"name": "read_file"}}],
    "availability": {
        "unavailable_toolsets": [
            {"name": "bfl", "env_vars": [], "tools": ["bfl_flux3_text_to_video"]},
            {"name": "browser", "env_vars": [], "tools": ["browser_navigate"]},
            {"name": "kanban", "env_vars": [], "tools": ["kanban_show"]},
        ],
        "lazy_tools": ["bfl_flux3_get_result", "browser_back"],
        "disabled_tools": [],
    },
}


def _collect_toolsets(hermes_home: Path) -> DashboardState:
    c = Collector(hermes_home)
    try:
        return c.collect()
    finally:
        c.close()


def test_toolset_availability_reads_live_banner_snapshot_shape(hermes_home: Path):
    """availability.unavailable_toolsets is a list of {name, env_vars, tools} dicts."""
    _write_banner_snapshot(hermes_home, _LIVE_AVAILABILITY_SNAPSHOT)

    state = _collect_toolsets(hermes_home)

    availability = state.toolset_availability
    assert availability.enabled_toolsets == ["bfl", "browser", "clarify", "codegraph", "file"]
    assert availability.unavailable_toolsets == ["bfl", "browser", "kanban"]
    assert availability.lazy_tool_count == 2
    assert availability.disabled_tool_count == 0
    assert "toolset_availability" not in state.health.failed_sources


def test_toolset_availability_accepts_plain_name_lists(hermes_home: Path):
    _write_banner_snapshot(
        hermes_home,
        {
            "enabled_toolsets": ["file"],
            "availability": {
                "unavailable_toolsets": ["kanban", "browser"],
                "lazy_tools": ["a"],
                "disabled_tools": ["b", "c"],
            },
        },
    )

    availability = _collect_toolsets(hermes_home).toolset_availability

    assert availability.unavailable_toolsets == ["browser", "kanban"]
    assert availability.lazy_tool_count == 1
    assert availability.disabled_tool_count == 2


def test_toolset_availability_unknown_shapes_are_empty(hermes_home: Path):
    _write_banner_snapshot(
        hermes_home,
        {
            "enabled_toolsets": "everything",
            "availability": {
                "unavailable_toolsets": {"kanban": True},
                "lazy_tools": 7,
                "disabled_tools": None,
            },
        },
    )

    availability = _collect_toolsets(hermes_home).toolset_availability

    assert availability.enabled_toolsets == []
    assert availability.unavailable_toolsets == []
    assert availability.lazy_tool_count == 0
    assert availability.disabled_tool_count == 0


def test_toolset_availability_absent_snapshot_is_empty(hermes_home: Path):
    availability = _collect_toolsets(hermes_home).toolset_availability

    assert availability.enabled_toolsets == []
    assert availability.unavailable_toolsets == []
