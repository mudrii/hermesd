from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import datetime
from typing import Generic, TypeVar

from rich.markup import escape
from rich.text import Text

from hermesd.theme import Theme

# Strips terminal escape sequences AND their payloads, plus raw control codes:
# OSC/DCS/SOS/PM/APC (payload terminated by BEL or ST, or unterminated at end
# of the single-line inputs we sanitize), CSI (ESC [ and C1), any remaining
# ESC + one byte, and leftover C0 (except \t \n) / C1 control characters.
_ANSI_STRIP_PATTERN = re.compile(
    r"\x1b[\]P_X^][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC/DCS/SOS/PM/APC, terminated
    r"|\x1b[\]P_X^][^\x07\x1b]*$"  # same, unterminated at end of line
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI sequences
    r"|\x9b[0-?]*[ -/]*[@-~]"  # C1 CSI
    r"|\x1b."  # any remaining ESC + one byte (charset selects, stray ST, ...)
    r"|[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]"  # leftover C0 (keep \t \n) and C1 controls
)

T = TypeVar("T")


class IdentityMemo(Generic[T]):
    """Single-entry memo for per-frame panel work keyed on input identity.

    The render loop redraws at 2 Hz while the collector replaces state objects
    only once per collect, so a key element matches when it is the same object
    (strings: an equal value). The entry holds strong references, so ids cannot
    be recycled into a false hit, and a fresh collect always misses.
    """

    def __init__(self) -> None:
        self._entry: tuple[tuple[object, ...], T] | None = None

    def get(self, key: tuple[object, ...], compute: Callable[[], T]) -> T:
        entry = self._entry
        if entry is not None and _same_key(entry[0], key):
            return entry[1]
        value = compute()
        self._entry = (key, value)
        return value


def _same_key(cached: tuple[object, ...], key: tuple[object, ...]) -> bool:
    return len(cached) == len(key) and all(
        old is new or (isinstance(old, str) and old == new)
        for old, new in zip(cached, key, strict=True)
    )


# Saturating cap for display-only counts. Token columns are untyped SQLite
# values: a TEXT payload coerces to an arbitrary-precision int, and dividing
# one into a float raises OverflowError instead of producing inf. Anything at
# or past the cap renders as a bounded sentinel rather than a fake precise
# figure; the cap matches the 10**15 bound _bounded_token_count applies to
# cost estimation.
_TOKEN_COUNT_CAP = 10**15

# Same guard for byte sizes (binary units): 1 PiB is past any real artifact.
_BYTES_CAP = 1024**5


def fmt_tokens(n: int) -> str:
    magnitude = abs(n)
    if magnitude >= _TOKEN_COUNT_CAP:
        return ">=999T" if n >= 0 else "<=-999T"
    if magnitude >= 999_950:
        label = f"{magnitude / 1_000_000:.1f}M"
    elif magnitude >= 1_000:
        label = f"{magnitude / 1_000:.1f}K"
    else:
        return str(n)
    return f"-{label}" if n < 0 else label


def sanitize_terminal_text(value: str) -> str:
    """Strip ANSI escape sequences (including payloads) and control codes.

    ``Text.append`` skips markup parsing but passes raw control bytes through
    to the terminal (e.g. ``\\x1b[2J`` clears the Live display), and
    ``Text.from_ansi`` consumes only the introducer, leaving OSC/DCS/APC
    payloads (``8;;http://...``) as visible garbage. Log lines and cron output
    excerpts are untrusted, so panels sanitize before appending.
    """
    if not value:
        return value
    return _ANSI_STRIP_PATTERN.sub("", value)


def escape_terminal_text(value: str) -> str:
    """Strip terminal controls, then escape Rich markup in a string cell."""
    return escape(sanitize_terminal_text(value))


def fmt_age_seconds(age: float | None) -> str:
    """Compact s/m/h/d label for an age in seconds; "—" when unknown or not finite.

    Negative ages (clock skew) clamp to "0s".
    """
    if age is None or not math.isfinite(age):
        return "—"
    seconds = max(0, int(age))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def fmt_bytes(size_bytes: int) -> str:
    """Binary-unit size label: B, then KB/MB/GB/TB with one decimal."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes >= _BYTES_CAP:
        return ">=1024 TB"
    value = float(size_bytes)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value / 1024:.1f} TB"


def section_heading(label: str, theme: Theme, *, leading_blank: bool = True) -> Text:
    """Bold sub-section heading inside a detail panel."""
    prefix = "\n" if leading_blank else ""
    return Text(f"{prefix}{label}\n", style=f"bold {theme.ui_label}")


def fmt_usd(value: float) -> str:
    if value < 0:
        return f"-${abs(value):.2f}"
    return f"${value:.2f}"


def fmt_iso_timestamp(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[int]) -> str:
    """One block per value, scaled to the largest; zero is always the lowest block."""
    peak = max(values, default=0)
    if peak <= 0:
        return _SPARK_BLOCKS[0] * len(values)
    top = len(_SPARK_BLOCKS) - 1
    # Exact integer ceil: ceil(v*top/peak) == (v*top + peak - 1)//peak. SQLite
    # keeps arbitrary-precision counts as TEXT, and pure integer math neither
    # overflows through float nor rounds a tiny positive share down to the
    # zero block, however wide the counts get.
    return "".join(_SPARK_BLOCKS[min(top, (max(v, 0) * top + peak - 1) // peak)] for v in values)
