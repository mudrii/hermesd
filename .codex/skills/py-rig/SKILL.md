---
name: py-rig
description: "Use this skill when building, reviewing, or refactoring Python code that requires strong maintainability discipline: SRP, DRY, OCP, explicit dependency injection, TDD/ATDD workflow, strict typing, architecture review, and clean project structure. Complements the project's Python AGENTS.md with process rigor."
metadata:
  short-description: Python design, workflow, and review discipline
  slash-command: enabled
---

<objective>
Apply strict design and testing discipline for Python projects.

This skill complements `AGENTS.md`, `.codex/rules/python-patterns.md`, and `.codex/rules/python-idioms.md`. It must stay aligned with all three and applies as an execution discipline layer on top of the project's Python standards.

This skill adds execution rigor:

- ATDD/TDD workflow
- SRP, DRY, and OCP decision rules
- explicit dependency injection discipline with a pragmatic carve-out for CLI composition roots
- strict type discipline
- comment quality standards
- structured implementation and review checks
- hermesd-specific invariants (cache-preservation, read-only access, panels as OCP seam)

If `AGENTS.md` is stricter on any point, follow `AGENTS.md`.
</objective>

<when_to_use>
Use this skill when:

- implementing a new feature or behavior increment
- refactoring Python code for clearer ownership or testability
- reviewing module boundaries or dependency flow
- replacing hidden collaborator construction with explicit injection
- tightening tests around user-visible or integration behavior
- removing `Any` types or tightening weak types in touched code
- cleaning up hardcoded values or global mutable state
</when_to_use>

<process>
Follow this workflow:

1. Inspect the project first.
   Read `pyproject.toml`, project layout, local `AGENTS.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, tool configs (`[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]`), and existing tests.

2. Define acceptance behavior first.
   Express the user-visible outcome before writing implementation details.

3. Add or update an acceptance-level test when the project has that layer.
   For hermesd, the acceptance seam is `Collector → DashboardState → panel renderer`. Use `rich.console.Console(record=True)` to capture output for end-to-end render assertions.

4. Add the next smallest failing test.
   Prefer a focused unit or module test for the next behavior increment.

5. Implement the minimum change that makes the test pass.
   Keep the diff tight. Do not rewrite unrelated code.

6. Refactor while green.
   Improve naming, cohesion, dependency flow, and readability without changing behavior.

7. Keep standards and user-facing docs in sync.
   If the task materially changes project conventions, architecture, or workflow expectations, update `AGENTS.md` or the relevant rule/skill in the same change. Update `CHANGELOG.md` for user-visible changes and `README.md` when install/usage instructions change.

8. Verify locally before opening a PR.
   Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy hermesd`, `uv run pytest tests/ -v -W error::ResourceWarning`, `uv run pip-audit`, `uv lock --check`, `uv build`, wheel smoke installs, and `uvx twine check dist/*`. CI runs the same gate commands across Python 3.11/3.12/3.13.
</process>

<design_rules>
Apply these rules during implementation:

- Keep project structure clean and predictable
- **SRP**: each module, class, and function should have one clear reason to change
- **DRY**: remove repeated validation, mapping, branching, and policy logic when the abstraction improves clarity
- **OCP**: extend behavior through composition, Protocols, configuration, and strategy injection instead of invasive branching or copy-paste forks
- Prefer domain-oriented module boundaries over technical dumping grounds
- Keep domain logic separate from transport, persistence, configuration, and presentation concerns
- Prefer the smallest coherent abstraction that solves the real duplication or extension point
- Do not introduce Protocol-first abstractions without real consumer pressure
- Prefer composition over inheritance; keep inheritance hierarchies shallow

**OCP example in hermesd.** `hermesd/panels/__init__.py` is the canonical OCP seam: add a new panel by writing `hermesd/panels/your_panel.py`, adding a `_render_your_panel(ctx: PanelRenderContext)` wrapper, then registering that wrapper in `_RENDERERS` and the label in `PANEL_NAMES`. This pattern is documented in `CONTRIBUTING.md#Adding a New Panel`.
</design_rules>

<dependency_injection>
Manage code relationships explicitly.

General rules:

- Use constructor injection (`__init__` parameters) for long-lived collaborators
- Use function parameters for short-lived collaborators and pure logic inputs
- Pass dependencies through typed config, constructors, or arguments
- Do not instantiate external clients, repositories, clocks, or runtime collaborators inside **core domain logic**
- Do not hardcode dependency selection, URLs, ports, credentials, or feature switches
- Avoid globals, module-level mutable state, and hidden singletons

**Composition-root carve-out.** The *composition root* — typically the `main()` entry point or a single top-level CLI class — is allowed to construct collaborators directly. In hermesd, `__main__.main()` and `DashboardApp.__init__` form this root: they may call `Collector(hermes_home)` and `load_theme(hermes_home)` without those being injected parameters. Below the composition root, follow the general DI rules strictly — collectors, renderers, and panel functions must not construct their own DB readers, file readers, clocks, or theme loaders.

When testing the composition root itself, prefer constructing it with a fake `hermes_home` directory (see `tests/conftest.py::hermes_home`) rather than mocking the inner collaborators.

Python-specific guidance:

- Prefer Protocols at dependency boundaries for structural typing
- Use `dataclasses` or Pydantic models for typed configuration
- pytest fixtures are the natural DI mechanism in tests — prefer fixtures over monkeypatch
- Use `functools.partial` or closures for lightweight function-level injection
- Avoid DI frameworks; explicit wiring is preferred

```python
# hermesd-shaped constructor injection — domain logic stays testable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

class DatabaseReader(Protocol):
    def read_sessions(self) -> list[dict[str, object]]: ...
    def read_tool_stats(self) -> list[dict[str, object]]: ...

class Clock(Protocol):
    def now(self) -> float: ...

@dataclass(frozen=True, slots=True)
class SessionSummarizer:
    db: DatabaseReader     # injected, not constructed inside
    clock: Clock           # no time.time() inside this class

    def summarize(self) -> dict[str, object]:
        rows = self.db.read_sessions()
        return {"count": len(rows), "as_of": self.clock.now()}
```

```python
# function parameter for short-lived/pure logic
def within_budget(cost: float, now: float, budget_reset_at: float) -> bool:
    return now >= budget_reset_at or cost < 10.0
```
</dependency_injection>

<threading_rules>
hermesd is threading-based, not async. Apply these rules to any code that runs across threads:

- Use `threading.Lock`/`RLock` for shared mutable state; name the attribute `_*_lock` and document what it guards.
- Use `threading.Event` for cross-thread signaling (e.g., `_force_refresh` in `DashboardApp`). Prefer `Event.wait(timeout=...)` over sleep loops.
- Daemon threads (`daemon=True`) must not hold open resources through shutdown. Close explicitly in a `finally:` block on the main thread.
- **Signals only deliver to the main thread.** Register `signal.signal(SIGINT, handler)` on the main thread; daemon threads cannot receive signals. Signal handlers must be fast and set flags only — never call into Rich or SQLite from a handler.
- Any SQLite connection shared across threads must be created with `check_same_thread=False`. See `hermesd/db.py`.
- Never raise across thread boundaries without propagation — the collector thread in `app.py::_collector_loop` swallows exceptions into `is_stale=True` on purpose. Mirror that pattern for any new background thread.
- When a new background task is added, require an explicit cancellation path (an Event or a `self._running` flag the thread checks every iteration).
- Flag shared mutable state immediately; push toward immutable data copies or message passing through a queue.

For any future async code (not applicable to current hermesd):

- Prefer `asyncio.TaskGroup` (3.11+) for structured concurrency over bare `create_task`.
- Use `asyncio.gather(return_exceptions=True)` when all tasks must complete regardless of failures.
- Never nest `asyncio.run()` calls; one event loop per thread; prefer `asyncio.run()` or `asyncio.Runner` as the single entry point.
- Never mix sync and async I/O in the same code path without `run_in_executor`.
</threading_rules>

<resilience_rules>
hermesd is a **read-only** observer of `~/.hermes/`. Data sources can disappear, corrupt, or lag. Readers MUST preserve continuity of display.

- **Cache last-good data.** Every reader (SQLite, JSON, YAML, log file) caches the last successful read and returns it on transient errors. See `hermesd/db.py::HermesDB._cached_*` and `hermesd/collector.py::_read_json_cached`.
- **Count consecutive errors.** After N (currently 3) consecutive failures, reconnect/reload rather than retrying blindly. See `HermesDB._consecutive_errors`.
- **Never blank the display.** If a panel has no data, show "no data" text styled for the theme — do not raise or return `None` for the renderable.
- **NULL tolerance.** SQLite column reads use `or 0` / `or ""` coalescence, not `dict.get(key, default)`. This is a `AGENTS.md` critical rule.
- **Read-only always.** Never open `~/.hermes/*.db` without `?mode=ro`. Never write files, create directories, or mutate data under `~/.hermes/`.
- **Do not import hermes-agent.** Read the on-disk formats directly.
</resilience_rules>

<style_and_readability>
Keep the code easy to read and maintain.

- Write modern Python and prefer 3.11-safe idioms; see `.codex/rules/python-idioms.md` for version-tagged patterns
- Put `from __future__ import annotations` at the top of every module (project convention)
- Keep functions short, explicit, and focused on one job
- Use consistent formatting and indentation; `ruff format` defines layout (4 spaces, 100-char lines per `[tool.ruff]`)
- Use whitespace to separate concepts, not decorate code
- Use meaningful whitespace: blank lines between logical sections within functions, two blank lines between top-level definitions
- Keep naming concrete and intention-revealing; `snake_case` for functions/variables, `PascalCase` for classes
- Prefer straightforward control flow over clever compression
- Early returns over deep nesting; guard clauses at the top
- Separate logic clearly so domain rules, I/O, transport, persistence, and orchestration are easy to trace
- Avoid hardcoded values; move runtime values and environment behavior into typed config, constants, or inputs
- Explicit imports; no `import *` outside `__init__.py` re-exports
- Inline function-level imports are allowed when they break an import cycle or defer heavy startup cost (see `hermesd/__main__.py::main` deferring `DashboardApp` import). Prefer top-of-module imports otherwise.
</style_and_readability>

<type_discipline>
Strict types are enforced via `mypy` in CI.

- `[tool.mypy]` in `pyproject.toml` is the source of truth for enforced strictness
- Every public function has explicit parameter and return type annotations
- Minimize `Any` in domain code; use `object`, `Protocol`, generics, `Union`, or narrower types
- **Boundary-Any carve-out.** Raw SQL row reads in `hermesd/db.py` return `list[dict[str, Any]]` — this is allowed by per-module mypy override because SQLite rows are untyped at the source. Translate into typed Pydantic models at the earliest reasonable point (see `hermesd/collector.py::_collect_sessions`). Do not propagate `Any` past the collector.
- `NewType` for domain IDs and values that should not mix: `UserId = NewType("UserId", int)` — plain assignment, not the `type` statement
- `Protocol` for dependency boundaries; enables structural typing without inheritance coupling
- `Literal` for constrained value sets; `Final` for immutable module-level bindings
- `# type: ignore[code]` requires a specific error code and inline justification — never bare `# type: ignore`
- Avoid `cast()` unless unavoidable; prefer narrowing with `isinstance` or restructuring
- Make invalid states unrepresentable with enums, dataclasses with validation, and constrained constructors
- Keep weakly typed data at the boundary and translate into strict internal types immediately

Version-specific type syntax (see `.codex/rules/python-idioms.md` for full details):

- `@override` (3.12+) — requires `typing_extensions` import on 3.11; do not use unconditionally while the project supports 3.11
- `TypeIs` (3.13) — requires `typing_extensions` on 3.11/3.12; prefer `TypeGuard` in library code supporting 3.11
- Inline generic syntax `def fn[T](...)` (3.12+) — SyntaxError on 3.11; use the old `TypeVar("T")` form in library code
</type_discipline>

<testing_discipline>
Testing is mandatory. TDD is the default workflow.

- **ATDD first** for user-visible changes: define the acceptance scenario before writing implementation
  1. Express the boundary behavior (Given/When/Then or equivalent scenario)
  2. Add or update the acceptance-level test
  3. Write the next smallest failing unit test
  4. Implement the minimum change that makes it pass
  5. Refactor while green
  6. Repeat for the next behavior increment
- **TDD** for the next increment: write the smallest failing unit test, implement, then refactor
- Use ATDD extensively for user-visible behavior, acceptance flows, and cross-boundary scenarios
- Every meaningful change should cover:
  - expected behavior (happy path)
  - invalid input and validation failures
  - edge cases and boundary values
  - error and failure paths
- Use `pytest` with fixtures; prefer fixtures over monkeypatch for dependency injection
- `@pytest.mark.parametrize` for table-driven tests; direct narrative tests when variation is not the point
- Fakes and stubs over mocks; test behavior not implementation
- Keep tests deterministic; avoid sleep-based flakiness
- Use `tmp_path` for filesystem tests; `time_machine` or `freezegun` for time-dependent tests
- Keep public API doctests accurate when behavior changes

**hermesd test taxonomy.** Tests live in a flat `tests/` directory with file-prefix categories:

| Prefix | Scope |
|---|---|
| `test_models.py` | Pydantic model construction, field defaults |
| `test_db*.py` | SQLite reader, caching, resilience |
| `test_collector*.py` | data collection from `~/.hermes/` |
| `test_*_panel.py` | panel rendering (compact + detail) |
| `test_app*.py` | TUI key handling, layout, lifecycle |
| `test_*_resilience.py` | error handling, cache preservation |

Add new tests to the matching prefix. Use `tests/conftest.py` fixtures (`hermes_home`, `sample_db`, `populated_hermes_home`) rather than hand-rolling setup.

**Acceptance seam.** For user-visible changes, assert against a full `Collector → DashboardState → render_panel` flow using `rich.console.Console(record=True)` to capture output. See existing `test_*_resilience.py` files for the pattern.

```python
# hermesd-shaped fixture + sync test (no async — project is threading-based)
@pytest.fixture
def collector(hermes_home: Path) -> Collector:
    return Collector(hermes_home)

def test_collector_returns_empty_state_when_home_is_bare(collector: Collector) -> None:
    state = collector.collect()
    assert state.gateway.running is False
    assert state.sessions == []

def test_collector_preserves_cache_on_db_corruption(
    collector: Collector, sample_db: Path,
) -> None:
    first = collector.collect()
    sample_db.write_bytes(b"corrupt")
    second = collector.collect()
    assert second.sessions == first.sessions  # cache-preservation invariant
    assert second.is_stale is False  # collector thread sets is_stale, not collect()
```
</testing_discipline>

<comment_rules>
Write comments only when they add information the code cannot carry cleanly on its own.

Good comments explain:

- intent and rationale for non-obvious design choices
- invariants and constraints
- ownership or concurrency rules
- non-obvious tradeoffs or performance considerations
- why a particular approach was chosen over alternatives

Do not write comments that:

- restate the code
- narrate simple assignments or obvious operations
- explain standard Python syntax
- leave vague TODOs without context or ticket reference
- duplicate the docstring with less precision

Docstrings (hermesd convention):

- Use short, single-line docstrings ("""Description.""") for public functions and fixtures
- Omit docstrings on private helpers, test functions, and obvious one-liners
- When the function has a complex contract, a short multi-line docstring is acceptable — match the neighbouring style
- Do not adopt Google/NumPy-style blocks; the project has not historically used them
- Update docstrings when exported behavior, config, or API semantics change
</comment_rules>

<error_and_type_rules>
Keep errors and types strict and readable.

- Raise specific exceptions with context; never bare `except:` — it catches `SystemExit` and `KeyboardInterrupt`
- `except Exception:` without re-raise is acceptable only at top-level boundary handlers:
  - CLI entry point (`hermesd/__main__.py::main`)
  - Collector and input threads (`hermesd/app.py::_collector_loop`, `_input_loop`)
  - Per-source collection boundary (`hermesd/collector.py::_CollectionHealth.collect`)
  - Message-search worker thread (`hermesd/app.py::_search_session_messages_worker`)
  - Signal handlers
  Everywhere else, re-raise or handle specifically.
- `raise NewError("context") from original_err` to preserve cause chains
- Custom exception hierarchies for domain errors; stdlib exceptions for programming errors
- `contextlib.suppress(SpecificError)` only for known-safe cases with clear justification
- Keep error messages actionable and specific enough to debug
- Never log secrets, tokens, credentials, or sensitive payloads
- Use `ExceptionGroup` and `except*` (3.11+) when multiple independent errors should be reported together
- Return `None` or `Optional` only when absence is a valid, documented part of the contract
- Do not use sentinel values when an exception or `Optional` return would be clearer
</error_and_type_rules>

<tooling_rules>
Verification is part of the implementation, not an optional cleanup step.

- Run `uv run ruff format --check .` (or `uv run ruff format .` to fix)
- Run `uv run ruff check .` (with `--fix` for auto-fixable issues)
- Run `uv run mypy hermesd` (configured via `[tool.mypy]` in `pyproject.toml`)
- Run `uv run pytest tests/ -v -W error::ResourceWarning` (with relevant paths for tighter affected-scope checks)
- Run `uv run pip-audit`; CI runs it on every push
- Run `uv lock --check`; if dependencies changed, run `uv lock` and verify the diff
- Run `uv build`, smoke install `dist/hermesd-*.whl`, and run `uvx twine check dist/*` before release or packaging-affecting changes
- If public API changed, verify docstrings and update README/CHANGELOG if user-facing
- Treat linting and static analysis as normal development tools, not release-only checks
- Fix root causes instead of scattering `# noqa` or `# type: ignore` comments
- If a lint rule is intentionally suppressed, use the specific code and add a justification comment
- For local enforcement, `pre-commit` is a reasonable option — add a `.pre-commit-config.yaml` running ruff + mypy on staged files if the team wants it
</tooling_rules>

<review_checklist>
Before finishing, verify:

- [ ] `AGENTS.md#Critical Rules` project invariants hold (read-only, no hermes-agent imports, cache preservation, NULL tolerance, escape-sequence bulk reads)
- [ ] module boundaries are coherent — no cross-domain leaks
- [ ] responsibilities are not mixed across domain, transport, persistence, and config
- [ ] structure is clean, predictable, and free of dumping-ground modules
- [ ] dependencies are injected explicitly below the composition root — no hidden construction or global state
- [ ] no hardcoded runtime values (URLs, ports, credentials, paths, timeouts)
- [ ] types are strict and explicit — no `Any` outside the documented `db.py` boundary, no bare `# type: ignore`
- [ ] every public function has explicit parameter and return type annotations
- [ ] functions are short, focused, and readable in one pass
- [ ] formatting, indentation, and whitespace follow `ruff format` defaults
- [ ] comments explain intent or invariants instead of restating the code
- [ ] error handling is clear, concise, contextual, and uses cause chains
- [ ] no bare `except:`; `except Exception:` only at documented boundary handlers; no swallowed errors; no mutable default arguments
- [ ] tests cover acceptance behavior and unit behavior; both new tests were written **first**
- [ ] TDD/ATDD flow was followed
- [ ] threading: locks, events, daemon-thread shutdown, and signal handlers follow `<threading_rules>`
- [ ] resilience: cache-preservation, consecutive-error counting, and "never blank the display" hold per `<resilience_rules>`
- [ ] public API docstrings are accurate and updated when behavior changed
- [ ] `CHANGELOG.md` updated for user-visible changes; `README.md` updated for install/usage changes
- [ ] the full local gate from `<tooling_rules>` passes locally
- [ ] version/tooling guidance from `AGENTS.md`, `.codex/rules/python-idioms.md`, and `.codex/rules/python-patterns.md` has been followed
</review_checklist>

<reject_patterns>
Reject these patterns:

- giant functions mixing validation, orchestration, and persistence
- Protocol-per-class abstraction without consumer need
- hardcoded configuration or collaborator construction **below the composition root**
- `Any` added or left in touched code without explicit justification or the documented boundary carve-out
- bare `except:` anywhere; `except Exception:` without re-raise outside top-level boundary handlers
- mutable default arguments (`def fn(items=[])`)
- `import *` in non-`__init__.py` files
- module-level mutable global state used as hidden dependency
- comments that restate code
- hidden singletons or global registries
- brittle mock-only tests when a fake or boundary test would be clearer
- transport or storage concerns embedded in core domain logic
- `# type: ignore` without specific error code and justification
- production design distorted to satisfy a mock framework
- large speculative refactors when a smaller coherent change would solve the task
- `cast()` used to silence type errors instead of fixing the type
- deep inheritance hierarchies when composition would be clearer
- writing to `~/.hermes/` or importing from `hermes-agent`
- returning blank/None renderables from panels on error (violates "never blank the display")
- 3.12+ syntax (inline generics, `type` statement, `@override`) or 3.13+ syntax (`TypeIs`, `warnings.deprecated`) in library code while `requires-python` still includes 3.11
</reject_patterns>

<success_criteria>
This skill is being followed correctly when:

- changes are small, test-backed, and easy to review
- dependency flow is explicit from the composition root downward
- module responsibilities are cleaner after the change, not blurrier
- types in touched code are more precise after the change, not less
- the implementation matches the Python standards in `AGENTS.md` and `.codex/rules/`
- tests speak in behavior terms, not implementation vocabulary
- the resulting code reads clearly without comments explaining the control flow
- the resulting code is easier to extend without rewriting stable behavior
- cache-preservation and read-only invariants are preserved
- `uv run ruff check`, `uv run mypy hermesd`, `uv run pytest`, and `uv run pip-audit` all pass
</success_criteria>
