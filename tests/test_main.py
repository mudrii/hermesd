from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermesd.__main__ import _positive_int, main, parse_args, resolve_hermes_home


def test_parse_args_defaults():
    args = parse_args([])
    assert args.hermes_home is None
    assert args.refresh_rate == 5
    assert args.no_color is False
    assert args.snapshot is False


def test_parse_args_custom():
    args = parse_args(["--hermes-home", "/tmp/h", "--refresh-rate", "10", "--no-color"])
    assert args.hermes_home == Path("/tmp/h")
    assert args.refresh_rate == 10
    assert args.no_color is True


def test_parse_args_snapshot():
    args = parse_args(["--snapshot"])
    assert args.snapshot is True


def test_parse_args_snapshot_file():
    args = parse_args(["--snapshot-file", "/tmp/hermesd.txt"])
    assert args.snapshot_file == Path("/tmp/hermesd.txt")


def test_parse_args_snapshot_panel():
    args = parse_args(["--snapshot-panel", "10"])
    assert args.snapshot_panel == 10


def test_parse_args_snapshot_panel_zero_alias():
    args = parse_args(["--snapshot-panel", "0"])
    assert args.snapshot_panel == 10


@pytest.mark.parametrize("value", ["11", "12", "13"])
def test_parse_args_snapshot_panel_above_ten(value: str):
    args = parse_args(["--snapshot-panel", value])
    assert args.snapshot_panel == int(value)


def test_parse_args_snapshot_format_and_log_tail_bytes():
    args = parse_args(["--snapshot-format", "json", "--log-tail-bytes", "4096"])
    assert args.snapshot_format == "json"
    assert args.log_tail_bytes == 4096


@pytest.mark.parametrize("value", ["14", "-1"])
def test_parse_args_rejects_invalid_snapshot_panel(value: str):
    with pytest.raises(SystemExit):
        parse_args(["--snapshot-panel", value])


def test_parse_args_invalid_snapshot_panel_lists_available_panels(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--snapshot-panel", "14"])

    err = capsys.readouterr().err
    assert "snapshot panel must be one of:" in err
    assert "10" in err
    assert "11" in err
    assert "12" in err
    assert "13" in err


def test_parse_args_help_describes_registered_snapshot_panels(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Select a panel by number" in out
    assert "0 aliases panel 10" in out
    assert "1-9 or 0" not in out


@pytest.mark.parametrize("value", ["0", "-1"])
def test_parse_args_rejects_non_positive_refresh_rate(value: str):
    with pytest.raises(SystemExit):
        parse_args(["--refresh-rate", value])


@pytest.mark.parametrize("value", ["0", "-1"])
def test_parse_args_rejects_non_positive_log_tail_bytes(value: str):
    with pytest.raises(SystemExit):
        parse_args(["--log-tail-bytes", value])


def test_positive_int_error_message_is_generic():
    with pytest.raises(argparse.ArgumentTypeError, match="value must be a positive integer"):
        _positive_int("-1")


def test_resolve_hermes_home_explicit():
    args = parse_args(["--hermes-home", "/tmp/test-hermes"])
    path = resolve_hermes_home(args)
    assert path == Path("/tmp/test-hermes")


def test_resolve_hermes_home_env(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    args = parse_args([])
    monkeypatch.setenv("HERMES_HOME", "/tmp/env-hermes")
    path = resolve_hermes_home(args)
    assert path == Path("/tmp/env-hermes")


def test_resolve_hermes_home_default(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    args = parse_args([])
    path = resolve_hermes_home(args)
    assert path == Path.home() / ".hermes"


def test_main_exits_on_missing_dir(tmp_path):
    missing = tmp_path / "nonexistent"
    with pytest.raises(SystemExit) as exc:
        main(["--hermes-home", str(missing)])
    assert exc.value.code == 1


def test_main_exits_on_missing_profile(populated_hermes_home: Path):
    with pytest.raises(SystemExit) as exc:
        main(["--hermes-home", str(populated_hermes_home), "--profile", "missing"])
    assert exc.value.code == 1


def test_main_snapshot_outputs_overview(populated_hermes_home: Path, capsys):
    main(["--hermes-home", str(populated_hermes_home), "--snapshot", "--no-color"])
    out = capsys.readouterr().out
    assert "Gateway & Platforms" in out
    assert "Sessions" in out
    assert "Logs" in out


def test_main_snapshot_stdout_renders_once(populated_hermes_home: Path, capsys, monkeypatch):
    from hermesd import __main__ as main_module

    render_count = 0
    closed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            nonlocal render_count
            render_count += 1
            return "snapshot"

        def render_snapshot_json(self, panel_num=None):
            raise AssertionError("json renderer should not be used")

        def render_snapshot(self, panel_num=None):
            raise AssertionError("stdout path should print the captured snapshot text")

        def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    main_module.main(["--hermes-home", str(populated_hermes_home), "--snapshot", "--no-color"])

    assert capsys.readouterr().out == "snapshot"
    assert render_count == 1
    assert closed is True


def test_main_snapshot_file_writes_output(populated_hermes_home: Path, tmp_path: Path):
    output_path = tmp_path / "snapshot.txt"
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-file",
            str(output_path),
            "--no-color",
        ]
    )
    text = output_path.read_text()
    assert "Gateway & Platforms" in text
    assert "Memory" in text


def test_main_rejects_snapshot_file_under_hermes_home(populated_hermes_home: Path):
    output_path = populated_hermes_home / "snapshot.txt"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--hermes-home",
                str(populated_hermes_home),
                "--snapshot-file",
                str(output_path),
                "--no-color",
            ]
        )

    assert exc.value.code == 1
    assert not output_path.exists()


def test_main_closes_snapshot_app_when_file_write_fails(
    populated_hermes_home: Path,
    tmp_path: Path,
    monkeypatch,
):
    from hermesd import __main__ as main_module

    closed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            return "snapshot"

        def close(self):
            nonlocal closed
            closed = True

    def fail_write_text(self: Path, text: str):
        raise OSError("disk full")

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)
    monkeypatch.setattr(Path, "write_text", fail_write_text)

    with pytest.raises(OSError, match="disk full"):
        main_module.main(
            [
                "--hermes-home",
                str(populated_hermes_home),
                "--snapshot-file",
                str(tmp_path / "snapshot.txt"),
                "--no-color",
            ]
        )

    assert closed is True


def test_main_snapshot_panel_outputs_detail(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "10",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "[10] Memory" in out
    assert "SOUL.md" in out


def test_main_snapshot_panel_11_outputs_kanban_detail(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "11",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "[11] Kanban" in out
    assert "Status Counts" in out


def test_main_snapshot_panel_file_writes_detail(populated_hermes_home: Path, tmp_path: Path):
    output_path = tmp_path / "memory-panel.txt"
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "2",
            "--snapshot-file",
            str(output_path),
            "--no-color",
        ]
    )
    text = output_path.read_text()
    assert "[2] Sessions" in text
    assert "sess_001" in text


def test_main_snapshot_panel_2_surfaces_billing_context_summary(
    populated_hermes_home: Path, capsys
):
    conn = sqlite3.connect(str(populated_hermes_home / "state.db"))
    conn.execute(
        "UPDATE sessions SET model = ?, billing_base_url = ?, input_tokens = ?, output_tokens = ?",
        ("MiniMax-M3", "https://api.minimax.io/anthropic", 50_000, 4_000),
    )
    conn.commit()
    conn.close()
    (populated_hermes_home / "context_length_cache.yaml").write_text(
        "context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 1048576\n"
    )

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "2",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "Billing & Context" in out
    assert "Lifetime / Limit" in out


def test_main_snapshot_panel_3_surfaces_endpoint_and_cost_status_summary(
    populated_hermes_home: Path, capsys
):
    conn = sqlite3.connect(str(populated_hermes_home / "state.db"))
    conn.execute(
        "UPDATE sessions SET billing_base_url = ?, cost_status = ?",
        ("https://api.minimax.io/anthropic", "estimated"),
    )
    conn.commit()
    conn.close()

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "3",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "Cost Status" in out
    assert "By Endpoint" in out


def test_main_snapshot_json_outputs_state(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-format",
            "json",
            "--no-color",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["panel_num"] is None
    assert payload["state"]["gateway"]["state"] == "running"
    assert payload["state"]["memory"]["memory_file_count"] >= 1


def test_main_snapshot_panel_json_file(populated_hermes_home: Path, tmp_path: Path):
    output_path = tmp_path / "panel.json"
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "8",
            "--snapshot-format",
            "json",
            "--snapshot-file",
            str(output_path),
            "--no-color",
        ]
    )
    payload = json.loads(output_path.read_text())
    assert payload["panel_num"] == 8
    assert payload["panel_name"] == "Logs"
    assert "logs" in payload["state"]


def test_main_snapshot_panel_12_json_annotates_operations(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "12",
            "--snapshot-format",
            "json",
            "--no-color",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["panel_num"] == 12
    assert payload["panel_name"] == "Operations"
    assert "operations" in payload["state"]


def test_main_snapshot_panel_12_outputs_operations_detail(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "12",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "[12] Operations" in out
    assert "Desktop Build" in out


def test_main_snapshot_panel_13_outputs_curator_detail(populated_hermes_home: Path, capsys):
    run_dir = populated_hermes_home / "logs" / "curator" / "20260610-133539"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "started_at": "2026-06-10T13:35:39+00:00",
                "model": "MiniMax-M3",
                "provider": "minimax",
                "counts": {"before": 8, "after": 5, "tool_calls_total": 67},
            }
        )
    )

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "13",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "[13] Curator" in out
    assert "MiniMax-M3" in out


def test_main_runs_dashboard_when_no_snapshot_flags(populated_hermes_home: Path, monkeypatch):
    from hermesd import __main__ as main_module

    ran = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def run(self):
            nonlocal ran
            ran = True

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    main_module.main(["--hermes-home", str(populated_hermes_home), "--no-color"])

    assert ran is True


def test_python_dash_m_entry_point(populated_hermes_home: Path):
    """`python -m hermesd` must reach main() (the __main__ guard)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "hermesd", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "hermesd" in result.stdout


def test_version_falls_back_when_package_metadata_missing(monkeypatch):
    import importlib
    from importlib.metadata import PackageNotFoundError

    import hermesd

    def missing_package(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", missing_package)
    try:
        reloaded = importlib.reload(hermesd)
        assert reloaded.__version__ == "0.0.0"
    finally:
        monkeypatch.undo()
        importlib.reload(hermesd)


def test_parse_args_version(capsys):
    from hermesd import __version__

    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "hermesd" in out
    assert __version__ in out
