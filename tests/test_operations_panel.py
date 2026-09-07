"""Tests for the [12] Operations panel."""

from __future__ import annotations

from hermesd.models import DashboardState, ModelCacheSummary, OperationsState
from hermesd.panels.operations import render_operations
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_operations_detail_handles_absurd_mtime() -> None:
    state = DashboardState(
        operations=OperationsState(
            model_caches=[ModelCacheSummary(name="models_dev_cache", mtime=float("inf"))]
        )
    )
    rendered = render_to_str(render_operations(state, Theme(), detail=True))
    assert "—" in rendered
