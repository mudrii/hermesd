from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, Field


class PlatformStatus(BaseModel):
    name: str
    state: str = "unknown"
    updated_at: str = ""


class GatewayState(BaseModel):
    pid: int = 0
    running: bool = False
    state: str = "unknown"
    platforms: list[PlatformStatus] = Field(default_factory=list)
    hermes_version: str = ""
    updates_behind: int = 0


class SessionInfo(BaseModel):
    session_id: str
    source: str = ""
    model: str = ""
    message_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0
    started_at: float = 0.0
    ended_at: float | None = None
    title: str | None = None
    is_active: bool = False


class TokenSummary(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_cost_usd: float = 0.0


class ToolStats(BaseModel):
    name: str
    call_count: int = 0


class CronJob(BaseModel):
    job_id: str = ""
    name: str = ""
    schedule_display: str = ""
    state: str = ""
    enabled: bool = True
    next_run_at: str | None = None
    last_status: str | None = None


class CronState(BaseModel):
    last_tick_ago_seconds: float | None = None
    job_count: int = 0
    error_count: int = 0
    jobs: list[CronJob] = Field(default_factory=list)


class ConfigSummary(BaseModel):
    model: str = ""
    provider: str = ""
    personality: str = ""
    max_turns: int = 0
    compression_threshold: float = 0.0
    reasoning_effort: str = ""
    security_redact: bool = False
    approvals_mode: str = ""


class ProviderInfo(BaseModel):
    name: str
    is_active: bool = False


class SkillInfo(BaseModel):
    name: str
    category: str = ""
    description: str = ""


class SkillsMemory(BaseModel):
    skill_count: int = 0
    skill_categories: int = 0
    memory_file_count: int = 0
    providers: list[ProviderInfo] = Field(default_factory=list)
    skills: list[SkillInfo] = Field(default_factory=list)


class LogLine(BaseModel):
    timestamp: str = ""
    level: str = ""
    message: str = ""


class LogState(BaseModel):
    agent_lines: list[LogLine] = Field(default_factory=list)
    gateway_lines: list[LogLine] = Field(default_factory=list)
    error_lines: list[LogLine] = Field(default_factory=list)


class DashboardState(BaseModel):
    hermes_home: Path = Field(default_factory=lambda: Path.home() / ".hermes")
    collected_at: float = Field(default_factory=time.time)
    is_stale: bool = False
    gateway: GatewayState = Field(default_factory=GatewayState)
    sessions: list[SessionInfo] = Field(default_factory=list)
    tokens_today: TokenSummary = Field(default_factory=TokenSummary)
    tokens_total: TokenSummary = Field(default_factory=TokenSummary)
    tool_stats: list[ToolStats] = Field(default_factory=list)
    total_tool_calls: int = 0
    available_tools: int = 0
    available_tool_names: list[str] = Field(default_factory=list)
    config: ConfigSummary = Field(default_factory=ConfigSummary)
    cron: CronState = Field(default_factory=CronState)
    skills_memory: SkillsMemory = Field(default_factory=SkillsMemory)
    logs: LogState = Field(default_factory=LogState)
    version_behind: int = 0
    active_skin: str = "default"
