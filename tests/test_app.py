import threading
from pathlib import Path

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


def test_view_state_cycle_log_view():
    vs = ViewState()
    assert vs.log_sub_view == "agent"
    vs.cycle_log_view()
    assert vs.log_sub_view == "gateway"
    vs.cycle_log_view()
    assert vs.log_sub_view == "errors"
    vs.cycle_log_view()
    assert vs.log_sub_view == "cron"
    vs.cycle_log_view()
    assert vs.log_sub_view == "agent"


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


def test_app_build_layout(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    layout = app._build_layout()
    assert layout is not None
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


def test_app_uses_event_for_running_state(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    assert isinstance(app._running, threading.Event)
    assert app._running.is_set() is False
    app.close()


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
