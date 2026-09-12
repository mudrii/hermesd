"""Plugin activation semantics, mirrored from hermes-agent without importing it.

hermesd reads ``plugins/<name>/plugin.yaml`` and ``config.yaml`` and reproduces
the decision upstream's ``plugins_discovery.gate_manifest`` would make. It never
imports or executes plugin code: kind auto-detection is a bounded text scan of
``__init__.py``, exactly as upstream does it.

The result describes *configured activation* — what hermes-agent would do with
this manifest. It is not evidence that the plugin loaded successfully.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermesd.models import PluginActivation

# Relay lifecycle is owned by Hermes core; an out-of-tree copy would compete for
# its registries, so upstream refuses it before consulting either config list.
_LEGACY_RELAY_PLUGIN_KEYS = frozenset({"nemo_relay", "observability/nemo_relay"})
# Upstream's undeclared-kind fallback, and the cap on how much of __init__.py it
# scans for provider markers.
PLUGIN_KIND_STANDALONE = "standalone"
_KIND_SOURCE_SCAN_CHARS = 8192


@dataclass(frozen=True, slots=True)
class PluginGate:
    """Configured-activation verdict for one discovered plugin."""

    activation: PluginActivation
    reason: str = ""


def plugin_name_set(value: object) -> frozenset[str]:
    """Names from a ``plugins.enabled``/``plugins.disabled`` list.

    Any other shape counts as no names at all, so a malformed allow-list degrades
    to "nothing opted in" rather than to "everything allowed".
    """
    if not isinstance(value, list):
        return frozenset()
    return frozenset(entry for entry in value if isinstance(entry, str) and entry)


def detect_kind_from_source(source_text: str) -> str:
    """Kind implied by source markers, mirroring upstream's import-free scan."""
    if "register_memory_provider" in source_text or "MemoryProvider" in source_text:
        return "exclusive"
    if "register_provider" in source_text and "ProviderProfile" in source_text:
        return "model-provider"
    return PLUGIN_KIND_STANDALONE


def resolve_plugin_kind(declared: object, init_source: str) -> str:
    """Normalize a manifest ``kind``, auto-detecting only when it is undeclared.

    Most real manifests — including every bundled memory provider — omit ``kind``
    and rely on upstream scanning ``__init__.py`` to route them to their own
    discovery. ``init_source`` is that text ("" when there is no ``__init__.py``),
    so hermesd matches the routing without importing anything.
    """
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    if init_source:
        return detect_kind_from_source(init_source[:_KIND_SOURCE_SCAN_CHARS])
    return PLUGIN_KIND_STANDALONE


def gate_plugin(
    *, key: str, name: str, kind: str, enabled: frozenset[str], disabled: frozenset[str]
) -> PluginGate:
    """Decide configured activation for one plugin, in upstream's gate order.

    Order matters: the legacy-Relay refusal and the deny list are checked before
    the category-owned kinds, which are checked before the ``plugins.enabled``
    opt-in. Both the path-derived key and the manifest name can match either
    list, so a plugin opted in under its legacy bare name is still recognised —
    and an empty allow-list means nothing is enabled, not that everything is.
    """
    names = frozenset(candidate for candidate in (key, name) if candidate)
    if names & _LEGACY_RELAY_PLUGIN_KEYS:
        return PluginGate(
            PluginActivation.REMOVED, "removed — Relay lifecycle is owned by Hermes core"
        )
    if names & disabled:
        return PluginGate(PluginActivation.DISABLED, "disabled via config")
    if kind == "exclusive":
        return PluginGate(
            PluginActivation.CATEGORY_OWNED,
            "exclusive plugin — activate via <category>.provider config",
        )
    if kind == "model-provider":
        return PluginGate(
            PluginActivation.ENABLED, "model provider — activated via providers discovery"
        )
    if not names & enabled:
        return PluginGate(PluginActivation.NOT_ENABLED, "not enabled in config")
    return PluginGate(PluginActivation.ENABLED)
