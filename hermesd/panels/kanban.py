from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState, KanbanState, KanbanTaskSummary, WorkerIdentity
from hermesd.panels.formatting import escape_terminal_text as escape
from hermesd.panels.formatting import fmt_age_seconds, sanitize_terminal_text, section_heading
from hermesd.theme import Theme

# Cap on task ids / profile names listed inline in the compact view.
_COMPACT_INLINE_LIST_LIMIT = 3


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
        if kanban.board_count:
            lines.append("  Boards: ", style=theme.ui_label)
            lines.append(f"{kanban.board_count}", style=theme.banner_text)
            if kanban.current_board:
                lines.append(
                    f" current={sanitize_terminal_text(kanban.current_board)}",
                    style=theme.banner_dim,
                )
            lines.append("\n")
        if kanban.stale_claim_count:
            lines.append("  Stale Claims: ", style=theme.ui_label)
            lines.append(f"{kanban.stale_claim_count}\n", style=theme.ui_warn)
        if kanban.worker_pid_reused_count:
            lines.append("  Reused worker PIDs: ", style=theme.ui_label)
            lines.append(f"{kanban.worker_pid_reused_count}\n", style=theme.ui_warn)
        if kanban.notify_sub_count:
            lines.append("  Notify Subs: ", style=theme.ui_label)
            lines.append(f"{kanban.notify_sub_count}", style=theme.banner_text)
            lines.append("  Backlog: ", style=theme.ui_label)
            lines.append(
                f"{kanban.notify_backlog_total}\n",
                style=theme.ui_warn if kanban.notify_backlog_total else theme.banner_text,
            )
        if kanban.notify_orphan_profile_count:
            orphans = ", ".join(kanban.notify_orphan_profiles[:_COMPACT_INLINE_LIST_LIMIT])
            extra = kanban.notify_orphan_profile_count - len(
                kanban.notify_orphan_profiles[:_COMPACT_INLINE_LIST_LIMIT]
            )
            suffix = f" (+{extra} more)" if extra > 0 else ""
            lines.append("  Orphan Profiles: ", style=theme.ui_label)
            # Text.append never parses Rich markup, so only terminal controls
            # are stripped here; escaping would show literal backslashes.
            lines.append(f"{sanitize_terminal_text(orphans)}{suffix}\n", style=theme.ui_warn)
        tripped_ids = [
            task.task_id
            for task in (
                *kanban.active_tasks,
                *kanban.problem_tasks,
                *kanban.recent_tasks,
            )
            if task.breaker_tripped
        ]
        if tripped_ids:
            shown = tripped_ids[:_COMPACT_INLINE_LIST_LIMIT]
            extra = len(tripped_ids) - len(shown)
            suffix = f" (+{extra} more)" if extra > 0 else ""
            lines.append("  Breaker Tripped: ", style=theme.ui_label)
            lines.append(
                f"{sanitize_terminal_text(', '.join(shown))}{suffix}\n", style=theme.ui_error
            )
        lines.append("  Dispatch: ", style=theme.ui_label)
        lines.append(
            "gateway" if kanban.dispatch_in_gateway else "disabled",
            style=theme.ui_ok if kanban.dispatch_in_gateway else theme.ui_warn,
        )
        lines.append("\n")
        for status, count in sorted(kanban.status_counts.items())[:4]:
            lines.append(f"  {sanitize_terminal_text(status)}: ", style=theme.ui_label)
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
    sections: list[RenderableType] = [_summary_table(kanban, theme)]

    if kanban.boards:
        sections.append(section_heading("Boards", theme))
        sections.append(_boards_table(kanban, theme))

    if kanban.task_links:
        sections.append(section_heading("Decomposition Tree", theme))
        sections.append(_task_links_table(kanban, theme))

    if kanban.status_counts:
        sections.append(section_heading("Status Counts", theme))
        sections.append(_status_counts_table(kanban, theme))

    if kanban.active_tasks:
        sections.append(section_heading("Active Workers", theme))
        sections.append(_task_table(kanban.active_tasks, theme, now=state.collected_at))

    if kanban.problem_tasks:
        sections.append(section_heading("Blocked / Failing Tasks", theme))
        sections.append(_task_table(kanban.problem_tasks, theme, now=state.collected_at))

    task_metadata = _task_metadata_table(
        [*kanban.active_tasks, *kanban.problem_tasks, *kanban.recent_tasks],
        theme,
        now=state.collected_at,
    )
    if task_metadata is not None:
        sections.append(section_heading("Task Metadata", theme))
        sections.append(task_metadata)

    if kanban.notify_backlog_subs:
        sections.append(section_heading("Notify Backlog", theme))
        if kanban.notify_backlog_sub_count > len(kanban.notify_backlog_subs):
            # Same disclosure rule as every bounded list: a cap must not read
            # as the whole backlog.
            sections.append(
                Text(
                    f"  showing {len(kanban.notify_backlog_subs)} of "
                    f"{kanban.notify_backlog_sub_count} subscriptions with a backlog",
                    style=theme.banner_text,
                )
            )
        sections.append(_notify_backlog_table(kanban, theme))

    if kanban.recent_runs:
        sections.append(section_heading("Recent Runs", theme))
        sections.append(_recent_runs_table(kanban, theme))

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


def _summary_table(kanban: KanbanState, theme: Theme) -> Table:
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
    if kanban.board_count:
        summary.add_row("Boards", str(kanban.board_count))
    if kanban.current_board:
        summary.add_row("Current Board", escape(kanban.current_board))
    if kanban.stale_claim_count:
        summary.add_row("Stale Claims", str(kanban.stale_claim_count))
    if kanban.notify_sub_count:
        summary.add_row("Notify Subs", str(kanban.notify_sub_count))
        backlog = (
            f"{kanban.notify_backlog_total} unseen (max {kanban.notify_max_backlog})"
            if kanban.notify_backlog_total
            else "0 unseen"
        )
        summary.add_row("Notify Backlog", backlog)
    if kanban.notify_platform_counts:
        # Named platforms only reach the panel here: the backlog table lists the
        # worst subscriptions, which says nothing about who is watching at all.
        platforms = " · ".join(
            f"{name} {count}" for name, count in sorted(kanban.notify_platform_counts.items())
        )
        if kanban.notify_platforms_truncated:
            platforms += " · truncated"
        summary.add_row("Notify Platforms", escape(platforms))
    if kanban.notify_orphan_profile_count:
        summary.add_row(
            "Orphan Profiles",
            f"{kanban.notify_orphan_profile_count}: "
            + escape(", ".join(kanban.notify_orphan_profiles)),
        )
    return summary


def _notify_backlog_table(kanban: KanbanState, theme: Theme) -> Table:
    """Subscriptions with unseen events; a backlog that only grows means the
    gateway watcher that owns the sub is wedged or gone."""
    subs = Table(box=None, show_header=True, padding=(0, 1))
    subs.add_column("Task", style=theme.ui_accent)
    subs.add_column("Platform", style=theme.banner_text)
    subs.add_column("Mode", style=theme.banner_dim)
    subs.add_column("Profile", style=theme.banner_text)
    subs.add_column("Cursor", justify="right", style=theme.banner_dim)
    subs.add_column("Newest", justify="right", style=theme.banner_dim)
    subs.add_column("Backlog", justify="right", style=theme.ui_warn)
    for sub in kanban.notify_backlog_subs:
        subs.add_row(
            escape(sub.task_id),
            escape(sub.platform) if sub.platform else "—",
            escape(sub.delivery_mode) if sub.delivery_mode else "—",
            escape(sub.notifier_profile) if sub.notifier_profile else "—",
            str(sub.last_event_id),
            str(sub.max_event_id),
            str(sub.backlog),
        )
    return subs


def _boards_table(kanban: KanbanState, theme: Theme) -> Table:
    board_table = Table(box=None, show_header=True, padding=(0, 1))
    board_table.add_column("Board", style=theme.ui_accent)
    board_table.add_column("Current", style=theme.banner_text)
    board_table.add_column("Tasks", justify="right", style=theme.banner_text)
    board_table.add_column("Runs", justify="right", style=theme.banner_text)
    board_table.add_column("Problems", justify="right", style=theme.ui_warn)
    board_table.add_column("Stale", justify="right", style=theme.ui_warn)
    board_table.add_column("Block Kinds", style=theme.banner_dim)
    for board in kanban.boards:
        block_kinds = (
            ", ".join(
                f"{escape(kind)}:{count}" for kind, count in sorted(board.block_kind_counts.items())
            )
            if board.block_kind_counts
            else "—"
        )
        board_table.add_row(
            escape(board.slug),
            "current" if board.current else "—",
            str(board.task_count),
            str(board.run_count),
            str(board.problem_count),
            str(board.stale_claim_count),
            block_kinds,
        )
    return board_table


def _task_links_table(kanban: KanbanState, theme: Theme) -> Table:
    links = Table(box=None, show_header=True, padding=(0, 2))
    links.add_column("Parent", style=theme.ui_accent)
    links.add_column("Child", style=theme.banner_text)
    for link in kanban.task_links:
        links.add_row(escape(link.parent_id), escape(link.child_id))
    return links


def _status_counts_table(kanban: KanbanState, theme: Theme) -> Table:
    status_table = Table(box=None, show_header=True, padding=(0, 2))
    status_table.add_column("Status", style=theme.ui_accent)
    status_table.add_column("Count", justify="right", style=theme.banner_text)
    for status, count in sorted(kanban.status_counts.items()):
        status_table.add_row(escape(status), str(count))
    return status_table


def _recent_runs_table(kanban: KanbanState, theme: Theme) -> Table:
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
    return runs


# Suffixes for a worker pid whose identity is not a plain match. "legacy" rows
# predate fingerprints and are not flagged: only their pid existence is known.
_WORKER_IDENTITY_SUFFIX = {
    WorkerIdentity.REUSED: " reused",
    WorkerIdentity.DEAD: " ✗",
    WorkerIdentity.UNVERIFIED: " ?",
}


def _worker_pid_label(pid: int, identity: WorkerIdentity) -> str:
    """Worker pid with its identity verdict (``reused``: another process holds it)."""
    if not pid:
        return "—"
    return f"{pid}{_WORKER_IDENTITY_SUFFIX.get(identity, '')}"


def _task_table(tasks: list[KanbanTaskSummary], theme: Theme, *, now: float) -> Table:
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
        # Upstream will not re-dispatch a task at the breaker limit: the
        # counter is preserved and recompute_ready skips it, so the card sits
        # in blocked until a human intervenes.
        failures: Text | str = (
            Text(
                f"{task.consecutive_failures}/{task.breaker_limit} breaker tripped",
                style=theme.ui_error,
            )
            if task.breaker_tripped
            else str(task.consecutive_failures)
        )
        table.add_row(
            escape(task.task_id),
            escape(task.status),
            escape(task.assignee) if task.assignee else "—",
            _worker_pid_label(task.worker_pid, task.worker_identity),
            _age_label(task.last_heartbeat_at, now),
            failures,
            escape(task.branch_name) if task.branch_name else "—",
            escape(task.title),
        )
    return table


def _task_metadata_table(
    tasks: list[KanbanTaskSummary], theme: Theme, *, now: float
) -> Table | None:
    # The same task can appear in more than one input list (e.g. an active task
    # that also matches the recent-tasks query); dedup by task_id, first wins.
    seen: set[str] = set()
    metadata_tasks: list[KanbanTaskSummary] = []
    for task in tasks:
        if task.task_id in seen:
            continue
        if (
            task.completed_at
            or task.workspace_path
            or task.goal_mode
            or task.current_step_key
            or task.branch_name
            or task.completion_contract
        ):
            seen.add(task.task_id)
            metadata_tasks.append(task)
    if not metadata_tasks:
        return None
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Task", style=theme.ui_accent)
    table.add_column("Branch", style=theme.banner_dim)
    table.add_column("Completed", justify="right", style=theme.banner_dim)
    table.add_column("Workspace", style=theme.banner_text)
    table.add_column("Goal Mode", style=theme.banner_dim)
    table.add_column("Step", style=theme.banner_dim)
    table.add_column("Contract", style=theme.banner_dim)
    for task in metadata_tasks:
        # "" is the local-only default; OWNER/REPO publishes a PR; a PR URL
        # gates completion on repository exact-head CI, so a review card that
        # looks done may be waiting on CI.
        table.add_row(
            escape(task.task_id),
            escape(task.branch_name) if task.branch_name else "—",
            _age_label(task.completed_at, now),
            escape(task.workspace_path) if task.workspace_path else "—",
            escape(task.goal_mode) if task.goal_mode else "—",
            escape(task.current_step_key) if task.current_step_key else "—",
            escape(task.completion_contract[:48]) if task.completion_contract else "—",
        )
    return table


def _age_label(timestamp: int, now: float) -> str:
    if timestamp <= 0:
        return "—"
    return fmt_age_seconds(int(now) - timestamp)
