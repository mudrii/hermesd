from __future__ import annotations

import json
import os
import re
import subprocess
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import yaml

from hermesd.db import HermesDB
from hermesd.file_cache import LastGoodFileCache
from hermesd.models import (
    BackgroundProcessInfo,
    CheckpointInfo,
    ConfigSummary,
    CredentialPoolEntry,
    CronJob,
    CronState,
    DashboardState,
    GatewayState,
    HealthSummary,
    HookInfo,
    LogLine,
    LogState,
    MCPServerInfo,
    MemoryOverview,
    PlatformStatus,
    PluginInfo,
    ProfilesState,
    ProfileSummary,
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
)
from hermesd.paths import HermesPaths
from hermesd.theme import normalize_skin_name


class Collector:
    def __init__(
        self,
        hermes_home: Path,
        pid_exists: Callable[[int], bool] | None = None,
        profile_name: str | None = None,
        log_tail_bytes: int = 32768,
    ):
        self._root_home = hermes_home
        self._file_cache = LastGoodFileCache()
        self._log_cache: dict[str, list[LogLine]] = {}
        self._pid_exists = pid_exists or _pid_exists
        self._log_tail_bytes = max(1024, log_tail_bytes)
        self._paths = HermesPaths(hermes_home, profile_name)
        self._db = HermesDB(self._paths.profile_path("state.db"))
        self._available_tools_cache_mtime: float | None = None
        self._available_tools_cache_value: tuple[int, list[str]] = (0, [])
        self._last_state: DashboardState | None = None

    def collect(self) -> DashboardState:
        failed_sources: list[str] = []
        total_sources = 0

        def safe_collect(
            fallback: Callable[[], Any],
            source_name: str,
            fn: Callable[[], Any],
            default_factory: Callable[[], Any],
        ) -> Any:
            nonlocal total_sources
            total_sources += 1
            try:
                return fn()
            except Exception:
                failed_sources.append(source_name)
                try:
                    return fallback()
                except Exception:
                    return default_factory()

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
        sessions = safe_collect(
            lambda: self._last_state.sessions if self._last_state is not None else [],
            "sessions",
            self._collect_sessions,
            list,
        )
        state = DashboardState(
            hermes_home=self._paths.root_home,
            selected_profile=self._paths.profile_name,
            profile_mode_label=self._paths.profile_mode_label,
            collected_at=time.time(),
            gateway=safe_collect(
                lambda: (
                    self._last_state.gateway if self._last_state is not None else GatewayState()
                ),
                "gateway",
                self._collect_gateway,
                GatewayState,
            ),
            sessions=sessions,
            tokens_today=safe_collect(
                lambda: (
                    self._last_state.tokens_today
                    if self._last_state is not None
                    else TokenSummary()
                ),
                "tokens_today",
                self._collect_tokens_today,
                TokenSummary,
            ),
            tokens_total=safe_collect(
                lambda: (
                    self._last_state.tokens_total
                    if self._last_state is not None
                    else TokenSummary()
                ),
                "tokens_total",
                self._collect_tokens_total,
                TokenSummary,
            ),
            token_analytics=safe_collect(
                lambda: (
                    self._last_state.token_analytics
                    if self._last_state is not None
                    else TokenAnalytics()
                ),
                "token_analytics",
                self._collect_token_analytics,
                TokenAnalytics,
            ),
            tool_stats=safe_collect(
                lambda: self._last_state.tool_stats if self._last_state is not None else [],
                "tool_stats",
                self._collect_tool_stats,
                list,
            ),
            total_tool_calls=safe_collect(
                lambda: self._last_state.total_tool_calls if self._last_state is not None else 0,
                "tool_call_total",
                self._collect_total_tool_calls,
                int,
            ),
            available_tools=tool_count,
            available_tool_names=tool_names,
            background_processes=safe_collect(
                lambda: (
                    self._last_state.background_processes if self._last_state is not None else []
                ),
                "background_processes",
                self._collect_background_processes,
                list,
            ),
            checkpoints=safe_collect(
                lambda: self._last_state.checkpoints if self._last_state is not None else [],
                "checkpoints",
                self._collect_checkpoints,
                list,
            ),
            config=safe_collect(
                lambda: (
                    self._last_state.config if self._last_state is not None else ConfigSummary()
                ),
                "config",
                self._collect_config,
                ConfigSummary,
            ),
            cron=safe_collect(
                lambda: self._last_state.cron if self._last_state is not None else CronState(),
                "cron",
                self._collect_cron,
                CronState,
            ),
            skills_memory=safe_collect(
                lambda: (
                    self._last_state.skills_memory
                    if self._last_state is not None
                    else SkillsMemory()
                ),
                "skills",
                self._collect_skills_memory,
                SkillsMemory,
            ),
            memory=safe_collect(
                lambda: (
                    self._last_state.memory if self._last_state is not None else MemoryOverview()
                ),
                "memory",
                self._collect_memory,
                MemoryOverview,
            ),
            profiles=safe_collect(
                lambda: (
                    self._last_state.profiles if self._last_state is not None else ProfilesState()
                ),
                "profiles",
                self._collect_profiles,
                ProfilesState,
            ),
            logs=safe_collect(
                lambda: self._last_state.logs if self._last_state is not None else LogState(),
                "logs",
                self._collect_logs,
                LogState,
            ),
            version_behind=safe_collect(
                lambda: self._last_state.version_behind if self._last_state is not None else 0,
                "version_check",
                self._collect_version_behind,
                int,
            ),
            active_skin=safe_collect(
                lambda: self._last_state.active_skin if self._last_state is not None else "default",
                "skin",
                self._collect_skin,
                str,
            ),
        )
        state.health = HealthSummary(
            total_sources=total_sources,
            ok_sources=total_sources - len(failed_sources),
            failed_sources=sorted(failed_sources),
        )
        state.runtime = self._collect_runtime_status(state.gateway, state.sessions)
        self._last_state = state
        return state

    def _read_json_cached(self, path: Path) -> dict[str, Any]:
        return self._file_cache.read_json_mapping(path)

    def _read_json_list_cached(self, path: Path) -> list[dict[str, Any]]:
        return self._file_cache.read_json_list(path)

    def _read_yaml_cached(self) -> dict[str, Any]:
        return self._file_cache.read_yaml_mapping(self._paths.shared_path("config.yaml"))

    def search_session_ids_by_message(self, query: str) -> set[str]:
        return self._db.search_session_ids_by_message(query)

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
                    state=info.get("state", "unknown"),
                    updated_at=info.get("updated_at", ""),
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
        return GatewayState(
            pid=pid,
            running=running,
            state=data.get("gateway_state", "unknown"),
            platforms=platforms,
            hermes_version=version,
            updates_behind=behind,
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

    def _collect_sessions(self) -> list[SessionInfo]:
        rows = self._db.read_sessions()
        return [
            SessionInfo(
                session_id=r["id"],
                source=r.get("source") or "",
                model=r.get("model") or "",
                parent_session_id=r.get("parent_session_id") or "",
                billing_provider=r.get("billing_provider") or "",
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
                started_at=r.get("started_at") or 0.0,
                ended_at=r.get("ended_at"),
                title=r.get("title"),
                is_active=r.get("ended_at") is None,
            )
            for r in rows
        ]

    def _collect_tokens_today(self) -> TokenSummary:
        return _summarize_tokens(
            self._db.read_sessions(),
            started_at_min=_today_epoch(),
        )

    def _collect_tokens_total(self) -> TokenSummary:
        return _summarize_tokens(self._db.read_sessions())

    def _collect_token_analytics(self) -> TokenAnalytics:
        rows = self._db.read_sessions()
        return TokenAnalytics(
            windows=[
                _summarize_window("7d", rows, days=7),
                _summarize_window("30d", rows, days=30),
            ],
            by_model=_summarize_breakdown(rows, key_name="model"),
            by_provider=_summarize_breakdown(rows, key_name="billing_provider"),
        )

    def _collect_tool_stats(self) -> list[ToolStats]:
        # Try messages table first (has per-tool breakdown)
        rows = self._db.read_tool_stats()
        if rows:
            return [ToolStats(name=r["tool_name"], call_count=r["call_count"]) for r in rows]
        # Fall back to per-session tool_call_count
        sessions = self._db.read_sessions()
        stats = []
        for s in sessions:
            tc = s.get("tool_call_count") or 0
            if tc > 0:
                sid = s.get("id", "?")
                src = s.get("source") or "?"
                label = f"{src}:{sid[-6:]}"
                stats.append(ToolStats(name=label, call_count=tc))
        return sorted(stats, key=lambda t: t.call_count, reverse=True)

    def _collect_total_tool_calls(self) -> int:
        return sum(r.get("tool_call_count") or 0 for r in self._db.read_sessions())

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
                watcher_interval=_coerce_int(entry.get("watcher_interval")),
                watch_patterns=[str(item) for item in entry.get("watch_patterns") or []],
            )
            for entry in entries
            if str(entry.get("session_id") or "")
        ]

    def _collect_available_tools(self) -> tuple[int, list[str]]:
        sessions_index = self._paths.profile_path("sessions", "sessions.json")
        sessions_mtime = _mtime(sessions_index)
        if sessions_mtime is not None and self._available_tools_cache_mtime == sessions_mtime:
            return self._available_tools_cache_value
        sessions_data = self._read_json_cached(sessions_index)
        if not sessions_data:
            self._available_tools_cache_mtime = sessions_mtime
            self._available_tools_cache_value = (0, [])
            return 0, []
        names: set[str] = set()
        for entry in sessions_data.values():
            if isinstance(entry, dict) and "session_id" in entry:
                sid = entry["session_id"]
                session_file = self._paths.profile_path("sessions", f"session_{sid}.json")
                data = self._read_json_cached(session_file)
                if isinstance(data, dict) and "tools" in data:
                    for t in data["tools"]:
                        if isinstance(t, dict):
                            name = t.get("function", {}).get("name") or t.get("name", "")
                        else:
                            name = str(t)
                        if name:
                            names.add(name)
        tool_names = sorted(names)
        self._available_tools_cache_mtime = sessions_mtime
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
        personality = agent_cfg.get("active_personality", "")
        if not personality:
            personalities = _as_dict(agent_cfg.get("personalities"))
            if personalities:
                personality = next(iter(personalities))
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
            tool_gateway_domain=os.environ.get("TOOL_GATEWAY_DOMAIN", ""),
            tool_gateway_scheme=os.environ.get("TOOL_GATEWAY_SCHEME", ""),
            firecrawl_gateway_url=_redact_secret_url(os.environ.get("FIRECRAWL_GATEWAY_URL", "")),
            tool_gateway_routes=self._collect_tool_gateway_routes(cfg),
        )

    def _collect_tool_gateway_routes(self, cfg: dict[str, Any]) -> list[ToolGatewayRoute]:
        token_present = bool(os.environ.get("TOOL_GATEWAY_USER_TOKEN"))
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
        tick_path = self._paths.shared_path("cron", ".tick.lock")
        last_tick: float | None = None
        if tick_path.exists():
            try:
                mtime = tick_path.stat().st_mtime
                last_tick = time.time() - mtime
            except OSError:
                pass

        jobs: list[CronJob] = []
        error_count = 0
        data = self._read_json_cached(self._paths.shared_path("cron", "jobs.json"))
        if data:
            directory = self._read_json_cached(self._paths.shared_path("channel_directory.json"))
            for j in data.get("jobs", []):
                if not isinstance(j, dict):
                    continue
                state = j.get("state", "")
                if j.get("last_status") == "error" or j.get("last_error"):
                    error_count += 1
                output_excerpt, silent_run = _latest_cron_output_excerpt(
                    self._paths.shared_path("cron", "output"),
                    str(j.get("id") or ""),
                )
                jobs.append(
                    CronJob(
                        job_id=j.get("id", ""),
                        name=j.get("name", ""),
                        schedule_display=j.get("schedule_display", ""),
                        state=state,
                        enabled=j.get("enabled", True),
                        deliver=str(j.get("deliver") or ""),
                        delivery_target_label=_delivery_target_label(
                            directory,
                            str(j.get("deliver") or ""),
                        ),
                        latest_output_excerpt=output_excerpt,
                        silent_run=silent_run,
                        next_run_at=j.get("next_run_at", ""),
                        last_status=j.get("last_status"),
                    )
                )

        return CronState(
            last_tick_ago_seconds=last_tick,
            job_count=len(jobs),
            error_count=error_count,
            jobs=jobs,
        )

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

        return MemoryOverview(
            provider=str(memory_cfg.get("provider") or ""),
            memory_file_count=len(memory_files),
            memory_word_count=_word_count(memories_dir / "MEMORY.md"),
            user_word_count=_word_count(memories_dir / "USER.md"),
            soul_size_bytes=_file_size(soul_path),
            soul_excerpt=_read_soul_excerpt(soul_path),
            memory_files=memory_files,
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

    def _collect_plugins(self, cfg: dict[str, Any] | None = None) -> list[PluginInfo]:
        if cfg is None:
            cfg = self._read_yaml_cached()
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

    def _collect_mcp_servers(self, cfg: dict[str, Any] | None = None) -> list[MCPServerInfo]:
        if cfg is None:
            cfg = self._read_yaml_cached()
        servers = _as_dict(cfg.get("mcp_servers"))
        result: list[MCPServerInfo] = []
        for name, raw_server in sorted(servers.items()):
            server = _as_dict(raw_server)
            if not server:
                continue
            args = server.get("args") or []
            command = str(server.get("command") or "")
            url = _redact_secret_url(str(server.get("url") or ""))
            target = url
            transport = "url" if url else ""
            if not target and command:
                rendered_args = (
                    " ".join(_redact_secret_args(args)) if isinstance(args, list) else ""
                )
                target = " ".join(part for part in [command, rendered_args] if part)
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
            if not repo_dir.is_dir():
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

    def _collect_providers(self, data: dict[str, Any] | None = None) -> list[ProviderInfo]:
        if data is None:
            data = self._read_json_cached(self._paths.shared_path("auth.json"))
        if not data:
            return []
        active = str(data.get("active_provider") or "")
        pool = _as_dict(data.get("credential_pool"))
        providers_section = _as_dict(data.get("providers"))
        all_names = set(pool.keys()) | set(providers_section.keys())
        return [ProviderInfo(name=name, is_active=(name == active)) for name in sorted(all_names)]

    def _collect_credential_pools(
        self, data: dict[str, Any] | None = None
    ) -> list[CredentialPoolEntry]:
        if data is None:
            data = self._read_json_cached(self._paths.shared_path("auth.json"))
        if not data:
            return []

        providers_section = _as_dict(data.get("providers"))
        entries = []
        for name, raw_entry in sorted(_as_dict(data.get("credential_pool")).items()):
            entry = _as_dict(raw_entry)
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
                )
            )
        return entries

    def _collect_logs(self) -> LogState:
        return LogState(
            agent_lines=self._tail_log(self._paths.profile_path("logs", "agent.log"), 20),
            gateway_lines=self._tail_log(self._paths.profile_path("logs", "gateway.log"), 20),
            error_lines=self._tail_log(self._paths.profile_path("logs", "errors.log"), 10),
            cron_lines=self._tail_latest_cron_output(self._paths.shared_path("cron", "output"), 20),
        )

    def _collect_profiles(self) -> ProfilesState:
        profiles_dir = self._paths.shared_path("profiles")
        if not profiles_dir.is_dir():
            return ProfilesState()
        profiles = [
            self._summarize_profile(profile_dir.name, profile_dir)
            for profile_dir in sorted(profiles_dir.iterdir())
            if profile_dir.is_dir()
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
        session_count = 0
        if db_path.exists():
            db = HermesDB(db_path)
            try:
                session_count = db.read_session_count()
            finally:
                db.close()
        return ProfileSummary(
            name=name,
            session_count=session_count,
            latest_log_mtime=_latest_log_mtime(profile_home / "logs"),
            skill_count=_count_skills(profile_home / "skills"),
            db_size_bytes=_file_size(db_path),
            soul_excerpt=_read_soul_excerpt(profile_home / "SOUL.md"),
        )

    def _tail_log(self, path: Path, max_lines: int) -> list[LogLine]:
        key = str(path)
        if not path.exists():
            return self._log_cache.get(key, [])
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - self._log_tail_bytes))
                text = f.read().decode("utf-8", errors="replace")
            lines = text.strip().splitlines()[-max_lines:]
            result = []
            for line in lines:
                match = re.match(
                    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),?\d*\s*-\s*([^-]+?)\s*-\s*(\w+)\s*-\s*(.*)",
                    line,
                )
                if match:
                    ts = match.group(1).split()[-1]
                    message = match.group(4).strip()
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
                    result.append(LogLine(message=line.strip()))
            if result:
                self._log_cache[key] = result
                return result
            return self._log_cache.get(key, [])
        except OSError:
            return self._log_cache.get(key, [])

    def _tail_latest_cron_output(self, output_root: Path, max_lines: int) -> list[LogLine]:
        key = f"cron:{output_root}"
        result = _tail_latest_cron_output(output_root, max_lines, self._log_tail_bytes)
        if result:
            self._log_cache[key] = result
            return result
        return self._log_cache.get(key, [])

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

    def close(self) -> None:
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
    totals = TokenSummary()
    for row in rows:
        started_at = row.get("started_at") or 0.0
        if started_at_min is not None and started_at < started_at_min:
            continue
        totals.input_tokens += row.get("input_tokens") or 0
        totals.output_tokens += row.get("output_tokens") or 0
        totals.cache_read_tokens += row.get("cache_read_tokens") or 0
        totals.cache_write_tokens += row.get("cache_write_tokens") or 0
        totals.reasoning_tokens += row.get("reasoning_tokens") or 0
        totals.total_cost_usd += _resolved_session_cost(row)
    return totals


def _summarize_window(label: str, rows: list[dict[str, Any]], days: int) -> TokenWindowSummary:
    cutoff = time.time() - days * 86400
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


# Approximate cost per 1M tokens (USD) — used when provider doesn't report costs.
# Covers the most common models; defaults to GPT-4o pricing as a reasonable midpoint.
_COST_PER_M = {
    "input": 2.50,  # GPT-4o / Claude Sonnet class
    "output": 10.00,
    "cache_read": 0.30,  # typical prompt caching discount
    "reasoning": 10.00,
}


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    reasoning_tokens: int,
) -> float:
    return (
        input_tokens * _COST_PER_M["input"]
        + output_tokens * _COST_PER_M["output"]
        + cache_read_tokens * _COST_PER_M["cache_read"]
        + reasoning_tokens * _COST_PER_M["reasoning"]
    ) / 1_000_000


def _resolved_session_cost(row: dict[str, Any]) -> float:
    cost = row.get("estimated_cost_usd")
    if str(row.get("cost_status") or "") == "reported":
        return float(cost or 0.0)
    if cost:
        return float(cost)
    return _estimate_cost(
        row.get("input_tokens") or 0,
        row.get("output_tokens") or 0,
        row.get("cache_read_tokens") or 0,
        row.get("reasoning_tokens") or 0,
    )


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


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _safe_mtime(path: Path) -> float:
    return _mtime(path) or 0.0


def _provider_model_label(cfg: dict[str, Any]) -> str:
    provider = str(cfg.get("provider") or "")
    model = str(cfg.get("model") or "")
    if provider and model:
        return f"{provider}/{model}"
    return provider or model


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


def _latest_cron_output_excerpt(output_root: Path, job_id: str) -> tuple[str, bool]:
    if not job_id:
        return "", False
    job_output_dir = output_root / job_id
    if not job_output_dir.is_dir():
        return "", False
    files = []
    for path in job_output_dir.iterdir():
        try:
            if path.is_file():
                files.append(path)
        except OSError:
            continue
    if not files:
        return "", False
    latest = max(files, key=_safe_mtime)
    try:
        lines = latest.read_text(errors="replace").splitlines()
    except OSError:
        return "", False
    silent = any("[SILENT]" in line.upper() for line in lines)
    for line in lines:
        stripped = line.strip()
        if stripped and "[SILENT]" not in stripped.upper():
            return stripped[:80], silent
    return "", silent


def _tail_latest_cron_output(output_root: Path, max_lines: int, max_bytes: int) -> list[LogLine]:
    if not output_root.is_dir():
        return []
    latest_file: Path | None = None
    latest_mtime = 0.0
    for job_dir in output_root.iterdir():
        if not job_dir.is_dir():
            continue
        for path in job_dir.iterdir():
            try:
                if not path.is_file():
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
        with latest_file.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()[-max_lines:]
    except OSError:
        return []
    return [LogLine(message=line.strip()) for line in lines if line.strip()]


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
    except (OSError, subprocess.TimeoutExpired):
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
    except (OSError, subprocess.TimeoutExpired):
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
    "refresh_token",
    "secret",
    "token",
    "user_token",
}

_OAUTH_FIELD_NAMES = {"id_token", "access_token", "refresh_token"}
_API_KEY_FIELD_NAMES = {"api_key", "secret", "token", "user_token"}
_SECRET_URL_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "auth_token",
    "id_token",
    "key",
    "password",
    "refresh_token",
    "secret",
    "token",
    "user_token",
}
_SECRET_OPTION_NAMES = {
    "access-token",
    "api-key",
    "apikey",
    "auth",
    "auth-token",
    "id-token",
    "key",
    "password",
    "refresh-token",
    "secret",
    "token",
    "user-token",
}


def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


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
        return float(value)
    if isinstance(value, str):
        try:
            return float(value or "0")
        except ValueError:
            return 0.0
    return 0.0


def _normalize_secret_option_name(option: str) -> str:
    return option.lstrip("-").lower().replace("_", "-")


def _redact_secret_url(value: str) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not query_pairs:
        return value
    redacted_query = "&".join(
        f"{key}={'[REDACTED]' if key.lower() in _SECRET_URL_QUERY_KEYS else item_value}"
        for key, item_value in query_pairs
    )
    return urlunsplit(parts._replace(query=redacted_query))


def _redact_secret_args(args: object) -> list[str]:
    if not isinstance(args, list):
        return []
    redacted: list[str] = []
    redact_next = False
    for raw_arg in args:
        arg = _redact_secret_url(str(raw_arg))
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
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
    for key in _SECRET_FIELD_NAMES:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


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


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
