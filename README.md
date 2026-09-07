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
| 1 | **Gateway & Platforms** | Live gateway PID, Hermes version, update status, drain/scale-to-zero state, channel aliases/staleness, platform families, per-platform connection dots, and channel-directory inventory |
| 2 | **Sessions** | Active/total count, message/tool/API call totals, cwd, archived state, handoff metadata, and parent-session lineage |
| 3 | **Tokens / Cost** | Today's and all-time token usage, authoritative vs estimated cost, cost-status reconciliation, recent-window rollups, and model/provider/endpoint breakdowns |
| 4 | **Tools** | Available tools count, per-session call stats, background processes, filesystem checkpoints, full tool name grid |
| 5 | **Config** | Model, provider, personality, MoA, Tool Search, dashboard auth, kanban, code execution, gateway, routing and memory/session settings |
| 6 | **Cron** | Scheduler tick/provider, ticker health, open incidents, Chronos config presence, suggestion count, job table with schedule, delivery target, 24 h run counters, error count, latest error, and output metadata |
| 7 | **Skills / Integrations** | Provider auth status/freshness, credential pools, hooks/plugins/MCP inventory, BOOT.md presence, skills with descriptions |
| 8 | **Logs** | Tailed agent, gateway, errors, cron, desktop, dashboard, GUI, update, gateway-error, crash, audit, MCP-stderr, and workspace logs with Tab switching and inline filtering |
| 9 | **Profiles** | Read-only profile discovery with session counts, log freshness, skill counts, DB size, and SOUL excerpts |
| 10 | **Memory** | Memory provider, MEMORY.md/USER.md word counts, learning summary, SOUL.md size/excerpt, and memory file inventory |
| 11 | **Kanban** | Read-only kanban task/run/event/comment counts, multi-board summaries, stale claims, dispatch config, active workers, blocked/failing tasks, and recent runs |
| 12 | **Operations** | Dashboard process count, Desktop build stamp, Response Store, verification evidence, goals, MoA trace metadata, Projects/correlations/newest repos, model-cache summaries, and PR monitor state |
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
- **Scrollable lists** — `j`/`k` scroll both skills and log detail views
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

Press `1` to expand. Shows whether the gateway process is alive (with correct PID even after launchd restarts), Hermes version with update status, served profiles, busy/drainable state, external drain markers, scale-to-zero idle timeout and relay-only intent, channel-alias inventory/staleness, platform family labels, and a per-platform table with connection state, last-seen timestamps, and an **Error** column surfacing per-platform connection failures (e.g. discord "failed to reconnect"), plus the active-agent count and a restart-requested marker. Catches the "gateway says running but the PID is dead" case.

![Gateway Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-01-gateway.png)

### [2] Sessions — What’s Active and Where Did It Fork?

Press `2` to expand. The detail table includes billing metadata plus parent-session lineage; a **Runtime** section surfaces API call counts, cwd, archived state, rewind count, and handoff metadata; and a **Billing & Context** section shows each session's end reason, billing endpoint, billing mode, and the model's context-window limit (joined from `context_length_cache.yaml`). Press `/` to filter the currently loaded sessions by ID, source, model, lineage, provider, title, cwd, archived state, handoff state, platform, or message content via `message:term`, and press `s` to cycle recent/cost/token sorting.

![Sessions Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-02-sessions.png)

### [3] Tokens / Cost — Where Are My Tokens Going?

Press `3` for the full per-session token breakdown plus recent `7d`/`30d` rollups and read-only model/provider/**endpoint** cost summaries derived from the current session table, and a **Cost Status** reconciliation line (unknown vs subscription-included vs estimated). In both the compact and detail views, costs carry a `~$` prefix when estimated and a plain `$` prefix when the provider cost is authoritative (`reported`/`exact`/`included`); subscription-`included` sessions render an authoritative `$0.00`.

![Tokens Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-03-tokens.png)

### [4] Tools — What's Available and What's Being Used?

Press `4` for four sections: **Tool Calls** showing the current call leaders by name (tool names when the `messages` table provides them, otherwise fallback session labels), **Available Tools** listing the union of tools discovered across session files in a 3-column grid, **Background Processes** showing the running `processes.json` checkpoint with PID, notify-on-complete, watch-pattern summary, start time, and command, and **Checkpoints** showing filesystem shadow repos with workdir name, commit depth, and latest checkpoint reason. The compact view shows the top callers plus the current background-process and checkpoint counts.

![Tools Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-04-tools.png)

### [5] Config — Current Agent Configuration

Press `5` for the full config key-value table: model, provider, personality, max turns, reasoning effort, compression threshold, secret redaction, approval mode, provider routing summary, smart routing, fallback model, dashboard theme/auth/public URL, session reset mode, memory provider, Tool Search, toolsets, code execution, kanban dispatch settings, gateway media trust, MoA preset/reference/aggregator/trace settings, and auxiliary slot count. Tool Gateway domain, scheme, Firecrawl endpoint, and route token presence are shown from config plus environment with secret-bearing values redacted.

![Config Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-05-config.png)

### [6] Cron — Scheduled Jobs

Press `6` to see cron scheduler state, provider, Chronos managed-cron config presence, persisted suggestion counts, max parallelism, response wrapping mode, all configured jobs, delivery targets, current state, last execution status, latest error, and latest saved output metadata from `~/.hermes/cron/output/`. `[SILENT]` runs are surfaced explicitly so “nothing to report” is distinguishable from missing output.

**Ticker health** is derived from `~/.hermes/cron/ticker_heartbeat` and `~/.hermes/cron/ticker_last_success` (one epoch float each, written by the 60 s ticker): `ok` when the heartbeat is under 120 s old and the last success under 600 s, `failing` when the heartbeat is fresh but the last success has fallen behind, `stale` when the heartbeat itself has stopped, and `unknown` when neither file exists. Both ages are shown in the detail view; the existing `cron/.tick.lock` age remains as the fallback tick indicator.

**Execution history** comes from `~/.hermes/cron/executions.db` (read-only, bounded queries — never a full table scan). Each job shows its last-24-hour counters (`7✓ 2✗ 1▶` for completed / failed / running) alongside the last run's status, duration (`finished_at − started_at`), and first-line error excerpt. The detail view adds a **Recent Executions** table (the last 10 runs with job name, status, start age, duration, and error excerpt) and an **Open Incidents** table from `cron_incidents` (job, state, failure type, first/last seen age, error excerpt) with open and unacked counts. Both degrade to empty summaries — never an error — when the database or either table is missing, as on older agents.

The job rows also surface the newer `cron/jobs.json` keys: `failure_streak` (shown as `✗3` in the compact row), `paused_at`/`paused_reason` (`⏸` — either one alone is enough to mark a job paused), `last_delivery_error`, `last_dispatch` lateness and kind, `repeat` progress, and `no_agent` script-only jobs.

![Cron Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-06-cron.png)

### [7] Skills & Integrations — What's Installed?

Press `7` for provider and integration visibility in one place: **Providers** with active auth state and safely persisted credential freshness/expiry metadata, **Credential Pools** with redacted metadata, **Hooks** discovered from `~/.hermes/hooks/`, **Plugins** from `~/.hermes/plugins/`, **MCP Servers** from `config.yaml` with secret-bearing args and URL query params redacted, `BOOT.md` presence, and **Skills** grouped by category with descriptions loaded from each skill's `SKILL.md` frontmatter. Use `j`/`k` to scroll through the full skill list.

![Skills Detail](https://raw.githubusercontent.com/mudrii/hermesd/v2026.6.15/images/panel-07-skills.png)

### [8] Logs — What Just Happened?

Press `8` for the full log viewer with discovered streams such as **agent**, **gateway**, **errors**, **cron**, **desktop**, **dashboard**, **gui**, **update**, **gateway.error**, **tui crash**, **workspace**, **workspace.error**, **audit**, and **mcp.stderr** (each shown only when its file exists). Press `Tab` to switch between them, `/` to filter the current log stream by `level:`, `minlevel:`, `component:`, `session:`, or free text, `j`/`k` to move the viewport, and `g`/`G` to jump to the top or bottom.

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

Use `]` from Kanban or `--snapshot-panel 12` to expand. The Operations panel summarizes dashboard background processes, Desktop build metadata, **Response Store** stats (conversation/response row counts + size from `response_store.db`), **Verification Evidence** from `verification_evidence.db`, active/waiting **Goals** from `state.db`, **MoA Traces** inventory and bounded latest-record metadata from `moa-traces/*.jsonl`, **Projects** from `projects.db` with missing primary paths, newest discovered repos, and verification/Kanban correlations, model-cache provider/model counts, cache ages, and PR monitor files across all of the agent's naming families (flat + subdirectory) with per-repo dedup.

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
No. All databases are opened read-only (`mode=ro`). When `state.db` is in WAL mode, hermesd copies the database plus its `-wal`/`-shm` sidecars to a temporary snapshot and reads that, so a busy writer never blocks the dashboard. If a read does fail transiently (e.g. during a WAL checkpoint), hermesd keeps the last good data on screen and retries on the next poll instead of blanking panels.

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
  __init__.py          Version string
  __main__.py          CLI entry point (argparse)
  app.py               Rich TUI: Live context, input thread, adaptive layout
  collector.py         Collector orchestration + public facade over collect/
  collect/        Per-domain readers behind the collector facade
    common.py     Coercion, path-safety and file-stat primitives
    config.py     config.yaml and auth.json summaries
    cron.py       Cron output discovery, excerpts, suggestions,
                  executions.db history/incidents, ticker health
    kanban.py     Kanban board SQL readers and board discovery
    logs.py       Log line parsing constants and helpers
    operations.py Verification, goals, projects, MoA, curator
    redaction.py  Secret redaction for URLs, argv, config, log text
    sessions.py   Session, token and cost analytics
    skills.py     Skill, memory and SOUL filesystem readers
    sqlite_util.py Read-only SQLite helpers
    system.py     Process liveness, git checkpoints, activity age
  defaults.py          Shared refresh-rate and log-tail-bytes defaults
  db.py                Read-only SQLite with data_version caching
  file_cache.py        mtime-keyed JSON/YAML cache
  models.py            Pydantic models for dashboard state
  paths.py             HermesPaths profile-scoped path resolution
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
