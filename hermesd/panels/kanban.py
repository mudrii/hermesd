from __future__ import annotations

import time

import rich.box
from rich.console import Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState, KanbanTaskSummary
from hermesd.theme import Theme


def render_kanban(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    kanban = state.kanban
    lines = Text()
    if not kanban.db_present:
        lines.append("  No kanban.db", style=theme.banner_dim)
    else:
        lines.append("  Tasks: ", style=theme.ui_label)
        lines.append(f"{kanban.task_count}", style=theme.banner_text)
        lines.append("  Runs: ", style=theme.ui_label)
        lines.append(f"{kanban.run_count}\n", style=theme.banner_text)
        lines.append("  Dispatch: ", style=theme.ui_label)
        lines.append(
            "gateway" if kanban.dispatch_in_gateway else "disabled",
            style=theme.ui_ok if kanban.dispatch_in_gateway else theme.ui_warn,
        )
        lines.append("\n")
        for status, count in sorted(kanban.status_counts.items())[:4]:
            lines.append(f"  {status}: ", style=theme.ui_label)
            lines.append(f"{count}", style=theme.banner_text)
    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[11] Kanban[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    kanban = state.kanban
    sections: list[RenderableType] = []

    summary = Table(box=None, show_header=False, padding=(0, 2))
    summary.add_column("Key", style=theme.ui_label)
    summary.add_column("Value", style=theme.banner_text)
    summary.add_row("Database", "present" if kanban.db_present else "missing")
    summary.add_row("Tasks", str(kanban.task_count))
    summary.add_row("Runs", str(kanban.run_count))
    summary.add_row("Events", str(kanban.event_count))
    summary.add_row("Comments", str(kanban.comment_count))
    summary.add_row("Dispatch", "gateway" if kanban.dispatch_in_gateway else "disabled")
    summary.add_row("Interval", f"{kanban.dispatch_interval_seconds}s")
    summary.add_row("Failure Limit", str(kanban.failure_limit or "—"))
    summary.add_row("Auto Decompose", "yes" if kanban.auto_decompose else "no")
    if kanban.link_count:
        summary.add_row("Decomposition Links", str(kanban.link_count))
    if kanban.attachment_count:
        summary.add_row("Attachments", str(kanban.attachment_count))
    sections.append(summary)

    if kanban.task_links:
        sections.append(Text("\nDecomposition Tree\n", style=f"bold {theme.ui_label}"))
        links = Table(box=None, show_header=True, padding=(0, 2))
        links.add_column("Parent", style=theme.ui_accent)
        links.add_column("Child", style=theme.banner_text)
        for link in kanban.task_links:
            links.add_row(escape(link.parent_id), escape(link.child_id))
        sections.append(links)

    if kanban.status_counts:
        sections.append(Text("\nStatus Counts\n", style=f"bold {theme.ui_label}"))
        status_table = Table(box=None, show_header=True, padding=(0, 2))
        status_table.add_column("Status", style=theme.ui_accent)
        status_table.add_column("Count", justify="right", style=theme.banner_text)
        for status, count in sorted(kanban.status_counts.items()):
            status_table.add_row(escape(status), str(count))
        sections.append(status_table)

    if kanban.active_tasks:
        sections.append(Text("\nActive Workers\n", style=f"bold {theme.ui_label}"))
        sections.append(_task_table(kanban.active_tasks, theme))

    if kanban.problem_tasks:
        sections.append(Text("\nBlocked / Failing Tasks\n", style=f"bold {theme.ui_label}"))
        sections.append(_task_table(kanban.problem_tasks, theme))

    task_metadata = _task_metadata_table(
        [*kanban.active_tasks, *kanban.problem_tasks, *kanban.recent_tasks],
        theme,
    )
    if task_metadata is not None:
        sections.append(Text("\nTask Metadata\n", style=f"bold {theme.ui_label}"))
        sections.append(task_metadata)

    if kanban.recent_runs:
        sections.append(Text("\nRecent Runs\n", style=f"bold {theme.ui_label}"))
        runs = Table(box=None, show_header=True, padding=(0, 1))
        runs.add_column("Run", justify="right", style=theme.ui_accent)
        runs.add_column("Task", style=theme.ui_label)
        runs.add_column("Profile", style=theme.banner_text)
        runs.add_column("Status", style=theme.banner_text)
        runs.add_column("Outcome", style=theme.banner_dim)
        runs.add_column("Error", style=theme.ui_error)
        for run in kanban.recent_runs:
            runs.add_row(
                str(run.run_id),
                escape(run.task_id),
                escape(run.profile) if run.profile else "—",
                escape(run.status),
                escape(run.outcome) if run.outcome else "—",
                escape(run.error[:80]) if run.error else "—",
            )
        sections.append(runs)

    if len(sections) == 1 and not kanban.db_present:
        sections.append(
            Text("\n  Kanban is not initialized in this Hermes home\n", style=theme.banner_dim)
        )

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[11] Kanban[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _task_table(tasks: list[KanbanTaskSummary], theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Task", style=theme.ui_accent)
    table.add_column("Status", style=theme.banner_text)
    table.add_column("Assignee", style=theme.ui_label)
    table.add_column("PID", justify="right", style=theme.banner_text)
    table.add_column("Heartbeat", style=theme.banner_dim)
    table.add_column("Failures", justify="right", style=theme.ui_warn)
    table.add_column("Branch", style=theme.banner_dim)
    table.add_column("Title", style=theme.banner_text, ratio=1)
    for task in tasks:
        table.add_row(
            escape(task.task_id),
            escape(task.status),
            escape(task.assignee) if task.assignee else "—",
            str(task.worker_pid) if task.worker_pid else "—",
            _age_label(task.last_heartbeat_at),
            str(task.consecutive_failures),
            escape(task.branch_name) if task.branch_name else "—",
            escape(task.title),
        )
    return table


def _task_metadata_table(tasks: list[KanbanTaskSummary], theme: Theme) -> Table | None:
    metadata_tasks = [
        task
        for task in tasks
        if task.completed_at
        or task.workspace_path
        or task.goal_mode
        or task.current_step_key
        or task.branch_name
    ]
    if not metadata_tasks:
        return None
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Task", style=theme.ui_accent)
    table.add_column("Branch", style=theme.banner_dim)
    table.add_column("Completed", justify="right", style=theme.banner_dim)
    table.add_column("Workspace", style=theme.banner_text)
    table.add_column("Goal Mode", style=theme.banner_dim)
    table.add_column("Step", style=theme.banner_dim)
    for task in metadata_tasks:
        table.add_row(
            escape(task.task_id),
            escape(task.branch_name) if task.branch_name else "—",
            str(task.completed_at) if task.completed_at else "—",
            escape(task.workspace_path) if task.workspace_path else "—",
            escape(task.goal_mode) if task.goal_mode else "—",
            escape(task.current_step_key) if task.current_step_key else "—",
        )
    return table


def _age_label(timestamp: int) -> str:
    if timestamp <= 0:
        return "—"
    age = max(0, int(time.time()) - timestamp)
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    return f"{age // 3600}h"
