"""Collection of cron jobs, tick state, output excerpts, and cron log tails."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import hermesd.collect.cron as cron_module
from hermesd.collect.common import _EXCERPT_MAX_CHARS
from hermesd.collect.cron import (
    _EXECUTIONS_RECENT_LIMIT,
    _INCIDENTS_LIMIT,
)
from hermesd.collect.logs import _MAX_LOG_LINE_CHARS
from hermesd.collector import (
    Collector,
    _delivery_target_label,
    _latest_cron_output_excerpt,
)
from hermesd.models import CronState, CronTickerHealth
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


def test_collect_cron_suggestions_from_a_directory_of_files(hermes_home: Path):
    """A suggestions/ directory counts its recognised files and skips the rest."""
    suggestions = hermes_home / "cron" / "suggestions"
    suggestions.mkdir()
    for name in ("a.json", "b.yaml", "c.yml", "d.md"):
        (suggestions / name).write_text("{}")
    (suggestions / "notes.txt").write_text("ignored")
    (suggestions / "nested").mkdir()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.suggestion_count == 4


def test_collect_cron_suggestions_symlinked_file_is_skipped(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "suggestions.json"
    outside.write_text(json.dumps({"suggestions": [{"name": "standup"}, {"name": "review"}]}))
    (hermes_home / "cron" / "suggestions.json").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.suggestion_count == 0
    assert "cron" not in state.health.failed_sources


def test_collect_cron_suggestions_mapping_without_a_list_counts_its_keys(hermes_home: Path):
    """A suggestions mapping with no suggestions/items/jobs list falls back to key count."""
    (hermes_home / "cron" / "suggestions.json").write_text(
        json.dumps({"standup": {"cron": "0 9 * * *"}, "review": {"cron": "0 18 * * *"}})
    )

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.suggestion_count == 2


def test_cron_output_scan_skips_files_whose_stat_fails(hermes_home: Path, monkeypatch):
    """A cron output file that disappears mid-scan must not fail the logs source."""
    job_dir = hermes_home / "cron" / "output" / "job-1"
    job_dir.mkdir(parents=True)
    vanishing = job_dir / "vanishing.log"
    vanishing.write_text("2026-04-09 15:41:58,123 - hermes - INFO - gone\n")
    survivor = job_dir / "survivor.log"
    survivor.write_text("2026-04-09 15:42:58,123 - hermes - INFO - cron ran\n")

    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object):
        if self == vanishing:
            raise OSError("stat raced with a rotation")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky_stat)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert "logs" not in state.health.failed_sources
    assert [line.message for line in state.logs.cron_lines] == [
        "2026-04-09 15:42:58,123 - hermes - INFO - cron ran"
    ]


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
    """An unstattable output file cannot be discovered, so the cached excerpt is kept.

    Both Path.stat and Path.is_file are patched: 3.14 routes is_file() through
    os.stat directly, so patching Path.stat alone no longer hides the file.
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

        real_stat = Path.stat

        def failing_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            if self == output_file:
                raise OSError("stat denied")
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        real_is_file = Path.is_file

        def failing_is_file(self: Path, *args: object, **kwargs: object) -> bool:
            if self == output_file:
                raise OSError("stat denied")
            return real_is_file(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", failing_stat)
        monkeypatch.setattr(Path, "is_file", failing_is_file)
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


# F07 — 'unknown' is a real upstream terminal status (cron/executions.py prunes
# status IN ('completed','failed','unknown')), and an unrecognized future value
# must not silently vanish from the window either.


def _write_permissive_executions_db(home: Path, rows: list[tuple[str, str, str]]) -> Path:
    """An executions table without the status CHECK, so future values can be tested."""
    db_path = home / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            "CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
            "source TEXT NOT NULL, process_id TEXT NOT NULL, pid INTEGER NOT NULL, "
            "process_started_at INTEGER, status TEXT, claimed_at TEXT NOT NULL, "
            "started_at TEXT, finished_at TEXT, error TEXT, "
            "handoff_pending INTEGER NOT NULL DEFAULT 0, handoff_started_at REAL)"
        )
        conn.executemany(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at) "
            "VALUES (?, ?, 'builtin', 'proc', 1, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_unknown_status_is_counted_and_not_folded_into_failed(hermes_home: Path):
    _write_permissive_executions_db(hermes_home, [("e1", "job-alpha", "unknown", iso_ago(600))])

    c = Collector(hermes_home)
    try:
        stats = _stats_by_job(c.collect())["job-alpha"]
    finally:
        c.close()

    assert stats.unknown_24h == 1
    assert stats.completed_24h == 0
    assert stats.failed_24h == 0
    assert stats.running_24h == 0


def test_window_counts_reconcile_against_the_total(hermes_home: Path):
    _write_permissive_executions_db(
        hermes_home,
        [
            ("e1", "job-alpha", "completed", iso_ago(100)),
            ("e2", "job-alpha", "failed", iso_ago(200)),
            ("e3", "job-alpha", "running", iso_ago(300)),
            ("e4", "job-alpha", "unknown", iso_ago(400)),
            ("e5", "job-alpha", "claimed", iso_ago(500)),
        ],
    )

    c = Collector(hermes_home)
    try:
        stats = _stats_by_job(c.collect())["job-alpha"]
    finally:
        c.close()

    assert stats.total_24h == 5
    assert stats.total_24h == (
        stats.completed_24h + stats.failed_24h + stats.running_24h + stats.unknown_24h
    )
    # 'claimed' is an in-flight status and belongs with running, not unknown.
    assert stats.running_24h == 2
    assert stats.unknown_24h == 1


def test_unrecognized_future_status_counts_as_unknown(hermes_home: Path):
    """A status hermesd has never heard of must be reported, not dropped."""
    _write_permissive_executions_db(
        hermes_home,
        [("e1", "job-alpha", "quarantined", iso_ago(100)), ("e2", "job-alpha", None, iso_ago(200))],
    )

    c = Collector(hermes_home)
    try:
        stats = _stats_by_job(c.collect())["job-alpha"]
    finally:
        c.close()

    assert stats.unknown_24h == 2
    assert stats.total_24h == 2


def test_sample_fixture_window_totals_reconcile(hermes_home: Path, sample_cron_executions_db: Path):
    c = Collector(hermes_home)
    try:
        stats = _stats_by_job(c.collect())
    finally:
        c.close()

    for entry in stats.values():
        assert entry.total_24h == (
            entry.completed_24h + entry.failed_24h + entry.running_24h + entry.unknown_24h
        )
    # The 3-day-old run is outside the window, so it is in neither total.
    assert stats["job-alpha"].total_24h == 4


# F06 — execution success and delivery outcome are different questions: a run can
# complete and still have its notification suppressed, so a completed counter
# must never be readable as "delivered".


def _write_delivery_db(home: Path, rows: list[tuple[str, str, str | None, int]]) -> Path:
    """Build an executions.db from (job_id, status, delivery_outcome, handoff_pending)."""
    db_path = home / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        create_cron_executions_tables(conn)
        for index, (job_id, status, outcome, handoff) in enumerate(rows):
            insert_cron_execution(
                conn,
                f"e{index}",
                job_id,
                status,
                claimed_at=iso_ago(600 + index),
                started_at=iso_ago(599 + index),
                finished_at=iso_ago(598 + index),
                delivery_outcome=outcome,
                handoff_pending=handoff,
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _collect_stats(home: Path) -> dict:
    c = Collector(home)
    try:
        return _stats_by_job(c.collect())
    finally:
        c.close()


def test_completed_with_suppressed_delivery_is_not_counted_as_delivered(hermes_home: Path):
    _write_delivery_db(hermes_home, [("job-a", "completed", "suppressed", 0)])

    stats = _collect_stats(hermes_home)["job-a"]

    assert stats.completed_24h == 1
    assert stats.delivery_tracked is True
    assert stats.delivery_outcomes_24h == {"suppressed": 1}
    assert "delivered" not in stats.delivery_outcomes_24h


def test_every_recorded_delivery_outcome_is_counted_verbatim(hermes_home: Path):
    _write_delivery_db(
        hermes_home,
        [
            ("job-a", "completed", "delivered", 0),
            ("job-a", "completed", "delivered", 0),
            ("job-a", "completed", "suppressed", 0),
            ("job-a", "completed", "suppressed_acked", 0),
            ("job-a", "completed", "queued", 0),
            ("job-a", "completed", "not_configured", 0),
            ("job-a", "failed", "failed", 0),
        ],
    )

    stats = _collect_stats(hermes_home)["job-a"]

    assert stats.delivery_outcomes_24h == {
        "delivered": 2,
        "suppressed": 1,
        "suppressed_acked": 1,
        "queued": 1,
        "not_configured": 1,
        "failed": 1,
    }


def test_unrecorded_delivery_is_counted_separately_from_recorded(hermes_home: Path):
    _write_delivery_db(
        hermes_home,
        [("job-a", "completed", None, 0), ("job-a", "completed", "delivered", 0)],
    )

    stats = _collect_stats(hermes_home)["job-a"]

    assert stats.delivery_unrecorded_24h == 1
    assert stats.delivery_outcomes_24h == {"delivered": 1}
    assert stats.total_24h == stats.delivery_unrecorded_24h + sum(
        stats.delivery_outcomes_24h.values()
    )


def test_unknown_delivery_outcome_is_preserved_not_bucketed(hermes_home: Path):
    _write_delivery_db(hermes_home, [("job-a", "completed", "teleported", 0)])

    assert _collect_stats(hermes_home)["job-a"].delivery_outcomes_24h == {"teleported": 1}


def test_pending_handoff_is_its_own_indicator(hermes_home: Path):
    _write_delivery_db(
        hermes_home,
        [("job-a", "completed", "delivered", 1), ("job-a", "completed", "delivered", 0)],
    )

    stats = _collect_stats(hermes_home)["job-a"]

    assert stats.handoff_pending_24h == 1
    assert stats.completed_24h == 2


def test_delivery_outcome_vocabulary_is_bounded(hermes_home: Path):
    rows = [("job-a", "completed", f"outcome-{index}", 0) for index in range(40)]
    _write_delivery_db(hermes_home, rows)

    stats = _collect_stats(hermes_home)["job-a"]

    assert len(stats.delivery_outcomes_24h) <= 8
    assert stats.total_24h == 40
    assert stats.delivery_unrecorded_24h == 0


def test_schema_without_delivery_columns_reports_untracked(hermes_home: Path):
    """An older agent has no delivery_outcome column: untracked, not all-unrecorded."""
    db_path = hermes_home / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(
            "CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
            "source TEXT NOT NULL, process_id TEXT NOT NULL, pid INTEGER NOT NULL, "
            "process_started_at INTEGER, status TEXT NOT NULL, claimed_at TEXT NOT NULL, "
            "started_at TEXT, finished_at TEXT, error TEXT)"
        )
        conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at) "
            "VALUES ('e1', 'job-a', 'builtin', 'proc', 1, 'completed', ?)",
            (iso_ago(600),),
        )
        conn.commit()
    finally:
        conn.close()

    stats = _collect_stats(hermes_home)["job-a"]

    assert stats.completed_24h == 1
    assert stats.delivery_tracked is False
    assert stats.delivery_outcomes_24h == {}
    assert stats.delivery_unrecorded_24h == 0
    assert stats.handoff_pending_24h == 0


def test_recent_execution_rows_carry_delivery_and_schedule(hermes_home: Path):
    db_path = hermes_home / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        create_cron_executions_tables(conn)
        insert_cron_execution(
            conn,
            "e1",
            "job-a",
            "completed",
            claimed_at=iso_ago(600),
            started_at=iso_ago(599),
            finished_at=iso_ago(590),
            delivery_outcome="suppressed",
            scheduled_instant=iso_ago(600),
            handoff_pending=1,
        )
        conn.commit()
    finally:
        conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    recent = state.cron_executions.recent[0]
    assert recent.delivery_outcome == "suppressed"
    assert recent.handoff_pending is True
    assert recent.scheduled_instant
    assert state.cron_executions.job_stats[0].last_status == "completed"


# F08 — upstream prunes terminal history to MAX_TERMINAL_EXECUTIONS records, so
# every aggregate describes *recorded* attempts. Reaching the cap does not prove
# a given 24h window is incomplete, and staying under it does not prove complete
# coverage either; the qualification has to say what is actually known.


def _write_history(home: Path, rows: list[tuple[str, str]], *, offset: float = 60.0) -> Path:
    db_path = home / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        create_cron_executions_tables(conn)
        for index, (job_id, status) in enumerate(rows):
            insert_cron_execution(
                conn,
                f"e{index}",
                job_id,
                status,
                claimed_at=iso_ago(offset + index),
                started_at=iso_ago(offset + index),
                finished_at=iso_ago(offset + index - 1) if status != "running" else None,
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _collect_executions(home: Path):
    c = Collector(home)
    try:
        return c.collect().cron_executions
    finally:
        c.close()


def test_retention_exposes_recorded_counts_and_observed_bounds(hermes_home: Path):
    _write_history(hermes_home, [("job-a", "completed")] * 3 + [("job-a", "failed")])

    executions = _collect_executions(hermes_home)

    assert executions.retained_total_count == 4
    assert executions.retained_terminal_count == 4
    assert executions.retention_cap == 1000
    assert executions.at_retention_cap is False
    assert executions.newest_claimed_age_seconds is not None
    assert executions.oldest_claimed_age_seconds is not None
    assert executions.oldest_claimed_age_seconds > executions.newest_claimed_age_seconds


def test_long_running_attempts_are_recorded_but_not_terminal(hermes_home: Path):
    """Upstream prunes only terminal rows, so an in-flight attempt is never capped out."""
    _write_history(
        hermes_home,
        [("job-a", "completed"), ("job-a", "running"), ("job-a", "claimed"), ("job-a", "unknown")],
    )

    executions = _collect_executions(hermes_home)

    assert executions.retained_total_count == 4
    assert executions.retained_terminal_count == 2


def test_history_at_the_retention_cap_is_flagged(hermes_home: Path):
    _write_history(hermes_home, [("job-a", "completed")] * 1000, offset=1.0)

    executions = _collect_executions(hermes_home)

    assert executions.retained_terminal_count == 1000
    assert executions.at_retention_cap is True


def test_history_just_below_the_retention_cap_is_not_flagged(hermes_home: Path):
    _write_history(hermes_home, [("job-a", "completed")] * 999, offset=1.0)

    executions = _collect_executions(hermes_home)

    assert executions.retained_terminal_count == 999
    assert executions.at_retention_cap is False


def test_absent_executions_db_reports_no_retention_evidence(hermes_home: Path):
    executions = _collect_executions(hermes_home)

    assert executions.db_present is False
    assert executions.retained_total_count == 0
    assert executions.retention_cap == 0
    assert executions.at_retention_cap is False
    assert executions.oldest_claimed_age_seconds is None


def test_retention_bounds_ignore_unparseable_timestamps(hermes_home: Path):
    db_path = hermes_home / "cron" / "executions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        create_cron_executions_tables(conn)
        insert_cron_execution(conn, "e1", "job-a", "completed", claimed_at="not-a-timestamp")
        insert_cron_execution(conn, "e2", "job-a", "completed", claimed_at=iso_ago(120))
        conn.commit()
    finally:
        conn.close()

    executions = _collect_executions(hermes_home)

    assert executions.retained_total_count == 2
    assert executions.newest_claimed_age_seconds == pytest.approx(120.0, abs=5.0)


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


@pytest.mark.parametrize("indexed", [False, True], ids=["unindexed", "indexed"])
def test_collect_cron_executions_counters_count_beyond_the_display_cap(
    hermes_home: Path, indexed: bool
):
    """600 in-window executions must report 600, not the 500-row display cap."""
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    create_cron_executions_tables(conn)
    if indexed:
        conn.execute("CREATE INDEX idx_executions_claimed ON executions (claimed_at)")
    # More recent rows than the scan cap: the detail list stays capped but the
    # 24h counters aggregate the full window, with or without an index.
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
    assert stats["job-bulk"].completed_24h == 600
    assert len(state.cron_executions.recent) == _EXECUTIONS_RECENT_LIMIT


def test_collect_cron_executions_busy_job_cannot_crowd_out_a_quiet_job(hermes_home: Path):
    """A chatty job's flood must not hide another job's counters or last run."""
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    create_cron_executions_tables(conn)
    # The 550 newest rows all belong to job-noisy, pushing every job-quiet row
    # past the display cap.
    for index in range(550):
        insert_cron_execution(
            conn,
            f"noisy_{index:04d}",
            "job-noisy",
            "completed",
            claimed_at=iso_ago(index),
            started_at=iso_ago(index),
            finished_at=iso_ago(index),
        )
    for index in range(5):
        insert_cron_execution(
            conn,
            f"quiet_{index}",
            "job-quiet",
            "failed",
            claimed_at=iso_ago(1000 + index * 60),
            started_at=iso_ago(1000 + index * 60),
            finished_at=iso_ago(990 + index * 60),
            error="quiet boom",
        )
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    stats = _stats_by_job(state)
    assert stats["job-noisy"].completed_24h == 550
    quiet = stats["job-quiet"]
    assert quiet.failed_24h == 5
    assert quiet.last_status == "failed"
    assert quiet.last_error_excerpt == "quiet boom"


def test_collect_cron_executions_window_boundary_is_inclusive(hermes_home: Path):
    """claimed_at exactly at now-24h counts; one second earlier does not."""
    now = 1_788_800_000.0
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    create_cron_executions_tables(conn)
    insert_cron_execution(
        conn,
        "exec_on_boundary",
        "job-edge",
        "completed",
        claimed_at=iso_ago(86400, now=now),
        started_at=iso_ago(86400, now=now),
        finished_at=iso_ago(86399, now=now),
    )
    insert_cron_execution(
        conn,
        "exec_just_outside",
        "job-edge",
        "completed",
        claimed_at=iso_ago(86401, now=now),
        started_at=iso_ago(86401, now=now),
        finished_at=iso_ago(86400, now=now),
    )
    conn.commit()
    conn.close()

    c = Collector(hermes_home, clock=lambda: now)
    try:
        state = c.collect()
    finally:
        c.close()

    stats = _stats_by_job(state)
    assert stats["job-edge"].completed_24h == 1
    # The boundary row is the newer one, so it is also the job's last run.
    assert stats["job-edge"].last_duration_seconds == pytest.approx(1.0)


def test_cron_execution_timestamps_are_compared_chronologically(hermes_home: Path):
    now = datetime(2026, 9, 12, 12, tzinfo=UTC).timestamp()
    db_path = hermes_home / "cron" / "executions.db"
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        create_cron_executions_tables(conn)
        for execution_id, status, claimed_at in [
            ("newer-offset", "failed", "2026-09-11T11:30:00-02:00"),
            ("older-utc", "completed", "2026-09-11T12:30:00+00:00"),
            ("outside", "running", "2026-09-11T12:30:00+02:00"),
            ("boundary-naive", "completed", "2026-09-11T12:00:00"),
            ("garbage", "failed", "not-a-timestamp"),
        ]:
            insert_cron_execution(
                conn, execution_id, "job", status, claimed_at=claimed_at, error=execution_id
            )
        conn.commit()
    collector = Collector(hermes_home, clock=lambda: now)
    try:
        state = collector.collect()
    finally:
        collector.close()

    stats = _stats_by_job(state)["job"]
    assert stats.completed_24h == 2
    assert stats.failed_24h == 1
    assert stats.running_24h == 0
    assert stats.last_status == "failed"
    assert stats.last_error_excerpt == "newer-offset"
    assert state.cron_executions.recent[0].execution_id == "newer-offset"


def _columns(conn) -> set[str]:
    """The executions column set the readers thread through their queries."""
    return cron_module._executions_columns(conn)


def test_cron_window_query_returns_aggregates_not_execution_history():
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        create_cron_executions_tables(conn)
        for index in range(600):
            insert_cron_execution(conn, str(index), "job", "completed", claimed_at=iso_ago(index))
        rows = cron_module._execution_window_rows(conn, now=time.time(), columns=_columns(conn))

    assert len(rows) == 1
    assert rows[0]["completed_24h"] == 600


def test_cron_last_run_uses_id_to_break_equal_instants():
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        create_cron_executions_tables(conn)
        for execution_id, claimed_at in [
            ("a", "2026-09-12T13:30:00+02:00"),
            ("b", "2026-09-12T11:30:00Z"),
        ]:
            insert_cron_execution(conn, execution_id, "job", "failed", claimed_at=claimed_at)
        rows = cron_module._last_execution_rows(conn, columns=_columns(conn))

    assert [row["id"] for row in rows] == ["b"]


def test_cron_schema_inspection_errors_propagate():
    """A denied PRAGMA must raise, not read as "every optional column is absent"."""
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        create_cron_executions_tables(conn)
        conn.set_authorizer(
            lambda action, *_: (
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            cron_module._executions_columns(conn)


def test_cron_window_aggregation_query_error_fails_source_and_keeps_last_good(
    hermes_home: Path, sample_cron_executions_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failing 24h-window aggregation read is a source failure, not zero counts."""
    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.cron_executions.job_stats
        assert "cron_executions" not in first.health.failed_sources

        real_query_rows = cron_module._query_rows

        def flaky_query_rows(conn, sql, *args):
            if "WHERE hermes_epoch(claimed_at) BETWEEN" in sql:
                raise sqlite3.OperationalError("simulated window aggregation failure")
            return real_query_rows(conn, sql, *args)

        monkeypatch.setattr(cron_module, "_query_rows", flaky_query_rows)
        second = c.collect()
        assert "cron_executions" in second.health.failed_sources
        assert second.cron_executions == first.cron_executions

        monkeypatch.setattr(cron_module, "_query_rows", real_query_rows)
        third = c.collect()
        assert "cron_executions" not in third.health.failed_sources
        assert third.cron_executions.job_stats
    finally:
        c.close()


def test_job_execution_stats_skips_out_of_window_and_garbage_rows():
    """Direct counter unit: garbage stamps and out-of-window rows never reach the
    24h counters, even without a last-run row — but an ``unknown`` status does,
    because it is a real terminal outcome rather than an unparseable one."""
    now = 1_788_800_000.0
    with contextlib.closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        create_cron_executions_tables(conn)
        for index, (status, claimed_at) in enumerate(
            [
                ("completed", iso_ago(60, now=now)),
                ("completed", iso_ago(2 * 86400, now=now)),
                ("failed", "not-a-timestamp"),
                ("unknown", iso_ago(30, now=now)),
                ("failed", iso_ago(-30, now=now)),
            ]
        ):
            insert_cron_execution(conn, str(index), "job-x", status, claimed_at=claimed_at)
        window_rows = cron_module._execution_window_rows(conn, now=now, columns=_columns(conn))
    stats = cron_module._job_execution_stats(window_rows, [], [], delivery_tracked=True)

    assert len(stats) == 1
    entry = stats[0]
    assert entry.job_id == "job-x"
    assert entry.completed_24h == 1
    assert entry.failed_24h == 0
    assert entry.running_24h == 0
    # An 'unknown' status is a real terminal outcome and must be counted, not
    # dropped; only the garbage stamp and the out-of-window rows are excluded.
    assert entry.unknown_24h == 1
    assert entry.total_24h == 2
    assert entry.last_status == ""


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


def test_collect_cron_incidents_detail_list_is_capped_but_counts_stay_exact(hermes_home: Path):
    """More open incidents than the detail cap: the list truncates, the counts do not."""
    total_open = _INCIDENTS_LIMIT + 4
    db_path = hermes_home / "cron" / "executions.db"
    conn = sqlite3.connect(str(db_path))
    try:
        create_cron_executions_tables(conn)
        for index in range(total_open):
            conn.execute(
                "INSERT INTO cron_incidents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"inc_{index:02d}",
                    "job-alpha",
                    f"sig-{index}",
                    "detected",
                    "timeout",
                    iso_ago(9000 - index),
                    iso_ago(total_open - index),
                    None,
                    None,
                    f"incident {index} exploded",
                    None,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    _write_jobs_json(hermes_home, [{"id": "job-alpha", "name": "Alpha Report"}])

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    executions = state.cron_executions
    assert executions.open_incident_count == total_open
    assert executions.unacked_incident_count == total_open
    assert len(executions.open_incidents) == _INCIDENTS_LIMIT
    # The newest last_seen_at rows win the cap.
    assert [incident.incident_id for incident in executions.open_incidents] == [
        f"inc_{index:02d}" for index in range(total_open - 1, total_open - 1 - _INCIDENTS_LIMIT, -1)
    ]


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


# --------------------------------------------------------------------------
# Cron path containment and output-line bounds
# --------------------------------------------------------------------------


def test_collect_cron_output_truncates_a_giant_line(hermes_home: Path):
    """A single unbounded line would be redacted and scanned whole every refresh."""
    job_dir = hermes_home / "cron" / "output" / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "latest.md").write_text("z" * 20_000 + "\n")

    c = Collector(hermes_home, log_tail_bytes=64 * 1024)
    try:
        state = c.collect()
    finally:
        c.close()

    assert len(state.logs.cron_lines) == 1
    assert len(state.logs.cron_lines[0].message) == _MAX_LOG_LINE_CHARS


def test_collect_cron_executions_ignores_a_symlinked_cron_directory(
    hermes_home: Path, tmp_path: Path
):
    """Only checking db_path.is_symlink() misses a symlinked cron/ directory."""
    outside_cron = tmp_path / "outside-cron"
    outside_cron.mkdir()
    conn = sqlite3.connect(str(outside_cron / "executions.db"))
    create_cron_executions_tables(conn)
    insert_cron_execution(conn, "e1", "job-1", "completed", claimed_at=iso_ago(60))
    conn.commit()
    conn.close()
    cron_dir = hermes_home / "cron"
    shutil.rmtree(cron_dir)
    cron_dir.symlink_to(outside_cron, target_is_directory=True)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron_executions.db_present is False
    assert state.cron_executions.recent == []
    assert "cron_executions" not in state.health.failed_sources


def test_symlinked_executions_db_after_a_good_read_keeps_last_good(
    hermes_home: Path, sample_cron_executions_db: Path, tmp_path: Path
):
    outside_db = tmp_path / "outside-executions.db"
    conn = sqlite3.connect(str(outside_db))
    create_cron_executions_tables(conn)
    conn.commit()
    conn.close()

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.cron_executions.db_present is True
        assert first.cron_executions.recent

        sample_cron_executions_db.unlink()
        sample_cron_executions_db.symlink_to(outside_db)
        second = c.collect()
    finally:
        c.close()

    assert second.cron_executions == first.cron_executions
    assert "cron_executions" in second.health.failed_sources


def test_cron_executions_query_error_fails_source_and_keeps_last_good(
    hermes_home: Path, sample_cron_executions_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failing executions read is a source failure, not an empty history."""
    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.cron_executions.recent
        assert "cron_executions" not in first.health.failed_sources

        real_query_rows = cron_module._query_rows

        def flaky_query_rows(conn, sql, *args):
            if "FROM executions ORDER BY" in sql:
                raise sqlite3.OperationalError("simulated executions read failure")
            return real_query_rows(conn, sql, *args)

        monkeypatch.setattr(cron_module, "_query_rows", flaky_query_rows)
        second = c.collect()
        assert "cron_executions" in second.health.failed_sources
        assert second.cron_executions == first.cron_executions

        monkeypatch.setattr(cron_module, "_query_rows", real_query_rows)
        third = c.collect()
        assert "cron_executions" not in third.health.failed_sources
        assert third.cron_executions.recent
    finally:
        c.close()


def test_cron_incidents_query_error_fails_source_and_keeps_last_good(
    hermes_home: Path, sample_cron_executions_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failing cron_incidents read is a source failure, not an empty list."""
    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.cron_executions.open_incidents
        assert "cron_executions" not in first.health.failed_sources

        real_query_rows = cron_module._query_rows

        def flaky_query_rows(conn, sql, *args):
            if "FROM cron_incidents" in sql and "ORDER BY" in sql:
                raise sqlite3.OperationalError("simulated incidents read failure")
            return real_query_rows(conn, sql, *args)

        monkeypatch.setattr(cron_module, "_query_rows", flaky_query_rows)
        second = c.collect()
        assert "cron_executions" in second.health.failed_sources
        assert second.cron_executions == first.cron_executions

        monkeypatch.setattr(cron_module, "_query_rows", real_query_rows)
        third = c.collect()
        assert "cron_executions" not in third.health.failed_sources
        assert third.cron_executions.open_incidents
    finally:
        c.close()


def test_cron_ticker_stamps_ignore_symlinks_outside_home(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "ticker_heartbeat"
    outside.write_text(str(time.time()))
    (hermes_home / "cron" / "ticker_heartbeat").symlink_to(outside)

    c = Collector(hermes_home)
    try:
        state = c.collect()
    finally:
        c.close()

    assert state.cron.ticker_heartbeat_age_seconds is None
    assert state.cron.ticker_health is CronTickerHealth.UNKNOWN


# --- cron/catch_up_occurrences and cron/ticker_last_error ---------------------
#
# Both markers are written by upstream's best-effort ``_write_marker``
# (cron/jobs.py:1129-1136, every exception swallowed) and both live in the
# profile-local cron store (``_current_cron_store()``, cron/jobs.py:119-133).
# Absence therefore proves nothing, and these tests pin that hermesd reports it
# as absence rather than as a zero or as a clean bill of health.

_FIXED_NOW = 1_788_792_000.0


def _collect_cron_at(home: Path, now: float = _FIXED_NOW) -> CronState:
    c = Collector(home, clock=lambda: now)
    try:
        return c.collect().cron
    finally:
        c.close()


def test_collect_cron_reports_an_absent_catch_up_counter_as_unobserved(hermes_home: Path):
    """No marker is not a zero: upstream's reader collapses the two.

    ``get_catch_up_occurrence_count`` returns 0 for a missing file and for a real
    zero alike (cron/jobs.py:1186-1193), and ``record_catch_up_occurrence`` is
    never called at all when catch-up is disabled (cron/jobs.py:2909-2918).
    """
    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_occurrences == 0
    assert cron.catch_up_occurrences_recorded is False
    assert cron.has_catch_up_occurrences is False


def test_collect_cron_distinguishes_a_recorded_zero_catch_up_count(hermes_home: Path):
    (hermes_home / "cron" / "catch_up_occurrences").write_text("0\n")

    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_occurrences == 0
    assert cron.catch_up_occurrences_recorded is True
    assert cron.has_catch_up_occurrences is False


def test_collect_cron_reads_a_nonzero_catch_up_counter(hermes_home: Path):
    (hermes_home / "cron" / "catch_up_occurrences").write_text("  17 \n")

    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_occurrences == 17
    assert cron.catch_up_occurrences_recorded is True
    assert cron.has_catch_up_occurrences is True


def test_collect_cron_catch_up_counter_clamps_a_negative_value(hermes_home: Path):
    """Upstream clamps with ``max(0, ...)``; a negative marker still counts as observed."""
    (hermes_home / "cron" / "catch_up_occurrences").write_text("-4")

    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_occurrences == 0
    assert cron.catch_up_occurrences_recorded is True


@pytest.mark.parametrize("payload", ["", "   \n", "not-a-number", "12.5", "{}"])
def test_collect_cron_unreadable_catch_up_counter_is_not_a_recorded_zero(
    hermes_home: Path, payload: str
):
    """A marker that yields no count reads as unobserved, never as `0 recorded`."""
    (hermes_home / "cron" / "catch_up_occurrences").write_text(payload)

    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_occurrences == 0
    assert cron.catch_up_occurrences_recorded is False


def test_collect_cron_catch_up_marker_ignores_symlinks_outside_home(
    hermes_home: Path, tmp_path: Path
):
    outside = tmp_path / "catch_up_occurrences"
    outside.write_text("99")
    (hermes_home / "cron" / "catch_up_occurrences").symlink_to(outside)

    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_occurrences == 0
    assert cron.catch_up_occurrences_recorded is False


@pytest.mark.parametrize(
    ("configured", "expected_enabled", "expected_set"),
    [
        pytest.param(True, True, True, id="explicit-true"),
        pytest.param(False, False, True, id="explicit-false"),
        # Upstream's cast is an identity check — ``lambda value: value is not
        # False`` (cron/jobs.py:2908 + :2615-2624) — so every falsy value that is
        # not the literal False leaves catch-up ENABLED. A naive ``bool(value)``
        # reports each of these as disabled, which is the opposite of what
        # hermes-agent does.
        pytest.param(None, True, True, id="yaml-null"),
        pytest.param(0, True, True, id="zero"),
        pytest.param("no", True, True, id="quoted-string-no"),
        pytest.param("", True, True, id="empty-string"),
        pytest.param([], True, True, id="empty-list"),
    ],
)
def test_collect_cron_catch_up_policy_mirrors_upstream_identity_cast(
    hermes_home: Path,
    configured: object,
    expected_enabled: bool,
    expected_set: bool,
):
    (hermes_home / "config.yaml").write_text(yaml.dump({"cron": {"catch_up_missed": configured}}))

    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_missed is expected_enabled
    assert cron.catch_up_missed_set is expected_set
    assert cron.catch_up_missed_disabled is (not expected_enabled)


def test_collect_cron_catch_up_policy_defaults_to_enabled_when_the_key_is_absent(
    hermes_home: Path,
):
    """A config.yaml with a cron block but no catch_up_missed key: default True."""
    (hermes_home / "config.yaml").write_text(yaml.dump({"cron": {"max_parallel_jobs": 5}}))

    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_missed is True
    assert cron.catch_up_missed_set is False
    assert cron.catch_up_missed_disabled is False


def test_collect_cron_catch_up_policy_defaults_to_enabled_without_any_config(
    hermes_home: Path,
):
    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_missed is True
    assert cron.catch_up_missed_set is False


def test_collect_cron_unquoted_yaml_no_disables_catch_up(hermes_home: Path):
    """An *unquoted* `no` is a literal False after PyYAML's YAML 1.1 resolution.

    What disables catch-up is the parsed Python value, not the spelling: upstream
    reads the same file through ``load_config``, so ``catch_up_missed: no`` and
    ``catch_up_missed: false`` are the same setting while ``catch_up_missed: 'no'``
    is not.
    """
    (hermes_home / "config.yaml").write_text("cron:\n  catch_up_missed: no\n")

    cron = _collect_cron_at(hermes_home)

    assert cron.catch_up_missed is False
    assert cron.catch_up_missed_set is True
    assert cron.catch_up_missed_disabled is True


def test_collect_cron_reads_ticker_last_error_message_and_age(hermes_home: Path):
    (hermes_home / "cron" / "ticker_last_error").write_text(
        f"{_FIXED_NOW - 45.5}\nRuntimeError: dispatch blew up\n"
    )

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_last_error == "RuntimeError: dispatch blew up"
    assert cron.ticker_last_error_age_seconds == pytest.approx(45.5)
    assert cron.ticker_error_recorded is True


def test_collect_cron_ticker_error_absent_by_default(hermes_home: Path):
    """``clear_ticker_error`` unlinks the marker on the next clean tick."""
    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_last_error == ""
    assert cron.ticker_last_error_age_seconds is None
    assert cron.ticker_error_recorded is False


def test_collect_cron_ticker_error_ignores_a_torn_single_line_marker(hermes_home: Path):
    """Upstream refuses fewer than two lines (cron/jobs.py:1218-1219); mirror it."""
    (hermes_home / "cron" / "ticker_last_error").write_text(f"{_FIXED_NOW}\n")

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_last_error == ""
    assert cron.ticker_last_error_age_seconds is None
    assert cron.ticker_error_recorded is False


def test_collect_cron_ticker_error_with_a_blank_message_reads_as_absent(hermes_home: Path):
    """``record_ticker_error("")`` writes a stamp and nothing else; upstream -> None."""
    (hermes_home / "cron" / "ticker_last_error").write_text(f"{_FIXED_NOW}\n\n")

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_last_error == ""
    assert cron.ticker_error_recorded is False


def test_collect_cron_ticker_error_without_a_parseable_stamp_keeps_the_message(
    hermes_home: Path,
):
    (hermes_home / "cron" / "ticker_last_error").write_text("not-an-epoch\nValueError: bad\n")

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_last_error == "ValueError: bad"
    assert cron.ticker_last_error_age_seconds is None
    assert cron.ticker_error_recorded is True


def test_collect_cron_ticker_error_clamps_a_future_stamp_to_zero(hermes_home: Path):
    (hermes_home / "cron" / "ticker_last_error").write_text(
        f"{_FIXED_NOW + 30.0}\nRuntimeError: clock skew\n"
    )

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_last_error_age_seconds == 0.0


def test_collect_cron_ticker_error_is_redacted_and_bounded(hermes_home: Path):
    """The message is an arbitrary exception string that can carry credentials."""
    secret = "https://user:super-secret-token@hooks.test/notify?key=abc123"
    (hermes_home / "cron" / "ticker_last_error").write_text(
        f"{_FIXED_NOW - 5}\nRuntimeError: POST {secret} failed " + "x" * 400 + "\n"
    )

    cron = _collect_cron_at(hermes_home)

    assert "super-secret-token" not in cron.ticker_last_error
    assert "abc123" not in cron.ticker_last_error
    assert "[REDACTED]" in cron.ticker_last_error
    assert len(cron.ticker_last_error) <= _EXCERPT_MAX_CHARS


def test_collect_cron_ticker_error_uses_the_first_non_blank_message_line(hermes_home: Path):
    (hermes_home / "cron" / "ticker_last_error").write_text(
        f"{_FIXED_NOW}\n\nOSError: store locked\nTraceback follows\n"
    )

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_last_error == "OSError: store locked"


def test_cron_ticker_error_marker_ignores_symlinks_outside_home(hermes_home: Path, tmp_path: Path):
    outside = tmp_path / "ticker_last_error"
    outside.write_text(f"{_FIXED_NOW}\nRuntimeError: from outside\n")
    (hermes_home / "cron" / "ticker_last_error").symlink_to(outside)

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_last_error == ""
    assert cron.ticker_error_recorded is False


def test_collect_cron_recorded_ticker_error_escalates_an_otherwise_ok_ticker(
    hermes_home: Path,
):
    """An un-cleared error marker means the last tick failed, even inside 600s.

    ``record_ticker_error`` is followed by ``record_ticker_heartbeat(success=ok)``
    and the marker is unlinked only when ``ok`` (cron/scheduler_provider.py:438,447),
    so a recorded error and a fresh last-success stamp cannot both be current —
    but hermesd's 600s staleness window is 3x looser than upstream's ~200s, so
    without this the panel would read `ok` next to a failure it is displaying.
    """
    (hermes_home / "cron" / "ticker_heartbeat").write_text(str(_FIXED_NOW - 10.0))
    (hermes_home / "cron" / "ticker_last_success").write_text(str(_FIXED_NOW - 20.0))
    (hermes_home / "cron" / "ticker_last_error").write_text(
        f"{_FIXED_NOW - 20.0}\nRuntimeError: boom\n"
    )

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_health is CronTickerHealth.FAILING


def test_collect_cron_recorded_ticker_error_does_not_invent_a_heartbeat(
    hermes_home: Path,
):
    """No heartbeat means hermesd cannot claim the loop is running at all."""
    (hermes_home / "cron" / "ticker_last_error").write_text(
        f"{_FIXED_NOW - 20.0}\nRuntimeError: boom\n"
    )

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_health is CronTickerHealth.UNKNOWN
    assert cron.ticker_error_recorded is True


def test_collect_cron_stale_ticker_is_not_downgraded_by_a_recorded_error(
    hermes_home: Path,
):
    (hermes_home / "cron" / "ticker_heartbeat").write_text(str(_FIXED_NOW - 300.0))
    (hermes_home / "cron" / "ticker_last_error").write_text(
        f"{_FIXED_NOW - 20.0}\nRuntimeError: boom\n"
    )

    cron = _collect_cron_at(hermes_home)

    assert cron.ticker_health is CronTickerHealth.STALE


@pytest.mark.parametrize("marker_name", ["catch_up_occurrences", "ticker_last_error"])
def test_cron_marker_open_failure_keeps_last_good_and_fails_the_source(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch, marker_name: str
):
    """A marker that fails after stat must fail `cron` and keep its last good read."""
    (hermes_home / "cron" / "catch_up_occurrences").write_text("3")
    (hermes_home / "cron" / "ticker_last_error").write_text(
        f"{_FIXED_NOW - 5}\nRuntimeError: boom\n"
    )
    c = Collector(hermes_home, clock=lambda: _FIXED_NOW)
    try:
        first = c.collect()
        assert first.cron.catch_up_occurrences == 3
        assert first.cron.ticker_last_error == "RuntimeError: boom"
        assert "cron" not in first.health.failed_sources

        blocked_path = hermes_home / "cron" / marker_name
        blocked_path.stat()
        real_open = Path.open

        def fail_marker_open(self: Path, *args: object, **kwargs: object):
            if self == blocked_path:
                raise PermissionError("marker read failed after stat")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_marker_open)
        second = c.collect()

        assert "cron" in second.health.failed_sources
        assert second.cron.catch_up_occurrences == 3
        assert second.cron.catch_up_occurrences_recorded is True
        assert second.cron.ticker_last_error == "RuntimeError: boom"

        monkeypatch.setattr(Path, "open", real_open)
        third = c.collect()

        assert "cron" not in third.health.failed_sources
        assert third.cron.catch_up_occurrences == 3
    finally:
        c.close()


def test_cron_catch_up_markers_are_read_from_the_root_store_under_a_profile(
    hermes_home: Path,
):
    """These markers keep the `cron` source's ROOT resolver (source-ownership.md).

    Upstream writes them profile-locally, so under ``--profile`` a profile's own
    ticker failure and catch-up counter are invisible — exactly as the rest of
    ``cron/`` already is. Read them from the profile store instead and the
    ownership table's `cron` row would be wrong for these two paths only.
    """
    profile_dir = hermes_home / "profiles" / "dev" / "cron"
    profile_dir.mkdir(parents=True)
    (profile_dir / "catch_up_occurrences").write_text("9")
    (profile_dir / "ticker_last_error").write_text(f"{_FIXED_NOW}\nRuntimeError: profile\n")
    (hermes_home / "cron" / "catch_up_occurrences").write_text("2")

    c = Collector(hermes_home, profile_name="dev", clock=lambda: _FIXED_NOW)
    try:
        cron = c.collect().cron
    finally:
        c.close()

    assert cron.catch_up_occurrences == 2
    assert cron.ticker_last_error == ""


@_skip_if_root
def test_cron_marker_readers_propagate_a_permission_error(tmp_path: Path):
    """An untraversable cron store must raise, not read as "no markers".

    ``_read_text_capped`` swallows the *open* failure, so a store hermesd cannot
    traverse would otherwise report a healthy absence — "no catch-up counter, no
    ticker error" — which is a claim about scheduling health with no evidence
    behind it. On Python 3.11 the ``is_symlink()`` checks raise first; the
    ``_exists_strict`` in ``_cron_marker_present`` is what keeps that true on
    3.14, where ``Path.exists()``/``is_symlink()`` swallow EACCES.
    """
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir()
    (cron_dir / "catch_up_occurrences").write_text("4")
    (cron_dir / "ticker_last_error").write_text(f"{_FIXED_NOW}\nRuntimeError: boom\n")

    assert cron_module._cron_catch_up_occurrences(cron_dir, tmp_path) == (4, True)
    assert cron_module._cron_ticker_last_error(cron_dir, now=_FIXED_NOW, root=tmp_path)[0]

    os.chmod(cron_dir, 0o000)
    try:
        if not _unreadable(cron_dir / "catch_up_occurrences"):
            pytest.skip("filesystem allowed traversal despite chmod 000")
        with pytest.raises(PermissionError):
            cron_module._cron_catch_up_occurrences(cron_dir, tmp_path)
        with pytest.raises(PermissionError):
            cron_module._cron_ticker_last_error(cron_dir, now=_FIXED_NOW, root=tmp_path)
    finally:
        os.chmod(cron_dir, 0o755)
