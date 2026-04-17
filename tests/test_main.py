import argparse
import json
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


def test_parse_args_snapshot_format_and_log_tail_bytes():
    args = parse_args(["--snapshot-format", "json", "--log-tail-bytes", "4096"])
    assert args.snapshot_format == "json"
    assert args.log_tail_bytes == 4096


@pytest.mark.parametrize("value", ["11", "-1"])
def test_parse_args_rejects_invalid_snapshot_panel(value: str):
    with pytest.raises(SystemExit):
        parse_args(["--snapshot-panel", value])


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


def test_parse_args_version(capsys):
    from hermesd import __version__

    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "hermesd" in out
    assert __version__ in out
