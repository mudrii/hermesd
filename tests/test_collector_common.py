"""Shared collect helpers: tail reads bounded by max_bytes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import pytest

from hermesd.collect.common import _read_tail_text


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
    log.write_bytes(b"a" * 100)

    def grow(path: Path) -> None:
        with path.open("ab") as extra:
            extra.write(b"b" * 5000)

    _mutate_before_read(monkeypatch, log, grow)

    result = _read_tail_text(log, 64)
    assert result == "a" * 64
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


def test_read_tail_text_above_max_bytes_returns_tail(tmp_path: Path):
    log = tmp_path / "agent.log"
    log.write_bytes(b"y" + b"x" * 64)
    assert _read_tail_text(log, 64) == "x" * 64


def test_read_tail_text_partial_utf8_at_cut_boundary_is_replaced(tmp_path: Path):
    log = tmp_path / "agent.log"
    # Cut lands on the second byte of the leading multi-byte character.
    log.write_bytes("é".encode() + b"a" * 62)
    assert _read_tail_text(log, 63) == "\ufffd" + "a" * 62
