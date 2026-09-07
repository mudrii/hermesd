"""Collection of cron jobs, tick state, output excerpts, and cron log tails."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from hermesd.collector import (
    Collector,
    _delivery_target_label,
    _latest_cron_output_excerpt,
)
from hermesd.panels import render_panel
from hermesd.theme import Theme
from tests.conftest import (
    _count_opens,
    _skip_if_root,
    _unreadable,
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
