"""Tests for session active/ended detection."""

import sqlite3
import time
from pathlib import Path

from hermesd.collector import Collector


def _make_db(hermes_home: Path) -> None:
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT,
            model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
            started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
            message_count INTEGER, tool_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, billing_provider TEXT,
            billing_base_url TEXT, billing_mode TEXT,
            estimated_cost_usd REAL, actual_cost_usd REAL,
            cost_status TEXT, cost_source TEXT, pricing_version TEXT,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL,
            token_count INTEGER, finish_reason TEXT, reasoning TEXT,
            reasoning_details TEXT, codex_reasoning_items TEXT
        );
    """)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, NULL)",
        ("active_cli", "cli", now - 100),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, ?)",
        ("ended_cli", "cli", now - 3600, now - 1800),
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, NULL)",
        ("active_telegram", "telegram", now - 50),
    )
    conn.commit()
    conn.close()


def test_active_sessions_detected(hermes_home: Path):
    _make_db(hermes_home)
    c = Collector(hermes_home)
    state = c.collect()
    by_id = {s.session_id: s for s in state.sessions}
    assert by_id["active_cli"].is_active is True
    assert by_id["active_telegram"].is_active is True
    assert by_id["ended_cli"].is_active is False
    c.close()


def test_active_count_in_compact_panel(hermes_home: Path):
    _make_db(hermes_home)
    from rich.console import Console

    from hermesd.collector import Collector
    from hermesd.panels import render_panel
    from hermesd.theme import Theme

    c = Collector(hermes_home)
    state = c.collect()
    panel = render_panel(2, state, Theme(), detail=False)
    console = Console(width=80, force_terminal=True)
    with console.capture() as cap:
        console.print(panel)
    text = cap.get()
    assert "2 active" in text
    assert "3 total" in text
    c.close()


def test_null_columns_in_session(hermes_home: Path):
    """All nullable columns as NULL must not crash."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT,
            model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
            started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
            message_count INTEGER, tool_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, billing_provider TEXT,
            billing_base_url TEXT, billing_mode TEXT,
            estimated_cost_usd REAL, actual_cost_usd REAL,
            cost_status TEXT, cost_source TEXT, pricing_version TEXT,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL,
            token_count INTEGER, finish_reason TEXT, reasoning TEXT,
            reasoning_details TEXT, codex_reasoning_items TEXT
        );
    """)
    conn.execute(
        "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
        ("null_sess", time.time()),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    s = state.sessions[0]
    assert s.session_id == "null_sess"
    assert s.source == ""
    assert s.model == ""
    assert s.message_count == 0
    assert s.input_tokens == 0
    assert s.estimated_cost_usd == 0.0
    assert s.billing_provider == ""
    assert s.cost_status == ""
    assert s.pricing_version == ""
    assert s.is_active is True  # ended_at is NULL
    c.close()


def test_session_schema_fields_mapped(hermes_home: Path):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT,
            model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
            started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
            message_count INTEGER, tool_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, billing_provider TEXT,
            billing_base_url TEXT, billing_mode TEXT,
            estimated_cost_usd REAL, actual_cost_usd REAL,
            cost_status TEXT, cost_source TEXT, pricing_version TEXT,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL,
            token_count INTEGER, finish_reason TEXT, reasoning TEXT,
            reasoning_details TEXT, codex_reasoning_items TEXT
        );
    """)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, billing_provider, cost_status, pricing_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("schema_sess", "cli", time.time(), "openai-codex", "reported", "2026-04"),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    session = state.sessions[0]
    assert session.billing_provider == "openai-codex"
    assert session.cost_status == "reported"
    assert session.pricing_version == "2026-04"
    c.close()


def test_session_parent_session_id_mapped(hermes_home: Path):
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT,
            model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
            started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
            message_count INTEGER, tool_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cache_read_tokens INTEGER, cache_write_tokens INTEGER,
            reasoning_tokens INTEGER, billing_provider TEXT,
            billing_base_url TEXT, billing_mode TEXT,
            estimated_cost_usd REAL, actual_cost_usd REAL,
            cost_status TEXT, cost_source TEXT, pricing_version TEXT,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT,
            tool_calls TEXT, tool_name TEXT, timestamp REAL,
            token_count INTEGER, finish_reason TEXT, reasoning TEXT,
            reasoning_details TEXT, codex_reasoning_items TEXT
        );
    """)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, parent_session_id) VALUES (?, ?, ?, ?)",
        ("child_sess", "cli", time.time(), "parent_sess"),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    state = c.collect()
    assert state.sessions[0].parent_session_id == "parent_sess"
    c.close()
