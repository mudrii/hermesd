import json
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    """Create a mock ~/.hermes directory with all expected files."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "logs").mkdir()
    (home / "sessions").mkdir()
    (home / "skills").mkdir()
    (home / "memories").mkdir()
    (home / "cron").mkdir()
    (home / "cron" / "output").mkdir()
    return home


@pytest.fixture
def sample_db(hermes_home: Path) -> Path:
    """Create a state.db with sample sessions and messages."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (6);

        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT
        );
    """)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sess_001", "cli", None, "gpt-5.4", None, None, None,
         now - 3600, None, None, 77, 51, 12400, 8200, 28300, 5000, 0,
         "openai-codex", None, None, 0.42, None, "unknown", None, None, None),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sess_002", "telegram", "user1", "gpt-5.4", None, None, None,
         now - 1800, None, None, 47, 14, 9100, 6300, 15200, 3000, 0,
         "openai-codex", None, None, 0.31, None, "unknown", None, None, None),
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (None, "sess_001", "assistant", f"response {i}", None,
             json.dumps([{"function": {"name": "shell_exec"}}]) if i % 2 == 0 else None,
             "shell_exec" if i % 2 == 0 else None,
             now - 3600 + i * 60, 100, "stop", None, None, None),
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def sample_gateway_state(hermes_home: Path) -> Path:
    """Create a gateway_state.json."""
    path = hermes_home / "gateway_state.json"
    path.write_text(json.dumps({
        "pid": 12345,
        "kind": "hermes-gateway",
        "argv": [],
        "start_time": None,
        "gateway_state": "running",
        "exit_reason": None,
        "platforms": {
            "telegram": {"state": "connected", "updated_at": "2026-04-08T17:42:57+00:00"},
            "discord": {"state": "disconnected", "updated_at": "2026-04-08T10:00:00+00:00"},
        },
        "updated_at": "2026-04-08T17:42:57+00:00",
    }))
    return path


@pytest.fixture
def sample_config(hermes_home: Path) -> Path:
    """Create a config.yaml."""
    import yaml
    path = hermes_home / "config.yaml"
    path.write_text(yaml.dump({
        "model": {"default": "gpt-5.4", "provider": "openai-codex"},
        "agent": {
            "max_turns": 192,
            "reasoning_effort": "medium",
            "personalities": {"kawaii": "uwu"},
            "active_personality": "kawaii",
        },
        "compression": {"threshold": 0.86},
        "security": {"redact_secrets": True},
        "approvals": {"mode": "manual"},
        "display": {"skin": "default"},
        "_config_version": 12,
    }))
    return path


@pytest.fixture
def sample_auth(hermes_home: Path) -> Path:
    """Create an auth.json with provider names only."""
    path = hermes_home / "auth.json"
    path.write_text(json.dumps({
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {"openai-codex": {"id_token": "REDACTED"}},
        "credential_pool": {
            "openai-codex": {}, "anthropic": {}, "deepseek": {},
            "gemini": {}, "kimi-coding": {},
        },
        "updated_at": "2026-04-08T00:00:00+00:00",
    }))
    return path


@pytest.fixture
def sample_skills_manifest(hermes_home: Path) -> Path:
    """Create skill directories with SKILL.md files."""
    skills_dir = hermes_home / "skills"
    for cat in ("dev", "research", "creative"):
        cat_dir = skills_dir / cat
        cat_dir.mkdir()
        for i in range(5):
            skill_dir = cat_dir / f"skill-{i}"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: skill-{i}\ndescription: A {cat} skill number {i}\n---\n"
            )
    return skills_dir


@pytest.fixture
def sample_logs(hermes_home: Path) -> Path:
    """Create sample log files."""
    agent_log = hermes_home / "logs" / "agent.log"
    agent_log.write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - Tool call: web_search\n"
        "2026-04-09 15:42:01,456 - hermes - INFO - Response generated (1.2s)\n"
        "2026-04-09 15:42:03,789 - hermes - INFO - Session saved\n"
    )
    gw_log = hermes_home / "logs" / "gateway.log"
    gw_log.write_text(
        "2026-04-09 15:40:00,000 - gateway - INFO - Telegram connected\n"
    )
    err_log = hermes_home / "logs" / "errors.log"
    err_log.write_text(
        "2026-04-09 14:00:00,000 - hermes - WARNING - High context usage\n"
    )
    return agent_log


@pytest.fixture
def sample_cron_tick(hermes_home: Path) -> Path:
    """Create a cron tick lock file."""
    path = hermes_home / "cron" / ".tick.lock"
    path.write_text("")
    return path


@pytest.fixture
def populated_hermes_home(
    hermes_home, sample_db, sample_gateway_state, sample_config,
    sample_auth, sample_skills_manifest, sample_logs, sample_cron_tick,
) -> Path:
    """A fully populated mock ~/.hermes."""
    return hermes_home
