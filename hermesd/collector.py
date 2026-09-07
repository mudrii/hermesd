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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Never, TypeVar

from hermesd.collect.common import (
    _MAX_TEXT_READ_BYTES,
    _as_dict,
    _as_list,
    _coerce_float,
    _coerce_int,
    _db_source_mtime_ns,
    _file_signature,
    _file_size,
    _int_mapping,
    _len_if_sized,
    _local_date,
    _mtime,
    _path_resolves_under,
    _read_tail_text,
    _read_text_capped,
    _safe_capped_file,
    _safe_child_path,
    _safe_or_absent_child_path,
    _today_epoch,
)
from hermesd.collect.config import (
    _channel_capabilities,
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
    _cron_job_dispatch,
    _cron_job_repeat,
    _cron_suggestion_count,
    _cron_ticker_ages,
    _cron_ticker_health,
    _delivery_target_label,
    _latest_cron_output_excerpt,
    _latest_cron_output_file,
    _read_cron_executions_state,
    _tail_latest_cron_output,
)
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
from hermesd.collect.operations import (
    _curator_with_scheduler_state,
    _goal_state_update,
    _is_dashboard_process,
    _moa_latest_record_summary,
    _model_cache_counts,
    _read_projects_state,
    _read_verification_evidence,
    _state_transition_label,
)
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
    _memory_card_count,
    _read_soul_excerpt,
    _skill_description,
    _skill_frontmatter,
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
    _pid_exists,
)
from hermesd.db import HermesDB
from hermesd.defaults import DEFAULT_LOG_TAIL_BYTES
from hermesd.file_cache import JsonMapping, JsonObjectList, LastGoodFileCache
from hermesd.models import (
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
    GatewayState,
    HealthSummary,
    HookInfo,
    KanbanBoardSummary,
    KanbanState,
    LogLine,
    LogState,
    LogStream,
    MCPServerInfo,
    MemoryOverview,
    ModelCacheSummary,
    OperationsState,
    PlatformStatus,
    PluginInfo,
    PRMonitorSummary,
    ProfilesState,
    ProfileSummary,
    ProviderInfo,
    RuntimeStatus,
    SessionInfo,
    SkillInfo,
    SkillsMemory,
    TokenAnalytics,
    TokenSummary,
    ToolGatewayRoute,
    ToolStats,
)
from hermesd.paths import HermesPaths
from hermesd.theme import normalize_skin_name

T = TypeVar("T")


def _closing_source() -> Never:
    raise RuntimeError("collector is closing")


class _SourceSpec(NamedTuple):
    """One entry in the dashboard-state collection table.

    ``field`` is both the DashboardState field and the ``_last_state``
    attribute read for the last-good fallback; ``fallback`` overrides that
    default for the two sources whose fallback is not a plain attribute read.
    """

    field: str
    source_name: str
    collect: Callable[[], Any]
    default_factory: Callable[[], Any]
    fallback: Callable[[], Any] | None = None


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
        self._log_tail_bytes = max(1024, log_tail_bytes)
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
        self._session_tool_names_cache: dict[
            str, tuple[tuple[str, int, int] | None, tuple[str, ...]]
        ] = {}
        self._last_state: DashboardState | None = None
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
        self._goal_state_cache: tuple[int | None, dict[str, Any]] | None = None
        self._checkpoint_summary_cache: dict[
            str, tuple[tuple[int, ...], tuple[int, float | None, str]]
        ] = {}
        # Derived values (word counts, card counts, excerpts, frontmatter)
        # keyed on the source file's signature, so an unchanged SKILL.md /
        # MEMORY.md / USER.md / SOUL.md is not re-read on every tick.
        self._derived_file_cache: dict[str, tuple[tuple[str, int, int] | None, Any]] = {}
        self._kanban_board_errors: list[str] = []
        self._derived_rows: list[dict[str, Any]] | None = None
        self._derived_date = ""
        self._derived_cache: dict[str, Any] = {}
        self._closed = False
        # Set by close() before it queues for _lock; an in-flight collect pass
        # checks it between sources and stops doing new work.
        self._closing = threading.Event()
        # _lock serializes collect() passes and guards the collector-internal
        # caches mutated during a pass (_file_cache, _log_cache,
        # _log_stream_cache, _available_tools_cache_*, _profile_count_cache,
        # _derived_*) plus _last_state/_last_session_rows. It is deliberately
        # NOT taken by search_session_ids_by_message(): HermesDB serializes its
        # own access, so a slow collect pass (git subprocesses, per-profile DB
        # snapshots) must not stall message search.
        self._lock = threading.RLock()

    def collect(self) -> DashboardState:
        with self._lock:
            if self._closed:
                raise RuntimeError("collector is closed")
            health = _CollectionHealth()
            session_rows = self._collect_session_rows(health)
            state = self._build_dashboard_state(health, session_rows)
            if not health.failed_sources:
                self._last_state = state
            return state

    def _build_dashboard_state(
        self,
        health: _CollectionHealth,
        session_rows: list[dict[str, Any]],
    ) -> DashboardState:
        session_rows_stale = "sessions" in health.failed_sources
        results: dict[str, Any] = {}

        def derived(name: str, compute: Callable[[list[dict[str, Any]]], T]) -> Callable[[], T]:
            # Shared shape for every session-row-derived source: memoized via
            # _derived_from_rows on fresh (non-stale) rows.
            return lambda: self._derived_from_rows(
                name,
                self._fresh_session_rows(session_rows, session_rows_stale),
                compute,
            )

        def last_good(field_name: str, default_factory: Callable[[], Any]) -> Callable[[], Any]:
            return lambda: (
                getattr(self._last_state, field_name)
                if self._last_state is not None
                else default_factory()
            )

        # Collected in order: entries below may read an earlier source's result
        # out of `results` (channels/runtime need gateway, operations needs the
        # background processes).
        specs = (
            _SourceSpec(
                "sessions", "session_models", derived("sessions", self._collect_sessions), list
            ),
            _SourceSpec(
                "available_tools",
                "tools_index",
                self._collect_available_tools,
                lambda: (0, []),
                # One source feeds two state fields, so its last-good fallback
                # cannot be a plain attribute read off _last_state.
                fallback=self._last_available_tools,
            ),
            _SourceSpec("gateway", "gateway", self._collect_gateway, GatewayState),
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
                derived("token_analytics", self._collect_token_analytics),
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
            _SourceSpec("skills_memory", "skills", self._collect_skills_memory, SkillsMemory),
            _SourceSpec("memory", "memory", self._collect_memory, MemoryOverview),
            _SourceSpec("profiles", "profiles", self._collect_profiles, ProfilesState),
            _SourceSpec("logs", "logs", self._collect_logs, LogState),
            _SourceSpec("version_behind", "version_check", self._collect_version_behind, int),
            # Without a last state the fallback is the shipped skin name, not
            # the "" that the str default factory would yield.
            _SourceSpec("active_skin", "skin", self._collect_skin, str, fallback=self._last_skin),
            _SourceSpec("curator", "curator", self._collect_curator, CuratorRun),
            _SourceSpec(
                "runtime",
                "runtime",
                lambda: self._collect_runtime_status(results["gateway"], results["sessions"]),
                RuntimeStatus,
            ),
        )

        self._kanban_board_errors = []
        for spec in specs:
            # close() sets _closing before it queues for the collect lock, so a
            # quit does not wait out a full pass: every source after the current
            # one falls straight back to its last-good value.
            fn = _closing_source if self._closing.is_set() else spec.collect
            results[spec.field] = health.collect(
                spec.fallback or last_good(spec.field, spec.default_factory),
                spec.source_name,
                fn,
                spec.default_factory,
            )
        if self._kanban_board_errors:
            health.mark_failed("kanban", "; ".join(self._kanban_board_errors))

        tool_count, tool_names = results.pop("available_tools")
        health_summary = HealthSummary(
            total_sources=health.total_sources,
            ok_sources=health.total_sources - len(health.failed_sources),
            failed_sources=sorted(health.failed_sources),
            errors={source: health.errors[source] for source in sorted(health.errors)},
        )
        # Every remaining `results` key is a DashboardState field name by
        # construction: _SourceSpec.field is what both the fallback getattr and
        # this expansion key off.
        return DashboardState(
            hermes_home=self._paths.root_home,
            selected_profile=self._paths.profile_name,
            profile_mode_label=self._paths.profile_mode_label,
            collected_at=self._clock(),
            health=health_summary,
            available_tools=tool_count,
            available_tool_names=tool_names,
            **results,
        )

    def _last_available_tools(self) -> tuple[int, list[str]]:
        if self._last_state is None:
            return 0, []
        return self._last_state.available_tools, self._last_state.available_tool_names

    def _last_skin(self) -> str:
        return self._last_state.active_skin if self._last_state is not None else "default"

    def _fresh_session_rows(
        self,
        rows: list[dict[str, Any]],
        session_rows_stale: bool,
    ) -> list[dict[str, Any]]:
        if session_rows_stale:
            raise RuntimeError("session rows are stale")
        return rows

    def _derived_from_rows(
        self,
        name: str,
        rows: list[dict[str, Any]],
        compute: Callable[[list[dict[str, Any]]], T],
    ) -> T:
        # HermesDB returns the same cached list object while data_version is
        # unchanged, so row identity is a cheap invalidation key. The local
        # date is part of the key because "today" aggregates shift at midnight.
        today = _local_date(self._clock())
        if rows is not self._derived_rows or today != self._derived_date:
            self._derived_cache = {}
            self._derived_rows = rows
            self._derived_date = today
        if name not in self._derived_cache:
            self._derived_cache[name] = compute(rows)
        # type-ignore[no-any-return]: heterogeneous per-name cache; each call
        # site pins T via its compute callable.
        return self._derived_cache[name]  # type: ignore[no-any-return]

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

    def _read_json_list_cached(self, path: Path) -> JsonObjectList:
        return self._file_cache.read_json_list(path)

    def _read_yaml_cached(self) -> JsonMapping:
        return self._file_cache.read_yaml_mapping(self._paths.shared_path("config.yaml"))

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
        data = self._read_json_cached(self._paths.shared_path("gateway_state.json"))
        if not data:
            return GatewayState()
        platforms = []
        for name, raw_info in _as_dict(data.get("platforms")).items():
            info = _as_dict(raw_info)
            if not info:
                continue
            platforms.append(
                PlatformStatus(
                    name=str(name),
                    state=str(info.get("state") or "unknown"),
                    updated_at=str(info.get("updated_at") or ""),
                    error_code=str(info.get("error_code") or ""),
                    error_message=str(info.get("error_message") or ""),
                )
            )
        # A non-positive PID is absent, never a target: os.kill(0)/os.kill(-1)
        # would signal a process group or every process the user owns.
        pid = max(0, _coerce_int(data.get("pid")))
        running = data.get("gateway_state") == "running"
        # The PID in gateway_state.json can be stale if launchd restarted
        # the gateway. Check both the recorded PID and the launchd PID.
        if running:
            if pid:
                if not self._pid_exists(pid):
                    # Recorded PID is dead — check if launchd has a live gateway
                    launchd_pid = self._find_gateway_launchd_pid()
                    if launchd_pid:
                        pid = launchd_pid
                    else:
                        running = False
            else:
                launchd_pid = self._find_gateway_launchd_pid()
                if launchd_pid:
                    pid = launchd_pid
                else:
                    running = False
        version, behind = self._collect_hermes_version()
        cfg = self._read_yaml_cached()
        gateway_cfg = _as_dict(cfg.get("gateway"))
        scale_cfg = _as_dict(cfg.get("scale_to_zero")) or _as_dict(gateway_cfg.get("scale_to_zero"))
        active_agents = _coerce_int(data.get("active_agents"))
        drain_request = self._read_json_cached(self._paths.shared_path(".drain_request.json"))
        return GatewayState(
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
            served_profiles=[
                str(profile) for profile in _as_list(data.get("served_profiles")) if profile
            ],
            scale_to_zero_idle_timeout_minutes=_coerce_int(scale_cfg.get("idle_timeout_minutes")),
            scale_to_zero_relay_only=_scale_to_zero_relay_only(scale_cfg, platforms),
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
        data = self._file_cache.read_yaml_mapping(
            self._paths.shared_path("context_length_cache.yaml")
        )
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
                session_id=r["id"],
                source=r.get("source") or "",
                model=r.get("model") or "",
                parent_session_id=r.get("parent_session_id") or "",
                billing_provider=r.get("billing_provider") or "",
                billing_base_url=r.get("billing_base_url") or "",
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
                started_at=r.get("started_at") or 0.0,
                ended_at=r.get("ended_at"),
                title=r.get("title"),
                is_active=r.get("ended_at") is None and not bool(r.get("archived") or 0),
            )
            for r in rows
        ]

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
                _background_process_from_ledger(entry)
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
            )
            for entry in entries
            if str(entry.get("session_id") or "")
        ]

    def _collect_available_tools(self) -> tuple[int, list[str]]:
        banner_names = self._banner_snapshot_tool_names()
        if banner_names:
            return len(banner_names), banner_names
        return self._session_file_tool_names()

    def _banner_snapshot_tool_names(self) -> list[str]:
        """Tool names from cache/banner_snapshot.json, the live tool inventory."""
        path = self._paths.shared_path("cache", "banner_snapshot.json")
        if not _safe_child_path(path, self._paths.root_home):
            return []
        return sorted(_tool_names_from_entries(self._read_json_cached(path).get("tools")))

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
        session_file_signatures = tuple(
            sorted((_file_signature(path) for path in session_files), key=str)
        )
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
            signature = _file_signature(session_file)
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
        cfg = self._read_yaml_cached()
        if not cfg:
            # A config.yaml that parses to nothing after a good read is a
            # truncated or emptied write, not a real "no configuration": fail
            # the source so the last-good summary survives instead of blanking
            # the config panel (same guard shape as the kanban.db readers).
            if self._last_state is not None and self._last_state.config != ConfigSummary():
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
        cfg = self._read_yaml_cached()
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
                        paused_reason=str(j.get("paused_reason") or ""),
                        last_delivery_error=str(j.get("last_delivery_error") or ""),
                        dispatch_lateness_seconds=dispatch_lateness,
                        dispatch_kind=dispatch_kind,
                        repeat_times=repeat_times,
                        repeat_completed=repeat_completed,
                        no_agent=bool(j.get("no_agent")),
                    )
                )

        heartbeat_age, last_success_age = _cron_ticker_ages(
            self._paths.shared_path("cron"), now=self._clock()
        )
        return CronState(
            last_tick_ago_seconds=last_tick,
            ticker_heartbeat_age_seconds=heartbeat_age,
            ticker_last_success_age_seconds=last_success_age,
            ticker_health=_cron_ticker_health(heartbeat_age, last_success_age),
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
            suggestion_count=_cron_suggestion_count(self._paths.shared_path("cron")),
            jobs=jobs,
        )

    def _collect_cron_executions(self, cron: CronState) -> CronExecutionsState:
        """Execution history and incidents, named from the already-collected jobs."""
        job_names = {job.job_id: job.name for job in cron.jobs if job.job_id}
        return _read_cron_executions_state(
            self._paths.shared_path("cron", "executions.db"),
            job_names,
            now=self._clock(),
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
        cfg = self._read_yaml_cached()
        kanban_cfg = _as_dict(cfg.get("kanban"))
        base_state = KanbanState(
            db_present=self._paths.shared_path("kanban.db").exists(),
            current_board=self._read_current_kanban_board(),
            dispatch_in_gateway=bool(kanban_cfg.get("dispatch_in_gateway")),
            dispatch_interval_seconds=_coerce_int(kanban_cfg.get("dispatch_interval_seconds")),
            claim_ttl_seconds=_kanban_claim_ttl_seconds(kanban_cfg),
            auto_decompose=bool(kanban_cfg.get("auto_decompose")),
            failure_limit=_coerce_int(kanban_cfg.get("failure_limit")),
        )
        db_path = self._paths.shared_path("kanban.db")
        if not db_path.exists():
            if self._last_state is not None and self._last_state.kanban.db_present:
                raise RuntimeError("kanban.db disappeared")
            return self._with_kanban_boards(base_state)
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.root_home):
            if self._last_state is not None and self._last_state.kanban.db_present:
                raise RuntimeError("kanban.db replaced by unsafe path")
            return self._with_kanban_boards(base_state)
        return self._with_kanban_boards(_read_kanban_state(db_path, base_state, now=self._clock()))

    def _read_current_kanban_board(self) -> str:
        path = self._paths.shared_path("kanban", "current")
        if path.is_symlink() or not _path_resolves_under(path, self._paths.root_home):
            if self._last_state is not None and self._last_state.kanban.current_board:
                raise RuntimeError("kanban current board replaced by unsafe path")
            return ""
        try:
            with path.open("rb") as handle:
                raw = handle.read(_MAX_TEXT_READ_BYTES)
        except OSError:
            if self._last_state is not None and self._last_state.kanban.current_board:
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
        desktop_stamp = self._read_json_cached(self._paths.shared_path("desktop-build-stamp.json"))
        stamp_label = str(
            desktop_stamp.get("version")
            or desktop_stamp.get("stamp")
            or desktop_stamp.get("builtAt")
            or desktop_stamp.get("built_at")
            or desktop_stamp.get("created_at")
            or str(desktop_stamp.get("contentHash") or "")[:12]
            or ""
        )
        operations = OperationsState(
            dashboard_process_count=dashboard_process_count,
            desktop_build_stamp=stamp_label,
            model_caches=self._collect_model_caches(),
            pr_monitors=self._collect_pr_monitors(),
        )
        operations = self._with_response_store(operations)
        operations = self._with_verification_evidence(operations)
        operations = self._with_goals(operations)
        operations = self._with_moa_traces(operations)
        return self._with_projects(operations)

    def _with_response_store(self, operations: OperationsState) -> OperationsState:
        db_path = self._paths.shared_path("response_store.db")
        if not db_path.exists():
            if self._last_state is not None and self._last_state.operations.response_store_present:
                raise RuntimeError("response_store.db disappeared")
            return operations
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.root_home):
            if self._last_state is not None and self._last_state.operations.response_store_present:
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
        if not db_path.exists():
            if self._last_state is not None and self._last_state.operations.verification_db_present:
                raise RuntimeError("verification_evidence.db disappeared")
            return operations
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.root_home):
            if self._last_state is not None and self._last_state.operations.verification_db_present:
                raise RuntimeError("verification_evidence.db replaced by unsafe path")
            return operations
        with _connect_readonly_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return _read_verification_evidence(conn, operations)

    def _with_moa_traces(self, operations: OperationsState) -> OperationsState:
        cfg = self._read_yaml_cached()
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
            if self._last_state is not None and self._last_state.operations.moa_trace_count:
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
        if not db_path.exists():
            if self._last_state is not None and self._last_state.operations.projects_db_present:
                raise RuntimeError("projects.db disappeared")
            return operations
        if db_path.is_symlink() or not _path_resolves_under(db_path, self._paths.root_home):
            if self._last_state is not None and self._last_state.operations.projects_db_present:
                raise RuntimeError("projects.db replaced by unsafe path")
            return operations
        with _connect_readonly_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return _read_projects_state(conn, operations, self._paths)

    def _with_goals(self, operations: OperationsState) -> OperationsState:
        db_path = self._paths.profile_path("state.db")
        if (
            not db_path.exists()
            or db_path.is_symlink()
            or not _path_resolves_under(db_path, self._paths.root_home)
        ):
            if self._last_state is not None and self._last_state.operations.goal_count:
                raise RuntimeError("state.db goal state disappeared or became unsafe")
            return operations
        # Opening state.db snapshots its WAL to a temp dir on every tick, on top
        # of the snapshot HermesDB already takes; only redo it when state.db
        # (or its -wal) changes.
        mtime = _db_source_mtime_ns(db_path)
        cached = self._goal_state_cache
        if cached is not None and mtime is not None and cached[0] == mtime:
            return operations.model_copy(update=cached[1])
        with _connect_readonly_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            update = _goal_state_update(conn)
        self._goal_state_cache = (mtime, update)
        return operations.model_copy(update=update)

    def _collect_curator(self) -> CuratorRun:
        # Read the scheduler state and curator config once for the whole pass;
        # both the no-run fallback and the populated run apply the same overlay.
        scheduler_state = self._read_json_cached(
            self._paths.profile_path("skills", ".curator_state")
        )
        curator_cfg = _as_dict(self._read_yaml_cached().get("curator"))
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
            summary = PRMonitorSummary(
                filename=path.name,
                repo=str(data.get("repo") or ""),
                checked_at=str(data.get("checked_at") or ""),
                monitored_count=_len_if_sized(data.get("prs"))
                or _len_if_sized(data.get("monitored")),
                tracked_count=_len_if_sized(data.get("tracked_numbers"))
                or _len_if_sized(data.get("tracked")),
                author_pr_count=_len_if_sized(data.get("author_prs"))
                or _len_if_sized(data.get("author_pr_numbers")),
            )
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
        mem_count = 0
        if mem_dir.is_dir():
            mem_count = sum(1 for f in mem_dir.iterdir() if f.is_file())

        auth_data = self._read_json_cached(self._paths.shared_path("auth.json"))
        cfg = self._read_yaml_cached()
        boot_md = self._paths.shared_path("BOOT.md")
        providers = self._collect_providers(auth_data)
        return SkillsMemory(
            skill_count=len(skills),
            skill_categories=len(categories),
            memory_file_count=mem_count,
            providers=providers,
            credential_pools=self._collect_credential_pools(auth_data),
            hooks=self._collect_hooks(),
            plugins=self._collect_plugins(cfg),
            mcp_servers=self._collect_mcp_servers(cfg),
            boot_md_present=boot_md.exists(),
            boot_md_mtime=_mtime(boot_md),
            skills=skills,
        )

    def _collect_memory(self) -> MemoryOverview:
        cfg = self._read_yaml_cached()
        memory_cfg = _as_dict(cfg.get("memory"))
        memories_dir = self._paths.profile_path("memories")
        soul_path = self._paths.profile_path("SOUL.md")
        root = self._paths.root_home

        memory_files = (
            sorted(path.name for path in memories_dir.iterdir() if path.is_file())
            if memories_dir.is_dir()
            else []
        )
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

    def _collect_plugins(self, cfg: dict[str, Any]) -> list[PluginInfo]:
        plugins_dir = self._paths.shared_path("plugins")
        if not plugins_dir.is_dir():
            return []

        disabled_cfg = _as_dict(cfg.get("plugins"))
        disabled_list = disabled_cfg.get("disabled") or []
        disabled = {str(name) for name in disabled_list if name}

        plugins: list[PluginInfo] = []
        for plugin_dir in sorted(plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            manifest_path = plugin_dir / "plugin.yaml"
            if not _safe_capped_file(manifest_path, plugins_dir):
                continue
            manifest = self._file_cache.read_yaml_mapping(manifest_path)
            if not manifest:
                continue
            dashboard_manifest = self._read_json_cached(plugin_dir / "dashboard" / "manifest.json")
            tools = manifest.get("provides_tools") or []
            hooks = manifest.get("provides_hooks") or manifest.get("hooks") or []
            name = str(manifest.get("name") or plugin_dir.name)
            plugins.append(
                PluginInfo(
                    name=name,
                    version=str(manifest.get("version") or ""),
                    description=str(manifest.get("description") or ""),
                    source="user",
                    enabled=name not in disabled,
                    tool_count=len(tools) if isinstance(tools, list) else 0,
                    hook_count=len(hooks) if isinstance(hooks, list) else 0,
                    dashboard_enabled=bool(dashboard_manifest),
                )
            )
        return plugins

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
        stream_specs = [
            ("agent", self._paths.profile_path("logs", "agent.log"), _LOG_TAIL_LINES),
            ("gateway", self._paths.profile_path("logs", "gateway.log"), _LOG_TAIL_LINES),
            ("errors", self._paths.profile_path("logs", "errors.log"), _ERROR_LOG_TAIL_LINES),
            ("desktop", self._paths.shared_path("logs", "desktop.log"), _LOG_TAIL_LINES),
            ("dashboard", self._paths.shared_path("logs", "dashboard.log"), _LOG_TAIL_LINES),
            ("gui", self._paths.shared_path("logs", "gui.log"), _LOG_TAIL_LINES),
            ("update", self._paths.shared_path("logs", "update.log"), _LOG_TAIL_LINES),
            (
                "gateway.error",
                self._paths.shared_path("logs", "gateway.error.log"),
                _LOG_TAIL_LINES,
            ),
            (
                "tui crash",
                self._paths.shared_path("logs", "tui_gateway_crash.log"),
                _LOG_TAIL_LINES,
            ),
            ("audit", self._paths.shared_path("logs", "audit.log"), _LOG_TAIL_LINES),
            ("mcp.stderr", self._paths.shared_path("logs", "mcp-stderr.log"), _LOG_TAIL_LINES),
            ("workspace", self._paths.shared_path("logs", "workspace.log"), _LOG_TAIL_LINES),
            (
                "workspace.error",
                self._paths.shared_path("logs", "workspace.error.log"),
                _LOG_TAIL_LINES,
            ),
        ]
        streams = [
            self._tail_log_stream(name, path, max_lines)
            for name, path, max_lines in stream_specs
            if path.exists() or str(path) in self._log_cache
        ]
        cron_lines = self._tail_latest_cron_output(
            self._paths.shared_path("cron", "output"), _LOG_TAIL_LINES
        )
        if cron_lines:
            streams.append(
                LogStream(
                    name="cron",
                    path="cron/output",
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
        if self._last_state is None:
            return False
        return any(profile.name == name for profile in self._last_state.profiles.profiles)

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

    def _tail_log_stream(self, name: str, path: Path, max_lines: int) -> LogStream:
        key = str(path)
        if not _path_resolves_under(path, self._paths.root_home) or not path.exists():
            return LogStream(name=name, path=path.name, lines=self._log_cache.get(key, []))
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
                size_bytes=size_bytes,
                mtime=mtime,
                lines=result if result else self._log_cache.get(key, []),
            )
            self._log_stream_cache[key] = (mtime, size_bytes, stream)
            return stream
        except OSError:
            return LogStream(name=name, path=path.name, lines=self._log_cache.get(key, []))

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
        cfg = self._read_yaml_cached()
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
