"""Incremental log health counters: MCP restarts, gateway error signatures,
workspace crash loops."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import hermesd.collect.logs as logs_module
import hermesd.collector as collector_module
from hermesd.collector import Collector
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str

_NOW = 1_790_000_000.0


class _Clock:
    def __init__(self, now: float = _NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _stamp(age_seconds: float, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """A local-time stamp ``age_seconds`` before the fixed clock, as upstream writes."""
    return time.strftime(fmt, time.localtime(_NOW - age_seconds))


def _banner(age_seconds: float, server: str) -> str:
    # tools/mcp_tool_config.py:67
    return f"\n===== [{_stamp(age_seconds)}] starting MCP server '{server}' =====\n"


def _gw(age_seconds: float, text: str) -> str:
    # hermes_cli/stderr_timestamp.py:21-30 — logging's asctime shape.
    return f"{_stamp(age_seconds)},123 {text}\n"


def _health(state, stream: str):
    return next(entry for entry in state.logs.health if entry.stream == stream)


def _counter(health, key: str):
    return next(counter for counter in health.counters if counter.key == key)


def _collector(home: Path, clock: _Clock | None = None) -> Collector:
    return Collector(home, clock=clock or _Clock())


def _collect(home: Path, clock: _Clock | None = None):
    collector = _collector(home, clock)
    try:
        return collector.collect()
    finally:
        collector.close()


def test_mcp_server_starts_and_supervisor_errors_are_rated(hermes_home: Path):
    log = hermes_home / "logs" / "mcp-stderr.log"
    log.write_text(
        _banner(90_000, "codegraph")  # older than 24h
        + _banner(7200, "codegraph")
        + "[CodeGraph] v1.6.0 is available\n"
        + _banner(600, "codegraph")
        + "usage: mcp_stdio_watchdog.py [-h] --ppid PPID ...\n"
        + "mcp_stdio_watchdog.py: error: unrecognized arguments: --create-time\n"
        + _banner(60, "github")
    )
    health = _health(_collect(hermes_home), "mcp.stderr")
    starts = _counter(health, "mcp_server_starts")
    assert (starts.last_1h, starts.last_24h, starts.undated) == (2, 3, 0)
    assert starts.last_seen_age_seconds == pytest.approx(60, abs=1)
    errors = _counter(health, "mcp_supervisor_arg_errors")
    # Dated by the banner above it: the error line carries no stamp of its own.
    assert (errors.last_1h, errors.last_24h) == (1, 1)
    assert [(top.signature, top.last_24h) for top in health.top] == [
        ("codegraph", 2),
        ("github", 1),
    ]


def test_gateway_error_signatures_are_normalized_and_counted(hermes_home: Path):
    log = hermes_home / "logs" / "gateway.error.log"
    log.write_text(
        _gw(
            3000, "ERROR gateway.run: Platform 'telegram' is registered but adapter creation failed"
        )
        + _gw(
            2000, "ERROR gateway.run: Platform 'telegram' is registered but adapter creation failed"
        )
        + _gw(1500, "WARNING gateway.run: No adapter available for telegram")
        + _gw(1000, "ERROR gateway.delivery: retry 17 failed after 2500 ms")
        + _gw(900, "ERROR gateway.delivery: retry 18 failed after 3100 ms")
        + _gw(800, "Traceback (most recent call last):")
        + '  File "/x/hermes_constants.py", line 16, in <module>\n'
        + "ModuleNotFoundError: No module named 'hermes_platform'\n"
        + _gw(100, "CRITICAL gateway.auth: token sk-" + "a" * 40 + " rejected")
    )
    health = _health(_collect(hermes_home), "gateway.error")
    errors = _counter(health, "gateway_errors")
    assert (errors.last_1h, errors.last_24h) == (6, 6)
    tops = {top.signature: top.last_24h for top in health.top}
    assert tops["gateway.run: Platform 'telegram' is registered but adapter creation failed"] == 2
    assert tops["gateway.delivery: retry N failed after N ms"] == 2
    assert tops["ModuleNotFoundError: No module named 'hermes_platform'"] == 1
    assert not any("No adapter available" in signature for signature in tops)
    assert not any("a" * 40 in signature for signature in tops)
    # Most frequent first.
    assert health.top[0].last_24h == 2


def test_workspace_crash_loop_backfill_is_undated_then_live_lines_are_dated(hermes_home: Path):
    log = hermes_home / "logs" / "workspace.log"
    log.write_text(
        "[vite] connected.\n"
        "[ELIFECYCLE] Command failed with exit code 143.\n"
        " ELIFECYCLE  Command failed with exit code 1.\n"
    )
    clock = _Clock()
    collector = _collector(hermes_home, clock)
    try:
        first = _counter(_health(collector.collect(), "workspace"), "workspace_crashes")
        assert (first.last_1h, first.last_24h, first.undated) == (0, 0, 2)
        clock.now += 30
        with log.open("a") as handle:
            handle.write("[vite] connected.\n ELIFECYCLE  Command failed with exit code 1.\n")
        second = _counter(_health(collector.collect(), "workspace"), "workspace_crashes")
        assert (second.last_1h, second.last_24h, second.undated) == (1, 1, 2)
        clock.now += 7200
        third = _counter(_health(collector.collect(), "workspace"), "workspace_crashes")
        assert (third.last_1h, third.last_24h) == (0, 1)
    finally:
        collector.close()


def test_only_appended_bytes_are_read(hermes_home: Path):
    log = hermes_home / "logs" / "mcp-stderr.log"
    log.write_text(_banner(600, "codegraph"))
    initial = log.stat().st_size
    clock = _Clock()
    collector = _collector(hermes_home, clock)
    try:
        first = _health(collector.collect(), "mcp.stderr")
        assert first.scanned_bytes == initial
        again = _health(collector.collect(), "mcp.stderr")
        assert again.scanned_bytes == initial  # nothing new, nothing read
        appended = _banner(10, "codegraph")
        with log.open("a") as handle:
            handle.write(appended)
        third = _health(collector.collect(), "mcp.stderr")
        assert third.scanned_bytes == initial + len(appended.encode())
        assert _counter(third, "mcp_server_starts").last_24h == 2
    finally:
        collector.close()


def test_partial_trailing_line_waits_for_its_newline(hermes_home: Path):
    log = hermes_home / "logs" / "workspace.log"
    log.write_text("start\n")
    clock = _Clock()
    collector = _collector(hermes_home, clock)
    try:
        collector.collect()
        with log.open("a") as handle:
            handle.write(" ELIFECYCLE  Command fai")
        partial = _counter(_health(collector.collect(), "workspace"), "workspace_crashes")
        assert partial.last_24h == 0
        with log.open("a") as handle:
            handle.write("led with exit code 1.\n")
        done = _counter(_health(collector.collect(), "workspace"), "workspace_crashes")
        assert done.last_24h == 1
    finally:
        collector.close()


def test_truncation_restarts_the_scan(hermes_home: Path):
    log = hermes_home / "logs" / "mcp-stderr.log"
    log.write_text(_banner(600, "codegraph") * 3)
    collector = _collector(hermes_home)
    try:
        before = _health(collector.collect(), "mcp.stderr")
        assert _counter(before, "mcp_server_starts").last_24h == 3
        log.write_text(_banner(5, "github"))  # rotated/truncated to a smaller file
        after = _health(collector.collect(), "mcp.stderr")
        # Past events stay counted; the new file is scanned from its start.
        assert _counter(after, "mcp_server_starts").last_24h == 4
        assert after.backlog_bytes == 0
    finally:
        collector.close()


def test_replaced_file_is_rescanned_from_the_start(hermes_home: Path):
    log = hermes_home / "logs" / "mcp-stderr.log"
    log.write_text(_banner(600, "codegraph"))
    collector = _collector(hermes_home)
    try:
        collector.collect()
        replacement = hermes_home / "logs" / "mcp-stderr.log.new"
        replacement.write_text(_banner(900, "alpha") + _banner(5, "beta") + _banner(4, "gamma"))
        replacement.replace(log)
        after = _health(collector.collect(), "mcp.stderr")
        assert _counter(after, "mcp_server_starts").last_24h == 4
    finally:
        collector.close()


def test_backfill_is_bounded_and_catch_up_is_capped_per_refresh(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(logs_module, "_LOG_HEALTH_BACKFILL_BYTES", 400)
    monkeypatch.setattr(logs_module, "_LOG_HEALTH_MAX_BYTES_PER_REFRESH", 150)
    log = hermes_home / "logs" / "mcp-stderr.log"
    block = "".join(_banner(600 - index, "codegraph") for index in range(20))
    log.write_text(block)
    size = log.stat().st_size
    collector = _collector(hermes_home)
    try:
        first = _health(collector.collect(), "mcp.stderr")
        assert first.backlog_bytes > 0
        assert first.scanned_bytes <= 150
        state = first
        for _ in range(10):
            state = _health(collector.collect(), "mcp.stderr")
        assert state.backlog_bytes == 0
        assert state.scanned_bytes <= 400
        starts = _counter(state, "mcp_server_starts").last_24h
        # Only the backfill window's banners (not all 20) were counted.
        banner_bytes = len(_banner(600, "codegraph").encode())
        assert 0 < starts <= 400 // banner_bytes + 1
        assert size > 400
    finally:
        collector.close()


def test_absent_logs_report_no_health(hermes_home: Path):
    assert _collect(hermes_home).logs.health == []


def test_symlinked_log_is_not_scanned(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside.log"
    outside.write_text(_banner(10, "evil"))
    (hermes_home / "logs" / "mcp-stderr.log").symlink_to(outside)
    assert _collect(hermes_home).logs.health == []


def test_scan_failure_keeps_last_good(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    (hermes_home / "logs" / "mcp-stderr.log").write_text(_banner(10, "codegraph"))
    collector = _collector(hermes_home)
    try:
        before = collector.collect()

        def boom(*args: object, **kwargs: object):
            raise OSError("read failed")

        monkeypatch.setattr(collector_module.IncrementalLogScanner, "scan", boom)
        after = collector.collect()
    finally:
        collector.close()
    assert "log_health" in after.health.failed_sources
    assert after.logs.health == before.logs.health
    assert after.logs.streams  # the tails themselves stay fresh


def test_health_renders_in_logs_detail_and_operations(hermes_home: Path):
    (hermes_home / "logs" / "mcp-stderr.log").write_text(
        _banner(600, "code[bold]graph") + _banner(60, "code[bold]graph")
    )
    (hermes_home / "logs" / "gateway.error.log").write_text(
        _gw(100, "ERROR gateway.run: adapter [red]failed")
    )
    state = _collect(hermes_home)
    logs_detail = render_to_str(
        render_panel(8, state, Theme(), detail=True, log_sub_view="mcp.stderr"),
        width=160,
        no_color=True,
    )
    assert "Health: MCP server starts 2/1h · 2/24h" in logs_detail
    ops_detail = render_to_str(
        render_panel(12, state, Theme(), detail=True), width=180, no_color=True
    )
    assert "Log Health" in ops_detail
    assert "code[bold]graph" in ops_detail
    assert "gateway.run: adapter [red]failed" in ops_detail
    ops_compact = render_to_str(render_panel(12, state, Theme()), width=120, no_color=True)
    assert "Log health: MCP starts 2/1h · gateway errors 1/1h" in ops_compact
