from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml

from hermesd.db import HermesDB
from hermesd.models import (
    ConfigSummary,
    CronJob,
    CronState,
    DashboardState,
    GatewayState,
    LogLine,
    LogState,
    PlatformStatus,
    ProviderInfo,
    SessionInfo,
    SkillInfo,
    SkillsMemory,
    TokenSummary,
    ToolStats,
)


class Collector:
    def __init__(self, hermes_home: Path):
        self._home = hermes_home
        self._db = HermesDB(hermes_home / "state.db")
        self._mtime_cache: dict[str, float] = {}
        self._json_cache: dict[str, Any] = {}
        self._config_cache: dict[str, Any] | None = None

    def collect(self) -> DashboardState:
        tool_count, tool_names = self._collect_available_tools()
        return DashboardState(
            hermes_home=self._home,
            collected_at=time.time(),
            gateway=self._collect_gateway(),
            sessions=self._collect_sessions(),
            tokens_today=self._collect_tokens_today(),
            tokens_total=self._collect_tokens_total(),
            tool_stats=self._collect_tool_stats(),
            total_tool_calls=self._collect_total_tool_calls(),
            available_tools=tool_count,
            available_tool_names=tool_names,
            config=self._collect_config(),
            cron=self._collect_cron(),
            skills_memory=self._collect_skills_memory(),
            logs=self._collect_logs(),
            version_behind=self._collect_version_behind(),
            active_skin=self._collect_skin(),
        )

    def _read_json_cached(self, path: Path) -> Any:
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self._json_cache.get(key)
        if self._mtime_cache.get(key) == mtime and key in self._json_cache:
            return self._json_cache[key]
        try:
            with open(path) as f:
                data = json.load(f)
            self._mtime_cache[key] = mtime
            self._json_cache[key] = data
            return data
        except (json.JSONDecodeError, OSError):
            return self._json_cache.get(key)

    def _read_yaml_cached(self) -> dict[str, Any]:
        if self._config_cache is not None:
            return self._config_cache
        path = self._home / "config.yaml"
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                self._config_cache = yaml.safe_load(f) or {}
        except Exception:
            self._config_cache = {}
        return self._config_cache

    def _collect_gateway(self) -> GatewayState:
        data = self._read_json_cached(self._home / "gateway_state.json")
        if not data:
            return GatewayState()
        platforms = []
        for name, info in (data.get("platforms") or {}).items():
            platforms.append(PlatformStatus(
                name=name,
                state=info.get("state", "unknown"),
                updated_at=info.get("updated_at", ""),
            ))
        pid = data.get("pid", 0)
        running = data.get("gateway_state") == "running"
        # The PID in gateway_state.json can be stale if launchd restarted
        # the gateway. Check both the recorded PID and the launchd PID.
        if running and pid:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                # Recorded PID is dead — check if launchd has a live gateway
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
        pid_file = self._home / "gateway.pid"
        if pid_file.exists():
            try:
                content = pid_file.read_text().strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        lpid = data.get("pid", 0)
                    else:
                        lpid = int(content)
                    if lpid:
                        os.kill(lpid, 0)
                        return lpid
            except (ValueError, json.JSONDecodeError, ProcessLookupError,
                    PermissionError, OSError):
                pass
        return None

    def _collect_hermes_version(self) -> tuple[str, int]:
        """Read hermes-agent version from pyproject.toml and update status."""
        version = ""
        pyproject = self._home / "hermes-agent" / "pyproject.toml"
        if pyproject.exists():
            try:
                for line in pyproject.read_text().splitlines():
                    if line.strip().startswith("version"):
                        version = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except OSError:
                pass
        behind = 0
        update_check = self._read_json_cached(self._home / ".update_check")
        if isinstance(update_check, dict):
            behind = update_check.get("behind", 0)
        return version, behind

    def _collect_sessions(self) -> list[SessionInfo]:
        rows = self._db.read_sessions()
        return [
            SessionInfo(
                session_id=r["id"],
                source=r.get("source") or "",
                model=r.get("model") or "",
                message_count=r.get("message_count") or 0,
                tool_call_count=r.get("tool_call_count") or 0,
                input_tokens=r.get("input_tokens") or 0,
                output_tokens=r.get("output_tokens") or 0,
                cache_read_tokens=r.get("cache_read_tokens") or 0,
                cache_write_tokens=r.get("cache_write_tokens") or 0,
                reasoning_tokens=r.get("reasoning_tokens") or 0,
                estimated_cost_usd=r.get("estimated_cost_usd") or 0.0,
                started_at=r.get("started_at") or 0.0,
                ended_at=r.get("ended_at"),
                title=r.get("title"),
                is_active=r.get("ended_at") is None,
            )
            for r in rows
        ]

    def _collect_tokens_today(self) -> TokenSummary:
        rows = self._db.read_sessions()
        today_start = _today_epoch()
        totals = TokenSummary()
        for r in rows:
            if (r.get("started_at") or 0) >= today_start:
                in_tok = r.get("input_tokens") or 0
                out_tok = r.get("output_tokens") or 0
                cache_r = r.get("cache_read_tokens") or 0
                cache_w = r.get("cache_write_tokens") or 0
                reason = r.get("reasoning_tokens") or 0
                totals.input_tokens += in_tok
                totals.output_tokens += out_tok
                totals.cache_read_tokens += cache_r
                totals.cache_write_tokens += cache_w
                totals.reasoning_tokens += reason
                cost = r.get("estimated_cost_usd") or 0.0
                if not cost:
                    cost = _estimate_cost(in_tok, out_tok, cache_r, reason)
                totals.total_cost_usd += cost
        return totals

    def _collect_tokens_total(self) -> TokenSummary:
        d = self._db.read_token_totals()
        in_tok = d.get("input_tokens", 0)
        out_tok = d.get("output_tokens", 0)
        cache_r = d.get("cache_read_tokens", 0)
        cache_w = d.get("cache_write_tokens", 0)
        reason = d.get("reasoning_tokens", 0)
        cost = d.get("total_cost_usd", 0.0)
        if not cost:
            cost = _estimate_cost(in_tok, out_tok, cache_r, reason)
        return TokenSummary(
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_tokens=cache_r,
            cache_write_tokens=cache_w,
            reasoning_tokens=reason,
            total_cost_usd=cost,
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

    def _collect_available_tools(self) -> tuple[int, list[str]]:
        sessions_data = self._read_json_cached(self._home / "sessions" / "sessions.json")
        if not isinstance(sessions_data, dict):
            return 0, []
        for entry in sessions_data.values():
            if isinstance(entry, dict) and "session_id" in entry:
                sid = entry["session_id"]
                session_file = self._home / "sessions" / f"session_{sid}.json"
                data = self._read_json_cached(session_file)
                if isinstance(data, dict) and "tools" in data:
                    names = []
                    for t in data["tools"]:
                        if isinstance(t, dict):
                            name = t.get("function", {}).get("name") or t.get("name", "")
                        else:
                            name = str(t)
                        if name:
                            names.append(name)
                    return len(names), sorted(names)
        return 0, []

    def _collect_config(self) -> ConfigSummary:
        cfg = self._read_yaml_cached()
        if not cfg:
            return ConfigSummary()
        model_cfg = cfg.get("model", {})
        agent_cfg = cfg.get("agent", {})
        comp_cfg = cfg.get("compression", {})
        sec_cfg = cfg.get("security", {})
        app_cfg = cfg.get("approvals", {})
        personality = agent_cfg.get("active_personality", "")
        if not personality:
            personalities = agent_cfg.get("personalities", {})
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
        )

    def _collect_cron(self) -> CronState:
        tick_path = self._home / "cron" / ".tick.lock"
        last_tick: float | None = None
        if tick_path.exists():
            try:
                mtime = tick_path.stat().st_mtime
                last_tick = time.time() - mtime
            except OSError:
                pass

        jobs: list[CronJob] = []
        error_count = 0
        data = self._read_json_cached(self._home / "cron" / "jobs.json")
        if isinstance(data, dict):
            for j in data.get("jobs", []):
                if not isinstance(j, dict):
                    continue
                state = j.get("state", "")
                if j.get("last_status") == "error" or j.get("last_error"):
                    error_count += 1
                jobs.append(CronJob(
                    job_id=j.get("id", ""),
                    name=j.get("name", ""),
                    schedule_display=j.get("schedule_display", ""),
                    state=state,
                    enabled=j.get("enabled", True),
                    next_run_at=j.get("next_run_at", ""),
                    last_status=j.get("last_status"),
                ))

        return CronState(
            last_tick_ago_seconds=last_tick,
            job_count=len(jobs),
            error_count=error_count,
            jobs=jobs,
        )

    def _collect_skills_memory(self) -> SkillsMemory:
        categories: set[str] = set()
        skills: list[SkillInfo] = []
        skills_dir = self._home / "skills"

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

        mem_dir = self._home / "memories"
        mem_count = 0
        if mem_dir.is_dir():
            mem_count = sum(1 for f in mem_dir.iterdir() if f.is_file())

        providers = self._collect_providers()
        return SkillsMemory(
            skill_count=len(skills),
            skill_categories=len(categories),
            memory_file_count=mem_count,
            providers=providers,
            skills=skills,
        )

    def _read_skill_description(self, category: str, name: str) -> str:
        """Read the description from a skill's SKILL.md frontmatter."""
        skills_dir = self._home / "skills"
        # Skills are at skills/<category>/<name>/SKILL.md
        skill_md = skills_dir / category / name / "SKILL.md"
        if not skill_md.exists():
            return ""
        try:
            text = skill_md.read_text(errors="replace")
            # Parse YAML frontmatter between --- markers
            if text.startswith("---"):
                end = text.find("---", 3)
                if end > 0:
                    front = text[3:end].strip()
                    for line in front.splitlines():
                        if line.startswith("description:"):
                            return line[len("description:"):].strip()
        except OSError:
            pass
        return ""

    def _collect_providers(self) -> list[ProviderInfo]:
        data = self._read_json_cached(self._home / "auth.json")
        if not data:
            return []
        active = data.get("active_provider", "")
        pool = data.get("credential_pool", {})
        providers_section = data.get("providers", {})
        all_names = set(pool.keys()) | set(providers_section.keys())
        return [
            ProviderInfo(name=name, is_active=(name == active))
            for name in sorted(all_names)
        ]

    def _collect_logs(self) -> LogState:
        return LogState(
            agent_lines=self._tail_log(self._home / "logs" / "agent.log", 20),
            gateway_lines=self._tail_log(self._home / "logs" / "gateway.log", 20),
            error_lines=self._tail_log(self._home / "logs" / "errors.log", 10),
        )

    def _tail_log(self, path: Path, max_lines: int) -> list[LogLine]:
        if not path.exists():
            return []
        try:
            text = path.read_text()
            lines = text.strip().splitlines()[-max_lines:]
            result = []
            for line in lines:
                match = re.match(
                    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}),?\d*\s*-\s*\w+\s*-\s*(\w+)\s*-\s*(.*)",
                    line,
                )
                if match:
                    ts = match.group(1).split()[-1]
                    result.append(LogLine(
                        timestamp=ts, level=match.group(2), message=match.group(3).strip(),
                    ))
                elif line.strip():
                    result.append(LogLine(message=line.strip()))
            return result
        except OSError:
            return []

    def _collect_version_behind(self) -> int:
        data = self._read_json_cached(self._home / ".update_check")
        if isinstance(data, dict):
            return data.get("behind", 0)
        return 0

    def _collect_skin(self) -> str:
        cfg = self._read_yaml_cached()
        return cfg.get("display", {}).get("skin", "default")

    def close(self) -> None:
        self._db.close()


def _today_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# Approximate cost per 1M tokens (USD) — used when provider doesn't report costs.
# Covers the most common models; defaults to GPT-4o pricing as a reasonable midpoint.
_COST_PER_M = {
    "input": 2.50,      # GPT-4o / Claude Sonnet class
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
