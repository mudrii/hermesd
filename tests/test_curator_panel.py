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
    added_count=2,
    pruned_count=3,
    consolidated_count=1,
    tool_calls_total=67,
    tool_call_counts={"read_file": 12, "list_dir": 3},
    state_transitions=["collecting -> summarizing @ 2026-06-10T13:40:00+00:00"],
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
    assert "Added" in text
    assert "2" in text
    assert "Consolidated" in text
    assert "1" in text
    assert "67" in text
    assert "read_file" in text
    assert "12" in text
    assert "collecting -> summarizing" in text
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


def test_curator_panel_compact_shows_error_marker():
    # A run that finished with an llm_error appends a ⚠ marker in the compact view.
    run = _RUN.model_copy(update={"llm_error": "model timeout"})
    text = render_to_str(render_panel(13, DashboardState(curator=run), Theme(), detail=False))
    assert "⚠" in text
    # The compact marker is just a flag; the full error text is detail-only.
    assert "model timeout" not in text


def test_curator_panel_compact_no_error_marker_when_clean():
    # Without an llm_error the compact view must not show the warning marker.
    text = render_to_str(render_panel(13, DashboardState(curator=_RUN), Theme(), detail=False))
    assert "⚠" not in text


def test_curator_panel_detail_truncates_long_summary():
    # llm_summary over 600 chars is cut to 600 and gets an ellipsis; the tail is dropped.
    long_summary = "A" * 600 + "TAIL_SENTINEL"
    run = _RUN.model_copy(update={"llm_summary": long_summary})
    text = render_to_str(render_panel(13, DashboardState(curator=run), Theme(), detail=True))
    assert "…" in text
    assert "TAIL_SENTINEL" not in text
    assert "A" * 100 in text


def test_curator_panel_detail_short_summary_not_truncated():
    # A summary at or under the limit renders whole, with no ellipsis appended.
    run = _RUN.model_copy(update={"llm_summary": "B" * 600})
    text = render_to_str(render_panel(13, DashboardState(curator=run), Theme(), detail=True))
    assert "…" not in text
    assert "B" * 100 in text
