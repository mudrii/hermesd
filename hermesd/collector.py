"""Public façade for the ~/.hermes collectors.

The per-domain readers live in ``hermesd.collect.*``; this module owns the
``Collector`` orchestration and re-exports the reader helpers so that
``from hermesd.collector import ...`` keeps resolving every public and
private name it resolved before the package split.
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import subprocess  # noqa: F401  # re-exported: tests patch hermesd.collector.subprocess.run
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, Literal, NamedTuple, Never, TypeVar

from pydantic import BaseModel

from hermesd.collect.api_runs import _read_api_runs
from hermesd.collect.common import (
    _MAX_TEXT_READ_BYTES,
    _age_seconds,
    _as_dict,
    _as_list,
    _coerce_float,
    _coerce_int,
    _db_source_mtime_ns,
    _exists_strict,
    _file_signature,
    _file_size,
    _int_mapping,
    _len_if_sized,
    _local_date,
    _mtime,
    _optional_epoch,
    _path_resolves_under,
    _read_tail_text,
    _read_text_capped,
    _safe_capped_file,
    _safe_child_path,
    _safe_or_absent_child_path,
    _today_epoch,
)
from hermesd.collect.config import (
    _CONFIG_BACKUP_ENTRY_LIMIT,
    _channel_capabilities,
    _config_agent_limits,
    _config_backup_groups,
    _credential_auth_type,
    _credential_expiry,
    _mcp_tool_filter_summary,
    _moa_config_summary,
    _platform_family_label,
    _provider_model_label,
    _provider_routing_summary,
    _scale_to_zero_relay_only,
    _select_pool_entry,
    _stale_alias_count,
)
from hermesd.collect.cron import (
    _chronos_configured,
    _cron_catch_up_occurrences,
    _cron_catch_up_policy,
    _cron_job_dispatch,
    _cron_job_paused,
    _cron_job_repeat,
    _cron_suggestion_count,
    _cron_ticker_ages,
    _cron_ticker_health,
    _cron_ticker_last_error,
    _delivery_target_label,
    _latest_cron_output_excerpt,
    _latest_cron_output_file,
    _read_cron_executions_state,
    _tail_latest_cron_output,
)
from hermesd.collect.desktop_plugins import read_desktop_plugins
from hermesd.collect.gateway import (
    _config_generation,
    _config_stale,
    _gateway_ledger_fields,
    _gateway_start_epoch,
    _GatewayLedgerRows,
    _heartbeat_liveness,
    _lifecycle_status,
    _platform_status,
    _read_gateway_ledger_rows,
    _record_writer,
    _update_receipt_status,
)
from hermesd.collect.hosted_rooms import _read_hosted_rooms
from hermesd.collect.kanban import (
    _kanban_claim_ttl_seconds,
    _read_kanban_board_summary,
    _read_kanban_state,
)
from hermesd.collect.logs import (
    _ERROR_LOG_TAIL_LINES,
    _LOG_LINE_PATTERN,
    _LOG_TAIL_LINES,
    _MAX_LOG_LINE_CHARS,
    _extract_session_id,
    _latest_log_mtime,
)
from hermesd.collect.migration import (
    _MANIFEST_NAME,
    _migration_state,
)
from hermesd.collect.operations import (
    StateDbRead,
    _count_delegation_live_logs,
    _curator_with_scheduler_state,
    _is_dashboard_process,
    _iso_age_seconds,
    _moa_latest_record_summary,
    _model_cache_counts,
    _read_projects_state,
    _read_state_snapshots,
    _read_verification_evidence,
    _state_db_update,
    _state_transition_label,
)
from hermesd.collect.operations import (
    _read_state_db as _read_state_db_tables,
)
from hermesd.collect.plugins import (
    CATALOG_SIDECAR_NAME,
    INSTALL_METADATA_NAME,
    MANIFEST_NAMES,
    MAX_PLUGIN_SCAN_DEPTH,
    PLUGIN_KIND_STANDALONE,
    CatalogProvenance,
    ManifestChoice,
    catalog_provenance,
    category_prefix,
    choose_manifest,
    declared_capabilities,
    gate_plugin,
    install_provenance,
    parse_portable_manifest,
    plugin_key,
    plugin_name_set,
    requires_hermes_spec,
    resolve_plugin_kind,
)
from hermesd.collect.recovery import _read_db_recovery
from hermesd.collect.redaction import (
    _has_secret_material,
    _redact_command_string,
    _redact_secret_args,
    _redact_secret_text,
    _redact_secret_url,
    _safe_exception_text,
)
from hermesd.collect.sessions import (
    _background_process_from_ledger,
    _context_limit_for,
    _count_cost_statuses,
    _estimate_cost,  # noqa: F401  # re-exported for hermesd.collector compatibility
    _read_session_tools,
    _resolved_session_cost,
    _summarize_breakdown,
    _summarize_tokens,
    _summarize_window,
    _tool_names_from_entries,
)
from hermesd.collect.skills import (
    _count_skills,
    _learning_summary,
    _mcp_schema_cache_summary,
    _memory_card_count,
    _read_soul_excerpt,
    _skill_description,
    _skill_frontmatter,
    _skills_prompt_summary,
    _toolset_availability,
    _word_count,
)
from hermesd.collect.sqlite_util import (
    _connect_readonly_sqlite,
    _table_count_or_zero,
)
from hermesd.collect.system import (
    _RECENT_ACTIVITY_WINDOW_SECONDS,
    _git_checkpoint_summary,
    _git_ref_signature,
    _latest_runtime_activity_age,
    _lease_age_seconds,
    _observed_process_start_times,
    _pid_exists,
    _surface_liveness,
)
from hermesd.db import HermesDB
from hermesd.defaults import DEFAULT_LOG_TAIL_BYTES
from hermesd.file_cache import JsonMapping, JsonObjectList, LastGoodFileCache
from hermesd.models import (
    PORTABLE_MANIFEST_NAME,
    ActiveSurface,
    BackgroundProcessInfo,
    ChannelDirectoryState,
    ChannelPlatformInfo,
    CheckpointInfo,
    ConfigSummary,
    CredentialPoolEntry,
    CronExecutionsState,
    CronJob,
    CronState,
    CuratorRun,
    DashboardState,
    DesktopPluginInfo,
    GatewayLoopHealth,
    GatewayState,
    HealthSummary,
    HookInfo,
    KanbanBoardSummary,
    KanbanState,
    LogLine,
    LogState,
    LogStream,
    MCPSchemaCache,
    MCPServerInfo,
    MemoryOverview,
    MigrationState,
    ModelCacheSummary,
    ModelUsage,
    OperationsState,
    PluginActivation,
    PluginInfo,
    PRMonitorSummary,
    ProfilesState,
    ProfileSummary,
    ProviderInfo,
    RuntimeStatus,
    SessionInfo,
    SkillInfo,
    SkillsMemory,
    SkillsPromptSnapshot,
    SourceScope,
    TokenAnalytics,
    TokenSummary,
    ToolGatewayRoute,
    ToolsetAvailability,
    ToolStats,
)
from hermesd.paths import HermesPaths
from hermesd.theme import normalize_skin_name

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)


def _closing_source() -> Never:
    raise RuntimeError("collector is closing")


@dataclass(frozen=True, slots=True)
class _StateDbReadout:
    """One state.db pass: the operations tables plus the raw gateway ledgers."""

    state: StateDbRead
    ledgers: _GatewayLedgerRows


def _state_db_readout(conn: sqlite3.Connection) -> _StateDbReadout:
    """Every state.db-backed source's raw rows, read from one connection."""
    return _StateDbReadout(
        state=_read_state_db_tables(conn),
        ledgers=_read_gateway_ledger_rows(conn),
    )


# Fields each gateway sub-source owns, used to restore just that source's
# values from the last good state when it fails.
_HEARTBEAT_FIELDS = ("heartbeat_age_seconds", "loop_health")
# Upper bound on runtime/active_sessions.json entries turned into surfaces. Each
# entry costs a liveness syscall per refresh, so an oversized file must not be
# able to stall the collector thread.
_ACTIVE_SURFACE_LIMIT = 200
# plugins/ scan bounds. The tree is walked at most two levels deep (one level of
# category recursion, matching upstream), and both the per-directory listing and
# the retained plugin list are capped because ~/.hermes is untrusted input read
# every refresh. Hitting either cap sets SkillsMemory.plugin_scan_truncated, so a
# bounded inventory never presents itself as a complete one.
_PLUGIN_DIR_ENTRY_LIMIT = 200
_PLUGIN_LIMIT = 200
# The desktop inventory enriches SkillsMemory through an independent health
# source so a transient root listing failure cannot blank agent integrations.
_DESKTOP_PLUGIN_FIELDS = ("desktop_plugins", "desktop_plugin_scan_truncated")
# backups/config/ scan — the fields the config-backups source owns on
# ConfigSummary, so its last-good fallback restores exactly those.
_CONFIG_BACKUP_FIELDS = (
    "config_backups_present",
    "config_backup_groups",
    "config_backup_groups_truncated",
)
# cache/blocked-scripts/ scan bounds and the fields the source owns.
_BLOCKED_SCRIPT_SCAN_LIMIT = 200
_BLOCKED_SCRIPT_NAME_LIMIT = 3
_MAX_FILE_LABEL_CHARS = 40
_BLOCKED_SCRIPT_FIELDS = (
    "blocked_script_count",
    "newest_blocked_script_age_seconds",
    "blocked_script_names",
)
# The recovery source owns exactly one nested field, so a corrupt repair ledger or
# retired-WAL manifest restores that whole value from its own last-good read.
_DB_RECOVERY_FIELDS = ("db_recovery",)
# Same shape for the two coordination databases: each source owns one nested
# field, so a corrupt shared-state.db or runs_idempotency.db degrades only itself.
_HOSTED_ROOM_FIELDS = ("hosted_rooms",)
_API_RUN_FIELDS = ("api_runs",)
_STATE_SNAPSHOT_FIELDS = ("snapshot_count", "snapshot_total_bytes", "newest_snapshot_age_seconds")
_LIFECYCLE_FIELDS = (
    "lifecycle_phase",
    "last_exit_code",
    "last_exit_reason",
    "unclean_previous_exit",
)
_UPDATE_RECEIPT_FIELDS = (
    "last_update_outcome",
    "last_update_finished_age_seconds",
    "last_update_from_version",
    "last_update_to_version",
    "last_update_failed_step",
    "runtime_code_skew",
    "runtime_code_skew_source",
    "update_receipt_unfinished",
    "update_fleet_states",
    "update_fleet_runtime_count",
)
_LEDGER_FIELDS = (
    "gateway_incarnation_count",
    "gateway_restarts_24h",
    "current_incarnation_uptime_seconds",
    "pending_delivery_count",
    "failed_delivery_count",
    "pending_deliveries",
)
# Bucket for the time-dependent part of derived-cache keys: sliding 7d/30d
# window cutoffs recompute at most this often when nothing else changed
# (matches the 60s cutoff bucketing in db.py's model-usage reads).
_DERIVED_WINDOW_BUCKET_SECONDS = 60


class _SourceSpec(NamedTuple):
    """One entry in the dashboard-state collection table.

    ``field`` is the DashboardState field the result lands in; ``source_name``
    keys the per-source last-good baseline read by the default fallback.
    ``fallback`` overrides that default for the sources whose fallback is not a
    plain per-source value read.
    """

    field: str
    source_name: str
    collect: Callable[[], Any]
    default_factory: Callable[[], Any]
    fallback: Callable[[], Any] | None = None


@dataclass(frozen=True, slots=True)
class _ModelUsageBundle:
    """Per-model usage rows for the all-time, 24h and 7d windows."""

    usage_source: Literal["session_model_usage", "sessions"] = "sessions"
    all_time: tuple[ModelUsage, ...] = ()
    last_24h: tuple[ModelUsage, ...] = ()
    last_7d: tuple[ModelUsage, ...] = ()


_EMPTY_MODEL_USAGE_BUNDLE = _ModelUsageBundle()


@dataclass(frozen=True, slots=True)
class _ActiveSurfaceReadout:
    """Bounded lease rows plus the selected registry's complete occupancy."""

    surfaces: tuple[ActiveSurface, ...] = ()
    total_count: int = 0


_EMPTY_ACTIVE_SURFACE_READOUT = _ActiveSurfaceReadout()


def _model_usage_from_rows(rows: list[dict[str, Any]]) -> tuple[ModelUsage, ...]:
    return tuple(
        ModelUsage(
            model=row.get("model") or "",
            provider=row.get("provider") or "",
            task=row.get("task") or "",
            api_calls=row.get("api_calls") or 0,
            input_tokens=row.get("input_tokens") or 0,
            output_tokens=row.get("output_tokens") or 0,
            cache_read_tokens=row.get("cache_read_tokens") or 0,
            cache_write_tokens=row.get("cache_write_tokens") or 0,
            reasoning_tokens=row.get("reasoning_tokens") or 0,
            estimated_cost_usd=_coerce_float(row.get("estimated_cost_usd")),
            actual_cost_usd=_coerce_float(row.get("actual_cost_usd")),
            has_actual_cost=_coerce_float(row.get("actual_cost_usd")) > 0,
            reported_cost_usd=_coerce_float(row.get("reported_cost_usd")),
            estimated_only_cost_usd=_coerce_float(row.get("estimated_only_cost_usd")),
            reported_row_count=row.get("reported_row_count") or 0,
            row_count=row.get("row_count") or 0,
            last_seen=row.get("last_seen") or 0.0,
        )
        for row in rows
    )


def _read_blocked_scripts(root: Path, home: Path, *, now: float) -> dict[str, Any]:
    """Stat ``cache/blocked-scripts/`` (bounded); contents are never read.

    These are shell scripts the agent refused to run, so only the file name,
    the count and the newest mtime are surfaced.
    """
    if not _safe_child_path(root, home) or not root.is_dir():
        return {
            "blocked_script_count": 0,
            "newest_blocked_script_age_seconds": None,
            "blocked_script_names": [],
        }
    entries: list[tuple[float, str]] = []
    for entry in sorted(islice(root.iterdir(), _BLOCKED_SCRIPT_SCAN_LIMIT)):
        if entry.is_symlink() or not entry.is_file() or not _path_resolves_under(entry, home):
            continue
        entries.append((_mtime(entry) or 0.0, entry.name))
    entries.sort(key=lambda item: (-item[0], item[1]))
    newest = entries[0][0] if entries else None
    return {
        "blocked_script_count": len(entries),
        "newest_blocked_script_age_seconds": max(0.0, now - newest) if newest else None,
        "blocked_script_names": [
            _sanitized_file_label(name) for _, name in entries[:_BLOCKED_SCRIPT_NAME_LIMIT]
        ],
    }


def _sanitized_file_label(name: str) -> str:
    """Printable, length-capped file name safe to hand to a panel."""
    return "".join(char for char in name if char.isprintable())[:_MAX_FILE_LABEL_CHARS]


def _path_confirmed_gone(path: Path) -> bool:
    """True only when the path is verifiably absent; stat errors keep the entry."""
    try:
        return not path.exists()
    except OSError:
        return False


def _memory_file_names(memories_dir: Path) -> list[str]:
    """Sorted memory documents, excluding the agent's ``*.lock`` files and dotfiles."""
    if not memories_dir.is_dir():
        return []
    return sorted(
        path.name
        for path in memories_dir.iterdir()
        if path.is_file() and not path.name.startswith(".") and path.suffix != ".lock"
    )


def _pr_monitor_summary(filename: str, data: Mapping[str, Any]) -> PRMonitorSummary:
    """One pr-monitor document, in either the repo/prs or the PR-keyed shape."""
    entries = _pr_keyed_entries(data)
    if entries is None:
        return PRMonitorSummary(
            filename=filename,
            repo=str(data.get("repo") or ""),
            checked_at=str(data.get("checked_at") or ""),
            monitored_count=_len_if_sized(data.get("prs")) or _len_if_sized(data.get("monitored")),
            tracked_count=_len_if_sized(data.get("tracked_numbers"))
            or _len_if_sized(data.get("tracked")),
            author_pr_count=_len_if_sized(data.get("author_prs"))
            or _len_if_sized(data.get("author_pr_numbers")),
        )
    updated = [str(entry.get("updatedAt") or "") for entry in entries]
    return PRMonitorSummary(
        filename=filename,
        checked_at=max(updated, default=""),
        monitored_count=len(entries),
        tracked_count=len(entries),
        open_count=sum(1 for entry in entries if str(entry.get("state") or "").upper() == "OPEN"),
        conflicting_count=sum(
            1 for entry in entries if str(entry.get("mergeable") or "").upper() == "CONFLICTING"
        ),
    )


def _pr_keyed_entries(data: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """The PR entries when every top-level key is a PR number, else ``None``."""
    if not data:
        return None
    if not all(str(key).isdigit() and isinstance(value, dict) for key, value in data.items()):
        return None
    return [value for value in data.values() if isinstance(value, dict)]


@dataclass(slots=True)
class _CollectionHealth:
    failed_sources: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    total_sources: int = 0

    def mark_failed(self, source_name: str, error: str) -> None:
        if source_name not in self.failed_sources:
            self.failed_sources.append(source_name)
        self.errors[source_name] = error

    def collect(
        self,
        fallback: Callable[[], T],
        source_name: str,
        fn: Callable[[], T],
        default_factory: Callable[[], T],
    ) -> T:
        self.total_sources += 1
        try:
            return fn()
        except Exception as exc:
            self.mark_failed(source_name, _safe_exception_text(exc))
            try:
                return fallback()
            except Exception as fallback_exc:
                self.errors[source_name] = (
                    f"{_safe_exception_text(exc)}; fallback={_safe_exception_text(fallback_exc)}"
                )
                return default_factory()


class Collector:
    def __init__(
        self,
        hermes_home: Path,
        pid_exists: Callable[[int], bool] | None = None,
        process_start_times: Callable[[Sequence[int]], dict[int, float]] | None = None,
        profile_name: str | None = None,
        log_tail_bytes: int = DEFAULT_LOG_TAIL_BYTES,
        db_factory: Callable[[Path], HermesDB] | None = None,
        file_cache: LastGoodFileCache | None = None,
        clock: Callable[[], float] = time.time,
        env: Mapping[str, str] | None = None,
    ):
        self._root_home = hermes_home
        self._file_cache = file_cache if file_cache is not None else LastGoodFileCache()
        self._log_cache: dict[str, list[LogLine]] = {}
        self._pid_exists = pid_exists or _pid_exists
        self._process_start_times = process_start_times or _observed_process_start_times
        self._log_tail_bytes = max(1, log_tail_bytes)
        self._paths = HermesPaths(hermes_home, profile_name)
        if db_factory is None:
            # Wire allowed_root so profile db targets are re-validated against
            # symlink swaps on every (re)connect, not just at startup.
            db_factory = functools.partial(HermesDB, allowed_root=self._paths.root_home)
        self._db_factory = db_factory
        self._db = db_factory(self._paths.profile_path("state.db"))
        self._env = env if env is not None else os.environ
        self._clock = clock
        self._available_tools_cache_key: (
            tuple[
                tuple[str, int, int] | None,
                tuple[tuple[str, int, int] | None, ...],
            ]
            | None
        ) = None
        self._available_tools_cache_value: tuple[int, list[str]] = (0, [])
        # Once a gateway_state.json writer is observed dead, the unchanged file
        # cannot become authoritative merely because that numeric PID reappears.
        self._invalidated_gateway_state_signature: tuple[str, int, int] | None = None
        self._session_tool_names_cache: dict[
            str, tuple[tuple[str, int, int] | None, tuple[str, ...]]
        ] = {}
        # Last successful value per source name. A source's fallback baseline
        # advances whenever that source succeeds, so one permanently failing
        # source cannot freeze every other source's last-good data (which a
        # whole-state snapshot taken only on fully clean passes did).
        self._last_good_by_source: dict[str, Any] = {}
        self._last_session_rows: list[dict[str, Any]] = []
        self._log_stream_cache: dict[str, tuple[float | None, int, LogStream]] = {}
        self._cron_excerpt_cache: dict[
            str,
            tuple[
                tuple[str, int, int] | tuple[str, float | None],
                tuple[str, bool, str, float | None],
            ],
        ] = {}
        self._profile_count_cache: dict[str, tuple[int | None, int]] = {}
        self._kanban_board_cache: dict[str, KanbanBoardSummary] = {}
        # One state.db readout per changed mtime, shared by the goal/delegation
        # state and the gateway ledgers so a pass snapshots the (large, WAL)
        # db only once.
        self._state_db_cache: tuple[int, _StateDbReadout] | None = None
        self._checkpoint_summary_cache: dict[
            str, tuple[tuple[int, ...], tuple[int, float | None, str]]
        ] = {}
        # Derived values (word counts, card counts, excerpts, frontmatter)
        # keyed on the source file's signature, so an unchanged SKILL.md /
        # MEMORY.md / USER.md / SOUL.md is not re-read on every tick.
        self._derived_file_cache: dict[str, tuple[tuple[str, int, int] | None, Any]] = {}
        self._kanban_board_errors: list[str] = []
        # Session-row-derived values, one entry per derived name, keyed on
        # (rows identity, local date, entry-specific deps) — see
        # _derived_from_rows.
        self._derived_cache: dict[str, tuple[tuple[object, ...], Any]] = {}
        self._closed = False
        # Set by close() before it queues for _lock; an in-flight collect pass
        # checks it between sources and stops doing new work.
        self._closing = threading.Event()
        # _lock serializes collect() passes and guards the collector-internal
        # caches mutated during a pass (_file_cache, _log_cache,
        # _log_stream_cache, _available_tools_cache_*, _profile_count_cache,
        # _derived_*) plus _last_good_by_source/_last_session_rows. It is
        # deliberately NOT taken by search_session_ids_by_message(): HermesDB
        # serializes its own access, so a slow collect pass (git subprocesses,
        # per-profile DB snapshots) must not stall message search.
        self._lock = threading.RLock()

    def collect(self) -> DashboardState:
        with self._lock:
            if self._closed:
                raise RuntimeError("collector is closed")
            self._prune_stale_caches()
            health = _CollectionHealth()
            session_rows = self._collect_session_rows(health)
            return self._build_dashboard_state(health, session_rows)

    def _prune_stale_caches(self) -> None:
        """Evict path-keyed cache entries whose backing file or board is gone."""
        boards_dir = self._paths.shared_path("kanban", "boards")
        self._kanban_board_cache = {
            slug: summary
            for slug, summary in self._kanban_board_cache.items()
            if not _path_confirmed_gone(boards_dir / slug)
        }
        self._derived_file_cache = {
            key: entry
            for key, entry in self._derived_file_cache.items()
            if not _path_confirmed_gone(Path(key.split(":", 1)[1]))
        }
        self._cron_excerpt_cache = {
            key: entry
            for key, entry in self._cron_excerpt_cache.items()
            if not _path_confirmed_gone(Path(key.rsplit(":", 1)[0]) / key.rsplit(":", 1)[1])
        }

    def _build_dashboard_state(
        self,
        health: _CollectionHealth,
        session_rows: list[dict[str, Any]],
    ) -> DashboardState:
        session_rows_stale = "sessions" in health.failed_sources
        results: dict[str, Any] = {}

        def derived(
            name: str,
            compute: Callable[[list[dict[str, Any]]], T],
            deps: Callable[[], tuple[object, ...]] | None = None,
        ) -> Callable[[], T]:
            # Shared shape for every session-row-derived source: memoized via
            # _derived_from_rows on fresh (non-stale) rows. `deps` supplies the
            # extra per-entry key parts (config file signatures, time buckets)
            # beyond rows identity and the local date.
            return lambda: self._derived_from_rows(
                name,
                self._fresh_session_rows(session_rows, session_rows_stale),
                compute,
                deps=deps() if deps is not None else (),
            )

        def last_good(source_name: str, default_factory: Callable[[], Any]) -> Callable[[], Any]:
            # Per-source baseline: the value this source last produced
            # successfully, regardless of how other sources fared that pass.
            return lambda: self._last_good_by_source.get(source_name, default_factory())

        # Collected in order: entries below may read an earlier source's result
        # out of `results` (channels/runtime need gateway, operations needs the
        # background processes).
        specs = (
            _SourceSpec(
                "sessions",
                "session_models",
                # Session rows join context_length_cache.yaml, so that file's
                # signature is part of the derived-entry key: editing it must
                # not wait on a SQLite data_version change.
                derived(
                    "sessions",
                    self._collect_sessions,
                    deps=self._context_length_signature,
                ),
                list,
            ),
            _SourceSpec(
                "available_tools",
                "tools_index",
                self._collect_available_tools,
                lambda: (0, []),
                # One source feeds two state fields, so its last-good fallback
                # cannot be a plain per-source value read.
                fallback=self._last_available_tools,
            ),
            _SourceSpec(
                "toolset_availability",
                "toolset_availability",
                self._collect_toolset_availability,
                ToolsetAvailability,
            ),
            _SourceSpec("gateway", "gateway", self._collect_gateway, GatewayState),
            # Four sources enrich the same `gateway` field in place: each one
            # fails (and falls back) independently, so a corrupt heartbeat file
            # cannot discard the freshly read gateway_state.json.
            _SourceSpec(
                "gateway",
                "gateway_heartbeat",
                lambda: self._with_heartbeat(results["gateway"]),
                lambda: results["gateway"],
                fallback=lambda: self._last_source_fields(
                    "gateway_heartbeat", results["gateway"], _HEARTBEAT_FIELDS
                ),
            ),
            _SourceSpec(
                "gateway",
                "gateway_lifecycle",
                lambda: self._with_lifecycle(results["gateway"]),
                lambda: results["gateway"],
                fallback=lambda: self._last_source_fields(
                    "gateway_lifecycle", results["gateway"], _LIFECYCLE_FIELDS
                ),
            ),
            _SourceSpec(
                "gateway",
                "update_receipt",
                lambda: self._with_update_receipt(results["gateway"]),
                lambda: results["gateway"],
                fallback=lambda: self._last_source_fields(
                    "update_receipt", results["gateway"], _UPDATE_RECEIPT_FIELDS
                ),
            ),
            _SourceSpec(
                "gateway",
                "gateway_ledgers",
                lambda: self._with_gateway_ledgers(results["gateway"]),
                lambda: results["gateway"],
                fallback=lambda: self._last_source_fields(
                    "gateway_ledgers", results["gateway"], _LEDGER_FIELDS
                ),
            ),
            # Own source_name so a torn gateway_migration.json (upstream writes it
            # with a plain write_text) degrades only the migration verdict and keeps
            # its own last-good value, leaving the gateway beside it fresh.
            _SourceSpec(
                "migration",
                "migration",
                lambda: self._collect_migration(
                    results["gateway"], gateway_fresh="gateway" not in health.failed_sources
                ),
                MigrationState,
            ),
            _SourceSpec(
                "tokens_today",
                "tokens_today",
                derived("tokens_today", self._collect_tokens_today),
                TokenSummary,
            ),
            _SourceSpec(
                "tokens_total",
                "tokens_total",
                derived("tokens_total", self._collect_tokens_total),
                TokenSummary,
            ),
            _SourceSpec(
                "token_analytics",
                "token_analytics",
                # The 7d/30d windows slide with the clock; the time bucket lets
                # a session age out of a window without waiting for midnight.
                derived(
                    "token_analytics",
                    self._collect_token_analytics,
                    deps=self._window_time_bucket,
                ),
                TokenAnalytics,
            ),
            _SourceSpec(
                "tool_stats",
                "tool_stats",
                lambda: self._collect_tool_stats(
                    session_rows,
                    session_rows_stale=session_rows_stale,
                ),
                list,
            ),
            _SourceSpec(
                "total_tool_calls",
                "tool_call_total",
                derived("tool_call_total", self._collect_total_tool_calls),
                int,
            ),
            _SourceSpec(
                "background_processes",
                "background_processes",
                self._collect_background_processes,
                list,
            ),
            _SourceSpec("checkpoints", "checkpoints", self._collect_checkpoints, list),
            _SourceSpec("config", "config", self._collect_config, ConfigSummary),
            # Second writer of the `config` field: backups/config/ is scanned and
            # grouped on its own source so a hostile directory (or a symlink
            # swap) degrades only the backup audit trail, not the settings read
            # out of config.yaml itself.
            _SourceSpec(
                "config",
                "config_backups",
                lambda: self._with_config_backups(results["config"]),
                lambda: results["config"],
                fallback=lambda: self._last_source_fields(
                    "config_backups", results["config"], _CONFIG_BACKUP_FIELDS
                ),
            ),
            _SourceSpec("cron", "cron", self._collect_cron, CronState),
            # Split from "cron" so a corrupt executions.db keeps jobs.json data.
            _SourceSpec(
                "cron_executions",
                "cron_executions",
                lambda: self._collect_cron_executions(results["cron"]),
                CronExecutionsState,
            ),
            _SourceSpec(
                "channels",
                "channels",
                lambda: self._collect_channels(results["gateway"]),
                ChannelDirectoryState,
            ),
            _SourceSpec("kanban", "kanban", self._collect_kanban, KanbanState),
            _SourceSpec(
                "operations",
                "operations",
                lambda: self._collect_operations(results["background_processes"]),
                OperationsState,
            ),
            # Second writer of the `operations` field: state-snapshots/ can hold
            # gigabytes, so a failed scan degrades to the operations state
            # collected above instead of blanking the whole panel.
            _SourceSpec(
                "operations",
                "state_snapshots",
                lambda: self._with_state_snapshots(results["operations"]),
                lambda: results["operations"],
                fallback=lambda: self._last_source_fields(
                    "state_snapshots", results["operations"], _STATE_SNAPSHOT_FIELDS
                ),
            ),
            # Third writer of `operations`: an unreadable blocked-scripts dir
            # keeps the last-good counts rather than reporting a false zero.
            _SourceSpec(
                "operations",
                "blocked_scripts",
                lambda: self._with_blocked_scripts(results["operations"]),
                lambda: results["operations"],
                fallback=lambda: self._last_source_fields(
                    "blocked_scripts", results["operations"], _BLOCKED_SCRIPT_FIELDS
                ),
            ),
            # Fourth writer of `operations`: the state.db recovery artifacts are
            # read as presence and metadata only, and a torn repair ledger or
            # retired-WAL manifest must never read as "no failed repairs".
            _SourceSpec(
                "operations",
                "db_recovery",
                lambda: self._with_db_recovery(results["operations"]),
                lambda: results["operations"],
                fallback=lambda: self._last_source_fields(
                    "db_recovery", results["operations"], _DB_RECOVERY_FIELDS
                ),
            ),
            # Fifth writer of `operations`: hosted-room coordination lives in its
            # own ROOT-scoped database, so a corrupt shared-state.db must not take
            # the rest of the panel's last-good values with it.
            _SourceSpec(
                "operations",
                "hosted_rooms",
                lambda: self._with_hosted_rooms(results["operations"]),
                lambda: results["operations"],
                fallback=lambda: self._last_source_fields(
                    "hosted_rooms", results["operations"], _HOSTED_ROOM_FIELDS
                ),
            ),
            # Sixth writer of `operations`: the API run replay window is a
            # separate PROFILE-scoped database again, and it fails independently
            # for the same reason.
            _SourceSpec(
                "operations",
                "api_runs",
                lambda: self._with_api_runs(results["operations"]),
                lambda: results["operations"],
                fallback=lambda: self._last_source_fields(
                    "api_runs", results["operations"], _API_RUN_FIELDS
                ),
            ),
            _SourceSpec("skills_memory", "skills", self._collect_skills_memory, SkillsMemory),
            _SourceSpec(
                "skills_memory",
                "desktop_plugins",
                lambda: self._with_desktop_plugins(results["skills_memory"]),
                lambda: results["skills_memory"],
                fallback=lambda: self._last_source_fields(
                    "desktop_plugins", results["skills_memory"], _DESKTOP_PLUGIN_FIELDS
                ),
            ),
            _SourceSpec("mcp_cache", "mcp_cache", self._collect_mcp_cache, MCPSchemaCache),
            _SourceSpec(
                "skills_prompt",
                "skills_prompt",
                self._collect_skills_prompt,
                SkillsPromptSnapshot,
            ),
            _SourceSpec("memory", "memory", self._collect_memory, MemoryOverview),
            _SourceSpec("profiles", "profiles", self._collect_profiles, ProfilesState),
            _SourceSpec("logs", "logs", self._collect_logs, LogState),
            _SourceSpec("version_behind", "version_check", self._collect_version_behind, int),
            # Without a last good read the fallback is the shipped skin name,
            # not the "" that the str default factory would yield.
            _SourceSpec("active_skin", "skin", self._collect_skin, str, fallback=self._last_skin),
            _SourceSpec("curator", "curator", self._collect_curator, CuratorRun),
            _SourceSpec(
                "model_usage",
                "model_usage",
                self._collect_model_usage,
                lambda: _EMPTY_MODEL_USAGE_BUNDLE,
                # The bundle is merged into token_analytics below, so its
                # last-good value is not a plain per-source value read.
                fallback=self._last_model_usage,
            ),
            _SourceSpec(
                "active_surface_readout",
                "active_sessions",
                self._collect_active_surfaces,
                lambda: _EMPTY_ACTIVE_SURFACE_READOUT,
            ),
            _SourceSpec(
                "runtime",
                "runtime",
                lambda: self._collect_runtime_status(results["gateway"], results["sessions"]),
                RuntimeStatus,
            ),
        )

        self._kanban_board_errors = []
        pass_good: dict[str, Any] = {}
        for spec in specs:
            # close() sets _closing before it queues for the collect lock, so a
            # quit does not wait out a full pass: every source after the current
            # one falls straight back to its last-good value.
            fn = _closing_source if self._closing.is_set() else spec.collect
            results[spec.field] = health.collect(
                spec.fallback or last_good(spec.source_name, spec.default_factory),
                spec.source_name,
                fn,
                spec.default_factory,
            )
            if spec.source_name not in health.failed_sources:
                pass_good[spec.source_name] = results[spec.field]
        if self._kanban_board_errors:
            health.mark_failed("kanban", "; ".join(self._kanban_board_errors))
        # Advance each source's last-good baseline only when that source
        # succeeded this pass; a failed source's fallback/default value must
        # never become the next pass's baseline. Kanban board errors are
        # reported after the loop, so the merge re-checks failed_sources.
        for source_name, value in pass_good.items():
            if source_name not in health.failed_sources:
                self._last_good_by_source[source_name] = value

        tool_count, tool_names = results.pop("available_tools")
        model_usage = results.pop("model_usage")
        active_surface_readout = results.pop("active_surface_readout")
        results["token_analytics"] = results["token_analytics"].model_copy(
            update={
                "usage_source": model_usage.usage_source,
                "model_usage_all": list(model_usage.all_time),
                "model_usage_24h": list(model_usage.last_24h),
                "model_usage_7d": list(model_usage.last_7d),
            }
        )
        health_summary = HealthSummary(
            total_sources=health.total_sources,
            ok_sources=health.total_sources - len(health.failed_sources),
            failed_sources=sorted(health.failed_sources),
            errors={source: health.errors[source] for source in sorted(health.errors)},
        )
        # Every remaining `results` key is a DashboardState field name by
        # construction: _SourceSpec.field is what this expansion keys off.
        return DashboardState(
            hermes_home=self._paths.root_home,
            selected_profile=self._paths.profile_name,
            profile_mode_label=self._paths.profile_mode_label,
            collected_at=self._clock(),
            health=health_summary,
            available_tools=tool_count,
            available_tool_names=tool_names,
            active_surfaces=list(active_surface_readout.surfaces),
            active_surface_count=active_surface_readout.total_count,
            active_surfaces_truncated=(
                active_surface_readout.total_count > len(active_surface_readout.surfaces)
            ),
            **results,
        )

    def _last_available_tools(self) -> tuple[int, list[str]]:
        cached: tuple[int, list[str]] = self._last_good_by_source.get("tools_index", (0, []))
        return cached

    def _last_skin(self) -> str:
        skin: str = self._last_good_by_source.get("skin", "default")
        return skin

    def _last_source_fields(self, source_name: str, current: M, fields: tuple[str, ...]) -> M:
        """Restore one source's fields from its own last successful result."""
        last: M | None = self._last_good_by_source.get(source_name)
        if last is None:
            return current
        return current.model_copy(update={name: getattr(last, name) for name in fields})

    def _fresh_session_rows(
        self,
        rows: list[dict[str, Any]],
        session_rows_stale: bool,
    ) -> list[dict[str, Any]]:
        if session_rows_stale:
            raise RuntimeError("session rows are stale")
        return rows

    def _context_length_signature(self) -> tuple[object, ...]:
        return (_file_signature(self._paths.shared_path("context_length_cache.yaml")),)

    def _window_time_bucket(self) -> tuple[object, ...]:
        return (int(self._clock() // _DERIVED_WINDOW_BUCKET_SECONDS),)

    def _derived_from_rows(
        self,
        name: str,
        rows: list[dict[str, Any]],
        compute: Callable[[list[dict[str, Any]]], T],
        *,
        deps: tuple[object, ...] = (),
    ) -> T:
        # HermesDB returns the same cached list object while data_version is
        # unchanged, so row identity is a cheap invalidation key (the collector
        # holds the list alive via _last_session_rows, so the id cannot be
        # recycled). The local date is part of every key because "today"
        # aggregates shift at midnight; deps carry the entry-specific
        # dependencies (config file signatures, time buckets for the sliding
        # windows) so a change there recomputes only the entries that consume
        # it instead of invalidating the whole cache.
        key = (id(rows), _local_date(self._clock()), deps)
        cached = self._derived_cache.get(name)
        if cached is not None and cached[0] == key:
            # type-ignore[no-any-return]: heterogeneous per-name cache; each
            # call site pins T via its compute callable.
            return cached[1]  # type: ignore[no-any-return]
        value = compute(rows)
        self._derived_cache[name] = (key, value)
        return value

    def _collect_session_rows(
        self,
        health: _CollectionHealth,
    ) -> list[dict[str, Any]]:
        def empty_rows() -> list[dict[str, Any]]:
            return []

        session_rows = health.collect(
            lambda: self._last_session_rows,
            "sessions",
            self._db.read_sessions,
            empty_rows,
        )
        if self._db.last_read_sessions_stale:
            health.mark_failed("sessions", "read_sessions returned cached rows after sqlite error")
        self._last_session_rows = session_rows
        return session_rows

    def _signature_cached(self, kind: str, path: Path, compute: Callable[[], T]) -> T:
        """Memoize a value derived from path until the file's signature changes.

        A file that cannot be stat'd has no usable key, so its value is
        recomputed; that path is also the cheap one (no successful open).
        """
        key = f"{kind}:{path}"
        signature = _file_signature(path)
        cached = self._derived_file_cache.get(key)
        if cached is not None and signature is not None and cached[0] == signature:
            # type-ignore[no-any-return]: heterogeneous per-kind cache; each
            # call site pins T via its compute callable.
            return cached[1]  # type: ignore[no-any-return]
        value = compute()
        if signature is not None:
            self._derived_file_cache[key] = (signature, value)
        return value

    def _cached_word_count(self, path: Path, root: Path) -> int:
        return self._signature_cached("words", path, lambda: _word_count(path, root))

    def _cached_card_count(self, path: Path, root: Path) -> int:
        return self._signature_cached("cards", path, lambda: _memory_card_count(path, root))

    def _cached_soul_excerpt(self, path: Path, root: Path) -> str:
        return self._signature_cached("soul", path, lambda: _read_soul_excerpt(path, root))

    def _cached_frontmatter(self, path: Path, root: Path | None = None) -> dict[str, Any]:
        return self._signature_cached("frontmatter", path, lambda: _skill_frontmatter(path, root))

    def _read_json_cached(self, path: Path) -> JsonMapping:
        return self._file_cache.read_json_mapping(path)

    def _read_json_confined(self, path: Path) -> JsonMapping:
        """Read a JSON mapping under ~/.hermes; a path that escapes reads as absent."""
        if not _safe_child_path(path, self._paths.root_home):
            return {}
        return self._read_json_cached(path)

    def _read_json_reporting_stale(self, path: Path) -> JsonMapping:
        """Read a JSON mapping, raising when the file cache had to serve last-good.

        Silently reusing the cached value would leave a corrupt source invisible
        in ``health.failed_sources``; raising names the source while the caller's
        fallback still preserves the cached data.
        """
        data = self._read_json_cached(path)
        if self._file_cache.last_read_was_stale(path):
            raise RuntimeError(f"{path.name} is unreadable; keeping last-good values")
        return data

    def _read_json_list_cached(self, path: Path) -> JsonObjectList:
        return self._file_cache.read_json_list(path)

    def _read_yaml_cached(self) -> JsonMapping:
        return self._file_cache.read_yaml_mapping(self._paths.shared_path("config.yaml"))

    def _read_yaml_reporting_stale(self) -> JsonMapping:
        """Read config.yaml, raising when the file cache had to serve last-good.

        Same contract as ``_read_json_reporting_stale``: a config that was once
        readable and is now malformed or unreadable must degrade the reading
        source's health instead of silently showing the stale mapping. A file
        that was never readable (or is absent) is not stale and does not raise.
        """
        data = self._read_yaml_cached()
        if self._file_cache.last_read_was_stale(self._paths.shared_path("config.yaml")):
            raise RuntimeError("config.yaml is unreadable; keeping last-good values")
        return data

    def search_session_ids_by_message(self, query: str) -> set[str]:
        # Intentionally no self._lock here: HermesDB serializes its own reads,
        # and taking the collect lock would block searches for the full
        # duration of a slow collect pass (see _lock comment in __init__).
        session_ids = self._db.search_session_ids_by_message(query)
        if self._db.last_message_search_stale:
            raise RuntimeError("message search returned cached rows after sqlite error")
        return session_ids

    def _session_rows_or_read(self, rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return rows if rows is not None else self._db.read_sessions()

    def _collect_gateway(self) -> GatewayState:
        path = self._paths.shared_path("gateway_state.json")
        if not _safe_child_path(path, self._paths.root_home):
            if "gateway" in self._last_good_by_source:
                raise RuntimeError(f"{path.name} became unsafe")
            return GatewayState()
        data = self._read_json_reporting_stale(path)
        if not data:
            return GatewayState()
        state_signature = _file_signature(path)
        if (
            state_signature is not None
            and self._invalidated_gateway_state_signature is not None
            and state_signature != self._invalidated_gateway_state_signature
        ):
            self._invalidated_gateway_state_signature = None
        now = self._clock()
        writer = _record_writer(data)
        # A non-positive PID is absent, never a target: os.kill(0)/os.kill(-1)
        # would signal a process group or every process the user owns.
        recorded_pid = writer.pid or 0
        pid = recorded_pid
        running = data.get("gateway_state") == "running"
        recorded_writer_live = False
        # The PID in gateway_state.json can be stale if launchd restarted
        # the gateway. A replacement PID proves a process is running, but it does
        # not make topology written by the previous process current.
        if running:
            if pid:
                if self._pid_exists(pid):
                    recorded_writer_live = self._invalidated_gateway_state_signature is None
                else:
                    if state_signature is not None:
                        self._invalidated_gateway_state_signature = state_signature
                    # Recorded PID is dead — check if launchd has a live gateway
                    launchd_pid = self._find_gateway_launchd_pid()
                    if launchd_pid:
                        pid = launchd_pid
                    else:
                        running = False
            else:
                if state_signature is not None:
                    self._invalidated_gateway_state_signature = state_signature
                launchd_pid = self._find_gateway_launchd_pid()
                if launchd_pid:
                    pid = launchd_pid
                else:
                    running = False
        # Built after liveness resolution because a platform entry's recorded
        # ingress URL is surfaced only while the state-file writer is still live.
        platforms = [
            _platform_status(str(name), info, now, writer, record_current=recorded_writer_live)
            for name, raw_info in _as_dict(data.get("platforms")).items()
            if (info := _as_dict(raw_info))
        ]
        # Tri-state: an absent or non-list `served_profiles` is no record at all,
        # while a real list (even []) from a live gateway is authoritative — the
        # distinction upstream's recorded_served_profiles() makes by returning
        # None instead of []. The names are kept either way; only the marker is
        # gated on state-writer liveness, so an old writer's record stays preserved.
        raw_served = data.get("served_profiles")
        served_recorded = recorded_writer_live and isinstance(raw_served, list)
        version, behind = self._collect_hermes_version()
        cfg = self._read_yaml_reporting_stale()
        gateway_cfg = _as_dict(cfg.get("gateway"))
        scale_cfg = _as_dict(cfg.get("scale_to_zero")) or _as_dict(gateway_cfg.get("scale_to_zero"))
        active_agents = _coerce_int(data.get("active_agents"))
        drain_request = self._read_json_cached(self._paths.shared_path(".drain_request.json"))
        config_generation = _config_generation(data)
        return GatewayState(
            code_sha=str(data.get("code_sha") or ""),
            code_version=str(data.get("code_version") or ""),
            config_fingerprint=config_generation.fingerprint,
            config_generation_short=config_generation.short,
            config_sources=config_generation.sources,
            config_stale=self._config_stale(data, now),
            session_store_status=str(_as_dict(data.get("session_store")).get("status") or ""),
            exit_reason=str(data.get("exit_reason") or ""),
            pid=pid,
            running=running,
            state=str(data.get("gateway_state") or "unknown"),
            platforms=platforms,
            hermes_version=version,
            updates_behind=behind,
            active_agents=active_agents,
            restart_requested=bool(data.get("restart_requested")),
            busy=running and active_agents > 0,
            drainable=running and active_agents == 0,
            drain_active=bool(drain_request),
            drain_requested_at=str(drain_request.get("requested_at") or ""),
            drain_principal=str(
                drain_request.get("principal") or drain_request.get("requested_by") or ""
            ),
            drain_suppress_notification=bool(drain_request.get("suppress_notification")),
            served_profiles=[str(profile) for profile in _as_list(raw_served) if profile],
            served_profiles_recorded=served_recorded,
            scale_to_zero_idle_timeout_minutes=_coerce_int(scale_cfg.get("idle_timeout_minutes")),
            scale_to_zero_relay_only=_scale_to_zero_relay_only(scale_cfg, platforms),
        )

    def _config_stale(self, gateway_state: JsonMapping, now: float) -> bool:
        """Whether config.yaml changed since the running gateway started.

        The recorded ``config_generation`` mtimes are not used: no current
        hermes-agent writes them, so the stamps left in gateway_state.json are
        months old and would report stale forever.
        """
        start_epoch = _gateway_start_epoch(
            self._read_json_confined(self._paths.shared_path("state", "gateway.heartbeat")),
            self._read_json_confined(self._paths.shared_path("state", "gateway.lifecycle.json")),
            gateway_state,
            now,
        )
        return _config_stale(
            self._paths.shared_path("config.yaml"), self._paths.root_home, start_epoch
        )

    def _read_liveness_json(self, path: Path, had_last_good: bool) -> JsonMapping:
        """Read a gateway liveness file, failing the source on a last-good fallback."""
        if not _safe_child_path(path, self._paths.root_home):
            if had_last_good:
                raise RuntimeError(f"{path.name} became unsafe")
            return {}
        data = self._read_json_cached(path)
        if self._file_cache.last_read_was_stale(path):
            raise RuntimeError(f"{path.name} is unreadable; keeping last-good values")
        return data

    def _with_heartbeat(self, gateway: GatewayState) -> GatewayState:
        path = self._paths.shared_path("state", "gateway.heartbeat")
        last = self._last_good_by_source.get("gateway_heartbeat")
        had_last_good = last is not None and last.loop_health is not GatewayLoopHealth.UNKNOWN
        data = self._read_liveness_json(path, had_last_good)
        file_mtime = _mtime(path) if _safe_child_path(path, self._paths.root_home) else None
        age, health = _heartbeat_liveness(
            data,
            file_mtime,
            self._clock(),
            running=gateway.state == "running",
        )
        return gateway.model_copy(update={"heartbeat_age_seconds": age, "loop_health": health})

    def _with_lifecycle(self, gateway: GatewayState) -> GatewayState:
        path = self._paths.shared_path("state", "gateway.lifecycle.json")
        last = self._last_good_by_source.get("gateway_lifecycle")
        data = self._read_liveness_json(path, bool(last is not None and last.lifecycle_phase))
        status = _lifecycle_status(data, self._pid_exists)
        return gateway.model_copy(
            update={
                "lifecycle_phase": status.phase,
                "last_exit_code": status.last_exit_code,
                "last_exit_reason": status.last_exit_reason,
                "unclean_previous_exit": status.unclean_previous_exit,
            }
        )

    def _with_update_receipt(self, gateway: GatewayState) -> GatewayState:
        path = self._paths.shared_path("logs", "update_receipts", "latest.json")
        last = self._last_good_by_source.get("update_receipt")
        data = self._read_liveness_json(path, bool(last is not None and last.last_update_outcome))
        receipt = _update_receipt_status(data, self._clock(), gateway.code_sha)
        return gateway.model_copy(
            update={
                "last_update_outcome": receipt.outcome,
                "last_update_finished_age_seconds": receipt.finished_age_seconds,
                "last_update_from_version": receipt.from_version,
                "last_update_to_version": receipt.to_version,
                "last_update_failed_step": receipt.failed_step,
                "runtime_code_skew": receipt.runtime_code_skew,
                "runtime_code_skew_source": receipt.runtime_code_skew_source,
                "update_receipt_unfinished": receipt.update_receipt_unfinished,
                "update_fleet_states": receipt.update_fleet_states,
                "update_fleet_runtime_count": receipt.update_fleet_runtime_count,
            }
        )

    def _with_gateway_ledgers(self, gateway: GatewayState) -> GatewayState:
        readout = self._read_state_db()
        if readout is None:
            last = self._last_good_by_source.get("gateway_ledgers")
            if last is not None and last.gateway_incarnation_count:
                raise RuntimeError("state.db gateway ledgers disappeared or became unsafe")
            return gateway
        return gateway.model_copy(update=_gateway_ledger_fields(readout.ledgers, self._clock()))

    def _collect_migration(self, gateway: GatewayState, *, gateway_fresh: bool) -> MigrationState:
        """Read ``gateway_migration.json`` and judge it against the live artifacts.

        ROOT-scoped: upstream anchors the manifest at the *default* profile home
        (``hermes_cli/gateway_migrate.py:467-468``), never a secondary's, so a
        served profile has no copy of its own to read.

        Presence is checked separately from parseability because the two carry
        different meanings: absent is "never migrated OR successfully rolled back",
        while present-but-unparseable is a torn ``write_text`` mid-flight. A file
        that was readable and then vanished or turned unsafe *raises*, so the source
        is marked failed and its last-good verdict stays on display instead of
        silently reporting "no migration".
        """
        if not gateway_fresh:
            raise RuntimeError("gateway dependency is stale; keeping last-good migration verdict")

        path = self._paths.shared_path(_MANIFEST_NAME)
        last = self._last_good_by_source.get("migration")
        had_last_good = bool(last is not None and last.manifest_present)
        if not _safe_child_path(path, self._paths.root_home):
            if had_last_good:
                raise RuntimeError(f"{path.name} became unsafe")
            return MigrationState()
        if not _exists_strict(path):
            if had_last_good:
                raise RuntimeError(f"{path.name} disappeared")
            return MigrationState()
        return _migration_state(
            self._read_json_reporting_stale(path),
            now=self._clock(),
            cfg=self._read_yaml_reporting_stale(),
            gateway=gateway,
        )

    def _find_gateway_launchd_pid(self) -> int | None:
        """Check if launchd has a live hermes gateway process."""
        pid_file = self._paths.shared_path("gateway.pid")
        if pid_file.exists():
            try:
                content = _read_text_capped(pid_file, self._paths.root_home).strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        lpid = int(data.get("pid", 0) or 0)
                    else:
                        lpid = int(content)
                    if lpid > 0 and self._pid_exists(lpid):
                        return lpid
            except (ValueError, json.JSONDecodeError, ProcessLookupError, PermissionError, OSError):
                pass
        return None

    def _collect_hermes_version(self) -> tuple[str, int]:
        """Read hermes-agent version from pyproject.toml and update status."""
        version = ""
        pyproject = self._paths.shared_path("hermes-agent", "pyproject.toml")
        if pyproject.exists():
            try:
                with pyproject.open("rb") as handle:
                    data = tomllib.load(handle)
                project = _as_dict(data.get("project"))
                version = str(project.get("version") or "")
            except OSError:
                pass
            except tomllib.TOMLDecodeError:
                pass
        behind = 0
        update_check = self._read_json_cached(self._paths.shared_path(".update_check"))
        if update_check:
            behind = _coerce_int(update_check.get("behind"))
        return version, behind

    def _read_context_lengths(self) -> dict[str, int]:
        path = self._paths.shared_path("context_length_cache.yaml")
        data = self._file_cache.read_yaml_mapping(path)
        if self._file_cache.last_read_was_stale(path):
            raise RuntimeError("context_length_cache.yaml is unreadable; keeping last-good values")
        raw = data.get("context_lengths")
        if not isinstance(raw, dict):
            return {}
        # Cache keys are "model@base_url" with case-mixed model names (do not
        # lowercase); normalize only a trailing slash on the base_url part.
        normalized: dict[str, int] = {}
        for key, value in raw.items():
            model, sep, base_url = str(key).partition("@")
            if not sep:
                continue
            normalized[f"{model}@{base_url.rstrip('/')}"] = _coerce_int(value)
        return normalized

    def _collect_sessions(self, rows: list[dict[str, Any]] | None = None) -> list[SessionInfo]:
        rows = self._session_rows_or_read(rows)
        context_lengths = self._read_context_lengths()
        return [
            SessionInfo(
                # NULL id must stay None so the row fails validation and the
                # source falls back to last-good; only a missing column defaults.
                session_id=r.get("id", ""),  # row-get-ok
                source=r.get("source") or "",
                model=r.get("model") or "",
                parent_session_id=r.get("parent_session_id") or "",
                billing_provider=r.get("billing_provider") or "",
                billing_base_url=_redact_secret_url(r.get("billing_base_url") or ""),
                billing_mode=r.get("billing_mode") or "",
                end_reason=r.get("end_reason") or "",
                context_limit=_context_limit_for(
                    context_lengths,
                    str(r.get("model") or ""),
                    str(r.get("billing_base_url") or ""),
                ),
                cost_status=r.get("cost_status") or "",
                pricing_version=r.get("pricing_version") or "",
                message_count=r.get("message_count") or 0,
                tool_call_count=r.get("tool_call_count") or 0,
                input_tokens=r.get("input_tokens") or 0,
                output_tokens=r.get("output_tokens") or 0,
                cache_read_tokens=r.get("cache_read_tokens") or 0,
                cache_write_tokens=r.get("cache_write_tokens") or 0,
                reasoning_tokens=r.get("reasoning_tokens") or 0,
                estimated_cost_usd=_resolved_session_cost(r),
                api_call_count=r.get("api_call_count") or 0,
                cwd=r.get("cwd") or "",
                archived=bool(r.get("archived") or 0),
                rewind_count=r.get("rewind_count") or 0,
                handoff_state=r.get("handoff_state") or "",
                handoff_platform=r.get("handoff_platform") or "",
                handoff_error=r.get("handoff_error") or "",
                # SQLite columns are untyped: a text value in an epoch column must
                # coerce, not fail model validation and blank the whole source.
                started_at=_coerce_float(r.get("started_at")),
                ended_at=r.get("ended_at"),
                title=r.get("title"),
                is_active=r.get("ended_at") is None and not bool(r.get("archived") or 0),
                git_branch=r.get("git_branch") or "",
                chat_type=r.get("chat_type") or "",
                display_name=r.get("display_name") or "",
                title_source=r.get("title_source") or "",
                profile_name=r.get("profile_name") or "",
                pinned=bool(r.get("pinned") or 0),
                last_activity_at=_coerce_float(r.get("last_activity_at")),
                last_activity_description=r.get("last_activity_description") or "",
                actual_cost_usd=_coerce_float(r.get("actual_cost_usd")),
                cost_source=r.get("cost_source") or "",
                compression_failure_error=r.get("compression_failure_error") or "",
                # 0 and NULL both mean "no deadline" — see _optional_epoch.
                compression_failure_cooldown_until=_optional_epoch(
                    r.get("compression_failure_cooldown_until")
                ),
                compression_fallback_streak=_coerce_int(r.get("compression_fallback_streak")),
                compression_ineffective_count=_coerce_int(r.get("compression_ineffective_count")),
                compression_recovery_deadline=_optional_epoch(
                    r.get("compression_recovery_deadline")
                ),
            )
            for r in rows
        ]

    def _collect_model_usage(self) -> _ModelUsageBundle:
        usage = self._db.read_model_usage(self._clock())
        if self._db.last_read_model_usage_stale:
            raise RuntimeError("model usage rows are stale")
        if not any(usage.values()):
            return _EMPTY_MODEL_USAGE_BUNDLE
        return _ModelUsageBundle(
            usage_source="session_model_usage",
            all_time=_model_usage_from_rows(usage["all"]),
            last_24h=_model_usage_from_rows(usage["24h"]),
            last_7d=_model_usage_from_rows(usage["7d"]),
        )

    def _collect_active_surfaces(self) -> _ActiveSurfaceReadout:
        """Lease entries from runtime/active_sessions.json.

        An existing pid is not the recorded process: pids get reused, so liveness
        compares the registry's ``process_start_time`` (epoch seconds — unlike
        gateway_state.json, which records centiseconds) against the start time
        observed for that pid on this host. Every pid is probed in one call rather
        than one per surface per tick.

        The lease's own metadata is carried beside that verdict: ``lease_id``,
        ``track_liveness``, and ``started_at``/``updated_at`` as *ages* against the
        injected clock. Ages are recomputed on every pass and are never stored in
        the mtime-keyed file cache, which only ever holds the raw JSON.

        Scope note (``.codex/rules/source-ownership.md``): this reads the selected
        profile's registry via ``profile_path``, matching upstream's
        ``_state_path`` (``hermes_cli/active_sessions.py:164-168``). Upstream's
        orphan reclamation sweeps the root home *and every profile home*
        (``release_orphaned_leases``, ``:660-687``), so the occupancy hermesd
        reports is one registry's — leases held under other profiles are invisible
        here and a cross-profile capacity picture would need every home read.
        """
        data = self._read_json_confined(self._paths.profile_path("runtime", "active_sessions.json"))
        entries: list[dict[str, Any]] = []
        total_count = 0
        for raw_entry in _as_list(data.get("entries")):
            entry = _as_dict(raw_entry)
            if str(entry.get("session_id") or ""):
                total_count += 1
                if len(entries) < _ACTIVE_SURFACE_LIMIT:
                    entries.append(entry)
        pids = sorted({_coerce_int(entry.get("pid")) for entry in entries} - {0})
        observed = self._process_start_times(pids) if pids else {}
        now = self._clock()
        surfaces = []
        for entry in entries:
            pid = _coerce_int(entry.get("pid"))
            raw_start = entry.get("process_start_time")
            recorded = _coerce_float(raw_start) if raw_start is not None else 0.0
            surfaces.append(
                ActiveSurface(
                    session_id=str(entry.get("session_id") or ""),
                    surface=str(entry.get("surface") or ""),
                    pid=pid,
                    # A non-positive stamp was never recorded; treating 0 as a real
                    # epoch would compare against every observed start time.
                    process_start_time=recorded if recorded > 0 else None,
                    liveness=_surface_liveness(
                        pid, recorded if recorded > 0 else None, observed, self._pid_exists
                    ),
                    lease_id=str(entry.get("lease_id") or ""),
                    started_at_age_seconds=_lease_age_seconds(entry.get("started_at"), now),
                    updated_at_age_seconds=_lease_age_seconds(entry.get("updated_at"), now),
                    track_liveness=bool(entry.get("track_liveness")),
                )
            )
        return _ActiveSurfaceReadout(surfaces=tuple(surfaces), total_count=total_count)

    def _last_model_usage(self) -> _ModelUsageBundle:
        bundle: _ModelUsageBundle = self._last_good_by_source.get(
            "model_usage", _EMPTY_MODEL_USAGE_BUNDLE
        )
        return bundle

    def _collect_tokens_today(self, rows: list[dict[str, Any]] | None = None) -> TokenSummary:
        rows = self._session_rows_or_read(rows)
        return _summarize_tokens(
            rows,
            started_at_min=_today_epoch(self._clock()),
        )

    def _collect_tokens_total(self, rows: list[dict[str, Any]] | None = None) -> TokenSummary:
        rows = self._session_rows_or_read(rows)
        return _summarize_tokens(rows)

    def _collect_token_analytics(self, rows: list[dict[str, Any]] | None = None) -> TokenAnalytics:
        rows = self._session_rows_or_read(rows)
        return TokenAnalytics(
            windows=[
                _summarize_window("7d", rows, days=7, now=self._clock()),
                _summarize_window("30d", rows, days=30, now=self._clock()),
            ],
            by_model=_summarize_breakdown(rows, key_name="model"),
            by_provider=_summarize_breakdown(rows, key_name="billing_provider"),
            by_endpoint=_summarize_breakdown(rows, key_name="billing_base_url"),
            cost_status_counts=_count_cost_statuses(rows),
        )

    def _collect_tool_stats(
        self,
        session_rows: list[dict[str, Any]] | None = None,
        session_rows_stale: bool = False,
    ) -> list[ToolStats]:
        # Try messages table first (has per-tool breakdown)
        rows = self._db.read_tool_stats()
        if self._db.last_read_tool_stats_stale:
            raise RuntimeError("tool stats are stale")
        if rows:
            return [ToolStats(name=r["tool_name"], call_count=r["call_count"]) for r in rows]
        # Fall back to per-session tool_call_count
        if session_rows_stale:
            raise RuntimeError("session rows are stale")
        sessions = session_rows if session_rows is not None else self._db.read_sessions()
        stats = []
        for s in sessions:
            tc = s.get("tool_call_count") or 0
            if tc > 0:
                sid = str(s.get("id") or "?")
                src = s.get("source") or "?"
                label = f"{src}:{sid[-6:]}"
                stats.append(ToolStats(name=label, call_count=tc))
        return sorted(stats, key=lambda t: t.call_count, reverse=True)

    def _collect_total_tool_calls(self, rows: list[dict[str, Any]] | None = None) -> int:
        rows = self._session_rows_or_read(rows)
        return sum(r.get("tool_call_count") or 0 for r in rows)

    def _collect_background_processes(self) -> list[BackgroundProcessInfo]:
        # hermes-agent >= 0.21 registers live processes in spawn-ledger.json;
        # processes.json is the legacy (now usually empty) registry.
        ledger = self._read_json_list_cached(self._paths.shared_path("spawn-ledger.json"))
        if ledger:
            return [
                _background_process_from_ledger(entry, self._pid_exists)
                for entry in ledger
                if _coerce_int(entry.get("pid")) > 0 or str(entry.get("session_id") or "")
            ]
        entries = self._read_json_list_cached(self._paths.shared_path("processes.json"))
        return [
            BackgroundProcessInfo(
                session_id=str(entry.get("session_id") or ""),
                command=str(entry.get("command") or ""),
                pid=_coerce_int(entry.get("pid")),
                pid_scope=str(entry.get("pid_scope") or ""),
                cwd=str(entry.get("cwd") or ""),
                started_at=_coerce_float(entry.get("started_at")),
                task_id=str(entry.get("task_id") or ""),
                session_key=str(entry.get("session_key") or ""),
                notify_on_complete=bool(entry.get("notify_on_complete")),
                watcher_platform=str(entry.get("watcher_platform") or ""),
                watcher_chat_id=str(entry.get("watcher_chat_id") or ""),
                watcher_user_id=str(entry.get("watcher_user_id") or ""),
                watcher_user_name=str(entry.get("watcher_user_name") or ""),
                watcher_thread_id=str(entry.get("watcher_thread_id") or ""),
                watcher_message_id=str(entry.get("watcher_message_id") or ""),
                watcher_interval=_coerce_int(entry.get("watcher_interval")),
                watch_patterns=[str(item) for item in _as_list(entry.get("watch_patterns"))],
                alive=self._process_alive(_coerce_int(entry.get("pid"))),
            )
            for entry in entries
            if str(entry.get("session_id") or "")
        ]

    def _process_alive(self, pid: int) -> bool:
        return bool(pid) and self._pid_exists(pid)

    def _collect_available_tools(self) -> tuple[int, list[str]]:
        banner_names = self._banner_snapshot_tool_names()
        if banner_names:
            return len(banner_names), banner_names
        return self._session_file_tool_names()

    def _banner_snapshot_tool_names(self) -> list[str]:
        """Tool names from cache/banner_snapshot.json, the live tool inventory."""
        return sorted(_tool_names_from_entries(self._banner_snapshot().get("tools")))

    def _collect_toolset_availability(self) -> ToolsetAvailability:
        return _toolset_availability(self._banner_snapshot())

    def _banner_snapshot(self) -> JsonMapping:
        path = self._paths.shared_path("cache", "banner_snapshot.json")
        if not _safe_child_path(path, self._paths.root_home):
            return {}
        return self._read_json_cached(path)

    def _session_file_tool_names(self) -> tuple[int, list[str]]:
        sessions_root = self._paths.profile_path("sessions")
        sessions_index = sessions_root / "sessions.json"
        if sessions_index.is_symlink() or not _path_resolves_under(sessions_index, sessions_root):
            raise OSError("Refusing unsafe sessions index")
        sessions_signature = _file_signature(sessions_index)
        sessions_data = self._read_json_cached(sessions_index)
        session_files: list[Path] = []
        for entry in sessions_data.values():
            if not isinstance(entry, dict) or "session_id" not in entry:
                continue
            session_file = sessions_root / f"session_{entry['session_id']}.json"
            if session_file.is_symlink() or not _path_resolves_under(session_file, sessions_root):
                raise OSError(f"Refusing unsafe session file: {session_file.name}")
            session_files.append(session_file)
        # Tool changes inside individual session_<sid>.json files must
        # invalidate the cache even when the index itself is not rewritten;
        # track each path with nanosecond mtime and size (not just the max) so
        # equal/coarse mtimes and filename swaps still invalidate correctly.
        # The directory mtime cannot replace these per-file stats: on POSIX it
        # only changes on create/delete/rename, never on a content-only edit.
        signatures = {str(path): _file_signature(path) for path in session_files}
        session_file_signatures = tuple(sorted(signatures.values(), key=str))
        cache_key = (sessions_signature, session_file_signatures)
        if sessions_signature is not None and self._available_tools_cache_key == cache_key:
            return self._available_tools_cache_value
        if not sessions_data:
            self._available_tools_cache_key = cache_key
            self._available_tools_cache_value = (0, [])
            return 0, []
        names: set[str] = set()
        # Cache the extracted names per file, not the parsed document: routing
        # every session file through the last-good file cache kept every session
        # JSON alive for the lifetime of the process.
        fresh_name_cache: dict[str, tuple[tuple[str, int, int] | None, tuple[str, ...]]] = {}
        for session_file in session_files:
            key = str(session_file)
            signature = signatures[key]
            cached = self._session_tool_names_cache.get(key)
            if cached is not None and signature is not None and cached[0] == signature:
                file_names = cached[1]
            else:
                file_names = tuple(
                    sorted(
                        _tool_names_from_entries(
                            _read_session_tools(session_file),
                            allow_bare_names=True,
                        )
                    )
                )
            fresh_name_cache[key] = (signature, file_names)
            names.update(file_names)
        self._session_tool_names_cache = fresh_name_cache
        tool_names = sorted(names)
        self._available_tools_cache_key = cache_key
        self._available_tools_cache_value = (len(tool_names), tool_names)
        return self._available_tools_cache_value

    def _collect_config(self) -> ConfigSummary:
        cfg = self._read_yaml_reporting_stale()
        if not cfg:
            # A config.yaml that parses to nothing after a good read is a
            # truncated or emptied write, not a real "no configuration": fail
            # the source so the last-good summary survives instead of blanking
            # the config panel (same guard shape as the kanban.db readers).
            last = self._last_good_by_source.get("config")
            if last is not None and last != ConfigSummary():
                raise RuntimeError("config.yaml parsed empty")
            return ConfigSummary()
        model_cfg = _as_dict(cfg.get("model"))
        agent_cfg = _as_dict(cfg.get("agent"))
        comp_cfg = _as_dict(cfg.get("compression"))
        sec_cfg = _as_dict(cfg.get("security"))
        app_cfg = _as_dict(cfg.get("approvals"))
        smart_cfg = _as_dict(cfg.get("smart_model_routing"))
        provider_routing_cfg = _as_dict(cfg.get("provider_routing"))
        fallback_cfg = _as_dict(cfg.get("fallback_model"))
        dashboard_cfg = _as_dict(cfg.get("dashboard"))
        session_reset_cfg = _as_dict(cfg.get("session_reset"))
        memory_cfg = _as_dict(cfg.get("memory"))
        tool_search_cfg = _as_dict(cfg.get("tools")).get("tool_search")
        if not isinstance(tool_search_cfg, dict):
            tool_search_cfg = _as_dict(cfg.get("tool_search"))
        tool_search = _as_dict(tool_search_cfg)
        code_execution_cfg = _as_dict(cfg.get("code_execution"))
        kanban_cfg = _as_dict(cfg.get("kanban"))
        gateway_cfg = _as_dict(cfg.get("gateway"))
        auxiliary_cfg = _as_dict(cfg.get("auxiliary"))
        moa_cfg = _as_dict(cfg.get("moa"))
        moa_summary = _moa_config_summary(moa_cfg)
        personality = str(agent_cfg.get("active_personality") or "")
        if not personality:
            personalities = _as_dict(agent_cfg.get("personalities"))
            if personalities:
                personality = str(next(iter(personalities)))
        dashboard_auth_provider = str(
            dashboard_cfg.get("auth_provider")
            or dashboard_cfg.get("auth")
            or self._env.get("HERMES_DASHBOARD_AUTH_PROVIDER", "")
        )
        return ConfigSummary(
            # Raw YAML may hold null or a wrong type for any of these keys; a
            # bare .get(key, default) would fail the whole config source.
            model=str(model_cfg.get("default") or ""),
            provider=str(model_cfg.get("provider") or ""),
            personality=personality,
            max_turns=_coerce_int(agent_cfg.get("max_turns")),
            compression_threshold=_coerce_float(comp_cfg.get("threshold")),
            reasoning_effort=str(agent_cfg.get("reasoning_effort") or ""),
            security_redact=bool(sec_cfg.get("redact_secrets")),
            approvals_mode=str(app_cfg.get("mode") or ""),
            provider_routing_summary=_provider_routing_summary(provider_routing_cfg),
            smart_model_routing_enabled=bool(smart_cfg.get("enabled")),
            smart_model_routing_cheap_model=_provider_model_label(
                _as_dict(smart_cfg.get("cheap_model"))
            ),
            fallback_model_label=_provider_model_label(fallback_cfg),
            dashboard_theme=str(dashboard_cfg.get("theme") or ""),
            session_reset_mode=str(session_reset_cfg.get("mode") or ""),
            memory_provider=str(memory_cfg.get("provider") or ""),
            # These values come from the dashboard process environment, not Hermes runtime state.
            tool_gateway_domain=self._env.get("TOOL_GATEWAY_DOMAIN", ""),
            tool_gateway_scheme=self._env.get("TOOL_GATEWAY_SCHEME", ""),
            firecrawl_gateway_url=_redact_secret_url(self._env.get("FIRECRAWL_GATEWAY_URL", "")),
            tool_gateway_routes=self._collect_tool_gateway_routes(cfg),
            tool_search_enabled=str(tool_search.get("enabled") or ""),
            tool_search_threshold_pct=_coerce_int(tool_search.get("threshold_pct")),
            tool_search_default_limit=_coerce_int(tool_search.get("search_default_limit")),
            tool_search_max_limit=_coerce_int(tool_search.get("max_search_limit")),
            toolsets=[str(item) for item in _as_list(cfg.get("toolsets")) if item],
            code_execution_mode=str(code_execution_cfg.get("mode") or ""),
            code_execution_timeout=_coerce_int(code_execution_cfg.get("timeout")),
            code_execution_max_tool_calls=_coerce_int(code_execution_cfg.get("max_tool_calls")),
            dashboard_public_url=_redact_secret_url(str(dashboard_cfg.get("public_url") or "")),
            dashboard_auth_provider=dashboard_auth_provider,
            dashboard_basic_auth_configured=(
                bool(self._env.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME"))
                or bool(self._env.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"))
                or dashboard_auth_provider.endswith("basic")
                or dashboard_auth_provider == "basic"
            ),
            kanban_dispatch_in_gateway=bool(kanban_cfg.get("dispatch_in_gateway")),
            kanban_auto_decompose=bool(kanban_cfg.get("auto_decompose")),
            kanban_dispatch_interval_seconds=_coerce_int(
                kanban_cfg.get("dispatch_interval_seconds")
            ),
            kanban_failure_limit=_coerce_int(kanban_cfg.get("failure_limit")),
            gateway_strict_media_delivery=bool(gateway_cfg.get("strict")),
            gateway_trust_recent_files=bool(gateway_cfg.get("trust_recent_files")),
            gateway_trust_recent_files_seconds=_coerce_int(
                gateway_cfg.get("trust_recent_files_seconds")
            ),
            auxiliary_slots=sorted(str(name) for name in auxiliary_cfg if name),
            moa_default_preset=moa_summary["default_preset"],
            moa_active_preset=moa_summary["active_preset"],
            moa_preset_count=_coerce_int(moa_summary["preset_count"]),
            moa_reference_model_count=_coerce_int(moa_summary["reference_model_count"]),
            moa_aggregator_label=moa_summary["aggregator_label"],
            moa_save_traces=bool(moa_cfg.get("save_traces")),
            moa_trace_dir=str(moa_cfg.get("trace_dir") or ""),
            **_config_agent_limits(cfg),
        )

    def _with_config_backups(self, current: ConfigSummary) -> ConfigSummary:
        """Group the point-in-time config copies recorded beside config.yaml.

        Upstream writes ``config.yaml.<reason>.<YYYYMMDD-HHMMSS>`` copies under
        ``<config dir>/backups/config`` (``hermes_cli/config_backups.py:29-69``),
        keeping the newest five per reason and skipping byte-identical repeats.
        The config path upstream copies is ``get_config_path()`` —
        ``hermes_constants.py:1132-1135`` — so the directory inherits whatever
        home that resolves to; hermesd keeps the ROOT copy on purpose, the same
        decision as the ``config`` source (see .codex/rules/source-ownership.md).

        Consequences worth rendering honestly: a "good" copy lands only when
        config.yaml's bytes change, so an old stamp means *unchanged*, not
        stale; and the stamps are the writer's local time.
        """
        backups_dir = self._paths.shared_path("backups", "config")
        if not _exists_strict(backups_dir) or not backups_dir.is_dir():
            return current.model_copy(
                update={
                    "config_backups_present": False,
                    "config_backup_groups": [],
                    "config_backup_groups_truncated": False,
                }
            )
        if backups_dir.is_symlink() or not _path_resolves_under(backups_dir, self._paths.root_home):
            # Same hardening as the curator run-dir scan: a planted symlink must
            # fail this source (keeping last-good) instead of being read.
            raise RuntimeError(f"unsafe config backups directory: {backups_dir.name}")
        # The directory scan is bounded before sorting: a hostile directory can
        # hold far more entries than the five-per-reason writer would leave.
        examined = list(islice(backups_dir.iterdir(), _CONFIG_BACKUP_ENTRY_LIMIT + 1))
        scan_truncated = len(examined) > _CONFIG_BACKUP_ENTRY_LIMIT
        entries = sorted(
            entry.name
            for entry in examined[:_CONFIG_BACKUP_ENTRY_LIMIT]
            if entry.is_file() and not entry.is_symlink()
        )
        groups, groups_truncated = _config_backup_groups(entries, now=self._clock())
        return current.model_copy(
            update={
                "config_backups_present": True,
                "config_backup_groups": groups,
                "config_backup_groups_truncated": scan_truncated or groups_truncated,
            }
        )

    def _collect_tool_gateway_routes(self, cfg: dict[str, Any]) -> list[ToolGatewayRoute]:
        token_present = bool(self._env.get("TOOL_GATEWAY_USER_TOKEN"))
        routes = []
        for tool_name in ("web", "image_gen", "tts", "browser"):
            tool_cfg = _as_dict(cfg.get(tool_name))
            mode = "gateway" if bool(tool_cfg.get("use_gateway")) else "direct"
            routes.append(
                ToolGatewayRoute(
                    tool=tool_name,
                    mode=mode,
                    token_present=token_present,
                )
            )
        return routes

    def _collect_cron(self) -> CronState:
        cfg = self._read_yaml_reporting_stale()
        cron_cfg = _as_dict(cfg.get("cron"))
        tick_path = self._paths.shared_path("cron", ".tick.lock")
        last_tick: float | None = None
        if tick_path.exists():
            try:
                mtime = tick_path.stat().st_mtime
                last_tick = max(0.0, self._clock() - mtime)
            except OSError:
                pass

        jobs: list[CronJob] = []
        error_count = 0
        data = self._read_json_cached(self._paths.shared_path("cron", "jobs.json"))
        if data:
            directory = self._read_json_cached(self._paths.shared_path("channel_directory.json"))
            for j in _as_list(data.get("jobs")):
                if not isinstance(j, dict):
                    continue
                state = str(j.get("state") or "")
                if j.get("last_status") == "error" or j.get("last_error"):
                    error_count += 1
                (
                    output_excerpt,
                    silent_run,
                    output_path,
                    output_mtime,
                ) = self._latest_cron_output_excerpt(
                    self._paths.shared_path("cron", "output"),
                    str(j.get("id") or ""),
                    self._log_tail_bytes,
                )
                last_status = j.get("last_status")
                # A raw null must fall back to the model default, not fail the job.
                raw_enabled = j.get("enabled", True)
                enabled = True if raw_enabled is None else bool(raw_enabled)
                dispatch_lateness, dispatch_kind = _cron_job_dispatch(j)
                repeat_times, repeat_completed = _cron_job_repeat(j)
                paused, paused_reason = _cron_job_paused(j)
                jobs.append(
                    CronJob(
                        job_id=str(j.get("id") or ""),
                        name=str(j.get("name") or ""),
                        schedule_display=str(j.get("schedule_display") or ""),
                        state=state,
                        enabled=enabled,
                        deliver=str(j.get("deliver") or ""),
                        delivery_target_label=_delivery_target_label(
                            directory,
                            str(j.get("deliver") or ""),
                        ),
                        latest_output_excerpt=output_excerpt,
                        latest_output_path=output_path,
                        latest_output_mtime=output_mtime,
                        silent_run=silent_run,
                        next_run_at=str(j.get("next_run_at") or ""),
                        last_status=str(last_status) if last_status is not None else None,
                        last_error=str(j.get("last_error") or ""),
                        failure_streak=_coerce_int(j.get("failure_streak")),
                        paused=paused,
                        paused_reason=paused_reason,
                        last_delivery_error=str(j.get("last_delivery_error") or ""),
                        dispatch_lateness_seconds=dispatch_lateness,
                        dispatch_kind=dispatch_kind,
                        repeat_times=repeat_times,
                        repeat_completed=repeat_completed,
                        no_agent=bool(j.get("no_agent")),
                    )
                )

        cron_dir = self._paths.shared_path("cron")
        root = self._paths.root_home
        now = self._clock()
        heartbeat_age, last_success_age = _cron_ticker_ages(cron_dir, now=now, root=root)
        ticker_error, ticker_error_age = _cron_ticker_last_error(cron_dir, now=now, root=root)
        catch_up_count, catch_up_recorded = _cron_catch_up_occurrences(cron_dir, root)
        catch_up_missed, catch_up_missed_set = _cron_catch_up_policy(cron_cfg)
        return CronState(
            last_tick_ago_seconds=last_tick,
            ticker_heartbeat_age_seconds=heartbeat_age,
            ticker_last_success_age_seconds=last_success_age,
            ticker_health=_cron_ticker_health(
                heartbeat_age,
                last_success_age,
                ticker_error_recorded=bool(ticker_error),
            ),
            ticker_last_error=ticker_error,
            ticker_last_error_age_seconds=ticker_error_age,
            catch_up_occurrences=catch_up_count,
            catch_up_occurrences_recorded=catch_up_recorded,
            catch_up_missed=catch_up_missed,
            catch_up_missed_set=catch_up_missed_set,
            job_count=len(jobs),
            error_count=error_count,
            max_parallel_jobs=_coerce_int(cron_cfg.get("max_parallel_jobs")),
            wrap_response=bool(cron_cfg.get("wrap_response")),
            provider=str(cron_cfg.get("provider") or "builtin"),
            chronos_configured=_chronos_configured(_as_dict(cron_cfg.get("chronos"))),
            chronos_portal_configured=bool(_as_dict(cron_cfg.get("chronos")).get("portal_url")),
            chronos_callback_configured=bool(_as_dict(cron_cfg.get("chronos")).get("callback_url")),
            chronos_audience_configured=bool(
                _as_dict(cron_cfg.get("chronos")).get("expected_audience")
            ),
            chronos_jwks_configured=bool(_as_dict(cron_cfg.get("chronos")).get("nas_jwks_url")),
            suggestion_count=_cron_suggestion_count(cron_dir),
            jobs=jobs,
        )

    def _collect_cron_executions(self, cron: CronState) -> CronExecutionsState:
        """Execution history and incidents, named from the already-collected jobs."""
        job_names = {job.job_id: job.name for job in cron.jobs if job.job_id}
        db_path = self._paths.shared_path("cron", "executions.db")
        # A database that still exists but no longer resolves under ~/.hermes
        # was swapped for something else; that is a failure, not an absence.
        if _exists_strict(db_path) and not _safe_child_path(db_path, self._paths.root_home):
            last = self._last_good_by_source.get("cron_executions")
            if last is not None and last.db_present:
                raise RuntimeError("cron/executions.db replaced by unsafe path")
            return CronExecutionsState()
        return _read_cron_executions_state(
            db_path,
            job_names,
            now=self._clock(),
            root=self._paths.root_home,
        )

    def _collect_channels(self, gateway: GatewayState) -> ChannelDirectoryState:
        directory = self._read_json_cached(self._paths.shared_path("channel_directory.json"))
        aliases = _as_dict(self._read_json_cached(self._paths.shared_path("channel_aliases.json")))
        platforms = _as_dict(directory.get("platforms"))
        gateway_states = {platform.name: platform.state for platform in gateway.platforms}
        missing_platforms = sorted(name for name in gateway_states if name not in platforms)
        platform_infos: list[ChannelPlatformInfo] = []
        for name, raw_entries in sorted(platforms.items()):
            entries = raw_entries if isinstance(raw_entries, list) else []
            states = sorted(
                {
                    str(_as_dict(entry).get("state") or "")
                    for entry in entries
                    if str(_as_dict(entry).get("state") or "")
                }
            )
            gateway_state = gateway_states.get(str(name), "")
            if gateway_state and gateway_state not in states:
                states.append(gateway_state)
            platform_infos.append(
                ChannelPlatformInfo(
                    name=str(name),
                    entry_count=len(entries),
                    states=states,
                    connected=gateway_state == "connected",
                    capabilities=_channel_capabilities(str(name)),
                    family_label=_platform_family_label(str(name)),
                )
            )
        for name in missing_platforms:
            platform_infos.append(
                ChannelPlatformInfo(
                    name=name,
                    states=[gateway_states[name]] if gateway_states[name] else [],
                    connected=gateway_states[name] == "connected",
                    capabilities=_channel_capabilities(name),
                    family_label=_platform_family_label(name),
                    missing_from_directory=True,
                )
            )
        return ChannelDirectoryState(
            updated_at=str(directory.get("updated_at") or ""),
            platform_count=len(platform_infos),
            alias_count=sum(len(_as_dict(entries)) for entries in aliases.values()),
            alias_platform_count=sum(1 for entries in aliases.values() if _as_dict(entries)),
            stale_alias_count=_stale_alias_count(aliases),
            missing_directory_platforms=missing_platforms,
            platforms=platform_infos,
        )

    def _collect_kanban(self) -> KanbanState:
        cfg = self._read_yaml_reporting_stale()
        kanban_cfg = _as_dict(cfg.get("kanban"))
        base_state = KanbanState(
            db_present=_exists_strict(self._paths.shared_path("kanban.db")),
            current_board=self._read_current_kanban_board(),
            dispatch_in_gateway=bool(kanban_cfg.get("dispatch_in_gateway")),
            dispatch_interval_seconds=_coerce_int(kanban_cfg.get("dispatch_interval_seconds")),
            claim_ttl_seconds=_kanban_claim_ttl_seconds(kanban_cfg),
            auto_decompose=bool(kanban_cfg.get("auto_decompose")),
            failure_limit=_coerce_int(kanban_cfg.get("failure_limit")),
        )
        db_path = self._paths.shared_path("kanban.db")
        last_kanban = self._last_good_by_source.get("kanban")
        if not _exists_strict(db_path):
            if last_kanban is not None and last_kanban.db_present:
                raise RuntimeError("kanban.db disappeared")
            return self._with_kanban_boards(base_state)
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.root_home):
            if last_kanban is not None and last_kanban.db_present:
                raise RuntimeError("kanban.db replaced by unsafe path")
            return self._with_kanban_boards(base_state)
        return self._with_kanban_boards(_read_kanban_state(db_path, base_state, now=self._clock()))

    def _read_current_kanban_board(self) -> str:
        path = self._paths.shared_path("kanban", "current")
        last_kanban = self._last_good_by_source.get("kanban")
        if path.is_symlink() or not _path_resolves_under(path, self._paths.root_home):
            if last_kanban is not None and last_kanban.current_board:
                raise RuntimeError("kanban current board replaced by unsafe path")
            return ""
        try:
            with path.open("rb") as handle:
                raw = handle.read(_MAX_TEXT_READ_BYTES)
        except OSError:
            if last_kanban is not None and last_kanban.current_board:
                raise
            return ""
        return raw.decode("utf-8", errors="replace").strip()

    def _with_kanban_boards(self, state: KanbanState) -> KanbanState:
        boards: list[KanbanBoardSummary] = []
        root_db = self._paths.shared_path("kanban.db")
        if (
            root_db.exists()
            and not root_db.is_symlink()
            and _path_resolves_under(root_db, self._paths.root_home)
        ):
            boards.append(
                KanbanBoardSummary(
                    slug="root",
                    current=state.current_board in {"", "root", "default"},
                    task_count=state.task_count,
                    run_count=state.run_count,
                    problem_count=sum(
                        state.status_counts.get(status, 0)
                        for status in ("blocked", "failed", "error")
                    ),
                    stale_claim_count=state.stale_claim_count,
                )
            )

        boards_dir = self._paths.shared_path("kanban", "boards")
        if (
            boards_dir.exists()
            and boards_dir.is_dir()
            and not boards_dir.is_symlink()
            and _path_resolves_under(boards_dir, self._paths.root_home)
        ):
            for board_dir in sorted(boards_dir.iterdir()):
                db_path = board_dir / "kanban.db"
                if (
                    not board_dir.is_dir()
                    or board_dir.is_symlink()
                    or not db_path.exists()
                    or db_path.is_symlink()
                    or not _path_resolves_under(db_path, self._paths.root_home)
                ):
                    continue
                try:
                    summary = _read_kanban_board_summary(
                        db_path,
                        slug=board_dir.name,
                        current=board_dir.name == state.current_board,
                        claim_ttl_seconds=state.claim_ttl_seconds,
                        now=self._clock(),
                    )
                except (sqlite3.Error, OSError) as exc:
                    self._kanban_board_errors.append(
                        f"{board_dir.name}: {_safe_exception_text(exc)}"
                    )
                    cached = self._kanban_board_cache.get(board_dir.name)
                    if cached is not None:
                        boards.append(
                            cached.model_copy(
                                update={"current": board_dir.name == state.current_board}
                            )
                        )
                    continue
                self._kanban_board_cache[board_dir.name] = summary
                boards.append(summary)
        return state.model_copy(update={"board_count": len(boards), "boards": boards})

    def _collect_operations(
        self,
        background_processes: list[BackgroundProcessInfo],
    ) -> OperationsState:
        dashboard_process_count = sum(
            1 for process in background_processes if _is_dashboard_process(process.command)
        )
        desktop_stamp = self._read_json_confined(
            self._paths.shared_path("desktop-build-stamp.json")
        )
        stamp_label = str(
            desktop_stamp.get("version")
            or desktop_stamp.get("stamp")
            or desktop_stamp.get("builtAt")
            or desktop_stamp.get("built_at")
            or desktop_stamp.get("created_at")
            or str(desktop_stamp.get("contentHash") or "")[:12]
            or ""
        )
        web_ui_stamp = self._read_json_confined(self._paths.shared_path("web-ui-build-stamp.json"))
        operations = OperationsState(
            dashboard_process_count=dashboard_process_count,
            desktop_build_stamp=stamp_label,
            model_caches=self._collect_model_caches(),
            pr_monitors=self._collect_pr_monitors(),
            web_ui_build_hash=str(web_ui_stamp.get("contentHash") or "")[:12],
            web_ui_built_age_seconds=_iso_age_seconds(
                str(web_ui_stamp.get("builtAt") or ""), self._clock()
            ),
        )
        operations = self._with_response_store(operations)
        operations = self._with_verification_evidence(operations)
        operations = self._with_goals(operations)
        operations = self._with_moa_traces(operations)
        return self._with_projects(operations)

    def _with_response_store(self, operations: OperationsState) -> OperationsState:
        db_path = self._paths.shared_path("response_store.db")
        last = self._last_good_by_source.get("operations")
        if not _exists_strict(db_path):
            if last is not None and last.response_store_present:
                raise RuntimeError("response_store.db disappeared")
            return operations
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.root_home):
            if last is not None and last.response_store_present:
                raise RuntimeError("response_store.db replaced by unsafe path")
            return operations
        with _connect_readonly_sqlite(db_path) as conn:
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return operations.model_copy(
                update={
                    "response_store_present": True,
                    "conversation_count": _table_count_or_zero(conn, "conversations"),
                    "response_count": _table_count_or_zero(conn, "responses"),
                    "response_store_size_bytes": _file_size(db_path),
                }
            )

    def _with_verification_evidence(self, operations: OperationsState) -> OperationsState:
        db_path = self._paths.profile_path("verification_evidence.db")
        last = self._last_good_by_source.get("operations")
        if not _exists_strict(db_path):
            if last is not None and last.verification_db_present:
                raise RuntimeError("verification_evidence.db disappeared")
            return operations
        # Confined to profile_home, not root_home: a path that resolves into a
        # *sibling* profile is still under the root, and this source is
        # profile-scoped (see .codex/rules/source-ownership.md).
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.profile_home):
            if last is not None and last.verification_db_present:
                raise RuntimeError("verification_evidence.db replaced by unsafe path")
            return operations
        with _connect_readonly_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return _read_verification_evidence(conn, operations)

    def _with_moa_traces(self, operations: OperationsState) -> OperationsState:
        cfg = self._read_yaml_reporting_stale()
        moa_cfg = _as_dict(cfg.get("moa"))
        trace_dir_value = str(moa_cfg.get("trace_dir") or "")
        trace_dir = (
            Path(trace_dir_value).expanduser()
            if trace_dir_value
            else self._paths.shared_path("moa-traces")
        )
        if not trace_dir.is_absolute():
            trace_dir = self._paths.shared_path(trace_dir_value)
        if (
            trace_dir.is_symlink()
            or not _path_resolves_under(trace_dir, self._paths.root_home)
            or not trace_dir.is_dir()
        ):
            last = self._last_good_by_source.get("operations")
            if last is not None and last.moa_trace_count:
                raise RuntimeError("MoA trace directory disappeared or became unsafe")
            return operations
        traces = [
            path
            for path in trace_dir.glob("*.jsonl")
            if path.is_file()
            and not path.is_symlink()
            and _path_resolves_under(path, self._paths.root_home)
        ]
        if not traces:
            return operations
        newest = max(traces, key=lambda path: path.stat().st_mtime)
        latest_record = _moa_latest_record_summary(newest, self._log_tail_bytes)
        return operations.model_copy(
            update={
                "moa_trace_count": len(traces),
                "moa_trace_size_bytes": sum(_file_size(path) for path in traces),
                "moa_trace_newest_session_id": newest.stem,
                "moa_trace_newest_mtime": _mtime(newest),
                "moa_trace_latest_record_summary": latest_record[0],
                "moa_trace_latest_record_keys": latest_record[1],
            }
        )

    def _with_projects(self, operations: OperationsState) -> OperationsState:
        db_path = self._paths.profile_path("projects.db")
        last = self._last_good_by_source.get("operations")
        if not _exists_strict(db_path):
            if last is not None and last.projects_db_present:
                raise RuntimeError("projects.db disappeared")
            return operations
        # Confined to profile_home, not root_home: a sibling profile's projects.db
        # is still under the root, and this source is profile-scoped.
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.profile_home):
            if last is not None and last.projects_db_present:
                raise RuntimeError("projects.db replaced by unsafe path")
            return operations
        with _connect_readonly_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return _read_projects_state(conn, operations, self._paths)

    def _with_goals(self, operations: OperationsState) -> OperationsState:
        """Apply every state.db-backed operations source from one open.

        Goals, delegations and DB-maintenance metadata all live in state.db, so
        they share the single (mtime-cached) readout with the gateway ledgers
        rather than taking a WAL snapshot each.
        """
        readout = self._read_state_db()
        if readout is None:
            last = self._last_good_by_source.get("operations")
            if last is not None and (
                last.goal_count or last.delegation_count or last.state_db_schema_version
            ):
                raise RuntimeError("state.db operations data disappeared or became unsafe")
            return operations
        db_path = self._paths.profile_path("state.db")
        update = _state_db_update(readout.state, now=self._clock(), pid_exists=self._pid_exists)
        update["state_db_size_bytes"] = _file_size(db_path)
        update["state_db_wal_size_bytes"] = _file_size(db_path.with_name(f"{db_path.name}-wal"))
        update["delegation_live_log_count"] = _count_delegation_live_logs(
            self._paths.shared_path("cache", "delegation", "live"),
            self._paths.root_home,
        )
        return operations.model_copy(update=update)

    def _read_state_db(self) -> _StateDbReadout | None:
        """Operations tables and gateway ledgers from one state.db pass; None when absent.

        Runs on the connection HermesDB already holds for the very same
        state.db, so the WAL is snapshotted once per change instead of twice
        per tick; the readout is redone only when state.db (or its -wal)
        changes and is shared by every source in a pass.
        """
        db_path = self._paths.profile_path("state.db")
        if (
            not _exists_strict(db_path)
            or db_path.is_symlink()
            # Confined to profile_home, not root_home: a sibling profile's
            # state.db is still under the root, and this source is
            # profile-scoped. In root mode profile_home is root_home.
            or not _path_resolves_under(db_path, self._paths.profile_home)
        ):
            return None
        mtime = _db_source_mtime_ns(db_path)
        cached = self._state_db_cache
        if cached is not None and mtime is not None and cached[0] == mtime:
            return cached[1]
        readout = self._db.run_readout(_state_db_readout)
        if mtime is not None:
            self._state_db_cache = (mtime, readout)
        return readout

    def _with_blocked_scripts(self, operations: OperationsState) -> OperationsState:
        return operations.model_copy(
            update=_read_blocked_scripts(
                self._paths.shared_path("cache", "blocked-scripts"),
                self._paths.root_home,
                now=self._clock(),
            )
        )

    def _with_state_snapshots(self, operations: OperationsState) -> OperationsState:
        return operations.model_copy(
            update=_read_state_snapshots(
                self._paths.shared_path("state-snapshots"),
                self._paths.root_home,
                now=self._clock(),
            )
        )

    def _with_db_recovery(self, operations: OperationsState) -> OperationsState:
        """Recovery artifacts beside the profile-scoped ``state.db``.

        PROFILE-scoped, and it agrees with upstream: the database hermes-agent
        repairs is ``get_hermes_home()/"state.db"`` (``hermes_state.py:160``,
        repair invoked at ``:535``), and every artifact is written as a sibling of
        it (``hermes_state_repair.py:317``, ``hermes_state_dbfile.py:228``). The
        scan is therefore confined to ``profile_home``, not ``root_home``: a
        sibling profile's ledger is that profile's evidence, not this one's. See
        ``.codex/rules/source-ownership.md``.

        Nothing here repairs, checkpoints, integrity-checks or hashes the
        database — only a bounded directory listing, ``stat`` on name-matched
        entries, and two small JSON manifests. Read errors and corrupt manifests
        propagate so this source alone falls back to its last-good value.
        """
        db_path = self._paths.profile_path("state.db")
        return operations.model_copy(
            update={
                "db_recovery": _read_db_recovery(
                    db_path, self._paths.profile_home, now=self._clock()
                )
            }
        )

    def _with_hosted_rooms(self, operations: OperationsState) -> OperationsState:
        """Hosted-room coordination from the ROOT ``shared-state.db``.

        ROOT-scoped *and* deliberately not the master ``state.db``:
        ``gateway/hosted_rooms.py:398-414`` resolves the hosted-room database to
        ``<root>/shared-state.db`` even for a profile gateway, because pointing
        profile gateways at the session store makes every profile process a
        long-lived writer on it. Upstream pins that with its own test
        (``tests/gateway/test_hosted_rooms.py:1344-1364``). The live ``state.db``
        still carries empty legacy ``hosted_room*`` tables, so reading *that*
        file would report a dead table as the coordination state. See
        ``.codex/rules/source-ownership.md``.

        Content-free: grants, link catalogs and target URLs, event payloads and
        actors, revoked-grant scope keys and the ``hosted_room_policy_*``
        transcript tables are never selected. An absent database is not a
        failure; one lost or made unsafe after a good read raises so this source
        keeps its last-good value.
        """
        db_path = self._paths.shared_path("shared-state.db")
        last = self._last_good_by_source.get("hosted_rooms")
        if not _exists_strict(db_path):
            if last is not None and last.hosted_rooms.db_present:
                raise RuntimeError("shared-state.db disappeared")
            return operations
        # The symlink test is the load-bearing half: shared_path() can only
        # resolve outside root_home through a link, and this source is ROOT-scoped
        # so root_home is the confinement boundary (as in _with_response_store).
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.root_home):
            if last is not None and last.hosted_rooms.db_present:
                raise RuntimeError("shared-state.db replaced by unsafe path")
            return operations
        with _connect_readonly_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            hosted_rooms = _read_hosted_rooms(
                conn, now=self._clock(), db_size_bytes=_file_size(db_path)
            )
        return operations.model_copy(update={"hosted_rooms": hosted_rooms})

    def _with_api_runs(self, operations: OperationsState) -> OperationsState:
        """Retained API run reservations from ``runs_idempotency.db``.

        PROFILE-scoped, and it agrees with upstream:
        ``gateway/platforms/api_server_run_idempotency.py:67`` resolves
        ``get_hermes_home()/"runs_idempotency.db"``. That is the opposite of the
        ROOT-scoped ``shared-state.db`` read beside it, and the two are documented
        as separate rows in ``.codex/rules/source-ownership.md`` for exactly that
        reason.

        ``fingerprint``, ``idempotency_key`` and ``scope`` are never selected. An
        absent or empty store is *not* reported as "no API activity": upstream
        prunes an aged row only once its status is terminal, and falls back to
        process memory when the file cannot be opened — a fallback hermesd cannot
        observe, because the ``durable`` capability is only served over HTTP.
        """
        db_path = self._paths.profile_path("runs_idempotency.db")
        last = self._last_good_by_source.get("api_runs")
        if not _exists_strict(db_path):
            if last is not None and last.api_runs.db_present:
                raise RuntimeError("runs_idempotency.db disappeared")
            return operations
        # Confined to profile_home, not root_home: a sibling profile's store is
        # still under the root, and this source is profile-scoped.
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.profile_home):
            if last is not None and last.api_runs.db_present:
                raise RuntimeError("runs_idempotency.db replaced by unsafe path")
            return operations
        with _connect_readonly_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            api_runs = _read_api_runs(
                conn,
                now=self._clock(),
                db_size_bytes=_file_size(db_path),
                pid_exists=self._pid_exists,
            )
        return operations.model_copy(update={"api_runs": api_runs})

    def _collect_curator(self) -> CuratorRun:
        # Read the scheduler state and curator config once for the whole pass;
        # both the no-run fallback and the populated run apply the same overlay.
        scheduler_state = self._read_json_cached(
            self._paths.profile_path("skills", ".curator_state")
        )
        curator_cfg = _as_dict(self._read_yaml_reporting_stale().get("curator"))
        base_run = _curator_with_scheduler_state(CuratorRun(), scheduler_state, curator_cfg)
        curator_dir = self._paths.shared_path("logs", "curator")
        if (
            curator_dir.is_symlink()
            or not _path_resolves_under(curator_dir, self._paths.root_home)
            or not curator_dir.is_dir()
        ):
            return base_run
        # Skip symlinked run dirs and any path that escapes the Hermes home,
        # matching the symlink hardening on the cron/checkpoint readers.
        run_dirs = sorted(
            p
            for p in curator_dir.iterdir()
            if p.is_dir() and not p.is_symlink() and _path_resolves_under(p, self._paths.root_home)
        )
        if not run_dirs:
            return base_run
        data: JsonMapping = {}
        newest: Path | None = None
        for candidate in reversed(run_dirs):
            run_json = candidate / "run.json"
            if run_json.is_symlink():
                continue
            data = self._read_json_cached(run_json)
            if data:
                newest = candidate
                break
        if newest is None:
            return base_run
        counts = _as_dict(data.get("counts"))
        tool_call_counts = _int_mapping(data.get("tool_call_counts"))
        state_transitions = [
            _state_transition_label(entry)
            for entry in _as_list(data.get("state_transitions"))
            if isinstance(entry, dict)
        ]
        return _curator_with_scheduler_state(
            CuratorRun(
                run_present=True,
                stamp=newest.name,
                started_at=str(data.get("started_at") or ""),
                duration_seconds=_coerce_float(data.get("duration_seconds")),
                model=str(data.get("model") or ""),
                provider=str(data.get("provider") or ""),
                count_before=_coerce_int(counts.get("before")),
                count_after=_coerce_int(counts.get("after")),
                count_delta=_coerce_int(counts.get("delta")),
                archived_count=_coerce_int(counts.get("archived_this_run"))
                or _len_if_sized(data.get("archived")),
                added_count=_coerce_int(counts.get("added_this_run"))
                or _len_if_sized(data.get("added")),
                pruned_count=_coerce_int(counts.get("pruned_this_run"))
                or _len_if_sized(data.get("pruned")),
                consolidated_count=_coerce_int(counts.get("consolidated_this_run"))
                or _len_if_sized(data.get("consolidated")),
                tool_calls_total=_coerce_int(counts.get("tool_calls_total")),
                tool_call_counts=tool_call_counts,
                state_transitions=state_transitions,
                llm_summary=str(data.get("llm_summary") or ""),
                llm_error=str(data.get("llm_error") or ""),
            ),
            scheduler_state,
            curator_cfg,
        )

    def _collect_model_caches(self) -> list[ModelCacheSummary]:
        cache_names = [
            "models_dev_cache.json",
            "provider_models_cache.json",
            "ollama_cloud_models_cache.json",
        ]
        summaries = []
        for cache_name in cache_names:
            path = self._paths.shared_path(cache_name)
            data = self._read_json_cached(path)
            if not data and not path.exists():
                continue
            provider_count, model_count = _model_cache_counts(data)
            summaries.append(
                ModelCacheSummary(
                    name=cache_name,
                    provider_count=provider_count,
                    model_count=model_count,
                    size_bytes=_file_size(path),
                    mtime=_mtime(path),
                )
            )
        return summaries

    def _collect_pr_monitors(self) -> list[PRMonitorSummary]:
        base = self._paths.shared_path()
        # The agent writes PR-monitor state under several naming families: flat
        # hyphen/underscore files and per-repo files inside pr-monitor/pr_monitor
        # subdirs. Read them all; sorted+dict-keyed paths keep the scan stable.
        paths = sorted(
            {
                path
                for pattern in (
                    "pr-monitor-*.json",
                    "pr_monitor_*.json",
                    "pr-monitor/*.json",
                    "pr_monitor/*.json",
                    # hermes-agent >= 0.21 keeps PR-monitor state here, next to
                    # a .bak and a .lock sibling that the *.json glob excludes.
                    "cron/state/pr_monitor*.json",
                )
                for path in base.glob(pattern)
                if path.is_file() and not path.is_symlink() and _path_resolves_under(path, base)
            }
        )
        # Collapse the same repo (seen across families) to its newest state;
        # files without a repo stay distinct, keyed by filename.
        deduped: dict[str, PRMonitorSummary] = {}
        for path in paths:
            data = self._read_json_cached(path)
            if not data:
                continue
            summary = _pr_monitor_summary(path.name, data)
            key = summary.repo or f"::{path.name}"
            existing = deduped.get(key)
            if existing is None or summary.checked_at > existing.checked_at:
                deduped[key] = summary
        return sorted(deduped.values(), key=lambda s: (s.repo, s.filename))

    def _collect_skills_memory(self) -> SkillsMemory:
        categories: set[str] = set()
        skills: list[SkillInfo] = []
        skills_dir = self._paths.profile_path("skills")

        # Build skill list from actual directory structure (authoritative)
        if skills_dir.is_dir():
            for cat_dir in sorted(skills_dir.iterdir()):
                if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                    continue
                cat = cat_dir.name
                for skill_dir in sorted(cat_dir.iterdir()):
                    if not skill_dir.is_dir():
                        continue
                    categories.add(cat)
                    desc = self._read_skill_description(cat, skill_dir.name)
                    skills.append(SkillInfo(name=skill_dir.name, category=cat, description=desc))

        mem_dir = self._paths.profile_path("memories")
        mem_count = len(_memory_file_names(mem_dir))

        auth_data = self._read_json_cached(self._paths.shared_path("auth.json"))
        cfg = self._read_yaml_reporting_stale()
        boot_md = self._paths.shared_path("BOOT.md")
        providers = self._collect_providers(auth_data)
        plugins, plugin_scan_truncated = self._collect_plugins(cfg)
        return SkillsMemory(
            skill_count=len(skills),
            skill_categories=len(categories),
            memory_file_count=mem_count,
            providers=providers,
            credential_pools=self._collect_credential_pools(auth_data),
            hooks=self._collect_hooks(),
            plugins=plugins,
            plugin_scan_truncated=plugin_scan_truncated,
            mcp_servers=self._collect_mcp_servers(cfg),
            boot_md_present=boot_md.exists(),
            boot_md_mtime=_mtime(boot_md),
            skills=skills,
        )

    def _collect_mcp_cache(self) -> MCPSchemaCache:
        path = self._paths.shared_path("cache", "mcp_schema_cache.json")
        if not _safe_or_absent_child_path(path, self._paths.root_home):
            return MCPSchemaCache()
        if not path.is_file() or path.is_symlink():
            return MCPSchemaCache()
        return _mcp_schema_cache_summary(
            self._read_json_reporting_stale(path),
            self._file_age_seconds(path),
            self._configured_mcp_server_names(),
            # Entry TTL is evaluated against the injected clock, not the file
            # mtime and not time.time(), so a frozen clock is testable.
            self._clock(),
        )

    def _configured_mcp_server_names(self) -> list[str]:
        """Every configured MCP server name, for cache-membership comparison.

        ``ConfigSummary.mcp_server_names`` is display-bounded, so membership has
        to be computed from the full set here: comparing against the truncated
        list would report every configured server past the cap as uncached.
        """
        servers = _as_dict(self._read_yaml_reporting_stale().get("mcp_servers"))
        return sorted(str(name) for name in servers)

    def _collect_skills_prompt(self) -> SkillsPromptSnapshot:
        path = self._paths.shared_path(".skills_prompt_snapshot.json")
        if not _safe_or_absent_child_path(path, self._paths.root_home):
            return SkillsPromptSnapshot()
        return _skills_prompt_summary(
            self._read_json_reporting_stale(path), self._file_age_seconds(path)
        )

    def _file_age_seconds(self, path: Path) -> float | None:
        """Age of ``path`` against the injected clock, clamped at zero."""
        return _age_seconds(_mtime(path), self._clock())

    def _collect_memory(self) -> MemoryOverview:
        cfg = self._read_yaml_reporting_stale()
        memory_cfg = _as_dict(cfg.get("memory"))
        memories_dir = self._paths.profile_path("memories")
        soul_path = self._paths.profile_path("SOUL.md")
        root = self._paths.root_home

        memory_files = _memory_file_names(memories_dir)
        memory_md = memories_dir / "MEMORY.md"
        user_md = memories_dir / "USER.md"
        learning_summary = _learning_summary(
            self._paths.profile_path("skills"),
            self._read_json_cached(self._paths.profile_path("skills", ".usage.json")),
            frontmatter=self._cached_frontmatter,
        )

        return MemoryOverview(
            provider=str(memory_cfg.get("provider") or ""),
            memory_file_count=len(memory_files),
            memory_word_count=self._cached_word_count(memory_md, root),
            user_word_count=self._cached_word_count(user_md, root),
            soul_size_bytes=_file_size(soul_path),
            soul_excerpt=self._cached_soul_excerpt(soul_path, root),
            memory_files=memory_files,
            skill_usage_count=learning_summary["used"],
            learned_skill_count=learning_summary["learned"],
            pinned_skill_count=learning_summary["pinned"],
            agent_created_skill_count=learning_summary["agent"],
            memory_card_count=self._cached_card_count(memory_md, root)
            + self._cached_card_count(user_md, root),
        )

    def _collect_hooks(self) -> list[HookInfo]:
        hooks_dir = self._paths.shared_path("hooks")
        if not hooks_dir.is_dir():
            return []

        hooks: list[HookInfo] = []
        for hook_dir in sorted(hooks_dir.iterdir()):
            if not hook_dir.is_dir():
                continue
            manifest_path = hook_dir / "HOOK.yaml"
            if not _safe_capped_file(manifest_path, hooks_dir):
                continue
            manifest = self._file_cache.read_yaml_mapping(manifest_path)
            if not manifest or not (hook_dir / "handler.py").exists():
                continue
            events = manifest.get("events") or []
            if not isinstance(events, list):
                events = []
            hooks.append(
                HookInfo(
                    name=str(manifest.get("name") or hook_dir.name),
                    description=str(manifest.get("description") or ""),
                    events=[str(event) for event in events if event],
                )
            )
        return hooks

    def _collect_plugins(self, cfg: dict[str, Any]) -> tuple[list[PluginInfo], bool]:
        """Discovered plugins, plus whether the directory walk was cut short.

        Both directory shapes upstream scans (``plugins_discovery.py:102-131``): a
        flat ``<root>/<name>/`` keyed by its manifest name, and a category
        ``<root>/<cat>/<name>/`` keyed by the path ``<cat>/<name>``. A directory
        with no manifest is a *category*, not a plugin, and is recursed into once;
        it is never reported on its own account.

        Three manifest filenames are accepted, in upstream's precedence order, and
        the losers are recorded rather than silently dropped. Every read is
        confined to the plugins root and byte-capped, so — unlike upstream, which
        accepts a symlinked ``plugin.json`` — a symlinked manifest is refused.
        """
        plugins_dir = self._paths.shared_path("plugins")
        if not plugins_dir.is_dir():
            return [], False

        plugins_cfg = _as_dict(cfg.get("plugins"))
        enabled = plugin_name_set(plugins_cfg.get("enabled"))
        disabled = plugin_name_set(plugins_cfg.get("disabled"))
        # One file keyed by manifest name: read once per pass however many plugins
        # were found, because a per-plugin read would be N opens of the same bytes.
        install_metadata = self._read_install_metadata(plugins_dir)

        found, truncated = self._scan_plugin_dirs(plugins_dir)
        plugins = [
            self._read_plugin(
                plugin_dir,
                prefix,
                choice,
                plugins_dir,
                enabled=enabled,
                disabled=disabled,
                install_metadata=install_metadata,
            )
            for plugin_dir, prefix, choice in found
        ]
        return plugins, truncated

    def _with_desktop_plugins(self, current: SkillsMemory) -> SkillsMemory:
        """Add the root app-extension inventory without coupling its health to skills."""
        plugins, truncated = self._collect_desktop_plugins()
        return current.model_copy(
            update={
                "desktop_plugins": plugins,
                "desktop_plugin_scan_truncated": truncated,
            }
        )

    def _collect_desktop_plugins(self) -> tuple[list[DesktopPluginInfo], bool]:
        """Read the app-level root defined by desktop-plugins-root.ts:1-38."""
        return read_desktop_plugins(
            self._paths.shared_path("desktop-plugins"),
            self._paths.root_home,
        )

    def _scan_plugin_dirs(self, base: Path) -> tuple[list[tuple[Path, str, ManifestChoice]], bool]:
        """``(plugin_dir, key prefix, winning manifest)`` triples under ``base``.

        Bounded twice over, because ``~/.hermes`` is untrusted: at most
        ``_PLUGIN_DIR_ENTRY_LIMIT`` entries of any one directory are examined, and
        at most ``_PLUGIN_LIMIT`` plugin directories are retained. Either cap
        firing sets the truncation flag the panel reports — a capped list must
        never read as a complete inventory.
        """
        found: list[tuple[Path, str, ManifestChoice]] = []
        truncated = False

        def walk(scan: Path, prefix: str, depth: int) -> None:
            nonlocal truncated
            entries = sorted(islice(scan.iterdir(), _PLUGIN_DIR_ENTRY_LIMIT + 1))
            if len(entries) > _PLUGIN_DIR_ENTRY_LIMIT:
                truncated = True
            for child in entries[:_PLUGIN_DIR_ENTRY_LIMIT]:
                if not child.is_dir():
                    continue
                choice = choose_manifest(self._plugin_manifest_names(child, base))
                if choice is not None:
                    if len(found) >= _PLUGIN_LIMIT:
                        truncated = True
                        return
                    found.append((child, prefix, choice))
                elif depth < MAX_PLUGIN_SCAN_DEPTH and _path_resolves_under(child, base):
                    # No manifest: a category, recursed into once. Past the cap
                    # upstream logs "no plugin.yaml, depth cap reached" and stops.
                    walk(child, category_prefix(prefix, child.name), depth + 1)

        walk(base, "", 0)
        return found, truncated

    def _plugin_manifest_names(self, plugin_dir: Path, base: Path) -> tuple[str, ...]:
        """Manifest filenames present in one plugin directory.

        Returned in ``MANIFEST_NAMES`` order rather than in the order they were
        stat'd, so :func:`choose_manifest` cannot be fed a readdir-order-dependent
        candidate list. A symlink is refused outright and an oversized manifest is
        refused by the byte cap: upstream accepts a symlinked ``plugin.json``,
        hermesd does not, because this check is what stops a plugin tree from
        steering a read outside ``~/.hermes``.
        """
        present: list[str] = []
        for name in MANIFEST_NAMES:
            path = plugin_dir / name
            if _exists_strict(path) and _safe_capped_file(path, base):
                present.append(name)
        return tuple(present)

    def _read_plugin(
        self,
        plugin_dir: Path,
        prefix: str,
        choice: ManifestChoice,
        base: Path,
        *,
        enabled: frozenset[str],
        disabled: frozenset[str],
        install_metadata: JsonMapping,
    ) -> PluginInfo:
        """Build one plugin's record from its winning manifest and its sidecars."""
        portable = choice.filename == PORTABLE_MANIFEST_NAME
        manifest_path = plugin_dir / choice.filename
        raw = (
            self._read_json_cached(manifest_path)
            if portable
            else self._file_cache.read_yaml_mapping(manifest_path)
        )
        if not raw:
            # Present but did not parse (or parsed to nothing). Reported rather
            # than dropped: this directory is a plugin, and the manifest that
            # would have named it is the thing that failed.
            return self._unusable_plugin(plugin_dir, choice, f"{choice.filename} is unreadable")

        caps: list[str] = []
        cap_count = 0
        tools: object = []
        hooks: object = []
        if portable:
            parsed, error = parse_portable_manifest(raw)
            if parsed is None:
                return self._unusable_plugin(plugin_dir, choice, error)
            name, version, description = parsed.name, parsed.version, parsed.description
            # portable_plugin_manifest maps only name/version/description, and
            # never sets kind — so there is no __init__.py scan and no capability
            # or version-gate declaration to read here, however plausible the
            # plugin.json looks.
            kind = PLUGIN_KIND_STANDALONE
            key = plugin_key(prefix=prefix, dirname=plugin_dir.name, name=name)
            requires = ""
        else:
            name = str(raw.get("name") or plugin_dir.name)
            version = str(raw.get("version") or "")
            description = str(raw.get("description") or "")
            kind = resolve_plugin_kind(
                raw.get("kind"),
                self._plugin_init_source(plugin_dir),
                declared_present="kind" in raw,
            )
            key = plugin_key(prefix=prefix, dirname=plugin_dir.name, name=name)
            requires = requires_hermes_spec(raw.get("requires_hermes"))
            caps, cap_count = declared_capabilities(raw.get("capabilities"))
            tools = raw.get("provides_tools") or []
            hooks = raw.get("provides_hooks") or raw.get("hooks") or []

        gate = gate_plugin(key=key, name=name, kind=kind, enabled=enabled, disabled=disabled)
        install = install_provenance(install_metadata.get(name))
        catalog = self._read_catalog_sidecar(plugin_dir, base)
        dashboard_manifest = self._read_json_cached(plugin_dir / "dashboard" / "manifest.json")
        return PluginInfo(
            name=name,
            version=version,
            description=description,
            source="user",
            activation=gate.activation,
            activation_reason=gate.reason,
            kind=kind,
            manifest_key=key,
            manifest_file=choice.filename,
            manifest_shadowed=list(choice.shadowed),
            tool_count=len(tools) if isinstance(tools, list) else 0,
            hook_count=len(hooks) if isinstance(hooks, list) else 0,
            dashboard_enabled=bool(dashboard_manifest),
            requires_hermes=requires,
            declared_capabilities=caps,
            declared_capability_count=cap_count,
            installed_revision=install.revision,
            pinned_revision=install.pinned_revision,
            install_source=install.source,
            catalog_name=catalog.name if catalog else "",
            catalog_repo=catalog.repo if catalog else "",
            catalog_sha=catalog.sha if catalog else "",
            catalog_tier=catalog.tier if catalog else "",
            catalog_installed_at=catalog.installed_at if catalog else "",
        )

    def _unusable_plugin(self, plugin_dir: Path, choice: ManifestChoice, reason: str) -> PluginInfo:
        """A manifest that is present but unusable: reported, never dropped.

        The directory name is all hermesd can state with evidence, because the
        manifest that would have named the plugin is the thing that failed.
        """
        return PluginInfo(
            name=plugin_dir.name,
            activation=PluginActivation.UNKNOWN,
            activation_reason=reason,
            manifest_file=choice.filename,
            manifest_shadowed=list(choice.shadowed),
        )

    def _read_install_metadata(self, plugins_dir: Path) -> JsonMapping:
        """``plugins/.install-metadata.json`` — one read per pass, keyed by name.

        Upstream raises ``PluginOperationError`` on a copy it cannot parse
        (``plugins_cmd.py:434-441``). hermesd treats it as no provenance instead:
        a read-only viewer must not fail a whole panel over a sidecar it reads
        only to annotate that panel, and "no provenance recorded" is the truthful
        reading of a file hermesd cannot parse.
        """
        path = plugins_dir / INSTALL_METADATA_NAME
        if not _exists_strict(path) or not _safe_capped_file(path, plugins_dir):
            return {}
        return self._read_json_confined(path)

    def _read_catalog_sidecar(self, plugin_dir: Path, base: Path) -> CatalogProvenance | None:
        """``<plugin_dir>/.hermes-catalog.json``, or None for a non-catalog install."""
        path = plugin_dir / CATALOG_SIDECAR_NAME
        if not _exists_strict(path) or not _safe_capped_file(path, base):
            return None
        return catalog_provenance(self._read_json_confined(path))

    def _plugin_init_source(self, plugin_dir: Path) -> str:
        """Text of a plugin's ``__init__.py``, for import-free kind detection.

        Upstream routes an undeclared memory/model provider to its own discovery
        by scanning this file; reading it is what lets hermesd agree without
        importing plugin code.
        """
        path = plugin_dir / "__init__.py"
        if not _safe_capped_file(path, plugin_dir):
            return ""
        return _read_text_capped(path, self._paths.root_home)

    def _collect_mcp_servers(self, cfg: dict[str, Any]) -> list[MCPServerInfo]:
        servers = _as_dict(cfg.get("mcp_servers"))
        result: list[MCPServerInfo] = []
        for name, raw_server in sorted(servers.items()):
            server = _as_dict(raw_server)
            if not server:
                continue
            args = server.get("args") or []
            command = _redact_command_string(str(server.get("command") or ""))
            env = _as_dict(server.get("env") or server.get("environment"))
            url = _redact_secret_url(str(server.get("url") or ""))
            target = url
            transport = "url" if url else ""
            if not target and command:
                rendered_env = "env:[REDACTED]" if _has_secret_material(env) else ""
                rendered_args = (
                    " ".join(_redact_secret_args(args)) if isinstance(args, list) else ""
                )
                target = " ".join(part for part in [rendered_env, command, rendered_args] if part)
                transport = "command"
            result.append(
                MCPServerInfo(
                    name=str(name),
                    enabled=bool(server.get("enabled", True)),
                    transport=transport,
                    target=target,
                    tool_filter=_mcp_tool_filter_summary(_as_dict(server.get("tools"))),
                )
            )
        return result

    def _collect_checkpoints(self) -> list[CheckpointInfo]:
        checkpoints_dir = self._paths.profile_path("checkpoints")
        if not checkpoints_dir.is_dir():
            return []

        checkpoints: list[CheckpointInfo] = []
        for repo_dir in sorted(checkpoints_dir.iterdir()):
            if repo_dir.is_symlink() or not repo_dir.is_dir():
                continue
            checkpoints.append(self._summarize_checkpoint(repo_dir))
        return checkpoints

    def _summarize_checkpoint(self, repo_dir: Path) -> CheckpointInfo:
        workdir_file = repo_dir / "HERMES_WORKDIR"
        workdir = _read_text_capped(workdir_file, repo_dir).strip()
        commit_count, last_checkpoint_at, last_reason = self._checkpoint_summary(repo_dir)
        workdir_name = Path(workdir).name if workdir else ""
        return CheckpointInfo(
            repo_id=repo_dir.name,
            workdir=workdir,
            workdir_name=workdir_name,
            commit_count=commit_count,
            last_reason=last_reason,
            last_checkpoint_at=last_checkpoint_at,
        )

    def _checkpoint_summary(self, repo_dir: Path) -> tuple[int, float | None, str]:
        # Two git subprocesses per repo per tick dominate a collect pass; the
        # answer only changes when the repo's refs change.
        key = str(repo_dir)
        signature = _git_ref_signature(repo_dir)
        cached = self._checkpoint_summary_cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        summary = _git_checkpoint_summary(repo_dir)
        self._checkpoint_summary_cache[key] = (signature, summary)
        return summary

    def _read_skill_description(self, category: str, name: str) -> str:
        """Read the description from a skill's SKILL.md frontmatter."""
        skills_dir = self._paths.profile_path("skills")
        # Skills are at skills/<category>/<name>/SKILL.md
        skill_md = skills_dir / category / name / "SKILL.md"
        return self._signature_cached(
            "skill_desc",
            skill_md,
            lambda: _skill_description(skill_md, skills_dir),
        )

    def _collect_providers(self, data: dict[str, Any]) -> list[ProviderInfo]:
        if not data:
            return []
        active = str(data.get("active_provider") or "")
        pool = _as_dict(data.get("credential_pool"))
        providers_section = _as_dict(data.get("providers"))
        all_names = set(pool.keys()) | set(providers_section.keys())
        return [ProviderInfo(name=name, is_active=(name == active)) for name in sorted(all_names)]

    def _collect_credential_pools(self, data: dict[str, Any]) -> list[CredentialPoolEntry]:
        if not data:
            return []

        providers_section = _as_dict(data.get("providers"))
        entries = []
        for name, raw_entry in sorted(_as_dict(data.get("credential_pool")).items()):
            entry = _select_pool_entry(raw_entry)
            provider_entry = _as_dict(providers_section.get(name))
            entries.append(
                CredentialPoolEntry(
                    name=str(name),
                    label=str(entry.get("label") or name),
                    auth_type=_credential_auth_type(entry, provider_entry),
                    source=str(entry.get("source") or ""),
                    last_status=str(entry.get("last_status") or entry.get("status") or ""),
                    request_count=_coerce_int(entry.get("request_count") or entry.get("requests")),
                    cooldown_remaining=str(entry.get("cooldown_remaining") or ""),
                    priority=_coerce_int(entry.get("priority")),
                    token_present=_has_secret_material(entry)
                    or _has_secret_material(provider_entry),
                    expires_at=_credential_expiry(entry, provider_entry),
                    last_refresh=str(
                        entry.get("last_refresh")
                        or entry.get("refreshed_at")
                        or provider_entry.get("last_refresh")
                        or ""
                    ),
                )
            )
        return entries

    def _collect_logs(self) -> LogState:
        # Each entry carries the scope that owns it: `profile_path` streams live
        # under the selected profile, `shared_path` streams under the root. The
        # two cannot be told apart from `LogStream.path` (a bare file name), so
        # the scope travels with the stream — see
        # .codex/rules/source-ownership.md.
        root = SourceScope.ROOT
        profile = SourceScope.PROFILE
        stream_specs = [
            ("agent", self._paths.profile_path("logs", "agent.log"), _LOG_TAIL_LINES, profile),
            ("gateway", self._paths.profile_path("logs", "gateway.log"), _LOG_TAIL_LINES, profile),
            (
                "errors",
                self._paths.profile_path("logs", "errors.log"),
                _ERROR_LOG_TAIL_LINES,
                profile,
            ),
            ("desktop", self._paths.shared_path("logs", "desktop.log"), _LOG_TAIL_LINES, root),
            ("dashboard", self._paths.shared_path("logs", "dashboard.log"), _LOG_TAIL_LINES, root),
            ("gui", self._paths.shared_path("logs", "gui.log"), _LOG_TAIL_LINES, root),
            ("update", self._paths.shared_path("logs", "update.log"), _LOG_TAIL_LINES, root),
            (
                "gateway.error",
                self._paths.shared_path("logs", "gateway.error.log"),
                _LOG_TAIL_LINES,
                root,
            ),
            (
                "tui crash",
                self._paths.shared_path("logs", "tui_gateway_crash.log"),
                _LOG_TAIL_LINES,
                root,
            ),
            ("audit", self._paths.shared_path("logs", "audit.log"), _LOG_TAIL_LINES, root),
            (
                "mcp.stderr",
                self._paths.shared_path("logs", "mcp-stderr.log"),
                _LOG_TAIL_LINES,
                root,
            ),
            ("workspace", self._paths.shared_path("logs", "workspace.log"), _LOG_TAIL_LINES, root),
            (
                "workspace.error",
                self._paths.shared_path("logs", "workspace.error.log"),
                _LOG_TAIL_LINES,
                root,
            ),
        ]
        streams = [
            self._tail_log_stream(name, path, max_lines, scope)
            for name, path, max_lines, scope in stream_specs
            if _exists_strict(path) or str(path) in self._log_cache
        ]
        cron_lines = self._tail_latest_cron_output(
            self._paths.shared_path("cron", "output"), _LOG_TAIL_LINES
        )
        if cron_lines:
            streams.append(
                LogStream(
                    name="cron",
                    path="cron/output",
                    scope=SourceScope.ROOT,
                    lines=cron_lines,
                )
            )
        stream_map = {stream.name: stream.lines for stream in streams}
        return LogState(
            agent_lines=stream_map.get("agent", []),
            gateway_lines=stream_map.get("gateway", []),
            error_lines=stream_map.get("errors", []),
            cron_lines=cron_lines,
            streams=streams,
        )

    def _collect_profiles(self) -> ProfilesState:
        profiles_dir = self._paths.shared_path("profiles")
        if (
            profiles_dir.is_symlink()
            or not _path_resolves_under(profiles_dir, self._paths.root_home)
            or not profiles_dir.is_dir()
        ):
            return ProfilesState()
        profiles = [
            self._summarize_profile(profile_dir.name, profile_dir)
            for profile_dir in sorted(profiles_dir.iterdir())
            if profile_dir.is_dir()
            and not profile_dir.is_symlink()
            and _path_resolves_under(profile_dir, profiles_dir)
        ]
        return ProfilesState(profile_count=len(profiles), profiles=profiles)

    def _collect_runtime_status(
        self, gateway: GatewayState, sessions: list[SessionInfo]
    ) -> RuntimeStatus:
        last_activity_age = _latest_runtime_activity_age(self._paths, self._clock())
        has_active_sessions = any(session.is_active for session in sessions)
        recent_activity = (
            last_activity_age is not None and last_activity_age <= _RECENT_ACTIVITY_WINDOW_SECONDS
        )
        agent_running = gateway.running or has_active_sessions or recent_activity
        banner = "" if agent_running else "AGENT OFFLINE"
        return RuntimeStatus(
            agent_running=agent_running,
            last_activity_age_seconds=last_activity_age,
            banner=banner,
        )

    def _summarize_profile(self, name: str, profile_home: Path) -> ProfileSummary:
        db_path = profile_home / "state.db"
        logs_path = profile_home / "logs"
        skills_path = profile_home / "skills"
        soul_path = profile_home / "SOUL.md"
        db_safe = _safe_child_path(db_path, profile_home)
        logs_safe = _safe_child_path(logs_path, profile_home)
        skills_safe = _safe_child_path(skills_path, profile_home)
        soul_safe = _safe_child_path(soul_path, profile_home)
        if self._last_profile_exists(name) and not all(
            (
                _safe_or_absent_child_path(db_path, profile_home),
                _safe_or_absent_child_path(logs_path, profile_home),
                _safe_or_absent_child_path(skills_path, profile_home),
                _safe_or_absent_child_path(soul_path, profile_home),
            )
        ):
            raise RuntimeError(f"profile {name} contains an unsafe replacement path")
        session_count = self._profile_session_count(name, db_path) if db_safe else 0
        return ProfileSummary(
            name=name,
            session_count=session_count,
            latest_log_mtime=_latest_log_mtime(logs_path) if logs_safe else None,
            skill_count=_count_skills(skills_path) if skills_safe else 0,
            db_size_bytes=_file_size(db_path) if db_safe else 0,
            soul_excerpt=(self._cached_soul_excerpt(soul_path, profile_home) if soul_safe else ""),
        )

    def _last_profile_exists(self, name: str) -> bool:
        last = self._last_good_by_source.get("profiles")
        if last is None:
            return False
        return any(profile.name == name for profile in last.profiles)

    def _profile_session_count(self, name: str, db_path: Path) -> int:
        # Opening a profile DB snapshots WAL files to a temp dir, so only
        # re-open and re-count when the db (or its -wal) mtime changes.
        mtime = _db_source_mtime_ns(db_path)
        cached = self._profile_count_cache.get(name)
        if cached is not None and mtime is not None and cached[0] == mtime:
            return cached[1]
        if cached is not None and mtime is None:
            return cached[1]
        db = self._db_factory(db_path)
        try:
            session_count = db.read_session_count()
            if getattr(db, "last_read_session_count_stale", False):
                raise RuntimeError("profile db returned cached count after sqlite error")
        finally:
            db.close()
        self._profile_count_cache[name] = (mtime, session_count)
        return session_count

    def _tail_log_stream(
        self, name: str, path: Path, max_lines: int, scope: SourceScope
    ) -> LogStream:
        key = str(path)
        if not _path_resolves_under(path, self._paths.root_home) or not path.exists():
            return LogStream(
                name=name, path=path.name, scope=scope, lines=self._log_cache.get(key, [])
            )
        size_bytes = _file_size(path)
        mtime = _mtime(path)
        cached_stream = self._log_stream_cache.get(key)
        if (
            cached_stream is not None
            and mtime is not None
            and cached_stream[0] == mtime
            and cached_stream[1] == size_bytes
        ):
            return cached_stream[2]
        try:
            text = _read_tail_text(path, self._log_tail_bytes)
            lines = [line[:_MAX_LOG_LINE_CHARS] for line in text.strip().splitlines()[-max_lines:]]
            result = []
            for line in lines:
                match = _LOG_LINE_PATTERN.match(line)
                if match:
                    ts = match.group(1).split()[-1]
                    message = _redact_secret_text(match.group(4).strip())
                    result.append(
                        LogLine(
                            timestamp=ts,
                            component=match.group(2).strip(),
                            level=match.group(3),
                            session_id=_extract_session_id(message),
                            message=message,
                        )
                    )
                elif line.strip():
                    result.append(LogLine(message=_redact_secret_text(line.strip())))
            if result:
                self._log_cache[key] = result
            stream = LogStream(
                name=name,
                path=path.name,
                scope=scope,
                size_bytes=size_bytes,
                mtime=mtime,
                lines=result if result else self._log_cache.get(key, []),
            )
            self._log_stream_cache[key] = (mtime, size_bytes, stream)
            return stream
        except OSError:
            return LogStream(
                name=name, path=path.name, scope=scope, lines=self._log_cache.get(key, [])
            )

    def _tail_latest_cron_output(self, output_root: Path, max_lines: int) -> list[LogLine]:
        key = f"cron:{output_root}"
        result = _tail_latest_cron_output(
            output_root,
            max_lines,
            self._log_tail_bytes,
            stop_at=self._paths.root_home,
        )
        if result:
            self._log_cache[key] = result
            return result
        return self._log_cache.get(key, [])

    def _latest_cron_output_excerpt(
        self,
        output_root: Path,
        job_id: str,
        max_bytes: int,
    ) -> tuple[str, bool, str, float | None]:
        cache_key = f"{output_root}:{job_id}"
        cached = self._cron_excerpt_cache.get(cache_key)
        latest = _latest_cron_output_file(output_root, job_id, stop_at=self._paths.root_home)
        if latest is None:
            if cached is not None:
                return cached[1]
            return "", False, "", None
        output_signature: tuple[str, int, int] | tuple[str, float | None]
        output_signature = _file_signature(latest) or (latest.name, _mtime(latest))
        if cached is not None and cached[0] == output_signature:
            return cached[1]
        excerpt = _latest_cron_output_excerpt(
            output_root,
            job_id,
            max_bytes,
            stop_at=self._paths.root_home,
        )
        if not excerpt[2]:
            if cached is not None:
                return cached[1]
            return "", False, "", None
        redacted = (
            _redact_secret_text(excerpt[0]),
            excerpt[1],
            excerpt[2],
            excerpt[3],
        )
        # Key on the mtime of the file actually read (excerpt[3]), not the
        # earlier independent directory scan, so key and content never diverge
        # when a newer output lands between the two scans.
        actual_path = output_root / job_id / excerpt[2]
        actual_signature = _file_signature(actual_path) or (excerpt[2], excerpt[3])
        self._cron_excerpt_cache[cache_key] = (actual_signature, redacted)
        return redacted

    def _collect_version_behind(self) -> int:
        data = self._read_json_cached(self._paths.shared_path(".update_check"))
        if data:
            return _coerce_int(data.get("behind"))
        return 0

    def _collect_skin(self) -> str:
        cfg = self._read_yaml_reporting_stale()
        skin = _as_dict(cfg.get("display")).get("skin", "default")
        if not skin:
            return "default"
        return normalize_skin_name(str(skin))

    def interrupt_searches(self) -> None:
        """Abort any in-flight SQLite query (e.g. a long message search)."""
        self._db.interrupt()

    def close(self) -> None:
        self._closing.set()
        with self._lock:
            self._closed = True
            self._db.close()
