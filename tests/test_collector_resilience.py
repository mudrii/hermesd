"""Resilience tests for the collector against corrupt, hostile or unreadable ~/.hermes data.

Every test here asserts the cache-preservation invariant: after a fault is
injected, the affected source falls back to the last good value instead of
blanking the dashboard, and the source is named in ``health.failed_sources``
whenever the collector could tell that the read failed.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from hermesd.collector import Collector
from hermesd.models import ConfigSummary

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
    collector = Collector(populated_hermes_home)
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permission bits")
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permission bits")
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permission bits")
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
            # cache serves the last-good mapping, so the source still passes.
            state = collector.collect()
            assert state.config.model == "gpt-5.4"
            assert "config" not in state.health.failed_sources

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
