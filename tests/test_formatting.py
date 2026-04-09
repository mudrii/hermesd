"""Tests for shared formatting utilities."""
from hermesd.panels.formatting import fmt_tokens


def test_fmt_tokens_zero():
    assert fmt_tokens(0) == "0"


def test_fmt_tokens_small():
    assert fmt_tokens(500) == "500"
    assert fmt_tokens(999) == "999"


def test_fmt_tokens_thousands():
    assert fmt_tokens(1_000) == "1.0K"
    assert fmt_tokens(12_400) == "12.4K"
    assert fmt_tokens(999_999) == "1000.0K"


def test_fmt_tokens_millions():
    assert fmt_tokens(1_000_000) == "1.0M"
    assert fmt_tokens(2_500_000) == "2.5M"
