"""Cron side ledgers: usage audit, delivery queue, Bot Chat deferrals, recoveries."""

from __future__ import annotations

import json
import sqlite3
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


_DELIVERIES_SCHEMA = """
CREATE TABLE deliveries (
    execution_id TEXT PRIMARY KEY,
    job_json TEXT NOT NULL,
    content TEXT NOT NULL,
    for_failure INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN
      ('pending','delivering','delivered','failed','unknown','suppressed')),
    owner_process_id TEXT,
    owner_pid INTEGER,
    owner_started_at INTEGER,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE TABLE delivery_tombstones (
    execution_id TEXT PRIMARY KEY,
    terminal_status TEXT NOT NULL,
    finished_at TEXT
);
"""


def _deliveries_db(home: Path, rows: list[tuple]) -> None:
    """``cron/deliveries.db`` in upstream's schema (``cron/delivery_queue.py:108-128``)."""
    conn = sqlite3.connect(str(home / "cron" / "deliveries.db"))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript(_DELIVERIES_SCHEMA)
        conn.executemany(
            "INSERT INTO deliveries (execution_id, job_json, content, for_failure, status, "
            "created_at, finished_at, error) VALUES (?, '{}', 'secret body', ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_delivery_queue_counts_pending_and_recent_failures(hermes_home: Path):
    _deliveries_db(
        hermes_home,
        [
            ("exec-pending-old", 0, "pending", iso_ago(900), None, None),
            ("exec-pending-new", 0, "pending", iso_ago(60), None, None),
            ("exec-delivering", 1, "delivering", iso_ago(30), None, None),
            ("exec-ok", 0, "delivered", iso_ago(3600), iso_ago(3590), None),
            (
                "exec-failed",
                1,
                "failed",
                iso_ago(1800),
                iso_ago(1700),
                "telegram 401 token=abcdef1234567890abcdef\nretry later",
            ),
            ("exec-unknown", 0, "unknown", iso_ago(7200), iso_ago(7100), None),
            ("exec-failed-old", 0, "failed", iso_ago(3 * 86400), iso_ago(3 * 86400), "old"),
            ("exec-suppressed", 0, "suppressed", iso_ago(100), iso_ago(90), None),
        ],
    )

    state = _collect(hermes_home)

    queue = state.cron_deliveries
    assert "cron_deliveries" not in state.health.failed_sources
    assert queue.db_present is True
    assert queue.status_counts == {
        "delivered": 1,
        "delivering": 1,
        "failed": 2,
        "pending": 2,
        "suppressed": 1,
        "unknown": 1,
    }
    assert queue.pending_count == 3
    assert queue.oldest_pending_age_seconds == pytest.approx(900, abs=30)
    assert queue.failed_24h == 2
    assert [f.execution_id for f in queue.recent_failures] == [
        "exec-failed",
        "exec-unknown",
        "exec-failed-old",
    ]
    failed = queue.recent_failures[0]
    assert failed.status == "failed"
    assert failed.for_failure is True
    assert failed.finished_age_seconds == pytest.approx(1700, abs=30)
    assert failed.error_excerpt.startswith("telegram 401")
    assert "abcdef1234567890" not in failed.error_excerpt
    assert "retry later" not in failed.error_excerpt
    assert "secret body" not in queue.model_dump_json()


def test_delivery_queue_absent_reads_as_not_present(hermes_home: Path):
    state = _collect(hermes_home)
    assert state.cron_deliveries.db_present is False
    assert "cron_deliveries" not in state.health.failed_sources


def test_delivery_queue_without_table_reads_as_empty(hermes_home: Path):
    sqlite3.connect(str(hermes_home / "cron" / "deliveries.db")).close()
    state = _collect(hermes_home)
    assert state.cron_deliveries.db_present is True
    assert state.cron_deliveries.pending_count == 0


def test_delivery_queue_unsafe_path_fails_and_keeps_last_good(hermes_home: Path, tmp_path: Path):
    _deliveries_db(hermes_home, [("exec-p", 0, "pending", iso_ago(60), None, None)])
    c = Collector(hermes_home)
    try:
        first = c.collect()
        db = hermes_home / "cron" / "deliveries.db"
        moved = tmp_path / "elsewhere.db"
        db.rename(moved)
        db.symlink_to(moved)
        second = c.collect()
    finally:
        c.close()
    assert first.cron_deliveries.pending_count == 1
    assert "cron_deliveries" in second.health.failed_sources
    assert second.cron_deliveries.pending_count == 1
