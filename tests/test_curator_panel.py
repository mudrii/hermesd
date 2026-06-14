from __future__ import annotations

from hermesd.models import CuratorRun, DashboardState
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str

_RUN = CuratorRun(
    run_present=True,
    stamp="20260610-133539",
    started_at="2026-06-10T13:35:39+00:00",
    duration_seconds=597.0,
    model="MiniMax-M3",
    provider="minimax",
    count_before=8,
    count_after=5,
    count_delta=-3,
    archived_count=3,
    pruned_count=3,
    tool_calls_total=67,
    llm_summary="processed the candidate skills",
)


def test_curator_panel_compact_shows_last_run():
    text = render_to_str(render_panel(13, DashboardState(curator=_RUN), Theme(), detail=False))
    assert "20260610-133539" in text
    assert "8 → 5" in text


def test_curator_panel_detail_shows_fields_and_summary():
    text = render_to_str(render_panel(13, DashboardState(curator=_RUN), Theme(), detail=True))
    assert "MiniMax-M3" in text
    assert "minimax" in text
    assert "67" in text
    assert "processed the candidate skills" in text


def test_curator_panel_compact_empty_state():
    text = render_to_str(render_panel(13, DashboardState(), Theme(), detail=False))
    assert "No curation runs" in text


def test_curator_panel_detail_empty_state():
    text = render_to_str(render_panel(13, DashboardState(), Theme(), detail=True))
    assert "No curation runs" in text


def test_curator_panel_detail_shows_error_over_summary():
    run = _RUN.model_copy(update={"llm_error": "model timeout"})
    text = render_to_str(render_panel(13, DashboardState(curator=run), Theme(), detail=True))
    assert "model timeout" in text
    assert "processed the candidate skills" not in text
