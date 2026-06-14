from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, Field

from hermesd.paths import default_hermes_home

# cost_status values the hermes-agent producer treats as authoritative (an actual
# billed/known cost, not a token-based estimate): "reported" (legacy), "exact",
# and "included" (subscription-covered, genuinely $0.00).
AUTHORITATIVE_COST_STATUSES: frozenset[str] = frozenset({"reported", "exact", "included"})


class PlatformStatus(BaseModel):
    name: str
    state: str = "unknown"
    updated_at: str = ""
    error_code: str = ""
    error_message: str = ""


class GatewayState(BaseModel):
    pid: int = 0
    running: bool = False
    state: str = "unknown"
    platforms: list[PlatformStatus] = Field(default_factory=list)
    hermes_version: str = ""
    updates_behind: int = 0
    active_agents: int = 0
    restart_requested: bool = False


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
    latest_output_path: str = ""
    latest_output_mtime: float | None = None
    silent_run: bool = False
    next_run_at: str | None = None
    last_status: str | None = None
    last_error: str = ""


class CronState(BaseModel):
    last_tick_ago_seconds: float | None = None
    job_count: int = 0
    error_count: int = 0
    max_parallel_jobs: int = 0
    wrap_response: bool = False
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


class LogStream(BaseModel):
    name: str
    path: str = ""
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


class ChannelDirectoryState(BaseModel):
    updated_at: str = ""
    platform_count: int = 0
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


class KanbanState(BaseModel):
    db_present: bool = False
    task_count: int = 0
    run_count: int = 0
    event_count: int = 0
    comment_count: int = 0
    dispatch_in_gateway: bool = False
    dispatch_interval_seconds: int = 0
    auto_decompose: bool = False
    failure_limit: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    assignee_counts: dict[str, int] = Field(default_factory=dict)
    active_tasks: list[KanbanTaskSummary] = Field(default_factory=list)
    problem_tasks: list[KanbanTaskSummary] = Field(default_factory=list)
    recent_runs: list[KanbanRunSummary] = Field(default_factory=list)


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


class OperationsState(BaseModel):
    dashboard_process_count: int = 0
    desktop_build_stamp: str = ""
    model_caches: list[ModelCacheSummary] = Field(default_factory=list)
    pr_monitors: list[PRMonitorSummary] = Field(default_factory=list)


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
    llm_summary: str = ""
    llm_error: str = ""


class HealthSummary(BaseModel):
    total_sources: int = 0
    ok_sources: int = 0
    failed_sources: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)


class RuntimeStatus(BaseModel):
    agent_running: bool = True
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
    sessions: list[SessionInfo] = Field(default_factory=list)
    session_message_match_query: str = ""
    session_message_match_ids: set[str] = Field(default_factory=set)
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
    channels: ChannelDirectoryState = Field(default_factory=ChannelDirectoryState)
    kanban: KanbanState = Field(default_factory=KanbanState)
    operations: OperationsState = Field(default_factory=OperationsState)
    skills_memory: SkillsMemory = Field(default_factory=SkillsMemory)
    memory: MemoryOverview = Field(default_factory=MemoryOverview)
    profiles: ProfilesState = Field(default_factory=ProfilesState)
    logs: LogState = Field(default_factory=LogState)
    version_behind: int = 0
    active_skin: str = "default"
    curator: CuratorRun = Field(default_factory=CuratorRun)
