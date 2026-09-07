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
