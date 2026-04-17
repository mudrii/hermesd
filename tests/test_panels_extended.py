from rich.console import Console

from hermesd.models import (
    CheckpointInfo,
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
    TokenAnalytics,
    TokenBreakdown,
    TokenWindowSummary,
    ToolGatewayRoute,
    ToolStats,
)
from hermesd.panels import render_panel
from hermesd.panels.sessions import extract_message_search_query
from hermesd.theme import Theme


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
                session_id="sess_001",
                source="cli",
                model="gpt-5.4",
                message_count=77,
                tool_call_count=51,
                input_tokens=12400,
                output_tokens=8200,
                estimated_cost_usd=0.42,
                billing_provider="openai-codex",
                cost_status="reported",
                pricing_version="2026-04",
                is_active=True,
            ),
            SessionInfo(
                session_id="sess_002",
                source="telegram",
                model="",
                message_count=47,
                tool_call_count=14,
                input_tokens=9100,
                output_tokens=6300,
                estimated_cost_usd=0.31,
                billing_provider="anthropic",
                cost_status="estimated",
                pricing_version="2026-05",
            ),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "cli" in text
    assert "telegram" in text
    assert "0.42" in text
    assert "openai-codex" in text
    assert "reported" in text
    assert "2026-04" in text


def test_sessions_panel_detail_filter_query():
    state = DashboardState(
        sessions=[
            SessionInfo(session_id="sess_alpha", source="cli", model="gpt-5.4"),
            SessionInfo(session_id="sess_beta", source="telegram", model="claude"),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True, filter_query="telegram")
    text = _render_to_str(panel)
    assert "Filter:" in text
    assert "telegram" in text
    assert "claude" in text
    assert "gpt-5.4" not in text


def test_sessions_panel_detail_structured_filter_and_sort():
    state = DashboardState(
        sessions=[
            SessionInfo(
                session_id="sess_alpha",
                source="cli",
                model="gpt-5.4",
                billing_provider="openai",
                estimated_cost_usd=1.20,
                started_at=10,
            ),
            SessionInfo(
                session_id="sess_beta",
                source="telegram",
                model="claude",
                billing_provider="anthropic",
                estimated_cost_usd=0.20,
                started_at=20,
            ),
        ],
    )
    panel = render_panel(
        2,
        state,
        Theme(),
        detail=True,
        filter_query="source:cli provider:openai",
        session_sort="cost",
    )
    text = _render_to_str(panel)
    assert "source:cli provider:openai" in text
    assert "Sort:" in text
    assert "sess_alpha"[-8:] in text
    assert "telegram" not in text


def test_sessions_panel_detail_message_filter():
    state = DashboardState(
        sessions=[
            SessionInfo(session_id="sess_alpha", source="cli", model="gpt-5.4"),
            SessionInfo(session_id="sess_beta", source="telegram", model="claude"),
        ],
    )
    panel = render_panel(
        2,
        state,
        Theme(),
        detail=True,
        filter_query="message:timeout",
        session_message_match_ids={"sess_beta"},
    )
    text = _render_to_str(panel)
    assert "message:timeout" in text
    assert "sess_beta"[-8:] in text
    assert "sess_alpha"[-8:] not in text


def test_extract_message_search_query_uses_last_message_term():
    assert extract_message_search_query("message:timeout message:retry") == "retry"


def test_sessions_panel_detail_shows_parent_session_id():
    state = DashboardState(
        sessions=[
            SessionInfo(
                session_id="child_session",
                source="cli",
                model="gpt-5.4",
                parent_session_id="parent_sess",
            )
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "ent_sess" in text


def test_tokens_panel_detail():
    state = DashboardState(
        sessions=[
            SessionInfo(
                session_id="s1",
                input_tokens=12400,
                output_tokens=8200,
                cache_read_tokens=28300,
                cache_write_tokens=5000,
                reasoning_tokens=1000,
                estimated_cost_usd=0.42,
            ),
        ],
        token_analytics=TokenAnalytics(
            windows=[
                TokenWindowSummary(
                    label="7d", session_count=1, input_tokens=12400, cache_ratio=0.70
                ),
                TokenWindowSummary(
                    label="30d", session_count=2, input_tokens=21500, cache_ratio=0.57
                ),
            ],
            by_model=[
                TokenBreakdown(
                    label="gpt-5.4", session_count=1, input_tokens=12400, total_cost_usd=0.42
                ),
            ],
            by_provider=[
                TokenBreakdown(
                    label="openai-codex",
                    session_count=1,
                    input_tokens=12400,
                    total_cost_usd=0.42,
                ),
            ],
        ),
    )
    panel = render_panel(3, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "12" in text
    assert "0.42" in text
    assert "Recent Windows" in text
    assert "7d" in text
    assert "By Model" in text
    assert "gpt-5.4" in text
    assert "By Provider" in text
    assert "openai-codex" in text


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


def test_tools_panel_detail_shows_background_processes():
    from hermesd.models import BackgroundProcessInfo

    state = DashboardState(
        background_processes=[
            BackgroundProcessInfo(
                session_id="proc_alpha",
                command="pytest -q",
                pid=4242,
                started_at=1775791440.0,
                notify_on_complete=True,
                watcher_interval=30,
                watch_patterns=["ERROR", "READY"],
            )
        ]
    )
    panel = render_panel(4, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "Background Processes" in text
    assert "proc_alpha" in text
    assert "pytest -q" in text
    assert "Yes" in text
    assert "2 @30s" in text


def test_tools_panel_detail_shows_checkpoints():
    state = DashboardState(
        checkpoints=[
            CheckpointInfo(
                repo_id="abc123def4567890",
                workdir="/tmp/project-alpha",
                workdir_name="project-alpha",
                commit_count=2,
                last_reason="Refine config panel",
                last_checkpoint_at=1775791450.0,
            )
        ]
    )
    panel = render_panel(4, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "Checkpoints" in text
    assert "project-alpha" in text
    assert "Refine config panel" in text
    assert "2" in text


def test_config_panel_detail():
    state = DashboardState(
        config=ConfigSummary(
            model="gpt-5.4",
            provider="openai-codex",
            personality="kawaii",
            max_turns=192,
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


def test_config_panel_detail_shows_tool_gateway_routes():
    state = DashboardState(
        config=ConfigSummary(
            model="gpt-5.4",
            provider="openai-codex",
            provider_routing_summary="throughput only:2",
            smart_model_routing_enabled=True,
            smart_model_routing_cheap_model="openrouter/google/gemini-2.5-flash",
            fallback_model_label="anthropic/claude-sonnet-4-20250514",
            dashboard_theme="midnight",
            session_reset_mode="both",
            memory_provider="supermemory",
            tool_gateway_domain="gateway.example.com",
            tool_gateway_scheme="https",
            tool_gateway_routes=[
                ToolGatewayRoute(tool="web", mode="gateway", token_present=True),
                ToolGatewayRoute(tool="image_gen", mode="direct", token_present=True),
            ],
            firecrawl_gateway_url="https://firecrawl.example.com",
        ),
    )
    panel = render_panel(5, state, Theme(), detail=True)
    text = _render_to_str(panel)
    assert "Tool Gateway" in text
    assert "dashboard-local env" in text
    assert "gateway.example.com" in text
    assert "https" in text
    assert "web" in text
    assert "image_gen" in text
    assert "gateway" in text
    assert "direct" in text
    assert "firecrawl.example.com" in text
    assert "throughput only:2" in text
    assert "openrouter/google/gemini-2.5-flash" in text
    assert "anthropic/claude-sonnet-4-20250514" in text
    assert "midnight" in text
    assert "supermemory" in text


def test_overview_panel_detail():
    from hermesd.models import SkillInfo

    state = DashboardState(
        skills_memory=SkillsMemory(
            skill_count=70,
            skill_categories=28,
            memory_file_count=3,
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


def test_logs_panel_detail_cron():
    state = DashboardState(
        logs=LogState(
            cron_lines=[LogLine(message="cron output line")],
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, log_sub_view="cron")
    text = _render_to_str(panel)
    assert "cron output line" in text
    assert "[cron]" in text


def test_logs_panel_detail_scroll_offset():
    state = DashboardState(
        logs=LogState(
            agent_lines=[
                LogLine(timestamp=f"15:42:{i:02d}", level="INFO", message=f"line-{i}")
                for i in range(12)
            ]
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, log_sub_view="agent", scroll_offset=5)
    text = _render_to_str(panel)
    assert "line-0" not in text
    assert "line-5" in text
    assert "j/k scroll" in text


def test_logs_panel_detail_filter_query():
    state = DashboardState(
        logs=LogState(
            agent_lines=[
                LogLine(
                    timestamp="15:42:03",
                    component="hermes",
                    level="INFO",
                    session_id="sess-alpha",
                    message="session saved",
                ),
                LogLine(
                    timestamp="15:42:04",
                    component="gateway",
                    level="ERROR",
                    session_id="sess-beta",
                    message="provider timeout",
                ),
            ]
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, log_sub_view="agent", filter_query="error")
    text = _render_to_str(panel)
    assert "Filter:" in text
    assert "provider timeout" in text
    assert "session saved" not in text


def test_logs_panel_detail_structured_filter_query():
    state = DashboardState(
        logs=LogState(
            agent_lines=[
                LogLine(
                    timestamp="15:42:03",
                    component="hermes",
                    level="INFO",
                    session_id="sess-alpha",
                    message="session saved",
                ),
                LogLine(
                    timestamp="15:42:04",
                    component="gateway",
                    level="ERROR",
                    session_id="sess-beta",
                    message="provider timeout",
                ),
            ]
        ),
    )
    panel = render_panel(
        8,
        state,
        Theme(),
        detail=True,
        log_sub_view="agent",
        filter_query="level:error component:gateway session:sess-beta",
    )
    text = _render_to_str(panel)
    assert "provider timeout" in text
    assert "session saved" not in text


def test_logs_panel_detail_minlevel_filter_query():
    state = DashboardState(
        logs=LogState(
            agent_lines=[
                LogLine(
                    timestamp="15:42:03",
                    component="hermes",
                    level="INFO",
                    session_id="sess-alpha",
                    message="session saved",
                ),
                LogLine(
                    timestamp="15:42:04",
                    component="gateway",
                    level="WARNING",
                    session_id="sess-beta",
                    message="provider slow",
                ),
                LogLine(
                    timestamp="15:42:05",
                    component="gateway",
                    level="ERROR",
                    session_id="sess-beta",
                    message="provider timeout",
                ),
            ]
        ),
    )
    panel = render_panel(
        8,
        state,
        Theme(),
        detail=True,
        log_sub_view="agent",
        filter_query="minlevel:warning",
    )
    text = _render_to_str(panel)
    assert "provider slow" in text
    assert "provider timeout" in text
    assert "session saved" not in text


def test_logs_panel_detail_invalid_minlevel_matches_nothing():
    state = DashboardState(
        logs=LogState(
            agent_lines=[
                LogLine(
                    timestamp="15:42:03", component="hermes", level="INFO", message="session saved"
                ),
                LogLine(
                    timestamp="15:42:04",
                    component="gateway",
                    level="ERROR",
                    message="provider timeout",
                ),
            ]
        ),
    )
    panel = render_panel(
        8,
        state,
        Theme(),
        detail=True,
        log_sub_view="agent",
        filter_query="minlevel:warnning",
    )
    text = _render_to_str(panel)
    assert "No matching log lines" in text
    assert "session saved" not in text
    assert "provider timeout" not in text


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
        gateway=GatewayState(
            running=True,
            pid=123,
            platforms=[
                PlatformStatus(name="telegram", state="connected"),
            ],
        ),
    )
    panel = render_panel(1, state, Theme("ares"), detail=False)
    text = _render_to_str(panel)
    assert "Running" in text
