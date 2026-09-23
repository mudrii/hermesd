"""Integration switches and doctor-parity checks read from config.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from hermesd.collector import Collector
from hermesd.models import ConfigSummary


def _config(hermes_home: Path, cfg: dict) -> ConfigSummary:
    (hermes_home / "config.yaml").write_text(yaml.dump(cfg))
    c = Collector(hermes_home)
    try:
        return c.collect().config
    finally:
        c.close()


def test_profile_routes_summarize_discriminators_without_ids(hermes_home: Path):
    config = _config(
        hermes_home,
        {
            "profile_routes": [
                {
                    "name": "alice-dm",
                    "platform": "telegram",
                    "profile": "Alice",
                    "user_id": "123456789",
                    "chat_id": "-100200",
                },
                {
                    "name": "ops",
                    "platform": "discord",
                    "profile": "ops",
                    "guild_id": 42,
                    "thread_id": "9",
                    "enabled": False,
                    "bot_profile": "ops",
                },
                # Skipped upstream: missing profile, empty user_id, not a mapping.
                {"name": "broken", "platform": "slack"},
                {"name": "blank", "platform": "slack", "profile": "x", "user_id": "  "},
                {"name": "null", "platform": "slack", "profile": "x", "user_id": None},
                "not-a-route",
            ]
        },
    )
    assert [
        (r.name, r.platform, r.profile, r.enabled, r.bot_profile, r.discriminators)
        for r in config.profile_routes
    ] == [
        ("alice-dm", "telegram", "alice", True, "", ["chat_id", "user_id"]),
        ("ops", "discord", "ops", False, "ops", ["guild_id", "thread_id"]),
    ]
    assert config.profile_routes_skipped == 4
    dumped = config.model_dump_json()
    assert "123456789" not in dumped
    assert "-100200" not in dumped


def test_profile_routes_fall_back_to_gateway_section(hermes_home: Path):
    config = _config(
        hermes_home,
        {"gateway": {"profile_routes": [{"name": "r", "platform": "telegram", "profile": "dev"}]}},
    )
    assert [r.profile for r in config.profile_routes] == ["dev"]
    assert config.profile_routes[0].discriminators == []


def test_profile_routes_ignore_a_non_list_value(hermes_home: Path):
    config = _config(hermes_home, {"profile_routes": {"name": "r"}})
    assert config.profile_routes == []
    assert config.profile_routes_skipped == 0


def test_monitoring_webhook_and_langfuse_flags(hermes_home: Path):
    config = _config(
        hermes_home,
        {
            "monitoring": {
                "gateway_health_export": {"enabled": True},
                "export": {
                    "otlp": {
                        "enabled": True,
                        "endpoint": "https://user:secret@otel.example.com",
                        "headers_env": {"Authorization": "OTEL_TOKEN"},
                    }
                },
            },
            "platforms": {"webhook": {"enabled": True}},
            "plugins": {"enabled": ["observability/langfuse"]},
        },
    )
    assert config.monitoring_health_export_enabled is True
    assert config.monitoring_otlp_enabled is True
    assert config.monitoring_otlp_endpoint_configured is True
    assert config.webhook_platform_enabled is True
    assert config.langfuse_plugin_enabled is True
    assert "secret" not in config.model_dump_json()


def test_integration_flags_default_off(hermes_home: Path):
    config = _config(
        hermes_home,
        {
            "monitoring": {"export": {"otlp": {"enabled": True, "endpoint": ""}}},
            "plugins": {"enabled": ["langfuse"], "disabled": ["observability/langfuse"]},
        },
    )
    assert config.monitoring_health_export_enabled is False
    assert config.monitoring_otlp_enabled is True
    assert config.monitoring_otlp_endpoint_configured is False
    assert config.webhook_platform_enabled is False
    # plugins.disabled wins over plugins.enabled (gate order).
    assert config.langfuse_plugin_enabled is False


def test_langfuse_bare_leaf_name_enables_it(hermes_home: Path):
    config = _config(hermes_home, {"plugins": {"enabled": ["langfuse"]}})
    assert config.langfuse_plugin_enabled is True


def test_doctor_stale_root_keys_and_legacy_custom_providers(hermes_home: Path):
    config = _config(
        hermes_home,
        {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "custom_providers": [
                {"name": "twinned", "base_url": "https://llm.example.com/v1/"},
                {"name": "orphan", "base_url": "https://orphan.example.com/v1"},
                {"url": "https://key:hunter2@anon.example.com/v1"},
                {"name": "no-url"},
                "junk",
            ],
            "providers": {"twin": {"api": "https://LLM.example.com/v1"}},
        },
    )
    assert config.stale_root_keys == ["provider", "base_url"]
    assert config.legacy_custom_provider_labels[0] == "orphan"
    assert len(config.legacy_custom_provider_labels) == 2
    assert "hunter2" not in config.model_dump_json()


def test_doctor_checks_quiet_on_a_clean_config(hermes_home: Path):
    config = _config(
        hermes_home,
        {
            "model": {"provider": "openrouter", "base_url": "https://x"},
            # A non-string root provider is not the stale scalar doctor flags.
            "provider": {"nested": True},
            "custom_providers": {"not": "a list"},
        },
    )
    assert config.stale_root_keys == []
    assert config.legacy_custom_provider_labels == []
