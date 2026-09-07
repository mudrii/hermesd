"""Opt-in contract test against the real ~/.hermes data.

The live-home check is skipped unless ``HERMESD_CONTRACT_TEST=1``. It compares
drift-sensitive collected values only when the underlying source supplies those
optional values. The live check is intentionally excluded from default CI so CI
never couples to a particular machine's data; fixture-backed contract checks run
normally.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from hermesd.collector import Collector
from hermesd.models import DashboardState
from hermesd.panels import render_panel
from hermesd.paths import default_hermes_home
from hermesd.theme import Theme
from tests.conftest import create_state_db_tables, render_to_str


def _home() -> Path:
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else default_hermes_home()


@pytest.mark.skipif(
    os.environ.get("HERMESD_CONTRACT_TEST") != "1",
    reason="set HERMESD_CONTRACT_TEST=1 to run the live ~/.hermes contract test",
)
def test_live_hermes_home_has_no_drifted_blank_fields():
    home = _home()
    if not home.exists():
        pytest.skip(f"no Hermes home at {home}")

    _assert_hermes_home_has_no_drifted_blank_fields(home)


def test_fixture_hermes_home_has_no_drifted_blank_fields(populated_hermes_home: Path):
    _assert_hermes_home_has_no_drifted_blank_fields(populated_hermes_home)


def test_fixture_contract_allows_absent_optional_source_values(hermes_home: Path):
    conn = sqlite3.connect(hermes_home / "state.db")
    create_state_db_tables(conn, include_schema_version=False)
    conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('minimal', 'cli', 1.0)")
    conn.commit()
    conn.close()
    (hermes_home / "gateway_state.json").write_text(json.dumps({"platforms": {}}))
    (hermes_home / "desktop-build-stamp.json").write_text("{}")
    (hermes_home / "pr-monitor-empty.json").write_text(
        json.dumps({"repo": "example/empty", "checked_at": "2026-09-07T00:00:00Z"})
    )
    empty_curator_run = hermes_home / "logs" / "curator" / "empty-run"
    empty_curator_run.mkdir(parents=True)
    (empty_curator_run / "run.json").write_text("{}")

    _assert_hermes_home_has_no_drifted_blank_fields(hermes_home)


def test_fixture_contract_ignores_hidden_sessions(hermes_home: Path):
    """A hidden session with populated optional fields must not be demanded of the collector."""
    conn = sqlite3.connect(hermes_home / "state.db")
    create_state_db_tables(conn, include_schema_version=False, include_v021_columns=True)
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, hidden, end_reason, billing_mode) "
        "VALUES ('gone', 'cli', 1.0, 1, 'user_quit', 'subscription_included')"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, hidden, end_reason) "
        "VALUES ('kept', 'cli', 2.0, 0, 'cron_complete')"
    )
    conn.commit()
    conn.close()

    assert "gone" not in _raw_session_optional_values(hermes_home / "state.db")
    assert _raw_session_optional_values(hermes_home / "state.db")["kept"] == {
        "end_reason": "cron_complete"
    }
    _assert_hermes_home_has_no_drifted_blank_fields(hermes_home)


def _assert_hermes_home_has_no_drifted_blank_fields(home: Path) -> None:
    collector = Collector(home)
    try:
        state = collector.collect()
    finally:
        collector.close()

    # The collector must not blank the whole display on a populated home.
    assert state.health.total_sources > 0

    # FIX A1 — credential_pool list-vs-dict: pools present but every label blank
    # (label falling back to the provider name) is the drift signature.
    pools = state.skills_memory.credential_pools
    if pools:
        assert all(p.label for p in pools), (
            "credential pool entries present but labels blank (see FIX A1)"
        )
    raw_pool_labels = _raw_credential_pool_labels(home / "auth.json")
    if raw_pool_labels:
        collected_labels = {p.label for p in pools}
        assert raw_pool_labels <= collected_labels, (
            "credential_pool list entry labels are not surfaced (see FIX A1)"
        )

    # FIX B — compare optional billing/end fields only when source rows populate them.
    expected_session_fields = _raw_session_optional_values(home / "state.db")
    sessions_by_id = {session.session_id: session for session in state.sessions}
    for session_id, expected_fields in expected_session_fields.items():
        assert session_id in sessions_by_id, f"source session {session_id!r} was not collected"
        session = sessions_by_id[session_id]
        for field, expected in expected_fields.items():
            assert getattr(session, field) == expected, (
                f"session {session_id!r} did not surface source field {field!r} (see FIX B)"
            )

    if state.sessions:
        authoritative_cost_sessions = [
            s for s in state.sessions if s.cost_status in {"included", "exact"}
        ]
        if authoritative_cost_sessions:
            text = render_to_str(
                render_panel(
                    3,
                    DashboardState(sessions=authoritative_cost_sessions),
                    Theme(),
                    detail=True,
                ),
                width=160,
            )
            assert "~$" not in text, (
                "live sessions with included/exact cost_status still render as estimated (see FIX A4)"
            )

    # C1 — platform names must match when the source has platform entries.
    raw_platform_names = _raw_gateway_platform_names(home / "gateway_state.json")
    if raw_platform_names:
        collected_platform_names = {platform.name for platform in state.gateway.platforms}
        assert raw_platform_names <= collected_platform_names, (
            "gateway_state.json platform entries were not surfaced (see C1)"
        )

    # FIX A2 — compare a build stamp only when the source has a supported value.
    expected_desktop_stamp = _raw_desktop_build_stamp(home / "desktop-build-stamp.json")
    if expected_desktop_stamp:
        assert state.operations.desktop_build_stamp == expected_desktop_stamp, (
            "desktop-build-stamp.json value was not surfaced (see FIX A2)"
        )

    # FIX A3/A6 — compare monitor counts, including legitimate zeros, to source data.
    expected_monitors = _raw_pr_monitor_expectations(home)
    collected_monitors = {
        monitor.repo or f"::{monitor.filename}": (
            monitor.filename,
            monitor.checked_at,
            monitor.monitored_count,
            monitor.tracked_count,
            monitor.author_pr_count,
        )
        for monitor in state.operations.pr_monitors
    }
    for key, expected in expected_monitors.items():
        assert collected_monitors.get(key) == expected, (
            f"pr-monitor source {key!r} was not surfaced accurately (see FIX A3/A6)"
        )

    # C3 — a parseable curator run must surface; empty/incomplete directories are optional.
    curator_dir = home / "logs" / "curator"
    if _has_raw_curator_run(curator_dir):
        assert state.curator.run_present, "curator run directories exist but none parsed (see C3)"


def _read_json_mapping(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _raw_credential_pool_labels(path: Path) -> set[str]:
    data = _read_json_mapping(path)
    pool = data.get("credential_pool")
    if not isinstance(pool, dict):
        return set()
    labels: set[str] = set()
    for raw_entry in pool.values():
        entries = raw_entry if isinstance(raw_entry, list) else [raw_entry]
        candidates = [entry for entry in entries if isinstance(entry, dict)]
        if candidates:
            selected = min(enumerate(candidates), key=lambda pair: (_priority(pair[1]), pair[0]))[1]
            if selected.get("label"):
                labels.add(str(selected["label"]))
    return labels


def _priority(entry: dict[str, object]) -> int:
    try:
        return int(entry.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def _raw_session_optional_values(path: Path) -> dict[str, dict[str, str]]:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    optional_fields = ("billing_base_url", "end_reason", "billing_mode")
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        selected_fields = [field for field in optional_fields if field in columns]
        if "id" not in columns or not selected_fields:
            return {}
        selected_columns = ", ".join(("id", *selected_fields))
        # Must agree with HermesDB.read_sessions: hidden rows are never collected.
        where = " WHERE COALESCE(hidden, 0) = 0" if "hidden" in columns else ""
        rows = conn.execute(f"SELECT {selected_columns} FROM sessions{where}").fetchall()
        expected: dict[str, dict[str, str]] = {}
        for row in rows:
            populated = {
                field: str(value)
                for field, value in zip(selected_fields, row[1:], strict=True)
                if value is not None and str(value)
            }
            if populated:
                expected[str(row[0])] = populated
        return expected
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _raw_gateway_platform_names(path: Path) -> set[str]:
    platforms = _read_json_mapping(path).get("platforms")
    if not isinstance(platforms, dict):
        return set()
    return {
        str(name)
        for name, info in platforms.items()
        if str(name) and isinstance(info, dict) and info
    }


def _raw_desktop_build_stamp(path: Path) -> str:
    data = _read_json_mapping(path)
    return str(
        data.get("version")
        or data.get("stamp")
        or data.get("builtAt")
        or data.get("built_at")
        or data.get("created_at")
        or str(data.get("contentHash") or "")[:12]
        or ""
    )


def _raw_pr_monitor_expectations(home: Path) -> dict[str, tuple[str, str, int, int, int]]:
    paths = sorted(
        {
            path
            for pattern in (
                "pr-monitor-*.json",
                "pr_monitor_*.json",
                "pr-monitor/*.json",
                "pr_monitor/*.json",
            )
            for path in home.glob(pattern)
            if path.is_file() and not path.is_symlink()
        }
    )
    expected: dict[str, tuple[str, str, int, int, int]] = {}
    for path in paths:
        data = _read_json_mapping(path)
        if not data:
            continue
        repo = str(data.get("repo") or "")
        checked_at = str(data.get("checked_at") or "")
        summary = (
            path.name,
            checked_at,
            _sized_len(data.get("prs")) or _sized_len(data.get("monitored")),
            _sized_len(data.get("tracked_numbers")) or _sized_len(data.get("tracked")),
            _sized_len(data.get("author_prs")) or _sized_len(data.get("author_pr_numbers")),
        )
        key = repo or f"::{path.name}"
        current = expected.get(key)
        if current is None or checked_at > current[1]:
            expected[key] = summary
    return expected


def _sized_len(value: object) -> int:
    return len(value) if isinstance(value, dict | list | tuple | set) else 0


def _has_raw_curator_run(curator_dir: Path) -> bool:
    if not curator_dir.is_dir() or curator_dir.is_symlink():
        return False
    return any(
        _read_json_mapping(run_dir / "run.json")
        for run_dir in curator_dir.iterdir()
        if run_dir.is_dir() and not run_dir.is_symlink()
    )


# --- Panel-surface contract checks (busy-home fixture) ----------------------
#
# Each test collects state from the fully populated fixture home and asserts
# the panel surfaces the fixture data whose source exists on disk — the same
# drift signature as FIX A1-A6/B/C1/C3: source present, panel blank.


def _collect_state(home: Path) -> DashboardState:
    collector = Collector(home)
    try:
        return collector.collect()
    finally:
        collector.close()


def _render(state: DashboardState, panel_num: int, detail: bool) -> str:
    return render_to_str(
        render_panel(panel_num, state, Theme(), detail=detail),
        width=160,
    )


@pytest.mark.parametrize("detail", [False, True])
def test_busy_home_panel_4_tools_surfaces_fixture_data(populated_hermes_home: Path, detail: bool):
    state = _collect_state(populated_hermes_home)
    rendered = _render(state, 4, detail)

    # state.db tool_call rows must surface as tool stats.
    stats = {ts.name: ts.call_count for ts in state.tool_stats}
    assert stats.get("shell_exec") == 3, "tool_call rows present but stats blank"
    assert state.total_tool_calls >= 3
    # processes.json entries must surface as background processes.
    process_ids = {p.session_id for p in state.background_processes}
    assert {"proc_alpha", "proc_beta"} <= process_ids
    # checkpoints repo must surface.
    assert state.checkpoints, "checkpoints dir present but no checkpoint info parsed"
    if detail:
        assert "proc_alpha" in rendered
    # Top tool stats render in both views.
    assert "shell_exec" in rendered


@pytest.mark.parametrize("detail", [False, True])
def test_busy_home_panel_5_config_surfaces_fixture_data(populated_hermes_home: Path, detail: bool):
    state = _collect_state(populated_hermes_home)
    rendered = _render(state, 5, detail)

    assert state.config.model == "gpt-5.4"
    assert state.config.provider == "openai-codex"
    assert "gpt-5.4" in rendered


@pytest.mark.parametrize("detail", [False, True])
def test_busy_home_panel_6_cron_surfaces_fixture_data(populated_hermes_home: Path, detail: bool):
    state = _collect_state(populated_hermes_home)
    rendered = _render(state, 6, detail)

    # cron/.tick.lock exists, so the last-tick age must be parsed.
    assert state.cron.last_tick_ago_seconds is not None, (
        "cron/.tick.lock present but last tick not parsed"
    )
    # config.yaml cron.max_parallel_jobs: 3 must surface.
    assert state.cron.max_parallel_jobs == 3
    assert "Last tick" in rendered
    assert "3" in rendered


@pytest.mark.parametrize("detail", [False, True])
def test_busy_home_panel_8_logs_surfaces_fixture_data(populated_hermes_home: Path, detail: bool):
    state = _collect_state(populated_hermes_home)
    rendered = _render(state, 8, detail)

    assert state.logs.agent_lines, "agent.log present but no lines parsed"
    assert any("web_search" in line.message for line in state.logs.agent_lines)
    assert state.logs.gateway_lines, "gateway.log present but no lines parsed"
    assert state.logs.error_lines, "errors.log present but no lines parsed"
    assert "web_search" in rendered


@pytest.mark.parametrize("detail", [False, True])
def test_busy_home_panel_9_profiles_surfaces_fixture_data(
    populated_hermes_home: Path, detail: bool
):
    # The busy home ships no profiles; add one so the source exists.
    profile_dir = populated_hermes_home / "profiles" / "coding"
    profile_dir.mkdir(parents=True)
    (profile_dir / "SOUL.md").write_text("coding profile soul\n")

    state = _collect_state(populated_hermes_home)
    rendered = _render(state, 9, detail)

    assert state.profiles.profile_count == 1, "profiles/coding present but not discovered"
    assert state.profiles.profiles[0].name == "coding"
    if detail:
        assert "coding" in rendered
    else:
        assert "1 discovered" in rendered


@pytest.mark.parametrize("detail", [False, True])
def test_busy_home_panel_10_memory_surfaces_fixture_data(populated_hermes_home: Path, detail: bool):
    state = _collect_state(populated_hermes_home)
    rendered = _render(state, 10, detail)

    # memories/MEMORY.md + USER.md exist, so counts and file list must surface.
    assert state.memory.memory_file_count >= 2, "memory files present but memory_file_count blank"
    assert state.memory.memory_word_count > 0
    if detail:
        assert "MEMORY.md" in rendered
    else:
        assert "Files:" in rendered and str(state.memory.memory_file_count) in rendered


@pytest.mark.parametrize("detail", [False, True])
def test_busy_home_panel_11_kanban_surfaces_fixture_data(populated_hermes_home: Path, detail: bool):
    state = _collect_state(populated_hermes_home)
    rendered = _render(state, 11, detail)

    assert state.kanban.db_present is True, "kanban.db present but db_present is False"
    assert state.kanban.task_count == 3
    assert state.kanban.status_counts == {"blocked": 1, "in_progress": 2}
    if detail:
        assert "Implement dashboard auth visibility" in rendered
    else:
        assert "3" in rendered
