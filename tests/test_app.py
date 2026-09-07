"""TUI application: layout, header, footer, view locking, and lifecycle."""

from __future__ import annotations

import re
import signal
import threading
from pathlib import Path

import pytest

from hermesd.app import (
    _LOG_PANEL_NUM,
    DashboardApp,
    ViewState,
    _osc52_sequence,
)


def test_view_state_defaults():
    vs = ViewState()
    assert vs.mode == "overview"
    assert vs.detail_panel is None
    assert vs.focus_panel == 1
    assert vs.scroll_offset == 0
    assert vs.log_sub_view == "agent"
    assert vs.filter_query == ""
    assert vs.filter_edit_mode is False
    assert vs.session_sort == "recent"


def test_view_state_enter_detail():
    vs = ViewState()
    vs.enter_detail(3)
    assert vs.mode == "detail"
    assert vs.detail_panel == 3
    assert vs.focus_panel == 3
    assert vs.scroll_offset == 0


def test_view_state_exit_detail():
    vs = ViewState()
    vs.enter_detail(3)
    vs.start_filter()
    vs.append_filter_char("x")
    vs.exit_detail()
    assert vs.mode == "overview"
    assert vs.detail_panel is None
    assert vs.filter_query == ""
    assert vs.filter_edit_mode is False
    assert vs.session_sort == "recent"


def test_view_state_scroll():
    vs = ViewState()
    vs.enter_detail(8)
    vs.scroll_down()
    assert vs.scroll_offset == 1
    vs.scroll_up()
    assert vs.scroll_offset == 0
    vs.scroll_up()
    assert vs.scroll_offset == 0


def test_view_state_toggle_focus_uses_last_panel():
    vs = ViewState()
    vs.toggle_focus()
    assert vs.mode == "detail"
    assert vs.detail_panel == 1
    vs.exit_detail()
    vs.enter_detail(4)
    vs.toggle_focus()
    assert vs.mode == "overview"
    vs.toggle_focus()
    assert vs.mode == "detail"
    assert vs.detail_panel == 4


def test_view_state_cycle_log_view_with_no_views_is_noop():
    vs = ViewState()
    vs.cycle_log_view_in(())
    assert vs.log_sub_view == "agent"


def test_view_state_cycle_log_view_resets_when_current_view_unknown():
    vs = ViewState()
    vs.cycle_log_view_in(("alpha", "beta"))
    assert vs.log_sub_view == "alpha"
    vs.cycle_log_view_in(("alpha", "beta"))
    assert vs.log_sub_view == "beta"


def test_app_build_layout(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    layout = app._build_layout()
    assert [child.name for child in layout.children] == ["header", "body", "footer"]
    assert layout["header"].size == 1
    assert layout["footer"].size == 1
    app.close()


def test_app_handle_key_quit(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    result = app.handle_key("q")
    assert result == "quit"
    app.close()


def test_app_handle_key_detail(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app.handle_key("3")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 3
    app.close()


def test_app_handle_key_zero_opens_panel_ten(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app.handle_key("0")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 10
    app.close()


def test_app_handle_bracket_navigation_reaches_new_panels(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    for _ in range(10):
        app.handle_key("]")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 11
    app.handle_key("]")
    assert app._view.detail_panel == 12
    app.handle_key("[")
    assert app._view.detail_panel == 11
    app.close()


def test_app_handle_bracket_navigation_wraps_at_boundaries(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    for _ in range(12):
        app.handle_key("]")
    assert app._view.detail_panel == 13
    app.handle_key("]")  # forward wrap: 13 -> 1
    assert app._view.detail_panel == 1
    app.handle_key("[")  # backward wrap: 1 -> 13
    assert app._view.detail_panel == 13
    app.close()


@pytest.mark.parametrize(
    ("setup_key", "key", "expected_mode", "expected_panel"),
    [
        ("3", "\x1b", "overview", None),  # lone Esc leaves detail
        ("3", "\x1b[A", "detail", 3),  # Up arrow must not exit detail
        (None, "\x1b", "overview", None),  # Esc in overview is a no-op
        (None, "\x1b[2~", "overview", None),  # CSI is not read as digit "2"
    ],
    ids=["esc-exits-detail", "arrow-kept-in-detail", "esc-overview-noop", "csi-not-digit"],
)
def test_app_handle_escape_keys(
    populated_hermes_home: Path,
    setup_key: str | None,
    key: str,
    expected_mode: str,
    expected_panel: int | None,
):
    """Lone Esc exits detail; multi-byte escape sequences are inert."""
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    if setup_key is not None:
        app.handle_key(setup_key)
    app.handle_key(key)
    assert app._view.mode == expected_mode
    assert app._view.detail_panel == expected_panel
    app.close()


def test_run_installs_signal_handlers_before_initial_collect(
    populated_hermes_home: Path, monkeypatch, restore_signal_handlers
):
    """Ctrl+C during a slow first collect must hit the app handler, not the default."""
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    seen: dict[str, object] = {}

    def fake_collect():
        seen["sigint_handler"] = signal.getsignal(signal.SIGINT)
        raise RuntimeError("stop before live loop")

    monkeypatch.setattr(app._collector, "collect", fake_collect)
    try:
        with pytest.raises(RuntimeError, match="stop before live loop"):
            app.run()
    finally:
        app.close()
    assert seen["sigint_handler"] == app._signal_handler


def test_run_live_loop_renders_and_exits_when_running_cleared(
    populated_hermes_home: Path, monkeypatch, restore_signal_handlers
):
    """run() drives the Live loop and exits cleanly once the running event clears."""
    import io

    from rich.console import Console

    import hermesd.app as app_module

    class RecordingLive:
        def __init__(self, renderable, *, console, refresh_per_second, screen):
            self.renderable = renderable
            self.console = console

        def __enter__(self):
            self.console.print(self.renderable)
            app_ref._running.clear()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def update(self, renderable):
            self.console.print(renderable)

    app = DashboardApp(populated_hermes_home, refresh_rate=1)
    buffer = io.StringIO()
    app._console = Console(file=buffer, width=80, height=24, force_terminal=True)
    app_ref = app

    monkeypatch.setattr(app_module, "Live", RecordingLive)
    try:
        app.run()
    finally:
        app.close()
    assert app._running.is_set() is False
    assert app._closed.is_set() is True
    assert "hermesd" in buffer.getvalue()


def test_run_breaks_promptly_when_stopped_during_render_wait(
    populated_hermes_home: Path, restore_signal_handlers
):
    """A signal arriving while the loop waits stops it before the next frame."""
    import io

    from rich.console import Console

    class StopsDuringWait(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            app._running.clear()  # simulate _signal_handler firing mid-wait
            self.set()
            return True

    app = DashboardApp(populated_hermes_home, refresh_rate=1)
    app._stop_requested = StopsDuringWait()
    app._console = Console(file=io.StringIO(), width=80, height=24, force_terminal=True)
    app.run()
    assert app._closed.is_set() is True


def test_run_waits_on_stop_event_before_rendering_next_frame(
    populated_hermes_home: Path, monkeypatch, restore_signal_handlers
):
    import io

    from rich.console import Console

    import hermesd.app as app_module

    waits: list[float | None] = []

    class RecordingStopEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            waits.append(timeout)
            return False

    class StopsAfterUpdate:
        def __init__(self, renderable, *, console, refresh_per_second, screen):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def update(self, renderable):
            app._running.clear()

    app = DashboardApp(populated_hermes_home, refresh_rate=1)
    app._stop_requested = RecordingStopEvent()
    app._console = Console(file=io.StringIO(), width=80, height=24, force_terminal=True)
    monkeypatch.setattr(app_module, "Live", StopsAfterUpdate)

    app.run()

    assert waits == [0.5]


def test_run_exits_cleanly_on_keyboard_interrupt(
    populated_hermes_home: Path, monkeypatch, restore_signal_handlers
):
    """Ctrl+C inside the Live loop quits without a traceback and closes the app."""
    import io

    from rich.console import Console

    app = DashboardApp(populated_hermes_home, refresh_rate=1)
    app._console = Console(file=io.StringIO(), width=80, height=24, force_terminal=True)
    original_build = app._build_layout
    calls: list[int] = []

    def interrupting_build(console=None):
        calls.append(1)
        if len(calls) > 1:
            raise KeyboardInterrupt
        return original_build(console=console)

    monkeypatch.setattr(app, "_build_layout", interrupting_build)
    app.run()  # must not raise
    assert app._running.is_set() is False
    assert app._closed.is_set() is True


@pytest.mark.skipif(not hasattr(signal, "SIGINT"), reason="platform has no SIGINT semantics")
def test_run_exits_cleanly_on_a_real_sigint(
    populated_hermes_home: Path, monkeypatch, restore_signal_handlers
):
    """A genuine SIGINT delivered to the process stops run() and joins both threads."""
    import io
    import os

    from rich.console import Console

    import hermesd.app as app_module

    entered = threading.Event()

    class SignallingLive:
        def __init__(self, renderable, *, console, refresh_per_second, screen):
            pass

        def __enter__(self):
            entered.set()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def update(self, renderable):
            pass

    def send_sigint() -> None:
        # The loop is inside Live; the signal is delivered to the main thread,
        # where run() installed the handler.
        assert entered.wait(timeout=10.0), "run() never entered the Live loop"
        os.kill(os.getpid(), signal.SIGINT)

    app = DashboardApp(populated_hermes_home, refresh_rate=1)
    app._console = Console(file=io.StringIO(), width=80, height=24, force_terminal=True)
    monkeypatch.setattr(app_module, "Live", SignallingLive)

    signaller = threading.Thread(target=send_sigint)
    signaller.start()
    try:
        app.run()  # must return, not raise KeyboardInterrupt
    finally:
        signaller.join(timeout=10.0)
        app.close()

    assert signaller.is_alive() is False
    assert app._running.is_set() is False
    assert app._stop_requested.is_set() is True
    assert app._closed.is_set() is True
    for thread in (app._collector_thread, app._input_thread):
        assert thread is not None
        thread.join(timeout=5.0)
        assert thread.is_alive() is False


def test_run_reports_render_failure_and_exits_nonzero(
    populated_hermes_home: Path, monkeypatch, restore_signal_handlers, capsys
):
    """A render error is a one-line stderr message plus exit code 1, not a traceback."""
    import io

    from rich.console import Console

    import hermesd.app as app_module

    class ImmediateStopEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            return False

    class PassthroughLive:
        def __init__(self, renderable, *, console, refresh_per_second, screen):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def update(self, renderable):
            pass

    app = DashboardApp(populated_hermes_home, refresh_rate=1)
    app._console = Console(file=io.StringIO(), width=80, height=24, force_terminal=True)
    app._stop_requested = ImmediateStopEvent()
    monkeypatch.setattr(app_module, "Live", PassthroughLive)
    original_build = app._build_layout
    calls: list[int] = []

    def failing_build(console=None):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("layout exploded")
        return original_build(console=console)

    monkeypatch.setattr(app, "_build_layout", failing_build)

    with pytest.raises(SystemExit) as excinfo:
        app.run()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "hermesd: render failed: RuntimeError: layout exploded" in captured.err
    assert "Traceback" not in captured.err
    assert app._running.is_set() is False
    assert app._closed.is_set() is True


def test_run_closes_threads_and_collector_when_loop_quits(
    populated_hermes_home: Path, monkeypatch, restore_signal_handlers
):
    """After a normal quit, both daemon threads have exited and the collector is closed."""
    import io

    from rich.console import Console

    import hermesd.app as app_module

    class StopsOnEnter:
        def __init__(self, renderable, *, console, refresh_per_second, screen):
            pass

        def __enter__(self):
            app._running.clear()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def update(self, renderable):
            pass

    app = DashboardApp(populated_hermes_home, refresh_rate=1)
    app._console = Console(file=io.StringIO(), width=80, height=24, force_terminal=True)
    monkeypatch.setattr(app_module, "Live", StopsOnEnter)
    collector_closes: list[int] = []
    real_collector_close = app._collector.close

    def spy_close() -> None:
        collector_closes.append(1)
        real_collector_close()

    monkeypatch.setattr(app._collector, "close", spy_close)

    app.run()

    assert app._collector_thread is not None
    assert app._input_thread is not None
    assert app._collector_thread.is_alive() is False
    assert app._input_thread.is_alive() is False
    assert collector_closes == [1]
    assert app._closed.is_set() is True


def test_signal_handler_clears_running_event(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._running.set()
    app._signal_handler(0, None)
    assert app._running.is_set() is False
    app.close()


def test_osc52_sequence():
    sequence = _osc52_sequence("hello")
    assert sequence.startswith("\033]52;c;")
    assert sequence.endswith("\a")


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


def test_dashboard_header_shows_profile_mode_label(profiled_hermes_home: Path):
    app = DashboardApp(profiled_hermes_home, profile_name="coding")
    state = app._collector.collect()
    header = app._build_header(state)
    assert "profile:coding" in header.plain
    app.close()


def test_dashboard_header_shows_root_mode_label(profiled_hermes_home: Path):
    app = DashboardApp(profiled_hermes_home)
    state = app._collector.collect()
    header = app._build_header(state)
    assert re.search(r"\broot\b", header.plain)
    app.close()
