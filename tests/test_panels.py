from __future__ import annotations

import re
import time

from hermesd.models import (
    ConfigSummary,
    CronState,
    DashboardState,
    GatewayState,
    KanbanRunSummary,
    KanbanState,
    KanbanTaskSummary,
    LogLine,
    LogState,
    ModelCacheSummary,
    OperationsState,
    PlatformStatus,
    PRMonitorSummary,
    ProviderInfo,
    SessionInfo,
    SkillsMemory,
    TokenSummary,
    ToolStats,
)
from hermesd.panels import _RENDERERS, PANEL_NAMES, render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_panel_registry_matches_names():
    assert set(_RENDERERS) == set(PANEL_NAMES)


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
    text = render_to_str(panel, width=80)
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
                ChannelPlatformInfo(name="telegram", entry_count=1, connected=True),
                ChannelPlatformInfo(
                    name="feishu",
                    entry_count=0,
                    capabilities=["meeting invites"],
                ),
            ],
        ),
    )
    panel = render_panel(1, state, Theme(), detail=True)
    text = render_to_str(panel, width=80)
    assert "telegram" in text.lower()
    assert "Channel Directory" in text
    assert "feishu" in text
    assert "meeting invites" in text


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


def test_cron_panel_compact():
    state = DashboardState(cron=CronState(last_tick_ago_seconds=42.0, job_count=0))
    panel = render_panel(6, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "42s ago" in text


def test_overview_panel_compact():
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=70,
            skill_categories=28,
            providers=[ProviderInfo(name="openai-codex", is_active=True)],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=False)
    text = render_to_str(panel, width=80)
    assert "70" in text
    assert "Skills / Integrations" in text


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


def test_kanban_panel_compact():
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            task_count=5,
            run_count=3,
            dispatch_in_gateway=True,
            status_counts={"done": 4, "in_progress": 1},
        )
    )
    panel = render_panel(11, state, Theme(), detail=False)
    text = render_to_str(panel, width=80, no_color=True)
    assert "Tasks: 5" in text
    assert "Runs: 3" in text
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
            dispatch_in_gateway=False,
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
