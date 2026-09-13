"""Plugin manifest compatibility: three filenames, two directory shapes, one precedence.

Upstream's ``scan_directory`` (``hermes_cli/plugins_discovery.py:102-131``) accepts
``plugin.yaml``, ``plugin.yml`` and a portable ``plugin.json`` in that precedence, and
walks two directory shapes: a flat ``<root>/<name>/`` whose key is the manifest name, and
a category ``<root>/<cat>/<name>/`` whose key is the path ``<cat>/<name>``. A directory
with no manifest is a *category*, not a plugin, and is recursed into once; past that
upstream logs "no plugin.yaml, depth cap reached" and stops.

hermesd reproduced only the flat shape and only ``plugin.yaml``, so a ``plugin.yml``
install, a portable Agent Plugin and every category plugin were invisible — and the
invisible ones were exactly the ones an operator would most want to see, because a
category plugin is gated on its path-derived key.

One deliberate divergence is pinned here:

* upstream accepts a **symlinked** ``plugin.json``; hermesd refuses every symlink,
  because its confinement helpers are what stop a plugin tree from steering a read
  outside ``~/.hermes``.

Like upstream, hermesd always derives a plugin key from its manifest name (flat)
or path (category) and never reads a manifest ``key:`` field.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermesd.collect.plugins import (
    MANIFEST_NAMES,
    MAX_PLUGIN_SCAN_DEPTH,
    PLUGIN_SCHEMA_V1,
    category_prefix,
    choose_manifest,
    declared_capabilities,
    parse_portable_manifest,
    plugin_key,
    requires_hermes_spec,
)
from hermesd.collector import Collector
from hermesd.models import PORTABLE_MANIFEST_NAME, PluginActivation, PluginInfo

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_SCHEMA = PLUGIN_SCHEMA_V1


def _portable(name: str = "portable-one", **extra: object) -> str:
    return json.dumps({"$schema": _SCHEMA, "name": name, **extra})


def _write(home: Path, rel: str, text: str) -> Path:
    path = home / "plugins" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _write_config(home: Path, plugins: object) -> None:
    import yaml

    (home / "config.yaml").write_text(yaml.dump({"plugins": plugins}))


def _collect(home: Path):
    collector = Collector(home, pid_exists=lambda pid: False)
    try:
        return collector.collect()
    finally:
        collector.close()


def _by_key(state) -> dict[str, object]:
    return {plugin.manifest_key: plugin for plugin in state.skills_memory.plugins}


def _by_name(state) -> dict[str, object]:
    return {plugin.name: plugin for plugin in state.skills_memory.plugins}


# --------------------------------------------------------------------------
# precedence
# --------------------------------------------------------------------------


def test_manifest_names_are_listed_in_upstream_precedence_order():
    """plugins_discovery.py:115 and plugin_dev.py:154 agree on this order."""
    assert MANIFEST_NAMES == ("plugin.yaml", "plugin.yml", "plugin.json")
    assert PORTABLE_MANIFEST_NAME == "plugin.json"


def test_a_native_yaml_manifest_outranks_a_portable_one():
    choice = choose_manifest(["plugin.json", "plugin.yaml"])

    assert choice is not None
    assert choice.filename == "plugin.yaml"
    assert choice.shadowed == ("plugin.json",)


def test_plugin_yaml_outranks_plugin_yml():
    choice = choose_manifest(["plugin.yml", "plugin.yaml"])

    assert choice is not None
    assert choice.filename == "plugin.yaml"
    assert choice.shadowed == ("plugin.yml",)


def test_plugin_yml_outranks_the_portable_manifest():
    choice = choose_manifest(["plugin.json", "plugin.yml"])

    assert choice is not None
    assert choice.filename == "plugin.yml"
    assert choice.shadowed == ("plugin.json",)


def test_all_three_present_records_both_losers_in_precedence_order():
    choice = choose_manifest(["plugin.json", "plugin.yml", "plugin.yaml"])

    assert choice is not None
    assert choice.filename == "plugin.yaml"
    assert choice.shadowed == ("plugin.yml", "plugin.json")


def test_choice_of_a_lone_manifest_shadows_nothing():
    choice = choose_manifest(["plugin.yml"])

    assert choice is not None
    assert choice.filename == "plugin.yml"
    assert choice.shadowed == ()


def test_choice_from_no_manifest_is_none():
    assert choose_manifest([]) is None


def test_choice_ignores_filenames_it_does_not_own():
    """An unrelated YAML file in the directory is not a manifest candidate."""
    assert choose_manifest(["manifest.yaml", "plugin.yaml.bak", "setup.py"]) is None


def test_choice_is_independent_of_the_order_it_is_handed():
    """The caller lists a directory; precedence must not depend on readdir order."""
    forward = choose_manifest(["plugin.yaml", "plugin.yml", "plugin.json"])
    backward = choose_manifest(["plugin.json", "plugin.yml", "plugin.yaml"])

    assert forward == backward


def test_manifest_format_names_the_reader_each_filename_needs():
    """Derived on the model, so the filename and its format cannot disagree."""
    assert PluginInfo(name="x", manifest_file="plugin.yaml").manifest_format == "yaml"
    assert PluginInfo(name="x", manifest_file="plugin.yml").manifest_format == "yaml"
    assert PluginInfo(name="x", manifest_file="plugin.json").manifest_format == "portable"
    assert PluginInfo(name="x", manifest_file="plugin.toml").manifest_format == ""


# --------------------------------------------------------------------------
# key derivation
# --------------------------------------------------------------------------


def test_a_category_key_is_the_path_not_the_manifest_name():
    """parse_manifest_file:468 — ``f"{prefix}/{plugin_dir.name}"`` when prefixed."""
    key = plugin_key(prefix="observability", dirname="tracer", name="tracer")

    assert key == "observability/tracer"


def test_a_category_key_uses_the_directory_name_even_when_the_manifest_renames():
    key = plugin_key(prefix="observability", dirname="tracer", name="otel")

    assert key == "observability/tracer"


def test_a_flat_key_is_the_manifest_name_not_the_directory_name():
    key = plugin_key(prefix="", dirname="weather-dir", name="weather")

    assert key == "weather"


def test_category_prefix_nests_only_one_level_deep():
    assert category_prefix("", "observability") == "observability"
    assert category_prefix("observability", "tracer") == "observability/tracer"


def test_the_category_recursion_depth_cap_is_one():
    """depth 0 = flat, depth 1 = category child; upstream refuses to go past 1."""
    assert MAX_PLUGIN_SCAN_DEPTH == 1


# --------------------------------------------------------------------------
# portable plugin.json validation
# --------------------------------------------------------------------------


def test_a_valid_portable_manifest_carries_name_version_and_description():
    data = json.loads(_portable("portable-one", version="2.1.0", description="Portable tools"))

    parsed, error = parse_portable_manifest(data)

    assert error == ""
    assert parsed is not None
    assert (parsed.name, parsed.version, parsed.description) == (
        "portable-one",
        "2.1.0",
        "Portable tools",
    )


def test_a_portable_manifest_without_the_v1_schema_is_rejected():
    parsed, error = parse_portable_manifest({"name": "portable-one"})

    assert parsed is None
    assert "schema" in error


def test_a_portable_manifest_with_a_foreign_schema_is_rejected():
    parsed, error = parse_portable_manifest(
        {"$schema": "https://example.invalid/plugin.schema.json", "name": "portable-one"}
    )

    assert parsed is None
    assert "schema" in error


def test_a_portable_manifest_with_an_invalid_name_is_rejected():
    for bad in ("", "Upper", "-lead", "trailing-", "a--b", "a..b", "x" * 65, 7, None):
        parsed, error = parse_portable_manifest({"$schema": _SCHEMA, "name": bad})

        assert parsed is None, bad
        assert "name" in error, bad


def test_a_portable_manifest_with_a_non_string_version_is_rejected():
    parsed, error = parse_portable_manifest({"$schema": _SCHEMA, "name": "ok", "version": 2})

    assert parsed is None
    assert "version" in error


def test_a_portable_manifest_with_a_non_string_description_is_rejected():
    parsed, error = parse_portable_manifest(
        {"$schema": _SCHEMA, "name": "ok", "description": ["x"]}
    )

    assert parsed is None
    assert "description" in error


@pytest.mark.parametrize("field", ["homepage", "repository", "license"])
def test_a_portable_manifest_rejects_non_string_link_and_license_fields(field: str):
    parsed, error = parse_portable_manifest({"$schema": _SCHEMA, "name": "ok", field: 7})

    assert parsed is None
    assert field in error


@pytest.mark.parametrize("keywords", ["tools", ["tools", 7], {"tools": True}, None])
def test_a_portable_manifest_rejects_malformed_keywords(keywords: object):
    parsed, error = parse_portable_manifest(
        {"$schema": _SCHEMA, "name": "ok", "keywords": keywords}
    )

    assert parsed is None
    assert "keywords" in error


@pytest.mark.parametrize(
    "author",
    ["Alice", {"name": "Alice", "role": "maintainer"}, {"name": 7}, None],
)
def test_a_portable_manifest_rejects_malformed_author(author: object):
    parsed, error = parse_portable_manifest({"$schema": _SCHEMA, "name": "ok", "author": author})

    assert parsed is None
    assert "author" in error


def test_a_portable_manifest_rejects_non_object_extension_namespaces():
    parsed, error = parse_portable_manifest(
        {"$schema": _SCHEMA, "name": "ok", "extensions": {"tools": "invalid"}}
    )

    assert parsed is None
    assert "extension" in error


def test_a_non_object_extensions_field_is_ignored_like_upstream_diagnostic():
    parsed, error = parse_portable_manifest(
        {"$schema": _SCHEMA, "name": "ok", "extensions": "invalid"}
    )

    assert error == ""
    assert parsed is not None


def test_valid_portable_metadata_is_accepted_without_being_retained():
    parsed, error = parse_portable_manifest(
        {
            "$schema": _SCHEMA,
            "name": "ok",
            "homepage": "https://example.test",
            "repository": "https://example.test/repo",
            "license": "MIT",
            "keywords": ["tools", "weather"],
            "author": {"name": "Alice", "email": "alice@example.test", "url": "https://a.test"},
            "extensions": {"example.test/settings": {"unit": "metric"}},
        }
    )

    assert error == ""
    assert parsed is not None
    assert parsed.name == "ok"


def test_a_non_mapping_portable_manifest_is_rejected():
    for bad in (None, [], "plugin", 7):
        parsed, error = parse_portable_manifest(bad)

        assert parsed is None, bad
        assert error, bad


def test_unknown_top_level_portable_fields_are_ignored_not_fatal():
    """_validate_manifest drops them with a diagnostic; hermesd has no diagnostic sink."""
    parsed, error = parse_portable_manifest(
        {"$schema": _SCHEMA, "name": "ok", "surprise": {"a": 1}}
    )

    assert error == ""
    assert parsed is not None
    assert parsed.name == "ok"


# --------------------------------------------------------------------------
# declarations
# --------------------------------------------------------------------------


def test_declared_capabilities_are_deduplicated_in_order():
    caps, count = declared_capabilities(["tools.override", "llm.model_override", "tools.override"])

    assert caps == ["tools.override", "llm.model_override"]
    assert count == 2


def test_declared_capabilities_of_a_non_list_is_empty():
    for bad in (None, "tools.override", {"a": 1}, 7):
        assert declared_capabilities(bad) == ([], 0), bad


def test_declared_capabilities_drop_non_strings_and_blanks():
    caps, count = declared_capabilities(["tools.override", 7, "", None, "  "])

    assert caps == ["tools.override"]
    assert count == 1


def test_declared_capabilities_keep_the_true_count_past_the_display_cap():
    """A capped list must never change the count — the cap is presentation only."""
    caps, count = declared_capabilities([f"cap-{i}" for i in range(50)])

    assert count == 50
    assert len(caps) < count


def test_requires_hermes_is_a_stripped_string():
    assert requires_hermes_spec("  >=0.19  ") == ">=0.19"
    assert requires_hermes_spec(None) == ""
    assert requires_hermes_spec(7) == ""
    assert requires_hermes_spec(["x"]) == ""


def test_requires_hermes_is_bounded():
    assert len(requires_hermes_spec(">" * 5000)) <= 100


# --------------------------------------------------------------------------
# collector: formats
# --------------------------------------------------------------------------


def test_collector_discovers_a_plugin_yml_manifest(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["notes"]})
    _write(hermes_home, "notes/plugin.yml", "name: notes\nversion: 0.2.0\n")

    plugin = _by_name(_collect(hermes_home))["notes"]

    assert plugin.manifest_file == "plugin.yml"
    assert plugin.manifest_format == "yaml"
    assert plugin.version == "0.2.0"
    assert plugin.activation == PluginActivation.ENABLED


def test_collector_discovers_a_portable_plugin_json(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["portable-one"]})
    _write(
        hermes_home,
        "portable-one/plugin.json",
        _portable("portable-one", version="3.0.0", description="Portable"),
    )

    plugin = _by_name(_collect(hermes_home))["portable-one"]

    assert plugin.manifest_file == "plugin.json"
    assert plugin.manifest_format == "portable"
    assert plugin.version == "3.0.0"
    assert plugin.description == "Portable"
    assert plugin.activation == PluginActivation.ENABLED


def test_a_portable_plugin_is_standalone_without_scanning_its_source(hermes_home: Path):
    """portable_plugin_manifest never sets kind, so no __init__.py scan applies."""
    _write_config(hermes_home, {"enabled": ["portable-one"]})
    _write(hermes_home, "portable-one/plugin.json", _portable("portable-one"))
    _write(hermes_home, "portable-one/__init__.py", "register_memory_provider\n")

    plugin = _by_name(_collect(hermes_home))["portable-one"]

    assert plugin.kind == "standalone"
    assert plugin.activation == PluginActivation.ENABLED


def test_collector_records_the_losing_manifest_of_a_conflicting_pair(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["dupe"]})
    _write(hermes_home, "dupe/plugin.yaml", "name: dupe\nversion: 1.0.0\n")
    _write(hermes_home, "dupe/plugin.yml", "name: dupe\nversion: 9.9.9\n")
    _write(hermes_home, "dupe/plugin.json", _portable("dupe", version="8.8.8"))

    plugin = _by_name(_collect(hermes_home))["dupe"]

    assert plugin.manifest_file == "plugin.yaml"
    assert plugin.version == "1.0.0"
    assert plugin.manifest_shadowed == ["plugin.yml", "plugin.json"]


def test_collector_prefers_plugin_yml_over_a_portable_manifest(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["dupe"]})
    _write(hermes_home, "dupe/plugin.yml", "name: dupe\nversion: 1.0.0\n")
    _write(hermes_home, "dupe/plugin.json", _portable("dupe", version="8.8.8"))

    plugin = _by_name(_collect(hermes_home))["dupe"]

    assert plugin.manifest_file == "plugin.yml"
    assert plugin.version == "1.0.0"
    assert plugin.manifest_shadowed == ["plugin.json"]


# --------------------------------------------------------------------------
# collector: category recursion
# --------------------------------------------------------------------------


def test_collector_discovers_a_category_plugin_under_its_path_key(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["observability/tracer"]})
    _write(hermes_home, "observability/tracer/plugin.yaml", "name: tracer\nversion: 0.4.0\n")

    plugin = _by_key(_collect(hermes_home))["observability/tracer"]

    assert plugin.name == "tracer"
    assert plugin.manifest_key == "observability/tracer"
    assert plugin.activation == PluginActivation.ENABLED


def test_a_category_plugin_is_not_enabled_under_its_bare_name_alone(hermes_home: Path):
    """The allow-list carries the path key; the bare name is a legacy spelling."""
    _write_config(hermes_home, {"enabled": []})
    _write(hermes_home, "observability/tracer/plugin.yaml", "name: tracer\n")

    plugin = _by_key(_collect(hermes_home))["observability/tracer"]

    assert plugin.activation == PluginActivation.NOT_ENABLED


def test_a_category_plugin_recognises_its_legacy_bare_name_in_the_allow_list(hermes_home: Path):
    """gate_plugin matches either spelling; this pins the wiring for a category path."""
    _write_config(hermes_home, {"enabled": ["tracer"]})
    _write(hermes_home, "observability/tracer/plugin.yaml", "name: tracer\n")

    plugin = _by_key(_collect(hermes_home))["observability/tracer"]

    assert plugin.activation == PluginActivation.ENABLED


def test_a_category_deny_list_entry_matches_the_path_key(hermes_home: Path):
    _write_config(
        hermes_home, {"enabled": ["observability/tracer"], "disabled": ["observability/tracer"]}
    )
    _write(hermes_home, "observability/tracer/plugin.yaml", "name: tracer\n")

    plugin = _by_key(_collect(hermes_home))["observability/tracer"]

    assert plugin.activation == PluginActivation.DISABLED


def test_collector_discovers_a_category_plugin_from_a_yml_manifest(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["observability/tracer"]})
    _write(hermes_home, "observability/tracer/plugin.yml", "name: tracer\n")

    plugin = _by_key(_collect(hermes_home))["observability/tracer"]

    assert plugin.manifest_file == "plugin.yml"
    assert plugin.activation == PluginActivation.ENABLED


def test_collector_discovers_a_category_plugin_from_a_portable_manifest(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["agents/portable-one"]})
    _write(hermes_home, "agents/portable-one/plugin.json", _portable("portable-one"))

    plugin = _by_key(_collect(hermes_home))["agents/portable-one"]

    assert plugin.manifest_format == "portable"
    assert plugin.activation == PluginActivation.ENABLED


def test_flat_and_category_plugins_coexist_in_one_pass(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather", "observability/tracer"]})
    _write(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write(hermes_home, "observability/tracer/plugin.yaml", "name: tracer\n")

    keys = set(_by_key(_collect(hermes_home)))

    assert keys == {"weather", "observability/tracer"}


def test_a_manifest_less_directory_is_still_not_a_plugin(hermes_home: Path):
    """The pinned behaviour: an empty category yields nothing, not one phantom plugin."""
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write(hermes_home, "weather/plugin.yaml", "name: weather\n")
    (hermes_home / "plugins" / "observability").mkdir(parents=True, exist_ok=True)

    assert set(_by_name(_collect(hermes_home))) == {"weather"}


def test_a_category_of_manifest_less_children_yields_nothing(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["observability"]})
    (hermes_home / "plugins" / "observability" / "tracer").mkdir(parents=True)
    _write(hermes_home, "observability/tracer/README.md", "not a manifest\n")

    assert _by_name(_collect(hermes_home)) == {}


def test_the_recursion_stops_at_the_depth_cap(hermes_home: Path):
    """plugins/a/b/c/ is two categories deep; upstream stops and logs the depth cap."""
    _write_config(hermes_home, {"enabled": ["a/b/c"]})
    _write(hermes_home, "a/b/c/plugin.yaml", "name: deep\n")

    assert _by_name(_collect(hermes_home)) == {}


def test_a_flat_plugin_beside_a_deep_tree_is_still_discovered(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write(hermes_home, "a/b/c/plugin.yaml", "name: deep\n")

    assert set(_by_name(_collect(hermes_home))) == {"weather"}


# --------------------------------------------------------------------------
# collector: malformed and unsafe
# --------------------------------------------------------------------------


def test_a_malformed_plugin_yml_degrades_to_unknown(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["broken"]})
    _write(hermes_home, "broken/plugin.yml", "name: broken\n\t- not: valid: yaml\n")

    plugin = _by_name(_collect(hermes_home))["broken"]

    assert plugin.activation == PluginActivation.UNKNOWN
    assert plugin.enabled is False
    assert plugin.activation_reason
    assert plugin.manifest_file == "plugin.yml"


def test_a_malformed_portable_manifest_degrades_to_unknown(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["broken"]})
    _write(hermes_home, "broken/plugin.json", "{not json at all")

    plugin = _by_name(_collect(hermes_home))["broken"]

    assert plugin.activation == PluginActivation.UNKNOWN
    assert plugin.enabled is False
    assert plugin.activation_reason
    assert plugin.manifest_file == "plugin.json"


def test_a_portable_manifest_with_a_foreign_schema_degrades_to_unknown(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["broken"]})
    _write(hermes_home, "broken/plugin.json", json.dumps({"$schema": "nope", "name": "broken"}))

    plugin = _by_name(_collect(hermes_home))["broken"]

    assert plugin.activation == PluginActivation.UNKNOWN
    assert "schema" in plugin.activation_reason


def test_a_portable_manifest_with_an_invalid_name_degrades_to_unknown(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["broken"]})
    _write(hermes_home, "broken/plugin.json", json.dumps({"$schema": _SCHEMA, "name": "Bad Name"}))

    plugin = _by_name(_collect(hermes_home))["broken"]

    assert plugin.activation == PluginActivation.UNKNOWN
    assert "name" in plugin.activation_reason


def test_a_malformed_manifest_never_fails_the_whole_skills_source(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather", "broken"]})
    _write(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write(hermes_home, "broken/plugin.json", "{not json")

    state = _collect(hermes_home)

    assert "skills" not in state.health.failed_sources
    assert set(_by_name(state)) == {"weather", "broken"}


def test_a_symlinked_portable_manifest_is_refused(hermes_home: Path, tmp_path: Path):
    """Upstream accepts a symlinked plugin.json; hermesd refuses every symlink."""
    outside = tmp_path / "outside.json"
    outside.write_text(_portable("escapee"))
    plugin_dir = hermes_home / "plugins" / "escapee"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").symlink_to(outside)

    assert _by_name(_collect(hermes_home)) == {}


def test_a_manifest_symlinked_outside_the_plugins_tree_is_refused(
    hermes_home: Path, tmp_path: Path
):
    outside = tmp_path / "plugin.yaml"
    outside.write_text("name: escapee\n")
    plugin_dir = hermes_home / "plugins" / "escapee"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").symlink_to(outside)

    assert _by_name(_collect(hermes_home)) == {}


def test_an_oversized_manifest_is_refused(hermes_home: Path):
    from hermesd.collect.common import _MAX_TEXT_READ_BYTES

    _write_config(hermes_home, {"enabled": ["huge"]})
    _write(hermes_home, "huge/plugin.yaml", "name: huge\n" + "# " * _MAX_TEXT_READ_BYTES)

    assert _by_name(_collect(hermes_home)) == {}


def test_a_symlinked_category_directory_escaping_the_root_yields_nothing(
    hermes_home: Path, tmp_path: Path
):
    outside = tmp_path / "elsewhere"
    (outside / "tracer").mkdir(parents=True)
    (outside / "tracer" / "plugin.yaml").write_text("name: tracer\n")
    (hermes_home / "plugins").mkdir(parents=True, exist_ok=True)
    (hermes_home / "plugins" / "observability").symlink_to(outside)

    assert _by_name(_collect(hermes_home)) == {}


def test_the_directory_scan_is_bounded_and_says_so(hermes_home: Path):
    """~/.hermes is untrusted: the walk has a budget, and hitting it is reported."""
    from hermesd.collector import _PLUGIN_LIMIT

    _write_config(hermes_home, {"enabled": []})
    for index in range(_PLUGIN_LIMIT + 1):
        _write(hermes_home, f"plug-{index:04d}/plugin.yaml", f"name: plug-{index:04d}\n")

    state = _collect(hermes_home)

    assert len(state.skills_memory.plugins) == _PLUGIN_LIMIT
    assert state.skills_memory.plugin_scan_truncated is True


def test_the_retained_plugin_list_is_bounded_across_categories(hermes_home: Path):
    """Two categories can each stay under the per-directory cap and still overflow."""
    from hermesd.collector import _PLUGIN_DIR_ENTRY_LIMIT, _PLUGIN_LIMIT

    _write_config(hermes_home, {"enabled": []})
    per_category = _PLUGIN_DIR_ENTRY_LIMIT // 2 + 50
    for category in ("alpha", "beta"):
        for index in range(per_category):
            _write(
                hermes_home,
                f"{category}/plug-{index:04d}/plugin.yaml",
                f"name: plug-{index:04d}\n",
            )

    state = _collect(hermes_home)

    assert 2 * per_category > _PLUGIN_LIMIT
    assert len(state.skills_memory.plugins) == _PLUGIN_LIMIT
    assert state.skills_memory.plugin_scan_truncated is True


def test_an_untruncated_scan_reports_no_truncation(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write(hermes_home, "weather/plugin.yaml", "name: weather\n")

    state = _collect(hermes_home)

    assert state.skills_memory.plugin_scan_truncated is False
    assert len(state.skills_memory.plugins) == 1


def test_collector_never_executes_a_portable_plugins_code(hermes_home: Path):
    """Discovery reads bytes; a hostile plugin beside a portable manifest stays inert."""
    _write_config(hermes_home, {"enabled": ["portable-one"]})
    marker = hermes_home / "imported.txt"
    _write(hermes_home, "portable-one/plugin.json", _portable("portable-one"))
    _write(
        hermes_home,
        "portable-one/__init__.py",
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('pwned')\n",
    )

    _collect(hermes_home)

    assert not marker.exists()


# --------------------------------------------------------------------------
# model defaults
# --------------------------------------------------------------------------


def test_manifest_format_is_derived_from_the_manifest_file():
    assert PluginInfo(name="x", manifest_file="plugin.yml").manifest_format == "yaml"
    assert PluginInfo(name="x", manifest_file="plugin.json").manifest_format == "portable"
    assert PluginInfo(name="x").manifest_format == ""


def test_a_bare_plugin_info_has_no_manifest_and_no_shadowing():
    bare = PluginInfo(name="x")

    assert bare.manifest_file == ""
    assert bare.manifest_format == ""
    assert bare.manifest_shadowed == []
    assert bare.requires_hermes == ""
    assert bare.declared_capabilities == []
    assert bare.declared_capability_count == 0


def test_the_new_fields_reach_the_json_snapshot():
    dumped = PluginInfo(
        name="x", manifest_file="plugin.yaml", manifest_shadowed=["plugin.json"]
    ).model_dump(mode="json")

    assert dumped["manifest_file"] == "plugin.yaml"
    assert dumped["manifest_shadowed"] == ["plugin.json"]
    assert dumped["manifest_format"] == "yaml"
