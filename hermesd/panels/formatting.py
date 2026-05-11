from __future__ import annotations

from datetime import datetime


def fmt_tokens(n: int) -> str:
    if n >= 999_950:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


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
