"""Log line parsing constants and log directory helpers."""

from __future__ import annotations

import re
from pathlib import Path

# Every quantifier is bounded and the groups cannot overlap: the previous
# `\s*-\s*([^-]+?)\s*-\s*` form backtracked cubically on a line made of spaces.
_LOG_LINE_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s{1,4}\d{2}:\d{2}:\d{2})(?:[.,]\d{1,9})?"
    r"\s{0,8}-\s{0,8}([^-\s][^-]{0,127}?)\s{0,8}-\s{0,8}(\w{1,32})\s{0,8}-\s{0,8}(.*)"
)


# Log lines are truncated to this width before parsing/redaction.
_MAX_LOG_LINE_CHARS = 4096


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
