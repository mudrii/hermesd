"""Tests for the [12] Operations panel."""

from __future__ import annotations

from hermesd.models import (
    DashboardState,
    DelegationInfo,
    ModelCacheSummary,
    OperationsState,
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
