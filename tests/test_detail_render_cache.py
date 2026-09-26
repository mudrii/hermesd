"""Idle frames and scrolling reuse a detail panel's rendered lines."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console

import hermesd.app as app_module
from hermesd.app import DashboardApp


@pytest.fixture
def counted_app(populated_hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[int, bool]] = []
    real = app_module.render_panel

    def counting(panel_num, state, theme, *args, **kwargs):
        calls.append((panel_num, bool(kwargs.get("detail"))))
        return real(panel_num, state, theme, *args, **kwargs)

    monkeypatch.setattr(app_module, "render_panel", counting)
    app = DashboardApp(populated_hermes_home, refresh_rate=5, no_color=True)
    app._set_state(app._collector.collect())
    app._console = Console(file=io.StringIO(), width=120, height=20, no_color=True)
    yield app, calls
    app.close()


def _detail_renders(calls: list[tuple[int, bool]], panel: int) -> int:
    return sum(1 for num, detail in calls if num == panel and detail)


def _frame(app: DashboardApp) -> str:
    console = app._console
    with console.capture() as capture:
        console.print(app._build_layout())
    return capture.get()


def test_idle_frames_and_scrolling_reuse_the_rendered_detail(counted_app, monkeypatch):
    app, calls = counted_app
    clock = Mock()
    clock.now.return_value.strftime.side_effect = ["12:00:00", "12:00:01", "12:00:02"]
    monkeypatch.setattr(app_module, "datetime", clock)
    app.handle_key("2")

    first = _frame(app)
    second = _frame(app)
    # The live header clock advances even when the detail viewport is cached.
    assert second.splitlines()[0] != first.splitlines()[0]
    assert second.splitlines()[1:] == first.splitlines()[1:]
    app.handle_key("j")
    scrolled = _frame(app)

    assert _detail_renders(calls, 2) == 1
    assert scrolled.splitlines()[1:] != first.splitlines()[1:], "scrolling moves the viewport"


def test_new_state_width_or_view_settings_rerender(counted_app):
    app, calls = counted_app
    app.handle_key("2")
    _frame(app)

    app._set_state(app._collector.collect())
    _frame(app)
    assert _detail_renders(calls, 2) == 2

    app._console = Console(file=io.StringIO(), width=100, height=20, no_color=True)
    _frame(app)
    assert _detail_renders(calls, 2) == 3

    app.handle_key("s")  # cycle the session sort
    _frame(app)
    assert _detail_renders(calls, 2) == 4


def test_logs_detail_is_not_cached(counted_app):
    app, calls = counted_app
    app.handle_key("8")

    _frame(app)
    _frame(app)

    assert _detail_renders(calls, 8) == 2
