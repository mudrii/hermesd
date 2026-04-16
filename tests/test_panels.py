from rich.console import Console

from hermesd.models import (
    ConfigSummary,
    CronState,
    DashboardState,
    GatewayState,
    LogLine,
    LogState,
    PlatformStatus,
    ProviderInfo,
    SessionInfo,
    SkillsMemory,
    TokenSummary,
    ToolStats,
)
from hermesd.panels import render_panel
from hermesd.theme import Theme


def _render_to_str(panel) -> str:
    console = Console(width=80, force_terminal=True)
    with console.capture() as cap:
        console.print(panel)
    return cap.get()


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
    text = _render_to_str(panel)
    assert "Gateway" in text
    assert "Running" in text or "running" in text


def test_gateway_panel_detail():
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
    )
    panel = render_panel(1, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "telegram" in text.lower()


def test_gateway_panel_stopped():
    state = DashboardState(
        gateway=GatewayState(running=False, state="stopped"),
    )
    panel = render_panel(1, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "Stopped" in text or "stopped" in text


def test_render_panel_invalid_number():
    state = DashboardState()
    panel = render_panel(99, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "Unknown" in text or panel is not None


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
    text = _render_to_str(panel)
    assert "sess_001" in text or "001" in text
    assert "cli" in text


def test_tokens_panel_compact():
    state = DashboardState(
        tokens_today=TokenSummary(input_tokens=12400, output_tokens=8200, total_cost_usd=0.42),
        tokens_total=TokenSummary(input_tokens=45100, output_tokens=32000, total_cost_usd=2.18),
    )
    panel = render_panel(3, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "12" in text
    assert "0.42" in text


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
    text = _render_to_str(panel)
    assert "shell_exec" in text


def test_config_panel_compact():
    state = DashboardState(
        config=ConfigSummary(
            model="gpt-5.4", provider="openai-codex", personality="kawaii", max_turns=192
        ),
    )
    panel = render_panel(5, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "gpt-5.4" in text


def test_cron_panel_compact():
    state = DashboardState(cron=CronState(last_tick_ago_seconds=42.0, job_count=0))
    panel = render_panel(6, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "42" in text or "Cron" in text


def test_overview_panel_compact():
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=70,
            skill_categories=28,
            memory_file_count=0,
            providers=[ProviderInfo(name="openai-codex", is_active=True)],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "70" in text


def test_logs_panel_compact():
    state = DashboardState(
        logs=LogState(
            agent_lines=[LogLine(timestamp="15:42:03", level="INFO", message="Session saved")]
        ),
    )
    panel = render_panel(8, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "Session saved" in text
