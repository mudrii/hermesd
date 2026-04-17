from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


class LastGoodFileCache:
    def __init__(self) -> None:
        self._mtimes: dict[str, float] = {}
        self._json_values: dict[str, dict[str, Any]] = {}
        self._json_lists: dict[str, list[dict[str, Any]]] = {}
        self._yaml_values: dict[str, dict[str, Any]] = {}

    def read_json_mapping(self, path: Path) -> dict[str, Any]:
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self._json_values.get(key, {})
        if self._mtimes.get(key) == mtime and key in self._json_values:
            return self._json_values[key]
        try:
            with path.open() as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return self._json_values.get(key, {})
        if not isinstance(value, dict):
            return self._json_values.get(key, {})
        self._mtimes[key] = mtime
        self._json_values[key] = value
        return value

    def read_json_list(self, path: Path) -> list[dict[str, Any]]:
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self._json_lists.get(key, [])
        if self._mtimes.get(key) == mtime and key in self._json_lists:
            return self._json_lists[key]
        try:
            with path.open() as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return self._json_lists.get(key, [])
        if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
            return self._json_lists.get(key, [])
        self._mtimes[key] = mtime
        self._json_lists[key] = value
        return value

    def read_yaml_mapping(self, path: Path) -> dict[str, Any]:
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self._yaml_values.get(key, {})
        if self._mtimes.get(key) == mtime and key in self._yaml_values:
            return self._yaml_values[key]
        try:
            with path.open() as handle:
                value = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError):
            return self._yaml_values.get(key, {})
        if not isinstance(value, dict):
            return self._yaml_values.get(key, {})
        self._mtimes[key] = mtime
        self._yaml_values[key] = value
        return value
