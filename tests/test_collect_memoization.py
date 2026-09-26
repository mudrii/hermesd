"""Per-refresh work that is pure in its inputs is memoized without changing results."""

from __future__ import annotations

import os
from pathlib import Path

from hermesd.collect import common, cron, redaction

_SECRET = "sk-live-MEMOSECRET0123456789abcdef"


def test_redaction_memo_matches_uncached_and_hits_on_repeat():
    text = f"POST https://u:{_SECRET}@api.example/x failed; api_key={_SECRET} Bearer {_SECRET}"
    expected = redaction._redact_secret_text_uncached(text)

    before = redaction._redact_secret_text_memo.cache_info().hits
    first = redaction._redact_secret_text(text)
    second = redaction._redact_secret_text(text)

    assert first == second == expected
    assert _SECRET not in first
    assert redaction._redact_secret_text_memo.cache_info().hits >= before + 1


def test_redaction_memo_skips_oversized_text():
    text = "x" * (redaction._REDACTION_MEMO_MAX_CHARS + 1) + f" token={_SECRET}"
    size = redaction._redact_secret_text_memo.cache_info().currsize

    out = redaction._redact_secret_text(text)

    assert _SECRET not in out
    assert redaction._redact_secret_text_memo.cache_info().currsize == size


def test_cron_epoch_udf_memo_matches_the_parser():
    for value in ("2026-09-23T10:00:00Z", "2026-09-23 10:00:00.5+02:00", "junk", None, 5):
        assert cron._memo_iso_to_epoch(value) == common._iso_to_epoch(value)


def test_path_confinement_notices_a_retargeted_home_symlink(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for target in (first, second):
        (target / "logs").mkdir(parents=True)
    home = tmp_path / "home"
    home.symlink_to(first)

    assert common._path_resolves_under(first / "logs", home)

    home.unlink()
    os.symlink(second, home)

    assert common._path_resolves_under(second / "logs", home)
    assert not common._path_resolves_under(first / "logs", home)
