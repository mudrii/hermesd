"""Tests for shared formatting utilities."""

from __future__ import annotations

from hermesd.panels.formatting import fmt_iso_timestamp, fmt_tokens, fmt_usd, sanitize_terminal_text


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
