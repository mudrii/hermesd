"""Tests for the [5] Config panel."""

from __future__ import annotations

from hermesd.models import ConfigSummary, DashboardState
from hermesd.panels.config_panel import render_config
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_config_detail_escapes_code_execution_mode() -> None:
    state = DashboardState(config=ConfigSummary(code_execution_mode="yolo [/] inj"))
    rendered = render_to_str(render_config(state, Theme(), detail=True))
    assert "yolo [/] inj" in rendered


def test_config_detail_escapes_moa_label_values() -> None:
    state = DashboardState(
        config=ConfigSummary(
            moa_active_preset="preset [/] x",
            moa_preset_count=2,
            moa_aggregator_label="agg [/] y",
        )
    )
    rendered = render_to_str(render_config(state, Theme(), detail=True))
    assert "preset [/] x" in rendered
    assert "agg [/] y" in rendered
