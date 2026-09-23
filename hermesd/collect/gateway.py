"""Gateway liveness and platform records: heartbeat, lifecycle, config generation,
updates, ledgers, and the shared-listener routing/ingress each platform entry carries.

Every reader here is pure: it takes already-loaded JSON (via the collector's
last-good file cache), an injected clock, and — where liveness depends on the
host — an injected ``pid_exists``. Nothing in this module opens a file for
writing or imports hermes-agent.
"""

from __future__ import annotations

import contextlib
import heapq
import json
import math
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
    _excerpt,
    _file_size,
    _iso_to_epoch,
    _mtime,
    _optional_int,
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
    DeadTargetSummary,
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
# Memory pressure tiers on system MemAvailable, worst first, copied from
# gateway/memory_status.py:16-26 (``critical`` doubles as the lifecycle ledger's
# OOM-suspicion heuristic). A sample older than the fresh TTL (:28-30) keeps its
# numbers but classifies as "unknown", so a dead gateway's last gasp never reads
# as a live critical.
_MEMORY_PRESSURE_TIERS = (
    ("critical", 64 * 1024, 0.05),
    ("elevated", 128 * 1024, 0.15),
)
_MEMORY_SAMPLE_FRESH_SECONDS = 150.0
# gateway/dead_targets.json: bounded display of an unbounded registry.
_DEAD_TARGET_ROW_LIMIT = 3
_DEAD_TARGET_PLATFORM_LIMIT = 8
_DEAD_TARGET_PLATFORM_CHARS = 40
_DEAD_TARGET_REASON_CHARS = 80
# gateway/restart_loop.json defaults (gateway/restart_loop_guard.py:24-33); upstream
# stores at most 50 boots, so a longer list is foreign and only its head is read.
_RESTART_LOOP_MAX_RESTARTS = 3
_RESTART_LOOP_WINDOW_SECONDS = 60
_RESTART_LOOP_MAX_GAP_SECONDS = 300
_RESTART_LOOP_BOOT_LIMIT = 200
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
# window of now. gateway_state.json's start_time is a PID-reuse fingerprint
# (``_get_process_start_time``, gateway/status.py:139-156): clock ticks since
# boot on Linux, psutil create_time in centiseconds elsewhere (178874708938,
# i.e. the year 7638 read as seconds). Neither may ever be read as an epoch.
_PLAUSIBLE_EPOCH_WINDOW_SECONDS = 50 * 365 * _DAY_SECONDS
# Outcomes that mean the update never reached a clean finish. Upstream also
# stamps a ``stop_reason`` on successful receipts, so that field alone is not
# evidence of failure and is only read alongside a missing success marker.
_UNFINISHED_OUTCOMES = frozenset({"failed", "partial", "running"})
# The receipt's fleet matrix holds one row per profile. Cap the retained state
# vocabulary so an untrusted file cannot grow the map; never cap the skew scan.
_FLEET_STATE_KIND_LIMIT = 8
# A fleet row serving a checkout this update did not touch (``EXTERNAL_STATE``,
# hermes_cli/update_receipt.py:387-392). Upstream's skew reader skips it
# (hermes_cli/update_cmd_fleet.py:224-231): another tree's SHA is not skew.
_EXTERNAL_FLEET_STATE = "external"
_EXTERNAL_ROOT_LIMIT = 4
_RECEIPT_PATH_CHARS = 120
# ``runtime_outcomes`` rows carry one of a small outcome vocabulary
# (hermes_cli/update_inventory.py:365-380); cap it like the fleet states.
_RUNTIME_OUTCOME_KIND_LIMIT = 8
_SKIP_NAME_LIMIT = 3
_SKIP_NAME_CHARS = 60
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
# Recorded gateway states that mean "live and serving" once the PID is alive:
# upstream's ``_DRAINABLE_GATEWAY_STATES`` (gateway/status.py:1207-1209).
# ``degraded`` is stamped by ``_serving_state`` when a platform is parked
# (gateway/run_startup.py:56-59) and by the out-of-loop watchdogs right before
# they hard-exit (gateway/shutdown_watchdog.py:148-163).
_SERVING_GATEWAY_STATES = frozenset({"running", "degraded"})
# ``exit_reason`` values the watchdogs stamp beside ``degraded``
# (``WATCHDOG_EXIT_REASONS``, gateway/status.py:362-364).
_WATCHDOG_EXIT_REASONS = frozenset({"loop_liveness_watchdog", "shutdown_watchdog"})


# The boot guard's reason can name several profiles and a remedy; keep the whole
# sentence but never an unbounded one from an untrusted file.
_STANDALONE_REASON_CHARS = 800


def _multiplex_standalone_reason(data: JsonMapping, *, record_current: bool) -> str:
    """The recorded standalone reason, only while its writer is live.

    Upstream prints it only for a running gateway (hermes_cli/gateway.py:1542-1549,
    5046, 5054); a dead writer's reason describes a boot that is over.
    """
    raw = data.get("multiplex_standalone_reason")
    if not record_current or not isinstance(raw, str):
        return ""
    return _excerpt(raw, _STANDALONE_REASON_CHARS)


def _claims_serving(state: str) -> bool:
    """Whether the recorded ``gateway_state`` claims a serving gateway (PID not checked)."""
    return state in _SERVING_GATEWAY_STATES


def _watchdog_exit_reason(data: JsonMapping, *, running: bool) -> str:
    """The watchdog's exit reason for a dead degraded record, else ``""``.

    Mirrors ``retained_gateway_state`` (gateway/status.py:367-385): only while the
    operator has not recorded ``desired_state: stopped``.
    """
    if running or data.get("gateway_state") != "degraded":
        return ""
    if data.get("desired_state") == "stopped":
        return ""
    reason = data.get("exit_reason")
    return reason if isinstance(reason, str) and reason in _WATCHDOG_EXIT_REASONS else ""


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
    # Only the default profile's bare entry owns the shared listener; a
    # namespaced ``<profile>:api_server`` key is never a mirror source upstream.
    mirrors = (
        {}
        if profile
        else _listener_mirror_urls(
            name,
            info,
            state,
            record_current=record_current,
            served_profiles=served,
        )
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
        error_message=_redact_secret_text(str(info.get("error_message") or "")),
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


@dataclass(frozen=True, slots=True)
class _HeartbeatMemory:
    """The heartbeat's ``mem`` block (KiB) and its pressure tier.

    ``sample_memory`` (gateway/lifecycle_ledger.py:64-74) is Linux-only and
    returns ``{}`` elsewhere, and the heartbeat writer embeds it only when
    non-empty (gateway/shutdown_watchdog.py:208-210): no block means "not
    sampled" and ``pressure`` stays empty rather than claiming "ok".
    """

    rss_kib: int | None = None
    total_kib: int | None = None
    available_kib: int | None = None
    swap_used_kib: int | None = None
    pressure: str = ""

    def as_update(self) -> dict[str, Any]:
        return {
            "memory_rss_kib": self.rss_kib,
            "memory_total_kib": self.total_kib,
            "memory_available_kib": self.available_kib,
            "memory_swap_used_kib": self.swap_used_kib,
            "memory_pressure": self.pressure,
        }


def _nonneg_kib(value: object) -> int | None:
    """A non-negative ``int`` (bools rejected), as upstream's ``_nonneg_int``."""
    return value if type(value) is int and value >= 0 else None


def _memory_pressure(available: int | None, total: int | None) -> str:
    """``classify_pressure`` (gateway/memory_status.py:50-60)."""
    if available is None:
        return "unknown"
    fraction = available / total if total else None
    for level, kib_floor, fraction_floor in _MEMORY_PRESSURE_TIERS:
        if available < kib_floor or (fraction is not None and fraction < fraction_floor):
            return level
    return "ok"


def _heartbeat_memory(data: JsonMapping, age_seconds: float | None) -> _HeartbeatMemory:
    mem = data.get("mem") if data else None
    if not isinstance(mem, dict):
        return _HeartbeatMemory()
    available = _nonneg_kib(mem.get("mem_available_kib"))
    total = _nonneg_kib(mem.get("mem_total_kib"))
    fresh = age_seconds is not None and age_seconds <= _MEMORY_SAMPLE_FRESH_SECONDS
    return _HeartbeatMemory(
        rss_kib=_nonneg_kib(mem.get("rss_kib")),
        total_kib=total,
        available_kib=available,
        swap_used_kib=_nonneg_kib(mem.get("swap_used_kib")),
        pressure=_memory_pressure(available, total) if fresh else "unknown",
    )


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
    process start-time fingerprint, not an epoch, under the same ``start_time`` key.
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
    update_fleet_external_roots: list[str] = field(default_factory=list)
    update_post_swap_pid: int | None = None
    update_pending_manual_serve_count: int = 0
    update_settled_from_live_fleet_age_seconds: float | None = None
    update_runtime_outcomes: dict[str, int] = field(default_factory=dict)
    update_skip_count: int = 0
    update_skip_names: list[str] = field(default_factory=list)

    def as_update(self) -> dict[str, Any]:
        """The ``GatewayState`` fields this receipt owns, for ``model_copy(update=...)``."""
        return {
            "last_update_outcome": self.outcome,
            "last_update_finished_age_seconds": self.finished_age_seconds,
            "last_update_from_version": self.from_version,
            "last_update_to_version": self.to_version,
            "last_update_failed_step": self.failed_step,
            "runtime_code_skew": self.runtime_code_skew,
            "runtime_code_skew_source": self.runtime_code_skew_source,
            "update_receipt_unfinished": self.update_receipt_unfinished,
            "update_fleet_states": self.update_fleet_states,
            "update_fleet_runtime_count": self.update_fleet_runtime_count,
            "update_fleet_external_roots": self.update_fleet_external_roots,
            "update_post_swap_pid": self.update_post_swap_pid,
            "update_pending_manual_serve_count": self.update_pending_manual_serve_count,
            "update_settled_from_live_fleet_age_seconds": (
                self.update_settled_from_live_fleet_age_seconds
            ),
            "update_runtime_outcomes": self.update_runtime_outcomes,
            "update_skip_count": self.update_skip_count,
            "update_skip_names": self.update_skip_names,
        }


def _update_receipt_status(data: JsonMapping, now: float, code_sha: str) -> _UpdateReceipt:
    if not data:
        return _UpdateReceipt()
    unfinished = _receipt_looks_unfinished(data)
    fleet = _as_list(data.get("fleet"))
    evidence = _skew_evidence(fleet, data, code_sha, unfinished)
    skips = _as_list(data.get("skips"))
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
        update_fleet_external_roots=_external_fleet_roots(fleet),
        # ``resume_update_receipt`` stamps the interpreter that finished the run
        # after the code swap (hermes_cli/update_receipt.py:149-155).
        update_post_swap_pid=_strict_pid(data.get("post_swap_pid")),
        # Manual serve restarts still owed when the receipt was written
        # (hermes_cli/update_receipt.py:201-203).
        update_pending_manual_serve_count=len(_as_list(data.get("pending_manual_serves"))),
        # ``settle_latest_receipt_fleet`` (hermes_cli/update_receipt.py:249-280)
        # rewrites latest.json once a later check saw the whole fleet current.
        update_settled_from_live_fleet_age_seconds=_age_seconds(
            _iso_to_epoch(_as_dict(data.get("gateway_restart")).get("settled_from_live_fleet_at")),
            now,
        ),
        update_runtime_outcomes=_capped_counts(
            _as_list(data.get("runtime_outcomes")), "outcome", _RUNTIME_OUTCOME_KIND_LIMIT
        ),
        update_skip_count=len(skips),
        update_skip_names=_skip_names(skips),
    )


def _strict_pid(value: object) -> int | None:
    """A machine-written pid: a real positive ``int`` or nothing (no bools, no strings)."""
    return value if type(value) is int and value > 0 else None


def _external_fleet_roots(fleet: list[object]) -> list[str]:
    """Distinct ``code_root`` values of external rows, bounded and redacted."""
    roots: list[str] = []
    for entry in fleet:
        info = _as_dict(entry)
        root = info.get("code_root")
        if info.get("state") != _EXTERNAL_FLEET_STATE or not isinstance(root, str) or not root:
            continue
        label = _excerpt(root, _RECEIPT_PATH_CHARS)
        if label not in roots:
            roots.append(label)
            if len(roots) >= _EXTERNAL_ROOT_LIMIT:
                break
    return roots


def _capped_counts(rows: list[object], key: str, limit: int) -> dict[str, int]:
    """Counts of ``row[key]`` over mapping rows, with the vocabulary capped at ``limit``."""
    counts: dict[str, int] = {}
    for entry in rows:
        info = _as_dict(entry)
        if not info:
            continue
        value = str(info.get(key) or "unknown")
        if value not in counts and len(counts) >= limit:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def _skip_names(skips: list[object]) -> list[str]:
    names: list[str] = []
    for entry in skips:
        name = _as_dict(entry).get("name")
        if isinstance(name, str) and name:
            names.append(_excerpt(name, _SKIP_NAME_CHARS))
            if len(names) >= _SKIP_NAME_LIMIT:
                break
    return names


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
    unfinished_outcome = isinstance(outcome, str) and outcome in _UNFINISHED_OUTCOMES
    if exit_code not in (0, None) or unfinished_outcome:
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
    """A recorded ``stale`` state is skew even when its sha was never stamped.

    An ``external`` row serves another checkout and is never skew, whatever its SHA
    (``row_is_external``, hermes_cli/update_cmd_fleet.py:224-231).
    """
    return any(
        _as_dict(entry).get("state") != _EXTERNAL_FLEET_STATE
        and (_as_dict(entry).get("state") == "stale" or _entry_sha_differs(entry, code_sha))
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

    def as_update(self) -> dict[str, Any]:
        """The model fields this readout owns, ready for ``model_copy(update=...)``.

        Named ``as_update`` rather than ``model_fields``: the latter is a
        Pydantic accessor, and a same-named method on a plain dataclass reads as
        one even though this class has no Pydantic base.
        """
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
    window = _RESTART_STORM_WINDOW_SECONDS
    if isinstance(raw_window, int | float) and not isinstance(raw_window, bool):
        with contextlib.suppress(OverflowError):
            configured_window = float(raw_window)
            if math.isfinite(configured_window) and configured_window > 0.0:
                window = configured_window
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


@dataclass(frozen=True, slots=True)
class _DashboardClientStatus:
    """Two related answers about the marker file, named rather than positional."""

    attached: bool = False
    age_seconds: float | None = None


def _dashboard_client_status(path: Path, root: Path, now: float) -> _DashboardClientStatus:
    status = _DashboardClientStatus()
    if _safe_child_path(path, root):
        age = _age_seconds(_mtime(path), now)
        if age is not None:
            status = _DashboardClientStatus(
                attached=age <= _DASHBOARD_CLIENT_ATTACHED_SECONDS,
                age_seconds=age,
            )
    return status


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
        files.append(
            ForensicFile(
                name=name,
                size_bytes=_file_size(path),
                age_seconds=_age_seconds(_mtime(path), now),
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


def _gateway_ledger_fields(
    rows: _GatewayLedgerRows, now: float, *, running: bool = True
) -> dict[str, Any]:
    starts = rows.incarnation_starts
    counts = rows.delivery_counts
    # The newest incarnation's age is only an uptime while a gateway runs; a
    # stopped gateway's last start would otherwise keep counting forever.
    newest_start = max(starts) if starts and running else None
    return {
        "gateway_incarnation_count": rows.incarnation_count,
        "gateway_restarts_24h": sum(1 for start in starts if now - start <= _DAY_SECONDS),
        "current_incarnation_uptime_seconds": _age_seconds(newest_start, now),
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
        last_error=_excerpt(row.get("last_error") or "", _DELIVERY_ERROR_EXCERPT_CHARS),
    )


# ---------------------------------------------------------------------------
# Dead delivery targets (gateway/dead_targets.json)
#
# ``DeadTargetRegistry`` (gateway/dead_targets.py:47-58) persists a mapping of
# ``platform:chat_id`` -> {platform, chat_id, reason[:200], marked_at} for chats
# confirmed unreachable; delivery short-circuits them until a send succeeds and
# ``clear`` drops the key (:75-100). The chat id is deliberately never surfaced.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DeadTargetRows:
    """Signature-cacheable facts from the registry; ages are derived per tick."""

    count: int = 0
    platforms: dict[str, int] = field(default_factory=dict)
    # (platform, reason, marked_at epoch or None), newest first.
    newest: list[tuple[str, str, float | None]] = field(default_factory=list)


def _dead_target_rows(data: JsonMapping) -> _DeadTargetRows:
    # Upstream keeps every mapping value, empty or not (dead_targets.py:55-57).
    entries = [value for value in data.values() if isinstance(value, dict)]
    platforms: dict[str, int] = {}
    for info in entries:
        platform = _dead_target_platform(info)
        if platform not in platforms and len(platforms) >= _DEAD_TARGET_PLATFORM_LIMIT:
            continue
        platforms[platform] = platforms.get(platform, 0) + 1
    stamped = [(info, _dead_target_marked_at(info)) for info in entries]
    newest = heapq.nlargest(
        _DEAD_TARGET_ROW_LIMIT,
        stamped,
        key=lambda item: item[1] if item[1] is not None else -math.inf,
    )
    return _DeadTargetRows(
        count=len(entries),
        platforms=platforms,
        newest=[
            (
                _dead_target_platform(info),
                _excerpt(info.get("reason") or "", _DEAD_TARGET_REASON_CHARS),
                marked_at,
            )
            for info, marked_at in newest
        ],
    )


def _dead_target_platform(info: dict[str, Any]) -> str:
    platform = info.get("platform")
    return (
        _excerpt(platform, _DEAD_TARGET_PLATFORM_CHARS)
        if isinstance(platform, str) and platform
        else "unknown"
    )


def _dead_target_marked_at(info: dict[str, Any]) -> float | None:
    raw = info.get("marked_at")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    return value if math.isfinite(value) and value > 0 else None


def _dead_target_fields(rows: _DeadTargetRows, now: float) -> dict[str, Any]:
    return {
        "dead_target_count": rows.count,
        "dead_target_platforms": rows.platforms,
        "dead_targets": [
            DeadTargetSummary(
                platform=platform, reason=reason, age_seconds=_age_seconds(marked_at, now)
            )
            for platform, reason, marked_at in rows.newest
        ],
    }


# ---------------------------------------------------------------------------
# Restart-loop breaker (gateway/restart_loop.json)
#
# ``restart_loop_guard`` (gateway/restart_loop_guard.py) records one epoch per
# boot that found restart-interrupted sessions; boots CHAIN while consecutive
# gaps stay within ``max(1, window_seconds, max_gap_seconds)`` (:56-59, :62-77),
# and a chain of ``max_restarts`` trips the breaker, which skips auto-resume for
# that boot (:88-105). The file is written with a plain ``write_text`` (:50-54),
# so a torn read is possible and falls back to last-good.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RestartLoopPolicy:
    max_restarts: int = _RESTART_LOOP_MAX_RESTARTS
    window_seconds: int = _RESTART_LOOP_WINDOW_SECONDS
    max_gap_seconds: int = _RESTART_LOOP_MAX_GAP_SECONDS

    @property
    def chain_gap(self) -> float:
        """``_chain_gap`` (restart_loop_guard.py:56-59)."""
        return float(max(1, self.window_seconds, self.max_gap_seconds))


def _restart_loop_policy(cfg: JsonMapping) -> _RestartLoopPolicy:
    """``gateway.restart_loop_guard`` as ``_restart_loop_guard_config`` reads it.

    Mirrors gateway/run_shutdown.py:344-363: an ``int`` value is used (any value for
    ``max_restarts``, where ``<= 0`` disables the breaker; only positive ones for
    the two windows), anything else keeps the defaults (restart_loop_guard.py:24-31;
    DEFAULT_CONFIG hermes_cli/config_defaults.py:2152).
    """
    section = _as_dict(_as_dict(cfg.get("gateway")).get("restart_loop_guard"))

    def int_or(key: str, default: int, *, positive: bool) -> int:
        value = section.get(key)
        if isinstance(value, int) and (value > 0 or not positive):
            return int(value)
        return default

    return _RestartLoopPolicy(
        max_restarts=int_or("max_restarts", _RESTART_LOOP_MAX_RESTARTS, positive=False),
        window_seconds=int_or("window_seconds", _RESTART_LOOP_WINDOW_SECONDS, positive=True),
        max_gap_seconds=int_or("max_gap_seconds", _RESTART_LOOP_MAX_GAP_SECONDS, positive=True),
    )


def _restart_loop_boots(data: JsonMapping) -> list[float]:
    """Recorded boot epochs; junk entries are dropped like ``_load_boots`` does."""
    boots: list[float] = []
    for raw in _as_list(data.get("boots"))[:_RESTART_LOOP_BOOT_LIMIT]:
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            continue
        value = float(raw)
        if math.isfinite(value):
            boots.append(value)
    return boots


def _restart_loop_chain(boots: list[float], now: float, gap: float) -> int:
    """Length of ``_chain_ending_at(boots, now, gap)`` (restart_loop_guard.py:62-77).

    A future boot (clock stepped back) is adjacent, not a break; the first gap
    wider than ``gap`` walking back from now ends the chain, so a loop that went
    quiet is forgotten exactly as upstream forgets it.
    """
    chain = 0
    previous = now
    for boot in sorted(boots, reverse=True):
        if boot > now:
            chain += 1
            continue
        if previous - boot > gap:
            break
        chain += 1
        previous = boot
    return chain


def _restart_loop_fields(
    data: JsonMapping, now: float, policy: _RestartLoopPolicy
) -> dict[str, Any]:
    boots = _restart_loop_boots(data)
    chain = _restart_loop_chain(boots, now, policy.chain_gap)
    return {
        "restart_loop_boots_recorded": len(boots),
        "restart_loop_chain": chain,
        "restart_loop_max_restarts": policy.max_restarts,
        "restart_loop_chain_gap_seconds": policy.chain_gap,
        # ``is_restart_loop_tripped`` (restart_loop_guard.py:117-140): the verdict
        # the next restart-interrupted boot would inherit, evaluated at now.
        "restart_loop_tripped": policy.max_restarts > 0 and chain >= policy.max_restarts,
        "restart_loop_last_boot_age_seconds": _age_seconds(max(boots), now) if boots else None,
    }
