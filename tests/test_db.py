from __future__ import annotations

from pathlib import Path

from hermesd.db import HermesDB


def test_read_sessions(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    assert len(sessions) == 2
    assert {s["id"] for s in sessions} == {"sess_001", "sess_002"}
    db.close()


def test_read_sessions_returns_dicts(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    s = sessions[0]
    assert "id" in s
    assert "source" in s
    assert "message_count" in s
    assert "input_tokens" in s
    db.close()


def test_read_tool_stats(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    stats = db.read_tool_stats()
    assert len(stats) == 1
    assert stats[0]["tool_name"] == "shell_exec"
    assert stats[0]["call_count"] == 3
    db.close()


def test_missing_db():
    db = HermesDB(Path("/nonexistent/state.db"))
    sessions = db.read_sessions()
    assert sessions == []
    db.close()


def test_read_only_mode(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    assert "mode=ro&immutable=1" in db._uri
    db.close()
