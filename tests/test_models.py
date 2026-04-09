import time

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


def test_dashboard_state_defaults():
    state = DashboardState()
    assert state.gateway.running is False
    assert state.sessions == []
    assert state.tokens_today.input_tokens == 0
    assert state.tool_stats == []
    assert state.config.model == ""
    assert state.cron.job_count == 0
    assert state.skills_memory.skill_count == 0
    assert state.logs.agent_lines == []
    assert state.active_skin == "default"


def test_gateway_state_with_platforms():
    gw = GatewayState(
        pid=12345,
        running=True,
        state="running",
        platforms=[
            PlatformStatus(name="telegram", state="connected"),
            PlatformStatus(name="discord", state="disconnected"),
        ],
    )
    assert gw.pid == 12345
    assert len(gw.platforms) == 2
    assert gw.platforms[0].state == "connected"


def test_session_info():
    s = SessionInfo(
        session_id="sess_001",
        source="cli",
        model="gpt-5.4",
        message_count=77,
        tool_call_count=51,
        input_tokens=12400,
        output_tokens=8200,
        estimated_cost_usd=0.42,
        started_at=time.time() - 3600,
        is_active=True,
    )
    assert s.session_id == "sess_001"
    assert s.is_active is True
    assert s.ended_at is None


def test_token_summary():
    t = TokenSummary(input_tokens=12400, output_tokens=8200, total_cost_usd=0.42)
    assert t.cache_read_tokens == 0
    assert t.total_cost_usd == 0.42


def test_tool_stats():
    ts = ToolStats(name="shell_exec", call_count=23)
    assert ts.name == "shell_exec"


def test_config_summary():
    c = ConfigSummary(
        model="gpt-5.4", provider="openai-codex",
        personality="kawaii", max_turns=192,
    )
    assert c.personality == "kawaii"


def test_cron_state_no_tick():
    c = CronState()
    assert c.last_tick_ago_seconds is None


def test_skills_memory_with_providers():
    sm = SkillsMemory(
        skill_count=70,
        skill_categories=28,
        providers=[
            ProviderInfo(name="openai-codex", is_active=True),
            ProviderInfo(name="anthropic"),
        ],
    )
    assert sm.providers[0].is_active is True
    assert sm.providers[1].is_active is False


def test_log_line():
    ll = LogLine(timestamp="15:42:03", level="INFO", message="Session saved")
    assert ll.level == "INFO"


def test_log_state():
    ls = LogState(
        agent_lines=[LogLine(timestamp="15:42:03", level="INFO", message="test")],
    )
    assert len(ls.agent_lines) == 1
    assert ls.gateway_lines == []


def test_dashboard_state_is_stale():
    state = DashboardState(is_stale=True)
    assert state.is_stale is True
