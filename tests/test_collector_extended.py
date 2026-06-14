from __future__ import annotations

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
    _has_secret_material,
    _latest_cron_output_excerpt,
    _pid_exists,
    _read_kanban_state,
    _redact_command_string,
    _redact_secret_args,
    _today_epoch,
)
from hermesd.models import KanbanState
from tests.conftest import create_kanban_db_tables, create_state_db_tables


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


def test_delivery_target_label_branches():
    directory = {"platforms": {"telegram": [{"name": "Team"}]}}

    assert _delivery_target_label(directory, "") == ""
    assert _delivery_target_label(directory, "local") == "local"
    assert _delivery_target_label(directory, "origin") == "origin"
    assert _delivery_target_label(directory, "email") == "email"
    assert _delivery_target_label(directory, "telegram:Team") == "telegram:Team"
    assert _delivery_target_label(directory, "telegram:Missing") == "telegram:Missing"


def test_collect_tokens_today_filters_by_date(
    hermes_home: Path, sample_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """Only sessions started today count toward today's tokens."""
    # Pin the "today" cutoff to two hours ago so the assertion is deterministic
    # regardless of wall-clock time: sample_db's sessions (≤1h old) count toward
    # today, sess_old (2 days old) does not — with no midnight-boundary flake.
    monkeypatch.setattr("hermesd.collector._today_epoch", lambda: time.time() - 7200)
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
    # sample_db: sess_001 (12_400 in) + sess_002 (9_100 in) started today.
    # sess_old (5_000 in) started two days ago, so it counts toward the total
    # but not toward today.
    assert state.tokens_today.input_tokens == 21_500
    assert state.tokens_total.input_tokens == 26_500
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


def test_collect_cron_logs_skip_non_dir_and_non_file_entries(hermes_home: Path):
    output_root = hermes_home / "cron" / "output"
    # A stray file sitting directly in the output root (not a job directory).
    (output_root / "stray.txt").write_text("not a job dir")
    job_dir = output_root / "job-1"
    job_dir.mkdir()
    # A nested directory inside a job dir (not an output file).
    (job_dir / "nested").mkdir()
    output_file = job_dir / "latest.md"
    output_file.write_text("real cron output line\n")

    c = Collector(hermes_home)
    state = c.collect()

    messages = [line.message for line in state.logs.cron_lines]
    assert any("real cron output line" in message for message in messages)
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


def test_collect_hermes_version_tolerates_malformed_pyproject(hermes_home: Path):
    pyproject = hermes_home / "hermes-agent" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True, exist_ok=True)
    pyproject.write_text("[project\nversion = broken")
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": 0, "gateway_state": "stopped", "platforms": {}})
    )

    c = Collector(hermes_home)
    state = c.collect()
    assert "gateway" not in state.health.failed_sources
    assert state.gateway.hermes_version == ""
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


def _insert_session_with_endpoint(db_path: Path, model: str, base_url: str) -> None:
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, model, billing_base_url) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ctx_sess", "cli", time.time(), model, base_url),
    )
    conn.commit()
    conn.close()


def test_collect_session_context_limit_joins_on_model_and_base_url(hermes_home: Path):
    """SessionInfo.context_limit joins model@billing_base_url against the cache."""
    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/v1"
    )
    (hermes_home / "context_length_cache.yaml").write_text(
        "context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 1048576\n"
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].context_limit == 1048576
    c.close()


def test_collect_session_context_limit_normalizes_trailing_slash(hermes_home: Path):
    """A trailing slash on the session base_url still matches the cache key."""
    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/v1/"
    )
    (hermes_home / "context_length_cache.yaml").write_text(
        "context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 1048576\n"
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].context_limit == 1048576
    c.close()


def test_collect_session_context_limit_missing_cache_is_zero(hermes_home: Path):
    _insert_session_with_endpoint(
        hermes_home / "state.db", "MiniMax-M3", "https://api.minimax.io/v1"
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].context_limit == 0
    assert "sessions" not in state.health.failed_sources
    c.close()


def test_collect_response_store_counts(hermes_home: Path):
    """response_store.db conversation/response counts surface in Operations."""
    db_path = hermes_home / "response_store.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY);"
        "CREATE TABLE responses (id TEXT PRIMARY KEY);"
        "INSERT INTO conversations VALUES ('c1'), ('c2');"
        "INSERT INTO responses VALUES ('r1'), ('r2'), ('r3');"
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    ops = state.operations
    assert ops.response_store_present is True
    assert ops.conversation_count == 2
    assert ops.response_count == 3
    assert ops.response_store_size_bytes > 0
    assert "operations" not in state.health.failed_sources
    c.close()


def test_collect_response_store_absent_is_zero(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.operations.response_store_present is False
    assert state.operations.conversation_count == 0
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


def test_collect_ignores_stray_entries_in_scanned_directories(populated_hermes_home: Path):
    """Stray files / incomplete dirs in skills, hooks, plugins, and checkpoints are skipped."""
    home = populated_hermes_home
    # skills: a stray file at category level, a dot-dir, and a file inside a category
    (home / "skills" / "README.md").write_text("not a category")
    (home / "skills" / ".hidden").mkdir()
    (home / "skills" / "dev" / "notes.txt").write_text("not a skill dir")
    # hooks: a stray file and a hook dir without handler.py
    (home / "hooks" / "stray.txt").write_text("not a hook")
    no_handler = home / "hooks" / "no-handler"
    no_handler.mkdir()
    (no_handler / "HOOK.yaml").write_text("name: no-handler\nevents: not-a-list\n")
    # plugins: a stray file and a plugin dir without plugin.yaml
    (home / "plugins" / "stray.txt").write_text("not a plugin")
    (home / "plugins" / "no-manifest").mkdir()
    # checkpoints: a stray file
    (home / "checkpoints" / "stray.txt").write_text("not a repo")

    c = Collector(home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()

    assert state.health.failed_sources == []
    assert state.skills_memory.skill_count == 15
    assert len(state.skills_memory.hooks) == 2
    assert {p.name for p in state.skills_memory.plugins} == {"weather", "disabled-plugin"}
    assert [cp.repo_id for cp in state.checkpoints] == ["abc123def4567890"]
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


def test_collect_hook_with_non_list_events_gets_empty_events(hermes_home: Path):
    hook_dir = hermes_home / "hooks" / "odd-events"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("name: odd-events\nevents: not-a-list\n")
    (hook_dir / "handler.py").write_text("def handle(event_type, context):\n    return None\n")

    c = Collector(hermes_home)
    state = c.collect()
    assert len(state.skills_memory.hooks) == 1
    assert state.skills_memory.hooks[0].events == []
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


def test_collect_logs_discovers_shared_named_streams(hermes_home: Path):
    shared_logs = {
        "desktop.log": "desktop ready",
        "dashboard.log": "dashboard ready",
        "gui.log": "gui ready",
        "update.log": "update ready",
        "gateway.error.log": "gateway error ready",
        "tui_gateway_crash.log": "crash ready",
    }
    for filename, message in shared_logs.items():
        (hermes_home / "logs" / filename).write_text(
            f"2026-04-09 15:41:58,123 - hermes - INFO - {message}\n"
        )

    c = Collector(hermes_home)
    state = c.collect()

    streams = {stream.name: stream for stream in state.logs.streams}
    assert set(streams) == {
        "desktop",
        "dashboard",
        "gui",
        "update",
        "gateway.error",
        "tui crash",
    }
    assert streams["desktop"].lines[0].message == "desktop ready"
    assert streams["tui crash"].lines[0].message == "crash ready"
    c.close()


def test_profiled_collector_uses_shared_aux_log_streams(profiled_hermes_home: Path):
    (profiled_hermes_home / "logs" / "desktop.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - shared desktop log\n"
    )

    c = Collector(profiled_hermes_home, profile_name="coding")
    state = c.collect()

    streams = {stream.name: stream for stream in state.logs.streams}
    assert streams["agent"].lines[0].message == "profile agent log"
    assert streams["desktop"].lines[0].message == "shared desktop log"
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


def test_collect_logs_ignores_symlinked_log_files_outside_home(hermes_home: Path, tmp_path: Path):
    outside_log = tmp_path / "outside-secret.log"
    outside_log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - outside secret\n")
    (hermes_home / "logs" / "agent.log").symlink_to(outside_log)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.logs.agent_lines == []
    assert all(
        "outside secret" not in line.message
        for stream in state.logs.streams
        for line in stream.lines
    )
    c.close()


def test_collect_logs_ignores_symlinked_log_directory_outside_home(
    hermes_home: Path, tmp_path: Path
):
    outside_logs = tmp_path / "outside-logs"
    outside_logs.mkdir()
    (outside_logs / "agent.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - outside secret\n"
    )
    logs_dir = hermes_home / "logs"
    logs_dir.rmdir()
    logs_dir.symlink_to(outside_logs, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.logs.agent_lines == []
    assert all(
        "outside secret" not in line.message
        for stream in state.logs.streams
        for line in stream.lines
    )
    c.close()


def test_collect_logs_and_cron_allow_symlinked_hermes_home(hermes_home: Path, tmp_path: Path):
    (hermes_home / "logs" / "agent.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - in-home log\n"
    )
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "Job 1"}]})
    )
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    (cron_output_dir / "latest.md").write_text("in-home cron output\n")
    linked_home = tmp_path / "linked-hermes"
    linked_home.symlink_to(hermes_home, target_is_directory=True)

    c = Collector(linked_home)
    state = c.collect()

    assert state.logs.agent_lines[0].message == "in-home log"
    assert state.logs.cron_lines[0].message == "in-home cron output"
    assert state.cron.jobs[0].latest_output_excerpt == "in-home cron output"
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


def test_collect_cron_logs_ignores_symlinked_output_files_outside_home(
    hermes_home: Path, tmp_path: Path
):
    outside_output = tmp_path / "outside-cron.md"
    outside_output.write_text("outside cron secret\n")
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    (cron_output_dir / "latest.md").symlink_to(outside_output)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.logs.cron_lines == []
    c.close()


def test_collect_cron_logs_ignores_symlinked_output_directory_outside_home(
    hermes_home: Path, tmp_path: Path
):
    outside_output = tmp_path / "outside-output" / "job-1"
    outside_output.mkdir(parents=True)
    (outside_output / "latest.md").write_text("outside cron secret\n")
    output_root = hermes_home / "cron" / "output"
    output_root.rmdir()
    output_root.symlink_to(outside_output.parent, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.logs.cron_lines == []
    c.close()


def test_collect_cron_job_excerpt_ignores_symlinked_output_directory_outside_home(
    hermes_home: Path, tmp_path: Path
):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "Job 1"}]})
    )
    outside_output = tmp_path / "outside-output"
    outside_output.mkdir()
    (outside_output / "latest.md").write_text("outside cron secret\n")
    job_output_dir = hermes_home / "cron" / "output" / "job-1"
    job_output_dir.symlink_to(outside_output, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.cron.jobs[0].latest_output_excerpt == ""
    assert state.cron.jobs[0].latest_output_path == ""
    assert state.cron.jobs[0].latest_output_mtime is None
    c.close()


def test_collect_cron_job_excerpt_rejects_traversal_job_id(hermes_home: Path, tmp_path: Path):
    outside_job = tmp_path / "outside-job"
    outside_job.mkdir()
    (outside_job / "latest.md").write_text("outside cron secret\n")
    traversal_id = os.path.relpath(outside_job, hermes_home / "cron" / "output")
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": traversal_id, "name": "Job 1"}]})
    )

    c = Collector(hermes_home)
    state = c.collect()

    assert state.cron.jobs[0].latest_output_excerpt == ""
    assert state.cron.jobs[0].latest_output_path == ""
    assert state.cron.jobs[0].latest_output_mtime is None
    c.close()


def test_collect_cron_ignores_non_dict_job_entries(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    "not-a-dict",
                    {"id": "job-real", "name": "Real Job", "state": "scheduled"},
                ]
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert "cron" not in state.health.failed_sources
    assert state.cron.job_count == 1
    assert state.cron.jobs[0].job_id == "job-real"
    c.close()


def test_latest_cron_output_excerpt_all_silent_lines(hermes_home: Path):
    """An all-[SILENT] output yields silent_run=True and no excerpt."""
    output_dir = hermes_home / "cron" / "output" / "job-1"
    output_dir.mkdir(parents=True)
    (output_dir / "latest.md").write_text("[SILENT]\n[silent]\n")

    excerpt, silent, output_path, output_mtime = _latest_cron_output_excerpt(
        hermes_home / "cron" / "output", "job-1", max_bytes=32768
    )

    assert excerpt == ""
    assert silent is True
    assert output_path == "latest.md"
    assert output_mtime is not None


def test_latest_cron_output_excerpt_empty_job_id_and_empty_dir(hermes_home: Path):
    empty = ("", False, "", None)
    assert _latest_cron_output_excerpt(hermes_home / "cron" / "output", "", 1024) == empty
    (hermes_home / "cron" / "output" / "job-empty").mkdir(parents=True)
    assert _latest_cron_output_excerpt(hermes_home / "cron" / "output", "job-empty", 1024) == empty


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

    excerpt, silent, output_path, output_mtime = _latest_cron_output_excerpt(
        hermes_home / "cron" / "output", "job-1", max_bytes=32768
    )

    assert excerpt == "fresh output"
    assert silent is False
    assert output_path == "latest.md"
    assert output_mtime is not None


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


def test_collect_skin_empty_value_falls_back_to_default(hermes_home: Path):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(yaml.dump({"display": {"skin": ""}}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.active_skin == "default"
    c.close()


@pytest.mark.parametrize(
    ("provider_routing", "expected"),
    [
        ({"sort": "price", "ignore": ["a", "b", "c"]}, "price ignore:3"),
        ({"order": ["a", "b"]}, "order:2"),
    ],
)
def test_collect_config_provider_routing_ignore_and_order(
    hermes_home: Path,
    provider_routing: dict[str, object],
    expected: str,
):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(yaml.dump({"provider_routing": provider_routing}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.config.provider_routing_summary == expected
    c.close()


def test_collect_mcp_servers_exclude_tool_filter_and_non_dict_entries(hermes_home: Path):
    cfg = hermes_home / "config.yaml"
    cfg.write_text(
        yaml.dump(
            {
                "mcp_servers": {
                    "filtered": {
                        "url": "https://example.com/mcp",
                        "tools": {"exclude": ["a", "b"]},
                    },
                    "broken": "not-a-mapping",
                }
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    servers = {server.name: server for server in state.skills_memory.mcp_servers}
    assert list(servers) == ["filtered"]
    assert servers["filtered"].tool_filter == "exclude:2"
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


def test_coerce_int_truncates_float_values():
    assert _coerce_int(3.9) == 3
    assert _coerce_int(-2.5) == -2


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


def test_gateway_surfaces_platform_errors_and_agent_counts(hermes_home: Path):
    """Live gateway_state.json carries per-platform error_message/error_code and
    top-level active_agents/restart_requested; hermesd surfaces them."""
    gw = hermes_home / "gateway_state.json"
    gw.write_text(
        json.dumps(
            {
                "pid": 0,
                "gateway_state": "stopped",
                "active_agents": 3,
                "restart_requested": True,
                "platforms": {
                    "discord": {
                        "state": "disconnected",
                        "updated_at": "2026-04-08T10:00:00+00:00",
                        "error_code": "reconnect_failed",
                        "error_message": "failed to reconnect",
                    },
                    "telegram": {
                        "state": "connected",
                        "updated_at": "2026-04-08T17:42:57+00:00",
                    },
                },
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    by_name = {p.name: p for p in state.gateway.platforms}
    assert by_name["discord"].error_message == "failed to reconnect"
    assert by_name["discord"].error_code == "reconnect_failed"
    assert by_name["telegram"].error_message == ""
    assert state.gateway.active_agents == 3
    assert state.gateway.restart_requested is True
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


def test_gateway_running_without_recorded_pid_uses_launchd_pid(hermes_home: Path):
    """gateway_state.json says running with no PID; gateway.pid supplies the live one."""
    my_pid = os.getpid()
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"gateway_state": "running", "platforms": {}})
    )
    (hermes_home / "gateway.pid").write_text(json.dumps({"pid": my_pid}))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.gateway.running is True
    assert state.gateway.pid == my_pid
    c.close()


def test_gateway_pid_file_with_plain_integer_content(hermes_home: Path):
    """gateway.pid may contain a bare integer instead of a JSON object."""
    my_pid = os.getpid()
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": 999999999, "gateway_state": "running", "platforms": {}})
    )
    (hermes_home / "gateway.pid").write_text(str(my_pid))
    c = Collector(hermes_home, pid_exists=lambda pid: pid != 999999999)
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


@pytest.mark.parametrize(
    ("skill_md_content", "reason"),
    [
        (None, "missing SKILL.md"),
        ("---\nname: lint\n---\nBody\n", "frontmatter without description"),
        ("---\ndescription: [unclosed\n---\nBody\n", "malformed YAML frontmatter"),
        ("No frontmatter at all\n", "no frontmatter delimiter"),
    ],
)
def test_skill_without_usable_frontmatter_gets_empty_description(
    hermes_home: Path,
    skill_md_content: str | None,
    reason: str,
):
    skill_dir = hermes_home / "skills" / "dev" / "lint"
    skill_dir.mkdir(parents=True, exist_ok=True)
    if skill_md_content is not None:
        (skill_dir / "SKILL.md").write_text(skill_md_content)

    c = Collector(hermes_home)
    state = c.collect()
    assert "skills" not in state.health.failed_sources
    assert state.skills_memory.skill_count == 1, reason
    assert state.skills_memory.skills[0].description == "", reason
    c.close()


def test_collect_credential_pool_infers_oauth_auth_type_from_token_fields(hermes_home: Path):
    """A pool entry without auth_type infers 'oauth' from OAuth token fields."""
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "active_provider": "codex",
                "providers": {"codex": {"id_token": "REDACTED"}},
                "credential_pool": {"codex": {"label": "Codex"}},
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    pools = {pool.name: pool for pool in state.skills_memory.credential_pools}
    assert pools["codex"].auth_type == "oauth"
    c.close()


def test_collect_credential_pool_accepts_list_shaped_entries(hermes_home: Path):
    """Live auth.json stores credential_pool as provider -> [entries]; surface the entry fields.

    hermesd used to feed the list to _as_dict (-> {}), blanking every credential
    field. The lowest-priority entry (next credential to be used) represents the
    provider.
    """
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "active_provider": "openai-codex",
                "providers": {"openai-codex": {"id_token": "REDACTED"}},
                "credential_pool": {
                    "openai-codex": [
                        {
                            "label": "Secondary Codex",
                            "auth_type": "oauth",
                            "source": "codex",
                            "last_status": "rate_limited",
                            "request_count": 42,
                            "priority": 2,
                            "id": "cred-b",
                        },
                        {
                            "label": "Primary Codex",
                            "auth_type": "oauth",
                            "source": "codex",
                            "last_status": "ok",
                            "request_count": 7,
                            "priority": 1,
                            "id": "cred-a",
                        },
                    ]
                },
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    pools = {pool.name: pool for pool in state.skills_memory.credential_pools}
    assert "openai-codex" in pools
    entry = pools["openai-codex"]
    assert entry.label == "Primary Codex"
    assert entry.last_status == "ok"
    assert entry.request_count == 7
    assert entry.priority == 1
    assert entry.auth_type == "oauth"
    assert entry.source == "codex"
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
    # Both sample sessions have ended_at=NULL. Assert the count first so the
    # all() below can't pass vacuously on an empty session list.
    assert len(state.sessions) == 2
    assert all(s.is_active for s in state.sessions)
    c.close()


def test_latest_cron_output_excerpt_caps_read_bytes(hermes_home: Path):
    output_dir = hermes_home / "cron" / "output" / "job-1"
    output_dir.mkdir(parents=True)
    output_file = output_dir / "latest.md"
    output_file.write_text("FIRST-MARKER\n" + "\n".join(f"tail filler {i}" for i in range(500)))

    excerpt, silent, output_path, output_mtime = _latest_cron_output_excerpt(
        hermes_home / "cron" / "output", "job-1", max_bytes=256
    )

    assert excerpt
    assert "FIRST-MARKER" not in excerpt
    assert silent is False
    assert output_path == "latest.md"
    assert output_mtime is not None


def test_collect_cron_excerpt_respects_log_tail_bytes(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "Job One", "state": "scheduled"}]})
    )
    output_dir = hermes_home / "cron" / "output" / "job-1"
    output_dir.mkdir(parents=True)
    (output_dir / "latest.md").write_text(
        "FIRST-MARKER\n" + "\n".join(f"tail filler {i}" for i in range(2000))
    )

    c = Collector(hermes_home, log_tail_bytes=1024)
    state = c.collect()

    assert len(state.cron.jobs) == 1
    excerpt = state.cron.jobs[0].latest_output_excerpt
    # The capped read only sees the file tail (possibly starting mid-line),
    # so the head marker can never be the excerpt.
    assert excerpt
    assert "FIRST-MARKER" not in excerpt
    c.close()


def test_tail_log_stream_skips_reread_when_mtime_and_size_unchanged(hermes_home: Path):
    log = hermes_home / "logs" / "agent.log"
    log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - original line\n")
    c = Collector(hermes_home)
    first = c.collect()
    assert [line.message for line in first.logs.agent_lines] == ["original line"]

    # Rewrite with identical size and restore the original mtime: an unchanged
    # mtime+size signature must short-circuit the re-read and return cached lines.
    stat = log.stat()
    replacement = "2026-04-09 15:41:58,123 - hermes - INFO - replaced line\n"
    assert len(replacement) == stat.st_size
    log.write_text(replacement)
    os.utime(log, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    second = c.collect()
    assert [line.message for line in second.logs.agent_lines] == ["original line"]

    # A size change invalidates the cache and the new content is read.
    log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - a much longer brand new line\n")
    third = c.collect()
    assert [line.message for line in third.logs.agent_lines] == ["a much longer brand new line"]
    c.close()


def test_summarize_profile_caches_session_count_by_db_mtime(hermes_home: Path):
    profile_home = hermes_home / "profiles" / "coding"
    profile_home.mkdir(parents=True)
    db_file = profile_home / "state.db"
    db_file.touch()

    constructed: list[Path] = []

    class CountingFakeDB:
        def __init__(self, db_path: Path):
            constructed.append(db_path)

        def read_session_count(self) -> int:
            return 7

        def close(self) -> None:
            return None

    c = Collector(hermes_home, db_factory=CountingFakeDB)
    baseline = len(constructed)  # Collector.__init__ constructs the root DB

    first = c._summarize_profile("coding", profile_home)
    second = c._summarize_profile("coding", profile_home)
    assert first.session_count == 7
    assert second.session_count == 7
    assert len(constructed) == baseline + 1  # unchanged mtime -> no new DB open

    bumped = time.time() + 10
    os.utime(db_file, (bumped, bumped))
    third = c._summarize_profile("coding", profile_home)
    assert third.session_count == 7
    assert len(constructed) == baseline + 2

    # WAL-only write: main db mtime unchanged, -wal mtime bumps -> must invalidate.
    wal_file = profile_home / "state.db-wal"
    wal_file.touch()
    wal_bumped = time.time() + 20
    os.utime(wal_file, (wal_bumped, wal_bumped))
    fourth = c._summarize_profile("coding", profile_home)
    assert fourth.session_count == 7
    assert len(constructed) == baseline + 3
    c.close()


def test_collect_profiles_preserves_last_good_when_profile_db_read_fails(hermes_home: Path):
    profile_home = hermes_home / "profiles" / "coding"
    profile_home.mkdir(parents=True)
    db_file = profile_home / "state.db"
    db_file.touch()

    class FlakyFakeDB:
        fail = False

        def __init__(self, db_path: Path):
            self.db_path = db_path

        def read_sessions(self) -> list[dict[str, object]]:
            return []

        def read_tool_stats(self) -> list[dict[str, object]]:
            return []

        def read_session_count(self) -> int:
            if FlakyFakeDB.fail:
                raise RuntimeError("profile db unavailable")
            return 7

        @property
        def last_read_sessions_stale(self) -> bool:
            return False

        @property
        def last_read_tool_stats_stale(self) -> bool:
            return False

        def close(self) -> None:
            return None

    c = Collector(hermes_home, db_factory=FlakyFakeDB)
    state1 = c.collect()
    assert state1.profiles.profiles[0].session_count == 7

    FlakyFakeDB.fail = True
    bumped = time.time() + 10
    os.utime(db_file, (bumped, bumped))
    state2 = c.collect()

    assert state2.profiles == state1.profiles
    assert "profiles" in state2.health.failed_sources
    c.close()


def test_collect_kanban_corrupt_db_preserves_last_good(populated_hermes_home: Path):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state1 = c.collect()
    assert state1.kanban.task_count == 3

    (populated_hermes_home / "kanban.db").write_bytes(b"this is not a sqlite database")
    state2 = c.collect()

    assert state2.kanban == state1.kanban
    assert "kanban" in state2.health.failed_sources
    c.close()


def test_collect_kanban_corrupt_db_without_history_uses_default(hermes_home: Path):
    (hermes_home / "kanban.db").write_bytes(b"garbage bytes")
    c = Collector(hermes_home)
    state = c.collect()
    assert "kanban" in state.health.failed_sources
    assert state.kanban.task_count == 0
    c.close()


def test_read_kanban_state_reads_wal_database(hermes_home: Path):
    db_path = hermes_home / "kanban.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    create_kanban_db_tables(writer)
    writer.execute(
        "INSERT INTO tasks (id, title, status, created_at, consecutive_failures) "
        "VALUES ('t_wal', 'WAL task', 'in_progress', ?, 0)",
        (int(time.time()),),
    )
    writer.commit()
    assert db_path.with_name("kanban.db-wal").exists()

    state = _read_kanban_state(db_path, KanbanState(db_present=True))
    writer.close()

    assert state.task_count == 1
    assert state.active_tasks[0].task_id == "t_wal"
    assert state.status_counts == {"in_progress": 1}


def test_collect_kanban_null_columns_coerced(populated_hermes_home: Path):
    c = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    state = c.collect()
    assert "kanban" not in state.health.failed_sources

    tasks = {task.task_id: task for task in state.kanban.active_tasks}
    null_task = tasks["t_null"]
    assert null_task.assignee == ""
    assert null_task.last_failure_error == ""
    assert null_task.priority == 0
    assert null_task.worker_pid == 0
    assert null_task.session_id == ""
    assert null_task.model_override == ""
    assert null_task.branch_name == ""
    assert state.kanban.assignee_counts.get("unassigned") == 1

    runs = {run.run_id: run for run in state.kanban.recent_runs}
    null_run = runs[2]
    assert null_run.profile == ""
    assert null_run.outcome == ""
    assert null_run.error == ""
    assert null_run.summary == ""
    assert null_run.worker_pid == 0
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


def test_collector_reads_jobs_json(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "j1",
                        "name": "test-cron",
                        "schedule_display": "every 10m",
                        "state": "scheduled",
                        "enabled": True,
                        "next_run_at": "2026-04-09T19:00:00",
                        "last_status": None,
                        "last_error": None,
                    },
                    {
                        "id": "j2",
                        "name": "failed-job",
                        "schedule_display": "every 1h",
                        "state": "error",
                        "enabled": True,
                        "last_status": "error",
                        "last_error": "timeout",
                    },
                ],
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.cron.job_count == 2
    assert state.cron.error_count == 1
    assert state.cron.jobs[0].name == "test-cron"
    assert state.cron.jobs[1].name == "failed-job"
    c.close()


def test_collector_no_jobs_json(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.cron.job_count == 0
    assert state.cron.jobs == []
    c.close()


def test_collector_enriches_cron_jobs_with_delivery_and_output(hermes_home: Path):
    (hermes_home / "channel_directory.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-04-09T19:00:00",
                "platforms": {
                    "telegram": [
                        {"id": "-1001", "name": "My Group", "type": "group"},
                    ]
                },
            }
        )
    )
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "j1",
                        "name": "test-cron",
                        "schedule_display": "every 10m",
                        "state": "scheduled",
                        "enabled": True,
                        "deliver": "telegram:My Group",
                    }
                ],
            }
        )
    )
    output_dir = hermes_home / "cron" / "output" / "j1"
    output_dir.mkdir(parents=True)
    (output_dir / "2026-04-09T19-00-00.md").write_text("[SILENT]\nNo changes to report.\n")

    c = Collector(hermes_home)
    state = c.collect()
    job = state.cron.jobs[0]
    assert job.delivery_target_label == "telegram:My Group"
    assert job.silent_run is True
    assert "No changes to report" in job.latest_output_excerpt
    c.close()


def test_collect_profiles_empty_when_no_profiles_dir(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.profiles.profile_count == 0
    assert state.profiles.profiles == []
    c.close()


def test_collect_profiles_lists_profile_directories(profiled_hermes_home: Path):
    c = Collector(profiled_hermes_home)
    state = c.collect()
    assert state.profiles.profile_count == 1
    profile = state.profiles.profiles[0]
    assert profile.name == "coding"
    assert profile.session_count == 1
    assert profile.skill_count == 1
    assert profile.db_size_bytes > 0
    assert profile.soul_excerpt == ""
    assert profile.latest_log_mtime is not None
    c.close()


def test_collect_profiles_reads_soul_excerpt_when_present(profiled_hermes_home: Path):
    soul = profiled_hermes_home / "profiles" / "coding" / "SOUL.md"
    soul.write_text("Profile soul line one\nProfile soul line two\n")
    c = Collector(profiled_hermes_home)
    state = c.collect()
    assert state.profiles.profiles[0].soul_excerpt == "Profile soul line one"
    c.close()
