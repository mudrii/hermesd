"""Plugin catalog drift, removals and unmanaged installs (cache/plugin-catalog.json).

Mirrors the catalog-side logic hermes-agent applies on install/update
(``hermes_cli/plugin_catalog.py``, ``hermes_cli/plugins_cmd_catalog.py``):
the live catalog cache carries ``{"entries": [...], "removed": [...]}``, an
installed plugin's ``.hermes-catalog.json`` sidecar records the reviewed sha,
and ``.install-metadata.json`` records what was actually installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermesd.collect.plugins import (
    RemovedCatalogEntry,
    normalize_repo,
    parse_catalog_cache,
    removed_catalog_match,
)
from hermesd.collector import Collector
from hermesd.models import PluginInfo
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import render_to_str

_SHA_NEW = "a" * 40
_SHA_OLD = "b" * 40


def _write_plugin(
    home: Path, name: str, *, sidecar: dict[str, object] | None, pin: bool = False
) -> None:
    plugin_dir = home / "plugins" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(f"name: {name}\nversion: 1.0.0\n")
    if sidecar is not None:
        (plugin_dir / ".hermes-catalog.json").write_text(json.dumps(sidecar))
    if pin:
        metadata = home / "plugins" / ".install-metadata.json"
        current = json.loads(metadata.read_text()) if metadata.exists() else {}
        current[name] = {
            "pinned": True,
            "revision": _SHA_OLD,
            "source": "https://git.example.com/x.git",
        }
        metadata.write_text(json.dumps(current))


def _write_catalog_cache(
    home: Path, *, entries: list[dict[str, object]], removed: list[dict[str, object]]
) -> Path:
    cache_dir = home / "cache"
    cache_dir.mkdir(exist_ok=True)
    path = cache_dir / "plugin-catalog.json"
    path.write_text(json.dumps({"entries": entries, "removed": removed}))
    return path


def test_parse_catalog_cache_reads_entries_and_removed():
    entries, removed = parse_catalog_cache(
        {
            "entries": [
                {"name": "weather", "sha": _SHA_NEW, "repo": "https://git.example.com/weather.git"},
                {"name": "", "sha": _SHA_NEW},  # no name: dropped
                "junk",
            ],
            "removed": [
                {
                    "name": "evil",
                    "repo": "https://git.example.com/evil.git",
                    "reason": "malicious",
                    "date": "2026-09-01",
                },
                {"repo": "orphan"},
                "junk",
            ],
            "generated_at": "2026-09-07T00:00:00Z",
        }
    )

    assert set(entries) == {"weather"}
    assert entries["weather"].sha == _SHA_NEW
    assert removed == [
        RemovedCatalogEntry(
            name="evil",
            repo="https://git.example.com/evil.git",
            reason="malicious",
            date="2026-09-01",
        )
    ]


def test_parse_catalog_cache_tolerates_wrong_shapes():
    assert parse_catalog_cache({}) == ({}, [])
    assert parse_catalog_cache({"entries": "junk", "removed": 4}) == ({}, [])
    assert parse_catalog_cache("junk") == ({}, [])


def test_normalize_repo_is_git_and_slash_insensitive():
    assert normalize_repo("https://Git.Example.com/Repo.git/") == "https://git.example.com/repo"
    assert normalize_repo("https://git.example.com/repo") == "https://git.example.com/repo"
    assert normalize_repo("") == ""


def test_removed_match_by_name_and_by_normalized_repo():
    removed = [
        RemovedCatalogEntry(
            name="evil", repo="https://git.example.com/evil.git", reason="malicious"
        )
    ]

    assert removed_catalog_match("evil", "other", removed=removed).reason == "malicious"
    assert (
        removed_catalog_match("other", "https://git.example.com/evil.git/", removed=removed)
        is removed[0]
    )
    assert removed_catalog_match("fine", "also-fine", removed=removed) is None
    assert removed_catalog_match("evil", "x", removed=[]) is None


def test_unmanaged_computed_from_both_missing_sidecars():
    assert PluginInfo(name="local-dev").unmanaged is True
    assert PluginInfo(name="pinned-one", installed_revision=_SHA_OLD).unmanaged is False
    assert PluginInfo(name="catalog-one", catalog_name="weather").unmanaged is False


def test_collector_flags_update_removed_and_unmanaged(hermes_home: Path):
    _write_plugin(
        hermes_home,
        "weather",
        sidecar={
            "catalog_name": "weather",
            "repo": "https://git.example.com/weather",
            "sha": _SHA_OLD,
        },
    )
    _write_plugin(
        hermes_home,
        "evil",
        sidecar={"catalog_name": "evil", "repo": "https://git.example.com/evil", "sha": _SHA_NEW},
    )
    _write_plugin(hermes_home, "local-dev", sidecar=None)
    _write_plugin(hermes_home, "pinned-fresh", sidecar=None, pin=True)
    _write_catalog_cache(
        hermes_home,
        entries=[
            {"name": "weather", "sha": _SHA_NEW, "repo": "https://git.example.com/weather"},
            {"name": "evil", "sha": _SHA_NEW, "repo": "https://git.example.com/evil"},
        ],
        removed=[{"name": "evil", "repo": "https://git.example.com/evil", "reason": "malicious"}],
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    sm = state.skills_memory
    assert sm.plugin_catalog_cache_present is True
    assert sm.plugin_catalog_cache_age_seconds is not None
    by_name = {plugin.name: plugin for plugin in sm.plugins}
    assert by_name["weather"].catalog_update_available is True
    assert by_name["weather"].catalog_removed is False
    assert by_name["evil"].catalog_removed is True
    assert by_name["evil"].catalog_removed_reason == "malicious"
    assert by_name["local-dev"].unmanaged is True
    assert by_name["pinned-fresh"].unmanaged is False
    assert by_name["pinned-fresh"].pinned_revision == _SHA_OLD
    assert sm.plugin_catalog_update_count == 1
    assert sm.plugin_catalog_removed_count == 1
    assert "plugin_catalog" not in state.health.failed_sources


def test_collector_absent_catalog_cache_is_healthy_and_makes_no_claims(hermes_home: Path):
    _write_plugin(
        hermes_home,
        "weather",
        sidecar={"catalog_name": "weather", "sha": _SHA_OLD},
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    sm = state.skills_memory
    assert sm.plugin_catalog_cache_present is False
    assert sm.plugin_catalog_update_count == 0
    assert sm.plugin_catalog_removed_count == 0
    plugin = sm.plugins[0]
    assert plugin.catalog_update_available is False
    assert plugin.catalog_removed is False
    assert "plugin_catalog" not in state.health.failed_sources


def test_collector_catalog_cache_symlink_fails_its_own_source(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "plugin-catalog.json").write_text("{}")
    cache_dir = hermes_home / "cache"
    cache_dir.mkdir()
    (cache_dir / "plugin-catalog.json").symlink_to(outside / "plugin-catalog.json")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "plugin_catalog" in state.health.failed_sources
    assert state.skills_memory.plugin_catalog_cache_present is False


def test_collector_catalog_drift_failure_keeps_last_good_flags(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_plugin(
        hermes_home,
        "weather",
        sidecar={"catalog_name": "weather", "sha": _SHA_OLD},
    )
    _write_catalog_cache(
        hermes_home,
        entries=[{"name": "weather", "sha": _SHA_NEW}],
        removed=[],
    )
    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.skills_memory.plugin_catalog_update_count == 1

        import hermesd.collector as collector_module

        def boom(current):
            raise RuntimeError("scan exploded")

        monkeypatch.setattr(collector_module.Collector, "_with_plugin_catalog", boom)
        second = c.collect()
    finally:
        c.close()

    assert "plugin_catalog" in second.health.failed_sources
    assert second.skills_memory.plugin_catalog_update_count == 1


def test_collector_junk_cache_payload_makes_no_update_claims(hermes_home: Path):
    _write_plugin(
        hermes_home,
        "weather",
        sidecar={"catalog_name": "weather", "sha": _SHA_OLD},
    )
    cache_dir = hermes_home / "cache"
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / "plugin-catalog.json").write_text('{"entries": 3, "removed": null}')

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    sm = state.skills_memory
    assert sm.plugin_catalog_cache_present is True
    assert sm.plugin_catalog_update_count == 0
    assert "plugin_catalog" not in state.health.failed_sources


def test_panel_shows_catalog_state_column_and_notes(hermes_home: Path):
    _write_plugin(
        hermes_home,
        "weather",
        sidecar={"catalog_name": "weather", "sha": _SHA_OLD},
    )
    _write_plugin(
        hermes_home,
        "evil",
        sidecar={"catalog_name": "evil", "sha": _SHA_NEW},
    )
    _write_plugin(hermes_home, "local-dev", sidecar=None)
    _write_catalog_cache(
        hermes_home,
        entries=[{"name": "weather", "sha": _SHA_NEW}],
        removed=[{"name": "evil", "reason": "malicious code"}],
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    detail = render_to_str(render_panel(7, state, Theme(), detail=True), width=220)
    compact = render_to_str(render_panel(7, state, Theme(), detail=False), width=120)

    assert "Catalog" in detail
    assert "update available" in detail
    assert "removed" in detail
    assert "malicious code" in detail
    assert "unmanaged" in detail
    assert "no catalog cache observed" not in detail
    assert "Catalog:" in compact
    assert "1 updates" in compact or "1 update" in compact
    assert "1 removed" in compact


def test_panel_notes_absent_catalog_cache(hermes_home: Path):
    _write_plugin(hermes_home, "weather", sidecar=None)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    detail = render_to_str(render_panel(7, state, Theme(), detail=True), width=220)

    assert "no catalog cache observed" in detail


def test_collect_free_tier_provider_marker(hermes_home: Path):
    """providers.nous with auth_method/account_tier "anonymous" is the free tier.

    Mirrors ``is_guest_state`` + ``ANON_ACCOUNT_TIER``
    (``hermes_cli/anon_auth.py:39-41,88-89,271-272``). Key names only: the
    state's token fields are never read.
    """
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "providers": {
                    "nous": {
                        "auth_method": "anonymous",
                        "account_tier": "anonymous",
                        "anon_token": "anon-secret-value-never-read",
                        "access_token": "jwt-value-never-read",
                    },
                    "openai-codex": {"auth_method": "api_key", "account_tier": "paid"},
                    "half-anon": {"auth_method": "anonymous"},
                },
                "active_provider": "nous",
            }
        )
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    providers = {provider.name: provider for provider in state.skills_memory.providers}
    assert providers["nous"].free_tier is True
    assert providers["nous"].is_active is True
    assert providers["openai-codex"].free_tier is False
    assert providers["half-anon"].free_tier is False
    # Key names only: no token value may surface anywhere in the state.
    assert "anon-secret-value-never-read" not in state.model_dump_json()
    assert "jwt-value-never-read" not in state.model_dump_json()


def test_free_tier_badge_renders_next_to_the_provider_list(hermes_home: Path):
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "providers": {
                    "nous": {"auth_method": "anonymous", "account_tier": "anonymous"},
                    "openai-codex": {"auth_method": "api_key"},
                }
            }
        )
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    detail = render_to_str(render_panel(7, state, Theme(), detail=True), width=160)
    compact = render_to_str(render_panel(7, state, Theme(), detail=False), width=120)

    assert "Nous free tier" in detail
    assert "Nous free tier" in compact
    assert "nous" in compact
