"""Collection of cron jobs, tick state, output excerpts, and cron log tails."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
import yaml

from hermesd.collect.cron import (
    _EXECUTIONS_RECENT_LIMIT,
    _EXECUTIONS_SCAN_LIMIT,
)
from hermesd.collector import (
    Collector,
    _delivery_target_label,
    _latest_cron_output_excerpt,
)
from hermesd.models import CronTickerHealth
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import (
    CRON_EXECUTIONS_SCHEMA,
    _count_opens,
    _skip_if_root,
    _unreadable,
    create_cron_executions_tables,
    insert_cron_execution,
    iso_ago,
    render_to_str,
)


def test_delivery_target_label_branches():
    directory = {"platforms": {"telegram": [{"name": "Team"}]}}

    assert _delivery_target_label(directory, "") == ""
    assert _delivery_target_label(directory, "local") == "local"
    assert _delivery_target_label(directory, "origin") == "origin"
    assert _delivery_target_label(directory, "email") == "email"
    assert _delivery_target_label(directory, "telegram:Team") == "telegram:Team"
    assert _delivery_target_label(directory, "telegram:Missing") == "telegram:Missing"


def test_collect_cron_logs_respect_log_tail_bytes(hermes_home: Path):
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    output_file = cron_output_dir / "latest.md"
    output_file.write_text("\n".join(f"cron line {idx}" for idx in range(40)))

    c = Collector(hermes_home, log_tail_bytes=128)
    state = c.collect()

    messages = [line.message for line in state.logs.cron_lines]
    assert any("cron line 39" in message for message in messages)
    assert all("cron line 0" not in message for message in messages)
    c.close()


def test_collect_cron_logs_skip_non_dir_and_non_file_entries(hermes_home: Path):
    output_root = hermes_home / "cron" / "output"
    # A stray file sitting directly in the output root (not a job directory).
    (output_root / "stray.txt").write_text("not a job dir")
    job_dir = output_root / "job-1"
    job_dir.mkdir()
    # A nested directory inside a job dir (not an output file).
    (job_dir / "nested").mkdir()
    output_file = job_dir / "latest.md"
    output_file.write_text("real cron output line\n")

    c = Collector(hermes_home)
    state = c.collect()

    messages = [line.message for line in state.logs.cron_lines]
    assert any("real cron output line" in message for message in messages)
    c.close()


def test_collect_cron_suggestion_count(hermes_home: Path):
    cron_dir = hermes_home / "cron"
    (cron_dir / "suggestions.json").write_text(
        json.dumps({"suggestions": [{"name": "standup"}, {"name": "review"}]})
    )

    c = Collector(hermes_home)
    state = c.collect()

    assert state.cron.suggestion_count == 2
    c.close()


def test_collect_cron_suggestions_ignores_malformed_json(hermes_home: Path):
    (hermes_home / "cron" / "suggestions.json").write_text("{not valid json")

    c = Collector(hermes_home)
    state = c.collect()

    assert state.cron.suggestion_count == 0
    assert "cron" not in state.health.failed_sources
    c.close()


def test_collect_chronos_config_health(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump(
            {
                "cron": {
                    "provider": "chronos",
                    "chronos": {
                        "portal_url": "https://portal.example",
                        "callback_url": "https://agent.example",
                        "expected_audience": "hermes-agent",
                        "nas_jwks_url": "https://portal.example/jwks",
                    },
                }
            }
        )
    )

    c = Collector(hermes_home)
    state = c.collect()

    assert state.cron.provider == "chronos"
    assert state.cron.chronos_configured is True
    c.close()


def test_collect_cron_and_curator_visibility_render_from_collected_state(hermes_home: Path):
    (hermes_home / "config.yaml").write_text(
        yaml.dump(
            {
                "cron": {
                    "provider": "chronos",
                    "chronos": {
                        "portal_url": "https://portal.example",
                        "callback_url": "https://agent.example",
                        "expected_audience": "hermes-agent",
                        "nas_jwks_url": "https://portal.example/jwks",
                    },
                },
                "curator": {"consolidate": True},
            }
        )
    )
    (hermes_home / "cron" / "suggestions.json").write_text(
        json.dumps({"suggestions": [{"name": "standup"}]})
    )
    (hermes_home / "skills" / ".curator_state").write_text(
        json.dumps({"paused": False, "run_count": 2, "last_report_path": "logs/curator/run.md"})
    )

    c = Collector(hermes_home)
    state = c.collect()
    cron_text = render_to_str(
        render_panel(6, state, Theme(), detail=True), width=120, no_color=True
    )
    curator_text = render_to_str(
        render_panel(13, state, Theme(), detail=True),
        width=120,
        no_color=True,
    )

    assert "provider=chronos" in cron_text
    assert "suggestions=1" in cron_text
    assert "Scheduler" in curator_text
    assert "Run Count" in curator_text
    assert "logs/curator/run.md" in curator_text
    c.close()


def test_collect_cron_output_redacts_secret_material(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "Job 1"}]})
    )
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    (cron_output_dir / "latest.md").write_text("api_key=cron-secret\n")

    c = Collector(hermes_home)
    state = c.collect()

    assert state.logs.cron_lines[0].message == "api_key=[REDACTED]"
    assert state.cron.jobs[0].latest_output_excerpt == "api_key=[REDACTED]"
    c.close()


def test_collect_cron_logs_preserve_cache_when_latest_output_disappears(hermes_home: Path):
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    output_file = cron_output_dir / "latest.md"
    output_file.write_text("cron line 1\ncron line 2\n")

    c = Collector(hermes_home)
    first = c.collect()
    output_file.unlink()
    second = c.collect()

    assert second.logs.cron_lines == first.logs.cron_lines
    c.close()


def test_collect_cron_job_excerpt_preserves_cache_when_latest_output_disappears(
    hermes_home: Path,
):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "Job 1"}]})
    )
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    output_file = cron_output_dir / "latest.md"
    output_file.write_text("cron line 1\n")

    c = Collector(hermes_home)
    first = c.collect()
    output_file.unlink()
    second = c.collect()

    assert second.cron.jobs[0].latest_output_excerpt == first.cron.jobs[0].latest_output_excerpt
    assert second.cron.jobs[0].latest_output_path == first.cron.jobs[0].latest_output_path
    assert second.cron.jobs[0].latest_output_mtime == first.cron.jobs[0].latest_output_mtime
    c.close()


def test_collect_cron_logs_ignores_symlinked_output_files_outside_home(
    hermes_home: Path, tmp_path: Path
):
    outside_output = tmp_path / "outside-cron.md"
    outside_output.write_text("outside cron secret\n")
    cron_output_dir = hermes_home / "cron" / "output" / "job-1"
    cron_output_dir.mkdir(parents=True)
    (cron_output_dir / "latest.md").symlink_to(outside_output)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.logs.cron_lines == []
    c.close()


def test_collect_cron_logs_ignores_symlinked_output_directory_outside_home(
    hermes_home: Path, tmp_path: Path
):
    outside_output = tmp_path / "outside-output" / "job-1"
    outside_output.mkdir(parents=True)
    (outside_output / "latest.md").write_text("outside cron secret\n")
    output_root = hermes_home / "cron" / "output"
    output_root.rmdir()
    output_root.symlink_to(outside_output.parent, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.logs.cron_lines == []
    c.close()


def test_collect_cron_job_excerpt_ignores_symlinked_output_directory_outside_home(
    hermes_home: Path, tmp_path: Path
):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "Job 1"}]})
    )
    outside_output = tmp_path / "outside-output"
    outside_output.mkdir()
    (outside_output / "latest.md").write_text("outside cron secret\n")
    job_output_dir = hermes_home / "cron" / "output" / "job-1"
    job_output_dir.symlink_to(outside_output, target_is_directory=True)

    c = Collector(hermes_home)
    state = c.collect()

    assert state.cron.jobs[0].latest_output_excerpt == ""
    assert state.cron.jobs[0].latest_output_path == ""
    assert state.cron.jobs[0].latest_output_mtime is None
    c.close()


def test_collect_cron_job_excerpt_rejects_traversal_job_id(hermes_home: Path, tmp_path: Path):
    outside_job = tmp_path / "outside-job"
    outside_job.mkdir()
    (outside_job / "latest.md").write_text("outside cron secret\n")
    traversal_id = os.path.relpath(outside_job, hermes_home / "cron" / "output")
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": traversal_id, "name": "Job 1"}]})
    )

    c = Collector(hermes_home)
    state = c.collect()

    assert state.cron.jobs[0].latest_output_excerpt == ""
    assert state.cron.jobs[0].latest_output_path == ""
    assert state.cron.jobs[0].latest_output_mtime is None
    c.close()


def test_collect_cron_ignores_non_dict_job_entries(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    "not-a-dict",
                    {"id": "job-real", "name": "Real Job", "state": "scheduled"},
                ]
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert "cron" not in state.health.failed_sources
    assert state.cron.job_count == 1
    assert state.cron.jobs[0].job_id == "job-real"
    c.close()


def test_latest_cron_output_excerpt_all_silent_lines(hermes_home: Path):
    """An all-[SILENT] output yields silent_run=True and no excerpt."""
    output_dir = hermes_home / "cron" / "output" / "job-1"
    output_dir.mkdir(parents=True)
    (output_dir / "latest.md").write_text("[SILENT]\n[silent]\n")

    excerpt, silent, output_path, output_mtime = _latest_cron_output_excerpt(
        hermes_home / "cron" / "output", "job-1", max_bytes=32768
    )

    assert excerpt == ""
    assert silent is True
    assert output_path == "latest.md"
    assert output_mtime is not None


def test_latest_cron_output_excerpt_empty_job_id_and_empty_dir(hermes_home: Path):
    empty = ("", False, "", None)
    assert _latest_cron_output_excerpt(hermes_home / "cron" / "output", "", 1024) == empty
    (hermes_home / "cron" / "output" / "job-empty").mkdir(parents=True)
    assert _latest_cron_output_excerpt(hermes_home / "cron" / "output", "job-empty", 1024) == empty


def test_latest_cron_output_excerpt_ignores_file_that_disappears_during_stat(
    hermes_home: Path, monkeypatch
):
    output_dir = hermes_home / "cron" / "output" / "job-1"
    output_dir.mkdir(parents=True)
    stale_file = output_dir / "stale.md"
    stale_file.write_text("stale output\n")
    latest_file = output_dir / "latest.md"
    latest_file.write_text("fresh output\n")
    original_stat = Path.stat

    def flaky_stat(path: Path, *args, **kwargs):
        if path == stale_file:
            raise FileNotFoundError
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    excerpt, silent, output_path, output_mtime = _latest_cron_output_excerpt(
        hermes_home / "cron" / "output", "job-1", max_bytes=32768
    )

    assert excerpt == "fresh output"
    assert silent is False
    assert output_path == "latest.md"
    assert output_mtime is not None


def test_latest_cron_output_excerpt_caps_read_bytes(hermes_home: Path):
    output_dir = hermes_home / "cron" / "output" / "job-1"
    output_dir.mkdir(parents=True)
    output_file = output_dir / "latest.md"
    output_file.write_text("FIRST-MARKER\n" + "\n".join(f"tail filler {i}" for i in range(500)))

    excerpt, silent, output_path, output_mtime = _latest_cron_output_excerpt(
        hermes_home / "cron" / "output", "job-1", max_bytes=256
    )

    assert excerpt
    assert "FIRST-MARKER" not in excerpt
    assert silent is False
    assert output_path == "latest.md"
    assert output_mtime is not None


def test_collect_cron_excerpt_respects_log_tail_bytes(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "job-1", "name": "Job One", "state": "scheduled"}]})
    )
    output_dir = hermes_home / "cron" / "output" / "job-1"
    output_dir.mkdir(parents=True)
    (output_dir / "latest.md").write_text(
        "FIRST-MARKER\n" + "\n".join(f"tail filler {i}" for i in range(2000))
    )

    c = Collector(hermes_home, log_tail_bytes=1024)
    state = c.collect()

    assert len(state.cron.jobs) == 1
    excerpt = state.cron.jobs[0].latest_output_excerpt
    # The capped read only sees the file tail (possibly starting mid-line),
    # so the head marker can never be the excerpt.
    assert excerpt
    assert "FIRST-MARKER" not in excerpt
    c.close()


def test_collector_reads_jobs_json(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "j1",
                        "name": "test-cron",
                        "schedule_display": "every 10m",
                        "state": "scheduled",
                        "enabled": True,
                        "next_run_at": "2026-04-09T19:00:00",
                        "last_status": None,
                        "last_error": None,
                    },
                    {
                        "id": "j2",
                        "name": "failed-job",
                        "schedule_display": "every 1h",
                        "state": "error",
                        "enabled": True,
                        "last_status": "error",
                        "last_error": "timeout",
                    },
                ],
            }
        )
    )
    c = Collector(hermes_home)
    state = c.collect()
    assert state.cron.job_count == 2
    assert state.cron.error_count == 1
    assert state.cron.jobs[0].name == "test-cron"
    assert state.cron.jobs[1].name == "failed-job"
    c.close()


def test_collector_no_jobs_json(hermes_home: Path):
    c = Collector(hermes_home)
    state = c.collect()
    assert state.cron.job_count == 0
    assert state.cron.jobs == []
    c.close()


def test_collector_enriches_cron_jobs_with_delivery_and_output(hermes_home: Path):
    (hermes_home / "channel_directory.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-04-09T19:00:00",
                "platforms": {
                    "telegram": [
                        {"id": "-1001", "name": "My Group", "type": "group"},
                    ]
                },
            }
        )
    )
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "j1",
                        "name": "test-cron",
                        "schedule_display": "every 10m",
                        "state": "scheduled",
                        "enabled": True,
                        "deliver": "telegram:My Group",
                    }
                ],
            }
        )
    )
    output_dir = hermes_home / "cron" / "output" / "j1"
    output_dir.mkdir(parents=True)
    (output_dir / "2026-04-09T19-00-00.md").write_text("[SILENT]\nNo changes to report.\n")

    c = Collector(hermes_home)
    state = c.collect()
    job = state.cron.jobs[0]
    assert job.delivery_target_label == "telegram:My Group"
    assert job.silent_run is True
    assert "No changes to report" in job.latest_output_excerpt
    c.close()


def test_cron_jobs_with_numeric_fields_are_coerced_to_strings(hermes_home: Path):
    (hermes_home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": 123,
                        "name": 456,
                        "schedule_display": 789,
                        "state": 1,
                        "enabled": True,
                        "deliver": "",
                        "next_run_at": 42,
                        "last_status": 7,
                    }
                ]
            }
        )
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.job_count == 1
    job = state.cron.jobs[0]
    assert job.job_id == "123"
    assert job.name == "456"
    assert job.schedule_display == "789"
    assert job.state == "1"
    assert job.next_run_at == "42"
    assert job.last_status == "7"


def test_cron_excerpt_cache_skips_file_read_when_mtime_unchanged(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    output_root = hermes_home / "cron" / "output"
    job_dir = output_root / "job-1"
    job_dir.mkdir(parents=True)
    output_file = job_dir / "out.txt"
    output_file.write_text("first output\n")

    opens = _count_opens(monkeypatch, output_file)

    c = Collector(hermes_home)
    try:
        first = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
        assert len(opens) == 1  # cold read opened the output file
        second = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
    finally:
        c.close()

    assert first[0] == "first output"
    assert second == first
    assert len(opens) == 1  # cache hit must not re-read the file tail


def test_cron_excerpt_refreshes_when_output_file_is_replaced(hermes_home: Path):
    """Replacing the output file changes its signature, so the excerpt is re-read.

    The cache is keyed on `_file_signature` (path, mtime_ns, size). Rewriting with
    a different length moves the size component, so the refresh does not depend on
    the filesystem's mtime granularity.
    """
    output_root = hermes_home / "cron" / "output"
    job_dir = output_root / "job-1"
    job_dir.mkdir(parents=True)
    output_file = job_dir / "out.txt"
    output_file.write_text("old output\n")

    c = Collector(hermes_home)
    try:
        first = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
        assert first[0] == "old output"

        output_file.unlink()
        output_file.write_text("a considerably longer new output line\n")
        second = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
    finally:
        c.close()

    assert second[0] == "a considerably longer new output line"


def test_cron_excerpt_serves_last_good_when_output_file_cannot_be_stat_ed(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unstattable output file has no signature, so the cached excerpt is kept."""
    output_root = hermes_home / "cron" / "output"
    job_dir = output_root / "job-1"
    job_dir.mkdir(parents=True)
    output_file = job_dir / "out.txt"
    output_file.write_text("old output\n")

    c = Collector(hermes_home)
    try:
        first = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
        assert first[0] == "old output"

        real_stat = Path.stat

        def failing_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            if self == output_file:
                raise OSError("stat denied")
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", failing_stat)
        output_file.write_text("new output\n")
        second = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
    finally:
        c.close()

    assert second == first


def test_cron_excerpt_cache_binds_signature_to_filename_with_equal_mtime(hermes_home: Path):
    output_root = hermes_home / "cron" / "output"
    job_dir = output_root / "job-1"
    job_dir.mkdir(parents=True)
    first_file = job_dir / "a.txt"
    first_file.write_text("first\n")
    os.utime(first_file, (100, 100))

    c = Collector(hermes_home)
    try:
        assert c._latest_cron_output_excerpt(output_root, "job-1", 32768)[0] == "first"
        first_file.unlink()
        second_file = job_dir / "b.txt"
        second_file.write_text("second\n")
        os.utime(second_file, (100, 100))
        second = c._latest_cron_output_excerpt(output_root, "job-1", 32768)
    finally:
        c.close()

    assert second[0] == "second"


def test_cron_last_tick_ago_clamped_to_zero_for_future_mtime(hermes_home: Path):
    tick = hermes_home / "cron" / ".tick.lock"
    tick.write_text("")
    future = 2_000_000_000.0
    os.utime(tick, (future, future))

    c = Collector(hermes_home, clock=lambda: future - 100.0)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.last_tick_ago_seconds == 0.0


def test_cron_excerpt_cache_key_matches_read_content(hermes_home: Path, monkeypatch):
    """The cache key must be the mtime of the file actually read, not of an
    earlier independent directory scan (a newer output can land between them)."""
    c = Collector(hermes_home)
    try:
        calls: list[int] = []
        monkeypatch.setattr(
            "hermesd.collector._latest_cron_output_file",
            lambda *a, **k: Path("/fake/out.log"),
        )
        # Scan 1 reports the old mtime...
        monkeypatch.setattr("hermesd.collector._mtime", lambda p: 100.0)

        def fake_excerpt(*a, **k):
            calls.append(1)
            # ...but the read itself sees the newer file that just landed.
            return ("newer content", False, "out.log", 200.0)

        monkeypatch.setattr("hermesd.collector._latest_cron_output_excerpt", fake_excerpt)

        first = c._latest_cron_output_excerpt(Path("/root"), "job1", 1024)
        assert first[0] == "newer content"
        assert len(calls) == 1

        # Next refresh: scan 1 now sees the newer file -> must be a cache HIT.
        monkeypatch.setattr("hermesd.collector._mtime", lambda p: 200.0)
        second = c._latest_cron_output_excerpt(Path("/root"), "job1", 1024)
        assert second[0] == "newer content"
        assert len(calls) == 1  # no re-read: key is the content's own mtime
    finally:
        c.close()


@_skip_if_root
def test_cron_tail_unreadable_file_yields_no_cron_lines(hermes_home: Path):
    # The latest cron output file lists/stats fine but cannot be read, so
    # _tail_latest_cron_output swallows the OSError and returns no lines. The
    # logs source must still succeed (cache-preservation / read-only invariant).
    job_dir = hermes_home / "cron" / "output" / "job-1"
    job_dir.mkdir(parents=True)
    out = job_dir / "run.log"
    out.write_text("2026-04-09 15:41:58,123 - hermes - INFO - cron ran\n")
    os.chmod(out, 0o000)
    try:
        if not _unreadable(out):
            pytest.skip("filesystem allowed read despite chmod 000")
        c = Collector(hermes_home)
        try:
            state = c.collect()
            assert "logs" not in state.health.failed_sources
            assert state.logs.cron_lines == []
        finally:
            c.close()
    finally:
        os.chmod(out, 0o644)


def test_cron_tail_open_error_yields_no_cron_lines(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    job_dir = hermes_home / "cron" / "output" / "job-1"
    job_dir.mkdir(parents=True)
    out = job_dir / "run.log"
    out.write_text("2026-04-09 15:41:58,123 - hermes - INFO - cron ran\n")

    real_open = Path.open

    def fail_target_open(self: Path, *args, **kwargs):
        if self == out:
            raise OSError("simulated cron read failure")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_target_open)
    c = Collector(hermes_home)
    try:
        state = c.collect()
        assert "logs" not in state.health.failed_sources
        assert state.logs.cron_lines == []
    finally:
        c.close()


@_skip_if_root
def test_cron_output_excerpt_unreadable_file_returns_empty(hermes_home: Path):
    # A job output file that lists/stats fine but is unreadable forces
    # _read_tail_text to raise OSError; the excerpt reader returns the empty
    # tuple instead of crashing.
    output_root = hermes_home / "cron" / "output"
    job_dir = output_root / "job-x"
    job_dir.mkdir(parents=True)
    out = job_dir / "latest.md"
    out.write_text("some cron output\n")
    os.chmod(out, 0o000)
    try:
        if not _unreadable(out):
            pytest.skip("filesystem allowed read despite chmod 000")
        result = _latest_cron_output_excerpt(output_root, "job-x", max_bytes=4096)
        assert result == ("", False, "", None)
    finally:
        os.chmod(out, 0o644)


def test_cron_output_excerpt_open_error_returns_empty(
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    output_root = hermes_home / "cron" / "output"
    job_dir = output_root / "job-x"
    job_dir.mkdir(parents=True)
    out = job_dir / "latest.md"
    out.write_text("some cron output\n")

    real_open = Path.open

    def fail_target_open(self: Path, *args, **kwargs):
        if self == out:
            raise OSError("simulated cron excerpt read failure")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_target_open)

    assert _latest_cron_output_excerpt(output_root, "job-x", max_bytes=4096) == (
        "",
        False,
        "",
        None,
    )


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


def _write_jobs_json(home: Path, jobs: list[dict]) -> Path:
    path = home / "cron" / "jobs.json"
    path.write_text(json.dumps({"jobs": jobs}))
    return path


def _stats_by_job(state) -> dict:
    return {stats.job_id: stats for stats in state.cron_executions.job_stats}


def test_collect_cron_executions_counts_the_last_24h_window(
    hermes_home: Path, sample_cron_executions_db: Path
):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "cron_executions" not in state.health.failed_sources
    assert state.cron_executions.db_present is True
    stats = _stats_by_job(state)
    # The 3-day-old completed run is outside the window and must not be counted.
    assert stats["job-alpha"].completed_24h == 2
    assert stats["job-alpha"].failed_24h == 1
    assert stats["job-alpha"].running_24h == 1
    assert stats["job-beta"].failed_24h == 1
    assert stats["job-beta"].completed_24h == 0


def test_collect_cron_executions_last_run_status_duration_and_error(
    hermes_home: Path, sample_cron_executions_db: Path
):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    stats = _stats_by_job(state)
    # Newest job-alpha row is the still-running one: no finished_at, so no duration.
    assert stats["job-alpha"].last_status == "running"
    assert stats["job-alpha"].last_duration_seconds is None
    assert stats["job-alpha"].last_error_excerpt == ""

    beta = stats["job-beta"]
    assert beta.last_status == "failed"
    assert beta.last_duration_seconds == pytest.approx(39.0, abs=1.0)
    # First line only, never the trailing lines of a multi-line error.
    assert beta.last_error_excerpt == "timeout waiting for the agent"


def test_collect_cron_executions_recent_list_joins_job_names(
    hermes_home: Path, sample_cron_executions_db: Path
):
    _write_jobs_json(hermes_home, [{"id": "job-alpha", "name": "Alpha Report"}])

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    recent = state.cron_executions.recent
    assert len(recent) == 6
    # Newest claimed_at first.
    assert recent[0].execution_id == "exec_alpha_running"
    assert recent[0].job_name == "Alpha Report"
    assert recent[0].started_age_seconds == pytest.approx(29.0, abs=5.0)
    # A job with no jobs.json entry falls back to its raw job_id.
    beta = next(row for row in recent if row.job_id == "job-beta")
    assert beta.job_name == "job-beta"
    assert beta.duration_seconds == pytest.approx(39.0, abs=1.0)
    assert beta.error_excerpt == "timeout waiting for the agent"


def test_collect_cron_executions_missing_db_leaves_source_healthy(hermes_home: Path):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "cron_executions" not in state.health.failed_sources
    assert state.cron_executions.db_present is False
    assert state.cron_executions.job_stats == []
    assert state.cron_executions.recent == []
    assert state.cron_executions.open_incident_count == 0


def test_collect_cron_executions_missing_tables_leave_source_healthy(hermes_home: Path):
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "cron_executions" not in state.health.failed_sources
    assert state.cron_executions.db_present is True
    assert state.cron_executions.job_stats == []
    assert state.cron_executions.open_incident_count == 0
    assert state.cron_executions.unacked_incident_count == 0


def test_collect_cron_executions_tolerates_null_and_garbage_timestamps(hermes_home: Path):
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    create_cron_executions_tables(conn)
    # claimed_at is NOT NULL but may still be empty; started/finished may be NULL or junk.
    insert_cron_execution(
        conn, "exec_blank", "job-null", "failed", claimed_at="", started_at=None, error=None
    )
    insert_cron_execution(
        conn,
        "exec_junk",
        "job-null",
        "completed",
        claimed_at="not-a-timestamp",
        started_at="also-junk",
        finished_at="",
    )
    insert_cron_execution(
        conn, "exec_ok", "job-null", "completed", claimed_at=iso_ago(60), started_at=iso_ago(59)
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "cron_executions" not in state.health.failed_sources
    stats = _stats_by_job(state)
    # Only the parseable row lands inside the 24h window.
    assert stats["job-null"].completed_24h == 1
    assert stats["job-null"].failed_24h == 0
    unparsed = next(row for row in state.cron_executions.recent if row.execution_id == "exec_junk")
    assert unparsed.started_age_seconds is None
    assert unparsed.duration_seconds is None


def test_collect_cron_executions_query_is_bounded(hermes_home: Path):
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    create_cron_executions_tables(conn)
    # More recent rows than the scan cap: the reader must not read the whole table.
    for index in range(600):
        insert_cron_execution(
            conn,
            f"exec_{index:04d}",
            "job-bulk",
            "completed",
            claimed_at=iso_ago(index),
            started_at=iso_ago(index),
            finished_at=iso_ago(index - 1),
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    stats = _stats_by_job(state)
    assert stats["job-bulk"].completed_24h == _EXECUTIONS_SCAN_LIMIT
    assert len(state.cron_executions.recent) == _EXECUTIONS_RECENT_LIMIT


def test_collect_cron_incidents_counts_open_and_unacked(
    hermes_home: Path, sample_cron_executions_db: Path
):
    _write_jobs_json(hermes_home, [{"id": "job-alpha", "name": "Alpha Report"}])

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    executions = state.cron_executions
    # 'detected' and 'alerted' are open; 'closed' is not.
    assert executions.open_incident_count == 2
    assert executions.unacked_incident_count == 1
    incidents = {incident.incident_id: incident for incident in executions.open_incidents}
    assert set(incidents) == {"inc_open_unacked", "inc_open_acked"}
    unacked = incidents["inc_open_unacked"]
    assert unacked.job_name == "Alpha Report"
    assert unacked.failure_type == "timeout"
    assert unacked.state == "detected"
    assert unacked.error_excerpt == "Script exited with code 1"
    assert unacked.first_seen_age_seconds == pytest.approx(7200, abs=30)
    assert unacked.last_seen_age_seconds == pytest.approx(600, abs=30)


def test_collect_cron_incidents_absent_table_reports_zero(hermes_home: Path):
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    # An older agent ships executions without the incidents table.
    conn.executescript(CRON_EXECUTIONS_SCHEMA.split("CREATE TABLE cron_incidents")[0])
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "cron_executions" not in state.health.failed_sources
    assert state.cron_executions.open_incident_count == 0
    assert state.cron_executions.unacked_incident_count == 0
    assert state.cron_executions.open_incidents == []


def test_collect_cron_reads_new_jobs_json_keys(hermes_home: Path):
    _write_jobs_json(
        hermes_home,
        [
            {
                "id": "job-alpha",
                "name": "Alpha Report",
                "failure_streak": "3",
                "paused_at": 1788792024.0,
                "paused_reason": "manual hold",
                "last_delivery_error": "telegram 429",
                "last_dispatch": {
                    "scheduled_at": "2026-09-07T22:29:36+08:00",
                    "dispatched_at": "2026-09-07T22:30:23+08:00",
                    "lateness_seconds": "46.8",
                    "kind": "late",
                },
                "repeat": {"times": 10, "completed": 4},
                "no_agent": True,
            }
        ],
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    job = state.cron.jobs[0]
    assert job.failure_streak == 3
    assert job.paused_reason == "manual hold"
    assert job.last_delivery_error == "telegram 429"
    assert job.dispatch_lateness_seconds == pytest.approx(46.8)
    assert job.dispatch_kind == "late"
    assert job.repeat_times == 10
    assert job.repeat_completed == 4
    assert job.no_agent is True


def test_collect_cron_new_jobs_json_keys_default_when_absent(hermes_home: Path):
    _write_jobs_json(hermes_home, [{"id": "job-old", "name": "Legacy", "repeat": None}])

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    job = state.cron.jobs[0]
    assert job.failure_streak == 0
    assert job.paused_reason == ""
    assert job.last_delivery_error == ""
    assert job.dispatch_lateness_seconds is None
    assert job.dispatch_kind == ""
    assert job.repeat_times is None
    assert job.repeat_completed == 0
    assert job.no_agent is False


@pytest.mark.parametrize(
    ("heartbeat_age", "last_success_age", "expected"),
    [
        (10.0, 30.0, CronTickerHealth.OK),
        (119.0, 599.0, CronTickerHealth.OK),
        (30.0, 900.0, CronTickerHealth.FAILING),
        (300.0, 30.0, CronTickerHealth.STALE),
    ],
)
def test_collect_cron_ticker_health(
    hermes_home: Path, heartbeat_age: float, last_success_age: float, expected
):
    now = 1_788_792_000.0
    (hermes_home / "cron" / "ticker_heartbeat").write_text(str(now - heartbeat_age))
    (hermes_home / "cron" / "ticker_last_success").write_text(str(now - last_success_age))

    c = Collector(hermes_home, clock=lambda: now)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.ticker_health == expected
    assert state.cron.ticker_heartbeat_age_seconds == pytest.approx(heartbeat_age)
    assert state.cron.ticker_last_success_age_seconds == pytest.approx(last_success_age)


def test_collect_cron_ticker_health_unknown_without_files(hermes_home: Path):
    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.ticker_health == CronTickerHealth.UNKNOWN
    assert state.cron.ticker_heartbeat_age_seconds is None
    assert state.cron.ticker_last_success_age_seconds is None


def test_collect_cron_ticker_ages_tolerate_garbage_and_clamp_to_zero(hermes_home: Path):
    now = 1_788_792_000.0
    # A clock skew ahead of the reader must never yield a negative age.
    (hermes_home / "cron" / "ticker_heartbeat").write_text(f"  {now + 45.0}\n")
    (hermes_home / "cron" / "ticker_last_success").write_text("not-an-epoch")

    c = Collector(hermes_home, clock=lambda: now)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.ticker_heartbeat_age_seconds == 0.0
    assert state.cron.ticker_last_success_age_seconds is None
    # Heartbeat fresh, last success unreadable: the ticker is running but not succeeding.
    assert state.cron.ticker_health == CronTickerHealth.FAILING


def test_collect_cron_executions_treats_naive_timestamps_as_utc(hermes_home: Path):
    """Older agents wrote claimed_at without an offset; those rows still count."""
    import datetime

    now = 1_788_792_000.0
    naive = datetime.datetime.fromtimestamp(now - 120, tz=datetime.UTC).replace(tzinfo=None)
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    create_cron_executions_tables(conn)
    insert_cron_execution(
        conn,
        "exec_naive",
        "job-naive",
        "completed",
        claimed_at=naive.isoformat(),
        started_at=naive.isoformat(),
        finished_at=(naive + datetime.timedelta(seconds=7)).isoformat(),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: now)
    try:
        state = c.collect()
    finally:
        c.close()

    stats = _stats_by_job(state)
    assert stats["job-naive"].completed_24h == 1
    assert stats["job-naive"].last_duration_seconds == pytest.approx(7.0)
    assert state.cron_executions.recent[0].started_age_seconds == pytest.approx(120.0)


def test_collect_cron_executions_incompatible_table_schema_is_not_a_failure(hermes_home: Path):
    """A future/older `executions` table without our columns degrades to empty."""
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE executions (id TEXT PRIMARY KEY, something_else TEXT)")
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "cron_executions" not in state.health.failed_sources
    assert state.cron_executions.db_present is True
    assert state.cron_executions.job_stats == []
    assert state.cron_executions.recent == []


def test_collect_cron_executions_blank_error_yields_no_excerpt(hermes_home: Path):
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    create_cron_executions_tables(conn)
    insert_cron_execution(
        conn,
        "exec_blank_error",
        "job-blank",
        "failed",
        claimed_at=iso_ago(30),
        started_at=iso_ago(29),
        finished_at=iso_ago(28),
        error="   \n\n\t\n",
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert _stats_by_job(state)["job-blank"].last_error_excerpt == ""
    assert state.cron_executions.recent[0].error_excerpt == ""


def test_collect_cron_paused_at_without_reason_marks_job_paused(hermes_home: Path):
    """paused_at alone is authoritative: hermes-agent may pause with a null reason."""
    _write_jobs_json(
        hermes_home,
        [{"id": "job-a", "name": "held", "paused_at": 1788792024.0, "paused_reason": None}],
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    job = state.cron.jobs[0]
    assert job.paused is True
    assert job.paused_reason == ""


def test_collect_cron_paused_at_iso_string_marks_job_paused(hermes_home: Path):
    _write_jobs_json(
        hermes_home, [{"id": "job-a", "name": "held", "paused_at": "2026-09-07T22:00:00+08:00"}]
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.jobs[0].paused is True


def test_collect_cron_paused_reason_without_paused_at_marks_job_paused(hermes_home: Path):
    _write_jobs_json(
        hermes_home,
        [{"id": "job-a", "name": "held", "paused_at": None, "paused_reason": "operator hold"}],
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    job = state.cron.jobs[0]
    assert job.paused is True
    assert job.paused_reason == "operator hold"


def test_collect_cron_running_job_is_not_paused(hermes_home: Path):
    _write_jobs_json(
        hermes_home,
        [{"id": "job-a", "name": "live", "paused_at": None, "paused_reason": None}],
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.jobs[0].paused is False


def test_collect_cron_blank_paused_at_string_is_not_paused(hermes_home: Path):
    _write_jobs_json(hermes_home, [{"id": "job-a", "name": "live", "paused_at": "  "}])

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.jobs[0].paused is False
