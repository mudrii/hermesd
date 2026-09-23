"""Collector orchestration: per-source fallbacks, shared caches and guards.

Each test pins one cross-source property of ``Collector.collect()`` — which
source a failure is charged to, what its fallback restores, and what a cache
or guard keeps between passes.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermesd.collector import Collector

_SHA_NEW = "a" * 40
_SHA_OLD = "b" * 40


def _write_plugin(home: Path, name: str, *, catalog_sha: str | None = None) -> None:
    plugin_dir = home / "plugins" / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(f"name: {name}\nversion: 1.0.0\n")
    if catalog_sha is not None:
        (plugin_dir / ".hermes-catalog.json").write_text(
            json.dumps({"catalog_name": name, "sha": catalog_sha})
        )


def test_plugin_catalog_failure_keeps_the_fresh_plugin_inventory(
    hermes_home: Path, tmp_path: Path
) -> None:
    """A failed catalog enrichment restores its verdicts, never the plugin list.

    The list itself belongs to the skills source: a plugin installed after the
    last good catalog read must still appear, while the drift flag the catalog
    stamped on an already-known plugin is carried forward by name.
    """
    _write_plugin(hermes_home, "weather", catalog_sha=_SHA_OLD)
    cache_dir = hermes_home / "cache"
    cache_dir.mkdir()
    cache_path = cache_dir / "plugin-catalog.json"
    cache_path.write_text(json.dumps({"entries": [{"name": "weather", "sha": _SHA_NEW}]}))
    outside = tmp_path / "elsewhere.json"
    outside.write_text("{}")

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.skills_memory.plugins[0].catalog_update_available is True

        _write_plugin(hermes_home, "rain")
        cache_path.unlink()
        cache_path.symlink_to(outside)
        second = c.collect()
    finally:
        c.close()

    assert "plugin_catalog" in second.health.failed_sources
    by_name = {plugin.name: plugin for plugin in second.skills_memory.plugins}
    assert set(by_name) == {"weather", "rain"}
    assert by_name["weather"].catalog_update_available is True
    assert by_name["rain"].catalog_update_available is False
    assert second.skills_memory.plugin_catalog_update_count == 1
