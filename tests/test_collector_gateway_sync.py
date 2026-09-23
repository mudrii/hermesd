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
    NOW,
    _armed_heartbeat,
    _collect,
    _collect_probed,
    _iso,
    _receipt,
    _write_gateway_state,
    _write_heartbeat_v2,
    _write_ledgers,
    _write_receipt,
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


def test_standalone_reason_of_a_replaced_writer_is_not_current(hermes_home: Path):
    """A live launchd replacement cannot vouch for its predecessor's boot verdict."""
    _write_gateway_state(hermes_home, pid=1111, multiplex_standalone_reason="old boot")
    (hermes_home / "gateway.pid").write_text("4242")

    gateway = _collect(hermes_home).gateway

    assert gateway.running is True
    assert gateway.multiplex_standalone_reason == ""


def test_standalone_reason_is_redacted_and_bounded(hermes_home: Path):
    secret = "sk-" + "A" * 40
    _write_gateway_state(hermes_home, multiplex_standalone_reason=f"token {secret} " + "x" * 5000)

    gateway = _collect(hermes_home).gateway

    assert secret not in gateway.multiplex_standalone_reason
    assert len(gateway.multiplex_standalone_reason) <= 800


# --------------------------------------------------------------------------
# update receipt: external fleet rows and the newer receipt keys
# (hermes_cli/update_receipt.py:387-392; update_cmd_fleet.py:224-231)
# --------------------------------------------------------------------------


def test_external_fleet_row_on_another_build_is_not_code_skew(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(
            fleet=[
                {"profile": "default", "code_sha": "bbbb", "state": "current"},
                {
                    "profile": "lab",
                    "code_sha": "zzzz",
                    "state": "external",
                    "code_root": "/opt/other-checkout",
                },
            ]
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is False
    assert gateway.runtime_code_skew_source == "fleet"
    assert gateway.update_fleet_states == {"current": 1, "external": 1}
    assert gateway.update_fleet_external_roots == ["/opt/other-checkout"]


def test_non_external_row_still_counts_as_skew_beside_an_external_one(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(
            fleet=[
                {"profile": "lab", "code_sha": "zzzz", "state": "external"},
                {"profile": "coding", "code_sha": "aaaa", "state": "current"},
            ]
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.runtime_code_skew is True


def test_external_roots_are_bounded_redacted_and_deduplicated(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    fleet: list[dict[str, object]] = [
        {"profile": f"p{i}", "state": "external", "code_root": f"/opt/r{i}"} for i in range(20)
    ]
    fleet.append({"profile": "dup", "state": "external", "code_root": "/opt/r0"})
    fleet.append({"profile": "junk", "state": "external", "code_root": 7})
    _write_receipt(hermes_home, _receipt(fleet=fleet))

    gateway = _collect(hermes_home).gateway

    assert gateway.update_fleet_external_roots == ["/opt/r0", "/opt/r1", "/opt/r2", "/opt/r3"]


def test_receipt_new_keys_are_surfaced(hermes_home: Path):
    _write_gateway_state(hermes_home, code_sha="bbbb")
    _write_receipt(
        hermes_home,
        _receipt(
            post_swap_pid=92904,
            pending_manual_serves=[{"kind": "serve", "pid": 1}, {"kind": "dashboard", "pid": 2}],
            gateway_restart={"incomplete": False, "settled_from_live_fleet_at": _iso(NOW - 120)},
            runtime_outcomes=[
                {"kind": "gateway", "outcome": "restarted"},
                {"kind": "dashboard", "outcome": "restarted"},
                {"kind": "serve", "outcome": "unaccounted"},
                "junk",
            ],
            skips=[
                {"name": "desktop_serve", "reason": "owned by the app"},
                {"name": "npm", "reason": "offline"},
            ],
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.update_post_swap_pid == 92904
    assert gateway.update_pending_manual_serve_count == 2
    assert gateway.update_settled_from_live_fleet_age_seconds == pytest.approx(120.0)
    assert gateway.update_runtime_outcomes == {"restarted": 2, "unaccounted": 1}
    assert gateway.update_skip_count == 2
    assert gateway.update_skip_names == ["desktop_serve", "npm"]


@pytest.mark.parametrize("pid", [True, 0, -3, "12", None, 1.5])
def test_receipt_post_swap_pid_rejects_non_pids(hermes_home: Path, pid: object):
    _write_gateway_state(hermes_home)
    _write_receipt(hermes_home, _receipt(post_swap_pid=pid))

    assert _collect(hermes_home).gateway.update_post_swap_pid is None


def test_receipt_new_keys_tolerate_wrong_types(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_receipt(
        hermes_home,
        _receipt(
            pending_manual_serves="bad",
            gateway_restart=[1],
            runtime_outcomes={"a": 1},
            skips=[None, {"name": None}, {"name": "x" * 500}],
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.update_pending_manual_serve_count == 0
    assert gateway.update_settled_from_live_fleet_age_seconds is None
    assert gateway.update_runtime_outcomes == {}
    assert gateway.update_skip_count == 3
    assert gateway.update_skip_names == ["x" * 60]


# --------------------------------------------------------------------------
# heartbeat memory block (gateway/shutdown_watchdog.py:208-210 embeds
# gateway/lifecycle_ledger.py:64-74 sample_memory; tiers gateway/memory_status.py:16-30)
# --------------------------------------------------------------------------

_GIB_KIB = 1024 * 1024


def _mem(**extra: object) -> dict[str, object]:
    block: dict[str, object] = {
        "rss_kib": 512 * 1024,
        "mem_total_kib": 8 * _GIB_KIB,
        "mem_available_kib": 4 * _GIB_KIB,
        "swap_used_kib": 0,
    }
    block.update(extra)
    return block


def test_heartbeat_memory_block_is_surfaced_with_ok_pressure(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(30.0, mem=_mem()))

    gateway = _collect(hermes_home).gateway

    assert gateway.memory_rss_kib == 512 * 1024
    assert gateway.memory_total_kib == 8 * _GIB_KIB
    assert gateway.memory_available_kib == 4 * _GIB_KIB
    assert gateway.memory_swap_used_kib == 0
    assert gateway.memory_pressure == "ok"


@pytest.mark.parametrize(
    ("available", "total", "expected"),
    [
        (60 * 1024, 0, "critical"),  # < 64 MiB, no total to take a fraction of
        (int(0.04 * 8 * _GIB_KIB), 8 * _GIB_KIB, "critical"),  # < 5 %
        (100 * 1024, 0, "elevated"),  # < 128 MiB
        (int(0.10 * 8 * _GIB_KIB), 8 * _GIB_KIB, "elevated"),  # < 15 %
        (int(0.20 * 8 * _GIB_KIB), 8 * _GIB_KIB, "ok"),
        (int(0.20 * 8 * _GIB_KIB), 0, "ok"),  # no total: absolute floors only
    ],
)
def test_memory_pressure_uses_upstream_tiers(
    hermes_home: Path, available: int, total: int, expected: str
):
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(
        hermes_home,
        _armed_heartbeat(30.0, mem=_mem(mem_available_kib=available, mem_total_kib=total)),
    )

    assert _collect(hermes_home).gateway.memory_pressure == expected


def test_stale_memory_sample_keeps_numbers_but_not_pressure(hermes_home: Path):
    """memory_status.py:98-104: a dead gateway's last sample must not pose as current."""
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(200.0, mem=_mem(mem_available_kib=1)))

    gateway = _collect(hermes_home).gateway

    assert gateway.memory_available_kib == 1
    assert gateway.memory_pressure == "unknown"


def test_absent_memory_block_reports_nothing(hermes_home: Path):
    """sample_memory() is Linux-only and returns {} elsewhere."""
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(hermes_home, _armed_heartbeat(30.0))

    gateway = _collect(hermes_home).gateway

    assert gateway.memory_rss_kib is None
    assert gateway.memory_available_kib is None
    assert gateway.memory_pressure == ""


def test_memory_block_rejects_non_integer_values(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_heartbeat_v2(
        hermes_home,
        _armed_heartbeat(
            30.0,
            mem={
                "rss_kib": True,
                "mem_total_kib": "8",
                "mem_available_kib": -5,
                "swap_used_kib": 1.5,
            },
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.memory_rss_kib is None
    assert gateway.memory_total_kib is None
    assert gateway.memory_available_kib is None
    assert gateway.memory_swap_used_kib is None
    assert gateway.memory_pressure == "unknown"


def test_memory_block_keeps_last_good_when_the_heartbeat_turns_corrupt(hermes_home: Path):
    from hermesd.collector import Collector
    from tests.test_collector_gateway import _clock

    _write_gateway_state(hermes_home)
    path = _write_heartbeat_v2(hermes_home, _armed_heartbeat(30.0, mem=_mem()))
    collector = Collector(hermes_home, pid_exists=lambda pid: pid == 4242, clock=_clock)
    try:
        first = collector.collect()
        path.write_text("{broken")
        second = collector.collect()
    finally:
        collector.close()

    assert first.gateway.memory_rss_kib == 512 * 1024
    assert second.gateway.memory_rss_kib == 512 * 1024
    assert second.gateway.memory_pressure == "ok"
    assert "gateway_heartbeat" in second.health.failed_sources
