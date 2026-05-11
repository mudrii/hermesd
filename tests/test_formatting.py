"""Tests for shared formatting utilities."""

from hermesd.panels.formatting import fmt_iso_timestamp, fmt_tokens, fmt_usd


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


def test_fmt_usd_places_negative_sign_before_dollar():
    assert fmt_usd(-0.5) == "-$0.50"
    assert fmt_usd(0.5) == "$0.50"


def test_fmt_iso_timestamp_preserves_non_iso_text():
    assert fmt_iso_timestamp("2026-04-09T18:21:49+08:00") == "2026-04-09 18:21:49"
    assert fmt_iso_timestamp("next Thursday") == "next Thursday"
