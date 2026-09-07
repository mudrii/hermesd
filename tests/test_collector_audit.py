"""Regression tests for the 2026-09 collector audit.

Each section maps to one audit finding:

1. Available tools must come from ``cache/banner_snapshot.json`` (the live
   inventory) and only fall back to the legacy sessions.json scan; the fallback
   must not retain every parsed session document in the file cache.
2. PR-monitor state written under ``cron/state/`` is never read.
3. The live process registry is ``spawn-ledger.json``; ``processes.json`` is a
   legacy fallback.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml

import hermesd.collector as collector_module
from hermesd.collector import _LOG_LINE_PATTERN, Collector, _coerce_int, _pid_exists

# --- 1. Available tools come from the banner snapshot ------------------------


def _write_banner_snapshot(hermes_home: Path, payload: object) -> Path:
    cache_dir = hermes_home / "cache"
    cache_dir.mkdir(exist_ok=True)
    path = cache_dir / "banner_snapshot.json"
    path.write_text(json.dumps(payload))
    return path


def _write_legacy_sessions(hermes_home: Path, tool_name: str) -> None:
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    (sessions_dir / "session_s1.json").write_text(
        json.dumps({"session_id": "s1", "tools": [{"name": tool_name}]})
    )


def test_available_tools_read_banner_snapshot(hermes_home: Path):
    _write_banner_snapshot(
        hermes_home,
        {
            "fingerprint": "f1",
            "enabled_toolsets": ["core"],
            "tools": [
                {"type": "function", "function": {"name": "web_search"}},
                {"type": "function", "function": {"name": "shell_exec"}},
                {"type": "function", "function": {"name": "web_search"}},
            ],
        },
    )
    # The legacy index is now a stub that names no sessions.
    (hermes_home / "sessions" / "sessions.json").write_text(json.dumps({"_README": "legacy"}))

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == ["shell_exec", "web_search"]
    assert state.available_tools == 2
    assert "tools_index" not in state.health.failed_sources


def test_available_tools_prefer_banner_snapshot_over_session_files(hermes_home: Path):
    _write_banner_snapshot(
        hermes_home,
        {"tools": [{"type": "function", "function": {"name": "banner_tool"}}]},
    )
    _write_legacy_sessions(hermes_home, "legacy_tool")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == ["banner_tool"]


def test_available_tools_tolerate_malformed_banner_entries(hermes_home: Path):
    _write_banner_snapshot(
        hermes_home,
        {
            "tools": [
                "not-a-mapping",
                {"type": "function"},
                {"type": "function", "function": {"name": 42}},
                {"type": "function", "function": {"name": "good_tool"}},
            ]
        },
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == ["good_tool"]


def test_available_tools_fall_back_to_sessions_when_banner_has_no_names(hermes_home: Path):
    _write_banner_snapshot(hermes_home, {"tools": "not-a-list"})
    _write_legacy_sessions(hermes_home, "legacy_tool")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == ["legacy_tool"]


def test_available_tools_empty_without_banner_or_sessions(hermes_home: Path):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.available_tool_names == []
    assert state.available_tools == 0


def test_session_tool_scan_does_not_retain_parsed_session_documents(hermes_home: Path):
    """The fallback scan caches extracted names, not whole session documents."""
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    session_file = sessions_dir / "session_s1.json"
    session_file.write_text(
        json.dumps({"session_id": "s1", "bulk": "x" * 4096, "tools": [{"name": "web_search"}]})
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
        # White-box on purpose: the defect is unbounded retention of parsed
        # session JSON inside LastGoodFileCache.
        cached_paths = set(c._file_cache._json_values)
    finally:
        c.close()

    assert state.available_tool_names == ["web_search"]
    assert str(session_file) not in cached_paths


# --- 2. PR monitors under cron/state ----------------------------------------


def test_pr_monitors_read_cron_state_directory(hermes_home: Path):
    state_dir = hermes_home / "cron" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "pr_monitor.json").write_text(
        json.dumps(
            {
                "repo": "NousResearch/hermes-agent",
                "checked_at": "2026-09-01T00:00:00Z",
                "prs": {"1": {}, "2": {}, "3": {}},
                "tracked_numbers": [1, 2, 3],
            }
        )
    )
    (state_dir / "pr_monitor.json.bak").write_text(json.dumps({"repo": "stale/backup"}))
    (state_dir / "pr_monitor.json.lock").write_text("")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    monitors = state.operations.pr_monitors
    assert [m.filename for m in monitors] == ["pr_monitor.json"]
    assert monitors[0].repo == "NousResearch/hermes-agent"
    assert monitors[0].monitored_count == 3
    assert monitors[0].tracked_count == 3


# --- 3. spawn-ledger.json is the live process registry ----------------------


def _ledger_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "pid": 4242,
        "create_time": 1775791440.0,
        "purpose": "mcp-helper",
        "install": "/opt/hermes",
        "spawner_pid": 1,
        "spawner_create": 0.0,
        "registered_at": 1775791441.0,
        "argv": "python -m hermes.mcp_helper",
        "host": "127.0.0.1",
        "port": None,
        "profile": "coding",
    }
    entry.update(overrides)
    return entry


def test_background_processes_read_spawn_ledger(hermes_home: Path, sample_processes: Path):
    (hermes_home / "spawn-ledger.json").write_text(
        json.dumps(
            [
                _ledger_entry(),
                _ledger_entry(
                    pid="4343",
                    purpose="serve",
                    argv="hermes serve",
                    port=8080,
                    profile="",
                    create_time=None,
                ),
            ]
        )
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    processes = state.background_processes
    assert [p.pid for p in processes] == [4242, 4343]
    # processes.json is dead once the ledger exists.
    assert all(p.session_id not in {"proc_alpha", "proc_beta"} for p in processes)
    assert processes[0].purpose == "mcp-helper"
    assert processes[0].command == "python -m hermes.mcp_helper"
    assert processes[0].started_at == 1775791440.0
    assert processes[0].profile == "coding"
    assert processes[0].session_id
    assert processes[1].port == 8080
    assert processes[1].started_at == 1775791441.0


def test_background_processes_fall_back_when_ledger_is_malformed(
    hermes_home: Path, sample_processes: Path
):
    (hermes_home / "spawn-ledger.json").write_text(json.dumps(["not-a-mapping", 7]))

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert {p.session_id for p in state.background_processes} == {"proc_alpha", "proc_beta"}


def test_background_processes_fall_back_when_ledger_is_empty(
    hermes_home: Path, sample_processes: Path
):
    (hermes_home / "spawn-ledger.json").write_text("[]")

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert {p.session_id for p in state.background_processes} == {"proc_alpha", "proc_beta"}


def test_background_processes_empty_without_ledger_or_processes_json(hermes_home: Path):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.background_processes == []


# --- 4. Log-line pattern backtracking ---------------------------------------


def test_log_line_pattern_matches_pathological_line_quickly():
    line = "2024-01-01 00:00:00 - " + " " * 20000

    start = time.perf_counter()
    _LOG_LINE_PATTERN.match(line)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "2026-04-09 15:41:58,123 - hermes - INFO - Tool call: web_search",
            ("2026-04-09 15:41:58", "hermes", "INFO", "Tool call: web_search"),
        ),
        (
            "2026-04-09 15:40:00,000 - gateway - INFO - Telegram connected",
            ("2026-04-09 15:40:00", "gateway", "INFO", "Telegram connected"),
        ),
        (
            "2026-04-09 14:00:00,000 - hermes - WARNING - High context usage",
            ("2026-04-09 14:00:00", "hermes", "WARNING", "High context usage"),
        ),
        (
            "2026-04-09 14:00:00 - hermes - ERROR - failed - retrying",
            ("2026-04-09 14:00:00", "hermes", "ERROR", "failed - retrying"),
        ),
    ],
)
def test_log_line_pattern_parses_normal_lines(line: str, expected: tuple[str, str, str, str]):
    match = _LOG_LINE_PATTERN.match(line)

    assert match is not None
    assert (match.group(1), match.group(2).strip(), match.group(3), match.group(4)) == expected


def test_log_lines_are_truncated_before_parsing(hermes_home: Path):
    (hermes_home / "logs" / "agent.log").write_text(
        "2026-04-09 15:41:58,123 - hermes - INFO - " + "x" * 40000 + "\n"
    )

    c = Collector(hermes_home, log_tail_bytes=1_000_000)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.logs.agent_lines
    assert len(state.logs.agent_lines[0].message) <= 4096


# --- 5. Null / wrong-typed config and cron values ----------------------------


def test_config_source_survives_null_and_wrong_typed_values(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump(
            {
                "model": {"default": None, "provider": None},
                "agent": {
                    "max_turns": "unlimited",
                    "reasoning_effort": None,
                    "active_personality": None,
                },
                "compression": {"threshold": None},
                "security": {"redact_secrets": None},
                "approvals": {"mode": None},
            }
        )
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "config" not in state.health.failed_sources
    assert state.config.model == ""
    assert state.config.provider == ""
    assert state.config.max_turns == 0
    assert state.config.compression_threshold == 0.0
    assert state.config.reasoning_effort == ""
    assert state.config.security_redact is False
    assert state.config.approvals_mode == ""
    assert state.config.personality == ""


def test_config_max_turns_null_falls_back_to_default(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"model": {"default": "gpt-5.4"}, "agent": {"max_turns": None}})
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "config" not in state.health.failed_sources
    assert state.config.model == "gpt-5.4"
    assert state.config.max_turns == 0


def test_cron_source_survives_null_enabled(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "j1", "name": "Nightly", "enabled": None}]})
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "cron" not in state.health.failed_sources
    assert [job.job_id for job in state.cron.jobs] == ["j1"]
    assert state.cron.jobs[0].enabled is True


# --- 6. _coerce_int must reject non-finite floats ----------------------------


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("-inf"), float("nan"), "inf", "-inf", "nan"],
)
def test_coerce_int_rejects_non_finite_values(value: object):
    assert _coerce_int(value) == 0


# --- 7. Goal state re-snapshots state.db on every tick -----------------------


def _count_sqlite_connects(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    original = collector_module._connect_readonly_sqlite
    seen: list[Path] = []

    def counting(db_path: Path):
        seen.append(Path(db_path))
        return original(db_path)

    monkeypatch.setattr(collector_module, "_connect_readonly_sqlite", counting)
    return seen


def test_goal_state_is_cached_while_state_db_is_unchanged(
    hermes_home: Path, sample_db: Path, monkeypatch: pytest.MonkeyPatch
):
    connects = _count_sqlite_connects(monkeypatch)

    c = Collector(hermes_home)
    try:
        c.collect()
        c.collect()
    finally:
        c.close()

    assert [p.name for p in connects].count("state.db") == 1


def test_goal_state_recomputed_when_state_db_changes(
    hermes_home: Path, sample_db: Path, monkeypatch: pytest.MonkeyPatch
):
    connects = _count_sqlite_connects(monkeypatch)

    c = Collector(hermes_home)
    try:
        c.collect()
        bumped = sample_db.stat().st_mtime + 10
        os.utime(sample_db, (bumped, bumped))
        c.collect()
    finally:
        c.close()

    assert [p.name for p in connects].count("state.db") == 2


# --- 8. Two git subprocesses per repo per tick -------------------------------


def _count_git_summaries(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    original = collector_module._git_checkpoint_summary
    seen: list[Path] = []

    def counting(repo_dir: Path):
        seen.append(repo_dir)
        return original(repo_dir)

    monkeypatch.setattr(collector_module, "_git_checkpoint_summary", counting)
    return seen


def test_git_checkpoint_summary_is_cached_while_refs_are_unchanged(
    hermes_home: Path, sample_checkpoints: Path, monkeypatch: pytest.MonkeyPatch
):
    summaries = _count_git_summaries(monkeypatch)

    c = Collector(hermes_home)
    try:
        first = c.collect()
        second = c.collect()
    finally:
        c.close()

    assert len(summaries) == 1
    assert first.checkpoints[0].commit_count == 2
    assert second.checkpoints[0].commit_count == 2
    assert second.checkpoints[0].last_reason == first.checkpoints[0].last_reason


def test_git_checkpoint_summary_recomputed_after_new_commit(
    hermes_home: Path, sample_checkpoints: Path, monkeypatch: pytest.MonkeyPatch
):
    summaries = _count_git_summaries(monkeypatch)
    repo_dir = next(p for p in sample_checkpoints.iterdir() if p.is_dir())
    workdir = Path((repo_dir / "HERMES_WORKDIR").read_text().strip())

    c = Collector(hermes_home)
    try:
        first = c.collect()
        _commit(repo_dir, workdir, "Third checkpoint")
        second = c.collect()
    finally:
        c.close()

    assert len(summaries) == 2
    assert first.checkpoints[0].commit_count == 2
    assert second.checkpoints[0].commit_count == 3
    assert second.checkpoints[0].last_reason == "Third checkpoint"


def _commit(repo_dir: Path, workdir: Path, message: str) -> None:
    (workdir / "tracked.txt").write_text(f"{message}\n")
    git = ["git", "--git-dir", str(repo_dir), "--work-tree", str(workdir)]
    subprocess.run([*git, "add", "tracked.txt"], check=True, capture_output=True, text=True)
    subprocess.run([*git, "commit", "-m", message], check=True, capture_output=True, text=True)


# --- 9. Symlink and size-cap policy on plain-text reads ----------------------


def test_memory_files_symlinked_outside_hermes_home_are_ignored(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside-memory.md"
    outside.write_text("## Card\nsecret words from outside the hermes home\n")
    (hermes_home / "memories" / "MEMORY.md").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "memory" not in state.health.failed_sources
    assert state.memory.memory_word_count == 0
    assert state.memory.memory_card_count == 0


def test_soul_excerpt_symlinked_outside_hermes_home_is_ignored(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "outside-soul.md"
    outside.write_text("secret soul line\n")
    (hermes_home / "SOUL.md").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.memory.soul_excerpt == ""


def test_large_memory_file_is_read_under_the_byte_cap(hermes_home: Path):
    (hermes_home / "memories" / "MEMORY.md").write_text("## Card\n" + "word " * 400_000)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "memory" not in state.health.failed_sources
    assert 0 < state.memory.memory_word_count < 400_000
    assert state.memory.memory_card_count == 1


# --- 10. Non-positive PIDs must never reach os.kill --------------------------


@pytest.mark.parametrize("pid", [-1, 0])
def test_gateway_non_positive_pid_is_treated_as_absent(hermes_home: Path, pid: int):
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"pid": pid, "gateway_state": "running", "platforms": {}})
    )
    probed: list[int] = []

    def spy(candidate: int) -> bool:
        probed.append(candidate)
        return True

    c = Collector(hermes_home, pid_exists=spy)
    try:
        state = c.collect()
    finally:
        c.close()

    assert probed == []
    assert state.gateway.pid == 0
    assert state.gateway.running is False


@pytest.mark.parametrize("pid", [-1, 0])
def test_pid_exists_rejects_non_positive_pids(pid: int):
    assert _pid_exists(pid) is False


def test_gateway_pid_file_with_non_positive_pid_is_ignored(hermes_home: Path):
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"gateway_state": "running", "platforms": {}})
    )
    (hermes_home / "gateway.pid").write_text("-1")
    probed: list[int] = []

    def spy(candidate: int) -> bool:
        probed.append(candidate)
        return True

    c = Collector(hermes_home, pid_exists=spy)
    try:
        state = c.collect()
    finally:
        c.close()

    assert probed == []
    assert state.gateway.pid == 0


# --- 12. close() must not wait for a whole collect pass ----------------------


def test_close_returns_promptly_and_skips_remaining_sources(hermes_home: Path, sample_db: Path):
    started = threading.Event()
    release = threading.Event()
    c = Collector(hermes_home)
    real_gateway = c._collect_gateway
    later_source_calls: list[int] = []
    real_config = c._collect_config

    def slow_gateway():
        started.set()
        release.wait(10)
        return real_gateway()

    def counting_config():
        later_source_calls.append(1)
        return real_config()

    c._collect_gateway = slow_gateway
    c._collect_config = counting_config

    collect_errors: list[BaseException] = []

    def run_collect() -> None:
        try:
            c.collect()
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            collect_errors.append(exc)

    closed = threading.Event()
    collector_thread = threading.Thread(target=run_collect)
    collector_thread.start()
    try:
        assert started.wait(10)
        closer = threading.Thread(target=lambda: (c.close(), closed.set()))
        closer.start()
        # Give close() time to reach the collect lock it cannot take yet.
        time.sleep(0.1)
        release.set()
        start = time.perf_counter()
        assert closed.wait(10)
        elapsed = time.perf_counter() - start
    finally:
        release.set()
        collector_thread.join(10)
        closer.join(10)

    assert collect_errors == []
    assert elapsed < 1.0
    assert later_source_calls == []
