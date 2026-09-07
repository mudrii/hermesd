"""Tests for the [11] Kanban panel."""

from __future__ import annotations

import time

from hermesd.models import DashboardState, KanbanState, KanbanTaskSummary
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
