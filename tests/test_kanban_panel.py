"""Tests for the [11] Kanban panel."""

from __future__ import annotations

import time

from hermesd.models import (
    DashboardState,
    KanbanNotifySubSummary,
    KanbanState,
    KanbanTaskSummary,
)
from hermesd.panels.kanban import render_kanban
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_kanban_completed_at_renders_age_label() -> None:
    completed = int(time.time()) - 120
    task = KanbanTaskSummary(task_id="t1", status="done", completed_at=completed)
    state = DashboardState(kanban=KanbanState(db_present=True, recent_tasks=[task]))
    rendered = render_to_str(render_kanban(state, Theme(), detail=True))
    assert str(completed) not in rendered
    assert "2m" in rendered


def test_kanban_compact_shows_current_board_suffix() -> None:
    state = DashboardState(
        kanban=KanbanState(db_present=True, board_count=3, current_board="ops-board")
    )
    text = render_to_str(render_kanban(state, Theme()), width=100, no_color=True)

    assert "Boards:" in text
    assert "current=ops-board" in text


def test_kanban_compact_omits_current_board_suffix_when_unset() -> None:
    state = DashboardState(kanban=KanbanState(db_present=True, board_count=3, current_board=""))
    text = render_to_str(render_kanban(state, Theme()), width=100, no_color=True)

    assert "Boards:" in text
    assert "current=" not in text


def test_kanban_detail_age_uses_collected_at_not_wall_clock() -> None:
    now = 1_800_000_000.0
    state = DashboardState(
        collected_at=now,
        kanban=KanbanState(
            db_present=True,
            task_count=1,
            active_tasks=[
                KanbanTaskSummary(
                    task_id="task-age",
                    title="age",
                    status="in_progress",
                    last_heartbeat_at=int(now) - 7200,
                )
            ],
        ),
    )

    text = render_to_str(render_kanban(state, Theme(), detail=True), width=200, no_color=True)

    assert "2h" in text


def test_kanban_detail_shows_contract_and_breaker_labels() -> None:
    review = KanbanTaskSummary(
        task_id="t_rev",
        status="review",
        completion_contract="owner/repo",
    )
    tripped = KanbanTaskSummary(
        task_id="t_trip",
        status="blocked",
        consecutive_failures=3,
        breaker_limit=3,
        breaker_tripped=True,
    )
    state = DashboardState(
        kanban=KanbanState(db_present=True, problem_tasks=[tripped], recent_tasks=[review])
    )

    text = render_to_str(render_kanban(state, Theme(), detail=True), width=160, no_color=True)

    assert "3/3" in text
    assert "breaker tripped" in text
    assert "Contract" in text
    assert "owner/repo" in text


def test_kanban_detail_failure_cell_stays_plain_when_not_tripped() -> None:
    task = KanbanTaskSummary(
        task_id="t1",
        status="in_progress",
        consecutive_failures=1,
        breaker_limit=3,
        breaker_tripped=False,
    )
    state = DashboardState(kanban=KanbanState(db_present=True, active_tasks=[task]))

    text = render_to_str(render_kanban(state, Theme(), detail=True), width=160, no_color=True)

    assert "breaker tripped" not in text
    assert "1" in text


def test_kanban_compact_shows_notify_backlog_and_orphans() -> None:
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            task_count=2,
            notify_sub_count=3,
            notify_backlog_total=4,
            notify_orphan_profile_count=1,
            notify_orphan_profiles=["ghost"],
        )
    )
    text = render_to_str(render_kanban(state, Theme()), width=100, no_color=True)

    assert "Notify Subs:" in text
    assert "Backlog: 4" in text
    assert "Orphan Profiles: ghost" in text


def test_kanban_compact_omits_notify_lines_without_subscriptions() -> None:
    state = DashboardState(kanban=KanbanState(db_present=True, task_count=1))
    text = render_to_str(render_kanban(state, Theme()), width=100, no_color=True)

    assert "Notify Subs:" not in text
    assert "Orphan Profiles:" not in text


def test_kanban_compact_orphan_profile_renders_literal_brackets() -> None:
    """Orphan profile names are appended to a Text buffer, which never parses
    Rich markup, so escaping them would leak the escape backslashes."""
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            task_count=1,
            notify_sub_count=1,
            notify_orphan_profile_count=1,
            notify_orphan_profiles=["we[i]rd"],
        )
    )
    text = render_to_str(render_kanban(state, Theme()), width=100, no_color=True)

    assert "Orphan Profiles: we[i]rd" in text
    assert "\\" not in text


def test_kanban_compact_breaker_content_renders_literal_brackets() -> None:
    """The compact breaker line appends task ids to the same Text buffer and
    must stay unescaped too."""
    tripped = KanbanTaskSummary(
        task_id="we[i]rd",
        status="blocked",
        consecutive_failures=1,
        breaker_limit=0,
        breaker_tripped=True,
    )
    state = DashboardState(kanban=KanbanState(db_present=True, problem_tasks=[tripped]))
    text = render_to_str(render_kanban(state, Theme()), width=100, no_color=True)

    assert "Breaker Tripped: we[i]rd" in text
    assert "\\" not in text


def test_kanban_compact_warns_when_breaker_tripped() -> None:
    tripped = KanbanTaskSummary(
        task_id="t_trip",
        status="blocked",
        consecutive_failures=2,
        breaker_limit=2,
        breaker_tripped=True,
    )
    state = DashboardState(kanban=KanbanState(db_present=True, problem_tasks=[tripped]))
    text = render_to_str(render_kanban(state, Theme()), width=100, no_color=True)

    assert "Breaker Tripped: t_trip" in text


def test_kanban_compact_omits_breaker_line_when_no_task_tripped() -> None:
    state = DashboardState(kanban=KanbanState(db_present=True, task_count=1))
    text = render_to_str(render_kanban(state, Theme()), width=100, no_color=True)

    assert "Breaker Tripped:" not in text


def test_kanban_detail_shows_notify_backlog_section_and_summary_rows() -> None:
    sub = KanbanNotifySubSummary(
        task_id="t1",
        platform="Discord",
        notifier_profile="ops",
        delivery_mode="notify+wake",
        last_event_id=3,
        max_event_id=5,
        backlog=2,
    )
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            notify_sub_count=3,
            notify_backlog_total=2,
            notify_max_backlog=2,
            notify_backlog_subs=[sub],
            notify_orphan_profile_count=1,
            notify_orphan_profiles=["ghost"],
        )
    )

    text = render_to_str(render_kanban(state, Theme(), detail=True), width=160, no_color=True)

    assert "Notify Backlog" in text
    assert "Notify Subs" in text
    assert "2 unseen (max 2)" in text
    assert "Orphan Profiles" in text
    assert "ghost" in text
    assert "Discord" in text
    assert "notify+wake" in text
    assert "ops" in text


def test_kanban_detail_notify_backlog_zero_reads_as_none_unseen() -> None:
    state = DashboardState(
        kanban=KanbanState(db_present=True, notify_sub_count=2, notify_backlog_total=0)
    )

    text = render_to_str(render_kanban(state, Theme(), detail=True), width=160, no_color=True)

    assert "0 unseen" in text


def test_kanban_detail_renders_notify_platform_counts() -> None:
    """The per-platform subscription rollup is collected; the panel must show it.

    It was the one notify field with no consumer outside the JSON snapshot, so
    "which platform's watchers are subscribed" — the first question when a
    backlog grows — had no answer in the UI.
    """
    state = DashboardState(
        kanban=KanbanState(
            db_present=True,
            notify_sub_count=3,
            notify_platform_counts={"discord": 2, "slack": 1},
        )
    )

    text = render_to_str(render_kanban(state, Theme(), detail=True), width=160, no_color=True)

    assert "Notify Platforms" in text
    assert "discord 2" in text
    assert "slack 1" in text
