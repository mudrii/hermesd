import json
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
