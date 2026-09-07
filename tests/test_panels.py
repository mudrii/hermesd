from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from hermesd.collector import Collector
from hermesd.models import (
    ChannelDirectoryState,
    ConfigSummary,
    CredentialPoolEntry,
    CronState,
    DashboardState,
    DiscoveredRepoSummary,
    GatewayState,
    GoalSummary,
    KanbanBoardSummary,
    KanbanRunSummary,
    KanbanState,
    KanbanTaskLink,
    KanbanTaskSummary,
    LogLine,
    LogState,
    MemoryOverview,
    ModelCacheSummary,
    OperationsState,
    PlatformStatus,
    PRMonitorSummary,
    ProjectSummary,
    ProviderInfo,
    SessionInfo,
    SkillsMemory,
    TokenSummary,
    ToolStats,
    VerificationEventSummary,
    VerificationRootSummary,
)
from hermesd.panels import PANEL_NAMES, render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str


@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("panel_num", sorted(PANEL_NAMES))
def test_registered_panels_render_by_public_number(panel_num: int, detail: bool):
    text = render_to_str(render_panel(panel_num, DashboardState(), Theme(), detail=detail))

    assert f"[{panel_num}] {PANEL_NAMES[panel_num]}" in text


@pytest.mark.parametrize(
    ("panel_num", "expected_snippets"),
    [
        (1, ("Channel Directory", "telegram", "meeting invites")),
        (2, ("sess_001", "gpt-5.4", "openai-codex")),
        (4, ("shell_exec", "proc_alpha", "project-alpha")),
        (5, ("openai-codex", "Tool Gateway", "Dashboard")),
        (7, ("Primary Codex", "startup-check", "weather", "skill-0")),
        (8, ("Tool call: web_search", "Response generated")),
        (10, ("MEMORY.md", "SOUL.md", "Remember the operator")),
        (11, ("Implement dashboard auth visibility", "Status Counts", "Recent Runs")),
        (12, ("Model Caches", "models_dev_cache.json", "NousResearch/hermes-agent")),
    ],
)
def test_collected_fixture_detail_panels_render_fixture_values(
    populated_hermes_home: Path,
    panel_num: int,
    expected_snippets: tuple[str, ...],
):
    collector = Collector(populated_hermes_home, pid_exists=lambda pid: pid == 12345)
    try:
        state = collector.collect()
    finally:
        collector.close()

    text = render_to_str(
        render_panel(panel_num, state, Theme(), detail=True),
        width=160,
        no_color=True,
    )

    for snippet in expected_snippets:
        assert snippet in text


def test_gateway_panel_compact():
    state = DashboardState(
        gateway=GatewayState(
            pid=12345,
            running=True,
            state="running",
            platforms=[
                PlatformStatus(name="telegram", state="connected"),
                PlatformStatus(name="discord", state="disconnected"),
            ],
        ),
    )
    panel = render_panel(1, state, Theme(), detail=False)
    text = render_to_str(panel, width=120, no_color=True)
    assert "Gateway" in text
    assert "Running" in text


def test_gateway_panel_detail():
    from hermesd.models import ChannelDirectoryState, ChannelPlatformInfo

    state = DashboardState(
        gateway=GatewayState(
            pid=12345,
            running=True,
            state="running",
            platforms=[
                PlatformStatus(
                    name="telegram", state="connected", updated_at="2026-04-08T17:42:57+00:00"
                ),
            ],
        ),
        channels=ChannelDirectoryState(
            platform_count=2,
            platforms=[
                ChannelPlatformInfo(
                    name="telegram",
                    entry_count=1,
                    connected=True,
                    family_label="Telegram",
                ),
                ChannelPlatformInfo(
                    name="feishu",
                    entry_count=0,
                    capabilities=["meeting invites"],
                    family_label="Feishu",
                    missing_from_directory=True,
                ),
            ],
        ),
    )
    panel = render_panel(1, state, Theme(), detail=True)
    text = render_to_str(panel, width=120, no_color=True)
    assert "telegram" in text.lower()
    assert "Channel Directory" in text
    assert "feishu" in text
    assert "missing directory" in text
    assert "Telegram" in text
    assert "meeting invites" in text


def test_gateway_panel_detail_shows_channel_aliases():
    state = DashboardState(
        channels=ChannelDirectoryState(alias_count=3, alias_platform_count=2, stale_alias_count=1),
    )
    text = render_to_str(render_panel(1, state, Theme(), detail=True), width=120, no_color=True)
    assert "Channel Aliases" in text
    assert "3 aliases" in text
    assert "2 platforms" in text
    assert "1 stale" in text


def test_gateway_panel_detail_shows_platform_error():
    state = DashboardState(
        gateway=GatewayState(
            pid=0,
            running=False,
            state="stopped",
            active_agents=3,
            restart_requested=True,
            platforms=[
                PlatformStatus(
                    name="discord",
                    state="disconnected",
                    error_code="reconnect_failed",
                    error_message="failed to reconnect",
                ),
            ],
        ),
    )
    panel = render_panel(1, state, Theme(), detail=True)
    text = render_to_str(panel, width=120)
    assert "failed to reconnect" in text
    assert "reconnect_failed" in text
    assert "restart" in text.lower()
    assert "3 active agents" in text


def test_gateway_panel_detail_shows_lifecycle_state():
    state = DashboardState(
        gateway=GatewayState(
            running=True,
            pid=12345,
            state="running",
            active_agents=2,
            busy=True,
            drainable=True,
            drain_active=True,
            drain_requested_at="2026-07-10T10:00:00Z",
            drain_principal="nas",
            drain_suppress_notification=True,
            served_profiles=["root", "coding"],
            scale_to_zero_idle_timeout_minutes=15,
            scale_to_zero_relay_only=True,
        )
    )
    text = render_to_str(render_panel(1, state, Theme(), detail=True), width=120, no_color=True)
    assert "busy" in text
    assert "drainable" in text
    assert "external drain" in text
    assert "nas" in text
    assert "root, coding" in text
    assert "15m idle" in text
    assert "relay-only" in text


def test_gateway_panel_compact_shows_platform_error_marker():
    state = DashboardState(
        gateway=GatewayState(
            running=True,
            state="running",
            platforms=[
                PlatformStatus(
                    name="discord",
                    state="disconnected",
                    error_code="reconnect_failed",
                ),
            ],
        ),
    )
    panel = render_panel(1, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "⚠" in text


def test_gateway_panel_stopped():
    state = DashboardState(
        gateway=GatewayState(running=False, state="stopped"),
    )
    panel = render_panel(1, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "Stopped" in text or "stopped" in text


def test_gateway_panel_compact_shows_version_and_updates_behind():
    state = DashboardState(
        gateway=GatewayState(
            pid=12345,
            running=True,
            state="running",
            hermes_version="1.2.3",
            updates_behind=4,
        ),
    )
    panel = render_panel(1, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "v1.2.3" in text
    assert "(4 behind)" in text


def test_gateway_panel_detail_updates_behind_warns():
    state = DashboardState(
        gateway=GatewayState(
            pid=12345,
            running=True,
            state="running",
            hermes_version="1.2.3",
            updates_behind=2,
        ),
    )
    panel = render_panel(1, state, Theme(), detail=True)
    text = render_to_str(panel, width=80)
    assert "Hermes v1.2.3" in text
    assert "2 commits behind" in text
    assert "hermes update" in text


def test_gateway_panel_detail_stopped_up_to_date():
    state = DashboardState(
        gateway=GatewayState(
            running=False,
            state="stopped",
            hermes_version="1.2.3",
            updates_behind=0,
        ),
    )
    panel = render_panel(1, state, Theme(), detail=True)
    text = render_to_str(panel, width=80)
    assert "Stopped" in text
    assert "(up to date)" in text


def test_render_panel_invalid_number():
    state = DashboardState()
    panel = render_panel(99, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "Unknown panel" in text


def test_sessions_panel_compact():
    state = DashboardState(
        sessions=[
            SessionInfo(
                session_id="sess_001",
                source="cli",
                message_count=77,
                tool_call_count=51,
                is_active=False,
            ),
            SessionInfo(session_id="sess_002", source="telegram", message_count=47, is_active=True),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "#ss_001" in text
    assert "cli" in text


def test_tokens_panel_compact():
    state = DashboardState(
        tokens_today=TokenSummary(input_tokens=12400, output_tokens=8200, total_cost_usd=0.42),
        tokens_total=TokenSummary(input_tokens=45100, output_tokens=32000, total_cost_usd=2.18),
    )
    panel = render_panel(3, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert re.search(r"In:\s+12\.4K", text)
    assert "Today:~$0.42" in text


def test_tokens_panel_compact_reported_cost_uses_plain_prefix():
    state = DashboardState(
        tokens_today=TokenSummary(input_tokens=12400, total_cost_usd=0.42, cost_is_estimated=False),
        tokens_total=TokenSummary(input_tokens=45100, total_cost_usd=2.18, cost_is_estimated=False),
    )
    panel = render_panel(3, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "Today:$0.42" in text
    assert "Total:$2.18" in text
    assert "~$" not in text


def test_tokens_panel_compact_empty_state():
    state = DashboardState()
    panel = render_panel(3, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "Today:~$0.00" in text
    assert "Total:~$0.00" in text
    assert re.search(r"In:\s+0\b", text)


def test_tools_panel_compact():
    state = DashboardState(
        tool_stats=[
            ToolStats(name="shell_exec", call_count=23),
            ToolStats(name="web_search", call_count=18),
        ],
        total_tool_calls=89,
        available_tools=29,
    )
    panel = render_panel(4, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "shell_exec" in text


def test_config_panel_compact():
    state = DashboardState(
        config=ConfigSummary(
            model="gpt-5.4", provider="openai-codex", personality="kawaii", max_turns=192
        ),
    )
    panel = render_panel(5, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "gpt-5.4" in text


def test_config_panel_compact_empty_config():
    state = DashboardState()
    panel = render_panel(5, state, Theme(), detail=False)
    text = render_to_str(panel, width=80, no_color=True)
    assert "Config" in text
    assert "Model: —" in text
    assert "Provider: —" in text


def test_config_panel_detail_shows_moa_summary():
    state = DashboardState(
        config=ConfigSummary(
            moa_default_preset="council",
            moa_active_preset="council",
            moa_preset_count=1,
            moa_reference_model_count=2,
            moa_aggregator_label="openrouter/claude-opus",
            moa_save_traces=True,
        )
    )
    text = render_to_str(render_panel(5, state, Theme(), detail=True), width=120, no_color=True)
    assert "MoA" in text
    assert "council" in text
    assert "2 refs" in text
    assert "openrouter/claude-opus" in text
    assert "traces on" in text


def test_cron_panel_compact():
    state = DashboardState(cron=CronState(last_tick_ago_seconds=42.0, job_count=0))
    panel = render_panel(6, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "42s ago" in text


def test_cron_panel_detail_shows_provider_and_chronos_config():
    state = DashboardState(
        cron=CronState(
            provider="chronos",
            chronos_configured=True,
            chronos_portal_configured=True,
            chronos_callback_configured=True,
            chronos_audience_configured=True,
            chronos_jwks_configured=True,
            suggestion_count=2,
        )
    )
    text = render_to_str(render_panel(6, state, Theme(), detail=True), width=120, no_color=True)
    assert "provider=chronos" in text
    assert "chronos configured" in text
    assert "portal" in text
    assert "callback" in text
    assert "suggestions=2" in text


def test_overview_panel_compact():
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=70,
            skill_categories=28,
            providers=[ProviderInfo(name="openai-codex", is_active=True)],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=False)
    text = render_to_str(panel, width=80, no_color=True)
    assert "Skills: 70 (28 cat)" in text
    assert "Skills / Integrations" in text


def test_skills_panel_detail_shows_provider_freshness():
    state = DashboardState(
        skills_memory=SkillsMemory(
            credential_pools=[
                CredentialPoolEntry(
                    name="vertex",
                    label="Vertex",
                    auth_type="oauth",
                    source="adc",
                    token_present=True,
                    last_status="ok",
                    expires_at="2026-07-11T12:00:00Z",
                    last_refresh="2026-07-11T11:00:00Z",
                )
            ]
        )
    )
    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=220, no_color=True)
    assert "Expires" in text
    assert "Refreshed" in text
    assert "2026-07-11T12:00:00Z" in text


def test_logs_panel_compact():
    state = DashboardState(
        logs=LogState(
            agent_lines=[LogLine(timestamp="15:42:03", level="INFO", message="Session saved")]
        ),
    )
    panel = render_panel(8, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "Session saved" in text


def test_memory_panel_empty():
    state = DashboardState()
    panel = render_panel(10, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "Memory" in text


def test_memory_panel_detail_shows_learning_summary():
    state = DashboardState(
        memory=MemoryOverview(
            memory_file_count=2,
            skill_usage_count=2,
            learned_skill_count=1,
            pinned_skill_count=1,
            agent_created_skill_count=1,
            memory_card_count=3,
        )
    )
    text = render_to_str(render_panel(10, state, Theme(), detail=True), width=120, no_color=True)
    assert "Learning" in text
    assert "2 used skills" in text
    assert "1 learned" in text
    assert "3 memory cards" in text


def test_kanban_panel_compact():
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            task_count=5,
            run_count=3,
            stale_claim_count=1,
            dispatch_in_gateway=True,
            status_counts={"done": 4, "in_progress": 1},
        )
    )
    panel = render_panel(11, state, Theme(), detail=False)
    text = render_to_str(panel, width=80, no_color=True)
    assert "Tasks: 5" in text
    assert "Runs: 3" in text
    assert "Stale Claims: 1" in text
    assert "Dispatch: gateway" in text
    assert "in_progress: 1" in text


def test_kanban_panel_compact_empty():
    state = DashboardState()
    panel = render_panel(11, state, Theme(), detail=False)
    text = render_to_str(panel, width=80, no_color=True)
    assert "No kanban.db" in text


def test_kanban_panel_detail_empty():
    state = DashboardState()
    panel = render_panel(11, state, Theme(), detail=True)
    text = render_to_str(panel, width=80, no_color=True)
    assert "missing" in text
    assert "Kanban is not initialized" in text


def test_kanban_panel_detail():
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            task_count=2,
            run_count=1,
            event_count=1,
            comment_count=1,
            stale_claim_count=1,
            dispatch_in_gateway=False,
            board_count=1,
            boards=[
                KanbanBoardSummary(
                    slug="main",
                    current=True,
                    task_count=2,
                    run_count=1,
                    stale_claim_count=1,
                )
            ],
            status_counts={"blocked": 1, "in_progress": 1},
            active_tasks=[
                KanbanTaskSummary(
                    task_id="t_active",
                    title="Implement dashboard auth visibility",
                    status="in_progress",
                    assignee="coding",
                    worker_pid=4242,
                )
            ],
            problem_tasks=[
                KanbanTaskSummary(
                    task_id="t_blocked",
                    title="Fix worker profile",
                    status="blocked",
                    consecutive_failures=2,
                    last_failure_error="missing credentials",
                )
            ],
        )
    )
    panel = render_panel(11, state, Theme(), detail=True)
    text = render_to_str(panel, width=80)
    assert "Kanban" in text
    assert "t_active" in text
    assert "t_blocked" in text
    assert "Status Counts" in text
    assert "Stale Claims" in text
    assert "main" in text


def test_kanban_panel_detail_shows_branch_and_guarded_link_attachment_rows():
    completed_at = 1_775_791_440
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            task_count=1,
            link_count=3,
            attachment_count=2,
            task_links=[KanbanTaskLink(parent_id="t_goal", child_id="t_child")],
            active_tasks=[
                KanbanTaskSummary(
                    task_id="t_goal",
                    title="Decompose milestone",
                    status="in_progress",
                    branch_name="feature/goal",
                    completed_at=completed_at,
                    workspace_path="/work/repo",
                    goal_mode="autonomous",
                    current_step_key="step-3",
                )
            ],
        )
    )
    text = render_to_str(render_panel(11, state, Theme(), detail=True), width=120)
    assert "feature/goal" in text
    assert "/work/repo" in text
    assert "autonomous" in text
    assert "step-3" in text
    # Completed renders as an age label (e.g. "3h"), not a raw epoch.
    assert str(completed_at) not in text
    assert "Decomposition Tree" in text
    assert "t_child" in text
    assert "Decomposition Links" in text
    assert "Attachments" in text


def test_kanban_panel_detail_shows_completed_task_metadata():
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            recent_tasks=[
                KanbanTaskSummary(
                    task_id="t_done",
                    title="Completed task",
                    status="done",
                    completed_at=1_775_791_440,
                    workspace_path="/work/done",
                    goal_mode="guided",
                    current_step_key="final",
                )
            ],
        )
    )
    text = render_to_str(render_panel(11, state, Theme(), detail=True), width=120)
    assert "t_done" in text
    assert "/work/done" in text
    assert "guided" in text
    assert "final" in text


def test_kanban_panel_detail_metadata_table_dedups_task_across_lists():
    # A task present in both active_tasks and recent_tasks must render once in
    # the Task Metadata table, not duplicated.
    task = KanbanTaskSummary(
        task_id="t_dup",
        title="Dual-listed",
        status="in_progress",
        workspace_path="/work/dup",
        branch_name="feature/dup",
    )
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            active_tasks=[task],
            recent_tasks=[task],
        )
    )
    text = render_to_str(render_panel(11, state, Theme(), detail=True), width=120)
    assert text.count("/work/dup") == 1


def test_kanban_panel_detail_hides_link_attachment_rows_when_zero():
    state = DashboardState(kanban=KanbanState(db_present=True, task_count=0))
    text = render_to_str(render_panel(11, state, Theme(), detail=True), width=120)
    assert "Decomposition Links" not in text
    assert "Attachments" not in text


def test_kanban_panel_detail_shows_multi_board_summary():
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            task_count=3,
            board_count=3,
            current_board="alpha",
            boards=[
                KanbanBoardSummary(slug="root", task_count=1),
                KanbanBoardSummary(
                    slug="alpha",
                    current=True,
                    task_count=1,
                    problem_count=1,
                    block_kind_counts={"needs_input": 1},
                ),
                KanbanBoardSummary(slug="beta", task_count=1),
            ],
        )
    )
    text = render_to_str(render_panel(11, state, Theme(), detail=True), width=120, no_color=True)
    assert "Boards" in text
    assert "alpha" in text
    assert "current" in text
    assert "needs_input:1" in text


def test_kanban_panel_detail_recent_runs():
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            run_count=2,
            recent_runs=[
                KanbanRunSummary(
                    run_id=7,
                    task_id="t_done",
                    profile="coding",
                    status="done",
                    outcome="success",
                ),
                KanbanRunSummary(
                    run_id=8,
                    task_id="t_failed",
                    status="failed",
                    error="worker crashed with missing credentials",
                ),
            ],
        )
    )
    panel = render_panel(11, state, Theme(), detail=True)
    text = render_to_str(panel, width=80, no_color=True)
    assert "Recent Runs" in text
    assert "t_done" in text
    assert "coding" in text
    assert "success" in text
    assert "t_failed" in text
    assert "worker crashed" in text


def test_kanban_panel_detail_heartbeat_age_labels():
    now = int(time.time())
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            task_count=3,
            active_tasks=[
                KanbanTaskSummary(
                    task_id="t_seconds", status="in_progress", last_heartbeat_at=now - 5
                ),
                KanbanTaskSummary(
                    task_id="t_minutes", status="in_progress", last_heartbeat_at=now - 300
                ),
                KanbanTaskSummary(
                    task_id="t_hours", status="in_progress", last_heartbeat_at=now - 7200
                ),
            ],
        )
    )
    panel = render_panel(11, state, Theme(), detail=True)
    text = render_to_str(panel, width=80, no_color=True)
    assert re.search(r"\b\d+s\b", text)
    assert "5m" in text
    assert "2h" in text


def test_operations_panel_compact():
    state = DashboardState(
        operations=OperationsState(
            dashboard_process_count=2,
            model_caches=[
                ModelCacheSummary(name="models_dev_cache.json", provider_count=2, model_count=7)
            ],
            pr_monitors=[PRMonitorSummary(filename="pr-monitor.json")],
        )
    )
    panel = render_panel(12, state, Theme(), detail=False)
    text = render_to_str(panel, width=80, no_color=True)
    assert "Dashboard: 2 proc" in text
    assert "1 files  7 models" in text
    assert "PR Monitors: 1" in text


def test_operations_panel_compact_empty():
    state = DashboardState()
    panel = render_panel(12, state, Theme(), detail=False)
    text = render_to_str(panel, width=80, no_color=True)
    assert "Dashboard: 0 proc" in text
    assert "0 files  0 models" in text
    assert "PR Monitors: 0" in text


def test_operations_panel_detail_empty():
    state = DashboardState()
    panel = render_panel(12, state, Theme(), detail=True)
    text = render_to_str(panel, width=80, no_color=True)
    assert "No operations artifacts found" in text


def test_operations_panel_detail():
    state = DashboardState(
        operations=OperationsState(
            dashboard_process_count=1,
            desktop_build_stamp="desktop-2026.6.14",
            model_caches=[
                ModelCacheSummary(
                    name="models_dev_cache.json",
                    provider_count=2,
                    model_count=3,
                    size_bytes=1200,
                )
            ],
            pr_monitors=[
                PRMonitorSummary(
                    filename="pr-monitor-nousresearch-hermes-agent.json",
                    repo="NousResearch/hermes-agent",
                    monitored_count=2,
                    tracked_count=2,
                    author_pr_count=1,
                )
            ],
        )
    )
    panel = render_panel(12, state, Theme(), detail=True)
    text = render_to_str(panel, width=80)
    assert "Operations" in text
    assert "desktop-2026.6.14" in text
    assert "models_dev_cache.json" in text
    assert "PR Monitors" in text


def test_operations_panel_detail_shows_response_store():
    state = DashboardState(
        operations=OperationsState(
            response_store_present=True,
            conversation_count=2,
            response_count=3,
            response_store_size_bytes=20480,
        )
    )
    text = render_to_str(render_panel(12, state, Theme(), detail=True), width=100, no_color=True)
    assert "Response Store" in text
    assert "2 conversations" in text
    assert "3 responses" in text
    assert "20.5K" in text
    assert "No operations artifacts found" not in text


def test_operations_panel_detail_shows_verification_evidence():
    state = DashboardState(
        operations=OperationsState(
            verification_db_present=True,
            verification_event_count=2,
            verification_failed_count=1,
            verification_state_count=1,
            verification_latest_events=[
                VerificationEventSummary(
                    event_id=2,
                    created_at="2026-07-10T10:05:00Z",
                    session_id="sess-a",
                    root="/repo",
                    command="uv run ruff check .",
                    canonical_command="ruff check",
                    kind="lint",
                    scope="full",
                    status="failed",
                    exit_code=1,
                    output_summary="F401 unused import",
                )
            ],
            verification_roots=[
                VerificationRootSummary(
                    session_id="sess-a",
                    root="/repo",
                    last_event_id=2,
                    last_edit_at="2026-07-10T10:06:00Z",
                    changed_path_count=2,
                )
            ],
        )
    )
    text = render_to_str(render_panel(12, state, Theme(), detail=True), width=120, no_color=True)
    assert "Verification Evidence" in text
    assert "2 events" in text
    assert "1 failed" in text
    assert "ruff check" in text
    assert "F401 unused import" in text
    assert "2 changed" in text
    assert "No operations artifacts found" not in text


def test_operations_panel_detail_shows_moa_trace_metadata():
    state = DashboardState(
        operations=OperationsState(
            moa_trace_count=2,
            moa_trace_size_bytes=2048,
            moa_trace_newest_session_id="sess-moa",
            moa_trace_newest_mtime=time.time() - 60,
            moa_trace_latest_record_summary="ok council",
            moa_trace_latest_record_keys=["preset", "status"],
        )
    )
    text = render_to_str(render_panel(12, state, Theme(), detail=True), width=120, no_color=True)
    assert "MoA Traces" in text
    assert "sess-moa" in text
    assert "Latest Record" in text
    assert "ok council" in text
    assert "Latest Keys" in text
    assert "preset, status" in text


def test_operations_panel_detail_shows_projects_and_discovered_repos():
    state = DashboardState(
        operations=OperationsState(
            projects_db_present=True,
            project_count=2,
            project_archived_count=1,
            project_folder_count=3,
            discovered_repo_count=4,
            project_missing_primary_path_count=1,
            projects=[
                ProjectSummary(
                    slug="hermesd",
                    name="hermesd",
                    board_slug="main",
                    primary_path="/repo/hermesd",
                )
            ],
            discovered_repos=[
                DiscoveredRepoSummary(
                    root="/repo/hermesd",
                    label="hermesd",
                    last_seen="2026-07-12T00:00:00Z",
                )
            ],
        )
    )
    text = render_to_str(render_panel(12, state, Theme(), detail=True), width=120, no_color=True)
    assert "Projects" in text
    assert re.search(r"Projects\s+2 projects\s+1 archived\s+3 folders\s+4 discovered", text)
    assert re.search(r"Projects\s+.*1 missing paths", text)
    assert "/repo/hermesd" in text
    assert "board present" not in text
    assert "Newest Discovered Repos" in text
    assert "2026-07-12T00:00:00Z" in text


def test_operations_panel_detail_shows_goals_and_project_correlations():
    state = DashboardState(
        operations=OperationsState(
            goal_count=1,
            active_goal_count=1,
            waiting_goal_count=1,
            goals=[
                GoalSummary(
                    session_id="sess-goal",
                    goal="Ship visibility",
                    status="active",
                    turns_used=3,
                    max_turns=8,
                    has_contract=True,
                    waiting_on_pid=4242,
                    waiting_reason="tests running",
                    subgoal_count=1,
                )
            ],
            projects_db_present=True,
            project_count=1,
            projects=[
                ProjectSummary(
                    slug="hermesd",
                    name="hermesd",
                    board_slug="main",
                    primary_path="/repo/hermesd",
                    verification_root_count=2,
                    kanban_board_present=True,
                )
            ],
        )
    )
    text = render_to_str(render_panel(12, state, Theme(), detail=True), width=120, no_color=True)
    assert "Goals" in text
    assert "Ship visibility" in text
    assert "3/8" in text
    assert "contract" in text
    assert "pid 4242" in text
    assert "2 verified roots" in text
    assert "board present" in text


def test_operations_panel_detail_size_and_age_labels():
    now = time.time()
    state = DashboardState(
        operations=OperationsState(
            model_caches=[
                ModelCacheSummary(
                    name="big_cache.json", model_count=1, size_bytes=2_500_000, mtime=now - 5
                ),
                ModelCacheSummary(
                    name="small_cache.json", model_count=1, size_bytes=500, mtime=now - 300
                ),
                ModelCacheSummary(
                    name="old_cache.json", model_count=1, size_bytes=1200, mtime=now - 7200
                ),
            ],
        )
    )
    panel = render_panel(12, state, Theme(), detail=True)
    text = render_to_str(panel, width=80, no_color=True)
    assert "2.5M" in text
    assert "500" in text
    assert "1.2K" in text
    assert re.search(r"\b\d+s\b", text)
    assert "5m" in text
    assert "2h" in text
