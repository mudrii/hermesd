"""F01 — plugin activation must be read, not inferred.

Upstream gates a discovered manifest through ``plugins_discovery.gate_manifest``:
an explicit ``plugins.enabled`` opt-in is required, the deny list wins, both the
path-derived key and the manifest name can match either list, and two kinds are
owned by their category's own discovery instead of the general manager. A plugin
that is merely present on disk is *not* enabled, which is what hermesd used to
report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermesd.collect.plugins import (
    detect_kind_from_source,
    gate_plugin,
    plugin_name_set,
    resolve_plugin_kind,
)
from hermesd.collector import Collector
from hermesd.models import PluginActivation, PluginInfo

ACTIVATION_ENABLED = PluginActivation.ENABLED
ACTIVATION_DISABLED = PluginActivation.DISABLED
ACTIVATION_NOT_ENABLED = PluginActivation.NOT_ENABLED
ACTIVATION_CATEGORY_OWNED = PluginActivation.CATEGORY_OWNED
ACTIVATION_REMOVED = PluginActivation.REMOVED
ACTIVATION_UNKNOWN = PluginActivation.UNKNOWN


def _gate(
    *,
    key: str = "",
    name: str = "weather",
    kind: str = "standalone",
    enabled: frozenset[str] = frozenset(),
    disabled: frozenset[str] = frozenset(),
):
    return gate_plugin(key=key or name, name=name, kind=kind, enabled=enabled, disabled=disabled)


# --------------------------------------------------------------------------
# opt-in semantics
# --------------------------------------------------------------------------


def test_discovered_plugin_with_empty_allow_and_deny_lists_is_not_enabled():
    """The reproduced defect: presence on disk is not activation."""
    assert _gate().activation == ACTIVATION_NOT_ENABLED


def test_plugin_listed_in_enabled_is_enabled():
    assert _gate(enabled=frozenset({"weather"})).activation == ACTIVATION_ENABLED


def test_deny_list_takes_precedence_over_the_allow_list():
    gate = _gate(enabled=frozenset({"weather"}), disabled=frozenset({"weather"}))

    assert gate.activation == ACTIVATION_DISABLED
    assert gate.reason == "disabled via config"


def test_conflicting_lists_never_report_enabled():
    assert _gate(enabled=frozenset({"weather"}), disabled=frozenset({"weather"})).activation != (
        ACTIVATION_ENABLED
    )


@pytest.mark.parametrize("legacy_key", ["nemo_relay", "observability/nemo_relay"])
def test_legacy_relay_plugin_is_reported_removed(legacy_key: str):
    gate = _gate(name=legacy_key, enabled=frozenset({legacy_key}))

    assert gate.activation == ACTIVATION_REMOVED
    assert "Relay" in gate.reason


def test_legacy_relay_refusal_wins_over_the_deny_list():
    gate = _gate(name="nemo_relay", disabled=frozenset({"nemo_relay"}))

    assert gate.activation == ACTIVATION_REMOVED


# --------------------------------------------------------------------------
# canonical vs legacy names
# --------------------------------------------------------------------------


def test_canonical_key_matches_when_the_bare_name_does_not():
    gate = _gate(
        key="observability/tracer", name="tracer", enabled=frozenset({"observability/tracer"})
    )

    assert gate.activation == ACTIVATION_ENABLED


def test_legacy_bare_name_still_matches_a_canonical_key():
    gate = _gate(key="observability/tracer", name="tracer", enabled=frozenset({"tracer"}))

    assert gate.activation == ACTIVATION_ENABLED


def test_deny_list_matches_either_the_key_or_the_name():
    by_key = _gate(
        key="observability/tracer", name="tracer", disabled=frozenset({"observability/tracer"})
    )
    by_name = _gate(key="observability/tracer", name="tracer", disabled=frozenset({"tracer"}))

    assert by_key.activation == ACTIVATION_DISABLED
    assert by_name.activation == ACTIVATION_DISABLED


def test_unrelated_names_do_not_match():
    gate = _gate(key="observability/tracer", name="tracer", enabled=frozenset({"observability"}))

    assert gate.activation == ACTIVATION_NOT_ENABLED


# --------------------------------------------------------------------------
# category-owned kinds
# --------------------------------------------------------------------------


def test_exclusive_kind_is_owned_by_its_category_not_the_allow_list():
    gate = _gate(kind="exclusive", enabled=frozenset({"weather"}))

    assert gate.activation == ACTIVATION_CATEGORY_OWNED
    assert "provider config" in gate.reason


def test_exclusive_kind_ignores_the_allow_list_entirely():
    assert _gate(kind="exclusive").activation == ACTIVATION_CATEGORY_OWNED


def test_model_provider_kind_is_active_via_providers_discovery():
    gate = _gate(kind="model-provider")

    assert gate.activation == ACTIVATION_ENABLED
    assert "providers discovery" in gate.reason


def test_deny_list_still_wins_over_a_category_kind():
    gate = _gate(kind="model-provider", disabled=frozenset({"weather"}))

    assert gate.activation == ACTIVATION_DISABLED


# --------------------------------------------------------------------------
# config name-set parsing
# --------------------------------------------------------------------------


def test_name_set_keeps_only_strings_from_a_list():
    assert plugin_name_set(["a", "b", 3, None, ""]) == frozenset({"a", "b"})


@pytest.mark.parametrize("value", [None, "everything", {"a": 1}, 7])
def test_name_set_of_a_non_list_is_empty(value: object):
    assert plugin_name_set(value) == frozenset()


# --------------------------------------------------------------------------
# kind resolution
# --------------------------------------------------------------------------


def test_declared_kind_is_normalized():
    assert resolve_plugin_kind("  Backend  ", "") == "backend"


def test_declared_kind_wins_over_source_markers():
    assert resolve_plugin_kind("standalone", "register_memory_provider") == "standalone"


@pytest.mark.parametrize("marker", ["register_memory_provider", "MemoryProvider"])
def test_undeclared_kind_detects_an_exclusive_memory_provider(marker: str):
    assert resolve_plugin_kind(None, f"from x import y\nclass Foo({marker}): ...") == "exclusive"


def test_undeclared_kind_detects_a_model_provider():
    source = "def register_provider(p: ProviderProfile): ..."

    assert resolve_plugin_kind(None, source) == "model-provider"


def test_model_provider_detection_needs_both_markers():
    assert resolve_plugin_kind(None, "def register_provider(p): ...") == "standalone"
    assert resolve_plugin_kind(None, "class ProviderProfile: ...") == "standalone"


def test_undeclared_kind_without_source_is_standalone():
    assert resolve_plugin_kind(None, "") == "standalone"
    assert detect_kind_from_source("") == "standalone"


def test_kind_detection_scan_is_bounded():
    """Upstream reads only the first 8 KiB; a marker past that must not be seen."""
    source = "# padding\n" * 2000 + "register_memory_provider"

    assert len(source) > 8192
    assert resolve_plugin_kind(None, source) == "standalone"


# --------------------------------------------------------------------------
# collector integration
# --------------------------------------------------------------------------


def _write_plugin(home: Path, dirname: str, manifest: str, *, init_source: str = "") -> Path:
    plugin_dir = home / "plugins" / dirname
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(manifest)
    if init_source:
        (plugin_dir / "__init__.py").write_text(init_source)
    return plugin_dir


def _write_config(home: Path, plugins: object) -> None:
    import yaml

    (home / "config.yaml").write_text(yaml.dump({"plugins": plugins}))


def _collect(home: Path):
    collector = Collector(home, pid_exists=lambda pid: False)
    try:
        return collector.collect()
    finally:
        collector.close()


def _by_name(state) -> dict[str, object]:
    return {plugin.name: plugin for plugin in state.skills_memory.plugins}


def test_collector_reports_not_enabled_for_empty_allow_and_deny_lists(hermes_home: Path):
    _write_config(hermes_home, {"enabled": [], "disabled": []})
    _write_plugin(hermes_home, "notes", "name: notes\nversion: 0.1.0\n")

    plugin = _by_name(_collect(hermes_home))["notes"]

    assert plugin.activation == ACTIVATION_NOT_ENABLED
    assert plugin.enabled is False


def test_collector_reports_not_enabled_when_plugins_section_is_absent(hermes_home: Path):
    _write_plugin(hermes_home, "notes", "name: notes\nversion: 0.1.0\n")

    plugin = _by_name(_collect(hermes_home))["notes"]

    assert plugin.activation == ACTIVATION_NOT_ENABLED


def test_collector_separates_enabled_disabled_and_not_enabled(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather", "notes"], "disabled": ["blocked"]})
    _write_plugin(hermes_home, "weather", "name: weather\nversion: 1.2.3\n")
    _write_plugin(hermes_home, "blocked", "name: blocked\n")
    _write_plugin(hermes_home, "notes", "name: notes\n")
    _write_plugin(hermes_home, "stray", "name: stray\n")

    plugins = _by_name(_collect(hermes_home))

    assert plugins["weather"].activation == ACTIVATION_ENABLED
    assert plugins["blocked"].activation == ACTIVATION_DISABLED
    assert plugins["notes"].activation == ACTIVATION_ENABLED
    assert plugins["stray"].activation == ACTIVATION_NOT_ENABLED


def test_collector_applies_deny_list_precedence(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"], "disabled": ["weather"]})
    _write_plugin(hermes_home, "weather", "name: weather\n")

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.activation == ACTIVATION_DISABLED
    assert plugin.enabled is False


def test_collector_matches_a_canonical_manifest_key(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["observability/tracer"]})
    _write_plugin(hermes_home, "tracer", "name: tracer\nkey: observability/tracer\n")

    plugin = _by_name(_collect(hermes_home))["tracer"]

    assert plugin.manifest_key == "observability/tracer"
    assert plugin.activation == ACTIVATION_ENABLED


def test_collector_routes_an_undeclared_memory_provider_to_its_category(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["memx"]})
    _write_plugin(
        hermes_home,
        "memx",
        "name: memx\n",
        init_source="from hermes import register_memory_provider\n",
    )

    plugin = _by_name(_collect(hermes_home))["memx"]

    assert plugin.kind == "exclusive"
    assert plugin.activation == ACTIVATION_CATEGORY_OWNED
    assert plugin.enabled is False


def test_collector_reports_unknown_for_an_unreadable_manifest(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["broken"]})
    _write_plugin(hermes_home, "broken", "name: broken\n\t- not: valid: yaml\n")

    plugin = _by_name(_collect(hermes_home))["broken"]

    assert plugin.activation == ACTIVATION_UNKNOWN
    assert plugin.enabled is False
    assert plugin.activation_reason


def test_collector_does_not_report_a_manifest_less_directory_as_a_plugin(hermes_home: Path):
    """Upstream reads a manifest-less directory as a category and recurses into it."""
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather", "name: weather\n")
    (hermes_home / "plugins" / "observability").mkdir(parents=True, exist_ok=True)

    assert set(_by_name(_collect(hermes_home))) == {"weather"}


def test_collector_records_the_declared_kind(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["spotify"]})
    _write_plugin(hermes_home, "spotify", "name: spotify\nkind: backend\n")

    plugin = _by_name(_collect(hermes_home))["spotify"]

    assert plugin.kind == "backend"
    assert plugin.activation == ACTIVATION_ENABLED


def test_collector_does_not_execute_plugin_code(hermes_home: Path):
    """Kind detection reads text; a hostile __init__.py must never be imported."""
    _write_config(hermes_home, {"enabled": ["hostile"]})
    marker = hermes_home / "imported.txt"
    _write_plugin(
        hermes_home,
        "hostile",
        "name: hostile\n",
        init_source=f"import pathlib; pathlib.Path({str(marker)!r}).write_text('pwned')\n",
    )

    _collect(hermes_home)

    assert not marker.exists()


def test_activation_states_are_all_represented():
    """Guard the vocabulary the panel and JSON output depend on."""
    assert (
        len(
            {
                ACTIVATION_ENABLED,
                ACTIVATION_DISABLED,
                ACTIVATION_NOT_ENABLED,
                ACTIVATION_CATEGORY_OWNED,
                ACTIVATION_REMOVED,
                ACTIVATION_UNKNOWN,
            }
        )
        == 6
    )


def test_enabled_is_derived_from_activation_and_reaches_the_json_snapshot():
    """Compact, detail and JSON must agree, so `enabled` cannot drift from `activation`."""
    not_enabled = PluginInfo(name="notes", activation=PluginActivation.NOT_ENABLED)
    enabled = PluginInfo(name="weather", activation=PluginActivation.ENABLED)

    assert not_enabled.enabled is False
    assert enabled.enabled is True

    dumped = not_enabled.model_dump(mode="json")
    assert dumped["activation"] == "not_enabled"
    assert dumped["enabled"] is False


def test_activation_defaults_to_unknown_not_enabled():
    """A bare PluginInfo must not claim activation it has no evidence for."""
    assert PluginInfo(name="x").activation == ACTIVATION_UNKNOWN
    assert PluginInfo(name="x").enabled is False
