"""Shared collect helpers: tail reads bounded by max_bytes."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import pytest

from hermesd.collect.common import _read_tail_text, _read_text_capped


class _MutatingHandle:
    """Wrap a binary handle and mutate the file before the first read.

    Stands in for a concurrent writer (or truncator) that changes the file
    between _read_tail_text's size check and its read.
    """

    def __init__(
        self,
        handle: BinaryIO,
        path: Path,
        mutation: Callable[[Path], None],
    ) -> None:
        self._handle = handle
        self._path = path
        self._mutation = mutation
        self._mutated = False

    def read(self, size: int = -1) -> bytes:
        if not self._mutated:
            self._mutated = True
            self._mutation(self._path)
        return self._handle.read(size)

    def __getattr__(self, name: str):
        return getattr(self._handle, name)

    def __enter__(self) -> _MutatingHandle:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self._handle.close()
        return False


def _mutate_before_read(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    mutation: Callable[[Path], None],
) -> None:
    real_open = Path.open

    def mutating_open(self: Path, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == target and args and args[0] == "rb":
            return _MutatingHandle(handle, target, mutation)
        return handle

    monkeypatch.setattr(Path, "open", mutating_open)


def test_read_tail_text_bounded_when_file_grows_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    log = tmp_path / "agent.log"
    log.write_bytes(b"a" * 50 + b"\n" + b"a" * 49)

    def grow(path: Path) -> None:
        with path.open("ab") as extra:
            extra.write(b"b" * 5000)

    _mutate_before_read(monkeypatch, log, grow)

    result = _read_tail_text(log, 64)
    assert result == "a" * 49
    assert len(result.encode()) <= 64


def test_read_tail_text_empty_when_file_truncated_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    log = tmp_path / "agent.log"
    log.write_bytes(b"a" * 100)

    def truncate(path: Path) -> None:
        path.write_bytes(b"")

    _mutate_before_read(monkeypatch, log, truncate)

    assert _read_tail_text(log, 64) == ""


def test_read_tail_text_empty_file(tmp_path: Path):
    log = tmp_path / "agent.log"
    log.write_bytes(b"")
    assert _read_tail_text(log, 64) == ""


def test_read_tail_text_below_max_bytes_returns_whole_file(tmp_path: Path):
    log = tmp_path / "agent.log"
    log.write_bytes(b"hello tail")
    assert _read_tail_text(log, 64) == "hello tail"


def test_read_tail_text_at_max_bytes_returns_whole_file(tmp_path: Path):
    log = tmp_path / "agent.log"
    log.write_bytes(b"x" * 64)
    assert _read_tail_text(log, 64) == "x" * 64


def test_read_tail_text_above_max_bytes_drops_partial_first_line(tmp_path: Path):
    log = tmp_path / "agent.log"
    log.write_bytes(b"api_key=sk-" + b"x" * 40 + b"\nnext line\n")
    # The window starts mid-line: the cut line's label is gone, so its tail
    # (which may be a secret value) must not be shown.
    assert _read_tail_text(log, 20) == "next line\n"


def test_read_tail_text_window_without_newline_is_empty(tmp_path: Path):
    log = tmp_path / "agent.log"
    log.write_bytes(b"y" + b"x" * 64)
    assert _read_tail_text(log, 64) == ""


def test_read_tail_text_window_starting_on_line_boundary_keeps_first_line(tmp_path: Path):
    log = tmp_path / "agent.log"
    log.write_bytes(b"old\nfirst\nsecond\n")
    assert _read_tail_text(log, len(b"first\nsecond\n")) == "first\nsecond\n"


def test_read_tail_text_partial_utf8_at_cut_boundary_is_dropped(tmp_path: Path):
    log = tmp_path / "agent.log"
    # Cut lands on the second byte of the leading multi-byte character.
    log.write_bytes("é".encode() + b"a\n" + b"b" * 60)
    assert _read_tail_text(log, 63) == "b" * 60


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_text_readers_refuse_fifo_without_blocking(tmp_path: Path):
    fifo = tmp_path / "agent.log"
    os.mkfifo(fifo)
    results: list[object] = []

    def read_both() -> None:
        results.append(_read_text_capped(fifo))
        try:
            _read_tail_text(fifo, 64)
        except OSError:
            results.append("refused")

    reader = threading.Thread(target=read_both, daemon=True)
    reader.start()
    reader.join(timeout=5)
    if reader.is_alive():  # pragma: no cover - only on regression
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("reading a FIFO blocked")
    assert results == ["", "refused"]


def test_coerce_float_huge_int_is_zero():
    from hermesd.collect.common import _coerce_float, _optional_epoch

    assert _coerce_float(10**400) == 0.0
    assert _coerce_float(-(10**400)) == 0.0
    assert _optional_epoch(10**400) is None


def test_file_swapped_for_non_regular_after_type_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import stat

    from hermesd.collect import common

    path = tmp_path / "agent.log"
    path.write_text("line\n")
    real_fstat = os.fstat

    def fifo_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        return os.stat_result((stat.S_IFIFO | 0o644, *tuple(result)[1:]))

    monkeypatch.setattr(common.os, "fstat", fifo_fstat)

    assert _read_text_capped(path) == ""
    with pytest.raises(OSError, match="not a regular file"):
        _read_tail_text(path, 64)
