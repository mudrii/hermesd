from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import yaml

T = TypeVar("T")


class LastGoodFileCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._json_mtimes: dict[str, float] = {}
        self._json_list_mtimes: dict[str, float] = {}
        self._yaml_mtimes: dict[str, float] = {}
        self._json_values: dict[str, dict[str, Any]] = {}
        self._json_lists: dict[str, list[dict[str, Any]]] = {}
        self._yaml_values: dict[str, dict[str, Any]] = {}
        self._json_bad_mtimes: dict[str, float] = {}
        self._json_list_bad_mtimes: dict[str, float] = {}
        self._yaml_bad_mtimes: dict[str, float] = {}

    def read_json_mapping(self, path: Path) -> dict[str, Any]:
        return self._cached_read(
            path,
            mtimes=self._json_mtimes,
            values=self._json_values,
            bad_mtimes=self._json_bad_mtimes,
            load=lambda: _load_json(path),
            load_errors=(OSError, json.JSONDecodeError),
            is_valid=lambda value: isinstance(value, dict),
            default_factory=dict,
        )

    def read_json_list(self, path: Path) -> list[dict[str, Any]]:
        return self._cached_read(
            path,
            mtimes=self._json_list_mtimes,
            values=self._json_lists,
            bad_mtimes=self._json_list_bad_mtimes,
            load=lambda: _load_json(path),
            load_errors=(OSError, json.JSONDecodeError),
            is_valid=lambda value: (
                isinstance(value, list) and all(isinstance(entry, dict) for entry in value)
            ),
            default_factory=list,
        )

    def read_yaml_mapping(self, path: Path) -> dict[str, Any]:
        return self._cached_read(
            path,
            mtimes=self._yaml_mtimes,
            values=self._yaml_values,
            bad_mtimes=self._yaml_bad_mtimes,
            load=lambda: _load_yaml(path),
            load_errors=(OSError, yaml.YAMLError),
            is_valid=lambda value: isinstance(value, dict),
            default_factory=dict,
        )

    def _cached_read(
        self,
        path: Path,
        mtimes: dict[str, float],
        values: dict[str, T],
        bad_mtimes: dict[str, float],
        load: Callable[[], object],
        load_errors: tuple[type[Exception], ...],
        is_valid: Callable[[object], bool],
        default_factory: Callable[[], T],
    ) -> T:
        with self._lock:
            key = str(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                return values.get(key, default_factory())
            if mtimes.get(key) == mtime and key in values:
                return values[key]
            if bad_mtimes.get(key) == mtime:
                return values.get(key, default_factory())
            try:
                value = load()
            except load_errors:
                bad_mtimes[key] = mtime
                return values.get(key, default_factory())
            if not is_valid(value):
                bad_mtimes[key] = mtime
                return values.get(key, default_factory())
            mtimes[key] = mtime
            bad_mtimes.pop(key, None)
            values[key] = cast(T, value)
            return values[key]


def _load_json(path: Path) -> object:
    with path.open() as handle:
        return json.load(handle)


def _load_yaml(path: Path) -> object:
    with path.open() as handle:
        return yaml.safe_load(handle) or {}
