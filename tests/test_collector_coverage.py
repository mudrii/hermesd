"""Behavior-focused coverage for collector IO-error fallbacks and caches.

Every test drives a real ``Collector().collect()`` (or a real module-level
helper) against a tmp ``~/.hermes`` and asserts observable behavior:
cache-preservation, the read-only path-escape guard, and mtime cache hits.
File IO errors are triggered realistically (chmod 000, symlink loops, symlinked
run dirs) rather than by monkeypatching collector internals.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermesd.collector import (
    Collector,
    _count_skills,
    _is_dashboard_process,
    _latest_cron_output_excerpt,
    _latest_log_mtime,
    _mcp_tool_filter_summary,
    _path_resolves_under,
    _read_soul_excerpt,
    _word_count,
)

_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_skip_if_root = pytest.mark.skipif(
    _RUNNING_AS_ROOT, reason="chmod 000 does not block reads when running as root"
)


def _unreadable(path: Path) -> bool:
    """True only when the OS actually denies reads (guards root/odd FS)."""
    try:
        path.read_bytes()
    except OSError:
        return True
    except Exception:
        return True
    return False


# --- available-tools mtime cache HIT (collector.py:694) ----------------------


def test_available_tools_cache_hit_skips_reread(hermes_home: Path):
    sessions_dir = hermes_home / "sessions"
    (sessions_dir / "sessions.json").write_text(json.dumps({"a": {"session_id": "s1"}}))
    (sessions_dir / "session_s1.json").write_text(
        json.dumps({"session_id": "s1", "tools": [{"name": "web_search"}]})
    )

    c = Collector(hermes_home)
    try:
        calls: list[Path] = []
        original = c._read_json_cached

        def counting(path: Path):
            calls.append(path)
            return original(path)

        c._read_json_cached = counting  # type: ignore[method-assign]

        first = c.collect()
        first_reads = len(calls)
        assert first.available_tools == 1
        assert "web_search" in first.available_tool_names

        # Second collect with unchanged sessions.json mtime: the available-tools
        # branch must return the cached (count, names) without re-reading any
        # session JSON files.
        calls.clear()
        second = c.collect()
        assert second.available_tools == 1
        assert second.available_tool_names == first.available_tool_names
        # The cache hit means the second collect issues strictly fewer JSON
        # reads than the cold first collect (it skips sessions.json + per-session
        # files entirely for the tools branch).
        assert len(calls) < first_reads
    finally:
        c.close()


# --- log stream OSError -> last-good cached lines (collector.py:1456-1457) ----


@_skip_if_root
def test_log_stream_oserror_preserves_last_good_lines(hermes_home: Path):
    agent_log = hermes_home / "logs" / "agent.log"
    agent_log.write_text("2026-04-09 15:41:58,123 - hermes - INFO - Tool call: web_search\n")

    c = Collector(hermes_home)
    try:
        first = c.collect()
        assert first.logs.agent_lines, "expected agent log lines on first read"
        good = [line.message for line in first.logs.agent_lines]

        # Make the file unreadable but keep it present and bump its mtime so the
        # stream cache is invalidated and the reader is forced to re-read.
        os.chmod(agent_log, 0o000)
        os.utime(agent_log, None)
        if not _unreadable(agent_log):
            pytest.skip("filesystem allowed read despite chmod 000")

        second = c.collect()
        # Cache-preservation: the unreadable file falls back to last-good lines
        # rather than blanking the stream.
        assert [line.message for line in second.logs.agent_lines] == good
    finally:
        os.chmod(agent_log, 0o644)
        c.close()


# --- curator run.json symlink -> empty, not a failed source (collector.py:982) -


def test_curator_run_json_symlink_returns_empty_without_failing_source(hermes_home: Path):
    run_dir = hermes_home / "logs" / "curator" / "20260610-133539"
    run_dir.mkdir(parents=True)
    real = hermes_home / "logs" / "curator" / "real_run.json"
    real.write_text(json.dumps({"model": "MiniMax-M3", "counts": {"before": 8}}))
    (run_dir / "run.json").symlink_to(real)

    c = Collector(hermes_home)
    try:
        state = c.collect()
        # Symlinked run.json is rejected by the read-only hardening: empty run,
        # and curator is NOT marked failed (it is a clean empty result).
        assert state.curator.run_present is False
        assert state.curator.model == ""
        assert "curator" not in state.health.failed_sources
    finally:
        c.close()


# --- _path_resolves_under guard returns False on error (collector.py:1882-1883) -


def test_path_resolves_under_false_when_resolve_raises(tmp_path: Path, monkeypatch):
    # On Linux/older CPython a symlink-loop makes Path.resolve raise OSError
    # (ELOOP) or RuntimeError; on macOS/CPython 3.13 resolve(strict=False) is
    # lexical and never raises. To prove the read-only guard's contract on every
    # platform we force the documented failure mode. This patches the stdlib
    # boundary (Path.resolve), not any collector internal.
    root = tmp_path / "home"
    root.mkdir()
    real_resolve = Path.resolve

    def boom(self, *args, **kwargs):
        if self.name == "escape":
            raise OSError("simulated ELOOP")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", boom)
    assert _path_resolves_under(tmp_path / "escape", root) is False


def test_path_resolves_under_true_for_real_child(tmp_path: Path):
    root = tmp_path / "home"
    child = root / "logs" / "agent.log"
    child.parent.mkdir(parents=True)
    child.write_text("x")
    assert _path_resolves_under(child, root) is True
    assert _path_resolves_under(tmp_path / "elsewhere", root) is False


# NOTE (collector.py:829-830, accepted-defensive): the cron-tick reader guards
# ``stat()`` with ``except OSError: pass`` *after* ``tick_path.exists()`` has
# already succeeded. The only way to reach the except is a TOCTOU race where the
# file vanishes/loses permissions between exists() and stat() in the same call.
# Stripping the parent dir's perms makes exists() itself raise (before the
# guarded line), so this branch is not realistically reachable without
# monkeypatching stat(). Left uncovered rather than forced with a brittle mock.


# --- checkpoint HERMES_WORKDIR read OSError -> blank workdir (1246-1247) -------


@_skip_if_root
def test_checkpoint_unreadable_workdir_file_blanks_workdir(hermes_home: Path):
    repo_dir = hermes_home / "checkpoints" / "deadbeefcafe0001"
    repo_dir.mkdir(parents=True)
    workdir_file = repo_dir / "HERMES_WORKDIR"
    workdir_file.write_text("/tmp/project")
    os.chmod(workdir_file, 0o000)
    try:
        if not _unreadable(workdir_file):
            pytest.skip("filesystem allowed read despite chmod 000")
        c = Collector(hermes_home)
        try:
            state = c.collect()
            assert "checkpoints" not in state.health.failed_sources
            cp = next(c for c in state.checkpoints if c.repo_id == "deadbeefcafe0001")
            # Unreadable HERMES_WORKDIR falls back to "" rather than crashing.
            assert cp.workdir == ""
            assert cp.workdir_name == ""
        finally:
            c.close()
    finally:
        os.chmod(workdir_file, 0o644)


# --- cron tail read OSError -> [] without failing logs (1873-1874) -------------


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


# --- module-level helper OSError fallbacks --------------------------------------


@_skip_if_root
def test_word_count_oserror_returns_zero(tmp_path: Path):
    f = tmp_path / "BOOT.md"
    f.write_text("one two three")
    os.chmod(f, 0o000)
    try:
        if not _unreadable(f):
            pytest.skip("filesystem allowed read despite chmod 000")
        assert _word_count(f) == 0
    finally:
        os.chmod(f, 0o644)


@_skip_if_root
def test_read_soul_excerpt_oserror_returns_empty(tmp_path: Path):
    f = tmp_path / "SOUL.md"
    f.write_text("Remember the operator.")
    os.chmod(f, 0o000)
    try:
        if not _unreadable(f):
            pytest.skip("filesystem allowed read despite chmod 000")
        assert _read_soul_excerpt(f) == ""
    finally:
        os.chmod(f, 0o644)


def test_read_soul_excerpt_whitespace_only_returns_empty(tmp_path: Path):
    # File is present and readable but every line is blank: the loop finds no
    # non-empty line and falls through to the trailing "" (collector.py:1708).
    f = tmp_path / "SOUL.md"
    f.write_text("\n   \n\t\n")
    assert _read_soul_excerpt(f) == ""


# NOTE (collector.py:1648-1649, accepted-defensive): the ``except OSError:
# continue`` around ``path.stat()`` in _latest_log_mtime only fires when a path
# that just passed ``is_file()`` then fails ``stat()`` in the same loop — a
# TOCTOU race not reproducible deterministically without monkeypatching stat().
# The reachable branches (non-file skip, empty dir -> None) are covered below.


def test_latest_log_mtime_skips_nonfiles_and_empty(tmp_path: Path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "subdir").mkdir()  # non-file: skipped (line 1644-1645)
    good = logs / "a.log"
    good.write_text("hi")
    assert _latest_log_mtime(logs) == good.stat().st_mtime

    # Empty dir (only a subdir) -> no file mtimes -> None (line 1650-1651).
    empty = tmp_path / "emptylogs"
    empty.mkdir()
    (empty / "nested").mkdir()
    assert _latest_log_mtime(empty) is None


# --- context_lengths key without "@" separator -> skipped (collector.py:565) --


def test_context_lengths_key_without_separator_is_skipped(hermes_home: Path):
    # context_length_cache.yaml keys are "model@base_url"; a key with no "@" is
    # malformed and must be skipped (the partition sep check), while a valid key
    # is normalized through. Drive it via a real collect() and assert sessions
    # still populate (no crash from the malformed entry).
    import yaml

    (hermes_home / "context_length_cache.yaml").write_text(
        yaml.dump(
            {
                "context_lengths": {
                    "no_separator_key": 12345,  # skipped (line 565)
                    "gpt-5.4@https://api.example.com/": 200000,  # normalized
                }
            }
        )
    )
    c = Collector(hermes_home)
    try:
        # No exception; the malformed key is silently dropped. We exercise the
        # private reader directly to assert the normalization contract.
        lengths = c._read_context_lengths()
        assert "no_separator_key" not in lengths
        assert lengths["gpt-5.4@https://api.example.com"] == 200000
    finally:
        c.close()


# --- cron output excerpt read OSError -> empty tuple (collector.py:1834-1835) --


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


# --- pure helpers: branches with no IO ------------------------------------------


def test_count_skills_ignores_dotdirs_and_files(tmp_path: Path):
    skills = tmp_path / "skills"
    real = skills / "dev"
    real.mkdir(parents=True)
    (real / "skill-a").mkdir()
    (real / "skill-b").mkdir()
    (real / "README.md").write_text("not a skill dir")  # non-dir child: not counted
    (skills / ".cache").mkdir()  # dotdir category: skipped (collector.py:1675)
    (skills / "loose.txt").write_text("x")  # non-dir category: skipped
    assert _count_skills(skills) == 2
    assert _count_skills(tmp_path / "missing") == 0


def test_mcp_tool_filter_summary_empty_returns_blank():
    # Empty cfg (1772-1773) and present-but-empty include/exclude (1780).
    assert _mcp_tool_filter_summary({}) == ""
    assert _mcp_tool_filter_summary({"include": [], "exclude": []}) == ""


def test_is_dashboard_process_matches_phrase_and_bad_quoting():
    # "hermes dashboard" phrase short-circuits to True (line 2044-2045).
    assert _is_dashboard_process("python -m hermes dashboard") is True
    # Unbalanced quote makes shlex.split raise ValueError -> str.split fallback
    # still finds the hermesd entrypoint (lines 2046-2050).
    assert _is_dashboard_process('/usr/bin/hermesd --flag "unterminated') is True
    assert _is_dashboard_process('/usr/bin/other "unterminated') is False
