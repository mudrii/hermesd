"""Summaries derived from config.yaml and auth.json."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from hermesd.collect.common import _age_seconds, _as_dict, _as_list, _coerce_bool, _coerce_int
from hermesd.collect.redaction import _API_KEY_FIELD_NAMES, _OAUTH_FIELD_NAMES
from hermesd.models import ConfigBackupGroup, PlatformStatus

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
        "max_concurrent_sessions": resolve_max_concurrent_sessions(cfg),
        "max_live_sessions": resolve_max_live_sessions(cfg),
        "streaming_enabled": bool(_as_dict(cfg.get("streaming")).get("enabled")),
        "logging_level": _plain_str(_as_dict(cfg.get("logging")).get("level")),
        "network_proxy_configured": _proxy_configured(_as_dict(cfg.get("network"))),
    }


def _coerce_session_cap(value: object) -> int | None:
    """Upstream's ``coerce_max_concurrent_sessions``: a positive int, else None.

    Mirrors ``hermes_cli/active_sessions.py:31-44``, which both capacity keys
    share. Booleans and fractional floats are rejected outright (upstream logs
    ``Ignoring invalid …`` and disables the cap), a string is parsed base 10, and
    ``0``/``None``/negative/invalid all collapse to None — *disabled*, which
    hermesd reports as "not configured" rather than as a limit of zero.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        parsed = int(value)
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip(), 10)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed > 0 else None


def resolve_max_concurrent_sessions(cfg: dict[str, Any]) -> int | None:
    """The cross-process active-session lease cap, or None when not configured.

    Mirrors ``resolve_max_concurrent_sessions``
    (``hermes_cli/active_sessions.py:47-61``), which resolves on key *presence*:
    a top-level ``max_concurrent_sessions`` wins even when its value is ``null``,
    and only an absent top-level key falls through to
    ``gateway.max_concurrent_sessions``.

    This is a **different resource** from ``max_live_sessions`` — a lease cap
    enforced when a surface attaches, not an LRU cap on in-memory sessions — so
    the two are resolved by separate functions and never share a field.
    """
    if "max_concurrent_sessions" in cfg:
        return _coerce_session_cap(cfg.get("max_concurrent_sessions"))
    gateway_cfg = cfg.get("gateway")
    if not isinstance(gateway_cfg, dict):
        return None
    return _coerce_session_cap(gateway_cfg.get("max_concurrent_sessions"))


def resolve_max_live_sessions(cfg: dict[str, Any]) -> int:
    """The gateway's in-memory LRU session cap; 0 when not configured/disabled.

    Mirrors ``_max_live_sessions`` (``tui_gateway/session_reaper.py:237-247``),
    which falls back on a *null value* rather than on key presence — so, unlike
    ``resolve_max_concurrent_sessions``, an explicit top-level
    ``max_live_sessions: null`` does reach ``gateway.max_live_sessions``. The
    asymmetry is upstream's, and reproducing it is what keeps hermesd's number
    equal to the one ``_enforce_session_cap`` acts on.

    ``_load_cfg()`` there is ``load_config_readonly`` minus the DEFAULT_CONFIG
    merge (``tui_gateway/server.py:1169-1176``), so an unset key really reads as
    0/disabled and the ``16`` default in ``config_defaults.py:42`` never applies.
    """
    raw: Any = cfg.get("max_live_sessions")
    if raw is None:
        gateway_cfg = cfg.get("gateway")
        if isinstance(gateway_cfg, dict):
            raw = gateway_cfg.get("max_live_sessions")
    return _coerce_session_cap(raw) or 0


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


# backups/config/ naming, mirrored from hermes_cli/config_backups.py:29-69:
# ``config.yaml.<reason>.<YYYYMMDD-HHMMSS>``, at most five copies kept per
# reason, byte-identical repeats skipped. Observed writer reasons:
# "good" (config.py:2217), "corrupt" (config.py:86,497), "pre-setup"
# (setup.py:655), "pre-migrate-xai" (xai_retirement.py:164) and
# "pre-docker-migrate" (scripts/docker_config_migrate.py:28).
_BACKUP_CONFIG_PREFIX = "config.yaml."
_BACKUP_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_BACKUP_STAMP_CHARS = 15
_CONFIG_BACKUP_GROUP_LIMIT = 8
_CONFIG_BACKUP_ENTRY_LIMIT = 512

_GOOD_REASON = "good"
_CORRUPT_REASON = "corrupt"

# Display ranking for the group cap. "good" and "corrupt" are single reasons,
# so ranking them first means the cap can never evict the "last changed" stamp
# or the corrupt alert; the bulk audit trail (setup/migration/other) absorbs
# the truncation instead.
_KIND_RANK = {"good": 0, "corrupt": 1, "setup": 2, "migration": 3, "other": 4}


def _backup_reason_kind(reason: str) -> str:
    """Coarse bucket for the audit trail: setup/migration stamps vs the rest."""
    if reason == _GOOD_REASON:
        return _GOOD_REASON
    if reason == _CORRUPT_REASON:
        return _CORRUPT_REASON
    if reason.startswith("pre-setup"):
        return "setup"
    if "migrate" in reason:
        return "migration"
    return "other"


def _config_backup_stamp_epoch(stamp: str) -> float | None:
    """Local-time epoch for a backup filename stamp.

    Upstream builds the stamp with ``time.strftime`` (``config_backups.py:59``),
    so the name carries the writer's *local* time; parsing it as a naive
    datetime and reading ``.timestamp()`` evaluates it against the same local
    clock instead of pretending it was UTC.
    """
    if len(stamp) != _BACKUP_STAMP_CHARS:
        return None
    try:
        return datetime.strptime(stamp, _BACKUP_STAMP_FORMAT).timestamp()
    except ValueError:
        return None


def _config_backup_groups(
    names: Iterable[str], *, now: float
) -> tuple[list[ConfigBackupGroup], bool]:
    """Group ``config.yaml.<reason>.<stamp>`` filenames by reason.

    Names are what the backups directory listed (any order); each group keeps
    its full count and its newest stamp. Junk and hand-named copies
    (``config.yaml.bak-my-note``) are skipped — only the writer's own naming
    scheme carries a reason. Both caps are display hygiene for a hostile
    directory: the entry cap bounds the scan, the group cap bounds the model,
    and either firing marks the result truncated. Groups come back ranked by
    kind (``_KIND_RANK``) so the cap always keeps the load-bearing ``good`` and
    ``corrupt`` rows and drops audit-trail bulk instead.
    """
    kept: list[str] = []
    scan_truncated = False
    for name in names:
        if len(kept) >= _CONFIG_BACKUP_ENTRY_LIMIT:
            scan_truncated = True
            break
        kept.append(name)

    stamps_by_reason: dict[str, list[tuple[str, float]]] = {}
    for name in kept:
        if not name.startswith(_BACKUP_CONFIG_PREFIX):
            continue
        reason, separator, stamp = name[len(_BACKUP_CONFIG_PREFIX) :].rpartition(".")
        if not separator or not reason:
            continue
        epoch = _config_backup_stamp_epoch(stamp)
        if epoch is None:
            continue
        stamps_by_reason.setdefault(reason, []).append((stamp, epoch))

    groups: list[ConfigBackupGroup] = []
    for reason, stamps in stamps_by_reason.items():
        stamps.sort()
        newest_stamp, newest_epoch = stamps[-1]
        groups.append(
            ConfigBackupGroup(
                reason=reason,
                kind=_backup_reason_kind(reason),
                count=len(stamps),
                newest_stamp=newest_stamp,
                newest_age_seconds=_age_seconds(newest_epoch, now),
            )
        )
    groups.sort(key=lambda group: (_KIND_RANK.get(group.kind, 99), group.reason))
    truncated = scan_truncated or len(groups) > _CONFIG_BACKUP_GROUP_LIMIT
    return groups[:_CONFIG_BACKUP_GROUP_LIMIT], truncated


def _provider_free_tier(entry: dict[str, Any]) -> bool:
    """The Nous free-tier identity: ``auth_method`` and ``account_tier`` both
    "anonymous" (``hermes_cli/anon_auth.py:39-41``, ``is_guest_state`` at
    ``:88-89``, minted state at ``:271-272``). Key names only — the entry's
    token values are never read, and a dead guest credential is removed rather
    than marked, so the tier simply disappears when it lapses.
    """
    return entry.get("auth_method") == "anonymous" and entry.get("account_tier") == "anonymous"


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
            # State payload written by the gateway: a stringified flag is
            # corruption, never truth (``bool("false")`` is True).
            if entry and (
                _coerce_bool(entry.get("stale"))
                or _coerce_bool(entry.get("is_stale"))
                or _coerce_bool(entry.get("expired"))
            ):
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
