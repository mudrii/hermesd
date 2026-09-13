from __future__ import annotations

import rich.box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hermesd.models import (
    API_RUN_RETENTION_SECONDS,
    CHECKPOINT_PRUNE_INTERVAL_SECONDS,
    CHECKPOINT_PRUNE_OVERDUE_AFTER_SECONDS,
    HOSTED_ROOM_DISBANDED_RETENTION_SECONDS,
    MAX_ACTIVE_HOSTED_ROOMS,
    MAX_DISBANDED_HOSTED_ROOM_TOMBSTONES,
    MAX_EVENTS_PER_HOSTED_ROOM,
    MAX_PERSISTENT_REPAIR_ATTEMPTS,
    PROCESS_RECEIPT_MAX_FILES,
    PROCESS_RECEIPT_RETENTION_SECONDS,
    ApiRunReservation,
    ApiRunReservationsState,
    DashboardState,
    DbRecoveryState,
    DelegationInfo,
    DelegationLiveManifest,
    HostedRoomState,
    HostedRoomSummary,
    OperationsState,
    ProcessReceiptsState,
)
from hermesd.panels.formatting import escape_terminal_text as escape
from hermesd.panels.formatting import fmt_age_seconds, sanitize_terminal_text
from hermesd.theme import Theme

# Rendered under the Database Recovery section. Every line is a limit on what the
# numbers above can mean, so the section is never read as a health verdict.
_RECOVERY_NOTE_LINES = (
    "Presence and metadata only: hermesd never repairs, checkpoints, integrity-checks or",
    "hashes the database.",
    'A successful repair deletes the ledger, so "none" is not evidence the database is healthy.',
    '"budget exhausted" counts the ledger\'s recorded failures; hermesd does not recompute the',
    "fingerprint upstream matches them against, so it cannot tell whether the file changed since.",
)

# Rendered under Hosted Rooms. The first block is the content-free contract, the
# second is the ROOT scope (the one thing an operator is most likely to get
# wrong, because every other database in this panel is profile-scoped or root
# for a different reason), and the third is upstream's own ceilings.
_HOSTED_ROOM_NOTE_LINES = (
    "Content-free by construction: grants, link catalogs and target URLs, event payloads and",
    "actors, revoked-grant scope keys and the hosted_room_policy_transcript* tables are never",
    "selected. Only counts, ids, names, timestamps, epochs, revisions, event bytes and the",
    "closed event-kind vocabulary leave the file.",
    "ROOT-scoped: <root>/shared-state.db is authoritative even under --profile; hermesd",
    "never reads the legacy hosted_room* tables in state.db for this section.",
    f"Upstream ceilings: {MAX_ACTIVE_HOSTED_ROOMS} active rooms, "
    f"{MAX_DISBANDED_HOSTED_ROOM_TOMBSTONES} disbanded tombstones retained "
    f"{HOSTED_ROOM_DISBANDED_RETENTION_SECONDS // 86400} days, "
    f"{MAX_EVENTS_PER_HOSTED_ROOM:,} events per room.",
)

# Rendered under Retained API Run Reservations. The first two blocks are the
# reason the section is not called "API Runs": an empty store is uninformative,
# and hermesd cannot see upstream's in-memory fallback at all.
_API_RUN_NOTE_LINES = (
    '"Retained", not "recent": upstream prunes an aged row only once its run status',
    f"is terminal, and long room runs extend the {API_RUN_RETENTION_SECONDS // 3600}h window — so",
    "an empty store is not evidence that the API was idle.",
    "The file may also be absent or stale while the gateway is actively reserving: when it",
    "cannot be opened upstream falls back to process memory, and that capability is served over",
    "HTTP only, never written to disk, so hermesd cannot detect the fallback.",
    "fingerprint, idempotency_key and scope are never read; the tenant scope appears only as a",
    "distinct count. An unrecognized run status is reported as unknown, never dropped or",
    "guessed.",
    "owner_started is platform-dependent units (/proc ticks on Linux, psutil centiseconds",
    "elsewhere), so it is reduced to 'identity recorded' and never compared to a timestamp.",
)

# Rendered under Process Receipts: the two lines an operator needs to not
# over-read an empty list — retention empties it legitimately, and the scope
# differs from every other registry in this panel.
_RECEIPT_NOTE_LINES = (
    f"Receipts are retained upstream for {PROCESS_RECEIPT_RETENTION_SECONDS // 86400} days / "
    f'{PROCESS_RECEIPT_MAX_FILES} files, so "no receipts yet" is normal on a quiet machine.',
    "PROFILE-scoped: <home>/logs/process-results/ — the ROOT spawn-ledger.json is a different",
    "registry. Command and output are redacted again before rendering; cwd and session keys",
    "are never read.",
)

# Rendered under Live Delegation Transcripts. The first block is the thing an
# operator will most expect and hermesd cannot have: the live roster (per-task
# tool counts, steer state, depth) exists only in gateway memory and over RPC.
# The second covers the writer's own best-effort status updates.
_LIVE_MANIFEST_NOTE_LINES = (
    "Manifests and task-log tails only: the live roster — per-task tool counts, steer",
    "state, depth — exists in gateway memory and over RPC, and hermesd cannot see it.",
    "Task statuses are the writer's best-effort post-join updates; a crashed join",
    "leaves tasks marked running. Tails are redacted again before rendering.",
)

# Rendered whenever a bounded list was cut short, so a display cap can never be
# mistaken for the size of the table it came from.
_TRUNCATION_LABEL = "showing {shown} of {total} — the counts above cover the whole table"


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
    if ops.delegation_live_manifests:
        running = sum(m.running_task_count for m in ops.delegation_live_manifests)
        lines.append("  Live delegations: ", style=theme.ui_label)
        lines.append(
            f"{ops.delegation_live_manifest_count} live · {running} running\n",
            style=theme.banner_text,
        )
    # Counts only, like every other compact row here: room names and run ids are
    # untrusted free text and belong to the detail view.
    if ops.hosted_rooms.db_present:
        lines.append("  Hosted Rooms: ", style=theme.ui_label)
        lines.append(
            f"{ops.hosted_rooms.room_count} rooms · "
            f"{ops.hosted_rooms.active_room_count} active · "
            f"{ops.hosted_rooms.disbanded_room_count} disbanded\n",
            style=theme.banner_text,
        )
    if ops.api_runs.db_present:
        lines.append("  API Runs: ", style=theme.ui_label)
        lines.append(f"{ops.api_runs.reservation_count} retained\n", style=theme.banner_text)
    if ops.process_receipts.receipt_count:
        lines.append("  Finished procs: ", style=theme.ui_label)
        lines.append(f"{ops.process_receipts.receipt_count} receipts\n", style=theme.banner_text)
    if ops.blocked_script_count:
        lines.append("  Blocked scripts: ", style=theme.ui_label)
        lines.append(f"{ops.blocked_script_count}\n", style=theme.ui_warn)
    if ops.checkpoint_prune_overdue:
        age = ops.checkpoint_prune_marker_age_seconds
        lines.append("  ⚠ Checkpoint prune overdue ", style=theme.ui_warn)
        lines.append(
            f"({_age_span_label(age)} since last pass)\n",
            style=theme.ui_warn,
        )
    if ops.spawn_ledger_corrupt_present:
        lines.append("  ⚠ spawn-ledger corrupt ", style=theme.ui_warn)
        lines.append(
            f"(parked {_age_span_label(ops.spawn_ledger_corrupt_age_seconds)} ago)\n",
            style=theme.ui_warn,
        )
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
        sections.append(_model_caches_table(ops, theme, now=state.collected_at))

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

    if ops.delegation_live_manifests:
        sections.append(_heading("Live Delegation Transcripts", theme))
        sections.extend(_live_manifest_sections(ops, theme))
        sections.append(_note(_LIVE_MANIFEST_NOTE_LINES, theme))

    # Always stated, even when the directory has never existed: an absent
    # receipt store is the normal case on a quiet machine, not a failure.
    sections.extend(_receipt_sections(ops.process_receipts, theme))

    if ops.state_db_size_bytes or ops.state_db_schema_version:
        sections.append(_heading("State DB", theme))
        sections.append(_state_db_table(ops, theme))

    # Recovery evidence sits beside the State DB section because it describes
    # artifacts of that same file. Rendered whenever there is a database to have
    # left them, so "no ledger" is stated rather than silently omitted.
    if ops.db_recovery.artifacts_present or ops.state_db_size_bytes:
        sections.extend(_recovery_section(ops.db_recovery, theme))

    if ops.moa_trace_count:
        sections.append(_heading("MoA Traces", theme))
        sections.append(_moa_table(ops, theme, now=state.collected_at))

    if ops.projects_db_present:
        sections.extend(_projects_sections(ops, theme))

    # Both coordination stores render whether or not they hold rows: presence is
    # the fact worth showing, and an empty one carries a caveat that must not be
    # silently omitted.
    if ops.hosted_rooms.db_present:
        sections.extend(_hosted_room_sections(ops.hosted_rooms, theme))

    if ops.api_runs.db_present:
        sections.extend(_api_run_sections(ops.api_runs, theme))

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
        and not ops.delegation_live_manifests
        and not ops.process_receipts.receipt_count
        and not ops.snapshot_count
        and not ops.state_db_size_bytes
        and not ops.web_ui_build_hash
        and not ops.blocked_script_count
        and not ops.db_recovery.artifacts_present
        and not ops.hosted_rooms.db_present
        and not ops.api_runs.db_present
    )


def _note(lines: tuple[str, ...], theme: Theme) -> Text:
    """A dimmed block of caveats. Literal text, never markup-parsed."""
    return Text("\n".join(f"  {line}" for line in lines), style=theme.banner_dim)


def _truncation_label(shown: int, total: int) -> str:
    return _TRUNCATION_LABEL.format(shown=shown, total=total)


def _hosted_room_sections(hosted: HostedRoomState, theme: Theme) -> list[RenderableType]:
    sections: list[RenderableType] = [
        _heading("Hosted Rooms", theme),
        _hosted_room_summary_table(hosted, theme),
    ]
    if hosted.rooms:
        sections.append(_hosted_rooms_table(hosted, theme))
    sections.append(_note(_HOSTED_ROOM_NOTE_LINES, theme))
    return sections


def _hosted_room_summary_table(hosted: HostedRoomState, theme: Theme) -> Table:
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.banner_text)
    table.add_row(
        "Rooms",
        f"{hosted.active_room_count} active · {hosted.disbanded_room_count} disbanded",
    )
    table.add_row(
        "Events",
        f"{hosted.event_count} · {_size_label(hosted.accounted_event_bytes)} accounted · "
        f"newest {_age_span_label(hosted.newest_event_age_seconds)} ago",
    )
    kinds = _hosted_event_kind_label(hosted)
    if kinds:
        table.add_row("Event Kinds", kinds)
    if hosted.retired_id_count:
        table.add_row(
            "Retired IDs",
            f"{hosted.retired_id_count} · newest "
            f"{_age_span_label(hosted.newest_retired_id_age_seconds)} ago",
        )
    if hosted.link_count:
        # A count only: every other column of that table is a credential-bearing
        # URL, a grant or a tool catalog.
        table.add_row("Links", f"{hosted.link_count} (targets never read)")
    if hosted.remote_run_count:
        table.add_row(
            "Remote Runs",
            f"{hosted.remote_run_count} · newest "
            f"{_age_span_label(hosted.newest_remote_run_age_seconds)} ago",
        )
    if hosted.revoked_grant_count:
        table.add_row("Revoked Grants", f"{hosted.revoked_grant_count} (scope keys never read)")
    if hosted.peer_reservation_count:
        table.add_row(
            "Peer Reservations",
            f"{hosted.live_peer_reservation_count} live · "
            f"{hosted.expired_peer_reservation_count} expired · "
            f"{hosted.revoked_peer_reservation_count} revoked",
        )
    if hosted.rooms_truncated:
        table.add_row("Room List", _truncation_label(len(hosted.rooms), hosted.room_count))
    return table


def _hosted_event_kind_label(hosted: HostedRoomState) -> str:
    """The closed-vocabulary histogram; keys are escaped even though the reader
    allowlists them, because a panel never trusts what a model can hold."""
    parts = [f"{escape(kind)} {count}" for kind, count in sorted(hosted.event_kind_counts.items())]
    if hosted.unknown_event_kind_count:
        parts.append(f"unknown {hosted.unknown_event_kind_count}")
    return " · ".join(parts)


def _hosted_rooms_table(hosted: HostedRoomState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Room", style=theme.ui_accent)
    table.add_column("Name", style=theme.banner_text)
    table.add_column("Members", justify="right", style=theme.banner_text)
    table.add_column("Epoch", justify="right", style=theme.banner_dim)
    table.add_column("Rev", justify="right", style=theme.banner_dim)
    table.add_column("Latest Seq", justify="right", style=theme.banner_text)
    table.add_column("Events", justify="right", style=theme.banner_dim)
    table.add_column("Updated", style=theme.banner_dim)
    table.add_column("State", style=theme.banner_text)
    for room in hosted.rooms:
        table.add_row(
            escape(room.room_id) or "—",
            escape(room.name) or "—",
            str(room.member_count),
            str(room.authority_epoch),
            str(room.revision),
            str(room.latest_seq),
            _size_label(room.event_bytes),
            _age_span_label(room.updated_at_age_seconds),
            _hosted_room_state_label(room),
        )
    return table


def _hosted_room_state_label(room: HostedRoomSummary) -> str:
    if not room.disbanded:
        return "active"
    return f"disbanded {_age_span_label(room.disbanded_at_age_seconds)} ago"


def _api_run_sections(api_runs: ApiRunReservationsState, theme: Theme) -> list[RenderableType]:
    sections: list[RenderableType] = [
        _heading("Retained API Run Reservations", theme),
        _api_run_summary_table(api_runs, theme),
    ]
    if api_runs.reservations:
        sections.append(_api_runs_table(api_runs, theme))
    else:
        # Stated in words rather than left as an empty table: the whole point of
        # this section is that zero rows is not a verdict.
        sections.append(
            Text(
                "  no reservations retained — not evidence that the API was idle\n",
                style=theme.ui_warn,
            )
        )
    sections.append(_note(_API_RUN_NOTE_LINES, theme))
    return sections


def _api_run_summary_table(api_runs: ApiRunReservationsState, theme: Theme) -> Table:
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.banner_text)
    table.add_row(
        "Store",
        f"{_size_label(api_runs.db_size_bytes)} · {api_runs.reservation_count} reservations · "
        f"{api_runs.scope_count} scopes",
    )
    table.add_row(
        "Ages",
        f"newest {_age_span_label(api_runs.newest_age_seconds)} · "
        f"oldest {_age_span_label(api_runs.oldest_age_seconds)}",
    )
    if api_runs.acknowledged_count:
        table.add_row("Acknowledged", str(api_runs.acknowledged_count))
    if api_runs.owner_recorded_count:
        table.add_row("Owners", f"{api_runs.owner_recorded_count} with a pid recorded")
    if api_runs.retention_expired_count:
        table.add_row(
            "Past Retention",
            f"{api_runs.retention_expired_count} past retention deadline",
        )
    if api_runs.reservations_truncated:
        table.add_row(
            "Reservation List",
            _truncation_label(len(api_runs.reservations), api_runs.reservation_count),
        )
    return table


def _api_runs_table(api_runs: ApiRunReservationsState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Run ID", style=theme.ui_accent)
    table.add_column("Status", style=theme.banner_text)
    table.add_column("Created", style=theme.banner_dim)
    table.add_column("Updated", style=theme.banner_dim)
    table.add_column("Retention", style=theme.banner_dim)
    table.add_column("Owner", style=theme.banner_text)
    for item in api_runs.reservations:
        table.add_row(
            escape(item.run_id) or "—",
            escape(item.status.value),
            _age_span_label(item.created_at_age_seconds),
            _age_span_label(item.updated_at_age_seconds),
            _retention_label(item.retention_remaining_seconds),
            _run_owner_label(item),
        )
    return table


def _retention_label(seconds: float | None) -> str:
    """Time until upstream may prune the row; None means no explicit deadline."""
    if seconds is None:
        return "—"
    if seconds < 0:
        return f"{_duration_label(-seconds)} overdue"
    return f"{_duration_label(seconds)} left"


def _run_owner_label(item: ApiRunReservation) -> str:
    """Ownership, with the pid-reuse caveat made explicit.

    A pid on its own cannot be told apart from a recycled one: upstream pairs it
    with ``owner_started``, whose units are platform-dependent, so hermesd says
    whether an identity was recorded rather than pretending to have verified one.
    """
    if not item.owner_pid_present:
        return "no pid"
    identity = "identity recorded" if item.owner_started_recorded else "identity unverified"
    liveness = "alive" if item.owner_alive else "gone"
    return f"pid {item.owner_pid} {liveness} · {identity}"


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
    if ops.blocked_script_count:
        summary.add_row("Blocked scripts", _blocked_scripts_label(ops))
    if ops.checkpoint_prune_marker_present:
        summary.add_row("Checkpoint Prune", _checkpoint_prune_label(ops))
    if ops.spawn_ledger_corrupt_present:
        summary.add_row("Spawn Ledger", _spawn_ledger_corrupt_label(ops))
    return summary


def _checkpoint_prune_label(ops: OperationsState) -> str:
    """Marker age against upstream's 24h interval, with the overdue verdict.

    The caveat is part of the row: a fresh marker proves the wrapper ran, not
    that pruning succeeded.
    """
    age = _age_span_label(ops.checkpoint_prune_marker_age_seconds)
    verdict = (
        f"OVERDUE (> {_age_span_label(float(CHECKPOINT_PRUNE_OVERDUE_AFTER_SECONDS))})"
        if ops.checkpoint_prune_overdue
        else f"interval {_age_span_label(float(CHECKPOINT_PRUNE_INTERVAL_SECONDS))}"
    )
    return f"last pass {age} ago · {verdict} · a fresh marker proves the wrapper ran, not that pruning succeeded"


def _spawn_ledger_corrupt_label(ops: OperationsState) -> str:
    """The parked corrupt ledger: presence and age, contents never read."""
    return (
        f"⚠ corrupt ledger parked {_age_span_label(ops.spawn_ledger_corrupt_age_seconds)} ago "
        "(read-only viewer; contents never parsed)"
    )


def _blocked_scripts_label(ops: OperationsState) -> str:
    """Refused-shell-script count, newest age and names — never their contents."""
    names = ", ".join(escape(name) for name in ops.blocked_script_names)
    label = (
        f"{ops.blocked_script_count} "
        f"(newest {_age_span_label(ops.newest_blocked_script_age_seconds)} ago)"
    )
    return f"{label}: {names}" if names else label


def _recovery_section(recovery: DbRecoveryState, theme: Theme) -> list[RenderableType]:
    """What hermes-agent's repair code left beside ``state.db``.

    The ledger row is always present, including when no ledger exists: absence is
    the ambiguous case (a successful repair deletes the file), so it is stated in
    words rather than left for the operator to infer from a missing row.
    """
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Key", style=theme.ui_label)
    table.add_column("Value", style=theme.banner_text)
    table.add_row("Repair Ledger", _repair_ledger_label(recovery))
    if recovery.malformed_backup_count:
        table.add_row("Malformed Backups", _malformed_backup_label(recovery))
    if recovery.retired_wal_count:
        table.add_row("Retired WAL", _retired_wal_label(recovery))
    table.add_row("Lock Files", _lock_files_label(recovery))
    if recovery.malformed_backup_staging_count or recovery.retired_wal_staging_count:
        table.add_row("In Progress", _staging_label(recovery))
    if recovery.scan_truncated:
        table.add_row(
            "Scan",
            "truncated — counts above are a floor, not an inventory",
        )
    return [_heading("Database Recovery", theme), table, _note(_RECOVERY_NOTE_LINES, theme)]


def _repair_ledger_label(recovery: DbRecoveryState) -> str:
    """The attempt ledger, or the honest reading of its absence."""
    if not recovery.repair_ledger_present:
        return "none — no failed repair recorded"
    if recovery.repair_budget_exhausted:
        budget = f"budget exhausted (max {MAX_PERSISTENT_REPAIR_ATTEMPTS})"
    else:
        budget = f"budget {recovery.failed_attempts}/{MAX_PERSISTENT_REPAIR_ATTEMPTS}"
    last = _age_span_label(recovery.last_attempt_age_seconds)
    return f"{recovery.failed_attempts} failed · {budget} · last {last} ago"


def _malformed_backup_label(recovery: DbRecoveryState) -> str:
    """Settled forensic copies only; staging is its own row."""
    return (
        f"{recovery.malformed_backup_count} · {_size_label(recovery.malformed_backup_bytes)} · "
        f"newest {_age_span_label(recovery.newest_malformed_backup_age_seconds)} ago"
    )


def _retired_wal_label(recovery: DbRecoveryState) -> str:
    """Generation count plus the newest capture's own manifest metadata."""
    parts = [f"{recovery.retired_wal_count} generation(s)"]
    newest = recovery.newest_retired_wal
    if not newest.manifest_present:
        parts.append("newest has no readable manifest.json")
        return " · ".join(parts)
    age = _age_span_label(newest.captured_at_age_seconds)
    parts.append(f"newest {escape(newest.captured_at) or '—'} ({age} ago)")
    parts.append(f"trigger={escape(newest.trigger) or '—'}")
    parts.append(f"wal {_size_label(newest.wal_bytes)}")
    parts.append(f"main {escape(newest.main_mode) or '—'}")
    return " · ".join(parts)


def _lock_files_label(recovery: DbRecoveryState) -> str:
    """File presence, which is not the same as a held lock."""
    repair = "✓" if recovery.repair_lock_file_present else "✗"
    maintenance = "✓" if recovery.auto_maintenance_lock_file_present else "✗"
    return f"repair {repair} · auto-maintenance {maintenance} (file present, not held)"


def _staging_label(recovery: DbRecoveryState) -> str:
    """Mid-write artifacts, kept out of every settled count above."""
    return (
        f"{recovery.malformed_backup_staging_count} backup staging · "
        f"{recovery.retired_wal_staging_count} retired-wal partial"
    )


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
    table.add_column("Procs", style=theme.banner_dim)
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
            _delegation_procs_label(delegation),
        )
    if not ops.delegations:
        table.add_row("—", "—", "—", "—", "—", "—", "—", "—")
    return table


def _live_manifest_sections(ops: OperationsState, theme: Theme) -> list[RenderableType]:
    """One card per parsed manifest, newest first, capped by the collector."""
    parts: list[RenderableType] = []
    for manifest in ops.delegation_live_manifests:
        table = Table(box=None, show_header=False, padding=(0, 2))
        table.add_column("Key", style=theme.ui_label)
        table.add_column("Value", style=theme.banner_text)
        table.add_row("Delegation", escape(manifest.delegation_id) or "—")
        label = " / ".join(part for part in (manifest.provider, manifest.model) if part)
        table.add_row("Model", escape(label) or "—")
        table.add_row(
            "Tasks", f"{manifest.task_count} total · {manifest.running_task_count} running"
        )
        if manifest.started:
            table.add_row("Started", escape(manifest.started))
        if manifest.completed:
            table.add_row("Completed", escape(manifest.completed))
        table.add_row("Dir Age", _age_span_label(manifest.dir_age_seconds))
        if manifest.tasks_truncated:
            table.add_row("Tasks", _truncation_label(len(manifest.tasks), manifest.task_count))
        parts.append(table)
        parts.append(_live_tasks_text(manifest, theme))
    shown = len(ops.delegation_live_manifests)
    if shown < ops.delegation_live_manifest_count:
        parts.append(
            Text(
                "  "
                + _truncation_label(shown, ops.delegation_live_manifest_count)
                + " — newest first\n",
                style=theme.banner_dim,
            )
        )
    return parts


def _live_tasks_text(manifest: DelegationLiveManifest, theme: Theme) -> Text:
    """Per-task status lines with the optional redacted log tail.

    Rich Text is literal (never markup-parsed), but the strings are escaped
    anyway so the model layer stays untrusted end to end.
    """
    lines = Text()
    if not manifest.tasks:
        lines.append("  no task entries parsed\n", style=theme.banner_dim)
        return lines
    for task in manifest.tasks:
        # Text() never parses markup, so sanitize (strip ANSI/control codes)
        # rather than escape(): escaping here would show literal backslashes.
        status = sanitize_terminal_text(task.status) or "unknown"
        label = f"  task {task.index} · {status}"
        if task.exit_reason:
            label += f" · exit {sanitize_terminal_text(task.exit_reason)}"
        lines.append(label + "\n", style=theme.banner_text)
        for tail_line in task.log_tail:
            lines.append(f"    {sanitize_terminal_text(tail_line)}\n", style=theme.banner_dim)
    return lines


def _receipt_sections(receipts: ProcessReceiptsState, theme: Theme) -> list[RenderableType]:
    parts: list[RenderableType] = [_heading("Process Receipts", theme)]
    if receipts.receipts:
        parts.append(_receipts_table(receipts, theme))
        if receipts.receipts_truncated:
            parts.append(
                Text(
                    "  "
                    + _truncation_label(len(receipts.receipts), receipts.receipt_count)
                    + " — newest first\n",
                    style=theme.banner_dim,
                )
            )
    else:
        parts.append(
            Text(
                "  no receipts yet — background processes that finish while unwatched land here\n",
                style=theme.banner_dim,
            )
        )
    parts.append(_note(_RECEIPT_NOTE_LINES, theme))
    return parts


def _receipts_table(receipts: ProcessReceiptsState, theme: Theme) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1))
    table.add_column("Process", style=theme.ui_accent)
    table.add_column("Exit", justify="right", style=theme.banner_text)
    table.add_column("Reason", style=theme.banner_dim)
    table.add_column("Finished", style=theme.banner_dim)
    table.add_column("Command", style=theme.banner_text)
    table.add_column("Output Tail", style=theme.banner_dim)
    for receipt in receipts.receipts:
        table.add_row(
            escape(receipt.process_id) or "—",
            "—" if receipt.exit_code is None else str(receipt.exit_code),
            escape(receipt.completion_reason) or escape(receipt.termination_source) or "—",
            _age_span_label(receipt.finished_age_seconds),
            escape(receipt.command) or "—",
            escape(_single_line(receipt.output_tail)) or "—",
        )
    return table


def _single_line(value: str) -> str:
    """Collapse a multi-line tail to one table cell, keeping the newest text."""
    collapsed = " ⏎ ".join(part for part in value.splitlines() if part.strip())
    return collapsed[-120:] if len(collapsed) > 120 else collapsed


def _delegation_procs_label(delegation: DelegationInfo) -> str:
    """Background-process accounting for one delegation; only non-zero buckets
    render, so an old payload without the keys stays visually quiet."""
    parts = []
    if delegation.handed_off_count:
        parts.append(f"{delegation.handed_off_count} handed")
    if delegation.orphaned_count:
        parts.append(f"{delegation.orphaned_count} orphaned")
    if delegation.unread_completion_count:
        parts.append(f"{delegation.unread_completion_count} unread")
    return " · ".join(parts) if parts else "—"


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


def _model_caches_table(ops: OperationsState, theme: Theme, *, now: float) -> Table:
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
            _age_label(cache.mtime, now),
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
    # The PR-keyed cron/state shape is the only one carrying per-PR review
    # state, so the two columns only widen the table when it is in play.
    show_review = any(m.open_count or m.conflicting_count for m in ops.pr_monitors)
    if show_review:
        pr_table.add_column("Open", justify="right", style=theme.ui_accent)
        pr_table.add_column("Conflict", justify="right", style=theme.ui_warn)
    for monitor in ops.pr_monitors:
        review = [str(monitor.open_count), str(monitor.conflicting_count)] if show_review else []
        pr_table.add_row(
            escape(monitor.filename),
            escape(monitor.repo) if monitor.repo else "—",
            escape(monitor.checked_at) if monitor.checked_at else "—",
            str(monitor.monitored_count),
            str(monitor.tracked_count),
            str(monitor.author_pr_count),
            *review,
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


def _moa_table(ops: OperationsState, theme: Theme, *, now: float) -> Table:
    moa_table = Table(box=None, show_header=False, padding=(0, 2))
    moa_table.add_column("Key", style=theme.ui_label)
    moa_table.add_column("Value", style=theme.banner_text)
    moa_table.add_row("Files", str(ops.moa_trace_count))
    moa_table.add_row("Size", _size_label(ops.moa_trace_size_bytes))
    moa_table.add_row(
        "Newest Session",
        escape(ops.moa_trace_newest_session_id) if ops.moa_trace_newest_session_id else "—",
    )
    moa_table.add_row("Newest Age", _age_label(ops.moa_trace_newest_mtime, now))
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


def _age_label(timestamp: float | None, now: float) -> str:
    if timestamp is None:
        return "—"
    try:
        age = max(0, int(now - timestamp))
    except (OverflowError, OSError, ValueError):
        return "—"
    return fmt_age_seconds(age)


def _goal_waiting_label(waiting_on_pid: int, waiting_on_session: str) -> str:
    if waiting_on_pid:
        return f"pid {waiting_on_pid}"
    if waiting_on_session:
        return f"session {waiting_on_session}"
    return ""
