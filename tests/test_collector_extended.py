import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import yaml

from hermesd.collector import Collector, _today_epoch


def test_today_epoch_is_midnight():
    import datetime
    epoch = _today_epoch()
    dt = datetime.datetime.fromtimestamp(epoch)
    assert dt.hour == 0
    assert dt.minute == 0
    assert dt.second == 0


def test_collect_tokens_today_filters_by_date(hermes_home: Path, sample_db: Path):
    """Only sessions started today count toward today's tokens."""
    conn = sqlite3.connect(str(sample_db))
    yesterday = time.time() - 86400 * 2
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sess_old", "cli", None, "gpt-5.4", None, None, None,
         yesterday, None, None, 10, 5, 5000, 3000, 1000, 500, 0,
         None, None, None, 0.10, None, None, None, None, None),
    )
    conn.commit()
    conn.close()
    c = Collector(hermes_home)
    state = c.collect()
    assert state.tokens_today.input_tokens < state.tokens_total.input_tokens
    c.close()


def test_collect_tool_stats(hermes_home: Path, sample_db: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert isinstance(state.tool_stats, list)
    names = [t.name for t in state.tool_stats]
    assert "shell_exec" in names
    c.close()


def test_collect_total_tool_calls(hermes_home: Path, sample_db: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.total_tool_calls == 51 + 14
    c.close()


def test_collect_available_tools_from_session_json(hermes_home: Path):
    sessions_json = hermes_home / "sessions" / "sessions.json"
    sessions_json.write_text(json.dumps({
        "entry1": {"session_id": "s1"},
    }))
    session_file = hermes_home / "sessions" / "session_s1.json"
    session_file.write_text(json.dumps({
        "session_id": "s1",
        "tools": [{"name": "terminal"}, {"name": "web_search"}, {"name": "read_file"}],
    }))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.available_tools == 3
    c.close()


def test_collect_available_tools_no_sessions_json(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.available_tools == 0
    c.close()


def test_collect_config_empty(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.config.model == ""
    assert state.config.provider == ""
    c.close()


def test_collect_config_partial(hermes_home: Path):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(yaml.dump({"model": {"default": "claude-4"}}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.config.model == "claude-4"
    assert state.config.provider == ""
    c.close()


def test_collect_config_personality_fallback(hermes_home: Path):
    """When active_personality is unset, pick first from personalities dict."""
    cfg = hermes_home / "config.yaml"
    cfg.write_text(yaml.dump({
        "model": {"default": "gpt-5.4"},
        "agent": {"personalities": {"pirate": "arrr"}},
    }))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.config.personality == "pirate"
    c.close()


def test_collect_skills_no_manifest(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.skills_memory.skill_count == 0
    assert state.skills_memory.skill_categories == 0
    c.close()


def test_collect_memory_files(hermes_home: Path):
    (hermes_home / "memories" / "MEMORY.md").write_text("test")
    (hermes_home / "memories" / "USER.md").write_text("test")
    c = Collector(hermes_home)
    state = c.collect()
    assert state.skills_memory.memory_file_count == 2
    c.close()


def test_collect_logs_empty(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.logs.agent_lines == []
    assert state.logs.gateway_lines == []
    assert state.logs.error_lines == []
    c.close()


def test_collect_logs_parses_format(hermes_home: Path, sample_logs: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert len(state.logs.agent_lines) == 3
    line = state.logs.agent_lines[0]
    assert line.level == "INFO"
    assert "web_search" in line.message
    c.close()


def test_collect_logs_non_standard_line(hermes_home: Path):
    log = hermes_home / "logs" / "agent.log"
    log.write_text("plain text without timestamp\n")
    c = Collector(hermes_home)
    state = c.collect()
    assert len(state.logs.agent_lines) == 1
    assert state.logs.agent_lines[0].message == "plain text without timestamp"
    assert state.logs.agent_lines[0].level == ""
    c.close()


def test_collect_version_behind(hermes_home: Path):
    (hermes_home / ".update_check").write_text(json.dumps({"behind": 7}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.version_behind == 7
    c.close()


def test_collect_version_behind_missing(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.version_behind == 0
    c.close()


def test_collect_skin(hermes_home: Path):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(yaml.dump({"display": {"skin": "ares"}}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.active_skin == "ares"
    c.close()


def test_collect_skin_default(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.active_skin == "default"
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


def test_json_cache_returns_none_on_missing_file(hermes_home: Path):
    c = Collector(hermes_home)
    data = c._read_json_cached(hermes_home / "nonexistent.json")
    assert data is None
    c.close()


def test_gateway_pid_not_running(hermes_home: Path):
    """Gateway state says running but PID doesn't exist and no gateway.pid."""
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": 999999999, "gateway_state": "running",
        "platforms": {}, "updated_at": "",
    }))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    assert state.gateway.state == "running"
    c.close()


def test_gateway_stale_pid_with_launchd_pid(hermes_home: Path):
    """gateway_state.json has stale PID but gateway.pid has live PID."""
    import os
    my_pid = os.getpid()  # use our own PID as a "live" process
    gw = hermes_home / "gateway_state.json"
    gw.write_text(json.dumps({
        "pid": 999999999, "gateway_state": "running",
        "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
        "updated_at": "",
    }))
    pid_file = hermes_home / "gateway.pid"
    pid_file.write_text(json.dumps({"pid": my_pid, "kind": "hermes-gateway"}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == my_pid
    c.close()


def test_session_active_detection(hermes_home: Path, sample_db: Path):
    """Sessions with ended_at=NULL should be marked active."""
    c = Collector(hermes_home)
    state = c.collect()
    # Both sample sessions have ended_at=NULL
    assert all(s.is_active for s in state.sessions)
    c.close()


def test_session_ended_detection(hermes_home: Path):
    """Sessions with ended_at set should not be marked active."""
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
            session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL,
            token_count INTEGER, finish_reason TEXT, reasoning TEXT,
            reasoning_details TEXT, codex_reasoning_items TEXT
        );
    """)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, ?)",
        ("sess_ended", "cli", now - 3600, now - 1800),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, NULL)",
        ("sess_active", "cli", now - 600),
    )
    conn.commit()
    conn.close()
    c = Collector(hermes_home)
    state = c.collect()
    by_id = {s.session_id: s for s in state.sessions}
    assert by_id["sess_ended"].is_active is False
    assert by_id["sess_active"].is_active is True
    c.close()
