# Python Patterns and Style Reference

Detailed conventions that complement `AGENTS.md`. The py-rig skill owns process discipline (ATDD/TDD, DI, review workflow). This file owns language-specific style, API, type, and testing patterns scoped to hermesd.

Sections irrelevant to hermesd (HTTP clients, DB transactions, structlog, pydantic-settings, click/typer CLI frameworks) have been removed. Re-add them only when the project actually gains those surfaces.

## Python Style

- Write modern Python; target the project's minimum version (`>=3.11`)
- Reference `.codex/rules/python-idioms.md` for version-tagged syntax — do not use 3.12+ or 3.13+ forms in library code
- `from __future__ import annotations` at the top of every module (project convention; see all `hermesd/*.py`)
- `snake_case` for functions, methods, variables, modules; `PascalCase` for classes; `UPPER_SNAKE_CASE` for module-level constants
- Boolean names should read clearly: `is_`, `has_`, `can_`, `should_` prefixes
- Private by convention: `_prefix` for internal APIs; avoid `__mangling` unless preventing subclass collision
- Prefer `pathlib.Path` over `os.path`; f-strings over `format()` or `%`
- Prefer comprehensions and generator expressions over `map`/`filter` with lambdas when readability improves
- Use `match`/`case` for multi-branch dispatch on structured data; prefer `if`/`elif` for simple conditions
- Prefer `dataclasses` (with `frozen=True, slots=True`) for plain data; Pydantic for validated external input (see `hermesd/models.py`)
- Use `enum.Enum` and `enum.StrEnum` for fixed sets of values; avoid stringly typed state
- Use `functools` and `itertools` idioms when they improve clarity; do not golf readability away
- Explicit imports; no `import *` outside `__init__.py` re-exports
- Inline function-level imports are allowed for deferred startup cost (see `hermesd/__main__.py::main`) or to break cycles; prefer top-of-module imports otherwise
- Import order: stdlib, third-party, local — `ruff` enforces this via the `I` (isort) rule set

## Type System

- `mypy` is enforced in CI via `[tool.mypy]` in `pyproject.toml`
- Every public function has explicit parameter and return types
- Minimize `Any` in domain code; use `object`, `Protocol`, generics, `Union`, or narrower types
- **Boundary `Any` carve-out**: `hermesd/db.py` returns `list[dict[str, Any]]` because SQLite rows are untyped at the source. The per-module mypy override allows this. Convert to Pydantic models at the earliest reasonable point (`hermesd/collector.py`). Do not let `Any` spread past the collector.
- `Protocol` for structural typing at dependency boundaries; prefer over ABCs when no shared state or default behavior is needed
- `TypeVar`, `ParamSpec`, `TypeVarTuple` for generic APIs; use the pre-3.12 form in library code (`T = TypeVar("T")`), not the 3.12+ inline `def fn[T](...)` syntax
- `NewType` for domain IDs and values that should not mix; uses plain assignment (`UserId = NewType("UserId", int)`), not the 3.12 `type` statement
- `Literal` for constrained string/int values; `Final` for immutable module-level bindings
- `Never` (3.11) for the bottom type in parameters and impossible branches; `NoReturn` for return annotations on functions that always raise
- Avoid `cast()` unless unavoidable; prefer narrowing or restructuring
- `# type: ignore[code]` requires a specific error code and inline justification

Version-guarded type features (import from `typing_extensions` on older versions if you need them on 3.11):

- `@override` — 3.12+, otherwise `typing_extensions.override`
- `TypeIs` — 3.13, otherwise `typing_extensions.TypeIs`; prefer `TypeGuard` (3.10) in 3.11-supporting library code

## Design and Dependencies

- **Open/Closed Principle**: extend through composition, Protocols, and config instead of modifying stable code paths. `hermesd/panels/__init__.py` is the project's canonical OCP seam — register a new renderer there rather than editing existing panel code.
- Prefer composition over inheritance; keep inheritance hierarchies shallow
- DI to manage code relationships: pass dependencies as arguments or constructor parameters
- CLI **composition-root carve-out**: the `main()` entry point and `DashboardApp` are allowed to construct collaborators directly (`Collector`, `load_theme`). Below that root, pass collaborators in rather than constructing them.
- Do not hardcode secrets, tokens, URLs, ports, file paths, or environment-specific IDs. `~/.hermes/` is resolved via `--hermes-home` CLI arg, `HERMES_HOME` env var, or default — never string-literaled.
- Reuse existing utilities and dependencies before adding new packages
- Do not add a dependency unless the project does not already solve the problem
- Do not create parallel helpers or wrappers when the project has a standard way

## Error Handling Detail

- Raise domain-specific exceptions for business rule violations
- Use stdlib exceptions (`ValueError`, `TypeError`, `KeyError`) for programming errors and invalid arguments
- `raise NewError("context") from original_err` to preserve cause chains
- Never bare `except:` — catches `SystemExit` and `KeyboardInterrupt`
- `except Exception:` without re-raise is acceptable only at top-level boundary handlers:
  - CLI entry point (`hermesd/__main__.py::main`)
  - Collector and input threads (`hermesd/app.py::_collector_loop`, `_input_loop`) — exceptions become `is_stale=True` rather than crashing the TUI
  - Per-source collection boundary (`hermesd/collector.py::_CollectionHealth.collect`) — a failing data source is marked failed and falls back to last-good/default data rather than aborting the whole refresh; preserves the cache-preservation invariant
  - Message-search worker thread (`hermesd/app.py::_search_session_messages_worker`) — a search failure surfaces a distinct error string instead of crashing the daemon worker
  - Signal handlers — set a flag and return fast
  Everywhere else, re-raise or handle specifically.
- `contextlib.suppress(SpecificError)` only for known-safe suppression
- Keep error messages actionable and specific enough to debug
- Never log secrets, tokens, credentials, or sensitive payloads
- Do not add defensive branches for impossible states unless evidence exists

## Testing Patterns

- `pytest` as the framework; use fixtures for setup and dependency injection
- Tests live in a flat `tests/` directory with file-prefix categories (`test_models.py`, `test_db*.py`, `test_collector*.py`, `test_*_panel.py`, `test_app*.py`, `test_*_resilience.py`). Match the existing taxonomy.
- Use `tests/conftest.py` fixtures (`hermes_home`, `sample_db`, `populated_hermes_home`) rather than hand-rolling mock directories
- `@pytest.mark.parametrize` for table-driven tests; direct narrative tests when variation is not the point
- Fixtures over monkeypatch for dependency injection; monkeypatch only for environment and module-level state
- `tmp_path` fixture for filesystem tests; `time_machine` or `freezegun` for time-dependent tests
- Fakes and stubs over mocks; test behavior not implementation
- Avoid `unittest.mock.patch` on internals; prefer injecting the dependency
- Resilience tests (`test_*_resilience.py`) must assert the cache-preservation invariant: after an error, the next read returns the last-good data, not empty/None
- Acceptance-level assertions use `rich.console.Console(record=True)` to capture rendered output; see existing tests for the pattern
- Do not add broad new test infrastructure for a small local fix
- Do not fix unrelated failing tests unless they block the requested change
- Inspect the diff before updating snapshots, fixtures, or generated files

## Threading

hermesd is threading-based. See `.codex/skills/py-rig/SKILL.md` `<threading_rules>` for the full policy. Key points:

- `threading.Lock`/`RLock` for shared state; name attributes `_*_lock` and document what they guard
- `threading.Event` for cross-thread signaling; prefer `Event.wait(timeout=...)` over sleep loops
- Daemon threads close resources in a `finally:` block on the main thread
- Signals deliver only to the main thread; handlers set flags and return fast
- SQLite connections shared across threads need `check_same_thread=False`

## Common Patterns

- **Logging**: hermesd currently prints via Rich; there is no structured logger. Do not add `structlog` or `logging` configuration speculatively.
- **Config**: CLI flags (`argparse`) + env var fallback (`HERMES_HOME`) + YAML at `~/.hermes/config.yaml`. Do not hardcode paths or tokens. No `pydantic-settings` — the existing pattern is sufficient.
- **Database**: parameterized queries only. Read-only URI (`file:...?mode=ro`). Cache last-good results. Count consecutive errors and reconnect after N failures.
- **Security**: `pip-audit` runs in CI. Keep dependencies minimal and pinned via `uv.lock`.

## Documentation

- **README.md** — install, usage, screenshots; update when CLI flags or install instructions change
- **CHANGELOG.md** — update for every user-visible change (new panel, new flag, behavior change, fix)
- **CONTRIBUTING.md** — canonical contributor workflow; `AGENTS.md` links to it
- **Docstrings** — short single-line (`"""Short description."""`) for public functions and fixtures; omit on private helpers and obvious one-liners; do not adopt Google/NumPy-style blocks (not the project convention)

## Project Profile

hermesd is a **CLI TUI app**:

- stable exit codes (0 on normal quit, 1 on missing `~/.hermes/`)
- clear stderr errors at startup
- no TTY assumptions for the input thread (guarded by `sys.stdin.isatty()`)
- machine-readable JSON snapshots are supported through `--snapshot-format json`; keep
  JSON output tests in sync with any model or CLI snapshot changes
