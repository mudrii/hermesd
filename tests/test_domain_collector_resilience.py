"""Malformed ~/.hermes values degrade to defaults instead of raising."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermesd.collect.config import _config_agent_limits, _moa_config_summary
from hermesd.collect.cron import _claim_owner_pid, _cron_suggestion_count
from hermesd.collect.gateway import _receipt_looks_unfinished
from hermesd.collect.skills import _assess_mcp_cache_entry, _cache_number, _skill_frontmatter


def test_cache_number_overflowing_int_is_not_a_number():
    assert _cache_number(10**400) is None
    entry = _assess_mcp_cache_entry("srv", {"fingerprint": "abc", "ttl_ms": 10**400}, 0.0)
    assert entry.ttl_ms is None


@pytest.mark.parametrize("outcome", [["failed"], {"failed": True}])
def test_unhashable_update_outcome_is_not_unfinished(outcome: object):
    assert _receipt_looks_unfinished({"exit_code": 0, "outcome": outcome}) is False


def test_claim_owner_pid_rejects_non_ascii_digits():
    assert _claim_owner_pid({"by": "host:²"}, "host") is None
    assert _claim_owner_pid({"by": "host:١٢"}, "host") is None
    assert _claim_owner_pid({"by": "host:42"}, "host") == 42


def test_cron_suggestion_count_survives_deeply_nested_json(tmp_path: Path):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "suggestions.json").write_text("[" * 100_000 + "]" * 100_000)

    assert _cron_suggestion_count(cron_dir) == 0


def test_skill_frontmatter_survives_deeply_nested_yaml(tmp_path: Path):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nx: " + "[" * 5000 + "]" * 5000 + "\n---\n")

    assert _skill_frontmatter(path) == {}


def test_skill_frontmatter_tolerates_utf8_bom(tmp_path: Path):
    path = tmp_path / "SKILL.md"
    path.write_bytes("﻿---\ndescription: with bom\n---\n".encode())

    assert _skill_frontmatter(path) == {"description": "with bom"}


def test_moa_default_preset_with_non_string_key_is_stringified():
    summary = _moa_config_summary({"presets": {1: {}}})

    assert summary["default_preset"] == "1"


def test_plugin_name_counts_ignore_junk_and_duplicates():
    limits = _config_agent_limits(
        {"plugins": {"enabled": ["a", "a", 3, "", None, "b"], "disabled": "a"}}
    )

    assert limits["plugin_enabled_count"] == 2
    assert limits["plugin_disabled_count"] == 0
