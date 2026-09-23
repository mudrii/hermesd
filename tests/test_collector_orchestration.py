"""Collector orchestration: per-source fallbacks, shared caches and guards.

Each test pins one cross-source property of ``Collector.collect()`` — which
source a failure is charged to, what its fallback restores, and what a cache
or guard keeps between passes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from hermesd.collector import _STATE_DB_SOURCES, Collector, _state_db_readout

_SHA_NEW = "a" * 40
_SHA_OLD = "b" * 40


def _write_plugin(home: Path, name: str, *, catalog_sha: str | None = None) -> None:
    plugin_dir = home / "plugins" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(f"name: {name}\nversion: 1.0.0\n")
    if catalog_sha is not None:
        (plugin_dir / ".hermes-catalog.json").write_text(
            json.dumps({"catalog_name": name, "sha": catalog_sha})
        )


def test_plugin_catalog_failure_keeps_the_fresh_plugin_inventory(
    hermes_home: Path, tmp_path: Path
) -> None:
    """A failed catalog enrichment restores its verdicts, never the plugin list.

    The list itself belongs to the skills source: a plugin installed after the
    last good catalog read must still appear, while the drift flag the catalog
    stamped on an already-known plugin is carried forward by name.
    """
    _write_plugin(hermes_home, "weather", catalog_sha=_SHA_OLD)
    cache_dir = hermes_home / "cache"
    cache_dir.mkdir()
    cache_path = cache_dir / "plugin-catalog.json"
    cache_path.write_text(json.dumps({"entries": [{"name": "weather", "sha": _SHA_NEW}]}))
    outside = tmp_path / "elsewhere.json"
    outside.write_text("{}")

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.skills_memory.plugins[0].catalog_update_available is True

        _write_plugin(hermes_home, "rain")
        cache_path.unlink()
        cache_path.symlink_to(outside)
        second = c.collect()
    finally:
        c.close()

    assert "plugin_catalog" in second.health.failed_sources
    by_name = {plugin.name: plugin for plugin in second.skills_memory.plugins}
    assert set(by_name) == {"weather", "rain"}
    assert by_name["weather"].catalog_update_available is True
    assert by_name["rain"].catalog_update_available is False
    assert second.skills_memory.plugin_catalog_update_count == 1


def test_one_bad_state_db_table_fails_only_its_own_source(forensic_hermes_home: Path) -> None:
    """A gateway_routing table missing entry_json fails gateway_routes alone.

    Every state.db source shares one readout per pass. A broken table group
    must be charged to the source that owns it, not to the five siblings read
    from the same connection, and the readout must not be retried by each of
    them in turn.
    """
    db_path = forensic_hermes_home / "state.db"
    c = Collector(forensic_hermes_home, pid_exists=lambda pid: pid == 12345)
    try:
        first = c.collect()
        assert first.session_coordination.route_total == 1

        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            "ALTER TABLE gateway_routing RENAME TO gateway_routing_old;"
            "CREATE TABLE gateway_routing (scope TEXT, session_key TEXT, updated_at REAL);"
        )
        conn.commit()
        conn.close()

        readouts: list[object] = []
        run_readout = c._db.run_readout

        def counting_readout(fn):  # type: ignore[no-untyped-def]
            readouts.append(fn)
            return run_readout(fn)

        c._db.run_readout = counting_readout  # type: ignore[method-assign]
        second = c.collect()
    finally:
        c.close()

    assert _STATE_DB_SOURCES & set(second.health.failed_sources) == {"gateway_routes"}
    assert second.session_coordination.routes == first.session_coordination.routes
    assert [lease.key for lease in second.session_coordination.leases] == [
        lease.key for lease in first.session_coordination.leases
    ]
    assert second.operations.delegation_count == first.operations.delegation_count
    assert len(readouts) == 1


def test_a_bad_ledger_table_fails_only_gateway_ledgers(forensic_hermes_home: Path) -> None:
    db_path = forensic_hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "DROP TABLE delivery_obligations;CREATE TABLE delivery_obligations (obligation_id TEXT);"
    )
    conn.commit()
    conn.close()

    c = Collector(forensic_hermes_home, pid_exists=lambda pid: pid == 12345)
    try:
        state = c.collect()
    finally:
        c.close()

    assert _STATE_DB_SOURCES & set(state.health.failed_sources) == {"gateway_ledgers"}
    assert state.session_coordination.route_total == 1


def test_an_unreadable_state_db_connection_raises_the_whole_readout() -> None:
    """With every group failing, the readout raises so HermesDB can reconnect."""
    conn = sqlite3.connect(":memory:")
    conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        _state_db_readout(conn)


def test_a_failed_state_db_readout_is_attempted_once_per_pass(
    forensic_hermes_home: Path,
) -> None:
    c = Collector(forensic_hermes_home, pid_exists=lambda pid: pid == 12345)
    try:
        c.collect()
        attempts: list[object] = []

        def failing_readout(fn):  # type: ignore[no-untyped-def]
            attempts.append(fn)
            raise sqlite3.OperationalError("database disk image is malformed")

        c._db.run_readout = failing_readout  # type: ignore[method-assign]
        # Touch state.db so the mtime-keyed clean readout is not reused.
        db_path = forensic_hermes_home / "state.db"
        stat = db_path.stat()
        os.utime(db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        second = c.collect()
        third = c.collect()
    finally:
        c.close()

    assert set(second.health.failed_sources) >= _STATE_DB_SOURCES
    assert set(third.health.failed_sources) >= _STATE_DB_SOURCES
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "content",
    ['{"pid": true}', '{"pid": [4243]}', '{"pid": 1e400}', "[4243]", "1e400", "true"],
    ids=["bool", "list", "overflow", "bare-list", "bare-overflow", "bare-bool"],
)
def test_malformed_gateway_pid_file_reads_as_no_launchd_gateway(
    hermes_home: Path, content: str
) -> None:
    """A junk gateway.pid is no replacement pid: never pid 1, never a crash."""
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": 4242, "gateway_state": "running", "platforms": {}})
    )
    (hermes_home / "gateway.pid").write_text(content)
    c = Collector(hermes_home, pid_exists=lambda pid: pid != 4242)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "gateway" not in state.health.failed_sources
    assert state.gateway.running is False
    assert state.gateway.pid == 4242


def test_derived_cache_never_serves_a_value_for_a_freed_rows_list(hermes_home: Path) -> None:
    """The derived cache is keyed on the rows list itself, not its id().

    A freed list's address is routinely reused by the next list allocated, so
    an id()-keyed entry could hand one list's derived value to another.
    """
    c = Collector(hermes_home)
    try:
        first = c._derived_from_rows("probe", [{"id": "a"}], lambda rows: rows[0]["id"])
        second = c._derived_from_rows("probe", [{"id": "b"}], lambda rows: rows[0]["id"])
    finally:
        c.close()

    assert (first, second) == ("a", "b")


def test_cron_excerpt_cache_survives_a_job_id_containing_a_colon(hermes_home: Path) -> None:
    """The excerpt cache must key (output root, job id) without re-splitting a
    joined string: a ``:`` in the id made pruning evict the entry every pass."""
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "team:nightly", "name": "Nightly"}]})
    )
    output_dir = hermes_home / "cron" / "output" / "team:nightly"
    output_dir.mkdir(parents=True)
    output_file = output_dir / "latest.md"
    output_file.write_text("digest sent\n")

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.cron.jobs[0].latest_output_excerpt == "digest sent"
        output_file.unlink()
        second = c.collect()
    finally:
        c.close()

    assert second.cron.jobs[0].latest_output_excerpt == "digest sent"


def test_cron_job_errors_are_redacted(hermes_home: Path) -> None:
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-1",
                        "name": "Job 1",
                        "last_error": "HTTP 401 with api_key=sk-live-cron-secret",
                        "last_delivery_error": "POST https://user:hunter2@hooks.example/x failed",
                    }
                ]
            }
        )
    )
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    (job,) = state.cron.jobs
    assert "sk-live-cron-secret" not in job.last_error
    assert "hunter2" not in job.last_delivery_error
    assert "[REDACTED]" in job.last_error


@pytest.mark.parametrize(
    ("relpath", "good", "source"),
    [
        ("cron/jobs.json", {"jobs": [{"id": "job-1", "name": "Job 1"}]}, "cron"),
        ("channel_directory.json", {"platforms": {"telegram": []}}, "channels"),
        ("spawn-ledger.json", [{"pid": 4242, "session_id": "s1", "command": "x"}], None),
    ],
    ids=["jobs", "channel-directory", "spawn-ledger"],
)
def test_corrupt_json_after_a_good_read_is_reported_stale(
    hermes_home: Path, relpath: str, good: object, source: str | None
) -> None:
    """A corrupt file served from the last-good cache must name its source."""
    path = hermes_home / relpath
    path.write_text(json.dumps(good))
    c = Collector(hermes_home, pid_exists=lambda pid: True)
    try:
        first = c.collect()
        path.write_text("{ torn write")
        second = c.collect()
    finally:
        c.close()

    name = source or "background_processes"
    assert name not in first.health.failed_sources
    assert name in second.health.failed_sources
    if name == "cron":
        assert [job.name for job in second.cron.jobs] == ["Job 1"]
    elif name == "channels":
        assert second.channels.platform_count == first.channels.platform_count
    else:
        assert second.background_processes == first.background_processes


def test_gateway_pid_file_names_the_live_replacement(hermes_home: Path) -> None:
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": 4242, "gateway_state": "running", "platforms": {}})
    )
    (hermes_home / "gateway.pid").write_text('{"pid": 4243}')
    c = Collector(hermes_home, pid_exists=lambda pid: pid == 4243)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.gateway.running is True
    assert state.gateway.pid == 4243
