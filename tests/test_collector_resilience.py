"""Resilience tests for the collector against corrupt, hostile or unreadable ~/.hermes data.

Every test here asserts the cache-preservation invariant: after a fault is
injected, the affected source falls back to the last good value instead of
blanking the dashboard, and the source is named in ``health.failed_sources``
whenever the collector could tell that the read failed.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

import hermesd.collect.sessions as sessions_module
import hermesd.collector as collector_module
from hermesd.collect.plugins import PLUGIN_KIND_STANDALONE
from hermesd.collector import Collector
from hermesd.models import ConfigSummary, DashboardState
from tests.test_collector_api_runs import _build_populated_db as build_runs_db
from tests.test_collector_hosted_rooms import _build_populated_db as build_shared_state_db
from tests.test_collector_operations import (
    create_projects_db_tables,
    create_verification_evidence_db_tables,
)

_UTF8_CONTINUATION_RANGE = range(0x80, 0xC0)


@contextmanager
def _unreadable_dir(path: Path) -> Iterator[None]:
    """chmod 000 a directory for the duration of the block, then restore it."""
    original_mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(0o000)
    try:
        yield
    finally:
        path.chmod(original_mode)


def _agent_log(home: Path) -> Path:
    return home / "logs" / "agent.log"


def _agent_lines(state) -> list[str]:
    return [line.message for line in state.logs.agent_lines]


def _write_legacy_session_index(home: Path, session_id: str) -> Path:
    """Write a legacy sessions.json stub plus its session_<id>.json payload."""
    sessions = home / "sessions"
    sessions.mkdir(exist_ok=True)
    (sessions / "sessions.json").write_text(json.dumps({"cli": {"session_id": session_id}}))
    session_file = sessions / f"session_{session_id}.json"
    session_file.write_text(
        json.dumps({"session_id": session_id, "tools": [{"name": "web_search"}]})
    )
    return session_file


def test_non_utf8_bytes_in_agent_log_are_replaced_not_fatal(populated_hermes_home: Path) -> None:
    collector = Collector(populated_hermes_home)
    try:
        first = collector.collect()
        assert "Session saved" in _agent_lines(first)[-1]

        with _agent_log(populated_hermes_home).open("ab") as handle:
            handle.write(b"\xff\xfe garbage\n")

        second = collector.collect()
        assert "logs" not in second.health.failed_sources
        messages = _agent_lines(second)
        # The undecodable bytes are replaced, not dropped, and never crash the read.
        assert "�" in messages[-1]
        assert "garbage" in messages[-1]
        # Every well-formed line before the garbage survives intact.
        assert any("Session saved" in message for message in messages)
        assert any("Tool call: web_search" in message for message in messages)
    finally:
        collector.close()


def test_corrupt_session_file_keeps_last_good_tool_inventory(populated_hermes_home: Path) -> None:
    session_file = _write_legacy_session_index(populated_hermes_home, "sess_001")
    collector = Collector(populated_hermes_home)
    try:
        first = collector.collect()
        assert first.available_tool_names == ["web_search"]

        # Truncated JSON carrying non-UTF-8 bytes: neither json nor utf-8 can read it.
        session_file.write_bytes(b'{"session_id": "sess_001", "tools": [{"na\xff\xfe')
        second = collector.collect()

        # No banner snapshot exists, so the session files are the only tool
        # inventory; an unreadable one must not blank the tools panel.
        assert "tools_index" in second.health.failed_sources
        assert second.available_tool_names == ["web_search"]
        assert second.available_tools == 1
    finally:
        collector.close()


def test_session_file_named_by_the_index_but_missing_contributes_no_tools(
    hermes_home: Path,
) -> None:
    """A dangling index entry is normal churn, not a read fault: it has no tools."""
    sessions = hermes_home / "sessions"
    sessions.mkdir(exist_ok=True)
    (sessions / "sessions.json").write_text(
        json.dumps(
            {
                "cli": {"session_id": "sess_present"},
                "telegram": {"session_id": "sess_deleted"},
            }
        )
    )
    (sessions / "session_sess_present.json").write_text(
        json.dumps({"session_id": "sess_present", "tools": [{"name": "web_search"}]})
    )

    collector = Collector(hermes_home)
    try:
        state = collector.collect()
    finally:
        collector.close()

    assert state.available_tool_names == ["web_search"]
    assert state.available_tools == 1
    assert "tools_index" not in state.health.failed_sources


@pytest.mark.parametrize(
    "corrupt_payload",
    ['["not", "a", "mapping"]', '"scalar"', "null"],
    ids=["list", "scalar", "null"],
)
def test_cron_jobs_json_with_wrong_top_level_keeps_last_good_jobs(
    populated_hermes_home: Path,
    corrupt_payload: str,
) -> None:
    jobs_path = populated_hermes_home / "cron" / "jobs.json"
    jobs_path.write_text(json.dumps({"jobs": [{"id": "nightly", "name": "Nightly digest"}]}))
    collector = Collector(populated_hermes_home)
    try:
        first = collector.collect()
        assert [job.name for job in first.cron.jobs] == ["Nightly digest"]

        jobs_path.write_text(corrupt_payload)
        second = collector.collect()

        assert [job.name for job in second.cron.jobs] == ["Nightly digest"]
        assert second.cron.job_count == 1
    finally:
        collector.close()


@pytest.mark.parametrize(
    "corrupt_payload",
    ["just a string\n", "- one\n- two\n", ""],
    ids=["scalar", "list", "empty"],
)
def test_config_yaml_with_wrong_top_level_keeps_last_good_summary(
    populated_hermes_home: Path,
    corrupt_payload: str,
) -> None:
    config_path = populated_hermes_home / "config.yaml"
    # Two collects must be comparable: the summary carries age fields, so freeze
    # the clock rather than comparing two different instants.
    frozen_now = time.time()
    collector = Collector(populated_hermes_home, clock=lambda: frozen_now)
    try:
        first = collector.collect()
        assert first.config.model == "gpt-5.4"
        assert first.config.provider == "openai-codex"

        config_path.write_text(corrupt_payload)
        second = collector.collect()

        assert second.config == first.config
        assert second.config != ConfigSummary()
    finally:
        collector.close()


def _log_payload_cut_mid_character(tail_bytes: int) -> bytes:
    """Build a multi-byte log file whose last ``tail_bytes`` start mid-character.

    Padding is appended to the final line: growing the file at the end slides
    the cut point forward through the content until it lands inside one of the
    three-byte characters.
    """
    for padding in range(96):
        text = "".join(
            f"2026-04-09 15:42:{index % 60:02d},000 - hermes - INFO - café ✓ línea número {index}"
            f"{'x' * padding if index == 39 else ''}\n"
            for index in range(40)
        )
        payload = text.encode()
        if payload[len(payload) - tail_bytes] in _UTF8_CONTINUATION_RANGE:
            return payload
    raise AssertionError("no padding put the tail cut inside a multi-byte character")


def test_log_tail_cut_mid_multibyte_character_does_not_break_later_lines(
    populated_hermes_home: Path,
) -> None:
    tail_bytes = 1024  # the collector's floor for --log-tail-bytes
    payload = _log_payload_cut_mid_character(tail_bytes)
    assert len(payload) > tail_bytes
    _agent_log(populated_hermes_home).write_bytes(payload)

    collector = Collector(populated_hermes_home, log_tail_bytes=tail_bytes)
    try:
        state = collector.collect()
        assert "logs" not in state.health.failed_sources
        messages = _agent_lines(state)
        assert messages, "the tail read produced no log lines"
        # The split character is either replaced or lost with its partial line;
        # what matters is that it never corrupts the lines that follow.
        assert "�" not in messages[-1]
        assert "café ✓ línea número 39" in messages[-1]
        for message in messages[1:]:
            assert "�" not in message
        # A second pass over the same file is stable.
        assert _agent_lines(collector.collect()) == messages
    finally:
        collector.close()


def test_gateway_state_with_wrong_types_falls_back_to_safe_defaults(
    populated_hermes_home: Path,
) -> None:
    gateway_path = populated_hermes_home / "gateway_state.json"
    collector = Collector(populated_hermes_home, pid_exists=lambda pid: True)
    try:
        first = collector.collect()
        assert first.gateway.pid == 12345
        assert [platform.name for platform in first.gateway.platforms] == ["telegram", "discord"]

        gateway_path.write_text(
            json.dumps(
                {
                    "gateway_state": "running",
                    "platforms": ["telegram", "discord"],
                    "pid": "abc",
                    "active_agents": {"count": 3},
                }
            )
        )
        second = collector.collect()

        assert "gateway" not in second.health.failed_sources
        # Wrong types coerce to safe defaults rather than raising.
        assert second.gateway.pid == 0
        assert second.gateway.platforms == []
        assert second.gateway.active_agents == 0
        assert second.gateway.running is False
    finally:
        collector.close()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permission bits",
)
def test_unreadable_logs_directory_keeps_last_good_log_lines(populated_hermes_home: Path) -> None:
    collector = Collector(populated_hermes_home)
    try:
        first = collector.collect()
        good_lines = _agent_lines(first)
        assert good_lines

        with _unreadable_dir(populated_hermes_home / "logs"):
            second = collector.collect()

        assert "logs" in second.health.failed_sources
        assert _agent_lines(second) == good_lines
        # Other sources are unaffected by one unreadable directory.
        assert second.config.model == "gpt-5.4"
        assert second.skills_memory.skill_count == 15
    finally:
        collector.close()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permission bits",
)
def test_unreadable_sessions_directory_keeps_last_good_tool_inventory(
    populated_hermes_home: Path,
) -> None:
    _write_legacy_session_index(populated_hermes_home, "sess_001")
    collector = Collector(populated_hermes_home)
    try:
        first = collector.collect()
        assert first.available_tool_names == ["web_search"]

        with _unreadable_dir(populated_hermes_home / "sessions"):
            second = collector.collect()

        assert "tools_index" in second.health.failed_sources
        assert second.available_tool_names == ["web_search"]
        assert second.config.model == "gpt-5.4"
    finally:
        collector.close()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permission bits",
)
def test_unreadable_skills_directory_keeps_last_good_skills(populated_hermes_home: Path) -> None:
    collector = Collector(populated_hermes_home)
    try:
        first = collector.collect()
        assert first.skills_memory.skill_count == 15

        with _unreadable_dir(populated_hermes_home / "skills"):
            second = collector.collect()

        assert "skills" in second.health.failed_sources
        assert second.skills_memory.skill_count == 15
        # A failed source does not take the rest of the pass down with it.
        assert second.config.model == "gpt-5.4"
        assert _agent_lines(second)

        third = collector.collect()
        assert "skills" not in third.health.failed_sources
        assert third.skills_memory.skill_count == 15
    finally:
        collector.close()


def test_future_log_mtime_clamps_activity_age_to_zero(populated_hermes_home: Path) -> None:
    log_path = _agent_log(populated_hermes_home)
    collector = Collector(populated_hermes_home)
    try:
        one_hour_ahead = log_path.stat().st_mtime + 3600
        os.utime(log_path, (one_hour_ahead, one_hour_ahead))

        state = collector.collect()

        assert state.runtime.last_activity_age_seconds == 0.0
        assert state.runtime.agent_running is True
        assert state.runtime.banner == ""
        for stream in state.logs.streams:
            assert stream.mtime is None or stream.mtime > 0
    finally:
        collector.close()


def test_failing_source_keeps_last_good_value_and_is_reported_then_recovers(
    populated_hermes_home: Path,
) -> None:
    jobs_path = populated_hermes_home / "cron" / "jobs.json"
    jobs_path.write_text(json.dumps({"jobs": [{"id": "nightly", "name": "Nightly digest"}]}))
    collector = Collector(populated_hermes_home)
    try:
        first = collector.collect()
        assert [job.name for job in first.cron.jobs] == ["Nightly digest"]
        assert first.health.failed_sources == []

        def boom() -> None:
            raise RuntimeError("cron reader exploded")

        collector._collect_cron = boom  # type: ignore[method-assign]
        second = collector.collect()

        assert second.health.failed_sources == ["cron"]
        assert "cron reader exploded" in second.health.errors["cron"]
        assert [job.name for job in second.cron.jobs] == ["Nightly digest"]

        del collector._collect_cron
        third = collector.collect()

        assert third.health.failed_sources == []
        assert third.health.errors == {}
        assert [job.name for job in third.cron.jobs] == ["Nightly digest"]
    finally:
        collector.close()


def test_config_yaml_scalar_then_list_then_empty_never_blanks_config(
    populated_hermes_home: Path,
) -> None:
    config_path = populated_hermes_home / "config.yaml"
    collector = Collector(populated_hermes_home)
    try:
        assert collector.collect().config.model == "gpt-5.4"
        for payload in ("just a string\n", "- one\n- two\n"):
            config_path.write_text(payload)
            # A wrong top-level type never reaches the collector: the file
            # cache serves the last-good mapping, so the panels stay populated —
            # but the stale serve is now reported as a degraded source, the
            # same contract JSON-backed sources already had.
            state = collector.collect()
            assert state.config.model == "gpt-5.4"
            assert "config" in state.health.failed_sources

        config_path.write_text("")
        emptied = collector.collect()
        # An emptied file parses to a valid but empty mapping, so the config
        # source itself has to refuse it and fall back to the last-good value.
        assert "config" in emptied.health.failed_sources
        assert emptied.config.model == "gpt-5.4"
        # Restoring a valid config recovers without a restart.
        config_path.write_text(yaml.dump({"model": {"default": "gpt-6", "provider": "acme"}}))
        recovered = collector.collect()
        assert recovered.config.model == "gpt-6"
        assert recovered.config.provider == "acme"
    finally:
        collector.close()


def test_corrupt_mcp_schema_cache_keeps_last_good(hermes_home: Path):
    path = hermes_home / "cache" / "mcp_schema_cache.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"playwright": {"tools": []}, "sheets": {"tools": []}}))

    collector = Collector(hermes_home)
    try:
        first = collector.collect()
        assert first.mcp_cache.mcp_cached_server_names == ["playwright", "sheets"]

        path.write_text("{corrupt")
        second = collector.collect()
    finally:
        collector.close()

    assert second.mcp_cache.mcp_cached_server_count == 2
    assert second.mcp_cache.mcp_cached_server_names == ["playwright", "sheets"]


def test_deleted_mcp_schema_cache_reports_empty(hermes_home: Path):
    path = hermes_home / "cache" / "mcp_schema_cache.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"playwright": {}}))

    collector = Collector(hermes_home)
    try:
        assert collector.collect().mcp_cache.mcp_cached_server_count == 1
        path.unlink()
        second = collector.collect()
    finally:
        collector.close()

    # A removed cache file is a real state change, not a read fault: the file
    # cache drops the entry rather than serving a deleted file's value forever.
    assert second.mcp_cache.mcp_cached_server_count == 0
    assert "mcp_cache" not in second.health.failed_sources


def test_corrupt_skills_prompt_snapshot_keeps_last_good(hermes_home: Path):
    path = hermes_home / ".skills_prompt_snapshot.json"
    path.write_text(json.dumps({"version": 1, "skills": [{"name": "dev-lint"}]}))

    collector = Collector(hermes_home)
    try:
        assert collector.collect().skills_prompt.prompted_skill_count == 1
        path.write_text("not json")
        second = collector.collect()
    finally:
        collector.close()

    assert second.skills_prompt.prompted_skill_count == 1


def test_corrupt_cron_executions_db_keeps_last_good_execution_history(
    populated_hermes_home: Path,
) -> None:
    db_path = populated_hermes_home / "cron" / "executions.db"
    collector = Collector(populated_hermes_home)
    try:
        first = collector.collect()
        assert "cron_executions" not in first.health.failed_sources
        good_stats = {
            stats.job_id: stats.completed_24h for stats in first.cron_executions.job_stats
        }
        assert good_stats["job-alpha"] == 2
        assert first.cron_executions.open_incident_count == 2

        # Overwrite the header so SQLite rejects the file as "not a database".
        db_path.write_bytes(b"this is not a sqlite database" + b"\x00" * 4096)

        second = collector.collect()
        assert "cron_executions" in second.health.failed_sources
        assert "cron_executions" in second.health.errors
        # Cache preservation: the panel keeps the last-good history, not zeros.
        assert {
            stats.job_id: stats.completed_24h for stats in second.cron_executions.job_stats
        } == good_stats
        assert second.cron_executions.open_incident_count == 2
        assert second.cron_executions.db_present is True
        # A failing executions.db must not take jobs.json/ticker data down with it.
        assert "cron" not in second.health.failed_sources
    finally:
        collector.close()


def test_corrupt_cron_executions_db_recovers_after_the_file_is_restored(
    populated_hermes_home: Path,
) -> None:
    db_path = populated_hermes_home / "cron" / "executions.db"
    original = db_path.read_bytes()
    collector = Collector(populated_hermes_home)
    try:
        collector.collect()
        db_path.write_bytes(b"corrupt")
        assert "cron_executions" in collector.collect().health.failed_sources

        db_path.write_bytes(original)
        recovered = collector.collect()
        assert "cron_executions" not in recovered.health.failed_sources
        assert recovered.cron_executions.recent[0].execution_id == "exec_alpha_running"
    finally:
        collector.close()


def test_deleted_cron_executions_db_reports_absent_without_failing_the_source(
    populated_hermes_home: Path,
) -> None:
    db_path = populated_hermes_home / "cron" / "executions.db"
    collector = Collector(populated_hermes_home)
    try:
        assert collector.collect().cron_executions.db_present is True
        db_path.unlink()

        state = collector.collect()
        # An absent DB is a normal state (agent never ran cron), not a failure.
        assert "cron_executions" not in state.health.failed_sources
        assert state.cron_executions.db_present is False
        assert state.cron_executions.job_stats == []
    finally:
        collector.close()


def test_corrupt_state_db_keeps_last_good_delegations(hermes_home: Path, sample_db: Path):
    """A corrupt state.db must serve the last-good delegation table, not a blank one."""
    c = Collector(hermes_home)
    try:
        good = c.collect().operations
        assert good.delegation_count == 3
        assert good.delegations

        sample_db.write_bytes(b"this is not a sqlite database" * 64)

        degraded = c.collect()
        assert "operations" in degraded.health.failed_sources
        assert degraded.operations.delegation_count == good.delegation_count
        assert degraded.operations.delegations == good.delegations
        assert degraded.operations.state_db_schema_version == good.state_db_schema_version
    finally:
        c.close()


def test_corrupt_state_db_names_model_usage_and_keeps_the_last_good_rows(
    hermes_home: Path, sample_db: Path
):
    """A corrupt state.db must both name `model_usage` and serve its last-good rows."""
    c = Collector(hermes_home)
    try:
        good = c.collect().token_analytics
        assert good.usage_source == "session_model_usage"
        assert [row.model for row in good.model_usage_all] == ["gpt-5.4", "gpt-5.4-mini"]

        sample_db.write_bytes(b"this is not a sqlite database" * 64)
        degraded = c.collect()

        assert "model_usage" in degraded.health.failed_sources
        assert degraded.token_analytics.model_usage_all == good.model_usage_all
        assert degraded.token_analytics.usage_source == good.usage_source
    finally:
        c.close()


def test_corrupt_gateway_state_json_names_the_gateway_source(
    hermes_home: Path, sample_gateway_state: Path
):
    """Falling back to a cached gateway_state.json must not hide the read failure."""
    c = Collector(hermes_home)
    try:
        good = c.collect().gateway
        assert good.code_version == "2026.9.1"

        sample_gateway_state.write_text("{ this is not json")
        degraded = c.collect()

        assert "gateway" in degraded.health.failed_sources
        assert "gateway" in degraded.health.errors
        assert degraded.gateway.code_version == good.code_version
        assert degraded.gateway.code_sha == good.code_sha
        assert {p.name for p in degraded.gateway.platforms} == {p.name for p in good.platforms}
    finally:
        c.close()


def test_corrupt_mcp_schema_cache_names_the_mcp_cache_source(
    hermes_home: Path, sample_mcp_schema_cache: Path
):
    c = Collector(hermes_home)
    try:
        good = c.collect().mcp_cache
        assert good.mcp_cached_server_names == ["playwright", "sheets"]

        sample_mcp_schema_cache.write_text("{ this is not json")
        degraded = c.collect()

        assert "mcp_cache" in degraded.health.failed_sources
        assert degraded.mcp_cache.mcp_cached_server_names == good.mcp_cached_server_names
        assert degraded.mcp_cache.mcp_cached_server_count == good.mcp_cached_server_count
    finally:
        c.close()


def test_corrupt_skills_prompt_snapshot_names_the_skills_prompt_source(
    hermes_home: Path, sample_skills_prompt_snapshot: Path
):
    c = Collector(hermes_home)
    try:
        good = c.collect().skills_prompt
        assert good.prompted_skill_count == 3

        sample_skills_prompt_snapshot.write_text("{ this is not json")
        degraded = c.collect()

        assert "skills_prompt" in degraded.health.failed_sources
        assert degraded.skills_prompt.prompted_skill_count == good.prompted_skill_count
    finally:
        c.close()


def test_repaired_state_db_clears_operations_from_failed_sources(
    hermes_home: Path, sample_db: Path
):
    """The recovery half of the delegations invariant: repair the DB, the source clears."""
    original = sample_db.read_bytes()
    c = Collector(hermes_home)
    try:
        good = c.collect().operations
        assert good.delegation_count == 3

        sample_db.write_bytes(b"this is not a sqlite database" * 64)
        degraded = c.collect()
        assert "operations" in degraded.health.failed_sources

        sample_db.write_bytes(original)
        recovered = c.collect()

        assert "operations" not in recovered.health.failed_sources
        assert "operations" not in recovered.health.errors
        assert recovered.operations.delegation_count == 3
        assert [d.delegation_id for d in recovered.operations.delegations] == [
            d.delegation_id for d in good.delegations
        ]
    finally:
        c.close()


def test_removed_state_db_keeps_last_good_delegations(hermes_home: Path, sample_db: Path):
    c = Collector(hermes_home)
    try:
        good = c.collect().operations
        sample_db.unlink()
        assert c.collect().operations.delegations == good.delegations
    finally:
        c.close()


def test_hygiene_last_good_restores_the_total_with_the_rows(
    forensic_hermes_home: Path, monkeypatch
):
    """A failed hygiene read must restore ``hygiene_total``, not only the rows.

    The state model's contract is that the row list is capped while the total is
    exact; restoring one without the other would render "0 hygiene cooldown(s)"
    above the very rows that are still displayed.
    """
    c = Collector(forensic_hermes_home, pid_exists=lambda pid: pid == 12345)
    try:
        first = c.collect()
        assert first.session_coordination.hygiene_total == 1
        assert len(first.session_coordination.hygiene) == 1

        def boom(*args: object, **kwargs: object) -> object:
            raise sqlite3.OperationalError("simulated hygiene read failure")

        monkeypatch.setattr(sessions_module, "_hygiene_fields", boom)
        monkeypatch.setattr(collector_module, "_hygiene_fields", boom, raising=False)
        second = c.collect()

        assert "gateway_hygiene" in second.health.failed_sources
        assert second.session_coordination.hygiene == first.session_coordination.hygiene
        assert second.session_coordination.hygiene_total == first.session_coordination.hygiene_total
    finally:
        c.close()


# ── Files that vanish or turn into escaping symlinks after a good read ─────


def _sqlite_file(path: Path, build: Callable[[sqlite3.Connection], None]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        build(conn)
        conn.commit()
    finally:
        conn.close()


def _setup_migration(home: Path) -> None:
    (home / "gateway_migration.json").write_text(
        json.dumps({"version": 1, "migrated_at": "2026-09-01T00:00:00Z", "secondaries": []})
    )


def _setup_kanban_current(home: Path) -> None:
    (home / "kanban").mkdir(exist_ok=True)
    (home / "kanban" / "current").write_text("root")


def _setup_response_store(home: Path) -> None:
    _sqlite_file(
        home / "response_store.db", lambda conn: conn.execute("CREATE TABLE conversations (id)")
    )


def _setup_verification(home: Path) -> None:
    _sqlite_file(home / "verification_evidence.db", create_verification_evidence_db_tables)


def _setup_projects(home: Path) -> None:
    _sqlite_file(home / "projects.db", create_projects_db_tables)


def _setup_hosted_rooms(home: Path) -> None:
    build_shared_state_db(home / "shared-state.db")


def _setup_api_runs(home: Path) -> None:
    build_runs_db(home / "runs_idempotency.db")


def _setup_moa(home: Path) -> None:
    (home / "moa-traces").mkdir()
    (home / "moa-traces" / "sess_moa.jsonl").write_text('{"event": "aggregate"}\n')


def _no_setup(home: Path) -> None:
    return None


@dataclass(frozen=True)
class _GuardedFile:
    """One previously-read file, the source it feeds, and the value it owns."""

    source: str
    relpath: str
    setup: Callable[[Path], None]
    value: Callable[[DashboardState], object]


_GUARDED_FILES = [
    _GuardedFile(
        "migration",
        "gateway_migration.json",
        _setup_migration,
        lambda s: s.migration.manifest_present,
    ),
    _GuardedFile(
        "kanban", "kanban.db", _no_setup, lambda s: (s.kanban.db_present, s.kanban.task_count)
    ),
    _GuardedFile(
        "kanban", "kanban/current", _setup_kanban_current, lambda s: s.kanban.current_board
    ),
    _GuardedFile(
        "operations",
        "response_store.db",
        _setup_response_store,
        lambda s: s.operations.response_store_present,
    ),
    _GuardedFile(
        "operations",
        "verification_evidence.db",
        _setup_verification,
        lambda s: s.operations.verification_db_present,
    ),
    _GuardedFile(
        "operations", "projects.db", _setup_projects, lambda s: s.operations.projects_db_present
    ),
    _GuardedFile(
        "hosted_rooms",
        "shared-state.db",
        _setup_hosted_rooms,
        lambda s: s.operations.hosted_rooms.db_present,
    ),
    _GuardedFile(
        "api_runs",
        "runs_idempotency.db",
        _setup_api_runs,
        lambda s: s.operations.api_runs.db_present,
    ),
    _GuardedFile("operations", "moa-traces", _setup_moa, lambda s: s.operations.moa_trace_count),
    _GuardedFile(
        "gateway_routes", "state.db", _no_setup, lambda s: s.session_coordination.route_total
    ),
]


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


@pytest.mark.parametrize("case", _GUARDED_FILES, ids=lambda case: case.relpath)
def test_deleted_file_fails_once_then_its_absence_is_accepted(
    forensic_hermes_home: Path, case: _GuardedFile
) -> None:
    """A vanished file keeps last-good for one pass, then reads as gone.

    The first absent pass may be a mid-rewrite race, so the source fails and
    keeps its prior values; a second consecutive absent pass is a deletion,
    and the source must stop reporting itself failed.
    """
    case.setup(forensic_hermes_home)
    c = Collector(forensic_hermes_home, pid_exists=lambda pid: pid == 12345)
    try:
        first = c.collect()
        assert case.source not in first.health.failed_sources
        good = case.value(first)
        assert good

        _remove(forensic_hermes_home / case.relpath)
        second = c.collect()
        assert case.source in second.health.failed_sources
        assert "disappeared" in second.health.errors[case.source]
        assert case.value(second) == good

        third = c.collect()
        assert case.source not in third.health.failed_sources
        assert case.value(third) != good
    finally:
        c.close()


def test_a_reappearing_file_resets_the_absence_count(hermes_home: Path) -> None:
    """Absence is counted in *consecutive* passes: a file that comes back
    between two absent passes restarts the count instead of being accepted."""
    _setup_response_store(hermes_home)
    db_path = hermes_home / "response_store.db"
    c = Collector(hermes_home)
    try:
        assert c.collect().operations.response_store_present
        db_path.rename(hermes_home / "response_store.db.moved")
        assert "operations" in c.collect().health.failed_sources
        (hermes_home / "response_store.db.moved").rename(db_path)
        assert "operations" not in c.collect().health.failed_sources
        db_path.unlink()
        again = c.collect()
    finally:
        c.close()

    assert "operations" in again.health.failed_sources
    assert again.operations.response_store_present is True


@pytest.mark.parametrize(
    "case",
    [
        *_GUARDED_FILES,
        _GuardedFile("gateway", "gateway_state.json", _no_setup, lambda s: s.gateway.code_version),
        _GuardedFile(
            "gateway_heartbeat",
            "state/gateway.heartbeat",
            _no_setup,
            lambda s: s.gateway.loop_health,
        ),
    ],
    ids=lambda case: case.relpath,
)
def test_file_replaced_by_an_escaping_symlink_keeps_failing_with_last_good(
    forensic_hermes_home: Path, tmp_path: Path, case: _GuardedFile
) -> None:
    """A symlink out of the home is refused on every pass, never accepted."""
    case.setup(forensic_hermes_home)
    c = Collector(forensic_hermes_home, pid_exists=lambda pid: pid == 12345)
    try:
        first = c.collect()
        assert case.source not in first.health.failed_sources
        good = case.value(first)

        target = forensic_hermes_home / case.relpath
        outside = tmp_path / "outside" / target.name
        outside.parent.mkdir()
        target.rename(outside)
        target.symlink_to(outside, target_is_directory=outside.is_dir())
        second = c.collect()
        third = c.collect()
    finally:
        c.close()

    for state in (second, third):
        assert case.source in state.health.failed_sources
        assert case.value(state) == good


@pytest.mark.parametrize(
    ("relpath", "setup", "source", "value", "empty"),
    [
        (
            "kanban/current",
            _setup_kanban_current,
            "kanban",
            lambda s: s.kanban.current_board,
            "",
        ),
        (
            "shared-state.db",
            _setup_hosted_rooms,
            "hosted_rooms",
            lambda s: s.operations.hosted_rooms.db_present,
            False,
        ),
        (
            "runs_idempotency.db",
            _setup_api_runs,
            "api_runs",
            lambda s: s.operations.api_runs.db_present,
            False,
        ),
    ],
    ids=["kanban-current", "shared-state", "runs-idempotency"],
)
def test_escaping_symlink_without_a_good_read_reads_as_absent(
    hermes_home: Path,
    tmp_path: Path,
    relpath: str,
    setup: Callable[[Path], None],
    source: str,
    value: Callable[[DashboardState], object],
    empty: object,
) -> None:
    """Never having read the file, a symlinked one is simply not there."""
    setup(hermes_home)
    target = hermes_home / relpath
    outside = tmp_path / "outside" / target.name
    outside.parent.mkdir()
    target.rename(outside)
    target.symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert source not in state.health.failed_sources
    assert value(state) == empty


def test_unreadable_current_board_after_a_good_read_keeps_last_good(hermes_home: Path) -> None:
    _setup_kanban_current(hermes_home)
    current = hermes_home / "kanban" / "current"
    c = Collector(hermes_home)
    try:
        assert c.collect().kanban.current_board == "root"
        current.unlink()
        current.mkdir()  # opening a directory raises IsADirectoryError, not absence
        second = c.collect()
        third = c.collect()
    finally:
        c.close()

    for state in (second, third):
        assert "kanban" in state.health.failed_sources
        assert state.kanban.current_board == "root"


def test_symlinked_terminal_breadcrumb_is_never_read(hermes_home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-breadcrumb"
    outside.write_text(json.dumps({"session_id": "SENTINEL_OUTSIDE", "ts": time.time()}))
    directory = hermes_home / "terminal-sessions"
    directory.mkdir()
    (directory / "tty-evil").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.terminal_sessions.count == 0
    assert "SENTINEL_OUTSIDE" not in state.model_dump_json()


def test_symlinked_board_kanban_db_is_never_read(sample_kanban_db: Path, tmp_path: Path) -> None:
    home = sample_kanban_db.parent
    outside = tmp_path / "outside-kanban.db"
    shutil.copy(sample_kanban_db, outside)
    board = home / "kanban" / "boards" / "evil"
    board.mkdir(parents=True)
    (board / "kanban.db").symlink_to(outside)

    c = Collector(home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "evil" not in {summary.slug for summary in state.kanban.boards}


def test_symlinked_hook_manifest_is_never_read(hermes_home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "HOOK.yaml"
    outside.write_text("name: SENTINEL_OUTSIDE\nevents: [agent:start]\n")
    hook = hermes_home / "hooks" / "evil"
    hook.mkdir(parents=True)
    (hook / "handler.py").write_text("def handle(event): pass\n")
    (hook / "HOOK.yaml").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.skills_memory.hooks == []
    assert "SENTINEL_OUTSIDE" not in state.model_dump_json()


def test_symlinked_plugin_init_is_never_read(hermes_home: Path, tmp_path: Path) -> None:
    """An ``__init__.py`` symlinked out of the home must not steer kind detection."""
    outside = tmp_path / "__init__.py"
    outside.write_text("class SentinelMemoryProvider(MemoryProvider):\n    pass\n")
    plugin = hermes_home / "plugins" / "evil"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: evil\nversion: 1.0.0\n")
    (plugin / "__init__.py").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    (evil,) = state.skills_memory.plugins
    assert evil.kind == PLUGIN_KIND_STANDALONE
