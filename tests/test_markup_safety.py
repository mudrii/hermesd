from __future__ import annotations

import pytest

from hermesd.models import (
    BackgroundProcessInfo,
    ChannelDirectoryState,
    ChannelPlatformInfo,
    CheckpointInfo,
    ConfigSummary,
    CredentialPoolEntry,
    CronJob,
    CronState,
    DashboardState,
    GatewayState,
    HookInfo,
    KanbanRunSummary,
    KanbanState,
    KanbanTaskSummary,
    LogLine,
    LogState,
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
    SessionInfo,
    SkillInfo,
    SkillsMemory,
    TokenAnalytics,
    TokenBreakdown,
    ToolGatewayRoute,
    ToolStats,
)
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str

# A bare closing tag (raises rich.markup.MarkupError at render time, which
# crashes the TUI render loop) plus an unbalanced bracket pair (silently
# stripped by Rich's markup parser). The closer is placed first so it has
# nothing to close — the genuine crash trigger. Untrusted ~/.hermes free-text
# must survive both: render without raising and keep the literal text.
PAIR = "[xy]"
CLOSER = "[/]"
INJECT = f"{CLOSER} desc {PAIR} tag"


def _state_for(panel_num: int) -> DashboardState:
    """Build a DashboardState whose free-text fields for the panel carry INJECT."""
    if panel_num == 1:  # Gateway & Platforms
        return DashboardState(
            gateway=GatewayState(
                pid=1,
                running=True,
                hermes_version=INJECT,
                platforms=[PlatformStatus(name=INJECT, state="connected")],
            ),
            channels=ChannelDirectoryState(
                platform_count=1,
                platforms=[
                    ChannelPlatformInfo(
                        name=INJECT,
                        entry_count=1,
                        states=[INJECT],
                        connected=True,
                        capabilities=[INJECT],
                    )
                ],
            ),
        )
    if panel_num == 2:  # Sessions
        return DashboardState(
            sessions=[
                SessionInfo(
                    session_id="sess_injection_001",
                    source=INJECT,
                    model=INJECT,
                    parent_session_id="parent_injection_001",
                    billing_provider=INJECT,
                    cost_status=INJECT,
                    pricing_version=INJECT,
                    cwd=INJECT,
                    handoff_state=INJECT,
                    handoff_platform=INJECT,
                    handoff_error=INJECT,
                    title=INJECT,
                    api_call_count=3,
                    archived=True,
                    rewind_count=1,
                    message_count=5,
                    is_active=True,
                )
            ],
        )
    if panel_num == 3:  # Tokens / Cost
        return DashboardState(
            sessions=[SessionInfo(session_id="sess_injection_tok", source=INJECT, model=INJECT)],
            token_analytics=TokenAnalytics(
                by_model=[TokenBreakdown(label=INJECT, session_count=1)],
                by_provider=[TokenBreakdown(label=INJECT, session_count=1)],
            ),
        )
    if panel_num == 4:  # Tools
        return DashboardState(
            tool_stats=[ToolStats(name=INJECT, call_count=2)],
            total_tool_calls=2,
            available_tools=1,
            available_tool_names=[INJECT],
            background_processes=[BackgroundProcessInfo(session_id=INJECT, command=INJECT, pid=42)],
            checkpoints=[
                CheckpointInfo(
                    repo_id=INJECT,
                    workdir_name=INJECT,
                    commit_count=1,
                    last_reason=INJECT,
                )
            ],
        )
    if panel_num == 5:  # Config
        return DashboardState(
            config=ConfigSummary(
                model=INJECT,
                provider=INJECT,
                personality=INJECT,
                provider_routing_summary=INJECT,
                smart_model_routing_cheap_model=INJECT,
                fallback_model_label=INJECT,
                memory_provider=INJECT,
                toolsets=[INJECT],
                tool_gateway_domain=INJECT,
                tool_gateway_scheme=INJECT,
                firecrawl_gateway_url=INJECT,
                tool_gateway_routes=[ToolGatewayRoute(tool=INJECT, mode="gateway")],
            ),
        )
    if panel_num == 6:  # Cron
        return DashboardState(
            cron=CronState(
                job_count=1,
                jobs=[
                    CronJob(
                        job_id="job_injection",
                        name=INJECT,
                        schedule_display=INJECT,
                        state="scheduled",
                        deliver=INJECT,
                        delivery_target_label=INJECT,
                        latest_output_excerpt=INJECT,
                        latest_output_path=INJECT,
                        last_status="error",
                        last_error=INJECT,
                        next_run_at="2026-06-14T00:00:00Z",
                    )
                ],
            ),
        )
    if panel_num == 7:  # Skills / Integrations
        return DashboardState(
            skills_memory=SkillsMemory(
                skill_count=1,
                skill_categories=1,
                providers=[ProviderInfo(name=INJECT, is_active=True)],
                credential_pools=[
                    CredentialPoolEntry(
                        name=INJECT,
                        label=INJECT,
                        auth_type=INJECT,
                        source=INJECT,
                        last_status=INJECT,
                        cooldown_remaining=INJECT,
                    )
                ],
                hooks=[HookInfo(name=INJECT, description=INJECT, events=[INJECT])],
                plugins=[PluginInfo(name=INJECT, version=INJECT, description=INJECT)],
                mcp_servers=[
                    MCPServerInfo(name=INJECT, transport=INJECT, target=INJECT, tool_filter=INJECT)
                ],
                skills=[SkillInfo(name=INJECT, category="dev", description=INJECT)],
            ),
        )
    if panel_num == 8:  # Logs
        line = LogLine(
            timestamp="2026-06-14 00:00:00",
            component=INJECT,
            level="ERROR",
            session_id=INJECT,
            message=INJECT,
        )
        return DashboardState(logs=LogState(agent_lines=[line]))
    if panel_num == 9:  # Profiles
        return DashboardState(
            profile_mode_label=INJECT,
            profiles=ProfilesState(
                profile_count=1,
                profiles=[ProfileSummary(name=INJECT, session_count=1, soul_excerpt=INJECT)],
            ),
        )
    if panel_num == 10:  # Memory
        return DashboardState(
            memory=MemoryOverview(
                provider=INJECT,
                memory_file_count=1,
                soul_size_bytes=128,
                soul_excerpt=INJECT,
                memory_files=[INJECT],
            ),
        )
    if panel_num == 11:  # Kanban
        task = KanbanTaskSummary(
            task_id=INJECT,
            title=INJECT,
            assignee=INJECT,
            status="in_progress",
            worker_pid=42,
            consecutive_failures=2,
            last_failure_error=INJECT,
            branch_name=INJECT,
        )
        return DashboardState(
            kanban=KanbanState(
                db_present=True,
                task_count=1,
                run_count=1,
                status_counts={INJECT: 1},
                active_tasks=[task],
                problem_tasks=[task],
                recent_runs=[
                    KanbanRunSummary(
                        run_id=1,
                        task_id=INJECT,
                        profile=INJECT,
                        status=INJECT,
                        outcome=INJECT,
                        error=INJECT,
                    )
                ],
            ),
        )
    if panel_num == 12:  # Operations
        return DashboardState(
            operations=OperationsState(
                dashboard_process_count=1,
                desktop_build_stamp=INJECT,
                model_caches=[ModelCacheSummary(name=INJECT, provider_count=1, model_count=1)],
                pr_monitors=[PRMonitorSummary(filename=INJECT, repo=INJECT, checked_at=INJECT)],
            ),
        )
    raise AssertionError(f"no state builder for panel {panel_num}")


@pytest.mark.parametrize("panel_num", range(1, 13))
@pytest.mark.parametrize("detail", [False, True])
def test_panel_does_not_crash_on_markup_injection(panel_num: int, detail: bool) -> None:
    state = _state_for(panel_num)
    # Must not raise rich.markup.MarkupError (which would crash the TUI loop).
    panel = render_panel(panel_num, state, Theme(), detail=detail)
    rendered = render_to_str(panel)
    assert rendered  # rendered to something rather than crashing


@pytest.mark.parametrize("panel_num", range(1, 13))
@pytest.mark.parametrize("detail", [False, True])
def test_panel_preserves_literal_brackets(panel_num: int, detail: bool) -> None:
    state = _state_for(panel_num)
    rendered = render_to_str(render_panel(panel_num, state, Theme(), detail=detail))
    # The bracket pair must survive as literal text, not be parsed away as a
    # Rich style tag. Compact views only show a subset of fields, so require
    # the literal in at least the detail view where every field is rendered.
    if detail:
        assert PAIR in rendered, f"panel {panel_num} detail stripped literal {PAIR!r}"
        assert CLOSER in rendered, f"panel {panel_num} detail dropped literal {CLOSER!r}"
