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


# --------------------------------------------------------------------------
# gateway/dead_targets.json (gateway/dead_targets.py:47-58,75-86): per-profile
# registry of confirmed-unreachable delivery targets
# --------------------------------------------------------------------------


def _write_dead_targets(home: Path, payload: object) -> Path:
    directory = home / "gateway"
    directory.mkdir(exist_ok=True)
    path = directory / "dead_targets.json"
    path.write_text(json.dumps(payload))
    return path


def _dead(platform: str, chat_id: str, reason: str, age: float) -> dict[str, object]:
    return {"platform": platform, "chat_id": chat_id, "reason": reason, "marked_at": NOW - age}


def test_dead_targets_are_counted_by_platform_newest_first(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_dead_targets(
        hermes_home,
        {
            "telegram:-100123": _dead("telegram", "-100123", "forbidden: bot was kicked", 7200),
            "telegram:555": _dead("telegram", "555", "not_found: chat not found", 60),
            "discord:9": _dead("discord", "9", "forbidden", 3600),
            "junk": "not a mapping",
        },
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.dead_target_count == 3
    assert gateway.dead_target_platforms == {"telegram": 2, "discord": 1}
    assert [(t.platform, t.age_seconds) for t in gateway.dead_targets] == [
        ("telegram", pytest.approx(60.0)),
        ("discord", pytest.approx(3600.0)),
        ("telegram", pytest.approx(7200.0)),
    ]
    assert gateway.dead_targets[0].reason == "not_found: chat not found"


def test_dead_targets_never_carry_the_chat_id(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_dead_targets(
        hermes_home, {"telegram:SECRETCHAT": _dead("telegram", "SECRETCHAT", "", 5)}
    )

    state = _collect(hermes_home)

    assert "SECRETCHAT" not in state.model_dump_json()


def test_dead_target_list_and_platforms_are_bounded(hermes_home: Path):
    _write_gateway_state(hermes_home)
    entries = {f"p{i}:{i}": _dead(f"p{i}", str(i), "r" * 500, float(i)) for i in range(50)}
    _write_dead_targets(hermes_home, entries)

    gateway = _collect(hermes_home).gateway

    assert gateway.dead_target_count == 50
    assert len(gateway.dead_targets) == 3
    assert len(gateway.dead_target_platforms) == 8
    assert all(len(t.reason) <= 80 for t in gateway.dead_targets)


def test_dead_target_reason_is_redacted(hermes_home: Path):
    secret = "sk-" + "B" * 40
    _write_gateway_state(hermes_home)
    _write_dead_targets(hermes_home, {"t:1": _dead("t", "1", f"forbidden {secret}", 5)})

    gateway = _collect(hermes_home).gateway

    assert secret not in gateway.dead_targets[0].reason


def test_dead_targets_absent_file_is_zero(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.dead_target_count == 0
    assert gateway.dead_targets == []


def test_dead_targets_wrong_shapes_do_not_crash(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_dead_targets(
        hermes_home,
        {"a:1": {"platform": None, "reason": 7, "marked_at": "soon"}, "b:2": {}},
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.dead_target_count == 2
    assert {t.platform for t in gateway.dead_targets} == {"unknown"}
    assert all(t.age_seconds is None for t in gateway.dead_targets)


def test_dead_targets_keep_last_good_when_the_file_turns_corrupt(hermes_home: Path):
    from hermesd.collector import Collector
    from tests.test_collector_gateway import _clock

    _write_gateway_state(hermes_home)
    path = _write_dead_targets(hermes_home, {"t:1": _dead("telegram", "1", "forbidden", 5)})
    collector = Collector(hermes_home, pid_exists=lambda pid: pid == 4242, clock=_clock)
    try:
        first = collector.collect()
        path.write_text("{torn")
        second = collector.collect()
    finally:
        collector.close()

    assert first.gateway.dead_target_count == 1
    assert second.gateway.dead_target_count == 1
    assert "dead_targets" in second.health.failed_sources
    assert "gateway" not in second.health.failed_sources


def test_dead_targets_are_read_from_the_selected_profile(profiled_hermes_home: Path):
    """``get_hermes_home()/"gateway"/"dead_targets.json"`` (dead_targets.py:54): PROFILE."""
    from hermesd.collector import Collector

    _write_dead_targets(profiled_hermes_home, {"root:1": _dead("root", "1", "", 5)})
    profile_home = profiled_hermes_home / "profiles" / "coding"
    _write_dead_targets(
        profile_home, {"a:1": _dead("a", "1", "", 5), "b:2": _dead("b", "2", "", 5)}
    )

    collector = Collector(profiled_hermes_home, profile_name="coding")
    try:
        state = collector.collect()
    finally:
        collector.close()

    assert state.gateway.dead_target_count == 2


# --------------------------------------------------------------------------
# gateway/restart_loop.json (gateway/restart_loop_guard.py:36-105): the
# auto-resume restart-loop breaker's boot chain
# --------------------------------------------------------------------------


def _write_restart_loop(home: Path, payload: object) -> Path:
    directory = home / "gateway"
    directory.mkdir(exist_ok=True)
    path = directory / "restart_loop.json"
    path.write_text(json.dumps(payload))
    return path


def test_restart_loop_chain_below_the_threshold_is_not_tripped(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_restart_loop(hermes_home, {"boots": [NOW - 900, NOW - 200, NOW - 20]})

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_loop_boots_recorded == 3
    # NOW-900 is more than 300s before NOW-200: that gap ends the chain.
    assert gateway.restart_loop_chain == 2
    assert gateway.restart_loop_max_restarts == 3
    assert gateway.restart_loop_tripped is False
    assert gateway.restart_loop_last_boot_age_seconds == pytest.approx(20.0)


def test_restart_loop_chain_at_the_threshold_is_tripped(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_restart_loop(hermes_home, {"boots": [NOW - 290, NOW - 150, NOW - 10]})

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_loop_chain == 3
    assert gateway.restart_loop_tripped is True


def test_restart_loop_forgets_a_resolved_episode(hermes_home: Path):
    """Real quiet (a gap wider than the chain gap before now) resets the chain."""
    _write_gateway_state(hermes_home)
    _write_restart_loop(hermes_home, {"boots": [NOW - 1300, NOW - 1200, NOW - 1100]})

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_loop_boots_recorded == 3
    assert gateway.restart_loop_chain == 0
    assert gateway.restart_loop_tripped is False


def test_restart_loop_future_boot_is_adjacent_not_a_break(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_restart_loop(hermes_home, {"boots": [NOW - 100, NOW - 50, NOW + 30]})

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_loop_chain == 3
    assert gateway.restart_loop_last_boot_age_seconds == 0.0


def test_restart_loop_uses_the_configured_policy(hermes_home: Path):
    import yaml

    (hermes_home / "config.yaml").write_text(
        yaml.dump({"gateway": {"restart_loop_guard": {"max_restarts": 2, "max_gap_seconds": 1000}}})
    )
    _write_gateway_state(hermes_home)
    _write_restart_loop(hermes_home, {"boots": [NOW - 900, NOW - 20]})

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_loop_max_restarts == 2
    assert gateway.restart_loop_chain == 2
    assert gateway.restart_loop_tripped is True


def test_restart_loop_disabled_breaker_never_trips(hermes_home: Path):
    import yaml

    (hermes_home / "config.yaml").write_text(
        yaml.dump({"gateway": {"restart_loop_guard": {"max_restarts": 0}}})
    )
    _write_gateway_state(hermes_home)
    _write_restart_loop(hermes_home, {"boots": [NOW - 30, NOW - 20, NOW - 10]})

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_loop_max_restarts == 0
    assert gateway.restart_loop_tripped is False


@pytest.mark.parametrize(
    "guard",
    [
        {"max_restarts": "x", "window_seconds": "60", "max_gap_seconds": -5},
        {"max_restarts": "3", "window_seconds": 0, "max_gap_seconds": 1.5},
        "not a mapping",
    ],
)
def test_restart_loop_policy_falls_back_to_upstream_defaults(hermes_home: Path, guard: object):
    import yaml

    (hermes_home / "config.yaml").write_text(yaml.dump({"gateway": {"restart_loop_guard": guard}}))
    _write_gateway_state(hermes_home)
    _write_restart_loop(hermes_home, {"boots": [NOW - 250, NOW - 120, NOW - 10]})

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_loop_max_restarts == 3
    assert gateway.restart_loop_tripped is True


def test_restart_loop_ignores_junk_boots(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_restart_loop(hermes_home, {"boots": ["x", None, True, float("nan"), NOW - 10]})

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_loop_boots_recorded == 1
    assert gateway.restart_loop_chain == 1


@pytest.mark.parametrize("payload", [{"boots": "bad"}, {}, {"boots": []}])
def test_restart_loop_without_boots_records_nothing(hermes_home: Path, payload: object):
    _write_gateway_state(hermes_home)
    _write_restart_loop(hermes_home, payload)

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_loop_boots_recorded == 0
    assert gateway.restart_loop_last_boot_age_seconds is None
    assert gateway.restart_loop_tripped is False


def test_restart_loop_is_read_from_the_selected_profile(profiled_hermes_home: Path):
    """``get_hermes_home()/"gateway"/"restart_loop.json"`` (restart_loop_guard.py:36-37): PROFILE."""
    import time

    from hermesd.collector import Collector

    now = time.time()
    _write_restart_loop(profiled_hermes_home, {"boots": [now - 5]})
    profile_home = profiled_hermes_home / "profiles" / "coding"
    _write_restart_loop(profile_home, {"boots": [now - 30, now - 20, now - 10]})

    collector = Collector(profiled_hermes_home, profile_name="coding")
    try:
        state = collector.collect()
    finally:
        collector.close()

    assert state.gateway.restart_loop_boots_recorded == 3


def test_restart_loop_keeps_last_good_when_the_file_is_torn(hermes_home: Path):
    """``_save_boots`` is a plain write_text (restart_loop_guard.py:50-54): torn reads happen."""
    from hermesd.collector import Collector
    from tests.test_collector_gateway import _clock

    _write_gateway_state(hermes_home)
    path = _write_restart_loop(hermes_home, {"boots": [NOW - 290, NOW - 150, NOW - 10]})
    collector = Collector(hermes_home, pid_exists=lambda pid: pid == 4242, clock=_clock)
    try:
        first = collector.collect()
        path.write_text('{"boots": [17')
        second = collector.collect()
    finally:
        collector.close()

    assert first.gateway.restart_loop_tripped is True
    assert second.gateway.restart_loop_tripped is True
    assert "restart_loop" in second.health.failed_sources


# --------------------------------------------------------------------------
# gateway_heartbeats grouped by profile and host (hermes_state_common.py:508-515;
# refreshed every 60s by tui_gateway/session_reaper.py:380-445)
# --------------------------------------------------------------------------


def _write_backend_rows(home: Path, rows: list[tuple[object, ...]]) -> None:
    import sqlite3

    from tests.test_collector_gateway import _create_ledger_tables

    conn = sqlite3.connect(str(home / "state.db"))
    _create_ledger_tables(conn)
    conn.executemany("INSERT INTO gateway_heartbeats VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_backend_heartbeats_are_grouped_by_profile_and_host(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_backend_rows(
        hermes_home,
        [
            ("default@a:1", 1, NOW - 3600, NOW - 30, "default", "a"),
            ("default@a:2", 2, NOW - 90_000, NOW - 80_000, "default", "a"),
            ("dev@a:3", 3, NOW - 600, NOW - 400, "dev", "a"),
            ("default@b:4", 4, NOW - 100, NOW - 50, "default", "b"),
            ("legacy", 5, None, None, None, None),
        ],
    )

    groups = _collect(hermes_home).gateway.gateway_backend_groups

    assert [(g.profile, g.host, g.backends) for g in groups] == [
        ("default", "a", 2),
        ("default", "b", 1),
        ("dev", "a", 1),
        ("", "", 1),
    ]
    assert groups[0].last_heartbeat_age_seconds == pytest.approx(30.0)
    assert groups[0].newest_start_age_seconds == pytest.approx(3600.0)
    assert groups[0].live is True
    assert groups[2].live is False
    assert groups[3].last_heartbeat_age_seconds is None
    assert groups[3].live is False


def test_backend_groups_are_bounded(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_backend_rows(
        hermes_home,
        [(f"p{i}@h:{i}", i, NOW - 100, NOW - i, f"p{i}", "h") for i in range(1, 30)],
    )

    gateway = _collect(hermes_home).gateway

    assert len(gateway.gateway_backend_groups) == 8
    assert gateway.gateway_backend_groups_truncated is True
    assert gateway.gateway_backend_groups[0].profile == "p1"


def test_backend_groups_absent_table_is_empty(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.gateway_backend_groups == []
    assert gateway.gateway_backend_groups_truncated is False


# --------------------------------------------------------------------------
# .restart_pending.json: a planned (non-chat) restart's back-online notice still
# owed to home channels (gateway/run_shutdown.py:2054-2064 writes it,
# gateway/run_notifications.py:936-972 unlinks it once every home was reached)
# --------------------------------------------------------------------------


def _write_restart_pending(home: Path, payload: object) -> Path:
    path = home / ".restart_pending.json"
    path.write_text(json.dumps(payload))
    return path


def test_restart_notice_pending_is_surfaced(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_restart_pending(
        hermes_home,
        {
            "requested_at": NOW - 600,
            "via_service": True,
            "detached": False,
            "delivered_targets": [["default", "telegram", "1", None]],
        },
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_notice_pending is True
    assert gateway.restart_notice_requested_age_seconds == pytest.approx(600.0)
    assert gateway.restart_notice_via_service is True
    assert gateway.restart_notice_detached is False
    assert gateway.restart_notice_delivered_count == 1


def test_restart_notice_absent_is_not_pending(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_notice_pending is False
    assert gateway.restart_notice_requested_age_seconds is None


def test_restart_notice_wrong_types_do_not_crash(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_restart_pending(
        hermes_home,
        {"requested_at": "soon", "via_service": "yes", "delivered_targets": "x"},
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.restart_notice_pending is True
    assert gateway.restart_notice_requested_age_seconds is None
    assert gateway.restart_notice_via_service is False
    assert gateway.restart_notice_delivered_count == 0


def test_restart_notice_keeps_last_good_when_torn(hermes_home: Path):
    from hermesd.collector import Collector
    from tests.test_collector_gateway import _clock

    _write_gateway_state(hermes_home)
    path = _write_restart_pending(hermes_home, {"requested_at": NOW - 60})
    collector = Collector(hermes_home, pid_exists=lambda pid: pid == 4242, clock=_clock)
    try:
        first = collector.collect()
        path.write_text("{torn")
        second = collector.collect()
    finally:
        collector.close()

    assert first.gateway.restart_notice_pending is True
    assert second.gateway.restart_notice_pending is True
    assert "restart_pending" in second.health.failed_sources


# --------------------------------------------------------------------------
# serve_restart_pending/<pid>-<hex>.json (hermes_cli/update_serve_obligations.py:
# 40-49 writes one immutable file per manual serve incarnation; :91-99 drops the
# ones whose process is provably gone)
# --------------------------------------------------------------------------


def _write_obligation(home: Path, pid: int, create_time: float, **extra: object) -> Path:
    directory = home / "serve_restart_pending"
    directory.mkdir(exist_ok=True)
    row: dict[str, object] = {
        "kind": "serve",
        "profile": "default",
        "pid": pid,
        "create_time": create_time,
    }
    row.update(extra)
    path = directory / f"{pid}-{float(create_time).hex()}.json"
    path.write_text(json.dumps(row))
    return path


def _collect_obligations(home: Path, starts: dict[int, float], live: set[int]):
    from hermesd.collector import Collector
    from tests.test_collector_gateway import _clock

    collector = Collector(
        home,
        pid_exists=lambda pid: pid in live,
        process_start_times=lambda pids: {pid: starts[pid] for pid in pids if pid in starts},
        clock=_clock,
    )
    try:
        return collector.collect()
    finally:
        collector.close()


def test_serve_obligations_count_only_live_incarnations(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_obligation(hermes_home, 501, 1000.0, kind="dashboard", profile="dev")
    _write_obligation(hermes_home, 502, 2000.0)  # pid reused by another process
    _write_obligation(hermes_home, 503, 3000.0)  # pid gone
    _write_obligation(hermes_home, 504, 4000.0)  # alive, start time unobservable

    state = _collect_obligations(
        hermes_home, starts={501: 1000.5, 502: 9999.0}, live={501, 502, 504, 4242}
    )
    gateway = state.gateway

    assert gateway.serve_restart_pending_count == 2
    assert gateway.serve_restart_stale_count == 2
    assert sorted((o.kind, o.profile, o.pid) for o in gateway.serve_restart_pending) == [
        ("dashboard", "dev", 501),
        ("serve", "default", 504),
    ]
    assert {o.pid: o.verified for o in gateway.serve_restart_pending} == {501: True, 504: False}


def test_serve_obligations_skip_junk_files_and_bound_the_scan(hermes_home: Path):
    _write_gateway_state(hermes_home)
    directory = hermes_home / "serve_restart_pending"
    directory.mkdir()
    (directory / "notes.txt").write_text("x")
    (directory / "bad.json").write_text("{torn")
    (directory / "wrong.json").write_text(json.dumps({"pid": "7", "create_time": None}))
    for pid in range(1000, 1100):
        _write_obligation(hermes_home, pid, float(pid))

    state = _collect_obligations(
        hermes_home,
        starts={pid: float(pid) for pid in range(1000, 1100)},
        live=set(range(1000, 1100)),
    )

    # Only the first 64 directory entries are examined; junk among them is skipped.
    assert 60 <= state.gateway.serve_restart_pending_count <= 64
    assert len(state.gateway.serve_restart_pending) == 5
    assert state.gateway.serve_restart_scan_truncated is True
    assert "serve_restart_pending" not in state.health.failed_sources


def test_serve_obligations_absent_directory_is_zero(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.serve_restart_pending_count == 0
    assert gateway.serve_restart_pending == []


def test_serve_obligation_symlink_outside_home_is_ignored(hermes_home: Path, tmp_path: Path):
    _write_gateway_state(hermes_home)
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"kind": "serve", "profile": "x", "pid": 4242, "create_time": 1.0})
    )
    directory = hermes_home / "serve_restart_pending"
    directory.mkdir()
    (directory / "4242-0x1p+0.json").symlink_to(outside)

    state = _collect_obligations(hermes_home, starts={4242: 1.0}, live={4242})

    assert state.gateway.serve_restart_pending_count == 0


# --------------------------------------------------------------------------
# older update receipts (logs/update_receipts/update_*.json, 20 kept per home:
# hermes_cli/update_receipt.py:21,206-211,241-246)
# --------------------------------------------------------------------------


def _write_archived_receipt(home: Path, stamp: str, **extra: object) -> Path:
    directory = home / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"update_{stamp}_1.json"
    path.write_text(json.dumps(_receipt(**extra)))
    return path


def test_update_history_lists_the_newest_failures(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_archived_receipt(hermes_home, "20260901_000000", outcome="success")
    _write_archived_receipt(
        hermes_home,
        "20260902_000000",
        outcome="failed",
        finished_at=_iso(NOW - 7200),
        steps=[{"name": "pull", "ok": False}],
    )
    _write_archived_receipt(
        hermes_home, "20260903_000000", outcome="partial", finished_at=_iso(NOW - 3600)
    )
    _write_archived_receipt(hermes_home, "20260904_000000", outcome="success")
    (hermes_home / "logs" / "update_receipts" / "latest.json").write_text(
        json.dumps(_receipt(outcome="failed"))
    )

    gateway = _collect(hermes_home).gateway

    assert gateway.update_history_scanned == 4
    assert gateway.update_history_failed == 2
    assert [(r.outcome, r.failed_step) for r in gateway.update_failures] == [
        ("partial", ""),
        ("failed", "pull"),
    ]
    assert gateway.update_failures[0].finished_age_seconds == pytest.approx(3600.0)


def test_update_history_is_bounded_to_the_newest_receipts(hermes_home: Path):
    _write_gateway_state(hermes_home)
    for day in range(1, 29):
        _write_archived_receipt(hermes_home, f"202609{day:02d}_000000", outcome="failed")

    gateway = _collect(hermes_home).gateway

    assert gateway.update_history_scanned == 10
    assert gateway.update_history_failed == 10
    assert len(gateway.update_failures) == 3


def test_update_history_skips_unreadable_receipts(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_archived_receipt(hermes_home, "20260901_000000", outcome="failed")
    (hermes_home / "logs" / "update_receipts" / "update_20260902_000000_1.json").write_text("{x")

    gateway = _collect(hermes_home).gateway

    assert gateway.update_history_scanned == 1
    assert gateway.update_history_failed == 1


def test_update_history_absent_directory_is_empty(hermes_home: Path):
    _write_gateway_state(hermes_home)

    gateway = _collect(hermes_home).gateway

    assert gateway.update_history_scanned == 0
    assert gateway.update_failures == []


def test_receipt_outcome_and_skip_vocabularies_are_bounded(hermes_home: Path):
    _write_gateway_state(hermes_home)
    _write_receipt(
        hermes_home,
        _receipt(
            runtime_outcomes=[{"outcome": f"o{i}"} for i in range(20)],
            skips=[{"name": f"s{i}"} for i in range(10)],
        ),
    )

    gateway = _collect(hermes_home).gateway

    assert len(gateway.update_runtime_outcomes) == 8
    assert gateway.update_skip_count == 10
    assert gateway.update_skip_names == ["s0", "s1", "s2"]


@pytest.mark.parametrize("create_time", [None, True, "1.0", 0, -1.0])
def test_serve_obligation_without_a_usable_create_time_is_ignored(
    hermes_home: Path, create_time: object
):
    """Upstream files a reminder only for an identified incarnation (:28-37)."""
    _write_gateway_state(hermes_home)
    directory = hermes_home / "serve_restart_pending"
    directory.mkdir()
    (directory / "501-x.json").write_text(
        json.dumps({"kind": "serve", "profile": "p", "pid": 501, "create_time": create_time})
    )

    state = _collect_obligations(hermes_home, starts={501: 1.0}, live={501})

    assert state.gateway.serve_restart_pending_count == 0
    assert state.gateway.serve_restart_stale_count == 0
