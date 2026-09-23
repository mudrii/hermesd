"""ESTOP sentinel: collection, header banner and Operations indication.

Upstream ``hermes pause`` writes ``$HERMES_HOME/ESTOP`` with optional JSON
``{"reason", "engaged_at"}``; a corrupt/empty file (``touch``) still counts as
engaged, and a profile process honours both its own home and the fleet root
(``agent/estop.py:1-8,33-50,64-73``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import hermesd.collector as collector_module
from hermesd.app import DashboardApp
from hermesd.collector import Collector
from hermesd.models import RuntimeStatus
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str

_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC).timestamp()


def _collect(home: Path, **kwargs: object):
    collector = Collector(home, clock=lambda: _NOW, **kwargs)  # type: ignore[arg-type]
    try:
        return collector.collect()
    finally:
        collector.close()


def _write_estop(home: Path, reason: object = "deploy freeze", engaged_at: object = None) -> None:
    payload = {
        "engaged_at": engaged_at or datetime(2026, 9, 23, 11, 0, tzinfo=UTC).isoformat(),
        "reason": reason,
    }
    (home / "ESTOP").write_text(json.dumps(payload, indent=2) + "\n")


def test_no_sentinel_is_not_engaged(hermes_home: Path):
    runtime = _collect(hermes_home).runtime
    assert runtime.estop_engaged is False
    assert runtime.estop_reason == ""


def test_json_sentinel_reports_reason_and_age(hermes_home: Path):
    _write_estop(hermes_home)
    state = _collect(hermes_home)
    runtime = state.runtime
    assert runtime.estop_engaged is True
    assert runtime.estop_reason == "deploy freeze"
    assert runtime.estop_age_seconds == pytest.approx(3600.0)
    assert runtime.estop_scope == "root"
    assert "estop" not in state.health.failed_sources


def test_touched_empty_sentinel_still_counts_as_engaged(hermes_home: Path):
    (hermes_home / "ESTOP").touch()
    runtime = _collect(hermes_home).runtime
    assert runtime.estop_engaged is True
    assert runtime.estop_reason == ""
    # No engaged_at: the file mtime dates the pause instead.
    assert runtime.estop_age_seconds is not None


def test_null_reason_and_junk_engaged_at_are_tolerated(hermes_home: Path):
    _write_estop(hermes_home, reason=None, engaged_at="not-a-date")
    runtime = _collect(hermes_home).runtime
    assert runtime.estop_engaged is True
    assert runtime.estop_reason == ""
    assert runtime.estop_age_seconds is not None


def test_reason_is_redacted_and_capped(hermes_home: Path):
    _write_estop(hermes_home, reason="token sk-" + "a" * 40 + " " + "x" * 400)
    runtime = _collect(hermes_home).runtime
    assert "a" * 40 not in runtime.estop_reason
    assert len(runtime.estop_reason) <= 120


def test_profile_process_honours_the_root_sentinel(profiled_hermes_home: Path):
    _write_estop(profiled_hermes_home, reason="fleet pause")
    runtime = _collect(profiled_hermes_home, profile_name="coding").runtime
    assert runtime.estop_engaged is True
    assert runtime.estop_reason == "fleet pause"
    assert runtime.estop_scope == "root"


def test_profile_sentinel_is_checked_first(profiled_hermes_home: Path):
    _write_estop(profiled_hermes_home / "profiles" / "coding", reason="profile pause")
    _write_estop(profiled_hermes_home, reason="fleet pause")
    runtime = _collect(profiled_hermes_home, profile_name="coding").runtime
    assert runtime.estop_reason == "profile pause"
    assert runtime.estop_scope == "profile"


def test_root_mode_ignores_a_profile_sentinel(profiled_hermes_home: Path):
    _write_estop(profiled_hermes_home / "profiles" / "coding", reason="profile pause")
    assert _collect(profiled_hermes_home).runtime.estop_engaged is False


def test_symlinked_sentinel_counts_but_is_not_read(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"reason": "OUTSIDE-SENTINEL"}))
    (hermes_home / "ESTOP").symlink_to(outside)
    runtime = _collect(hermes_home).runtime
    assert runtime.estop_engaged is True
    assert "OUTSIDE-SENTINEL" not in runtime.estop_reason


def test_read_failure_keeps_last_good_estop(hermes_home: Path, monkeypatch: pytest.MonkeyPatch):
    _write_estop(hermes_home)
    collector = Collector(hermes_home, clock=lambda: _NOW)
    try:
        before = collector.collect()
        assert before.runtime.estop_engaged is True

        def boom(*args: object, **kwargs: object):
            raise OSError("stat failed")

        monkeypatch.setattr(collector_module, "_read_estop", boom)
        after = collector.collect()
    finally:
        collector.close()
    assert "estop" in after.health.failed_sources
    assert after.runtime.estop_engaged is True
    assert after.runtime.estop_reason == "deploy freeze"


def test_header_shows_paused_banner(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    try:
        state = app._state.model_copy(
            update={
                "runtime": RuntimeStatus(
                    agent_running=True,
                    estop_engaged=True,
                    estop_reason="deploy [bold]freeze\x1b[31m",
                    estop_age_seconds=3600.0,
                )
            }
        )
        header = app._build_header(state)
    finally:
        app.close()
    assert "⏸ PAUSED (ESTOP): deploy [bold]freeze, since 1h" in header.plain
    assert "\x1b" not in header.plain


def test_header_paused_banner_without_reason(populated_hermes_home: Path):
    app = DashboardApp(populated_hermes_home, refresh_rate=5)
    try:
        state = app._state.model_copy(
            update={"runtime": RuntimeStatus(agent_running=True, estop_engaged=True)}
        )
        header = app._build_header(state)
    finally:
        app.close()
    assert "⏸ PAUSED (ESTOP)" in header.plain


def test_operations_panel_indicates_estop(hermes_home: Path):
    _write_estop(hermes_home)
    state = _collect(hermes_home)
    compact = render_to_str(render_panel(12, state, Theme()), width=100, no_color=True)
    detail = render_to_str(render_panel(12, state, Theme(), detail=True), width=160, no_color=True)
    assert "PAUSED (ESTOP)" in compact
    assert "deploy freeze" in detail
