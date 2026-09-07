"""Summaries derived from config.yaml and auth.json."""

from __future__ import annotations

from typing import Any

from hermesd.collect.common import _as_dict, _as_list, _coerce_int
from hermesd.collect.redaction import _API_KEY_FIELD_NAMES, _OAUTH_FIELD_NAMES
from hermesd.models import PlatformStatus


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
