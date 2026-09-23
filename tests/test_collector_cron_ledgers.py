"""Cron side ledgers: usage audit, delivery queue, Bot Chat deferrals, recoveries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermesd.collect.cron as cron_module
from hermesd.collector import Collector
from tests.conftest import iso_ago


def _write_jobs_json(home: Path, jobs: list[dict]) -> None:
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}))


def _collect(home: Path):
    c = Collector(home)
    try:
        return c.collect()
    finally:
        c.close()


def _audit_line(job_id: str, age: float, **fields: object) -> str:
    """One ``cron/usage_audit.jsonl`` record as ``_FireAudit.write`` emits it
    (``cron/scheduler.py:2415-2433``)."""
    record = {
        "ts": iso_ago(age),
        "job_id": job_id,
        "fire_id": f"fire-{job_id}-{age}",
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "response_silent": False,
        "deliver_target": "local",
        "model": "grok-4.6",
        "duration_ms": 1500,
        "error": None,
    }
    record.update(fields)
    return json.dumps(record)


def test_usage_audit_rolls_up_tokens_per_job_over_24h_and_7d(hermes_home: Path):
    _write_jobs_json(hermes_home, [{"id": "job-a", "name": "Alpha"}])
    lines = [
        _audit_line("job-a", 8 * 86400, total_tokens=9999),  # outside both windows
        _audit_line("job-a", 3 * 86400, total_tokens=500, error="boom api_key=sk-abcdef123456"),
        _audit_line("job-a", 3600, total_tokens=None),  # unknown tokens still count a fire
        _audit_line("job-a", 60, total_tokens=120, model="gpt-9", duration_ms=2500),
        _audit_line("job-b", 120, total_tokens=7),
        "{torn line",
        "",
    ]
    (hermes_home / "cron" / "usage_audit.jsonl").write_text("\n".join(lines) + "\n")

    state = _collect(hermes_home)

    usage = state.cron_usage
    assert "cron_usage_audit" not in state.health.failed_sources
    assert usage.present is True
    assert usage.unparseable_lines == 1
    assert usage.window_truncated is False
    assert usage.tokens_24h == 127
    assert usage.tokens_7d == 627
    by_id = {job.job_id: job for job in usage.jobs}
    alpha = by_id["job-a"]
    assert alpha.job_name == "Alpha"
    assert (alpha.fires_24h, alpha.tokens_24h) == (2, 120)
    assert (alpha.fires_7d, alpha.tokens_7d, alpha.errors_7d) == (3, 620, 1)
    assert alpha.last_total_tokens == 120
    assert alpha.last_model == "gpt-9"
    assert alpha.last_duration_seconds == pytest.approx(2.5)
    assert alpha.last_fire_age_seconds == pytest.approx(60, abs=30)
    assert alpha.last_error_excerpt == ""
    # A job absent from jobs.json keeps its id as the name.
    assert by_id["job-b"].job_name == "job-b"
    # The biggest 7d consumer sorts first.
    assert usage.jobs[0].job_id == "job-a"


def test_usage_audit_last_error_is_redacted(hermes_home: Path):
    (hermes_home / "cron" / "usage_audit.jsonl").write_text(
        _audit_line("job-a", 60, error="provider 401 api_key=sk-abcdef1234567890\nmore") + "\n"
    )

    usage = _collect(hermes_home).cron_usage

    (job,) = usage.jobs
    assert job.last_error_excerpt.startswith("provider 401")
    assert "sk-abcdef" not in job.last_error_excerpt
    assert job.errors_7d == 1


def test_usage_audit_reads_only_a_bounded_tail(hermes_home: Path, monkeypatch):
    """The ledger is never pruned upstream: only the capped tail is parsed, and a
    cut that still lands inside the 7d window is reported as truncated."""
    monkeypatch.setattr(cron_module, "_USAGE_AUDIT_TAIL_BYTES", 600)
    lines = [_audit_line("job-a", 3600 + index) for index in range(20)]
    (hermes_home / "cron" / "usage_audit.jsonl").write_text("\n".join(lines) + "\n")

    usage = _collect(hermes_home).cron_usage

    (job,) = usage.jobs
    assert 0 < job.fires_7d < 20
    assert usage.window_truncated is True


def test_usage_audit_absent_reads_as_not_present(hermes_home: Path):
    state = _collect(hermes_home)
    assert state.cron_usage.present is False
    assert state.cron_usage.jobs == []
    assert "cron_usage_audit" not in state.health.failed_sources


def test_usage_audit_is_parsed_once_per_file_signature(hermes_home: Path, monkeypatch):
    path = hermes_home / "cron" / "usage_audit.jsonl"
    path.write_text(_audit_line("job-a", 60) + "\n")
    calls: list[Path] = []
    real = cron_module._read_tail_text

    def counting(p: Path, max_bytes: int) -> str:
        if p.name == "usage_audit.jsonl":
            calls.append(p)
        return real(p, max_bytes)

    monkeypatch.setattr(cron_module, "_read_tail_text", counting)
    c = Collector(hermes_home)
    try:
        c.collect()
        c.collect()
        assert len(calls) == 1
        with path.open("a") as handle:
            handle.write(_audit_line("job-a", 30) + "\n")
        state = c.collect()
    finally:
        c.close()
    assert len(calls) == 2
    assert state.cron_usage.jobs[0].fires_24h == 2


def test_usage_audit_symlink_escape_fails_the_source_and_keeps_last_good(
    hermes_home: Path, tmp_path: Path
):
    path = hermes_home / "cron" / "usage_audit.jsonl"
    path.write_text(_audit_line("job-a", 60) + "\n")
    c = Collector(hermes_home)
    try:
        first = c.collect()
        outside = tmp_path / "outside.jsonl"
        outside.write_text(_audit_line("job-z", 60) + "\n")
        path.unlink()
        path.symlink_to(outside)
        second = c.collect()
    finally:
        c.close()
    assert first.cron_usage.jobs[0].job_id == "job-a"
    assert "cron_usage_audit" in second.health.failed_sources
    assert second.cron_usage.jobs[0].job_id == "job-a"
