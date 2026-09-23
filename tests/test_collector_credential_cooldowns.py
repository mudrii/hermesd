"""Credential-pool cooldowns read from auth.json the way upstream benches entries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermesd.collector import Collector
from hermesd.models import CredentialPoolEntry

_NOW = 1_800_000_000.0


def _pools(hermes_home: Path, pool: dict) -> list[CredentialPoolEntry]:
    (hermes_home / "auth.json").write_text(json.dumps({"credential_pool": pool}))
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        return c.collect().skills_memory.credential_pools
    finally:
        c.close()


def test_cooldown_reads_last_error_reset_at(hermes_home: Path):
    # _exhausted_until (agent/credential_pool.py:468-480): an exhausted entry is
    # benched until last_error_reset_at when the provider named one.
    pools = _pools(
        hermes_home,
        {
            "codex": [
                {
                    "label": "Codex",
                    "last_status": "exhausted",
                    "last_status_at": _NOW - 60,
                    "last_error_code": 429,
                    "last_error_reset_at": _NOW + 600,
                }
            ]
        },
    )
    assert pools[0].cooldown_remaining_seconds == pytest.approx(600)


def test_cooldown_accepts_iso_and_millisecond_reset_at(hermes_home: Path):
    reset_iso = datetime.fromtimestamp(_NOW + 120, tz=UTC).isoformat()
    pools = _pools(
        hermes_home,
        {
            "a": [{"last_status": "exhausted", "last_error_reset_at": reset_iso}],
            "b": [{"last_status": "exhausted", "last_error_reset_at": (_NOW + 30) * 1000}],
        },
    )
    by_name = {pool.name: pool for pool in pools}
    assert by_name["a"].cooldown_remaining_seconds == pytest.approx(120)
    assert by_name["b"].cooldown_remaining_seconds == pytest.approx(30)


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        # No reset_at: last_status_at + the TTL for the error code
        # (_exhausted_ttl, agent/credential_pool.py:372-398). A 429 with a
        # rotation partner benches for an hour.
        (
            [
                {
                    "last_status": "exhausted",
                    "last_status_at": _NOW - 600,
                    "last_error_code": 429,
                    "priority": 0,
                },
                {"last_status": "ok", "priority": 1},
            ],
            3000.0,
        ),
        # 401 keeps its own five-minute TTL.
        (
            [{"last_status": "exhausted", "last_status_at": _NOW - 60, "last_error_code": 401}],
            240.0,
        ),
        # A sole credential's transient throttle is capped at 60s...
        (
            [{"last_status": "exhausted", "last_status_at": _NOW - 30, "last_error_code": 429}],
            30.0,
        ),
        # ...a dead sibling does not count as a rotation partner...
        (
            [
                {
                    "last_status": "exhausted",
                    "last_status_at": _NOW - 30,
                    "last_error_code": 429,
                    "priority": 0,
                },
                {"last_status": "dead", "priority": 1},
            ],
            30.0,
        ),
        # ...but billing keeps the full bench even when sole.
        (
            [{"last_status": "exhausted", "last_status_at": _NOW - 30, "last_error_code": 402}],
            3570.0,
        ),
        (
            [
                {
                    "last_status": "exhausted",
                    "last_status_at": _NOW - 30,
                    "last_error_code": 403,
                    "failure_reason": "billing",
                }
            ],
            3570.0,
        ),
        # Unverified billing is short regardless of pool size.
        (
            [
                {
                    "last_status": "exhausted",
                    "last_status_at": _NOW - 30,
                    "last_error_code": 403,
                    "failure_reason": "billing_unverified",
                    "priority": 0,
                },
                {"last_status": "ok", "priority": 1},
            ],
            30.0,
        ),
        # Elapsed cooldowns, missing stamps and non-exhausted entries: none.
        ([{"last_status": "exhausted", "last_error_reset_at": _NOW - 1}], None),
        ([{"last_status": "exhausted"}], None),
        ([{"last_status": "ok", "last_error_reset_at": _NOW + 600}], None),
        ([{"last_status": None, "last_error_reset_at": None}], None),
    ],
)
def test_cooldown_ttl_fallback(hermes_home: Path, entries: list, expected: float | None):
    pools = _pools(hermes_home, {"codex": entries})
    if expected is None:
        assert pools[0].cooldown_remaining_seconds is None
    else:
        assert pools[0].cooldown_remaining_seconds == pytest.approx(expected)


def test_active_model_cooldowns_merge_across_entries(hermes_home: Path):
    # model_cooldowns {model: epoch} (agent/credential_pool.py:236-239,
    # agent/credential_pool_model_cooldowns.py:34-44): latest reset per model
    # wins; expired and non-numeric values are dropped.
    pools = _pools(
        hermes_home,
        {
            "anthropic": [
                {
                    "priority": 0,
                    "model_cooldowns": {
                        "claude-opus": _NOW + 300,
                        "claude-haiku": _NOW - 5,
                        "bogus": "soon",
                    },
                },
                {
                    "priority": 1,
                    "model_cooldowns": {"claude-opus": _NOW + 900, "a-model": _NOW + 5},
                },
                {"priority": 2, "model_cooldowns": None},
            ]
        },
    )
    assert [(item.model, item.remaining_seconds) for item in pools[0].model_cooldowns] == [
        ("a-model", pytest.approx(5)),
        ("claude-opus", pytest.approx(900)),
    ]
    assert pools[0].cooldown_remaining_seconds is None


def test_dict_shaped_pool_entry_reports_model_cooldowns(hermes_home: Path):
    pools = _pools(
        hermes_home,
        {"anthropic": {"model_cooldowns": {"claude-opus": _NOW + 60}}},
    )
    assert [item.model for item in pools[0].model_cooldowns] == ["claude-opus"]
