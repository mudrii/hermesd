from __future__ import annotations

import math
import re
import time
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from hermesd.paths import default_hermes_home

# cost_status values the hermes-agent producer treats as authoritative (an actual
# billed/known cost, not a token-based estimate): "reported" (legacy), "exact",
# and "included" (subscription-covered, genuinely $0.00).
AUTHORITATIVE_COST_STATUSES: frozenset[str] = frozenset({"reported", "exact", "included"})

# Plugin manifest filenames hermesd recognises, in upstream's precedence order:
# a native YAML manifest wins over a portable one, and plugin.yaml wins over
# plugin.yml (hermes_cli/plugins_discovery.py:115, hermes_cli/plugin_dev.py:154,
# hermes_cli/plugins_cmd.py:265-271).
YAML_MANIFEST_NAMES: tuple[str, ...] = ("plugin.yaml", "plugin.yml")
PORTABLE_MANIFEST_NAME: str = "plugin.json"
MANIFEST_NAMES: tuple[str, ...] = (*YAML_MANIFEST_NAMES, PORTABLE_MANIFEST_NAME)

# A git commit SHA the way hermes-agent records one: `git rev-parse HEAD`,
# stripped and lowercased (hermes_cli/plugins_cmd.py:489-494). A provenance value
# that does not match is never rendered as a revision — a corrupt sidecar is not
# a pin, and comparing garbage against a real SHA would fabricate drift.
_FULL_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# Slack allowed between an active-session lease's ``started_at`` and
# ``updated_at`` ages before hermesd calls it transferred. Upstream writes both
# from separate ``time.time()`` calls in one acquisition
# (``hermes_cli/active_sessions.py:426-433``), so an exactly-equal pair can still
# differ by a fraction of a second once rounded against the injected clock.
_LEASE_TRANSFER_TOLERANCE_SECONDS = 1.0
# A routing entry's durable turn token is CAS-cleared on normal unwind and left
# behind by SIGKILL/OOM (gateway/session.py:515-518), so a token older than this
# grace reads as "turn never unwound" — a crash marker, not a slow turn.
_TURN_UNWIND_GRACE_SECONDS = 300

# Cross-restart schema-repair budget hermes-agent refuses to exceed on one
# damaged file: ``_MAX_PERSISTENT_REPAIR_ATTEMPTS`` (``hermes_state_repair.py:48``).
# Copied as a constant, never imported — hermesd reads hermes-agent's files and
# never its code. Only ``SQLITE_CORRUPT``/``SQLITE_NOTADB`` failures burn it
# (``_repair_failure_consumes_attempt``, ``:302-314``); locks, disk-full and I/O
# errors deliberately do not.
MAX_PERSISTENT_REPAIR_ATTEMPTS: int = 3


def _remaining_seconds(deadline: float | None, now: float) -> float | None:
    """Seconds until ``deadline`` at ``now``; None when it is disarmed or past.

    A non-positive deadline is *disarmed*, not an epoch: upstream writes ``0``
    for "not armed" (``set_compression_recovery_deadline`` stores
    ``normalized or None``, ``hermes_state_compression.py:413-431``), and
    treating ``0`` as a timestamp would render as January 1970. A non-finite
    value coerced out of an untyped SQLite column is likewise no deadline.
    """
    if deadline is None or not math.isfinite(deadline) or deadline <= 0.0 or deadline <= now:
        return None
    return deadline - now


class GatewayLoopHealth(StrEnum):
    """Event-loop liveness for the gateway, heartbeat age refined by the loop-tick witness.

    The heartbeat file is rewritten off-loop since #90502, so freshness alone no longer
    proves the loop dispatches. When the heartbeat advertises a witness
    (``loop_tick_socket``), one probe of that witness is direct evidence and the
    verdict upgrades to :attr:`ALIVE` (answered) or escalates to :attr:`WEDGED`
    (stale heartbeat plus witness silence sustained across refreshes). A stale
    heartbeat from a writer that predates the witness key is :attr:`LEGACY`:
    an on-loop writer, so staleness alone is proof.
    """

    ALIVE = "alive"
    TICKING = "ticking"
    STALE = "stale"
    WEDGED = "wedged"
    # Stale heartbeat from a gateway old enough to predate ``loop_tick_socket``:
    # the heartbeat was written on-loop, so its age is direct (absence of) evidence.
    LEGACY = "legacy"
    UNKNOWN = "unknown"


class ForensicFile(BaseModel):
    """An event-only diagnostics file: presence metadata, contents never read.

    Upstream appends to these only on signal shutdowns, freeze dumps or supervisor
    reloads, and nothing prunes them, so absence is the healthy state and growth
    is the signal worth surfacing.
    """

    name: str
    size_bytes: int = 0
    age_seconds: float | None = None


class PlatformOwnership(StrEnum):
    """Whether a platform record still belongs to the gateway that writes the file.

    ``gateway_state.json`` re-stamps its top-level ``pid``/``start_time`` on every
    write, so those describe the most recent writer, while each platform entry
    keeps the identity of the process that recorded it. Ownership is therefore
    independent of heartbeat freshness: a ticking loop can still be serving a
    platform entry preserved from an earlier life.
    """

    CURRENT = "current"
    PRESERVED = "preserved"
    UNVERIFIABLE = "unverifiable"


class PlatformStatus(BaseModel):
    name: str
    # Profile segment of a grammar-valid ``<profile>:<platform>`` status key —
    # how the multiplexer keys a served profile's adapter
    # (``gateway/run_adapters.py:1048``). Empty for a plain platform key, and
    # empty for a namespaced key that failed upstream's key grammar
    # (``hermes_cli/web_routers/status.py:122-136``), which stays verbatim in
    # ``name``: splitting an arbitrary key would project it onto a profile.
    profile: str = ""
    # Shared-listener ingress URL as recorded by the gateway, already redacted
    # and already suppressed where upstream suppresses it. Recorded information,
    # never a probe result: hermesd does not request these URLs.
    ingress_url: str = ""
    state: str = "unknown"
    updated_at: str = ""
    error_code: str = ""
    error_message: str = ""
    needs_attention: bool = False
    retrying_since: str = ""
    retrying_since_age_seconds: float | None = None
    # Per-served-profile mirror URLs synthesized from this port-binder's
    # listener_base (``<listener_base>/p/<profile><mirror_path>``,
    # gateway/status.py:951-974), where a client reaches the profile on the
    # default listener. Empty unless the writer is live and the adapter serves.
    mirror_urls: dict[str, str] = Field(default_factory=dict)
    # The synthesized roster is sliced to a display bound, so a short list must
    # not read as the complete set of served profiles.
    mirror_urls_truncated: bool = False
    # Per-entry writer provenance. Absent on a gateway that predates the stamps,
    # which is why ownership defaults to unverifiable rather than current.
    writer_pid: int | None = None
    writer_start_time: int | None = None
    ownership: PlatformOwnership = PlatformOwnership.UNVERIFIABLE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ingress_url_is_path_only(self) -> bool:
        """A recorded bare path rather than a URL.

        ``publish_shared_ingress`` records ``f"{base}/p/{profile}{ingress_path}"``
        when the default profile has a live listener, and a bare
        ``f"/p/{profile}{ingress_path}"`` when it does not
        (``gateway/platforms/shared_ingress.py:72-95``). The bare form names a
        route that had no host to be reached at when it was recorded.
        """
        return bool(self.ingress_url) and "://" not in self.ingress_url


class ConfigSourceStamp(BaseModel):
    """One config file as recorded in the running gateway's config generation."""

    name: str = ""
    path: str = ""
    exists: bool = False
    mtime_ns: int = 0
    size: int = 0


class DeliveryObligationSummary(BaseModel):
    """A queued outbound delivery. Message content is deliberately never read."""

    platform: str = ""
    state: str = ""
    attempts: int = 0
    age_seconds: float | None = None
    last_error: str = ""


class GatewayState(BaseModel):
    pid: int = 0
    running: bool = False
    state: str = "unknown"
    platforms: list[PlatformStatus] = Field(default_factory=list)
    hermes_version: str = ""
    updates_behind: int = 0
    active_agents: int = 0
    restart_requested: bool = False
    busy: bool = False
    drainable: bool = False
    drain_active: bool = False
    drain_requested_at: str = ""
    drain_principal: str = ""
    drain_suppress_notification: bool = False
    served_profiles: list[str] = Field(default_factory=list)
    # Tri-state marker over ``served_profiles``, mirroring upstream's
    # ``recorded_served_profiles()`` (``hermes_cli/gateway_multiplex_served.py:28-38``):
    # True only when the file carries a real list *and* the gateway that recorded
    # it is live. An absent key, an unparseable value and a dead writer all leave
    # it False, so an authoritative "serves nobody else" (a live ``[]``) stays
    # distinguishable from "no live record" — while ``served_profiles`` still keeps
    # the names a dead gateway left behind, as preserved rather than current.
    served_profiles_recorded: bool = False
    scale_to_zero_idle_timeout_minutes: int = 0
    scale_to_zero_relay_only: bool = False
    # Event-loop liveness (state/gateway.heartbeat)
    heartbeat_age_seconds: float | None = None
    loop_health: GatewayLoopHealth = GatewayLoopHealth.UNKNOWN
    # Witness armed on the heartbeat that produced loop_health: True when the
    # payload advertised ``loop_tick_socket`` truthy, False when it wrote the key
    # with any other value (the witness could not be armed), and None when the
    # payload predates the key (a legacy on-loop writer) or no verdict was made.
    loop_tick_armed: bool | None = None
    # Lifecycle (state/gateway.lifecycle.json)
    lifecycle_phase: str = ""
    last_exit_code: int | None = None
    last_exit_reason: str = ""
    unclean_previous_exit: bool = False
    # Carry flags from the running record (gateway/lifecycle_ledger.py record_startup):
    # the verdict on the *previous* life, stamped onto this life's sentinel. Strict
    # booleans upstream; any other value means the key was never really written.
    prior_unclean_exit: bool = False
    prior_suspected_oom: bool = False
    # Respawn-storm ledger (gateway-starts.log). The file records one epoch per
    # start; an absent file is NOT evidence of zero restarts, because
    # HERMES_GATEWAY_MAX_STARTS<=0 disables the writer. ``window`` counts starts
    # inside the configured ``gateway.respawn_storm.window_seconds`` (120 s by
    # default), which is also what ``in_respawn_backoff`` compares against the cap.
    gateway_starts_recorded: bool = False
    gateway_starts_window: int = 0
    gateway_starts_1h: int = 0
    restart_storm_cap: int = 0
    restart_storm_window_seconds: float = 0.0
    seconds_since_last_gateway_start: float | None = None
    in_respawn_backoff: bool = False
    # Exit diagnostics ledger (logs/gateway-exit-diag.log): one JSON object per
    # asyncio.run() return path plus one gateway.previous_unclean_exit per unclean
    # boot. Nothing prunes it upstream; HERMES_GATEWAY_EXIT_DIAG=0 disables the
    # writer, so an absent file is no evidence of clean exits.
    exit_diag_recorded: bool = False
    exit_diag_last_tag: str = ""
    exit_diag_last_age_seconds: float | None = None
    exit_diag_unclean_24h: int = 0
    exit_diag_size_bytes: int = 0
    exit_diag_oversized: bool = False
    # Event-only companion logs (shutdown blocks, freeze dumps, supervisor
    # reloads): metadata only, present files only.
    forensic_files: list[ForensicFile] = Field(default_factory=list)
    # Web dashboard client attachment (state/dashboard_clients.heartbeat): a
    # 0-byte marker whose mtime is the whole payload. Absent means never, not idle.
    dashboard_client_attached: bool = False
    dashboard_client_last_frame_age_seconds: float | None = None
    # Code and config identity (gateway_state.json)
    code_sha: str = ""
    code_version: str = ""
    config_fingerprint: str = ""
    config_generation_short: str = ""
    config_sources: list[ConfigSourceStamp] = Field(default_factory=list)
    config_stale: bool = False
    session_store_status: str = ""
    exit_reason: str = ""
    # Update receipts (logs/update_receipts/latest.json)
    last_update_outcome: str = ""
    last_update_finished_age_seconds: float | None = None
    last_update_from_version: str = ""
    last_update_to_version: str = ""
    last_update_failed_step: str = ""
    runtime_code_skew: bool = False
    # Which recorded evidence decided skew: "fleet" (post-restart matrix),
    # "plan" (pre-update inventory, only for a run that never finished), or ""
    # when skew was not assessable at all.
    runtime_code_skew_source: str = ""
    update_receipt_unfinished: bool = False
    update_fleet_states: dict[str, int] = Field(default_factory=dict)
    update_fleet_runtime_count: int = 0
    # Restart history and delivery obligations (state.db)
    gateway_incarnation_count: int = 0
    gateway_restarts_24h: int = 0
    current_incarnation_uptime_seconds: float | None = None
    pending_delivery_count: int = 0
    failed_delivery_count: int = 0
    pending_deliveries: list[DeliveryObligationSummary] = Field(default_factory=list)


class MigrationVerificationGap(StrEnum):
    """Which clause of the verified predicate hermesd could not satisfy.

    ``NONE`` is the only value that licenses a "multiplexed (verified)" claim.
    Every other value names *missing evidence*, never an outcome: upstream writes
    the manifest before it flips the multiplex flag and restarts the default
    gateway, and never updates it afterwards, so the file cannot tell a migration
    still in flight from one that was applied and never verified — and neither can
    hermesd. ``NO_MANIFEST`` is likewise ambiguous: rollback deletes the manifest on
    success, so absence means "never migrated OR successfully rolled back".
    """

    NONE = ""
    NO_MANIFEST = "no_manifest"
    MANIFEST_UNREADABLE = "manifest_unreadable"
    MANIFEST_INVALID = "manifest_invalid"
    FLAG_OFF = "flag_off"
    GATEWAY_NOT_LIVE = "gateway_not_live"
    SERVED_NOT_RECORDED = "served_not_recorded"
    PROFILES_UNSERVED = "profiles_unserved"
    SECONDARIES_TRUNCATED = "secondaries_truncated"


class MigrationProfileRecord(BaseModel):
    """One profile's standalone-gateway footprint as recorded in the manifest.

    ``home`` is display data that hermesd never resolves: upstream's rollback builds
    ``Path(rec["home"])`` straight from this file (``gateway_migrate.py:594``), and
    an untrusted manifest must not be able to steer a hermesd read. ``served`` is
    coverage by the *live* default gateway's recorded ``served_profiles``, so it is
    always False when nothing live was recorded.
    """

    profile: str = ""
    home: str = ""
    service_kind: str = ""
    service_system: bool = False
    served: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def service_label(self) -> str:
        """Upstream's own ``ProfileGateway.service_label()`` wording (``:48-52``)."""
        if not self.service_kind:
            return "none"
        if self.service_kind == "systemd":
            return f"systemd ({'system' if self.service_system else 'user'})"
        return self.service_kind


class MigrationState(BaseModel):
    """``gateway_migration.json``: recorded intent, progress, and one verified verdict.

    Three things are kept apart on purpose, because the manifest conflates them:

    * **recorded intent** — the manifest contents. ``migrated_at`` means "the attempt
      began at": upstream builds the dict and writes it *inside* the per-secondary
      loop (``gateway_migrate.py:528-544``), before ``_write_multiplex_flag``
      (``:545``) and before ``_restart_default`` (``:549``), rewrites it
      byte-identically at ``:546``, and then never touches it again on either the
      verified (``:553-557``) or the unverified (``:558-561``) path. There is no
      ``completed``/``verified``/``outcome`` field, so the file is a start marker.
    * **intermediate progress** — ``flag_flipped``, ``default_gateway_live``,
      ``served_recorded`` and each record's ``served``: re-read every pass, and each
      one true of a migration that crashed halfway.
    * **verified current topology** — ``migration_verified``, derived from a
      predicate over artifacts hermesd can actually read.

    ``multiplex_flag_on`` mirrors upstream's *reader* (``:203-215``), which ORs a
    stale top-level ``multiplex_profiles`` alias with ``gateway.multiplex_profiles``
    after an environment override hermesd cannot see — so every verdict built on it
    is labelled "as recorded in config".

    Known limit: upstream verifies against every profile in its plan
    (``expected = {p.name for p in plan.profiles}``, ``:551-552``), which includes
    profiles that never had a standalone gateway and so are *not* in the manifest.
    hermesd can only see the manifest, so its expected set is a subset of upstream's
    and its verdict is correspondingly weaker.
    """

    # Recorded intent (the manifest, never rewritten after the attempt began)
    manifest_present: bool = False
    # A present manifest hermesd could not parse: upstream writes it with a plain
    # write_text, so a torn file is observable mid-write. Distinct from absent.
    manifest_parsed: bool = False
    # Parsed JSON can still be an unsupported or malformed migration schema.
    # Its intent remains displayable, but it cannot license a verified verdict.
    manifest_schema_valid: bool = False
    manifest_version: int = 0
    migrated_at: str = ""
    migrated_at_age_seconds: float | None = None
    flag_was: bool = False
    default_profile: MigrationProfileRecord = Field(default_factory=MigrationProfileRecord)
    secondaries: list[MigrationProfileRecord] = Field(default_factory=list)
    secondary_count: int = 0
    secondaries_truncated: bool = False
    # Intermediate progress, re-read every pass
    multiplex_flag_on: bool = False
    default_gateway_live: bool = False
    served_recorded: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def flag_flipped(self) -> bool:
        """Progress, not success: the config now differs from the recorded prior value."""
        return (
            self.manifest_parsed
            and self.manifest_schema_valid
            and self.multiplex_flag_on != self.flag_was
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unserved_profiles(self) -> list[str]:
        """Recorded profiles the live served set does not cover, default first.

        The expected set is ``{"default"} | {manifest secondaries}``: upstream always
        names the default profile ``default`` (``ProfileGateway.is_default``), and
        ``_record_served_profiles`` puts the active profile at index 0.
        """
        missing = [] if self.default_profile.served else ["default"]
        missing.extend(record.profile or "?" for record in self.secondaries if not record.served)
        return missing

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verification_gap(self) -> MigrationVerificationGap:
        """The first clause of the predicate that hermesd-readable artifacts refute."""
        if not self.manifest_present:
            return MigrationVerificationGap.NO_MANIFEST
        if not self.manifest_parsed:
            return MigrationVerificationGap.MANIFEST_UNREADABLE
        if not self.manifest_schema_valid:
            return MigrationVerificationGap.MANIFEST_INVALID
        if not self.multiplex_flag_on:
            return MigrationVerificationGap.FLAG_OFF
        if not self.default_gateway_live:
            return MigrationVerificationGap.GATEWAY_NOT_LIVE
        if not self.served_recorded:
            return MigrationVerificationGap.SERVED_NOT_RECORDED
        if self.secondaries_truncated:
            return MigrationVerificationGap.SECONDARIES_TRUNCATED
        if self.unserved_profiles:
            return MigrationVerificationGap.PROFILES_UNSERVED
        return MigrationVerificationGap.NONE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def migration_verified(self) -> bool:
        """Derived, never stored: the gap is the single source of truth."""
        return self.verification_gap is MigrationVerificationGap.NONE


class SessionInfo(BaseModel):
    session_id: str
    source: str = ""
    model: str = ""
    parent_session_id: str = ""
    billing_provider: str = ""
    billing_base_url: str = ""
    billing_mode: str = ""
    end_reason: str = ""
    context_limit: int = 0
    cost_status: str = ""
    pricing_version: str = ""
    message_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0
    api_call_count: int = 0
    cwd: str = ""
    archived: bool = False
    rewind_count: int = 0
    handoff_state: str = ""
    handoff_platform: str = ""
    handoff_error: str = ""
    started_at: float = 0.0
    ended_at: float | None = None
    title: str | None = None
    is_active: bool = False
    # hermes-agent 0.21 columns; absent on older databases (defaults apply).
    git_branch: str = ""
    chat_type: str = ""
    display_name: str = ""
    title_source: str = ""
    profile_name: str = ""
    pinned: bool = False
    last_activity_at: float = 0.0
    last_activity_description: str = ""
    actual_cost_usd: float = 0.0
    cost_source: str = ""
    compression_failure_error: str = ""
    # The durable half of the compressor's anti-thrash guard
    # (hermes_state_common.py:375-379). Counters and timestamps only: nothing
    # here is derived from conversation content, which hermesd never reads.
    # Both deadlines are None when the column is NULL *or* 0, because upstream
    # stores 0 to mean "disarmed" and 0 rendered as an epoch reads as 1970.
    compression_failure_cooldown_until: float | None = None
    compression_fallback_streak: int = 0
    compression_ineffective_count: int = 0
    compression_recovery_deadline: float | None = None

    def compression_cooldown_remaining(self, now: float) -> float | None:
        """Seconds of live compression-failure cooldown at ``now``, else None.

        Mirrors ``get_compression_failure_cooldown``
        (``hermes_state_compression.py:329-335``), which reports a cooldown only
        while ``cooldown_until > now`` — an expired one is cleared state, not a
        warning.
        """
        return _remaining_seconds(self.compression_failure_cooldown_until, now)

    def compression_recovery_remaining(self, now: float) -> float | None:
        """Seconds until the durable anti-thrash probe at ``now``, else None.

        ``context_compressor`` treats ``deadline <= 0.0`` as unarmed (``:2558``)
        and re-probes once ``now >= deadline`` (``:2563``), so both ends are
        "not active" here.
        """
        return _remaining_seconds(self.compression_recovery_deadline, now)

    def compression_recovery_active(self, now: float) -> bool:
        """Derived: a live cooldown and/or a future recovery deadline.

        A parameterized method rather than a ``computed_field`` because the
        answer is clock-relative, and the clock is injected: the sessions source
        is memoized on row identity, so baking a countdown into the model would
        freeze it until the database next changed.
        """
        return (
            self.compression_cooldown_remaining(now) is not None
            or self.compression_recovery_remaining(now) is not None
        )


class ProcessLiveness(StrEnum):
    """Whether a registry entry is still backed by the process it names.

    ``UNVERIFIABLE`` is a first-class answer, not a fallback: an existing pid
    proves nothing about identity once pids are reused, and a start time is not
    always observable (no psutil dependency, an unreadable ``/proc``, or a
    registry that recorded none).
    """

    LIVE = "live"
    DEAD = "dead"
    UNVERIFIABLE = "unverifiable"


class SessionLeaseKind(StrEnum):
    """Which state.db coordination row a :class:`SessionLease` summarizes.

    A *turn lease* is keyed by the conversation lineage root and serializes
    turns across processes (``hermes_state_compression.py:519-536``); a
    *compression lock* is keyed by the exact session id and only blocks other
    compressions (``:451-474``).
    """

    TURN_LEASE = "turn_lease"
    COMPRESSION_LOCK = "compression_lock"


class SessionLease(BaseModel):
    """One ``session_turn_leases`` / ``compression_locks`` row.

    Both tables share the shape ``(key, holder, acquired_at, expires_at)``
    (``hermes_state_common.py:506-518``) and the same default 300s TTL; there
    is no background sweeper upstream, so a row disappears only when its holder
    releases it or another acquirer reclaims it.

    Liveness mirrors upstream's reclaim rule
    (``hermes_state.py:119-143``): a holder is *dead* only on kernel proof its
    ``pid=`` is gone; an unparseable holder (no ``pid=``) is unverifiable and
    keeps its lease until TTL, and a recycled pid reads as alive — the same
    conservatism as the active-surface checks.
    """

    kind: SessionLeaseKind = SessionLeaseKind.TURN_LEASE
    # conversation lineage root (turn lease) or exact session id (lock)
    key: str = ""
    holder: str = ""
    pid: int = 0
    # Ages against the injected clock, like every other cached-readout value.
    held_seconds: float | None = None
    # expires_at - now; negative means the TTL has already elapsed.
    expires_in_seconds: float | None = None
    expired: bool = False
    liveness: ProcessLiveness = ProcessLiveness.UNVERIFIABLE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def orphaned(self) -> bool:
        """Derived: the holder's pid is provably gone (upstream would reclaim)."""
        return self.liveness is ProcessLiveness.DEAD


class GatewayHygieneState(BaseModel):
    """One ``gateway_hygiene_state`` row: per-chat session-hygiene failures.

    Writers increment the streak per failed hygiene run
    (``hermes_state_gateway.py:513-529``); the consumer escalates a cooldown
    ladder x1/x3/x9 over the default 300s base (``hygiene_failure_cooldown_seconds``),
    clamped at 3600s
    (``gateway/run.py:101-149``), so a streak of 3+ effectively disables
    pre-turn compaction for up to an hour. Rows are deleted only when a
    compression actually recovers the chat (``gateway/run.py:152-167``).
    """

    session_key: str = ""
    failure_streak: int = 0
    # streak >= 3: compaction effectively suspended
    suspended: bool = False
    # Joined from the session row's compression_failure_error ("" when no
    # matching session row or no recorded error): the why behind the streak.
    compression_failure_error: str = ""


class GatewayRouteState(BaseModel):
    """Decoded ``gateway_routing.entry_json`` (one ``SessionEntry.to_dict()``,
    ``gateway/session.py:535-545``) for one routed chat.

    ``entry_json`` also carries token counters and Slack watermarks; only the
    state flags below are extracted, and ``display_name`` is redacted before it
    reaches this model. ``turn_never_unwound`` is the crash marker: a durable
    turn token is CAS-cleared on normal unwind and left behind by SIGKILL/OOM
    (``gateway/session.py:515-518``), so a token older than a few minutes means
    the turn died mid-flight.
    """

    session_key: str = ""
    session_id: str = ""
    platform: str = ""
    chat_type: str = ""
    display_name: str = ""
    updated_at_age_seconds: float | None = None
    suspended: bool = False
    resume_pending: bool = False
    resume_reason: str = ""
    was_auto_reset: bool = False
    auto_reset_reason: str = ""
    # Age of active_turn_started_at; None when the entry carries no token.
    turn_age_seconds: float | None = None
    # The routed session id has no row in sessions: the gateway would try to
    # resume a nonexistent id.
    dangling: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def turn_never_unwound(self) -> bool:
        """Derived: a turn token older than the unwind grace — a crash marker."""
        return (
            self.turn_age_seconds is not None and self.turn_age_seconds > _TURN_UNWIND_GRACE_SECONDS
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_user_message(self) -> bool:
        """Derived: suspended or resume-pending chats recover only on the next
        inbound user message (a resume-pending chat auto-continues the *same*
        transcript, but both sit idle until the chat speaks again)."""
        return self.suspended or self.resume_pending


class ConversationGeneration(BaseModel):
    """One ``conversation_generations`` row: the monotonic reset counter for a
    routing peer (``hermes_state_common.py:460-487``).

    The table is deliberately never garbage-collected, so hermesd treats a
    shrinking row count between refreshes as an invariant break and raises a
    panel warning (``SessionCoordinationState.generation_count_shrank``).
    """

    source: str = ""
    session_key: str = ""
    generation: int = 0


class TerminalBreadcrumb(BaseModel):
    """One terminal breadcrumb under ``terminal-sessions/``.

    Upstream records ``{"session_id", "cwd", "ts"}`` per terminal identity so
    ``hermes -c`` can find that terminal's session
    (``hermes_cli/terminal_breadcrumbs.py:76-94``). One file per terminal, best
    effort: the file count is only an *upper bound* on live terminals.
    """

    terminal: str = ""
    session_id: str = ""
    cwd: str = ""
    age_seconds: float | None = None


class TerminalSessionReadout(BaseModel):
    """Bounded recent terminal breadcrumbs plus the 24h file count.

    ``count`` covers every breadcrumb within the 24-hour window even when
    ``sessions`` is truncated to the newest rows. When the directory itself
    exceeded the scan bound, ``truncated`` marks ``count`` as a lower bound and
    the panel says so rather than presenting a partial scan as the total.
    """

    sessions: list[TerminalBreadcrumb] = Field(default_factory=list)
    count: int = 0
    truncated: bool = False


class SessionCoordinationState(BaseModel):
    """Gateway/lease coordination state beside the session table.

    Four independently-failing sources write nested groups here:
    ``session_leases`` (turn leases + compression locks), ``gateway_hygiene``
    (per-chat hygiene failure streaks), ``gateway_routes`` (decoded routing
    entries) and ``generation_churn`` (conversation generations). All read the
    selected profile's ``state.db``; totals are exact row counts while the row
    lists are capped, so a panel must never present a capped list as the count.
    """

    leases: list[SessionLease] = Field(default_factory=list)
    lease_total: int = 0
    hygiene: list[GatewayHygieneState] = Field(default_factory=list)
    # Exact count of chats with a non-zero failure streak; ``hygiene`` is capped.
    hygiene_total: int = 0
    routes: list[GatewayRouteState] = Field(default_factory=list)
    route_total: int = 0
    generations: list[ConversationGeneration] = Field(default_factory=list)
    # One row per (source, session_key) peer.
    generation_chat_total: int = 0
    # Sum of generations: lifetime reset boundaries ever written upstream.
    generation_reset_total: int = 0
    # The never-prune invariant broke: the row count shrank between refreshes.
    generation_count_shrank: bool = False


class ActiveSurface(BaseModel):
    """One lease in ``runtime/active_sessions.json``.

    A lease is a *slot*, not a process and not a running turn. Upstream records
    one per attached surface (``_lease_entry``,
    ``hermes_cli/active_sessions.py:421-439``), and several entries may name the
    same pid — one process can hold leases for multiple sessions — so the entry
    count is registry occupancy and never a process count.

    There is no expiry and no renewal field: a lease lives until it is released
    (``release_active_session``, ``:536-551``) or pruned because its owner is
    provably dead (``_prune_dead``, ``:348-360``). ``updated_at`` moves past
    ``started_at`` only when ``transfer_active_session`` (``:554-601``) moves the
    lease to another session id.

    Scope note: hermesd reads the *selected profile's* registry, while upstream's
    orphan sweep covers the root home and every profile home
    (``release_orphaned_leases``, ``:660-687``). Occupancy here is therefore one
    registry's, not the install's. See ``.codex/rules/source-ownership.md``.
    """

    session_id: str = ""
    surface: str = ""
    pid: int = 0
    # Epoch seconds, as this registry records them. gateway_state.json stores
    # centiseconds instead; the two must never be compared directly.
    process_start_time: float | None = None
    liveness: ProcessLiveness = ProcessLiveness.UNVERIFIABLE
    # uuid4 hex as upstream mints it. Display data only: hermesd never uses it to
    # decide ownership (that is upstream's ``(pid, metadata.live_session_id)``
    # test, ``_is_same_writer`` ``:136-148``).
    lease_id: str = ""
    # Ages of ``started_at``/``updated_at`` against the injected clock, never the
    # stored epochs: an age is wall-clock-relative and must not be frozen into an
    # mtime-keyed cache (see the StateDbRead docstring rule in
    # ``hermesd/collect/operations.py``). None when the stamp is absent or is not
    # the epoch float upstream writes.
    started_at_age_seconds: float | None = None
    updated_at_age_seconds: float | None = None
    # Upstream sets this only for desktop-surface leases
    # (``tui_gateway/session_lifecycle.py:33-38``); it asks the registry to raise
    # rather than warn when it cannot prove liveness.
    track_liveness: bool = False
    # The lease advertises cooperative attach: metadata.shared_runtime_url is
    # present (``hermes_cli/shared_session_attach.py:32-47`` — presence is the
    # whole signal; the handshake is HTTP and hermesd never probes it). The URL
    # itself is deliberately not stored: only gateway surfaces that advertise it
    # show the joinable chip. ``surface`` is carried verbatim, never
    # allowlisted, so gateway leases recording "gateway:<platform>"
    # (``gateway/run_busy.py:187``) and bot delivery consumers carrying
    # metadata.bot_live_delivery_consumer (``tui_gateway/session_lifecycle.py:36``)
    # both render as written.
    joinable: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alive(self) -> bool:
        """Derived: not proven dead, which includes an unverifiable identity."""
        return self.liveness is not ProcessLiveness.DEAD

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lease_renewed(self) -> bool:
        """Derived: ``updated_at`` moved past acquisition, i.e. the lease moved.

        Compared with a one-second tolerance because the two ages are rounded
        against the same injected clock and upstream writes them from two
        ``time.time()`` calls. False whenever either stamp was unusable: an
        unrecorded ``updated_at`` is not evidence of a transfer.
        """
        started = self.started_at_age_seconds
        updated = self.updated_at_age_seconds
        if started is None or updated is None:
            return False
        return started - updated > _LEASE_TRANSFER_TOLERANCE_SECONDS


class ModelUsage(BaseModel):
    """One aggregated `session_model_usage` group."""

    model: str = ""
    provider: str = ""
    # Non-empty for auxiliary work (title generation, compression, ...).
    task: str = ""
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    has_actual_cost: bool = False
    # Per-row split of the group's cost: rows with a provider-reported (or
    # otherwise authoritative) cost contribute to reported_cost_usd, rows
    # without one to estimated_only_cost_usd. A billed row's own estimate
    # column is never double-counted. row_count/reported_row_count tell the
    # all-reported / all-estimated / mixed cases apart.
    reported_cost_usd: float = 0.0
    estimated_only_cost_usd: float = 0.0
    reported_row_count: int = 0
    row_count: int = 0
    last_seen: float = 0.0


class TokenSummary(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_cost_usd: float = 0.0
    # True unless every contributing session cost was provider-reported.
    # Zero-session summaries keep the estimated default ("~$" display).
    cost_is_estimated: bool = True


class TokenWindowSummary(BaseModel):
    label: str
    session_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    total_cost_usd: float = 0.0
    cache_ratio: float = 0.0


class TokenBreakdown(BaseModel):
    label: str
    session_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    total_cost_usd: float = 0.0


class TokenAnalytics(BaseModel):
    windows: list[TokenWindowSummary] = Field(default_factory=list)
    by_model: list[TokenBreakdown] = Field(default_factory=list)
    by_provider: list[TokenBreakdown] = Field(default_factory=list)
    by_endpoint: list[TokenBreakdown] = Field(default_factory=list)
    cost_status_counts: dict[str, int] = Field(default_factory=dict)
    # "session_model_usage" when the per-model usage table is present; otherwise
    # the legacy per-session model breakdown ("sessions") drives the panel.
    usage_source: Literal["session_model_usage", "sessions"] = "sessions"
    model_usage_all: list[ModelUsage] = Field(default_factory=list)
    model_usage_24h: list[ModelUsage] = Field(default_factory=list)
    model_usage_7d: list[ModelUsage] = Field(default_factory=list)


class ToolStats(BaseModel):
    name: str
    call_count: int = 0


class BackgroundProcessInfo(BaseModel):
    session_id: str
    command: str = ""
    pid: int = 0
    pid_scope: str = ""
    cwd: str = ""
    started_at: float = 0.0
    task_id: str = ""
    session_key: str = ""
    notify_on_complete: bool = False
    watcher_platform: str = ""
    watcher_chat_id: str = ""
    watcher_user_id: str = ""
    watcher_user_name: str = ""
    watcher_thread_id: str = ""
    watcher_message_id: str = ""
    watcher_interval: int = 0
    watch_patterns: list[str] = Field(default_factory=list)
    # spawn-ledger.json fields (empty for legacy processes.json entries).
    purpose: str = ""
    port: int = 0
    profile: str = ""
    # True when the recorded pid is still live (checked via the injected
    # pid_exists); False marks a stale ledger/registry entry.
    alive: bool = False


class CheckpointInfo(BaseModel):
    repo_id: str
    workdir: str = ""
    workdir_name: str = ""
    commit_count: int = 0
    last_reason: str = ""
    last_checkpoint_at: float | None = None


class CronModelSource(StrEnum):
    """Which axis resolved a cron job's model, in upstream's precedence order."""

    PINNED = "pinned"
    CRON_DEFAULT = "cron.model default"
    MAIN_MODEL = "follows main model"


class CronJob(BaseModel):
    job_id: str = ""
    name: str = ""
    schedule_display: str = ""
    state: str = ""
    enabled: bool = True
    deliver: str = ""
    delivery_target_label: str = ""
    latest_output_excerpt: str = ""
    latest_output_path: str = ""
    latest_output_mtime: float | None = None
    silent_run: bool = False
    next_run_at: str | None = None
    last_status: str | None = None
    last_error: str = ""
    failure_streak: int = 0
    paused: bool = False
    paused_reason: str = ""
    last_delivery_error: str = ""
    dispatch_lateness_seconds: float | None = None
    dispatch_kind: str = ""
    repeat_times: int | None = None
    repeat_completed: int = 0
    no_agent: bool = False
    # Explicit inference pins from jobs.json. Empty means unpinned: the job
    # follows ``cron.model`` or the main model at fire time.
    model: str = ""
    provider: str = ""
    # The model the next fire resolves to, and which axis it came from
    # (``_load_cron_job_config``, ``cron/scheduler.py:1561-1590``). "" / None for
    # a no-agent job or when nothing is configured (upstream refuses to run).
    effective_model: str = ""
    model_source: CronModelSource | None = None
    # ``fire_claim`` (``cron/jobs.py:2588-2608``): the dispatch lease, refreshed
    # every 60 s against a 300 s TTL. ``fire_claim_state`` is derived from the
    # claim age in the collector; both are None/"" when no usable claim exists.
    fire_claim_age_seconds: float | None = None
    fire_claim_state: CronFireClaimState | None = None
    # ``pending_slot`` (``cron/occurrences.py:38-87``): the occurrence a tick took
    # off the schedule but never claimed. The scheduled instant is reported
    # verbatim; the stamp age is None when the record carries no usable stamp.
    pending_slot_scheduled_at: str = ""
    pending_slot_age_seconds: float | None = None
    # ``last_fire_error`` (``cron/jobs.py:2200-2212``): the only durable record
    # that a dashboard fire webhook could not forward. ``last_error`` is NOT set
    # for it. Redacted and capped in the collector; age is None without a stamp.
    last_fire_error: str = ""
    last_fire_error_age_seconds: float | None = None
    # ``preflight_alerted`` (``cron/jobs.py:2190-2198``): upstream's alert-once
    # dedup marker for a config-blocked job.
    preflight_alerted: bool = False


class CronTickerHealth(StrEnum):
    """Derived health of the cron ticker from its heartbeat/last-success files."""

    OK = "ok"
    FAILING = "failing"
    STALE = "stale"
    UNKNOWN = "unknown"


class CronFireClaimState(StrEnum):
    """Derived liveness of a job's ``fire_claim`` lease.

    Upstream takes the claim at dispatch and heartbeats it every 60 s against a
    300 s ``FIRE_CLAIM_TTL_SECONDS`` (``cron/jobs.py:889-892``,
    ``heartbeat_fire_claim`` ``:2600-2608``). A claim inside the window is a run
    in progress; an older one was never cleared because the runner died mid-run.
    """

    RUNNING = "running"
    ABANDONED_RUN = "abandoned run"


class CronExecution(BaseModel):
    execution_id: str = ""
    job_id: str = ""
    job_name: str = ""
    status: str = ""
    started_age_seconds: float | None = None
    duration_seconds: float | None = None
    error_excerpt: str = ""
    # Delivery is a separate question from execution: "" means the schema or the
    # row records no outcome, which is not the same as a failed delivery.
    delivery_outcome: str = ""
    scheduled_instant: str = ""
    handoff_pending: bool = False


class CronJobExecutionStats(BaseModel):
    """One job's recorded executions inside the 24-hour window.

    ``total_24h`` is the denominator the four buckets reconcile against, so a
    window can be checked for arithmetic that silently dropped a row. ``unknown``
    is a real upstream terminal status and also catches any value hermesd has not
    seen; it is never folded into ``failed``, which would imply a retry is safe.

    Delivery counters are kept apart from execution counters for the same reason:
    a completed run whose notification was suppressed was not delivered.
    ``delivery_tracked`` is False when this schema has no ``delivery_outcome``
    column at all, which is different from a column that is present but NULL.
    """

    job_id: str = ""
    total_24h: int = 0
    completed_24h: int = 0
    failed_24h: int = 0
    running_24h: int = 0
    unknown_24h: int = 0
    delivery_tracked: bool = False
    delivery_outcomes_24h: dict[str, int] = Field(default_factory=dict)
    delivery_unrecorded_24h: int = 0
    handoff_pending_24h: int = 0
    last_status: str = ""
    last_duration_seconds: float | None = None
    last_error_excerpt: str = ""


class CronIncident(BaseModel):
    incident_id: str = ""
    job_id: str = ""
    job_name: str = ""
    state: str = ""
    failure_type: str = ""
    first_seen_age_seconds: float | None = None
    last_seen_age_seconds: float | None = None
    # Age of the latest delivered failure ping: upstream restamps ``alerted_at``
    # on every alert, including cooldown reminders (``cron/incidents.py:196-212``).
    # None when no ping was delivered or the ledger predates the column.
    alerted_age_seconds: float | None = None
    error_excerpt: str = ""


class CronExecutionsState(BaseModel):
    db_present: bool = False
    job_stats: list[CronJobExecutionStats] = Field(default_factory=list)
    recent: list[CronExecution] = Field(default_factory=list)
    open_incident_count: int = 0
    unacked_incident_count: int = 0
    open_incidents: list[CronIncident] = Field(default_factory=list)
    # ``resolved`` = the job ran OK after the failure; a repeat of the same error
    # re-opens it (``cron/incidents.py:32,151-181,233-248``). Not open, not acked.
    resolved_incident_count: int = 0
    resolved_24h_count: int = 0
    # Retention. Upstream prunes terminal history to a fixed record cap, so every
    # aggregate above describes *recorded* attempts rather than every attempt that
    # happened. ``retention_cap`` is 0 when the table could not be read, which is
    # not the same as a cap of zero.
    retained_total_count: int = 0
    retained_terminal_count: int = 0
    retention_cap: int = 0
    at_retention_cap: bool = False
    oldest_claimed_age_seconds: float | None = None
    newest_claimed_age_seconds: float | None = None


class CronState(BaseModel):
    last_tick_ago_seconds: float | None = None
    ticker_heartbeat_age_seconds: float | None = None
    ticker_last_success_age_seconds: float | None = None
    ticker_health: CronTickerHealth = CronTickerHealth.UNKNOWN
    # ``cron/ticker_last_error`` is ``"<epoch>\n<message>"`` and is *unlinked* on
    # the next clean tick (``cron/jobs.py:1200-1220``), so an empty string means
    # "no failure recorded right now" and never "scheduling has not failed". The
    # message is an arbitrary exception string, so it is redacted and capped in
    # the collector. ``ticker_last_error_age_seconds`` is None when the stamp on
    # line 1 did not parse, which does not make the message any less recorded.
    ticker_last_error: str = ""
    ticker_last_error_age_seconds: float | None = None
    # ``cron/catch_up_occurrences`` is a monotonic lifetime counter with no
    # timestamp, never reset. Upstream's own reader returns 0 for a missing file
    # and for a genuine zero alike (``cron/jobs.py:1186-1193``), which loses the
    # only distinction that matters here, so presence is carried beside the
    # value. The counter is written best effort and is not written at all while
    # catch-up is disabled, so a flat or absent counter proves nothing.
    catch_up_occurrences: int = 0
    catch_up_occurrences_recorded: bool = False
    # ``cron.catch_up_missed`` from config.yaml, read with upstream's own cast
    # ``lambda value: value is not False`` (``cron/jobs.py:2908`` + ``:2615-2624``):
    # only a literal YAML ``false`` disables catch-up, and ``None``/``0``/``"no"``
    # all leave it on. ``_set`` keeps the upstream default apart from a choice.
    catch_up_missed: bool = True
    catch_up_missed_set: bool = False
    job_count: int = 0
    error_count: int = 0
    max_parallel_jobs: int = 0
    wrap_response: bool = False
    provider: str = "builtin"
    chronos_configured: bool = False
    chronos_portal_configured: bool = False
    chronos_callback_configured: bool = False
    chronos_audience_configured: bool = False
    chronos_jwks_configured: bool = False
    suggestion_count: int = 0
    jobs: list[CronJob] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ticker_error_recorded(self) -> bool:
        """Derived: a tick failure is recorded *right now*.

        Recovery deletes the marker, so this is a live signal and not a history.
        """
        return bool(self.ticker_last_error)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def catch_up_missed_disabled(self) -> bool:
        """Derived: catch-up was explicitly switched off in config.yaml.

        This is the "missed runs are being silently skipped" signal — upstream
        re-anchors the schedule without recording anything when it is off.
        """
        return self.catch_up_missed_set and not self.catch_up_missed

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_catch_up_occurrences(self) -> bool:
        """Derived: the marker was observed and at least one catch-up happened."""
        return self.catch_up_occurrences_recorded and self.catch_up_occurrences > 0


class ToolGatewayRoute(BaseModel):
    tool: str
    mode: str = "direct"
    token_present: bool = False


class ConfigBackupKind(StrEnum):
    """Fixed vocabulary for a backup group's coarse bucket.

    Derived from the writer's reason word (``hermes_cli/config_backups.py:29-69``):
    ``good``/``corrupt`` are the load-bearing states, ``setup`` and ``migration``
    are the audit trail, and anything else the naming scheme allows is ``other``.
    """

    GOOD = "good"
    CORRUPT = "corrupt"
    SETUP = "setup"
    MIGRATION = "migration"
    OTHER = "other"


class ConfigBackupGroup(BaseModel):
    """One backup-reason group inside ``backups/config/``.

    Upstream writes ``config.yaml.<reason>.<YYYYMMDD-HHMMSS>`` copies and keeps
    the newest five per reason (``hermes_cli/config_backups.py:29-69``). The
    stamp is the writer's ``time.strftime`` value, i.e. *local* time, so ages
    are computed against the same local clock — never against the file mtime,
    which a later ``hermes update`` may have reset.
    """

    reason: str
    kind: ConfigBackupKind = ConfigBackupKind.OTHER
    count: int = 0
    newest_stamp: str = ""
    newest_age_seconds: float | None = None


class ConfigSummary(BaseModel):
    model: str = ""
    provider: str = ""
    personality: str = ""
    max_turns: int = 0
    compression_threshold: float = 0.0
    reasoning_effort: str = ""
    security_redact: bool = False
    approvals_mode: str = ""
    provider_routing_summary: str = ""
    smart_model_routing_enabled: bool = False
    smart_model_routing_cheap_model: str = ""
    fallback_model_label: str = ""
    dashboard_theme: str = ""
    session_reset_mode: str = ""
    memory_provider: str = ""
    tool_gateway_domain: str = ""
    tool_gateway_scheme: str = ""
    firecrawl_gateway_url: str = ""
    tool_gateway_routes: list[ToolGatewayRoute] = Field(default_factory=list)
    tool_search_enabled: str = ""
    tool_search_threshold_pct: int = 0
    tool_search_default_limit: int = 0
    tool_search_max_limit: int = 0
    toolsets: list[str] = Field(default_factory=list)
    code_execution_mode: str = ""
    code_execution_timeout: int = 0
    code_execution_max_tool_calls: int = 0
    dashboard_public_url: str = ""
    dashboard_auth_provider: str = ""
    dashboard_basic_auth_configured: bool = False
    kanban_dispatch_in_gateway: bool = False
    kanban_auto_decompose: bool = False
    kanban_dispatch_interval_seconds: int = 0
    kanban_failure_limit: int = 0
    gateway_strict_media_delivery: bool = False
    gateway_trust_recent_files: bool = False
    gateway_trust_recent_files_seconds: int = 0
    auxiliary_slots: list[str] = Field(default_factory=list)
    moa_default_preset: str = ""
    moa_active_preset: str = ""
    moa_preset_count: int = 0
    moa_reference_model_count: int = 0
    moa_aggregator_label: str = ""
    moa_save_traces: bool = False
    moa_trace_dir: str = ""
    delegation_max_concurrent_children: int = 0
    delegation_max_spawn_depth: int = 0
    delegation_orchestrator_enabled: bool = False
    goals_max_turns: int = 0
    updates_check: bool = False
    updates_pre_update_backup: str = ""
    updates_backup_keep: int = 0
    # Server names only — MCP server config values may carry credentials.
    mcp_server_count: int = 0
    mcp_server_names: list[str] = Field(default_factory=list)
    plugin_enabled_count: int = 0
    plugin_disabled_count: int = 0
    tool_loop_warnings_enabled: bool = False
    tool_loop_hard_stop_enabled: bool = False
    # Two different caps over two different resources. Never conflate them:
    #
    # ``max_concurrent_sessions`` is a cross-process **active-session lease cap**
    # checked when a surface attaches (``try_acquire_active_session``,
    # ``hermes_cli/active_sessions.py:524-532`` — "Capacity second, and only when
    # an operator asked for one"). None means *not configured*: upstream resolves
    # it to None for an absent key, ``0``, ``null`` and any invalid value alike
    # (``resolve_max_concurrent_sessions`` ``:47-61``,
    # ``coerce_max_concurrent_sessions`` ``:31-44``), and its default is None
    # (``hermes_cli/config_defaults.py:38``). A refusal under it carries reason
    # ``MAX_CONCURRENT_SESSIONS`` and names the holders per surface.
    max_concurrent_sessions: int | None = None
    # ``max_live_sessions`` is a soft **LRU cap on the gateway's in-memory
    # sessions** — a different resource entirely. ``_enforce_session_cap``
    # (``tui_gateway/session_reaper.py:250-265``) evicts the least-recently-active
    # *detached* sessions with ``end_reason="lru_evict"`` and never a running,
    # pending or live-transport one. 0 means not configured/disabled, which is
    # what upstream's own reader returns for an unset key: ``_load_cfg()`` is
    # documented as ``load_config_readonly`` *minus* the DEFAULT_CONFIG merge
    # (``tui_gateway/server.py:1169-1176``), so the ``16`` in
    # ``config_defaults.py:42`` never reaches ``_max_live_sessions``
    # (``tui_gateway/session_reaper.py:237-247``).
    max_live_sessions: int = 0
    streaming_enabled: bool = False
    logging_level: str = ""
    # Presence only — a proxy URL can embed credentials.
    network_proxy_configured: bool = False
    # backups/config/ point-in-time copies of this file, grouped by writer
    # reason. The newest "good" stamp is the honest "config last changed" date:
    # a good copy is written only when the bytes change, so an old stamp is an
    # unchanged config, not a stale reader. "corrupt" counts are a hard alert.
    config_backups_present: bool = False
    config_backup_groups: list[ConfigBackupGroup] = Field(default_factory=list)
    config_backup_groups_truncated: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def active_session_cap_configured(self) -> bool:
        """Derived: an operator asked for a cross-process lease cap.

        Upstream enforces capacity *only* when this is true, so a False here means
        the active-session registry is unbounded — not that it is empty.
        """
        return self.max_concurrent_sessions is not None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def live_session_cap_configured(self) -> bool:
        """Derived: the in-memory LRU cap is armed (``_enforce_session_cap``)."""
        return self.max_live_sessions > 0


class ProviderInfo(BaseModel):
    name: str
    is_active: bool = False
    # The Nous free-tier identity marker: providers.<name> with
    # auth_method == "anonymous" (hermes_cli/anon_auth.py:39-41,88-89), which
    # is the single condition upstream's is_guest_state tests. Presence of the
    # marker only — the state's token values are never read, and quota state
    # never reaches disk.
    free_tier: bool = False


class CredentialPoolEntry(BaseModel):
    name: str
    label: str = ""
    auth_type: str = ""
    source: str = ""
    last_status: str = ""
    request_count: int = 0
    cooldown_remaining: str = ""
    priority: int = 0
    token_present: bool = False
    expires_at: str = ""
    last_refresh: str = ""


class SkillInfo(BaseModel):
    name: str
    category: str = ""
    description: str = ""


class HookInfo(BaseModel):
    name: str
    description: str = ""
    events: list[str] = Field(default_factory=list)


class PluginActivation(StrEnum):
    """Configured activation of a discovered plugin.

    Each value is what ``config.yaml`` and the manifest say hermes-agent *would*
    do with this plugin. None of them is evidence that it loaded: hermesd reads
    files and never imports or executes plugin code.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    NOT_ENABLED = "not_enabled"
    CATEGORY_OWNED = "category_owned"
    REMOVED = "removed"
    UNKNOWN = "unknown"


class PluginInfo(BaseModel):
    """One discovered plugin: its manifest, its configured activation, its provenance.

    Everything here is *read*, never executed. hermesd never imports plugin code,
    so no field is evidence that a plugin loaded: ``activation`` says what
    ``config.yaml`` and the manifest tell hermes-agent to do, ``declared_*`` and
    ``requires_hermes`` say what the manifest claims, and the provenance fields
    say which commit the install tooling recorded.

    Two field names deliberately differ from the on-disk keys they come from,
    because both would otherwise collide with an existing field:

    * ``install_source`` is ``source`` in ``plugins/.install-metadata.json`` (a
      credential-scrubbed git URL), while ``PluginInfo.source`` is upstream's
      discovery root (``user``/``bundled``/``project``/``entrypoint``);
    * ``catalog_*`` prefixes every key of ``<plugin_dir>/.hermes-catalog.json``,
      whose ``sha`` and ``repo`` would otherwise be indistinguishable from the
      installed revision and the install source.
    """

    name: str
    version: str = ""
    description: str = ""
    source: str = "user"
    activation: PluginActivation = PluginActivation.UNKNOWN
    activation_reason: str = ""
    kind: str = ""
    manifest_key: str = ""
    # Which manifest file won, and which lost to it. Upstream accepts plugin.yaml,
    # plugin.yml and a portable plugin.json in that precedence and says nothing
    # about the losers; hermesd records them so a conflicting pair is observable.
    manifest_file: str = ""
    manifest_shadowed: list[str] = Field(default_factory=list)
    tool_count: int = 0
    hook_count: int = 0
    dashboard_enabled: bool = False
    # Declarations, not capabilities: a manifest asserting `tools.override` is
    # consent metadata upstream still gates behind plugins.entries.<id>
    # .granted_capabilities (hermes_cli/plugin_capabilities.py:44-46).
    requires_hermes: str = ""
    declared_capabilities: list[str] = Field(default_factory=list)
    declared_capability_count: int = 0
    # plugins/.install-metadata.json — the actually-installed HEAD, and the SHA an
    # explicit `--ref` pinned it to ("" when the install was not pinned).
    installed_revision: str = ""
    pinned_revision: str = ""
    install_source: str = ""
    # <plugin_dir>/.hermes-catalog.json — the catalog entry that was reviewed,
    # which is not necessarily the commit that was installed.
    catalog_name: str = ""
    catalog_repo: str = ""
    catalog_sha: str = ""
    catalog_tier: str = ""
    catalog_installed_at: str = ""
    # cache/plugin-catalog.json comparisons (the live catalog's view of this
    # plugin): an entry sha that differs from the sidecar's reviewed sha, and
    # the kill-list verdict for name/catalog name/repo. Neither is set when the
    # cache is absent — no cache, no claim.
    catalog_update_available: bool = False
    catalog_removed: bool = False
    catalog_removed_reason: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def enabled(self) -> bool:
        """Derived, never stored: activation is the single source of truth."""
        return self.activation is PluginActivation.ENABLED

    @computed_field  # type: ignore[prop-decorator]
    @property
    def manifest_format(self) -> str:
        """``yaml`` or ``portable`` — which reader the winning filename needs.

        Derived from ``manifest_file`` so the two cannot disagree: a portable
        Agent Plugin is parsed and validated differently from a native manifest.
        """
        if self.manifest_file in YAML_MANIFEST_NAMES:
            return "yaml"
        if self.manifest_file == PORTABLE_MANIFEST_NAME:
            return "portable"
        return ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pinned(self) -> bool:
        """Derived: upstream records a pin only beside the revision it pins."""
        return bool(self.pinned_revision)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unmanaged(self) -> bool:
        """Derived: neither provenance sidecar recorded anything usable.

        A catalog sidecar would fill ``catalog_*`` and an install record would
        fill ``installed_revision``/``install_source``; neither existing means
        the directory was never installed by the plugin tooling — a local or
        hand-copied plugin, which is a fact about the files, not an error.
        """
        return not (
            self.catalog_name or self.catalog_sha or self.installed_revision or self.install_source
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def provenance_drift(self) -> bool:
        """True when the catalog's reviewed SHA and the installed HEAD disagree.

        ``install_catalog_entry`` installs at ``ref or entry.sha`` and then writes
        the sidecar from ``entry`` (hermes_cli/plugins_cmd_catalog.py:108-118), so
        an explicit ``--ref`` leaves the two apart by design — and the sidecar
        alone is then not evidence of what code is on disk. Both sides must be
        full 40-hex revisions: a malformed SHA is a corrupt sidecar, not a move,
        and claiming drift from it would invent a comparison hermesd cannot make.
        """
        catalog = self.catalog_sha.lower()
        installed = self.installed_revision.lower()
        if not _FULL_REVISION_PATTERN.fullmatch(catalog):
            return False
        if not _FULL_REVISION_PATTERN.fullmatch(installed):
            return False
        return catalog != installed


class MCPServerInfo(BaseModel):
    name: str
    enabled: bool = True
    transport: str = ""
    target: str = ""
    tool_filter: str = ""


class DesktopPluginInfo(BaseModel):
    """One app-level Desktop plugin folder with a regular ``plugin.js`` entry."""

    name: str


class SkillsMemory(BaseModel):
    skill_count: int = 0
    skill_categories: int = 0
    memory_file_count: int = 0
    providers: list[ProviderInfo] = Field(default_factory=list)
    credential_pools: list[CredentialPoolEntry] = Field(default_factory=list)
    hooks: list[HookInfo] = Field(default_factory=list)
    plugins: list[PluginInfo] = Field(default_factory=list)
    # ~/.hermes is untrusted, so the plugins walk has a budget. `plugins` is
    # therefore a capped list, and this says when the cap cut it short — a
    # truncated scan must never read as a complete inventory.
    plugin_scan_truncated: bool = False
    desktop_plugins: list[DesktopPluginInfo] = Field(default_factory=list)
    desktop_plugin_scan_truncated: bool = False
    mcp_servers: list[MCPServerInfo] = Field(default_factory=list)
    boot_md_present: bool = False
    boot_md_mtime: float | None = None
    skills: list[SkillInfo] = Field(default_factory=list)
    # cache/plugin-catalog.json — the live catalog's view of the installed
    # plugins above. Absent cache means the drift/removal checks made no
    # claims this pass, which must not read as "everything is current" — and
    # neither may a present-but-unreadable one (`usable` False: the payload is
    # not the ``{"entries": [...]}`` object upstream writes).
    plugin_catalog_cache_present: bool = False
    plugin_catalog_cache_usable: bool = False
    plugin_catalog_cache_age_seconds: float | None = None
    plugin_catalog_update_count: int = 0
    plugin_catalog_removed_count: int = 0


class ToolsetAvailability(BaseModel):
    """Toolset availability from ``cache/banner_snapshot.json``."""

    enabled_toolsets: list[str] = Field(default_factory=list)
    unavailable_toolsets: list[str] = Field(default_factory=list)
    lazy_tool_count: int = 0
    disabled_tool_count: int = 0


class MCPCacheEntryState(StrEnum):
    """Whether hermes-agent would serve one ``cache/mcp_schema_cache.json`` entry.

    Mirrors ``get_cached_entry`` (``tools/mcp_schema_cache.py:59-73``): an entry
    whose ``ttl_ms`` and ``written_at`` are both numeric expires once
    ``(now - written_at) * 1000 >= ttl_ms`` — so ``ttl_ms: 0`` is always expired
    — and an entry with no numeric TTL never expires at all.

    There is deliberately **no** ``configuration-mismatched`` member. Upstream's
    first test is ``entry["fingerprint"] != config_fingerprint(config)``, but the
    ``config`` it hashes is not the ``config.yaml`` hermesd can read:
    ``_load_mcp_config`` (``tools/mcp_tool_config.py:322-345``) hands
    ``config_fingerprint`` the ``${VAR}``-interpolated block, resolved through
    the active profile's secret scope and ``${workspaceFolder}``
    (``:244-259``), from a ``load_config()`` that is already ``DEFAULT_CONFIG +
    config.yaml + managed scope, env-expanded`` (``hermes_cli/config.py:1965``),
    and merges in plugin-provided portable servers that appear in no YAML at all
    (``:307-320``). Recomputing the hash from raw YAML would therefore report a
    mismatch for every server whose ``command``/``args``/``url`` carries a
    placeholder, and hermesd can neither resolve secrets nor import the loader
    that would. ``fingerprint`` below is the cache's own record, exposed as a
    prefix so an operator can compare it against ``hermes mcp`` output instead
    of being handed a verdict hermesd cannot prove.

    ``UNASSESSABLE`` is likewise a first-class answer, not a fallback: a
    non-mapping entry or a missing/unusable ``fingerprint`` is a cache upstream
    would also refuse, and saying so is more useful than guessing.
    """

    VALID = "valid"
    EXPIRED = "expired"
    UNASSESSABLE = "unassessable"


class MCPCacheEntry(BaseModel):
    """Validity metadata for one cached MCP server entry.

    Only metadata: ``tools``, ``utility_tools`` and every ``inputSchema`` inside
    them stay on disk. They are opaque, unbounded, and a tool manifest can carry
    a default credential, so nothing from them is copied into state — nor is
    ``cache_scope``, which upstream documents as irrelevant to validity
    (``tools/mcp_schema_cache.py:63``).
    """

    name: str = ""
    state: MCPCacheEntryState = MCPCacheEntryState.UNASSESSABLE
    # Why the state is not VALID, when that reason is not already the numbers
    # below (an unassessable entry has no TTL story to tell).
    reason: str = ""
    # First 8 hex chars of the entry's own recorded fingerprint — a display
    # bound on an untrusted string, not a comparison hermesd performed.
    fingerprint: str = ""
    # ``ttl_ms`` exactly as recorded, or None when it is not a number upstream
    # would treat as one.
    ttl_ms: float | None = None
    # Age of the entry's own ``written_at`` against the injected clock — never
    # the cache file's mtime, which advances on every unrelated write-through.
    age_seconds: float | None = None
    # TTL left, when there is a numeric TTL and it has not run out.
    remaining_seconds: float | None = None


class MCPSchemaCache(BaseModel):
    """Summary of ``cache/mcp_schema_cache.json`` — server names and validity.

    ``mcp_uncached_*`` is configured-minus-cached, computed from the complete
    name sets on both sides. Absence of a cache entry is an observation about
    this read, not evidence a server never connected: the cache may have been
    cleared, invalidated, or written under another profile.

    The ``mcp_*_entry_count`` rollups are computed over **every** entry in the
    file while ``mcp_entries`` is display-bounded, so truncating the list can
    never change a count.
    """

    mcp_cache_present: bool = False
    mcp_cached_server_count: int = 0
    mcp_cached_server_names: list[str] = Field(default_factory=list)
    mcp_schema_cache_age_seconds: float | None = None
    mcp_uncached_server_count: int = 0
    mcp_uncached_server_names: list[str] = Field(default_factory=list)
    mcp_valid_entry_count: int = 0
    mcp_expired_entry_count: int = 0
    mcp_unassessable_entry_count: int = 0
    mcp_entries: list[MCPCacheEntry] = Field(default_factory=list)


class SkillsPromptSnapshot(BaseModel):
    """Summary of ``.skills_prompt_snapshot.json``."""

    prompted_skill_count: int = 0
    prompt_snapshot_age_seconds: float | None = None


class MemoryOverview(BaseModel):
    provider: str = ""
    memory_file_count: int = 0
    memory_word_count: int = 0
    user_word_count: int = 0
    soul_size_bytes: int = 0
    soul_excerpt: str = ""
    memory_files: list[str] = Field(default_factory=list)
    skill_usage_count: int = 0
    learned_skill_count: int = 0
    pinned_skill_count: int = 0
    agent_created_skill_count: int = 0
    memory_card_count: int = 0


class ProfileSummary(BaseModel):
    name: str
    session_count: int = 0
    latest_log_mtime: float | None = None
    skill_count: int = 0
    db_size_bytes: int = 0
    soul_excerpt: str = ""


class ProfilesState(BaseModel):
    profile_count: int = 0
    profiles: list[ProfileSummary] = Field(default_factory=list)


class LogLine(BaseModel):
    timestamp: str = ""
    component: str = ""
    level: str = ""
    session_id: str = ""
    message: str = ""


class SourceScope(StrEnum):
    """Which Hermes home a source is resolved against.

    ``ROOT`` is ``~/.hermes`` (``HermesPaths.shared_path``); ``PROFILE`` is
    ``~/.hermes/profiles/<name>`` when a profile is selected and the root
    otherwise (``HermesPaths.profile_path``). The value names the resolver that
    *owns* the source, not the directory it happened to resolve to, so a
    profile-owned source reads ``PROFILE`` even in root mode.

    There is deliberately no ``SHARED`` member: ``shared_home is root_home``.
    Values that come from hermesd's own process environment rather than from
    disk are documented as ``PROCESS-ENV`` in
    ``.codex/rules/source-ownership.md`` and have no model consumer yet.
    """

    ROOT = "root"
    PROFILE = "profile"


class LogStream(BaseModel):
    name: str
    path: str = ""
    scope: SourceScope = SourceScope.ROOT
    size_bytes: int = 0
    mtime: float | None = None
    lines: list[LogLine] = Field(default_factory=list)


class LogState(BaseModel):
    agent_lines: list[LogLine] = Field(default_factory=list)
    gateway_lines: list[LogLine] = Field(default_factory=list)
    error_lines: list[LogLine] = Field(default_factory=list)
    cron_lines: list[LogLine] = Field(default_factory=list)
    streams: list[LogStream] = Field(default_factory=list)


class ChannelPlatformInfo(BaseModel):
    name: str
    entry_count: int = 0
    states: list[str] = Field(default_factory=list)
    connected: bool = False
    capabilities: list[str] = Field(default_factory=list)
    family_label: str = ""
    missing_from_directory: bool = False


class ChannelDirectoryState(BaseModel):
    updated_at: str = ""
    platform_count: int = 0
    alias_count: int = 0
    alias_platform_count: int = 0
    stale_alias_count: int = 0
    missing_directory_platforms: list[str] = Field(default_factory=list)
    platforms: list[ChannelPlatformInfo] = Field(default_factory=list)


class KanbanTaskSummary(BaseModel):
    task_id: str
    title: str = ""
    assignee: str = ""
    status: str = ""
    priority: int = 0
    consecutive_failures: int = 0
    worker_pid: int = 0
    session_id: str = ""
    last_failure_error: str = ""
    last_heartbeat_at: int = 0
    claim_expires: int = 0
    current_run_id: int = 0
    model_override: str = ""
    branch_name: str = ""
    skills: str = ""
    completed_at: int = 0
    workspace_path: str = ""
    goal_mode: str = ""
    current_step_key: str = ""
    # Completion contract: "" (local-only), "OWNER/REPO", or an exact PR URL.
    # A PR-contract task gates completion on repository exact-head CI
    # (tools/kanban_tools_schemas.py:461-464), so a done-looking review card
    # may be waiting on CI rather than finished.
    completion_contract: str = ""
    # Per-task breaker trip threshold: the raw column, so NULL ("no override")
    # stays distinct from a stored 0 ("trip on the first failure"). This is the
    # failure count at which the breaker trips, not a retry budget
    # (hermes_cli/kanban_db.py:908-914).
    max_retries: int | None = None
    # Effective trip threshold and verdict for this task, computed against the
    # configured kanban.failure_limit at collect time.
    breaker_limit: int = 0
    breaker_tripped: bool = False


class KanbanRunSummary(BaseModel):
    run_id: int = 0
    task_id: str = ""
    profile: str = ""
    status: str = ""
    outcome: str = ""
    worker_pid: int = 0
    started_at: int = 0
    ended_at: int = 0
    error: str = ""
    summary: str = ""


class KanbanTaskLink(BaseModel):
    parent_id: str = ""
    child_id: str = ""


class KanbanNotifySubSummary(BaseModel):
    """One task notification subscription and its unseen-event backlog.

    The gateway kanban-notifier claims *this task's* task_events with
    ``task_id = ? AND id > last_event_id`` (``hermes_cli/kanban_db_notify.py:310-337``);
    ``backlog`` counts those rows, so a backlog that only grows means the
    watcher that owns the sub is wedged or gone. ``max_event_id`` is the task's
    newest event id, not the backlog source.
    """

    task_id: str = ""
    platform: str = ""
    notifier_profile: str = ""
    delivery_mode: str = ""
    last_event_id: int = 0
    max_event_id: int = 0
    backlog: int = 0


class KanbanBoardSummary(BaseModel):
    slug: str = ""
    current: bool = False
    task_count: int = 0
    run_count: int = 0
    problem_count: int = 0
    stale_claim_count: int = 0
    block_kind_counts: dict[str, int] = Field(default_factory=dict)


class KanbanState(BaseModel):
    db_present: bool = False
    task_count: int = 0
    run_count: int = 0
    event_count: int = 0
    comment_count: int = 0
    dispatch_in_gateway: bool = False
    dispatch_interval_seconds: int = 0
    claim_ttl_seconds: int = 0
    auto_decompose: bool = False
    failure_limit: int = 0
    link_count: int = 0
    attachment_count: int = 0
    board_count: int = 0
    current_board: str = ""
    stale_claim_count: int = 0
    boards: list[KanbanBoardSummary] = Field(default_factory=list)
    status_counts: dict[str, int] = Field(default_factory=dict)
    assignee_counts: dict[str, int] = Field(default_factory=dict)
    active_tasks: list[KanbanTaskSummary] = Field(default_factory=list)
    problem_tasks: list[KanbanTaskSummary] = Field(default_factory=list)
    recent_tasks: list[KanbanTaskSummary] = Field(default_factory=list)
    task_links: list[KanbanTaskLink] = Field(default_factory=list)
    recent_runs: list[KanbanRunSummary] = Field(default_factory=list)
    # Notify subscriptions (kanban_notify source over kanban_notify_subs).
    notify_sub_count: int = 0
    notify_platform_counts: dict[str, int] = Field(default_factory=dict)
    # True when notify_platform_counts was cut to its busiest entries.
    notify_platforms_truncated: bool = False
    notify_backlog_total: int = 0
    notify_max_backlog: int = 0
    # Subscriptions holding any unseen event: the exact count behind the
    # capped worst-ten notify_backlog_subs list.
    notify_backlog_sub_count: int = 0
    notify_backlog_subs: list[KanbanNotifySubSummary] = Field(default_factory=list)
    notify_orphan_profile_count: int = 0
    notify_orphan_profiles: list[str] = Field(default_factory=list)


class ModelCacheSummary(BaseModel):
    name: str
    provider_count: int = 0
    model_count: int = 0
    size_bytes: int = 0
    mtime: float | None = None


class PRMonitorSummary(BaseModel):
    filename: str
    repo: str = ""
    checked_at: str = ""
    monitored_count: int = 0
    tracked_count: int = 0
    author_pr_count: int = 0
    open_count: int = 0
    conflicting_count: int = 0


class VerificationEventSummary(BaseModel):
    event_id: int = 0
    created_at: str = ""
    session_id: str = ""
    root: str = ""
    command: str = ""
    canonical_command: str = ""
    kind: str = ""
    scope: str = ""
    status: str = ""
    exit_code: int = 0
    output_summary: str = ""


class VerificationRootSummary(BaseModel):
    session_id: str = ""
    root: str = ""
    last_event_id: int = 0
    last_edit_at: str = ""
    changed_path_count: int = 0


class ProjectSummary(BaseModel):
    slug: str = ""
    name: str = ""
    board_slug: str = ""
    primary_path: str = ""
    archived: bool = False
    verification_root_count: int = 0
    kanban_board_present: bool = False


class DiscoveredRepoSummary(BaseModel):
    root: str = ""
    label: str = ""
    last_seen: str = ""


class GoalSummary(BaseModel):
    session_id: str = ""
    goal: str = ""
    status: str = ""
    turns_used: int = 0
    max_turns: int = 0
    has_contract: bool = False
    waiting_on_pid: int = 0
    waiting_on_session: str = ""
    waiting_reason: str = ""
    subgoal_count: int = 0


class DelegationInfo(BaseModel):
    delegation_id: str = ""
    origin_session: str = ""
    state: str = ""
    delivery_state: str = ""
    delivery_attempts: int = 0
    dispatched_at: float = 0.0
    completed_at: float | None = None
    duration_seconds: float | None = None
    goal: str = ""
    result_status: str = ""
    error_excerpt: str = ""
    owner_alive: bool = False
    # Background-process handoff accounting, summed across the result payload's
    # per-child entries (``tools/delegate_tool_child_run.py:744-762``, persisted
    # by ``tools/async_delegation.py:198-225``). This is the only durable trace
    # of the handoff feature — the live roster lives in gateway memory — so the
    # counts are kept even though the session ids, commands and output tails
    # they summarize never leave the payload.
    handed_off_count: int = 0
    orphaned_count: int = 0
    unread_completion_count: int = 0


class DelegationLiveTask(BaseModel):
    """One per-child entry of a live delegation manifest.

    ``status``/``exit_reason`` are the writer's best-effort updates after the
    batch joins (``tools/delegation_live_log.py:270-286``); a crash before the
    join leaves the task marked ``running`` forever. ``log_tail`` holds the last
    few redacted lines of ``task-<index>.log`` for the detail view — the file
    upstream pre-redacts per line, and hermesd redacts again before it leaves
    the collector.
    """

    index: int = 0
    goal: str = ""
    status: str = ""
    exit_reason: str = ""
    log_name: str = ""
    log_tail: list[str] = Field(default_factory=list)


class DelegationLiveManifest(BaseModel):
    """Per-delegation card from ``cache/delegation/live/<id>/manifest.json``.

    Written at dispatch and amended after the join
    (``tools/delegation_live_log.py:255-287``): model, provider, task count and
    per-task status. This is the *manifest*, not the live roster — tool counts,
    steer state and depth exist only in gateway memory and over RPC, so nothing
    here claims to show them. ``dir_age_seconds`` comes from the directory
    mtime, which is the dispatch write time; appending a ``task-<index>.log`` or
    rewriting the manifest does not advance it, so this is a *dispatched* age,
    not a liveness signal.
    """

    delegation_id: str = ""
    model: str = ""
    provider: str = ""
    started: str = ""
    completed: str = ""
    manifest_present: bool = False
    dir_age_seconds: float | None = None
    task_count: int = 0
    running_task_count: int = 0
    tasks: list[DelegationLiveTask] = Field(default_factory=list)
    tasks_truncated: bool = False


class RetiredWalGeneration(BaseModel):
    """The newest ``state.db.retired-wal-*`` capture, from its ``manifest.json``.

    Upstream writes the directory when a writer's ``-wal``/``-shm`` generation is
    deleted or replaced underneath it, so the frames that lived only in the
    unlinked inode are preserved before process exit drops them
    (``hermes_state_dbfile.py:220-236``, ``capture_retired_wal_generation``
    ``:334-425``). The manifest is published last, inside a ``.partial`` staging
    directory that is ``os.replace``d into place, so a settled generation without
    a manifest is anomalous but is still a directory on disk: it is counted, and
    nothing is claimed about its contents.

    ``captured_at`` is ``%Y-%m-%dT%H:%M:%SZ`` (UTC) — unlike the repair ledger's
    ``last_attempt``, which is naive *local* time. The two are parsed differently
    on purpose.
    """

    manifest_present: bool = False
    captured_at: str = ""
    captured_at_age_seconds: float | None = None
    trigger: str = ""
    # ``wal.bytes`` — the size of the retired WAL that was copied out.
    wal_bytes: int = 0
    # ``main.mode``: ``copied`` when the main image fit under upstream's 512 MiB
    # cap (``RETIRED_GENERATION_MAIN_IMAGE_MAX_BYTES``, ``:236``), otherwise
    # ``header_only`` for the 100-byte SQLite header.
    main_mode: str = ""


class DbRecoveryState(BaseModel):
    """What hermes-agent's own repair code left beside ``state.db``.

    Presence and metadata only. hermesd never runs a repair, a checkpoint or an
    integrity check, and never hashes the database: the live ``state.db`` is
    hundreds of megabytes, and hashing it is exactly the expensive work this
    reader exists to avoid. The only reads are a bounded directory listing,
    ``stat`` on the entries whose names match, and two small JSON documents.

    Three traps this model is shaped around:

    * **``~/.hermes/recovery/`` is not recovery evidence.** That directory holds
      operator-made remediation bundles (``config.yaml.before``,
      ``git-status.before.txt``, ``repository.bundle``, …); upstream's repair code
      never writes it. Every artifact here is a *sibling of ``state.db``* named
      after it, so ``recovery/`` never matches.
    * **An absent ledger is not a healthy database.** ``_record_repair_outcome``
      deletes the ledger on success (``hermes_state_repair.py:414-416``), so its
      absence means "no failed repair outstanding OR never repaired" and nothing
      stronger.
    * **``repair_budget_exhausted`` is weaker than upstream's predicate.**
      ``_persistent_repair_attempts_exhausted`` (``:487-500``) also requires the
      ledger's fingerprint to match the *current* file, which hermesd cannot check
      without hashing it. This flag therefore says "the ledger has recorded at
      least ``MAX_PERSISTENT_REPAIR_ATTEMPTS`` failures", i.e. the budget upstream
      would treat as exhausted for an unchanged file.

    Staging artifacts are counted apart from settled ones and never inside them:
    ``.backup-staging-*`` and ``*.incomplete*`` backups, and ``.partial`` retired
    generations, are all mid-write and presenting one as a completed capture
    would misreport the forensic record.
    """

    # ``state.db.repair-attempts.json`` — ``_repair_ledger_path``,
    # ``hermes_state_repair.py:317-318``.
    repair_ledger_present: bool = False
    failed_attempts: int = 0
    # ``datetime.now().isoformat(timespec="seconds")``: naive local time.
    last_attempt: str = ""
    last_attempt_age_seconds: float | None = None
    # ``state.db.malformed-backup-<stamp>[_<seq>]`` — ``:503-513``, retained to
    # ``_MAX_MALFORMED_BACKUPS`` by ``_prune_malformed_backups`` ``:443-451``.
    # ``count`` is settled main copies only; ``bytes`` adds each settled copy's
    # present ``-wal``/``-shm``/``-journal`` sidecars, which upstream prunes with
    # it and which are the disk the bundle actually occupies.
    malformed_backup_count: int = 0
    malformed_backup_bytes: int = 0
    newest_malformed_backup_age_seconds: float | None = None
    malformed_backup_staging_count: int = 0
    # ``state.db.retired-wal-<utc stamp>-<pid>[-n]/`` —
    # ``RETIRED_GENERATION_DIR_SUFFIX``, ``hermes_state_dbfile.py:228``.
    retired_wal_count: int = 0
    retired_wal_staging_count: int = 0
    newest_retired_wal: RetiredWalGeneration = Field(default_factory=RetiredWalGeneration)
    # ``state.db.repair.lock`` and ``state.db.auto-maintenance.lock``
    # (``_open_lock_file``, ``hermes_state_repair.py:176-229``). Presence of the
    # *file* only: upstream opens both with ``"a+b"`` and never deletes them, so
    # a file on disk is not proof anything currently holds the lock.
    repair_lock_file_present: bool = False
    auto_maintenance_lock_file_present: bool = False
    # True when the bounded directory listing was cut short, so every count above
    # is a floor and not an inventory.
    scan_truncated: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def repair_budget_exhausted(self) -> bool:
        """Derived: the ledger has recorded the whole persistent-attempt budget.

        See the class docstring — hermesd compares the recorded count against
        ``MAX_PERSISTENT_REPAIR_ATTEMPTS`` and deliberately does not recompute the
        fingerprint upstream also matches on. False whenever no ledger is present.
        """
        return self.repair_ledger_present and self.failed_attempts >= (
            MAX_PERSISTENT_REPAIR_ATTEMPTS
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def artifacts_present(self) -> bool:
        """Derived: anything at all was found, settled or in progress."""
        return bool(
            self.repair_ledger_present
            or self.malformed_backup_count
            or self.malformed_backup_staging_count
            or self.retired_wal_count
            or self.retired_wal_staging_count
            or self.repair_lock_file_present
            or self.auto_maintenance_lock_file_present
        )


class HostedRoomEventKind(StrEnum):
    """The closed hosted-room event vocabulary (``gateway/hosted_rooms.py:49-57``).

    A ``kind`` is a protocol word, not content, so it is the one string from
    ``hosted_room_events`` hermesd is allowed to carry. Anything outside this
    vocabulary is counted as ``UNKNOWN`` rather than passed through: an event
    table is untrusted input, and an unbounded histogram of arbitrary strings is
    both a rendering hazard and a way to smuggle payload text into a model.
    """

    MESSAGE_USER = "message.user"
    MESSAGE_MEMBER = "message.member"
    TURN_STARTED = "turn.started"
    TURN_SETTLED = "turn.settled"
    TURN_FAILED = "turn.failed"
    TURN_CANCELLED = "turn.cancelled"
    TURN_DEFERRED = "turn.deferred"
    TURN_REASSIGNED = "turn.reassigned"
    ROOM_CREATED = "room.created"
    ROOM_RENAMED = "room.renamed"
    ROOM_DISBANDED = "room.disbanded"
    ROOM_MEMBERS_CHANGED = "room.members_changed"
    ROOM_ACTIVITY = "room.activity"
    ROOM_STOP_REQUESTED = "room.stop_requested"
    AUTHORITY_CLAIMED = "authority.claimed"
    AUTHORITY_LOST = "authority.lost"
    MEMBER_UNAVAILABLE = "member.unavailable"
    UNKNOWN = "unknown"


# The recognized kinds, as bound query parameters. ``UNKNOWN`` is not queried: it
# is the complement of these inside ``event_count``, which keeps the histogram
# exact without ever selecting an unrecognized string out of an untrusted table.
KNOWN_HOSTED_ROOM_EVENT_KINDS: tuple[str, ...] = tuple(
    kind.value for kind in HostedRoomEventKind if kind is not HostedRoomEventKind.UNKNOWN
)

# Upstream retention ceilings (``gateway/hosted_rooms.py:33-41``). Rendered beside
# the counts so an operator can tell "two rooms" from "at the cap".
MAX_ACTIVE_HOSTED_ROOMS: int = 256
MAX_DISBANDED_HOSTED_ROOM_TOMBSTONES: int = 512
HOSTED_ROOM_DISBANDED_RETENTION_SECONDS: int = 90 * 24 * 60 * 60
MAX_EVENTS_PER_HOSTED_ROOM: int = 50_000


class HostedRoomSummary(BaseModel):
    """One hosted room, as counts and coordinates only.

    There is deliberately no field that could hold what the room *said*: no
    member list, no event payload, no actor, no link target. ``member_count`` is
    the length of ``members_json``, never its contents; ``latest_seq`` is derived
    from ``next_seq`` because upstream keeps ``next_seq`` one past the last
    written sequence number.
    """

    room_id: str = ""
    name: str = ""
    member_count: int = 0
    authority_epoch: int = 0
    next_seq: int = 0
    event_bytes: int = 0
    revision: int = 0
    created_at_age_seconds: float | None = None
    updated_at_age_seconds: float | None = None
    disbanded_at_age_seconds: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def latest_seq(self) -> int:
        """Derived: ``next_seq - 1``, clamped so an empty room reads as 0."""
        return max(0, self.next_seq - 1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def disbanded(self) -> bool:
        """Derived from the tombstone stamp, never stored beside it."""
        return self.disbanded_at_age_seconds is not None


class HostedRoomState(BaseModel):
    """Hosted-room coordination, read from the ROOT ``shared-state.db``.

    Two facts shape this model.

    *Which file.* ``gateway/hosted_rooms.py:398-414`` resolves even a profile
    gateway to the shared ROOT ``shared-state.db`` and explicitly not to the
    master ``state.db``, because pointing profile gateways at the session store
    makes every profile process a long-lived writer on it. The live ``state.db``
    nonetheless still carries empty legacy ``hosted_room*`` tables, so reading
    them would be reading a dead table.

    *What may leave the file.* Grants, link catalogs and target URLs (which may
    embed credentials), event payloads and actors, revoked-grant scope keys and
    everything in the ``hosted_room_policy_transcript*`` conversation tables are
    never selected. Only counts, ids, names, timestamps, epochs, revisions,
    ``event_bytes`` and the closed ``HostedRoomEventKind`` histogram do.
    """

    db_present: bool = False
    db_size_bytes: int = 0
    # ``disbanded_at IS NULL`` is upstream's own active-room predicate
    # (``gateway/hosted_rooms.py:867``); hermesd spells it ``COALESCE(..) > 0``
    # for the disbanded side so the count and ``HostedRoomSummary.disbanded``
    # cannot disagree about a stored 0.
    active_room_count: int = 0
    disbanded_room_count: int = 0
    rooms: list[HostedRoomSummary] = Field(default_factory=list)
    # True when ``rooms`` was cut short by the display cap. The counts here are
    # whole-table aggregates and are never affected by that cap.
    rooms_truncated: bool = False
    event_count: int = 0
    event_kind_counts: dict[str, int] = Field(default_factory=dict)
    newest_event_age_seconds: float | None = None
    # ``SUM(hosted_rooms.event_bytes)`` — upstream's own logical budget counter,
    # which it compares against ``MAX_GATEWAY_EVENT_BYTES`` when admitting events.
    accounted_event_bytes: int = 0
    retired_id_count: int = 0
    newest_retired_id_age_seconds: float | None = None
    # A count only: every other column of ``hosted_room_links`` is either a
    # credential-bearing URL, a grant or a tool catalog.
    link_count: int = 0
    remote_run_count: int = 0
    newest_remote_run_age_seconds: float | None = None
    revoked_grant_count: int = 0
    live_peer_reservation_count: int = 0
    expired_peer_reservation_count: int = 0
    revoked_peer_reservation_count: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def room_count(self) -> int:
        """Derived: every row is either active or a tombstone, never both."""
        return self.active_room_count + self.disbanded_room_count

    @computed_field  # type: ignore[prop-decorator]
    @property
    def peer_reservation_count(self) -> int:
        """Derived: revoked wins, then expiry, so the buckets partition the table."""
        return (
            self.live_peer_reservation_count
            + self.expired_peer_reservation_count
            + self.revoked_peer_reservation_count
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unknown_event_kind_count(self) -> int:
        """Derived: events whose kind is outside the closed vocabulary."""
        return max(0, self.event_count - sum(self.event_kind_counts.values()))


# Upstream's API-run replay window (``api_server_run_idempotency.py:58-59``).
API_RUN_RETENTION_SECONDS: int = 24 * 60 * 60


class ApiRunStatus(StrEnum):
    """The ``/v1/runs`` status vocabulary hermesd will name.

    Assembled from the statuses upstream actually sets — ``queued``
    (``api_server_runs.py:475``), ``running`` (``:705``),
    ``waiting_for_approval`` (``:592``), ``stopping`` (``:871``) and the four
    terminal ones in ``TERMINAL_STATUSES``
    (``api_server_run_idempotency.py:17``). Anything else becomes ``UNKNOWN``: a
    status hermesd does not recognize is reported as unknown rather than dropped
    or guessed, because the store outlives this enumeration.
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


# Upstream's ``TERMINAL_STATUSES`` — the only statuses whose aged rows
# ``_prune_stale_terminal_locked`` is allowed to delete.
API_RUN_TERMINAL_STATUSES: frozenset[ApiRunStatus] = frozenset(
    {
        ApiRunStatus.COMPLETED,
        ApiRunStatus.FAILED,
        ApiRunStatus.CANCELLED,
        ApiRunStatus.INTERRUPTED,
    }
)


class ApiRunReservation(BaseModel):
    """One retained ``POST /v1/runs`` reservation.

    ``fingerprint``, ``idempotency_key`` and ``scope`` never leave the reader, and
    ``status_json`` is parsed no further than the single allowlisted status word.
    ``owner_started`` is deliberately *not* carried: upstream fills it from
    ``gateway/status.get_process_start_time`` (``api_server_runs.py:81-87``),
    which returns ``/proc`` ticks on Linux and psutil centiseconds elsewhere. It
    is comparable only against the same host's own reading, so hermesd reduces it
    to "was an identity recorded at all" instead of inventing a timestamp out of
    a unit it cannot establish.
    """

    run_id: str = ""
    status: ApiRunStatus = ApiRunStatus.UNKNOWN
    created_at_age_seconds: float | None = None
    updated_at_age_seconds: float | None = None
    # ``retention_until - now``; None when the row carries no explicit deadline
    # and upstream prunes it on ``updated_at + RETENTION_SECONDS`` instead.
    retention_remaining_seconds: float | None = None
    acknowledged: bool = False
    owner_pid: int = 0
    owner_started_recorded: bool = False
    owner_alive: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def owner_pid_present(self) -> bool:
        """Derived: upstream stores 0 for "no owner recorded"."""
        return self.owner_pid > 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def terminal(self) -> bool:
        """Derived: only a terminal row is eligible for retention pruning."""
        return self.status in API_RUN_TERMINAL_STATUSES


class ApiRunReservationsState(BaseModel):
    """Retained API run reservations, read from ``runs_idempotency.db``.

    **An empty store is not an idle API.** ``_prune_stale_terminal_locked``
    (``api_server_run_idempotency.py:168-186``) runs inside every ``reserve`` and
    ``lookup`` and deletes an aged row only once its stored status is terminal,
    and long room runs push ``retention_until`` out
    (``api_server_runs.py:56-61,222-232``). On top of that, when the file cannot
    be opened upstream logs and falls back to ``":memory:"`` (``:63-84``), setting
    ``_db_path = None`` so ``durable`` is False — and that capability is only
    served over HTTP (``api_server.py:2276``), never written to disk. hermesd
    therefore cannot distinguish "nothing retained" from "the gateway is
    reserving in process memory".

    PROFILE-scoped: ``get_hermes_home()/"runs_idempotency.db"`` (``:67``) — the
    opposite of the ROOT-scoped ``shared-state.db`` rendered beside it.
    """

    db_present: bool = False
    db_size_bytes: int = 0
    reservation_count: int = 0
    # ``COUNT(DISTINCT scope)``: the tenant scope is counted, never carried.
    scope_count: int = 0
    reservations: list[ApiRunReservation] = Field(default_factory=list)
    # True when ``reservations`` was cut short by the display cap; every count
    # here is a whole-table aggregate and is unaffected by it.
    reservations_truncated: bool = False
    acknowledged_count: int = 0
    owner_recorded_count: int = 0
    # Rows already past ``retention_until`` that remain until an opportunistic
    # request-triggered pruning pass.
    retention_expired_count: int = 0
    # Age of the most recently *updated* row, and of the oldest *created* one.
    newest_age_seconds: float | None = None
    oldest_age_seconds: float | None = None


# Upstream's receipt retention ceiling (``tools/process_registry_results.py:19-20``).
PROCESS_RECEIPT_RETENTION_SECONDS: int = 7 * 24 * 60 * 60
PROCESS_RECEIPT_MAX_FILES: int = 64

# Upstream's checkpoint auto-prune wrapper defaults (``tools/checkpoint_manager.py:1094``):
# it short-circuits within ``min_interval_hours`` of the marker, so hermesd flags
# overdue at 2x the interval — a stale marker is a missed wrapper run, not proof
# of anything about the store itself.
CHECKPOINT_PRUNE_INTERVAL_SECONDS: int = 24 * 60 * 60


def checkpoint_prune_overdue_after(interval_seconds: float) -> float:
    """When a prune marker counts as missed: twice the *effective* cadence.

    The wrapper short-circuits within ``min_interval_hours`` of the marker, so
    the overdue window follows ``checkpoints.min_interval_hours`` when it is set
    (``tools/checkpoint_manager.py:1094``) instead of a hardcoded default. One
    helper so the verdict and the bound the panel prints cannot disagree.
    """
    return 2 * interval_seconds


class ProcessReceipt(BaseModel):
    """One finished background process, from ``logs/process-results/proc_*.json``.

    Fields mirror upstream's ``_RESULT_FIELDS`` plus the redacted output tail
    (``tools/process_registry_results.py:21-25,48-61``). Upstream redacts the
    command and output before writing and marks the file 0600; hermesd redacts
    both again at the collector boundary and never carries ``cwd``,
    ``session_key`` or ``parent_session_id``.
    """

    process_id: str = ""
    command: str = ""
    exit_code: int | None = None
    completion_reason: str = ""
    termination_source: str = ""
    started_age_seconds: float | None = None
    finished_age_seconds: float | None = None
    output_tail: str = ""


class ProcessReceiptsState(BaseModel):
    """Recently finished background processes, read from PROFILE
    ``logs/process-results/``.

    Upstream prunes receipts to 7 days / 64 files
    (``tools/process_registry_results.py:28-45``), so an absent directory or an
    empty store is normal on any machine that has not run detachable processes
    lately — the panel says "no receipts yet" rather than implying a failure.
    """

    dir_present: bool = False
    receipt_count: int = 0
    newest_receipt_age_seconds: float | None = None
    receipts: list[ProcessReceipt] = Field(default_factory=list)
    receipts_truncated: bool = False


class OperationsState(BaseModel):
    dashboard_process_count: int = 0
    desktop_build_stamp: str = ""
    model_caches: list[ModelCacheSummary] = Field(default_factory=list)
    pr_monitors: list[PRMonitorSummary] = Field(default_factory=list)
    response_store_present: bool = False
    conversation_count: int = 0
    response_count: int = 0
    response_store_size_bytes: int = 0
    verification_db_present: bool = False
    verification_event_count: int = 0
    verification_failed_count: int = 0
    verification_state_count: int = 0
    verification_latest_events: list[VerificationEventSummary] = Field(default_factory=list)
    verification_roots: list[VerificationRootSummary] = Field(default_factory=list)
    moa_trace_count: int = 0
    moa_trace_size_bytes: int = 0
    moa_trace_newest_session_id: str = ""
    moa_trace_newest_mtime: float | None = None
    moa_trace_latest_record_summary: str = ""
    moa_trace_latest_record_keys: list[str] = Field(default_factory=list)
    projects_db_present: bool = False
    project_count: int = 0
    project_archived_count: int = 0
    project_folder_count: int = 0
    discovered_repo_count: int = 0
    project_missing_primary_path_count: int = 0
    projects: list[ProjectSummary] = Field(default_factory=list)
    discovered_repos: list[DiscoveredRepoSummary] = Field(default_factory=list)
    goal_count: int = 0
    active_goal_count: int = 0
    waiting_goal_count: int = 0
    goals: list[GoalSummary] = Field(default_factory=list)
    delegations: list[DelegationInfo] = Field(default_factory=list)
    delegation_count: int = 0
    delegation_running_count: int = 0
    delegation_failed_count: int = 0
    delegation_undelivered_count: int = 0
    delegation_live_log_count: int = 0
    # Written by its own source (``delegation_live``) so a torn manifest keeps
    # the last-good card list instead of blanking the panel.
    delegation_live_manifests: list[DelegationLiveManifest] = Field(default_factory=list)
    delegation_live_manifest_count: int = 0
    # Counted run dirs whose manifest could not be read into a card (over the
    # parse cap, torn, or not JSON). The count above is presence-based, so
    # without this the difference is invisible.
    delegation_live_unparsed_count: int = 0
    state_db_schema_version: int = 0
    state_db_size_bytes: int = 0
    state_db_wal_size_bytes: int = 0
    state_db_file_generation: str = ""
    state_db_fts_storage_version: str = ""
    last_auto_prune_age_seconds: float | None = None
    last_auto_archive_age_seconds: float | None = None
    snapshot_count: int = 0
    snapshot_total_bytes: int = 0
    newest_snapshot_age_seconds: float | None = None
    web_ui_build_hash: str = ""
    web_ui_built_age_seconds: float | None = None
    blocked_script_count: int = 0
    newest_blocked_script_age_seconds: float | None = None
    blocked_script_names: list[str] = Field(default_factory=list)
    # Written by its own source (``db_recovery``), so a corrupt repair ledger or
    # retired-WAL manifest degrades only this field and keeps its last-good value.
    db_recovery: DbRecoveryState = Field(default_factory=DbRecoveryState)
    # Each written by its own source (``hosted_rooms`` and ``api_runs``) for the
    # same reason: these are two more SQLite databases, and a corrupt or vanished
    # one must not take the rest of the panel's last-good values with it. They
    # also resolve to different homes — ROOT and PROFILE respectively — so they
    # are recorded as separate rows in .codex/rules/source-ownership.md.
    hosted_rooms: HostedRoomState = Field(default_factory=HostedRoomState)
    api_runs: ApiRunReservationsState = Field(default_factory=ApiRunReservationsState)
    # Written by its own source (``process_receipts``): a receipt directory that
    # disappears (7-day retention) must keep the last-good list, not blank it.
    process_receipts: ProcessReceiptsState = Field(default_factory=ProcessReceiptsState)
    # Checkpoint auto-prune wrapper marker, PROFILE ``checkpoints/.last_prune``
    # (``tools/checkpoint_manager.py:1094-1130``).
    checkpoint_prune_marker_present: bool = False
    checkpoint_prune_marker_age_seconds: float | None = None
    # The effective wrapper cadence: ``checkpoints.min_interval_hours`` from
    # config.yaml, defaulting to upstream's 24h. The overdue window is 2x this,
    # so a deliberately slower policy is never reported as a missed pass.
    checkpoint_prune_interval_seconds: float = float(CHECKPOINT_PRUNE_INTERVAL_SECONDS)
    # ROOT parking bay for an unparseable spawn ledger
    # (``hermes_cli/process_identity.py:160-171``).
    spawn_ledger_corrupt_present: bool = False
    spawn_ledger_corrupt_age_seconds: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def checkpoint_prune_overdue(self) -> bool:
        """True when a marker exists and is older than twice the configured interval.

        Caveat kept with the render copy: a fresh marker proves the wrapper RAN,
        not that pruning succeeded — per-repo failures land in the prune result,
        not in the marker — so this flag is never a store-health verdict.
        """
        age = self.checkpoint_prune_marker_age_seconds
        overdue_after = checkpoint_prune_overdue_after(self.checkpoint_prune_interval_seconds)
        return self.checkpoint_prune_marker_present and age is not None and age > overdue_after


class SkillCurationWindow(BaseModel):
    """One skill's distance to the curator's stale/archive thresholds.

    The window runs from the skill's last real activity (use, view or patch);
    ``created_at`` is deliberately excluded upstream
    (``tools/skill_usage.py:106-111``), so a never-used skill has no window at
    all rather than one that silently starts at its creation.
    """

    name: str
    state: str = ""
    pinned: bool = False
    patch_pending_reuse: bool = False
    last_activity_age_seconds: float | None = None
    days_until_stale: float | None = None
    days_until_archive: float | None = None


class CuratorRun(BaseModel):
    run_present: bool = False
    stamp: str = ""
    started_at: str = ""
    duration_seconds: float = 0.0
    model: str = ""
    provider: str = ""
    count_before: int = 0
    count_after: int = 0
    count_delta: int = 0
    archived_count: int = 0
    added_count: int = 0
    pruned_count: int = 0
    consolidated_count: int = 0
    tool_calls_total: int = 0
    tool_call_counts: dict[str, int] = Field(default_factory=dict)
    state_transitions: list[str] = Field(default_factory=list)
    llm_summary: str = ""
    llm_error: str = ""
    scheduler_state_present: bool = False
    scheduler_paused: bool = False
    scheduler_run_count: int = 0
    scheduler_last_run_at: str = ""
    scheduler_last_report_path: str = ""
    consolidate_enabled: bool = False
    # Patch-reuse loop and threshold hygiene over skills/.usage.json, using the
    # same effective thresholds the curator's transitions use
    # (agent/curator.py:29,115-120): 14/30 days by default, overridable via
    # curator.stale_after_days / curator.archive_after_days in config.yaml.
    stale_after_days: int = 14
    archive_after_days: int = 30
    thresholds_customized: bool = False
    managed_skill_count: int = 0
    patch_pending_reuse_count: int = 0
    state_active_count: int = 0
    state_stale_count: int = 0
    state_archived_count: int = 0
    state_unknown_count: int = 0
    pinned_count: int = 0
    # Display-bounded slice of the per-skill windows, soonest deadline first;
    # managed_skill_count is the complete number.
    skill_windows: list[SkillCurationWindow] = Field(default_factory=list)


class HealthSummary(BaseModel):
    total_sources: int = 0
    ok_sources: int = 0
    failed_sources: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class RuntimeStatus(BaseModel):
    # Default False: before the first successful runtime collection the agent
    # state is unknown and must read as not-running rather than running.
    # The collector sets this explicitly on every successful collect.
    agent_running: bool = False
    last_activity_age_seconds: float | None = None
    banner: str = ""


class DashboardState(BaseModel):
    hermes_home: Path = Field(default_factory=default_hermes_home)
    selected_profile: str | None = None
    profile_mode_label: str = "root"
    collected_at: float = Field(default_factory=time.time)
    is_stale: bool = False
    health: HealthSummary = Field(default_factory=HealthSummary)
    runtime: RuntimeStatus = Field(default_factory=RuntimeStatus)
    gateway: GatewayState = Field(default_factory=GatewayState)
    migration: MigrationState = Field(default_factory=MigrationState)
    sessions: list[SessionInfo] = Field(default_factory=list)
    session_coordination: SessionCoordinationState = Field(default_factory=SessionCoordinationState)
    terminal_sessions: TerminalSessionReadout = Field(default_factory=TerminalSessionReadout)
    active_surfaces: list[ActiveSurface] = Field(default_factory=list)
    active_surface_count: int = 0
    active_surfaces_truncated: bool = False
    session_message_match_query: str = ""
    session_message_match_ids: set[str] = Field(default_factory=set)
    tokens_today: TokenSummary = Field(default_factory=TokenSummary)
    tokens_total: TokenSummary = Field(default_factory=TokenSummary)
    token_analytics: TokenAnalytics = Field(default_factory=TokenAnalytics)
    tool_stats: list[ToolStats] = Field(default_factory=list)
    total_tool_calls: int = 0
    available_tools: int = 0
    available_tool_names: list[str] = Field(default_factory=list)
    toolset_availability: ToolsetAvailability = Field(default_factory=ToolsetAvailability)
    background_processes: list[BackgroundProcessInfo] = Field(default_factory=list)
    checkpoints: list[CheckpointInfo] = Field(default_factory=list)
    config: ConfigSummary = Field(default_factory=ConfigSummary)
    cron: CronState = Field(default_factory=CronState)
    cron_executions: CronExecutionsState = Field(default_factory=CronExecutionsState)
    channels: ChannelDirectoryState = Field(default_factory=ChannelDirectoryState)
    kanban: KanbanState = Field(default_factory=KanbanState)
    operations: OperationsState = Field(default_factory=OperationsState)
    skills_memory: SkillsMemory = Field(default_factory=SkillsMemory)
    mcp_cache: MCPSchemaCache = Field(default_factory=MCPSchemaCache)
    skills_prompt: SkillsPromptSnapshot = Field(default_factory=SkillsPromptSnapshot)
    memory: MemoryOverview = Field(default_factory=MemoryOverview)
    profiles: ProfilesState = Field(default_factory=ProfilesState)
    logs: LogState = Field(default_factory=LogState)
    version_behind: int = 0
    active_skin: str = "default"
    curator: CuratorRun = Field(default_factory=CuratorRun)
