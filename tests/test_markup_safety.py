from __future__ import annotations

import io

import pytest
from rich.console import Console

from hermesd.models import (
    ActiveSurface,
    BackgroundProcessInfo,
    ChannelDirectoryState,
    ChannelPlatformInfo,
    CheckpointInfo,
    ConfigSourceStamp,
    ConfigSummary,
    CredentialPoolEntry,
    CronExecution,
    CronExecutionsState,
    CronIncident,
    CronJob,
    CronJobExecutionStats,
    CronState,
    CuratorRun,
    DashboardState,
    DelegationInfo,
    DiscoveredRepoSummary,
    GatewayState,
    GoalSummary,
    HookInfo,
    KanbanBoardSummary,
    KanbanRunSummary,
    KanbanState,
    KanbanTaskLink,
    KanbanTaskSummary,
    LogLine,
    LogState,
    MCPSchemaCache,
    MCPServerInfo,
    MemoryOverview,
    ModelCacheSummary,
    ModelUsage,
    OperationsState,
    PlatformStatus,
    PluginInfo,
    PRMonitorSummary,
    ProfilesState,
    ProfileSummary,
    ProjectSummary,
    ProviderInfo,
    SessionInfo,
    SkillInfo,
    SkillsMemory,
    SkillsPromptSnapshot,
    TokenAnalytics,
    TokenBreakdown,
    ToolGatewayRoute,
    ToolsetAvailability,
    ToolStats,
    VerificationEventSummary,
    VerificationRootSummary,
)
from hermesd.panels import PANEL_NAMES, render_panel
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
                drain_active=True,
                drain_principal=INJECT,
                drain_requested_at=INJECT,
                code_sha=INJECT,
                code_version=INJECT,
                config_fingerprint=INJECT,
                config_generation_short=INJECT,
                config_sources=[ConfigSourceStamp(name=INJECT, path=INJECT, exists=True)],
                session_store_status=INJECT,
                exit_reason=INJECT,
                lifecycle_phase=INJECT,
                last_exit_reason=INJECT,
                last_update_outcome=INJECT,
                last_update_from_version=INJECT,
                last_update_to_version=INJECT,
                last_update_failed_step=INJECT,
                served_profiles=[INJECT],
                platforms=[
                    PlatformStatus(
                        name=INJECT,
                        state="connected",
                        updated_at=INJECT,
                        error_code=INJECT,
                        error_message=INJECT,
                    )
                ],
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
                        family_label=INJECT,
                        missing_from_directory=True,
                    )
                ],
                alias_count=1,
                stale_alias_count=1,
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
                    profile_name=INJECT,
                    chat_type=INJECT,
                    display_name=INJECT,
                    title_source=INJECT,
                    git_branch=INJECT,
                    last_activity_description=INJECT,
                    compression_failure_error=INJECT,
                    cost_source=INJECT,
                    end_reason=INJECT,
                    billing_base_url=INJECT,
                    billing_mode=INJECT,
                    pinned=True,
                )
            ],
            active_surface_count=1,
            active_surfaces=[
                ActiveSurface(session_id="sess_injection_001", surface=INJECT, pid=42, alive=True)
            ],
        )
    if panel_num == 3:  # Tokens / Cost
        return DashboardState(
            sessions=[SessionInfo(session_id="sess_injection_tok", source=INJECT, model=INJECT)],
            token_analytics=TokenAnalytics(
                by_model=[TokenBreakdown(label=INJECT, session_count=1)],
                by_provider=[TokenBreakdown(label=INJECT, session_count=1)],
                usage_source="session_model_usage",
                model_usage_all=[
                    ModelUsage(model=INJECT, provider=INJECT, task=INJECT, api_calls=2)
                ],
                model_usage_24h=[
                    ModelUsage(model=INJECT, provider=INJECT, task=INJECT, api_calls=1)
                ],
                model_usage_7d=[
                    ModelUsage(model=INJECT, provider=INJECT, task=INJECT, api_calls=2)
                ],
            ),
        )
    if panel_num == 4:  # Tools
        return DashboardState(
            tool_stats=[ToolStats(name=INJECT, call_count=2)],
            total_tool_calls=2,
            available_tools=1,
            available_tool_names=[INJECT],
            toolset_availability=ToolsetAvailability(
                enabled_toolsets=[INJECT],
                unavailable_toolsets=[INJECT],
                lazy_tool_count=1,
                disabled_tool_count=1,
            ),
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
                code_execution_mode=INJECT,
                moa_active_preset=INJECT,
                moa_preset_count=1,
                moa_aggregator_label=INJECT,
                mcp_server_count=1,
                mcp_server_names=[INJECT],
                updates_pre_update_backup=INJECT,
                updates_check=True,
                updates_backup_keep=5,
                logging_level=INJECT,
                plugin_enabled_count=1,
                plugin_disabled_count=1,
                goals_max_turns=3,
                delegation_max_concurrent_children=2,
                delegation_max_spawn_depth=2,
                delegation_orchestrator_enabled=True,
                tool_loop_warnings_enabled=True,
                tool_loop_hard_stop_enabled=True,
                max_live_sessions=4,
                streaming_enabled=True,
                network_proxy_configured=True,
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
                        paused=True,
                        paused_reason=INJECT,
                        last_delivery_error=INJECT,
                        dispatch_kind=INJECT,
                    )
                ],
            ),
            cron_executions=CronExecutionsState(
                db_present=True,
                job_stats=[
                    CronJobExecutionStats(
                        job_id="job_injection",
                        completed_24h=1,
                        failed_24h=1,
                        last_status=INJECT,
                        last_error_excerpt=INJECT,
                    )
                ],
                recent=[
                    CronExecution(
                        execution_id=INJECT,
                        job_id="job_injection",
                        job_name=INJECT,
                        status=INJECT,
                        error_excerpt=INJECT,
                    )
                ],
                open_incident_count=1,
                unacked_incident_count=1,
                open_incidents=[
                    CronIncident(
                        incident_id=INJECT,
                        job_id="job_injection",
                        job_name=INJECT,
                        state=INJECT,
                        failure_type=INJECT,
                        error_excerpt=INJECT,
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
                        expires_at=INJECT,
                        last_refresh=INJECT,
                    )
                ],
                hooks=[HookInfo(name=INJECT, description=INJECT, events=[INJECT])],
                plugins=[PluginInfo(name=INJECT, version=INJECT, description=INJECT)],
                mcp_servers=[
                    MCPServerInfo(name=INJECT, transport=INJECT, target=INJECT, tool_filter=INJECT)
                ],
                skills=[SkillInfo(name=INJECT, category="dev", description=INJECT)],
            ),
            config=ConfigSummary(mcp_server_count=2, mcp_server_names=[INJECT, "cached-server"]),
            mcp_cache=MCPSchemaCache(
                mcp_cached_server_count=1,
                mcp_cached_server_names=["cached-server"],
                mcp_schema_cache_age_seconds=60.0,
            ),
            skills_prompt=SkillsPromptSnapshot(
                prompted_skill_count=1,
                prompt_snapshot_age_seconds=120.0,
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
            workspace_path=INJECT,
            goal_mode=INJECT,
            current_step_key=INJECT,
        )
        return DashboardState(
            kanban=KanbanState(
                db_present=True,
                task_count=1,
                run_count=1,
                current_board=INJECT,
                boards=[
                    KanbanBoardSummary(
                        slug=INJECT,
                        current=True,
                        task_count=1,
                        block_kind_counts={INJECT: 1},
                    )
                ],
                status_counts={INJECT: 1},
                active_tasks=[task],
                problem_tasks=[task],
                task_links=[KanbanTaskLink(parent_id=INJECT, child_id=INJECT)],
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
                pr_monitors=[
                    PRMonitorSummary(
                        filename=INJECT,
                        repo=INJECT,
                        checked_at=INJECT,
                        monitored_count=1,
                        tracked_count=1,
                        author_pr_count=1,
                        open_count=1,
                        conflicting_count=1,
                    )
                ],
                delegation_count=1,
                delegation_running_count=1,
                delegations=[
                    DelegationInfo(
                        delegation_id=INJECT,
                        origin_session=INJECT,
                        state=INJECT,
                        delivery_state=INJECT,
                        goal=INJECT,
                        result_status=INJECT,
                        error_excerpt=INJECT,
                    )
                ],
                state_db_schema_version=6,
                state_db_file_generation=INJECT,
                state_db_fts_storage_version=INJECT,
                web_ui_build_hash=INJECT,
                blocked_script_count=1,
                blocked_script_names=[INJECT],
                verification_db_present=True,
                verification_latest_events=[
                    VerificationEventSummary(
                        command=INJECT,
                        canonical_command=INJECT,
                        kind=INJECT,
                        scope=INJECT,
                        status=INJECT,
                        output_summary=INJECT,
                    )
                ],
                verification_roots=[
                    VerificationRootSummary(
                        session_id=INJECT,
                        root=INJECT,
                        last_edit_at=INJECT,
                    )
                ],
                moa_trace_count=1,
                moa_trace_newest_session_id=INJECT,
                moa_trace_latest_record_summary=INJECT,
                moa_trace_latest_record_keys=[INJECT],
                projects_db_present=True,
                projects=[
                    ProjectSummary(
                        slug=INJECT,
                        name=INJECT,
                        board_slug=INJECT,
                        primary_path=INJECT,
                    )
                ],
                discovered_repos=[
                    DiscoveredRepoSummary(root=INJECT, label=INJECT, last_seen=INJECT)
                ],
                goal_count=1,
                goals=[
                    GoalSummary(
                        session_id=INJECT,
                        goal=INJECT,
                        status=INJECT,
                        waiting_reason=INJECT,
                    )
                ],
            ),
        )
    if panel_num == 13:  # Curator
        return DashboardState(
            curator=CuratorRun(
                run_present=True,
                stamp=INJECT,
                started_at=INJECT,
                model=INJECT,
                provider=INJECT,
                state_transitions=[INJECT],
                llm_summary=INJECT,
                llm_error=INJECT,
                scheduler_state_present=True,
                scheduler_last_run_at=INJECT,
                scheduler_last_report_path=INJECT,
            ),
        )
    raise AssertionError(f"no state builder for panel {panel_num}")


# Panels 3 (Tokens / Cost) and 12 (Operations) render only numeric aggregates in
# their compact views — no untrusted free-text field reaches the screen there, so
# the literal-bracket assertion cannot apply to them.
_NO_FREE_TEXT_COMPACT_PANELS = {3, 12}


@pytest.mark.parametrize("panel_num", range(1, 14))
@pytest.mark.parametrize("detail", [False, True])
def test_panel_does_not_crash_on_markup_injection(panel_num: int, detail: bool) -> None:
    state = _state_for(panel_num)
    # Must not raise rich.markup.MarkupError (which would crash the TUI loop).
    panel = render_panel(panel_num, state, Theme(), detail=detail)
    rendered = render_to_str(panel)
    # Rendering must produce the actual panel (titled), not an empty fallback.
    assert PANEL_NAMES[panel_num] in rendered
    # The injected markup literal must survive in the detail view, where every
    # field is rendered (compact views show only a subset of fields).
    if detail:
        assert "desc" in rendered


@pytest.mark.parametrize("panel_num", range(1, 14))
@pytest.mark.parametrize("detail", [False, True])
def test_panel_preserves_literal_brackets(panel_num: int, detail: bool) -> None:
    state = _state_for(panel_num)
    rendered = render_to_str(render_panel(panel_num, state, Theme(), detail=detail))
    if not detail and panel_num in _NO_FREE_TEXT_COMPACT_PANELS:
        return
    # The bracket pair must survive as literal text, not be parsed away as a
    # Rich style tag.
    view = "detail" if detail else "compact"
    assert PAIR in rendered, f"panel {panel_num} {view} stripped literal {PAIR!r}"
    assert CLOSER in rendered, f"panel {panel_num} {view} dropped literal {CLOSER!r}"


@pytest.mark.parametrize(
    "payload",
    [
        "before\x1b[2Jafter",
        "before\x1b]52;c;SGVsbG8=\x1b\\after",
    ],
    ids=["csi", "osc"],
)
@pytest.mark.parametrize("panel_num", range(1, 14))
@pytest.mark.parametrize("detail", [False, True])
def test_panel_strips_terminal_control_sequences(
    panel_num: int,
    detail: bool,
    payload: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(globals(), "INJECT", payload)
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system=None, width=160)

    console.print(render_panel(panel_num, _state_for(panel_num), Theme(), detail=detail))

    rendered = output.getvalue()
    assert "\x1b" not in rendered
    assert "\x9b" not in rendered
    # Stripping the escape must not eat the surrounding text: wherever a free-text
    # field reaches the screen, the literal payload has to still be readable.
    if detail or panel_num not in _NO_FREE_TEXT_COMPACT_PANELS:
        assert "beforeafter" in rendered
