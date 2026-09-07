"""Collection of log streams: discovery, tailing, parsing, and redaction."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermesd.collector import (
    _LOG_LINE_PATTERN,
    Collector,
    _latest_log_mtime,
)
from tests.conftest import (
    _skip_if_root,
    _unreadable,
)


def test_collect_logs_respects_log_tail_bytes(hermes_home: Path):
    agent_log = hermes_home / "logs" / "agent.log"
    lines = [f"2026-04-09 15:42:{idx:02d},000 - hermes - INFO - line {idx}\n" for idx in range(30)]
    agent_log.write_text("".join(lines))

    c = Collector(hermes_home, log_tail_bytes=256)
    state = c.collect()

    messages = [line.message for line in state.logs.agent_lines]
    assert any("line 29" in message for message in messages)
    assert all("line 00" not in message for message in messages)
    c.close()


def test_collect_logs_empty(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.logs.agent_lines == []
    assert state.logs.gateway_lines == []
    assert state.logs.error_lines == []
    c.close()


def test_collect_logs_parses_format(hermes_home: Path, sample_logs: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert len(state.logs.agent_lines) == 3
    line = state.logs.agent_lines[0]
    assert line.level == "INFO"
    assert line.component == "hermes"
    assert "web_search" in line.message
    c.close()


def test_collect_logs_discovers_shared_named_streams(hermes_home: Path):
    shared_logs = {
        "desktop.log": "desktop ready",
        "dashboard.log": "dashboard ready",
        "gui.log": "gui ready",
        "update.log": "update ready",
        "gateway.error.log": "gateway error ready",
        "tui_gateway_crash.log": "crash ready",
    }
    for filename, message in shared_logs.items():
        (hermes_home / "logs" / filename).write_text(
            f"2026-04-09 15:41:58,123 - hermes - INFO - {message}\n"
        )

    c = Collector(hermes_home)
    state = c.collect()

    streams = {stream.name: stream for stream in state.logs.streams}
    assert set(streams) == {
        "desktop",
        "dashboard",
        "gui",
        "update",
        "gateway.error",
        "tui crash",
    }
    assert streams["desktop"].lines[0].message == "desktop ready"
    assert streams["tui crash"].lines[0].message == "crash ready"
    c.close()


def test_collect_logs_discovers_audit_mcp_and_workspace_streams(hermes_home: Path):
    extra_logs = {
        "audit.log": ("audit", "audit entry"),
        "mcp-stderr.log": ("mcp.stderr", "mcp stderr entry"),
        "workspace.log": ("workspace", "workspace entry"),
        "workspace.error.log": ("workspace.error", "workspace error entry"),
    }
    for filename, (_, message) in extra_logs.items():
        (hermes_home / "logs" / filename).write_text(
            f"2026-04-09 15:41:58,123 - hermes - INFO - {message}\n"
        )

    c = Collector(hermes_home)
    state = c.collect()

    streams = {stream.name: stream for stream in state.logs.streams}
    for _, (stream_name, message) in extra_logs.items():
        assert stream_name in streams
        assert streams[stream_name].lines[0].message == message
    c.close()


def test_profiled_collector_uses_shared_aux_log_streams(profiled_hermes_home: Path):
    (profiled_hermes_home / "logs" / "desktop.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - shared desktop log\n"
    )

    c = Collector(profiled_hermes_home, profile_name="coding")
    state = c.collect()

    streams = {stream.name: stream for stream in state.logs.streams}
    assert streams["agent"].lines[0].message == "profile agent log"
    assert streams["desktop"].lines[0].message == "shared desktop log"
    c.close()


def test_collect_logs_non_standard_line(hermes_home: Path):
    log = hermes_home / "logs" / "agent.log"
    log.write_text("plain text without timestamp\n")
    c = Collector(hermes_home)
    state = c.collect()
    assert len(state.logs.agent_lines) == 1
    assert state.logs.agent_lines[0].message == "plain text without timestamp"
    assert state.logs.agent_lines[0].level == ""
    c.close()


def test_collect_logs_preserves_cache_when_file_disappears(hermes_home: Path):
    log = hermes_home / "logs" / "agent.log"
    log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - first line\n")
    c = Collector(hermes_home)
    first = c.collect()
    log.unlink()
    second = c.collect()
    assert second.logs.agent_lines == first.logs.agent_lines
    c.close()


def test_collect_logs_preserves_cache_when_file_rotates_to_empty(hermes_home: Path):
    log = hermes_home / "logs" / "agent.log"
    log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - first line\n")
    c = Collector(hermes_home)
    first = c.collect()
    log.write_text("")
    second = c.collect()
    assert second.logs.agent_lines == first.logs.agent_lines
    c.close()


def test_collect_logs_redacts_secret_material(hermes_home: Path):
    log = hermes_home / "logs" / "agent.log"
    log.write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - "
        "bearer sk-secret token=abc123 https://user:pass@example.com/mcp\n"
    )

    c = Collector(hermes_home)
    state = c.collect()

    message = state.logs.agent_lines[0].message
    assert "sk-secret" not in message
    assert "abc123" not in message
    assert "user:pass" not in message
    assert "bearer [REDACTED]" in message
    assert "token=[REDACTED]" in message
    assert "https://[REDACTED]@example.com/mcp" in message
    c.close()


def test_collect_logs_ignores_symlinked_log_files_outside_home(hermes_home: Path, tmp_path: Path):
    outside_log = tmp_path / "outside-secret.log"
    outside_log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - outside secret\n")
    (hermes_home / "logs" / "agent.log").symlink_to(outside_log)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.logs.agent_lines == []
    assert all(
        "outside secret" not in line.message
        for stream in state.logs.streams
        for line in stream.lines
    )
    c.close()


def test_collect_logs_ignores_symlinked_log_directory_outside_home(
    hermes_home: Path, tmp_path: Path
):
    outside_logs = tmp_path / "outside-logs"
    outside_logs.mkdir()
    (outside_logs / "agent.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - outside secret\n"
    )
    logs_dir = hermes_home / "logs"
    logs_dir.rmdir()
    logs_dir.symlink_to(outside_logs, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.logs.agent_lines == []
    assert all(
        "outside secret" not in line.message
        for stream in state.logs.streams
        for line in stream.lines
    )
    c.close()


def test_collect_logs_and_cron_allow_symlinked_hermes_home(hermes_home: Path, tmp_path: Path):
    (hermes_home / "logs" / "agent.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - in-home log\n"
    )
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "Job 1"}]})
    )
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    (cron_output_dir / "latest.md").write_text("in-home cron output\n")
    linked_home = tmp_path / "linked-hermes"
    linked_home.symlink_to(hermes_home, target_is_directory=True)

    c = Collector(linked_home)
    state = c.collect()

    assert state.logs.agent_lines[0].message == "in-home log"
    assert state.logs.cron_lines[0].message == "in-home cron output"
    assert state.cron.jobs[0].latest_output_excerpt == "in-home cron output"
    c.close()


def test_tail_log_stream_skips_reread_when_mtime_and_size_unchanged(hermes_home: Path):
    log = hermes_home / "logs" / "agent.log"
    log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - original line\n")
    c = Collector(hermes_home)
    first = c.collect()
    assert [line.message for line in first.logs.agent_lines] == ["original line"]

    # Rewrite with identical size and restore the original mtime: an unchanged
    # mtime+size signature must short-circuit the re-read and return cached lines.
    stat = log.stat()
    replacement = "2026-04-09 15:41:58,123 - hermes - INFO - replaced line\n"
    assert len(replacement) == stat.st_size
    log.write_text(replacement)
    os.utime(log, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    second = c.collect()
    assert [line.message for line in second.logs.agent_lines] == ["original line"]

    # A size change invalidates the cache and the new content is read.
    log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - a much longer brand new line\n")
    third = c.collect()
    assert [line.message for line in third.logs.agent_lines] == ["a much longer brand new line"]
    c.close()


@_skip_if_root
def test_log_stream_oserror_preserves_last_good_lines(hermes_home: Path):
    agent_log = hermes_home / "logs" / "agent.log"
    agent_log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - Tool call: web_search\n")

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.logs.agent_lines, "expected agent log lines on first read"
        good = [line.message for line in first.logs.agent_lines]

        # Make the file unreadable but keep it present and bump its mtime so the
        # stream cache is invalidated and the reader is forced to re-read.
        os.chmod(agent_log, 0o000)
        os.utime(agent_log, None)
        if not _unreadable(agent_log):
            pytest.skip("filesystem allowed read despite chmod 000")

        second = c.collect()
        # Cache-preservation: the unreadable file falls back to last-good lines
        # rather than blanking the stream.
        assert [line.message for line in second.logs.agent_lines] == good
    finally:
        os.chmod(agent_log, 0o644)
        c.close()


def test_log_stream_open_error_preserves_last_good_lines(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    agent_log = hermes_home / "logs" / "agent.log"
    agent_log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - Tool call: web_search\n")

    c = Collector(hermes_home)
    try:
        first = c.collect()
        good = [line.message for line in first.logs.agent_lines]
        assert good == ["Tool call: web_search"]

        real_open = Path.open

        def fail_target_open(self: Path, *args, **kwargs):
            if self == agent_log:
                raise OSError("simulated log read failure")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_target_open)
        os.utime(agent_log, None)

        second = c.collect()
        assert [line.message for line in second.logs.agent_lines] == good
    finally:
        c.close()


def test_latest_log_mtime_skips_nonfiles_and_empty(tmp_path: Path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "subdir").mkdir()  # non-file: skipped
    good = logs / "a.log"
    good.write_text("hi")
    assert _latest_log_mtime(logs) == good.stat().st_mtime

    # Empty dir (only a subdir) -> no file mtimes -> None.
    empty = tmp_path / "emptylogs"
    empty.mkdir()
    (empty / "nested").mkdir()
    assert _latest_log_mtime(empty) is None


def test_log_line_pattern_matches_pathological_line_quickly():
    line = "2024-01-01 00:00:00 - " + " " * 20000

    start = time.perf_counter()
    _LOG_LINE_PATTERN.match(line)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "2026-04-09 15:41:58,123 - hermes - INFO - Tool call: web_search",
            ("2026-04-09 15:41:58", "hermes", "INFO", "Tool call: web_search"),
        ),
        (
            "2026-04-09 15:40:00,000 - gateway - INFO - Telegram connected",
            ("2026-04-09 15:40:00", "gateway", "INFO", "Telegram connected"),
        ),
        (
            "2026-04-09 14:00:00,000 - hermes - WARNING - High context usage",
            ("2026-04-09 14:00:00", "hermes", "WARNING", "High context usage"),
        ),
        (
            "2026-04-09 14:00:00 - hermes - ERROR - failed - retrying",
            ("2026-04-09 14:00:00", "hermes", "ERROR", "failed - retrying"),
        ),
    ],
)
def test_log_line_pattern_parses_normal_lines(line: str, expected: tuple[str, str, str, str]):
    match = _LOG_LINE_PATTERN.match(line)

    assert match is not None
    assert (match.group(1), match.group(2).strip(), match.group(3), match.group(4)) == expected


def test_log_lines_are_truncated_before_parsing(hermes_home: Path):
    (hermes_home / "logs" / "agent.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - " + "x" * 40000 + "\n"
    )

    c = Collector(hermes_home, log_tail_bytes=1_000_000)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.logs.agent_lines
    assert len(state.logs.agent_lines[0].message) <= 4096
