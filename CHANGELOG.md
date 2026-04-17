# Changelog

All notable changes to hermesd will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `--refresh-rate` now rejects non-positive values at argument parsing time instead of allowing a busy collector loop.
- hermesd can now read profile-scoped runtime data with `--profile NAME` or `HERMES_PROFILE=NAME` while keeping root-only mode as the default; shared files still come from the root Hermes home.
- Collector reads are more defensive against malformed Hermes files: gateway state, config, auth, and update metadata now ignore invalid shapes and continue rendering with the last valid data they can recover.
- Log tailing now preserves the last good lines when a log file temporarily disappears or cannot be read, matching the dashboard's cache-preservation behavior.
- The logs detail view now supports `j`/`k` scrolling, and compact overview mode now keeps panels 4 and 7 visible instead of dropping them on smaller terminals.
- The dashboard theme now refreshes live when the active skin changes in `config.yaml`, so the header label and panel colors stay in sync.
- Available tool discovery now unions tool names across session files instead of stopping at the first session entry that contains a `tools` list.
- The overview layout is now driven by declarative layout specs, and footer/help panel-range hints derive from the panel registry instead of hardcoded `1-8` assumptions.
- Added a read-only Profiles panel with per-profile session counts, log freshness, skill counts, DB size, SOUL excerpts, and panel-local `p` cycling that does not change the selected data source.
- Sessions detail now surfaces `billing_provider`, `cost_status`, and `pricing_version` from the existing SQLite schema.
- Panel 7 now shows redacted credential-pool metadata from `auth.json`, including auth type, source, status, request counts, cooldowns, and token presence without ever rendering secret values.
- Panel 5 now surfaces Tool Gateway routing for `web`, `image_gen`, `tts`, and `browser`, including domain, scheme, Firecrawl endpoint, and token presence from config plus environment.
- Panel 4 now shows read-only background-process checkpoint data from `processes.json`, including PID, notify-on-complete, watch-pattern summary, start time, and command for running processes.
- Panel 3 now includes lightweight analytics in detail view: `7d`/`30d` token windows plus per-model and per-provider cost breakdowns derived from the existing session table.
- Panel 2 now surfaces `parent_session_id` lineage in Sessions detail so child/compressed sessions are visible directly in the dashboard.
- Panel 5 now shows additional config metadata already present in `config.yaml`, including provider routing summary, smart routing, fallback model, dashboard theme, session reset mode, and memory provider.
- Panel 6 now enriches cron jobs with delivery targets plus latest saved output summaries from `cron/output/`, including explicit `[SILENT]` runs.
- Panel 8 now includes a fourth `cron` tab that tails the most recent saved cron output file.
- Panel 7 now surfaces read-only integration inventory from the Hermes home: user hooks, installed plugins, configured MCP servers, and `BOOT.md` presence alongside providers, credential pools, and skills.
- Panel 4 now shows filesystem checkpoint repos from `checkpoints/`, including workdir name, commit depth, and the latest checkpoint reason.
- Added a dedicated Memory panel that surfaces the configured memory provider, `MEMORY.md` and `USER.md` word counts, `SOUL.md` size/excerpt, and memory file inventory without conflating that data with skills/integrations.
- Panels 2 and 8 now support richer inline detail filtering with `/`, including live query editing, persisted filters after `Enter`, and field-aware queries for sessions/logs.
- Sessions detail now supports `s` sort cycling for recent/cost/token views, and scrollable detail views support `g`/`G` jump-to-top/jump-to-bottom navigation.
- Added `f` as a focus toggle for the last selected panel, reusing the full-screen detail view as a quick overview/detail switch instead of requiring digit re-entry every time.
- Narrow but tall terminals now get a dedicated single-column 10-row overview layout instead of the denser compact mixed grid, which improves readability in vertical tmux splits.
- The footer now shows a collector health indicator with `ok/total` source counts and degraded-source names, while preserving the last good source data if one collector slice raises unexpectedly during refresh.
- The header and footer now surface an `AGENT OFFLINE` banner when the gateway is down and there has been no recent runtime activity under the selected profile, making “idle because stopped” clearer than a merely quiet dashboard.
- hermesd now supports `--snapshot` for one-shot overview rendering to stdout without entering the live TUI loop, `--snapshot-panel N` for exporting a single panel detail view, and `--snapshot-file PATH` for writing either form to disk.
- Pressing `c` now copies the current rendered view as plain text via OSC 52, so overview and detail panels can be pasted directly into bug reports or chats from compatible terminals.
- Panel expansion shortcuts now match the actual keyboard contract: use `1`-`9` for panels 1-9 and `0` for panel 10.

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
