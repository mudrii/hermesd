# hermesd

A real-time TUI monitoring dashboard for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

![hermesd overview](images/SCR-20260409-pzqv.png)

## Why This Exists

When you run Hermes Agent seriously — gateway handling Telegram, Discord, Feishu, and WhatsApp simultaneously, cron jobs firing reminders, multiple CLI sessions with sub-agents spawning sub-agents, dozens of skills loaded, 7+ LLM providers configured — the information gets scattered fast.

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

### 8 Dashboard Panels

| # | Panel | What It Shows |
|---|-------|---------------|
| 1 | **Gateway & Platforms** | Live gateway PID, Hermes version, update status, per-platform connection dots |
| 2 | **Sessions** | Active/total count, message and tool call totals, recent session list |
| 3 | **Tokens / Cost** | Today's and all-time token usage, estimated cost (~USD) from token counts |
| 4 | **Tools** | Available tools count, per-session call stats, full tool name grid |
| 5 | **Config** | Model, provider, personality, max turns, reasoning, compression, security |
| 6 | **Cron** | Scheduler tick, job table with schedule, state, next run, error count |
| 7 | **Skills / Providers** | Provider auth status, skills grouped by category with descriptions, j/k scrolling |
| 8 | **Logs** | Tailed agent, gateway, and error logs with level coloring and Tab switching |

### Key Features

- **Read-only** — hermesd never writes to `~/.hermes/` or modifies Hermes Agent state
- **Live-updating** — polls every 5 seconds (configurable with `--refresh-rate`)
- **Adaptive layout** — full 8-panel grid on wide terminals, compact single-column on 80x24 (SSH/tmux)
- **Detail views** — press `1`-`8` to expand any panel to full-screen
- **Scrollable lists** — `j`/`k` to scroll through skills and logs in detail mode
- **Resilient** — keeps showing last known good data on transient SQLite lock contention
- **Theme-aware** — inherits your Hermes Agent skin (default, ares, mono, slate, poseidon, sisyphus, charizard)
- **SSH/tmux compatible** — `tty.setcbreak` mode, escape sequence handling for remote terminals
- **Cost estimation** — computes ~USD from token counts when the provider doesn't report costs
- **Zero config** — no config file, no API keys, just `hermesd` and go

## Screenshots

### Overview — The Full Picture

The main dashboard shows all 8 panels at a glance. Gateway status with PID and version at the top, sessions and token costs side by side, tools and config, cron and skills, logs at the bottom. The footer shows keyboard shortcuts and a polling indicator.

![Overview](images/SCR-20260409-pzqv.png)

### [1] Gateway & Platforms — Is Everything Connected?

Press `1` to expand. Shows whether the gateway process is alive (with correct PID even after launchd restarts), Hermes version with update status, and a per-platform table with connection state and last-seen timestamps. Catches the "gateway says running but the PID is dead" case.

![Gateway Detail](images/SCR-20260409-pzxz.png)

### [3] Tokens / Cost — Where Are My Tokens Going?

Press `3` for the full per-session token breakdown. Shows input, output, cache-read, cache-write, and reasoning tokens for every session, plus estimated cost. The compact view shows today's totals with `~$` prefix indicating estimated costs when the provider (e.g., OpenAI Codex) doesn't report them.

![Tokens Detail](images/SCR-20260409-qaah.png)

### [4] Tools — What's Available and What's Being Used?

Press `4` for two tables: **Tool Calls** showing per-session call counts (which sessions are using the most tools), and **Available Tools** listing all 29 registered tools in a 3-column grid. The compact view shows the top callers.

![Tools Detail](images/SCR-20260409-qacn.png)

### [5] Config — Current Agent Configuration

Press `5` for the full config key-value table: model, provider, personality, max turns, reasoning effort, compression threshold, secret redaction, and approval mode. All read from `~/.hermes/config.yaml`.

![Config Detail](images/SCR-20260409-qael.png)

### [6] Cron — Scheduled Jobs

Press `6` to see all cron jobs with their schedule, current state, next run time, and last execution status. The compact view shows tick recency, job count, and error count. Reads from `~/.hermes/cron/jobs.json`.

![Cron Detail](images/SCR-20260409-qbwi.png)

### [7] Skills & Providers — What's Installed?

Press `7` for two sections: **Providers** showing auth status for each configured LLM provider (active marked with `●`), and **Skills** grouped by category with descriptions loaded from each skill's `SKILL.md` frontmatter. Use `j`/`k` to scroll through the full skill list. The scroll position indicator shows `[11-69/69]`.

![Skills Detail](images/SCR-20260409-qbym.png)

### [8] Logs — What Just Happened?

Press `8` for the full log viewer with three tabs: **agent**, **gateway**, and **errors**. Press `Tab` to switch between them. Log lines are color-coded by level (INFO green, WARNING orange, ERROR red). The compact view shows the last 5 agent log lines.

![Logs Detail](images/SCR-20260409-qcgt.png)

## Installation

Requires Python 3.11+ and a working [Hermes Agent](https://github.com/NousResearch/hermes-agent) installation (`~/.hermes/` must exist).

### From Source

```bash
git clone https://github.com/NousResearch/hermesd.git
cd hermesd
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e .
hermesd
```

### Via pip

```bash
pip install hermesd
hermesd
```

### Via uv

```bash
uv tool install hermesd
hermesd
```

## Usage

```bash
# Launch the dashboard (reads ~/.hermes by default)
hermesd

# Custom hermes home directory (for profiles)
hermesd --hermes-home ~/.hermes-work

# Faster polling (every 2 seconds)
hermesd --refresh-rate 2

# Disable colors
hermesd --no-color

# Show version
hermesd --version
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_HOME` | `~/.hermes` | Override the Hermes home directory |

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `1`-`8` | Expand panel to full-screen detail view |
| `Esc` | Return to overview |
| `j` / `k` | Scroll down/up in detail mode |
| `Tab` | Cycle log sub-view: agent / gateway / errors (panel 8) |
| `r` | Force immediate refresh |
| `q` | Quit |
| `?` | Toggle help overlay |

## Architecture

hermesd is a **read-only companion** — it reads files from `~/.hermes/` and never writes anything.

```
~/.hermes/                        hermesd
  state.db (SQLite WAL) ───────> db.py      Read-only (mode=ro), data_version cache
  gateway_state.json ──────────> collector.py  JSON/YAML mtime-cached readers
  gateway.pid ─────────────────>
  config.yaml ─────────────────>
  cron/jobs.json ──────────────>
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
                                 panels/*.py  8 panel renderers (compact + detail)
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
| Adaptive layout threshold | 80x24 gets compact single-column; 100+ gets full grid |

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
git clone https://github.com/NousResearch/hermesd.git
cd hermesd
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run tests (164 tests, <0.5s)
python -m pytest tests/ -v

# Run the dashboard
hermesd
```

### Project Structure

```
hermesd/
  __init__.py          Version string
  __main__.py          CLI entry point (argparse)
  app.py               Rich TUI: Live context, input thread, adaptive layout
  collector.py         Reads all ~/.hermes data sources
  db.py                Read-only SQLite with data_version caching
  models.py            Pydantic models for dashboard state
  theme.py             Skin/color system matching Hermes Agent
  panels/
    __init__.py        Panel dispatch and registry
    gateway.py         [1] Gateway & Platforms
    sessions.py        [2] Sessions
    tokens.py          [3] Tokens / Cost
    tools.py           [4] Tools
    config_panel.py    [5] Config
    cron.py            [6] Cron
    overview.py        [7] Skills / Providers
    logs.py            [8] Logs
tests/                 164 tests: panels, data, resilience, edge cases
```

### Adding a Panel

1. Create `hermesd/panels/your_panel.py` with `render_*(state, theme, detail)` function
2. Add data model to `hermesd/models.py`
3. Collect data in `hermesd/collector.py`
4. Register in `hermesd/panels/__init__.py`
5. Add to layout in `hermesd/app.py`
6. Write tests in `tests/test_your_panel.py`

## Requirements

- **Python** >= 3.11
- **Hermes Agent** installed with `~/.hermes/` directory present
- **Terminal** with 256-color or truecolor support

### Dependencies

Only 3 runtime dependencies:

| Package | Version | Purpose |
|---------|---------|---------|
| `rich` | >= 14.0 | TUI rendering (Live, Layout, Panel, Table, Text) |
| `pyyaml` | >= 6.0 | Reading config.yaml |
| `pydantic` | >= 2.0 | Data models and validation |

## License

[MIT License](LICENSE)

## Credits

Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com).
