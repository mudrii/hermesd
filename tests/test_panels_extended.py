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
from hermesd.theme import Theme
from hermesd.panels import render_panel


def _render_to_str(panel) -> str:
    console = Console(width=120, force_terminal=True)
    with console.capture() as cap:
        console.print(panel)
    return cap.get()


# ── Detail view tests ──────────────────────────────────────────────────


def test_sessions_panel_detail():
    state = DashboardState(
        sessions=[
            SessionInfo(
                session_id="sess_001", source="cli", model="gpt-5.4",
                message_count=77, tool_call_count=51,
                input_tokens=12400, output_tokens=8200,
                estimated_cost_usd=0.42, is_active=True,
            ),
            SessionInfo(
                session_id="sess_002", source="telegram", model="",
                message_count=47, tool_call_count=14,
                input_tokens=9100, output_tokens=6300,
                estimated_cost_usd=0.31,
            ),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "cli" in text
    assert "telegram" in text
    assert "0.42" in text


def test_tokens_panel_detail():
    state = DashboardState(
        sessions=[
            SessionInfo(
                session_id="s1", input_tokens=12400, output_tokens=8200,
                cache_read_tokens=28300, cache_write_tokens=5000,
                reasoning_tokens=1000, estimated_cost_usd=0.42,
            ),
        ],
    )
    panel = render_panel(3, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "12" in text
    assert "0.42" in text


def test_tools_panel_detail():
    state = DashboardState(
        tool_stats=[
            ToolStats(name="shell_exec", call_count=23),
            ToolStats(name="web_search", call_count=18),
            ToolStats(name="read_file", call_count=7),
        ],
    )
    panel = render_panel(4, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "shell_exec" in text
    assert "23" in text
    assert "read_file" in text


def test_config_panel_detail():
    state = DashboardState(
        config=ConfigSummary(
            model="gpt-5.4", provider="openai-codex",
            personality="kawaii", max_turns=192,
            reasoning_effort="medium",
            compression_threshold=0.86,
            security_redact=True,
            approvals_mode="manual",
        ),
    )
    panel = render_panel(5, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "gpt-5.4" in text
    assert "192" in text
    assert "medium" in text


def test_overview_panel_detail():
    from hermesd.models import SkillInfo
    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=70, skill_categories=28, memory_file_count=3,
            providers=[
                ProviderInfo(name="openai-codex", is_active=True),
                ProviderInfo(name="anthropic", is_active=False),
            ],
            skills=[SkillInfo(name="dev-lint", category="dev")],
        ),
    )
    panel = render_panel(7, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "openai-codex" in text
    assert "dev" in text


def test_logs_panel_detail_agent():
    state = DashboardState(
        logs=LogState(
            agent_lines=[LogLine(timestamp="15:42:03", level="INFO", message="Session saved")],
            gateway_lines=[LogLine(timestamp="15:40:00", level="INFO", message="Connected")],
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, log_sub_view="agent")
    text = _render_to_str(panel)
    assert "Session saved" in text
    assert "[agent]" in text


def test_logs_panel_detail_gateway():
    state = DashboardState(
        logs=LogState(
            agent_lines=[],
            gateway_lines=[LogLine(timestamp="15:40:00", level="INFO", message="Connected")],
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, log_sub_view="gateway")
    text = _render_to_str(panel)
    assert "Connected" in text
    assert "[gateway]" in text


def test_logs_panel_detail_errors():
    state = DashboardState(
        logs=LogState(
            error_lines=[LogLine(timestamp="14:00:00", level="ERROR", message="Crash")],
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, log_sub_view="errors")
    text = _render_to_str(panel)
    assert "Crash" in text
    assert "[errors]" in text


# ── Empty state tests ──────────────────────────────────────────────────


def test_sessions_panel_empty():
    state = DashboardState(sessions=[])
    panel = render_panel(2, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "Sessions" in text
    assert "0 active" in text


def test_tools_panel_empty():
    state = DashboardState(tool_stats=[], total_tool_calls=0, available_tools=0)
    panel = render_panel(4, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "0 available" in text


def test_logs_panel_empty():
    state = DashboardState(logs=LogState())
    panel = render_panel(8, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "Logs" in text


def test_cron_panel_no_tick():
    state = DashboardState(cron=CronState())
    panel = render_panel(6, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "Cron" in text


def test_overview_panel_no_providers():
    state = DashboardState(skills_memory=SkillsMemory())
    panel = render_panel(7, state, Theme(), detail=False)
    text = _render_to_str(panel)
    assert "0" in text


# ── Skin tests ─────────────────────────────────────────────────────────


def test_panels_render_with_ares_skin():
    state = DashboardState(
        gateway=GatewayState(running=True, pid=123, platforms=[
            PlatformStatus(name="telegram", state="connected"),
        ]),
    )
    panel = render_panel(1, state, Theme("ares"), detail=False)
    text = _render_to_str(panel)
    assert "Running" in text
