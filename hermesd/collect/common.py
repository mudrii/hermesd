"""Coercion, path-safety and file-stat primitives shared by every collector."""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


def _read_text_capped(path: Path, root: Path | None = None) -> str:
    """Read at most _MAX_TEXT_READ_BYTES of a non-symlinked file under root."""
    if path.is_symlink() or (root is not None and not _path_resolves_under(path, root)):
        return ""
    try:
        with path.open("rb") as handle:
            return handle.read(_MAX_TEXT_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


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
    """Read at most the last max_bytes of path, decoded with replacement."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


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


def _db_source_mtime_ns(db_path: Path) -> int | None:
    """Newest mtime of a SQLite db and its -wal sidecar, in nanoseconds.

    None when neither path can be stat'd, which callers treat as "unknown"
    rather than "unchanged". Nanoseconds because a float st_mtime collides on
    filesystems with 1-second granularity.
    """
    mtimes = []
    for candidate in (db_path, db_path.with_name(f"{db_path.name}-wal")):
        try:
            mtimes.append(candidate.stat().st_mtime_ns)
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def _path_resolves_under(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
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


def _coerce_float(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        result = float(value)
        return result if math.isfinite(result) else 0.0
    if isinstance(value, str):
        try:
            result = float(value or "0")
        except ValueError:
            return 0.0
        return result if math.isfinite(result) else 0.0
    return 0.0
