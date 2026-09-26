"""mtime-keyed JSON/YAML last-good file cache."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
import yaml

from hermesd.file_cache import _MAX_PARSED_FILE_BYTES, LastGoodFileCache


def test_cache_hit_reuses_value_until_mtime_changes(tmp_path):
    """An unchanged mtime returns cached data; a newer mtime reloads it."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"v": 1}))
    assert cache.read_json_mapping(path) == {"v": 1}
    original = path.stat()

    # Same size, same inode, same mtime: indistinguishable, so still cached.
    path.write_text(json.dumps({"v": 9}))
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
    real_os_open = os.open

    def counting_open(name, flags, *args, **kwargs):
        nonlocal open_calls
        if Path(name) == path:
            open_calls += 1
        return real_os_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting_open)

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
    real_os_open = os.open

    def counting_open(name, flags, *args, **kwargs):
        nonlocal open_calls
        if Path(name) == path:
            open_calls += 1
        return real_os_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting_open)

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
    real_os_open = os.open

    def counting_open(name, flags, *args, **kwargs):
        nonlocal open_calls
        if Path(name) == path:
            open_calls += 1
        return real_os_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting_open)

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


def _clone_stat(
    result: os.stat_result,
    *,
    st_mtime: float,
    st_mtime_ns: int,
    st_size: int | None = None,
) -> os.stat_result:
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
            result.st_size if st_size is None else st_size,
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


def test_oversized_json_is_refused_and_keeps_the_last_good_value(tmp_path):
    """A document over the parse cap is treated exactly like a malformed one."""
    cache = LastGoodFileCache()
    path = tmp_path / "models_dev_cache.json"
    path.write_text(json.dumps({"v": 1}))
    assert cache.read_json_mapping(path) == {"v": 1}

    padding = "p" * (_MAX_PARSED_FILE_BYTES + 1)
    path.write_text(json.dumps({"v": 2, "pad": padding}))
    assert path.stat().st_size > _MAX_PARSED_FILE_BYTES

    assert cache.read_json_mapping(path) == {"v": 1}
    assert cache.last_read_was_stale(path) is True


def test_oversized_json_is_not_reparsed_on_every_read(tmp_path, monkeypatch):
    cache = LastGoodFileCache()
    path = tmp_path / "huge.json"
    path.write_text(json.dumps({"pad": "p" * (_MAX_PARSED_FILE_BYTES + 1)}))

    def exploding_open(*args, **kwargs):
        raise AssertionError("an over-cap file must never be opened")

    # Both open lanes: readers using Path.open and the descriptor-level guard.
    monkeypatch.setattr(Path, "open", exploding_open)
    monkeypatch.setattr(os, "open", exploding_open)

    assert cache.read_json_mapping(path) == {}
    assert cache.read_json_mapping(path) == {}


def test_file_grown_past_cap_between_stat_and_read_is_rejected(tmp_path, monkeypatch):
    """A stat that under-reports size must not let an oversized document parse."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"v": 1}))
    assert cache.read_json_mapping(path) == {"v": 1}

    path.write_text(json.dumps({"v": 2, "pad": "p" * (_MAX_PARSED_FILE_BYTES + 1)}))
    real_stat = Path.stat

    def small_size_stat(self: Path, *args, **kwargs) -> os.stat_result:
        result = real_stat(self, *args, **kwargs)
        if self == path:
            return _clone_stat(
                result,
                st_mtime=result.st_mtime,
                st_mtime_ns=result.st_mtime_ns,
                st_size=1,
            )
        return result

    monkeypatch.setattr(Path, "stat", small_size_stat)

    assert cache.read_json_mapping(path) == {"v": 1}
    assert cache.last_read_was_stale(path) is True


def test_file_swapped_mid_read_is_not_cached_under_stale_mtime(tmp_path, monkeypatch):
    """A post-read mtime mismatch must drop the read instead of caching it."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"v": 1}))
    assert cache.read_json_mapping(path) == {"v": 1}

    path.write_text(json.dumps({"v": 2}))
    real_stat = Path.stat
    stat_calls = 0

    def swapped_stat(self: Path, *args, **kwargs) -> os.stat_result:
        nonlocal stat_calls
        result = real_stat(self, *args, **kwargs)
        if self == path:
            stat_calls += 1
            if stat_calls == 2:
                return _clone_stat(
                    result,
                    st_mtime=result.st_mtime,
                    st_mtime_ns=result.st_mtime_ns + 1_000_000,
                )
        return result

    monkeypatch.setattr(Path, "stat", swapped_stat)
    assert cache.read_json_mapping(path) == {"v": 1}
    assert cache.last_read_was_stale(path) is True

    monkeypatch.undo()
    assert cache.read_json_mapping(path) == {"v": 2}


def test_file_just_under_the_cap_still_loads(tmp_path):
    """models_dev_cache.json is ~4.5 MB in a real ~/.hermes and must keep loading."""
    cache = LastGoodFileCache()
    path = tmp_path / "big-but-ok.json"
    path.write_text(json.dumps({"pad": "p" * (5 * 1024 * 1024)}))
    assert path.stat().st_size < _MAX_PARSED_FILE_BYTES

    assert cache.read_json_mapping(path)["pad"].startswith("p")


@pytest.mark.parametrize("kind", ["json", "yaml"])
def test_transient_open_failure_recovers_without_mtime_change(tmp_path, monkeypatch, kind):
    """A PermissionError mid-load must not poison the mtime as permanently bad.

    The file's content changes (new mtime) while the open fails: the cache
    serves the last-good value and marks the read stale, but — unlike a parse
    error — it must NOT record the new mtime as bad. Once access is restored
    with the SAME mtime, the very next refresh returns the new value without
    any mtime change, and the stale flag clears.
    """
    cache = LastGoodFileCache()
    path = tmp_path / f"data.{kind}"
    if kind == "json":
        path.write_text('{"v": 1}')
        read = cache.read_json_mapping
    else:
        path.write_text("v: 1\n")
        read = cache.read_yaml_mapping
    assert read(path) == {"v": 1}

    # Swap in new content (new mtime), then make open fail.
    if kind == "json":
        path.write_text('{"v": 2}')
    else:
        path.write_text("v: 2\n")

    real_os_open = os.open
    open_calls = 0

    def failing_open(name, flags, *args, **kwargs):
        nonlocal open_calls
        if Path(name) == path:
            open_calls += 1
            raise PermissionError("file locked by another process")
        return real_os_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)

    assert read(path) == {"v": 1}
    assert cache.last_read_was_stale(path) is True
    # Not bad-mtime cached: a second failed read retries the open instead of
    # short-circuiting on the poisoned mtime (parse errors stay cached — see
    # the *_invalid_shape_reuses_bad_mtime tests).
    assert read(path) == {"v": 1}
    assert open_calls == 2

    monkeypatch.undo()
    assert read(path) == {"v": 2}
    assert cache.last_read_was_stale(path) is False


def test_transient_oserror_on_never_readable_file_returns_default(tmp_path, monkeypatch):
    """A transient I/O failure with no cached value yields the default and no stale flag."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"v": 1}))

    real_os_open = os.open

    def failing_open(name, flags, *args, **kwargs):
        if Path(name) == path:
            raise OSError("transient read failure")
        return real_os_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)

    assert cache.read_json_mapping(path) == {}
    assert cache.last_read_was_stale(path) is False


def test_invalid_utf8_keeps_bad_mtime_caching(tmp_path, monkeypatch):
    """UnicodeDecodeError is a content (decode) failure, not transient I/O.

    The same bytes always fail to decode, so — like a parse error — the mtime
    is remembered as bad and the file is not re-opened every refresh.
    """
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text('{"ok": 1}')
    assert cache.read_json_mapping(path) == {"ok": 1}

    path.write_bytes(b"\xff")
    open_calls = 0
    real_os_open = os.open

    def counting_open(name, flags, *args, **kwargs):
        nonlocal open_calls
        if Path(name) == path:
            open_calls += 1
        return real_os_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting_open)

    assert cache.read_json_mapping(path) == {"ok": 1}
    assert cache.read_json_mapping(path) == {"ok": 1}
    assert open_calls == 1


def test_deeply_nested_json_is_refused_and_not_reparsed(tmp_path, monkeypatch):
    """A nesting bomb is a content failure, not a transient one.

    ``json.loads`` refuses nesting deep enough to exhaust the decoder with
    ``RecursionError``, which was not in ``load_errors``: the exception escaped
    the mtime bookkeeping, so the file was re-parsed on every refresh instead of
    being remembered as bad for its mtime.
    """
    cache = LastGoodFileCache()
    path = tmp_path / "bomb.json"
    path.write_text("[" * 3000 + "]" * 3000)

    open_calls = 0
    real_os_open = os.open

    def counting_open(name, flags, *args, **kwargs):
        nonlocal open_calls
        if Path(name) == path:
            open_calls += 1
        return real_os_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting_open)

    assert cache.read_json_mapping(path) == {}
    assert cache.read_json_mapping(path) == {}
    assert open_calls == 1


def test_same_mtime_rewrite_with_new_size_is_reloaded(tmp_path):
    """Coarse-timestamp filesystems: a same-mtime rewrite must not stay stale forever."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"v": 1}))
    assert cache.read_json_mapping(path) == {"v": 1}
    original = path.stat()

    path.write_text(json.dumps({"v": 1234}))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert cache.read_json_mapping(path) == {"v": 1234}


def test_same_mtime_atomic_replace_is_reloaded(tmp_path):
    """An atomic rename-over (new inode) with identical size and mtime still reloads."""
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"v": 1}))
    assert cache.read_json_mapping(path) == {"v": 1}
    original = path.stat()

    replacement = tmp_path / "data.json.tmp"
    replacement.write_text(json.dumps({"v": 2}))
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    # Keep the old inode alive so the filesystem cannot recycle its number.
    keep = tmp_path / "old.json"
    os.link(path, keep)
    replacement.replace(path)
    assert path.stat().st_ino != original.st_ino

    assert cache.read_json_mapping(path) == {"v": 2}


def test_bad_file_fixed_with_same_mtime_but_new_size_is_reloaded(tmp_path):
    cache = LastGoodFileCache()
    path = tmp_path / "data.json"
    path.write_text("{ not valid")
    assert cache.read_json_mapping(path) == {}
    original = path.stat()

    path.write_text(json.dumps({"fixed": True}))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert cache.read_json_mapping(path) == {"fixed": True}


@pytest.mark.parametrize("document", ["false\n", "0\n", "[]\n", "- a\n", "text\n"])
def test_non_mapping_yaml_document_keeps_last_good(tmp_path, document):
    """Every non-mapping YAML document is rejected alike, falsy ones included."""
    cache = LastGoodFileCache()
    path = tmp_path / "config.yaml"
    path.write_text("ok: 1\n")
    assert cache.read_yaml_mapping(path) == {"ok": 1}

    path.write_text(document)

    assert cache.read_yaml_mapping(path) == {"ok": 1}
    assert cache.last_read_was_stale(path) is True


def test_empty_yaml_document_reads_as_empty_mapping(tmp_path):
    cache = LastGoodFileCache()
    path = tmp_path / "config.yaml"
    path.write_text("")
    assert cache.read_yaml_mapping(path) == {}
    assert cache.last_read_was_stale(path) is False


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_fifo_source_is_refused_without_blocking(tmp_path):
    """A FIFO in place of config.yaml must not hang the collector thread."""
    cache = LastGoodFileCache()
    path = tmp_path / "config.yaml"
    os.mkfifo(path)
    result: list[object] = []
    reader = threading.Thread(target=lambda: result.append(cache.read_yaml_mapping(path)))
    reader.daemon = True
    reader.start()
    reader.join(timeout=5)
    if reader.is_alive():  # pragma: no cover - only on regression
        # Unblock the reader so the test process can exit.
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("reading a FIFO blocked")
    assert result == [{}]
