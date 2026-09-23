"""Log line parsing constants, log directory helpers and incremental log
health scanning."""

from __future__ import annotations

import os
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hermesd.collect.common import _open_regular_file, _path_resolves_under
from hermesd.collect.redaction import _redact_secret_text
from hermesd.models import LogHealthCounter, LogSignatureCount, LogStreamHealth

# Every quantifier is bounded and the groups cannot overlap: the previous
# `\s*-\s*([^-]+?)\s*-\s*` form backtracked cubically on a line made of spaces.
_LOG_LINE_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s{1,4}\d{2}:\d{2}:\d{2})(?:[.,]\d{1,9})?"
    r"\s{0,8}-\s{0,8}([^-\s][^-]{0,127}?)\s{0,8}-\s{0,8}(\w{1,32})\s{0,8}-\s{0,8}(.*)"
)


# Log lines are truncated to this width before parsing/redaction.
_MAX_LOG_LINE_CHARS = 4096
# Lines kept per log stream. Errors get a shorter tail: the panel shows them
# alongside every other stream and a long error burst would crowd it out.
_LOG_TAIL_LINES = 20
_ERROR_LOG_TAIL_LINES = 10


def _latest_log_mtime(logs_dir: Path) -> float | None:
    if not logs_dir.is_dir():
        return None
    mtimes = []
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    return max(mtimes)


def _extract_session_id(message: str) -> str:
    match = re.search(r"(?:session(?:_id)?|sid)[=: ]([A-Za-z0-9_-]+)", message, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


# ── Incremental log health scanning (source ``log_health``) ─────────────────

# First sight of a log scans at most this much of its tail (the rest of its
# history is never read), a chunk of at most _LOG_HEALTH_MAX_BYTES_PER_REFRESH
# per refresh; after that only appended bytes are read, under the same cap.
_LOG_HEALTH_BACKFILL_BYTES = 2 * 1024 * 1024
_LOG_HEALTH_MAX_BYTES_PER_REFRESH = 256 * 1024
# A partial trailing line longer than this is dropped instead of carried.
_LOG_HEALTH_MAX_CARRY_BYTES = 64 * 1024
_LOG_HEALTH_WINDOW_SECONDS = 24 * 3600.0
_LOG_HEALTH_HOUR_SECONDS = 3600.0
_LOG_HEALTH_MAX_SIGNATURES = 256
_LOG_HEALTH_TOP = 5
_LOG_HEALTH_SIGNATURE_CHARS = 120

# ``===== [YYYY-MM-DD HH:MM:SS] starting MCP server '<name>' =====`` — the
# per-spawn session marker (``tools/mcp_tool_config.py:62-67``).
_MCP_BANNER = re.compile(
    r"^===== \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] starting MCP server '(.{1,200})' =====$"
)
# argparse errors of the MCP stdio supervisor — the retired
# ``mcp_stdio_watchdog.py`` (``tools/mcp_death_supervisor.py:17``) rejecting
# ``--create-time``, or its successor ``mcp_death_supervisor.py`` (``:177-187``).
_MCP_SUPERVISOR_ERROR = re.compile(
    r"^(?:mcp_stdio_watchdog|mcp_death_supervisor)\.py: error: (.{0,200})$"
)
# ``logging``'s asctime shape, which ``hermes_cli/stderr_timestamp.py:19-30``
# prefixes onto every gateway stderr line it does not already find stamped.
_ASCTIME_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}(?: |$)")
_LOGGER_ERROR = re.compile(r"^(?:ERROR|CRITICAL) ([\w.\-]{1,128}): (.{1,2000})$")
_EXCEPTION_LINE = re.compile(
    r"^([A-Za-z_][\w.]{0,127}(?:Error|Exception|Exit|Interrupt)): (.{0,2000})$"
)
# pnpm's lifecycle failure line, both of the shapes it prints.
_WORKSPACE_CRASH = re.compile(r"ELIFECYCLE\]?\s{1,4}Command failed")
_SIGNATURE_NUMBER = re.compile(r"\b(?:0x[0-9a-fA-F]+|[0-9a-fA-F]{8,}|\d+(?:\.\d+)?)\b")


def _local_stamp_epoch(stamp: str) -> float | None:
    """Epoch of a naive local ``YYYY-MM-DD HH:MM:SS`` stamp, None when invalid."""
    try:
        parsed = time.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    try:
        return time.mktime(parsed)
    except (OverflowError, ValueError):
        return None


def _error_signature(text: str) -> str:
    """A stable, redacted signature: numbers and hex ids collapsed to ``N``."""
    collapsed = _SIGNATURE_NUMBER.sub("N", " ".join(text.split()))
    return _redact_secret_text(collapsed)[:_LOG_HEALTH_SIGNATURE_CHARS]


@dataclass(frozen=True, slots=True)
class LogHealthSpec:
    """What one scanned stream counts: its counters and the line parser."""

    stream: str
    counters: tuple[tuple[str, str], ...]
    parse: Callable[[IncrementalLogScanner, str], None]
    top_label: str = ""


class IncrementalLogScanner:
    """Counts health events in one append-only log, reading each byte once.

    Remembers the file's inode and the byte offset scanned so far. Each refresh
    reads at most ``_LOG_HEALTH_MAX_BYTES_PER_REFRESH`` of new bytes; a file
    that shrank or was replaced is rescanned from its start. The first sight of
    a log starts ``_LOG_HEALTH_BACKFILL_BYTES`` before its end: those
    historical lines are dated from their own timestamps when they have one and
    counted as undated otherwise, while lines appended after that first sight
    fall back to the refresh that read them. Events are kept for 24 hours.
    """

    def __init__(self, spec: LogHealthSpec) -> None:
        self.spec = spec
        self._inode: int | None = None
        self._offset = 0
        self._live_from = 0
        self._carry = b""
        self._scanned = 0
        self._stamp_text = ""
        self._stamp_epoch: float | None = None
        self._now = 0.0
        self._live = False
        self._skip_partial = False
        self._events: dict[str, deque[float]] = {key: deque() for key, _ in spec.counters}
        self._undated: dict[str, int] = dict.fromkeys(self._events, 0)
        self._signatures: dict[str, deque[float]] = {}
        self._signature_undated: dict[str, int] = {}

    # -- parsing helpers used by the per-stream parsers ---------------------

    def note_stamp(self, stamp: str) -> None:
        """The newest timestamp seen above the current line."""
        if stamp != self._stamp_text:
            self._stamp_text = stamp
            self._stamp_epoch = _local_stamp_epoch(stamp)

    def record(self, key: str, signature: str = "") -> None:
        when = self._stamp_epoch
        if when is None and self._live:
            when = self._now
        if when is None:
            self._undated[key] += 1
            if signature:
                self._remember_signature(signature)
                self._signature_undated[signature] = self._signature_undated.get(signature, 0) + 1
            return
        self._events[key].append(when)
        if signature:
            self._remember_signature(signature).append(when)

    def _remember_signature(self, signature: str) -> deque[float]:
        events = self._signatures.get(signature)
        if events is None:
            if len(self._signatures) >= _LOG_HEALTH_MAX_SIGNATURES:
                stalest = min(
                    self._signatures,
                    key=lambda sig: self._signatures[sig][-1] if self._signatures[sig] else 0.0,
                )
                del self._signatures[stalest]
                self._signature_undated.pop(stalest, None)
            events = self._signatures[signature] = deque()
        return events

    # -- scanning -----------------------------------------------------------

    def scan(self, path: Path, home: Path, now: float) -> LogStreamHealth | None:
        """Read what was appended since the last refresh; None when absent/unsafe."""
        if path.is_symlink() or not _path_resolves_under(path, home):
            return None
        try:
            handle = _open_regular_file(path)
        except FileNotFoundError:
            return None
        with handle:
            stat = os.fstat(handle.fileno())
            size = stat.st_size
            if self._inode is None:
                # First sight: only the bounded tail is history; everything
                # past today's end is appended while hermesd watches.
                self._reset(stat.st_ino, max(0, size - _LOG_HEALTH_BACKFILL_BYTES), live_from=size)
            elif stat.st_ino != self._inode or size < self._offset:
                # Rotated or truncated: the new content is all new since the
                # last refresh, so it is scanned from its (bounded) start as live.
                self._reset(stat.st_ino, max(0, size - _LOG_HEALTH_BACKFILL_BYTES), live_from=0)
            self._now = now
            end = min(size, self._offset + _LOG_HEALTH_MAX_BYTES_PER_REFRESH)
            if self._offset < self._live_from:
                end = min(end, self._live_from)
            if end > self._offset:
                handle.seek(self._offset)
                chunk = handle.read(end - self._offset)
                self._live = self._offset >= self._live_from
                self._consume(chunk)
                self._offset += len(chunk)
                self._scanned += len(chunk)
        self._prune(now)
        return self._snapshot(path, size, now)

    def _reset(self, inode: int, offset: int, *, live_from: int) -> None:
        self._inode = inode
        self._offset = offset
        self._live_from = live_from
        self._carry = b""
        # A scan that opens mid-file starts mid-line: that fragment is dropped.
        self._skip_partial = offset > 0
        self._stamp_text = ""
        self._stamp_epoch = None

    def _consume(self, chunk: bytes) -> None:
        data = self._carry + chunk
        cut = data.rfind(b"\n")
        if cut < 0:
            self._carry = data if len(data) <= _LOG_HEALTH_MAX_CARRY_BYTES else b""
            return
        self._carry = data[cut + 1 :]
        if len(self._carry) > _LOG_HEALTH_MAX_CARRY_BYTES:
            self._carry = b""
        lines = data[:cut].decode("utf-8", errors="replace").split("\n")
        if self._skip_partial:
            lines = lines[1:]
            self._skip_partial = False
        parse = self.spec.parse
        for line in lines:
            if line:
                parse(self, line[:_MAX_LOG_LINE_CHARS].rstrip("\r"))

    def _prune(self, now: float) -> None:
        cutoff = now - _LOG_HEALTH_WINDOW_SECONDS
        for events in (*self._events.values(), *self._signatures.values()):
            while events and events[0] < cutoff:
                events.popleft()

    def _snapshot(self, path: Path, size: int, now: float) -> LogStreamHealth:
        hour = now - _LOG_HEALTH_HOUR_SECONDS
        counters = [
            LogHealthCounter(
                key=key,
                label=label,
                last_1h=sum(1 for when in self._events[key] if when >= hour),
                last_24h=len(self._events[key]),
                undated=self._undated[key],
                last_seen_age_seconds=(
                    max(0.0, now - max(self._events[key])) if self._events[key] else None
                ),
            )
            for key, label in self.spec.counters
        ]
        top = sorted(
            (
                LogSignatureCount(
                    signature=signature,
                    last_24h=len(events),
                    undated=self._signature_undated.get(signature, 0),
                    last_seen_age_seconds=max(0.0, now - max(events)) if events else None,
                )
                for signature, events in self._signatures.items()
                if events or self._signature_undated.get(signature)
            ),
            key=lambda entry: (-entry.last_24h, -entry.undated, entry.signature),
        )[:_LOG_HEALTH_TOP]
        dated = [when for events in self._events.values() for when in events]
        return LogStreamHealth(
            stream=self.spec.stream,
            path=path.name,
            size_bytes=size,
            scanned_bytes=self._scanned,
            backlog_bytes=max(0, size - self._offset),
            oldest_event_age_seconds=max(0.0, now - min(dated)) if dated else None,
            counters=counters,
            top=top,
        )


def _parse_mcp_stderr(scanner: IncrementalLogScanner, line: str) -> None:
    if line.startswith("====="):
        match = _MCP_BANNER.match(line)
        if match:
            scanner.note_stamp(match.group(1))
            scanner.record("mcp_server_starts", match.group(2))
        return
    if "_" in line and ".py: error:" in line and _MCP_SUPERVISOR_ERROR.match(line):
        scanner.record("mcp_supervisor_arg_errors")


def _parse_gateway_error(scanner: IncrementalLogScanner, line: str) -> None:
    content = line
    if line[:1].isdigit():
        match = _ASCTIME_PREFIX.match(line)
        if match:
            scanner.note_stamp(match.group(1))
            content = line[match.end() :]
    if content.startswith(("ERROR ", "CRITICAL ")):
        logged = _LOGGER_ERROR.match(content)
        if logged:
            scanner.record(
                "gateway_errors", _error_signature(f"{logged.group(1)}: {logged.group(2)}")
            )
        return
    if ": " in content and ("Error" in content or "Exception" in content):
        raised = _EXCEPTION_LINE.match(content)
        if raised:
            scanner.record(
                "gateway_errors", _error_signature(f"{raised.group(1)}: {raised.group(2)}")
            )


def _parse_workspace(scanner: IncrementalLogScanner, line: str) -> None:
    if "ELIFECYCLE" in line and _WORKSPACE_CRASH.search(line):
        scanner.record("workspace_crashes")


# Stream name (as the Logs panel names it) -> what its scanner counts.
LOG_HEALTH_SPECS: tuple[tuple[str, LogHealthSpec], ...] = (
    (
        "mcp-stderr.log",
        LogHealthSpec(
            stream="mcp.stderr",
            counters=(
                ("mcp_server_starts", "MCP server starts"),
                ("mcp_supervisor_arg_errors", "supervisor arg errors"),
            ),
            parse=_parse_mcp_stderr,
            top_label="starts per server",
        ),
    ),
    (
        "gateway.error.log",
        LogHealthSpec(
            stream="gateway.error",
            counters=(("gateway_errors", "gateway errors"),),
            parse=_parse_gateway_error,
            top_label="top error signatures",
        ),
    ),
    (
        "workspace.log",
        LogHealthSpec(
            stream="workspace",
            counters=(("workspace_crashes", "ELIFECYCLE crashes"),),
            parse=_parse_workspace,
        ),
    ),
)
