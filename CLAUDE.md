# CLAUDE.md

## Overview

**hermesd** is a standalone TUI monitoring dashboard for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It reads data from `~/.hermes/` (SQLite DB, JSON/YAML config, log files, skill directories) and displays it in a live-updating Rich terminal interface.

**Key constraint:** hermesd is **read-only** — it must never write to `~/.hermes/` or import any hermes-agent code. It's installed and runs independently.

## Build & Test

```bash
uv venv .venv --python 3.11
source .venv/bin/activate
uv sync --locked --all-extras --dev
uv run pytest tests/ -v -W error::ResourceWarning  # full suite
uv run ruff check .                 # lint
uv run ruff format --check .        # format check
uv run mypy hermesd                 # type check
uv run python -m compileall hermesd # compile check
uv run pip-audit                    # dependency audit
uv lock --check                     # lockfile freshness
uv build                            # package build
python -m venv /tmp/hermesd-wheel-smoke
/tmp/hermesd-wheel-smoke/bin/python -m pip install dist/hermesd-*.whl
/tmp/hermesd-wheel-smoke/bin/hermesd --version
/tmp/hermesd-wheel-smoke/bin/python -m hermesd --version
python -m venv /tmp/hermesd-sdist-smoke
/tmp/hermesd-sdist-smoke/bin/python -m pip install dist/hermesd-*.tar.gz
/tmp/hermesd-sdist-smoke/bin/hermesd --version
/tmp/hermesd-sdist-smoke/bin/python -m hermesd --version
uv run twine check dist/*           # package metadata
hermesd                              # run the dashboard
hermesd --hermes-home /path          # custom hermes home
hermesd --profile coding             # opt-in profile-scoped runtime data
hermesd --snapshot                   # one-shot overview to stdout
hermesd --snapshot-panel 10          # one-shot detail snapshot in text mode
hermesd --snapshot-panel 0           # panel 10 alias matching the interactive shortcut
hermesd --snapshot-panel 12          # one-shot detail snapshot for any registered panel
hermesd --snapshot-format json       # machine-readable full-state snapshot
hermesd --log-tail-bytes 8192        # cap per-refresh log reads for large files
```

## Architecture

```
hermesd/
  __main__.py     CLI entry point (argparse)
  app.py          Rich TUI: Live context, input thread, adaptive layout
  collector.py    Reads all ~/.hermes data sources (JSON, YAML, SQLite, files)
  db.py           Read-only SQLite with PRAGMA data_version caching
  file_cache.py   mtime-keyed JSON/YAML cache
  models.py       Pydantic models for DashboardState
  paths.py        Profile-scoped path resolution
  theme.py        Skin/color system (inherits from hermes config.yaml)
  panels/         13 panel renderers (compact + detail views)
```

### Data Flow

`Collector` polls `~/.hermes/` every N seconds → builds `DashboardState` (Pydantic) → `DashboardApp` renders via `panels/*.py` into Rich `Layout`.

By default hermesd stays root-only. Profile-scoped reads are opt-in via `--profile` or `HERMES_PROFILE`; hermesd does not auto-follow `active_profile`.

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

Canonical contributor workflow lives in [`CONTRIBUTING.md`](CONTRIBUTING.md). The short version:

1. **Write the failing test first** — this project mandates TDD/ATDD (see `.codex/skills/py-rig/SKILL.md`).
2. Add data model fields to `models.py`.
3. Populate them in `collector.py`.
4. Render in `panels/*.py` (both `_render_compact` and `_render_detail`).
5. Make tests pass with the minimum change; refactor while green.
6. Update `app.py` layout if adding new panels; add a `_render_*_panel(ctx: PanelRenderContext)` wrapper in `panels/__init__.py`, register it in `_RENDERERS` and `PANEL_NAMES`, then add its panel number to `_WIDE_LAYOUT_SPEC`, `_COMPACT_LAYOUT_SPEC`, and `_TALL_NARROW_LAYOUT_SPEC` in `app.py` as needed.
7. Update `CHANGELOG.md` for user-visible changes.

See also:

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branch/PR workflow and per-panel instructions.
- [`.codex/rules/python-idioms.md`](.codex/rules/python-idioms.md) — version-tagged modern Python syntax.
- [`.codex/rules/python-patterns.md`](.codex/rules/python-patterns.md) — style, types, errors, tests.
- [`.codex/skills/py-rig/SKILL.md`](.codex/skills/py-rig/SKILL.md) — design/TDD/ATDD/DI/review discipline.
