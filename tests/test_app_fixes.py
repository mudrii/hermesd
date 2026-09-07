"""Regression tests for audit fixes in hermesd/app.py and hermesd/__main__.py.

Covers:
- input thread survives transient OSError/termios errors (fail-safe after N)
- escape sequences split across the 64-byte bulk-read boundary
- _build_footer reads log_sub_view under _view_lock
- _capture_layout_text mutates _view under _view_lock
- snapshot mode: missing parent dirs for --snapshot-file, SIGINT handling
"""

from __future__ import annotations

import os
import select
import signal
import termios
import threading
import tty
from pathlib import Path

import pytest

from hermesd.__main__ import main
from hermesd.app import (
    _LOG_PANEL_NUM,
    _MAX_CONSECUTIVE_INPUT_FAILURES,
    DashboardApp,
    ViewState,
)


class FakeStdin:
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 123


@pytest.fixture
def fake_terminal(monkeypatch):
    restored: dict[str, object] = {}
    monkeypatch.setattr("sys.stdin", FakeStdin())
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["old-settings"])
    monkeypatch.setattr(tty, "setcbreak", lambda fd: None)
    monkeypatch.setattr(select, "select", lambda read, write, err, timeout: ([123], [], []))
    monkeypatch.setattr(
        termios,
        "tcsetattr",
        lambda fd, when, settings: restored.update(fd=fd, settings=settings),
    )
    return restored


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


def test_build_footer_reads_log_sub_view_under_view_lock(populated_hermes_home: Path, monkeypatch):
    """The fallback view read in _build_footer must hold _view_lock."""

    class TrackingLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self.held = False

        def __enter__(self):
            self._lock.acquire()
            self.held = True
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.held = False
            self._lock.release()

    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    lock = TrackingLock()
    app._view_lock = lock
    observed: list[bool] = []
    checking = [False]

    class LockCheckingView(ViewState):
        def __getattribute__(self, name: str):
            if name == "log_sub_view" and checking[0]:
                observed.append(lock.held)
            return super().__getattribute__(name)

    app._view = LockCheckingView()
    app._view.mode = "detail"
    app._view.detail_panel = _LOG_PANEL_NUM
    checking[0] = True

    app._build_footer(app._collector.collect())

    assert observed
    assert all(observed)
    app.close()


def test_build_footer_uses_passed_log_sub_view(populated_hermes_home: Path, monkeypatch):
    """log_sub_view is passed in as a parameter like the other view fields."""
    import hermesd.app as app_module

    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    captured: dict[str, str] = {}
    real_max_scroll = app_module._detail_max_scroll_offset

    def spy_max_scroll(panel_num, state, log_sub_view, filter_query):
        captured["log_sub_view"] = log_sub_view
        return real_max_scroll(panel_num, state, log_sub_view, filter_query)

    monkeypatch.setattr(app_module, "_detail_max_scroll_offset", spy_max_scroll)
    app._view.log_sub_view = "agent"

    app._build_footer(
        app._collector.collect(),
        view_mode="detail",
        detail_panel=_LOG_PANEL_NUM,
        filter_query="",
        filter_edit_mode=False,
        session_sort="recent",
        log_sub_view="errors",
    )

    assert captured["log_sub_view"] == "errors"
    app.close()


def test_capture_layout_text_enters_detail_under_view_lock(
    populated_hermes_home: Path, monkeypatch
):
    """enter_detail during snapshot capture must be wrapped in _view_lock."""

    class TrackingLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self.held = False

        def __enter__(self):
            self._lock.acquire()
            self.held = True
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.held = False
            self._lock.release()

    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    lock = TrackingLock()
    app._view_lock = lock
    held_during_enter: list[bool] = []
    real_enter_detail = app._view.enter_detail

    def spy_enter_detail(panel_num: int) -> None:
        held_during_enter.append(lock.held)
        real_enter_detail(panel_num)

    monkeypatch.setattr(app._view, "enter_detail", spy_enter_detail)

    app._capture_layout_text(panel_num=2)

    assert held_during_enter == [True]
    app.close()


def test_main_snapshot_file_creates_missing_parent_dirs(
    populated_hermes_home: Path, tmp_path: Path
):
    output_path = tmp_path / "nested" / "deeper" / "snapshot.txt"
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-file",
            str(output_path),
            "--no-color",
        ]
    )
    text = output_path.read_text()
    assert "Gateway & Platforms" in text


def test_main_snapshot_sigint_during_collect_exits_cleanly(
    populated_hermes_home: Path, monkeypatch, capsys
):
    """Ctrl+C during a slow snapshot collect must not dump a traceback."""
    closed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            os.kill(os.getpid(), signal.SIGINT)
            return "snapshot"

        def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)
    previous_handler = signal.getsignal(signal.SIGINT)

    with pytest.raises(SystemExit) as excinfo:
        main(["--hermes-home", str(populated_hermes_home), "--snapshot", "--no-color"])

    assert excinfo.value.code == 130  # 128 + SIGINT, conventional shell exit code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "Interrupted" in captured.err
    assert closed is True
    assert signal.getsignal(signal.SIGINT) == previous_handler


def test_main_snapshot_sigterm_during_collect_exits_cleanly(
    populated_hermes_home: Path, monkeypatch, capsys
):
    closed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_json(self, panel_num=None):
            os.kill(os.getpid(), signal.SIGTERM)
            return "{}"

        def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--hermes-home",
                str(populated_hermes_home),
                "--snapshot-format",
                "json",
                "--no-color",
            ]
        )

    assert excinfo.value.code == 143  # 128 + SIGTERM
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "Interrupted" in captured.err
    assert closed is True


def test_main_snapshot_second_signal_forces_default_disposition(
    populated_hermes_home: Path, monkeypatch
):
    """A second Ctrl+C/SIGTERM must be able to kill a wedged collect: the
    first signal is caught, but immediately re-arms the default disposition."""
    dfl_observed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            nonlocal dfl_observed
            os.kill(os.getpid(), signal.SIGINT)
            dfl_observed = signal.getsignal(signal.SIGINT) == signal.SIG_DFL
            return "snapshot"

        def close(self):
            pass

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    with pytest.raises(SystemExit):
        main(["--hermes-home", str(populated_hermes_home), "--snapshot", "--no-color"])

    assert dfl_observed is True


def test_main_rechecks_snapshot_path_after_render(
    populated_hermes_home: Path, tmp_path: Path, monkeypatch
):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    tunnel = outside_dir / "tunnel"
    tunnel.mkdir()
    output_path = tunnel / "snapshot.txt"

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            tunnel.rmdir()
            tunnel.symlink_to(populated_hermes_home, target_is_directory=True)
            return "snapshot"

        def close(self):
            pass

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--hermes-home",
                str(populated_hermes_home),
                "--snapshot-file",
                str(output_path),
                "--no-color",
            ]
        )

    assert excinfo.value.code == 1
    assert not (populated_hermes_home / "snapshot.txt").exists()


def test_close_interrupts_and_joins_message_search_thread(populated_hermes_home):
    """close() must interrupt an in-flight message search and join its thread
    before closing the collector (and its SQLite connection) underneath it."""
    import threading

    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    calls: list[str] = []
    unblock = threading.Event()
    real_close = app._collector.close

    def fake_interrupt() -> None:
        calls.append("interrupt")
        unblock.set()

    def fake_close() -> None:
        calls.append("close")
        real_close()

    app._collector.interrupt_searches = fake_interrupt  # type: ignore[attr-defined]
    app._collector.close = fake_close  # type: ignore[assignment]

    def worker() -> None:
        unblock.wait(10)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    app._message_search_thread = thread

    app.close()

    assert calls == ["interrupt", "close"]
    assert not thread.is_alive()
