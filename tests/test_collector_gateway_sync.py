"""Gateway readers kept in step with upstream hermes-agent (HEAD 38c289c014).

Each section names the upstream contract it pins. Helpers are shared with
``test_collector_gateway.py`` so the fixtures describe the same files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermesd.models import GatewayLoopHealth
from tests.test_collector_gateway import (
    _armed_heartbeat,
    _collect,
    _collect_probed,
    _write_gateway_state,
    _write_heartbeat_v2,
    _write_ledgers,
)

# --------------------------------------------------------------------------
# degraded is a serving state (gateway/run_startup.py:56-59 _serving_state;
# gateway/status.py:1207-1222 _DRAINABLE_GATEWAY_STATES)
# --------------------------------------------------------------------------


def test_degraded_gateway_with_a_live_pid_is_serving(hermes_home: Path):
    _write_gateway_state(hermes_home, gateway_state="degraded", active_agents=2)

    gateway = _collect(hermes_home).gateway

    assert gateway.running is True
    assert gateway.state == "degraded"
    assert gateway.degraded is True
    assert gateway.busy is True
    assert gateway.watchdog_exit_reason == ""


def test_idle_degraded_gateway_is_drainable(hermes_home: Path):
    _write_gateway_state(hermes_home, gateway_state="degraded", active_agents=0)

    gateway = _collect(hermes_home).gateway

    assert gateway.drainable is True
    assert gateway.busy is False


def test_degraded_gateway_with_a_dead_pid_is_not_serving(hermes_home: Path):
    _write_gateway_state(hermes_home, gateway_state="degraded")

    gateway = _collect(hermes_home, live_pid=-1).gateway

    assert gateway.running is False
    assert gateway.degraded is False
    assert gateway.drainable is False


@pytest.mark.parametrize("reason", ["loop_liveness_watchdog", "shutdown_watchdog"])
def test_watchdog_stamped_degraded_exit_is_reported(hermes_home: Path, reason: str):
    """gateway/status.py:362-385: a dead degraded record with a watchdog reason is a failure."""
    _write_gateway_state(hermes_home, gateway_state="degraded", exit_reason=reason)

    gateway = _collect(hermes_home, live_pid=-1).gateway

    assert gateway.running is False
    assert gateway.watchdog_exit_reason == reason


def test_watchdog_exit_is_not_reported_once_the_operator_stopped_the_gateway(hermes_home: Path):
    _write_gateway_state(
        hermes_home,
        gateway_state="degraded",
        exit_reason="loop_liveness_watchdog",
        desired_state="stopped",
    )

    gateway = _collect(hermes_home, live_pid=-1).gateway

    assert gateway.watchdog_exit_reason == ""


def test_live_degraded_gateway_is_not_a_watchdog_exit(hermes_home: Path):
    _write_gateway_state(hermes_home, gateway_state="degraded", exit_reason="shutdown_watchdog")

    gateway = _collect(hermes_home).gateway

    assert gateway.watchdog_exit_reason == ""


def test_degraded_exit_with_a_non_watchdog_reason_is_not_a_watchdog_exit(hermes_home: Path):
    _write_gateway_state(hermes_home, gateway_state="degraded", exit_reason="something else")

    gateway = _collect(hermes_home, live_pid=-1).gateway

    assert gateway.watchdog_exit_reason == ""


def test_degraded_gateway_is_probed_for_loop_liveness(hermes_home: Path):
    _write_gateway_state(hermes_home, gateway_state="degraded")
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0))

    gateway = _collect_probed(hermes_home, lambda pid, tcp_port: True).gateway  # type: ignore[attr-defined]

    assert gateway.loop_health is GatewayLoopHealth.ALIVE


def test_degraded_gateway_long_silent_heartbeat_is_wedged(hermes_home: Path):
    _write_gateway_state(hermes_home, gateway_state="degraded")
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(400.0, pid=1))

    gateway = _collect(hermes_home).gateway

    assert gateway.loop_health is GatewayLoopHealth.WEDGED


def test_degraded_gateway_counts_as_live_for_uptime_migration_and_runtime(hermes_home: Path):
    _write_gateway_state(hermes_home, gateway_state="degraded")
    _write_ledgers(hermes_home)
    (hermes_home / "gateway_migration.json").write_text(json.dumps({"version": 1}))

    state = _collect(hermes_home)

    assert state.gateway.current_incarnation_uptime_seconds == pytest.approx(3600.0)
    assert state.migration.default_gateway_live is True
    assert state.runtime.agent_running is True


# --------------------------------------------------------------------------
# multiplex_standalone_reason (hermes_cli/gateway_multiplex_mode.py:194-199)
# --------------------------------------------------------------------------


def test_standalone_reason_of_a_live_gateway_is_surfaced(hermes_home: Path):
    _write_gateway_state(
        hermes_home, multiplex_standalone_reason="profiles 'default' and 'dev' share a token"
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.multiplex_standalone_reason == "profiles 'default' and 'dev' share a token"


def test_standalone_reason_of_a_dead_gateway_is_not_current(hermes_home: Path):
    """Upstream prints it only for a running gateway (hermes_cli/gateway.py:5046,5054)."""
    _write_gateway_state(hermes_home, multiplex_standalone_reason="stale reason")

    gateway = _collect(hermes_home, live_pid=-1).gateway

    assert gateway.multiplex_standalone_reason == ""


@pytest.mark.parametrize("value", [None, "", 7, ["x"], {"a": 1}])
def test_standalone_reason_ignores_non_string_values(hermes_home: Path, value: object):
    _write_gateway_state(hermes_home, multiplex_standalone_reason=value)

    gateway = _collect(hermes_home).gateway

    assert gateway.multiplex_standalone_reason == ""


def test_standalone_reason_is_redacted_and_bounded(hermes_home: Path):
    secret = "sk-" + "A" * 40
    _write_gateway_state(hermes_home, multiplex_standalone_reason=f"token {secret} " + "x" * 5000)

    gateway = _collect(hermes_home).gateway

    assert secret not in gateway.multiplex_standalone_reason
    assert len(gateway.multiplex_standalone_reason) <= 800
