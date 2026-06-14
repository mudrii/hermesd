"""Opt-in contract test against the real ~/.hermes data.

Skipped unless ``HERMESD_CONTRACT_TEST=1``. It runs the collector against the
live Hermes home and asserts that drift-sensitive fields are populated wherever
the underlying data exists. Each check is guarded by the presence of its source,
so the test fails only when data is present but a field is blank — the exact
signature of a producer-side schema/shape drift (the class of bug behind
FIX A1-A6 and FIX B). It is intentionally excluded from the default CI run so CI
never couples to a particular machine's data.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermesd.collector import Collector
from hermesd.paths import default_hermes_home

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMESD_CONTRACT_TEST") != "1",
    reason="set HERMESD_CONTRACT_TEST=1 to run the live ~/.hermes contract test",
)


def _home() -> Path:
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else default_hermes_home()


def test_live_hermes_home_has_no_drifted_blank_fields():
    home = _home()
    if not home.exists():
        pytest.skip(f"no Hermes home at {home}")

    collector = Collector(home)
    try:
        state = collector.collect()
    finally:
        collector.close()

    # The collector must not blank the whole display on a populated home.
    assert state.health.total_sources > 0

    # FIX A1 — credential_pool list-vs-dict: pools present but every label blank
    # (label falling back to the provider name) is the drift signature.
    pools = state.skills_memory.credential_pools
    if pools:
        assert any(p.label and p.label != p.name for p in pools), (
            "credential pool entries present but all labels blank/echo the name — "
            "auth.json shape drift (see FIX A1)"
        )

    # FIX B — sessions billing/end fields: some session should expose at least
    # one of the previously-unread columns on a busy home.
    if state.sessions:
        assert any(s.billing_base_url or s.end_reason or s.billing_mode for s in state.sessions), (
            "no session exposes billing_base_url/end_reason/billing_mode (see FIX B)"
        )

    # C1 — gateway platforms populated when gateway_state.json exists.
    if (home / "gateway_state.json").exists():
        assert state.gateway.platforms, (
            "gateway_state.json present but no platforms parsed (see C1)"
        )

    # FIX A2 — desktop build stamp non-blank when the stamp file exists.
    if (home / "desktop-build-stamp.json").exists():
        assert state.operations.desktop_build_stamp, (
            "desktop-build-stamp.json present but stamp blank — camelCase drift (see FIX A2)"
        )

    # FIX A3/A6 — pr-monitor counts plausible: if a monitor file is read, at
    # least one should report a non-zero tracked/monitored count on a live home.
    monitors = state.operations.pr_monitors
    if monitors:
        assert any(m.monitored_count or m.tracked_count or m.author_pr_count for m in monitors), (
            "pr-monitor files read but all counts zero — key drift (see FIX A3/A6)"
        )

    # C3 — curator run surfaced when curation logs exist.
    curator_dir = home / "logs" / "curator"
    if curator_dir.is_dir() and any(p.is_dir() for p in curator_dir.iterdir()):
        assert state.curator.run_present, "curator run directories exist but none parsed (see C3)"
