"""F04 — active-session liveness must verify process identity, not just a pid.

Pids are reused, so "a process with this pid exists" is not evidence that the
recorded session is still running. The registry records ``process_start_time`` in
epoch seconds; hermesd compares it against the start time observed for that pid
on this host and reports live, dead, or unverifiable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import hermesd.collect.system as system_module
from hermesd.collect.system import (
    _observed_process_start_times,
    _parse_lstart,
    _proc_start_times,
    _ps_start_times,
    _surface_liveness,
)
from hermesd.collector import Collector
from hermesd.models import ActiveSurface, ProcessLiveness

_LIVE_PIDS = frozenset({4242, 9999})


def _pid_exists(pid: int) -> bool:
    return pid in _LIVE_PIDS


# --------------------------------------------------------------------------
# the pure three-state verdict
# --------------------------------------------------------------------------


def test_matching_owner_is_live():
    verdict = _surface_liveness(4242, 1000.0, {4242: 1000.0}, _pid_exists)

    assert verdict is ProcessLiveness.LIVE


def test_reused_pid_is_dead_not_live():
    """The pid exists, but a different process owns it now."""
    verdict = _surface_liveness(4242, 1000.0, {4242: 5000.0}, _pid_exists)

    assert verdict is ProcessLiveness.DEAD


def test_exited_owner_is_dead():
    verdict = _surface_liveness(1234, 1000.0, {1234: 1000.0}, _pid_exists)

    assert verdict is ProcessLiveness.DEAD


def test_missing_recorded_metadata_is_unverifiable():
    """An older registry entry carries no start time; that is not proof of death."""
    verdict = _surface_liveness(4242, None, {4242: 1000.0}, _pid_exists)

    assert verdict is ProcessLiveness.UNVERIFIABLE


def test_unobservable_start_time_is_unverifiable():
    """The pid is alive but this host cannot read its start time."""
    verdict = _surface_liveness(4242, 1000.0, {}, _pid_exists)

    assert verdict is ProcessLiveness.UNVERIFIABLE


def test_entry_without_a_pid_is_dead():
    assert _surface_liveness(0, 1000.0, {0: 1000.0}, _pid_exists) is ProcessLiveness.DEAD


@pytest.mark.parametrize("drift", [0.0, 0.5, 1.0, 2.0, -1.0, -2.0])
def test_start_time_within_the_probe_tolerance_is_live(drift: float):
    """`ps -o lstart=` only reports whole seconds, so exact equality is too strict."""
    verdict = _surface_liveness(4242, 1000.0, {4242: 1000.0 + drift}, _pid_exists)

    assert verdict is ProcessLiveness.LIVE


@pytest.mark.parametrize("drift", [2.5, -2.5, 60.0])
def test_start_time_beyond_the_tolerance_is_dead(drift: float):
    verdict = _surface_liveness(4242, 1000.0, {4242: 1000.0 + drift}, _pid_exists)

    assert verdict is ProcessLiveness.DEAD


def test_permission_denied_pid_with_no_probe_is_unverifiable():
    """os.kill raises PermissionError for a live process we do not own."""

    def exists(pid: int) -> bool:
        try:
            import os

            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    assert exists(1) is True
    assert _surface_liveness(1, 1000.0, {}, exists) is ProcessLiveness.UNVERIFIABLE


# --------------------------------------------------------------------------
# observing start times without psutil
# --------------------------------------------------------------------------


def test_parse_lstart_reads_a_real_ps_timestamp():
    parsed = _parse_lstart("Tue Sep  8 09:59:12 2026")

    assert parsed is not None
    assert parsed == pytest.approx(
        system_module.time.mktime(
            system_module.time.strptime("Tue Sep  8 09:59:12 2026", "%a %b %d %H:%M:%S %Y")
        )
    )


def test_parse_lstart_rejects_garbage():
    assert _parse_lstart("not-a-timestamp") is None
    assert _parse_lstart("") is None


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_ps_start_times_parses_every_row(monkeypatch: pytest.MonkeyPatch):
    def fake_run(*_args, **_kwargs):
        return _Completed("4242 Tue Sep  8 09:59:12 2026\n9999 Wed Sep  9 10:00:00 2026\n")

    monkeypatch.setattr(system_module.subprocess, "run", fake_run)

    observed = _ps_start_times([4242, 9999])

    assert sorted(observed) == [4242, 9999]
    assert observed[4242] == pytest.approx(_parse_lstart("Tue Sep  8 09:59:12 2026"))


def test_ps_start_times_tolerates_a_nonzero_exit(monkeypatch: pytest.MonkeyPatch):
    """ps exits 1 when some pids have already gone; the survivors still count."""

    def fake_run(*_args, **_kwargs):
        return _Completed("4242 Tue Sep  8 09:59:12 2026\n", returncode=1)

    monkeypatch.setattr(system_module.subprocess, "run", fake_run)

    assert sorted(_ps_start_times([4242, 7777])) == [4242]


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("no ps"), subprocess.TimeoutExpired(cmd="ps", timeout=2), OSError("boom")],
)
def test_ps_start_times_degrades_to_empty(monkeypatch: pytest.MonkeyPatch, error: Exception):
    def fake_run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(system_module.subprocess, "run", fake_run)

    assert _ps_start_times([4242]) == {}


def test_ps_start_times_is_bounded_by_a_timeout(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}

    def fake_run(_args, **kwargs):
        seen.update(kwargs)
        return _Completed("")

    monkeypatch.setattr(system_module.subprocess, "run", fake_run)
    _ps_start_times([4242])

    assert seen["timeout"] == system_module._PS_START_TIMEOUT_SECONDS
    assert seen["capture_output"] is True


def test_observed_start_times_asks_for_every_pid_in_one_call(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[int]] = []

    def fake_ps(pids):
        calls.append(list(pids))
        return dict.fromkeys(pids, 1000.0)

    monkeypatch.setattr(system_module, "_proc_start_times", lambda pids: {})
    monkeypatch.setattr(system_module, "_darwin_start_times", lambda pids: {})
    monkeypatch.setattr(system_module, "_ps_start_times", fake_ps)

    observed = _observed_process_start_times([4242, 4242, 9999, 0, -3])

    assert calls == [[4242, 9999]]
    assert sorted(observed) == [4242, 9999]


def test_observed_start_times_without_pids_does_not_probe(monkeypatch: pytest.MonkeyPatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("must not spawn a probe for an empty pid set")

    monkeypatch.setattr(system_module, "_proc_start_times", fail)
    monkeypatch.setattr(system_module, "_darwin_start_times", fail)
    monkeypatch.setattr(system_module, "_ps_start_times", fail)

    assert _observed_process_start_times([]) == {}


def test_proc_start_times_survives_a_comm_field_with_spaces_and_parens(
    monkeypatch: pytest.MonkeyPatch,
):
    """Field 2 can contain ') ' — anchoring on the last ')' is what keeps 22 correct."""
    # starttime is field 22, i.e. index 19 once the leading "pid (comm)" is cut:
    # index 0 is field 3 (state), so build exactly that shape.
    tail = ["S", *["0"] * 18, "12345", "0", "0"]
    stat_text = f"70747 (weird (name) S ) {' '.join(tail)}"

    class _FakePath(type(Path())):
        def read_text(self, *_args, **_kwargs):
            if self.name == "stat" and self.parent.name == "70747":
                return stat_text
            raise OSError("no such file")

    monkeypatch.setattr(system_module, "Path", _FakePath)
    monkeypatch.setattr(system_module, "_proc_boot_epoch", lambda: 1_000_000.0)
    monkeypatch.setattr(system_module.os, "sysconf", lambda _name: 100)

    observed = _proc_start_times([70747])

    assert observed == {70747: 1_000_000.0 + 12345 / 100}


def test_proc_start_times_is_empty_without_a_boot_epoch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(system_module, "_proc_boot_epoch", lambda: None)

    assert _proc_start_times([4242]) == {}


# --------------------------------------------------------------------------
# collector integration
# --------------------------------------------------------------------------


def _write_registry(home: Path, entries: list[dict]) -> None:
    runtime = home / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "active_sessions.json").write_text(json.dumps({"entries": entries}))


def _collect(home: Path, *, observed: dict[int, float], live: frozenset[int]):
    probes: list[list[int]] = []

    def probe(pids):
        probes.append(list(pids))
        return dict(observed)

    collector = Collector(home, pid_exists=lambda pid: pid in live, process_start_times=probe)
    try:
        state = collector.collect()
    finally:
        collector.close()
    return state, probes


def test_collector_reports_live_dead_and_unverifiable_surfaces(hermes_home: Path):
    _write_registry(
        hermes_home,
        [
            {"session_id": "live", "surface": "cli", "pid": 111, "process_start_time": 1000.0},
            {"session_id": "reused", "surface": "cli", "pid": 222, "process_start_time": 1000.0},
            {"session_id": "gone", "surface": "cli", "pid": 333, "process_start_time": 1000.0},
            {"session_id": "legacy", "surface": "cli", "pid": 444},
        ],
    )

    state, _ = _collect(
        hermes_home,
        observed={111: 1000.0, 222: 9000.0, 333: 1000.0},
        live=frozenset({111, 222, 444}),
    )
    by_id = {surface.session_id: surface for surface in state.active_surfaces}

    assert by_id["live"].liveness is ProcessLiveness.LIVE
    assert by_id["reused"].liveness is ProcessLiveness.DEAD
    assert by_id["gone"].liveness is ProcessLiveness.DEAD
    assert by_id["legacy"].liveness is ProcessLiveness.UNVERIFIABLE


def test_collector_probes_every_pid_once_per_pass(hermes_home: Path):
    _write_registry(
        hermes_home,
        [
            {"session_id": "a", "surface": "cli", "pid": 111, "process_start_time": 1000.0},
            {"session_id": "b", "surface": "cli", "pid": 222, "process_start_time": 1000.0},
            {"session_id": "c", "surface": "cli", "pid": 111, "process_start_time": 1000.0},
        ],
    )

    _, probes = _collect(
        hermes_home, observed={111: 1000.0, 222: 1000.0}, live=frozenset({111, 222})
    )

    assert len(probes) == 1
    assert sorted(probes[0]) == [111, 222]


def test_collector_records_the_registry_start_time(hermes_home: Path):
    _write_registry(
        hermes_home,
        [{"session_id": "a", "surface": "cli", "pid": 111, "process_start_time": 1788832752.35404}],
    )

    state, _ = _collect(hermes_home, observed={111: 1788832752.0}, live=frozenset({111}))

    surface = state.active_surfaces[0]
    assert surface.process_start_time == pytest.approx(1788832752.35404)
    assert surface.liveness is ProcessLiveness.LIVE


def test_collector_treats_a_zero_start_time_as_unrecorded(hermes_home: Path):
    _write_registry(
        hermes_home,
        [{"session_id": "a", "surface": "cli", "pid": 111, "process_start_time": 0}],
    )

    state, _ = _collect(hermes_home, observed={111: 1000.0}, live=frozenset({111}))

    surface = state.active_surfaces[0]
    assert surface.process_start_time is None
    assert surface.liveness is ProcessLiveness.UNVERIFIABLE


def test_alive_is_derived_from_liveness_and_reaches_the_json_snapshot():
    live = ActiveSurface(session_id="a", liveness=ProcessLiveness.LIVE)
    unverifiable = ActiveSurface(session_id="b", liveness=ProcessLiveness.UNVERIFIABLE)
    dead = ActiveSurface(session_id="c", liveness=ProcessLiveness.DEAD)

    assert live.alive is True
    assert unverifiable.alive is True
    assert dead.alive is False

    assert dead.model_dump(mode="json")["liveness"] == "dead"
    assert dead.model_dump(mode="json")["alive"] is False


def test_bare_active_surface_does_not_claim_to_be_live():
    assert ActiveSurface().liveness is ProcessLiveness.UNVERIFIABLE
    assert ActiveSurface().pid == 0
