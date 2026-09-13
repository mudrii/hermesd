"""Tests for the [5] Config panel."""

from __future__ import annotations

from hermesd.models import ConfigBackupGroup, ConfigSummary, DashboardState
from hermesd.panels.config_panel import render_config
from hermesd.theme import Theme
from tests.conftest import render_to_str


def test_config_detail_escapes_code_execution_mode() -> None:
    state = DashboardState(config=ConfigSummary(code_execution_mode="yolo [/] inj"))
    rendered = render_to_str(render_config(state, Theme(), detail=True))
    assert "yolo [/] inj" in rendered


def test_config_detail_escapes_moa_label_values() -> None:
    state = DashboardState(
        config=ConfigSummary(
            moa_active_preset="preset [/] x",
            moa_preset_count=2,
            moa_aggregator_label="agg [/] y",
        )
    )
    rendered = render_to_str(render_config(state, Theme(), detail=True))
    assert "preset [/] x" in rendered
    assert "agg [/] y" in rendered


def _agent_limits_config() -> ConfigSummary:
    return ConfigSummary(
        delegation_max_concurrent_children=10,
        delegation_max_spawn_depth=2,
        delegation_orchestrator_enabled=True,
        goals_max_turns=20,
        updates_check=True,
        updates_pre_update_backup="quick",
        updates_backup_keep=5,
        mcp_server_count=2,
        mcp_server_names=["playwright", "sheets"],
        plugin_enabled_count=3,
        plugin_disabled_count=1,
        tool_loop_warnings_enabled=True,
        tool_loop_hard_stop_enabled=True,
        max_live_sessions=8,
        streaming_enabled=True,
        logging_level="INFO",
        network_proxy_configured=True,
    )


def _render(config: ConfigSummary, detail: bool) -> str:
    state = DashboardState(config=config)
    return render_to_str(render_config(state, Theme(), detail=detail), width=200, no_color=True)


def test_config_detail_shows_agent_limits_and_integrations() -> None:
    text = _render(_agent_limits_config(), detail=True)

    assert "Agent limits" in text
    assert "Integrations" in text
    assert "children 10" in text
    assert "depth 2" in text
    assert "max turns 20" in text
    assert "warn" in text
    assert "hard-stop" in text
    assert "INFO" in text
    assert "playwright" in text
    assert "sheets" in text
    assert "quick" in text
    assert "keep 5" in text
    assert "3 enabled" in text
    assert "1 disabled" in text


def test_config_detail_empty_sections_render_placeholder() -> None:
    text = _render(ConfigSummary(), detail=True)

    assert "Agent limits" in text
    assert "Integrations" in text
    assert "—" in text
    assert "playwright" not in text


def test_config_compact_shows_integrations_line() -> None:
    text = _render(_agent_limits_config(), detail=False)

    assert "mcp 2" in text
    assert "plugins 3" in text
    assert "goals 20" in text


def test_config_compact_shows_goals_placeholder_when_unset() -> None:
    text = _render(ConfigSummary(), detail=False)

    assert "mcp 0" in text
    assert "goals —" in text


def test_config_detail_escapes_markup_hostile_server_names() -> None:
    config = ConfigSummary(
        mcp_server_count=1,
        mcp_server_names=["[bold red]evil\x1b[2J"],
        logging_level="[blink]DEBUG",
    )
    text = _render(config, detail=True)

    assert "[bold red]evil" in text
    assert "\x1b[2J" not in text
    assert "[blink]DEBUG" in text


def test_config_compact_never_blanks_with_hostile_names() -> None:
    config = ConfigSummary(mcp_server_count=1, mcp_server_names=["\x1b]0;pwn\x07x"])
    text = _render(config, detail=False)

    assert "Config" in text
    assert "\x1b]0;" not in text


def _backup_config(groups: list[ConfigBackupGroup], **overrides: object) -> ConfigSummary:
    fields: dict[str, object] = {
        "config_backups_present": True,
        "config_backup_groups": groups,
    }
    fields.update(overrides)
    return ConfigSummary(**fields)


def test_config_detail_shows_backup_audit_trail() -> None:
    groups = [
        ConfigBackupGroup(
            reason="good",
            kind="good",
            count=2,
            newest_stamp="20260907-143000",
            newest_age_seconds=3600.0,
        ),
        ConfigBackupGroup(reason="corrupt", kind="corrupt", count=2),
        ConfigBackupGroup(
            reason="pre-setup",
            kind="setup",
            count=1,
            newest_stamp="20260901-101010",
        ),
        ConfigBackupGroup(
            reason="pre-migrate-xai",
            kind="migration",
            count=1,
            newest_stamp="20260902-111111",
        ),
    ]
    text = _render(_backup_config(groups), detail=True)

    assert "Config Backups" in text
    assert "Last changed" in text
    assert "20260907-143000" in text
    assert "Corrupt snapshots" in text
    assert "2" in text
    assert "pre-setup" in text
    assert "pre-migrate-xai" in text


def test_config_detail_backup_alert_and_placeholder() -> None:
    corrupt = ConfigBackupGroup(reason="corrupt", kind="corrupt", count=3)
    good = ConfigBackupGroup(reason="good", kind="good", count=1, newest_stamp="20260907-143000")
    alert_text = _render(_backup_config([good, corrupt]), detail=True)
    calm_text = _render(_backup_config([good]), detail=True)

    assert "3" in alert_text
    assert "none" in calm_text


def test_config_detail_backups_absent_dir_renders_honestly() -> None:
    text = _render(_backup_config([], config_backups_present=False), detail=True)

    assert "no backups directory observed" in text


def test_config_detail_marks_truncated_backup_groups() -> None:
    good = ConfigBackupGroup(reason="good", kind="good", count=1, newest_stamp="20260907-143000")
    text = _render(_backup_config([good], config_backup_groups_truncated=True), detail=True)

    assert "truncated" in text


def test_config_detail_escapes_backup_reasons() -> None:
    hostile = ConfigBackupGroup(
        reason="[bold]evil [/] reason",
        kind="other",
        count=1,
        newest_stamp="20260907-143000",
    )
    text = _render(_backup_config([hostile]), detail=True)

    assert "[bold]evil [/] reason" in text


def test_config_compact_shows_backup_stamp_and_corrupt_alert() -> None:
    good = ConfigBackupGroup(reason="good", kind="good", count=1, newest_stamp="20260907-143000")
    corrupt = ConfigBackupGroup(reason="corrupt", kind="corrupt", count=2)
    with_alert = _render(_backup_config([good, corrupt]), detail=False)
    calm = _render(_backup_config([good]), detail=False)
    absent = _render(_backup_config([], config_backups_present=False), detail=False)

    assert "Backups:" in with_alert
    assert "20260907-143000" in with_alert
    assert "corrupt 2" in with_alert
    assert "corrupt 2" not in calm
    assert "—" in absent


def _row_line(text: str, key: str) -> str:
    for line in text.splitlines():
        if key in line:
            return line
    raise AssertionError(f"{key!r} not rendered")


def test_config_backup_rows_survive_the_group_cap() -> None:
    """Under cap pressure the panel must still show the good stamp and the
    corrupt count, not a false all-clear."""
    from hermesd.collect.config import _CONFIG_BACKUP_GROUP_LIMIT, _config_backup_groups

    names = [
        f"config.yaml.aaa-{index:03d}.20260907-1430{index:02d}"
        for index in range(_CONFIG_BACKUP_GROUP_LIMIT)
    ]
    names += [
        "config.yaml.good.20260907-143000",
        "config.yaml.corrupt.20260906-090000",
    ]
    groups, truncated = _config_backup_groups(names, now=1_783_000_000.0)
    assert truncated is True

    config = _backup_config(groups, config_backup_groups_truncated=True)
    detail = _render(config, detail=True)
    compact = _render(config, detail=False)

    assert "20260907-143000" in _row_line(detail, "Last changed")
    assert "no good copy" not in detail
    assert "none" not in _row_line(detail, "Corrupt snapshots")
    assert "1" in _row_line(detail, "Corrupt snapshots")
    assert "20260907-143000" in compact
    assert "corrupt 1" in compact


def test_config_compact_flags_truncated_backup_groups() -> None:
    good = ConfigBackupGroup(reason="good", kind="good", count=1, newest_stamp="20260907-143000")
    truncated = _render(_backup_config([good], config_backup_groups_truncated=True), detail=False)
    calm = _render(_backup_config([good]), detail=False)

    assert "truncated" in truncated
    assert "truncated" not in calm
