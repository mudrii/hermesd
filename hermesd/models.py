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
    parent_session_id: str = ""
    billing_provider: str = ""
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
    watcher_interval: int = 0
    watch_patterns: list[str] = Field(default_factory=list)


class CheckpointInfo(BaseModel):
    repo_id: str
    workdir: str = ""
    workdir_name: str = ""
    commit_count: int = 0
    last_reason: str = ""
    last_checkpoint_at: float | None = None


class CronJob(BaseModel):
    job_id: str = ""
    name: str = ""
    schedule_display: str = ""
    state: str = ""
    enabled: bool = True
    deliver: str = ""
    delivery_target_label: str = ""
    latest_output_excerpt: str = ""
    silent_run: bool = False
    next_run_at: str | None = None
    last_status: str | None = None


class CronState(BaseModel):
    last_tick_ago_seconds: float | None = None
    job_count: int = 0
    error_count: int = 0
    jobs: list[CronJob] = Field(default_factory=list)


class ToolGatewayRoute(BaseModel):
    tool: str
    mode: str = "direct"
    token_present: bool = False


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


class ProviderInfo(BaseModel):
    name: str
    is_active: bool = False


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


class SkillInfo(BaseModel):
    name: str
    category: str = ""
    description: str = ""


class HookInfo(BaseModel):
    name: str
    description: str = ""
    events: list[str] = Field(default_factory=list)


class PluginInfo(BaseModel):
    name: str
    version: str = ""
    description: str = ""
    source: str = "user"
    enabled: bool = True
    tool_count: int = 0
    hook_count: int = 0
    dashboard_enabled: bool = False


class MCPServerInfo(BaseModel):
    name: str
    enabled: bool = True
    transport: str = ""
    target: str = ""
    tool_filter: str = ""


class SkillsMemory(BaseModel):
    skill_count: int = 0
    skill_categories: int = 0
    memory_file_count: int = 0
    providers: list[ProviderInfo] = Field(default_factory=list)
    credential_pools: list[CredentialPoolEntry] = Field(default_factory=list)
    hooks: list[HookInfo] = Field(default_factory=list)
    plugins: list[PluginInfo] = Field(default_factory=list)
    mcp_servers: list[MCPServerInfo] = Field(default_factory=list)
    boot_md_present: bool = False
    boot_md_mtime: float | None = None
    skills: list[SkillInfo] = Field(default_factory=list)


class MemoryOverview(BaseModel):
    provider: str = ""
    memory_file_count: int = 0
    memory_word_count: int = 0
    user_word_count: int = 0
    soul_size_bytes: int = 0
    soul_excerpt: str = ""
    memory_files: list[str] = Field(default_factory=list)


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


class LogState(BaseModel):
    agent_lines: list[LogLine] = Field(default_factory=list)
    gateway_lines: list[LogLine] = Field(default_factory=list)
    error_lines: list[LogLine] = Field(default_factory=list)
    cron_lines: list[LogLine] = Field(default_factory=list)


class HealthSummary(BaseModel):
    total_sources: int = 0
    ok_sources: int = 0
    failed_sources: list[str] = Field(default_factory=list)


class RuntimeStatus(BaseModel):
    agent_running: bool = True
    last_activity_age_seconds: float | None = None
    banner: str = ""


class DashboardState(BaseModel):
    hermes_home: Path = Field(default_factory=lambda: Path.home() / ".hermes")
    selected_profile: str | None = None
    profile_mode_label: str = "root"
    collected_at: float = Field(default_factory=time.time)
    is_stale: bool = False
    health: HealthSummary = Field(default_factory=HealthSummary)
    runtime: RuntimeStatus = Field(default_factory=RuntimeStatus)
    gateway: GatewayState = Field(default_factory=GatewayState)
    sessions: list[SessionInfo] = Field(default_factory=list)
    tokens_today: TokenSummary = Field(default_factory=TokenSummary)
    tokens_total: TokenSummary = Field(default_factory=TokenSummary)
    token_analytics: TokenAnalytics = Field(default_factory=TokenAnalytics)
    tool_stats: list[ToolStats] = Field(default_factory=list)
    total_tool_calls: int = 0
    available_tools: int = 0
    available_tool_names: list[str] = Field(default_factory=list)
    background_processes: list[BackgroundProcessInfo] = Field(default_factory=list)
    checkpoints: list[CheckpointInfo] = Field(default_factory=list)
    config: ConfigSummary = Field(default_factory=ConfigSummary)
    cron: CronState = Field(default_factory=CronState)
    skills_memory: SkillsMemory = Field(default_factory=SkillsMemory)
    memory: MemoryOverview = Field(default_factory=MemoryOverview)
    profiles: ProfilesState = Field(default_factory=ProfilesState)
    logs: LogState = Field(default_factory=LogState)
    version_behind: int = 0
    active_skin: str = "default"
