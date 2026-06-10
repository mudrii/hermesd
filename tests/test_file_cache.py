from __future__ import annotations

import json
import threading
from pathlib import Path

import yaml

from hermesd.file_cache import LastGoodFileCache


def test_json_mapping_invalid_shape_reuses_bad_mtime(tmp_path, monkeypatch):
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"ok": 1}))
    assert cache.read_json_mapping(path) == {"ok": 1}

    path.write_text(json.dumps(["bad-shape"]))
    open_calls = 0
    real_open = Path.open

    def counting_open(self: Path, *args, **kwargs):
        nonlocal open_calls
        if self == path:
            open_calls += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    assert cache.read_json_mapping(path) == {"ok": 1}
    assert cache.read_json_mapping(path) == {"ok": 1}
    assert open_calls == 1


def test_yaml_mapping_invalid_shape_reuses_bad_mtime(tmp_path, monkeypatch):
    cache = LastGoodFileCache()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"ok": 1}))
    assert cache.read_yaml_mapping(path) == {"ok": 1}

    path.write_text(yaml.safe_dump(["bad-shape"]))
    open_calls = 0
    real_open = Path.open

    def counting_open(self: Path, *args, **kwargs):
        nonlocal open_calls
        if self == path:
            open_calls += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    assert cache.read_yaml_mapping(path) == {"ok": 1}
    assert cache.read_yaml_mapping(path) == {"ok": 1}
    assert open_calls == 1


def test_json_list_invalid_shape_reuses_bad_mtime(tmp_path, monkeypatch):
    cache = LastGoodFileCache()
    path = tmp_path / "rows.json"
    path.write_text(json.dumps([{"ok": 1}]))
    assert cache.read_json_list(path) == [{"ok": 1}]

    path.write_text(json.dumps({"bad": "shape"}))
    open_calls = 0
    real_open = Path.open

    def counting_open(self: Path, *args, **kwargs):
        nonlocal open_calls
        if self == path:
            open_calls += 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    assert cache.read_json_list(path) == [{"ok": 1}]
    assert cache.read_json_list(path) == [{"ok": 1}]
    assert open_calls == 1


def test_same_path_json_and_yaml_reads_use_independent_mtimes(tmp_path):
    cache = LastGoodFileCache()
    path = tmp_path / "shared"
    path.write_text(json.dumps({"json": 1}))
    assert cache.read_json_mapping(path) == {"json": 1}

    path.write_text(yaml.safe_dump({"yaml": 2}))
    assert cache.read_yaml_mapping(path) == {"yaml": 2}


def test_file_cache_handles_concurrent_reads(tmp_path):
    cache = LastGoodFileCache()
    json_path = tmp_path / "data.json"
    yaml_path = tmp_path / "config.yaml"
    json_path.write_text(json.dumps({"json": 1}))
    yaml_path.write_text(yaml.safe_dump({"yaml": 2}))
    errors: list[BaseException] = []

    def read_repeatedly() -> None:
        try:
            for _ in range(100):
                assert cache.read_json_mapping(json_path) == {"json": 1}
                assert cache.read_yaml_mapping(yaml_path) == {"yaml": 2}
        except BaseException as exc:  # pragma: no cover - exercised on failure
            errors.append(exc)

    threads = [threading.Thread(target=read_repeatedly) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
