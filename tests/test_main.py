from pathlib import Path

import pytest

from hermesd.__main__ import main, parse_args, resolve_hermes_home


def test_parse_args_defaults():
    args = parse_args([])
    assert args.hermes_home is None
    assert args.refresh_rate == 5
    assert args.no_color is False


def test_parse_args_custom():
    args = parse_args(["--hermes-home", "/tmp/h", "--refresh-rate", "10", "--no-color"])
    assert args.hermes_home == Path("/tmp/h")
    assert args.refresh_rate == 10
    assert args.no_color is True


@pytest.mark.parametrize("value", ["0", "-1"])
def test_parse_args_rejects_non_positive_refresh_rate(value: str):
    with pytest.raises(SystemExit):
        parse_args(["--refresh-rate", value])


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


def test_parse_args_version(capsys):
    from hermesd import __version__

    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "hermesd" in out
    assert __version__ in out
