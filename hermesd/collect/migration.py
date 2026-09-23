"""``gateway_migration.json``: the record of an UNFINISHED migration, never a success.

Upstream is ``hermes_cli/gateway_migrate.py``. The manifest lives at
``<DEFAULT profile home>/gateway_migration.json`` (``MANIFEST_NAME`` ``:37``,
``_manifest_path`` ``:741-742``, home from ``_default_home`` ``:212-214``) — always
the default home, never a secondary's. ``_write_manifest`` (``:769-772``) writes it
through ``atomic_json_write``, so a torn file is no longer an upstream shape; a
malformed one is hand-edited or foreign and is still reported as present-but-unparsed.

Shape (built at ``:945-950``; ``ProfileGateway.to_dict()`` at ``:78-85``)::

    version: int = 1
    migrated_at: str   time.strftime("%Y-%m-%dT%H:%M:%S%z")  <- LOCAL time, and the
                                                                 offset has no colon
    flag_was: bool     the explicit gateway.multiplex_profiles opt-in before the run
    default: {profile, home, pid, service: {kind, system}|null, services, ...}
    secondaries: [ {same shape} ]        <- standalone_secondaries only

It is written once, before the first destructive step (``:953``, ahead of the flag
write at ``:955`` and the restart at ``:958``), and never rewritten. It is deleted
when the apply *confirms* the default gateway serves every profile (``:967-973``:
"Manifest present == migration UNFINISHED") and when the failed-apply compensator
brings one gateway back (``:1059``, ``:1087``). There is no ``--standalone`` command
and no user-facing rollback any more (``:10-14``); re-running
``hermes gateway migrate --multiplex`` resumes from the manifest. So a manifest on
disk means "unfinished — resume", and absence means "never migrated, converged, or
compensated" — indistinguishable.

The one exception is a manifest left behind by a run that never saw convergence
confirmed while a later boot converged anyway: ``already_multiplexed`` (``:130-139``)
then short-circuits without deleting it. The verified verdict is therefore computed
only from artifacts hermesd can read (``MigrationState.migration_verified``), and a
verified topology with a manifest still on disk is reported as exactly that.

This module resolves no path at all: it takes already-loaded JSON, so a recorded
``home`` value can never become a filesystem target.

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
# Upstream's falsy config tokens (``_FALSY_STRINGS``, gateway/config.py:27); every
# other string, recognised truthy or not, is an explicit opt-in.
_FALSY_TOKENS = frozenset({"0", "false", "no", "off"})


def _explicit_multiplex_flag(cfg: JsonMapping) -> bool | None:
    """The operator's explicit ``gateway.multiplex_profiles`` as recorded in config.

    Mirrors ``explicit_multiplex_flag`` (``hermes_cli/gateway_multiplex_mode.py:49-72``)
    minus its ``GATEWAY_MULTIPLEX_PROFILES`` override, which hermesd cannot see: the
    top-level alias wins whenever it is present, else the nested key; a string is a
    token (``gateway/config.py:26-33``) and an unrecognised one counts as ``True``;
    anything else is truthiness. ``None`` means unset — the default (on) that the
    gateway settles at boot.
    """
    gateway_section = cfg.get("gateway")
    value = cfg.get("multiplex_profiles")
    if value is None and isinstance(gateway_section, dict):
        value = gateway_section.get("multiplex_profiles")
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_TOKENS
    return bool(value)


def _multiplex_flag_on(cfg: JsonMapping) -> bool:
    """The explicit opt-in only, as upstream's ``_read_multiplex_flag`` reads it
    (``gateway_migrate.py:335-340``)."""
    return _explicit_multiplex_flag(cfg) is True


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
        # (``_service_dict``, ``gateway_migrate.py:95-96``).
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
    but could not be parsed (hand-edited or foreign), which is reported as
    present-and-unparsed rather than as no migration at all.
    """
    explicit_flag = _explicit_multiplex_flag(cfg)
    flag_on = explicit_flag is True
    # Retired: parsed, logged and resolved like an unset key at boot
    # (hermes_cli/gateway_multiplex_mode.py:161-191, RETIRED_OPT_OUT_REASON :43-46).
    retired_off = explicit_flag is False
    gateway_live = gateway.running
    served_recorded = gateway.served_profiles_recorded
    if not manifest:
        return MigrationState(
            manifest_present=True,
            multiplex_flag_on=flag_on,
            multiplex_flag_retired_off=retired_off,
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
        multiplex_flag_retired_off=retired_off,
        default_gateway_live=gateway_live,
        served_recorded=served_recorded,
    )
