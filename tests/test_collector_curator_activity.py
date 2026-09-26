"""Skills Hub inventory and curator activity files under ``skills/``.

Upstream writers: ``skills/.hub/lock.json`` and ``skills/.hub/quarantine/``
(``tools/skills_hub.py:59-62,295-344``), ``skills/.curator_suppressed``
(``tools/skill_usage.py:195-207``), ``skills/.curator_ledger.jsonl``
(``tools/skill_ledger.py:83-84,298-312``), ``skills/.locks/curator-run``
(``agent/curator.py:1116-1141``) and ``skills/.curator_state``
``last_run_duration_seconds`` (``agent/curator.py:45,949``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from hermesd.collector import Collector

_NOW = 1_800_000_000.0


def _collect(hermes_home: Path, *, pid_exists=None):
    c = Collector(hermes_home, clock=lambda: _NOW, pid_exists=pid_exists or (lambda pid: True))
    try:
        return c.collect()
    finally:
        c.close()


def _hub(hermes_home: Path) -> Path:
    hub = hermes_home / "skills" / ".hub"
    hub.mkdir(parents=True, exist_ok=True)
    return hub


def test_skills_hub_counts_installed_and_quarantined(hermes_home: Path):
    hub = _hub(hermes_home)
    (hub / "lock.json").write_text(
        json.dumps({"version": 1, "installed": {"a": {"source": "x"}, "b": {}}})
    )
    quarantine = hub / "quarantine"
    quarantine.mkdir()
    (quarantine / "suspect-skill").mkdir()
    (quarantine / "stray.txt").write_text("not a skill dir")

    state = _collect(hermes_home)

    sm = state.skills_memory
    assert sm.hub_lock_present is True
    assert sm.hub_installed_count == 2
    assert sm.hub_quarantine_count == 1
    assert "skills_hub" not in state.health.failed_sources


def test_skills_hub_absent_reads_as_empty(hermes_home: Path):
    state = _collect(hermes_home)
    assert state.skills_memory.hub_lock_present is False
    assert state.skills_memory.hub_installed_count == 0
    assert state.skills_memory.hub_quarantine_count == 0
    assert "skills_hub" not in state.health.failed_sources


def test_skills_hub_keeps_last_good_when_lock_turns_corrupt(hermes_home: Path):
    hub = _hub(hermes_home)
    lock = hub / "lock.json"
    lock.write_text(json.dumps({"installed": {"a": {}, "b": {}, "c": {}}}))
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        lock.write_text("{not json")
        second = c.collect()
    finally:
        c.close()
    assert first.skills_memory.hub_installed_count == 3
    assert "skills_hub" in second.health.failed_sources
    assert second.skills_memory.hub_installed_count == 3


def test_skills_hub_refuses_symlinked_quarantine(hermes_home: Path, tmp_path: Path):
    hub = _hub(hermes_home)
    outside = tmp_path / "outside"
    (outside / "a").mkdir(parents=True)
    (hub / "quarantine").symlink_to(outside)
    state = _collect(hermes_home)
    assert "skills_hub" in state.health.failed_sources
    assert state.skills_memory.hub_quarantine_count == 0


def test_curator_state_reports_last_run_duration(hermes_home: Path):
    (hermes_home / "skills" / ".curator_state").write_text(
        json.dumps({"run_count": 3, "last_run_duration_seconds": 3.2})
    )
    state = _collect(hermes_home)
    assert state.curator.last_run_duration_seconds == 3.2


def test_curator_activity_counts_suppressed_and_tails_ledger(hermes_home: Path):
    skills = hermes_home / "skills"
    (skills / ".curator_suppressed").write_text("pdf\n\ndocx\nxlsx\n")
    rows = [
        {
            "id": f"id{index}",
            "ts": f"2026-09-16T20:34:4{index}+00:00",
            "actor": "curator",
            "action": "archive",
            "skill": f"skill-{index}",
            "evidence": {},
            "before": [{"path": "/x", "sha256": "0" * 64}],
        }
        for index in range(8)
    ]
    rows.append({"ts": "2026-09-16T20:35:00+00:00", "actor": "user", "action": "restore"})
    lines = [json.dumps(row) for row in rows]
    lines.insert(3, "{torn line")
    (skills / ".curator_ledger.jsonl").write_text("\n".join(lines) + "\n")

    state = _collect(hermes_home)

    cur = state.curator
    assert cur.suppressed_count == 3
    assert cur.ledger_present is True
    # Newest first, bounded; a row without a skill still renders its action.
    assert [(row.actor, row.action, row.skill) for row in cur.ledger_recent] == [
        ("user", "restore", ""),
        ("curator", "archive", "skill-7"),
        ("curator", "archive", "skill-6"),
        ("curator", "archive", "skill-5"),
        ("curator", "archive", "skill-4"),
    ]
    assert cur.ledger_recent[1].ts == "2026-09-16T20:34:47+00:00"
    assert "curator_activity" not in state.health.failed_sources
    # Manifests and hashes never reach the model.
    assert "sha256" not in state.model_dump_json()


def test_curator_activity_reads_only_the_ledger_tail(hermes_home: Path):
    skills = hermes_home / "skills"
    padding = {"actor": "curator", "action": "patch", "skill": "old", "blob": "x" * 200_000}
    newest = {"actor": "agent", "action": "create", "skill": "fresh"}
    (skills / ".curator_ledger.jsonl").write_text(
        json.dumps(padding) + "\n" + json.dumps(newest) + "\n"
    )
    state = _collect(hermes_home)
    assert [row.skill for row in state.curator.ledger_recent] == ["fresh"]


def test_curator_run_claim_live_when_pid_alive_and_fresh(hermes_home: Path):
    locks = hermes_home / "skills" / ".locks"
    locks.mkdir(parents=True)
    claim = locks / "curator-run"
    claim.write_text("4242")
    os.utime(claim, (_NOW - 120, _NOW - 120))

    state = _collect(hermes_home, pid_exists=lambda pid: pid == 4242)

    cur = state.curator
    assert cur.run_claim_present is True
    assert cur.run_claim_pid == 4242
    assert cur.run_claim_age_seconds == 120
    assert cur.run_claim_live is True


def test_curator_run_claim_stale_after_an_hour_or_dead_pid(hermes_home: Path):
    locks = hermes_home / "skills" / ".locks"
    locks.mkdir(parents=True)
    claim = locks / "curator-run"
    claim.write_text("4242")
    os.utime(claim, (_NOW - 3601, _NOW - 3601))
    stale = _collect(hermes_home)
    assert stale.curator.run_claim_present is True
    assert stale.curator.run_claim_live is False

    os.utime(claim, (_NOW - 10, _NOW - 10))
    dead = _collect(hermes_home, pid_exists=lambda pid: False)
    assert dead.curator.run_claim_live is False

    claim.write_text("not-a-pid")
    garbage = _collect(hermes_home)
    assert garbage.curator.run_claim_pid is None
    assert garbage.curator.run_claim_live is False


def test_curator_activity_absent_files_read_as_empty(hermes_home: Path):
    state = _collect(hermes_home)
    cur = state.curator
    assert cur.suppressed_count == 0
    assert cur.ledger_present is False
    assert cur.ledger_recent == []
    assert cur.run_claim_present is False
    assert "curator_activity" not in state.health.failed_sources


def test_curator_activity_keeps_last_good_on_failure(hermes_home: Path, tmp_path: Path):
    skills = hermes_home / "skills"
    (skills / ".curator_suppressed").write_text("pdf\n")
    c = Collector(hermes_home, clock=lambda: _NOW)
    try:
        first = c.collect()
        (skills / ".curator_suppressed").unlink()
        outside = tmp_path / "suppressed"
        outside.write_text("a\nb\nc\n")
        (skills / ".curator_suppressed").symlink_to(outside)
        second = c.collect()
    finally:
        c.close()
    assert first.curator.suppressed_count == 1
    assert "curator_activity" in second.health.failed_sources
    assert second.curator.suppressed_count == 1


def test_curator_ledger_skips_torn_and_actionless_rows(hermes_home: Path):
    lines = [
        json.dumps({"actor": "agent", "action": "create", "skill": "kept"}),
        "{torn",
        json.dumps(["not", "a", "row"]),
        json.dumps({"actor": "agent", "skill": "no-action"}),
    ]
    (hermes_home / "skills" / ".curator_ledger.jsonl").write_text("\n".join(lines) + "\n")
    state = _collect(hermes_home)
    assert [row.skill for row in state.curator.ledger_recent] == ["kept"]
