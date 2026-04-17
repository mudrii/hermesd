# Changelog

All notable changes to hermesd will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2026.4.17] - 2026-04-17

### Added

- Added two new read-only dashboard panels: **Profiles** for profile-scoped runtime inspection and **Memory** for persisted memory-file visibility.
- Added one-shot snapshot export for automation and bug reports with `--snapshot`, `--snapshot-panel`, `--snapshot-file`, and `--snapshot-format json`.
- Added richer detail-view controls: `f` focus toggle, `/` inline filters for Sessions and Logs, `s` session sorting, `g` / `G` jump navigation, `p` profile cycling, and `c` clipboard export via OSC 52.
- Added deeper operational visibility across existing panels, including background processes, filesystem checkpoints, credential pools, Tool Gateway routing, token analytics, cron output summaries, and hooks/plugins/MCP/BOOT.md inventory.

### Changed

- hermesd can now read profile-scoped runtime data with `--profile NAME` or `HERMES_PROFILE=NAME` while keeping root-only reads as the default.
- The dashboard now supports a full 10-panel overview, including a dedicated tall single-column layout for narrow but high terminals such as vertical tmux splits.
- Panel shortcuts now match the live UI contract: `1`-`9` open panels 1-9 and `0` opens panel 10, including snapshot mode.
- Sessions detail now exposes billing metadata and parent-session lineage, making cost attribution and compression chains visible without querying SQLite directly.
- Logs now include a fourth `cron` tab, field-aware filtering, severity-threshold filtering via `minlevel:`, and more consistent scrolling behavior.
- The header and footer now surface clearer runtime state with an offline banner, collector health counts, and degraded-source names when a refresh partially fails.
- Theme updates now reload live from `config.yaml`, and unknown skins normalize to `default` so the rendered theme stays stable.

### Fixed

- Collector reads are now more defensive against malformed Hermes files and keep the last good state instead of blanking panels on transient bad input.
- SQLite access is now serialized across threads and cache invalidation now tracks `PRAGMA data_version`, which fixes stale session, tool, and message-search reads after database updates.
- Log reads now preserve the last good content when files rotate, disappear briefly, or saved cron output becomes temporarily unavailable.
- Session message search now falls back to SQL `LIKE` when FTS misses punctuation-heavy queries, reducing false negatives in Sessions filtering.
- Tool Gateway and MCP metadata now redact secret-bearing query params and command arguments before rendering them in panels or snapshots.
- `--refresh-rate` and `--log-tail-bytes` now reject non-positive values at argument parsing time instead of allowing invalid runtime behavior.

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
