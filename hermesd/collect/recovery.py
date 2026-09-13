"""Database recovery evidence: repair ledger, forensic backups, retired WALs.

Everything here is a *sibling of ``state.db``*, named after it, and written by
hermes-agent's own repair code:

* ``state.db.repair-attempts.json`` — the cross-restart attempt ledger
  (``_repair_ledger_path``, ``hermes_state_repair.py:317-318``; shape written by
  ``_record_repair_outcome``, ``:409-431``);
* ``state.db.malformed-backup-<stamp>[_<seq>]`` plus ``-wal``/``-shm``/``-journal``
  sidecars — the forensic copies (``_backup_db_file``, ``:481-513``), retained to
  ``_MAX_MALFORMED_BACKUPS`` (``:49``) by ``_prune_malformed_backups`` (``:443-451``);
* ``state.db.retired-wal-<utc stamp>-<pid>[-n]/`` — durable captures of a WAL
  generation whose inode was retired underneath a writer
  (``RETIRED_GENERATION_DIR_SUFFIX``, ``hermes_state_dbfile.py:228``,
  ``capture_retired_wal_generation`` ``:334-425``);
* ``state.db.repair.lock`` and ``state.db.auto-maintenance.lock``
  (``_open_lock_file``, ``hermes_state_repair.py:176-229``).

``~/.hermes/recovery/`` is deliberately **not** read: it holds operator-made
remediation bundles (``config.yaml.before``, ``git-status.before.txt``,
``repository.bundle``, …) that no upstream repair code writes, and treating it as
recovery evidence would report a human's cleanup as the agent's.

Hard limits, all of them load-bearing: no repair, no checkpoint, no integrity
check and no hashing. The live ``state.db`` is hundreds of megabytes, so the only
reads are one bounded directory listing, ``stat`` on the entries whose *names*
match, and two small JSON documents. Errors and corrupt manifests propagate, so
the ``db_recovery`` source falls back to its last-good value instead of reporting
a false all-clear.
"""

from __future__ import annotations

import json
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any

from hermesd.collect.common import (
    _age_seconds,
    _as_dict,
    _coerce_int,
    _exists_strict,
    _file_size,
    _iso_to_epoch,
    _mtime,
    _path_resolves_under,
    _read_text_capped,
    _safe_capped_file,
)
from hermesd.models import DbRecoveryState, RetiredWalGeneration

# Artifact name fragments, all of them suffixed onto the database file name.
_LEDGER_SUFFIX = ".repair-attempts.json"
_REPAIR_LOCK_SUFFIX = ".repair.lock"
_AUTO_MAINTENANCE_LOCK_SUFFIX = ".auto-maintenance.lock"
_MALFORMED_BACKUP_INFIX = ".malformed-backup-"
_BACKUP_STAGING_INFIX = ".backup-staging-"
_RETIRED_WAL_INFIX = ".retired-wal-"
# Upstream's two mid-write spellings for a forensic copy: the old ``.incomplete``
# suffix (which prefix-matches as a settled backup and sorts newest, so it would
# survive pruning forever) and the ``.backup-staging-*`` names that live outside
# the ``.malformed-backup-`` prefix on purpose (``hermes_state_repair.py:487-501``).
_INCOMPLETE_INFIX = ".incomplete"
_PARTIAL_SUFFIX = ".partial"
_MANIFEST_NAME = "manifest.json"
# Copied with a damaged database and pruned with it
# (``_DB_SIDECAR_SUFFIXES``, ``hermes_state_repair.py:51``).
_DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# Bound on the profile-home listing, matching the other bounded scans in
# ``hermesd/collect/operations.py``. ~/.hermes is untrusted and read every tick,
# so a directory stuffed with entries must not stall the collector — and hitting
# the cap sets ``scan_truncated`` so a bounded count never reads as an inventory.
_RECOVERY_SCAN_LIMIT = 200
# Display bound on an untrusted manifest string. Upstream writes a 19-char
# ISO stamp, a 20-char ``captured_at`` and short mode/trigger words.
_MAX_MANIFEST_TEXT_CHARS = 64


def _read_db_recovery(db_path: Path, root: Path, *, now: float) -> DbRecoveryState:
    """Recovery artifacts beside ``db_path``, confined under ``root``.

    ``root`` is the profile home: every matched entry is skipped when it is a
    symlink or resolves outside it, so a link swapped into the home cannot steer
    a read. ``iterdir`` and ``stat`` errors propagate rather than reading as an
    empty all-clear.
    """
    parent = db_path.parent
    if not _exists_strict(parent):
        return DbRecoveryState()

    name = db_path.name
    exact_names = (
        f"{name}{_LEDGER_SUFFIX}",
        f"{name}{_REPAIR_LOCK_SUFFIX}",
        f"{name}{_AUTO_MAINTENANCE_LOCK_SUFFIX}",
    )
    prefixes = (
        f"{name}{_MALFORMED_BACKUP_INFIX}",
        f"{name}{_BACKUP_STAGING_INFIX}",
        f"{name}{_RETIRED_WAL_INFIX}",
    )

    ledger: Path | None = None
    repair_lock = False
    maintenance_lock = False
    settled_backups: list[tuple[str, int, float]] = []
    sidecars: list[tuple[str, int]] = []
    backup_staging = 0
    generations: list[str] = []
    generation_staging = 0
    truncated = False

    for index, entry in enumerate(islice(parent.iterdir(), _RECOVERY_SCAN_LIMIT + 1)):
        if index == _RECOVERY_SCAN_LIMIT:
            truncated = True
            break
        entry_name = entry.name
        # Name test first: it is a pure string compare, so the ~100 unrelated
        # entries in a real home cost no syscalls at all.
        if entry_name not in exact_names and not entry_name.startswith(prefixes):
            continue
        if entry.is_symlink() or not _path_resolves_under(entry, root):
            continue

        if entry_name == exact_names[0]:
            ledger = entry if entry.is_file() else None
        elif entry_name == exact_names[1]:
            repair_lock = entry.is_file()
        elif entry_name == exact_names[2]:
            maintenance_lock = entry.is_file()
        elif entry_name.startswith(prefixes[0]):
            if entry_name.endswith(_DB_SIDECAR_SUFFIXES):
                if entry.is_file():
                    sidecars.append((entry_name, _file_size(entry)))
            elif _INCOMPLETE_INFIX in entry_name:
                backup_staging += 1
            elif entry.is_file():
                settled_backups.append((entry_name, _file_size(entry), _mtime(entry) or 0.0))
        elif entry_name.startswith(prefixes[1]):
            backup_staging += 1
        elif entry_name.startswith(prefixes[2]) and entry.is_dir():
            if entry_name.endswith(_PARTIAL_SUFFIX):
                generation_staging += 1
            else:
                generations.append(entry_name)

    # The directory name embeds a UTC ``%Y%m%d-%H%M%S`` stamp, so a reverse
    # lexical sort is chronological — the same ordering upstream uses for its
    # forensic backups (``_existing_malformed_backups``, ``:432-441``).
    generations.sort(reverse=True)
    newest = RetiredWalGeneration()
    if generations:
        newest = _read_retired_generation(parent / generations[0], root, now=now)
    return DbRecoveryState(
        **_ledger_fields(ledger, root, now=now),
        malformed_backup_count=len(settled_backups),
        malformed_backup_bytes=_backup_bytes(settled_backups, sidecars),
        newest_malformed_backup_age_seconds=_newest_age(settled_backups, now),
        malformed_backup_staging_count=backup_staging,
        retired_wal_count=len(generations),
        retired_wal_staging_count=generation_staging,
        newest_retired_wal=newest,
        repair_lock_file_present=repair_lock,
        auto_maintenance_lock_file_present=maintenance_lock,
        scan_truncated=truncated,
    )


def _ledger_fields(ledger: Path | None, root: Path, *, now: float) -> dict[str, Any]:
    """The attempt-ledger half of the state, or the absent-ledger defaults.

    A ledger that is present but unusable raises: upstream writes it with a plain
    ``write_text``, so a torn file is observable mid-write, and reporting it as
    "no failed repairs" would be the one wrong answer this reader must not give.
    """
    if ledger is None:
        return {"repair_ledger_present": False}
    data = _read_json_object(ledger, root)
    last_attempt = _capped_str(data.get("last_attempt"))
    return {
        "repair_ledger_present": True,
        "failed_attempts": _coerce_int(data.get("failed_attempts")),
        "last_attempt": last_attempt,
        "last_attempt_age_seconds": _age_seconds(_local_iso_to_epoch(last_attempt), now),
    }


def _read_retired_generation(generation: Path, root: Path, *, now: float) -> RetiredWalGeneration:
    """The newest settled generation's manifest, or an unread record when absent.

    ``os.replace`` publishes the whole staging directory at once with the manifest
    already inside it, so a settled generation without ``manifest.json`` is
    anomalous — but it is still a directory on disk, and the honest report is
    "counted, contents unknown" rather than a fabricated zero-valued capture.
    """
    manifest = generation / _MANIFEST_NAME
    if not _exists_strict(manifest):
        return RetiredWalGeneration()
    data = _read_json_object(manifest, root)
    captured_at = _capped_str(data.get("captured_at"))
    return RetiredWalGeneration(
        manifest_present=True,
        captured_at=captured_at,
        # ``%Y-%m-%dT%H:%M:%SZ`` — the one UTC stamp in this reader, so it goes
        # through the shared parser while the ledger's naive local stamp does not.
        captured_at_age_seconds=_age_seconds(_iso_to_epoch(captured_at), now),
        trigger=_capped_str(data.get("trigger")),
        wal_bytes=_coerce_int(_as_dict(data.get("wal")).get("bytes")),
        main_mode=_capped_str(_as_dict(data.get("main")).get("mode")),
    )


def _read_json_object(path: Path, root: Path) -> dict[str, Any]:
    """Parse a small JSON object, refusing anything symlinked, oversized or torn.

    Raises rather than returning ``{}``: a present-but-unusable document must fail
    its source so the panel keeps last-good values instead of showing an all-clear.
    """
    if not _safe_capped_file(path, root):
        raise RuntimeError(f"{path.name} is not a readable file under {root.name}")
    text = _read_text_capped(path, root)
    if not text:
        raise RuntimeError(f"{path.name} is present but could not be read")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path.name} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{path.name} is not a JSON object")
    return decoded


def _backup_bytes(
    settled: list[tuple[str, int, float]],
    sidecars: list[tuple[str, int]],
) -> int:
    """Settled copies plus the sidecars that belong to a copy still on disk.

    A sidecar whose main copy was pruned is skipped: upstream prunes them as a
    bundle, so a straggler is debris and not part of any recoverable image.
    """
    total = sum(size for _, size, _ in settled)
    names = {name for name, _, _ in settled}
    for sidecar_name, size in sidecars:
        for suffix in _DB_SIDECAR_SUFFIXES:
            if sidecar_name.endswith(suffix) and sidecar_name[: -len(suffix)] in names:
                total += size
                break
    return total


def _newest_age(settled: list[tuple[str, int, float]], now: float) -> float | None:
    newest = max((mtime for _, _, mtime in settled if mtime > 0), default=None)
    return _age_seconds(newest, now)


def _capped_str(value: object) -> str:
    """A length-bounded rendering of an untrusted manifest value."""
    if not isinstance(value, str):
        return ""
    return value[:_MAX_MANIFEST_TEXT_CHARS]


def _local_iso_to_epoch(value: object) -> float | None:
    """Epoch seconds for the ledger's naive *local* stamp; garbage is None.

    Deliberately not ``common._iso_to_epoch``, which reads a naive stamp as UTC:
    ``_record_repair_outcome`` writes ``datetime.now().isoformat(timespec="seconds")``
    (``hermes_state_repair.py:428-430``), i.e. local time with no zone, so the
    shared parser would shift every ledger age by the host's UTC offset. A stamp
    that does carry an offset is honoured exactly as written.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
