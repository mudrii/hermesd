"""Curator readers: run reports, scheduler state, thresholds, skill hygiene.

Everything here reads what the curator writes — ``logs/curator/<stamp>/run.json``
(ROOT), ``skills/.curator_state`` and ``skills/.usage.json`` (PROFILE) — plus the
thresholds the ROOT ``config.yaml`` overrides. It lives apart from the other
``operations`` readers because the curator is its own source with its own
ownership row (``.codex/rules/source-ownership.md``), and because the block is
large enough that keeping it in a general module hid where it belonged.

Upstream references are quoted per function; the writer of the run report is
``agent/curator.py`` and the usage records come from ``tools/skill_usage.py``.
"""

from __future__ import annotations

import json
import math
from typing import Any

from hermesd.collect.common import (
    _age_seconds,
    _coerce_bool,
    _coerce_int,
    _iso_to_epoch,
    _printable_capped,
)
from hermesd.models import CuratorLedgerAction, CuratorRun, SkillCurationWindow


def _curator_with_scheduler_state(
    run: CuratorRun,
    state: dict[str, Any],
    curator_cfg: dict[str, Any],
) -> CuratorRun:
    if not state and not curator_cfg:
        return run
    return run.model_copy(
        update={
            "scheduler_state_present": bool(state),
            # ``.curator_state`` is machine-written with real booleans
            # (``agent/curator.py:44,65``): a stringified flag is corruption.
            "scheduler_paused": _coerce_bool(state.get("paused")),
            "scheduler_run_count": _coerce_int(state.get("run_count")),
            "scheduler_last_run_at": str(state.get("last_run_at") or ""),
            "scheduler_last_report_path": str(state.get("last_report_path") or ""),
            "consolidate_enabled": bool(curator_cfg.get("consolidate")),
            "last_run_duration_seconds": _duration_or_none(state.get("last_run_duration_seconds")),
        }
    )


def _duration_or_none(value: object) -> float | None:
    """A recorded non-negative duration; None when absent or not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


# skills/.curator_ledger.jsonl is append-only telemetry (tools/skill_ledger.py:
# 1-8) that trims at 5 MB; each row carries before/after file manifests, so
# only a bounded tail is read and only the newest few rows are kept.
_LEDGER_TAIL_BYTES = 64 * 1024
_LEDGER_RECENT_LIMIT = 5
_LEDGER_FIELD_CHARS = 120
# agent/curator.py:1116 — a claim older than this is a crashed holder.
_CURATOR_CLAIM_STALE_SECONDS = 3600.0


def _suppressed_count(text: str) -> int:
    """Non-blank lines of skills/.curator_suppressed (tools/skill_usage.py:194-207)."""
    return sum(1 for line in text.splitlines() if line.strip())


def _ledger_recent(text: str) -> tuple[CuratorLedgerAction, ...]:
    """Newest-first actor/action/skill rows from a ledger tail; torn lines skipped."""
    rows: list[CuratorLedgerAction] = []
    for line in reversed(text.splitlines()):
        if len(rows) >= _LEDGER_RECENT_LIMIT:
            break
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(row, dict) or not row.get("action"):
            continue
        rows.append(
            CuratorLedgerAction(
                ts=_printable_capped(row.get("ts"), _LEDGER_FIELD_CHARS),
                actor=_printable_capped(row.get("actor"), _LEDGER_FIELD_CHARS),
                action=_printable_capped(row.get("action"), _LEDGER_FIELD_CHARS),
                skill=_printable_capped(row.get("skill"), _LEDGER_FIELD_CHARS),
            )
        )
    return tuple(rows)


def _claim_pid(text: str) -> int | None:
    """The pid ``_claim_run`` writes into skills/.locks/curator-run."""
    stripped = text.strip()
    if not stripped.isdecimal():
        return None
    pid = int(stripped)
    return pid if pid > 0 else None


# Curator transition thresholds, agent/curator.py:29 — 14 days to stale, 30 to
# archive — overridable per install via curator.stale_after_days /
# curator.archive_after_days (resolved by get_stale_after_days /
# get_archive_after_days, agent/curator.py:115-120).
_CURATOR_DEFAULT_STALE_AFTER_DAYS = 14
_CURATOR_DEFAULT_ARCHIVE_AFTER_DAYS = 30
# Display bound on the per-skill window table; the counts stay complete.
_SKILL_WINDOW_LIMIT = 20
_SECONDS_PER_DAY = 86400.0


def _curator_threshold_days(cfg: dict[str, Any], key: str, default: int) -> int:
    """``int(curator.<key>)`` with the default on any cast failure.

    Mirrors ``_config_number`` (``agent/curator.py:96-100``): an uncastable or
    absent value falls back to the default, while a present numeric value —
    including ``0`` — is kept exactly as the curator would keep it. YAML's
    ``.inf`` reaches ``int()`` as a float infinity and raises ``OverflowError``
    rather than ``ValueError``, so that is a cast failure too: the alternative
    is losing the whole curator source over one token.
    """
    try:
        return int(cfg.get(key, default))
    except (OverflowError, TypeError, ValueError):
        return default


def _curator_thresholds(cfg: dict[str, Any]) -> tuple[int, int, bool]:
    """Effective (stale, archive) day thresholds, and whether config overrode them."""
    customized = "stale_after_days" in cfg or "archive_after_days" in cfg
    stale = _curator_threshold_days(cfg, "stale_after_days", _CURATOR_DEFAULT_STALE_AFTER_DAYS)
    archive = _curator_threshold_days(
        cfg, "archive_after_days", _CURATOR_DEFAULT_ARCHIVE_AFTER_DAYS
    )
    return stale, archive, customized


def _usage_last_activity_epoch(record: dict[str, Any]) -> float | None:
    """Newest use/view/patch stamp as an epoch, or None when the skill never fired.

    ``created_at`` is deliberately left out — upstream excludes it so
    never-active skills stay distinguishable (``tools/skill_usage.py:106-111``).
    """
    stamps = [
        epoch
        for key in ("last_used_at", "last_viewed_at", "last_patched_at")
        if (epoch := _iso_to_epoch(record.get(key))) is not None
    ]
    return max(stamps) if stamps else None


def _skill_curation_hygiene(
    usage: dict[str, Any],
    *,
    now: float,
    stale_after_days: int,
    archive_after_days: int,
) -> dict[str, Any]:
    """Patch-reuse, state and threshold-window rollups over ``skills/.usage.json``.

    Records are what ``tools/skill_usage.py:330-340`` writes: ``state`` in
    {active, stale, archived} with ``pinned`` as a separate flag, and the patch
    loop tracked as ``patch_generation`` vs ``last_reused_patch_generation`` —
    a generation gap means the skill was patched but the patched version has
    not been re-used yet. Any other ``state`` value counts as unknown rather
    than being folded into a known bucket.
    """
    counts = {"active": 0, "stale": 0, "archived": 0}
    unknown = 0
    pinned = 0
    patch_pending = 0
    windows: list[SkillCurationWindow] = []
    for name, raw in sorted(usage.items()):
        record = raw if isinstance(raw, dict) else None
        if record is None:
            continue
        state = str(record.get("state") or "active")
        if state in counts:
            counts[state] += 1
        else:
            unknown += 1
        # ``.usage.json`` is a state payload with real booleans
        # (``set_pinned`` stores ``bool(pinned)``): a string is corruption.
        is_pinned = _coerce_bool(record.get("pinned"))
        if is_pinned:
            pinned += 1
        pending = _coerce_int(record.get("patch_generation")) > _coerce_int(
            record.get("last_reused_patch_generation")
        )
        if pending:
            patch_pending += 1
        activity = _usage_last_activity_epoch(record)
        age = _age_seconds(activity, now)
        window = SkillCurationWindow(
            name=str(name),
            state=state,
            pinned=is_pinned,
            patch_pending_reuse=pending,
            last_activity_age_seconds=age,
            days_until_stale=_days_remaining(age, stale_after_days),
            days_until_archive=_days_remaining(age, archive_after_days),
        )
        if len(windows) < _SKILL_WINDOW_LIMIT:
            windows.append(window)
        else:
            _replace_soonest_window(windows, window)
    windows.sort(key=_window_sort_key)
    return {
        "managed_skill_count": sum(counts.values()) + unknown,
        "patch_pending_reuse_count": patch_pending,
        "state_active_count": counts["active"],
        "state_stale_count": counts["stale"],
        "state_archived_count": counts["archived"],
        "state_unknown_count": unknown,
        "pinned_count": pinned,
        "skill_windows": windows,
    }


def _days_remaining(age_seconds: float | None, threshold_days: int) -> float | None:
    """Days left before a threshold, from the last activity; None with no activity."""
    if age_seconds is None:
        return None
    return threshold_days - age_seconds / _SECONDS_PER_DAY


def _window_sort_key(window: SkillCurationWindow) -> tuple[bool, float, str]:
    days = window.days_until_stale
    return (days is None, days if days is not None else 0.0, window.name)


def _replace_soonest_window(
    windows: list[SkillCurationWindow], candidate: SkillCurationWindow
) -> None:
    """Keep the soonest-deadline window when the display list is already full."""
    slowest_index = max(range(len(windows)), key=lambda idx: _window_sort_key(windows[idx]))
    if _window_sort_key(candidate) < _window_sort_key(windows[slowest_index]):
        windows[slowest_index] = candidate


def _state_transition_label(entry: dict[str, Any]) -> str:
    from_state = str(entry.get("from") or entry.get("from_state") or "")
    to_state = str(entry.get("to") or entry.get("to_state") or "")
    at = str(entry.get("at") or entry.get("timestamp") or entry.get("created_at") or "")
    if from_state or to_state:
        label = f"{from_state or 'unknown'} -> {to_state or 'unknown'}"
    else:
        label = str(entry.get("state") or "")
    return f"{label} @ {at}" if at and label else label
