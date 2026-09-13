"""Plugin discovery, activation and provenance, mirrored from hermes-agent.

hermesd reads ``plugins/`` and ``config.yaml`` and reproduces the decisions
upstream's ``plugins_discovery`` and ``plugins_cmd`` would make. It never imports
or executes plugin code: manifests are parsed as data, kind auto-detection is a
bounded text scan of ``__init__.py`` exactly as upstream does it, and provenance
comes from the two JSON sidecars the installer writes.

Three separate claims are reported, and **none of them is evidence that a plugin
loaded** — hermesd is a reader, so the strongest thing it can say is what the
files say:

* *discovery* — which manifest won (``plugin.yaml`` > ``plugin.yml`` >
  ``plugin.json``) and which lost, across both directory shapes upstream scans;
* *configured activation* — what ``gate_manifest`` would do with that manifest;
* *provenance* — the commit the installer recorded, and the commit the catalog
  reviewed, which are not the same thing (see :func:`catalog_provenance`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from hermesd.collect.common import _as_list
from hermesd.collect.redaction import _redact_secret_url
from hermesd.models import _FULL_REVISION_PATTERN, MANIFEST_NAMES, PluginActivation

# Relay lifecycle is owned by Hermes core; an out-of-tree copy would compete for
# its registries, so upstream refuses it before consulting either config list.
_LEGACY_RELAY_PLUGIN_KEYS = frozenset({"nemo_relay", "observability/nemo_relay"})
# Upstream's undeclared-kind fallback, and the cap on how much of __init__.py it
# scans for provider markers.
PLUGIN_KIND_STANDALONE = "standalone"
_KIND_SOURCE_SCAN_CHARS = 8192
_VALID_PLUGIN_KINDS = frozenset(
    {PLUGIN_KIND_STANDALONE, "backend", "exclusive", "platform", "model-provider"}
)

# One level of category recursion. Depth 0 is the flat ``<root>/<name>/`` shape,
# depth 1 is ``<root>/<cat>/<name>/``; a manifest-less directory at depth 1 is
# where upstream logs "no plugin.yaml, depth cap reached" and stops
# (plugins_discovery.py:127-131).
MAX_PLUGIN_SCAN_DEPTH = 1

# Installer sidecars. They are keyed differently, which is the trap: one is a
# single file under plugins/ keyed by *manifest name*, the other is one file per
# plugin directory.
INSTALL_METADATA_NAME = ".install-metadata.json"
CATALOG_SIDECAR_NAME = ".hermes-catalog.json"

# The schema a portable Agent Plugin must declare, and the name grammar it must
# satisfy (agent_plugins.py:16,24,92-99).
PLUGIN_SCHEMA_V1 = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
_PORTABLE_NAME_PATTERN = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_PORTABLE_NAME_MAX_CHARS = 64

# Bounds on values taken from an untrusted manifest or sidecar. The capability
# list is display-bounded, so its true count travels beside it.
DECLARED_CAPABILITY_LIMIT = 8
_MAX_CAPABILITY_CHARS = 64
_MAX_PROVENANCE_CHARS = 256
_MAX_TIER_CHARS = 64
_MAX_REQUIRES_HERMES_CHARS = 100


@dataclass(frozen=True, slots=True)
class PluginGate:
    """Configured-activation verdict for one discovered plugin."""

    activation: PluginActivation
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ManifestChoice:
    """The manifest that won a directory, and the ones precedence outranked."""

    filename: str
    shadowed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PortableManifest:
    """The fields a portable ``plugin.json`` carries into a manifest.

    Only these three survive upstream's own mapping
    (``plugins_manifest.py:422-437``): kind, ``requires_hermes``, ``capabilities``
    and the ``provides_*`` lists are native-manifest-only, so hermesd must not
    read them out of a ``plugin.json`` either, however plausible they look there.
    """

    name: str
    version: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class InstallProvenance:
    """One ``plugins/.install-metadata.json`` entry — what was actually installed."""

    revision: str = ""
    pinned_revision: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class CatalogProvenance:
    """One ``<plugin_dir>/.hermes-catalog.json`` sidecar — what was reviewed."""

    name: str = ""
    repo: str = ""
    sha: str = ""
    tier: str = ""
    installed_at: str = ""


@dataclass(frozen=True, slots=True)
class CatalogCacheEntry:
    """One live-catalog entry, from the ``cache/plugin-catalog.json`` cache.

    The cache holds the raw published mappings (``extract-plugins.py:149-151``)
    that ``entry_from_mapping`` would validate upstream; hermesd keeps only the
    fields its three derived states need.
    """

    name: str
    sha: str = ""
    repo: str = ""


@dataclass(frozen=True, slots=True)
class RemovedCatalogEntry:
    """One kill-list row (``plugin_catalog.py:39-44``)."""

    name: str
    repo: str = ""
    reason: str = ""
    date: str = ""


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


def resolve_plugin_kind(
    declared: object,
    init_source: str,
    *,
    declared_present: bool = True,
) -> str:
    """Normalize a manifest ``kind``, auto-detecting only when it is undeclared.

    Most real manifests — including every bundled memory provider — omit ``kind``
    and rely on upstream scanning ``__init__.py`` to route them to their own
    discovery. ``init_source`` is that text ("" when there is no ``__init__.py``),
    so hermesd matches the routing without importing anything.
    """
    if declared_present:
        kind = declared.strip().lower() if isinstance(declared, str) else PLUGIN_KIND_STANDALONE
        return kind if kind in _VALID_PLUGIN_KINDS else PLUGIN_KIND_STANDALONE
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


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def choose_manifest(present: Iterable[str]) -> ManifestChoice | None:
    """Pick the winning manifest from the candidates present in one directory.

    ``present`` is whatever the caller could safely stat, in any order; the result
    follows upstream's fixed precedence (``plugins_discovery.py:115``, repeated at
    ``plugin_dev.py:154``, ``plugins_cmd.py:265-271``, ``config.py:3753``), never
    the order it was handed. Filenames hermesd does not own are ignored, so an
    unrelated YAML file in a plugin directory is not a candidate.

    The losers are returned rather than dropped. Upstream resolves a directory
    carrying both a native and a portable manifest silently, which makes "why did
    it pick that one?" unanswerable from outside; recording them makes the choice
    observable without changing it.
    """
    candidates = set(present)
    ordered = tuple(name for name in MANIFEST_NAMES if name in candidates)
    if not ordered:
        return None
    return ManifestChoice(filename=ordered[0], shadowed=ordered[1:])


def category_prefix(prefix: str, dirname: str) -> str:
    """Nest one category level, mirroring ``scan_directory``'s ``sub_prefix``."""
    return f"{prefix}/{dirname}" if prefix else dirname


def plugin_key(*, prefix: str, dirname: str, name: str) -> str:
    """Registry key that ``plugins.enabled``/``plugins.disabled`` are matched on.

    A category plugin's key is ``<prefix>/<dirname>``; a flat plugin's is its
    manifest ``name``. Both derive from the path and the manifest name alone,
    because that is exactly what ``parse_manifest_file`` computes
    (``plugins_manifest.py:468``) — nothing upstream ever reads a ``key:`` field
    out of a manifest, so ``plugins.enabled`` can never contain one. Honouring a
    declared key here would let hermesd report a plugin as enabled that
    hermes-agent would refuse to load, and a monitor must not be more permissive
    than the thing it monitors.
    """
    return f"{prefix}/{dirname}" if prefix else name


def parse_portable_manifest(data: object) -> tuple[PortableManifest | None, str]:
    """Validate a ``plugin.json`` mapping: ``(manifest, "")`` or ``(None, reason)``.

    Mirrors every load-blocking check in ``agent_plugins._validate_manifest``
    (``:87-118``), while retaining only the three fields upstream carries into
    ``PluginManifest``. A non-object ``extensions`` value is diagnostic-only
    upstream and remains non-fatal here. Upstream raises ``AgentPluginError``;
    hermesd returns the reason so the caller can report
    :attr:`PluginActivation.UNKNOWN` — a portable manifest that does not parse is
    still a plugin directory, and dropping it silently would hide the only
    evidence that something there is broken.
    """
    if not isinstance(data, dict):
        return None, "plugin.json is not a JSON object"
    if data.get("$schema") != PLUGIN_SCHEMA_V1:
        return None, "plugin.json declares an unsupported or missing schema"
    name = data.get("name")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= _PORTABLE_NAME_MAX_CHARS
        or _PORTABLE_NAME_PATTERN.fullmatch(name) is None
    ):
        return None, "plugin.json name does not satisfy the v1 constraints"
    for field in ("version", "description", "homepage", "repository", "license"):
        if field in data and not isinstance(data[field], str):
            return None, f"plugin.json {field} must be a string"
    keywords = data.get("keywords", [])
    if not isinstance(keywords, list) or not all(isinstance(value, str) for value in keywords):
        return None, "plugin.json keywords must be an array of strings"
    author = data.get("author", {})
    if not isinstance(author, dict):
        return None, "plugin.json author must be an object"
    author_fields = {"name", "email", "url"}
    if set(author) - author_fields or not all(isinstance(value, str) for value in author.values()):
        return None, "plugin.json author may contain only string name, email, and url fields"
    extensions = data.get("extensions", {})
    if isinstance(extensions, dict) and any(
        not isinstance(value, dict) for value in extensions.values()
    ):
        return None, "plugin.json extension namespace values must be objects"
    return PortableManifest(
        name=name,
        version=str(data.get("version") or ""),
        description=str(data.get("description") or ""),
    ), ""


def requires_hermes_spec(value: object) -> str:
    """The manifest's declared ``requires_hermes`` gate, stripped and bounded.

    A *declaration*, nothing more: upstream evaluates it against the running
    hermes-agent version at load time (``plugins_manifest.py:412-419``), which
    hermesd cannot observe and does not attempt.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()[:_MAX_REQUIRES_HERMES_CHARS]


def declared_capabilities(value: object) -> tuple[list[str], int]:
    """Declared capability ids (display-bounded) and their true count.

    Kept as written rather than filtered to upstream's registry, which drops an
    unknown id as fail-closed (``plugin_capabilities.py:63-84``). That is right
    for a consent screen — an id this build cannot grant must not be offered — and
    wrong for a dashboard, where a declaration hermesd does not recognise is
    exactly what an operator needs to see. The count is the full one, so capping
    the list can never change what hermesd reports.
    """
    if not isinstance(value, list):
        return [], 0
    seen: dict[str, None] = {}
    for item in value:
        if not isinstance(item, str):
            continue
        capability = item.strip()
        if capability:
            seen.setdefault(capability[:_MAX_CAPABILITY_CHARS], None)
    return list(seen)[:DECLARED_CAPABILITY_LIMIT], len(seen)


# --------------------------------------------------------------------------
# live catalog cache
# --------------------------------------------------------------------------


def normalize_repo(value: object) -> str:
    """Upstream's repo normalization (``plugin_catalog.py:194-195``): ``.git``/
    trailing-slash/case insensitive, so a kill-list row cannot be dodged by
    respelling the URL."""
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/").removesuffix(".git").lower()


def parse_catalog_cache(
    data: object,
) -> tuple[dict[str, CatalogCacheEntry], list[RemovedCatalogEntry]] | None:
    """Entries and kill-list rows from ``cache/plugin-catalog.json``.

    The cache is what ``fetch_live_catalog`` writes
    (``plugin_catalog.py:221-248``): ``{"entries": [...], "removed": [...]}``.
    Upstream validates the payload with ``isinstance(data, dict) and
    isinstance(data.get("entries"), list)`` before it is ever written
    (``:238-239``), so anything else is *unreadable*, not empty: None is
    returned and the caller must report the checks as unavailable instead of
    claiming the plugins match. Rows inside a valid payload stay tolerant — a
    malformed row is skipped, not fatal.
    """
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    entries: dict[str, CatalogCacheEntry] = {}
    for raw in _as_list(data.get("entries")):
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            continue
        entries[name] = CatalogCacheEntry(
            name=name,
            sha=_revision(raw.get("sha")),
            repo=normalize_repo(raw.get("repo")),
        )
    removed: list[RemovedCatalogEntry] = []
    for raw in _as_list(data.get("removed")):
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            continue
        removed.append(
            RemovedCatalogEntry(
                name=name,
                # Kept as written: the repo is display text upstream too
                # (plugin_catalog.py:144); matching normalizes both sides.
                repo=_bounded(raw.get("repo"), _MAX_PROVENANCE_CHARS),
                reason=_bounded(raw.get("reason"), _MAX_PROVENANCE_CHARS),
                date=_bounded(raw.get("date"), _MAX_PROVENANCE_CHARS),
            )
        )
    return entries, removed


def removed_catalog_match(
    *candidates: str,
    removed: list[RemovedCatalogEntry],
) -> RemovedCatalogEntry | None:
    """The first kill-list row matching any candidate name or repo.

    Mirrors ``find_removed`` (``plugin_catalog.py:198-211``) via
    ``removed_annotation`` (``plugins_cmd_catalog.py:96-103``): a candidate
    matches a row by exact name or by normalized repo; a row without a repo
    matches by name only.
    """
    for candidate in candidates:
        if not candidate:
            continue
        candidate_repo = normalize_repo(candidate)
        for row in removed:
            if candidate == row.name or (row.repo and candidate_repo == normalize_repo(row.repo)):
                return row
    return None


def catalog_update_available(sidecar_sha: str, cache_entry: CatalogCacheEntry | None) -> bool:
    """Whether the catalog pins a different commit than the one reviewed here.

    Compares the sidecar's recorded sha against the live entry's pin, as
    ``catalog_row_fields`` does (``plugins_cmd_catalog.py:283-291``). Both
    sides must be full 40-hex revisions first: a malformed sha is a corrupt
    sidecar, not a move, and claiming drift from it would invent a comparison
    hermesd cannot make (same discipline as ``PluginInfo.provenance_drift``).
    """
    if cache_entry is None:
        return False
    if not _FULL_REVISION_PATTERN.fullmatch(sidecar_sha.lower()):
        return False
    if not _FULL_REVISION_PATTERN.fullmatch(cache_entry.sha.lower()):
        return False
    return sidecar_sha.lower() != cache_entry.sha.lower()


# --------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------


def _revision(value: object) -> str:
    """A full lowercase 40-hex commit SHA, or "" — never a guess at one."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    return candidate if _FULL_REVISION_PATTERN.fullmatch(candidate) else ""


def _bounded(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def install_provenance(entry: object) -> InstallProvenance:
    """Normalize one ``.install-metadata.json`` entry.

    Written at ``plugins_cmd.py:670-672`` as ``{"pinned": requested_revision is
    not None, "revision": installed_revision, "source": source}``, and read back
    by ``pinned_revision()`` (``:449-454``) — which returns the SHA *only* when
    ``pinned is True``. The identity check is mirrored deliberately: a truthy
    string in that field is not a pin, and calling it one would claim an
    immutability guarantee nobody asked for.

    ``source`` is re-redacted here rather than trusted. Upstream scrubs
    credentials before writing (``_scrub_git_url``, ``:513-521``), but this is a
    file on disk like any other, and the ``#subdir`` fragment it appends for
    monorepos (``:524-526``) has to survive the scrub.
    """
    if not isinstance(entry, dict):
        return InstallProvenance()
    revision = _revision(entry.get("revision"))
    return InstallProvenance(
        revision=revision,
        pinned_revision=revision if entry.get("pinned") is True else "",
        source=_redact_secret_url(str(entry.get("source") or ""))[:_MAX_PROVENANCE_CHARS],
    )


def catalog_provenance(data: object) -> CatalogProvenance | None:
    """Parsed ``.hermes-catalog.json``, or None for a non-catalog install.

    Mirrors ``read_catalog_sidecar`` (``plugins_cmd_catalog.py:76-85``): absent,
    corrupt, or missing ``catalog_name`` all mean "not from the catalog" — never
    an error, and never a fabricated value.

    The ``sha`` here is the catalog's **reviewed** commit, not the installed one.
    ``install_catalog_entry`` (``:108-118``) installs at ``ref or entry.sha`` and
    then writes the sidecar from ``entry``, so an explicit ``--ref`` leaves this
    file describing a commit that is not on disk. That is why the two SHAs are
    carried separately and their disagreement is surfaced instead of resolved.
    """
    if not isinstance(data, dict):
        return None
    name = data.get("catalog_name")
    if not isinstance(name, str) or not name.strip():
        return None
    return CatalogProvenance(
        name=name.strip()[:_MAX_PROVENANCE_CHARS],
        repo=_redact_secret_url(str(data.get("repo") or ""))[:_MAX_PROVENANCE_CHARS],
        sha=_revision(data.get("sha")),
        tier=_bounded(data.get("tier"), _MAX_TIER_CHARS),
        installed_at=_bounded(data.get("installed_at"), _MAX_PROVENANCE_CHARS),
    )
