"""Tests for the [12] Operations panel."""

from __future__ import annotations

from hermesd.models import (
    ApiRunReservation,
    ApiRunReservationsState,
    DashboardState,
    DelegationInfo,
    DelegationLiveManifest,
    DelegationLiveTask,
    GoalSummary,
    HostedRoomState,
    HostedRoomSummary,
    ModelCacheSummary,
    OperationsState,
    PRMonitorSummary,
    ProcessReceipt,
    ProcessReceiptsState,
)
from hermesd.panels.operations import render_operations
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_operations_detail_handles_absurd_mtime() -> None:
    state = DashboardState(
        operations=OperationsState(
            model_caches=[ModelCacheSummary(name="models_dev_cache", mtime=float("inf"))]
        )
    )
    rendered = render_to_str(render_operations(state, Theme(), detail=True))
    assert "—" in rendered


def _ops_state(**fields: object) -> DashboardState:
    return DashboardState(operations=OperationsState(**fields))


def _delegation(**fields: object) -> DelegationInfo:
    base: dict[str, object] = {
        "delegation_id": "deleg_1da954ba",
        "origin_session": "sess_001",
        "state": "completed",
        "delivery_state": "delivered",
        "delivery_attempts": 1,
        "dispatched_at": 1775791400.0,
        "completed_at": 1775791460.0,
        "duration_seconds": 60.0,
        "goal": "ship the feature",
        "result_status": "ok",
        "error_excerpt": "",
        "owner_alive": True,
    }
    base.update(fields)
    return DelegationInfo(**base)  # type: ignore[arg-type]


def test_compact_shows_delegation_counters():
    state = _ops_state(
        delegation_count=8,
        delegation_running_count=2,
        delegation_failed_count=1,
        delegation_undelivered_count=3,
    )
    text = render_to_str(render_operations(state, Theme()))
    assert "Delegations:" in text
    assert "2 running" in text
    assert "1 failed" in text
    assert "3 undelivered" in text


def test_compact_hides_delegation_line_when_counters_are_zero():
    text = render_to_str(render_operations(_ops_state(delegation_count=4), Theme()))
    assert "Delegations:" not in text


def test_detail_renders_delegation_table():
    state = _ops_state(
        delegation_count=2,
        delegation_live_log_count=3,
        delegations=[
            _delegation(),
            _delegation(
                delegation_id="deleg_2c320307",
                state="error",
                delivery_state="pending",
                delivery_attempts=4,
                result_status="error",
                error_excerpt="boom in the worker",
                owner_alive=False,
            ),
        ],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=160)
    assert "Delegations (2 total · 3 live logs)" in text
    assert "deleg_1da954ba" in text
    assert "ship the feature" in text
    assert "pending x4" in text
    assert "boom in the worker" in text
    assert "alive" in text


def test_detail_delegation_rows_show_process_accounting_counts():
    state = _ops_state(
        delegation_count=2,
        delegations=[
            _delegation(handed_off_count=2, orphaned_count=1, unread_completion_count=3),
            _delegation(delegation_id="deleg_2c320307"),
        ],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=160)
    assert "2 handed · 1 orphaned · 3 unread" in text
    assert "—" in text  # a delegation with no accounting still gets a placeholder


def test_detail_renders_state_db_section():
    state = _ops_state(
        state_db_schema_version=6,
        state_db_size_bytes=467_000_000,
        state_db_wal_size_bytes=4_100_000,
        state_db_file_generation="3",
        state_db_fts_storage_version="2",
        last_auto_prune_age_seconds=7200.0,
        last_auto_archive_age_seconds=259200.0,
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=160)
    assert "State DB" in text
    assert "Schema Version" in text
    assert "467.0M db" in text
    assert "4.1M wal" in text
    assert "3d" in text


def test_detail_renders_snapshot_and_web_ui_summary_rows():
    state = _ops_state(
        snapshot_count=4,
        snapshot_total_bytes=1_100_000_000,
        newest_snapshot_age_seconds=259200.0,
        web_ui_build_hash="314422207985",
        web_ui_built_age_seconds=3600.0,
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=160)
    assert "Snapshots" in text
    assert "1.1G" in text
    assert "newest 3d ago" in text
    assert "314422207985" in text


def test_detail_empty_operations_reports_no_artifacts():
    text = render_to_str(render_operations(_ops_state(), Theme(), detail=True), width=160)
    assert "No operations artifacts found" in text
    assert "Delegations (" not in text
    assert "State DB" not in text


def test_detail_escapes_markup_hostile_delegation_text():
    state = _ops_state(
        delegation_count=1,
        delegations=[
            _delegation(
                delegation_id="deleg_[red]evil[/red]",
                goal="[bold]fix\x1b[2J the thing[/bold]",
                error_excerpt="\x1b]8;;http://evil\x07[blink]boom[/blink]",
            )
        ],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200)
    assert "\x1b[2J" not in text
    assert "http://evil" not in text
    assert "[bold]fix" in text
    assert "[blink]boom" in text


def test_detail_uses_placeholders_for_missing_delegation_fields():
    state = _ops_state(
        delegation_count=1,
        delegations=[
            _delegation(
                state="",
                delivery_state="",
                delivery_attempts=0,
                goal="",
                result_status="",
                error_excerpt="",
                duration_seconds=None,
                owner_alive=False,
            )
        ],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=160)
    assert "—" in text


def test_operations_detail_shows_pr_open_and_conflicting_counts() -> None:
    state = _ops_state(
        pr_monitors=[
            PRMonitorSummary(
                filename="pr_monitor.json",
                monitored_count=2,
                tracked_count=2,
                open_count=1,
                conflicting_count=1,
            )
        ]
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Open" in text
    assert "Conflict" in text


def test_operations_detail_hides_pr_open_columns_when_all_zero() -> None:
    state = _ops_state(
        pr_monitors=[
            PRMonitorSummary(filename="pr_monitor.json", repo="acme/widget", monitored_count=3)
        ]
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Conflict" not in text


def test_operations_detail_shows_blocked_scripts_line() -> None:
    state = _ops_state(
        blocked_script_count=3,
        newest_blocked_script_age_seconds=7200.0,
        blocked_script_names=["a.sh", "b.sh", "c.sh"],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Blocked scripts" in text
    assert "3" in text
    assert "a.sh, b.sh, c.sh" in text


def test_operations_compact_shows_blocked_scripts_only_when_present() -> None:
    present = render_to_str(
        render_operations(_ops_state(blocked_script_count=2), Theme()), width=100, no_color=True
    )
    absent = render_to_str(render_operations(_ops_state(), Theme()), width=100, no_color=True)

    assert "Blocked" in present
    assert "Blocked" not in absent


def test_operations_detail_escapes_blocked_script_names() -> None:
    state = _ops_state(blocked_script_count=1, blocked_script_names=["[bold red]evil.sh"])
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "[bold red]evil.sh" in text


def _placeholder_rows(text: str, cells_count: int) -> list[list[str]]:
    """Every rendered line that is nothing but em-dash cells."""
    row = ["—"] * cells_count
    return [cells for line in text.splitlines() if (cells := line.split()) == row]


def test_operations_detail_delegations_table_placeholder_row_when_list_empty() -> None:
    # A counted-but-unlisted delegation set still renders one all-dash row.
    state = _ops_state(delegation_count=3)
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Delegations (3 total · 0 live logs)" in text
    assert _placeholder_rows(text, 8) == [["—"] * 8]


def test_operations_detail_reports_no_verification_events_when_ledger_empty() -> None:
    state = _ops_state(verification_db_present=True)
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Verification Evidence" in text
    assert "No verification events recorded" in text
    # The event table (and its Exit column) must not be drawn at all.
    assert "Exit" not in text


def test_operations_detail_projects_table_placeholder_row_when_list_empty() -> None:
    state = _ops_state(projects_db_present=True)
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Projects" in text
    assert "Slug" in text
    assert _placeholder_rows(text, 7) == [["—"] * 7]


def test_operations_detail_goal_waiting_falls_back_to_session() -> None:
    # With no waiting pid the Waiting cell names the blocking session instead.
    state = _ops_state(
        goal_count=1,
        goals=[
            GoalSummary(
                session_id="sess_goal",
                goal="ship the feature",
                status="active",
                waiting_on_session="sess_blocker",
            )
        ],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "session sess_blocker" in text
    assert "pid " not in text


# --- checkpoint prune marker (item 22) + corrupt ledger (item 24) -----------


def test_compact_warns_when_checkpoint_prune_is_overdue():
    from hermesd.models import (
        CHECKPOINT_PRUNE_OVERDUE_AFTER_SECONDS,
    )

    overdue = render_to_str(
        render_operations(
            _ops_state(
                checkpoint_prune_marker_present=True,
                checkpoint_prune_marker_age_seconds=float(
                    CHECKPOINT_PRUNE_OVERDUE_AFTER_SECONDS + 3600
                ),
            ),
            Theme(),
        ),
        no_color=True,
    )
    fresh = render_to_str(
        render_operations(
            _ops_state(
                checkpoint_prune_marker_present=True,
                checkpoint_prune_marker_age_seconds=3600.0,
            ),
            Theme(),
        ),
        no_color=True,
    )
    assert "Checkpoint prune overdue" in overdue
    assert "Checkpoint prune overdue" not in fresh


def test_compact_warns_when_spawn_ledger_corrupt_marker_exists():
    flagged = render_to_str(
        render_operations(
            _ops_state(spawn_ledger_corrupt_present=True, spawn_ledger_corrupt_age_seconds=7200.0),
            Theme(),
        ),
        no_color=True,
    )
    absent = render_to_str(render_operations(_ops_state(), Theme()), no_color=True)
    assert "spawn-ledger corrupt" in flagged
    assert "2h" in flagged
    assert "spawn-ledger corrupt" not in absent


def test_detail_summary_rows_render_prune_and_ledger_markers():
    from hermesd.models import CHECKPOINT_PRUNE_INTERVAL_SECONDS

    state = _ops_state(
        checkpoint_prune_marker_present=True,
        checkpoint_prune_marker_age_seconds=float(CHECKPOINT_PRUNE_INTERVAL_SECONDS * 5),
        spawn_ledger_corrupt_present=True,
        spawn_ledger_corrupt_age_seconds=7200.0,
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)
    assert "Checkpoint Prune" in text
    assert "OVERDUE" in text
    assert "Spawn Ledger" in text
    assert "corrupt" in text


# --- live delegation manifests (item 12) ------------------------------------


def _live_manifest(**fields: object) -> DelegationLiveManifest:
    base: dict[str, object] = {
        "delegation_id": "deleg_live01",
        "model": "Hermes-4.5",
        "provider": "nous",
        "started": "2026-07-10 10:00:00",
        "manifest_present": True,
        "dir_age_seconds": 45.0,
        "task_count": 2,
        "running_task_count": 1,
        "tasks": [
            DelegationLiveTask(
                index=0,
                goal="crawl the docs",
                status="completed",
                log_name="task-0.log",
                log_tail=["12:00:01 tool | read docs/index.md"],
            ),
            DelegationLiveTask(
                index=1,
                goal="summarize",
                status="running",
                log_name="task-1.log",
                log_tail=["12:00:02 assistant | working [hard]"],
            ),
        ],
    }
    base.update(fields)
    return DelegationLiveManifest(**base)  # type: ignore[arg-type]


def test_detail_renders_live_delegation_manifest_cards():
    state = _ops_state(
        delegation_live_manifest_count=1,
        delegation_live_manifests=[_live_manifest()],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)
    assert "Live Delegation Transcripts" in text
    assert "deleg_live01" in text
    assert "Hermes-4.5" in text
    assert "nous" in text
    assert "2 total · 1 running" in text
    assert "task 0" in text
    assert "task 1" in text
    assert "max_iterations" not in text  # exit_reason only when present
    assert "12:00:02 assistant | working [hard]" in text


def test_detail_live_manifests_render_truncation_and_roster_note():
    state = _ops_state(
        delegation_live_manifest_count=9,
        delegation_live_manifests=[_live_manifest()],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)
    assert "showing 1 of 9" in text
    # The live roster is memory/RPC-only: the panel must say so, not leave a blank.
    assert "roster" in text
    assert "memory" in text


def test_detail_omits_live_manifest_section_when_absent():
    text = render_to_str(render_operations(_ops_state(), Theme(), detail=True), no_color=True)
    assert "Live Delegation Transcripts" not in text


def test_detail_live_manifest_reports_dispatched_age():
    state = _ops_state(
        delegation_live_manifest_count=1,
        delegation_live_manifests=[_live_manifest()],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)
    # The run-dir mtime is set at dispatch write time, not by later log appends
    # or manifest rewrites, so the row must not claim to be a liveness signal.
    assert "Dispatched" in text
    assert "Dir Age" not in text


def test_detail_live_manifest_states_task_truncation_once():
    state = _ops_state(
        delegation_live_manifest_count=1,
        delegation_live_manifests=[
            _live_manifest(
                task_count=12,
                running_task_count=0,
                tasks=[DelegationLiveTask(index=index, status="completed") for index in range(8)],
                tasks_truncated=True,
            )
        ],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)
    assert text.count("Tasks") == 1  # one summary row, no duplicate key
    assert "Task List" in text
    assert "showing 8 of 12 — the counts above cover the whole table" in text


def test_detail_escapes_hostile_live_manifest_text():
    state = _ops_state(
        delegation_live_manifest_count=1,
        delegation_live_manifests=[
            _live_manifest(
                model="[red]evil-model[/red]",
                provider="[/] provider",
                tasks=[
                    DelegationLiveTask(
                        index=0,
                        goal="[link]x[/link]",
                        status="[bold]running[/bold]",
                        exit_reason="[italic]boom[/italic]",
                        log_name="task-0.log",
                        log_tail=["[underline]tail[/underline] \x1b[2J line"],
                    )
                ],
            )
        ],
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)
    assert "[red]evil-model[/red]" in text
    assert "[bold]running[/bold]" in text
    assert "[underline]tail[/underline]" in text


def test_compact_shows_live_delegation_line_when_present():
    populated = render_to_str(
        render_operations(
            _ops_state(
                delegation_live_manifest_count=2,
                delegation_live_manifests=[_live_manifest()],
            ),
            Theme(),
        ),
        no_color=True,
    )
    absent = render_to_str(render_operations(_ops_state(), Theme()), no_color=True)
    assert "Live delegations:" in populated
    assert "2 manifests · 1 running" in populated
    assert "Live delegations:" not in absent


def test_compact_live_line_marks_the_running_sum_as_displayed():
    text = render_to_str(
        render_operations(
            _ops_state(
                delegation_live_manifest_count=2,
                delegation_live_manifests=[_live_manifest()],
            ),
            Theme(),
        ),
        no_color=True,
    )
    # The manifest count covers every run dir; the running sum only covers the
    # cards the collector parsed, so the copy has to say which is which.
    assert "2 manifests · 1 running (shown)" in text


def test_compact_live_line_does_not_call_every_run_dir_live():
    """The count covers run directories, and finished ones are not live.

    Only the parsed cards carry ``completed``/``running_task_count``, so the
    total cannot be filtered by liveness without parsing every counted
    manifest; calling the total "live" claimed nine July delegations were
    running on the live home, where every parsed manifest had completed.
    """
    finished = _live_manifest()
    finished = finished.model_copy(update={"completed": 1_700_000_000, "running_task_count": 0})
    text = render_to_str(
        render_operations(
            _ops_state(
                delegation_live_manifest_count=9,
                delegation_live_manifests=[finished],
            ),
            Theme(),
        ),
        no_color=True,
    )
    assert "9 manifests · 0 running (shown)" in text
    assert "9 live" not in text


# --- process completion receipts (item 13) ----------------------------------


def _receipts(**fields: object) -> ProcessReceiptsState:
    base: dict[str, object] = {
        "dir_present": True,
        "receipt_count": 2,
        "newest_receipt_age_seconds": 120.0,
        "receipts": [
            ProcessReceipt(
                process_id="proc_abc123",
                command="curl -H 'Authorization: Bearer [REDACTED]' https://api.example.dev",
                exit_code=2,
                completion_reason="exited",
                started_age_seconds=600.0,
                finished_age_seconds=120.0,
                output_tail="AUTH Bearer [REDACTED]\ndone, exit 2",
            ),
            ProcessReceipt(
                process_id="proc_def456",
                command="npm run build",
                exit_code=0,
                completion_reason="completed",
                started_age_seconds=3600.0,
                finished_age_seconds=900.0,
                output_tail="built in 42s",
            ),
        ],
    }
    base.update(fields)
    return ProcessReceiptsState(**base)  # type: ignore[arg-type]


def test_detail_renders_process_receipt_section():
    state = _ops_state(process_receipts=_receipts())
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)
    assert "Process Receipts" in text
    assert "proc_abc123" in text
    assert "proc_def456" in text
    assert "Bearer [REDACTED]" in text
    assert "done, exit 2" in text


def test_detail_receipt_section_states_no_receipts_yet_when_dir_absent():
    # Another artifact keeps the panel out of its fully-empty state, where the
    # single "No operations artifacts found" line replaces this section.
    state = _ops_state(process_receipts=ProcessReceiptsState(), state_db_size_bytes=4096)
    text = render_to_str(render_operations(state, Theme(), detail=True), width=160, no_color=True)
    assert "no receipts yet" in text


def test_detail_empty_state_renders_a_single_message():
    text = render_to_str(
        render_operations(_ops_state(), Theme(), detail=True), width=160, no_color=True
    )
    assert "No operations artifacts found" in text
    assert "no receipts yet" not in text
    assert "Process Receipts" not in text


def test_detail_marker_readout_suppresses_the_no_artifacts_message():
    state = _ops_state(
        checkpoint_prune_marker_present=True,
        checkpoint_prune_marker_age_seconds=3600.0,
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=160, no_color=True)
    assert "No operations artifacts found" not in text
    assert "no receipts yet" in text


def test_detail_receipts_escape_hostile_text():
    state = _ops_state(
        process_receipts=_receipts(
            receipts=[
                ProcessReceipt(
                    process_id="proc_[link]evil[/link]",
                    command="[bold]rm -rf[/bold] /tmp/x",
                    exit_code=1,
                    output_tail="\x1b[2J [italic]boom[/italic]",
                )
            ]
        )
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)
    assert "proc_[link]evil[/link]" in text
    assert "[italic]boom[/italic]" in text
    assert "\x1b[2J" not in text


def test_compact_shows_finished_proc_count_when_present():
    populated = render_to_str(
        render_operations(_ops_state(process_receipts=_receipts()), Theme()), no_color=True
    )
    absent = render_to_str(
        render_operations(_ops_state(process_receipts=ProcessReceiptsState()), Theme()),
        no_color=True,
    )
    assert "Finished procs:" in populated
    assert "2 receipts" in populated
    assert "Finished procs:" not in absent


# --- hosted rooms and retained API run reservations -------------------------


def _hosted_rooms(**fields: object) -> HostedRoomState:
    base: dict[str, object] = {
        "db_present": True,
        "db_size_bytes": 143_360,
        "active_room_count": 1,
        "disbanded_room_count": 1,
        "event_count": 4,
        "event_kind_counts": {"room.created": 1, "message.user": 1, "turn.settled": 1},
        "newest_event_age_seconds": 90.0,
        "accounted_event_bytes": 4096,
        "retired_id_count": 1,
        "newest_retired_id_age_seconds": 5 * 86_400.0,
        "link_count": 1,
        "remote_run_count": 1,
        "newest_remote_run_age_seconds": 300.0,
        "revoked_grant_count": 1,
        "live_peer_reservation_count": 1,
        "expired_peer_reservation_count": 1,
        "revoked_peer_reservation_count": 1,
        "rooms": [
            HostedRoomSummary(
                room_id="room-active",
                name="Active Room",
                member_count=2,
                authority_epoch=1,
                next_seq=6,
                event_bytes=4096,
                revision=1,
                updated_at_age_seconds=60.0,
            ),
            HostedRoomSummary(
                room_id="room-disbanded",
                name="Disbanded Room",
                member_count=3,
                authority_epoch=4,
                next_seq=2,
                revision=7,
                updated_at_age_seconds=3 * 86_400.0,
                disbanded_at_age_seconds=2 * 86_400.0,
            ),
        ],
    }
    base.update(fields)
    return HostedRoomState(**base)  # type: ignore[arg-type]


def _api_runs(**fields: object) -> ApiRunReservationsState:
    base: dict[str, object] = {
        "db_present": True,
        "db_size_bytes": 16_384,
        "reservation_count": 2,
        "scope_count": 1,
        "acknowledged_count": 1,
        "owner_recorded_count": 1,
        "retention_expired_count": 1,
        "newest_age_seconds": 60.0,
        "oldest_age_seconds": 30 * 3_600.0,
        "reservations": [
            ApiRunReservation(
                run_id="run-live",
                status="running",
                created_at_age_seconds=2 * 3_600.0,
                updated_at_age_seconds=60.0,
                retention_remaining_seconds=12 * 3_600.0,
                owner_pid=4242,
                owner_started_recorded=True,
                owner_alive=True,
            ),
            ApiRunReservation(
                run_id="run-terminal",
                status="completed",
                created_at_age_seconds=30 * 3_600.0,
                updated_at_age_seconds=29 * 3_600.0,
                acknowledged=True,
            ),
        ],
    }
    base.update(fields)
    return ApiRunReservationsState(**base)  # type: ignore[arg-type]


def test_operations_detail_renders_hosted_room_section() -> None:
    state = _ops_state(hosted_rooms=_hosted_rooms())
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Hosted Rooms" in text
    assert "1 active" in text
    assert "1 disbanded" in text
    assert "room-active" in text
    assert "room-disbanded" in text
    # latest_seq is derived from next_seq, never stored beside it.
    assert "Latest Seq" in text
    assert "1 live · 1 expired · 1 revoked" in text
    # The scope note is part of the section, not a footnote elsewhere.
    assert "shared-state.db" in text


def test_operations_detail_omits_hosted_rooms_when_the_database_is_absent() -> None:
    text = render_to_str(
        render_operations(_ops_state(), Theme(), detail=True), width=200, no_color=True
    )

    assert "Hosted Rooms" not in text


def test_operations_detail_marks_hosted_room_list_truncation() -> None:
    rooms = [
        HostedRoomSummary(room_id=f"room-{index:02d}", next_seq=index + 1) for index in range(8)
    ]
    state = _ops_state(
        hosted_rooms=_hosted_rooms(
            active_room_count=12,
            disbanded_room_count=0,
            rooms=rooms,
            rooms_truncated=True,
        )
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    # The rendered list is capped; the count is not.
    assert "showing 8 of 12" in text
    assert "12 active" in text


def test_operations_detail_renders_api_run_reservation_section() -> None:
    state = _ops_state(api_runs=_api_runs())
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Retained API Run Reservations" in text
    assert "run-live" in text
    assert "running" in text
    assert "completed" in text
    assert "pid 4242" in text
    assert "12h" in text


def test_operations_detail_states_the_zero_row_caveat_for_an_empty_store() -> None:
    """An empty store must not read as an idle API."""
    state = _ops_state(api_runs=_api_runs(reservation_count=0, reservations=[]))
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Retained API Run Reservations" in text
    assert "no reservations retained" in text
    assert "not evidence that the API was idle" in text
    assert "process memory" in text


def test_operations_detail_omits_api_runs_when_the_database_is_absent() -> None:
    text = render_to_str(
        render_operations(_ops_state(), Theme(), detail=True), width=200, no_color=True
    )

    assert "Retained API Run Reservations" not in text


def test_operations_detail_reports_an_unverifiable_run_owner() -> None:
    state = _ops_state(
        api_runs=_api_runs(
            reservations=[
                ApiRunReservation(run_id="run-no-owner", status="unknown"),
                ApiRunReservation(
                    run_id="run-stale-pid",
                    status="queued",
                    owner_pid=9999,
                    owner_started_recorded=False,
                ),
            ]
        )
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "no pid" in text
    # A pid with no recorded start time cannot be told apart from a reused one.
    assert "identity unverified" in text
    assert "unknown" in text


def test_operations_detail_marks_api_run_list_truncation() -> None:
    state = _ops_state(
        api_runs=_api_runs(
            reservation_count=12,
            reservations=[ApiRunReservation(run_id=f"run-{index}") for index in range(8)],
            reservations_truncated=True,
        )
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "showing 8 of 12" in text


def test_operations_detail_escapes_hosted_room_names() -> None:
    state = _ops_state(
        hosted_rooms=_hosted_rooms(
            rooms=[HostedRoomSummary(room_id="room-1", name="[/] desc [xy] tag")]
        )
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "[/]" in text
    assert "[xy]" in text


def test_operations_detail_coordination_stores_are_artifacts() -> None:
    hosted = render_to_str(
        render_operations(_ops_state(hosted_rooms=_hosted_rooms()), Theme(), detail=True),
        width=200,
        no_color=True,
    )
    runs = render_to_str(
        render_operations(_ops_state(api_runs=_api_runs()), Theme(), detail=True),
        width=200,
        no_color=True,
    )

    assert "No operations artifacts found" not in hosted
    assert "No operations artifacts found" not in runs


def test_operations_compact_shows_coordination_counts_only_when_present() -> None:
    empty = render_to_str(render_operations(_ops_state(), Theme()), no_color=True)
    assert "Hosted Rooms:" not in empty
    assert "API Runs:" not in empty

    populated = render_to_str(
        render_operations(_ops_state(hosted_rooms=_hosted_rooms(), api_runs=_api_runs()), Theme()),
        no_color=True,
    )
    assert "Hosted Rooms:" in populated
    assert "1 active" in populated
    assert "API Runs:" in populated
    assert "2 retained" in populated


def test_operations_detail_renders_a_present_but_empty_hosted_room_store() -> None:
    """Presence is the fact worth showing even with no rooms; the note still runs."""
    state = _ops_state(hosted_rooms=HostedRoomState(db_present=True, db_size_bytes=16_384))
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "Hosted Rooms" in text
    assert "0 active · 0 disbanded" in text
    # No room table is drawn, and no histogram row either.
    assert "Latest Seq" not in text
    assert "Event Kinds" not in text
    assert "shared-state.db" in text


def test_operations_detail_shows_an_overdue_retention_window() -> None:
    state = _ops_state(
        api_runs=_api_runs(
            reservations=[
                ApiRunReservation(
                    run_id="run-overdue",
                    status="completed",
                    retention_remaining_seconds=-300.0,
                )
            ]
        )
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "5m overdue" in text
    assert "Past Retention" in text
    assert "1 past retention deadline" in text
    assert "awaiting a terminal status" not in text


def test_operations_detail_shows_a_reservation_with_no_retention_deadline() -> None:
    state = _ops_state(
        api_runs=_api_runs(
            reservations=[ApiRunReservation(run_id="run-no-deadline", status="queued")]
        )
    )
    text = render_to_str(render_operations(state, Theme(), detail=True), width=200, no_color=True)

    assert "run-no-deadline" in text
    assert "—" in text
