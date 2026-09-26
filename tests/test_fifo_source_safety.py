"""Non-regular files in place of state files must never hang collection.

Opening a FIFO that has no writer blocks forever, so every state-file read has
to refuse non-regular files at the open boundary. These tests exercise the CLI
module entry point (``python -m hermesd``) from this checkout against synthetic
homes with FIFOs at the known read points, plus Collector-level scenarios —
degraded-source and last-good behavior included — run in bounded child
processes so a regressed (blocking) guard can never hang the pytest process
itself. Every subprocess run carries a timeout, which kills and reaps a wedged
child instead of leaving it behind.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

posix_only = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")

# A healthy snapshot against a tiny synthetic home takes about a second; the
# margin covers slow CI hosts while still catching a blocking open.
_CHILD_TIMEOUT_SECONDS = 60

_REPO_ROOT = Path(__file__).resolve().parents[1]

_CHILD_ENTRY = (
    "import sys; from tests.test_fifo_source_safety import _run_scenario;"
    " _run_scenario(sys.argv[1], sys.argv[2])"
)


def _run_snapshot(home: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    # subprocess.run kills and reaps the child when the timeout fires, so a
    # regression fails the test instead of leaving a hung process behind.
    return subprocess.run(
        [sys.executable, "-m", "hermesd", "--hermes-home", str(home), *extra],
        capture_output=True,
        text=True,
        check=False,
        timeout=_CHILD_TIMEOUT_SECONDS,
    )


def _run_child_scenario(scenario: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _CHILD_ENTRY, scenario, str(home)],
        capture_output=True,
        text=True,
        check=False,
        cwd=_REPO_ROOT,
        timeout=_CHILD_TIMEOUT_SECONDS,
    )


# --- CLI-level: a full snapshot completes with a FIFO at any read point ------


@posix_only
@pytest.mark.parametrize("marker", ["catch_up_occurrences", "ticker_last_error"])
def test_snapshot_completes_when_cron_marker_is_a_fifo(hermes_home: Path, marker: str) -> None:
    """One FIFO per child: a failure reading one marker must not shield the other."""
    os.mkfifo(hermes_home / "cron" / marker)

    result = _run_snapshot(hermes_home, "--snapshot", "--no-color")

    assert result.returncode == 0, result.stderr


@posix_only
def test_snapshot_completes_when_kanban_current_is_a_fifo(hermes_home: Path) -> None:
    (hermes_home / "kanban").mkdir()
    os.mkfifo(hermes_home / "kanban" / "current")

    result = _run_snapshot(hermes_home, "--snapshot", "--no-color")

    assert result.returncode == 0, result.stderr


@posix_only
def test_snapshot_completes_when_pyproject_is_a_fifo(hermes_home: Path) -> None:
    """A non-empty gateway_state.json triggers the hermes-agent version read."""
    (hermes_home / "hermes-agent").mkdir()
    os.mkfifo(hermes_home / "hermes-agent" / "pyproject.toml")
    (hermes_home / "gateway_state.json").write_text('{"pid": 0, "state": "stopped"}')

    result = _run_snapshot(hermes_home, "--snapshot", "--no-color")

    assert result.returncode == 0, result.stderr


# --- Collector-level scenarios, each run in a bounded child ------------------
#
# Assertions live inside the child: a failed assert exits non-zero, and a
# regressed guard that blocks on the FIFO is killed by the parent's timeout.


def _scenario_cron_catch_up_fifo(home: Path) -> None:
    from hermesd.collector import Collector

    os.mkfifo(home / "cron" / "catch_up_occurrences")
    collector = Collector(home)
    try:
        state = collector.collect()
    finally:
        collector.close()
    assert "cron" in state.health.failed_sources
    assert state.cron.catch_up_occurrences == 0
    assert state.cron.catch_up_occurrences_recorded is False


def _scenario_cron_ticker_fifo(home: Path) -> None:
    from hermesd.collector import Collector

    os.mkfifo(home / "cron" / "ticker_last_error")
    collector = Collector(home)
    try:
        state = collector.collect()
    finally:
        collector.close()
    assert "cron" in state.health.failed_sources
    assert state.cron.ticker_last_error == ""


def _scenario_kanban_current_fifo(home: Path) -> None:
    """A FIFO replacing kanban/current after a good read keeps the last-good board."""
    from hermesd.collector import Collector

    kanban_dir = home / "kanban"
    kanban_dir.mkdir(exist_ok=True)
    current = kanban_dir / "current"
    current.write_text("root\n")
    collector = Collector(home)
    try:
        assert collector.collect().kanban.current_board == "root"
        current.unlink()
        os.mkfifo(current)
        second = collector.collect()
    finally:
        collector.close()
    assert "kanban" in second.health.failed_sources
    assert second.kanban.current_board == "root"


def _scenario_pyproject_version_fifo(home: Path) -> None:
    """Good -> FIFO -> recovered: a failed version read degrades the gateway
    source to its last-good state instead of blanking the version."""
    from hermesd.collector import Collector

    agent_dir = home / "hermes-agent"
    agent_dir.mkdir(exist_ok=True)
    pyproject = agent_dir / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "1.2.3"\n')
    (home / "gateway_state.json").write_text(
        json.dumps({"pid": 4242, "gateway_state": "running", "platforms": {}})
    )
    collector = Collector(home, pid_exists=lambda pid: pid == 4242)
    try:
        first = collector.collect()
        assert "gateway" not in first.health.failed_sources
        assert first.gateway.hermes_version == "1.2.3"

        pyproject.unlink()
        os.mkfifo(pyproject)
        second = collector.collect()
        assert "gateway" in second.health.failed_sources
        assert second.gateway.hermes_version == "1.2.3"  # last-good gateway state

        pyproject.unlink()
        pyproject.write_text('[project]\nversion = "4.5.6"\n')
        third = collector.collect()
        assert "gateway" not in third.health.failed_sources
        assert third.gateway.hermes_version == "4.5.6"
    finally:
        collector.close()


def _scenario_strict_reader_refuses_fifo(home: Path) -> None:
    """The strict capped reader refuses a FIFO at the open boundary."""
    from hermesd.collect.common import _read_text_capped_strict

    fifo = home / "cron" / "ticker_last_error"
    os.mkfifo(fifo)
    try:
        _read_text_capped_strict(fifo)
    except OSError:
        return
    raise AssertionError("expected OSError for a FIFO")


def _scenario_marker_swapped_for_fifo_mid_open(home: Path) -> None:
    """A regular->FIFO swap at the real open boundary must not wedge collection.

    Patches ``os.open`` (the descriptor boundary used by ``_open_regular_file``)
    so the marker is replaced by a FIFO immediately before the descriptor is
    taken; the nonblocking open plus fstat validation must refuse it instead of
    blocking on a writer that never comes.
    """
    import hermesd.collect.common as common
    from hermesd.collector import Collector

    marker = home / "cron" / "catch_up_occurrences"
    marker.write_text("3")
    real_os_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, **kwargs):
        nonlocal swapped
        if Path(path) == marker and not swapped:
            swapped = True
            marker.unlink()
            os.mkfifo(marker)
        return real_os_open(path, flags, mode, **kwargs)

    common.os.open = swapping_open  # child process: the global os module is disposable
    try:
        collector = Collector(home)
        try:
            state = collector.collect()
        finally:
            collector.close()
    finally:
        common.os.open = real_os_open
    assert swapped, "the marker open was never intercepted"
    assert "cron" in state.health.failed_sources


_SCENARIOS = {
    "cron-catch-up-fifo": _scenario_cron_catch_up_fifo,
    "cron-ticker-fifo": _scenario_cron_ticker_fifo,
    "kanban-current-fifo": _scenario_kanban_current_fifo,
    "pyproject-version-fifo": _scenario_pyproject_version_fifo,
    "strict-reader-fifo": _scenario_strict_reader_refuses_fifo,
    "swap-mid-open": _scenario_marker_swapped_for_fifo_mid_open,
}


def _run_scenario(name: str, home: str) -> None:
    """Child-process entry point; see _CHILD_ENTRY."""
    try:
        scenario = _SCENARIOS[name]
    except KeyError:
        raise SystemExit(f"unknown FIFO scenario: {name}") from None
    scenario(Path(home))


@posix_only
@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_fifo_collector_scenario_completes_degraded(hermes_home: Path, scenario: str) -> None:
    result = _run_child_scenario(scenario, hermes_home)

    assert result.returncode == 0, result.stderr
