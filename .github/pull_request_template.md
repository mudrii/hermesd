## Summary

Brief description of the change.

## Changes

- ...

## Testing

- [ ] Full local gate passes:
      `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy hermesd`,
      `uv run python -m compileall hermesd`,
      `uv run pytest tests/ -v -W error::ResourceWarning`, `uv run pip-audit`,
      `uv lock --check`, `uv build`, wheel/sdist smoke install, `uv run twine check dist/*`
- [ ] New tests added for new functionality
- [ ] Tested manually with `hermesd` against a live `~/.hermes/`
- [ ] Works in SSH/tmux at 80x24
- [ ] Updated `CHANGELOG.md` and docs for visible behavior, packaging, release, CI, or developer-tooling changes

## Screenshots

If this changes the TUI, include before/after screenshots.
