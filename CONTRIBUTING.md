# Contributing to hermesd

Thanks for your interest in contributing to hermesd!

## Getting Started

```bash
git clone https://github.com/mudrii/hermesd.git
cd hermesd
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest tests/ -v
```

## Development Workflow

This project uses **TDD/ATDD** — write the failing test first, then the smallest implementation that makes it pass, then refactor while green. See `.claude/skills/py-rig/SKILL.md` for the full discipline.

1. **Create a branch** from `main`
2. **Write the failing test first** — acceptance-level if user-visible, unit-level otherwise
3. **Implement the minimum change** that makes the test pass
4. **Refactor while green** — improve naming/cohesion without changing behavior
5. **Run the full suite** — `uv run pytest tests/ -v` (all 274+ tests must pass)
6. **Run lint + type + audit** — `uv run ruff check . && uv run ruff format --check . && uv run mypy hermesd && uv run pip-audit`
7. **Test the TUI manually** — run `hermesd` and verify your changes look correct
8. **Update `CHANGELOG.md`** for user-visible changes
9. **Open a PR** with a clear description

## Code Guidelines

- **Python 3.11+** — see `.claude/rules/python-idioms.md` for version-safe modern syntax
- **`from __future__ import annotations`** at the top of every module
- **Type annotations** on all public functions; `mypy` enforces this in CI
- **Pydantic models** for data structures; `@dataclass(frozen=True, slots=True)` for value objects
- **Dependency injection** — pass collaborators through constructors / function arguments (see `py-rig` skill for the CLI-entry carve-out)
- **Read-only** — hermesd must never write to `~/.hermes/`
- **No hermes-agent imports** — hermesd reads files directly, zero dependency on hermes-agent code
- **Error resilience** — never crash on missing/corrupt data; show last known good state (cache-preservation pattern)

## Adding a New Panel

1. Create `hermesd/panels/your_panel.py` with `render_your_panel(state, theme, detail=False)` function
2. Add your data to `hermesd/models.py` (Pydantic model)
3. Collect the data in `hermesd/collector.py`
4. Register in `hermesd/panels/__init__.py` (add to renderers dict and PANEL_NAMES)
5. Add tests in `tests/test_your_panel.py`
6. Update the layout spec in `hermesd/app.py` if the new panel needs overview placement

## Adding Data to an Existing Panel

1. Add fields to the relevant model in `hermesd/models.py`
2. Populate them in `hermesd/collector.py`
3. Render them in the panel's `_render_compact` and/or `_render_detail` functions
4. Add tests

## Testing

```bash
# Full suite
python -m pytest tests/ -v

# Single file
python -m pytest tests/test_collector.py -v

# Watch for failures
python -m pytest tests/ -x --tb=short
```

Test categories:
- `test_models.py` — Pydantic model construction
- `test_db*.py` — SQLite reader, caching, resilience
- `test_collector*.py` — data collection from `~/.hermes/`
- `test_*_panel.py` — panel rendering (compact + detail)
- `test_app*.py` — TUI key handling, layout, lifecycle
- `test_*_resilience.py` — error handling, cache preservation

## Reporting Issues

Please include:
- Terminal emulator and size (`echo $TERM`, `tput cols`, `tput lines`)
- Whether you're using SSH/tmux/screen
- Hermes Agent version (`hermes version`)
- hermesd version (`hermesd --version`)
- The error traceback if applicable

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
