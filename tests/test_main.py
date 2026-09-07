"""CLI entry point: argument parsing, snapshot modes, signal handling, and exit codes."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

from hermesd.__main__ import (
    _positive_int,
    _snapshot_file_inside_hermes_home,
    main,
    parse_args,
    resolve_hermes_home,
    resolve_profile_name,
)
from tests.conftest import create_kanban_db_tables


def test_parse_args_defaults():
    args = parse_args([])
    assert args.hermes_home is None
    assert args.refresh_rate == 5
    assert args.no_color is False
    assert args.snapshot is False


def test_parse_args_custom():
    args = parse_args(["--hermes-home", "/tmp/h", "--refresh-rate", "10", "--no-color"])
    assert args.hermes_home == Path("/tmp/h")
    assert args.refresh_rate == 10
    assert args.no_color is True


def test_parse_args_snapshot():
    args = parse_args(["--snapshot"])
    assert args.snapshot is True


def test_parse_args_snapshot_file():
    args = parse_args(["--snapshot-file", "/tmp/hermesd.txt"])
    assert args.snapshot_file == Path("/tmp/hermesd.txt")


def test_parse_args_snapshot_panel():
    args = parse_args(["--snapshot-panel", "10"])
    assert args.snapshot_panel == 10


def test_parse_args_snapshot_panel_zero_alias():
    args = parse_args(["--snapshot-panel", "0"])
    assert args.snapshot_panel == 10


@pytest.mark.parametrize("value", ["11", "12", "13"])
def test_parse_args_snapshot_panel_above_ten(value: str):
    args = parse_args(["--snapshot-panel", value])
    assert args.snapshot_panel == int(value)


def test_parse_args_snapshot_format_and_log_tail_bytes():
    args = parse_args(["--snapshot-format", "json", "--log-tail-bytes", "4096"])
    assert args.snapshot_format == "json"
    assert args.log_tail_bytes == 4096


@pytest.mark.parametrize("value", ["14", "-1"])
def test_parse_args_rejects_invalid_snapshot_panel(value: str):
    with pytest.raises(SystemExit):
        parse_args(["--snapshot-panel", value])


def test_parse_args_invalid_snapshot_panel_lists_available_panels(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--snapshot-panel", "14"])

    err = capsys.readouterr().err
    assert "snapshot panel must be one of:" in err
    assert "10" in err
    assert "11" in err
    assert "12" in err
    assert "13" in err


def test_parse_args_help_describes_registered_snapshot_panels(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_args(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Select a panel by number" in out
    assert "0 aliases panel 10" in out
    assert "1-9 or 0" not in out


@pytest.mark.parametrize("value", ["0", "-1"])
def test_parse_args_rejects_non_positive_refresh_rate(value: str):
    with pytest.raises(SystemExit):
        parse_args(["--refresh-rate", value])


@pytest.mark.parametrize("value", ["0", "-1"])
def test_parse_args_rejects_non_positive_log_tail_bytes(value: str):
    with pytest.raises(SystemExit):
        parse_args(["--log-tail-bytes", value])


def test_positive_int_error_message_is_generic():
    with pytest.raises(argparse.ArgumentTypeError, match="value must be a positive integer"):
        _positive_int("-1")


def test_resolve_hermes_home_explicit():
    args = parse_args(["--hermes-home", "/tmp/test-hermes"])
    path = resolve_hermes_home(args)
    assert path == Path("/tmp/test-hermes")


def test_resolve_hermes_home_env(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    args = parse_args([])
    monkeypatch.setenv("HERMES_HOME", "/tmp/env-hermes")
    path = resolve_hermes_home(args)
    assert path == Path("/tmp/env-hermes")


def test_resolve_hermes_home_default(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    args = parse_args([])
    path = resolve_hermes_home(args)
    assert path == Path.home() / ".hermes"


def test_resolve_profile_name_default_none(monkeypatch):
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    assert resolve_profile_name(parse_args([])) is None


def test_resolve_profile_name_uses_env(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "research")
    assert resolve_profile_name(parse_args([])) == "research"


def test_resolve_profile_name_cli_overrides_env(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "research")
    assert resolve_profile_name(parse_args(["--profile", "coding"])) == "coding"


def test_main_exits_on_missing_dir(tmp_path):
    missing = tmp_path / "nonexistent"
    with pytest.raises(SystemExit) as exc:
        main(["--hermes-home", str(missing)])
    assert exc.value.code == 1


def test_main_exits_on_missing_profile(populated_hermes_home: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--hermes-home", str(populated_hermes_home), "--profile", "missing"])

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "Profile 'missing' does not exist" in err


def test_main_snapshot_outputs_overview(populated_hermes_home: Path, capsys):
    main(["--hermes-home", str(populated_hermes_home), "--snapshot", "--no-color"])
    out = capsys.readouterr().out
    assert "Gateway & Platforms" in out
    assert "Sessions" in out
    assert "Logs" in out


def test_main_snapshot_stdout_renders_once(populated_hermes_home: Path, capsys, monkeypatch):
    from hermesd import __main__ as main_module

    render_count = 0
    closed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            nonlocal render_count
            render_count += 1
            return "snapshot"

        def render_snapshot_json(self, panel_num=None):
            raise AssertionError("json renderer should not be used")

        def render_snapshot(self, panel_num=None):
            raise AssertionError("stdout path should print the captured snapshot text")

        def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    main_module.main(["--hermes-home", str(populated_hermes_home), "--snapshot", "--no-color"])

    assert capsys.readouterr().out == "snapshot"
    assert render_count == 1
    assert closed is True


def test_main_snapshot_file_writes_output(populated_hermes_home: Path, tmp_path: Path):
    output_path = tmp_path / "snapshot.txt"
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-file",
            str(output_path),
            "--no-color",
        ]
    )
    text = output_path.read_text()
    assert "Gateway & Platforms" in text
    assert "Memory" in text


def test_main_rejects_snapshot_file_under_hermes_home(populated_hermes_home: Path):
    output_path = populated_hermes_home / "snapshot.txt"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--hermes-home",
                str(populated_hermes_home),
                "--snapshot-file",
                str(output_path),
                "--no-color",
            ]
        )

    assert exc.value.code == 1
    assert not output_path.exists()


def test_snapshot_file_guard_output_equal_to_hermes_home(populated_hermes_home: Path):
    assert _snapshot_file_inside_hermes_home(populated_hermes_home, populated_hermes_home) is True


def test_snapshot_file_guard_dotdot_traversal_into_home(populated_hermes_home: Path):
    sneaky = populated_hermes_home / "logs" / ".." / "snapshot.txt"
    assert _snapshot_file_inside_hermes_home(sneaky, populated_hermes_home) is True


def test_snapshot_file_guard_dotdot_traversal_escaping_home(populated_hermes_home: Path):
    escaped = populated_hermes_home / ".." / "snapshot.txt"
    assert _snapshot_file_inside_hermes_home(escaped, populated_hermes_home) is False


def test_snapshot_file_guard_common_prefix_sibling_is_outside(populated_hermes_home: Path):
    sibling = populated_hermes_home.with_name(populated_hermes_home.name + "-evil") / "snap.txt"
    assert _snapshot_file_inside_hermes_home(sibling, populated_hermes_home) is False


def test_snapshot_file_guard_symlinked_output_path_into_home(populated_hermes_home: Path):
    outside_dir = populated_hermes_home.parent / "outside"
    outside_dir.mkdir()
    link = outside_dir / "tunnel"
    link.symlink_to(populated_hermes_home, target_is_directory=True)
    assert _snapshot_file_inside_hermes_home(link / "snapshot.txt", populated_hermes_home) is True


def test_snapshot_file_guard_symlinked_file_into_home(populated_hermes_home: Path):
    target = populated_hermes_home / "real-target.txt"
    link = populated_hermes_home.parent / "linked-output.txt"
    link.symlink_to(target)
    assert _snapshot_file_inside_hermes_home(link, populated_hermes_home) is True


def test_main_rejects_symlinked_snapshot_file_into_hermes_home(
    populated_hermes_home: Path, tmp_path: Path
):
    tunnel = tmp_path / "tunnel"
    tunnel.symlink_to(populated_hermes_home, target_is_directory=True)
    output_path = tunnel / "snapshot.txt"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--hermes-home",
                str(populated_hermes_home),
                "--snapshot-file",
                str(output_path),
                "--no-color",
            ]
        )

    assert exc.value.code == 1
    assert not (populated_hermes_home / "snapshot.txt").exists()


def test_main_closes_snapshot_app_when_file_write_fails(
    populated_hermes_home: Path,
    tmp_path: Path,
    monkeypatch,
):
    from hermesd import __main__ as main_module

    closed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            return "snapshot"

        def close(self):
            nonlocal closed
            closed = True

    def fail_write_text(self: Path, text: str):
        raise OSError("disk full")

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)
    monkeypatch.setattr(Path, "write_text", fail_write_text)

    with pytest.raises(OSError, match="disk full"):
        main_module.main(
            [
                "--hermes-home",
                str(populated_hermes_home),
                "--snapshot-file",
                str(tmp_path / "snapshot.txt"),
                "--no-color",
            ]
        )

    assert closed is True


def test_main_snapshot_panel_outputs_detail(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "10",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "[10] Memory" in out
    assert "SOUL.md" in out


def test_main_snapshot_panel_11_outputs_kanban_detail(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "11",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "[11] Kanban" in out
    assert "Status Counts" in out


def test_main_snapshot_panel_file_writes_detail(populated_hermes_home: Path, tmp_path: Path):
    output_path = tmp_path / "memory-panel.txt"
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "2",
            "--snapshot-file",
            str(output_path),
            "--no-color",
        ]
    )
    text = output_path.read_text()
    assert "[2] Sessions" in text
    assert "sess_001" in text


def test_main_snapshot_panel_2_surfaces_billing_context_summary(
    populated_hermes_home: Path, capsys
):
    conn = sqlite3.connect(str(populated_hermes_home / "state.db"))
    conn.execute(
        "UPDATE sessions SET model = ?, billing_base_url = ?, input_tokens = ?, output_tokens = ?",
        ("MiniMax-M3", "https://api.minimax.io/anthropic", 50_000, 4_000),
    )
    conn.commit()
    conn.close()
    (populated_hermes_home / "context_length_cache.yaml").write_text(
        "context_lengths:\n  MiniMax-M3@https://api.minimax.io/v1: 1048576\n"
    )

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "2",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "Billing & Context" in out
    assert "Lifetime / Limit" in out


def test_main_snapshot_panel_3_surfaces_endpoint_and_cost_status_summary(
    populated_hermes_home: Path, capsys
):
    conn = sqlite3.connect(str(populated_hermes_home / "state.db"))
    conn.execute(
        "UPDATE sessions SET billing_base_url = ?, cost_status = ?",
        ("https://api.minimax.io/anthropic", "estimated"),
    )
    conn.commit()
    conn.close()

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "3",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "Cost Status" in out
    assert "By Endpoint" in out


def test_main_snapshot_panel_5_surfaces_config_detail_from_yaml(
    populated_hermes_home: Path, capsys
):
    (populated_hermes_home / "config.yaml").write_text(
        "dashboard:\n"
        "  public_url: https://dashboard.example.com\n"
        "toolsets:\n"
        "  - coding\n"
        "  - research\n"
        "auxiliary:\n"
        "  summarizer: gpt-5.4\n"
        "  reviewer: gpt-5.4\n"
        "  planner: gpt-5.4\n"
    )

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "5",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert re.search(r"Dashboard URL\s+https://dashboard[.]example[.]com", out)
    assert "Toolsets" in out
    assert "coding, research" in out
    assert re.search(r"Auxiliary Slots\s+3", out)


def test_main_snapshot_json_outputs_state(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-format",
            "json",
            "--no-color",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["panel_num"] is None
    assert payload["state"]["gateway"]["state"] == "running"
    assert payload["state"]["memory"]["memory_file_count"] >= 1


def test_main_snapshot_panel_json_file(populated_hermes_home: Path, tmp_path: Path):
    output_path = tmp_path / "panel.json"
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "8",
            "--snapshot-format",
            "json",
            "--snapshot-file",
            str(output_path),
            "--no-color",
        ]
    )
    payload = json.loads(output_path.read_text())
    assert payload["panel_num"] == 8
    assert payload["panel_name"] == "Logs"
    assert "logs" in payload["state"]


def test_main_snapshot_panel_12_json_annotates_operations(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "12",
            "--snapshot-format",
            "json",
            "--no-color",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["panel_num"] == 12
    assert payload["panel_name"] == "Operations"
    assert "operations" in payload["state"]


def test_main_snapshot_panel_12_json_includes_visibility_fields(
    populated_hermes_home: Path,
    capsys,
):
    verification_db = populated_hermes_home / "verification_evidence.db"
    conn = sqlite3.connect(str(verification_db))
    conn.executescript(
        """
        CREATE TABLE verification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_id TEXT NOT NULL,
            cwd TEXT NOT NULL,
            root TEXT NOT NULL,
            command TEXT NOT NULL,
            canonical_command TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            output_summary TEXT NOT NULL
        );
        INSERT INTO verification_events VALUES (
            1, '2026-07-10T10:00:00Z', 'sess-a', '/repo', '/repo',
            'uv run pytest', 'pytest', 'test', 'full', 'passed', 0, '12 passed'
        );
        """
    )
    conn.commit()
    conn.close()

    trace_dir = populated_hermes_home / "moa-traces"
    trace_dir.mkdir()
    (trace_dir / "sess-moa.jsonl").write_text(
        json.dumps({"status": "ok", "preset": "council"}) + "\n"
    )
    conn = sqlite3.connect(str(populated_hermes_home / "state.db"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO state_meta VALUES (?, ?)",
        (
            "goal:sess-goal",
            json.dumps({"goal": "Ship visibility", "status": "active"}),
        ),
    )
    conn.commit()
    conn.close()

    projects_db = populated_hermes_home / "projects.db"
    conn = sqlite3.connect(str(projects_db))
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            icon TEXT,
            color TEXT,
            board_slug TEXT,
            primary_path TEXT,
            created_at TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE discovered_repos (
            root TEXT PRIMARY KEY,
            label TEXT,
            last_seen TEXT NOT NULL
        );
        INSERT INTO projects VALUES (
            'p1', 'hermesd', 'hermesd', '', '', '', '', '',
            '2026-07-10T00:00:00Z', 0
        );
        INSERT INTO discovered_repos VALUES ('/repo/hermesd', 'hermesd', '2026-07-12T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "12",
            "--snapshot-format",
            "json",
            "--no-color",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    operations = payload["state"]["operations"]

    assert operations["verification_event_count"] == 1
    assert operations["verification_latest_events"][0]["canonical_command"] == "pytest"
    assert operations["moa_trace_count"] == 1
    assert operations["moa_trace_latest_record_summary"] == "ok council"
    assert operations["goal_count"] == 1
    assert operations["goals"][0]["goal"] == "Ship visibility"
    assert operations["project_missing_primary_path_count"] == 1
    assert operations["discovered_repos"][0]["root"] == "/repo/hermesd"


def test_main_snapshot_panel_12_text_outputs_visibility_detail(
    populated_hermes_home: Path,
    capsys,
):
    verification_db = populated_hermes_home / "verification_evidence.db"
    conn = sqlite3.connect(str(verification_db))
    conn.executescript(
        """
        CREATE TABLE verification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_id TEXT NOT NULL,
            cwd TEXT NOT NULL,
            root TEXT NOT NULL,
            command TEXT NOT NULL,
            canonical_command TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            output_summary TEXT NOT NULL
        );
        INSERT INTO verification_events VALUES (
            1, '2026-07-10T10:00:00Z', 'sess-a', '/repo', '/repo',
            'uv run ruff check .', 'ruff check', 'lint', 'full', 'failed', 1,
            'F401 unused import'
        );
        """
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(populated_hermes_home / "state.db"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO state_meta VALUES (?, ?)",
        ("goal:sess-goal", json.dumps({"goal": "Ship visibility", "status": "active"})),
    )
    conn.commit()
    conn.close()

    trace_dir = populated_hermes_home / "moa-traces"
    trace_dir.mkdir()
    (trace_dir / "sess-moa.jsonl").write_text(json.dumps({"status": "ok"}) + "\n")

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "12",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out

    assert "Verification Evidence" in out
    assert "ruff check" in out
    assert "Ship visibility" in out
    assert "MoA Traces" in out


def test_main_snapshot_json_includes_visibility_state(populated_hermes_home: Path, capsys):
    (populated_hermes_home / "config.yaml").write_text(
        yaml.dump(
            {
                "scale_to_zero": {"idle_timeout_minutes": 10},
                "cron": {"provider": "chronos"},
            }
        )
    )
    (populated_hermes_home / "gateway_state.json").write_text(
        json.dumps(
            {
                "pid": 12345,
                "gateway_state": "running",
                "active_agents": 0,
                "platforms": {"raft": {"state": "connected"}},
            }
        )
    )
    (populated_hermes_home / "channel_aliases.json").write_text(
        json.dumps({"raft": {"room-1": {"label": "Ops", "stale": True}}})
    )
    (populated_hermes_home / "cron" / "suggestions.json").write_text(
        json.dumps({"suggestions": [{"name": "standup"}]})
    )
    (populated_hermes_home / "skills" / ".curator_state").write_text(
        json.dumps({"run_count": 2, "last_report_path": "logs/curator/run.md"})
    )
    (populated_hermes_home / "skills" / ".usage.json").write_text(
        json.dumps({"dev-lint": {"use_count": 4, "pinned": True}})
    )
    (populated_hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "credential_pool": {
                    "vertex": [
                        {
                            "access_expires_at": "2026-07-11T12:00:00Z",
                            "last_refresh": "2026-07-11T11:00:00Z",
                        }
                    ]
                }
            }
        )
    )
    kanban_db = populated_hermes_home / "kanban" / "boards" / "alpha" / "kanban.db"
    kanban_db.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(kanban_db))
    create_kanban_db_tables(conn)
    conn.execute("ALTER TABLE tasks ADD COLUMN block_kind TEXT")
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, block_kind) VALUES (?, ?, ?, ?, ?)",
        ("alpha-task", "Alpha task", "blocked", 1, "needs_input"),
    )
    conn.commit()
    conn.close()

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-format",
            "json",
            "--no-color",
        ]
    )
    state = json.loads(capsys.readouterr().out)["state"]

    assert state["gateway"]["scale_to_zero_relay_only"] is True
    assert state["channels"]["stale_alias_count"] == 1
    assert state["cron"]["provider"] == "chronos"
    assert state["cron"]["suggestion_count"] == 1
    assert state["cron"]["ticker_health"] == "unknown"
    executions = state["cron_executions"]
    assert executions["db_present"] is True
    assert executions["open_incident_count"] == 2
    assert executions["unacked_incident_count"] == 1
    assert {stats["job_id"] for stats in executions["job_stats"]} == {"job-alpha", "job-beta"}
    assert executions["recent"][0]["execution_id"] == "exec_alpha_running"
    boards = {board["slug"]: board for board in state["kanban"]["boards"]}
    assert boards["alpha"]["block_kind_counts"] == {"needs_input": 1}
    assert state["skills_memory"]["credential_pools"][0]["expires_at"] == "2026-07-11T12:00:00Z"
    assert state["memory"]["skill_usage_count"] == 1
    assert state["curator"]["scheduler_run_count"] == 2


def test_main_snapshot_panel_12_outputs_operations_detail(populated_hermes_home: Path, capsys):
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "12",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "[12] Operations" in out
    assert "Desktop Build" in out


def test_main_snapshot_panel_13_outputs_curator_detail(populated_hermes_home: Path, capsys):
    run_dir = populated_hermes_home / "logs" / "curator" / "20260610-133539"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "started_at": "2026-06-10T13:35:39+00:00",
                "model": "MiniMax-M3",
                "provider": "minimax",
                "counts": {"before": 8, "after": 5, "tool_calls_total": 67},
            }
        )
    )

    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-panel",
            "13",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert "[13] Curator" in out
    assert "MiniMax-M3" in out


def test_main_runs_dashboard_when_no_snapshot_flags(populated_hermes_home: Path, monkeypatch):
    from hermesd import __main__ as main_module

    ran = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def run(self):
            nonlocal ran
            ran = True

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    main_module.main(["--hermes-home", str(populated_hermes_home), "--no-color"])

    assert ran is True


def test_python_dash_m_entry_point(populated_hermes_home: Path):
    """`python -m hermesd` must reach main() (the __main__ guard)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "hermesd", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "hermesd" in result.stdout


def test_version_falls_back_when_package_metadata_missing(monkeypatch):
    import importlib
    from importlib.metadata import PackageNotFoundError

    import hermesd

    def missing_package(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", missing_package)
    try:
        reloaded = importlib.reload(hermesd)
        assert reloaded.__version__ == "0.0.0"
    finally:
        monkeypatch.undo()
        importlib.reload(hermesd)


def test_parse_args_version(capsys):
    from hermesd import __version__

    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "hermesd" in out
    assert __version__ in out


def test_main_snapshot_file_creates_missing_parent_dirs(
    populated_hermes_home: Path, tmp_path: Path
):
    output_path = tmp_path / "nested" / "deeper" / "snapshot.txt"
    main(
        [
            "--hermes-home",
            str(populated_hermes_home),
            "--snapshot-file",
            str(output_path),
            "--no-color",
        ]
    )
    text = output_path.read_text()
    assert "Gateway & Platforms" in text


def test_main_snapshot_sigint_during_collect_exits_cleanly(
    populated_hermes_home: Path, monkeypatch, capsys
):
    """Ctrl+C during a slow snapshot collect must not dump a traceback."""
    closed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            os.kill(os.getpid(), signal.SIGINT)
            return "snapshot"

        def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)
    previous_handler = signal.getsignal(signal.SIGINT)

    with pytest.raises(SystemExit) as excinfo:
        main(["--hermes-home", str(populated_hermes_home), "--snapshot", "--no-color"])

    assert excinfo.value.code == 130  # 128 + SIGINT, conventional shell exit code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "Interrupted" in captured.err
    assert closed is True
    assert signal.getsignal(signal.SIGINT) == previous_handler


def test_main_snapshot_sigterm_during_collect_exits_cleanly(
    populated_hermes_home: Path, monkeypatch, capsys
):
    closed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_json(self, panel_num=None):
            os.kill(os.getpid(), signal.SIGTERM)
            return "{}"

        def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--hermes-home",
                str(populated_hermes_home),
                "--snapshot-format",
                "json",
                "--no-color",
            ]
        )

    assert excinfo.value.code == 143  # 128 + SIGTERM
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert "Interrupted" in captured.err
    assert closed is True


def test_main_snapshot_second_signal_forces_default_disposition(
    populated_hermes_home: Path, monkeypatch
):
    """A second Ctrl+C/SIGTERM must be able to kill a wedged collect: the
    first signal is caught, but immediately re-arms the default disposition."""
    dfl_observed = False

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            nonlocal dfl_observed
            os.kill(os.getpid(), signal.SIGINT)
            dfl_observed = signal.getsignal(signal.SIGINT) == signal.SIG_DFL
            return "snapshot"

        def close(self):
            pass

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    with pytest.raises(SystemExit):
        main(["--hermes-home", str(populated_hermes_home), "--snapshot", "--no-color"])

    assert dfl_observed is True


def test_main_rechecks_snapshot_path_after_render(
    populated_hermes_home: Path, tmp_path: Path, monkeypatch
):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    tunnel = outside_dir / "tunnel"
    tunnel.mkdir()
    output_path = tunnel / "snapshot.txt"

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def render_snapshot_text(self, panel_num=None):
            tunnel.rmdir()
            tunnel.symlink_to(populated_hermes_home, target_is_directory=True)
            return "snapshot"

        def close(self):
            pass

    monkeypatch.setattr("hermesd.app.DashboardApp", FakeApp)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--hermes-home",
                str(populated_hermes_home),
                "--snapshot-file",
                str(output_path),
                "--no-color",
            ]
        )

    assert excinfo.value.code == 1
    assert not (populated_hermes_home / "snapshot.txt").exists()


def test_main_exits_with_code_one_when_render_fails(
    populated_hermes_home: Path, monkeypatch, capsys
):
    """A render failure inside run() reaches the CLI as exit code 1, not a traceback."""

    class FailingApp:
        def __init__(self, **kwargs):
            pass

        def run(self):
            print("hermesd: render failed: RuntimeError: boom", file=sys.stderr)
            raise SystemExit(1)

        def close(self):
            pass

    monkeypatch.setattr("hermesd.app.DashboardApp", FailingApp)

    with pytest.raises(SystemExit) as excinfo:
        main(["--hermes-home", str(populated_hermes_home), "--no-color"])

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "render failed" in captured.err


def test_parse_args_profile_default_none():
    args = parse_args([])
    assert args.profile is None


def test_parse_args_accepts_profile_flag():
    args = parse_args(["--profile", "coding"])
    assert args.profile == "coding"
