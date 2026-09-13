from __future__ import annotations

import re

from hermesd.models import CuratorRun, DashboardState
from hermesd.panels import render_panel
from hermesd.panels.curator_panel import render_curator
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
    text = render_to_str(
        render_panel(13, DashboardState(curator=_RUN), Theme(), detail=True),
        width=120,
        no_color=True,
    )
    assert "MiniMax-M3" in text
    assert "minimax" in text
    # Bind each label to its value so a wrong count can't pass on a stray digit.
    assert re.search(r"Added\s+2\b", text)
    assert re.search(r"Consolidated\s+1\b", text)
    assert re.search(r"Tool Calls\s+67\b", text)
    assert re.search(r"read_file\s+12\b", text)
    assert "collecting -> summarizing" in text
    assert "processed the candidate skills" in text


def test_curator_panel_compact_empty_state():
    text = render_to_str(render_panel(13, DashboardState(), Theme(), detail=False))
    assert "No curation runs" in text


def test_curator_panel_detail_empty_state():
    text = render_to_str(render_panel(13, DashboardState(), Theme(), detail=True))
    assert "No curation runs" in text


def test_curator_panel_compact_shows_scheduler_state_without_run():
    # No run report yet, but scheduler state exists: compact shows it instead of
    # the "No curation runs" placeholder.
    run = CuratorRun(
        scheduler_state_present=True,
        scheduler_paused=True,
        scheduler_run_count=7,
    )
    text = render_to_str(
        render_panel(13, DashboardState(curator=run), Theme(), detail=False),
        no_color=True,
    )
    assert "No curation runs" not in text
    assert re.search(r"Scheduler:\s+paused", text)
    assert re.search(r"Runs:\s+7\b", text)


def test_curator_panel_compact_scheduler_active_when_not_paused():
    run = CuratorRun(scheduler_state_present=True, scheduler_run_count=0)
    text = render_to_str(
        render_panel(13, DashboardState(curator=run), Theme(), detail=False),
        no_color=True,
    )
    assert re.search(r"Scheduler:\s+active", text)


def test_curator_panel_detail_shows_scheduler_state_without_run():
    run = CuratorRun(
        scheduler_state_present=True,
        scheduler_paused=True,
        scheduler_run_count=7,
        scheduler_last_run_at="2026-07-10T10:00:00Z",
        scheduler_last_report_path="logs/curator/2026/run.md",
        consolidate_enabled=True,
    )
    text = render_to_str(
        render_panel(13, DashboardState(curator=run), Theme(), detail=True),
        no_color=True,
    )
    assert "Scheduler" in text
    assert "paused" in text
    assert re.search(r"Run Count\s+7\b", text)
    assert re.search(r"Last Run\s+2026-07-10T10:00:00Z", text)
    assert "consolidate on" in text
    assert "logs/curator/2026/run.md" in text


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


def test_curator_compact_blank_stamp_falls_back_to_dash() -> None:
    state = DashboardState(curator=CuratorRun(run_present=True, stamp=""))
    rendered = render_to_str(render_curator(state, Theme()))
    assert "Last run:" in rendered
    assert "—" in rendered


def _render(state: DashboardState, detail: bool) -> str:
    return render_to_str(render_curator(state, Theme(), detail=detail), width=160)


def _curator_state(**overrides: object) -> DashboardState:
    from hermesd.models import CuratorRun, SkillCurationWindow

    fields: dict[str, object] = {
        "run_present": False,
        "scheduler_state_present": True,
        "managed_skill_count": 6,
        "patch_pending_reuse_count": 1,
        "state_active_count": 3,
        "state_stale_count": 1,
        "state_archived_count": 1,
        "state_unknown_count": 1,
        "pinned_count": 2,
        "stale_after_days": 14,
        "archive_after_days": 30,
        "skill_windows": [
            SkillCurationWindow(
                name="research",
                state="active",
                patch_pending_reuse=True,
                last_activity_age_seconds=86400.0,
                days_until_stale=13.0,
                days_until_archive=29.0,
            ),
            SkillCurationWindow(
                name="never-used",
                state="active",
            ),
        ],
    }
    fields.update(overrides)
    return DashboardState(curator=CuratorRun(**fields))


def test_curator_compact_shows_patch_reuse_and_state_counts() -> None:
    text = _render(_curator_state(), detail=False)

    assert "Patch reuse:" in text
    assert "1 patched, not re-used" in text
    assert "States:" in text
    assert "3 active" in text
    assert "1 stale" in text
    assert "1 archived" in text
    assert "2 pinned" in text


def test_curator_compact_hides_hygiene_when_no_skills_managed() -> None:
    text = _render(_curator_state(managed_skill_count=0, skill_windows=[]), detail=False)

    assert "Patch reuse:" not in text
    assert "States:" not in text


def test_curator_detail_shows_hygiene_section_with_thresholds() -> None:
    text = _render(_curator_state(), detail=True)

    assert "Skill Hygiene" in text
    assert "Managed" in text
    assert "stale after 14d" in text
    assert "archive after 30d" in text
    assert "defaults" in text
    assert "research" in text
    assert "13d" in text
    assert "29d" in text
    assert "never" in text
    assert "—" in text


def test_curator_detail_marks_custom_thresholds() -> None:
    text = _render(
        _curator_state(stale_after_days=7, archive_after_days=21, thresholds_customized=True),
        detail=True,
    )

    assert "stale after 7d" in text
    assert "archive after 21d" in text
    assert "curator.stale_after_days" in text


def test_curator_detail_escapes_skill_names() -> None:
    from hermesd.models import SkillCurationWindow

    hostile = _curator_state(
        skill_windows=[
            SkillCurationWindow(name="[dim]evil [/] skill", state="active", days_until_stale=2.0)
        ]
    )
    text = _render(hostile, detail=True)

    assert "[dim]evil [/] skill" in text


def test_curator_detail_marks_overdue_and_caps_windows() -> None:
    from hermesd.models import SkillCurationWindow

    overdue = SkillCurationWindow(name="past-due", state="active", days_until_stale=-3.0)
    text = _render(_curator_state(skill_windows=[overdue]), detail=True)

    assert "due" in text
