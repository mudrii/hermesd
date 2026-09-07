"""mtime-keyed JSON/YAML last-good file cache."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from hermesd.file_cache import LastGoodFileCache


def test_cache_hit_reuses_value_until_mtime_changes(tmp_path):
    """An unchanged mtime returns cached data; a newer mtime reloads it."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"v": 1}))
    assert cache.read_json_mapping(path) == {"v": 1}
    original = path.stat()

    path.write_text(json.dumps({"v": 99}))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert cache.read_json_mapping(path) == {"v": 1}

    path.write_text(json.dumps({"v": 2}))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 10_000_000_000))
    assert cache.read_json_mapping(path) == {"v": 2}


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


@pytest.mark.parametrize("kind", ["json", "yaml", "json_list"])
def test_deleted_file_evicts_cache_and_returns_default(tmp_path, kind):
    """A deleted source file must stop serving its last-good value forever."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.src"
    if kind == "json":
        path.write_text(json.dumps({"value": 1}))
        read, loaded, default = cache.read_json_mapping, {"value": 1}, {}
    elif kind == "yaml":
        path.write_text(yaml.safe_dump({"value": 1}))
        read, loaded, default = cache.read_yaml_mapping, {"value": 1}, {}
    else:
        path.write_text(json.dumps([{"value": 1}]))
        read, loaded, default = cache.read_json_list, [{"value": 1}], []
    assert read(path) == loaded

    path.unlink()

    assert read(path) == default
    # Recreating the file must be picked up rather than serving the evicted value.
    if kind == "json":
        path.write_text(json.dumps({"value": 2}))
        assert read(path) == {"value": 2}
    elif kind == "yaml":
        path.write_text(yaml.safe_dump({"value": 2}))
        assert read(path) == {"value": 2}
    else:
        path.write_text(json.dumps([{"value": 2}]))
        assert read(path) == [{"value": 2}]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="chmod 000 does not block stat when running as root",
)
def test_unstatable_file_preserves_last_good_value(tmp_path):
    """A transient stat error (not deletion) keeps serving the last-good value."""
    cache = LastGoodFileCache()
    directory = tmp_path / "locked"
    directory.mkdir()
    path = directory / "data.json"
    path.write_text(json.dumps({"value": 1}))
    assert cache.read_json_mapping(path) == {"value": 1}

    directory.chmod(0o000)
    try:
        assert cache.read_json_mapping(path) == {"value": 1}
    finally:
        directory.chmod(0o755)

    assert cache.read_json_mapping(path) == {"value": 1}


@pytest.mark.parametrize("kind", ["json", "yaml"])
def test_invalid_utf8_preserves_last_good_value(tmp_path, kind):
    cache = LastGoodFileCache()
    path = tmp_path / f"data.{kind}"
    path.write_text('{"value": 1}' if kind == "json" else "value: 1\n")
    read = cache.read_json_mapping if kind == "json" else cache.read_yaml_mapping
    assert read(path) == {"value": 1}

    path.write_bytes(b"\xff")

    assert read(path) == {"value": 1}


def _clone_stat(result: os.stat_result, *, st_mtime: float, st_mtime_ns: int) -> os.stat_result:
    """Rebuild a stat result with a controlled mtime granularity.

    Simulates a filesystem whose float st_mtime has 1-second granularity while
    st_mtime_ns still distinguishes successive writes.
    """
    return os.stat_result(
        (
            result.st_mode,
            result.st_ino,
            result.st_dev,
            result.st_nlink,
            result.st_uid,
            result.st_gid,
            result.st_size,
            result.st_atime,
            st_mtime,
            result.st_ctime,
        ),
        {
            "st_atime_ns": result.st_atime_ns,
            "st_mtime_ns": st_mtime_ns,
            "st_ctime_ns": result.st_ctime_ns,
        },
    )


def _pin_coarse_mtime(monkeypatch, path: Path, reference: os.stat_result) -> None:
    """Force path.stat() to report reference's float mtime but a newer st_mtime_ns."""
    real_stat = Path.stat

    def fake_stat(self: Path, *args, **kwargs) -> os.stat_result:
        result = real_stat(self, *args, **kwargs)
        if self == path:
            return _clone_stat(
                result,
                st_mtime=reference.st_mtime,
                st_mtime_ns=reference.st_mtime_ns + 1_000_000,
            )
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)


def test_malformed_json_fixed_within_same_second_is_reloaded(tmp_path, monkeypatch):
    """A same-second fix to a malformed file must not be hidden by bad-mtime caching."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text("{ not valid json")
    assert cache.read_json_mapping(path) == {}
    reference = path.stat()

    path.write_text(json.dumps({"fixed": 1}))
    _pin_coarse_mtime(monkeypatch, path, reference)

    assert cache.read_json_mapping(path) == {"fixed": 1}


def test_malformed_yaml_fixed_within_same_second_is_reloaded(tmp_path, monkeypatch):
    cache = LastGoodFileCache()
    path = tmp_path / "config.yaml"
    path.write_text(":\n  - [unclosed")
    assert cache.read_yaml_mapping(path) == {}
    reference = path.stat()

    path.write_text(yaml.safe_dump({"fixed": 1}))
    _pin_coarse_mtime(monkeypatch, path, reference)

    assert cache.read_yaml_mapping(path) == {"fixed": 1}


def test_valid_json_changed_within_same_second_is_reloaded(tmp_path, monkeypatch):
    """Same-second valid-content changes must invalidate the cached value too."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"v": 1}))
    assert cache.read_json_mapping(path) == {"v": 1}
    reference = path.stat()

    path.write_text(json.dumps({"v": 2}))
    _pin_coarse_mtime(monkeypatch, path, reference)

    assert cache.read_json_mapping(path) == {"v": 2}
