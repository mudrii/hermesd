"""Summaries derived from config.yaml and auth.json."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from hermesd.collect.common import (
    _age_seconds,
    _as_dict,
    _as_list,
    _coerce_bool,
    _coerce_int,
    _iso_to_epoch,
)
from hermesd.collect.plugins import PLUGIN_KIND_STANDALONE, gate_plugin, plugin_name_set
from hermesd.collect.redaction import (
    _API_KEY_FIELD_NAMES,
    _OAUTH_FIELD_NAMES,
    _redact_secret_url,
)
from hermesd.models import (
    ConfigBackupGroup,
    ConfigBackupKind,
    ModelCooldown,
    PlatformStatus,
    PluginActivation,
    ProfileRouteSummary,
)

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
        "plugin_enabled_count": len(plugin_name_set(plugins.get("enabled"))),
        "plugin_disabled_count": len(plugin_name_set(plugins.get("disabled"))),
        "tool_loop_warnings_enabled": bool(guardrails.get("warnings_enabled")),
        "tool_loop_hard_stop_enabled": bool(guardrails.get("hard_stop_enabled")),
        "max_concurrent_sessions": resolve_max_concurrent_sessions(cfg),
        "max_live_sessions": resolve_max_live_sessions(cfg),
        "streaming_enabled": bool(_as_dict(cfg.get("streaming")).get("enabled")),
        "logging_level": _plain_str(_as_dict(cfg.get("logging")).get("level")),
        "network_proxy_configured": _proxy_configured(_as_dict(cfg.get("network"))),
    }


# Built-in personality names, ``BUILTIN_PERSONALITIES`` in
# ``hermes_cli/personality.py:19-34`` (names only; the prompts are upstream's).
_BUILTIN_PERSONALITY_NAMES = frozenset(
    {
        "helpful",
        "concise",
        "technical",
        "creative",
        "teacher",
        "kawaii",
        "catgirl",
        "pirate",
        "shakespeare",
        "surfer",
        "noir",
        "uwu",
        "philosopher",
        "hype",
    }
)
# ``NEUTRAL_PERSONALITY_NAMES`` (``hermes_cli/personality.py:16``).
_NEUTRAL_PERSONALITY_NAMES = frozenset({"", "none", "default", "neutral"})


def _normalize_personality_name(value: object) -> str:
    name = str(value or "").strip().lower()
    return "" if name in _NEUTRAL_PERSONALITY_NAMES else name


def _active_personality_name(cfg: dict[str, Any]) -> str:
    """The selected personality, as ``active_personality_name`` resolves it.

    ``display.personality`` holds the selection (the only sanctioned write path,
    ``persist_personality`` at ``hermes_cli/personality.py:127-131``); it counts
    only when it names a known personality — a built-in, a root
    ``personalities`` entry or an ``agent.personalities`` entry
    (``available_personalities``, ``:86-96``). Anything else is no overlay.
    """
    name = _normalize_personality_name(_as_dict(cfg.get("display")).get("personality"))
    if not name:
        return ""
    known = set(_BUILTIN_PERSONALITY_NAMES)
    for user in (cfg.get("personalities"), _as_dict(cfg.get("agent")).get("personalities")):
        known.update(_normalize_personality_name(key) for key in _as_dict(user))
    return name if name in known else ""


# ``PROFILE_ID_RE`` (``hermes_constants.py:283``) and ``_RESERVED_NAMES``
# (``hermes_cli/profiles.py:151``); ``default`` is always valid.
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESERVED_PROFILE_NAMES = frozenset({"hermes", "test", "tmp", "root", "sudo"})
_ROUTE_DISCRIMINATORS = ("guild_id", "chat_id", "thread_id", "user_id")


def _route_profile_name(value: object) -> str | None:
    """``normalize_profile_name`` + ``validate_profile_name`` (``profiles.py:235-270``)."""
    stripped = str(value).strip()
    if not stripped:
        return None
    if stripped.casefold() == "default":
        return "default"
    name = stripped.lower()
    if not _PROFILE_ID_RE.match(name) or name in _RESERVED_PROFILE_NAMES:
        return None
    return name


def _route_id_present(value: object) -> bool:
    """Whether ``_coerce_route_id`` leaves a truthy discriminator (``:110-131``)."""
    return value is not None and str(value) != ""


def _profile_routes(cfg: dict[str, Any]) -> tuple[list[ProfileRouteSummary], int]:
    """The routes ``parse_profile_routes`` keeps, most-specific first, and a skip count.

    The list comes from the root ``profile_routes`` key, else
    ``gateway.profile_routes`` (``gateway/config_loader.py:93,117-121``); only a
    list is accepted. Discriminator *names* are reported, never the chat, guild
    or user ids themselves (``gateway/profile_routing.py:133-171``).
    """
    raw = cfg.get("profile_routes")
    if raw is None:
        raw = _as_dict(cfg.get("gateway")).get("profile_routes")
    if not isinstance(raw, list):
        return [], 0
    routes: list[ProfileRouteSummary] = []
    skipped = 0
    for entry in raw:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        platform = entry.get("platform") or ""
        profile = _route_profile_name(entry.get("profile") or "")
        user_id = entry.get("user_id")
        if (
            not platform
            or profile is None
            or ("user_id" in entry and (user_id is None or not str(user_id).strip()))
        ):
            skipped += 1
            continue
        bot_profile = str(entry.get("bot_profile") or "").strip()
        routes.append(
            ProfileRouteSummary(
                name=str(entry.get("name") or ""),
                platform=str(platform),
                profile=profile,
                enabled=entry.get("enabled", True) is not False,
                bot_profile="" if bot_profile == "default" else bot_profile,
                discriminators=[
                    key for key in _ROUTE_DISCRIMINATORS if _route_id_present(entry.get(key))
                ],
            )
        )
    routes.sort(key=_route_specificity, reverse=True)
    return routes, skipped


def _route_specificity(route: ProfileRouteSummary) -> int:
    """``ProfileRoute.specificity``: guild 2, chat 4, thread 8, user 16."""
    weights = {"guild_id": 2, "chat_id": 4, "thread_id": 8, "user_id": 16}
    return sum(weights[key] for key in route.discriminators)


def _integration_flags(cfg: dict[str, Any]) -> dict[str, Any]:
    """Monitoring, webhook and Langfuse switches — flags only, never endpoints."""
    monitoring = _as_dict(cfg.get("monitoring"))
    otlp = _as_dict(_as_dict(monitoring.get("export")).get("otlp"))
    plugins = _as_dict(cfg.get("plugins"))
    langfuse = gate_plugin(
        key="observability/langfuse",
        name="langfuse",
        kind=PLUGIN_KIND_STANDALONE,
        enabled=plugin_name_set(plugins.get("enabled")),
        disabled=plugin_name_set(plugins.get("disabled")),
    )
    routes, skipped = _profile_routes(cfg)
    return {
        "webhook_platform_enabled": bool(
            _as_dict(_as_dict(cfg.get("platforms")).get("webhook")).get("enabled")
        ),
        "profile_routes": routes,
        "profile_routes_skipped": skipped,
        "monitoring_health_export_enabled": bool(
            _as_dict(monitoring.get("gateway_health_export")).get("enabled")
        ),
        "monitoring_otlp_enabled": bool(otlp.get("enabled")),
        "monitoring_otlp_endpoint_configured": bool(str(otlp.get("endpoint") or "").strip()),
        "langfuse_plugin_enabled": langfuse.activation is PluginActivation.ENABLED,
    }


def _endpoint_url(entry: dict[str, Any]) -> str:
    """``_endpoint_url`` (``hermes_cli/doctor_config.py:398-401``)."""
    url = entry.get("api") or entry.get("base_url") or entry.get("url") or ""
    return str(url).strip().rstrip("/").lower()


def _doctor_config_findings(cfg: dict[str, Any]) -> dict[str, Any]:
    """The raw-file drift checks ``hermes doctor`` runs that need nothing but the file.

    ``_drift_stale_root_keys`` (``hermes_cli/doctor_config.py:341-361``): string
    root ``provider``/``base_url``. ``_drift_legacy_custom_providers``
    (``:405-430``): ``custom_providers`` list entries whose endpoint has no
    ``providers:`` twin. Labels are the entry name, else its URL redacted.
    """
    stale = [key for key in ("provider", "base_url") if isinstance(cfg.get(key), str)]
    legacy = cfg.get("custom_providers")
    labels: list[str] = []
    if isinstance(legacy, list):
        providers = cfg.get("providers")
        twins = {
            _endpoint_url(entry)
            for entry in (providers.values() if isinstance(providers, dict) else ())
            if isinstance(entry, dict)
        }
        for entry in legacy:
            if not isinstance(entry, dict):
                continue
            url = _endpoint_url(entry)
            if not url or url in twins:
                continue
            labels.append(str(entry.get("name") or "").strip() or _redact_secret_url(url))
    return {
        "stale_root_keys": stale,
        "legacy_custom_provider_labels": labels[:_MAX_LISTED_NAMES],
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
_KIND_RANK = {
    ConfigBackupKind.GOOD: 0,
    ConfigBackupKind.CORRUPT: 1,
    ConfigBackupKind.SETUP: 2,
    ConfigBackupKind.MIGRATION: 3,
    ConfigBackupKind.OTHER: 4,
}


def _backup_reason_kind(reason: str) -> ConfigBackupKind:
    """Coarse bucket for the audit trail: setup/migration stamps vs the rest."""
    if reason == _GOOD_REASON:
        return ConfigBackupKind.GOOD
    if reason == _CORRUPT_REASON:
        return ConfigBackupKind.CORRUPT
    if reason.startswith("pre-setup"):
        return ConfigBackupKind.SETUP
    if "migrate" in reason:
        return ConfigBackupKind.MIGRATION
    return ConfigBackupKind.OTHER


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
    scheme carries a reason. The entry cap bounds the *caller's* directory scan
    (the collector slices before calling), so this function only applies the
    display cap on the number of groups and marks the result truncated when it
    fires. Groups come back ranked by kind (``_KIND_RANK``) so that cap always
    keeps the load-bearing ``good`` and ``corrupt`` rows and drops audit-trail
    bulk instead.
    """
    stamps_by_reason: dict[str, list[tuple[str, float]]] = {}
    for name in names:
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
    groups.sort(key=lambda group: (_KIND_RANK[group.kind], group.reason))
    truncated = len(groups) > _CONFIG_BACKUP_GROUP_LIMIT
    return groups[:_CONFIG_BACKUP_GROUP_LIMIT], truncated


def _provider_free_tier(entry: dict[str, Any]) -> bool:
    """The Nous free-tier identity: an anonymous credential.

    Mirrors ``is_guest_state`` (``hermes_cli/anon_auth.py:88-89``), which keys
    on ``auth_method == ANON_AUTH_METHOD`` alone — the tier is a consequence of
    the credential, not a second condition, and an upgrade rewrites
    ``auth_method`` in place (``:647,756``). Key names only — the entry's token
    values are never read, and a dead guest credential is removed rather than
    marked, so the tier simply disappears when it lapses.
    """
    return entry.get("auth_method") == "anonymous"


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
        default_preset = str(next(iter(presets)))
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


def _pool_entries(raw_entry: object) -> list[dict[str, Any]]:
    """Every non-empty entry of a credential_pool value (list or legacy dict)."""
    items = raw_entry if isinstance(raw_entry, list) else [raw_entry]
    return [entry for entry in (_as_dict(item) for item in items) if entry]


def _absolute_timestamp(value: object) -> float | None:
    """``_parse_absolute_timestamp`` (``agent/credential_pool.py:402-424``).

    Epoch seconds, epoch milliseconds (anything past 1e12) or ISO-8601; a
    non-positive number is no timestamp at all.
    """
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int | float):
        numeric = float(value)
        if numeric <= 0 or not math.isfinite(numeric):
            return None
        return numeric / 1000.0 if numeric > 1_000_000_000_000 else numeric
    if isinstance(value, str):
        try:
            numeric = float(value.strip())
        except ValueError:
            return _iso_to_epoch(value)
        return _absolute_timestamp(numeric) if math.isfinite(numeric) else None
    return None


# Cooldown TTLs, ``agent/credential_pool.py:133-139`` and ``:143,149``.
_EXHAUSTED_TTL_401_SECONDS = 5 * 60
_EXHAUSTED_TTL_DEFAULT_SECONDS = 60 * 60
_EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS = 60
_FAILURE_REASON_BILLING = "billing"
_FAILURE_REASON_BILLING_UNVERIFIED = "billing_unverified"


def _exhausted_ttl(error_code: int, *, sole_credential: bool, failure_reason: str) -> int:
    """``_exhausted_ttl`` (``agent/credential_pool.py:372-398``).

    429 and the catch-all default share the one-hour bench upstream, so a single
    constant covers both here.
    """
    if error_code == 401:
        return _EXHAUSTED_TTL_401_SECONDS
    base = _EXHAUSTED_TTL_DEFAULT_SECONDS
    if failure_reason == _FAILURE_REASON_BILLING_UNVERIFIED and error_code != 402:
        return min(base, _EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS)
    is_billing = error_code == 402 or failure_reason == _FAILURE_REASON_BILLING
    if sole_credential and not is_billing:
        return min(base, _EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS)
    return base


def _credential_cooldown_remaining(
    entry: dict[str, Any], *, sole_credential: bool, now: float
) -> float | None:
    """Seconds an exhausted credential stays benched, as ``_exhausted_until`` decides.

    ``agent/credential_pool.py:468-480``: only ``last_status == "exhausted"`` is
    benched; the provider's ``last_error_reset_at`` wins, else
    ``last_status_at`` plus the TTL for the recorded error. ``sole_credential``
    mirrors ``_is_sole_credential`` (``:1064-1066``): at most one non-dead entry.
    """
    if str(entry.get("last_status") or "") != "exhausted":
        return None
    until = _absolute_timestamp(entry.get("last_error_reset_at"))
    if until is None:
        status_at = _absolute_timestamp(entry.get("last_status_at"))
        if status_at is None:
            return None
        until = status_at + _exhausted_ttl(
            _coerce_int(entry.get("last_error_code")),
            sole_credential=sole_credential,
            failure_reason=str(entry.get("failure_reason") or ""),
        )
    remaining = until - now
    return remaining if remaining > 0 else None


def _active_model_cooldowns(entries: list[dict[str, Any]], *, now: float) -> list[ModelCooldown]:
    """Per-model cooldowns still running, merged across a provider's entries.

    ``merge_model_cooldowns`` / ``model_cooldown_until``
    (``agent/credential_pool_model_cooldowns.py:22-44``): the latest reset per
    model wins, and only numeric resets still in the future are active. Model
    names only; the values are epochs, not secrets.
    """
    merged: dict[str, float] = {}
    for entry in entries:
        for model, until in _as_dict(entry.get("model_cooldowns")).items():
            if isinstance(until, bool) or not isinstance(until, int | float):
                continue
            merged[str(model)] = max(float(until), merged.get(str(model), 0.0))
    return [
        ModelCooldown(model=model, remaining_seconds=until - now)
        for model, until in sorted(merged.items())
        if until > now
    ][:_MAX_LISTED_NAMES]


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
