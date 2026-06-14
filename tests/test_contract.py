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

import json
import os
from pathlib import Path

import pytest

from hermesd.collector import Collector
from hermesd.models import DashboardState
from hermesd.panels import render_panel
from hermesd.paths import default_hermes_home
from hermesd.theme import Theme
from tests.conftest import render_to_str

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
        assert all(p.label for p in pools), (
            "credential pool entries present but labels blank (see FIX A1)"
        )
    raw_pool_labels = _raw_credential_pool_labels(home / "auth.json")
    if raw_pool_labels:
        collected_labels = {p.label for p in pools}
        assert raw_pool_labels <= collected_labels, (
            "credential_pool list entry labels are not surfaced (see FIX A1)"
        )

    # FIX B — sessions billing/end fields: some session should expose at least
    # one of the previously-unread columns on a busy home.
    if state.sessions:
        assert any(s.billing_base_url or s.end_reason or s.billing_mode for s in state.sessions), (
            "no session exposes billing_base_url/end_reason/billing_mode (see FIX B)"
        )
        authoritative_cost_sessions = [
            s for s in state.sessions if s.cost_status in {"included", "exact"}
        ]
        if authoritative_cost_sessions:
            text = render_to_str(
                render_panel(
                    3,
                    DashboardState(sessions=authoritative_cost_sessions),
                    Theme(),
                    detail=True,
                ),
                width=160,
            )
            assert "~$" not in text, (
                "live sessions with included/exact cost_status still render as estimated (see FIX A4)"
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


def _raw_credential_pool_labels(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    pool = data.get("credential_pool")
    if not isinstance(pool, dict):
        return set()
    labels: set[str] = set()
    for raw_entry in pool.values():
        entries = raw_entry if isinstance(raw_entry, list) else [raw_entry]
        candidates = [entry for entry in entries if isinstance(entry, dict)]
        if candidates:
            selected = min(enumerate(candidates), key=lambda pair: (_priority(pair[1]), pair[0]))[1]
            if selected.get("label"):
                labels.add(str(selected["label"]))
    return labels


def _priority(entry: dict[str, object]) -> int:
    try:
        return int(entry.get("priority") or 0)
    except (TypeError, ValueError):
        return 0
