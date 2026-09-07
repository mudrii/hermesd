"""Summaries derived from config.yaml and auth.json."""

from __future__ import annotations

from typing import Any

from hermesd.collect.common import _as_dict, _as_list, _coerce_int
from hermesd.collect.redaction import _API_KEY_FIELD_NAMES, _OAUTH_FIELD_NAMES
from hermesd.models import PlatformStatus

# Upper bound on name lists surfaced from config/cache mappings.
_MAX_LISTED_NAMES = 20


def _config_agent_limits(cfg: dict[str, Any]) -> dict[str, Any]:
    """Read the optional hermes-agent 0.21 config sections.

    Every section is optional and may carry an unexpected shape; anything that
    does not match falls back to the ``ConfigSummary`` default. Only names,
    counts and flags are reported — never a configured value, which may hold a
    token or a URL with embedded credentials.
    """
    delegation = _as_dict(cfg.get("delegation"))
    goals = _as_dict(cfg.get("goals"))
    updates = _as_dict(cfg.get("updates"))
    guardrails = _as_dict(cfg.get("tool_loop_guardrails"))
    mcp_names = sorted(str(name) for name in _as_dict(cfg.get("mcp_servers")))
    plugins = _as_dict(cfg.get("plugins"))
    return {
        "delegation_max_concurrent_children": _coerce_int(
            delegation.get("max_concurrent_children")
        ),
        "delegation_max_spawn_depth": _coerce_int(delegation.get("max_spawn_depth")),
        "delegation_orchestrator_enabled": bool(delegation.get("orchestrator_enabled")),
        "goals_max_turns": _coerce_int(goals.get("max_turns")),
        "updates_check": bool(updates.get("check")),
        "updates_pre_update_backup": _pre_update_backup_mode(updates.get("pre_update_backup")),
        "updates_backup_keep": _coerce_int(updates.get("backup_keep")),
        "mcp_server_count": len(mcp_names),
        "mcp_server_names": mcp_names[:_MAX_LISTED_NAMES],
        "plugin_enabled_count": _name_list_count(plugins.get("enabled")),
        "plugin_disabled_count": _name_list_count(plugins.get("disabled")),
        "tool_loop_warnings_enabled": bool(guardrails.get("warnings_enabled")),
        "tool_loop_hard_stop_enabled": bool(guardrails.get("hard_stop_enabled")),
        "max_live_sessions": _coerce_int(cfg.get("max_live_sessions")),
        "streaming_enabled": bool(_as_dict(cfg.get("streaming")).get("enabled")),
        "logging_level": _plain_str(_as_dict(cfg.get("logging")).get("level")),
        "network_proxy_configured": _proxy_configured(_as_dict(cfg.get("network"))),
    }


def _name_list_count(value: object) -> int:
    """Length of a plugins.enabled/disabled name list; other shapes count as none."""
    return len(value) if isinstance(value, list) else 0


def _pre_update_backup_mode(value: object) -> str:
    """``updates.pre_update_backup`` mode; legacy booleans map to full/off."""
    if isinstance(value, bool):
        return "full" if value else "off"
    return _plain_str(value)


def _plain_str(value: object) -> str:
    """Stringify a scalar YAML value; anything else (dict, list, bool) is ""."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return ""
    return str(value)


def _proxy_configured(network: dict[str, Any]) -> bool:
    return any(bool(network.get(key)) for key in ("proxy", "http_proxy", "https_proxy"))


def _provider_model_label(cfg: dict[str, Any]) -> str:
    provider = str(cfg.get("provider") or "")
    model = str(cfg.get("model") or "")
    if provider and model:
        return f"{provider}/{model}"
    return provider or model


def _moa_config_summary(cfg: dict[str, Any]) -> dict[str, Any]:
    presets = _as_dict(cfg.get("presets"))
    default_preset = str(cfg.get("default_preset") or "")
    if not default_preset and presets:
        default_preset = next(iter(presets))
    active_preset = str(cfg.get("active_preset") or "")
    selected_preset = _as_dict(presets.get(active_preset) or presets.get(default_preset))
    if not selected_preset and not presets:
        selected_preset = cfg
    references = [
        item for item in _as_list(selected_preset.get("reference_models")) if isinstance(item, dict)
    ]
    return {
        "default_preset": default_preset,
        "active_preset": active_preset,
        "preset_count": len(presets),
        "reference_model_count": len(references),
        "aggregator_label": _provider_model_label(_as_dict(selected_preset.get("aggregator"))),
    }


def _scale_to_zero_relay_only(
    cfg: dict[str, Any],
    platforms: list[PlatformStatus],
) -> bool:
    if "relay_only" in cfg or "relay_only_when_idle" in cfg:
        return bool(cfg.get("relay_only") or cfg.get("relay_only_when_idle"))
    connected = [platform.name for platform in platforms if platform.state == "connected"]
    return not connected or all(_platform_is_relay_only(name) for name in connected)


def _platform_is_relay_only(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return normalized in {"raft", "photon", "imessage"}


def _provider_routing_summary(cfg: dict[str, Any]) -> str:
    if not cfg:
        return ""
    sort = str(cfg.get("sort") or "")
    only = cfg.get("only") or []
    ignore = cfg.get("ignore") or []
    order = cfg.get("order") or []
    parts = []
    if sort:
        parts.append(sort)
    if isinstance(only, list) and only:
        parts.append(f"only:{len(only)}")
    elif isinstance(ignore, list) and ignore:
        parts.append(f"ignore:{len(ignore)}")
    elif isinstance(order, list) and order:
        parts.append(f"order:{len(order)}")
    return " ".join(parts)


def _mcp_tool_filter_summary(cfg: dict[str, Any]) -> str:
    if not cfg:
        return ""
    include = cfg.get("include") or []
    exclude = cfg.get("exclude") or []
    if isinstance(include, list) and include:
        return ",".join(str(item) for item in include[:3])
    if isinstance(exclude, list) and exclude:
        return f"exclude:{len(exclude)}"
    return ""


def _channel_capabilities(name: str) -> list[str]:
    if name == "feishu":
        return ["meeting invites"]
    return []


def _platform_family_label(name: str) -> str:
    normalized = name.lower().replace("-", "_")
    families = {
        "whatsapp_cloud": "WhatsApp Cloud",
        "whatsapp_baileys": "WhatsApp Baileys",
        "teams": "Teams",
        "microsoft_teams": "Teams",
        "photon": "Photon/iMessage",
        "imessage": "Photon/iMessage",
        "raft": "Raft",
        "slack": "Slack",
        "discord": "Discord",
        "telegram": "Telegram",
        "matrix": "Matrix",
        "feishu": "Feishu",
    }
    return families.get(normalized, name)


def _stale_alias_count(aliases: dict[str, Any]) -> int:
    count = 0
    for entries in aliases.values():
        for value in _as_dict(entries).values():
            entry = _as_dict(value)
            if entry and bool(entry.get("stale") or entry.get("is_stale") or entry.get("expired")):
                count += 1
    return count


def _select_pool_entry(raw_entry: object) -> dict[str, Any]:
    """Reduce a credential_pool value to one representative entry.

    Live ``auth.json`` stores each provider's credentials as a list of entries;
    older configs used a single dict. The lowest-priority entry (the next
    credential to be used) represents the provider; ties keep list order.
    """
    if isinstance(raw_entry, list):
        candidates = [_as_dict(item) for item in raw_entry]
        candidates = [item for item in candidates if item]
        if not candidates:
            return {}
        return min(
            enumerate(candidates),
            key=lambda pair: (_coerce_int(pair[1].get("priority")), pair[0]),
        )[1]
    return _as_dict(raw_entry)


def _credential_auth_type(entry: dict[str, Any], provider_entry: dict[str, Any]) -> str:
    auth_type = str(entry.get("auth_type") or "")
    if auth_type:
        return auth_type

    merged_keys = set(entry) | set(provider_entry)
    if merged_keys & _OAUTH_FIELD_NAMES:
        return "oauth"
    if merged_keys & _API_KEY_FIELD_NAMES:
        return "api_key"
    return ""


def _credential_expiry(entry: dict[str, Any], provider_entry: dict[str, Any]) -> str:
    for key in (
        "expires_at",
        "access_expires_at",
        "token_expires_at",
        "agent_key_expires_at",
        "expiry",
    ):
        value = entry.get(key) or provider_entry.get(key)
        if value:
            return str(value)
    return ""
