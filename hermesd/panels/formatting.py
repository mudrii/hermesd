from __future__ import annotations

import re
from datetime import datetime

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


def fmt_tokens(n: int) -> str:
    magnitude = abs(n)
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


def fmt_age_seconds(age: int) -> str:
    """Render a non-negative age in seconds as a compact s/m/h label."""
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    return f"{age // 3600}h"


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
