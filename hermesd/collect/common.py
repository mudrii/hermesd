"""Coercion, path-safety and file-stat primitives shared by every collector."""

from __future__ import annotations

import contextlib
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from typing import Any, BinaryIO

# Upper bound on any plain-text file read whole (memory cards, manifests,
# frontmatter, excerpts): ~/.hermes is untrusted input for a read-only viewer.
_MAX_TEXT_READ_BYTES = 256 * 1024
# Width of the one-line previews shown in compact panels (SOUL.md and cron
# output excerpts); wide enough for a headline, short enough for a cell.
_EXCERPT_MAX_CHARS = 80


def _iso_to_epoch(value: object) -> float | None:
    """Epoch seconds for an ISO-8601 stamp; naive values are UTC, garbage is None.

    The single ISO parser for every collector: ``~/.hermes`` writes timestamps
    with a trailing ``Z``, with an explicit offset, and without either.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _age_seconds(epoch: float | None, now: float) -> float | None:
    """Age of `epoch` at `now`, clamped at zero so clock skew never goes negative."""
    if epoch is None or not math.isfinite(epoch):
        return None
    return max(0.0, now - epoch)


def _today_epoch(now: float) -> float:
    """Epoch seconds of local midnight on the day containing `now`."""
    import datetime

    moment = datetime.datetime.fromtimestamp(now)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _local_date(now: float) -> str:
    """Local calendar date of `now` as YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _open_regular_file(path: Path) -> BinaryIO:
    """Open path read-only, refusing FIFOs, devices and directories.

    Opening a FIFO that has no writer blocks forever, which would hang the
    collector thread. A stat-then-open pre-check would leave a race — the path
    can be swapped for a FIFO between the check and the open — so the
    descriptor is opened nonblocking (O_NONBLOCK is a no-op on regular files)
    and the opened descriptor itself is validated as a regular file: the
    fstat is the only check, with no gap to race.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        is_regular = S_ISREG(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not is_regular:
        os.close(fd)
        raise OSError(f"{path} is not a regular file")
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _read_text_capped(path: Path, root: Path | None = None) -> str:
    """Read at most _MAX_TEXT_READ_BYTES of a non-symlinked file under root."""
    if path.is_symlink() or (root is not None and not _path_resolves_under(path, root)):
        return ""
    try:
        with _open_regular_file(path) as handle:
            return handle.read(_MAX_TEXT_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _read_text_capped_strict(path: Path, root: Path | None = None) -> str:
    """Like ``_read_text_capped``, but an I/O failure raises instead of reading as empty.

    The signature-cached readers (word/card counts, SOUL excerpt, skill
    frontmatter) key on ``_file_signature`` — (path, mtime_ns, size): a failed
    read returned as "" would be recorded under the new signature as a
    *successful* empty, blanking the panel, hiding the source from
    health.failed_sources, and surviving even after the file becomes readable
    again. Raising lets the caller fail the source to its last-good value and
    retry on the next poll. Unsafe paths (symlink, escaping root) still read
    as absent, matching the lenient reader.
    """
    if path.is_symlink() or (root is not None and not _path_resolves_under(path, root)):
        return ""
    with _open_regular_file(path) as handle:
        return handle.read(_MAX_TEXT_READ_BYTES).decode("utf-8", errors="replace")


def _safe_capped_file(path: Path, root: Path) -> bool:
    """True when path is a non-symlinked file under root within the byte cap."""
    if path.is_symlink() or not _path_resolves_under(path, root):
        return False
    return _file_size(path) <= _MAX_TEXT_READ_BYTES


def _exists_strict(path: Path) -> bool:
    """Like Path.exists(), but only absence is False; other OSErrors propagate.

    Python 3.14 made Path.exists() return False on EACCES too, which would turn
    an unreadable ~/.hermes directory into "no data" instead of a failed source.
    """
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _read_tail_text(path: Path, max_bytes: int) -> str:
    """Read at most the last max_bytes of path, decoded with replacement.

    A window that starts mid-line drops that partial first line: its label
    (``api_key=``) may be cut off, leaving a bare secret tail no redaction
    rule can recognise. A window with no line break at all is dropped whole.
    """
    with _open_regular_file(path) as handle:
        handle.seek(0, 2)
        size = handle.tell()
        is_cut = size > max_bytes
        # A cut window reads one byte early, so a window that opens exactly on
        # a line start keeps that line (the extra byte is the "\n" before it).
        start = size - max_bytes - 1 if is_cut else 0
        handle.seek(start)
        # Bound the read so bytes appended after the size check are excluded.
        data = handle.read(size - start)
    if is_cut:
        data = data.partition(b"\n")[2]
    return data.decode("utf-8", errors="replace")


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _safe_mtime(path: Path) -> float:
    return _mtime(path) or 0.0


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _file_signature(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return str(path), stat.st_mtime_ns, stat.st_size


_DbSourceSignature = tuple[tuple[int, int, int] | None, ...]


def _db_source_signature(db_path: Path) -> _DbSourceSignature | None:
    """(st_mtime_ns, st_size, st_ino) of a SQLite db and its -wal sidecar.

    Stricter change key than mtime alone: a same-timestamp write on a
    coarse-granularity filesystem still changes the size or inode. None when
    neither path can be stat'd ("unknown", not "unchanged").
    """
    signature: list[tuple[int, int, int] | None] = []
    for candidate in (db_path, db_path.with_name(f"{db_path.name}-wal")):
        try:
            stat = candidate.stat()
        except OSError:
            signature.append(None)
            continue
        signature.append((stat.st_mtime_ns, stat.st_size, stat.st_ino))
    return tuple(signature) if any(entry is not None for entry in signature) else None


# Confinement roots (the Hermes home, a profile home) are checked ~200 times a
# refresh but almost never move. Their resolution is cached per root, keyed on
# the root's own lstat so re-pointing a symlinked home is noticed at once.
_ROOT_RESOLVE_CACHE: dict[str, tuple[tuple[int, int, int], Path]] = {}
_ROOT_RESOLVE_CACHE_LIMIT = 64


def _resolved_root(root: Path) -> Path:
    try:
        stat = os.lstat(root)
    except OSError:
        return root.resolve(strict=False)
    key = str(root)
    signature = (stat.st_ino, stat.st_dev, stat.st_mtime_ns)
    cached = _ROOT_RESOLVE_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    resolved = root.resolve(strict=False)
    if len(_ROOT_RESOLVE_CACHE) >= _ROOT_RESOLVE_CACHE_LIMIT:
        _ROOT_RESOLVE_CACHE.clear()
    _ROOT_RESOLVE_CACHE[key] = (signature, resolved)
    return resolved


def _path_resolves_under(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = _resolved_root(root)
    except (OSError, RuntimeError):
        return False
    return resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)


def _safe_child_path(path: Path, root: Path) -> bool:
    return not path.is_symlink() and _path_resolves_under(path, root)


def _safe_or_absent_child_path(path: Path, root: Path) -> bool:
    if path.is_symlink():
        return False
    return not path.exists() or _path_resolves_under(path, root)


def _len_if_sized(value: object) -> int:
    if isinstance(value, dict | list | tuple | set):
        return len(value)
    return 0


def _int_mapping(value: object) -> dict[str, int]:
    raw = _as_dict(value)
    return {str(key): _coerce_int(count) for key, count in raw.items() if str(key)}


def _as_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return []


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # inf/nan raise on int(); match _coerce_float and fall back to 0.
        return int(value) if math.isfinite(value) else 0
    if isinstance(value, str):
        try:
            return int(value or "0")
        except ValueError:
            return 0
    if isinstance(value, (bytes, bytearray)):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


_TRUTHY_STATE_VALUES = frozenset({"1", "true", "yes", "on"})


def _coerce_bool(value: object) -> bool:
    """Boolean read for untrusted state, where ``bool()`` is wrong.

    ``bool("false")`` is True, so a corrupted or foreign payload could flip a
    warning on with a stringified flag. Only the shapes the writers actually
    produce count as truth: JSON ``true``, SQLite ``1`` (or a float), and the
    ``"1"`` / ``"true"`` / ``"yes"`` / ``"on"`` spellings upstream itself accepts
    (``gateway/scale_to_zero.py:38``). Everything else — including ``"false"``,
    ``"0"``, ``None`` and junk — is False.

    Use it for values read out of *state payloads and database rows* — files and
    tables a program writes, where a string in a boolean slot is corruption. Do
    **not** use it for settings a human authored (``config.yaml``, SKILL.md
    frontmatter): upstream reads those truthily when it decides what to do, so a
    strict read here would describe a policy the agent does not actually apply.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        # NaN and the infinities are not booleans any writer produces, and both
        # compare unequal to zero: a JSON ``NaN`` would otherwise read as set.
        return math.isfinite(value) and value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STATE_VALUES
    return False


def _coerce_float(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        try:
            result = float(value)
        except OverflowError:
            # An int past the float range (JSON allows arbitrary precision).
            return 0.0
        return result if math.isfinite(result) else 0.0
    if isinstance(value, str):
        try:
            result = float(value or "0")
        except ValueError:
            return 0.0
        return result if math.isfinite(result) else 0.0
    return 0.0


def _optional_epoch(value: object) -> float | None:
    """A persisted epoch, or None when the column holds no usable deadline.

    NULL, ``0`` and anything that coerces to a non-positive or non-finite number
    all read as "no deadline". ``0`` in particular is not an epoch: upstream
    stores it to mean *disarmed* (``set_compression_recovery_deadline`` writes
    ``normalized or None``, ``hermes_state_compression.py:413-431``), and
    surfacing it as a timestamp would render as January 1970.
    """
    coerced = _coerce_float(value)
    return coerced if coerced > 0.0 else None


# Bound on a JSON column read out of a database. The column itself can hold
# megabytes (SQLite does not enforce a length), and the payload is rendered, so
# anything past this reads as absent rather than being parsed. A 64 KiB cap
# comfortably holds a real goal's full contract and subgoal list.
_JSON_COLUMN_MAX_BYTES = 64 * 1024


def _json_object_capped(
    raw: object, max_bytes: int = _JSON_COLUMN_MAX_BYTES
) -> dict[str, Any] | None:
    """Decode a JSON object column, refusing payloads over ``max_bytes``.

    None means "no usable object" — absent, over the cap, malformed, or not a
    JSON object — which lets callers distinguish that from a genuine ``{}``.
    ``RecursionError`` joins the suppressed set because nesting deep enough to
    exhaust the decoder is just more junk: it must not fail the source.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if len(raw.encode("utf-8", errors="replace")) > max_bytes:
        return None
    with contextlib.suppress(json.JSONDecodeError, ValueError, RecursionError):
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            return decoded
    return None


# Head of an untrusted free-text value that an excerpt scans: far more than any
# cell shows, small enough that a megabyte error column is never redacted whole.
_EXCERPT_SCAN_CHARS = 4096


def _excerpt(value: object, cap: int) -> str:
    """One-line excerpt of untrusted text: whitespace collapsed, redacted, then capped.

    Redaction runs before the cap: slicing first can cut the ``Bearer ``/``key=``
    marker off a credential and keep the token itself.
    """
    # Deferred so this leaf module keeps no import-time dependency on redaction.
    from hermesd.collect.redaction import _redact_secret_text

    return _redact_secret_text(" ".join(str(value)[:_EXCERPT_SCAN_CHARS].split()))[:cap]


def _optional_int(value: object) -> int | None:
    """Coerce to int, preserving a genuine null (an exit code that never happened)."""
    return None if value is None else _coerce_int(value)


def _printable_capped(value: object, cap: int) -> str:
    """Printable, length-capped text safe to hand to a panel; "" for non-str.

    Control characters are stripped here, in the collector: a panel escapes
    markup but must not be the place an escape sequence is neutralised.
    """
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if char.isprintable())[:cap]
