"""NULL tolerance for SQLite reads, plus a static guard on how row columns are read.

``~/.hermes/state.db`` is written by hermes-agent, so every nullable column can
arrive as NULL. ``row.get("col", default)`` does not help there: the key exists
and its value is None, so the default never applies. The project rule is to use
``row.get("col") or <default>`` instead, and the first test enforces it.
"""

from __future__ import annotations

import ast
import sqlite3
import time
from pathlib import Path

import pytest

import hermesd
from hermesd.collector import Collector
from hermesd.db import HermesDB
from tests.conftest import create_state_db_tables, insert_model_usage

# A line carrying this marker is exempt from the row-get guard.
_ALLOW_MARKER = "# row-get-ok"
_ROW_PARAM_NAMES = frozenset({"conn", "row", "rows"})


def _reads_sqlite_rows(node: ast.FunctionDef) -> bool:
    """True when a function looks like a SQLite row reader."""
    if node.name.startswith("_read_"):
        return True
    arguments = node.args
    names = {
        argument.arg
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    }
    return bool(names & _ROW_PARAM_NAMES)


def find_row_get_default_violations(path: Path) -> list[str]:
    """Report ``<row>.get("col", default)`` calls inside SQLite row readers."""
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    violations: list[str] = []
    for function in ast.walk(ast.parse(source, filename=str(path))):
        if not isinstance(function, ast.FunctionDef) or not _reads_sqlite_rows(function):
            continue
        for call in ast.walk(function):
            if (
                not isinstance(call, ast.Call)
                or not isinstance(call.func, ast.Attribute)
                or call.func.attr != "get"
                or len(call.args) != 2
                or not isinstance(call.args[0], ast.Constant)
                or not isinstance(call.args[0].value, str)
            ):
                continue
            if _ALLOW_MARKER in source_lines[call.lineno - 1]:
                continue
            violations.append(
                f"{path.name}:{call.lineno}: {function.name} uses "
                f'.get("{call.args[0].value}", ...) on a SQLite row'
            )
    return violations


def _row_reading_modules() -> list[Path]:
    package_root = Path(hermesd.__file__).parent
    return [
        package_root / "db.py",
        package_root / "collector.py",
        *sorted(path for path in (package_root / "collect").glob("*.py")),
    ]


@pytest.mark.parametrize("module_path", _row_reading_modules(), ids=lambda path: path.name)
def test_row_readers_never_use_get_with_a_default(module_path: Path) -> None:
    assert find_row_get_default_violations(module_path) == []


def test_row_get_guard_detects_a_violation(tmp_path: Path) -> None:
    """The guard is only worth having if it actually fails on the pattern."""
    module = tmp_path / "offender.py"
    module.write_text(
        "def _read_rows(conn):\n"
        '    return [row.get("hidden", 0) for row in conn.execute("SELECT 1")]\n'
        "\n"
        "def _read_allowed(conn):\n"
        '    return conn.get("hidden", 0)  # row-get-ok\n'
        "\n"
        "def unrelated(payload):\n"
        '    return payload.get("hidden", 0)\n'
    )

    violations = find_row_get_default_violations(module)

    assert len(violations) == 1
    assert "offender.py:2" in violations[0]
    assert "_read_rows" in violations[0]


def _create_modern_state_db(db_path: Path) -> None:
    """A state.db carrying the hermes-agent 0.21 columns, all of them nullable."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            started_at REAL,
            ended_at REAL,
            last_activity_at REAL,
            hidden INTEGER,
            message_count INTEGER,
            tool_call_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT,
            content TEXT,
            tool_name TEXT,
            timestamp REAL,
            active INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, last_activity_at, hidden, "
        "message_count, tool_call_count, estimated_cost_usd, actual_cost_usd, cost_status) "
        "VALUES ('sess_null', 'cli', 'gpt-5.4', ?, NULL, NULL, 1, 1, NULL, NULL, NULL)",
        (time.time() - 60,),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active) "
        "VALUES ('sess_null', 'assistant', 'needle in the transcript', 'web_search', ?, NULL)",
        (time.time() - 60,),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def null_column_db(hermes_home: Path) -> Path:
    db_path = hermes_home / "state.db"
    _create_modern_state_db(db_path)
    return db_path


def test_null_hidden_session_is_treated_as_visible(null_column_db: Path) -> None:
    db = HermesDB(null_column_db)
    try:
        rows = db.read_sessions()
        assert [row["id"] for row in rows] == ["sess_null"]
        # A NULL hidden flag means "not soft-deleted", so it must also be counted.
        assert db.read_session_count() == 1
        # last_activity_at is both an ordering key and a displayed column.
        assert rows[0]["last_activity_at"] is None
    finally:
        db.close()


def test_null_last_activity_at_orders_by_started_at(
    hermes_home: Path, null_column_db: Path
) -> None:
    conn = sqlite3.connect(str(null_column_db))
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, last_activity_at, hidden) "
        "VALUES ('sess_newer', 'cli', ?, NULL, NULL)",
        (time.time(),),
    )
    conn.commit()
    conn.close()

    db = HermesDB(null_column_db)
    try:
        # COALESCE(last_activity_at, started_at) must not sort NULL rows away.
        assert [row["id"] for row in db.read_sessions()] == ["sess_newer", "sess_null"]
    finally:
        db.close()


def test_null_active_message_still_counts_as_a_tool_call(null_column_db: Path) -> None:
    db = HermesDB(null_column_db)
    try:
        stats = db.read_tool_stats()
        assert [(row["tool_name"], row["call_count"]) for row in stats] == [("web_search", 1)]
    finally:
        db.close()


def test_search_finds_messages_with_null_active(null_column_db: Path) -> None:
    db = HermesDB(null_column_db)
    try:
        assert db.search_session_ids_by_message("needle") == {"sess_null"}
    finally:
        db.close()


def test_null_cost_columns_collect_as_zero(hermes_home: Path, null_column_db: Path) -> None:
    collector = Collector(hermes_home)
    try:
        state = collector.collect()
        assert "sessions" not in state.health.failed_sources
        session = state.sessions[0]
        assert session.session_id == "sess_null"
        # NULL costs and NULL token counts read as zero, never as None.
        assert session.estimated_cost_usd == 0.0
        assert session.input_tokens == 0
        assert session.cost_status == ""
        assert state.tokens_total.total_cost_usd == 0.0
    finally:
        collector.close()


def _null_v021_db(hermes_home: Path) -> Path:
    """A 0.21-schema state.db whose new session and usage columns are all NULL."""
    db_path = hermes_home / "state.db"
    conn = sqlite3.connect(str(db_path))
    create_state_db_tables(conn, include_schema_version=False, include_v021_columns=True)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        ("sess_null", "cli", time.time() - 60),
    )
    insert_model_usage(
        conn,
        "sess_null",
        "gpt-5.4",
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        reasoning_tokens=None,
        estimated_cost_usd=None,
        actual_cost_usd=None,
    )
    conn.commit()
    conn.close()
    return db_path


def test_null_v021_session_columns_map_to_defaults(hermes_home: Path) -> None:
    _null_v021_db(hermes_home)
    collector = Collector(hermes_home)
    try:
        session = collector.collect().sessions[0]
    finally:
        collector.close()

    assert session.git_branch == ""
    assert session.chat_type == ""
    assert session.display_name == ""
    assert session.title_source == ""
    assert session.profile_name == ""
    assert session.pinned is False
    assert session.last_activity_at == 0.0
    assert session.last_activity_description == ""
    assert session.actual_cost_usd == 0.0
    assert session.cost_source == ""
    assert session.compression_failure_error == ""


def test_null_model_usage_columns_collect_as_zero(hermes_home: Path) -> None:
    _null_v021_db(hermes_home)
    collector = Collector(hermes_home)
    try:
        state = collector.collect()
    finally:
        collector.close()

    usage = state.token_analytics.model_usage_all[0]
    assert usage.model == "gpt-5.4"
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0
    assert usage.reasoning_tokens == 0
    assert usage.estimated_cost_usd == 0.0
    assert usage.actual_cost_usd == 0.0
    assert usage.has_actual_cost is False
    assert state.health.failed_sources == []
