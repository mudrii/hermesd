"""Gateway liveness and platform records: heartbeat, lifecycle, config generation,
updates, ledgers, and the shared-listener routing/ingress each platform entry carries.

Every reader here is pure: it takes already-loaded JSON (via the collector's
last-good file cache), an injected clock, and — where liveness depends on the
host — an injected ``pid_exists``. Nothing in this module opens a file for
writing or imports hermes-agent.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermesd.collect.common import (
    _age_seconds,
    _as_dict,
    _as_list,
    _coerce_bool,
    _coerce_float,
    _coerce_int,
    _file_size,
    _iso_to_epoch,
    _mtime,
    _read_tail_text,
    _read_text_capped,
    _safe_child_path,
)
from hermesd.collect.redaction import _redact_secret_text, _redact_secret_url
from hermesd.collect.sqlite_util import (
    _count_by,
    _query_rows,
    _table_count_or_zero,
    _table_exists,
)
from hermesd.file_cache import JsonMapping
from hermesd.models import (
    ConfigSourceStamp,
    DeliveryObligationSummary,
    ForensicFile,
    GatewayLoopHealth,
    PlatformOwnership,
    PlatformStatus,
)

# The gateway watchdog rewrites state/gateway.heartbeat every 30s: three missed
# writes is stale — and, for a witness-less writer, that budget is upstream's
# decisive cutoff (`DEFAULT_LOOP_LIVENESS_STALE_AFTER_S`, hermes_cli/gateway.py:345).
# The ten-write figure only survives in the heartbeat-only fallback below, where
# nothing witnessed the loop and a long-silent file may just be a stopped gateway.
_HEARTBEAT_TICKING_SECONDS = 90.0
_HEARTBEAT_STALE_SECONDS = 300.0
# Loop-tick witness probe (hermes_cli/gateway.py:363-424): one byte, one second.
_LOOP_TICK_PROBE_TIMEOUT_SECONDS = 1.0
# Never escalate on a single silent probe (hermes_cli/gateway.py
# _probe_loop_tick_socket_sustained): the loop may be in a transient synchronous
# stall. hermesd spreads the strikes across collector refreshes instead of sleeping
# inside one pass, so a wedge verdict needs silence on this many consecutive probes.
_LOOP_TICK_SILENCE_STRIKES = 3
# Respawn-storm ledger policy (gateway/status.py record_start_and_check_storm:57-83):
# upstream defaults, overridable there by HERMES_GATEWAY_MAX_STARTS /
# HERMES_GATEWAY_START_WINDOW_S — hermesd reads only the file, so it renders these
# defaults and labels the count against them.
_RESTART_STORM_WINDOW_SECONDS = 120.0
_RESTART_STORM_CAP = 5
# state/dashboard_clients.heartbeat younger than this means a web client is
# attached right now (gateway/scale_to_zero.py:100-111 touches it per frame).
_DASHBOARD_CLIENT_ATTACHED_SECONDS = 60.0
# logs/gateway-exit-diag.log grows forever upstream (one JSON object per
# asyncio.run() return path plus one gateway.previous_unclean_exit per unclean
# boot; writer hermes_cli/gateway.py:4643-4665, HERMES_GATEWAY_EXIT_DIAG=0 opts
# out). Warn once the file is past a couple of megabytes; read only the tail.
_EXIT_DIAG_SIZE_WARN_BYTES = 2 * 1024 * 1024
_EXIT_DIAG_TAG_CHARS = 120
_UNCLEAN_EXIT_TAG = "gateway.previous_unclean_exit"
# Event-only companion logs. Signal-initiated shutdown blocks (POSIX), freeze
# dumps, and macOS supervisor reload attempts: metadata only, never contents.
_FORENSIC_COMPANION_FILES = (
    "gateway-shutdown-diag.log",
    "gateway_faulthandler.log",
    "launchd-reload.log",
)
# Port binders whose /p/<profile>/ surface is a MIRROR served by the default
# profile's own adapter; a secondary never gets an instance of these. Copied
# from gateway/config.py (SHARED_LISTENER_MIRROR_*), never imported.
_SHARED_LISTENER_MIRROR_PLATFORMS = frozenset({"api_server", "webhook"})
_SHARED_LISTENER_MIRROR_PATHS = {"api_server": "/v1", "webhook": "/webhooks/<route>"}
_MIRROR_PROFILE_LIMIT = 16
_DAY_SECONDS = 86400.0
# Undelivered obligations still in flight; "delivered" is done and "failed" is
# counted separately.
_PENDING_DELIVERY_STATES = ("pending", "attempting")
_DELIVERY_ERROR_EXCERPT_CHARS = 80
# Bound on the restart history scanned per pass; state.db is large and only the
# newest incarnations matter for uptime and the 24h restart count.
_INCARNATION_SCAN_LIMIT = 500
_OPEN_DELIVERY_LIMIT = 5
# A recorded start_time is only usable as wall-clock when it lands inside this
# window of now. The live gateway_state.json carries a monotonic-clock value
# (178874708938, i.e. the year 7638), which must never be read as an epoch.
_PLAUSIBLE_EPOCH_WINDOW_SECONDS = 50 * 365 * _DAY_SECONDS
# Outcomes that mean the update never reached a clean finish. Upstream also
# stamps a ``stop_reason`` on successful receipts, so that field alone is not
# evidence of failure and is only read alongside a missing success marker.
_UNFINISHED_OUTCOMES = frozenset({"failed", "partial", "running"})
# The receipt's fleet matrix holds one row per profile. Cap the retained state
# vocabulary so an untrusted file cannot grow the map; never cap the skew scan.
_FLEET_STATE_KIND_LIMIT = 8
# The multiplexer keys a served profile's adapter ``<profile>:<platform>``
# (gateway/run_adapters.py:1048). Upstream validates that grammar
# *unconditionally* before projecting a status key anywhere
# (hermes_cli/web_routers/status.py:122-127), with the stated reason that a
# failed config-set load must not fail open into projecting arbitrary keys from a
# process-local JSON file. hermesd mirrors the grammar: a key that fails it stays
# opaque, so nothing in gateway_state.json can invent a profile name.
_PROFILE_PLATFORM_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}:[a-z0-9][a-z0-9_-]{0,63}$")
# Adapter states whose recorded ingress URL upstream never surfaces
# (hermes_cli/gateway_multiplex_served.py:66-68): the route belonged to an
# adapter that is not serving, so the URL is history rather than an endpoint.
_INGRESS_SUPPRESSED_STATES = frozenset({"fatal", "disconnected", "stopped"})
# Adapter states the multiplexer will mirror a shared listener for
# (gateway/status.py:962-966). Upstream asks whether the default's entry is
# *serving*, not whether it escaped a failure vocabulary: ``paused`` and any
# unrecognised or missing state are refused, so neither may publish a callback
# URL hermesd synthesized for a secondary profile.
_MIRROR_SERVING_STATES = frozenset({"connected", "connecting", "retrying"})


def _optional_int(value: object) -> int | None:
    """Coerce to int, preserving a genuine null (an exit code that never happened)."""
    return None if value is None else _coerce_int(value)


@dataclass(frozen=True, slots=True)
class _RecordWriter:
    """Identity of the process that most recently wrote ``gateway_state.json``.

    Upstream re-stamps the top-level ``pid``/``start_time`` on every write, so
    this describes the current writer and nothing older.
    """

    pid: int | None = None
    start_time: int | None = None


def _record_writer(data: JsonMapping) -> _RecordWriter:
    return _RecordWriter(
        pid=_identity_stamp(data.get("pid")),
        start_time=_identity_stamp(data.get("start_time")),
    )


def _identity_stamp(value: object) -> int | None:
    """A writer-identity stamp, or None when it cannot be compared.

    Zero and absent both collapse to None: coercing a missing stamp to 0 would
    make two records that carry no identity compare equal and read as owned.
    """
    if value is None or isinstance(value, bool):
        return None
    coerced = _coerce_int(value)
    return coerced if coerced > 0 else None


def _platform_ownership(info: dict[str, Any], writer: _RecordWriter) -> PlatformOwnership:
    """Classify a platform entry by exact ``(pid, start_time)`` equality.

    All four values must be present. A gateway that predates writer provenance —
    or one whose host could not resolve a process start time — records no usable
    identity, and guessing "current" there would present a preserved record as
    live. Ownership is deliberately independent of heartbeat freshness.
    """
    entry_pid = _identity_stamp(info.get("writer_pid"))
    entry_start = _identity_stamp(info.get("writer_start_time"))
    if entry_pid is None or entry_start is None:
        return PlatformOwnership.UNVERIFIABLE
    if writer.pid is None or writer.start_time is None:
        return PlatformOwnership.UNVERIFIABLE
    if entry_pid == writer.pid and entry_start == writer.start_time:
        return PlatformOwnership.CURRENT
    return PlatformOwnership.PRESERVED


def _split_platform_key(key: str) -> tuple[str, str]:
    """``(profile, platform)`` for a grammar-valid namespaced key, else ``("", key)``.

    A key that contains ``:`` but fails the grammar is deliberately *not* split:
    it stays verbatim as the platform name and records no profile, so an arbitrary
    string in a process-local JSON file can never be projected onto a profile.
    """
    if ":" not in key or not _PROFILE_PLATFORM_KEY_RE.fullmatch(key):
        return "", key
    profile, _, platform = key.partition(":")
    return profile, platform


def _recorded_ingress_url(info: dict[str, Any], *, state: str, record_current: bool) -> str:
    """The entry's ingress URL, or ``""`` wherever upstream would suppress it.

    Mirrors ``served_profile_ingress_urls`` (``hermes_cli/gateway_multiplex_served.py:46-70``):
    nothing is surfaced unless the process that wrote ``gateway_state.json`` is
    still live (``record_current`` here); a falsy recorded URL or a fatal,
    disconnected or stopped adapter is also suppressed. A different live
    ``gateway.pid`` proves a replacement process is running but cannot authorize
    its predecessor's URL. ``hermes_cli/web_routers/messaging.py:263`` applies the
    same liveness rule to plain platform keys, which is why this is not restricted
    to namespaced ones. The value is redacted *here*, at the data boundary: an
    ingress URL is exactly the shape that carries userinfo or a secret query
    parameter, and a panel must never be the first thing to see it.
    """
    if not record_current or state in _INGRESS_SUPPRESSED_STATES:
        return ""
    url = str(info.get("ingress_url") or "")
    return _redact_secret_url(url) if url else ""


def _listener_mirror_urls(
    name: str,
    info: dict[str, Any],
    state: str,
    *,
    record_current: bool,
    served_profiles: list[str],
) -> dict[str, str]:
    """Mirror URLs a served profile reaches through this default-listener binder.

    Mirrors ``shared_listener_mirror_platforms`` (``gateway/status.py:951-974``):
    the multiplexer never builds api_server/webhook adapters for a secondary, so
    every reader must synthesize ``<listener_base>/p/<profile><mirror_path>``
    from the default profile's own entry. Only an entry upstream calls *serving*
    is mirrored — ``_MIRROR_SERVING_STATES``, the allow-list at
    ``gateway/status.py:962-966`` — not the ingress deny-list, which answers a
    different question (a paused or unlabelled adapter is not serving, and
    upstream refuses to publish its URL). A dead writer or a missing base also
    mean the URL is history rather than an endpoint, and the value is redacted at
    the data boundary. Only a bounded roster of served profiles is synthesized.
    """
    if not record_current or state not in _MIRROR_SERVING_STATES:
        return {}
    if name not in _SHARED_LISTENER_MIRROR_PLATFORMS:
        return {}
    base = info.get("listener_base")
    if not isinstance(base, str) or not base:
        return {}
    mirrors: dict[str, str] = {}
    for profile in served_profiles[:_MIRROR_PROFILE_LIMIT]:
        if not profile or profile == "default":
            # "default" owns the listener; it needs no mirror of itself.
            continue
        path = _SHARED_LISTENER_MIRROR_PATHS.get(name, "")
        mirrors[profile] = _redact_secret_url(f"{base}/p/{profile}{path}")
    return mirrors


def _platform_status(
    key: str,
    info: dict[str, Any],
    now: float,
    writer: _RecordWriter,
    *,
    record_current: bool,
    served_profiles: list[str] | None = None,
) -> PlatformStatus:
    profile, name = _split_platform_key(key)
    state = str(info.get("state") or "unknown")
    retrying_since = str(info.get("retrying_since") or "")
    served = served_profiles or []
    mirrors = _listener_mirror_urls(
        name,
        info,
        state,
        record_current=record_current,
        served_profiles=served,
    )
    return PlatformStatus(
        name=name,
        profile=profile,
        mirror_urls=mirrors,
        # Only a built roster can be short: an empty one means nothing was
        # mirrored, not that the profile list was cut.
        mirror_urls_truncated=bool(mirrors) and len(served) > _MIRROR_PROFILE_LIMIT,
        ingress_url=_recorded_ingress_url(info, state=state, record_current=record_current),
        state=state,
        updated_at=str(info.get("updated_at") or ""),
        error_code=str(info.get("error_code") or ""),
        error_message=str(info.get("error_message") or ""),
        needs_attention=_coerce_bool(info.get("needs_attention")),
        retrying_since=retrying_since,
        retrying_since_age_seconds=_age_seconds(_iso_to_epoch(retrying_since), now),
        writer_pid=_identity_stamp(info.get("writer_pid")),
        writer_start_time=_identity_stamp(info.get("writer_start_time")),
        ownership=_platform_ownership(info, writer),
    )


def _heartbeat_liveness(
    data: JsonMapping,
    file_mtime: float | None,
    now: float,
    *,
    running: bool,
) -> tuple[float | None, GatewayLoopHealth]:
    """Event-loop liveness from the watchdog heartbeat; file mtime is the fallback clock."""
    stamp = _iso_to_epoch(data.get("updated_at")) if data else None
    age = _age_seconds(stamp if stamp is not None else file_mtime, now)
    if age is None:
        return None, GatewayLoopHealth.UNKNOWN
    if age <= _HEARTBEAT_TICKING_SECONDS:
        return age, GatewayLoopHealth.TICKING
    if age <= _HEARTBEAT_STALE_SECONDS:
        return age, GatewayLoopHealth.STALE
    # A long-silent heartbeat is only "wedged" while the gateway claims to run;
    # otherwise it is just an old file left by a stopped gateway.
    return age, GatewayLoopHealth.WEDGED if running else GatewayLoopHealth.STALE


# ---------------------------------------------------------------------------
# Loop-tick witness (state/gateway.loop-tick.<pid>.sock, or a 127.0.0.1 TCP port)
#
# The heartbeat moved off-loop (#90502), so a fresh file no longer proves the loop
# dispatches. The gateway arms a witness served *by the loop itself*
# (gateway/shutdown_watchdog.py:288-302) and advertises it on the heartbeat as
# ``loop_tick_socket`` plus, when the loop bound a loopback listener, the
# ``loop_tick_tcp_port`` to reach it — both derived from the same server, so a
# port without the flag is not a shape the writer produces (the flag is true on
# POSIX and Windows alike; an unbound listener nulls both). The protocol is
# one byte: connect, expect b"1", close — the client sends nothing, so probing is
# non-mutating and safe for a read-only tool. Verdict vocabulary mirrors
# ``classify`` in hermes_cli/gateway.py:425-497.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LoopTickPlan:
    """Which witness to probe, and what the heartbeat promised about it."""

    pid: int
    tcp_port: int | None
    # True = advertised and armed; False = the key was written but the witness is
    # not usable (upstream bind failed); None = the payload predates the key.
    armed: bool | None


def _loop_tick_probe_plan(
    heartbeat: JsonMapping,
    gateway_pid: int,
    *,
    running: bool,
) -> _LoopTickPlan | None:
    """Whether a probe would be evidence, and which node to hit.

    A stopped gateway's stale file is history, not a wedged loop, so it is never
    probed. The socket node carries the writer's PID in its name and stale sibling
    nodes from dead PIDs are swept only at gateway start
    (``_sweep_stale_tick_sockets``), so the heartbeat PID must match the gateway's
    own before any node is touched — otherwise the probe would interrogate another
    process's witness. A missing or non-positive PID is no match.
    """
    if not running or not heartbeat:
        return None
    pid = _coerce_int(heartbeat.get("pid"))
    if pid <= 0 or gateway_pid <= 0 or pid != gateway_pid:
        return None
    raw_port = heartbeat.get("loop_tick_tcp_port")
    tcp_port: int | None = None
    if raw_port is not None and not isinstance(raw_port, bool):
        port = _coerce_int(raw_port)
        if 0 < port <= 65535:
            tcp_port = port
    raw_armed = heartbeat.get("loop_tick_socket")
    armed: bool | None
    if "loop_tick_socket" not in heartbeat:
        armed = None
    elif raw_armed is True:
        armed = True
    else:
        # Malformed or explicitly false: never a reason to escalate.
        armed = False
    return _LoopTickPlan(pid=pid, tcp_port=tcp_port, armed=armed)


def _loop_tick_verdict(
    current: GatewayLoopHealth,
    age: float | None,
    plan: _LoopTickPlan | None,
    probe_result: bool | None,
    *,
    sustained_silence: bool,
) -> GatewayLoopHealth:
    """Refine the heartbeat-age verdict with witness evidence.

    Mirrors ``probe_gateway_loop_liveness`` (hermes_cli/gateway.py:425-497): an
    answer proves the loop alive no matter how old the file is; a fresh file with a
    silent witness is ambiguity (the off-loop write may land after a freeze); a
    stale file escalates only when the witness was armed *and* stayed silent across
    refreshes. A legacy payload (no witness key) means the heartbeat was still
    written on-loop, so staleness alone is proof. Every other stale combination is
    ambiguity, never a wedge — the socket handler swallows errors, so connection
    refused and timeout mean the same thing and a vanished node is no evidence.
    """
    if plan is None or age is None:
        return current
    if probe_result is True:
        return GatewayLoopHealth.ALIVE
    if age <= _HEARTBEAT_TICKING_SECONDS:
        return GatewayLoopHealth.UNKNOWN if probe_result is False else current
    # Past the stale budget the witness decides, exactly as upstream escalates
    # (``:465-497``): there is no milder band between the budget and the
    # old on-loop 300 s cutoff, because a gateway wedged for four minutes is
    # not a slow heartbeat. The three-strike guard still stands, so the first
    # silent probes read STALE rather than WEDGED.
    if plan.armed is None:
        return GatewayLoopHealth.LEGACY
    if plan.armed is False:
        return GatewayLoopHealth.UNKNOWN
    if probe_result is False:
        return GatewayLoopHealth.WEDGED if sustained_silence else GatewayLoopHealth.STALE
    return GatewayLoopHealth.UNKNOWN


def _default_loop_tick_probe(
    pid: int,
    tcp_port: int | None,
    home: Path,
    *,
    timeout: float = _LOOP_TICK_PROBE_TIMEOUT_SECONDS,
) -> bool | None:
    """One witness probe: True answered, False silent, None no node to ask.

    Mirrors ``_ping_loop_tick_witness`` (hermes_cli/gateway.py:363-376): the
    handler swallows its own errors, so refusal and timeout are both just
    "silent". Sends nothing and reads at most one byte.
    """
    if tcp_port is not None:
        if not 0 < tcp_port <= 65535:
            return None
        family: int = socket.AF_INET
        address: tuple[str, int] | str = ("127.0.0.1", tcp_port)
    else:
        node = home / "state" / f"gateway.loop-tick.{pid}.sock"
        try:
            if not node.is_socket():
                return None
        except OSError:
            return None
        family = socket.AF_UNIX
        address = str(node)
    sock: socket.socket | None = None
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(max(float(timeout), 0.0))
        sock.connect(address)
        return sock.recv(1) == b"1"
    except OSError:
        return False
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()


@dataclass(frozen=True, slots=True)
class _LifecycleStatus:
    phase: str = ""
    last_exit_code: int | None = None
    last_exit_reason: str = ""
    unclean_previous_exit: bool = False
    prior_unclean_exit: bool = False
    prior_suspected_oom: bool = False


def _lifecycle_status(data: JsonMapping, pid_exists: Callable[[int], bool]) -> _LifecycleStatus:
    """A ``running`` phase whose pid is gone means the previous life never wrote an exit.

    The running record can also carry the previous life's verdict
    (``record_startup``, gateway/lifecycle_ledger.py:84-93): ``prior_unclean_exit``
    and ``prior_suspected_oom``. Strict boolean reads — the sentinel is written by
    the live gateway, so only a literal True is evidence.
    """
    if not data:
        return _LifecycleStatus()
    phase = str(data.get("phase") or "")
    pid = _coerce_int(data.get("pid"))
    return _LifecycleStatus(
        phase=phase,
        last_exit_code=_optional_int(data.get("exit_code")),
        last_exit_reason=str(data.get("exit_reason") or ""),
        unclean_previous_exit=phase == "running" and pid > 0 and not pid_exists(pid),
        prior_unclean_exit=data.get("prior_unclean_exit") is True,
        prior_suspected_oom=data.get("prior_suspected_oom") is True,
    )


@dataclass(frozen=True, slots=True)
class _ConfigGeneration:
    """The gateway's recorded config fingerprint and source stamps.

    Informational only: no current hermes-agent writes ``config_generation``,
    so the recorded per-source mtimes are orphan data and are never compared
    against the live files (see ``_config_stale``).
    """

    fingerprint: str = ""
    short: str = ""
    sources: list[ConfigSourceStamp] = field(default_factory=list)


def _config_generation(data: JsonMapping) -> _ConfigGeneration:
    """Config files the running gateway recorded that it loaded."""
    raw = _as_dict(data.get("config_generation"))
    if not raw:
        return _ConfigGeneration()
    sources = [
        ConfigSourceStamp(
            name=str(info.get("name") or ""),
            path=str(info.get("path") or ""),
            exists=_coerce_bool(info.get("exists")),
            mtime_ns=_coerce_int(info.get("mtime_ns")),
            size=_coerce_int(info.get("size")),
        )
        for entry in _as_list(raw.get("sources"))
        if (info := _as_dict(entry))
    ]
    return _ConfigGeneration(
        fingerprint=str(raw.get("fingerprint") or ""),
        short=str(raw.get("short") or ""),
        sources=sources,
    )


def _plausible_epoch(value: object, now: float) -> float | None:
    """Wall-clock epoch for `value`, or None when it cannot be one.

    Accepts an ISO-8601 string or a numeric epoch, and rejects anything more
    than _PLAUSIBLE_EPOCH_WINDOW_SECONDS from `now` — hermes-agent records a
    monotonic clock reading under the same ``start_time`` key.
    """
    epoch = _iso_to_epoch(value)
    if epoch is None:
        if isinstance(value, str) or value is None:
            return None
        epoch = _coerce_float(value)
    if epoch <= 0 or abs(epoch - now) > _PLAUSIBLE_EPOCH_WINDOW_SECONDS:
        return None
    return epoch


def _gateway_start_epoch(
    heartbeat: JsonMapping,
    lifecycle: JsonMapping,
    state: JsonMapping,
    now: float,
) -> float | None:
    """When the running gateway started, from the first source carrying a usable stamp."""
    for data in (heartbeat, lifecycle, state):
        epoch = _plausible_epoch(data.get("start_time"), now)
        if epoch is not None:
            return epoch
    return None


def _config_stale(config_path: Path, root: Path, start_epoch: float | None) -> bool:
    """True when config.yaml was written after the running gateway started.

    ``config_path`` is stat'd through symlinks on purpose: dotfiles setups link
    ``~/.hermes/config.yaml`` at a repository copy, and that is still the file
    the gateway loads. Only the lexical location is confined to `root`.
    """
    if start_epoch is None:
        return False
    if not config_path.is_relative_to(root):
        return False
    try:
        return config_path.stat().st_mtime > start_epoch
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class _SkewEvidence:
    """Which recorded evidence decided skew, so the panel can say so honestly.

    An empty ``source`` means skew was *not assessable* — no fleet matrix and no
    unfinished run to fall back on, or no gateway sha to compare against. That is
    distinct from "assessed and found consistent".
    """

    skewed: bool = False
    source: str = ""


@dataclass(frozen=True, slots=True)
class _UpdateReceipt:
    outcome: str = ""
    finished_age_seconds: float | None = None
    from_version: str = ""
    to_version: str = ""
    failed_step: str = ""
    runtime_code_skew: bool = False
    runtime_code_skew_source: str = ""
    update_receipt_unfinished: bool = False
    update_fleet_states: dict[str, int] = field(default_factory=dict)
    update_fleet_runtime_count: int = 0


def _update_receipt_status(data: JsonMapping, now: float, code_sha: str) -> _UpdateReceipt:
    if not data:
        return _UpdateReceipt()
    unfinished = _receipt_looks_unfinished(data)
    fleet = _as_list(data.get("fleet"))
    evidence = _skew_evidence(fleet, data, code_sha, unfinished)
    return _UpdateReceipt(
        outcome=str(data.get("outcome") or ""),
        finished_age_seconds=_age_seconds(_iso_to_epoch(data.get("finished_at")), now),
        from_version=str(_as_dict(data.get("pre_update")).get("version") or ""),
        to_version=str(_as_dict(data.get("post_update")).get("version") or ""),
        failed_step=_first_failed_step(data.get("steps")),
        runtime_code_skew=evidence.skewed,
        runtime_code_skew_source=evidence.source,
        update_receipt_unfinished=unfinished,
        update_fleet_states=_fleet_state_counts(fleet),
        update_fleet_runtime_count=len(fleet),
    )


def _first_failed_step(steps: object) -> str:
    """Name of the first step that recorded ok=false; a missing ok is not a failure."""
    for entry in _as_list(steps):
        info = _as_dict(entry)
        if "ok" in info and not info["ok"]:
            return str(info.get("name") or "")
    return ""


def _receipt_looks_unfinished(data: JsonMapping) -> bool:
    """True when the receipt is from an update that never reached a clean finish.

    Mirrors upstream ``_receipt_looks_unfinished``. The command boundary stamps a
    ``stop_reason`` on clean receipts too, so that field only counts when nothing
    else vouched for success — otherwise a successful update looks unfinished and
    its pre-pull plan SHAs retrigger a restart forever.
    """
    exit_code = data.get("exit_code")
    outcome = data.get("outcome")
    if exit_code not in (0, None) or outcome in _UNFINISHED_OUTCOMES:
        return True
    if _as_dict(data.get("gateway_restart")).get("incomplete"):
        return True
    succeeded = exit_code == 0 or outcome == "success"
    return bool(data.get("stop_reason")) and not succeeded


def _skew_evidence(
    fleet: list[object], data: JsonMapping, code_sha: str, unfinished: bool
) -> _SkewEvidence:
    """Prefer the post-restart fleet matrix; the plan only explains an unfinished run.

    ``plan.runtimes[].code_sha`` is captured *before* the pull, so a finished
    update's plan always disagrees with the running tree. Treating that as skew
    both cries wolf on healthy fleets and hides a fleet that genuinely came back
    on the wrong build, which is why the plan is consulted only when there is no
    fleet matrix and the receipt shows the update never finished.
    """
    if not code_sha:
        return _SkewEvidence()
    if fleet:
        return _SkewEvidence(_any_fleet_skew(fleet, code_sha), "fleet")
    if not unfinished:
        return _SkewEvidence()
    plan_runtimes = _as_list(_as_dict(data.get("plan")).get("runtimes"))
    return _SkewEvidence(_any_sha_mismatch(plan_runtimes, code_sha), "plan")


def _any_fleet_skew(fleet: list[object], code_sha: str) -> bool:
    """A recorded ``stale`` state is skew even when its sha was never stamped."""
    return any(
        _as_dict(entry).get("state") == "stale" or _entry_sha_differs(entry, code_sha)
        for entry in fleet
    )


def _any_sha_mismatch(runtimes: list[object], code_sha: str) -> bool:
    return any(_entry_sha_differs(entry, code_sha) for entry in runtimes)


def _entry_sha_differs(entry: object, code_sha: str) -> bool:
    """A runtime pinned to a different non-empty sha than the gateway is skewed."""
    sha = str(_as_dict(entry).get("code_sha") or "")
    return bool(sha) and sha != code_sha


def _fleet_state_counts(fleet: list[object]) -> dict[str, int]:
    """Recorded fleet matrix states, vocabulary-capped. Counts every row scanned."""
    counts: dict[str, int] = {}
    for entry in fleet:
        state = str(_as_dict(entry).get("state") or "unknown")
        if state not in counts and len(counts) >= _FLEET_STATE_KIND_LIMIT:
            continue
        counts[state] = counts.get(state, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Respawn-storm ledger (gateway-starts.log, gateway/status.py:57-83)
#
# One repr(float) UTC epoch per line, rewritten atomically as a ring of
# max(max_starts*4, 40) entries. An absent file is NOT evidence of zero
# restarts: HERMES_GATEWAY_MAX_STARTS<=0 disables the writer upstream. Kept on
# the ROOT resolver like the other gateway launch files (recorded divergence).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StartStorm:
    """Start ledger facts for the configured respawn-storm window."""

    recorded: bool = False
    starts_window: int = 0
    starts_1h: int = 0
    last_start_age_seconds: float | None = None
    cap: int = _RESTART_STORM_CAP
    window_seconds: float = _RESTART_STORM_WINDOW_SECONDS

    def model_fields(self) -> dict[str, Any]:
        return {
            "gateway_starts_recorded": self.recorded,
            "gateway_starts_window": self.starts_window,
            "gateway_starts_1h": self.starts_1h,
            "seconds_since_last_gateway_start": self.last_start_age_seconds,
            "restart_storm_cap": self.cap if self.recorded else 0,
            "restart_storm_window_seconds": self.window_seconds,
            "in_respawn_backoff": self.recorded and self.starts_window > self.cap,
        }


def _respawn_storm_policy(cfg: JsonMapping) -> tuple[int, float]:
    """``(max_starts, window_seconds)`` as upstream resolves them from config.

    Mirrors ``_respawn_storm_backoff`` (``hermes_cli/gateway.py:4673-4685``):
    only a real ``int`` counts for ``max_starts`` (a JSON ``true`` is an int in
    Python but not upstream) and only ``int``/``float`` for ``window_seconds``;
    anything else keeps ``DEFAULT_CONFIG``'s 5 / 120 s
    (``hermes_cli/config_defaults.py:1978``). The environment overrides upstream
    also honours (``HERMES_GATEWAY_MAX_STARTS``, ``HERMES_GATEWAY_START_WINDOW_S``)
    belong to the gateway's environment and are deliberately not read here — the
    panel therefore reports the policy *as recorded in config*.
    """
    respawn = _as_dict(_as_dict(cfg.get("gateway")).get("respawn_storm"))
    raw_cap = respawn.get("max_starts")
    cap = (
        raw_cap
        if isinstance(raw_cap, int) and not isinstance(raw_cap, bool)
        else _RESTART_STORM_CAP
    )
    raw_window = respawn.get("window_seconds")
    window = (
        float(raw_window)
        if isinstance(raw_window, int | float) and not isinstance(raw_window, bool)
        else _RESTART_STORM_WINDOW_SECONDS
    )
    return cap, window


def _read_start_storm(
    path: Path,
    root: Path,
    now: float,
    *,
    cap: int = _RESTART_STORM_CAP,
    window_seconds: float = _RESTART_STORM_WINDOW_SECONDS,
) -> _StartStorm:
    """Count recorded gateway starts in the storm-detection windows.

    ``recorded`` means "a ledger we can read", not "a file exists": upstream
    appends ``now`` before its atomic ``os.replace`` (``gateway/status.py:64-79``),
    so a file whose every line fails to parse — empty, whitespace or junk — was
    not written by the ledger and proves as little as an absent one. A
    non-positive ``cap`` disables the writer upstream (``max_starts <= 0``), so a
    leftover file is stale by construction and no verdict is derived from it.
    """
    if cap <= 0 or not path.is_file():
        return _StartStorm(cap=cap, window_seconds=window_seconds)
    text = _read_text_capped(path, root)
    if not text and _file_size(path) > 0:
        return _StartStorm(cap=cap, window_seconds=window_seconds)
    starts: list[float] = []
    for line in text.splitlines():
        try:
            epoch = float(line.strip())
        except ValueError:
            continue
        # A future stamp is clock skew, not a restart that has not happened yet.
        if 0.0 < epoch <= now:
            starts.append(epoch)
    if not starts:
        return _StartStorm(cap=cap, window_seconds=window_seconds)
    return _StartStorm(
        recorded=True,
        starts_window=sum(1 for start in starts if now - start <= window_seconds),
        starts_1h=sum(1 for start in starts if now - start <= _DAY_SECONDS / 24.0),
        last_start_age_seconds=now - max(starts),
        cap=cap,
        window_seconds=window_seconds,
    )


# ---------------------------------------------------------------------------
# Dashboard client attachment (state/dashboard_clients.heartbeat)
#
# A 0-byte file touched on every websocket connect and inbound frame, throttled
# to once per 5s per process (gateway/scale_to_zero.py:100-111). The mtime is
# the whole payload; a missing file means "never", not "idle". Stat only — the
# websocket itself is never contacted.
# ---------------------------------------------------------------------------


def _dashboard_client_status(path: Path, root: Path, now: float) -> tuple[bool, float | None]:
    attached = False
    age: float | None = None
    if _safe_child_path(path, root):
        stamp = _mtime(path)
        if stamp is not None:
            age = max(0.0, now - stamp)
            attached = age <= _DASHBOARD_CLIENT_ATTACHED_SECONDS
    return attached, age


# ---------------------------------------------------------------------------
# Exit diagnostics ledger (logs/gateway-exit-diag.log)
#
# JSONL: {"ts", "tag", "pid", "python", "platform", ...extras such as code,
# traceback, argv}. Only the tag and timestamp are ever surfaced — extras stay
# unread, and the tail read is capped by the same log-tail-bytes knob that
# bounds log panels.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ExitDiag:
    recorded: bool = False
    last_tag: str = ""
    last_age_seconds: float | None = None
    unclean_24h: int = 0
    size_bytes: int = 0
    oversized: bool = False
    forensic_files: list[ForensicFile] = field(default_factory=list)


def _read_exit_diag(path: Path, root: Path, now: float, tail_bytes: int) -> _ExitDiag:
    """Ledger facts from the tail of the exit-diag JSONL; absent is no evidence."""
    if not _safe_child_path(path, root) or not path.is_file():
        return _ExitDiag()
    size = _file_size(path)
    text = _read_tail_text(path, max(1, tail_bytes))
    last_tag = ""
    last_age: float | None = None
    unclean_24h = 0
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue  # includes a line the tail cut in half
        if not isinstance(record, dict):
            continue
        tag = str(record.get("tag") or "")
        if not tag:
            continue
        last_tag = tag[:_EXIT_DIAG_TAG_CHARS]
        stamp = _iso_to_epoch(record.get("ts"))
        if stamp is not None:
            last_age = _age_seconds(stamp, now)
            if tag == _UNCLEAN_EXIT_TAG and 0 <= now - stamp <= _DAY_SECONDS:
                unclean_24h += 1
    return _ExitDiag(
        recorded=True,
        # The ledger is a log file: upstream writes literal tags, but a foreign
        # or tampered writer can put credential-shaped text in one, and every
        # other log-derived string hermesd surfaces goes through the redactor.
        last_tag=_redact_secret_text(last_tag[:_EXIT_DIAG_TAG_CHARS]),
        last_age_seconds=last_age,
        unclean_24h=unclean_24h,
        size_bytes=size,
        oversized=size > _EXIT_DIAG_SIZE_WARN_BYTES,
    )


def _read_forensic_companions(logs_dir: Path, root: Path, now: float) -> list[ForensicFile]:
    """Stat the event-only companion logs; a missing file is the healthy state."""
    files: list[ForensicFile] = []
    for name in _FORENSIC_COMPANION_FILES:
        path = logs_dir / name
        if not _safe_child_path(path, root) or not path.is_file():
            continue
        stamp = _mtime(path)
        files.append(
            ForensicFile(
                name=name,
                size_bytes=_file_size(path),
                age_seconds=max(0.0, now - stamp) if stamp is not None else None,
            )
        )
    return files


@dataclass(frozen=True, slots=True)
class _GatewayLedgerRows:
    """Raw state.db ledger data; ages are derived per tick from the live clock."""

    incarnation_count: int = 0
    incarnation_starts: list[float] = field(default_factory=list)
    delivery_counts: dict[str, int] = field(default_factory=dict)
    delivery_rows: list[dict[str, Any]] = field(default_factory=list)


def _read_gateway_ledger_rows(conn: sqlite3.Connection) -> _GatewayLedgerRows:
    """Read the restart history and delivery obligations. Both tables may be absent."""
    return _GatewayLedgerRows(
        incarnation_count=_table_count_or_zero(conn, "gateway_heartbeats"),
        incarnation_starts=_read_incarnation_starts(conn),
        delivery_counts=_read_delivery_counts(conn),
        delivery_rows=_read_open_delivery_rows(conn),
    )


def _read_incarnation_starts(conn: sqlite3.Connection) -> list[float]:
    if not _table_exists(conn, "gateway_heartbeats"):
        return []
    rows = _query_rows(
        conn,
        "SELECT started_at FROM gateway_heartbeats "
        f"ORDER BY started_at DESC LIMIT {_INCARNATION_SCAN_LIMIT}",
    )
    starts = [_coerce_float(row.get("started_at") or 0.0) for row in rows]
    return [start for start in starts if start > 0]


def _read_delivery_counts(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "delivery_obligations"):
        return {}
    return _count_by(conn, "SELECT state, COUNT(*) FROM delivery_obligations GROUP BY state")


def _read_open_delivery_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Newest undelivered obligations. ``content`` is deliberately never selected."""
    if not _table_exists(conn, "delivery_obligations"):
        return []
    return _query_rows(
        conn,
        "SELECT platform, state, attempts, created_at, updated_at, last_error "
        "FROM delivery_obligations "
        "WHERE state IS NULL OR state <> 'delivered' "
        f"ORDER BY COALESCE(updated_at, created_at, 0) DESC LIMIT {_OPEN_DELIVERY_LIMIT}",
    )


def _gateway_ledger_fields(rows: _GatewayLedgerRows, now: float) -> dict[str, Any]:
    starts = rows.incarnation_starts
    counts = rows.delivery_counts
    return {
        "gateway_incarnation_count": rows.incarnation_count,
        "gateway_restarts_24h": sum(1 for start in starts if now - start <= _DAY_SECONDS),
        "current_incarnation_uptime_seconds": _age_seconds(max(starts) if starts else None, now),
        "pending_delivery_count": sum(counts.get(state) or 0 for state in _PENDING_DELIVERY_STATES),
        "failed_delivery_count": counts.get("failed") or 0,
        "pending_deliveries": [_delivery_summary(row, now) for row in rows.delivery_rows],
    }


def _delivery_summary(row: dict[str, Any], now: float) -> DeliveryObligationSummary:
    timestamp = _coerce_float(row.get("updated_at") or row.get("created_at") or 0.0)
    return DeliveryObligationSummary(
        platform=str(row.get("platform") or ""),
        state=str(row.get("state") or ""),
        attempts=_coerce_int(row.get("attempts") or 0),
        age_seconds=_age_seconds(timestamp or None, now),
        last_error=_error_excerpt(row.get("last_error") or ""),
    )


def _error_excerpt(value: object) -> str:
    """Collapse whitespace and cap an untrusted error string to a cell-sized excerpt."""
    return " ".join(str(value).split())[:_DELIVERY_ERROR_EXCERPT_CHARS]
