"""TUI input thread: raw key reads, escape-sequence reassembly, and failure backoff."""

from __future__ import annotations

import os
import select
from pathlib import Path

from hermesd.app import (
    _MAX_CONSECUTIVE_INPUT_FAILURES,
    DashboardApp,
)


def test_input_loop_survives_transient_read_error(
    populated_hermes_home: Path, fake_terminal, monkeypatch
):
    """A one-time OSError from os.read must not kill the app; the loop retries."""
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._running.set()
    read_calls = 0

    def flaky_read(fd: int, size: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            raise OSError("transient glitch")
        return b"q"

    monkeypatch.setattr(os, "read", flaky_read)

    app._input_loop()

    # The app kept running after the transient error and only quit on "q".
    assert read_calls == 2
    assert app._running.is_set() is False
    assert app._input_error == "input error: transient glitch"
    assert fake_terminal == {"fd": 123, "settings": ["old-settings"]}
    app.close()


def test_input_loop_quits_after_max_consecutive_failures(
    populated_hermes_home: Path, fake_terminal, monkeypatch
):
    """Fail-safe: persistent input errors eventually stop the app."""
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._running.set()
    read_calls = 0

    def fail_read(fd: int, size: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        raise OSError("stdin failed")

    monkeypatch.setattr(os, "read", fail_read)

    app._input_loop()

    assert read_calls == _MAX_CONSECUTIVE_INPUT_FAILURES
    assert app._running.is_set() is False
    assert app._input_error == "input error: stdin failed"
    assert fake_terminal == {"fd": 123, "settings": ["old-settings"]}
    app.close()


def test_input_loop_reassembles_escape_sequence_split_across_reads(
    populated_hermes_home: Path, fake_terminal, monkeypatch
):
    """A CSI split across the 64-byte bulk-read boundary is one arrow key."""
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._running.set()
    reads = iter([b"\x1b[", b"A"])
    monkeypatch.setattr(os, "read", lambda fd, size: next(reads, b"q"))
    keys_seen: list[str] = []
    real_handle_key = app.handle_key

    def spy_handle_key(key: str):
        keys_seen.append(key)
        return real_handle_key(key)

    monkeypatch.setattr(app, "handle_key", spy_handle_key)

    app._input_loop()

    assert "\x1b[A" in keys_seen
    assert "A" not in keys_seen
    assert "\x1b[" not in keys_seen
    app.close()


def test_input_loop_reassembles_escape_sequence_split_after_lone_escape(
    populated_hermes_home: Path, fake_terminal, monkeypatch
):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._running.set()
    reads = iter([b"\x1b", b"[A", b"q"])
    monkeypatch.setattr(os, "read", lambda fd, size: next(reads))
    keys_seen: list[str] = []
    real_handle_key = app.handle_key

    def spy_handle_key(key: str):
        keys_seen.append(key)
        return real_handle_key(key)

    monkeypatch.setattr(app, "handle_key", spy_handle_key)

    app._input_loop()

    assert "\x1b[A" in keys_seen
    assert "\x1b" not in keys_seen
    assert "[" not in keys_seen
    assert "A" not in keys_seen
    app.close()


def test_input_loop_flushes_lone_escape_after_continuation_timeout(
    populated_hermes_home: Path, fake_terminal, monkeypatch
):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._running.set()
    readiness = iter([([123], [], []), ([], [], []), ([123], [], [])])
    reads = iter([b"\x1b", b"q"])
    monkeypatch.setattr(select, "select", lambda *args: next(readiness))
    monkeypatch.setattr(os, "read", lambda fd, size: next(reads))
    keys_seen: list[str] = []
    real_handle_key = app.handle_key

    def spy_handle_key(key: str):
        keys_seen.append(key)
        return real_handle_key(key)

    monkeypatch.setattr(app, "handle_key", spy_handle_key)

    app._input_loop()

    assert keys_seen == ["\x1b", "q"]
    app.close()


def test_handle_input_data_buffers_partial_escape_sequence(
    populated_hermes_home: Path, monkeypatch
):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    keys_seen: list[str] = []

    def spy_handle_key(key: str):
        keys_seen.append(key)

    monkeypatch.setattr(app, "handle_key", spy_handle_key)

    action, pending = app._handle_input_data(b"\x1b[")
    assert action is None
    assert pending == b"\x1b["
    assert keys_seen == []

    action, pending = app._handle_input_data(b"A", pending)
    assert action is None
    assert pending == b""
    assert keys_seen == ["\x1b[A"]
    app.close()


def test_handle_input_data_flushes_remainder_on_quit(populated_hermes_home: Path, monkeypatch):
    """A quit action discards any buffered partial sequence."""
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    action, pending = app._handle_input_data(b"q\x1b[")
    assert action == "quit"
    assert pending == b""
    app.close()
