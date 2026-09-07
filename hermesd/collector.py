from __future__ import annotations

import contextlib
import functools
import json
import math
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import yaml

from hermesd.db import HermesDB, snapshot_wal_database
from hermesd.file_cache import JsonMapping, JsonObjectList, LastGoodFileCache
from hermesd.models import (
    AUTHORITATIVE_COST_STATUSES,
    BackgroundProcessInfo,
    ChannelDirectoryState,
    ChannelPlatformInfo,
    CheckpointInfo,
    ConfigSummary,
    CredentialPoolEntry,
    CronJob,
    CronState,
    CuratorRun,
    DashboardState,
    DiscoveredRepoSummary,
    GatewayState,
    GoalSummary,
    HealthSummary,
    HookInfo,
    KanbanBoardSummary,
    KanbanRunSummary,
    KanbanState,
    KanbanTaskLink,
    KanbanTaskSummary,
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
    ProjectSummary,
    ProviderInfo,
    RuntimeStatus,
    SessionInfo,
    SkillInfo,
    SkillsMemory,
    TokenAnalytics,
    TokenBreakdown,
    TokenSummary,
    TokenWindowSummary,
    ToolGatewayRoute,
    ToolStats,
    VerificationEventSummary,
    VerificationRootSummary,
)
from hermesd.paths import HermesPaths
from hermesd.theme import normalize_skin_name

T = TypeVar("T")
_LOG_LINE_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),?\d*\s*-\s*([^-]+?)\s*-\s*(\w+)\s*-\s*(.*)"
)


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
        log_tail_bytes: int = 32768,
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
        self._profile_count_cache: dict[str, tuple[float | None, int]] = {}
        self._kanban_board_cache: dict[str, KanbanBoardSummary] = {}
        self._kanban_board_errors: list[str] = []
        self._derived_rows: list[dict[str, Any]] | None = None
        self._derived_date = ""
        self._derived_cache: dict[str, Any] = {}
        self._closed = False
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
        safe_collect = health.collect
        session_rows_stale = "sessions" in health.failed_sources

        def empty_tool_stats() -> list[ToolStats]:
            return []

        def empty_background_processes() -> list[BackgroundProcessInfo]:
            return []

        def empty_checkpoints() -> list[CheckpointInfo]:
            return []

        def empty_sessions() -> list[SessionInfo]:
            return []

        def derived(name: str, compute: Callable[[list[dict[str, Any]]], T]) -> Callable[[], T]:
            # Shared shape for every session-row-derived source: memoized via
            # _derived_from_rows on fresh (non-stale) rows.
            return lambda: self._derived_from_rows(
                name,
                self._fresh_session_rows(session_rows, session_rows_stale),
                compute,
            )

        sessions = safe_collect(
            lambda: self._last_state.sessions if self._last_state is not None else empty_sessions(),
            "session_models",
            derived("sessions", self._collect_sessions),
            empty_sessions,
        )
        available_tools = safe_collect(
            lambda: (
                (
                    self._last_state.available_tools,
                    self._last_state.available_tool_names,
                )
                if self._last_state is not None
                else (0, [])
            ),
            "tools_index",
            self._collect_available_tools,
            lambda: (0, []),
        )
        tool_count, tool_names = available_tools
        gateway = safe_collect(
            lambda: self._last_state.gateway if self._last_state is not None else GatewayState(),
            "gateway",
            self._collect_gateway,
            GatewayState,
        )
        tokens_today = safe_collect(
            lambda: (
                self._last_state.tokens_today if self._last_state is not None else TokenSummary()
            ),
            "tokens_today",
            derived("tokens_today", self._collect_tokens_today),
            TokenSummary,
        )
        tokens_total = safe_collect(
            lambda: (
                self._last_state.tokens_total if self._last_state is not None else TokenSummary()
            ),
            "tokens_total",
            derived("tokens_total", self._collect_tokens_total),
            TokenSummary,
        )
        token_analytics = safe_collect(
            lambda: (
                self._last_state.token_analytics
                if self._last_state is not None
                else TokenAnalytics()
            ),
            "token_analytics",
            derived("token_analytics", self._collect_token_analytics),
            TokenAnalytics,
        )
        tool_stats = safe_collect(
            lambda: (
                self._last_state.tool_stats if self._last_state is not None else empty_tool_stats()
            ),
            "tool_stats",
            lambda: self._collect_tool_stats(
                session_rows,
                session_rows_stale=session_rows_stale,
            ),
            empty_tool_stats,
        )
        total_tool_calls = safe_collect(
            lambda: self._last_state.total_tool_calls if self._last_state is not None else 0,
            "tool_call_total",
            derived("tool_call_total", self._collect_total_tool_calls),
            int,
        )
        background_processes = safe_collect(
            lambda: (
                self._last_state.background_processes
                if self._last_state is not None
                else empty_background_processes()
            ),
            "background_processes",
            self._collect_background_processes,
            empty_background_processes,
        )
        checkpoints = safe_collect(
            lambda: (
                self._last_state.checkpoints
                if self._last_state is not None
                else empty_checkpoints()
            ),
            "checkpoints",
            self._collect_checkpoints,
            empty_checkpoints,
        )
        config = safe_collect(
            lambda: self._last_state.config if self._last_state is not None else ConfigSummary(),
            "config",
            self._collect_config,
            ConfigSummary,
        )
        cron = safe_collect(
            lambda: self._last_state.cron if self._last_state is not None else CronState(),
            "cron",
            self._collect_cron,
            CronState,
        )
        channels = safe_collect(
            lambda: (
                self._last_state.channels
                if self._last_state is not None
                else ChannelDirectoryState()
            ),
            "channels",
            lambda: self._collect_channels(gateway),
            ChannelDirectoryState,
        )
        self._kanban_board_errors = []
        kanban = safe_collect(
            lambda: self._last_state.kanban if self._last_state is not None else KanbanState(),
            "kanban",
            self._collect_kanban,
            KanbanState,
        )
        if self._kanban_board_errors:
            health.mark_failed("kanban", "; ".join(self._kanban_board_errors))
        operations = safe_collect(
            lambda: (
                self._last_state.operations if self._last_state is not None else OperationsState()
            ),
            "operations",
            lambda: self._collect_operations(background_processes),
            OperationsState,
        )
        skills_memory = safe_collect(
            lambda: (
                self._last_state.skills_memory if self._last_state is not None else SkillsMemory()
            ),
            "skills",
            self._collect_skills_memory,
            SkillsMemory,
        )
        memory = safe_collect(
            lambda: self._last_state.memory if self._last_state is not None else MemoryOverview(),
            "memory",
            self._collect_memory,
            MemoryOverview,
        )
        profiles = safe_collect(
            lambda: self._last_state.profiles if self._last_state is not None else ProfilesState(),
            "profiles",
            self._collect_profiles,
            ProfilesState,
        )
        logs = safe_collect(
            lambda: self._last_state.logs if self._last_state is not None else LogState(),
            "logs",
            self._collect_logs,
            LogState,
        )
        version_behind = safe_collect(
            lambda: self._last_state.version_behind if self._last_state is not None else 0,
            "version_check",
            self._collect_version_behind,
            int,
        )
        active_skin = safe_collect(
            lambda: self._last_state.active_skin if self._last_state is not None else "default",
            "skin",
            self._collect_skin,
            str,
        )
        curator = safe_collect(
            lambda: self._last_state.curator if self._last_state is not None else CuratorRun(),
            "curator",
            self._collect_curator,
            CuratorRun,
        )
        runtime = safe_collect(
            lambda: self._last_state.runtime if self._last_state is not None else RuntimeStatus(),
            "runtime",
            lambda: self._collect_runtime_status(gateway, sessions),
            RuntimeStatus,
        )
        health_summary = HealthSummary(
            total_sources=health.total_sources,
            ok_sources=health.total_sources - len(health.failed_sources),
            failed_sources=sorted(health.failed_sources),
            errors={source: health.errors[source] for source in sorted(health.errors)},
        )
        return DashboardState(
            hermes_home=self._paths.root_home,
            selected_profile=self._paths.profile_name,
            profile_mode_label=self._paths.profile_mode_label,
            collected_at=self._clock(),
            health=health_summary,
            runtime=runtime,
            gateway=gateway,
            sessions=sessions,
            tokens_today=tokens_today,
            tokens_total=tokens_total,
            token_analytics=token_analytics,
            tool_stats=tool_stats,
            total_tool_calls=total_tool_calls,
            available_tools=tool_count,
            available_tool_names=tool_names,
            background_processes=background_processes,
            checkpoints=checkpoints,
            config=config,
            cron=cron,
            channels=channels,
            kanban=kanban,
            operations=operations,
            skills_memory=skills_memory,
            memory=memory,
            profiles=profiles,
            logs=logs,
            version_behind=version_behind,
            active_skin=active_skin,
            curator=curator,
        )

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
        today = _local_date()
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
        pid = _coerce_int(data.get("pid"))
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
                content = pid_file.read_text().strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        lpid = int(data.get("pid", 0) or 0)
                    else:
                        lpid = int(content)
                    if lpid and self._pid_exists(lpid):
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
            started_at_min=_today_epoch(),
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
        for session_file in session_files:
            data = self._read_json_cached(session_file)
            if isinstance(data, dict) and "tools" in data:
                for t in _as_list(data["tools"]):
                    if isinstance(t, dict):
                        name = t.get("function", {}).get("name") or t.get("name", "")
                    else:
                        name = str(t)
                    if name:
                        names.add(name)
        tool_names = sorted(names)
        self._available_tools_cache_key = cache_key
        self._available_tools_cache_value = (len(tool_names), tool_names)
        return self._available_tools_cache_value

    def _collect_config(self) -> ConfigSummary:
        cfg = self._read_yaml_cached()
        if not cfg:
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
        personality = agent_cfg.get("active_personality", "")
        if not personality:
            personalities = _as_dict(agent_cfg.get("personalities"))
            if personalities:
                personality = next(iter(personalities))
        dashboard_auth_provider = str(
            dashboard_cfg.get("auth_provider")
            or dashboard_cfg.get("auth")
            or self._env.get("HERMES_DASHBOARD_AUTH_PROVIDER", "")
        )
        return ConfigSummary(
            model=model_cfg.get("default", ""),
            provider=model_cfg.get("provider", ""),
            personality=personality,
            max_turns=agent_cfg.get("max_turns", 0),
            compression_threshold=comp_cfg.get("threshold", 0.0),
            reasoning_effort=agent_cfg.get("reasoning_effort", ""),
            security_redact=sec_cfg.get("redact_secrets", False),
            approvals_mode=app_cfg.get("mode", ""),
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
                jobs.append(
                    CronJob(
                        job_id=str(j.get("id") or ""),
                        name=str(j.get("name") or ""),
                        schedule_display=str(j.get("schedule_display") or ""),
                        state=state,
                        enabled=j.get("enabled", True),
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
                    )
                )

        return CronState(
            last_tick_ago_seconds=last_tick,
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
        return self._with_kanban_boards(_read_kanban_state(db_path, base_state))

    def _read_current_kanban_board(self) -> str:
        path = self._paths.shared_path("kanban", "current")
        if path.is_symlink() or not _path_resolves_under(path, self._paths.root_home):
            if self._last_state is not None and self._last_state.kanban.current_board:
                raise RuntimeError("kanban current board replaced by unsafe path")
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            if self._last_state is not None and self._last_state.kanban.current_board:
                raise
        return ""

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
        with _connect_readonly_sqlite(db_path) as conn:
            conn.row_factory = sqlite3.Row
            return _read_goal_state(conn, operations)

    def _collect_curator(self) -> CuratorRun:
        base_run = _curator_with_scheduler_state(
            CuratorRun(),
            self._read_json_cached(self._paths.profile_path("skills", ".curator_state")),
            _as_dict(self._read_yaml_cached().get("curator")),
        )
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
            self._read_json_cached(self._paths.profile_path("skills", ".curator_state")),
            _as_dict(self._read_yaml_cached().get("curator")),
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

        memory_files = (
            sorted(path.name for path in memories_dir.iterdir() if path.is_file())
            if memories_dir.is_dir()
            else []
        )
        learning_summary = _learning_summary(
            self._paths.profile_path("skills"),
            self._read_json_cached(self._paths.profile_path("skills", ".usage.json")),
        )

        return MemoryOverview(
            provider=str(memory_cfg.get("provider") or ""),
            memory_file_count=len(memory_files),
            memory_word_count=_word_count(memories_dir / "MEMORY.md"),
            user_word_count=_word_count(memories_dir / "USER.md"),
            soul_size_bytes=_file_size(soul_path),
            soul_excerpt=_read_soul_excerpt(soul_path),
            memory_files=memory_files,
            skill_usage_count=learning_summary["used"],
            learned_skill_count=learning_summary["learned"],
            pinned_skill_count=learning_summary["pinned"],
            agent_created_skill_count=learning_summary["agent"],
            memory_card_count=_memory_card_count(memories_dir / "MEMORY.md")
            + _memory_card_count(memories_dir / "USER.md"),
        )

    def _collect_hooks(self) -> list[HookInfo]:
        hooks_dir = self._paths.shared_path("hooks")
        if not hooks_dir.is_dir():
            return []

        hooks: list[HookInfo] = []
        for hook_dir in sorted(hooks_dir.iterdir()):
            if not hook_dir.is_dir():
                continue
            manifest = self._file_cache.read_yaml_mapping(hook_dir / "HOOK.yaml")
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
            manifest = self._file_cache.read_yaml_mapping(plugin_dir / "plugin.yaml")
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
        workdir = ""
        workdir_file = repo_dir / "HERMES_WORKDIR"
        if workdir_file.exists():
            try:
                workdir = workdir_file.read_text(errors="replace").strip()
            except OSError:
                workdir = ""
        commit_count, last_checkpoint_at, last_reason = _git_checkpoint_summary(repo_dir)
        workdir_name = Path(workdir).name if workdir else ""
        return CheckpointInfo(
            repo_id=repo_dir.name,
            workdir=workdir,
            workdir_name=workdir_name,
            commit_count=commit_count,
            last_reason=last_reason,
            last_checkpoint_at=last_checkpoint_at,
        )

    def _read_skill_description(self, category: str, name: str) -> str:
        """Read the description from a skill's SKILL.md frontmatter."""
        skills_dir = self._paths.profile_path("skills")
        # Skills are at skills/<category>/<name>/SKILL.md
        skill_md = skills_dir / category / name / "SKILL.md"
        if not skill_md.exists():
            return ""
        try:
            text = skill_md.read_text(errors="replace")
            lines = text.splitlines()
            if lines and lines[0].strip() == "---":
                frontmatter_lines: list[str] = []
                for line in lines[1:]:
                    if line.strip() == "---":
                        data = yaml.safe_load("\n".join(frontmatter_lines)) or {}
                        if isinstance(data, dict):
                            description = data.get("description")
                            if isinstance(description, str):
                                return description
                        break
                    frontmatter_lines.append(line)
        except OSError:
            pass
        except yaml.YAMLError:
            pass
        return ""

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
            ("agent", self._paths.profile_path("logs", "agent.log"), 20),
            ("gateway", self._paths.profile_path("logs", "gateway.log"), 20),
            ("errors", self._paths.profile_path("logs", "errors.log"), 10),
            ("desktop", self._paths.shared_path("logs", "desktop.log"), 20),
            ("dashboard", self._paths.shared_path("logs", "dashboard.log"), 20),
            ("gui", self._paths.shared_path("logs", "gui.log"), 20),
            ("update", self._paths.shared_path("logs", "update.log"), 20),
            ("gateway.error", self._paths.shared_path("logs", "gateway.error.log"), 20),
            ("tui crash", self._paths.shared_path("logs", "tui_gateway_crash.log"), 20),
            ("audit", self._paths.shared_path("logs", "audit.log"), 20),
            ("mcp.stderr", self._paths.shared_path("logs", "mcp-stderr.log"), 20),
            ("workspace", self._paths.shared_path("logs", "workspace.log"), 20),
            ("workspace.error", self._paths.shared_path("logs", "workspace.error.log"), 20),
        ]
        streams = [
            self._tail_log_stream(name, path, max_lines)
            for name, path, max_lines in stream_specs
            if path.exists() or str(path) in self._log_cache
        ]
        cron_lines = self._tail_latest_cron_output(self._paths.shared_path("cron", "output"), 20)
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
        last_activity_age = _latest_runtime_activity_age(self._paths)
        has_active_sessions = any(session.is_active for session in sessions)
        recent_activity = last_activity_age is not None and last_activity_age <= 300
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
            soul_excerpt=_read_soul_excerpt(soul_path) if soul_safe else "",
        )

    def _last_profile_exists(self, name: str) -> bool:
        if self._last_state is None:
            return False
        return any(profile.name == name for profile in self._last_state.profiles.profiles)

    def _profile_session_count(self, name: str, db_path: Path) -> int:
        # Opening a profile DB snapshots WAL files to a temp dir, so only
        # re-open and re-count when the db (or its -wal) mtime changes.
        mtime = _profile_db_mtime(db_path)
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
            lines = text.strip().splitlines()[-max_lines:]
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
        with self._lock:
            self._closed = True
            self._db.close()


def _today_epoch() -> float:
    import datetime

    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _summarize_tokens(
    rows: list[dict[str, Any]],
    started_at_min: float | None = None,
) -> TokenSummary:
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    reasoning_tokens = 0
    total_cost_usd = 0.0
    contributing_rows = 0
    reported_rows = 0
    for row in rows:
        started_at = row.get("started_at") or 0.0
        if started_at_min is not None and started_at < started_at_min:
            continue
        contributing_rows += 1
        if _session_cost_is_reported(row):
            reported_rows += 1
        input_tokens += row.get("input_tokens") or 0
        output_tokens += row.get("output_tokens") or 0
        cache_read_tokens += row.get("cache_read_tokens") or 0
        cache_write_tokens += row.get("cache_write_tokens") or 0
        reasoning_tokens += row.get("reasoning_tokens") or 0
        total_cost_usd += _resolved_session_cost(row)
    return TokenSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        total_cost_usd=total_cost_usd,
        cost_is_estimated=contributing_rows == 0 or reported_rows < contributing_rows,
    )


def _summarize_window(
    label: str,
    rows: list[dict[str, Any]],
    days: int,
    *,
    now: float | None = None,
) -> TokenWindowSummary:
    cutoff = (now if now is not None else time.time()) - days * 86400
    filtered = [row for row in rows if (row.get("started_at") or 0.0) >= cutoff]
    totals = _summarize_tokens(filtered)
    prompt_tokens = totals.input_tokens + totals.cache_read_tokens
    cache_ratio = totals.cache_read_tokens / prompt_tokens if prompt_tokens > 0 else 0.0
    return TokenWindowSummary(
        label=label,
        session_count=len(filtered),
        input_tokens=totals.input_tokens,
        output_tokens=totals.output_tokens,
        cache_read_tokens=totals.cache_read_tokens,
        total_cost_usd=totals.total_cost_usd,
        cache_ratio=cache_ratio,
    )


def _count_cost_statuses(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("cost_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _summarize_breakdown(rows: list[dict[str, Any]], key_name: str) -> list[TokenBreakdown]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        label = str(row.get(key_name) or "unknown")
        grouped.setdefault(label, []).append(row)

    summaries = []
    for label, group in grouped.items():
        totals = _summarize_tokens(group)
        summaries.append(
            TokenBreakdown(
                label=label,
                session_count=len(group),
                input_tokens=totals.input_tokens,
                output_tokens=totals.output_tokens,
                cache_read_tokens=totals.cache_read_tokens,
                total_cost_usd=totals.total_cost_usd,
            )
        )
    return sorted(
        summaries,
        key=lambda summary: (-summary.total_cost_usd, -summary.input_tokens, summary.label),
    )


# Approximate fallback cost per 1M tokens (USD), not billing authority.
# Provider-reported costs win; keep these estimates reviewed when common model pricing changes.
_COST_PER_M = {
    "input": 2.50,  # GPT-4o / Claude Sonnet class
    "output": 10.00,
    "cache_read": 0.30,  # typical prompt caching discount
    "cache_write": 3.125,  # input x 1.25, typical cache write premium
    "reasoning": 10.00,
}


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    reasoning_tokens: int,
    cache_write_tokens: int = 0,
) -> float:
    input_tokens = _bounded_token_count(input_tokens)
    output_tokens = _bounded_token_count(output_tokens)
    cache_read_tokens = _bounded_token_count(cache_read_tokens)
    cache_write_tokens = _bounded_token_count(cache_write_tokens)
    reasoning_tokens = _bounded_token_count(reasoning_tokens)
    return (
        input_tokens * _COST_PER_M["input"]
        + output_tokens * _COST_PER_M["output"]
        + cache_read_tokens * _COST_PER_M["cache_read"]
        + cache_write_tokens * _COST_PER_M["cache_write"]
        + reasoning_tokens * _COST_PER_M["reasoning"]
    ) / 1_000_000


def _session_cost_is_reported(row: dict[str, Any]) -> bool:
    return (
        str(row.get("cost_status") or "") in AUTHORITATIVE_COST_STATUSES
        and row.get("estimated_cost_usd") is not None
    )


def _resolved_session_cost(row: dict[str, Any]) -> float:
    raw_cost = row.get("estimated_cost_usd")
    cost = _coerce_float(raw_cost)
    if _session_cost_is_reported(row):
        return cost
    if cost:
        return cost
    return _estimate_cost(
        row.get("input_tokens") or 0,
        row.get("output_tokens") or 0,
        row.get("cache_read_tokens") or 0,
        row.get("reasoning_tokens") or 0,
        row.get("cache_write_tokens") or 0,
    )


def _bounded_token_count(value: int) -> int:
    if value <= 0:
        return 0
    return min(value, 10**15)


def _latest_log_mtime(logs_dir: Path) -> float | None:
    if not logs_dir.is_dir():
        return None
    mtimes = []
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    return max(mtimes)


def _latest_runtime_activity_age(paths: HermesPaths) -> float | None:
    now = time.time()
    candidates = [
        paths.profile_path("state.db"),
        paths.profile_path("sessions", "sessions.json"),
        paths.profile_path("logs", "agent.log"),
        paths.shared_path("gateway_state.json"),
    ]
    latest = max((_mtime(path) or 0.0) for path in candidates)
    if latest <= 0.0:
        return None
    return max(0.0, now - latest)


def _count_skills(skills_dir: Path) -> int:
    if not skills_dir.is_dir():
        return 0
    count = 0
    for category_dir in skills_dir.iterdir():
        if not category_dir.is_dir() or category_dir.name.startswith("."):
            continue
        for skill_dir in category_dir.iterdir():
            if skill_dir.is_dir():
                count += 1
    return count


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _word_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return len(path.read_text(errors="replace").split())
    except OSError:
        return 0


def _read_soul_excerpt(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:80]
    except OSError:
        return ""
    return ""


def _learning_summary(skills_dir: Path, usage: dict[str, Any]) -> dict[str, int]:
    usage_metadata = {str(name): _as_dict(raw) for name, raw in usage.items()}
    learned = set(_learned_skill_names(skills_dir))
    pinned = set()
    agent_created = set()

    for name, metadata in usage_metadata.items():
        if _usage_indicates_learned(metadata):
            learned.add(name)
        if bool(metadata.get("pinned")):
            pinned.add(name)
        if str(metadata.get("created_by") or metadata.get("source") or "") == "agent":
            agent_created.add(name)

    learned_dir = skills_dir / "learned"
    if learned_dir.is_dir() and not learned_dir.is_symlink():
        for skill_dir in sorted(learned_dir.iterdir()):
            if not skill_dir.is_dir() or skill_dir.is_symlink():
                continue
            name = skill_dir.name
            metadata = _skill_frontmatter(skill_dir / "SKILL.md")
            learned.add(name)
            if bool(metadata.get("pinned")):
                pinned.add(name)
            if str(metadata.get("created_by") or metadata.get("source") or "") == "agent":
                agent_created.add(name)

    return {
        "used": len(usage_metadata),
        "learned": len(learned),
        "pinned": len(pinned),
        "agent": len(agent_created),
    }


def _usage_indicates_learned(metadata: dict[str, Any]) -> bool:
    return bool(
        metadata.get("learned")
        or metadata.get("agent_created")
        or metadata.get("profile_skill")
        or metadata.get("pinned")
        or str(metadata.get("created_by") or metadata.get("source") or "") == "agent"
    )


def _learned_skill_names(skills_dir: Path) -> list[str]:
    learned_dir = skills_dir / "learned"
    if not learned_dir.is_dir() or learned_dir.is_symlink():
        return []
    return [
        skill_dir.name
        for skill_dir in sorted(learned_dir.iterdir())
        if skill_dir.is_dir() and not skill_dir.is_symlink()
    ]


def _skill_frontmatter(path: Path) -> dict[str, Any]:
    with contextlib.suppress(OSError, yaml.YAMLError):
        lines = path.read_text(errors="replace").splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        frontmatter: list[str] = []
        for line in lines[1:]:
            if line.strip() == "---":
                data = yaml.safe_load("\n".join(frontmatter)) or {}
                return data if isinstance(data, dict) else {}
            frontmatter.append(line)
    return {}


def _memory_card_count(path: Path) -> int:
    with contextlib.suppress(OSError):
        return sum(
            1 for line in path.read_text(errors="replace").splitlines() if line.startswith("## ")
        )
    return 0


def _read_tail_text(path: Path, max_bytes: int) -> str:
    """Read at most the last max_bytes of path, decoded with replacement."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _file_signature(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return str(path), stat.st_mtime_ns, stat.st_size


def _safe_mtime(path: Path) -> float:
    return _mtime(path) or 0.0


def _profile_db_mtime(db_path: Path) -> float | None:
    mtimes = [
        mtime
        for candidate in (db_path, db_path.with_name(f"{db_path.name}-wal"))
        if (mtime := _mtime(candidate)) is not None
    ]
    return max(mtimes) if mtimes else None


def _local_date() -> str:
    return time.strftime("%Y-%m-%d")


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


def _kanban_claim_ttl_seconds(cfg: dict[str, Any]) -> int:
    for key in ("claim_ttl_seconds", "worker_claim_ttl_seconds", "claim_timeout_seconds"):
        value = _coerce_int(cfg.get(key))
        if value > 0:
            return value
    return 300


def _moa_latest_record_summary(path: Path, max_bytes: int) -> tuple[str, list[str]]:
    with contextlib.suppress(OSError):
        for line in reversed(_read_tail_text(path, max_bytes).splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(stripped)
                if isinstance(data, dict):
                    keys = sorted(str(key) for key in data)[:8]
                    labels = [
                        str(data.get(field) or "")
                        for field in ("event", "type", "status", "phase", "preset")
                        if data.get(field)
                    ]
                    return (" ".join(labels[:3]) or "json record", keys)
    return "", []


def _chronos_configured(cfg: dict[str, Any]) -> bool:
    return bool(
        cfg.get("portal_url")
        and cfg.get("callback_url")
        and cfg.get("expected_audience")
        and cfg.get("nas_jwks_url")
    )


def _cron_suggestion_count(cron_dir: Path) -> int:
    candidates = [
        cron_dir / "suggestions.json",
        cron_dir / "cron_suggestions.json",
        cron_dir / "suggestions",
    ]
    total = 0
    for path in candidates:
        if path.is_symlink() or not _path_resolves_under(path, cron_dir.parent):
            continue
        if path.is_file():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                data = json.loads(path.read_text(encoding="utf-8"))
                total += _suggestion_count_from_data(data)
        elif path.is_dir():
            with contextlib.suppress(OSError):
                total += sum(
                    1
                    for child in path.iterdir()
                    if child.is_file()
                    and not child.is_symlink()
                    and child.suffix.lower() in {".json", ".yaml", ".yml", ".md"}
                    and _path_resolves_under(child, cron_dir.parent)
                )
    return total


def _suggestion_count_from_data(data: object) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("suggestions", "items", "jobs"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
        return len(data)
    return 0


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


def _extract_session_id(message: str) -> str:
    match = re.search(r"(?:session(?:_id)?|sid)[=: ]([A-Za-z0-9_-]+)", message, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _delivery_target_label(directory: dict[str, Any], deliver: str) -> str:
    if not deliver:
        return ""
    if deliver in {"local", "origin"}:
        return deliver
    if ":" not in deliver:
        return deliver

    platform, target = deliver.split(":", 1)
    entries = _as_dict(directory.get("platforms")).get(platform)
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("name") or "") == target:
                return f"{platform}:{target}"
    return deliver


def _latest_cron_output_file(
    output_root: Path,
    job_id: str,
    *,
    stop_at: Path | None = None,
) -> Path | None:
    """Locate the newest cron output file for job_id without reading it."""
    if not job_id:
        return None
    job_output_dir = output_root / job_id
    output_root_escaped = stop_at is not None and not _path_resolves_under(output_root, stop_at)
    job_output_dir_escaped = not _path_resolves_under(job_output_dir, output_root)
    if output_root_escaped or job_output_dir_escaped or not job_output_dir.is_dir():
        return None
    files = []
    for path in job_output_dir.iterdir():
        try:
            if not path.is_symlink() and path.is_file():
                files.append(path)
        except OSError:
            continue
    if not files:
        return None
    return max(files, key=_safe_mtime)


def _latest_cron_output_excerpt(
    output_root: Path,
    job_id: str,
    max_bytes: int,
    *,
    stop_at: Path | None = None,
) -> tuple[str, bool, str, float | None]:
    latest = _latest_cron_output_file(output_root, job_id, stop_at=stop_at)
    if latest is None:
        return "", False, "", None
    latest_mtime = _mtime(latest)
    try:
        lines = _read_tail_text(latest, max_bytes).splitlines()
    except OSError:
        return "", False, "", None
    silent = any("[SILENT]" in line.upper() for line in lines)
    for line in lines:
        stripped = line.strip()
        if stripped and "[SILENT]" not in stripped.upper():
            return stripped[:80], silent, latest.name, latest_mtime
    return "", silent, latest.name, latest_mtime


def _tail_latest_cron_output(
    output_root: Path,
    max_lines: int,
    max_bytes: int,
    *,
    stop_at: Path | None = None,
) -> list[LogLine]:
    output_root_escaped = stop_at is not None and not _path_resolves_under(output_root, stop_at)
    if output_root_escaped or not output_root.is_dir():
        return []
    latest_file: Path | None = None
    latest_mtime = 0.0
    for job_dir in output_root.iterdir():
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        for path in job_dir.iterdir():
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_file = path
                latest_mtime = mtime
    if latest_file is None:
        return []
    try:
        lines = _read_tail_text(latest_file, max_bytes).splitlines()[-max_lines:]
    except OSError:
        return []
    return [LogLine(message=_redact_secret_text(line.strip())) for line in lines if line.strip()]


def _path_resolves_under(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)


def _read_kanban_state(db_path: Path, base_state: KanbanState) -> KanbanState:
    with _connect_readonly_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        status_counts = _count_by(conn, "SELECT status, COUNT(*) FROM tasks GROUP BY status")
        assignee_counts = _count_by(
            conn,
            "SELECT COALESCE(NULLIF(assignee, ''), 'unassigned'), COUNT(*) "
            "FROM tasks GROUP BY COALESCE(NULLIF(assignee, ''), 'unassigned')",
        )
        active_rows = _query_rows(
            conn,
            "SELECT * FROM tasks "
            "WHERE status IN ('in_progress', 'running', 'claimed') "
            "OR current_run_id IS NOT NULL OR worker_pid IS NOT NULL "
            "ORDER BY COALESCE(last_heartbeat_at, started_at, created_at, 0) DESC LIMIT 10",
        )
        problem_rows = _query_rows(
            conn,
            "SELECT * FROM tasks "
            "WHERE status IN ('blocked', 'failed', 'error') "
            "OR consecutive_failures > 0 OR COALESCE(last_failure_error, '') != '' "
            "ORDER BY COALESCE(last_heartbeat_at, started_at, created_at, 0) DESC LIMIT 10",
        )
        run_rows = _query_rows(
            conn,
            "SELECT * FROM task_runs ORDER BY started_at DESC, id DESC LIMIT 10",
        )
        recent_task_rows = _read_recent_enriched_tasks(conn)
        return base_state.model_copy(
            update={
                "db_present": True,
                "task_count": _table_count(conn, "tasks"),
                "run_count": _table_count(conn, "task_runs"),
                "event_count": _table_count(conn, "task_events"),
                "comment_count": _table_count(conn, "task_comments"),
                "link_count": _table_count_or_zero(conn, "task_links"),
                "attachment_count": _table_count_or_zero(conn, "task_attachments"),
                "stale_claim_count": _stale_claim_count_from_tasks(
                    conn,
                    base_state.claim_ttl_seconds,
                ),
                "status_counts": status_counts,
                "assignee_counts": assignee_counts,
                "active_tasks": [_kanban_task_from_row(row) for row in active_rows],
                "problem_tasks": [_kanban_task_from_row(row) for row in problem_rows],
                "recent_tasks": [_kanban_task_from_row(row) for row in recent_task_rows],
                "task_links": _read_task_links(conn),
                "recent_runs": [_kanban_run_from_row(row) for row in run_rows],
            }
        )


def _read_kanban_board_summary(
    db_path: Path,
    *,
    slug: str,
    current: bool,
    claim_ttl_seconds: int,
) -> KanbanBoardSummary:
    with _connect_readonly_sqlite(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return KanbanBoardSummary(
            slug=slug,
            current=current,
            task_count=_table_count(conn, "tasks"),
            run_count=_table_count_or_zero(conn, "task_runs"),
            problem_count=_count_rows_or_zero(
                conn,
                "SELECT COUNT(*) FROM tasks WHERE status IN ('blocked', 'failed', 'error')",
            ),
            stale_claim_count=_stale_claim_count_from_tasks(conn, claim_ttl_seconds),
            block_kind_counts=(
                _count_by(
                    conn,
                    "SELECT block_kind, COUNT(*) FROM tasks "
                    "WHERE COALESCE(block_kind, '') != '' GROUP BY block_kind",
                )
                if _column_exists(conn, "tasks", "block_kind")
                else {}
            ),
        )


def _read_verification_evidence(
    conn: sqlite3.Connection,
    operations: OperationsState,
) -> OperationsState:
    return operations.model_copy(
        update={
            "verification_db_present": True,
            "verification_event_count": _table_count(conn, "verification_events"),
            "verification_failed_count": _verification_failed_count(conn),
            "verification_state_count": _table_count_or_zero(conn, "verification_state"),
            "verification_latest_events": _read_verification_events(conn),
            "verification_roots": _read_verification_roots(conn),
        }
    )


def _read_goal_state(conn: sqlite3.Connection, operations: OperationsState) -> OperationsState:
    goals = _read_goal_summaries(conn)
    return operations.model_copy(
        update={
            "goal_count": len(goals),
            "active_goal_count": sum(1 for goal in goals if goal.status == "active"),
            "waiting_goal_count": sum(
                1 for goal in goals if goal.waiting_on_pid or goal.waiting_on_session
            ),
            "goals": goals,
        }
    )


def _read_goal_summaries(conn: sqlite3.Connection) -> list[GoalSummary]:
    if not _table_exists(conn, "state_meta"):
        return []
    rows = _query_rows(
        conn,
        "SELECT key, value FROM state_meta WHERE key LIKE 'goal:%' ORDER BY key LIMIT 8",
    )
    goals: list[GoalSummary] = []
    for row in rows:
        raw_value = str(row.get("value") or "")
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(raw_value)
            if isinstance(data, dict):
                goals.append(_goal_summary_from_row(str(row.get("key") or ""), data))
    return goals


def _goal_summary_from_row(key: str, data: dict[str, Any]) -> GoalSummary:
    contract = _as_dict(data.get("contract"))
    return GoalSummary(
        session_id=key.removeprefix("goal:"),
        goal=str(data.get("goal") or ""),
        status=str(data.get("status") or ""),
        turns_used=_coerce_int(data.get("turns_used")),
        max_turns=_coerce_int(data.get("max_turns")),
        has_contract=any(value not in (None, "", [], {}) for value in contract.values()),
        waiting_on_pid=_coerce_int(data.get("waiting_on_pid")),
        waiting_on_session=str(data.get("waiting_on_session") or ""),
        waiting_reason=str(data.get("waiting_reason") or ""),
        subgoal_count=len(_as_list(data.get("subgoals"))),
    )


def _read_projects_state(
    conn: sqlite3.Connection,
    operations: OperationsState,
    paths: HermesPaths,
) -> OperationsState:
    return operations.model_copy(
        update={
            "projects_db_present": True,
            "project_count": _table_count(conn, "projects"),
            "project_archived_count": _count_rows_or_zero(
                conn,
                "SELECT COUNT(*) FROM projects WHERE archived != 0",
            ),
            "project_folder_count": _table_count_or_zero(conn, "project_folders"),
            "discovered_repo_count": _table_count_or_zero(conn, "discovered_repos"),
            "project_missing_primary_path_count": _count_rows_or_zero(
                conn,
                "SELECT COUNT(*) FROM projects WHERE COALESCE(primary_path, '') = ''",
            ),
            "projects": _read_project_summaries(conn, operations.verification_roots, paths),
            "discovered_repos": _read_discovered_repos(conn),
        }
    )


def _curator_with_scheduler_state(
    run: CuratorRun,
    state: dict[str, Any],
    curator_cfg: dict[str, Any],
) -> CuratorRun:
    if not state and not curator_cfg:
        return run
    return run.model_copy(
        update={
            "scheduler_state_present": bool(state),
            "scheduler_paused": bool(state.get("paused")),
            "scheduler_run_count": _coerce_int(state.get("run_count")),
            "scheduler_last_run_at": str(state.get("last_run_at") or ""),
            "scheduler_last_report_path": str(state.get("last_report_path") or ""),
            "consolidate_enabled": bool(curator_cfg.get("consolidate")),
        }
    )


def _read_project_summaries(
    conn: sqlite3.Connection,
    verification_roots: list[VerificationRootSummary],
    paths: HermesPaths,
) -> list[ProjectSummary]:
    with contextlib.suppress(sqlite3.Error):
        rows = _query_rows(
            conn,
            "SELECT slug, name, board_slug, primary_path, archived "
            "FROM projects ORDER BY archived ASC, created_at DESC, slug ASC LIMIT 8",
        )
        return [
            ProjectSummary(
                slug=str(row.get("slug") or ""),
                name=str(row.get("name") or ""),
                board_slug=str(row.get("board_slug") or ""),
                primary_path=str(row.get("primary_path") or ""),
                archived=bool(row.get("archived")),
                verification_root_count=_project_verification_root_count(
                    str(row.get("primary_path") or ""),
                    verification_roots,
                ),
                kanban_board_present=_kanban_board_present(
                    paths,
                    str(row.get("board_slug") or ""),
                ),
            )
            for row in rows
        ]
    return []


def _read_discovered_repos(conn: sqlite3.Connection) -> list[DiscoveredRepoSummary]:
    if not _table_exists(conn, "discovered_repos"):
        return []
    with contextlib.suppress(sqlite3.Error):
        rows = _query_rows(
            conn,
            "SELECT root, label, last_seen FROM discovered_repos "
            "ORDER BY COALESCE(last_seen, '') DESC, root ASC LIMIT 5",
        )
        return [
            DiscoveredRepoSummary(
                root=str(row.get("root") or ""),
                label=str(row.get("label") or ""),
                last_seen=str(row.get("last_seen") or ""),
            )
            for row in rows
        ]
    return []


def _project_verification_root_count(
    primary_path: str,
    verification_roots: list[VerificationRootSummary],
) -> int:
    if not primary_path:
        return 0
    return sum(
        1 for root in verification_roots if _same_path_or_descendant(root.root, primary_path)
    )


def _kanban_board_present(paths: HermesPaths, board_slug: str) -> bool:
    if not board_slug:
        return False
    if board_slug in {"root", "default"}:
        path = paths.shared_path("kanban.db")
        return (
            path.exists() and not path.is_symlink() and _path_resolves_under(path, paths.root_home)
        )
    path = paths.shared_path("kanban", "boards", board_slug, "kanban.db")
    return path.exists() and not path.is_symlink() and _path_resolves_under(path, paths.root_home)


def _same_path_or_descendant(candidate: str, parent: str) -> bool:
    candidate_path = candidate.rstrip(os.sep)
    parent_path = parent.rstrip(os.sep)
    if not candidate_path or not parent_path:
        return False
    return candidate_path == parent_path or candidate_path.startswith(parent_path + os.sep)


def _read_verification_events(conn: sqlite3.Connection) -> list[VerificationEventSummary]:
    rows = _query_rows(
        conn,
        "SELECT id, created_at, session_id, root, command, canonical_command, "
        "kind, scope, status, exit_code, output_summary "
        "FROM verification_events ORDER BY id DESC LIMIT 8",
    )
    return [
        VerificationEventSummary(
            event_id=_coerce_int(row.get("id")),
            created_at=str(row.get("created_at") or ""),
            session_id=str(row.get("session_id") or ""),
            root=str(row.get("root") or ""),
            command=str(row.get("command") or ""),
            canonical_command=str(row.get("canonical_command") or ""),
            kind=str(row.get("kind") or ""),
            scope=str(row.get("scope") or ""),
            status=str(row.get("status") or ""),
            exit_code=_coerce_int(row.get("exit_code")),
            output_summary=str(row.get("output_summary") or ""),
        )
        for row in rows
    ]


def _read_verification_roots(conn: sqlite3.Connection) -> list[VerificationRootSummary]:
    if not _table_exists(conn, "verification_state"):
        return []
    rows = _query_rows(
        conn,
        "SELECT session_id, root, last_event_id, last_edit_at, changed_paths_json "
        "FROM verification_state "
        "ORDER BY COALESCE(last_edit_at, '') DESC, COALESCE(last_event_id, 0) DESC "
        "LIMIT 8",
    )
    return [
        VerificationRootSummary(
            session_id=str(row.get("session_id") or ""),
            root=str(row.get("root") or ""),
            last_event_id=_coerce_int(row.get("last_event_id")),
            last_edit_at=str(row.get("last_edit_at") or ""),
            changed_path_count=_json_list_count(row.get("changed_paths_json")),
        )
        for row in rows
    ]


def _verification_failed_count(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM verification_events WHERE status != 'passed'")
    row = cur.fetchone()
    return int(row[0] or 0) if row is not None else 0


def _count_rows_or_zero(conn: sqlite3.Connection, sql: str) -> int:
    with contextlib.suppress(sqlite3.Error):
        cur = conn.execute(sql)
        row = cur.fetchone()
        return int(row[0] or 0) if row is not None else 0
    return 0


def _json_list_count(value: object) -> int:
    if not isinstance(value, str) or not value:
        return 0
    with contextlib.suppress(json.JSONDecodeError):
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return len(decoded)
    return 0


def _context_limit_for(context_lengths: Mapping[str, int], model: str, base_url: str) -> int:
    normalized_base = base_url.rstrip("/")
    exact = context_lengths.get(f"{model}@{normalized_base}")
    if exact is not None:
        return exact

    origin = _url_origin(normalized_base)
    if not origin:
        return 0
    for key, value in sorted(context_lengths.items()):
        cached_model, sep, cached_base = key.partition("@")
        if sep and cached_model == model and _url_origin(cached_base) == origin:
            return value
    return 0


def _url_origin(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _safe_child_path(path: Path, root: Path) -> bool:
    return not path.is_symlink() and _path_resolves_under(path, root)


def _safe_or_absent_child_path(path: Path, root: Path) -> bool:
    if path.is_symlink():
        return False
    return not path.exists() or _path_resolves_under(path, root)


@contextlib.contextmanager
def _connect_readonly_sqlite(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn: sqlite3.Connection | None = None
    if db_path.with_name(f"{db_path.name}-wal").exists():
        snapshot_dir, snapshot_db = snapshot_wal_database(db_path, prefix="hermesd-kanban-")
        try:
            conn = sqlite3.connect(f"{snapshot_db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
            yield conn
            return
        finally:
            if conn is not None:
                conn.close()
            snapshot_dir.cleanup()
    conn = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=2,
    )
    try:
        yield conn
    finally:
        conn.close()


def _query_rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    cur = conn.execute(sql)
    return [dict(row) for row in cur.fetchall()]


def _table_count(conn: sqlite3.Connection, table_name: str) -> int:
    cur = conn.execute(f"SELECT COUNT(*) FROM {table_name}")
    row = cur.fetchone()
    return int(row[0]) if row is not None else 0


def _table_count_or_zero(conn: sqlite3.Connection, table_name: str) -> int:
    with contextlib.suppress(sqlite3.Error):
        return _table_count(conn, table_name)
    return 0


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    )
    return cur.fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    with contextlib.suppress(sqlite3.Error):
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(str(row[1] or "") == column_name for row in rows)
    return False


def _stale_claim_count_from_tasks(conn: sqlite3.Connection, claim_ttl_seconds: int) -> int:
    if not _table_exists(conn, "tasks"):
        return 0
    now = int(time.time())
    ttl = claim_ttl_seconds if claim_ttl_seconds > 0 else 300
    conditions = []
    if _column_exists(conn, "tasks", "claim_expires"):
        conditions.append(f"COALESCE(claim_expires, 0) > 0 AND claim_expires < {now}")
    if _column_exists(conn, "tasks", "last_heartbeat_at"):
        conditions.append(f"COALESCE(last_heartbeat_at, 0) > 0 AND last_heartbeat_at < {now - ttl}")
    if not conditions:
        return 0
    return _count_rows_or_zero(
        conn,
        "SELECT COUNT(*) FROM tasks WHERE "
        + " OR ".join(f"({condition})" for condition in conditions),
    )


def _read_task_links(conn: sqlite3.Connection) -> list[KanbanTaskLink]:
    with contextlib.suppress(sqlite3.Error):
        rows = _query_rows(
            conn,
            "SELECT parent_id, child_id FROM task_links "
            "ORDER BY COALESCE(parent_id, ''), COALESCE(child_id, '') LIMIT 20",
        )
        return [
            KanbanTaskLink(
                parent_id=str(row.get("parent_id") or ""),
                child_id=str(row.get("child_id") or ""),
            )
            for row in rows
        ]
    return []


def _read_recent_enriched_tasks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    with contextlib.suppress(sqlite3.Error):
        return _query_rows(
            conn,
            "SELECT * FROM tasks "
            "WHERE completed_at IS NOT NULL OR COALESCE(workspace_path, '') != '' "
            "OR COALESCE(goal_mode, '') != '' OR COALESCE(current_step_key, '') != '' "
            "OR COALESCE(branch_name, '') != '' "
            "ORDER BY COALESCE(completed_at, last_heartbeat_at, started_at, created_at, 0) "
            "DESC LIMIT 10",
        )
    return []


def _count_by(conn: sqlite3.Connection, sql: str) -> dict[str, int]:
    cur = conn.execute(sql)
    return {str(row[0] or "unknown"): int(row[1] or 0) for row in cur.fetchall()}


def _kanban_task_from_row(row: dict[str, Any]) -> KanbanTaskSummary:
    return KanbanTaskSummary(
        task_id=str(row.get("id") or ""),
        title=str(row.get("title") or ""),
        assignee=str(row.get("assignee") or ""),
        status=str(row.get("status") or ""),
        priority=_coerce_int(row.get("priority")),
        consecutive_failures=_coerce_int(row.get("consecutive_failures")),
        worker_pid=_coerce_int(row.get("worker_pid")),
        session_id=str(row.get("session_id") or ""),
        last_failure_error=str(row.get("last_failure_error") or ""),
        last_heartbeat_at=_coerce_int(row.get("last_heartbeat_at")),
        claim_expires=_coerce_int(row.get("claim_expires")),
        current_run_id=_coerce_int(row.get("current_run_id")),
        model_override=str(row.get("model_override") or ""),
        branch_name=str(row.get("branch_name") or ""),
        skills=str(row.get("skills") or ""),
        completed_at=_coerce_int(row.get("completed_at")),
        workspace_path=str(row.get("workspace_path") or ""),
        goal_mode=str(row.get("goal_mode") or ""),
        current_step_key=str(row.get("current_step_key") or ""),
    )


def _kanban_run_from_row(row: dict[str, Any]) -> KanbanRunSummary:
    return KanbanRunSummary(
        run_id=_coerce_int(row.get("id")),
        task_id=str(row.get("task_id") or ""),
        profile=str(row.get("profile") or ""),
        status=str(row.get("status") or ""),
        outcome=str(row.get("outcome") or ""),
        worker_pid=_coerce_int(row.get("worker_pid")),
        started_at=_coerce_int(row.get("started_at")),
        ended_at=_coerce_int(row.get("ended_at")),
        error=str(row.get("error") or ""),
        summary=str(row.get("summary") or ""),
    )


def _model_cache_counts(data: dict[str, Any]) -> tuple[int, int]:
    if not data:
        return 0, 0
    model_count = 0
    for provider_data in data.values():
        provider = _as_dict(provider_data)
        models = provider.get("models")
        if isinstance(models, dict | list):
            model_count += len(models)
    return len(data), model_count


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


def _is_dashboard_process(command: str) -> bool:
    if "hermes dashboard" in command:
        return True
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return any(Path(part).name == "hermesd" for part in parts)


def _len_if_sized(value: object) -> int:
    if isinstance(value, dict | list | tuple | set):
        return len(value)
    return 0


def _int_mapping(value: object) -> dict[str, int]:
    raw = _as_dict(value)
    return {str(key): _coerce_int(count) for key, count in raw.items() if str(key)}


def _state_transition_label(entry: dict[str, Any]) -> str:
    from_state = str(entry.get("from") or entry.get("from_state") or "")
    to_state = str(entry.get("to") or entry.get("to_state") or "")
    at = str(entry.get("at") or entry.get("timestamp") or entry.get("created_at") or "")
    if from_state or to_state:
        label = f"{from_state or 'unknown'} -> {to_state or 'unknown'}"
    else:
        label = str(entry.get("state") or "")
    return f"{label} @ {at}" if at and label else label


def _git_checkpoint_summary(repo_dir: Path) -> tuple[int, float | None, str]:
    commit_count = 0
    try:
        count_result = subprocess.run(
            ["git", "--git-dir", str(repo_dir), "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return 0, None, ""

    if count_result.returncode == 0:
        commit_count = _coerce_int(count_result.stdout.strip())
    if commit_count <= 0:
        return 0, None, ""

    try:
        log_result = subprocess.run(
            ["git", "--git-dir", str(repo_dir), "log", "-1", "--format=%ct%x09%s", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        # A non-UTF-8 commit subject must not fail the whole checkpoints source.
        return commit_count, None, ""

    if log_result.returncode != 0:
        return commit_count, None, ""

    raw = log_result.stdout.strip()
    if "\t" not in raw:
        return commit_count, None, raw

    ts_text, reason = raw.split("\t", 1)
    timestamp = _coerce_float(ts_text)
    return commit_count, (timestamp or None), reason


_SECRET_FIELD_NAMES = {
    "api_key",
    "id_token",
    "access_token",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "pass",
    "passwd",
    "pin",
    "pwd",
    "refresh_token",
    "secret",
    "token",
    "x_api_key",
    "x-api-key",
    "user_token",
}

_OAUTH_FIELD_NAMES = {"id_token", "access_token", "refresh_token"}
_API_KEY_FIELD_NAMES = {"api_key", "secret", "token", "user_token"}
_SECRET_URL_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "auth_token",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "id_token",
    "key",
    "pass",
    "passwd",
    "password",
    "pin",
    "pwd",
    "refresh_token",
    "secret",
    "token",
    "x_api_key",
    "x-api-key",
    "user_token",
}
_SECRET_OPTION_NAMES = {
    "access-token",
    "api-key",
    "apikey",
    "auth",
    "auth-token",
    "authorization",
    "bearer",
    "client-secret",
    "credential",
    "h",
    "header",
    "id-token",
    "k",
    "key",
    "p",
    "pass",
    "passwd",
    "password",
    "pin",
    "pwd",
    "refresh-token",
    "secret",
    "t",
    "token",
    "x-api-key",
    "user-token",
}


def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return []


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


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value or "0")
        except ValueError:
            return 0
    if isinstance(value, (bytes, bytearray)):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _coerce_float(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        result = float(value)
        return result if math.isfinite(result) else 0.0
    if isinstance(value, str):
        try:
            result = float(value or "0")
        except ValueError:
            return 0.0
        return result if math.isfinite(result) else 0.0
    return 0.0


def _normalize_secret_option_name(option: str) -> str:
    return option.lstrip("-").lower().replace("_", "-")


def _secret_key_name(value: object) -> str:
    return str(value).strip().lower().replace("_", "-")


def _redact_secret_url(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    netloc = parts.netloc
    if parts.username or parts.password:
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        netloc = f"[REDACTED]@{host}"
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not query_pairs:
        return urlunsplit(parts._replace(netloc=netloc))
    redacted_query = "&".join(
        f"{key}={'[REDACTED]' if key.lower() in _SECRET_URL_QUERY_KEYS else item_value}"
        for key, item_value in query_pairs
    )
    return urlunsplit(parts._replace(netloc=netloc, query=redacted_query))


def _redact_secret_args(args: object) -> list[str]:
    if not isinstance(args, list):
        return []
    redacted: list[str] = []
    redact_next = False
    for raw_arg in args:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if isinstance(raw_arg, list):
            redacted.extend(_redact_secret_args(raw_arg))
            continue
        if isinstance(raw_arg, dict) and _has_secret_material(raw_arg):
            redacted.append("[REDACTED]")
            continue
        arg = _redact_secret_url(str(raw_arg))
        if "=" in arg:
            option, _value = arg.split("=", 1)
            if _normalize_secret_option_name(option) in _SECRET_OPTION_NAMES:
                redacted.append(f"{option}=[REDACTED]")
                continue
            if option.startswith(("http://", "https://")):
                redacted.append(_redact_secret_url(arg))
                continue
        if arg.startswith("-") and _normalize_secret_option_name(arg) in _SECRET_OPTION_NAMES:
            redacted.append(arg)
            redact_next = True
            continue
        redacted.append(arg)
    return redacted


def _has_secret_material(data: dict[str, Any]) -> bool:
    for key, value in data.items():
        key_name = _secret_key_name(key)
        if (
            key_name in _SECRET_FIELD_NAMES
            or key_name in _SECRET_OPTION_NAMES
            or key_name in _SECRET_URL_QUERY_KEYS
        ) and value not in (None, ""):
            return True
        if isinstance(value, str) and _looks_like_secret_value(value):
            return True
        if isinstance(value, dict) and _has_secret_material(value):
            return True
        if isinstance(value, list) and any(_contains_secret_material(item) for item in value):
            return True
    return False


def _contains_secret_material(value: object) -> bool:
    if isinstance(value, dict):
        return _has_secret_material(value)
    if isinstance(value, list):
        return any(_contains_secret_material(item) for item in value)
    return isinstance(value, str) and _looks_like_secret_value(value)


def _looks_like_secret_value(value: str) -> bool:
    lowered = value.lower()
    return "bearer " in lowered or "authorization:" in lowered or "x-api-key" in lowered


def _redact_secret_text(value: str) -> str:
    redacted = re.sub(r"https?://[^,\s]+", lambda match: _redact_secret_url(match.group(0)), value)
    redacted = re.sub(r"(?i)(bearer)\s+[^,\s]+", r"\1 [REDACTED]", redacted)
    return re.sub(
        r"(?i)(access[-_]?token|api[-_]?key|authorization|client[-_]?secret|credential|"
        r"pass(?:word|wd)?|pwd|pin|refresh[-_]?token|secret|token|x[-_]?api[-_]?key)"
        r"([=:]\s*)[^,\s]+",
        r"\1\2[REDACTED]",
        redacted,
    )


def _safe_exception_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {_redact_secret_text(str(exc))[:200]}"


def _redact_command_string(command: str) -> str:
    if not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        return _redact_secret_text(command)
    return " ".join(_redact_secret_args(parts))


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


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
