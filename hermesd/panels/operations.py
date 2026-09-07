from __future__ import annotations

import time

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import DashboardState, OperationsState
from hermesd.panels.formatting import escape_terminal_text as escape
from hermesd.panels.formatting import fmt_age_seconds
from hermesd.theme import Theme


def render_operations(state: DashboardState, theme: Theme, detail: bool = False) -> Panel:
    if detail:
        return _render_detail(state, theme)
    return _render_compact(state, theme)


def _render_compact(state: DashboardState, theme: Theme) -> Panel:
    ops = state.operations
    model_count = sum(cache.model_count for cache in ops.model_caches)
    lines = Text()
    lines.append("  Dashboard: ", style=theme.ui_label)
    lines.append(f"{ops.dashboard_process_count} proc\n", style=theme.banner_text)
    lines.append("  Model Caches: ", style=theme.ui_label)
    lines.append(f"{len(ops.model_caches)} files  {model_count} models\n", style=theme.banner_text)
    lines.append("  PR Monitors: ", style=theme.ui_label)
    lines.append(f"{len(ops.pr_monitors)}\n", style=theme.banner_text)
    lines.append("  Projects: ", style=theme.ui_label)
    lines.append(f"{ops.project_count}\n", style=theme.banner_text)
    lines.append("  MoA Traces: ", style=theme.ui_label)
    lines.append(str(ops.moa_trace_count), style=theme.banner_text)
    lines.append("\n")
    delegation_line = _delegation_summary_line(ops)
    if delegation_line:
        lines.append("  Delegations: ", style=theme.ui_label)
        lines.append(f"{delegation_line}\n", style=theme.banner_text)
    lines.append("  Verify: ", style=theme.ui_label)
    if ops.verification_db_present:
        lines.append(
            f"{ops.verification_event_count} events  {ops.verification_failed_count} failed",
            style=theme.banner_text,
        )
    else:
        lines.append("no ledger", style=theme.banner_dim)
    return Panel(
        lines,
        title=f"[{theme.panel_title_style}]\\[12] Operations[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(0, 1),
    )


def _render_detail(state: DashboardState, theme: Theme) -> Panel:
    ops = state.operations
    sections: list[RenderableType] = [_summary_table(ops, theme)]

    if ops.model_caches:
        sections.append(_heading("Model Caches", theme))
        sections.append(_model_caches_table(ops, theme))

    if ops.pr_monitors:
        sections.append(_heading("PR Monitors", theme))
        sections.append(_pr_monitors_table(ops, theme))

    if ops.verification_db_present:
        sections.extend(_verification_sections(ops, theme))

    if ops.goals:
        sections.append(_heading("Goals", theme))
        sections.append(_goals_table(ops, theme))

    if ops.delegation_count:
        sections.append(
            _heading(
                f"Delegations ({ops.delegation_count} total · "
                f"{ops.delegation_live_log_count} live logs)",
                theme,
            )
        )
        sections.append(_delegations_table(ops, theme))

    if ops.state_db_size_bytes or ops.state_db_schema_version:
        sections.append(_heading("State DB", theme))
        sections.append(_state_db_table(ops, theme))

    if ops.moa_trace_count:
        sections.append(_heading("MoA Traces", theme))
        sections.append(_moa_table(ops, theme))

    if ops.projects_db_present:
        sections.extend(_projects_sections(ops, theme))

    if _has_no_artifacts(ops):
        sections.append(Text("\n  No operations artifacts found\n", style=theme.banner_dim))

    return Panel(
        Group(*sections),
        title=f"[{theme.panel_title_style}]\\[12] Operations[/]",
        title_align="left",
        border_style=theme.panel_border_style,
        box=rich.box.HORIZONTALS,
        padding=(1, 2),
    )


def _heading(label: str, theme: Theme) -> Text:
    return Text(f"\n{label}\n", style=f"bold {theme.ui_label}")


def _has_no_artifacts(ops: OperationsState) -> bool:
    return (
        not ops.model_caches
        and not ops.pr_monitors
        and ops.dashboard_process_count == 0
        and not ops.response_store_present
        and not ops.verification_db_present
        and not ops.moa_trace_count
        and not ops.projects_db_present
        and not ops.goal_count
        and not ops.delegation_count
        and not ops.snapshot_count
        and not ops.state_db_size_bytes
        and not ops.web_ui_build_hash
    )


def _summary_table(ops: OperationsState, theme: Theme) -> Table:
    summary = Table(box=None, show_header=False, padding=(0, 2))
    summary.add_column("Key", style=theme.ui_label)
    summary.add_column("Value", style=theme.banner_text)
    summary.add_row("Dashboard Processes", str(ops.dashboard_process_count))
    summary.add_row(
        "Desktop Build", escape(ops.desktop_build_stamp) if ops.desktop_build_stamp else "—"
    )
    if ops.response_store_present:
        summary.add_row(
            "Response Store",
            f"{ops.conversation_count} conversations  {ops.response_count} responses  "
            f"{_size_label(ops.response_store_size_bytes)}",
        )
    if ops.verification_db_present:
        summary.add_row(
            "Verification",
            f"{ops.verification_event_count} events  {ops.verification_failed_count} failed  "
            f"{ops.verification_state_count} roots",
        )
    if ops.moa_trace_count:
        summary.add_row(
            "MoA Traces",
            f"{ops.moa_trace_count} files  {_size_label(ops.moa_trace_size_bytes)}  "
            f"newest={escape(ops.moa_trace_newest_session_id) or '—'}",
        )
    if ops.projects_db_present:
        summary.add_row(
            "Projects",
            f"{ops.project_count} projects  {ops.project_archived_count} archived  "
            f"{ops.project_folder_count} folders  {ops.discovered_repo_count} discovered  "
            f"{ops.project_missing_primary_path_count} missing paths",
        )
    if ops.goal_count:
        summary.add_row(
            "Goals",
            f"{ops.goal_count} goals  {ops.active_goal_count} active  "
            f"{ops.waiting_goal_count} waiting",
        )
    if ops.web_ui_build_hash or ops.web_ui_built_age_seconds is not None:
        summary.add_row(
            "Web UI Build",
            f"{escape(ops.web_ui_build_hash) or '—'}  "
            f"built {_age_span_label(ops.web_ui_built_age_seconds)} ago",
        )
    if ops.snapshot_count:
        summary.add_row(
            "Snapshots",
            f"{ops.snapshot_count} · {_size_label(ops.snapshot_total_bytes)} · "
            f"newest {_age_span_label(ops.newest_snapshot_age_seconds)} ago",
        )
    return summary


def _delegation_summary_line(ops: OperationsState) -> str:
    if not (
        ops.delegation_running_count
        or ops.delegation_failed_count
        or ops.delegation_undelivered_count
    ):
        return ""
    return (
        f"{ops.delegation_running_count} running · "
        f"{ops.delegation_failed_count} failed · "
        f"{ops.delegation_undelivered_count} undelivered"
    )


def _delegations_table(ops: OperationsState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("ID", style=theme.ui_accent)
    table.add_column("State", style=theme.banner_text)
    table.add_column("Delivery", style=theme.banner_text)
    table.add_column("Owner", style=theme.banner_dim)
    table.add_column("Took", justify="right", style=theme.banner_dim)
    table.add_column("Goal", style=theme.banner_text)
    table.add_column("Result", style=theme.banner_dim)
    for delegation in ops.delegations:
        delivery = escape(delegation.delivery_state) or "—"
        if delegation.delivery_attempts:
            delivery = f"{delivery} x{delegation.delivery_attempts}"
        table.add_row(
            escape(delegation.delegation_id) or "—",
            escape(delegation.state) or "—",
            delivery,
            "alive" if delegation.owner_alive else "—",
            _duration_label(delegation.duration_seconds),
            escape(delegation.goal) or "—",
            escape(delegation.error_excerpt) or escape(delegation.result_status) or "—",
        )
    if not ops.delegations:
        table.add_row("—", "—", "—", "—", "—", "—", "—")
    return table


def _state_db_table(ops: OperationsState, theme: Theme) -> Table:
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.banner_text)
    table.add_row("Schema Version", str(ops.state_db_schema_version))
    table.add_row(
        "Size",
        f"{_size_label(ops.state_db_size_bytes)} db · "
        f"{_size_label(ops.state_db_wal_size_bytes)} wal",
    )
    table.add_row("Last Auto Prune", _age_span_label(ops.last_auto_prune_age_seconds))
    table.add_row("Last Auto Archive", _age_span_label(ops.last_auto_archive_age_seconds))
    table.add_row("File Generation", escape(ops.state_db_file_generation) or "—")
    table.add_row("FTS Storage", escape(ops.state_db_fts_storage_version) or "—")
    return table


def _model_caches_table(ops: OperationsState, theme: Theme) -> Table:
    cache_table = Table(box=None, show_header=True, padding=(0, 1))
    cache_table.add_column("File", style=theme.ui_accent)
    cache_table.add_column("Providers", justify="right", style=theme.banner_text)
    cache_table.add_column("Models", justify="right", style=theme.banner_text)
    cache_table.add_column("Size", justify="right", style=theme.banner_dim)
    cache_table.add_column("Age", style=theme.banner_dim)
    for cache in ops.model_caches:
        cache_table.add_row(
            escape(cache.name),
            str(cache.provider_count),
            str(cache.model_count),
            _size_label(cache.size_bytes),
            _age_label(cache.mtime),
        )
    return cache_table


def _pr_monitors_table(ops: OperationsState, theme: Theme) -> Table:
    pr_table = Table(box=None, show_header=True, padding=(0, 1))
    pr_table.add_column("File", style=theme.ui_accent)
    pr_table.add_column("Repo", style=theme.banner_text)
    pr_table.add_column("Checked", style=theme.banner_dim)
    pr_table.add_column("Monitored", justify="right", style=theme.banner_text)
    pr_table.add_column("Tracked", justify="right", style=theme.banner_text)
    pr_table.add_column("Author", justify="right", style=theme.banner_text)
    for monitor in ops.pr_monitors:
        pr_table.add_row(
            escape(monitor.filename),
            escape(monitor.repo) if monitor.repo else "—",
            escape(monitor.checked_at) if monitor.checked_at else "—",
            str(monitor.monitored_count),
            str(monitor.tracked_count),
            str(monitor.author_pr_count),
        )
    return pr_table


def _verification_sections(ops: OperationsState, theme: Theme) -> list[RenderableType]:
    sections: list[RenderableType] = [_heading("Verification Evidence", theme)]
    if ops.verification_latest_events:
        sections.append(_verification_events_table(ops, theme))
    else:
        sections.append(Text("  No verification events recorded\n", style=theme.banner_dim))
    if ops.verification_roots:
        sections.append(_verification_roots_table(ops, theme))
    return sections


def _verification_events_table(ops: OperationsState, theme: Theme) -> Table:
    event_table = Table(box=None, show_header=True, padding=(0, 1))
    event_table.add_column("Status", style=theme.banner_text)
    event_table.add_column("Kind", style=theme.ui_accent)
    event_table.add_column("Scope", style=theme.banner_dim)
    event_table.add_column("Command", style=theme.banner_text)
    event_table.add_column("Exit", justify="right", style=theme.banner_dim)
    event_table.add_column("Summary", style=theme.banner_dim)
    for event in ops.verification_latest_events:
        command = event.canonical_command or event.command
        event_table.add_row(
            escape(event.status or "unknown"),
            escape(event.kind or "—"),
            escape(event.scope or "—"),
            escape(command) if command else "—",
            str(event.exit_code),
            escape(event.output_summary) if event.output_summary else "—",
        )
    return event_table


def _verification_roots_table(ops: OperationsState, theme: Theme) -> Table:
    root_table = Table(box=None, show_header=True, padding=(0, 1))
    root_table.add_column("Root", style=theme.ui_accent)
    root_table.add_column("Session", style=theme.banner_text)
    root_table.add_column("Last Edit", style=theme.banner_dim)
    root_table.add_column("Pending", justify="right", style=theme.banner_text)
    for root in ops.verification_roots:
        root_table.add_row(
            escape(root.root) if root.root else "—",
            escape(root.session_id) if root.session_id else "—",
            escape(root.last_edit_at) if root.last_edit_at else "—",
            f"{root.changed_path_count} changed",
        )
    return root_table


def _goals_table(ops: OperationsState, theme: Theme) -> Table:
    goal_table = Table(box=None, show_header=True, padding=(0, 1))
    goal_table.add_column("Session", style=theme.ui_accent)
    goal_table.add_column("Status", style=theme.banner_text)
    goal_table.add_column("Turns", style=theme.banner_dim)
    goal_table.add_column("Contract", style=theme.banner_text)
    goal_table.add_column("Waiting", style=theme.banner_dim)
    goal_table.add_column("Goal", style=theme.banner_text)
    for goal in ops.goals:
        waiting = _goal_waiting_label(goal.waiting_on_pid, goal.waiting_on_session)
        if goal.waiting_reason:
            waiting = f"{waiting} {goal.waiting_reason}".strip()
        goal_table.add_row(
            escape(goal.session_id),
            escape(goal.status),
            f"{goal.turns_used}/{goal.max_turns}" if goal.max_turns else str(goal.turns_used),
            "contract" if goal.has_contract else "—",
            escape(waiting) if waiting else "—",
            escape(goal.goal),
        )
    return goal_table


def _moa_table(ops: OperationsState, theme: Theme) -> Table:
    moa_table = Table(box=None, show_header=False, padding=(0, 2))
    moa_table.add_column("Key", style=theme.ui_label)
    moa_table.add_column("Value", style=theme.banner_text)
    moa_table.add_row("Files", str(ops.moa_trace_count))
    moa_table.add_row("Size", _size_label(ops.moa_trace_size_bytes))
    moa_table.add_row(
        "Newest Session",
        escape(ops.moa_trace_newest_session_id) if ops.moa_trace_newest_session_id else "—",
    )
    moa_table.add_row("Newest Age", _age_label(ops.moa_trace_newest_mtime))
    if ops.moa_trace_latest_record_summary:
        moa_table.add_row("Latest Record", escape(ops.moa_trace_latest_record_summary))
    if ops.moa_trace_latest_record_keys:
        moa_table.add_row("Latest Keys", escape(", ".join(ops.moa_trace_latest_record_keys)))
    return moa_table


def _projects_sections(ops: OperationsState, theme: Theme) -> list[RenderableType]:
    sections: list[RenderableType] = [
        _heading("Projects", theme),
        _projects_table(ops, theme),
    ]
    if ops.discovered_repos:
        sections.append(_heading("Newest Discovered Repos", theme))
        sections.append(_discovered_repos_table(ops, theme))
    return sections


def _projects_table(ops: OperationsState, theme: Theme) -> Table:
    project_table = Table(box=None, show_header=True, padding=(0, 1))
    project_table.add_column("Slug", style=theme.ui_accent)
    project_table.add_column("Name", style=theme.banner_text)
    project_table.add_column("Board", style=theme.ui_label)
    project_table.add_column("Path", style=theme.banner_dim)
    project_table.add_column("Verify", style=theme.banner_text)
    project_table.add_column("Kanban", style=theme.banner_text)
    project_table.add_column("State", style=theme.banner_text)
    for project in ops.projects:
        project_table.add_row(
            escape(project.slug) if project.slug else "—",
            escape(project.name) if project.name else "—",
            escape(project.board_slug) if project.board_slug else "—",
            escape(project.primary_path) if project.primary_path else "—",
            f"{project.verification_root_count} verified roots"
            if project.verification_root_count
            else "—",
            "board present" if project.kanban_board_present else "—",
            "archived" if project.archived else "active",
        )
    if not ops.projects:
        project_table.add_row("—", "—", "—", "—", "—", "—", "—")
    return project_table


def _discovered_repos_table(ops: OperationsState, theme: Theme) -> Table:
    repo_table = Table(box=None, show_header=True, padding=(0, 1))
    repo_table.add_column("Root", style=theme.ui_accent)
    repo_table.add_column("Label", style=theme.banner_text)
    repo_table.add_column("Last Seen", style=theme.banner_dim)
    for repo in ops.discovered_repos:
        repo_table.add_row(
            escape(repo.root) if repo.root else "—",
            escape(repo.label) if repo.label else "—",
            escape(repo.last_seen) if repo.last_seen else "—",
        )
    return repo_table


def _duration_label(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return fmt_age_seconds(max(0, int(seconds)))


def _age_span_label(seconds: float | None) -> str:
    """Compact age label with a day tier for prune/archive/snapshot ages."""
    if seconds is None:
        return "—"
    if seconds < 86400:
        return _duration_label(seconds)
    return f"{int(seconds // 86400)}d"


def _size_label(size_bytes: int) -> str:
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.1f}G"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.1f}M"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.1f}K"
    return str(size_bytes)


def _age_label(timestamp: float | None) -> str:
    if timestamp is None:
        return "—"
    try:
        age = max(0, int(time.time() - timestamp))
    except (OverflowError, OSError, ValueError):
        return "—"
    return fmt_age_seconds(age)


def _goal_waiting_label(waiting_on_pid: int, waiting_on_session: str) -> str:
    if waiting_on_pid:
        return f"pid {waiting_on_pid}"
    if waiting_on_session:
        return f"session {waiting_on_session}"
    return ""
