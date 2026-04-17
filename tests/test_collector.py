import json
import os
import time
from pathlib import Path

from hermesd.collector import Collector
from hermesd.models import DashboardState


def test_collect_full(populated_hermes_home: Path):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()
    assert isinstance(state, DashboardState)
    assert state.health.total_sources == 18
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
    assert state.skills_memory.skill_count == 15
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
    import sqlite3

    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (6);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT,
            model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
            started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
            message_count INTEGER, tool_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, billing_provider TEXT,
            billing_base_url TEXT, billing_mode TEXT,
            estimated_cost_usd REAL, actual_cost_usd REAL,
            cost_status TEXT, cost_source TEXT, pricing_version TEXT,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL,
            token_count INTEGER, finish_reason TEXT, reasoning TEXT,
            reasoning_details TEXT, codex_reasoning_items TEXT
        );
    """)
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
    assert s.message_count == 0
    assert s.input_tokens == 0
    assert s.estimated_cost_usd == 0.0
    c.close()


def test_collect_mtime_cache(populated_hermes_home: Path):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    s1 = c.collect()
    s2 = c.collect()
    assert s1.gateway.pid == s2.gateway.pid
    c.close()


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

    def boom() -> list:
        raise RuntimeError("tool stats unavailable")

    monkeypatch.setattr(c, "_collect_tool_stats", boom)
    state2 = c.collect()

    assert state2.tool_stats == state1.tool_stats
    assert "tool_stats" in state2.health.failed_sources
    assert state2.health.ok_sources == state2.health.total_sources - 1
    c.close()
