"""Plugin provenance: what is installed, what was reviewed, and whether they agree.

Two sidecars, and conflating them is the trap this file exists to prevent.

``plugins/.install-metadata.json`` (``hermes_cli/plugins_cmd.py:425-446``) is one JSON
object keyed by **manifest name** — not directory name — whose entries are written at
``:670-672`` as ``{"pinned": requested_revision is not None, "revision": installed_revision,
"source": source}``. ``revision`` is the HEAD actually installed; ``pinned`` is True only for
an explicit ``--ref`` install, and ``pinned_revision()`` (``:449-454``) returns the SHA only
in that case.

``<plugin_dir>/.hermes-catalog.json`` (``hermes_cli/plugins_cmd_catalog.py:62-85``) records
the catalog entry that was installed: ``catalog_name``, ``repo``, ``sha``, ``tier``,
``installed_at``. Absent or corrupt means a non-catalog install — ``read_catalog_sidecar``
returns ``None``, never an error.

They disagree by design. ``install_catalog_entry`` (``:108-118``) installs at
``ref or entry.sha`` and then writes the sidecar from ``entry`` — so the sidecar always
records the catalog's *reviewed* SHA while the install metadata records the *installed* HEAD.
With an explicit ``--ref`` those differ, and the catalog sidecar alone is not evidence of
what code is on disk. hermesd therefore shows both and marks the disagreement.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermesd.collect.plugins import (
    CATALOG_SIDECAR_NAME,
    INSTALL_METADATA_NAME,
    catalog_provenance,
    install_provenance,
)
from hermesd.collector import Collector
from hermesd.file_cache import LastGoodFileCache
from hermesd.models import PluginActivation, PluginInfo

CATALOG_SHA = "a" * 40
INSTALLED_SHA = "b" * 40
SAME_SHA = "c" * 40

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_plugin(home: Path, rel: str, manifest: str) -> Path:
    path = home / "plugins" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest)
    return path.parent


def _write_metadata(home: Path, payload: object) -> Path:
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    path = home / "plugins" / INSTALL_METADATA_NAME
    path.write_text(json.dumps(payload))
    return path


def _write_sidecar(plugin_dir: Path, payload: object) -> Path:
    path = plugin_dir / CATALOG_SIDECAR_NAME
    path.write_text(json.dumps(payload))
    return path


def _sidecar(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "catalog_name": "weather",
        "repo": "https://github.com/nousresearch/hermes-plugins.git",
        "sha": CATALOG_SHA,
        "tier": "official",
        "installed_at": "2026-09-01T10:00:00Z",
    }
    data.update(overrides)
    return data


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


# --------------------------------------------------------------------------
# install metadata parsing
# --------------------------------------------------------------------------


def test_a_pinned_entry_yields_both_the_revision_and_the_pin():
    provenance = install_provenance({"pinned": True, "revision": INSTALLED_SHA, "source": "s"})

    assert provenance.revision == INSTALLED_SHA
    assert provenance.pinned_revision == INSTALLED_SHA


def test_an_unpinned_entry_yields_the_revision_but_no_pin():
    """``pinned`` is True only for an explicit --ref install (plugins_cmd.py:670)."""
    provenance = install_provenance({"pinned": False, "revision": INSTALLED_SHA, "source": "s"})

    assert provenance.revision == INSTALLED_SHA
    assert provenance.pinned_revision == ""


def test_a_truthy_but_non_boolean_pinned_flag_is_not_a_pin():
    """pinned_revision() checks ``is True``; "yes" must not become a pin."""
    provenance = install_provenance({"pinned": "yes", "revision": INSTALLED_SHA})

    assert provenance.pinned_revision == ""


def test_a_short_or_malformed_revision_is_not_reported_as_one():
    """Fail closed: hermesd never renders arbitrary text as a commit SHA."""
    for bad in ("deadbeef", "not-a-sha", "", None, 7, "z" * 40):
        provenance = install_provenance({"pinned": True, "revision": bad})

        assert provenance.revision == "", bad
        assert provenance.pinned_revision == "", bad


def test_an_uppercase_revision_is_normalized_to_lowercase():
    provenance = install_provenance({"pinned": True, "revision": INSTALLED_SHA.upper()})

    assert provenance.revision == INSTALLED_SHA
    assert provenance.pinned_revision == INSTALLED_SHA


def test_a_non_mapping_entry_is_no_provenance():
    for bad in (None, [], "x", 7):
        assert install_provenance(bad) == install_provenance(None), bad


def test_credentials_in_the_recorded_source_are_redacted():
    """Upstream scrubs before writing; hermesd does not trust that it did."""
    provenance = install_provenance(
        {"revision": INSTALLED_SHA, "source": "https://user:hunter2@github.com/o/r.git"}
    )

    assert "hunter2" not in provenance.source
    assert "user" not in provenance.source
    assert provenance.source.startswith("https://[REDACTED]@github.com")


def test_a_monorepo_subdir_fragment_survives_redaction():
    provenance = install_provenance(
        {"revision": INSTALLED_SHA, "source": "https://github.com/o/r.git#plugins/weather"}
    )

    assert provenance.source.endswith("#plugins/weather")


def test_an_overlong_source_is_bounded():
    provenance = install_provenance({"revision": INSTALLED_SHA, "source": "s" * 5000})

    assert len(provenance.source) <= 256


# --------------------------------------------------------------------------
# catalog sidecar parsing
# --------------------------------------------------------------------------


def test_a_catalog_sidecar_yields_its_reviewed_pin():
    provenance = catalog_provenance(_sidecar())

    assert provenance is not None
    assert (provenance.name, provenance.tier) == ("weather", "official")
    assert provenance.sha == CATALOG_SHA
    assert provenance.installed_at == "2026-09-01T10:00:00Z"


def test_a_sidecar_without_a_catalog_name_parses_as_no_provenance():
    """read_catalog_sidecar returns None unless catalog_name is truthy."""
    assert catalog_provenance(_sidecar(catalog_name="")) is None
    assert catalog_provenance({"repo": "https://x", "sha": CATALOG_SHA}) is None


def test_a_non_mapping_sidecar_is_a_non_catalog_install():
    for bad in (None, [], "x", 7):
        assert catalog_provenance(bad) is None, bad


def test_a_sidecar_with_a_malformed_sha_keeps_the_install_but_reports_no_sha():
    """A corrupt sha is not a revision; guessing one would fabricate provenance."""
    provenance = catalog_provenance(_sidecar(sha="nope"))

    assert provenance is not None
    assert provenance.sha == ""


def test_credentials_in_the_catalog_repo_are_redacted():
    provenance = catalog_provenance(_sidecar(repo="https://user:hunter2@github.com/o/r.git"))

    assert provenance is not None
    assert "hunter2" not in provenance.repo
    assert provenance.repo.startswith("https://[REDACTED]@github.com")


def test_a_missing_tier_is_left_empty_for_the_label_to_default():
    """catalog_annotation defaults to 'community' at render time, not in the data."""
    provenance = catalog_provenance(_sidecar(tier=None))

    assert provenance is not None
    assert provenance.tier == ""


def test_sidecar_strings_are_bounded():
    provenance = catalog_provenance(
        _sidecar(catalog_name="n" * 5000, installed_at="t" * 5000, tier="x" * 500)
    )

    assert provenance is not None
    assert len(provenance.name) <= 256
    assert len(provenance.installed_at) <= 256
    assert len(provenance.tier) <= 64


# --------------------------------------------------------------------------
# drift and pin derivation on the model
# --------------------------------------------------------------------------


def test_drift_fires_when_the_reviewed_sha_and_the_installed_head_differ():
    plugin = PluginInfo(name="x", catalog_sha=CATALOG_SHA, installed_revision=INSTALLED_SHA)

    assert plugin.provenance_drift is True


def test_no_drift_when_the_two_agree():
    plugin = PluginInfo(name="x", catalog_sha=SAME_SHA, installed_revision=SAME_SHA)

    assert plugin.provenance_drift is False


def test_drift_comparison_is_case_insensitive():
    plugin = PluginInfo(name="x", catalog_sha=SAME_SHA.upper(), installed_revision=SAME_SHA)

    assert plugin.provenance_drift is False


def test_no_drift_is_claimed_without_both_revisions():
    assert PluginInfo(name="x", catalog_sha=CATALOG_SHA).provenance_drift is False
    assert PluginInfo(name="x", installed_revision=INSTALLED_SHA).provenance_drift is False
    assert PluginInfo(name="x").provenance_drift is False


def test_no_drift_is_claimed_from_a_malformed_sha():
    """A corrupt sidecar is not evidence that the install moved."""
    plugin = PluginInfo(name="x", catalog_sha="nope", installed_revision=INSTALLED_SHA)

    assert plugin.provenance_drift is False


def test_pinned_is_derived_from_the_pinned_revision():
    assert PluginInfo(name="x", pinned_revision=CATALOG_SHA).pinned is True
    assert PluginInfo(name="x", installed_revision=CATALOG_SHA).pinned is False
    assert PluginInfo(name="x").pinned is False


def test_provenance_fields_reach_the_json_snapshot():
    dumped = PluginInfo(
        name="x",
        catalog_sha=CATALOG_SHA,
        catalog_tier="official",
        installed_revision=INSTALLED_SHA,
        pinned_revision=INSTALLED_SHA,
        install_source="https://github.com/o/r.git",
    ).model_dump(mode="json")

    assert dumped["catalog_sha"] == CATALOG_SHA
    assert dumped["installed_revision"] == INSTALLED_SHA
    assert dumped["pinned"] is True
    assert dumped["provenance_drift"] is True


# --------------------------------------------------------------------------
# collector: install metadata
# --------------------------------------------------------------------------


def test_collector_reads_provenance_for_an_installed_plugin(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_metadata(
        hermes_home,
        {
            "weather": {
                "pinned": False,
                "revision": INSTALLED_SHA,
                "source": "https://github.com/o/r.git",
            }
        },
    )

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.installed_revision == INSTALLED_SHA
    assert plugin.pinned_revision == ""
    assert plugin.pinned is False
    assert plugin.install_source == "https://github.com/o/r.git"


def test_a_ref_install_is_reported_as_pinned(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_metadata(
        hermes_home, {"weather": {"pinned": True, "revision": INSTALLED_SHA, "source": "s"}}
    )

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.pinned is True
    assert plugin.pinned_revision == INSTALLED_SHA


def test_the_metadata_is_keyed_by_manifest_name_not_directory_name(hermes_home: Path):
    """plugins_cmd.py:670 keys on manifest['name']; the directory is _sanitize_plugin_name."""
    _write_config(hermes_home, {"enabled": ["Weather Tools"]})
    _write_plugin(hermes_home, "weather-tools/plugin.yaml", "name: Weather Tools\n")
    _write_metadata(
        hermes_home,
        {
            "Weather Tools": {"pinned": False, "revision": INSTALLED_SHA, "source": "s"},
            "weather-tools": {"pinned": False, "revision": SAME_SHA, "source": "other"},
        },
    )

    plugin = _by_name(_collect(hermes_home))["Weather Tools"]

    assert plugin.installed_revision == INSTALLED_SHA
    assert plugin.install_source == "s"


def test_a_plugin_absent_from_the_metadata_has_no_provenance(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_metadata(hermes_home, {"other": {"pinned": True, "revision": INSTALLED_SHA}})

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.installed_revision == ""
    assert plugin.install_source == ""
    assert plugin.pinned is False


def test_a_corrupt_install_metadata_file_is_no_provenance_not_a_failure(hermes_home: Path):
    """Upstream raises PluginOperationError here; a viewer must not fail the panel."""
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    (home_meta := hermes_home / "plugins" / INSTALL_METADATA_NAME).write_text("{not json")

    state = _collect(hermes_home)

    assert home_meta.read_text() == "{not json"
    assert "skills" not in state.health.failed_sources
    plugin = _by_name(state)["weather"]
    assert plugin.installed_revision == ""
    assert plugin.activation == PluginActivation.ENABLED


def test_a_non_object_install_metadata_file_is_no_provenance(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_metadata(hermes_home, ["not", "an", "object"])

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.installed_revision == ""


def test_a_non_mapping_metadata_entry_is_ignored(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_metadata(hermes_home, {"weather": INSTALLED_SHA})

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.installed_revision == ""


def test_the_metadata_file_is_read_once_per_pass(hermes_home: Path, monkeypatch) -> None:
    """One file keyed by name: N plugins must not mean N reads."""
    _write_config(hermes_home, {"enabled": ["a", "b", "c"]})
    for name in ("a", "b", "c"):
        _write_plugin(hermes_home, f"{name}/plugin.yaml", f"name: {name}\n")
    _write_metadata(hermes_home, {name: {"revision": INSTALLED_SHA} for name in ("a", "b", "c")})

    seen: list[str] = []
    original = LastGoodFileCache.read_json_mapping

    def spy(self, path):
        seen.append(path.name)
        return original(self, path)

    monkeypatch.setattr(LastGoodFileCache, "read_json_mapping", spy)
    state = _collect(hermes_home)

    assert seen.count(INSTALL_METADATA_NAME) == 1
    assert {p.name for p in state.skills_memory.plugins} == {"a", "b", "c"}


def test_the_metadata_sidecar_is_not_mistaken_for_a_plugin(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_metadata(hermes_home, {"weather": {"revision": INSTALLED_SHA}})

    assert set(_by_name(_collect(hermes_home))) == {"weather"}


def test_a_symlinked_metadata_file_is_refused(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "metadata.json"
    outside.write_text(json.dumps({"weather": {"revision": INSTALLED_SHA}}))
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    (hermes_home / "plugins" / INSTALL_METADATA_NAME).symlink_to(outside)

    assert _by_name(_collect(hermes_home))["weather"].installed_revision == ""


# --------------------------------------------------------------------------
# collector: catalog sidecar
# --------------------------------------------------------------------------


def test_collector_reads_the_catalog_sidecar(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_sidecar(plugin_dir, _sidecar())

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.catalog_name == "weather"
    assert plugin.catalog_sha == CATALOG_SHA
    assert plugin.catalog_tier == "official"
    assert plugin.catalog_installed_at == "2026-09-01T10:00:00Z"
    assert plugin.catalog_repo == "https://github.com/nousresearch/hermes-plugins.git"


def test_a_plugin_without_a_sidecar_is_not_a_catalog_install(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.catalog_name == ""
    assert plugin.catalog_sha == ""


def test_a_corrupt_sidecar_is_a_non_catalog_install_not_a_failure(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    (plugin_dir / CATALOG_SIDECAR_NAME).write_text("{not json")

    state = _collect(hermes_home)

    assert "skills" not in state.health.failed_sources
    plugin = _by_name(state)["weather"]
    assert plugin.catalog_name == ""
    assert plugin.catalog_sha == ""


def test_a_sidecar_without_a_catalog_name_is_a_non_catalog_install(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_sidecar(plugin_dir, {"repo": "https://x", "sha": CATALOG_SHA, "tier": "official"})

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.catalog_name == ""
    assert plugin.catalog_sha == ""


def test_a_sidecar_is_read_per_plugin_directory(hermes_home: Path):
    """Two plugins, two sidecars: neither may inherit the other's provenance."""
    _write_config(hermes_home, {"enabled": ["weather", "notes"]})
    weather = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    notes = _write_plugin(hermes_home, "notes/plugin.yaml", "name: notes\n")
    _write_sidecar(weather, _sidecar(catalog_name="weather", sha=CATALOG_SHA))
    _write_sidecar(notes, _sidecar(catalog_name="notes", sha=SAME_SHA, tier="community"))

    plugins = _by_name(_collect(hermes_home))

    assert plugins["weather"].catalog_sha == CATALOG_SHA
    assert plugins["notes"].catalog_sha == SAME_SHA
    assert plugins["notes"].catalog_tier == "community"


def test_a_category_plugins_sidecar_is_read_from_its_own_directory(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["observability/tracer"]})
    plugin_dir = _write_plugin(hermes_home, "observability/tracer/plugin.yaml", "name: tracer\n")
    _write_sidecar(plugin_dir, _sidecar(catalog_name="tracer"))

    plugins = {p.manifest_key: p for p in _collect(hermes_home).skills_memory.plugins}

    assert plugins["observability/tracer"].catalog_name == "tracer"


def test_a_symlinked_sidecar_is_refused(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "sidecar.json"
    outside.write_text(json.dumps(_sidecar()))
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    (plugin_dir / CATALOG_SIDECAR_NAME).symlink_to(outside)

    assert _by_name(_collect(hermes_home))["weather"].catalog_sha == ""


# --------------------------------------------------------------------------
# collector: drift
# --------------------------------------------------------------------------


def test_a_ref_install_that_moved_off_the_catalog_pin_is_flagged_as_drift(hermes_home: Path):
    """The --ref case: the sidecar keeps the reviewed sha, the metadata records the HEAD."""
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_sidecar(plugin_dir, _sidecar(sha=CATALOG_SHA))
    _write_metadata(
        hermes_home, {"weather": {"pinned": True, "revision": INSTALLED_SHA, "source": "s"}}
    )

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.provenance_drift is True
    assert plugin.catalog_sha == CATALOG_SHA
    assert plugin.installed_revision == INSTALLED_SHA


def test_a_catalog_install_at_its_reviewed_pin_shows_no_drift(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_sidecar(plugin_dir, _sidecar(sha=SAME_SHA))
    _write_metadata(hermes_home, {"weather": {"pinned": False, "revision": SAME_SHA}})

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.provenance_drift is False
    assert plugin.catalog_sha == SAME_SHA
    assert plugin.installed_revision == SAME_SHA


def test_an_unpinned_install_that_drifted_from_the_catalog_is_still_flagged(hermes_home: Path):
    """Drift is about the two SHAs, not about whether a pin was requested."""
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_sidecar(plugin_dir, _sidecar(sha=CATALOG_SHA))
    _write_metadata(hermes_home, {"weather": {"pinned": False, "revision": INSTALLED_SHA}})

    assert _by_name(_collect(hermes_home))["weather"].provenance_drift is True


def test_a_catalog_sidecar_alone_never_claims_drift(hermes_home: Path):
    """With no installed HEAD recorded there is nothing to compare against."""
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_sidecar(plugin_dir, _sidecar(sha=CATALOG_SHA))

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.provenance_drift is False
    assert plugin.installed_revision == ""


# --------------------------------------------------------------------------
# collector: declarations
# --------------------------------------------------------------------------


def test_declared_requires_hermes_and_capabilities_are_recorded(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(
        hermes_home,
        "weather/plugin.yaml",
        "name: weather\nrequires_hermes: '>=0.19'\n"
        "capabilities:\n  - tools.override\n  - llm.model_override\n",
    )

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.requires_hermes == ">=0.19"
    assert plugin.declared_capabilities == ["tools.override", "llm.model_override"]
    assert plugin.declared_capability_count == 2


def test_a_capability_list_longer_than_the_cap_keeps_its_true_count(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    caps = "\n".join(f"  - cap-{i}" for i in range(40))
    _write_plugin(hermes_home, "weather/plugin.yaml", f"name: weather\ncapabilities:\n{caps}\n")

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.declared_capability_count == 40
    assert len(plugin.declared_capabilities) < 40


def test_a_non_list_capabilities_value_is_no_declaration(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\ncapabilities: tools\n")

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert plugin.declared_capabilities == []
    assert plugin.declared_capability_count == 0


def test_a_portable_manifest_declares_no_capabilities_or_version_gate(hermes_home: Path):
    """portable_plugin_manifest maps only name/version/description (+ portable, namespace)."""
    _write_config(hermes_home, {"enabled": ["portable-one"]})
    _write_plugin(
        hermes_home,
        "portable-one/plugin.json",
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": "portable-one",
                "requires_hermes": ">=99",
                "capabilities": ["tools.override"],
            }
        ),
    )

    plugin = _by_name(_collect(hermes_home))["portable-one"]

    assert plugin.requires_hermes == ""
    assert plugin.declared_capabilities == []
    assert plugin.declared_capability_count == 0


# --------------------------------------------------------------------------
# redaction at the data boundary
# --------------------------------------------------------------------------


def test_a_credential_bearing_source_never_reaches_the_json_snapshot(hermes_home: Path):
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_metadata(
        hermes_home,
        {
            "weather": {
                "pinned": True,
                "revision": INSTALLED_SHA,
                "source": "https://bot:ghp_SECRET123@github.com/o/r.git",
            }
        },
    )
    _write_sidecar(plugin_dir, _sidecar(repo="https://deploy:TOKEN456@github.com/o/r.git"))

    dumped = json.dumps(_collect(hermes_home).model_dump(mode="json"))

    assert "ghp_SECRET123" not in dumped
    assert "TOKEN456" not in dumped
    assert "bot:" not in dumped
    assert "deploy:" not in dumped
    assert "[REDACTED]" in dumped


def test_redaction_happens_in_the_reader_not_the_panel(hermes_home: Path):
    """The model itself must already be clean — panels only escape markup."""
    _write_config(hermes_home, {"enabled": ["weather"]})
    plugin_dir = _write_plugin(hermes_home, "weather/plugin.yaml", "name: weather\n")
    _write_metadata(hermes_home, {"weather": {"source": "https://user:pw@github.com/o/r.git"}})
    _write_sidecar(plugin_dir, _sidecar(repo="https://user:pw@github.com/o/r.git"))

    plugin = _by_name(_collect(hermes_home))["weather"]

    assert "pw" not in plugin.install_source
    assert "pw" not in plugin.catalog_repo
