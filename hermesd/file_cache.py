from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard, TypeVar

import yaml

T = TypeVar("T")
JsonMapping = dict[str, object]
JsonObjectList = list[JsonMapping]

# Upper bound on a single JSON/YAML document parsed whole and then retained for
# the life of the process. ~/.hermes ships a 4.5 MB models_dev_cache.json, so
# the cap has to clear that with headroom while still refusing a file large
# enough to stall a refresh or exhaust memory.
_MAX_PARSED_FILE_BYTES = 8 * 1024 * 1024


class _OversizedFileError(OSError):
    """A file read crossed _MAX_PARSED_FILE_BYTES after the stat-based check."""


class LastGoodFileCache:
    def __init__(self) -> None:
        # Guards the mtime/value/bad-mtime cache dicts below shared across threads.
        self._lock = threading.RLock()
        # mtimes are keyed on st_mtime_ns (matching db.py): float st_mtime can
        # collide on filesystems with coarse (1-second) mtime granularity.
        self._json_mtimes: dict[str, int] = {}
        self._json_list_mtimes: dict[str, int] = {}
        self._yaml_mtimes: dict[str, int] = {}
        self._json_values: dict[str, JsonMapping] = {}
        self._json_lists: dict[str, JsonObjectList] = {}
        self._yaml_values: dict[str, JsonMapping] = {}
        self._json_bad_mtimes: dict[str, int] = {}
        self._json_list_bad_mtimes: dict[str, int] = {}
        self._yaml_bad_mtimes: dict[str, int] = {}
        # True when the most recent read of a path served a previously cached
        # value because the file could not be read or parsed. Callers use it to
        # mark their source degraded while still showing last-good data
        # (mirrors HermesDB.last_read_sessions_stale).
        self._stale_reads: dict[str, bool] = {}

    def last_read_was_stale(self, path: Path) -> bool:
        """Whether the last read of path fell back to a cached value."""
        with self._lock:
            return self._stale_reads.get(str(path), False)

    def read_json_mapping(self, path: Path) -> JsonMapping:
        return self._cached_read(
            path,
            mtimes=self._json_mtimes,
            values=self._json_values,
            bad_mtimes=self._json_bad_mtimes,
            load=lambda: _load_json(path),
            load_errors=(OSError, UnicodeError, json.JSONDecodeError),
            is_valid=_is_json_mapping,
            default_factory=dict,
        )

    def read_json_list(self, path: Path) -> JsonObjectList:
        return self._cached_read(
            path,
            mtimes=self._json_list_mtimes,
            values=self._json_lists,
            bad_mtimes=self._json_list_bad_mtimes,
            load=lambda: _load_json(path),
            load_errors=(OSError, UnicodeError, json.JSONDecodeError),
            is_valid=_is_json_object_list,
            default_factory=list,
        )

    def read_yaml_mapping(self, path: Path) -> JsonMapping:
        return self._cached_read(
            path,
            mtimes=self._yaml_mtimes,
            values=self._yaml_values,
            bad_mtimes=self._yaml_bad_mtimes,
            load=lambda: _load_yaml(path),
            load_errors=(OSError, UnicodeError, yaml.YAMLError),
            is_valid=_is_json_mapping,
            default_factory=dict,
        )

    def _cached_read(
        self,
        path: Path,
        mtimes: dict[str, int],
        values: dict[str, T],
        bad_mtimes: dict[str, int],
        load: Callable[[], object],
        load_errors: tuple[type[Exception], ...],
        is_valid: Callable[[object], TypeGuard[T]],
        default_factory: Callable[[], T],
    ) -> T:
        with self._lock:
            key = str(path)
            try:
                stat = path.stat()
                mtime = stat.st_mtime_ns
            except FileNotFoundError:
                # The source is gone, not transiently unreadable: evict rather
                # than serve a deleted file's value forever.
                mtimes.pop(key, None)
                bad_mtimes.pop(key, None)
                values.pop(key, None)
                self._stale_reads[key] = False
                return default_factory()
            except OSError:
                return self._stale(key, values, default_factory)
            if mtimes.get(key) == mtime and key in values:
                self._stale_reads[key] = False
                return values[key]
            if bad_mtimes.get(key) == mtime:
                return self._stale(key, values, default_factory)
            if stat.st_size > _MAX_PARSED_FILE_BYTES:
                # Treated exactly like a malformed file: remembered as bad for
                # this mtime so the size is not re-checked every refresh.
                bad_mtimes[key] = mtime
                return self._stale(key, values, default_factory)
            try:
                value = load()
            except load_errors as exc:
                # Transient I/O failures (permission, lock, file vanished
                # between stat and open) must NOT poison the mtime: the file
                # may be readable again on the next refresh with the same
                # mtime, and bad-marking would serve the stale value forever.
                # Deterministic content failures (decode, parse) keep the
                # bad-mtime cache so the file is not re-parsed every refresh.
                # _OversizedFileError subclasses OSError but is a deterministic
                # size rejection, so it is excluded from the transient branch.
                if isinstance(exc, OSError) and not isinstance(exc, _OversizedFileError):
                    return self._stale(key, values, default_factory)
                bad_mtimes[key] = mtime
                return self._stale(key, values, default_factory)
            if not is_valid(value):
                bad_mtimes[key] = mtime
                return self._stale(key, values, default_factory)
            try:
                post_read_mtime = path.stat().st_mtime_ns
            except OSError:
                post_read_mtime = mtime
            if post_read_mtime != mtime:
                # The file was swapped mid-read: the parsed bytes may not match
                # the pre-read stat, so drop them uncached and let the next
                # refresh re-read a stable file. Not recorded as a bad mtime —
                # the file is changing, not malformed.
                return self._stale(key, values, default_factory)
            self._stale_reads[key] = False
            mtimes[key] = mtime
            bad_mtimes.pop(key, None)
            # is_valid is a TypeGuard, so `value` is narrowed to T here: the
            # runtime shape check and the static type stay in lock step.
            values[key] = value
            return values[key]

    def _stale(
        self,
        key: str,
        values: dict[str, T],
        default_factory: Callable[[], T],
    ) -> T:
        # Only a fallback onto a previously good value counts as stale; a file
        # that was never readable yields the default and is simply absent.
        self._stale_reads[key] = key in values
        return values.get(key, default_factory())


def _is_json_mapping(value: object) -> TypeGuard[JsonMapping]:
    return isinstance(value, dict)


def _is_json_object_list(value: object) -> TypeGuard[JsonObjectList]:
    return isinstance(value, list) and all(isinstance(entry, dict) for entry in value)


def _read_capped(path: Path) -> str:
    # The stat-based size check races with a growing or swapped file, so the
    # read itself is capped and anything past the cap is never decoded/parsed.
    with path.open("rb") as handle:
        data = handle.read(_MAX_PARSED_FILE_BYTES + 1)
    if len(data) > _MAX_PARSED_FILE_BYTES:
        raise _OversizedFileError(f"{path} grew past {_MAX_PARSED_FILE_BYTES} bytes")
    return data.decode("utf-8")


def _load_json(path: Path) -> object:
    return json.loads(_read_capped(path))


def _load_yaml(path: Path) -> object:
    return yaml.safe_load(_read_capped(path)) or {}
