from pathlib import Path

from hermesd.db import HermesDB


def test_read_sessions(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    sessions = db.read_sessions()
    assert len(sessions) == 2
    assert sessions[0]["id"] == "sess_001" or sessions[1]["id"] == "sess_001"
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
    assert isinstance(stats, list)
    db.close()


def test_read_token_totals(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    totals = db.read_token_totals()
    assert totals["input_tokens"] == 12400 + 9100
    assert totals["output_tokens"] == 8200 + 6300
    db.close()


def test_data_version_caching(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    s1 = db.read_sessions()
    s2 = db.read_sessions()
    assert s1 == s2
    assert db._cache_hits >= 1
    db.close()


def test_missing_db():
    db = HermesDB(Path("/nonexistent/state.db"))
    sessions = db.read_sessions()
    assert sessions == []
    db.close()


def test_read_only_mode(sample_db, hermes_home):
    db = HermesDB(hermes_home / "state.db")
    assert "mode=ro" in db._uri or db._readonly
    db.close()
