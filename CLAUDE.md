# CLAUDE.md

## Overview

**hermesd** is a standalone TUI monitoring dashboard for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It reads data from `~/.hermes/` (SQLite DB, JSON/YAML config, log files, skill directories) and displays it in a live-updating Rich terminal interface.

**Key constraint:** hermesd is **read-only** — it must never write to `~/.hermes/` or import any hermes-agent code. It's installed and runs independently.

## Build & Test

```bash
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest tests/ -v          # 164 tests, <0.5s
hermesd                              # run the dashboard
hermesd --hermes-home /path          # custom hermes home
```

## Architecture

```
hermesd/
  __main__.py     CLI entry point (argparse)
  app.py          Rich TUI: Live context, input thread, adaptive layout
  collector.py    Reads all ~/.hermes data sources (JSON, YAML, SQLite, files)
  db.py           Read-only SQLite with PRAGMA data_version caching
  models.py       Pydantic models for DashboardState
  theme.py        Skin/color system (inherits from hermes config.yaml)
  panels/         8 panel renderers (compact + detail views)
```

### Data Flow

`Collector` polls `~/.hermes/` every N seconds → builds `DashboardState` (Pydantic) → `DashboardApp` renders via `panels/*.py` into Rich `Layout`.

### Threading Model

- **Main thread**: Rich `Live` render loop (0.5s update cycle)
- **Collector thread**: daemon, polls data every `refresh_rate` seconds
- **Input thread**: daemon, reads raw terminal keypresses via `tty.setcbreak`

State is shared via `threading.Lock` on `_state`. SQLite uses `check_same_thread=False`.

## Critical Rules

- **Never write to `~/.hermes/`** — read-only, always
- **Never import hermes-agent code** — read files directly
- **Cache preservation** — on SQLite errors, keep last good data (never blank the display)
- **Escape sequence handling** — `os.read(fd, 64)` bulk reads, `len(key)==1` check for Esc vs arrow keys
- **NULL tolerance** — all SQLite column reads must use `or 0` / `or ""` pattern, not `.get("key", default)`

## Adding Features

1. Add data model fields to `models.py`
2. Populate them in `collector.py`
3. Render in `panels/*.py` (both `_render_compact` and `_render_detail`)
4. Write tests (compact render, detail render, empty state, collector integration)
5. Update `app.py` layout if adding new panels
