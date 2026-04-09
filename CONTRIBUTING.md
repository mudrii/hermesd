# Contributing to hermesd

Thanks for your interest in contributing to hermesd!

## Getting Started

```bash
git clone https://github.com/NousResearch/hermesd.git
cd hermesd
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest tests/ -v
```

## Development Workflow

1. **Create a branch** from `main`
2. **Write tests first** — every change should have tests
3. **Make your changes** — keep diffs small and focused
4. **Run the test suite** — `python -m pytest tests/ -v` (all 164+ tests must pass)
5. **Test the TUI manually** — run `hermesd` and verify your changes look correct
6. **Open a PR** with a clear description

## Code Guidelines

- **Python 3.11+** — use modern syntax (`X | None`, not `Optional[X]`)
- **Type annotations** on all public functions
- **Pydantic models** for data structures
- **Read-only** — hermesd must never write to `~/.hermes/`
- **No hermes-agent imports** — hermesd reads files directly, zero dependency on hermes-agent code
- **Error resilience** — never crash on missing/corrupt data; show last known good state

## Adding a New Panel

1. Create `hermesd/panels/your_panel.py` with `render_your_panel(state, theme, detail=False)` function
2. Add your data to `hermesd/models.py` (Pydantic model)
3. Collect the data in `hermesd/collector.py`
4. Register in `hermesd/panels/__init__.py` (add to renderers dict and PANEL_NAMES)
5. Add tests in `tests/test_your_panel.py`
6. Update the layout in `hermesd/app.py` (`_build_overview_wide` and `_build_overview_compact`)

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
