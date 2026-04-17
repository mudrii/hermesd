import json
import sqlite3
import subprocess
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
        (
            "sess_001",
            "cli",
            None,
            "gpt-5.4",
            None,
            None,
            None,
            now - 3600,
            None,
            None,
            77,
            51,
            12400,
            8200,
            28300,
            5000,
            0,
            "openai-codex",
            None,
            None,
            0.42,
            None,
            "unknown",
            None,
            None,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "sess_002",
            "telegram",
            "user1",
            "gpt-5.4",
            None,
            None,
            None,
            now - 1800,
            None,
            None,
            47,
            14,
            9100,
            6300,
            15200,
            3000,
            0,
            "openai-codex",
            None,
            None,
            0.31,
            None,
            "unknown",
            None,
            None,
            None,
        ),
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                None,
                "sess_001",
                "assistant",
                f"response {i}",
                None,
                json.dumps([{"function": {"name": "shell_exec"}}]) if i % 2 == 0 else None,
                "shell_exec" if i % 2 == 0 else None,
                now - 3600 + i * 60,
                100,
                "stop",
                None,
                None,
                None,
            ),
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def sample_gateway_state(hermes_home: Path) -> Path:
    """Create a gateway_state.json."""
    path = hermes_home / "gateway_state.json"
    path.write_text(
        json.dumps(
            {
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
            }
        )
    )
    return path


@pytest.fixture
def sample_config(hermes_home: Path) -> Path:
    """Create a config.yaml."""
    import yaml

    path = hermes_home / "config.yaml"
    path.write_text(
        yaml.dump(
            {
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
                "provider_routing": {"sort": "throughput", "only": ["anthropic", "google"]},
                "smart_model_routing": {
                    "enabled": True,
                    "cheap_model": {"provider": "openrouter", "model": "google/gemini-2.5-flash"},
                },
                "fallback_model": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-20250514",
                },
                "memory": {"provider": "supermemory"},
                "session_reset": {"mode": "both"},
                "dashboard": {"theme": "midnight"},
                "plugins": {"disabled": ["disabled-plugin"]},
                "mcp_servers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["@playwright/mcp@latest"],
                        "enabled": True,
                        "tools": {"include": ["browser_navigate", "browser_screenshot"]},
                    },
                    "sheets": {
                        "url": "https://mcp.example.com/sheets",
                        "enabled": False,
                    },
                },
                "web": {"use_gateway": True},
                "image_gen": {"use_gateway": False},
                "tts": {"use_gateway": True},
                "browser": {"use_gateway": False},
                "display": {"skin": "default"},
                "_config_version": 12,
            }
        )
    )
    return path


@pytest.fixture
def sample_auth(hermes_home: Path) -> Path:
    """Create an auth.json with provider names only."""
    path = hermes_home / "auth.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": "openai-codex",
                "providers": {
                    "openai-codex": {"id_token": "REDACTED"},
                    "anthropic": {"api_key": "sk-ant-secret"},
                },
                "credential_pool": {
                    "openai-codex": {
                        "label": "Primary Codex",
                        "auth_type": "oauth",
                        "source": "codex",
                        "last_status": "ok",
                        "request_count": 42,
                        "priority": 1,
                    },
                    "anthropic": {
                        "label": "Fallback Anthropic",
                        "auth_type": "api_key",
                        "source": "env:ANTHROPIC_API_KEY",
                        "last_status": "rate_limited",
                        "request_count": 3,
                        "cooldown_remaining": "58m",
                        "priority": 2,
                        "api_key": "sk-live-secret",
                    },
                    "deepseek": {},
                    "gemini": {},
                    "kimi-coding": {},
                },
                "updated_at": "2026-04-08T00:00:00+00:00",
            }
        )
    )
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
def sample_memory_files(hermes_home: Path) -> Path:
    memories_dir = hermes_home / "memories"
    (memories_dir / "MEMORY.md").write_text("Long term memory for operator preferences.\n")
    (memories_dir / "USER.md").write_text("User profile and habits.\n")
    (hermes_home / "SOUL.md").write_text("Remember the operator's habits.\n")
    return memories_dir


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
    gw_log.write_text("2026-04-09 15:40:00,000 - gateway - INFO - Telegram connected\n")
    err_log = hermes_home / "logs" / "errors.log"
    err_log.write_text("2026-04-09 14:00:00,000 - hermes - WARNING - High context usage\n")
    return agent_log


@pytest.fixture
def sample_processes(hermes_home: Path) -> Path:
    """Create a processes.json checkpoint file for running background processes."""
    path = hermes_home / "processes.json"
    path.write_text(
        json.dumps(
            [
                {
                    "session_id": "proc_alpha",
                    "command": "pytest -q",
                    "pid": 4242,
                    "pid_scope": "host",
                    "cwd": "/tmp/project",
                    "started_at": 1775791440.0,
                    "task_id": "task-1",
                    "session_key": "telegram:123",
                    "watcher_interval": 30,
                    "notify_on_complete": True,
                    "watch_patterns": ["ERROR", "listening on port"],
                },
                {
                    "session_id": "proc_beta",
                    "command": "npm run dev",
                    "pid": 4343,
                    "pid_scope": "host",
                    "cwd": "/tmp/web",
                    "started_at": 1775791450.0,
                    "task_id": "task-2",
                    "session_key": "discord:456",
                    "watcher_interval": 0,
                    "notify_on_complete": False,
                    "watch_patterns": [],
                },
            ]
        )
    )
    return path


@pytest.fixture
def sample_hooks(hermes_home: Path) -> Path:
    hooks_dir = hermes_home / "hooks"
    startup = hooks_dir / "startup-check"
    startup.mkdir(parents=True)
    (startup / "HOOK.yaml").write_text(
        "name: startup-check\n"
        "description: Run startup validation\n"
        "events:\n"
        "  - gateway:startup\n"
        "  - agent:start\n"
    )
    (startup / "handler.py").write_text("async def handle(event_type, context):\n    return None\n")

    session = hooks_dir / "session-audit"
    session.mkdir()
    (session / "HOOK.yaml").write_text(
        "name: session-audit\ndescription: Track session boundaries\nevents:\n  - session:end\n"
    )
    (session / "handler.py").write_text("def handle(event_type, context):\n    return None\n")
    return hooks_dir


@pytest.fixture
def sample_plugins(hermes_home: Path) -> Path:
    plugins_dir = hermes_home / "plugins"

    weather = plugins_dir / "weather"
    weather.mkdir(parents=True)
    (weather / "plugin.yaml").write_text(
        "name: weather\n"
        "version: 1.2.3\n"
        "description: Weather tools and alerts\n"
        "provides_tools:\n"
        "  - forecast\n"
        "  - alerts\n"
        "provides_hooks:\n"
        "  - post_tool_call\n"
    )
    (weather / "dashboard").mkdir()
    (weather / "dashboard" / "manifest.json").write_text(
        json.dumps(
            {
                "name": "weather",
                "label": "Weather",
                "description": "Weather dashboard widgets",
                "version": "1.2.3",
                "tab": {"path": "/weather", "position": "end"},
                "entry": "dist/index.js",
            }
        )
    )

    disabled = plugins_dir / "disabled-plugin"
    disabled.mkdir()
    (disabled / "plugin.yaml").write_text(
        "name: disabled-plugin\n"
        "version: 0.4.0\n"
        "description: Disabled plugin fixture\n"
        "provides_hooks:\n"
        "  - pre_llm_call\n"
    )
    return plugins_dir


@pytest.fixture
def sample_boot_md(hermes_home: Path) -> Path:
    path = hermes_home / "BOOT.md"
    path.write_text("Check gateway health before traffic.\n")
    return path


def _init_shadow_checkpoint_repo(repo_dir: Path, workdir: Path, commits: list[str]) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "HERMES_WORKDIR").write_text(str(workdir))

    subprocess.run(
        ["git", "init", "--bare", str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(repo_dir),
            "--work-tree",
            str(workdir),
            "config",
            "user.email",
            "tests@example.com",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(repo_dir),
            "--work-tree",
            str(workdir),
            "config",
            "user.name",
            "Hermesd Tests",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    target_file = workdir / "tracked.txt"
    for idx, message in enumerate(commits, start=1):
        target_file.write_text(f"revision {idx}\n")
        subprocess.run(
            ["git", "--git-dir", str(repo_dir), "--work-tree", str(workdir), "add", "tracked.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(repo_dir),
                "--work-tree",
                str(workdir),
                "commit",
                "-m",
                message,
            ],
            check=True,
            capture_output=True,
            text=True,
        )


@pytest.fixture
def sample_checkpoints(hermes_home: Path, tmp_path: Path) -> Path:
    checkpoints_dir = hermes_home / "checkpoints"
    repo_dir = checkpoints_dir / "abc123def4567890"
    workdir = tmp_path / "workspaces" / "project-alpha"
    _init_shadow_checkpoint_repo(repo_dir, workdir, ["Before patch", "Refine config panel"])
    return checkpoints_dir


@pytest.fixture
def sample_cron_tick(hermes_home: Path) -> Path:
    """Create a cron tick lock file."""
    path = hermes_home / "cron" / ".tick.lock"
    path.write_text("")
    return path


@pytest.fixture
def populated_hermes_home(
    hermes_home,
    sample_db,
    sample_gateway_state,
    sample_config,
    sample_auth,
    sample_skills_manifest,
    sample_memory_files,
    sample_logs,
    sample_processes,
    sample_hooks,
    sample_plugins,
    sample_boot_md,
    sample_checkpoints,
    sample_cron_tick,
) -> Path:
    """A fully populated mock ~/.hermes."""
    return hermes_home


def _write_minimal_state_db(db_path: Path, session_id: str, source: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
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
            session_id TEXT NOT NULL,
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
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            source,
            None,
            "gpt-5.4",
            None,
            None,
            None,
            time.time(),
            None,
            None,
            1,
            1,
            100,
            50,
            10,
            0,
            0,
            "openai-codex",
            None,
            None,
            0.01,
            None,
            "reported",
            None,
            "v1",
            f"{source} title",
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def profiled_hermes_home(hermes_home: Path) -> Path:
    import yaml

    # Shared root files
    (hermes_home / "active_profile").write_text("coding\n")
    (hermes_home / "config.yaml").write_text(
        yaml.dump(
            {
                "model": {"default": "root-model", "provider": "root-provider"},
                "display": {"skin": "default"},
            }
        )
    )
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "active_provider": "root-provider",
                "providers": {"root-provider": {"api_key": "REDACTED"}},
                "credential_pool": {"root-provider": {}, "backup-provider": {}},
            }
        )
    )
    (hermes_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 12345,
                "gateway_state": "running",
                "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
            }
        )
    )
    (hermes_home / ".update_check").write_text(json.dumps({"behind": 2}))

    # Root-scoped runtime data
    _write_minimal_state_db(hermes_home / "state.db", "root_session", "root")
    root_sessions = hermes_home / "sessions"
    root_sessions.mkdir(exist_ok=True)
    (root_sessions / "sessions.json").write_text(
        json.dumps({"root": {"session_id": "root_session"}})
    )
    (root_sessions / "session_root_session.json").write_text(
        json.dumps({"session_id": "root_session", "tools": [{"name": "root_tool"}]})
    )
    root_logs = hermes_home / "logs"
    root_logs.mkdir(exist_ok=True)
    (root_logs / "agent.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - root agent log\n"
    )
    root_skill = hermes_home / "skills" / "dev" / "root-skill"
    root_skill.mkdir(parents=True, exist_ok=True)
    (root_skill / "SKILL.md").write_text("---\ndescription: Root skill\n---\n")
    (hermes_home / "memories" / "ROOT.md").write_text("root memory\n")

    # Profile-scoped runtime data
    profile_home = hermes_home / "profiles" / "coding"
    (profile_home / "logs").mkdir(parents=True)
    (profile_home / "sessions").mkdir()
    (profile_home / "skills").mkdir()
    (profile_home / "memories").mkdir()
    _write_minimal_state_db(profile_home / "state.db", "profile_session", "profile")
    (profile_home / "sessions" / "sessions.json").write_text(
        json.dumps({"profile": {"session_id": "profile_session"}})
    )
    (profile_home / "sessions" / "session_profile_session.json").write_text(
        json.dumps({"session_id": "profile_session", "tools": [{"name": "profile_tool"}]})
    )
    (profile_home / "logs" / "agent.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - profile agent log\n"
    )
    profile_skill = profile_home / "skills" / "dev" / "profile-skill"
    profile_skill.mkdir(parents=True)
    (profile_skill / "SKILL.md").write_text("---\ndescription: Profile skill\n---\n")
    (profile_home / "memories" / "PROFILE.md").write_text("profile memory\n")

    return hermes_home
