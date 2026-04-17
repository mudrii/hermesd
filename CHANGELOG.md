# Changelog

All notable changes to hermesd will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `--refresh-rate` now rejects non-positive values at argument parsing time instead of allowing a busy collector loop.
- Collector reads are more defensive against malformed Hermes files: gateway state, config, auth, and update metadata now ignore invalid shapes and continue rendering with the last valid data they can recover.
- Log tailing now preserves the last good lines when a log file temporarily disappears or cannot be read, matching the dashboard's cache-preservation behavior.
- The logs detail view now supports `j`/`k` scrolling, and compact overview mode now keeps panels 4 and 7 visible instead of dropping them on smaller terminals.
- The dashboard theme now refreshes live when the active skin changes in `config.yaml`, so the header label and panel colors stay in sync.
- Available tool discovery now unions tool names across session files instead of stopping at the first session entry that contains a `tools` list.

### Developer tooling

- CI now runs `ruff check`, `ruff format --check`, `mypy hermesd`, `pytest`, and `pip-audit` across Python 3.11/3.12/3.13; the release/publish workflow runs the same gates before building the PyPI artifact.
- Added `ruff`, `mypy`, `types-PyYAML`, and `pip-audit` to dev dependencies; `pyproject.toml` now contains `[tool.ruff]` and `[tool.mypy]` configuration with per-module overrides for the SQLite boundary (`hermesd/db.py`) and tests.
- Refreshed `.claude/rules/python-idioms.md`, `.claude/rules/python-patterns.md`, and `.claude/skills/py-rig/SKILL.md`: version-tagged idioms (3.11/3.12/3.13), threading and resilience rules, a CLI composition-root carve-out for DI, the `panels/__init__.py` OCP seam, hermesd-shaped DI and test examples, and a unified TDD-first contributor workflow shared across `CLAUDE.md`, `CONTRIBUTING.md`, and `README.md`.

### Compatibility

- `HermesDB._ensure_connection()` now returns `sqlite3.Connection | None` instead of `bool`; external callers that relied on the bool form should check `is not None` instead.

## [2026.4.9] - 2026-04-09

### Added

- Initial release of hermesd TUI monitoring dashboard
- 8-panel overview with compact and detail views:
  - [1] Gateway & Platforms — PID, version, update status, platform connection state
  - [2] Sessions — active/total count, messages, tool calls, per-session detail table
  - [3] Tokens / Cost — today and total token usage, estimated cost from token counts
  - [4] Tools — available tools grid, per-session call stats
  - [5] Config — model, provider, personality, compression, security
  - [6] Cron — scheduler tick, job list with schedule/state/next-run
  - [7] Skills / Providers — provider auth status, skills by category with descriptions, j/k scrolling
  - [8] Logs — tailed agent/gateway/error logs with Tab switching
- Adaptive layout: single-column at 80x24 (SSH/tmux), full grid at 100+ columns
- Read-only SQLite access with `PRAGMA data_version` caching
- Cache preservation on transient errors (never blanks out on write contention)
- Auto-reconnect after 3 consecutive DB errors
- Gateway PID detection with `gateway.pid` fallback for launchd restarts
- Cost estimation from token counts when provider doesn't report costs
- Skin/theme system inheriting from Hermes Agent config
- Keyboard navigation: 1-8 expand, Esc back, j/k scroll, Tab cycle logs, r refresh, q quit
- Escape sequence handling for Ghostty/SSH/tmux environments
- 164 tests at initial release covering all panels, data collection, resilience, and edge cases
