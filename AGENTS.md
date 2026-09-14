# AGENTS.md

Guidance for AI coding agents working in this repository. See
<https://agents.md/> for the file convention.

## Coding Guidelines

Python conventions for this project. Rules are normative: **MUST** = enforced, **PREFER** = default unless there's a reason. Each rule has a minimal example. Consistency with existing code beats any rule below.

## Type annotations

- **MUST** annotate all function signatures (parameters and return types). Let type checkers infer locals.
  ```python
  # bad
  def fetch(url): ...
  # good
  def fetch(url: str) -> Response: ...
  ```
- **MUST** run `mypy --strict` (or `pyright` strict mode) in CI with zero errors.
- **MUST NOT** use `Any` except at true FFI boundaries. Use `object` and narrow instead.
- **MUST** use `TypeAlias` or `type` (3.12+) for non-trivial type expressions so they have a readable name.
  ```python
  type JsonDict = dict[str, "JsonValue"]
  type JsonValue = str | int | float | bool | None | list["JsonValue"] | JsonDict
  ```

## Data modeling — make illegal states unrepresentable

- **PREFER** `dataclasses` (frozen) or `NamedTuple` over plain dicts for structured data. Dicts are for dynamic keys.
  ```python
  # bad
  user = {"id": "abc", "name": "Jo"}
  # good
  @dataclass(frozen=True, slots=True)
  class User:
      id: str
      name: str
  ```
- **PREFER** `Literal` unions over open `str` for known finite sets.
  ```python
  Role = Literal["admin", "guest", "editor"]
  ```
- **PREFER** discriminated unions via tagged dataclasses and `isinstance` checks over dicts with optional keys.
  ```python
  @dataclass(frozen=True)
  class Loading: pass

  @dataclass(frozen=True)
  class Success(Generic[T]):
      data: T

  @dataclass(frozen=True)
  class Failure:
      error: str

  type Request[T] = Loading | Success[T] | Failure
  ```
- **MUST** use `Enum` or `StrEnum` for fixed value sets that carry behavior or need iteration. Use `Literal` for lightweight type narrowing only.
- **PREFER** immutable by default: frozen dataclasses, tuples over lists when length is fixed.

## Errors and results

- **MUST** define domain exceptions as a small hierarchy rooted in one base per module. Never raise bare `Exception` or `ValueError` for domain logic.
  ```python
  class PaymentError(Exception): pass
  class InsufficientFunds(PaymentError): pass
  class CardDeclined(PaymentError): pass
  ```
- **PREFER** returning a result type for *expected* outcomes; reserve exceptions for programmer errors and truly unexpected failures.
  ```python
  type Result[T, E] = Ok[T] | Err[E]
  ```
- **MUST NOT** use exceptions for control flow. If a branch is normal, model it in the return type.
- **MUST** let unexpected exceptions propagate. Do not catch `Exception` or `BaseException` broadly except at top-level entry points (CLI main, HTTP middleware).

## Functions

- **MUST** keep functions short and single-purpose. If a docstring needs "and" to describe what it does, split it.
- **MUST** separate pure computation from I/O. Functions that compute should not read files, call APIs, or print. Push side effects to the caller.
  ```python
  # bad: compute + I/O mixed
  def report(path: str) -> str:
      data = Path(path).read_text()
      return transform(data)

  # good: caller owns I/O
  def transform(raw: str) -> str: ...
  ```
- **PREFER** positional-or-keyword args for 1–2 params, keyword-only (`*`) for 3+.
  ```python
  def connect(*, host: str, port: int, timeout: float = 5.0) -> Socket: ...
  ```
- **MUST NOT** use mutable default arguments.
  ```python
  # bad
  def append(item: int, target: list[int] = []) -> list[int]: ...
  # good
  def append(item: int, target: list[int] | None = None) -> list[int]: ...
  ```

## Classes — use only when justified

- **MUST NOT** use a class when a function or dataclass suffices. A class needs **identity + mutable state + an invariant to protect + behavior**. No state → function. No behavior → dataclass.
- **MUST** validate in a `@classmethod` factory when construction can fail; keep `__init__` trivial.
  ```python
  class Email:
      def __init__(self, value: str) -> None:
          self._value = value

      @classmethod
      def create(cls, raw: str) -> "Email":
          if "@" not in raw:
              raise InvalidEmail(raw)
          return cls(raw)
  ```
- **MUST NOT** do I/O in `__init__`. Use named async factories or classmethods: `Config.load()`, `User.from_row()`.
- **MUST** use name-mangled (`__field`) or underscore-prefixed fields for internal state. Expose read access via `@property`.
- **PREFER** composition over inheritance. Use inheritance only for genuine "is-a" behavior, not code reuse. Use `Protocol` for structural subtyping.
- **MUST** keep the public surface minimal. Prefix internal methods with `_`.

## Protocols and abstractions

- **MUST** define abstractions from the *consumer's* perspective using `Protocol`, listing only the methods the consumer needs.
  ```python
  class SessionStore(Protocol):
      def save(self, session_id: str, ttl: int) -> None: ...
  # not: def __init__(self, db: FullDatabase)  # 40 methods, uses one
  ```
- **PREFER** `Protocol` over `ABC` unless you need shared implementation. Protocols enable structural typing without forcing an inheritance chain.
- **MUST NOT** create an interface for every class. An abstraction earns its existence when there are at least two implementations or the boundary is a test seam.

## Dependency injection

- **MUST** inject collaborators (clock, store, HTTP client, logger) rather than constructing or importing them inside business logic.
  ```python
  # bad
  def process() -> None:
      db = Database("prod-url")
      ...

  # good
  def process(store: SessionStore) -> None: ...
  ```
- **PREFER** plain function parameters for injection over framework-level magic. A function that takes its dependencies as arguments is trivially testable.

## Async

- **MUST NOT** mix sync and async I/O in the same call path. If a function awaits anything, its entire chain should be async.
- **MUST** use `asyncio.TaskGroup` (3.11+) for concurrent work, not bare `create_task` with manual gather.
  ```python
  async with asyncio.TaskGroup() as tg:
      tg.create_task(fetch_a())
      tg.create_task(fetch_b())
  ```
- **MUST NOT** call blocking I/O from async code without `asyncio.to_thread`.

## File & project structure

- **MUST** keep one primary concept per module and name the file after it: `email.py` → `Email`, `parse_config.py` → `parse_config`.
- **MUST NOT** create `utils.py` / `helpers.py` / `misc.py` grab-bags. If you can't name the module after its contents, the contents don't belong together.
- **MUST** enforce one-directional dependency flow: entry points (CLI, HTTP handlers) → core logic → nothing. Core **MUST NOT** import from I/O or delivery layers.
- **MUST** keep `__init__.py` as a re-export surface only — no logic.
- **MUST** avoid package/module stutter: `config/loader.py`, not `config/config_loader.py`.
- **MUST** colocate tests with a matching name: `planner.py` → `test_planner.py` (or `planner_test.py`).
- **PREFER** flat package layouts. Nest only when a sub-package has 4+ modules and a clear boundary.

## Testing

- **MUST** write tests for every public function and method. Untested code is assumed broken.
- **MUST** test behavior, not implementation. Assert on outputs and observable side effects, not on call counts or internal state.
- **PREFER** plain `pytest` functions over `unittest.TestCase` classes.
- **MUST** use fakes (in-memory implementations of protocols) over mocks for dependencies with complex behavior. Use `unittest.mock` only for verifying a call was made, not for replacing logic.
  ```python
  class FakeStore:
      def __init__(self) -> None:
          self.data: dict[str, int] = {}
      def save(self, key: str, ttl: int) -> None:
          self.data[key] = ttl
  ```
- **MUST** keep tests deterministic. No real network, no real filesystem, no real clock. Inject fakes.

## Meta

- **MUST** match an existing convention over introducing a "better" second one. A codebase with two conventions is worse than either alone.
- **MUST** format with `ruff format` and lint with `ruff check`. No style debates — the tool decides.