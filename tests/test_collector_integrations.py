"""Integration stores: pairing, webhook subscriptions, shared metrics, rate limits.

Every store is PROFILE-scoped upstream (``get_hermes_home()``):
``gateway/pairing.py:58-59,318-348`` (``get_hermes_dir("platforms/pairing",
"pairing")``), ``hermes_cli/webhook.py:18-35``,
``hermes_cli/observability/shared_metrics.py:171-176`` and
``agent/nous_rate_guard.py:37-46``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermesd.collector import Collector
from hermesd.models import DashboardState

_NOW = 1_800_000_000.0


def _collect(hermes_home: Path, profile: str | None = None) -> DashboardState:
    c = Collector(hermes_home, clock=lambda: _NOW, profile_name=profile)
    try:
        return c.collect()
    finally:
        c.close()


def _pairing_dir(home: Path, legacy: bool = False) -> Path:
    path = home / "pairing" if legacy else home / "platforms" / "pairing"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- pairing -------------------------------------------------------------


def test_pairing_counts_live_pending_and_approved_per_platform(hermes_home: Path):
    pairing = _pairing_dir(hermes_home)
    (pairing / "telegram-pending.json").write_text(
        json.dumps(
            {
                "h1": {"user_id": "111", "user_name": "alice", "created_at": _NOW - 60},
                # Expired (CODE_TTL_SECONDS = 3600, gateway/pairing.py:32) and
                # malformed entries count as gone, like _cleanup_expired.
                "h2": {"user_id": "222", "created_at": _NOW - 3601},
                "h3": {"user_id": "333"},
                "h4": "junk",
            }
        )
    )
    (pairing / "telegram-approved.json").write_text(
        json.dumps(
            {
                "111": {"user_name": "alice", "approved_at": _NOW - 7200},
                "444": {"user_name": "bob", "approved_at": _NOW - 120},
            }
        )
    )
    (pairing / "discord-approved.json").write_text(json.dumps({"9": {}}))
    # Shared state files are not platforms.
    (pairing / "_rate_limits.json").write_text(json.dumps({"x": 1}))

    state = _collect(hermes_home)

    summary = {p.platform: p for p in state.integrations.pairing_platforms}
    assert set(summary) == {"telegram", "discord"}
    assert summary["telegram"].pending_count == 1
    assert summary["telegram"].approved_count == 2
    assert summary["telegram"].newest_approved_age_seconds == 120
    assert summary["discord"].approved_count == 1
    assert summary["discord"].newest_approved_age_seconds is None
    dumped = state.integrations.model_dump_json()
    for secret in ("alice", "bob", "111", "h1"):
        assert secret not in dumped
    assert "pairing" not in state.health.failed_sources


def test_pairing_prefers_a_populated_legacy_directory(hermes_home: Path):
    legacy = _pairing_dir(hermes_home, legacy=True)
    (legacy / "slack-approved.json").write_text(json.dumps({"u": {"approved_at": _NOW}}))
    modern = _pairing_dir(hermes_home)
    (modern / "telegram-approved.json").write_text(json.dumps({"u": {}}))
    state = _collect(hermes_home)
    assert [p.platform for p in state.integrations.pairing_platforms] == ["slack"]


def test_pairing_ignores_an_empty_legacy_directory(hermes_home: Path):
    _pairing_dir(hermes_home, legacy=True)
    (_pairing_dir(hermes_home) / "telegram-approved.json").write_text(json.dumps({"u": {}}))
    state = _collect(hermes_home)
    assert [p.platform for p in state.integrations.pairing_platforms] == ["telegram"]


def test_pairing_absent_is_empty_and_healthy(hermes_home: Path):
    state = _collect(hermes_home)
    assert state.integrations.pairing_platforms == []
    assert "pairing" not in state.health.failed_sources


def test_pairing_keeps_last_good_when_store_turns_corrupt(hermes_home: Path):
    pairing = _pairing_dir(hermes_home)
    approved = pairing / "telegram-approved.json"
    approved.write_text(json.dumps({"u": {}, "v": {}}))
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        approved.write_text("{broken")
        second = c.collect()
    finally:
        c.close()
    assert first.integrations.pairing_platforms[0].approved_count == 2
    assert "pairing" in second.health.failed_sources
    assert second.integrations.pairing_platforms[0].approved_count == 2


def test_pairing_is_profile_scoped(profiled_hermes_home: Path):
    profile_home = profiled_hermes_home / "profiles" / "coding"
    (_pairing_dir(profiled_hermes_home) / "root-approved.json").write_text(json.dumps({"u": {}}))
    (_pairing_dir(profile_home) / "telegram-approved.json").write_text(json.dumps({"u": {}}))
    state = _collect(profiled_hermes_home, profile="coding")
    assert [p.platform for p in state.integrations.pairing_platforms] == ["telegram"]


# --- webhook subscriptions -----------------------------------------------


def test_webhook_subscriptions_report_names_and_enabled_counts(hermes_home: Path):
    (hermes_home / "webhook_subscriptions.json").write_text(
        json.dumps(
            {
                "github-push": {"secret": "whsec-should-never-render", "events": ["push"]},
                "alerts": {"enabled": False, "secret": "x"},
                "cron": {"enabled": True},
            }
        )
    )
    state = _collect(hermes_home)
    integrations = state.integrations
    assert integrations.webhook_subscriptions_present is True
    assert integrations.webhook_subscription_count == 3
    assert integrations.webhook_enabled_count == 2
    assert integrations.webhook_route_names == ["alerts", "cron", "github-push"]
    assert "whsec" not in integrations.model_dump_json()


def test_webhook_subscriptions_absent(hermes_home: Path):
    state = _collect(hermes_home)
    assert state.integrations.webhook_subscriptions_present is False
    assert state.integrations.webhook_subscription_count == 0


# --- shared metrics ------------------------------------------------------


def _metrics_db(home: Path) -> Path:
    path = home / "telemetry" / "shared_metrics" / "metrics.sqlite3"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE counter_aggregates (
            period_start TEXT NOT NULL, metric_name TEXT NOT NULL,
            hermes_version TEXT NOT NULL, os_family TEXT NOT NULL,
            architecture TEXT NOT NULL, install_method TEXT NOT NULL,
            dimensions_json TEXT NOT NULL, value INTEGER NOT NULL,
            packaged_value INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE package_outbox (
            package_id TEXT PRIMARY KEY, period_start TEXT NOT NULL,
            period_end TEXT NOT NULL, payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL, exported_at TEXT,
            sent_at TEXT, send_state TEXT, send_attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT, last_error TEXT, sent_install_id TEXT, claim_token TEXT
        );
        CREATE TABLE consent_marks (name TEXT PRIMARY KEY, stamp TEXT NOT NULL);
        INSERT INTO counter_aggregates VALUES
            ('2026-09-20', 'm', '1', 'darwin', 'arm64', 'git', '{}', 5, 0),
            ('2026-09-20', 'n', '1', 'darwin', 'arm64', 'git', '{}', 2, 1),
            ('2026-09-21', 'm', '1', 'darwin', 'arm64', 'git', '{}', 3, 3),
            ('2026-09-22', 'm', '1', 'darwin', 'arm64', 'git', '{}', 4, 0);
        INSERT INTO package_outbox (package_id, period_start, period_end, payload_json,
                                    created_at, send_state, last_error)
        VALUES
            ('p1', 'a', 'b', '{"secret": "payload"}', 'c', NULL, NULL),
            ('p2', 'a', 'b', '{}', 'c', 'sent', NULL),
            ('p3', 'a', 'b', '{}', 'c', 'rejected', 'HTTP 400 token=abc'),
            ('p4', 'a', 'b', '{}', 'c', 'pending', 'timeout');
        INSERT INTO consent_marks VALUES ('obs', '2026-09-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()
    return path


def test_shared_metrics_counts_rows_periods_outbox_and_consent(hermes_home: Path):
    _metrics_db(hermes_home)
    state = _collect(hermes_home)
    integrations = state.integrations
    assert integrations.shared_metrics_present is True
    assert integrations.shared_metrics_counter_rows == 4
    # Two distinct (period, resource) groups hold value > packaged_value.
    assert integrations.shared_metrics_pending_periods == 2
    assert integrations.shared_metrics_outbox_by_state == {
        "pending": 2,
        "rejected": 1,
        "sent": 1,
    }
    assert integrations.shared_metrics_outbox_error_count == 2
    assert integrations.shared_metrics_consent_marks == {"obs": "2026-09-01T00:00:00Z"}
    dumped = integrations.model_dump_json()
    assert "token=abc" not in dumped
    assert "payload" not in dumped
    assert "shared_metrics" not in state.health.failed_sources


def test_shared_metrics_tolerates_a_pre_send_column_outbox(hermes_home: Path):
    path = hermes_home / "telemetry" / "shared_metrics" / "metrics.sqlite3"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE package_outbox (package_id TEXT PRIMARY KEY, period_start TEXT,
            period_end TEXT, payload_json TEXT, created_at TEXT, exported_at TEXT);
        INSERT INTO package_outbox VALUES ('p', 'a', 'b', '{}', 'c', NULL);
        """
    )
    conn.commit()
    conn.close()
    state = _collect(hermes_home)
    integrations = state.integrations
    assert integrations.shared_metrics_present is True
    assert integrations.shared_metrics_counter_rows == 0
    assert integrations.shared_metrics_outbox_by_state == {"pending": 1}
    assert integrations.shared_metrics_outbox_error_count == 0
    assert integrations.shared_metrics_consent_marks == {}


def test_shared_metrics_absent(hermes_home: Path):
    state = _collect(hermes_home)
    assert state.integrations.shared_metrics_present is False
    assert "shared_metrics" not in state.health.failed_sources


def test_shared_metrics_keeps_last_good_on_a_corrupt_db(hermes_home: Path):
    path = _metrics_db(hermes_home)
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        path.write_bytes(b"not a database at all" * 100)
        second = c.collect()
    finally:
        c.close()
    assert first.integrations.shared_metrics_counter_rows == 4
    assert "shared_metrics" in second.health.failed_sources
    assert second.integrations.shared_metrics_counter_rows == 4


def test_shared_metrics_reuses_the_readout_until_the_db_changes(hermes_home: Path):
    path = _metrics_db(hermes_home)
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        # Same bytes, same signature: the cached readout is served.
        second = c.collect()
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM counter_aggregates")
        conn.commit()
        conn.close()
        third = c.collect()
    finally:
        c.close()
    assert first.integrations.shared_metrics_counter_rows == 4
    assert second.integrations.shared_metrics_counter_rows == 4
    assert third.integrations.shared_metrics_counter_rows == 0


# --- rate limits ---------------------------------------------------------


def test_rate_limit_holds_report_time_remaining(hermes_home: Path):
    limits = hermes_home / "rate_limits"
    limits.mkdir()
    (limits / "nous.json").write_text(
        json.dumps({"reset_at": _NOW + 240, "recorded_at": _NOW - 60, "reset_seconds": 300})
    )
    # An elapsed hold is no hold (nous_rate_limit_remaining, :89-102).
    (limits / "nous-anonymous.json").write_text(
        json.dumps({"reset_at": _NOW - 1, "recorded_at": _NOW - 400})
    )
    state = _collect(hermes_home)
    holds = state.integrations.rate_limit_holds
    assert [(h.name, h.remaining_seconds, h.recorded_age_seconds) for h in holds] == [
        ("nous", 240, 60)
    ]
    assert "rate_limits" not in state.health.failed_sources


def test_rate_limit_holds_tolerate_null_fields(hermes_home: Path):
    limits = hermes_home / "rate_limits"
    limits.mkdir()
    (limits / "nous.json").write_text(json.dumps({"reset_at": None, "recorded_at": None}))
    (limits / "nous-anonymous.json").write_text(json.dumps({"reset_at": _NOW + 5}))
    state = _collect(hermes_home)
    holds = state.integrations.rate_limit_holds
    assert [(h.name, h.recorded_age_seconds) for h in holds] == [("nous-anonymous", None)]


def test_rate_limit_holds_keep_last_good_on_corruption(hermes_home: Path):
    limits = hermes_home / "rate_limits"
    limits.mkdir()
    hold = limits / "nous.json"
    hold.write_text(json.dumps({"reset_at": _NOW + 240}))
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        hold.write_text("{nope")
        second = c.collect()
    finally:
        c.close()
    assert len(first.integrations.rate_limit_holds) == 1
    assert "rate_limits" in second.health.failed_sources
    assert len(second.integrations.rate_limit_holds) == 1
