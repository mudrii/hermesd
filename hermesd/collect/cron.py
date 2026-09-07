"""Cron job output discovery, excerpts and suggestion counts."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from hermesd.collect.common import (
    _EXCERPT_MAX_CHARS,
    _as_dict,
    _mtime,
    _path_resolves_under,
    _read_tail_text,
    _read_text_capped,
    _safe_mtime,
)
from hermesd.collect.redaction import _redact_secret_text
from hermesd.models import LogLine


def _latest_cron_output_file(
    output_root: Path,
    job_id: str,
    *,
    stop_at: Path | None = None,
) -> Path | None:
    """Locate the newest cron output file for job_id without reading it."""
    if not job_id:
        return None
    job_output_dir = output_root / job_id
    output_root_escaped = stop_at is not None and not _path_resolves_under(output_root, stop_at)
    job_output_dir_escaped = not _path_resolves_under(job_output_dir, output_root)
    if output_root_escaped or job_output_dir_escaped or not job_output_dir.is_dir():
        return None
    files = []
    for path in job_output_dir.iterdir():
        try:
            if not path.is_symlink() and path.is_file():
                files.append(path)
        except OSError:
            continue
    if not files:
        return None
    return max(files, key=_safe_mtime)


def _latest_cron_output_excerpt(
    output_root: Path,
    job_id: str,
    max_bytes: int,
    *,
    stop_at: Path | None = None,
) -> tuple[str, bool, str, float | None]:
    latest = _latest_cron_output_file(output_root, job_id, stop_at=stop_at)
    if latest is None:
        return "", False, "", None
    latest_mtime = _mtime(latest)
    try:
        lines = _read_tail_text(latest, max_bytes).splitlines()
    except OSError:
        return "", False, "", None
    silent = any("[SILENT]" in line.upper() for line in lines)
    for line in lines:
        stripped = line.strip()
        if stripped and "[SILENT]" not in stripped.upper():
            return stripped[:_EXCERPT_MAX_CHARS], silent, latest.name, latest_mtime
    return "", silent, latest.name, latest_mtime


def _tail_latest_cron_output(
    output_root: Path,
    max_lines: int,
    max_bytes: int,
    *,
    stop_at: Path | None = None,
) -> list[LogLine]:
    output_root_escaped = stop_at is not None and not _path_resolves_under(output_root, stop_at)
    if output_root_escaped or not output_root.is_dir():
        return []
    latest_file: Path | None = None
    latest_mtime = 0.0
    for job_dir in output_root.iterdir():
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        for path in job_dir.iterdir():
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_file = path
                latest_mtime = mtime
    if latest_file is None:
        return []
    try:
        lines = _read_tail_text(latest_file, max_bytes).splitlines()[-max_lines:]
    except OSError:
        return []
    return [LogLine(message=_redact_secret_text(line.strip())) for line in lines if line.strip()]


def _cron_suggestion_count(cron_dir: Path) -> int:
    candidates = [
        cron_dir / "suggestions.json",
        cron_dir / "cron_suggestions.json",
        cron_dir / "suggestions",
    ]
    total = 0
    for path in candidates:
        if path.is_symlink() or not _path_resolves_under(path, cron_dir.parent):
            continue
        if path.is_file():
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(_read_text_capped(path, cron_dir.parent))
                total += _suggestion_count_from_data(data)
        elif path.is_dir():
            with contextlib.suppress(OSError):
                total += sum(
                    1
                    for child in path.iterdir()
                    if child.is_file()
                    and not child.is_symlink()
                    and child.suffix.lower() in {".json", ".yaml", ".yml", ".md"}
                    and _path_resolves_under(child, cron_dir.parent)
                )
    return total


def _suggestion_count_from_data(data: object) -> int:
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("suggestions", "items", "jobs"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
        return len(data)
    return 0


def _chronos_configured(cfg: dict[str, Any]) -> bool:
    return bool(
        cfg.get("portal_url")
        and cfg.get("callback_url")
        and cfg.get("expected_audience")
        and cfg.get("nas_jwks_url")
    )


def _delivery_target_label(directory: dict[str, Any], deliver: str) -> str:
    if not deliver:
        return ""
    if deliver in {"local", "origin"}:
        return deliver
    if ":" not in deliver:
        return deliver

    platform, target = deliver.split(":", 1)
    entries = _as_dict(directory.get("platforms")).get(platform)
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("name") or "") == target:
                return f"{platform}:{target}"
    return deliver
