from __future__ import annotations

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
    TokenSummary,
    TokenWindowSummary,
    ToolGatewayRoute,
    ToolStats,
)
from hermesd.panels import render_panel
from hermesd.panels.sessions import extract_message_search_query
from hermesd.theme import Theme
from tests.conftest import render_to_str

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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
    assert "message:timeout" in text
    assert "sess_beta"[-8:] in text
    assert "sess_alpha"[-8:] not in text


def test_sessions_panel_detail_multiple_message_tokens_last_wins():
    # Last occurrence wins everywhere: only the final message: value is
    # applied, matching extract_message_search_query.
    state = DashboardState(
        sessions=[
            SessionInfo(session_id="sess_alpha", source="cli"),
            SessionInfo(session_id="sess_beta", source="telegram"),
        ],
    )
    panel = render_panel(
        2,
        state,
        Theme(),
        detail=True,
        filter_query="message:foo message:bar",
        session_message_match_ids={"sess_beta"},
    )
    text = render_to_str(panel)
    assert "sess_beta"[-8:] in text
    assert "sess_alpha"[-8:] not in text


def test_sessions_panel_detail_active_false_filter():
    state = DashboardState(
        sessions=[
            SessionInfo(session_id="sess_active", source="cli", is_active=True),
            SessionInfo(session_id="sess_done", source="cli", ended_at=1.0, is_active=False),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True, filter_query="active:false")
    text = render_to_str(panel)
    assert "ss_active" not in text
    assert "sess_done"[-8:] in text


def test_sessions_panel_filter_exact_fields_duplicate_keys_and_invalid_active():
    state = DashboardState(
        sessions=[
            SessionInfo(session_id="sess_cli", source="cli", is_active=True),
            SessionInfo(session_id="sess_cli_tool", source="cli-tool", is_active=True),
            SessionInfo(session_id="sess_gateway", source="gateway", is_active=False),
        ],
    )

    exact_panel = render_panel(2, state, Theme(), detail=True, filter_query="source:cli")
    exact_text = render_to_str(exact_panel)
    assert "sess_cli"[-8:] in exact_text
    assert "cli-tool" not in exact_text

    duplicate_panel = render_panel(
        2,
        state,
        Theme(),
        detail=True,
        filter_query="source:cli source:gateway",
    )
    assert "No matching sessions" in render_to_str(duplicate_panel)

    invalid_active_panel = render_panel(2, state, Theme(), detail=True, filter_query="active:t")
    assert "No matching sessions" in render_to_str(invalid_active_panel)


def test_sessions_panel_formats_negative_cost_and_recent_tiebreaker():
    state = DashboardState(
        sessions=[
            SessionInfo(session_id="sess_b", source="cli", started_at=10, estimated_cost_usd=0.1),
            SessionInfo(session_id="sess_a", source="cli", started_at=10, estimated_cost_usd=-0.5),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True)
    text = render_to_str(panel)
    assert "-$0.50" in text
    assert text.find("sess_b") < text.find("sess_a")


def test_sessions_panel_detail_unknown_structured_filter_matches_as_text():
    state = DashboardState(
        sessions=[
            SessionInfo(session_id="sess_alpha", source="cli", model="gpt-5.4"),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True, filter_query="unknown:value")
    text = render_to_str(panel)
    assert "No matching sessions" in text
    assert "ss_alpha" not in text


def test_sessions_panel_detail_text_filter_and_token_sort_order():
    state = DashboardState(
        sessions=[
            SessionInfo(
                session_id="sess_low",
                title="deploy task",
                input_tokens=10,
                output_tokens=5,
                started_at=20,
            ),
            SessionInfo(
                session_id="sess_high",
                title="deploy task",
                input_tokens=100,
                output_tokens=50,
                started_at=10,
            ),
        ],
    )
    panel = render_panel(
        2, state, Theme(), detail=True, filter_query="text:deploy", session_sort="tokens"
    )
    text = render_to_str(panel)
    assert text.find("ss_high") < text.find("sess_low")


def test_extract_message_search_query_uses_last_message_term():
    assert extract_message_search_query("message:timeout message:retry") == "retry"


def test_extract_message_search_query_ignores_plain_terms():
    assert extract_message_search_query("timeout message:retry source:cli") == "retry"


def test_sessions_panel_detail_archived_filter():
    state = DashboardState(
        sessions=[
            SessionInfo(session_id="sess_archived", source="cli", archived=True),
            SessionInfo(session_id="sess_live", source="cli", archived=False),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True, filter_query="archived:true")
    text = render_to_str(panel)
    assert "sess_archived"[-8:] in text
    assert "sess_live"[-8:] not in text

    invalid_panel = render_panel(2, state, Theme(), detail=True, filter_query="archived:x")
    assert "No matching sessions" in render_to_str(invalid_panel)


def test_sessions_panel_detail_substring_field_filter():
    state = DashboardState(
        sessions=[
            SessionInfo(session_id="sess_gpt", source="cli", model="gpt-5.4"),
            SessionInfo(session_id="sess_claude", source="cli", model="claude"),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True, filter_query="model:gpt")
    text = render_to_str(panel)
    assert "sess_gpt"[-8:] in text
    assert "sess_claude"[-8:] not in text


def test_sessions_panel_detail_runtime_table():
    state = DashboardState(
        sessions=[
            SessionInfo(
                session_id="sess_runtime",
                source="cli",
                api_call_count=3,
                cwd="/home/user/project-alpha/",
                archived=True,
                rewind_count=2,
                handoff_state="pending",
                handoff_platform="telegram",
                handoff_error="delivery failed",
            ),
            SessionInfo(
                session_id="sess_no_cwd",
                source="cli",
                api_call_count=1,
            ),
        ],
    )
    panel = render_panel(2, state, Theme(), detail=True)
    text = render_to_str(panel)
    assert "Runtime" in text
    assert "project-alpha" in text
    assert "archived" in text
    assert "rewind:2" in text
    assert "pending" in text
    assert "delivery failed" in text
    # Session with no cwd shows a dash instead of an empty cell
    assert "—" in text


def test_sessions_panel_detail_no_runtime_section_without_runtime_data():
    state = DashboardState(
        sessions=[SessionInfo(session_id="sess_plain", source="cli")],
    )
    panel = render_panel(2, state, Theme(), detail=True)
    text = render_to_str(panel)
    assert "Runtime" not in text


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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
    assert "12.4K" in text
    assert "$0.42" in text
    assert "Recent Windows" in text
    assert "7d" in text
    assert "By Model" in text
    assert "gpt-5.4" in text
    assert "By Provider" in text
    assert "openai-codex" in text


def test_tokens_panel_detail_cost_prefix_estimated_vs_reported():
    state = DashboardState(
        sessions=[
            SessionInfo(
                session_id="sess_reported",
                estimated_cost_usd=0.42,
                cost_status="reported",
            ),
            SessionInfo(
                session_id="sess_estimated",
                estimated_cost_usd=0.31,
                cost_status="estimated",
            ),
        ],
    )
    panel = render_panel(3, state, Theme(), detail=True)
    text = render_to_str(panel)
    assert "$0.42" in text
    assert "~$0.42" not in text
    assert "~$0.31" in text


def _analytics_state(*, cost_is_estimated: bool) -> DashboardState:
    return DashboardState(
        tokens_total=TokenSummary(cost_is_estimated=cost_is_estimated),
        token_analytics=TokenAnalytics(
            windows=[
                TokenWindowSummary(
                    label="7d", session_count=1, input_tokens=100, total_cost_usd=0.42
                ),
            ],
            by_model=[
                TokenBreakdown(
                    label="gpt-5.4", session_count=1, input_tokens=100, total_cost_usd=0.31
                ),
            ],
        ),
    )


def test_tokens_panel_detail_aggregate_tables_estimated_prefix():
    text = render_to_str(
        render_panel(3, _analytics_state(cost_is_estimated=True), Theme(), detail=True)
    )
    assert "~$0.42" in text
    assert "~$0.31" in text


def test_tokens_panel_detail_aggregate_tables_reported_prefix():
    text = render_to_str(
        render_panel(3, _analytics_state(cost_is_estimated=False), Theme(), detail=True)
    )
    assert "$0.42" in text
    assert "~$0.42" not in text
    assert "$0.31" in text
    assert "~$0.31" not in text


def test_tools_panel_detail():
    state = DashboardState(
        tool_stats=[
            ToolStats(name="shell_exec", call_count=23),
            ToolStats(name="web_search", call_count=18),
            ToolStats(name="read_file", call_count=7),
        ],
    )
    panel = render_panel(4, state, Theme(), detail=True)
    text = render_to_str(panel)
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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
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


def test_config_panel_detail_feature_labels():
    state = DashboardState(
        config=ConfigSummary(
            model="gpt-5.4",
            dashboard_auth_provider="oauth",
            dashboard_basic_auth_configured=True,
            tool_search_enabled="auto",
            tool_search_threshold_pct=80,
            tool_search_default_limit=5,
            tool_search_max_limit=20,
            code_execution_mode="sandbox",
            code_execution_timeout=120,
            code_execution_max_tool_calls=50,
            kanban_dispatch_in_gateway=True,
            kanban_dispatch_interval_seconds=30,
            kanban_failure_limit=3,
            kanban_auto_decompose=True,
            gateway_strict_media_delivery=True,
            gateway_trust_recent_files=True,
            gateway_trust_recent_files_seconds=90,
        ),
    )
    panel = render_panel(5, state, Theme(), detail=True)
    text = render_to_str(panel)
    assert "oauth basic-configured" in text
    assert "auto threshold=80% limit=5/20" in text
    assert "sandbox 120s 50 calls" in text
    assert "gateway 30s fail=3 auto-decompose" in text
    assert "strict trust-recent=90s" in text


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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
    assert "Connected" in text
    assert "[gateway]" in text


def test_logs_panel_detail_errors():
    state = DashboardState(
        logs=LogState(
            error_lines=[LogLine(timestamp="14:00:00", level="ERROR", message="Crash")],
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, log_sub_view="errors")
    text = render_to_str(panel)
    assert "Crash" in text
    assert "[errors]" in text


def test_logs_panel_detail_cron():
    state = DashboardState(
        logs=LogState(
            cron_lines=[LogLine(message="cron output line")],
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, log_sub_view="cron")
    text = render_to_str(panel)
    assert "cron output line" in text
    assert "[cron]" in text


def test_logs_panel_detail_unknown_sub_view_falls_back_to_first_stream():
    state = DashboardState(
        logs=LogState(
            agent_lines=[LogLine(timestamp="15:42:03", level="INFO", message="Session saved")],
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, log_sub_view="bogus")
    text = render_to_str(panel)
    assert "[agent]" in text
    assert "Session saved" in text


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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
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
    text = render_to_str(panel)
    assert "provider timeout" in text
    assert "session saved" not in text


def test_logs_panel_detail_component_and_session_filters_exclude_mismatches():
    state = DashboardState(
        logs=LogState(
            agent_lines=[
                LogLine(component="hermes", session_id="sess-alpha", message="session saved"),
                LogLine(component="gateway", session_id="sess-beta", message="provider timeout"),
            ]
        ),
    )
    component_panel = render_panel(
        8, state, Theme(), detail=True, filter_query="component:gateway"
    )
    component_text = render_to_str(component_panel)
    assert "provider timeout" in component_text
    assert "session saved" not in component_text

    session_panel = render_panel(8, state, Theme(), detail=True, filter_query="session:sess-alpha")
    session_text = render_to_str(session_panel)
    assert "session saved" in session_text
    assert "provider timeout" not in session_text


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
    text = render_to_str(panel)
    assert "provider slow" in text
    assert "provider timeout" in text
    assert "session saved" not in text


def test_logs_panel_detail_unknown_minlevel_shows_all_lines():
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
    text = render_to_str(panel)
    # An unknown minlevel value deactivates the filter instead of hiding everything.
    assert "session saved" in text
    assert "provider timeout" in text


def test_logs_panel_detail_text_filter_query():
    state = DashboardState(
        logs=LogState(
            agent_lines=[
                LogLine(component="hermes", level="INFO", message="session saved"),
                LogLine(component="gateway", level="ERROR", message="provider timeout"),
            ]
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, filter_query="text:timeout")
    text = render_to_str(panel)
    assert "provider timeout" in text
    assert "session saved" not in text


def test_logs_panel_detail_unknown_structured_filter_matches_as_text():
    state = DashboardState(
        logs=LogState(
            agent_lines=[
                LogLine(component="gateway", level="ERROR", message="provider timeout"),
            ]
        ),
    )
    panel = render_panel(8, state, Theme(), detail=True, filter_query="unknown:value")
    text = render_to_str(panel)
    assert "No matching log lines" in text
    assert "provider timeout" not in text


# ── Empty state tests ──────────────────────────────────────────────────


def test_sessions_panel_empty():
    state = DashboardState(sessions=[])
    panel = render_panel(2, state, Theme(), detail=False)
    text = render_to_str(panel)
    assert "Sessions" in text
    assert "0 active" in text


def test_tools_panel_empty():
    state = DashboardState(tool_stats=[], total_tool_calls=0, available_tools=0)
    panel = render_panel(4, state, Theme(), detail=False)
    text = render_to_str(panel)
    assert "0 available" in text


def test_logs_panel_empty():
    state = DashboardState(logs=LogState())
    panel = render_panel(8, state, Theme(), detail=False)
    text = render_to_str(panel)
    assert "Logs" in text


def test_cron_panel_no_tick():
    state = DashboardState(cron=CronState())
    panel = render_panel(6, state, Theme(), detail=False)
    text = render_to_str(panel)
    assert "Cron" in text


def test_overview_panel_no_providers():
    state = DashboardState(skills_memory=SkillsMemory())
    panel = render_panel(7, state, Theme(), detail=False)
    text = render_to_str(panel)
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
    text = render_to_str(panel)
    assert "Running" in text
