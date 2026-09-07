"""Unicode rendering safety for CJK/emoji fixture data.

Untrusted ~/.hermes free-text (session titles, log lines, skill descriptions)
routinely contains wide CJK characters and emoji. These tests build a fixture
home with such data and assert that header/footer/panel rendering does not
crash and that Rich's cell-width accounting (``cell_len``) keeps every
rendered line within the console width — behavioral assertions only, never
exact-output snapshots.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.text import Text

from hermesd.app import DashboardApp
from hermesd.collector import Collector
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import create_state_db_tables, render_to_str

CJK_TITLE = "調査ダッシュボード改善🚀✨ コンテキスト"
UNICODE_LOG_MESSAGE = "ツール呼び出し完了 🛠️ 応答 ✅ 絵文字テスト"
UNICODE_SKILL_DESCRIPTION = "日本語の説明 📊 ユニコードスキル"


@pytest.fixture
def unicode_hermes_home(hermes_home: Path) -> Path:
    """A hermes home whose free-text fields carry CJK/emoji content."""
    conn = sqlite3.connect(str(hermes_home / "state.db"))
    create_state_db_tables(conn)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, title, message_count) "
        "VALUES (?, ?, ?, ?, ?)",
        ("sess_unicode", "cli", time.time(), CJK_TITLE, 1),
    )
    conn.commit()
    conn.close()

    (hermes_home / "logs" / "agent.log").write_text(
        f"2026-04-09 15:41:58,123 - hermes - INFO - {UNICODE_LOG_MESSAGE}\n"
    )

    skill_dir = hermes_home / "skills" / "dev" / "unicode-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: unicode-skill\ndescription: {UNICODE_SKILL_DESCRIPTION}\n---\n"
    )
    return hermes_home


def _collect(home: Path):
    collector = Collector(home)
    try:
        return collector.collect()
    finally:
        collector.close()


@pytest.mark.parametrize("panel_num", [2, 7, 8])
@pytest.mark.parametrize("detail", [False, True])
def test_panels_render_unicode_fixture_data_within_width(
    unicode_hermes_home: Path, panel_num: int, detail: bool
):
    state = _collect(unicode_hermes_home)
    width = 60  # narrow on purpose: forces truncation of wide-char content

    rendered = render_to_str(
        render_panel(panel_num, state, Theme(), detail=detail),
        width=width,
        no_color=True,
    )

    assert rendered
    # Rich must never let a line exceed the console width, even when the
    # content is full of double-width CJK characters and emoji.
    for line in rendered.splitlines():
        visible_line = Text.from_ansi(line).plain
        assert cell_len(visible_line) <= width, (
            f"panel {panel_num} overflowed width {width}: {visible_line!r}"
        )


def test_sessions_panel_filter_matches_unicode_title(unicode_hermes_home: Path):
    state = _collect(unicode_hermes_home)
    # The collector must surface the CJK/emoji title intact.
    assert state.sessions[0].title == CJK_TITLE

    # Titles feed the filter haystack: a CJK filter term must match the
    # session, and a non-matching term must hide it — behavioral, not output.
    matched = render_to_str(
        render_panel(2, state, Theme(), detail=True, filter_query="調査"),
        width=160,
        no_color=True,
    )
    assert "_unicode" in matched

    unmatched = render_to_str(
        render_panel(2, state, Theme(), detail=True, filter_query="存在しない語"),
        width=160,
        no_color=True,
    )
    assert "_unicode" not in unmatched


def test_logs_panel_surfaces_unicode_log_message(unicode_hermes_home: Path):
    state = _collect(unicode_hermes_home)

    rendered = render_to_str(render_panel(8, state, Theme(), detail=True), width=160, no_color=True)

    assert "ツール呼び出し完了" in rendered


def test_dashboard_header_and_footer_render_unicode_state(unicode_hermes_home: Path):
    app = DashboardApp(unicode_hermes_home, refresh_rate=5, no_color=True)
    try:
        state = app._collector.collect()
        header = app._build_header(state)
        footer = app._build_footer(state)
    finally:
        app.close()

    assert header.plain
    assert footer.plain


def test_full_snapshot_renders_unicode_home_without_crash(unicode_hermes_home: Path):
    app = DashboardApp(unicode_hermes_home, refresh_rate=5, no_color=True)
    try:
        snapshot = app.render_snapshot_text()
    finally:
        app.close()

    assert snapshot
