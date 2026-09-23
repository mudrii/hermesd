"""Free-text error and goal excerpts are redacted before they are capped."""

from __future__ import annotations

import json

from hermesd.collect.common import _excerpt
from hermesd.collect.gateway import _delivery_summary, _platform_status, _RecordWriter
from hermesd.collect.operations import _delegation_from_row

_SECRET = "sk-live-SUPERSECRETTOKEN0123456789"


def test_excerpt_collapses_whitespace_redacts_then_caps():
    # key=value secrets end at whitespace, so the filler survives and the cap applies.
    text = "auth failed:\n  api_key=" + _SECRET + " " + "x" * 200

    excerpt = _excerpt(text, 80)

    assert "SUPERSECRET" not in excerpt
    assert "[REDACTED]" in excerpt
    assert "\n" not in excerpt
    assert len(excerpt) == 80


def test_excerpt_bounds_the_redaction_scan():
    assert _excerpt("y" * 1_000_000, 10) == "y" * 10
    assert _excerpt(None, 10) == "None"


def test_delivery_last_error_is_redacted():
    summary = _delivery_summary(
        {"platform": "telegram", "last_error": f"POST https://u:{_SECRET}@api.example/x failed"},
        now=0.0,
    )

    assert "SUPERSECRET" not in summary.last_error
    assert "api.example" in summary.last_error


def test_platform_error_message_is_redacted():
    status = _platform_status(
        "telegram",
        {"state": "fatal", "error_message": f"invalid token: Bearer {_SECRET}"},
        0.0,
        _RecordWriter(),
        record_current=True,
    )

    assert "SUPERSECRET" not in status.error_message
    assert status.error_message.startswith("invalid token")


def test_delegation_goal_and_error_excerpt_are_redacted():
    row = {
        "delegation_id": "d1",
        "task_json": json.dumps({"goal": f"call api with Bearer {_SECRET}"}),
        "result_json": json.dumps({"results": [{"status": "error", "error": f"token={_SECRET}"}]}),
    }

    info = _delegation_from_row(row, 0.0, lambda _pid: False)

    assert "SUPERSECRET" not in info.goal
    assert "SUPERSECRET" not in info.error_excerpt
    assert info.goal.startswith("call api with Bearer")
