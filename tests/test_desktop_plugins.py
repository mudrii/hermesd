"""Desktop plugin inventory is root-scoped metadata, never JavaScript execution state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermesd.app import DashboardApp
from hermesd.collect.desktop_plugins import DESKTOP_PLUGIN_LIMIT, read_desktop_plugins
from hermesd.collector import Collector
from hermesd.models import DashboardState, DesktopPluginInfo, SkillsMemory
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str


def _write_desktop_plugin(home: Path, name: str, source: str = "export default {}") -> Path:
    plugin_dir = home / "desktop-plugins" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.js").write_text(source)
    return plugin_dir


def _collect(home: Path, *, profile_name: str | None = None) -> DashboardState:
    collector = Collector(home, profile_name=profile_name)
    try:
        return collector.collect()
    finally:
        collector.close()


def test_reader_inventories_folder_names_without_reading_plugin_source(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = _write_desktop_plugin(hermes_home, "weather", "TOP SECRET SOURCE")
    original_open = Path.open
    original_read_text = Path.read_text

    def refuse_plugin_source_open(path: Path, *args: object, **kwargs: object):
        if path == plugin_dir / "plugin.js":
            raise AssertionError("desktop plugin source must not be opened")
        return original_open(path, *args, **kwargs)

    def refuse_plugin_source(path: Path, *args: object, **kwargs: object) -> str:
        if path == plugin_dir / "plugin.js":
            raise AssertionError("desktop plugin source must not be read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_plugin_source_open)
    monkeypatch.setattr(Path, "read_text", refuse_plugin_source)

    plugins, truncated = read_desktop_plugins(hermes_home / "desktop-plugins", hermes_home)

    assert plugins == [DesktopPluginInfo(name="weather")]
    assert truncated is False


def test_collector_keeps_desktop_and_agent_plugins_separate(hermes_home: Path) -> None:
    _write_desktop_plugin(hermes_home, "desktop-weather")
    agent_dir = hermes_home / "plugins" / "agent-weather"
    agent_dir.mkdir(parents=True)
    (agent_dir / "plugin.yaml").write_text("name: agent-weather\n")

    state = _collect(hermes_home)

    assert [plugin.name for plugin in state.skills_memory.desktop_plugins] == ["desktop-weather"]
    assert [plugin.name for plugin in state.skills_memory.plugins] == ["agent-weather"]


def test_desktop_inventory_is_root_scoped_when_a_profile_is_selected(hermes_home: Path) -> None:
    _write_desktop_plugin(hermes_home, "root-plugin")
    profile_home = hermes_home / "profiles" / "coding"
    for name in ("logs", "sessions", "skills", "memories", "cron/output"):
        (profile_home / name).mkdir(parents=True, exist_ok=True)
    _write_desktop_plugin(profile_home, "profile-plugin")

    state = _collect(hermes_home, profile_name="coding")

    assert [plugin.name for plugin in state.skills_memory.desktop_plugins] == ["root-plugin"]


def test_absent_desktop_root_is_an_empty_healthy_inventory(hermes_home: Path) -> None:
    state = _collect(hermes_home)

    assert state.skills_memory.desktop_plugins == []
    assert state.skills_memory.desktop_plugin_scan_truncated is False
    assert "desktop_plugins" not in state.health.failed_sources


def test_unsafe_desktop_root_is_a_failed_source_without_a_baseline(
    hermes_home: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    _write_desktop_plugin(outside, "escaped")
    (hermes_home / "desktop-plugins").symlink_to(
        outside / "desktop-plugins", target_is_directory=True
    )

    with pytest.raises(RuntimeError, match="unsafe"):
        read_desktop_plugins(hermes_home / "desktop-plugins", hermes_home)

    state = _collect(hermes_home)

    assert state.skills_memory.desktop_plugins == []
    assert "desktop_plugins" in state.health.failed_sources


def test_unsafe_desktop_entries_and_entry_files_are_refused(
    hermes_home: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    _write_desktop_plugin(outside, "escaped")

    desktop_root = hermes_home / "desktop-plugins"
    desktop_root.mkdir()
    (desktop_root / "linked-dir").symlink_to(
        outside / "desktop-plugins" / "escaped", target_is_directory=True
    )
    local = desktop_root / "linked-file"
    local.mkdir()
    (local / "plugin.js").symlink_to(outside / "desktop-plugins" / "escaped" / "plugin.js")
    directory_entry = desktop_root / "directory-entry" / "plugin.js"
    directory_entry.mkdir(parents=True)

    assert read_desktop_plugins(desktop_root, hermes_home) == ([], False)


def test_unsafe_root_after_a_good_read_keeps_last_good_inventory(
    hermes_home: Path, tmp_path: Path
) -> None:
    _write_desktop_plugin(hermes_home, "weather")
    desktop_root = hermes_home / "desktop-plugins"
    collector = Collector(hermes_home)
    try:
        first = collector.collect()
        renamed_root = hermes_home / "desktop-plugins-old"
        desktop_root.rename(renamed_root)
        outside = tmp_path / "outside"
        _write_desktop_plugin(outside, "escaped")
        desktop_root.symlink_to(outside / "desktop-plugins", target_is_directory=True)

        second = collector.collect()
    finally:
        collector.close()

    assert first.skills_memory.desktop_plugins == [DesktopPluginInfo(name="weather")]
    assert second.skills_memory.desktop_plugins == first.skills_memory.desktop_plugins
    assert "desktop_plugins" in second.health.failed_sources


def test_desktop_inventory_is_bounded_and_marks_truncation(hermes_home: Path) -> None:
    for index in range(DESKTOP_PLUGIN_LIMIT + 1):
        _write_desktop_plugin(hermes_home, f"plugin-{index:03d}")

    plugins, truncated = read_desktop_plugins(hermes_home / "desktop-plugins", hermes_home)

    assert len(plugins) == DESKTOP_PLUGIN_LIMIT
    assert truncated is True


def test_desktop_read_failure_keeps_last_good_inventory_and_has_own_health_source(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_desktop_plugin(hermes_home, "weather")
    desktop_root = hermes_home / "desktop-plugins"
    collector = Collector(hermes_home)
    original_iterdir = Path.iterdir
    try:
        first = collector.collect()

        def fail_desktop_root(path: Path):
            if path == desktop_root:
                raise PermissionError("desktop inventory temporarily unreadable")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", fail_desktop_root)
        second = collector.collect()
    finally:
        collector.close()

    assert first.skills_memory.desktop_plugins == [DesktopPluginInfo(name="weather")]
    assert second.skills_memory.desktop_plugins == first.skills_memory.desktop_plugins
    assert "desktop_plugins" in second.health.failed_sources
    assert "skills" not in second.health.failed_sources


def test_detail_renderer_labels_desktop_inventory_without_claiming_load_state() -> None:
    state = DashboardState(
        skills_memory=SkillsMemory(
            desktop_plugins=[DesktopPluginInfo(name="[bold]weather\x1b[2J")],
            desktop_plugin_scan_truncated=True,
        )
    )

    text = render_to_str(render_panel(7, state, Theme(), detail=True), width=160, no_color=True)

    assert "Desktop Plugins" in text
    assert "plugin.js present" in text
    assert "inventory only" in text.lower()
    assert "cannot observe enabled or loaded state" in text.lower()
    assert "truncated" in text.lower()
    assert "[bold]weather" in text
    assert "\x1b[2J" not in text


def test_compact_renderer_distinguishes_agent_and_desktop_plugin_counts() -> None:
    state = DashboardState(
        skills_memory=SkillsMemory(
            desktop_plugins=[DesktopPluginInfo(name="desktop-one")],
            desktop_plugin_scan_truncated=True,
        )
    )

    text = render_to_str(render_panel(7, state, Theme()), width=120, no_color=True)

    assert "0 plug (agent)" in text
    assert "1+ plug (desktop)" in text


def test_json_snapshot_accepts_the_separate_desktop_inventory(hermes_home: Path) -> None:
    _write_desktop_plugin(hermes_home, "weather")
    app = DashboardApp(hermes_home, no_color=True)
    try:
        payload = json.loads(app.render_snapshot_json())
    finally:
        app.close()

    assert payload["state"]["skills_memory"]["desktop_plugins"] == [{"name": "weather"}]
    assert payload["state"]["skills_memory"]["desktop_plugin_scan_truncated"] is False
