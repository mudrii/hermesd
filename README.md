# hermesd

A real-time TUI monitoring dashboard for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

![hermesd overview](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/overview.png)

Screenshots are from `v2026.6.15`; panel content has grown since, but the layout and interactions are unchanged.

## Why This Exists

When you run Hermes Agent seriously — gateway handling Telegram, Discord, Slack, and WhatsApp simultaneously, cron jobs firing reminders, multiple CLI sessions with sub-agents spawning sub-agents, dozens of skills loaded, 7+ LLM providers configured — the information gets scattered fast.

**The problem:** there was no single place to answer the obvious questions:
- Is my gateway actually running? Which platforms are connected?
- How many tokens have I burned today and what's the estimated cost?
- Which sessions are active and how much context are they consuming?
- What cron jobs are scheduled, and did the last one succeed or fail?
- Which skills are installed and what do they do?
- What's in my error log right now?

The only way to answer these was running `hermes status`, `hermes sessions list`, `hermes cron list`, tailing log files, and mentally stitching together a picture from 5+ different sources. That friction adds up.

**The solution:** `hermesd` — a single terminal command that reads `~/.hermes/` and presents everything in one live-updating dashboard. Gateway health, sessions, tokens, costs, tools, cron, skills, logs — all refreshed automatically, no API keys, no network access, zero writes to your agent state.

It's not trying to replace the Hermes CLI or your Telegram interface. It's the at-a-glance overview layer that tells you whether everything is healthy and where your tokens are going — so you can make decisions without hunting for data.

## Features

### 13 Dashboard Panels

| # | Panel | What It Shows |
|---|-------|---------------|
| 1 | **Gateway & Platforms** | Live gateway PID, Hermes version, update status, drain/scale-to-zero state, served-profile record, per-platform profile routing and recorded shared-listener ingress URLs, multiplex-migration verdict, channel aliases/staleness, platform families, per-platform connection dots, and channel-directory inventory |
| 2 | **Sessions** | Active/total count, identity-verified live surfaces, display names, pinned sessions, branch/profile, last-activity age, compression-failure warnings, message/tool/API call totals, cwd, archived state, handoff metadata, and parent-session lineage |
| 3 | **Tokens / Cost** | Today's and all-time token usage, per-model usage from `session_model_usage` (all-time/24h/7d) with actual vs estimated cost, an auxiliary-work subtotal, cost-status reconciliation, recent-window rollups, and provider/endpoint breakdowns |
| 4 | **Tools** | Available tools count, toolset availability (enabled/unavailable/lazy/disabled), per-session call stats, background processes with purpose/port/profile and a dead-pid marker, filesystem checkpoints, full tool name grid |
| 5 | **Config** | Model, provider, personality, MoA, Tool Search, dashboard auth, kanban, code execution, gateway, routing and memory/session settings, agent limits (delegation concurrency/depth/orchestrator, goal turn budget, tool-loop guardrails, live sessions, streaming, logging) and integrations (MCP, plugins, updates, proxy presence) |
| 6 | **Cron** | Scheduler tick/provider, ticker health, recorded ticker error, missed-run catch-up policy and counter, open incidents, Chronos config presence, suggestion count, job table with schedule, delivery target, 24 h run counters, error count, latest error, and output metadata |
| 7 | **Skills / Integrations** | Provider auth status/freshness, credential pools, hooks/plugins/MCP inventory, plugin configured-activation gate, plugin manifest format and precedence conflicts, plugin install/catalog provenance with `--ref` drift, declared version gate and capabilities, MCP schema cache with no-cache-entry hint, prompted-skill snapshot, BOOT.md presence, skills with descriptions |
| 8 | **Logs** | Tailed agent, gateway, errors, cron, desktop, dashboard, GUI, update, gateway-error, crash, audit, MCP-stderr, and workspace logs with Tab switching, inline filtering, and a per-stream scope label (root vs. profile) |
| 9 | **Profiles** | Read-only profile discovery with session counts, log freshness, skill counts, DB size, and SOUL excerpts |
| 10 | **Memory** | Memory provider, MEMORY.md/USER.md word counts, learning summary, SOUL.md size/excerpt, and memory file inventory |
| 11 | **Kanban** | Read-only kanban task/run/event/comment counts, multi-board summaries, stale claims, dispatch config, active workers, blocked/failing tasks, and recent runs |
| 12 | **Operations** | Dashboard process count, Desktop and Web UI build stamps, Response Store, verification evidence, goals, async delegations, state.db maintenance, snapshot backups, MoA trace metadata, Projects/correlations/newest repos, model-cache summaries, PR monitor state (including open/conflicting PR counts), and blocked-script counts |
| 13 | **Curator** | Scheduler state plus newest memory-curation run: skill before/after counts, archived/pruned/added totals, model/provider, duration, tool-call total + per-tool breakdown, state-transition trail, and LLM summary or error |

### Key Features

- **Read-only** — hermesd never writes to `~/.hermes/` or modifies Hermes Agent state
- **Live-updating** — polls every 5 seconds (configurable with `--refresh-rate`)
- **Snapshot mode** — `--snapshot` renders one overview frame to stdout and exits; `--snapshot-panel N` selects any registered detail panel for text snapshots and annotates JSON snapshots (`0` aliases panel 10); `--snapshot-file PATH` writes either form to disk outside the Hermes home; `--snapshot-format json` emits machine-readable full-state snapshots
- **Bounded log reads** — `--log-tail-bytes` caps how much of each log file and cron output excerpt is read per refresh
- **Opt-in profiles** — root mode stays the default; use `--profile NAME` or `HERMES_PROFILE=NAME` to read profile-scoped runtime data
- **Adaptive layout** — full 13-panel grid on wide terminals, a tall-narrow single-column overview for vertical tmux splits, and a denser all-panel overview on 80x24
- **Detail views** — press `1`-`9` or `0` for panel 10 to expand directly, or use `[` / `]` to move through every panel including Kanban, Operations, and Curator
- **Focus toggle** — press `f` to jump between the overview and the last selected full-screen panel
- **Clipboard export** — press `c` to copy the current rendered view as plain text via OSC 52 in compatible terminals
- **Inline detail filters** — press `/` in Sessions or Logs detail view to live-filter the current table/log stream with field-aware queries, including message-content and severity-threshold filters
- **Session sorting** — press `s` in Sessions detail to cycle recent/cost/token ordering
- **Jump navigation** — press `g` / `G` in scrollable detail views to jump to the top or bottom
- **Footer health indicator** — a green/yellow/red dot shows how many collector sources succeeded on the last refresh, with failed source names surfaced inline when degraded
- **Header status** — the top-left header shows the installed `hermesd` version, while the header/footer surface an `AGENT OFFLINE` warning when Hermes Agent appears inactive
- **Scrollable detail views** — `j`/`k` scroll Sessions, Skills, and Logs; Sessions scrolling includes the full rendered content, not just the session rows
- **Profile inspection** — press `p` inside the Profiles panel to cycle the viewed profile without changing the selected data source
- **Resilient** — keeps showing last known good data on transient SQLite and log-read failures
- **Theme-aware** — inherits your Hermes Agent skin and updates live when `config.yaml` changes
- **SSH/tmux compatible** — `tty.setcbreak` mode, escape sequence handling for remote terminals
- **Cost estimation** — computes ~USD from token counts when the provider doesn't report costs
- **Zero config** — no config file, no API keys, just `hermesd` and go

## Screenshots

### Overview — The Full Picture

The main dashboard shows all 13 panels at a glance. The header starts with the installed `hermesd` version, then shows the current profile mode and time on the right. Gateway status with PID and Hermes Agent version sits at the top (note the `discord ⚠` connection-error marker), sessions and token costs side by side, tools and config, cron and skills, logs plus profile metadata, and dedicated memory, kanban, operations, and curator panels at the bottom. The footer shows keyboard shortcuts and a polling indicator.

![Overview](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/overview.png)

### [1] Gateway & Platforms — Is Everything Connected?

Press `1` to expand. Shows whether the gateway process is alive (with correct PID even after launchd restarts), Hermes version with update status, the served-profile record, busy/drainable state, external drain markers, scale-to-zero idle timeout and relay-only intent, channel-alias inventory/staleness, platform family labels, and a per-platform table with connection state, a **Profile** column, an **Owner** column, last-seen timestamps, and an **Error** column surfacing per-platform connection failures (e.g. discord "failed to reconnect"), plus the active-agent count and a restart-requested marker. Catches the "gateway says running but the PID is dead" case.

**Shared-listener routing and ingress.** Under `gateway.multiplex_profiles` one default gateway serves every profile, and it keys each served profile's adapter as `<profile>:<platform>` in `gateway_state.json`. hermesd splits that key against the grammar upstream itself validates unconditionally, so the platform table shows the platform in **Platform** and the profile it belongs to in **Profile** (the compact strip renders `dev/telegram`) — and a key that fails the grammar is kept verbatim with no profile, because splitting an arbitrary string out of a process-local JSON file would invent a profile name. Each entry's recorded `ingress_url` — the shared-listener callback URL a vendor console needs — is surfaced in a **Shared-Listener Ingress** section, redacted at the data boundary like every other URL, and only where upstream would surface it: nothing is kept when the gateway is not live, when the value is empty, or when the adapter state is `fatal`/`disconnected`/`stopped`. The section states plainly that these are values the gateway *recorded* and that hermesd never requests them; a bare path (`/p/dev/telegram/webhook`) means the default profile had no live listener when the URL was recorded, and is flagged as such.

**Served profiles are a tri-state, not a list.** `served_profiles` absent, unparseable, and explicitly empty all used to render identically. An empty list from a *live* gateway is authoritative — it serves nobody else — and now says so; an absent key renders nothing; and a list left behind by a gateway that is not live is labelled `(record, gateway not live)` instead of being presented as the current topology. A served profile writes no `gateway_state.json` of its own, so under `--profile dev` the root file's `dev:<platform>` entries are the only evidence of its routing.

**Multiplex migration is progress evidence, never proof.** `gateway_migration.json` is written *inside* the per-secondary loop, before hermes-agent flips `gateway.multiplex_profiles` and before it restarts the default gateway, and is never updated afterwards — the verified and the applied-but-unverified exit paths both leave the identical file behind, and it carries no completion field. A manifest on disk therefore cannot distinguish a migration in flight, one that crashed after a single secondary, one that was applied but never verified, and one that fully succeeded; rollback deletes it on success, so its absence cannot distinguish "never migrated" from "rolled back" either. The **Multiplex Migration** section keeps three things apart: the *recorded intent* (`migrated_at`, always labelled `Started:` because it is when the attempt began, plus the recorded `flag_was`, homes and service managers), *intermediate progress* (flag flipped? default gateway live? a live served record? which recorded profiles does it cover?), and one *verified current topology* verdict. That verdict is `● multiplexed (verified)` only when the manifest parses, `gateway.multiplex_profiles` is on, the default gateway is live, and the live `served_profiles` covers `default` plus every secondary the manifest recorded; otherwise it is `⚠ migration unverified` with the specific missing evidence named. Because the flag is read from config — where an environment override hermes-agent honours is invisible to hermesd — the verdict is always labelled "as recorded in config". The panel never says "migrated". The manifest's recorded `home` values are display data only and are never used to build a path hermesd reads. Note also that upstream verifies against every profile in its *plan*, which includes profiles that never had a standalone gateway and so are absent from the manifest: hermesd can only see the manifest, so its expected set is a subset of upstream's.

**Platform record ownership.** `gateway_state.json` re-stamps its top-level `pid`/`start_time` on every write, while each platform entry keeps the `writer_pid`/`writer_start_time` of the process that recorded it. hermesd compares the two by exact equality — the same rule upstream's `/api/status` uses to tell a live record from a preserved one — and renders `current`, `⚠ preserved` (the entry outlived the gateway life that wrote it, so its state may describe a process that is gone), or `—` when the record carries no usable writer identity (an older gateway, or a host that could not resolve a process start time). Ownership is evaluated separately from heartbeat freshness: a ticking event loop says nothing about who wrote a given platform row, and the compact view adds `⚠ N platform record(s) outlived their writer`. Because the comparison needs a start-time stamp on both sides, a matching PID alone is never treated as identity — the same PID reused by a later process reads as preserved, not current.

**Liveness, lifecycle and updates.** The compact view adds a `loop:` indicator next to the running dot — `ticking` (heartbeat ≤ 90 s), `stale` (≤ 300 s), `wedged` (older while the gateway still claims to be running), or `unknown` — plus one-line warnings for "config changed, restart needed", "update unfinished", "code skew", "migration unverified", and any pending/failed deliveries. Platforms flagged `needs_attention` get a `!` marker.

The detail view adds a **Liveness** section (heartbeat age, incarnation count, restarts in the last 24 h, current incarnation uptime, running code version/short sha, config generation, session-store status), the gateway lifecycle (phase, last exit code/reason, and a warning when the previous life ended without recording an exit), an **Updates** section (outcome, finish age, from → to version, first failed step, the recorded post-restart fleet matrix as state counts, and the runtime code-skew verdict with the evidence behind it), the **Shared-Listener Ingress** and **Multiplex Migration** sections described above, and a **Delivery Obligations** table with the five newest undelivered messages (platform, state, attempts, age, truncated last error — message content is never read). The platform table gains a **Retrying** age column and a `! needs attention` status marker.

Sources: `state/gateway.heartbeat`, `state/gateway.lifecycle.json`, the `code_sha`/`code_version`/`config_generation`/`session_store`/`exit_reason`/`served_profiles` keys and the per-platform `ingress_url` of `gateway_state.json`, `logs/update_receipts/latest.json`, `gateway_migration.json`, the `gateway.multiplex_profiles` key of `config.yaml`, and the `gateway_heartbeats` / `delivery_obligations` tables of `state.db` (all optional; anything missing renders as `—`).

![Gateway Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-01-gateway.png)

### [2] Sessions — What’s Active and Where Did It Fork?

Press `2` to expand. An **Activity** section (hermes-agent 0.21 and newer) shows each session's display name (`display_name`, falling back to `title`), branch, profile, chat type, age from `last_activity_at` (falling back to `started_at`), and last-activity description, with a pin marker on pinned sessions; a **Live Surfaces** section lists the surfaces recorded in `runtime/active_sessions.json` with their PID and an identity-verified liveness state (`live`, `dead`, or `unverified`); a **Warnings** section shows truncated `compression_failure_error` text; a **Runtime** section surfaces API call counts, cwd, archived state, rewind count, and handoff metadata; and a **Billing & Context** section shows each session's end reason, billing endpoint, billing mode, and the model's context-window limit (joined from `context_length_cache.yaml`). Session costs prefer a positive provider-billed `actual_cost_usd` and also preserve an explicit zero when its cost status is authoritative (`reported`/`exact`/`included`); the compact header shows `N surface(s)` for the registry entries plus identity-verified `M live` and `K unverified` counts, so a registry entry is never presented as a running turn.

**Surface liveness verifies process identity, not just the PID.** PIDs are reused, so "a process with this PID exists" is not evidence that the recorded session is still running. hermesd compares the registry's `process_start_time` against the start time observed for that PID on this host and reports `live` (identity matched), `dead` (the PID is gone, *or* it now belongs to a different process), or `unverified` (the PID exists but the start time was never recorded or could not be observed here). The two sources use different units — this registry records **epoch seconds**, while `gateway_state.json` records **centiseconds** — and are never compared against each other. Start times are read from `/proc/<pid>/stat` field 22 on Linux, and elsewhere from a single bounded `ps -o lstart=` call covering every PID at once, since hermesd has no `psutil` dependency; `lstart` reports whole seconds against a fractional recorded stamp, so identity is matched within a 2-second tolerance rather than by exact equality. A PID that cannot be observed is reported as unverified, never as dead. Press `/` to filter the currently loaded sessions by ID, source, model, lineage, provider, title, cwd, archived state, handoff state, platform, or message content via `message:term`, and press `s` to cycle recent/cost/token sorting. Use `j`/`k` or `g`/`G` to reach the full detail content at smaller terminal heights. Databases without the 0.21 columns simply omit the new sections.

![Sessions Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-02-sessions.png)

### [3] Tokens / Cost — Where Are My Tokens Going?

Press `3` for the full per-session token breakdown plus recent `7d`/`30d` rollups and read-only model/provider/**endpoint** cost summaries, and a **Cost Status** reconciliation line (unknown vs subscription-included vs estimated). When hermes-agent 0.21's `session_model_usage` table is present, **By Model** is driven by it: per-model API calls, tokens and cost aggregated across sessions (top 50 by token volume), with provider-billed costs shown plainly and estimates marked `est.`, task-tagged auxiliary work collapsed into a single `aux` subtotal row, and a 24h/7d window summary line; the compact view adds a top-3 model line. Older databases keep the per-session model breakdown unchanged. In both the compact and detail views, costs carry a `~$` prefix when estimated and a plain `$` prefix when the provider cost is authoritative (`reported`/`exact`/`included`); subscription-`included` sessions render an authoritative `$0.00`.

![Tokens Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-03-tokens.png)

### [4] Tools — What's Available and What's Being Used?

Press `4` for four sections: **Tool Calls** showing the current call leaders by name (tool names when the `messages` table provides them, otherwise fallback session labels), **Available Tools** listing the union of tools discovered across session files in a 3-column grid followed by a `Toolsets: N enabled · unavailable: … · N lazy · N disabled` line read from the `availability` block of `cache/banner_snapshot.json` (the compact view shows a `⚠ N toolsets unavailable` marker whenever any toolset failed to load), **Background Processes** showing the live registry (`spawn-ledger.json`, falling back to the legacy `processes.json`) with PID, purpose, port, profile, notify-on-complete, watch-pattern summary, start time, and command — a PID whose process is gone is marked with `✗`, and absent purpose/port/profile render as `—` — and **Checkpoints** showing filesystem shadow repos with workdir name, commit depth, and latest checkpoint reason. The compact view shows the top callers plus the current background-process and checkpoint counts.

![Tools Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-04-tools.png)

### [5] Config — Current Agent Configuration

Press `5` for the full config key-value table: model, provider, personality, max turns, reasoning effort, compression threshold, secret redaction, approval mode, provider routing summary, smart routing, fallback model, dashboard theme/auth/public URL, session reset mode, memory provider, Tool Search, toolsets, code execution, kanban dispatch settings, gateway media trust, MoA preset/reference/aggregator/trace settings, and auxiliary slot count. Tool Gateway domain, scheme, Firecrawl endpoint, and route token presence are shown from config plus environment with secret-bearing values redacted.

Two further sub-sections summarise the hermes-agent 0.21 config sections when they are present: **Agent limits** (`delegation.max_concurrent_children`, `delegation.max_spawn_depth`, `delegation.orchestrator_enabled`; `goals.max_turns`; `tool_loop_guardrails.warnings_enabled` / `hard_stop_enabled`; `max_live_sessions`, `streaming.enabled`, `logging.level`) and **Integrations** (configured MCP server count and names, `plugins.enabled`/`plugins.disabled` counts, `updates.check` / `updates.pre_update_backup` / `updates.backup_keep`, and whether a network proxy is configured). Both sections list only non-default values and fall back to `—`. Values are never rendered for these keys — MCP server configs and proxy URLs can embed credentials, so hermesd reports names, counts, and flags only. The compact view adds one `mcp N · plugins N · goals N` line.

![Config Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-05-config.png)

### [6] Cron — Scheduled Jobs

Press `6` to see cron scheduler state, provider, Chronos managed-cron config presence, persisted suggestion counts, max parallelism, response wrapping mode, all configured jobs, delivery targets, current state, last execution status, latest error, and latest saved output metadata from `~/.hermes/cron/output/`. `[SILENT]` runs are surfaced explicitly so “nothing to report” is distinguishable from missing output.

**Ticker health** is derived from `~/.hermes/cron/ticker_heartbeat` and `~/.hermes/cron/ticker_last_success` (one epoch float each, written by the 60 s ticker): `ok` when the heartbeat is under 120 s old and the last success under 600 s, `failing` when the heartbeat is fresh but the last success has fallen behind, `stale` when the heartbeat itself has stopped, and `unknown` when neither file exists. A recorded `cron/ticker_last_error` escalates an otherwise `ok` ticker to `failing`, because hermes-agent only unlinks that marker on a clean tick — so a marker that is still there means the last tick failed even inside the 600 s window. It never escalates `unknown` (no heartbeat means hermesd cannot claim the loop runs at all) and never downgrades `stale`. Both ages are shown in the detail view; the existing `cron/.tick.lock` age remains as the fallback tick indicator. Note a threshold divergence that is deliberately *not* papered over: `hermes cron status` calls the ticker STALLED after `TICKER_INTERVAL_SECONDS * 3 + 20` ≈ 200 s, while hermesd's last-success window is 600 s, so there is a range where `hermes cron status` already says STALLED and panel 6 still says `ok`/`failing`.

**Missed-run catch-up** is its own detail section, because two of hermes-agent's markers describe dropped work and neither can be read as a clean bill of health. `cron/catch_up_occurrences` is a monotonic lifetime counter that `_fast_forward_missed_recurring` increments when a recurring job's `next_run_at` was more than its grace window in the past; it carries no timestamp and is never reset, so it is rendered as a lifetime total and never as a rate or an age. Upstream's own reader returns `0` for a missing file and for a genuine zero alike, which is the one distinction an operator needs, so hermesd keeps them apart: `Catch-ups: no counter observed` versus `Catch-ups: 0 recorded`. The configured policy is shown beside it from `cron.catch_up_missed` in `config.yaml`, read with upstream's exact cast — an identity check, so only a literal YAML `false` disables catch-up while `null`, `0`, `'no'` and a missing key all leave it on. When it *is* off the panel flags it distinctly, because that is the silent-failure case: hermes-agent then re-anchors the schedule without recording anything, so missed runs are dropped and the counter stays flat while they are. Per job, a `last_dispatch.kind` of `catch_up` is labelled `catch-up after missed fire` rather than shown as `late`, matching upstream's CLI wording — the two mean different things.

Neither marker proves anything by its absence, and the detail view says so on every render: `ticker_last_error` is **deleted** on the next clean tick, so a ticker that failed for hours and then recovered leaves no trace, and `catch_up_occurrences` is written best effort and not at all while catch-up is off. Under `--profile` both are read from the root store, like the rest of `cron/`, so a selected profile's own tick failure and catch-up counter are invisible (see `.codex/rules/source-ownership.md`).

**Execution history** comes from `~/.hermes/cron/executions.db` (read-only). The recent-executions list is capped; SQL aggregates cover the whole retained table for the 24h window, and each job's latest run is selected across its history. Timestamps are compared chronologically, including UTC offsets and naive-as-UTC values. These queries may scan historical rows; a capped result does not guarantee bounded scan work. Each job shows its last-24-hour counters (`7✓ 2✗ 1▶ 1?` for completed / failed / running / unknown) alongside the last run's status, duration (`finished_at − started_at`), and first-line error excerpt. The `?` bucket is shown rather than absorbed: `unknown` is a real hermes-agent terminal status, and any status hermesd has not seen lands there too, because folding an unresolved outcome into `✗` would imply a retry is safe. The underlying `total_24h` denominator is exposed in the JSON snapshot so the four buckets can be checked to reconcile. The detail view adds a **Recent Executions** table (the last 10 runs with job name, status, start age, duration, and error excerpt) and an **Open Incidents** table from `cron_incidents` (job, state, failure type, first/last seen age, error excerpt) with open and unacked counts. Both degrade to empty summaries — never an error — when the database or either table is missing, as on older agents.

**Delivery is reported separately from execution.** A run can complete successfully and still have its notification suppressed, so the two are never collapsed into one number. Where the schema has the newer `delivery_outcome` / `handoff_pending` / `scheduled_instant` columns, the Recent Executions table gains a **Delivery** column (`suppressed`, `delivered`, `queued`, `not_configured`, `suppressed_acked`, `failed`, or `unrecorded`, plus a `⤵ handoff` marker) and the detail view adds a **Delivery Outcomes (24h)** section counting each recorded outcome verbatim per job, with unrecorded rows and pending handoffs kept apart. Outcomes hermesd has not seen are preserved as written rather than bucketed, and the retained vocabulary is bounded per job. A schema without those columns reports `delivery_tracked: false` and renders no delivery column at all — an older agent recording nothing is not the same as a window where every delivery went unrecorded.

The job rows also surface the newer `cron/jobs.json` keys: `failure_streak` (shown as `✗3` in the compact row), `paused_at`/`paused_reason` (`⏸` — either one alone is enough to mark a job paused), `last_delivery_error`, `last_dispatch` lateness and kind, `repeat` progress, and `no_agent` script-only jobs.

**Retention qualification.** hermes-agent prunes terminal execution history to `MAX_TERMINAL_EXECUTIONS` (1000) records, keeping the newest by `finished_at`; in-flight rows are never pruned. Every aggregate therefore describes *recorded attempts*, not every attempt that ever happened, and the detail view says so: a `Recorded attempts: N retained (M terminal) spanning Xs to Yh ago` line exposes the retained counts and the observed bounds. When the terminal count reaches the cap, a qualified warning notes that older terminal runs were pruned — and explicitly that a capped history does not by itself make any particular 24h window incomplete, since the retained span can cover far more than a day. `retention_cap` is `0` when the table could not be read at all, which is distinct from an empty one.

![Cron Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-06-cron.png)

### [7] Skills & Integrations — What's Installed?

Press `7` for provider and integration visibility in one place: **Providers** with active auth state and safely persisted credential freshness/expiry metadata, **Credential Pools** with redacted metadata, **Hooks** discovered from `~/.hermes/hooks/`, **Plugins** from `~/.hermes/plugins/`, **MCP Servers** from `config.yaml` with secret-bearing args and URL query params redacted, `BOOT.md` presence, and **Skills** grouped by category with descriptions loaded from each skill's `SKILL.md` frontmatter. Use `j`/`k` to scroll through the full skill list.

The **Plugins** table reports *configured activation*, not a yes/no "is it on" flag, because hermes-agent requires an explicit `plugins.enabled` opt-in — a plugin that is merely present on disk will not load. The **Activation** column reproduces upstream's gate order and shows one of `enabled`, `disabled` (in `plugins.disabled`, which wins over the allow-list), `not enabled` (discovered but absent from `plugins.enabled`), `category` (an `exclusive` memory provider, activated through `<category>.provider` config instead), `removed` (a legacy Relay plugin key that core now refuses), or `unknown` (a manifest that exists but did not parse). Both the path-derived manifest `key` and the bare `name` are matched against each list, so a plugin opted in under its legacy name is still recognised. Where a manifest omits `kind`, hermesd scans the first 8 KiB of the plugin's `__init__.py` for provider markers exactly as upstream does — it reads text and never imports or executes plugin code. Activation is what the configuration says hermes-agent *would* do; it is not proof the plugin loaded successfully.

Discovery covers both directory shapes and all three manifest filenames hermes-agent accepts. A flat `~/.hermes/plugins/<name>/` is keyed by its manifest `name`; a category `~/.hermes/plugins/<category>/<name>/` is keyed by its **path** (`observability/tracer`), which is the spelling `plugins.enabled`/`plugins.disabled` actually contain. A directory with no manifest is a *category*, not a plugin: it is never reported on its own account and is recursed into exactly one level, matching upstream's depth cap. Manifests are accepted in upstream's precedence order — `plugin.yaml`, then `plugin.yml`, then a portable Agent Plugin `plugin.json` — and a directory carrying more than one is resolved by that precedence **and reported**: the winning filename lands in `manifest_file`/`manifest_format`, the losers in `manifest_shadowed`, and the detail view lists the conflicts under the table rather than resolving them silently. A portable manifest is validated against the v1 Agent Plugins schema and its name grammar before it is believed, and only `name`/`version`/`description` are carried over, because that is all upstream's own mapping keeps — a `plugin.json` that declares `kind`, `capabilities` or `requires_hermes` is not read as declaring them. Any format that is present but unusable degrades to `unknown` with a reason instead of disappearing. Unlike upstream, hermesd refuses a *symlinked* manifest (including the symlinked `plugin.json` upstream accepts) and an oversized one; both are what stop a plugin tree from steering a read outside `~/.hermes`. The walk itself is budgeted — at most 200 entries per directory and 200 retained plugins — and hitting either bound sets `plugin_scan_truncated`, rendered as `⚠ plugin list truncated` in the detail view and a `+` after the count in the compact view, so a bounded sample never presents itself as an inventory.

The **Provenance** column keeps two different claims apart, because they are written by two different sidecars and can disagree. `catalog:<tier>@<sha8>` comes from `<plugin_dir>/.hermes-catalog.json` and is the commit the Hermes plugin **catalog reviewed**; `git@<sha8>` / `git pinned@<sha8>` comes from `~/.hermes/plugins/.install-metadata.json` (one file keyed by manifest *name*, read once per pass) and is the commit **actually installed**, with `pinned` set only for an explicit `hermes plugins install --ref`. Those are the same two annotations `hermes plugins list` renders. They differ by design when `--ref` is used: `install_catalog_entry` installs at `ref or entry.sha` and then writes the sidecar from the catalog entry, so the sidecar keeps describing the reviewed commit while the metadata records the checked-out HEAD. hermesd therefore shows both plus a `⚠ drift` marker when the two full 40-hex SHAs disagree, and drops the redundant second token when they agree — the catalog sidecar alone is not evidence of what code is on disk. Both `source` and `repo` URLs are passed through the same secret-URL redactor as every other URL hermesd reads, at the data boundary rather than in the panel, so a credential that survived upstream's own scrubbing still never reaches the display or `--snapshot-format json`. A revision that is not a full 40-hex SHA is reported as no revision at all: a corrupt sidecar is not a pin, and drift is never claimed from a value hermesd could not parse.

The **Declares** column shows what a manifest *claims* — its `requires_hermes` version gate and a `caps:N` count of declared capabilities — and the note under the table states on every pass that activation, provenance and declarations are all read from files and that hermesd never imports plugin code, so none of them proves a plugin loads. That wording is deliberate: upstream evaluates `requires_hermes` against the running version at load time and gates every declared capability behind an explicit grant under `plugins.entries.<id>.granted_capabilities`, neither of which is observable from outside. The capability count is the full declared one, not the length of the display-bounded list, so capping that list cannot change what the panel reports. Declared capability ids are preserved as written rather than filtered to the ids this build recognises — for a consent screen dropping an unknown id is the fail-closed choice, but for a dashboard an unrecognised declaration is exactly what an operator needs to see.

An **MCP** section summarises `~/.hermes/cache/mcp_schema_cache.json`: how many servers have a cached schema, their names, the cache age, and a **No cache entry** row listing servers configured in `config.yaml` with no entry in that cache. The row is deliberately worded as an observation, not a history — a missing entry does not prove a server never connected, since the cache may have been cleared, invalidated, or written under another profile; when the cache file itself is absent the section says `no cache file observed` instead. Membership is computed from the complete configured and cached name sets, so the 20-name display cap cannot make a cached server look uncached; when a list is capped the row shows `(+N more)` rather than silently looking complete. Cached payloads are treated as opaque and never rendered. A `Prompted skills: N (snapshot 2h ago)` line summarises `~/.hermes/.skills_prompt_snapshot.json`. The compact view adds `mcp N cached` when the cache is populated.

![Skills Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-07-skills.png)

### [8] Logs — What Just Happened?

Press `8` for the full log viewer with discovered streams such as **agent**, **gateway**, **errors**, **cron**, **desktop**, **dashboard**, **gui**, **update**, **gateway.error**, **tui crash**, **workspace**, **workspace.error**, **audit**, and **mcp.stderr** (each shown only when its file exists). Press `Tab` to switch between them, `/` to filter the current log stream by `level:`, `minlevel:`, `component:`, `session:`, or free text, `j`/`k` to move the viewport, and `g`/`G` to jump to the top or bottom.

The stream list is not one directory: **agent**, **gateway** and **errors** come from the selected profile's `logs/`, while the rest come from `~/.hermes/logs/`. Since a stream's file name alone cannot tell `profiles/coding/logs/agent.log` apart from `logs/agent.log`, the detail view prints a `Scope: profile` / `Scope: root` line under the tab bar for the stream you are reading, and `--snapshot-format json` carries the same value as `logs.streams[].scope`. The label names the resolver that *owns* the stream, not the directory it happened to resolve to — with no profile selected everything lives under `~/.hermes/`, but the profile-owned streams still read `profile`. Which scope owns every other source is recorded in [`.codex/rules/source-ownership.md`](.codex/rules/source-ownership.md).

![Logs Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-08-logs.png)

### [9] Profiles — Which Runtime State Am I Looking At?

Press `9` for a read-only profile table discovered from `~/.hermes/profiles/*/`. It shows per-profile session count, latest log mtime, skill count, DB size, and a short `SOUL.md` excerpt when present. Press `p` in this panel to cycle the viewed profile highlight without changing the dashboard's selected data source.

![Profiles Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-09-profiles.png)

### [10] Memory — What Context Is Persisted?

Press `0` to expand. The Memory panel shows the configured memory provider, memory-file count, `MEMORY.md` and `USER.md` word counts, a lightweight learning summary from skill usage and learned skill metadata, `SOUL.md` size, and a short `SOUL.md` excerpt with the discovered memory files listed below.

![Memory Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-10-memory.png)

### [11] Kanban — What Are Workers Doing?

Use `]` from panel 10 or `--snapshot-panel 11` to expand. The Kanban panel reads `~/.hermes/kanban.db` and `~/.hermes/kanban/boards/*/kanban.db` in read-only mode and shows board counts, the current board, stale claim counts, typed blocker counts, configured dispatch mode, status breakdowns, active worker claims, blocked/failing tasks, recent run outcomes, a parent→child **Decomposition Tree** (`task_links`), and a **Task Metadata** table (branch, workspace, goal mode, current step) when those columns are populated.

![Kanban Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-11-kanban.png)

### [12] Operations — What Runtime Artifacts Exist?

Use `]` from Kanban or `--snapshot-panel 12` to expand. The Operations panel summarizes dashboard background processes, Desktop build metadata, **Response Store** stats (conversation/response row counts + size from `response_store.db`), **Verification Evidence** from `verification_evidence.db`, active/waiting **Goals** from `state.db`, **MoA Traces** inventory and bounded latest-record metadata from `moa-traces/*.jsonl`, **Projects** from `projects.db` with missing primary paths, newest discovered repos, and verification/Kanban correlations, model-cache provider/model counts, cache ages, and PR monitor files across all of the agent's naming families (flat + subdirectory) with per-repo dedup. `cron/state/pr_monitor.json` is also read in its PR-keyed shape (a mapping of PR number to `{state, mergeable, title, updatedAt, …}`), which adds **Open** and **Conflict** columns to the PR table. A **Blocked scripts** row counts the shell scripts the agent refused to run under `cache/blocked-scripts/` with the newest age and the three newest file names — a bounded, symlink-safe, stat-only scan; the script contents are never read. It also reports **Delegations** from the `async_delegations` table in `state.db` — a compact `Delegations: N running · N failed · N undelivered` line whenever any counter is non-zero, and a detail table of the 10 newest delegations (id, state, delivery state and attempt count, whether the owner pid is still alive, duration, the goal parsed out of `task_json`, and the status/error excerpt parsed out of `result_json`), alongside a count of live subagent transcripts under `cache/delegation/live/<id>/task-*.log`. Both JSON columns are size-capped before parsing and every excerpt is clipped to 80 characters. A **State DB** section reports the `schema_version`, database and WAL size, last auto-prune and auto-archive ages, `db_file_generation` and `fts_storage_version` from `state_meta`; all of it comes from the same mtime-cached `state.db` open as the goals, so a refresh never snapshots the (multi-hundred-megabyte) WAL database twice. **Snapshots** summarizes `state-snapshots/` (count, total size, newest age) from a bounded, one-level, stat-only scan registered as its own `state_snapshots` health source, and **Web UI Build** shows the short content hash and build age from `web-ui-build-stamp.json`. Delegation tables and build stamps are simply omitted when the agent version does not produce them.

![Operations Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-12-operations.png)

### [13] Curator — What Did the Last Memory Curation Do?

Use `]` from Operations or `--snapshot-panel 13` to expand. The Curator panel reads `skills/.curator_state` for scheduler pause/run/report state and the newest usable non-symlinked `~/.hermes/logs/curator/<stamp>/run.json` for skill before/after/delta counts, archived/added/pruned/consolidated totals, the model and provider used, run duration, total tool calls plus a per-tool call breakdown, the state-transition trail, and the LLM summary (or error).

![Curator Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-13-curator.png)

## Installation

Requires Python 3.11+ and a working [Hermes Agent](https://github.com/NousResearch/hermes-agent) installation (`~/.hermes/` must exist).

### Via pip

```bash
pip install hermesd
hermesd
hermesd --snapshot
```

### Via uv

```bash
uv tool install hermesd
hermesd
```

### From Source

```bash
git clone https://github.com/mudrii/hermesd.git
cd hermesd
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e .
hermesd
```

### Docker

```bash
docker build -t hermesd .
docker run -it -v ~/.hermes:/home/hermesd/.hermes:ro hermesd
```

### Nix Flake

```bash
# Run directly
nix run github:mudrii/hermesd

# Dev shell
nix develop github:mudrii/hermesd
```

## Usage

```bash
# Launch the dashboard (reads ~/.hermes by default)
hermesd

# Custom hermes home directory
hermesd --hermes-home ~/.hermes-work

# Read profile-scoped runtime data from ~/.hermes/profiles/coding
hermesd --profile coding

# Faster polling (every 2 seconds)
hermesd --refresh-rate 2

# Disable colors
hermesd --no-color

# Show version
hermesd --version

# Write a one-shot overview snapshot outside the Hermes home
hermesd --snapshot-file /tmp/hermesd.txt

# Export a single panel detail snapshot
hermesd --snapshot-panel 10

# Panel 10 also accepts the interactive shortcut alias
hermesd --snapshot-panel 0

# Emit a machine-readable JSON snapshot of the full dashboard state
hermesd --snapshot-format json

# Annotate a JSON snapshot with a selected panel number
hermesd --snapshot-panel 8 --snapshot-format json

# Export new higher-numbered panels
hermesd --snapshot-panel 11
hermesd --snapshot-panel 12 --snapshot-format json
hermesd --snapshot-panel 13

# Reduce log-read overhead for large log files and cron output excerpts
hermesd --log-tail-bytes 8192
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_HOME` | `~/.hermes` | Override the Hermes home directory |
| `HERMES_PROFILE` | unset | Read profile-scoped runtime data from `profiles/<name>`; root mode remains the default when unset |

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `1`-`9`, `0` | Expand panels 1-10 to full-screen detail view (`0` opens panel 10) |
| `[` / `]` | Move to the previous/next registered panel, including panels 11-13 |
| `Esc` | Return to overview |
| `f` | Toggle focus mode for the last selected panel |
| `c` | Copy the current rendered view as plain text via OSC 52 |
| `j` / `k` | Scroll down/up in scrollable detail views |
| `Tab` | Cycle log sub-view for the discovered log streams (panel 8) |
| `/` | Edit the inline filter for Sessions or Logs detail |
| `s` | Cycle session sort in Sessions detail |
| `g` / `G` | Jump to top/bottom in scrollable detail views |
| `p` | Cycle the viewed profile in Profiles detail |
| `r` | Force immediate refresh |
| `q` | Quit |
| `?` | Toggle help overlay |

## Troubleshooting / FAQ

**Running hermesd without a TTY (cron, CI, pipes)**
The interactive TUI expects a terminal: the input thread only starts when stdin is a TTY (`tty.setcbreak` mode), and the Rich `Live` display forces terminal output. Run non-interactively with `--snapshot` (overview), `--snapshot-panel N` (one panel), or `--snapshot-format json` (machine-readable full state) — these render one frame to stdout and exit, so they work from cron:

```cron
*/5 * * * * hermesd --snapshot-format json --snapshot-file /tmp/hermesd-state.json
```

**What does `AGENT OFFLINE` mean?**
The header/footer show `AGENT OFFLINE` when Hermes Agent appears inactive: the gateway process is not running, no sessions are active, and the most recent activity is older than 5 minutes. Start the gateway or a session and the banner clears on the next refresh.

**What does the footer health dot mean?**
The green/yellow/red dot next to the polling spinner shows how many collector sources succeeded on the last refresh (`ok/total`). Green means every source read cleanly, yellow means some failed (the failed source names are listed inline), red means none did. A `(stale)` marker after the refresh interval means the last refresh failed outright and hermesd is showing cached data.

**Does hermesd fight Hermes Agent for the SQLite database?**
No. Source databases without a WAL sidecar use immutable, read-only connections. Databases with a WAL sidecar are copied with their available `-wal`/`-shm` sidecars to a private temporary directory outside your Hermes home and opened read-only there. Any SQLite shared-memory coordination is confined to that copy, including when another connection in the same process is writing to the source. The shared `state.db` snapshot is reused until the source changes; updates require another copy, which adds I/O for large databases. If a read fails transiently (e.g. during a WAL checkpoint), hermesd keeps the last good data on screen and retries on the next poll instead of blanking panels.

**hermesd is slow with very large log files**
Each refresh reads only the last `--log-tail-bytes` bytes of every log file and cron output excerpt (default: 32768). Lower it to cut I/O on multi-GB logs:

```bash
hermesd --log-tail-bytes 8192
```

## Architecture

hermesd is a **read-only companion** — it reads files from `~/.hermes/` and never writes to Hermes Agent state. The only write path is the explicit `--snapshot-file PATH` export, which is rejected when the target is under the Hermes home.

```
~/.hermes/                        hermesd
  state.db (SQLite WAL) ───────> db.py      Read-only (mode=ro), data_version cache
  gateway_state.json ──────────> collector.py  JSON/YAML mtime-cached readers
  gateway.pid ─────────────────>
  config.yaml ─────────────────>
  cron/jobs.json ──────────────>
  cron/executions.db ──────────>
  cron/ticker_* ───────────────>
  kanban.db ───────────────────>
  channel_directory.json ──────>
  model cache JSON ────────────>
  pr-monitor*.json ────────────>
  auth.json ───────────────────>
  skills/*/SKILL.md ───────────>
  sessions/*.json ─────────────>
  logs/*.log ──────────────────>
                                     |
                                     v
                                 models.py   Pydantic DashboardState
                                     |
                                     v
                                 app.py      Rich TUI (Live + Layout + threads)
                                     |
                                     v
                                 panels/*.py  13 panel renderers (compact + detail)
```

### Design Decisions

| Decision | Why |
|----------|-----|
| SQLite `mode=ro` + `check_same_thread=False` | Guarantees no writes; safe for cross-thread polling/render |
| `PRAGMA data_version` caching | Skips re-reads when agent hasn't written, minimizing I/O |
| Cache preservation on error | Transient SQLite lock contention keeps last good data visible |
| Auto-reconnect after 3 errors | Recovers from WAL checkpoint invalidation |
| `gateway.pid` fallback | Detects correct PID after launchd/systemd restarts |
| `tty.setcbreak` (not `setraw`) | Preserves signal handling over SSH/tmux |
| `os.read(fd, 64)` bulk read | Captures escape sequences as single chunks |
| Cost estimation from tokens | Shows ~USD when provider doesn't report costs |
| Adaptive layout threshold | Width < 100 with height >= 50 gets the tall single-column layout; smaller terminals get the compact mixed grid |

## Themes

hermesd inherits the active skin from Hermes Agent's `config.yaml`:

| Skin | Style |
|------|-------|
| `default` | Gold/bronze on dark — the classic Hermes look |
| `ares` | Deep red with gold accents |
| `mono` | Grayscale minimalist |
| `slate` | Cool blue tones |
| `poseidon` | Ocean blue |
| `sisyphus` | Silver/stone gray |
| `charizard` | Warm orange/amber |

## Development

```bash
git clone https://github.com/mudrii/hermesd.git
cd hermesd
uv venv .venv --python 3.11
source .venv/bin/activate
uv sync --locked --all-extras --dev

# Run the full local gate set for the active interpreter.
# CI runs the same checks across Python 3.11, 3.12, 3.13, and 3.14.
uv run ruff check .
uv run ruff format --check .
uv run mypy hermesd
uv run python -m compileall hermesd
uv run pytest tests/ -v -W error::ResourceWarning --cov=hermesd --cov-report=term-missing
uv run pip-audit
uv lock --check
uv build
python -m venv /tmp/hermesd-wheel-smoke
/tmp/hermesd-wheel-smoke/bin/python -m pip install dist/hermesd-*.whl
/tmp/hermesd-wheel-smoke/bin/hermesd --version
/tmp/hermesd-wheel-smoke/bin/python -I -m hermesd --version
python -m venv /tmp/hermesd-sdist-smoke
/tmp/hermesd-sdist-smoke/bin/python -m pip install dist/hermesd-*.tar.gz
/tmp/hermesd-sdist-smoke/bin/hermesd --version
/tmp/hermesd-sdist-smoke/bin/python -I -m hermesd --version
uv run twine check dist/*

# Run the dashboard
hermesd
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full TDD-first contributor workflow.

### Releases

User-facing release notes live in [`CHANGELOG.md`](CHANGELOG.md). Before tagging, bump the package version in `pyproject.toml`, keep `flake.nix` aligned, move user-facing `[Unreleased]` notes into a dated release section matching the package version, and update screenshot URLs if the release refreshes images. PyPI publishing is driven from GitHub Releases: tag the release as `vYYYY.M.D`, publish the release on GitHub, and the `python-publish` workflow reruns the test matrix, verifies that the tag, package version, changelog section, and distribution filenames match, smoke-tests the wheel and sdist, checks package metadata, then uploads to PyPI via OIDC. GitHub Actions dependencies are pinned to immutable SHAs with version comments and tracked by Dependabot.

### Project Structure

```
hermesd/
  __init__.py          Version derived from package metadata
  __main__.py          CLI entry point (argparse)
  app.py               Rich TUI: Live context, input thread, adaptive layout
  collector.py         Collector orchestration + public facade over collect/
  collect/        Per-domain readers behind the collector facade
    common.py     Coercion, path-safety and file-stat primitives
    config.py     config.yaml and auth.json summaries
    cron.py       Cron output discovery, excerpts, suggestions,
                  executions.db history/incidents, ticker health
    gateway.py    Gateway heartbeat, lifecycle, updates, ledgers
    kanban.py     Kanban board SQL readers and board discovery
    logs.py       Log line parsing constants and helpers
    migration.py  gateway_migration.json: recorded intent, progress,
                  and the verified-topology predicate
    operations.py Verification, goals, projects, MoA, curator
    plugins.py    Plugin discovery (3 manifest formats, 2 directory shapes),
                  the configured-activation gate, and install/catalog provenance
    redaction.py  Secret redaction for URLs, argv, config, log text
    sessions.py   Session, token and cost analytics
    skills.py     Skill, memory and SOUL filesystem readers
    sqlite_util.py Read-only SQLite helpers
    system.py     Process liveness, git checkpoints, activity age
  defaults.py          Shared refresh-rate and log-tail-bytes defaults
  db.py                Read-only SQLite with data_version caching
  file_cache.py        mtime-keyed JSON/YAML cache
  models.py            Pydantic models for dashboard state
  paths.py             HermesPaths root/profile path resolution (source
                       ownership: .codex/rules/source-ownership.md)
  theme.py             Skin/color system matching Hermes Agent
  panels/
    __init__.py        Panel dispatch and registry
    formatting.py      Shared rendering helpers
    gateway.py         [1] Gateway & Platforms
    sessions.py        [2] Sessions
    tokens.py          [3] Tokens / Cost
    tools.py           [4] Tools
    config_panel.py    [5] Config
    cron.py            [6] Cron
    overview.py        [7] Skills / Integrations
    logs.py            [8] Logs
    profiles.py        [9] Profiles
    memory_panel.py    [10] Memory
    kanban.py          [11] Kanban
    operations.py      [12] Operations
    curator_panel.py   [13] Curator
tests/                 Test suite: panels, data, resilience, edge cases
```

### Adding a Panel

hermesd uses **TDD-first** contribution (see [`CONTRIBUTING.md`](CONTRIBUTING.md)). Write the failing test first, then implement:

1. Write the failing test in `tests/test_your_panel.py` — acceptance-level (full `Collector → DashboardState → render` flow) + unit tests for edge cases
2. Add data model to `hermesd/models.py`
3. Collect data in the matching `hermesd/collect/*.py` reader, wired in via `hermesd/collector.py`
4. Create `hermesd/panels/your_panel.py` with `render_*(state, theme, detail)` function
5. Register in `hermesd/panels/__init__.py` (`PANEL_NAMES` and `_RENDERERS`)
6. Add the panel number to the overview layout specs in `hermesd/app.py` (`_WIDE_LAYOUT_SPEC`, `_COMPACT_LAYOUT_SPEC`, `_TALL_NARROW_LAYOUT_SPEC`) as needed
7. Update `CHANGELOG.md` under `[Unreleased]`

## Requirements

- **Python** >= 3.11
- **Hermes Agent** installed with `~/.hermes/` directory present
- **Terminal** with 256-color or truecolor support

### Dependencies

Only 3 runtime dependencies:

| Package | Version | Purpose | In hermes-agent? |
|---------|---------|---------|------------------|
| `rich` | >= 14.0 | TUI rendering (Live, Layout, Panel, Table, Text) | Yes |
| `pyyaml` | >= 6.0 | Reading config.yaml | Yes |
| `pydantic` | >= 2.0 | Data models and validation | Yes |

If you install hermesd into the same environment as Hermes Agent, these dependencies are usually already present, so no additional downloads may be needed.

## License

[MIT License](LICENSE)

## Credits

Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com).
