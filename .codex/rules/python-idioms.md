# Modern Python Idioms (3.11 – 3.13)

Each pattern below is tagged with its minimum Python version. hermesd's `requires-python = ">=3.11"` and CI matrix-tests on 3.11, 3.12, and 3.13 — so **any syntax tagged 3.12+ or 3.13+ cannot be used unconditionally** in the codebase. It may only appear behind a version guard or in code paths that the 3.11 interpreter never parses.

If you raise `requires-python` in `pyproject.toml`, update this file's floor.

Project-wide conventions that apply on every version:

- `from __future__ import annotations` at the top of every module — keeps annotations as strings so forward refs and `X | None` stay cheap on all supported versions.
- `pathlib.Path` over `os.path`.
- f-strings over `format()` or `%`.
- `dict`, `list`, `tuple`, `set` as generic parameters (no `typing.Dict`/`typing.List`).
- `X | None` over `Optional[X]`; `X | Y` over `Union[X, Y]`.

## Version badges

- **3.11+** — safe everywhere in hermesd.
- **3.12+** — SyntaxError on 3.11. Do not use in library code.
- **3.13+** — ImportError/AttributeError on 3.11 and 3.12. Do not use in library code.

---

## Exception Groups — 3.11+

```python
raise ExceptionGroup("validation errors", [
    ValueError("name is required"),
    ValueError("age must be positive"),
])

try:
    validate(data)
except* ValueError as eg:
    for err in eg.exceptions:
        log.error(str(err))
```

Use when multiple independent errors should be reported together (validation, concurrent task failures).

## TaskGroup — 3.11+

```python
async with asyncio.TaskGroup() as tg:
    tg.create_task(fetch_users())
    tg.create_task(fetch_orders())
```

Prefer over bare `create_task` for structured concurrency with automatic cancellation. Use `asyncio.gather(return_exceptions=True)` when all tasks must complete regardless of individual failures. **Note:** hermesd is threading-based, not async — this rule applies to any future async surface only.

## StrEnum — 3.11+

```python
from enum import StrEnum

class Status(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    CLOSED = "closed"
```

Prefer over plain string constants for fixed string sets. Values serialize naturally to JSON.

## Self Type — 3.11+

```python
from typing import Self

class Builder:
    def with_name(self, name: str) -> Self:
        self._name = name
        return self
```

Use for fluent APIs and factory methods instead of forward references or class-bound `TypeVar`.

## Never / NoReturn — 3.11+

```python
from typing import Never, NoReturn

def die(msg: str) -> NoReturn:
    raise RuntimeError(msg)

def exhaustive(x: Status) -> str:
    match x:
        case Status.PENDING: return "p"
        case Status.ACTIVE:  return "a"
        case Status.CLOSED:  return "c"
    _: Never = x  # mypy catches a missing case here
    raise AssertionError(_)
```

`NoReturn` on return annotations of functions that always raise; `Never` for the bottom type in parameters or exhaustiveness checks. Equivalent to type checkers — convention differs.

## Structural Pattern Matching — 3.10+ (available since 3.11)

```python
match command:
    case {"action": "create", "name": str(name)}:
        create(name)
    case {"action": "delete", "id": int(item_id)}:
        delete(item_id)
    case _:
        raise ValueError(f"unknown command: {command}")
```

Use for multi-branch dispatch on structured data and command patterns. Prefer `if`/`elif` for simple value comparisons.

## Dataclass improvements — 3.10+

```python
from dataclasses import dataclass, field

@dataclass(frozen=True, slots=True, kw_only=True)
class OrderLine:
    product_id: str
    quantity: int = field(default=1)
    unit_price: Decimal = field(default=Decimal("0"))
```

Prefer `frozen=True` for immutable value objects, `slots=True` for memory efficiency, `kw_only=True` for clarity at call sites.

## Walrus Operator — 3.8+

```python
if (match := pattern.search(text)) is not None:
    process(match.group(1))

while chunk := file.read(8192):
    process(chunk)
```

Use when it eliminates a redundant call or makes a loop cleaner. Do not nest or chain walrus expressions.

---

## 3.12+ syntax — **do not use unconditionally** while `requires-python = ">=3.11"`

The following forms parse as SyntaxError on 3.11. Either bump `requires-python` first, or stick with the pre-3.12 equivalent.

### Type statement (3.12+)

```python
# 3.12+ only — SyntaxError on 3.11:
type Vector = list[float]
type Callback[T] = Callable[[T], None]
```

Pre-3.12 equivalent (safe everywhere):

```python
from typing import TypeAlias
Vector: TypeAlias = list[float]
```

### Inline generic syntax (3.12+)

```python
# 3.12+ only:
def first[T](items: Sequence[T]) -> T: ...

def decorator[**P, R](fn: Callable[P, R]) -> Callable[P, R]: ...
```

Pre-3.12 equivalent:

```python
from typing import TypeVar, ParamSpec
T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")

def first(items: Sequence[T]) -> T: ...
def decorator(fn: Callable[P, R]) -> Callable[P, R]: ...
```

### @override (3.12+)

```python
from typing import override

class JsonParser(BaseParser):
    @override
    def parse(self, data: bytes) -> dict[str, Any]: ...
```

`typing.override` exists in 3.12+. On 3.11 it imports from `typing_extensions` — add `typing-extensions` as a dependency first if the project needs this on 3.11.

### NewType

```python
UserId = NewType("UserId", int)
```

`NewType` itself works on every supported version — use plain assignment, **not** the 3.12 `type` statement.

---

## 3.13+ only — do not use unconditionally

### TypeIs (3.13)

```python
from typing import TypeIs

def is_str_list(val: list[object]) -> TypeIs[list[str]]:
    return all(isinstance(x, str) for x in val)
```

On 3.11/3.12, import from `typing_extensions` (add dependency first). If the project supports 3.11, prefer `TypeGuard` — it's available since 3.10 and works everywhere the project runs.

### warnings.deprecated (3.13)

```python
from warnings import deprecated

@deprecated("Use new_function instead")
def old_function() -> None: ...
```

`warnings.deprecated` is 3.13-only. On 3.11/3.12, import from `typing_extensions` (add dependency first) or use a manual deprecation decorator.

---

## Modern stdlib preferences — safe on every supported version

| Older pattern | Modern replacement | Available |
|---|---|---|
| `os.path.join` | `pathlib.Path` | everywhere |
| `"{}".format(x)` | `f"{x}"` | everywhere |
| `typing.Dict`, `typing.List` | `dict`, `list` | 3.9+ |
| `typing.Optional[X]` | `X \| None` | 3.10+ |
| `typing.Union[X, Y]` | `X \| Y` | 3.10+ |
| `typing.Tuple[X, Y]` | `tuple[X, Y]` | 3.9+ |
| `asyncio.gather` | `asyncio.TaskGroup` when cancel-on-failure is desired | 3.11+ |
| `@abc.abstractmethod` only | `Protocol` when no shared state needed | 3.8+ |
| `unittest.TestCase` | `pytest` functions with fixtures | everywhere |

## uv patterns

```sh
uv init                          # new project with pyproject.toml
uv add <package>                 # add dependency
uv add --dev <package>           # add dev dependency
uv run pytest                    # run in managed environment
uv run mypy .                    # type check in managed environment
uv run ruff check .              # lint
uv run ruff format .             # format
uv lock                          # regenerate lockfile
uv sync                          # sync environment to lockfile
uv tool run ruff check .         # run tool without installing globally
```

Prefer `uv run` for all project commands. Use `uv tool run` (or `uvx`) for one-off tool execution.
