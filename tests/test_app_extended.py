import io
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from hermesd.app import _SKILLS_PANEL_NUM, DashboardApp
from hermesd.models import HealthSummary, RuntimeStatus
from hermesd.panels import PANEL_NAMES
from hermesd.theme import load_theme


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


def test_handle_key_c_copies_current_view(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5, no_color=True)
    buffer = io.StringIO()
    app._console = Console(file=buffer, width=120, height=40, force_terminal=True, no_color=True)
    app._handle_key("c")
    copied = buffer.getvalue()
    assert "]52;c;" in copied
    app.close()


def test_handle_key_f_toggles_focus_mode(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("f")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 1
    app._handle_key("f")
    assert app._view.mode == "overview"
    app.close()


def test_handle_key_f_reuses_last_selected_panel(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("5")
    app._handle_key("f")
    assert app._view.mode == "overview"
    app._handle_key("f")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 5
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
    app._handle_key("\t")
    assert app._view.log_sub_view == "cron"
    app.close()


def test_handle_key_tab_ignored_outside_logs(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("3")
    app._handle_key("\t")
    assert app._view.log_sub_view == "agent"
    app.close()


def test_handle_key_slash_enters_filter_mode_in_sessions(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("2")
    app._handle_key("/")
    assert app._view.filter_edit_mode is True
    assert app._view.filter_query == ""
    app.close()


def test_handle_key_filter_text_and_enter(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("8")
    app._handle_key("/")
    app._handle_key("e")
    app._handle_key("r")
    app._handle_key("r")
    assert app._view.filter_query == "err"
    assert app._view.filter_edit_mode is True
    app._handle_key("\r")
    assert app._view.filter_edit_mode is False
    assert app._view.filter_query == "err"
    app.close()


def test_handle_key_filter_backspace(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("2")
    app._handle_key("/")
    app._handle_key("a")
    app._handle_key("b")
    app._handle_key("\x7f")
    assert app._view.filter_query == "a"
    app.close()


def test_handle_key_escape_exits_filter_mode_before_detail(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("2")
    app._handle_key("/")
    app._handle_key("x")
    app._handle_key("\x1b")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 2
    assert app._view.filter_edit_mode is False
    assert app._view.filter_query == "x"
    app.close()


def test_handle_key_slash_ignored_outside_filter_panels(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("5")
    app._handle_key("/")
    assert app._view.filter_edit_mode is False
    assert app._view.filter_query == ""
    app.close()


def test_handle_key_s_cycles_session_sort(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("2")
    assert app._view.session_sort == "recent"
    app._handle_key("s")
    assert app._view.session_sort == "cost"
    app._handle_key("s")
    assert app._view.session_sort == "tokens"
    app._handle_key("s")
    assert app._view.session_sort == "recent"
    app.close()


def test_handle_key_s_ignored_outside_sessions(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("8")
    app._handle_key("s")
    assert app._view.session_sort == "recent"
    app.close()


def test_handle_key_g_and_big_g_jump_scroll(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("8")
    app._handle_key("j")
    app._handle_key("j")
    assert app._view.scroll_offset == 2
    app._handle_key("G")
    assert app._view.scroll_offset == 999_999
    app._handle_key("g")
    assert app._view.scroll_offset == 0
    app.close()


def test_handle_key_invalid_returns_none(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    result = app._handle_key("x")
    assert result is None
    app.close()


def test_handle_key_digit_0_enters_memory_detail(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("0")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 10
    app.close()


def test_handle_key_digit_9_enters_profiles_detail(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("9")
    assert app._view.mode == "detail"
    assert app._view.detail_panel == 9
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


def test_handle_key_p_cycles_profile_view_in_profiles_panel(profiled_hermes_home: Path):
    app = DashboardApp(profiled_hermes_home, refresh_rate=5)
    app._handle_key("9")
    assert app._view.profile_cycle_index == 0
    app._handle_key("p")
    assert app._view.profile_cycle_index == 1
    app.close()


def test_handle_key_p_ignored_outside_profiles_panel(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._handle_key("3")
    app._handle_key("p")
    assert app._view.profile_cycle_index == 0
    app.close()


def test_build_help_panel(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._view.show_help = True
    layout = app._build_layout()
    assert layout is not None
    app.close()


def test_build_help_panel_uses_dynamic_panel_range(populated_hermes_home: Path, monkeypatch):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    monkeypatch.setattr(
        "hermesd.app.PANEL_NAMES",
        {
            1: "Gateway & Platforms",
            2: "Sessions",
            3: "Tokens / Cost",
            4: "Tools",
            5: "Config",
            6: "Cron",
            7: "Skills / Integrations",
            8: "Logs",
            9: "Profiles",
        },
    )
    panel = app._build_help()
    assert "1-9" in panel.renderable.plain
    app.close()


def test_build_detail_layout(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._view.enter_detail(2)
    layout = app._build_layout()
    assert layout is not None
    app.close()


def test_build_detail_layout_uses_session_message_search(populated_hermes_home: Path, monkeypatch):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._set_state(app._collector.collect())
    app._view.enter_detail(2)
    app._view.filter_query = "message:response"
    called: dict[str, str] = {}

    def fake_search(query: str) -> set[str]:
        called["query"] = query
        return {"sess_001"}

    monkeypatch.setattr(app._collector, "search_session_ids_by_message", fake_search)
    layout = app._build_layout()
    assert layout is not None
    assert called["query"] == "response"
    app.close()


def test_snapshot_view_state_round_trips_all_fields(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._view.mode = "detail"
    app._view.detail_panel = 8
    app._view.focus_panel = 7
    app._view.scroll_offset = 12
    app._view.log_sub_view = "errors"
    app._view.show_help = True
    app._view.profile_cycle_index = 3
    app._view.filter_query = "message:timeout"
    app._view.filter_edit_mode = True
    app._view.session_sort = "cost"

    snapshot = app._snapshot_view_state()
    app._view = app._view.__class__()
    app._restore_view_state(snapshot)

    restored = app._snapshot_view_state()
    assert restored == snapshot
    assert restored.mode == "detail"
    assert restored.detail_panel == 8
    assert restored.focus_panel == 7
    assert restored.scroll_offset == 12
    assert restored.log_sub_view == "errors"
    assert restored.show_help is True
    assert restored.profile_cycle_index == 3
    assert restored.filter_query == "message:timeout"
    assert restored.filter_edit_mode is True
    assert restored.session_sort == "cost"
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


def test_build_header_pads_to_console_width(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._console = Console(width=100, height=24, force_terminal=True)
    header = app._build_header(app._state)

    assert len(header.plain) == app._console.width
    app.close()


def test_app_refreshes_theme_when_skin_changes(populated_hermes_home: Path):
    import yaml

    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    assert app._theme.skin_name == "default"

    config_path = populated_hermes_home / "config.yaml"
    config_path.write_text(yaml.dump({"display": {"skin": "ares"}}))

    new_state = app._collector.collect()
    app._set_state(new_state)

    assert app._theme.skin_name == "ares"
    app.close()


def test_app_unknown_skin_does_not_reload_theme_every_update(populated_hermes_home: Path):
    import yaml

    config_path = populated_hermes_home / "config.yaml"
    config_path.write_text(yaml.dump({"display": {"skin": "nonexistent"}}))

    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    state = app._collector.collect()

    with patch("hermesd.app.load_theme", wraps=load_theme) as mocked:
        app._set_state(state)
        app._set_state(state)

    assert mocked.call_count <= 1
    app.close()


def test_build_footer_overview(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    footer = app._build_footer(app._state)
    assert footer is not None
    assert "[1-9,0]" in footer.plain
    assert "[f]" in footer.plain
    assert "[c]" in footer.plain
    assert "0/0" in footer.plain
    app.close()


def test_build_footer_overview_uses_dynamic_panel_range(populated_hermes_home: Path, monkeypatch):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    monkeypatch.setattr(
        "hermesd.app.PANEL_NAMES",
        {
            1: "Gateway & Platforms",
            2: "Sessions",
            3: "Tokens / Cost",
            4: "Tools",
            5: "Config",
            6: "Cron",
            7: "Skills / Integrations",
            8: "Logs",
            9: "Profiles",
        },
    )
    footer = app._build_footer(app._state)
    assert "[1-9]" in footer.plain
    app.close()


def test_build_footer_detail_logs(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._view.enter_detail(8)
    footer = app._build_footer(app._state)
    from rich.text import Text

    assert isinstance(footer, Text)
    assert "[f]" in footer.plain
    assert "[c]" in footer.plain
    assert "[/]" in footer.plain
    assert "[g/G]" in footer.plain
    app.close()


def test_build_footer_detail_sessions_shows_sort(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._view.enter_detail(2)
    footer = app._build_footer(app._state)
    assert "[s]" in footer.plain
    assert "sort=recent" in footer.plain
    app.close()


def test_skills_panel_constant_matches_registry():
    assert _SKILLS_PANEL_NUM in PANEL_NAMES
    assert PANEL_NAMES[_SKILLS_PANEL_NUM] == "Skills / Integrations"


def test_build_help_panel_shows_filter_shortcut(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    panel = app._build_help()
    assert "1-9,0" in panel.renderable.plain
    assert "Toggle focus mode" in panel.renderable.plain
    assert "Copy the current rendered view" in panel.renderable.plain
    assert "/" in panel.renderable.plain
    assert "detail filter" in panel.renderable.plain
    assert "Cycle session sort" in panel.renderable.plain
    assert "Jump to top/bottom" in panel.renderable.plain
    app.close()


def test_build_footer_shows_input_error(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._input_error = "input failure"
    footer = app._build_footer(app._state)
    assert "input failure" in footer.plain
    app.close()


def test_build_footer_shows_health_failures(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    state = app._state.model_copy(
        update={
            "health": HealthSummary(
                total_sources=16, ok_sources=14, failed_sources=["logs", "cron"]
            )
        }
    )
    footer = app._build_footer(state)
    assert "14/16" in footer.plain
    assert "logs,cron" in footer.plain
    app.close()


def test_build_header_shows_offline_banner(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    state = app._state.model_copy(
        update={"runtime": RuntimeStatus(agent_running=False, banner="AGENT OFFLINE")}
    )
    header = app._build_header(state)
    assert "AGENT OFFLINE" in header.plain
    app.close()


def test_build_footer_shows_offline_banner(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    state = app._state.model_copy(
        update={"runtime": RuntimeStatus(agent_running=False, banner="AGENT OFFLINE")}
    )
    footer = app._build_footer(state)
    assert "agent offline" in footer.plain
    app.close()


def test_compact_overview_renders_tools_and_skills_panels(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._console = Console(width=80, height=24, force_terminal=True)
    state = app._collector.collect()
    overview = app._build_overview(state)

    with app._console.capture() as cap:
        app._console.print(overview)
    text = cap.get()

    assert "Tools" in text
    assert "Skills / Integrations" in text
    assert "Memory" in text
    app.close()


def test_wide_overview_renders_existing_panels(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._console = Console(width=120, height=40, force_terminal=True)
    state = app._collector.collect()
    overview = app._build_overview(state)

    with app._console.capture() as cap:
        app._console.print(overview)
    text = cap.get()

    assert "Gateway & Platforms" in text
    assert "Sessions" in text
    assert "Tokens / Cost" in text
    assert "Tools" in text
    assert "Config" in text
    assert "Cron" in text
    assert "Skills / Integrations" in text
    assert "Logs" in text
    assert "Memory" in text
    app.close()


def test_tall_narrow_overview_uses_single_column_rows(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app._console = Console(width=90, height=55, force_terminal=True)
    state = app._collector.collect()
    overview = app._build_overview(state)

    assert len(overview.children) == 10

    with app._console.capture() as cap:
        app._console.print(overview)
    text = cap.get()

    assert "Gateway & Platforms" in text
    assert "Profiles" in text
    assert "Memory" in text
    app.close()


def test_app_close_idempotent(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    app.close()
    app.close()


def test_app_close_wakes_collector_wait(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=30)
    assert app._force_refresh.is_set() is False
    app.close()
    assert app._force_refresh.is_set() is True


def test_no_color_mode(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5, no_color=True)
    assert app._console.no_color is True
    app.close()


def test_app_requires_positive_refresh_rate(populated_hermes_home: Path):
    try:
        DashboardApp(populated_hermes_home, refresh_rate=0)
    except ValueError as exc:
        assert "refresh_rate must be positive" in str(exc)
    else:
        raise AssertionError("DashboardApp should reject non-positive refresh_rate")


def test_copy_current_view_returns_overview_text(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5, no_color=True)
    buffer = io.StringIO()
    app._console = Console(file=buffer, width=120, height=40, force_terminal=True, no_color=True)
    copied = app.copy_current_view()
    assert "Gateway & Platforms" in copied
    assert "Memory" in copied
    app.close()


def test_copy_current_view_returns_detail_text(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5, no_color=True)
    buffer = io.StringIO()
    app._console = Console(file=buffer, width=120, height=40, force_terminal=True, no_color=True)
    app._set_state(app._collector.collect())
    app._handle_key("2")
    copied = app.copy_current_view()
    assert "[2] Sessions" in copied
    assert "sess_001" in copied
    app.close()


def test_copy_current_view_preserves_detail_filter_and_sort(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5, no_color=True)
    buffer = io.StringIO()
    app._console = Console(file=buffer, width=120, height=40, force_terminal=True, no_color=True)
    app._set_state(app._collector.collect())
    app._handle_key("2")
    app._view.filter_query = "source:telegram"
    app._view.session_sort = "cost"

    copied = app.copy_current_view()

    assert "Filter: source:telegram" in copied
    assert "Sort: cost" in copied
    app.close()
