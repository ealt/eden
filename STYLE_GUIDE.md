# Style Guide

Formatting and naming conventions for EDEN.

Most of this guide applies to **Python code** that will live in `reference/` from Phase 3 onward. Spec prose (`spec/`) has its own conventions noted at the end.

## General

- Indentation: 4 spaces (Python), 2 spaces (JSON, YAML).
- Maximum line length: 120 characters.
- Use trailing commas in multiline structures.
- End files with a single newline.

## Python

- Target version: Python 3.12+.
- Formatter/linter: [Ruff](https://docs.astral.sh/ruff/).
- Type checker: [Pyright](https://github.com/microsoft/pyright), standard mode.

### Naming

| Kind | Convention | Example |
|---|---|---|
| Modules | `snake_case.py` | `task_queue.py` |
| Test files | `test_<area>.py` | `test_task_queue.py` |
| Functions, variables | `snake_case` | `claim_task()` |
| Classes | `PascalCase` | `TaskClaim` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_CLAIM_LEASE_S` |
| Booleans | prefix with `is`, `has`, `should`, `can` | `is_idempotent` |

Related config fields that serve parallel roles use consistent grammatical form — e.g., all imperative verbs: `plan_command`, `implement_command`, `evaluate_command` — not a mix of verb/noun.

### Type annotations

- Explicit types on all public API signatures and dataclass fields.
- Use `from __future__ import annotations` when needed for forward refs.
- Prefer built-in generics (`list[str]`, `dict[str, int]`) over `typing` imports.

### Docstrings

Google style:

```python
def claim_task(store: TaskStore, worker_id: str) -> Task | None:
    """Atomically claim the next ready task for a worker.

    Args:
        store: The task store.
        worker_id: Identity of the claiming worker.

    Returns:
        The claimed task, or None if no tasks are ready.
    """
```

Module-, magic-method-, and `__init__` docstrings are not required.

### Patterns

**Frozen dataclasses for value types**:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class TaskClaim:
    task_id: str
    worker_id: str
    claim_token: str
```

**Explicit error handling at boundaries** (subprocess, network, file I/O):

```python
try:
    result = subprocess.run(cmd, capture_output=True, check=True)
except subprocess.CalledProcessError as e:
    raise ExecutionError(f"Command failed: {e.stderr}") from e
```

**Avoid mutable default arguments**:

```python
# Avoid
def process(items: list[str] = []) -> None: ...

# Prefer
def process(items: list[str] | None = None) -> None:
    items = items or []
```

## Ruff configuration

Lives in `pyproject.toml` once the first Python package lands in Phase 3. Baseline rules to carry over:

- line-length 120, target Python 3.12
- enabled rule sets: `A`, `B`, `D`, `E`, `F`, `I`, `PT`, `SIM`, `UP`
- ignored: `D100`, `D105`, `D107`, `SIM108`
- docstring rules disabled for test files

## Pyright configuration

- `typeCheckingMode = "standard"`
- `reportUnnecessaryTypeIgnoreComment = "warning"`
- `reportMissingTypeStubs = "none"`

## Spec prose (`spec/`)

- Use RFC 2119 normative keywords (MUST, SHOULD, MAY) where behavior is prescriptive. Informative prose uses ordinary English and says so.
- Numbered sections per chapter.
- Every wire format has a corresponding JSON Schema under `spec/v*/schemas/`. The chapter cites the schema file by path.
- No references to specific technologies (Postgres, Redis, FastAPI). The spec describes *semantics*; mechanisms are reference-impl detail.

## Markdown (all `.md` in the repo)

- Atx-style headings (`#`, `##`, `###`).
- Reference-style links are fine when a URL repeats; inline otherwise.
- Code fences specify a language tag when applicable.
- `markdownlint-cli2` is the authoritative linter; [`.markdownlint.json`](.markdownlint.json) holds the exception list.

For commands to run linters, see [AGENTS.md](AGENTS.md#commands).
