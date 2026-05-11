import json
import os
import sqlite3
import time
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from hermesd.collector import (
    Collector,
    _coerce_float,
    _coerce_int,
    _delivery_target_label,
    _git_checkpoint_summary,
    _latest_cron_output_excerpt,
    _pid_exists,
    _redact_secret_args,
    _today_epoch,
)
from tests.conftest import create_state_db_tables


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


def test_today_epoch_is_midnight():
    import datetime

    epoch = _today_epoch()
    dt = datetime.datetime.fromtimestamp(epoch)
    assert dt.hour == 0
    assert dt.minute == 0
    assert dt.second == 0


def test_coerce_float_handles_edge_cases():
    assert _coerce_float(None) == 0.0
    assert _coerce_float("not-a-number") == 0.0
    assert _coerce_float("nan") == 0.0
    assert _coerce_float(float("inf")) == 0.0
    assert _coerce_float("-1.25") == -1.25
    assert _coerce_float(True) == 1.0


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


def test_delivery_target_label_branches():
    directory = {"platforms": {"telegram": [{"name": "Team"}]}}

    assert _delivery_target_label(directory, "") == ""
    assert _delivery_target_label(directory, "local") == "local"
    assert _delivery_target_label(directory, "origin") == "origin"
    assert _delivery_target_label(directory, "email") == "email"
    assert _delivery_target_label(directory, "telegram:Team") == "telegram:Team"
    assert _delivery_target_label(directory, "telegram:Missing") == "telegram:Missing"


def test_collect_tokens_today_filters_by_date(hermes_home: Path, sample_db: Path):
    """Only sessions started today count toward today's tokens."""
    conn = sqlite3.connect(str(sample_db))
    yesterday = time.time() - 86400 * 2
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "sess_old",
            "cli",
            None,
            "gpt-5.4",
            None,
            None,
            None,
            yesterday,
            None,
            None,
            10,
            5,
            5000,
            3000,
            1000,
            500,
            0,
            None,
            None,
            None,
            0.10,
            None,
            None,
            None,
            None,
            None,
        ),
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


def test_collect_logs_respects_log_tail_bytes(hermes_home: Path):
    agent_log = hermes_home / "logs" / "agent.log"
    lines = [f"2026-04-09 15:42:{idx:02d},000 - hermes - INFO - line {idx}\n" for idx in range(30)]
    agent_log.write_text("".join(lines))

    c = Collector(hermes_home, log_tail_bytes=256)
    state = c.collect()

    messages = [line.message for line in state.logs.agent_lines]
    assert any("line 29" in message for message in messages)
    assert all("line 00" not in message for message in messages)
    c.close()


def test_collect_cron_logs_respect_log_tail_bytes(hermes_home: Path):
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    output_file = cron_output_dir / "latest.md"
    output_file.write_text("\n".join(f"cron line {idx}" for idx in range(40)))

    c = Collector(hermes_home, log_tail_bytes=128)
    state = c.collect()

    messages = [line.message for line in state.logs.cron_lines]
    assert any("cron line 39" in message for message in messages)
    assert all("cron line 0" not in message for message in messages)
    c.close()


def test_collect_total_tool_calls(hermes_home: Path, sample_db: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.total_tool_calls == 51 + 14
    c.close()


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


def test_collect_available_tools_reuses_cached_index_when_sessions_json_is_unchanged(
    hermes_home: Path,
):
    sessions_json = hermes_home / "sessions" / "sessions.json"
    sessions_json.write_text(json.dumps({"entry1": {"session_id": "s1"}}))
    session_file = hermes_home / "sessions" / "session_s1.json"
    session_file.write_text(json.dumps({"session_id": "s1", "tools": [{"name": "terminal"}]}))

    c = Collector(hermes_home)
    original = c._read_json_cached
    session_reads = 0

    def counting_read_json(path: Path) -> dict[str, object]:
        nonlocal session_reads
        if path.name.startswith("session_"):
            session_reads += 1
        return original(path)

    c._read_json_cached = counting_read_json

    assert c._collect_available_tools() == (1, ["terminal"])
    assert c._collect_available_tools() == (1, ["terminal"])
    assert session_reads == 1
    c.close()


def test_collect_available_tools_no_sessions_json(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.available_tools == 0
    c.close()


def test_summarize_profile_uses_session_count_reader(hermes_home: Path):
    profile_home = hermes_home / "profiles" / "coding"
    profile_home.mkdir(parents=True)
    (profile_home / "state.db").touch()

    class FakeHermesDB:
        def __init__(self, db_path: Path):
            self.db_path = db_path

        def read_session_count(self) -> int:
            return 7

        def read_sessions(self) -> list[dict[str, object]]:
            raise AssertionError("read_sessions() should not be used for profile counts")

        def close(self) -> None:
            return None

    collector = Collector(hermes_home, db_factory=FakeHermesDB)
    summary = collector._summarize_profile("coding", profile_home)

    assert summary.session_count == 7
    collector.close()


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


def test_collect_config_ignores_non_mapping_yaml(hermes_home: Path):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(yaml.dump(["not", "a", "mapping"]))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.config.model == ""
    assert state.config.provider == ""
    assert state.active_skin == "default"
    c.close()


def test_collect_config_preserves_last_good_mapping_on_non_mapping_yaml(hermes_home: Path):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(yaml.dump({"model": {"default": "claude-4"}}))
    c = Collector(hermes_home)
    first = c.collect()
    assert first.config.model == "claude-4"

    cfg.write_text(yaml.dump(["not", "a", "mapping"]))
    second = c.collect()
    assert second.config.model == "claude-4"
    c.close()


def test_collect_config_personality_fallback(hermes_home: Path):
    """When active_personality is unset, pick first from personalities dict."""
    cfg = hermes_home / "config.yaml"
    cfg.write_text(
        yaml.dump(
            {
                "model": {"default": "gpt-5.4"},
                "agent": {"personalities": {"pirate": "arrr"}},
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.config.personality == "pirate"
    c.close()


def test_collect_config_tool_gateway_routes(hermes_home: Path, monkeypatch):
    import yaml

    cfg = hermes_home / "config.yaml"
    cfg.write_text(
        yaml.dump(
            {
                "model": {"default": "gpt-5.4", "provider": "openai-codex"},
                "web": {"use_gateway": True},
                "image_gen": {"use_gateway": False},
                "tts": {"use_gateway": True},
                "browser": {"use_gateway": False},
            }
        )
    )
    monkeypatch.setenv("TOOL_GATEWAY_DOMAIN", "gateway.example.com")
    monkeypatch.setenv("TOOL_GATEWAY_SCHEME", "https")
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "secret-token")
    monkeypatch.setenv("FIRECRAWL_GATEWAY_URL", "https://firecrawl.example.com")

    c = Collector(hermes_home)
    state = c.collect()

    routes = {route.tool: route for route in state.config.tool_gateway_routes}
    assert routes["web"].mode == "gateway"
    assert routes["image_gen"].mode == "direct"
    assert routes["tts"].mode == "gateway"
    assert routes["browser"].mode == "direct"
    assert all(route.token_present for route in routes.values())
    assert state.config.tool_gateway_domain == "gateway.example.com"
    assert state.config.tool_gateway_scheme == "https"
    assert state.config.firecrawl_gateway_url == "https://firecrawl.example.com"
    c.close()


def test_collect_config_redacts_secret_bearing_tool_gateway_urls(hermes_home: Path, monkeypatch):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(yaml.dump({"web": {"use_gateway": True}}))
    monkeypatch.setenv("FIRECRAWL_GATEWAY_URL", "https://firecrawl.example.com?token=secret")

    c = Collector(hermes_home)
    state = c.collect()

    assert state.config.firecrawl_gateway_url == "https://firecrawl.example.com?token=[REDACTED]"
    c.close()


def test_collect_hermes_version_ignores_non_version_keys(hermes_home: Path):
    pyproject = hermes_home / "hermes-agent" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True, exist_ok=True)
    pyproject.write_text(
        """
[tool.example]
versioning_scheme = "calendar"

[project]
name = "hermes-agent"
version = "2026.4.10"
"""
    )
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": 0, "gateway_state": "stopped", "platforms": {}})
    )

    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.hermes_version == "2026.4.10"
    c.close()


def test_collect_config_richer_agent_settings(populated_hermes_home: Path):
    c = Collector(populated_hermes_home)
    state = c.collect()
    assert state.config.provider_routing_summary == "throughput only:2"
    assert state.config.smart_model_routing_enabled is True
    assert state.config.smart_model_routing_cheap_model == "openrouter/google/gemini-2.5-flash"
    assert state.config.fallback_model_label == "anthropic/claude-sonnet-4-20250514"
    assert state.config.dashboard_theme == "midnight"
    assert state.config.session_reset_mode == "both"
    assert state.config.memory_provider == "supermemory"
    c.close()


def test_collect_mcp_servers_redacts_secret_targets(hermes_home: Path):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(
        yaml.dump(
            {
                "mcp_servers": {
                    "local-demo": {
                        "command": "python --token command-secret",
                        "args": ["server.py", "--api-key", "sk-test-secret"],
                        "env": {"AUTHORIZATION": "Bearer env-secret"},
                    },
                    "remote-demo": {
                        "url": "https://example.com/mcp?token=secret123&mode=full",
                    },
                }
            }
        )
    )

    c = Collector(hermes_home)
    state = c.collect()

    servers = {server.name: server for server in state.skills_memory.mcp_servers}
    assert "command-secret" not in servers["local-demo"].target
    assert "sk-test-secret" not in servers["local-demo"].target
    assert "env-secret" not in servers["local-demo"].target
    assert servers["local-demo"].target == (
        "env:[REDACTED] python --token [REDACTED] server.py --api-key [REDACTED]"
    )
    assert servers["remote-demo"].target == "https://example.com/mcp?token=[REDACTED]&mode=full"
    c.close()


def test_collect_gateway_preserves_last_good_mapping_on_non_mapping_json(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 12345,
                "gateway_state": "running",
                "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
            }
        )
    )

    c = Collector(hermes_home, pid_exists=lambda pid: pid == 12345)
    first = c.collect()
    assert first.gateway.running is True
    assert first.gateway.pid == 12345

    gw.write_text(json.dumps(["not", "a", "mapping"]))
    second = c.collect()
    assert second.gateway.running is True
    assert second.gateway.pid == 12345
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
    assert line.component == "hermes"
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


def test_collect_logs_preserves_cache_when_file_disappears(hermes_home: Path):
    log = hermes_home / "logs" / "agent.log"
    log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - first line\n")
    c = Collector(hermes_home)
    first = c.collect()
    log.unlink()
    second = c.collect()
    assert second.logs.agent_lines == first.logs.agent_lines
    c.close()


def test_collect_logs_preserves_cache_when_file_rotates_to_empty(hermes_home: Path):
    log = hermes_home / "logs" / "agent.log"
    log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - first line\n")
    c = Collector(hermes_home)
    first = c.collect()
    log.write_text("")
    second = c.collect()
    assert second.logs.agent_lines == first.logs.agent_lines
    c.close()


def test_collect_cron_logs_preserve_cache_when_latest_output_disappears(hermes_home: Path):
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    output_file = cron_output_dir / "latest.md"
    output_file.write_text("cron line 1\ncron line 2\n")

    c = Collector(hermes_home)
    first = c.collect()
    output_file.unlink()
    second = c.collect()

    assert second.logs.cron_lines == first.logs.cron_lines
    c.close()


def test_latest_cron_output_excerpt_ignores_file_that_disappears_during_stat(
    hermes_home: Path, monkeypatch
):
    output_dir = hermes_home / "cron" / "output" / "job-1"
    output_dir.mkdir(parents=True)
    stale_file = output_dir / "stale.md"
    stale_file.write_text("stale output\n")
    latest_file = output_dir / "latest.md"
    latest_file.write_text("fresh output\n")
    original_stat = Path.stat

    def flaky_stat(path: Path, *args, **kwargs):
        if path == stale_file:
            raise FileNotFoundError
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    excerpt, silent = _latest_cron_output_excerpt(hermes_home / "cron" / "output", "job-1")

    assert excerpt == "fresh output"
    assert silent is False


def test_git_checkpoint_summary_sets_timeouts(monkeypatch):
    timeouts: list[int] = []

    def fake_run(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        if "rev-list" in args[0]:
            return CompletedProcess(args[0], 0, stdout="3\n")
        return CompletedProcess(args[0], 0, stdout="1712345678\tcheckpoint reason\n")

    monkeypatch.setattr("hermesd.collector.subprocess.run", fake_run)

    commit_count, timestamp, reason = _git_checkpoint_summary(Path("/tmp/repo.git"))

    assert timeouts == [2, 2]
    assert commit_count == 3
    assert timestamp == 1712345678.0
    assert reason == "checkpoint reason"


def test_git_checkpoint_summary_empty_repo_skips_log(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return CompletedProcess(args[0], 0, stdout="0\n")

    monkeypatch.setattr("hermesd.collector.subprocess.run", fake_run)

    assert _git_checkpoint_summary(Path("/tmp/repo.git")) == (0, None, "")
    assert len(calls) == 1
    assert "rev-list" in calls[0]


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


def test_collect_skin_unknown_falls_back_to_default(hermes_home: Path):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(yaml.dump({"display": {"skin": "nonexistent"}}))
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


def test_json_cache_returns_empty_mapping_on_missing_file(hermes_home: Path):
    c = Collector(hermes_home)
    data = c._read_json_cached(hermes_home / "nonexistent.json")
    assert data == {}
    c.close()


def test_gateway_pid_not_running(hermes_home: Path):
    """Gateway state says running but PID doesn't exist and no gateway.pid."""
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "gateway_state": "running",
                "platforms": {},
                "updated_at": "",
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is False
    assert state.gateway.state == "running"
    c.close()


def test_gateway_invalid_pid_type_does_not_make_state_stale(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": "not-a-pid",
                "gateway_state": "running",
                "platforms": {},
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.is_stale is False
    assert state.gateway.running is False
    assert state.gateway.pid == 0
    c.close()


def test_coerce_int_handles_bool_values():
    assert _coerce_int(True) == 1
    assert _coerce_int(False) == 0


def test_coerce_int_handles_bytes_and_bytearray():
    assert _coerce_int(b"42") == 42
    assert _coerce_int(bytearray(b"7")) == 7
    assert _coerce_int(b"not-a-number") == 0


def test_gateway_ignores_non_mapping_platform_entries(hermes_home: Path):
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 0,
                "gateway_state": "stopped",
                "platforms": {
                    "telegram": {"state": "connected", "updated_at": "2026-04-08T17:42:57+00:00"},
                    "broken": ["not", "a", "mapping"],
                },
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.is_stale is False
    assert len(state.gateway.platforms) == 1
    assert state.gateway.platforms[0].name == "telegram"
    c.close()


def test_gateway_stale_pid_with_launchd_pid(hermes_home: Path):
    """gateway_state.json has stale PID but gateway.pid has live PID."""
    import os

    my_pid = os.getpid()  # use our own PID as a "live" process
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 999999999,
                "gateway_state": "running",
                "platforms": {"telegram": {"state": "connected", "updated_at": ""}},
                "updated_at": "",
            }
        )
    )
    pid_file = hermes_home / "gateway.pid"
    pid_file.write_text(json.dumps({"pid": my_pid, "kind": "hermes-gateway"}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == my_pid
    c.close()


def test_pid_exists_returns_false_for_missing_process(monkeypatch):
    def fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", fake_kill)
    assert _pid_exists(12345) is False


def test_pid_exists_returns_true_for_permission_error(monkeypatch):
    def fake_kill(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", fake_kill)
    assert _pid_exists(12345) is True


def test_read_skill_description_parses_yaml_frontmatter(hermes_home: Path):
    skill_md = hermes_home / "skills" / "dev" / "lint" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(
        """---
description: |
  Use: lint tools
  Keep style clean
---

Body
"""
    )

    c = Collector(hermes_home)
    assert c._read_skill_description("dev", "lint") == "Use: lint tools\nKeep style clean"
    c.close()


def test_collect_providers_ignores_non_mapping_auth_json(hermes_home: Path):
    auth = hermes_home / "auth.json"
    auth.write_text(json.dumps(["not", "a", "mapping"]))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.skills_memory.providers == []
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
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn)
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
