"""F19 — ``gateway_migration.json`` is progress evidence, never proof of success.

Upstream writes the manifest *inside* the per-secondary loop, before it flips
``gateway.multiplex_profiles`` and before it restarts the default gateway, and
never touches it again on either the verified or the unverified exit path
(``hermes_cli/gateway_migrate.py:528-561``). There is no ``completed`` /
``verified`` / ``outcome`` field. So a manifest on disk cannot distinguish
mid-flight from crashed, applied-but-unverified, or fully verified, and every
test here pins one of those separations.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import yaml

from hermesd.collect.common import _iso_to_epoch
from hermesd.collect.migration import (
    _MANIFEST_SECONDARY_LIMIT,
    _migration_state,
    _multiplex_flag_on,
)
from hermesd.collector import Collector
from hermesd.models import GatewayState, MigrationState, MigrationVerificationGap

NOW = 1_800_000_000.0
# The only local-time, uncolonned-offset stamp hermesd reads: upstream builds it
# with time.strftime("%Y-%m-%dT%H:%M:%S%z") (gateway_migrate.py:529).
MIGRATED_AT = "2026-09-13T00:52:11+0200"
MIGRATED_AT_EPOCH = 1_789_253_531.0


def _clock() -> float:
    return NOW


def _collect(home: Path, *, live_pid: int = 4242):
    collector = Collector(home, pid_exists=lambda pid: pid == live_pid, clock=_clock)
    try:
        return collector.collect()
    finally:
        collector.close()


def _write_gateway_state(home: Path, *, served: object = None, running: bool = True) -> None:
    payload: dict[str, object] = {
        "pid": 4242,
        "gateway_state": "running" if running else "stopped",
        "platforms": {},
    }
    if served is not None:
        payload["served_profiles"] = served
    (home / "gateway_state.json").write_text(json.dumps(payload))


def _write_config(home: Path, *, multiplex: object = True, nested: bool = True) -> None:
    cfg: dict[str, object] = {}
    if nested:
        cfg["gateway"] = {"multiplex_profiles": multiplex}
    else:
        cfg["multiplex_profiles"] = multiplex
    (home / "config.yaml").write_text(yaml.dump(cfg))


def _manifest(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "version": 1,
        "migrated_at": MIGRATED_AT,
        "flag_was": False,
        "default": {
            "profile": "default",
            "home": "/h/.hermes",
            "pid": 4242,
            "service": {"kind": "launchd", "system": False},
        },
        "secondaries": [
            {
                "profile": "dev",
                "home": "/h/.hermes/profiles/dev",
                "pid": 4300,
                "service": {"kind": "launchd", "system": False},
            },
            {
                "profile": "coding",
                "home": "/h/.hermes/profiles/coding",
                "pid": None,
                "service": {"kind": "systemd", "system": True},
            },
        ],
    }
    data.update(overrides)
    return data


def _write_manifest(home: Path, data: object = None) -> Path:
    path = home / "gateway_migration.json"
    path.write_text(json.dumps(_manifest() if data is None else data))
    return path


def _multiplexed(home: Path, *, served: object = None) -> None:
    """A home whose live gateway records the manifest's whole expected set."""
    _write_gateway_state(home, served=["default", "dev", "coding"] if served is None else served)
    _write_config(home)
    _write_manifest(home)


# --------------------------------------------------------------------------
# A. the manifest alone is not proof of anything
# --------------------------------------------------------------------------


def test_absent_manifest_reads_as_no_record(hermes_home: Path):
    """Absent means "never migrated OR successfully rolled back" — indistinguishable."""
    _write_gateway_state(hermes_home, served=["default"])
    _write_config(hermes_home, multiplex=False)

    state = _collect(hermes_home)

    assert state.migration.manifest_present is False
    assert state.migration.migration_verified is False
    assert state.migration.verification_gap is MigrationVerificationGap.NO_MANIFEST
    assert "migration" not in state.health.failed_sources


def test_a_live_multiplexer_matching_the_manifest_is_verified(hermes_home: Path):
    _multiplexed(hermes_home)

    migration = _collect(hermes_home).migration

    assert migration.migration_verified is True
    assert migration.verification_gap is MigrationVerificationGap.NONE
    assert migration.manifest_parsed is True
    assert migration.multiplex_flag_on is True
    assert migration.default_gateway_live is True
    assert migration.served_recorded is True
    assert migration.default_profile.served is True
    assert [record.served for record in migration.secondaries] == [True, True]


def test_a_manifest_with_no_live_served_record_is_not_verified(hermes_home: Path):
    """The central defect: the manifest is written before verification ever runs.

    ``gateway_state.json`` here carries no ``served_profiles`` at all, which is
    exactly what a gateway that has not yet restarted looks like — and exactly
    what a fully verified migration looks like too, from the manifest alone.
    """
    _write_gateway_state(hermes_home, served=None)
    _write_config(hermes_home)
    _write_manifest(hermes_home)

    migration = _collect(hermes_home).migration

    assert migration.manifest_present is True
    assert migration.migration_verified is False
    assert migration.verification_gap is MigrationVerificationGap.SERVED_NOT_RECORDED


def test_an_empty_live_served_record_is_not_verified(hermes_home: Path):
    """A live ``[]`` is authoritative ("serves nobody else"), so it refutes the manifest."""
    _multiplexed(hermes_home, served=[])

    migration = _collect(hermes_home).migration

    assert migration.served_recorded is True
    assert migration.migration_verified is False
    assert migration.verification_gap is MigrationVerificationGap.PROFILES_UNSERVED
    assert migration.unserved_profiles == ["default", "dev", "coding"]


def test_the_multiplex_flag_must_be_on(hermes_home: Path):
    _write_gateway_state(hermes_home, served=["default", "dev", "coding"])
    _write_config(hermes_home, multiplex=False)
    _write_manifest(hermes_home)

    migration = _collect(hermes_home).migration

    assert migration.multiplex_flag_on is False
    assert migration.migration_verified is False
    assert migration.verification_gap is MigrationVerificationGap.FLAG_OFF


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({"multiplex_profiles": True}, True),  # stale top-level alias only
        ({"gateway": {"multiplex_profiles": True}}, True),
        # The reader ORs the stale alias with the nested key, so a leftover
        # top-level True wins over a nested False. hermesd matches the *reader*
        # (gateway_migrate.py:203-215), not the writer, which pops the alias.
        ({"multiplex_profiles": True, "gateway": {"multiplex_profiles": False}}, True),
        ({"gateway": {}}, False),
        ({}, False),
        ({"multiplex_profiles": "yes"}, True),
        ({"multiplex_profiles": 0, "gateway": {"multiplex_profiles": None}}, False),
    ],
)
def test_the_flag_is_read_the_way_upstream_reads_it(cfg: dict[str, object], expected: bool):
    assert _multiplex_flag_on(cfg) is expected


def test_a_dead_default_gateway_is_not_verified(hermes_home: Path):
    """``recorded_served_profiles`` returns None when the pid is dead; so does the verdict."""
    _write_gateway_state(hermes_home, served=["default", "dev", "coding"], running=False)
    _write_config(hermes_home)
    _write_manifest(hermes_home)

    migration = _collect(hermes_home, live_pid=0).migration

    assert migration.default_gateway_live is False
    assert migration.served_recorded is False
    assert migration.migration_verified is False
    assert migration.verification_gap is MigrationVerificationGap.GATEWAY_NOT_LIVE


def test_a_served_set_missing_a_secondary_is_not_verified(hermes_home: Path):
    """Mid-flight shape: the manifest lists two secondaries, one is being served."""
    _multiplexed(hermes_home, served=["default", "dev"])

    migration = _collect(hermes_home).migration

    assert migration.migration_verified is False
    assert migration.verification_gap is MigrationVerificationGap.PROFILES_UNSERVED
    assert migration.unserved_profiles == ["coding"]
    assert [record.served for record in migration.secondaries] == [True, False]


def test_a_served_set_missing_the_default_is_not_verified(hermes_home: Path):
    _multiplexed(hermes_home, served=["dev", "coding"])

    migration = _collect(hermes_home).migration

    assert migration.migration_verified is False
    assert migration.unserved_profiles == ["default"]


def test_coverage_is_never_claimed_from_a_served_record_that_is_not_live(
    hermes_home: Path,
):
    """A dead gateway's stale served list must not mark recorded profiles as covered.

    The verdict already refuses — ``served_recorded`` gates it — but the per-record
    ``served`` flags and ``unserved_profiles`` are exported in the JSON snapshot, so
    they have to agree with the verdict instead of describing a topology that is gone.
    """
    _write_gateway_state(hermes_home, served=["default", "dev", "coding"], running=False)
    _write_config(hermes_home)
    _write_manifest(hermes_home)

    migration = _collect(hermes_home, live_pid=0).migration

    assert migration.served_recorded is False
    assert migration.default_profile.served is False
    assert [record.served for record in migration.secondaries] == [False, False]
    assert migration.unserved_profiles == ["default", "dev", "coding"]
    assert migration.migration_verified is False


def test_an_unfinished_update_receipt_does_not_decide_the_migration_verdict(
    hermes_home: Path,
):
    """``maybe_auto_migrate_after_update`` makes both sections light up on one tick.

    "Update receipt unverified" and "migration unverified" are different claims, so
    a failed update receipt must not overturn a topology the live artifacts verify.
    """
    _multiplexed(hermes_home)
    receipts = hermes_home / "logs" / "update_receipts"
    receipts.mkdir(parents=True)
    (receipts / "latest.json").write_text(
        json.dumps({"outcome": "failed", "exit_code": 1, "steps": [{"name": "pull", "ok": False}]})
    )

    state = _collect(hermes_home)

    assert state.gateway.update_receipt_unfinished is True
    assert state.gateway.last_update_failed_step == "pull"
    assert state.migration.migration_verified is True


# --------------------------------------------------------------------------
# B. recorded intent, kept separate from progress and from the verdict
# --------------------------------------------------------------------------


def test_recorded_intent_is_surfaced_verbatim(hermes_home: Path):
    _multiplexed(hermes_home)

    migration = _collect(hermes_home).migration

    assert migration.manifest_version == 1
    assert migration.migrated_at == MIGRATED_AT
    assert migration.flag_was is False
    assert migration.flag_flipped is True
    assert migration.default_profile.profile == "default"
    assert migration.default_profile.home == "/h/.hermes"
    assert migration.default_profile.service_label == "launchd"
    assert migration.secondary_count == 2
    assert migration.secondaries_truncated is False
    assert [record.profile for record in migration.secondaries] == ["dev", "coding"]
    assert migration.secondaries[1].service_label == "systemd (system)"
    assert migration.secondaries[0].service_label == "launchd"


def test_a_service_less_secondary_records_no_service(hermes_home: Path):
    _multiplexed(hermes_home)
    _write_manifest(
        hermes_home,
        _manifest(secondaries=[{"profile": "dev", "home": "/h/.hermes/profiles/dev", "pid": 7}]),
    )

    migration = _collect(hermes_home).migration

    assert migration.secondaries[0].service_kind == ""
    assert migration.secondaries[0].service_label == "none"


def test_flag_was_true_with_the_flag_still_on_is_not_a_flip(hermes_home: Path):
    _multiplexed(hermes_home)
    _write_manifest(hermes_home, _manifest(flag_was=True))

    migration = _collect(hermes_home).migration

    assert migration.flag_was is True
    assert migration.flag_flipped is False


def test_a_missing_version_or_stamp_degrades_to_defaults(hermes_home: Path):
    _multiplexed(hermes_home)
    _write_manifest(hermes_home, _manifest(version=None, migrated_at=""))

    migration = _collect(hermes_home).migration

    assert migration.manifest_parsed is True
    assert migration.manifest_version == 0
    assert migration.migrated_at == ""
    assert migration.migrated_at_age_seconds is None


def test_garbage_secondary_entries_are_never_counted_as_covered(hermes_home: Path):
    _multiplexed(hermes_home)
    _write_manifest(hermes_home, _manifest(secondaries=["dev", 3, {"profile": "coding"}]))

    migration = _collect(hermes_home).migration

    assert migration.secondary_count == 3
    assert migration.migration_verified is False
    assert migration.verification_gap is MigrationVerificationGap.PROFILES_UNSERVED


def test_secondaries_past_the_retained_limit_cannot_verify(hermes_home: Path):
    """An untrusted manifest cannot grow the retained list, and cannot verify either."""
    entries = [{"profile": f"p{index}", "home": "/h", "pid": None} for index in range(60)]
    _write_gateway_state(hermes_home, served=["default", *(entry["profile"] for entry in entries)])
    _write_config(hermes_home)
    _write_manifest(hermes_home, _manifest(secondaries=entries))

    migration = _collect(hermes_home).migration

    assert migration.secondary_count == 60
    assert len(migration.secondaries) == _MANIFEST_SECONDARY_LIMIT
    assert migration.secondaries_truncated is True
    assert migration.migration_verified is False
    assert migration.verification_gap is MigrationVerificationGap.SECONDARIES_TRUNCATED


def test_a_long_recorded_home_is_capped(hermes_home: Path):
    """A recorded home is display data: a 5 KB untrusted string must not reach a panel."""
    _multiplexed(hermes_home)
    _write_manifest(
        hermes_home,
        _manifest(secondaries=[{"profile": "dev", "home": "x" * 5000, "pid": None}]),
    )

    migration = _collect(hermes_home).migration

    assert len(migration.secondaries[0].home) <= 200
    assert set(migration.secondaries[0].home) == {"x"}


# --------------------------------------------------------------------------
# C. migrated_at is local time with an uncolonned offset
# --------------------------------------------------------------------------


def test_migrated_at_parses_an_uncolonned_local_offset():
    """``datetime.fromisoformat`` accepts ``+0200`` on Python 3.11, the pinned floor.

    Every other stamp hermesd reads is UTC (``Z`` or ``+00:00``); this one is local
    time, so the offset is load-bearing rather than decorative — dropping it would
    read the attempt as two hours later than it began.
    """
    assert _iso_to_epoch(MIGRATED_AT) == MIGRATED_AT_EPOCH
    assert _iso_to_epoch("2026-09-13T00:52:11+00:00") - MIGRATED_AT_EPOCH == 7200.0


def test_migrated_at_age_is_measured_from_the_local_offset(hermes_home: Path):
    _multiplexed(hermes_home)
    _write_manifest(hermes_home, _manifest(migrated_at=MIGRATED_AT))

    collector = Collector(
        hermes_home,
        pid_exists=lambda pid: pid == 4242,
        clock=lambda: MIGRATED_AT_EPOCH + 3600.0,
    )
    try:
        migration = collector.collect().migration
    finally:
        collector.close()

    assert migration.migrated_at_age_seconds == pytest.approx(3600.0)


def test_an_unparseable_migrated_at_is_not_an_age(hermes_home: Path):
    _multiplexed(hermes_home)
    _write_manifest(hermes_home, _manifest(migrated_at="yesterday"))

    migration = _collect(hermes_home).migration

    assert migration.migrated_at == "yesterday"
    assert migration.migrated_at_age_seconds is None


# --------------------------------------------------------------------------
# D. resilience: a torn manifest degrades only itself
# --------------------------------------------------------------------------


def test_a_torn_manifest_keeps_last_good_and_fails_only_its_own_source(hermes_home: Path):
    _multiplexed(hermes_home)
    manifest = hermes_home / "gateway_migration.json"

    collector = Collector(hermes_home, pid_exists=lambda pid: pid == 4242, clock=_clock)
    try:
        first = collector.collect()
        assert first.migration.migration_verified is True
        # _write_manifest upstream is a plain write_text, so a torn file is observable.
        manifest.write_text('{"version": 1, "migrated_at": "2026-09-13T00:5')
        second = collector.collect()
    finally:
        collector.close()

    assert second.migration.migration_verified is True
    assert second.migration.migrated_at == first.migration.migrated_at
    assert second.migration.secondary_count == 2
    assert "migration" in second.health.failed_sources
    # Only the migration source degraded: the gateway beside it is still fresh.
    assert "gateway" not in second.health.failed_sources
    assert second.gateway.served_profiles_recorded is True


def test_a_torn_manifest_without_a_prior_read_is_present_but_unparsed(hermes_home: Path):
    """Never-readable is not the same as lost: this is a reportable state, not a failure."""
    _write_gateway_state(hermes_home, served=["default", "dev", "coding"])
    _write_config(hermes_home)
    (hermes_home / "gateway_migration.json").write_text('{"version": 1, "migrated_at"')

    state = _collect(hermes_home)

    assert state.migration.manifest_present is True
    assert state.migration.manifest_parsed is False
    assert state.migration.migration_verified is False
    assert state.migration.verification_gap is MigrationVerificationGap.MANIFEST_UNREADABLE
    assert "migration" not in state.health.failed_sources


def test_a_manifest_that_disappears_after_a_good_read_fails_the_source(hermes_home: Path):
    """Rollback deletes the manifest, so vanishing after a good read is reported."""
    _multiplexed(hermes_home)
    manifest = hermes_home / "gateway_migration.json"

    collector = Collector(hermes_home, pid_exists=lambda pid: pid == 4242, clock=_clock)
    try:
        first = collector.collect()
        assert first.migration.manifest_present is True
        manifest.unlink()
        second = collector.collect()
    finally:
        collector.close()

    assert second.migration.manifest_present is True
    assert second.migration.migrated_at == first.migration.migrated_at
    assert "migration" in second.health.failed_sources


def test_an_absent_manifest_never_fails_its_source(hermes_home: Path):
    _write_gateway_state(hermes_home, served=["default"])

    state = _collect(hermes_home)

    assert state.migration.manifest_present is False
    assert "migration" not in state.health.failed_sources


def test_a_symlinked_manifest_outside_the_home_is_not_followed(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_manifest()))
    (hermes_home / "gateway_migration.json").symlink_to(outside)
    _write_gateway_state(hermes_home, served=["default", "dev", "coding"])
    _write_config(hermes_home)

    state = _collect(hermes_home)

    assert state.migration.manifest_present is False
    assert state.migration.migration_verified is False
    assert "migration" not in state.health.failed_sources


def test_an_unsafe_manifest_after_a_good_read_fails_the_source(hermes_home: Path, tmp_path: Path):
    _multiplexed(hermes_home)
    manifest = hermes_home / "gateway_migration.json"

    collector = Collector(hermes_home, pid_exists=lambda pid: pid == 4242, clock=_clock)
    try:
        first = collector.collect()
        assert first.migration.manifest_present is True
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps(_manifest()))
        manifest.unlink()
        manifest.symlink_to(outside)
        second = collector.collect()
    finally:
        collector.close()

    assert second.migration.manifest_present is True
    assert "migration" in second.health.failed_sources


def test_a_non_mapping_manifest_is_not_a_record(hermes_home: Path):
    _multiplexed(hermes_home)
    (hermes_home / "gateway_migration.json").write_text(json.dumps(["not", "a", "mapping"]))

    migration = _collect(hermes_home).migration

    assert migration.manifest_present is True
    assert migration.manifest_parsed is False
    assert migration.migration_verified is False


# --------------------------------------------------------------------------
# E. the recorded home is never a path hermesd reads
# --------------------------------------------------------------------------


def test_the_migration_reader_cannot_resolve_a_recorded_home():
    """Upstream's rollback builds ``Path(rec["home"])`` straight from this file
    (``gateway_migrate.py:594``). hermesd must not: an untrusted manifest cannot be
    allowed to steer a read. The reader is pure — it takes already-loaded JSON and
    has no path type at all, so no recorded value can become a filesystem target.
    """
    module = Path(__file__).resolve().parent.parent / "hermesd" / "collect" / "migration.py"
    tree = ast.parse(module.read_text())

    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(module_name.split(".")[0] == "pathlib" for module_name in imported)

    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_attrs = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "Path" not in called_names
    assert not called_attrs & {"shared_path", "profile_path", "open", "read_text", "stat"}


def test_a_recorded_home_pointing_outside_the_home_is_display_data(
    hermes_home: Path, tmp_path: Path
):
    """A hostile ``home`` is recorded verbatim and changes nothing else."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "config.yaml").write_text(yaml.dump({"model": {"default": "SENTINEL"}}))
    _multiplexed(hermes_home)
    _write_manifest(
        hermes_home,
        _manifest(secondaries=[{"profile": "dev", "home": str(outside), "pid": None}]),
    )

    state = _collect(hermes_home)

    assert state.migration.secondaries[0].home == str(outside)
    assert state.config.model != "SENTINEL"
    assert state.migration.migration_verified is True


# --------------------------------------------------------------------------
# F. model defaults
# --------------------------------------------------------------------------


def test_migration_state_constructs_empty():
    migration = MigrationState()

    assert migration.manifest_present is False
    assert migration.manifest_parsed is False
    assert migration.secondaries == []
    assert migration.unserved_profiles == ["default"]
    assert migration.verification_gap is MigrationVerificationGap.NO_MANIFEST
    assert migration.migration_verified is False
    assert migration.flag_flipped is False


def test_the_verdict_is_derived_from_the_evidence():
    """``migration_verified`` is never stored: it cannot drift from the gap."""
    assert "migration_verified" not in MigrationState.model_fields
    assert "verification_gap" not in MigrationState.model_fields
    assert "unserved_profiles" not in MigrationState.model_fields


def test_the_verdict_needs_every_clause():
    """Each clause of the predicate is load-bearing; dropping one flips the verdict."""
    verified = _migration_state(
        _manifest(),
        now=NOW,
        cfg={"gateway": {"multiplex_profiles": True}},
        gateway=GatewayState(
            running=True,
            served_profiles=["default", "dev", "coding"],
            served_profiles_recorded=True,
        ),
    )
    assert verified.migration_verified is True

    refutations = {
        "manifest": verified.model_copy(update={"manifest_parsed": False}),
        "flag": verified.model_copy(update={"multiplex_flag_on": False}),
        "liveness": verified.model_copy(update={"default_gateway_live": False}),
        "served record": verified.model_copy(update={"served_recorded": False}),
        "coverage": verified.model_copy(
            update={
                "secondaries": [
                    record.model_copy(update={"served": False}) for record in verified.secondaries
                ]
            }
        ),
    }
    for clause, refuted in refutations.items():
        assert refuted.migration_verified is False, clause
