"""``gateway_migration.json``: recorded migration intent, never proof of success.

Upstream is ``hermes_cli/gateway_migrate.py``. The manifest lives at
``<DEFAULT profile home>/gateway_migration.json`` (``MANIFEST_NAME`` ``:25``,
``_manifest_path`` ``:467-468``) — always the default home, never a secondary's —
and ``_write_manifest`` (``:482-483``) writes it with a plain ``write_text``, so the
write is **not atomic** and hermesd can observe a torn file.

Shape (built at ``:528-533``; ``ProfileGateway.to_dict()`` at ``:55-59``)::

    version: int = 1
    migrated_at: str   time.strftime("%Y-%m-%dT%H:%M:%S%z")  <- LOCAL time, and the
                                                                 offset has no colon
    flag_was: bool     the gateway.multiplex_profiles value to restore
    default: {profile, home, pid, service: {kind, system}|null}
    secondaries: [ {same shape} ]        <- standalone_secondaries only

The load-bearing fact is *when* it is written: inside the per-secondary loop at
``:543-544``, before ``_write_multiplex_flag`` (``:545``) and before
``_restart_default`` (``:549``); rewritten byte-identically at ``:546``; and never
touched again — neither the success path (``:553-557``) nor the unverified path
(``:558-561``) writes it, and there is no ``completed``/``verified``/``outcome``
field. So ``migrated_at`` means "the attempt began at", and a manifest on disk is
indistinguishable between mid-flight, crashed after one secondary,
applied-but-unverified and fully verified. ``rollback_migration`` (``:564-610``)
deletes it on success, so absence means "never migrated OR successfully rolled
back" — also indistinguishable.

Everything here is therefore *recorded intent*. The verified verdict lives in
``MigrationState.migration_verified`` and is computed only from artifacts hermesd
can read. This module resolves no path at all: it takes already-loaded JSON, so a
recorded ``home`` value can never become a filesystem target (upstream's rollback
does build ``Path(rec["home"])`` from it, at ``:594``).

``migrated_at`` is the only local-time stamp hermesd reads; every other source
writes UTC (``Z`` or ``+00:00``). ``_iso_to_epoch`` still parses it because
``datetime.fromisoformat`` accepts basic-format offsets (``+0200``) from Python
3.11, the pinned floor. Reading that offset as UTC — or dropping it — would place
the attempt two hours later than it began.
"""

from __future__ import annotations

from itertools import islice

from hermesd.collect.common import (
    _age_seconds,
    _as_dict,
    _as_list,
    _coerce_bool,
    _coerce_int,
    _iso_to_epoch,
)
from hermesd.file_cache import JsonMapping
from hermesd.models import GatewayState, MigrationProfileRecord, MigrationState

_MANIFEST_NAME = "gateway_migration.json"
# Bound on the retained secondary records. Upstream writes one entry per profile
# that had its own gateway, so this sits far above any real install; the file is
# still untrusted input, and a list the cap truncated can never verify (see
# MigrationVerificationGap.SECONDARIES_TRUNCATED).
_MANIFEST_SECONDARY_LIMIT = 32
# A recorded home is display data only: collapse whitespace and cap the length like
# every other string taken from an untrusted file.
_RECORDED_HOME_CHARS = 200


def _multiplex_flag_on(cfg: JsonMapping) -> bool:
    """``gateway.multiplex_profiles`` as recorded in config, mirroring upstream's reader.

    ``_read_multiplex_flag`` (``gateway_migrate.py:203-215``) consults an environment
    override hermesd cannot see, then returns ``cfg.get("multiplex_profiles") or
    gateway_section.get("multiplex_profiles")`` — ORing a stale top-level alias with
    the nested key. hermesd matches the *reader*, not ``_write_multiplex_flag``
    (``:217-228``), which pops the alias, so a leftover top-level True still counts
    as on. Because the env override is invisible from here, this value is only ever
    reported "as recorded in config".
    """
    gateway_section = _as_dict(cfg.get("gateway"))
    return bool(cfg.get("multiplex_profiles") or gateway_section.get("multiplex_profiles"))


def _recorded_home(value: object) -> str:
    """A recorded path, whitespace-collapsed and length-capped — never resolved."""
    return " ".join(str(value or "").split())[:_RECORDED_HOME_CHARS]


def _profile_record(
    entry: object, covered: set[str], *, fallback_name: str = ""
) -> MigrationProfileRecord:
    """One recorded profile footprint and whether the live served set covers it.

    A malformed entry still produces a record with an empty profile, which is never
    in ``covered`` — so garbage in the manifest reads as unverified rather than
    silently dropping out of the expected set.
    """
    info = _as_dict(entry)
    service = _as_dict(info.get("service"))
    profile = str(info.get("profile") or "") or fallback_name
    return MigrationProfileRecord(
        profile=profile,
        home=_recorded_home(info.get("home")),
        service_kind=str(service.get("kind") or ""),
        # The manifest is machine-written with real booleans
        # (``gateway_migrate.py:58,588``).
        service_system=_coerce_bool(service.get("system")),
        served=profile in covered,
    )


def _manifest_schema_valid(manifest: JsonMapping) -> bool:
    version = manifest.get("version")
    if type(version) is not int or version != 1:
        return False

    default = manifest.get("default")
    if not isinstance(default, dict) or default.get("profile") != "default":
        return False

    secondaries = manifest.get("secondaries")
    if not isinstance(secondaries, list):
        return False
    return all(
        isinstance(entry, dict)
        and isinstance(entry.get("profile"), str)
        and bool(entry["profile"].strip())
        for entry in secondaries
    )


def _migration_state(
    manifest: JsonMapping,
    *,
    now: float,
    cfg: JsonMapping,
    gateway: GatewayState,
) -> MigrationState:
    """Recorded intent from ``manifest`` judged against the live gateway and config.

    ``manifest`` is the already-loaded file: an empty mapping means the file exists
    but could not be parsed (a torn write), which is reported as present-and-unparsed
    rather than as no migration at all.
    """
    flag_on = _multiplex_flag_on(cfg)
    gateway_live = gateway.running
    served_recorded = gateway.served_profiles_recorded
    if not manifest:
        return MigrationState(
            manifest_present=True,
            multiplex_flag_on=flag_on,
            default_gateway_live=gateway_live,
            served_recorded=served_recorded,
        )
    # Coverage is only meaningful against a *live* record: a served list left behind
    # by a dead gateway describes a topology that is gone.
    covered = set(gateway.served_profiles) if served_recorded else set()
    raw_secondaries = _as_list(manifest.get("secondaries"))
    retained = list(islice(raw_secondaries, _MANIFEST_SECONDARY_LIMIT))
    migrated_at = str(manifest.get("migrated_at") or "")
    return MigrationState(
        manifest_present=True,
        manifest_parsed=True,
        manifest_schema_valid=_manifest_schema_valid(manifest),
        manifest_version=_coerce_int(manifest.get("version")),
        migrated_at=migrated_at,
        migrated_at_age_seconds=_age_seconds(_iso_to_epoch(migrated_at), now),
        flag_was=_coerce_bool(manifest.get("flag_was")),
        default_profile=_profile_record(manifest.get("default"), covered, fallback_name="default"),
        secondaries=[_profile_record(entry, covered) for entry in retained],
        secondary_count=len(raw_secondaries),
        secondaries_truncated=len(raw_secondaries) > len(retained),
        multiplex_flag_on=flag_on,
        default_gateway_live=gateway_live,
        served_recorded=served_recorded,
    )
