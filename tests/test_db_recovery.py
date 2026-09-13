"""F17 — database recovery evidence, presence and metadata only.

Every artifact upstream's repair code leaves is a *sibling* of ``state.db``
(``hermes_state_repair.py:317`` ledger, ``:503-513`` forensic backups,
``hermes_state_dbfile.py:228`` retired-WAL generations, ``:176-229`` locks).

Two traps this file pins:

* ``~/.hermes/recovery/`` is an **operator-made** remediation bundle directory.
  Upstream's repair code never writes it, so it is not recovery evidence and must
  not be read as such.
* The live ``state.db`` is hundreds of megabytes. hermesd stats, globs and reads
  only the small JSON manifests — it never hashes, checkpoints, integrity-checks
  or repairs the database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hermesd.collect.recovery as recovery_module
from hermesd.collect.recovery import _local_iso_to_epoch, _read_db_recovery
from hermesd.collector import Collector
from hermesd.models import (
    MAX_PERSISTENT_REPAIR_ATTEMPTS,
    DashboardState,
    DbRecoveryState,
    OperationsState,
    RetiredWalGeneration,
)
from hermesd.panels.operations import render_operations
from hermesd.theme import Theme
from tests.conftest import _count_opens, render_to_str

_NOW = 1_800_000_000.0
_LEDGER_NAME = "state.db.repair-attempts.json"
_MANIFEST_NAME = "manifest.json"


def _collect(home: Path) -> DashboardState:
    collector = Collector(home, clock=lambda: _NOW)
    try:
        return collector.collect()
    finally:
        collector.close()


def _touch_db(home: Path) -> Path:
    """A stand-in state.db: the artifacts are named after it, never read by us."""
    db_path = home / "state.db"
    db_path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 84)
    return db_path


def _write_ledger(home: Path, *, failed_attempts: int = 1, last_attempt: str | None = None) -> Path:
    stamp = "2026-09-12T21:48:03" if last_attempt is None else last_attempt
    path = home / _LEDGER_NAME
    path.write_text(
        json.dumps(
            {
                "fingerprint": "1024:0123456789abcdef0123456789abcdef",
                "failed_attempts": failed_attempts,
                "last_attempt": stamp,
            }
        )
    )
    return path


def _write_backup(home: Path, stamp: str, *, size: int = 10, incomplete: bool = False) -> Path:
    name = f"state.db.malformed-backup-{stamp}"
    if incomplete:
        name = f"{name}.incomplete-2"
    path = home / name
    path.write_bytes(b"x" * size)
    return path


def _write_retired_generation(
    home: Path,
    stamp: str,
    *,
    manifest: dict | None = None,
    partial: bool = False,
) -> Path:
    name = f"state.db.retired-wal-{stamp}-4242"
    if partial:
        name = f"{name}.partial"
    generation = home / name
    generation.mkdir()
    if manifest is not None:
        (generation / _MANIFEST_NAME).write_text(json.dumps(manifest))
    return generation


def _manifest(**overrides: object) -> dict:
    payload: dict = {
        "version": 1,
        "database": "/home/u/.hermes/state.db",
        "trigger": "wal_generation_lost",
        "pid": 4242,
        "captured_at": "2026-09-12T21:48:03Z",
        "python": "3.11.9",
        "sqlite": "3.45.1",
        "wal": {"identity": [1, 2], "file": "state.db-wal", "bytes": 4096, "sha256": "ab" * 32},
        "shm": None,
        "main": {
            "identity": [1, 3],
            "size": 1024,
            "mode": "header_only",
            "file": "state.db.header",
        },
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# absence
# --------------------------------------------------------------------------


def test_a_home_without_artifacts_reports_nothing(hermes_home: Path) -> None:
    _touch_db(hermes_home)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery == DbRecoveryState()
    assert recovery.artifacts_present is False
    assert recovery.repair_budget_exhausted is False


def test_bare_recovery_state_constructs_empty() -> None:
    assert DbRecoveryState().repair_ledger_present is False
    assert DbRecoveryState().newest_retired_wal.manifest_present is False


def test_an_operator_made_recovery_directory_is_not_evidence(hermes_home: Path) -> None:
    """``~/.hermes/recovery/`` is operator-made; upstream's repair never writes it."""
    _touch_db(hermes_home)
    bundle = hermes_home / "recovery"
    bundle.mkdir()
    (bundle / "config.yaml.before").write_text("model: x\n")
    (bundle / "repository.bundle").write_bytes(b"\x00" * 32)
    (bundle / "git-status.before.txt").write_text("clean\n")

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery == DbRecoveryState()


# --------------------------------------------------------------------------
# repair-attempt ledger
# --------------------------------------------------------------------------


def test_ledger_presence_attempts_and_age(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    stamp = "2026-09-12T21:48:03"
    _write_ledger(hermes_home, failed_attempts=2, last_attempt=stamp)
    expected_age = _NOW - _local_epoch(stamp)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.repair_ledger_present is True
    assert recovery.failed_attempts == 2
    assert recovery.last_attempt == stamp
    assert recovery.last_attempt_age_seconds == pytest.approx(expected_age)
    assert recovery.repair_budget_exhausted is False
    assert recovery.artifacts_present is True


def _local_epoch(stamp: str) -> float:
    """The ledger's naive stamp read the way upstream wrote it: local time."""
    return datetime.fromisoformat(stamp).timestamp()


def test_last_attempt_is_read_as_local_time_not_utc(hermes_home: Path) -> None:
    """Upstream writes ``datetime.now().isoformat(timespec="seconds")`` — no zone.

    Reading a naive stamp as UTC would shift the age by the host's offset, so the
    ledger gets its own parser rather than the shared naive-as-UTC one.
    """
    _touch_db(hermes_home)
    _write_ledger(hermes_home, last_attempt="2026-09-12T21:48:03")

    age = _collect(hermes_home).operations.db_recovery.last_attempt_age_seconds

    assert age == pytest.approx(_NOW - _local_epoch("2026-09-12T21:48:03"))


@pytest.mark.parametrize("attempts", [3, 4, 99])
def test_the_recorded_budget_is_exhausted_at_three_failures(
    hermes_home: Path, attempts: int
) -> None:
    _touch_db(hermes_home)
    _write_ledger(hermes_home, failed_attempts=attempts)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.failed_attempts == attempts
    assert recovery.repair_budget_exhausted is True


def test_the_budget_constant_matches_upstream() -> None:
    assert MAX_PERSISTENT_REPAIR_ATTEMPTS == 3


def test_a_recorded_count_without_a_ledger_claims_no_budget() -> None:
    """Exhaustion is a claim about a ledger; with none present there is nothing to exhaust."""
    assert DbRecoveryState(failed_attempts=9).repair_budget_exhausted is False
    assert DbRecoveryState(failed_attempts=9).artifacts_present is False
    exhausted = DbRecoveryState(repair_ledger_present=True, failed_attempts=3)
    assert exhausted.repair_budget_exhausted is True
    assert exhausted.artifacts_present is True


def test_a_ledger_under_the_budget_is_not_exhausted(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    _write_ledger(hermes_home, failed_attempts=2)

    assert _collect(hermes_home).operations.db_recovery.repair_budget_exhausted is False


def test_a_missing_ledger_is_not_a_health_claim(hermes_home: Path) -> None:
    """A successful repair deletes the ledger, so absence is ambiguous by design."""
    _touch_db(hermes_home)
    (hermes_home / "state.db.auto-maintenance.lock").write_bytes(b"")

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.repair_ledger_present is False
    assert recovery.failed_attempts == 0
    assert recovery.repair_budget_exhausted is False
    rendered = _render(recovery)
    assert "not evidence the database is healthy" in rendered


# --------------------------------------------------------------------------
# forensic backups
# --------------------------------------------------------------------------


def test_malformed_backups_are_counted_with_their_sidecars(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    _write_backup(hermes_home, "20260912_214803", size=1000)
    (hermes_home / "state.db.malformed-backup-20260912_214803-wal").write_bytes(b"w" * 500)
    _write_backup(hermes_home, "20260911_101010", size=2000)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.malformed_backup_count == 2
    assert recovery.malformed_backup_bytes == 3500
    assert recovery.malformed_backup_staging_count == 0
    assert recovery.newest_malformed_backup_age_seconds is not None


def test_a_sidecar_of_a_pruned_backup_is_not_counted(hermes_home: Path) -> None:
    """Retention prunes the main copy and its sidecars; an orphan counts for nothing."""
    _touch_db(hermes_home)
    (hermes_home / "state.db.malformed-backup-20260912_214803-shm").write_bytes(b"s" * 32)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.malformed_backup_count == 0
    assert recovery.malformed_backup_bytes == 0


def test_incomplete_and_staging_backups_are_counted_separately(hermes_home: Path) -> None:
    """An in-progress copy is never presented as a completed forensic capture."""
    _touch_db(hermes_home)
    _write_backup(hermes_home, "20260912_214803", size=1000)
    _write_backup(hermes_home, "20260912_214900", size=900, incomplete=True)
    (hermes_home / "state.db.backup-staging-20260912_215000").write_bytes(b"y" * 800)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.malformed_backup_count == 1
    assert recovery.malformed_backup_bytes == 1000
    assert recovery.malformed_backup_staging_count == 2


def test_the_newest_backup_age_is_the_newest(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    old = _write_backup(hermes_home, "20260910_000000")
    new = _write_backup(hermes_home, "20260912_000000")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.newest_malformed_backup_age_seconds == pytest.approx(_NOW - 1_700_000_000)


# --------------------------------------------------------------------------
# retired-WAL generations
# --------------------------------------------------------------------------


def test_retired_wal_generation_manifest_is_summarized(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    _write_retired_generation(hermes_home, "20260912-214803", manifest=_manifest())

    recovery = _collect(hermes_home).operations.db_recovery
    newest = recovery.newest_retired_wal

    assert recovery.retired_wal_count == 1
    assert recovery.retired_wal_staging_count == 0
    assert newest.manifest_present is True
    assert newest.captured_at == "2026-09-12T21:48:03Z"
    assert newest.trigger == "wal_generation_lost"
    assert newest.wal_bytes == 4096
    assert newest.main_mode == "header_only"


def test_captured_at_is_read_as_utc(hermes_home: Path) -> None:
    """``captured_at`` is ``%Y-%m-%dT%H:%M:%SZ`` — unlike the ledger's local stamp."""
    _touch_db(hermes_home)
    _write_retired_generation(hermes_home, "20260912-214803", manifest=_manifest())

    newest = _collect(hermes_home).operations.db_recovery.newest_retired_wal
    expected = datetime(2026, 9, 12, 21, 48, 3, tzinfo=UTC).timestamp()

    assert newest.captured_at_age_seconds == pytest.approx(_NOW - expected)


def test_the_newest_generation_wins(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    _write_retired_generation(
        hermes_home, "20260910-010101", manifest=_manifest(trigger="older_capture")
    )
    _write_retired_generation(
        hermes_home, "20260912-214803", manifest=_manifest(trigger="newer_capture")
    )

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.retired_wal_count == 2
    assert recovery.newest_retired_wal.trigger == "newer_capture"


def test_a_partial_generation_directory_is_not_a_completed_capture(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    _write_retired_generation(hermes_home, "20260912-214803", partial=True)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.retired_wal_count == 0
    assert recovery.retired_wal_staging_count == 1
    assert recovery.newest_retired_wal.manifest_present is False


def test_a_generation_without_a_manifest_is_counted_but_unread(hermes_home: Path) -> None:
    """``os.replace`` publishes the manifest, so a settled dir without one is odd
    but is still a directory on disk: count it, claim nothing about its contents."""
    _touch_db(hermes_home)
    _write_retired_generation(hermes_home, "20260912-214803")

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.retired_wal_count == 1
    assert recovery.newest_retired_wal.manifest_present is False
    assert recovery.newest_retired_wal.trigger == ""


def test_a_copied_main_image_is_reported_as_copied(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    _write_retired_generation(
        hermes_home,
        "20260912-214803",
        manifest=_manifest(main={"identity": [1, 3], "size": 1024, "mode": "copied"}),
    )

    assert _collect(hermes_home).operations.db_recovery.newest_retired_wal.main_mode == "copied"


# --------------------------------------------------------------------------
# locks
# --------------------------------------------------------------------------


def test_lock_files_are_reported_as_presence_only(hermes_home: Path) -> None:
    """Upstream opens both with ``a+b`` and never deletes them, so a file on disk
    is not proof anything holds the lock."""
    _touch_db(hermes_home)
    (hermes_home / "state.db.repair.lock").write_bytes(b"")
    (hermes_home / "state.db.auto-maintenance.lock").write_bytes(b"")

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.repair_lock_file_present is True
    assert recovery.auto_maintenance_lock_file_present is True
    assert recovery.artifacts_present is True


def test_absent_lock_files(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    _write_ledger(hermes_home)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.repair_lock_file_present is False
    assert recovery.auto_maintenance_lock_file_present is False


# --------------------------------------------------------------------------
# scan hygiene
# --------------------------------------------------------------------------


def test_the_directory_scan_is_bounded(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    for index in range(260):
        (hermes_home / f"state.db.malformed-backup-2026{index:010d}").write_bytes(b"x")

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.scan_truncated is True
    assert recovery.malformed_backup_count <= 200


def test_a_symlinked_ledger_is_refused(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    outside = hermes_home.parent / "elsewhere.json"
    outside.write_text(json.dumps({"failed_attempts": 9, "last_attempt": "2026-09-12T21:48:03"}))
    (hermes_home / _LEDGER_NAME).symlink_to(outside)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.repair_ledger_present is False
    assert recovery.failed_attempts == 0


def test_a_symlinked_backup_directory_is_refused(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    outside = hermes_home.parent / "outside"
    outside.mkdir()
    (hermes_home / "state.db.retired-wal-20260912-214803-4242").symlink_to(outside)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.retired_wal_count == 0


# --------------------------------------------------------------------------
# resilience: a corrupt manifest degrades only itself, keeping last-good
# --------------------------------------------------------------------------


def test_a_corrupt_ledger_fails_only_the_recovery_source(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    (hermes_home / "state.db.repair.lock").write_bytes(b"")
    _write_ledger(hermes_home, failed_attempts=2)

    collector = Collector(hermes_home, clock=lambda: _NOW)
    try:
        good = collector.collect()
        assert good.operations.db_recovery.failed_attempts == 2

        (hermes_home / _LEDGER_NAME).write_text('{"failed_attempts": ')
        bad = collector.collect()
    finally:
        collector.close()

    assert bad.operations.db_recovery.failed_attempts == 2
    assert bad.operations.db_recovery.repair_lock_file_present is True
    assert "db_recovery" in bad.health.failed_sources


def test_a_corrupt_retired_manifest_keeps_last_good(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    generation = _write_retired_generation(
        hermes_home, "20260912-214803", manifest=_manifest(trigger="good_trigger")
    )

    collector = Collector(hermes_home, clock=lambda: _NOW)
    try:
        good = collector.collect()
        assert good.operations.db_recovery.newest_retired_wal.trigger == "good_trigger"

        (generation / _MANIFEST_NAME).write_text("[not json")
        bad = collector.collect()
    finally:
        collector.close()

    assert bad.operations.db_recovery.newest_retired_wal.trigger == "good_trigger"
    assert "db_recovery" in bad.health.failed_sources


def test_an_oversized_ledger_is_refused_not_parsed(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    (hermes_home / _LEDGER_NAME).write_text(
        json.dumps({"failed_attempts": 1, "padding": "x" * (512 * 1024)})
    )

    state = _collect(hermes_home)

    assert state.operations.db_recovery == DbRecoveryState()
    assert "db_recovery" in state.health.failed_sources


def test_a_non_object_ledger_is_refused(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    (hermes_home / _LEDGER_NAME).write_text("[1, 2, 3]")

    state = _collect(hermes_home)

    assert "db_recovery" in state.health.failed_sources
    assert state.operations.db_recovery.repair_ledger_present is False


def test_recovery_survives_a_home_without_state_db(hermes_home: Path) -> None:
    _write_ledger(hermes_home, failed_attempts=1)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.repair_ledger_present is True


# --------------------------------------------------------------------------
# reader edges
# --------------------------------------------------------------------------


def test_a_missing_home_directory_reports_nothing(tmp_path: Path) -> None:
    """An absent parent is "nothing there"; an unreadable one propagates instead."""
    recovery = _read_db_recovery(tmp_path / "gone" / "state.db", tmp_path, now=_NOW)

    assert recovery == DbRecoveryState()


def test_a_directory_named_like_a_backup_is_not_a_backup(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    (hermes_home / "state.db.malformed-backup-20260912_214803").mkdir()
    (hermes_home / "state.db.malformed-backup-20260912_214804-wal").mkdir()

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.malformed_backup_count == 0
    assert recovery.malformed_backup_bytes == 0


def test_a_file_named_like_a_retired_generation_is_not_a_generation(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    (hermes_home / "state.db.retired-wal-20260912-214803-4242").write_bytes(b"")

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.retired_wal_count == 0
    assert recovery.retired_wal_staging_count == 0


def test_an_empty_ledger_is_refused_not_read_as_zero(hermes_home: Path) -> None:
    """Upstream writes the ledger with a plain write_text, so a torn file exists."""
    _touch_db(hermes_home)
    (hermes_home / _LEDGER_NAME).write_text("")

    state = _collect(hermes_home)

    assert state.operations.db_recovery.repair_ledger_present is False
    assert "db_recovery" in state.health.failed_sources


def test_non_string_ledger_fields_are_ignored(hermes_home: Path) -> None:
    """A wrong-typed value is a damaged ledger, not a crash and not a silent 0."""
    _touch_db(hermes_home)
    (hermes_home / _LEDGER_NAME).write_text(
        json.dumps({"failed_attempts": "2", "last_attempt": 1788832752})
    )

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.repair_ledger_present is True
    assert recovery.failed_attempts == 2
    assert recovery.last_attempt == ""
    assert recovery.last_attempt_age_seconds is None


@pytest.mark.parametrize("stamp", ["", "not-a-stamp", "2026-13-45T99:99:99"])
def test_an_unparseable_last_attempt_yields_no_age(hermes_home: Path, stamp: str) -> None:
    _touch_db(hermes_home)
    _write_ledger(hermes_home, failed_attempts=1, last_attempt=stamp)

    recovery = _collect(hermes_home).operations.db_recovery

    assert recovery.repair_ledger_present is True
    assert recovery.last_attempt_age_seconds is None


@pytest.mark.parametrize("error_type", [OverflowError, OSError, ValueError])
def test_an_unrepresentable_stamp_is_never_an_age(
    error_type: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Platform timestamp conversion failures never become an age."""

    class UnrepresentableDatetime:
        @classmethod
        def fromisoformat(cls, value: str) -> UnrepresentableDatetime:
            return cls()

        def timestamp(self) -> float:
            raise error_type("unrepresentable local time")

    monkeypatch.setattr(recovery_module, "datetime", UnrepresentableDatetime)

    assert _local_iso_to_epoch("9999-12-31T23:59:59") is None


@pytest.mark.parametrize("value", [None, 1788832752, ["2026-09-12T21:48:03"], b"x"])
def test_a_non_string_stamp_is_not_an_age(value: object) -> None:
    assert _local_iso_to_epoch(value) is None


def test_an_offset_stamp_is_honoured_as_written() -> None:
    """A ledger stamp that *does* carry a zone is not re-read as local time."""
    assert _local_iso_to_epoch("2026-09-12T21:48:03+00:00") == pytest.approx(
        datetime(2026, 9, 12, 21, 48, 3, tzinfo=UTC).timestamp()
    )


def test_a_non_string_manifest_value_is_not_rendered(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    _write_retired_generation(
        hermes_home,
        "20260912-214803",
        manifest=_manifest(trigger=12345, captured_at=None, main={"mode": ["copied"]}),
    )

    newest = _collect(hermes_home).operations.db_recovery.newest_retired_wal

    assert newest.manifest_present is True
    assert newest.trigger == ""
    assert newest.captured_at == ""
    assert newest.captured_at_age_seconds is None
    assert newest.main_mode == ""


def test_a_long_manifest_string_is_capped(hermes_home: Path) -> None:
    _touch_db(hermes_home)
    _write_retired_generation(hermes_home, "20260912-214803", manifest=_manifest(trigger="t" * 500))

    newest = _collect(hermes_home).operations.db_recovery.newest_retired_wal

    assert len(newest.trigger) <= 64


# --------------------------------------------------------------------------
# the hard limits: never open, hash, integrity-check or repair the database
# --------------------------------------------------------------------------


def _populate_artifacts(home: Path) -> Path:
    db_path = _touch_db(home)
    _write_ledger(home, failed_attempts=3)
    _write_backup(home, "20260912_214803", size=1000)
    _write_retired_generation(home, "20260912-214803", manifest=_manifest())
    return db_path


def test_the_database_file_is_never_opened(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live ``state.db`` is ~480 MB; opening it is the work this reader avoids."""
    db_path = _populate_artifacts(hermes_home)
    opens = _count_opens(monkeypatch, db_path)

    recovery = _read_db_recovery(db_path, hermes_home, now=_NOW)

    assert opens == []
    # ...and it still reported everything, so the artifacts really are siblings.
    assert recovery.repair_ledger_present is True
    assert recovery.malformed_backup_count == 1
    assert recovery.retired_wal_count == 1


def test_the_reader_never_hashes_or_connects_to_the_database(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No digest, no SQLite connection: so no integrity check, checkpoint or repair.

    ``_db_fingerprint`` and ``_backup_content_identity`` both hash the file, and
    every repair/checkpoint lane needs a connection. Making both impossible is
    what proves this reader is stat-and-small-JSON only.
    """
    db_path = _populate_artifacts(hermes_home)

    def refused(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the recovery reader must not hash or connect to state.db")

    monkeypatch.setattr(hashlib, "sha256", refused)
    monkeypatch.setattr(hashlib, "new", refused)
    monkeypatch.setattr(sqlite3, "connect", refused)

    recovery = _read_db_recovery(db_path, hermes_home, now=_NOW)

    assert recovery.repair_ledger_present is True
    assert recovery.failed_attempts == 3
    assert recovery.repair_budget_exhausted is True
    assert recovery.newest_retired_wal.trigger == "wal_generation_lost"


# --------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------


def _render(recovery: DbRecoveryState, *, detail: bool = True, width: int = 200) -> str:
    state = DashboardState(operations=OperationsState(db_recovery=recovery))
    return render_to_str(
        render_operations(state, Theme(), detail=detail), width=width, no_color=True
    )


def _populated(**overrides: object) -> DbRecoveryState:
    base: dict = {
        "repair_ledger_present": True,
        "failed_attempts": 1,
        "last_attempt": "2026-09-12T21:48:03",
        "last_attempt_age_seconds": 7200.0,
        "malformed_backup_count": 2,
        "malformed_backup_bytes": 1_500_000_000,
        "newest_malformed_backup_age_seconds": 259_200.0,
        "retired_wal_count": 1,
        "malformed_backup_staging_count": 1,
        "retired_wal_staging_count": 1,
        "repair_lock_file_present": False,
        "auto_maintenance_lock_file_present": True,
    }
    base.update(overrides)
    return DbRecoveryState(**base)


def test_detail_renders_the_recovery_section() -> None:
    rendered = _render(
        _populated(
            newest_retired_wal=RetiredWalGeneration(
                manifest_present=True,
                captured_at="2026-09-12T21:48:03Z",
                captured_at_age_seconds=7200.0,
                trigger="wal_generation_lost",
                wal_bytes=4096,
                main_mode="header_only",
            )
        )
    )

    assert "Database Recovery" in rendered
    assert "Repair Ledger" in rendered
    assert "1 failed · budget 1/3 · last 2h ago" in rendered
    assert "Malformed Backups" in rendered
    assert "2 · 1.5G · newest 3d ago" in rendered
    assert "Retired WAL" in rendered
    assert "wal_generation_lost" in rendered
    assert "header_only" in rendered
    assert "Lock Files" in rendered
    assert "auto-maintenance ✓" in rendered
    assert "In Progress" in rendered


def test_detail_says_when_the_repair_budget_is_exhausted() -> None:
    rendered = _render(_populated(failed_attempts=3))

    assert "3 failed · budget exhausted (max 3) · last 2h ago" in rendered


def test_detail_wording_for_an_absent_ledger() -> None:
    rendered = _render(_populated(repair_ledger_present=False, failed_attempts=0))

    assert "none — no failed repair recorded" in rendered
    assert "not evidence the database is healthy" in rendered


def test_detail_states_that_nothing_is_hashed_or_repaired() -> None:
    rendered = _render(_populated())

    assert "never repairs" in rendered
    assert "hashes" in rendered


def test_detail_flags_a_truncated_scan() -> None:
    rendered = _render(_populated(scan_truncated=True))

    assert "truncated" in rendered


def test_compact_operations_shows_no_recovery_free_text() -> None:
    """Panel 12's compact view stays numeric: no untrusted string reaches it."""
    rendered = _render(_populated(), detail=False, width=120)

    assert "Database Recovery" not in rendered
    assert "wal_generation_lost" not in rendered


def test_empty_recovery_renders_no_section() -> None:
    rendered = _render(DbRecoveryState())

    assert "Database Recovery" not in rendered
    assert "No operations artifacts found" in rendered
