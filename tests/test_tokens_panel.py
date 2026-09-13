"""Tests for the [3] Tokens / Cost panel."""

from __future__ import annotations

from hermesd.models import (
    DashboardState,
    ModelUsage,
    SessionInfo,
    TokenAnalytics,
    TokenBreakdown,
    TokenSummary,
)
from hermesd.panels.tokens import render_tokens
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_tokens_detail_caps_session_table_with_footer() -> None:
    sessions = [SessionInfo(session_id=f"sess_{i:04d}") for i in range(55)]
    rendered = render_to_str(render_tokens(DashboardState(sessions=sessions), Theme(), detail=True))
    assert "… and 5 more" in rendered


def test_tokens_detail_no_footer_when_under_cap() -> None:
    sessions = [SessionInfo(session_id=f"sess_{i:04d}") for i in range(3)]
    rendered = render_to_str(render_tokens(DashboardState(sessions=sessions), Theme(), detail=True))
    assert "… and" not in rendered


def _usage_state() -> DashboardState:
    return DashboardState(
        tokens_today=TokenSummary(input_tokens=100, output_tokens=50, total_cost_usd=0.5),
        tokens_total=TokenSummary(input_tokens=900, output_tokens=200, total_cost_usd=1.5),
        token_analytics=TokenAnalytics(
            usage_source="session_model_usage",
            model_usage_all=[
                ModelUsage(
                    model="gpt-5.4",
                    provider="openai",
                    api_calls=7,
                    input_tokens=100_000,
                    output_tokens=20_000,
                    cache_read_tokens=5_000,
                    estimated_cost_usd=0.9,
                    actual_cost_usd=0.55,
                    has_actual_cost=True,
                ),
                ModelUsage(
                    model="claude-opus",
                    provider="anthropic",
                    api_calls=3,
                    input_tokens=40_000,
                    output_tokens=8_000,
                    estimated_cost_usd=0.30,
                ),
                ModelUsage(
                    model="gpt-5.4-mini",
                    provider="openai",
                    task="title",
                    api_calls=2,
                    input_tokens=900,
                    estimated_cost_usd=0.01,
                ),
            ],
            model_usage_24h=[ModelUsage(model="gpt-5.4", provider="openai", input_tokens=10_000)],
            model_usage_7d=[ModelUsage(model="gpt-5.4", provider="openai", input_tokens=90_000)],
        ),
    )


def _legacy_state() -> DashboardState:
    return DashboardState(
        sessions=[SessionInfo(session_id="sess_legacy", model="legacy-model")],
        token_analytics=TokenAnalytics(
            by_model=[TokenBreakdown(label="legacy-model", session_count=1, input_tokens=10)],
        ),
    )


def test_compact_shows_top_models_from_usage_table() -> None:
    rendered = render_to_str(render_tokens(_usage_state(), Theme()), width=100)

    assert "gpt-5.4" in rendered
    assert "claude-opus" in rendered


def test_compact_omits_model_line_when_usage_table_absent() -> None:
    rendered = render_to_str(render_tokens(_legacy_state(), Theme()), width=100)

    assert "Models" not in rendered


def test_detail_uses_usage_table_with_actual_and_estimated_costs() -> None:
    rendered = render_to_str(render_tokens(_usage_state(), Theme(), detail=True), width=140)

    assert "By Model" in rendered
    assert "$0.55" in rendered
    assert "est." in rendered
    # Task-tagged rows collapse into the aux subtotal.
    assert "aux (1)" in rendered
    assert "gpt-5.4-mini" not in rendered
    assert "24h" in rendered


def test_detail_keeps_session_model_breakdown_when_usage_source_is_sessions() -> None:
    rendered = render_to_str(render_tokens(_legacy_state(), Theme(), detail=True), width=140)

    assert "legacy-model" in rendered
    assert "aux" not in rendered


def test_compact_omits_models_line_when_every_row_is_task_tagged() -> None:
    # Auxiliary (task-tagged) usage never earns the compact Models line.
    state = DashboardState(
        token_analytics=TokenAnalytics(
            usage_source="session_model_usage",
            model_usage_all=[
                ModelUsage(model="gpt-5.4-mini", provider="openai", task="title", api_calls=2)
            ],
        )
    )
    rendered = render_to_str(render_tokens(state, Theme()), width=100, no_color=True)

    assert "Models" not in rendered


def test_detail_omits_aux_row_when_no_task_tagged_models() -> None:
    state = _usage_state()
    state.token_analytics.model_usage_all = [
        usage for usage in state.token_analytics.model_usage_all if not usage.task
    ]

    rendered = render_to_str(render_tokens(state, Theme(), detail=True), width=140, no_color=True)

    assert "By Model" in rendered
    assert "gpt-5.4" in rendered
    assert "aux" not in rendered


def test_detail_escapes_markup_hostile_model_names() -> None:
    state = _usage_state()
    state.token_analytics.model_usage_all[0].model = "[/] evil [x]\x1b[2Jmodel"

    rendered = render_to_str(render_tokens(state, Theme(), detail=True), width=160)

    assert "[/]" in rendered
    assert "[x]" in rendered
    assert "\x1b[2J" not in rendered


def _mixed_usage_state() -> DashboardState:
    return DashboardState(
        token_analytics=TokenAnalytics(
            usage_source="session_model_usage",
            model_usage_all=[
                ModelUsage(
                    model="gpt-5.4",
                    provider="openai",
                    api_calls=5,
                    input_tokens=1000,
                    output_tokens=100,
                    estimated_cost_usd=3.00,
                    actual_cost_usd=1.00,
                    has_actual_cost=True,
                    reported_cost_usd=1.00,
                    estimated_only_cost_usd=2.00,
                    reported_row_count=1,
                    row_count=2,
                ),
                ModelUsage(
                    model="claude-opus",
                    provider="anthropic",
                    api_calls=2,
                    input_tokens=500,
                    output_tokens=50,
                    estimated_cost_usd=0.40,
                    estimated_only_cost_usd=0.40,
                    row_count=1,
                ),
                ModelUsage(
                    model="gpt-5.4-mini",
                    provider="openai",
                    task="title",
                    api_calls=3,
                    input_tokens=100,
                    output_tokens=10,
                    estimated_cost_usd=0.05,
                    actual_cost_usd=0.02,
                    has_actual_cost=True,
                    reported_cost_usd=0.02,
                    estimated_only_cost_usd=0.03,
                    reported_row_count=1,
                    row_count=2,
                ),
            ],
        ),
    )


def test_detail_labels_mixed_actual_and_estimated_group() -> None:
    rendered = render_to_str(render_tokens(_mixed_usage_state(), Theme(), detail=True), width=140)

    # The estimate-only rows are shown, not silently dropped behind the actuals.
    assert "$1.00 + $2.00 est." in rendered


def test_detail_all_estimated_group_keeps_est_suffix() -> None:
    rendered = render_to_str(render_tokens(_mixed_usage_state(), Theme(), detail=True), width=140)

    assert "$0.40 est." in rendered


def test_aux_subtotal_uses_identical_mixed_semantics() -> None:
    rendered = render_to_str(render_tokens(_mixed_usage_state(), Theme(), detail=True), width=140)

    assert "aux (1)" in rendered
    assert "$0.02 + $0.03 est." in rendered


def test_detail_all_actual_group_shows_plain_cost() -> None:
    state = DashboardState(
        token_analytics=TokenAnalytics(
            usage_source="session_model_usage",
            model_usage_all=[
                ModelUsage(
                    model="gpt-5.4",
                    provider="openai",
                    api_calls=4,
                    input_tokens=800,
                    output_tokens=80,
                    actual_cost_usd=0.75,
                    has_actual_cost=True,
                    reported_cost_usd=0.75,
                    reported_row_count=2,
                    row_count=2,
                ),
            ],
        ),
    )
    rendered = render_to_str(render_tokens(state, Theme(), detail=True), width=140)

    assert "$0.75" in rendered
    assert "est." not in rendered


def test_detail_authoritative_zero_cost_group_shows_plain_zero() -> None:
    state = DashboardState(
        token_analytics=TokenAnalytics(
            usage_source="session_model_usage",
            model_usage_all=[
                ModelUsage(
                    model="gpt-5.4-mini",
                    provider="openai",
                    api_calls=2,
                    input_tokens=300,
                    output_tokens=30,
                    reported_cost_usd=0.0,
                    reported_row_count=1,
                    row_count=1,
                ),
            ],
        ),
    )
    rendered = render_to_str(render_tokens(state, Theme(), detail=True), width=140)

    assert "$0.00" in rendered
    assert "est." not in rendered


def test_model_usage_cost_split_fields_serialize_in_snapshot() -> None:
    state = _mixed_usage_state()
    payload = state.model_dump(mode="json")
    first = payload["token_analytics"]["model_usage_all"][0]
    assert first["reported_cost_usd"] == 1.00
    assert first["estimated_only_cost_usd"] == 2.00
    assert first["reported_row_count"] == 1
    assert first["row_count"] == 2
