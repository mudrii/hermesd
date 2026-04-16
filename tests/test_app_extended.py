from pathlib import Path

from hermesd.app import DashboardApp


def test_handle_key_refresh(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    result = app._handle_key("r")
    assert result == "refresh"
    assert app._force_refresh.is_set()
    app.close()


def test_handle_key_help_toggle(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    assert app._view.show_help is False
    app._handle_key("?")
    assert app._view.show_help is True
    app._handle_key("?")
    assert app._view.show_help is False
    app.close()


def test_handle_key_scroll_in_detail(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("3")
    app._handle_key("j")
    assert app._view.scroll_offset == 1
    app._handle_key("j")
    assert app._view.scroll_offset == 2
    app._handle_key("k")
    assert app._view.scroll_offset == 1
    app.close()


def test_handle_key_tab_cycles_log_view(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("8")
    assert app._view.detail_panel == 8
    assert app._view.log_sub_view == "agent"
    app._handle_key("\t")
    assert app._view.log_sub_view == "gateway"
    app._handle_key("\t")
    assert app._view.log_sub_view == "errors"
    app.close()


def test_handle_key_tab_ignored_outside_logs(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("3")
    app._handle_key("\t")
    assert app._view.log_sub_view == "agent"
    app.close()


def test_handle_key_invalid_returns_none(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    result = app._handle_key("x")
    assert result is None
    app.close()


def test_handle_key_digit_0_ignored(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("0")
    assert app._view.mode == "overview"
    app.close()


def test_handle_key_digit_9_ignored(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("9")
    assert app._view.mode == "overview"
    app.close()


def test_handle_key_escape_in_overview_noop(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("\x1b")
    assert app._view.mode == "overview"
    app.close()


def test_handle_key_escape_sequence_not_digit(populated_hermes_home: Path):
    """Arrow keys like \\x1b[2~ must not be treated as digit '2'."""
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("\x1b[2~")
    assert app._view.mode == "overview"
    assert app._view.detail_panel is None
    app.close()


def test_handle_key_multi_char_ignored(populated_hermes_home: Path):
    """Multi-char input that isn't an escape sequence is ignored."""
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    result = app._handle_key("ab")
    assert result is None
    assert app._view.mode == "overview"
    app.close()


def test_build_help_panel(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._view.show_help = True
    layout = app._build_layout()
    assert layout is not None
    app.close()


def test_build_detail_layout(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._view.enter_detail(2)
    layout = app._build_layout()
    assert layout is not None
    app.close()


def test_build_header_with_custom_skin(populated_hermes_home: Path):
    import yaml

    config_path = populated_hermes_home / "config.yaml"
    config_path.write_text(yaml.dump({"display": {"skin": "ares"}}))
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    state = app._collector.collect()
    header = app._build_header(state)
    assert header is not None
    app.close()


def test_build_footer_overview(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    footer = app._build_footer(app._state)
    assert footer is not None
    app.close()


def test_build_footer_detail_logs(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._view.enter_detail(8)
    footer = app._build_footer(app._state)
    from rich.text import Text

    assert isinstance(footer, Text)
    app.close()


def test_app_close_idempotent(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app.close()
    app.close()


def test_no_color_mode(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5, no_color=True)
    assert app._console.no_color is True
    app.close()
