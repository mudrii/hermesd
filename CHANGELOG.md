# Changelog

All notable changes to hermesd will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses date-based versions in `YYYY.M.D` form.

## [Unreleased]

### Fixed

- Cron 24-hour aggregates no longer drop unresolved executions. The window SQL bucketed only `completed`, `failed`, and `running`/`claimed`, so a row with any other status — including `unknown`, which is a real hermes-agent terminal state that its own retention pruning names explicitly — contributed to none of them and disappeared from the summary while still occupying the table. A window containing only such a row rendered as a placeholder dash, indistinguishable from a job with no history. The window now counts a fourth `unknown_24h` bucket as the complement of the three recognized ones (NULL included), renders it as `1?`, and exposes a `total_24h` denominator so the buckets can be checked to reconcile. Unknown is deliberately not folded into `failed`, which would imply a retry is safe.
- Gateway platform records are no longer presented without establishing who wrote them. Upstream stamps `writer_pid`/`writer_start_time` on every platform entry while re-stamping the top-level `pid`/`start_time` on each write, and hermesd dropped both — so an entry preserved from an earlier gateway life was indistinguishable from one the running process just wrote. Ownership is now classified by exact `(pid, start_time)` equality (upstream's own live-vs-preserved rule) into `current`, `preserved`, or `unverifiable`, rendered as a new **Owner** column plus a compact `⚠ N platform record(s) outlived their writer` warning, and evaluated independently of heartbeat freshness — a ticking loop says nothing about who wrote a given row. All four identity values must be present: a record from a gateway that predates writer provenance, or one whose host could not resolve a process start time, reads as unverifiable rather than silently passing as current, and a matching PID with a different start time reads as preserved, since PID equality alone is not identity.
- A discovered plugin is no longer reported as enabled just because it is on disk. hermes-agent requires an explicit `plugins.enabled` opt-in, but hermesd inferred activation from the deny list alone (`enabled = name not in disabled`), so an ordinary user plugin with empty `enabled` and `disabled` lists rendered as available when it would never load. `PluginInfo.enabled` is now derived from an explicit `activation` state produced by a new `hermesd/collect/plugins.py` reader that mirrors upstream's `gate_manifest` order: legacy-Relay refusal (`removed`), deny list (`disabled`, which beats the allow-list), category-owned kinds (`category_owned` for `exclusive` memory providers; `model-provider` is active via providers discovery), then the `plugins.enabled` opt-in (`not_enabled` when absent). Both the path-derived manifest `key` and the bare `name` are matched against each list, so canonical and legacy spellings both resolve. A manifest that exists but does not parse is reported as `unknown` with a reason instead of being silently dropped, while a manifest-less directory is still skipped because upstream reads it as a category, not a plugin. Where `kind` is undeclared, hermesd scans the first 8 KiB of `__init__.py` for provider markers exactly as upstream does — text only, never importing or executing plugin code. The Skills panel's `Enabled` Yes/No column becomes an **Activation** column carrying these states; activation describes what the configuration says hermes-agent would do, not proof a plugin loaded.
- MCP cache membership no longer changes with the display cap. The schema-cache reader reported the true cached count but kept only the first 20 names, and the Skills panel computed "which configured servers are missing" by subtracting that truncated list — so with 21 cached servers the 21st was reported as absent purely because it fell off a presentation limit. Membership is now computed in the collector from the complete configured *and* cached name sets (`ConfigSummary.mcp_server_names` is display-bounded too, so neither side can be used for the join), the panel renders the resulting `mcp_uncached_server_count`/`mcp_uncached_server_names`, and a capped list shows `(+N more)` instead of silently looking complete.
- The Skills panel no longer claims a server "Never connected". Absence from `cache/mcp_schema_cache.json` is an observation about one read, not a connection history — the cache can be cleared, invalidated, or written under another profile — so the row is now **No cache entry**. An absent cache file is distinguished from an empty or unreadable one via `mcp_cache_present` and renders as `no cache file observed`, and a stale or unreadable cache still fails the `mcp_cache` source into health rather than being presented as current.
- Update/runtime code-skew detection now reads the same evidence upstream does. `plan.runtimes[].code_sha` is captured *before* the pull, so a finished update's plan always disagrees with the running tree: hermesd reported `⚠ code skew` on healthy fleets (false positive) while a fleet that genuinely came back on the wrong build passed silently whenever its pre-update plan happened to match (false negative). The post-restart `fleet` matrix is now authoritative — including its explicit `stale` state, which counts as skew even when that row never stamped a sha — and the pre-update plan is consulted only when there is no fleet matrix *and* the receipt shows the run never finished (non-zero exit, `failed`/`partial`/`running`/`refused` outcome, or an incomplete gateway restart). A `stop_reason` alone no longer marks a clean receipt unfinished. The verdict now names its evidence (`runtime_code_skew_source`: `fleet`, `plan`, or empty when not assessable), the recorded fleet matrix is exposed as state counts, and an unassessable receipt says so instead of implying a clean bill of health. All of it is part of the `update_receipt` last-good payload, so a failed read keeps the prior verdict rather than clearing it.
- A successful update is no longer labeled `⚠ update failed`. hermes-agent writes `outcome` as `running | success | partial | failed`, but the gateway panel compared against `"ok"` — a value upstream never produces — so every real receipt rendered as a failure and its outcome was styled as a warning. The compact warning is now driven by the same unfinished-receipt rules as skew detection, and the test fixtures were corrected to write `outcome: "success"` with a realistic pre-update plan and post-restart fleet matrix, which is what let the mismatch survive review.
- URL redaction is now scheme-agnostic and case-insensitive: uppercase schemes (`url=HTTPS://user:pw@host/x`) and non-HTTP schemes (`ftp://`, `ssh://`) have their credentials masked in `key=<url>` argv forms and in free text, not just lowercase `http(s)` URLs. The URL and secret-field scan patterns are length-bounded so long letter runs (base64 blobs, minified JS) cannot stall log redaction quadratically.
- Secret-text redaction no longer propagates `RecursionError` (or any other structured-path failure) on deeply nested JSON log lines: parse, redact, and re-serialize are guarded as one unit and fail closed to the line-oriented redactor, keeping the sanitizer-never-raises guarantee at its collector and cron call sites.
- Unquoted multi-word secret values in log text (e.g. `password=my secret pass`) no longer leak their tail: the bare-value form is redacted to the next top-level `,`, `}`, `]`, or end-of-line (failing closed on space-separated prose), while an already-redacted URL following the value stays visible.
- Argv redaction now covers `key=<url>` forms (e.g. `url=https://user:pw@host/x?token=t1` and `--url=https://…`): the URL value has its userinfo and secret query parameters masked instead of passing through byte-identical because the `key=` prefix defeated whole-argument URL detection.
- Snapshot export (`--snapshot-file`) now writes to a temporary file that is atomically renamed into place: a destination hard-linked to a file inside `~/.hermes/` can no longer truncate the protected file, and failed or interrupted writes clean up their partial temporary output.
- Secret redaction is now consistent across representations: JSON-style `"api_key": "..."` text, quoted values containing spaces, keys with whitespace around the separator, and nested dict/list arguments (e.g. `private_token`) are all redacted through one canonical secret-key classifier, and the `billing_base_url` endpoint is redacted where it enters dashboard state, breakdown labels, and JSON snapshots.
- Redaction also covers escaped or truncated quoted values, structured secret values in prefixed log messages, and malformed URLs with encoded secret keys or no path before their query string.
- Exception sanitization no longer raises on malformed URLs (e.g. invalid IPv6 literals or ports inside error messages), so a single poisoned source value can no longer abort startup or `--snapshot` runs; malformed URLs still have credentials and secret query parameters masked.
- Last-good fallback baselines are now tracked per source instead of per fully clean pass: a source that succeeds keeps advancing its own fallback even while an unrelated source is failing, so a later failure no longer regresses the display to older data.
- State-snapshot directory read failures now surface in health and retain that source's last-good counts without reverting unrelated Operations fields.
- Stale cached YAML reads (e.g. config.yaml that became malformed after a good read) now mark the affected sources degraded in health while the last-good values stay on display — matching the existing JSON behavior — instead of silently showing stale data with green health. Intentionally absent optional files remain healthy.
- Derived result caches now key on their actual dependencies: editing `context_length_cache.yaml` (e.g. a context-limit change) refreshes dependent session data without waiting for a database write or midnight, and 7d/30d token windows recompute when sessions age across a boundary within the same day (bounded to one-minute buckets).
- Transient context-length cache failures cannot promote a stale fallback into a fresh derived result: the unchanged file is retried on the next refresh.
- Mixed actual/estimated cost groups are no longer displayed as a complete actual total: groups with provider-billed and estimated rows render both parts explicitly (e.g. `$1.00 + $2.00 est.`), per-row actual-or-estimated selection never double-counts billed rows, and the aux subtotal uses the same semantics.
- An explicitly reported actual cost of zero remains authoritative, including subscription-included usage, rather than being replaced by a positive estimate. Older schemas without actual-cost values retain their fallback behavior.
- Transient file-read failures (permissions, temporary I/O errors) no longer poison the file's mtime in the config cache — the next refresh retries, so recovered files are picked up without another edit or a restart. Deterministic failures (malformed JSON/YAML, oversize) still skip re-parsing until the file changes.
- SQLite query errors in the cron executions, cron incidents, kanban task-links/enriched-tasks, and operations delegation readers now propagate to source health (panels keep last-good data) instead of being silently swallowed into empty results; genuinely absent optional tables still legitimately report empty.
- Cron 24-hour counters now use full-window SQL aggregation, and per-job last runs are selected across history instead of from the 500-row display history. Comparisons normalize UTC offsets and naive-as-UTC timestamps; equal timestamps have deterministic ordering. Busy jobs no longer undercount or crowd out other jobs' statistics, though complete historical queries can still require a table scan.
- Live message search now re-runs when dashboard data refreshes, even with an unchanged query: new matches appear and removed matches disappear without editing the filter, and a failed search retries on the next refresh instead of sticking as empty.
- Sessions detail view now scrolls the entire rendered content (`j`/`k`, `g`/`G`) like the Logs and Skills detail views, including lower sections at ordinary terminal heights. Scroll positions clamp after resize or filtering, and snapshots do not alter interactive scroll state.
- Log tail reads are strictly bounded to `--log-tail-bytes` even when the file grows between the size check and the read; every positive byte limit is honored without a hidden 1024-byte minimum.
- The Docker image includes Git for checkpoint summaries, normalizes runtime package readability for its non-root user, and has a CI acceptance check using a synthetic checkpoint repository.
- "Recent" session ordering now sorts by latest activity (matching the displayed age column and the collection ordering), so recently resumed sessions no longer appear below less-active newer ones.
- Transient SQLite read errors (locked db, torn snapshot) in the kanban, gateway, cron, operations, and response-store row-count readers now fail the affected source so the panels keep their last-good counts, instead of being swallowed into a false `0`. A genuinely absent table still legitimately reports zero, and a `conn.close()` failure on the WAL snapshot path no longer leaks its temporary directory.
- SQLite read errors in the remaining operations readers — `state_meta` maintenance metadata, `schema_version` introspection, project summaries, and discovered repos — now propagate to source health so the panels keep their last-good data, instead of being swallowed into empty metadata, a false schema version `0`, or empty lists. Genuinely absent optional tables (`state_meta`, `schema_version`, `discovered_repos`) still legitimately report empty.
- PRAGMA failures in column introspection (`PRAGMA table_info`) now propagate so the kanban source fails to its last-good data, instead of reading as "column absent" and silently degrading the column-aware block-kind, stale-claim, and enriched-task queries to empty results. A genuinely absent column or table still legitimately reads as absent.

### Changed

- WAL databases always use private temporary copies, preserving the zero-write guarantee even with a same-process writer. The shared `state.db` connection reuses its copy until the source changes, avoiding duplicate copies across unchanged refreshes and consumers. The `-wal` probe uses strict existence checking so an unreadable sidecar fails the source instead of silently serving checkpoint-lagging data via `immutable=1`.
- Contributor tooling alignment: coverage reporting no longer excludes real function bodies whose signatures contain `...` (variadic annotations) — the exclusion now matches stub-only lines; contributor docs reflect the full Python 3.11–3.14 CI matrix; the PR checklist uses the coverage-enabled test command that CI enforces; and the documented `Any`-type policy now matches what mypy actually enforces.

## [2026.9.8] - 2026-09-08

### Added

- Tools detail now reports toolset availability from the `availability` block of `cache/banner_snapshot.json` — `Toolsets: N enabled · unavailable: … · N lazy · N disabled` — and the compact view carries a `⚠ N toolsets unavailable` marker. The reader accepts both the `{name, env_vars, tools}` mappings hermes-agent 0.21 writes and plain name strings; unknown shapes read as empty.
- Operations now surfaces `cache/blocked-scripts/` (shell scripts the agent refused to run): a `Blocked scripts: N (newest Xh ago): a.sh, b.sh` detail row and a compact count. The scan is bounded to 200 entries, symlink-safe, stat-only — script contents are never read — and is registered as its own `blocked_scripts` health source, so an unreadable directory keeps the last-good counts.
- The Operations PR table gains **Open** and **Conflict** columns whenever a PR monitor reports per-PR review state.
- Config panel now reads the hermes-agent 0.21 `config.yaml` sections — `delegation`, `goals`, `updates`, `mcp_servers`, `plugins`, `tool_loop_guardrails`, `max_live_sessions`, `streaming`, `logging`, and `network` — into new "Agent limits" and "Integrations" detail sub-sections plus a compact `mcp N · plugins N · goals on` line. Only names, counts and flags are surfaced: MCP server config values are never rendered and a configured `network.proxy` shows as presence only.
- Skills / Integrations panel now surfaces the MCP schema cache (`~/.hermes/cache/mcp_schema_cache.json`, source `mcp_cache`) as cached server names, cache age, and a never-connected hint for configured-but-uncached servers, plus the skills prompt snapshot (`~/.hermes/.skills_prompt_snapshot.json`, source `skills_prompt`) as a "Prompted skills: N (snapshot 2h ago)" line. Cached payloads stay opaque and both files are ignored when they are symlinks.
- Cron panel now reads execution history from `~/.hermes/cron/executions.db`: per-job completed/failed/running counters over the last 24 hours, the last run's status, duration and first-line error excerpt, and a "Recent Executions" table of the last 10 runs in the detail view. Execution-history results are capped by `LIMIT` (which does not guarantee bounded scan work); a missing database or a missing `executions` table degrades to empty summaries rather than a failed source.
- Cron panel surfaces open incidents from `cron_incidents` in the same database — open and unacked counts in the compact view, and the latest five open incidents (job, state, failure type, first/last seen age, error excerpt) in the detail view. Older agents without the table report zero.
- Cron ticker health derived from `cron/ticker_heartbeat` and `cron/ticker_last_success`: a one-word `ok`/`failing`/`stale`/`unknown` status in the compact view and both stamp ages in the detail view. The existing `cron/.tick.lock` age remains the fallback tick indicator.
- Cron jobs now surface the newer `cron/jobs.json` keys — `failure_streak` (compact marker `✗3`), `paused_at`/`paused_reason` (`⏸`, set when either is present, so a job paused with a null reason is still marked), `last_delivery_error`, `last_dispatch` lateness and kind, `repeat` progress, and `no_agent` — with full values in the detail view.
- Execution history is collected as its own `cron_executions` source, so a corrupt `executions.db` falls back to the last-good history and is named in health without taking `jobs.json` and ticker data down with it.
- Gateway panel: event-loop liveness from `state/gateway.heartbeat` — a `loop:` status (`ticking`/`stale`/`wedged`/`unknown`) in the compact view and the heartbeat age in detail, derived from `updated_at` (offset-aware or naive-as-UTC) and falling back to the file mtime.
- Gateway panel: lifecycle from `state/gateway.lifecycle.json` (phase, last exit code and reason) plus an "ended without recording an exit" warning when the recorded pid of a `running` phase is no longer alive.
- Gateway panel: new `gateway_state.json` keys — running `code_sha`/`code_version`, config generation fingerprint and sources, session-store status, `exit_reason`, and per-platform `needs_attention`/`retrying_since`. A "config changed, restart needed" line appears when any recorded config source is older than the file on disk.
- Gateway panel: update receipts from `logs/update_receipts/latest.json` — outcome, finish age, from → to version, the first failed step, and a runtime code-skew warning when a planned runtime is pinned to a different build than the gateway.
- Gateway panel: restart history and delivery obligations from `state.db` (`gateway_heartbeats`, `delivery_obligations`) — incarnation count, restarts in the last 24 h, current incarnation uptime, pending/failed delivery counts, and the five newest undelivered obligations with a truncated last error. Message content is never read. The goal-state and gateway-ledger readers now share one mtime-cached `state.db` pass instead of snapshotting the (large, WAL) database twice.
- Operations panel now surfaces **async delegations** from `state.db` (`async_delegations`): a compact `Delegations: N running · N failed · N undelivered` line and a detail table of the 10 newest delegations with state, delivery state/attempts, owner liveness, duration, goal, and result/error excerpt, plus a count of live subagent transcripts under `cache/delegation/live/<id>/task-*.log`. Absent on older agents without the table.
- Operations detail gains a **State DB** section (schema version, db/WAL size, last auto-prune and auto-archive ages, file generation, FTS storage version) read from `state_meta` and `schema_version` through the same mtime-cached `state.db` open as goals — no extra snapshot per refresh.
- Operations detail gains **Snapshots** (count, total size, newest age) from `state-snapshots/`, collected as its own `state_snapshots` health source so a slow or failing multi-gigabyte scan degrades on its own instead of blanking the panel, and a **Web UI Build** row from `web-ui-build-stamp.json` beside the desktop build stamp.
- Tools panel process table now renders the `purpose`, `port`, and `profile` recorded in `spawn-ledger.json`, and marks processes whose recorded pid is gone with a `✗` next to the PID.
- Tokens / Cost now reads hermes-agent 0.21's `session_model_usage` table and shows per-model usage (API calls, tokens, cost) for all time plus 24h and 7d windows, with provider-billed `actual_cost_usd` shown plainly, estimates marked `est.`, and task-tagged auxiliary work collapsed into an `aux` subtotal; databases without the table keep the previous per-session model breakdown.
- Sessions now surfaces the hermes-agent 0.21 session columns — `display_name` (preferred over `title`), `pinned`, `git_branch`, `profile_name`, `chat_type`, `last_activity_at`/`last_activity_description`, `actual_cost_usd`/`cost_source`, and `compression_failure_error` (sanitized warning line) — all optional, so older schemas render as before.
- Sessions shows live agent surfaces from `~/.hermes/runtime/active_sessions.json`: an `N live` marker in the compact header and a detail table with surface, PID, and process liveness.
- Test coverage tooling: `pytest-cov` with branch coverage, enforced at 96% in CI; a PTY-based end-to-end TUI integration test; contract tests extended to panels 4, 5, 6, 8, 9, 10, and 11; Unicode/CJK rendering tests; snapshot-file symlink/traversal edge-case tests.
- `SECURITY.md` with a vulnerability reporting policy, and a Troubleshooting/FAQ section in the README covering non-TTY usage, the AGENT OFFLINE banner, footer health indicators, SQLite WAL snapshotting, and `--log-tail-bytes` tuning.
- CI now tests Python 3.11–3.14 on Linux plus Python 3.14 on macOS, smoke-runs the Docker image, checks the commit-pinned Nix flake, runs packaging checks in a single-version job, and tracks `uv` and Docker dependency updates with Dependabot.
- Published wheels include `py.typed`; sdists include the repository files required by their shipped test suite; artifact module smoke tests use isolated import mode so they cannot accidentally import the source checkout.

### Changed

- Internal: the duplicated ISO-8601 parsers and age helpers across `collect/gateway.py`, `collect/cron.py`, `collect/operations.py` and `collector.py` are consolidated into `collect/common.py` (`_iso_to_epoch`, `_age_seconds`); behaviour is unchanged.
- Internal: `collector.py` is split into a `hermesd/collect/` package of per-domain readers, with `hermesd.collector` kept as the public facade; no behaviour or import path changes.
- Internal: long panel `_render_detail` functions are split into named per-section helpers, with the shared section heading and age formatter moved to `hermesd/panels/formatting.py`; rendered output is byte-identical.
- Removed the dead `_cache_hits` counter and unified the duplicated WAL-snapshot-to-tempdir logic between `db.py` and `collector.py` into one shared helper.
- Package metadata now uses a PEP 639 SPDX license expression (`License-Expression: MIT`); Hatchling 1.32 emits Core Metadata 2.5 and Twine 7 validates the resulting artifacts.
- Dev-toolchain floor pins raised (`pip>=26.2` for PYSEC-2026-3721) and documented with an explanatory comment.
- Lockfile bumps the Linux-only dev transitive `cryptography` (via twine → keyring → secretstorage) from 49.0.0 to 50.0.1 to clear the PKCS#7 Bleichenbacher-oracle advisory; the shipped package does not depend on it.

### Fixed

- The Nix flake referenced the removed `flake-utils` input and nested its outputs as `<system>.packages` instead of `packages.<system>`, so `nix flake check` failed and `nix run github:mudrii/hermesd` / `nix develop` could not resolve any output; outputs are now per-system as flakes require and the CI flake check evaluates them.
- The Config panel read hermes-agent config keys that do not exist. The **Agent limits** and **Integrations** sections now read the real 0.21 keys: `delegation.max_concurrent_children` / `max_spawn_depth` / `orchestrator_enabled`, `goals.max_turns`, `updates.check` / `pre_update_backup` (legacy booleans map to `full`/`off`) / `backup_keep`, `tool_loop_guardrails.warnings_enabled` / `hard_stop_enabled`, and `plugins.enabled` / `plugins.disabled` counts. The invented `delegation.enabled`, `delegation.compression_threshold_tokens`, `delegation.max_parallel`, `goals.enabled`, `goals.turn_budget`, `updates.channel`, `updates.auto`, `tool_loop_guardrails.max_repeats` and `plugins` entry-count readings are gone; the compact line now reads `mcp N · plugins N · goals N`.
- `cron/state/pr_monitor.json` is a mapping keyed by PR number on hermes-agent 0.21, which the reader did not recognise — every live PR monitor reported 0 PRs. That shape is now detected and reports monitored/tracked counts, open and conflicting counts, and the newest `updatedAt` as the checked-at stamp.
- `memory_files` / `memory_file_count` counted `MEMORY.md.lock` and `USER.md.lock`, doubling the reported memory-file count. `*.lock` files and dotfiles are now excluded.
- Sessions, Kanban and Operations computed ages with `time.time()` instead of the collector's injected clock, so a JSON snapshot's ages drifted from its own `collected_at`. All three now measure against `state.collected_at`.
- The Cron detail job table falls back to the executions.db `last_error_excerpt` when `jobs.json` carries no `last_error`, so a failure that has already been cleared from `jobs.json` still shows an error.
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
- A text value in the `sessions.started_at` or `last_activity_at` column (SQLite is untyped) no longer fails the whole sessions source and every token panel with it; epochs are coerced before model construction.
- A corrupt `gateway_state.json`, `cache/mcp_schema_cache.json` or `.skills_prompt_snapshot.json` now names its source in the health footer while serving the last-good value, instead of degrading silently.
- `runtime/active_sessions.json` is capped at 200 entries so an oversized file cannot stall the collector with one liveness probe per entry.
- The opt-in live contract test applies the same hidden-session filter as the reader, so it passes on homes with hidden sessions.
- On Python 3.14, an unreadable `logs/` directory is reported as a failed source (with last-good lines kept) instead of "no logs": `Path.exists()` now returns False on permission errors there, so the logs reader uses a strict existence check that only treats absence as absence.
- Quitting no longer waits for a full collection pass: `close()` signals the in-flight pass, which stops after the current source and keeps last-good values for the rest.
- Logs and Skills detail views clamp a negative scroll offset to the top instead of slicing from the end and rendering an empty page with a `[-4--5/30]` counter.
- A `config.yaml` truncated to zero bytes (for example mid-write) now fails the config source and keeps the last-good summary instead of blanking the Config panel to defaults.
- A corrupt or undecodable session file in the legacy `sessions/` scan now fails the tools source and preserves the last-good tool inventory instead of silently shrinking it.
- Gateway no longer reports "config stale" permanently: staleness is now derived from `config.yaml`'s mtime against the running gateway's start time (`state/gateway.heartbeat`, `state/gateway.lifecycle.json`, then `gateway_state.json`, ignoring monotonic-clock values), instead of the orphan `config_generation` mtimes that no current hermes-agent writes. A symlinked `config.yaml` (dotfiles setups) is still evaluated, and no known start time reports not-stale.
- A process ID too large for the OS (for example `2**40` in `gateway_state.json`, `runtime/active_sessions.json` or `spawn-ledger.json`) no longer raises `OverflowError` and fails the whole source; it is simply reported as not alive.
- `runtime/active_sessions.json`, `desktop-build-stamp.json` and `web-ui-build-stamp.json` are now confined to `~/.hermes` like every other read, and `cron/executions.db`, `cron/ticker_heartbeat` and `cron/ticker_last_success` are confined to the Hermes home rather than only checked for being symlinks themselves — a symlinked `cron/` directory no longer escapes. An `executions.db` swapped for an outside path after a good read keeps the last-good executions and names the source failed.
- JSON/YAML files under `~/.hermes` are refused above 8 MiB instead of being parsed whole and retained forever; the 4.5 MB `models_dev_cache.json` still loads, and an over-cap file keeps the last-good value like a malformed one.
- Goal records in `state_meta` are now decoded through the same size-capped JSON helper as delegation payloads, and that cap is raised from 4 KiB to 64 KiB so realistic delegation tasks and results are no longer blanked.
- `state.db` is snapshotted once per change instead of twice per refresh: the goals/delegations/ledger readout now runs on the read-only connection `HermesDB` already holds, halving the WAL copy cost on non-APFS filesystems. A corrupt `state.db` still falls back to last-good data for those sources.
- Cron output lines are truncated to 4096 characters before redaction, matching the log reader, so a single 20 000-character line cannot slow a refresh.
- Database existence checks (`state.db`, `kanban.db`, `response_store.db`, `verification_evidence.db`, `projects.db`) use the strict existence probe, so on Python 3.14 an unreadable directory marks the source failed instead of reporting an empty database.

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
