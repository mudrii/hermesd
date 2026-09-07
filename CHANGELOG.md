# Changelog

All notable changes to hermesd will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses date-based versions in `YYYY.M.D` form.

## [Unreleased]

### Fixed

- Config detail view no longer crashes with a Rich `MarkupError` when `code_execution_mode` or MoA preset config values contain `[`; both labels are now escaped like their siblings.
- Sessions hidden by hermes-agent 0.21 (`sessions.hidden = 1`) are excluded from the session list and the header count, and sessions are ordered by `COALESCE(last_activity_at, started_at)` so revived sessions sort where the agent shows them; older schemas without those columns keep the previous behaviour.
- Tool-call statistics and message search skip messages compacted out of the live transcript (`messages.active = 0`) on hermes-agent 0.21 schemas, so counts and search hits reflect what the agent still holds in context.
- The LIKE fallback for message search is bounded to 500 sessions, so a pathological match cannot hand an unbounded result set to the UI.
- A file that is deleted under `~/.hermes` (for example `.drain_request.json`) is evicted from the last-good cache instead of being served forever; only transiently unreadable files keep their last-good value.
- Every panel now strips ANSI and terminal control sequences from collected data, so crafted or corrupt input can no longer redraw the display or issue terminal clipboard commands.
- Cron jobs with non-string JSON fields (e.g. a numeric `id`) no longer crash the whole cron source to fallback; all string fields are coerced.
- One corrupt per-board `kanban.db` no longer discards the root board and all healthy boards; a last-good summary is preserved when available and Kanban health is marked degraded.
- Closing the dashboard no longer risks closing the SQLite connection underneath an in-flight message search: the query is interrupted and the search thread joined first.
- Cron output excerpt cache now stats before reading (skipping the per-refresh file read when the mtime is unchanged) and no longer returns a stale excerpt when the mtime is unavailable.
- Session cost estimation now includes `cache_write_tokens` (billed at 1.25× the input rate), fixing systematic undercounting of cache-heavy sessions.
- The available-tools cache now keys on path-associated nanosecond file signatures, so tool changes are visible without waiting for the sessions index to be rewritten or losing changes to coarse/equal mtimes.
- Token analytics windows, runtime `last_tick_ago_seconds` (now clamped to zero for future mtimes), and git checkpoint collection (now tolerant of non-UTF-8 commit subjects) no longer fail or misreport in edge cases.
- Kanban "Completed" column renders an age label instead of a raw epoch; compact Logs falls back to the first non-empty stream when `agent.log` is absent; `fmt_tokens` formats negative values by magnitude; Skills detail caps its visible window; Gateway formats `drain_requested_at` and falls back to `version_behind` when `updates_behind` is absent; Profiles/Tools/Operations survive absurd mtimes; Tokens/Sessions detail tables cap at 50 rows with an overflow footer; Cron/Curator render `—` instead of blank placeholders.
- The input thread survives transient read/termios errors (fail-safe after 5 consecutive failures), including escape sequences split immediately after a lone Escape byte; the render loop no longer busy-spins between frames, and footer/view mutations are consistently guarded by the view lock.
- Snapshot mode creates missing parent directories for `--snapshot-file` and handles SIGINT/SIGTERM cleanly instead of dumping a traceback; an interrupted snapshot now exits with the conventional signal exit code (130/143) and a notice, and a second signal force-kills a wedged collect.
- `file_cache` keys on `st_mtime_ns`, so same-second fixes to malformed files are picked up; profile-scoped paths and databases re-validate symlink targets on every collection/connect; invalid UTF-8 theme config falls back safely; `RuntimeStatus.agent_running` now defaults to not-running instead of reporting a running agent when the first collect fails.
- Follow-up hardening from a second review pass: the terminal-text sanitizer now removes OSC/DCS/APC payloads too (hyperlinks, clipboard sequences) instead of leaving them as visible garbage; kanban per-board tolerance also covers WAL-snapshot `OSError`s (e.g. a sidecar vanishing mid-copy); the cron excerpt cache keys on the mtime of the file actually read; the available-tools cache tracks every session file's mtime (not just the max); and Skills detail scrolling clamps to a full 20-row window instead of shrinking to a stub at the bottom.
- A failure while rendering a frame now prints a single-line `hermesd: render failed: ...` message to stderr and exits with status 1 after the terminal is restored, instead of dumping a traceback out of the live loop.
- A session message-search query typed while the previous search was finishing is no longer dropped: worker availability is tracked explicitly instead of inferred from a thread object that may still be unwinding.
- Copying the current view with `c` renders outside the view lock, so the input thread no longer blocks the render loop for the length of a full re-render, and the OSC 52 payload is capped at 96 KiB with a truncation marker.
- The tools panel reads the live tool inventory from `cache/banner_snapshot.json` and only falls back to the legacy `sessions/` scan, so hermes-agent 0.21 (which no longer writes per-session files) again shows the available tools; the fallback now caches only extracted tool names instead of retaining every parsed session document for the life of the process.
- PR-monitor state written to `cron/state/pr_monitor.json` is now surfaced in Operations; its `.bak` and `.lock` siblings are ignored.
- Background processes come from the live `spawn-ledger.json` registry (with the process purpose, port, and profile), falling back to the legacy `processes.json` when the ledger is absent or empty.
- A log line consisting mostly of whitespace no longer stalls a refresh: the log-line pattern is fully bounded (it previously backtracked cubically, seconds per line) and each line is truncated to 4096 characters before parsing.
- A `null` or wrong-typed value in `config.yaml` (for example `max_turns: null` or `max_turns: unlimited`) or in `cron/jobs.json` (`enabled: null`) no longer fails the whole config or cron source; values are coerced and defaults applied.
- Goal state is re-read only when `state.db` changes, and per-repo checkpoint commit counts only when the repository's refs change, removing a database snapshot copy and two git subprocesses per repository from every refresh.
- Plain-text reads under `~/.hermes` (memory files, `SOUL.md`, skill frontmatter, hook and plugin manifests, checkpoint workdir markers, the gateway pid file, cron suggestions) now refuse symlinks that escape the Hermes home and read at most 256 KiB, so an oversized or redirected file cannot stall or mislead the dashboard.
- Quitting no longer waits for a full collection pass: `close()` signals the in-flight pass, which stops after the current source and keeps last-good values for the rest.

### Changed

- Internal: `collector.py` is split into a `hermesd/collect/` package of per-domain readers, with `hermesd.collector` kept as the public facade; no behaviour or import path changes.
- Internal: long panel `_render_detail` functions are split into named per-section helpers, with the shared section heading and age formatter moved to `hermesd/panels/formatting.py`; rendered output is byte-identical.
- Removed the dead `_cache_hits` counter and unified the duplicated WAL-snapshot-to-tempdir logic between `db.py` and `collector.py` into one shared helper.
- Package metadata now uses a PEP 639 SPDX license expression (`License-Expression: MIT`); Hatchling 1.32 emits Core Metadata 2.5 and Twine 7 validates the resulting artifacts.
- Dev-toolchain floor pins raised (`pip>=26.2` for PYSEC-2026-3721) and documented with an explanatory comment.

### Added

- Test coverage tooling: `pytest-cov` with branch coverage, enforced at 96% in CI; a PTY-based end-to-end TUI integration test; contract tests extended to panels 4, 5, 6, 8, 9, 10, and 11; Unicode/CJK rendering tests; snapshot-file symlink/traversal edge-case tests.
- `SECURITY.md` with a vulnerability reporting policy, and a Troubleshooting/FAQ section in the README covering non-TTY usage, the AGENT OFFLINE banner, footer health indicators, SQLite WAL snapshotting, and `--log-tail-bytes` tuning.
- CI now tests Python 3.11–3.14 on Linux plus Python 3.14 on macOS, smoke-runs the Docker image, checks the commit-pinned Nix flake, runs packaging checks in a single-version job, and tracks `uv` and Docker dependency updates with Dependabot.
- Published wheels include `py.typed`; sdists include the repository files required by their shipped test suite; artifact module smoke tests use isolated import mode so they cannot accidentally import the source checkout.

## [2026.7.11] - 2026-07-11

### Added

- The Operations panel now reads `~/.hermes/verification_evidence.db` read-only and surfaces verification event totals, failed-check counts, latest evidence rows, and pending changed-path counts so Hermes Agent's coding verification ledger is visible in hermesd.
- Config now summarizes MoA presets, reference model counts, aggregator, and trace setting; Operations inventories `moa-traces/*.jsonl` with bounded latest-record metadata without rendering trace contents.
- Operations now reads `projects.db` read-only and surfaces project, folder, discovered-repo, archive, missing-primary-path, newest-repo, and board-mapping summaries.
- Gateway now surfaces served profiles, external drain markers, busy/drainable state, scale-to-zero idle timeout, and relay-only intent.
- Kanban now discovers `kanban/boards/*/kanban.db`, marks the current board, and shows per-board task/run/problem counts, stale-claim counts, and typed blocker counts when present.
- Operations now reads `/goal` state from `state.db` and shows active/waiting goals, contract presence, turn budget, and project correlations to verification roots and Kanban boards.
- Memory now surfaces a lightweight learning summary from skill usage, learned skill metadata, and memory-card headings.
- Cron now shows the active scheduler provider, Chronos managed-cron config presence, and persisted cron suggestion counts.
- Gateway now reports channel-alias inventory from `channel_aliases.json`, stale alias counts, platform family labels, and gateway platforms missing from the channel directory.
- Skills / Integrations now shows credential expiry and last-refresh metadata when providers persist it safely in `auth.json`.
- Curator now shows scheduler state from `skills/.curator_state` plus consolidation config even when no run report exists yet.

### Changed

- Detail footers and help text now advertise `j`/`k` scrolling only for scrollable detail views.
- Release validation now installs from `uv.lock`, pins GitHub Actions and `uv` versions, checks lockfile freshness, compiles the package, audits dependencies, smoke-installs both wheel and sdist artifacts, verifies release tag/changelog/distribution filename consistency before PyPI publish, and tracks action updates with Dependabot.

### Fixed

- Logs and cron output excerpts now redact common secret material before rendering in the TUI or snapshots.
- URL fields shown in config/integration views now redact embedded username/password credentials as well as secret query parameters.
- Profile session counts and cron job output excerpts now preserve last-good values across transient file/database disappearance.
- Curator state now falls back to the newest usable run instead of blanking when a newer run directory is incomplete or corrupt.
- Nix package metadata now matches the Python package version, and README screenshots use package-metadata-safe image URLs.

## [2026.6.15] - 2026-06-15

### Added

- The Kanban panel now reads task `completed_at`, `workspace_path`, `goal_mode`, and `current_step_key`, shows each task's `branch_name` in the worker/problem tables, and surfaces decomposition-link and attachment counts (`task_links`/`task_attachments`) when those tables hold rows.
- The Logs panel now tails `audit.log`, `mcp-stderr.log`, `workspace.log`, and `workspace.error.log` as additional Tab sub-views (shown only when the files exist).
- The Operations detail panel now shows **Response Store** stats (conversation and response row counts plus file size) read-only from `~/.hermes/response_store.db`.
- The Tokens / Cost detail panel now includes a **By Endpoint** breakdown aggregating spend and tokens per billing endpoint (`billing_base_url`) — finer than the existing per-provider view (same provider, different base URLs).
- The Tokens / Cost detail panel now shows a **Cost Status** reconciliation line counting sessions by `cost_status` (e.g. unknown vs subscription-included vs estimated), so it's clear how much spend is authoritative versus unknown.
- New **Curator** panel (panel 13) showing the newest memory-curation run from `~/.hermes/logs/curator/<stamp>/run.json` — skill before/after/delta counts, archived/added/pruned/consolidated totals, the model and provider used, run duration, total tool calls plus a per-tool call breakdown, the state-transition trail, and the LLM summary (or error). Reach it with `]` from Operations or `--snapshot-panel 13`.
- The Sessions detail panel now has a Billing & Context table surfacing each session's end reason, billing endpoint (`billing_base_url`), billing mode, and the model's context-window size — joined from `context_length_cache.yaml` on `model@base_url`. (This is the model's context limit against lifetime cumulative tokens, not live context occupancy.)
- The Gateway panel now surfaces per-platform connection errors (`error_message`/`error_code`), the active-agent count, and a restart-requested marker from `gateway_state.json` — a `⚠` appears in the compact view and a dedicated Error column in the detail view.

### Changed

- Token/cost figures now use a `~$` prefix only when costs are estimated and a plain `$` prefix when every contributing session cost is provider-authoritative, in both the compact and detail views. Authoritative now covers the live producer's `exact` and `included` (subscription-covered) statuses in addition to the legacy `reported`. Subscription-`included` sessions therefore render an authoritative `$0.00` instead of a token-based estimate.
- Cron output excerpts now respect `--log-tail-bytes` instead of reading whole output files.
- Reduced redundant file and database re-reads per refresh cycle.

### Fixed

- The Operations panel's desktop build stamp now reads the live `desktop-build-stamp.json` camelCase keys (`builtAt`, falling back to a truncated `contentHash`); previously it looked only for snake_case keys and always rendered blank.
- PR-monitor counts now read the live JSON shape (`prs`, `tracked_numbers`, `checked_at`); previously they looked for the retired `monitored`/`tracked`/`checkedAt` keys and stayed stuck at zero.
- PR-monitor state is now read from all of the agent's naming families — flat `pr-monitor-*.json`/`pr_monitor_*.json` and the `pr-monitor/`/`pr_monitor/` subdirectories — and the same repo seen across families collapses to its most recently checked entry.
- The credential pool view now reads the live `auth.json` shape where each provider maps to a list of credential entries (previously only the legacy single-dict shape was understood, leaving every credential field blank). The lowest-priority entry represents the provider.
- Untrusted free-text from `~/.hermes/` (session, config, skill, kanban, cron, gateway, memory, profile, operations, and token labels, plus formatted values such as gateway platform timestamps) is now escaped before it reaches Rich's markup parser, so values containing `[...]` render literally and a stray `[/]` can no longer crash the dashboard.
- The Kanban Task Metadata table now de-duplicates by task ID, so a task present in more than one list (e.g. active and recent) renders a single row.
- Symlinked log files, log directories, cron output files, and cron output directories are now ignored when tailing logs so hermesd does not read outside the Hermes home.
- Fixed a deadlock when pressing `G` to jump to the bottom of a scrollable detail view.
- Fixed the footer health indicator showing yellow when zero collector sources were healthy.
- An unknown `minlevel:` log filter value now shows all lines instead of none.
- The memory panel compact view now distinguishes an empty `SOUL.md` from a missing one.
- Message search no longer blocks the dashboard while a refresh is in progress.
- SQL `LIKE` wildcard characters are now escaped in message search queries.
- The stale indicator is now set when the SQLite database file disappears.

## [2026.6.5] - 2026-06-05

### Added

- Added read-only Kanban visibility from `~/.hermes/kanban.db`, including task/run/event/comment counts, status and assignee breakdowns, active workers, blocked/failing tasks, recent runs, and config-derived dispatch state.
- Added an Operations panel for dashboard process counts, Desktop build metadata, model-cache freshness/counts, and PR monitor summaries.
- Added channel-directory inventory to Gateway & Platforms, expanded log streams for Desktop/Dashboard/GUI/update/gateway-error/crash logs, and richer session fields including cwd, API call count, archived state, rewind count, and handoff metadata.

### Changed

- The overview now supports 12 panels. `1`-`9` and `0` still open panels 1-10 directly, while `[` and `]` move between panels so Kanban and Operations are reachable interactively; `--snapshot-panel` accepts the new panel numbers.
- Config and Cron panels now surface newer Hermes Agent settings such as Tool Search, toolsets, code execution, dashboard auth mode, kanban dispatch settings, gateway media trust, cron parallelism, last cron error, and latest output file metadata.
- The top-left dashboard header now shows the installed `hermesd` package version next to the app name.
- Documentation and CLI help now describe the 12-panel dashboard, higher-numbered snapshot panels, and current panel registry.

## [2026.6.3] - 2026-06-03

### Security

- Bumped the `idna` transitive dependency from 3.11 to 3.17 in `uv.lock` to clear CVE-2026-45409; the affected package is a dev-only transitive (`pip-audit` → `cachecontrol` → `requests`) and the runtime closure (`rich`, `pyyaml`, `pydantic`) is unaffected. Lockfile-only change; runtime dependencies in `pyproject.toml` are unchanged.

### Developer tooling

- `_summarize_tokens` now accumulates token and cost totals in plain locals and constructs the `TokenSummary` Pydantic model once at the return boundary instead of mutating a model in place, restoring per-construction validation while preserving identical NULL coercion, `started_at_min` filtering, and field values.
- Added regression tests locking the three behaviors the boundary-construct refactor must preserve: NULL column coercion, the `started_at_min` cutoff filter, and multi-row accumulation with per-row cost resolution (reported cost preserved, estimated fallback).

## [2026.5.12] - 2026-05-12

### Changed

- `--snapshot-file` now rejects output paths under the Hermes home to preserve hermesd's read-only observer contract.
- Profile names from `--profile` and `HERMES_PROFILE` are now validated as single path segments before profile-scoped reads are resolved.
- SQLite WAL-backed databases are now read from temporary snapshots when needed, preserving hermesd's read-only contract without missing uncheckpointed Hermes data.
- Session and log filters now preserve duplicate field filters instead of silently overwriting earlier values.
- Session filters now use exact matching for ID-like fields and stricter boolean parsing for `active:`.

### Fixed

- SQLite read-only URIs now handle Hermes home paths containing URI metacharacters such as `?` and `#`.
- Collector health now marks session-derived summaries as degraded when session rows are served from stale cache after a read failure.
- The developer workflow docs now point at the active `.codex` rule and skill paths.
- SQLite cache staleness is now surfaced for session counts, tool stats, and message search, not only full session reads.
- Collector refreshes are now serialized so snapshot rendering and the polling thread cannot race while updating last-good state.
- Failed first collection cycles no longer poison the last-good dashboard state with default values.
- Session message search now runs off the render path, coalesces rapid query changes, reports search errors distinctly from no matches, and cancels cleanly during shutdown.
- The live render loop now wakes promptly on shutdown signals instead of sleeping through the next refresh tick.
- JSON snapshots now sort set-backed fields for deterministic output.
- Sessions, logs, cron, gateway, profiles, overview, and token/cost formatting now handle the validated display edge cases from the audit, including negative costs, large token rollovers, non-ISO timestamps, profile size tiers, log scroll ranges, inactive provider color, and deterministic session sort ties.

### Security

- Secret redaction now covers additional common option names, header-style secrets, nested argument lists, dict-shaped arguments, MCP command strings, and MCP environment values before rendering panel or snapshot output.
- Collector health diagnostics now redact secret-like exception text before exposing it in JSON snapshots.

### Developer tooling

- Hardened audit coverage for read-only SQLite behavior, WAL snapshots, DB stale-cache paths, collector lifecycle, message-search threading, panel filter semantics, formatting boundaries, and ANSI-stable panel assertions.
- Refreshed vulnerable transitive dependencies in `uv.lock`; `pip-audit` reports no known vulnerabilities for this branch.

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
