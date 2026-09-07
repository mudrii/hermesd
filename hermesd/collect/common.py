"""Coercion, path-safety and file-stat primitives shared by every collector."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

# Upper bound on any plain-text file read whole (memory cards, manifests,
# frontmatter, excerpts): ~/.hermes is untrusted input for a read-only viewer.
_MAX_TEXT_READ_BYTES = 256 * 1024


def _today_epoch() -> float:
    import datetime

    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _local_date() -> str:
    return time.strftime("%Y-%m-%d")


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


def _profile_db_mtime(db_path: Path) -> float | None:
    mtimes = [
        mtime
        for candidate in (db_path, db_path.with_name(f"{db_path.name}-wal"))
        if (mtime := _mtime(candidate)) is not None
    ]
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
