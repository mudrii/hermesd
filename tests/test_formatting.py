"""Tests for shared formatting utilities."""

from __future__ import annotations

import math

import pytest

from hermesd.panels.formatting import (
    IdentityMemo,
    fmt_age_seconds,
    fmt_bytes,
    fmt_iso_timestamp,
    fmt_tokens,
    fmt_usd,
    sanitize_terminal_text,
    sparkline,
)


def test_fmt_tokens_zero():
    assert fmt_tokens(0) == "0"


def test_fmt_tokens_small():
    assert fmt_tokens(500) == "500"
    assert fmt_tokens(999) == "999"


def test_fmt_tokens_thousands():
    assert fmt_tokens(1_000) == "1.0K"
    assert fmt_tokens(12_400) == "12.4K"
    assert fmt_tokens(999_949) == "999.9K"
    assert fmt_tokens(999_999) == "1.0M"


def test_fmt_tokens_millions():
    assert fmt_tokens(1_000_000) == "1.0M"
    assert fmt_tokens(2_500_000) == "2.5M"


def test_fmt_tokens_negative_values_use_magnitude_formatting():
    assert fmt_tokens(-5) == "-5"
    assert fmt_tokens(-999) == "-999"
    assert fmt_tokens(-1500) == "-1.5K"
    assert fmt_tokens(-999_999) == "-1.0M"
    assert fmt_tokens(-2_500_000) == "-2.5M"


def test_fmt_usd_places_negative_sign_before_dollar():
    assert fmt_usd(-0.5) == "-$0.50"
    assert fmt_usd(0.5) == "$0.50"


def test_fmt_iso_timestamp_preserves_non_iso_text():
    assert fmt_iso_timestamp("2026-04-09T18:21:49+08:00") == "2026-04-09 18:21:49"
    assert fmt_iso_timestamp("next Thursday") == "next Thursday"


def test_fmt_iso_timestamp_empty_value_renders_dash():
    assert fmt_iso_timestamp(None) == "—"
    assert fmt_iso_timestamp("") == "—"


def test_sanitize_strips_osc_hyperlink_payload():
    assert sanitize_terminal_text("pre\x1b]8;;http://evil\x07link\x1b]8;;\x07post") == "prelinkpost"


def test_sanitize_strips_osc52_clipboard_payload():
    assert sanitize_terminal_text("a\x1b]52;c;SGVsbG8=\x07b") == "ab"


def test_sanitize_strips_dcs_and_apc_payloads():
    assert sanitize_terminal_text("a\x1bPq#0;2;0;0\x1b\\b") == "ab"
    assert sanitize_terminal_text("a\x1b_Gsome\x07b") == "ab"


def test_sanitize_strips_unterminated_osc_at_end():
    assert sanitize_terminal_text("a\x1b]8;;http://evil") == "a"


def test_sanitize_strips_c1_csi_and_c0_controls():
    assert sanitize_terminal_text("a\x9b31mb") == "ab"
    assert sanitize_terminal_text("a\x00\x01\x07b") == "ab"


def test_sanitize_preserves_plain_text():
    assert (
        sanitize_terminal_text("調査 emoji 🎉 path\\x [brackets]")
        == "調査 emoji 🎉 path\\x [brackets]"
    )
    assert sanitize_terminal_text("\x1b[31mred\x1b[0m") == "red"


def test_identity_memo_reuses_value_for_same_objects_and_equal_strings():
    memo: IdentityMemo[list[int]] = IdentityMemo()
    items = [1, 2]
    calls: list[int] = []

    def compute() -> list[int]:
        calls.append(1)
        return sorted(items)

    first = memo.get((items, "".join(["cost"])), compute)
    second = memo.get((items, "cost"), compute)

    assert first is second
    assert len(calls) == 1


def test_identity_memo_recomputes_for_equal_but_distinct_objects():
    memo: IdentityMemo[int] = IdentityMemo()

    assert memo.get(([1],), lambda: 1) == 1
    assert memo.get(([1],), lambda: 2) == 2
    assert memo.get(([1], "a"), lambda: 3) == 3


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0, "0s"),
        (59.9, "59s"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86399, "23h"),
        (86400, "1d"),
        (3 * 86400 + 5, "3d"),
        (-5, "0s"),
        (None, "—"),
        (math.inf, "—"),
        (-math.inf, "—"),
        (math.nan, "—"),
    ],
)
def test_fmt_age_seconds_tiers_and_non_finite_guard(age: float | None, expected: str):
    assert fmt_age_seconds(age) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024 * 1024, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
        (2 * 1024**4, "2.0 TB"),
    ],
)
def test_fmt_bytes_uses_binary_units(size: int, expected: str):
    assert fmt_bytes(size) == expected


def test_fmt_tokens_saturates_counts_past_the_cap():
    """SQLite TEXT in a numeric column coerces to an arbitrary-precision int;
    dividing it into a float raises OverflowError, so absurd counts saturate."""
    assert fmt_tokens(10**400) == ">=999T"
    assert fmt_tokens(-(10**400)) == "<=-999T"


def test_fmt_tokens_below_the_cap_is_unchanged():
    assert fmt_tokens(999_000_000_000_000) == "999000000.0M"
    assert fmt_tokens(-999_000_000_000_000) == "-999000000.0M"


def test_fmt_bytes_saturates_absurd_sizes():
    assert fmt_bytes(10**400) == ">=1024 TB"
    assert fmt_bytes(1024**5) == ">=1024 TB"
    assert fmt_bytes(1000 * 1024**4) == "1000.0 TB"


def test_sparkline_scales_arbitrary_precision_counts():
    huge = 10**400
    assert sparkline([huge, 0]) == "█▁"
    assert sparkline([huge, huge]) == "██"
    # huge//2 / huge -> ceil(0.5 * 7) = 4 -> ▅; exact integer math, no float.
    assert sparkline([huge, huge // 2, 0]) == "█▅▁"


def test_sparkline_keeps_a_tiny_positive_share_above_the_zero_block():
    # ceil(1 / 10**20 * 7) == 1: a small positive value must not collapse to ▁.
    assert sparkline([1, 10**20]) == "▂█"
    assert sparkline([1, 10**400]) == "▂█"
