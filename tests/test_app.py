from __future__ import annotations

import signal
import threading
from pathlib import Path

import pytest

from hermesd.app import DashboardApp, ViewState, _osc52_sequence


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
    result = app._handle_key("q")
    assert result == "quit"
    app.close()


def test_app_handle_key_detail(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("3")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 3
    app.close()


def test_app_handle_key_zero_opens_panel_ten(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("0")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 10
    app.close()


def test_app_handle_bracket_navigation_reaches_new_panels(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    for _ in range(10):
        app._handle_key("]")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 11
    app._handle_key("]")
    assert app._view.detail_panel == 12
    app._handle_key("[")
    assert app._view.detail_panel == 11
    app.close()


def test_app_handle_bracket_navigation_wraps_at_boundaries(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    for _ in range(11):
        app._handle_key("]")
    assert app._view.detail_panel == 12
    app._handle_key("]")  # forward wrap: 12 -> 1
    assert app._view.detail_panel == 1
    app._handle_key("[")  # backward wrap: 1 -> 12
    assert app._view.detail_panel == 12
    app.close()


def test_app_handle_key_escape(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("3")
    app._handle_key("\x1b")  # single Esc byte
    assert app._view.mode == "overview"
    app.close()


def test_app_handle_key_escape_sequence_ignored(populated_hermes_home: Path):
    """Multi-byte escape sequences (arrow keys, etc.) must not exit detail."""
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("3")
    app._handle_key("\x1b[A")  # Up arrow
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 3
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
    populated_hermes_home: Path, restore_signal_handlers
):
    """run() drives the Live loop and exits cleanly once the running event clears."""
    import io

    from rich.console import Console

    app = DashboardApp(populated_hermes_home, refresh_rate=1)
    buffer = io.StringIO()
    app._console = Console(file=buffer, width=80, height=24, force_terminal=True)
    stopper = threading.Timer(0.2, app._running.clear)
    stopper.start()
    try:
        app.run()
    finally:
        stopper.cancel()
    assert app._running.is_set() is False
    assert app._closed.is_set() is True
    assert "hermesd" in buffer.getvalue()


def test_run_breaks_promptly_when_stopped_during_render_wait(
    populated_hermes_home: Path, restore_signal_handlers
):
    """A signal arriving while the loop waits stops it before the next frame."""
    import io

    from rich.console import Console

    class ClearsDuringWait(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            if self.is_set():
                self.clear()  # simulate _signal_handler firing mid-wait
                return False
            return super().wait(timeout)

    app = DashboardApp(populated_hermes_home, refresh_rate=1)
    app._running = ClearsDuringWait()
    app._console = Console(file=io.StringIO(), width=80, height=24, force_terminal=True)
    app.run()
    assert app._closed.is_set() is True


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
