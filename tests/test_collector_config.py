"""Collection of config.yaml settings, provider routing, MCP servers,
credential pools, skins, and version metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hermesd.collector import (
    Collector,
    _mcp_tool_filter_summary,
)


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


def test_collect_provider_auth_freshness(hermes_home: Path):
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "credential_pool": {
                    "vertex": [
                        {
                            "label": "Vertex",
                            "auth_type": "oauth",
                            "source": "adc",
                            "last_status": "ok",
                            "access_expires_at": "2026-07-11T12:00:00Z",
                            "last_refresh": "2026-07-11T11:00:00Z",
                        }
                    ]
                }
            }
        )
    )

    c = Collector(hermes_home)
    state = c.collect()

    assert state.skills_memory.credential_pools[0].expires_at == "2026-07-11T12:00:00Z"
    assert state.skills_memory.credential_pools[0].last_refresh == "2026-07-11T11:00:00Z"
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


def test_collect_credential_pool_empty_and_non_dict_list_degrade_gracefully(hermes_home: Path):
    """An empty list or a list of non-dicts yields a name-only entry, no crash."""
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "active_provider": "p_empty",
                "credential_pool": {
                    "p_empty": [],
                    "p_scalar": ["not-a-dict", 7],
                },
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    pools = {p.name: p for p in state.skills_memory.credential_pools}
    assert pools["p_empty"].name == "p_empty"
    assert pools["p_empty"].label == "p_empty"  # falls back to provider name
    assert pools["p_empty"].source == ""
    assert pools["p_scalar"].source == ""
    c.close()


def test_collect_providers_ignores_non_mapping_auth_json(hermes_home: Path):
    auth = hermes_home / "auth.json"
    auth.write_text(json.dumps(["not", "a", "mapping"]))
    c = Collector(hermes_home)
    state = c.collect()
    assert state.skills_memory.providers == []
    c.close()


def test_mcp_tool_filter_summary_empty_returns_blank():
    # Empty cfg and present-but-empty include/exclude both render as blank.
    assert _mcp_tool_filter_summary({}) == ""
    assert _mcp_tool_filter_summary({"include": [], "exclude": []}) == ""


def test_config_source_survives_null_and_wrong_typed_values(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump(
            {
                "model": {"default": None, "provider": None},
                "agent": {
                    "max_turns": "unlimited",
                    "reasoning_effort": None,
                    "active_personality": None,
                },
                "compression": {"threshold": None},
                "security": {"redact_secrets": None},
                "approvals": {"mode": None},
            }
        )
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "config" not in state.health.failed_sources
    assert state.config.model == ""
    assert state.config.provider == ""
    assert state.config.max_turns == 0
    assert state.config.compression_threshold == 0.0
    assert state.config.reasoning_effort == ""
    assert state.config.security_redact is False
    assert state.config.approvals_mode == ""
    assert state.config.personality == ""


def test_config_max_turns_null_falls_back_to_default(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"model": {"default": "gpt-5.4"}, "agent": {"max_turns": None}})
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "config" not in state.health.failed_sources
    assert state.config.model == "gpt-5.4"
    assert state.config.max_turns == 0


def _agent_limits_config(home: Path, payload: object):
    (home / "config.yaml").write_text(yaml.dump(payload))
    collector = Collector(home)
    try:
        return collector.collect().config
    finally:
        collector.close()


def test_config_new_sections_present(hermes_home: Path):
    config = _agent_limits_config(
        hermes_home,
        {
            "delegation": {
                "enabled": True,
                "compression_threshold_tokens": 60000,
                "max_parallel": 4,
            },
            "goals": {"enabled": True, "turn_budget": 40},
            "updates": {"auto": True, "channel": "stable"},
            "mcp_servers": {"sheets": {"url": "https://mcp.example.com"}, "fs": {"command": "npx"}},
            "plugins": {"disabled": ["a"], "extra": {}},
            "tool_loop_guardrails": {"enabled": True, "max_repeats": 3},
            "max_live_sessions": 8,
            "streaming": {"enabled": True},
            "logging": {"level": "INFO"},
            "network": {"proxy": "http://proxy.example.com:8080"},
        },
    )

    assert config.delegation_enabled is True
    assert config.delegation_compression_threshold_tokens == 60000
    assert config.delegation_max_parallel == 4
    assert config.goals_enabled is True
    assert config.goals_turn_budget == 40
    assert config.updates_auto is True
    assert config.updates_channel == "stable"
    assert config.mcp_server_count == 2
    assert config.mcp_server_names == ["fs", "sheets"]
    assert config.plugin_config_count == 2
    assert config.tool_loop_guardrails_enabled is True
    assert config.tool_loop_max_repeats == 3
    assert config.max_live_sessions == 8
    assert config.streaming_enabled is True
    assert config.logging_level == "INFO"
    assert config.network_proxy_configured is True


def test_config_new_sections_absent_use_defaults(hermes_home: Path):
    config = _agent_limits_config(hermes_home, {"model": {"default": "gpt-5.4"}})

    assert config.delegation_enabled is False
    assert config.delegation_compression_threshold_tokens == 0
    assert config.delegation_max_parallel == 0
    assert config.goals_enabled is False
    assert config.goals_turn_budget == 0
    assert config.updates_auto is False
    assert config.updates_channel == ""
    assert config.mcp_server_count == 0
    assert config.mcp_server_names == []
    assert config.plugin_config_count == 0
    assert config.tool_loop_guardrails_enabled is False
    assert config.tool_loop_max_repeats == 0
    assert config.max_live_sessions == 0
    assert config.streaming_enabled is False
    assert config.logging_level == ""
    assert config.network_proxy_configured is False


def test_config_new_sections_wrong_types_fall_back_to_defaults(hermes_home: Path):
    config = _agent_limits_config(
        hermes_home,
        {
            "model": {"default": "gpt-5.4"},
            "delegation": ["not", "a", "mapping"],
            "goals": "on",
            "updates": 7,
            "mcp_servers": ["sheets"],
            "plugins": "all",
            "tool_loop_guardrails": [],
            "max_live_sessions": "not-a-number",
            "streaming": None,
            "logging": {"level": {"nested": "bad"}},
            "network": 12,
        },
    )

    assert config.delegation_enabled is False
    assert config.delegation_compression_threshold_tokens == 0
    assert config.goals_turn_budget == 0
    assert config.updates_channel == ""
    assert config.mcp_server_count == 0
    assert config.mcp_server_names == []
    assert config.plugin_config_count == 0
    assert config.tool_loop_max_repeats == 0
    assert config.max_live_sessions == 0
    assert config.streaming_enabled is False
    assert config.logging_level == ""
    assert config.network_proxy_configured is False


def test_config_plugins_list_shape_counts_entries(hermes_home: Path):
    assert _agent_limits_config(hermes_home, {"plugins": ["a", "b", "c"]}).plugin_config_count == 3


def test_config_updates_check_key_sets_auto(hermes_home: Path):
    config = _agent_limits_config(hermes_home, {"updates": {"check": True, "channel": "beta"}})

    assert config.updates_auto is True
    assert config.updates_channel == "beta"


def test_config_mcp_server_names_capped_and_sorted(hermes_home: Path):
    config = _agent_limits_config(
        hermes_home,
        {"mcp_servers": {f"srv-{index:02d}": {"command": "npx"} for index in range(30)}},
    )

    assert config.mcp_server_count == 30
    assert len(config.mcp_server_names) == 20
    assert config.mcp_server_names == sorted(config.mcp_server_names)
    assert config.mcp_server_names[0] == "srv-00"


def test_config_new_sections_never_surface_secret_values(hermes_home: Path):
    config = _agent_limits_config(
        hermes_home,
        {
            "network": {"proxy": "https://admin:hunter2@proxy.example.com:8080"},
            "mcp_servers": {
                "sheets": {
                    "url": "https://mcp.example.com/sheets?api_key=sk-live-secret",
                    "api_key": "sk-live-secret",
                }
            },
            "logging": {"level": "DEBUG", "token": "sk-live-secret"},
            "delegation": {"enabled": True, "secret": "sk-live-secret"},
        },
    )
    payload = json.dumps(config.model_dump(mode="json"))

    assert "hunter2" not in payload
    assert "sk-live-secret" not in payload
    assert config.network_proxy_configured is True
    assert config.mcp_server_names == ["sheets"]


@pytest.mark.parametrize("key", ["http_proxy", "https_proxy"])
def test_config_network_proxy_alternate_keys(hermes_home: Path, key: str):
    config = _agent_limits_config(hermes_home, {"network": {key: "http://proxy.example.com"}})

    assert config.network_proxy_configured is True


def test_config_fixture_home_surfaces_new_sections(populated_hermes_home: Path):
    collector = Collector(populated_hermes_home)
    try:
        config = collector.collect().config
    finally:
        collector.close()

    assert config.goals_enabled is True
    assert config.streaming_enabled is True
    assert config.logging_level == "INFO"
    assert config.mcp_server_names == ["playwright", "sheets"]
    assert config.network_proxy_configured is True
    assert "hunter2" not in json.dumps(config.model_dump(mode="json"))
