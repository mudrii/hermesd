# Contributing to hermesd

Thanks for your interest in contributing to hermesd!

## Getting Started

```bash
git clone https://github.com/mudrii/hermesd.git
cd hermesd
uv venv .venv --python 3.11
source .venv/bin/activate
uv sync --locked --all-extras --dev
uv run pytest tests/ -v -W error::ResourceWarning --cov=hermesd --cov-report=term-missing
```

## Development Workflow

This project uses **TDD/ATDD** — write the failing test first, then the smallest implementation that makes it pass, then refactor while green. See `.codex/skills/py-rig/SKILL.md` for the full discipline.

1. **Create a branch** from `main`
2. **Write the failing test first** — acceptance-level if user-visible, unit-level otherwise
3. **Implement the minimum change** that makes the test pass
4. **Refactor while green** — improve naming/cohesion without changing behavior
5. **Run the full suite** — `uv run pytest tests/ -v -W error::ResourceWarning --cov=hermesd --cov-report=term-missing`
6. **Run lint + type + audit + lock + build + package smoke**:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy hermesd
   uv run python -m compileall hermesd
   uv run pip-audit
   uv lock --check
   uv build
   python -m venv /tmp/hermesd-wheel-smoke
   /tmp/hermesd-wheel-smoke/bin/python -m pip install dist/hermesd-*.whl
   /tmp/hermesd-wheel-smoke/bin/hermesd --version
   /tmp/hermesd-wheel-smoke/bin/python -m hermesd --version
   python -m venv /tmp/hermesd-sdist-smoke
   /tmp/hermesd-sdist-smoke/bin/python -m pip install dist/hermesd-*.tar.gz
   /tmp/hermesd-sdist-smoke/bin/hermesd --version
   /tmp/hermesd-sdist-smoke/bin/python -m hermesd --version
   uv run twine check dist/*
   ```

7. **Test the TUI manually** — run `hermesd` and verify your changes look correct
   Consider both text and JSON snapshot paths when you change CLI/render surfaces (`--snapshot-format json`).
8. **Update `CHANGELOG.md`** for user-visible, packaging, release, CI, and developer-tooling changes
9. **Open a PR** with a clear description

## Release Checklist

1. Merge the release PR only after the full local and CI gate set passes.
2. Make sure `README.md`, `CHANGELOG.md`, and any affected contributor docs match the shipped behavior.
3. Keep latest-release behavior and current-main behavior distinct: public release copy should describe the tag being released, while current-branch-only behavior stays under `[Unreleased]` or is clearly labeled unreleased.
4. Move the current user-facing notes from `[Unreleased]` into a dated release section in `CHANGELOG.md`.
5. Bump the package version in `pyproject.toml` and the editable-package entry in `uv.lock`.
6. Keep `flake.nix` version metadata aligned with `pyproject.toml` when Nix support remains advertised.
7. Update README screenshot URLs when release screenshots change, and keep package metadata safe for PyPI rendering.
8. Create and publish a GitHub Release tagged `vYYYY.M.D`; PyPI publishing runs from `.github/workflows/python-publish.yml` after the release is published. The workflow rejects mismatched package/changelog versions, skips prerelease build/publish after the test gate, pins GitHub Actions to immutable SHAs with version comments, and tracks action updates with Dependabot.

## Code Guidelines

- **Python 3.11+** — see `.codex/rules/python-idioms.md` for version-safe modern syntax
- **`from __future__ import annotations`** at the top of every module
- **Type annotations** on all public functions; `mypy` enforces this in CI
- **Pydantic models** for data structures; `@dataclass(frozen=True, slots=True)` for value objects
- **Dependency injection** — pass collaborators through constructors / function arguments (see `py-rig` skill for the CLI-entry carve-out)
- **Read-only** — hermesd must never write to `~/.hermes/`
- **No hermes-agent imports** — hermesd reads files directly, zero dependency on hermes-agent code
- **Error resilience** — never crash on missing/corrupt data; show last known good state (cache-preservation pattern)
- **User-facing and release text stays documented** — update `README.md`, `CHANGELOG.md`, and affected tests when CLI help, snapshot output, panel labels, header/footer text, packaging, release, CI, or developer-tooling behavior changes
- **Package version source** — `hermesd.__version__` is derived from installed package metadata; bump `pyproject.toml` for releases rather than hardcoding version text in the UI

## Adding a New Panel

1. Create `hermesd/panels/your_panel.py` with a `render_your_panel(state, theme, detail=False)` function
2. Add your data to `hermesd/models.py` (Pydantic model)
3. Collect the data in the matching `hermesd/collect/*.py` reader, wired in via `hermesd/collector.py`
4. Register in `hermesd/panels/__init__.py`: add a `_render_your_panel(ctx: PanelRenderContext)` wrapper, add it to `_RENDERERS`, and add the label to `PANEL_NAMES`
5. Add tests in `tests/test_your_panel.py`
6. Update the overview layout specs in `hermesd/app.py` (`_WIDE_LAYOUT_SPEC`, `_COMPACT_LAYOUT_SPEC`, `_TALL_NARROW_LAYOUT_SPEC`) if the new panel needs overview placement

## Adding Data to an Existing Panel

1. Add fields to the relevant model in `hermesd/models.py`
2. Populate them in the matching `hermesd/collect/*.py` reader, wired in via `hermesd/collector.py`
3. Render them in the panel's `_render_compact` and/or `_render_detail` functions
4. Add tests

## Testing

```bash
# Full suite
uv run pytest tests/ -v -W error::ResourceWarning --cov=hermesd --cov-report=term-missing

# Single file
uv run pytest tests/test_collector.py -v

# Watch for failures
uv run pytest tests/ -x --tb=short
```

Test categories:
- `test_models.py` — Pydantic model construction
- `test_db_extended.py` / `test_db_resilience.py` — SQLite reader, WAL snapshotting, caching, resilience
- `test_file_cache.py` — mtime-keyed JSON/YAML cache
- `test_collector.py` / `test_collector_extended.py` / `test_collector_coverage.py` / `test_collector_fixes.py` — data collection from `~/.hermes/`
- `test_session_active.py` — session active/ended detection
- `test_cost_estimation.py` — token/cost reconciliation edge cases
- `test_theme.py` — skin loading and theme inheritance
- `test_formatting.py` — shared panel formatting helpers
- `test_main.py` — CLI argument parsing and snapshot modes
- `test_app.py` / `test_app_extended.py` / `test_app_fixes.py` — TUI key handling, layout, lifecycle
- `test_panels.py` / `test_panels_extended.py` / `test_panels_fixes.py` — cross-panel rendering (compact + detail) and panel regression fixes
- `test_cron_panel.py` / `test_curator_panel.py` / `test_curator.py` / `test_memory_panel.py` / `test_skills_panel.py` / `test_tools_panel.py` / `test_profiles_panel.py` / `test_profiles.py` — dedicated panel rendering tests
- `test_gateway_resilience.py` / `test_curator_resilience.py` — error handling, cache preservation
- `test_persistence_fixes.py` — persistence-layer regression fixes
- `test_package_metadata.py` — packaging, workflow, long-description, and wheel-smoke contracts
- `test_import_hygiene.py` / `test_readonly_invariant.py` — standalone-package and read-only safety contracts
- `test_markup_safety.py` — Rich markup and secret-redaction safety
- `test_contract.py` — opt-in contract test against a real `~/.hermes` (not part of the default suite)

## Reporting Issues

Please include:
- Terminal emulator and size (`echo $TERM`, `tput cols`, `tput lines`)
- Whether you're using SSH/tmux/screen
- Hermes Agent version (`hermes version`)
- hermesd version (`hermesd --version`)
- The error traceback if applicable
- A short log excerpt or `--snapshot` / `--snapshot-format json` output when it helps reproduce the display state

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
