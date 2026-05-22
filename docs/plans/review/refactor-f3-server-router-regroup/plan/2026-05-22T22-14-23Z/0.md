# Refactor F-3 — `eden-wire/server.py` APIRouter regroup

**Status.** Draft (plan-stage; awaiting operator approval before impl spawn).

**Tracks issue.** [ealt/eden#115](https://github.com/ealt/eden/issues/115).

**Predecessors.** PR #105 (Code-quality audit Phase A + C) landed
the Tier-1 complexity gate
([`scripts/check-complexity.py`](../../scripts/check-complexity.py))
and the per-file/per-function `# slop-allow:` annotations that
defer F-3 / F-4 / L-D / L-E / L-F to dedicated chunks. This is the
F-3 / L-D / L-F chunk (and, in a final follow-on wave, L-E for
control-plane).

**Sibling chunk.** [ealt/eden#116](https://github.com/ealt/eden/issues/116)
(F-4) is the symmetric per-resource split of
[`eden-wire/client.py`](../../reference/packages/eden-wire/src/eden_wire/client.py).
F-3 and F-4 share the `eden-wire` package but touch disjoint files;
no direct conflict expected. The two should not block each other —
either can land first.

**Naming.** Pre-draft check against [`docs/glossary.md`](../glossary.md)
and AGENTS.md "Naming discipline":

- The refactor introduces no new EDEN-domain identifiers. Module
  names use FastAPI's upstream vocabulary (`routers`, `APIRouter`,
  `include_router`, `Depends`) plus the existing resource nouns
  (tasks, ideas, variants, events, workers, groups, dispatch_mode,
  experiment, checkpoints, reference).
- The `RouterDeps` dataclass introduced in §3.2 is a wire-binding
  internal carrier (not exported through `eden_wire.__init__`); the
  name does not collide with any spec or domain identifier.
- No route paths, request/response body fields, error types, or
  status codes change — F-3 is structural-internal only.

## 1. Context

### 1.1 What F-3 is

[`reference/packages/eden-wire/src/eden_wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py)
is the FastAPI app factory that exposes a `Store` over the
chapter-7 wire binding. At PR #105 the file carries:

- **1832 lines total.**
- A single 1378-line `make_app(...)` closure (lines 327 → 1706)
  that nests **43 route handlers**, **9 exception handlers**, and
  **5 helper closures** (`_enforce_worker`, `_enforce_in_any_group`,
  `_stamp_created_by`, `_problem`, `_check_experiment`) — 57
  callables captured over `store`, `admin_token`,
  `subscribe_timeout`, `subscribe_poll_interval`, `artifact_root`,
  `checkpoint_repo_root`, and `checkpoint_config_text`.
- Two `# slop-allow:` annotations marking the deferral:
  - `# slop-allow-file: F-3 eden-wire/server.py APIRouter regroup deferred to issue #115` (line 1)
  - `# slop-allow: L-D resolved by F-3 — deferred to issue #115` (line 327, on `def make_app`)
- A separate L-F entry (`_serve_artifact`, 117 LOC, CC=14) flagged
  by the audit as "covered by F-3 — moves to its own module".

The audit disposition
([`docs/audits/2026-05-20-phase-c-disposition.md`](../audits/2026-05-20-phase-c-disposition.md)
§F-3) proposes per-resource `APIRouter` extraction with
`make_app` reduced to a ~50-line assembler. F-3 is the chunk
that delivers that.

### 1.2 What L-D, L-F, L-E are (and why this chunk addresses them)

The Phase-A/C audit measured five entries that F-3 either resolves
or symmetrically un-blocks:

| ID | File:fn | LEN | CC | Disposition |
|---|---|---:|---:|---|
| F-3 | `eden-wire/server.py` (file) | 1211 | 38.53 (MI) | This chunk — APIRouter regroup |
| L-D | `eden-wire/server.py:327 make_app` | 1378 | 5 | Resolved by F-3 |
| L-F | `eden-wire/server.py:1664 _serve_artifact` | 117 | 14 | Moves to `routers/reference.py` (or `routers/artifacts.py`) under F-3 |
| L-E | `control-plane/app.py:121 make_app` | 415 | 2 | Symmetric APIRouter regroup; smaller surface (8 handlers); included as a follow-on wave (§7 wave 6) |

L-D + L-F + the file-level F-3 annotation all live inside
`eden-wire/server.py`; once the regroup completes, those three
`# slop-allow:` annotations are removed and the
[`scripts/check-complexity.py`](../../scripts/check-complexity.py)
gate runs clean without entries for them. L-E is logically
separate (different file, different service) but is the same
shape; bundling it into this plan keeps the wire-binding refactor
coherent and avoids leaving a half-symmetric codebase. The
operator may split L-E into its own follow-on PR if blast radius
matters; default recommendation is to land it as wave 6 of this
chunk.

### 1.3 Why now

Per AGENTS.md "Slop prevention", the audit's `# slop-allow:`
annotations are an explicit IOU. The F-3-deferred annotations
carry a cite back to issue #115; the issue is the contract for
resolving them. Delaying further risks:

- **Annotation drift.** Each new route added to `make_app` (e.g.,
  a hypothetical 12c control-plane reach-through or a future
  worker-affinity endpoint) makes the eventual split harder
  because the new route's auth-dispatch threading has to be
  re-derived. The audit predicts post-refactor `server.py` ≤ 120
  SLOC and per-router 80–200 SLOC; every additional in-place
  route delays that target.
- **Slop-prevention discipline erosion.** AGENTS.md §Slop
  prevention warns: "The annotation is per-function or per-file,
  not module-wide; it has to be re-justified on any subsequent
  change that materially extends the violator." A long-lived
  `# slop-allow:` with a deferred resolution becomes a license to
  grow the violator further. Resolving F-3 reinforces that the
  audit's IOUs are honored.
- **Naming-symmetry with F-4.** F-4 (client.py) is plan-stage in
  parallel. A landed F-3 sets the structural pattern (per-resource
  router modules) that F-4's per-resource client modules can
  mirror name-for-name, simplifying cross-file navigation.

### 1.4 Behavior preservation contract

F-3 is **purely structural**. The chunk MUST NOT change:

- Any HTTP route path, method, query-parameter shape, header
  contract, request/response body shape, or status code.
- Any error envelope (`eden://error/...` or
  `eden://reference-error/...` URIs, `problem+json` media-type,
  envelope status/title/detail/instance fields).
- The `make_app(...)` signature: `(store, *, subscribe_timeout,
  subscribe_poll_interval, admin_token, artifacts_dir,
  checkpoint_experiment_config, checkpoint_repo_path)`.
- The auth-dispatch semantics: `admin_token is None` keeps auth
  disabled (test posture); non-`None` installs the §13 middleware
  and gates every route on the matching authority table.
- The `chapter-7 §1.3` `X-Eden-Experiment-Id` invariant:
  `_check_experiment(...)` runs on every experiment-scoped route
  before any store access.
- Conformance: [`uv run pytest -q conformance/`](../../conformance/)
  is the contract enforcer. Every conformance scenario MUST pass
  pre- and post-refactor.

The contract is "the same wire surface, same behavior, different
internal organization." A passing conformance suite is the
necessary-and-sufficient acceptance gate for the wire surface; the
existing eden-wire tests + the task-store-server + control-plane +
orchestrator e2e tests cover the implementation-side seams.

## 2. Decisions

### Decision 1 — Per-resource router modules under `routers/`

**Decision.** Introduce
`reference/packages/eden-wire/src/eden_wire/routers/` as the home
for per-resource `APIRouter` modules. Each module exports a
`def build_router(deps: RouterDeps) -> APIRouter` factory. `make_app`
constructs the `RouterDeps` once and calls each `build_router(deps)`
in order, then `app.include_router(...)` each.

**Why over alternatives.**

- **Alternative A: keep one file but extract per-resource sections
  into module-private helpers.** Rejected — does not lift the
  file-SLOC violation (L-D / file-level F-3 annotation stays).
  Audit threshold is file-level; this would be a half-refactor
  that still needs `# slop-allow-file`.
- **Alternative B: per-route modules** (`routers/create_task.py`,
  `routers/list_tasks.py`, etc.). Rejected — 43 files of ~30 SLOC
  each is fragmentation, not cohesion. Audit's predicted shape
  ("each router 80–200 SLOC") matches per-resource grouping.
- **Alternative C: per-section package** (one Python package per
  resource with `routes.py` + `models.py` + `service.py`). Rejected
  — the existing wire-binding has no service-layer separation
  (handlers are thin adapters by design per the module docstring);
  introducing one is feature-creep beyond F-3's scope.

### Decision 2 — Dependency threading via a `RouterDeps` dataclass

**Decision.** `RouterDeps` is a frozen dataclass capturing every
value the current closure captures from `make_app`'s arguments:

```python
@dataclass(frozen=True)
class RouterDeps:
    store: Store
    admin_token: str | None
    subscribe_timeout: float
    subscribe_poll_interval: float
    artifact_root: Path | None
    checkpoint_repo_root: Path | None
    checkpoint_config_text: str
```

Each `routers/<name>.py` accepts `deps: RouterDeps` in `build_router`
and binds its handlers as closures over `deps`. Mirror semantics
of the current `make_app` closure capture.

**Why over alternatives.**

- **Alternative A: `app.state.deps` + FastAPI `Depends(get_deps)`**
  per the audit disposition's suggestion. Rejected for the F-3
  baseline because it introduces a request-time dependency-lookup
  layer that doesn't exist today, changes the inferred function
  signatures of every handler (each gains a `deps: RouterDeps =
  Depends(get_deps)` parameter), and surface-area for typo-style
  bugs grows (forgetting `Depends(...)` on a new handler silently
  falls through to a positional-arg interpretation). The `RouterDeps`
  bundle preserves the existing closure-capture semantics —
  invariant by construction. A future amendment MAY switch to
  `Depends(...)` if needed; this is internal-only.
- **Alternative B: module-level state set by `make_app`**
  (`routers.tasks._deps = deps` at app-build time). Rejected
  outright — breaks the existing multi-app posture (
  `make_app(store_a)` + `make_app(store_b)` in one process), which
  the module docstring explicitly guarantees: "multiple apps for
  different experiments can coexist in one process."
- **Alternative C: pass each individual value as kwargs to
  `build_router(...)`.** Rejected — 5–7 kwargs per call site is
  the same surface area as `RouterDeps` but without the type-name
  documentation benefit. `RouterDeps` is also extensible (adding
  a new dep is one field addition, not a 10-callsite signature
  change).

### Decision 3 — Module-level helpers move to a co-located file

**Decision.**

- `_enforce_worker`, `_enforce_in_any_group`, `_stamp_created_by`,
  `_check_experiment` — currently closures over `store` +
  `admin_token` in `make_app`. Lift to module-level functions
  in `eden_wire/_dependencies.py` taking `(deps: RouterDeps,
  request: Request, ...)`. Each router module imports them. (The
  leading underscore preserves the private-API posture.)
- `_problem` — small JSON envelope helper, currently a closure;
  lift to module-level in `eden_wire/_dependencies.py`.
- `_worker_id_from_request` — already module-level; stays as-is.
- `_submission_to_wire` / `_submission_from_wire` — already
  module-level; co-locate in `routers/tasks.py` (their only
  caller) or keep at `eden_wire/_submissions.py` if shared. Spot
  check shows only the tasks router uses them; recommend
  co-location in `routers/tasks.py`.
- Artifact helpers (`_open_artifact_fd`, `_check_not_symlink`,
  `_is_symlink`, `_build_content_disposition`,
  `_open_and_read_artifact`, `_artifact_response_headers`,
  `_DIR_FLAGS`, `_FILE_FLAGS`, `_REJECT_PATH_COMPONENTS`,
  `_SymlinkRejected`, `MAX_ARTIFACT_BYTES`) — currently
  module-level above `make_app`. Move into a new
  `eden_wire/_artifact_fd.py` so `routers/reference.py` (or
  `routers/artifacts.py`) can import them. Keeps the descriptor-
  walk security primitives in one auditable file.

**Why.** Co-location matches usage — `_open_artifact_fd` and friends
are only called from one place (`_serve_artifact`); the artifact
router is the natural home. The auth helpers are shared across
~all routers and need a shared module. Both moves preserve the
existing module-private naming convention.

### Decision 4 — Exception handlers stay on the app, not the router

**Decision.** All 9 `@app.exception_handler(...)` registrations
(StorageError, BadRequest, ExperimentIdMismatch, Unauthorized,
Forbidden, WireReferenceError, RequestValidationError,
ValidationError, CheckpointError) stay in `make_app` and apply
app-wide. They do NOT move into router modules.

**Why.** FastAPI's `add_exception_handler` is an app-level API —
router-level exception handlers don't exist in the framework. Any
exception raised by any router's handler bubbles up to the app's
handler chain. This preserves the current uniform-envelope
behavior: a `StorageError` raised in `routers/tasks.py` produces
the same `problem+json` body as one raised in `routers/groups.py`.

The handler closures over `instance=str(request.url)` are stateless
beyond the exception object — no `store` capture — so they remain
the same shape they have today.

### Decision 5 — Route ordering preserved at `include_router` time

**Decision.** `make_app` calls `app.include_router(...)` in the
exact registration order the current monolith uses:

1. tasks
2. ideas
3. variants
4. dispatch_mode
5. experiment lifecycle (terminate / policy-errors / state /
   `GET /v0/experiments/{id}`)
6. events
7. workers
8. groups
9. checkpoints (`POST {base}/checkpoint` and
   `POST /v0/checkpoints/import`)
10. reference (`GET {ref_base}/tasks/{id}/validate-terminal`,
    `POST {ref_base}/validate/evaluation`,
    `GET {ref_base}/artifacts/{path:path}`)

**Why.** FastAPI matches routes in registration order. The current
ordering places `GET /v0/experiments/{experiment_id}` (the
"read full experiment" route at line 1425) AFTER the more-specific
`/v0/experiments/{id}/tasks` / `/ideas` / etc. routes — this is
load-bearing for path-parameter resolution. The plan preserves the
ordering exactly so behavior is byte-identical to pre-refactor;
the wave-1 proof-of-shape test will assert it (see §6.2).

**Edge case.** `POST /v0/checkpoints/import` (line 1515) is the
ONE route NOT under the `/v0/experiments/{id}` base — it's a
top-level admin endpoint. The checkpoints router (`routers/
checkpoints.py`) registers TWO prefixes:

- `POST /v0/experiments/{experiment_id}/checkpoint` (export — under
  the per-experiment base)
- `POST /v0/checkpoints/import` (import — top-level)

The router does not use `APIRouter(prefix=...)` because the two
endpoints have different prefixes; instead it declares each route
with its full path. Alternatively, two routers
(`checkpoints_per_experiment.py` and `checkpoints_import.py`) —
call this out as a tradeoff (§7.3).

### Decision 6 — Test files stay as-is for F-3 wave 1–5

**Decision.** The eden-wire tests in
[`reference/packages/eden-wire/tests/`](../../reference/packages/eden-wire/tests/)
are already partially split by topic (artifacts, auth, checkpoints,
groups, lifecycle, reassign+dispatch, workers, roundtrip, schema-
parity). They invoke `make_app(store)` at the HTTP level — they are
behavioral, not structural. They MUST pass unchanged through every
wave of F-3.

**Why no split.**

- The tests are already organized by resource: `test_groups_wire.py`,
  `test_workers_wire.py`, `test_lifecycle_wire.py`, etc. Further
  split would mostly be moving content out of `test_wire_roundtrip.py`
  (549 LOC, covers tasks/ideas/variants/events/dispatch).
- Renaming basenames triggers AGENTS.md "Adding a new service or
  package with its own `tests/`" — basenames must be unique across
  `testpaths`. The current names are fine; splitting introduces
  new basenames that must each be checked.
- A behavior-level test that passes through a refactor is the
  strongest validation of "no semantic change." Splitting tests
  *during* the refactor would obscure that signal.

**Follow-up (out of scope).** If post-F-3 the team wants per-router
test alignment (move tasks tests from `test_wire_roundtrip.py` to
a new `test_tasks_wire.py`), that's a clean follow-up PR with no
F-3 dependency. The plan does not block on it.

### Decision 7 — `make_app` post-refactor target shape

**Decision.** Post-F-3 `make_app` is ~80–120 LOC: docstring,
`RouterDeps` construction, optional `install_auth_middleware`,
9 exception-handler registrations, 10 `app.include_router(...)`
calls, `return app`. The two `# slop-allow:` annotations (line 1
file-level, line 327 function-level L-D) are removed.

**Why.** The Tier-1 complexity gate
([`scripts/check-complexity.py`](../../scripts/check-complexity.py))
thresholds are:

- function length > 100 → fail
- file SLOC > 800 → fail

Audit predicts `server.py` → ~120 SLOC + each router 80–200 SLOC.
The target is well below both gates; no new `# slop-allow:`
annotations should be needed.

If, post-refactor, `make_app` is between 100 and 200 LOC (still
under the file-SLOC threshold but over the function-LEN one), the
fix is to extract small helpers (e.g., `_install_exception_handlers(app, deps)`,
`_register_routers(app, deps)`) until under 100. The plan budgets
one wave (wave 5) for this polish; explicitly NOT a new
`# slop-allow:`.

## 3. Design

### 3.1 Target file layout

```text
reference/packages/eden-wire/src/eden_wire/
├── __init__.py              # public surface — unchanged exports
├── auth.py                  # unchanged
├── client.py                # unchanged (F-4 territory)
├── errors.py                # unchanged
├── models.py                # unchanged
├── server.py                # ~80–120 LOC after refactor (the assembler)
├── _dependencies.py         # NEW — RouterDeps + auth/check helpers
├── _artifact_fd.py          # NEW — descriptor-walk artifact helpers
└── routers/                 # NEW
    ├── __init__.py          # empty (or re-exports the build_router fns)
    ├── tasks.py             # 10 routes; ~250 LOC
    ├── ideas.py             # 4 routes; ~80 LOC
    ├── variants.py          # 5 routes; ~120 LOC
    ├── dispatch_mode.py     # 2 routes; ~110 LOC (the GET/PATCH pair w/ value-grammar walk)
    ├── experiment.py        # 4 routes (terminate / policy-errors / state / read-experiment); ~130 LOC
    ├── events.py            # 2 routes; ~60 LOC
    ├── workers.py           # 5 routes; ~140 LOC
    ├── groups.py            # 6 routes; ~150 LOC
    ├── checkpoints.py       # 2 routes (export + import); ~150 LOC
    └── reference.py         # 3 routes (validate-terminal + validate/evaluation + artifacts); ~180 LOC
```

Total estimated SLOC (rough): `80 + 50 + 80 + 250 + 80 + 120 +
110 + 130 + 60 + 140 + 150 + 150 + 180 + 100 ≈ 1680` SLOC across
14 files vs **1832 LOC in 1 file** today. The slight net reduction
comes from dropping inlined `_check_experiment` URL strings in
favor of `request.url` (cheap and equivalent — see Decision 4).
The dominant change is distribution: **no file exceeds the 800
SLOC threshold; no function exceeds the 100 LOC threshold.**

### 3.2 `RouterDeps` shape + lifecycle

```python
# eden_wire/_dependencies.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eden_storage import Store
from fastapi import Request

from .auth import require_admin, require_worker
from .errors import BadRequest, Forbidden


@dataclass(frozen=True)
class RouterDeps:
    """Per-`make_app` bundle of dependencies threaded into each router.

    Mirrors the closure-capture set of the pre-F-3 monolithic
    ``make_app``. Frozen so accidental mutation surfaces as
    ``dataclasses.FrozenInstanceError`` rather than silent
    cross-router state drift.
    """
    store: Store
    admin_token: str | None
    subscribe_timeout: float
    subscribe_poll_interval: float
    artifact_root: Path | None
    checkpoint_repo_root: Path | None
    checkpoint_config_text: str


def check_experiment(deps: RouterDeps, path_exp: str,
                     header_exp: str | None) -> None:
    # ... (current _check_experiment body, with deps.store.experiment_id)


def enforce_worker(deps: RouterDeps, request: Request) -> None:
    # ... (current _enforce_worker body)


def enforce_in_any_group(deps: RouterDeps, request: Request,
                         group_ids: tuple[str, ...]) -> str:
    # ... (current _enforce_in_any_group body, using deps.store)


def stamp_created_by(deps: RouterDeps, request: Request,
                     body: dict[str, Any],
                     field: str = "created_by") -> dict[str, Any]:
    # ... (current _stamp_created_by body)
```

The helpers take `deps: RouterDeps` explicitly. No `request.state`
back-channel beyond what the middleware already uses.

### 3.3 Router skeleton (per-module)

Every `routers/<name>.py` follows the same shape so reviewers can
read one and infer the others:

```python
# eden_wire/routers/tasks.py
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Body, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from eden_contracts import TaskAdapter
from eden_storage.submissions import (
    Submission, submission_from_payload, submission_to_payload,
)

from .._dependencies import (
    RouterDeps, check_experiment, enforce_in_any_group, enforce_worker,
    stamp_created_by,
)
from ..errors import BadRequest
from ..models import ClaimRequest, ReassignRequest, ReclaimRequest, RejectRequest, SubmitRequest


def build_router(deps: RouterDeps) -> APIRouter:
    """Return the tasks APIRouter bound to ``deps``.

    Mirrors the chapter-7 §2 task-lifecycle endpoints. All routes
    are experiment-scoped under ``/v0/experiments/{experiment_id}/tasks``.
    """
    router = APIRouter(prefix="/v0/experiments/{experiment_id}/tasks")

    @router.post("")
    async def create_task(
        request: Request,
        experiment_id: str,
        body: dict[str, Any] = Body(...),
        x_eden_experiment_id: str | None = Header(None),
    ) -> dict[str, Any]:
        check_experiment(deps, experiment_id, x_eden_experiment_id)
        # ... (current _create_task body, with deps.store)
        # ...

    @router.get("")
    async def list_tasks(...):
        ...

    # ... rest of the task routes

    return router


def _submission_to_wire(submission: Submission) -> dict[str, Any]:
    kind, payload = submission_to_payload(submission)
    return {"kind": kind, **payload}


def _submission_from_wire(kind: str, payload: dict[str, Any]) -> Submission:
    try:
        return submission_from_payload(kind, payload)
    except ValueError as exc:
        # Module-level mapping preserved.
        raise BadRequest(str(exc)) from exc
```

Notes:

- `APIRouter(prefix=...)` carries the experiment-scoped path
  prefix, so each route declares only the suffix. Routes that
  share the prefix (tasks, ideas, variants, events, workers,
  groups, dispatch_mode, experiment-lifecycle) follow this shape.
- Per-resource module-level helpers (`_submission_to_wire`,
  `_submission_from_wire`) co-locate with the router that uses
  them.
- The `_check_experiment` helper drops the third `url` parameter
  (it was the path string for error envelope construction);
  inside the new design the URL comes from
  `request.url` at exception-handler time. This is a cosmetic
  simplification — the error envelope's `instance` field already
  carries `str(request.url)` via the existing exception handlers;
  the inline `url` was unused by the current
  `ExperimentIdMismatch` exception path. Verified by inspection
  during wave 1. (Surface in the wave-1 PR description so codex
  flags it explicitly.)

### 3.4 Checkpoints router with two prefixes

`routers/checkpoints.py` is the only router that crosses the
`{experiment_id}` boundary. Two shapes are defensible:

```python
# Option A — one router, full paths declared per route:
router = APIRouter()

@router.post("/v0/experiments/{experiment_id}/checkpoint")
async def export_checkpoint(...): ...

@router.post("/v0/checkpoints/import")
async def import_checkpoint(...): ...
```

```python
# Option B — two routers in one module:
def build_router(deps) -> tuple[APIRouter, APIRouter]:
    per_experiment = APIRouter(prefix="/v0/experiments/{experiment_id}")
    import_router = APIRouter(prefix="/v0/checkpoints")
    ...
    return per_experiment, import_router
```

Recommend **Option A**. It keeps the module signature uniform
(`build_router(deps) -> APIRouter`), the two routes are
intrinsically related (same admin-gated checkpoint substrate), and
`make_app` calls one `include_router` rather than two. The
extra-explicit per-route paths are 2 lines in a small module — not
a maintainability concern.

### 3.5 Reference router with artifact-serving helpers

`routers/reference.py` holds the three `/_reference/` routes:

- `GET /_reference/experiments/{id}/tasks/{task_id}/validate-terminal`
- `POST /_reference/experiments/{id}/validate/evaluation`
- `GET /_reference/experiments/{id}/artifacts/{path:path}`

The artifact route imports its filesystem helpers from
`eden_wire/_artifact_fd.py` (Decision 3). The self-auth check
(the middleware skips `/_reference/` paths, so the handler calls
`authenticate(...)` directly) stays in the route handler with the
same posture.

```python
router = APIRouter(prefix="/_reference/experiments/{experiment_id}")

@router.get("/tasks/{task_id}/validate-terminal")
async def validate_terminal(...): ...

@router.post("/validate/evaluation")
async def validate_evaluation(...): ...

@router.get("/artifacts/{path:path}")
async def serve_artifact(...):
    # current _serve_artifact body, importing from _artifact_fd
    ...
```

### 3.6 `make_app` post-refactor shape

```python
# eden_wire/server.py — post-refactor
def make_app(
    store: Store,
    *,
    subscribe_timeout: float = 30.0,
    subscribe_poll_interval: float = 0.1,
    admin_token: str | None = None,
    artifacts_dir: Path | str | None = None,
    checkpoint_experiment_config: str | None = None,
    checkpoint_repo_path: Path | str | None = None,
) -> FastAPI:
    """Build a FastAPI app that exposes ``store`` over the wire binding.

    ... (docstring preserved verbatim, with one updated paragraph
    noting the per-resource router decomposition)
    """
    app = FastAPI(
        title=f"EDEN task store — {store.experiment_id}",
        version="0",
    )

    deps = RouterDeps(
        store=store,
        admin_token=admin_token,
        subscribe_timeout=subscribe_timeout,
        subscribe_poll_interval=subscribe_poll_interval,
        artifact_root=Path(artifacts_dir) if artifacts_dir is not None else None,
        checkpoint_repo_root=Path(checkpoint_repo_path) if checkpoint_repo_path is not None else None,
        checkpoint_config_text=checkpoint_experiment_config or "",
    )

    if admin_token is not None:
        install_auth_middleware(app, admin_token=admin_token, store=store)

    _install_exception_handlers(app)

    # Order is load-bearing per chapter-7 §1.3 path resolution; see
    # docs/plans/refactor-f3-server-router-regroup.md Decision 5.
    app.include_router(tasks.build_router(deps))
    app.include_router(ideas.build_router(deps))
    app.include_router(variants.build_router(deps))
    app.include_router(dispatch_mode.build_router(deps))
    app.include_router(experiment.build_router(deps))
    app.include_router(events.build_router(deps))
    app.include_router(workers.build_router(deps))
    app.include_router(groups.build_router(deps))
    app.include_router(checkpoints.build_router(deps))
    app.include_router(reference.build_router(deps))

    return app


def _install_exception_handlers(app: FastAPI) -> None:
    """Wire the 9 app-level problem+json exception handlers.

    Extracted to keep ``make_app`` under the 100-LOC threshold
    (audit's L-D resolution). All handlers are stateless beyond
    the request URL.
    """
    @app.exception_handler(StorageError)
    async def _storage_error_handler(...): ...
    # ... 8 more
```

## 4. Resource → router mapping

Complete mapping from current `server.py` line ranges to
post-refactor router modules. Verified by `grep -nE
'@app\.(get|post|patch|put|delete)\('` against
[`reference/packages/eden-wire/src/eden_wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py)
at HEAD `f3f7932`.

| Current line | Method | Path | New module |
|---:|---|---|---|
| 605 | POST | `{base}/tasks` | `routers/tasks.py` |
| 646 | GET | `{base}/tasks` | `routers/tasks.py` |
| 661 | GET | `{base}/tasks/{task_id}` | `routers/tasks.py` |
| 673 | GET | `{base}/tasks/{task_id}/submission` | `routers/tasks.py` |
| 689 | POST | `{base}/tasks/{task_id}/claim` | `routers/tasks.py` |
| 717 | POST | `{base}/tasks/{task_id}/submit` | `routers/tasks.py` |
| 742 | POST | `{base}/tasks/{task_id}/accept` | `routers/tasks.py` |
| 759 | POST | `{base}/tasks/{task_id}/reject` | `routers/tasks.py` |
| 777 | POST | `{base}/tasks/{task_id}/reclaim` | `routers/tasks.py` |
| 794 | POST | `{base}/tasks/{task_id}/reassign` | `routers/tasks.py` |
| 826 | POST | `{base}/ideas` | `routers/ideas.py` |
| 849 | GET | `{base}/ideas` | `routers/ideas.py` |
| 861 | GET | `{base}/ideas/{idea_id}` | `routers/ideas.py` |
| 874 | POST | `{base}/ideas/{idea_id}/mark-ready` | `routers/ideas.py` |
| 894 | POST | `{base}/variants` | `routers/variants.py` |
| 915 | GET | `{base}/variants` | `routers/variants.py` |
| 929 | GET | `{base}/variants/{variant_id}` | `routers/variants.py` |
| 942 | POST | `{base}/variants/{variant_id}/declare-evaluation-error` | `routers/variants.py` |
| 958 | POST | `{base}/variants/{variant_id}/integrate` | `routers/variants.py` |
| 984 | GET | `{base}/dispatch_mode` | `routers/dispatch_mode.py` |
| 1007 | PATCH | `{base}/dispatch_mode` | `routers/dispatch_mode.py` |
| 1071 | POST | `{base}/terminate` | `routers/experiment.py` |
| 1098 | POST | `{base}/policy-errors` | `routers/experiment.py` |
| 1140 | GET | `{base}/state` | `routers/experiment.py` |
| 1425 | GET | `{base}` (read full experiment) | `routers/experiment.py` |
| 1168 | GET | `{base}/events` | `routers/events.py` |
| 1181 | GET | `{base}/events/subscribe` | `routers/events.py` |
| 1213 | POST | `{base}/workers` | `routers/workers.py` |
| 1240 | GET | `{base}/workers` | `routers/workers.py` |
| 1260 | GET | `{base}/workers/{worker_id}` | `routers/workers.py` |
| 1274 | POST | `{base}/workers/{worker_id}/reissue-credential` | `routers/workers.py` |
| 1294 | GET | `{base}/whoami` | `routers/workers.py` |
| 1319 | POST | `{base}/groups` | `routers/groups.py` |
| 1339 | GET | `{base}/groups` | `routers/groups.py` |
| 1354 | GET | `{base}/groups/{group_id}` | `routers/groups.py` |
| 1368 | POST | `{base}/groups/{group_id}/members` | `routers/groups.py` |
| 1386 | DELETE | `{base}/groups/{group_id}/members/{member_id}` | `routers/groups.py` |
| 1404 | DELETE | `{base}/groups/{group_id}` | `routers/groups.py` |
| 1455 | POST | `{base}/checkpoint` | `routers/checkpoints.py` |
| 1515 | POST | `/v0/checkpoints/import` (top-level) | `routers/checkpoints.py` |
| 1624 | GET | `{ref_base}/tasks/{task_id}/validate-terminal` | `routers/reference.py` |
| 1640 | POST | `{ref_base}/validate/evaluation` | `routers/reference.py` |
| 1666 | GET | `{ref_base}/artifacts/{path:path}` | `routers/reference.py` |

**Total: 43 routes mapped.** No route is unmapped; no route maps
to two routers; the include-order matches the current registration
order (Decision 5).

Exception handlers (lines 505, 514, 523, 534, 545, 556, 575, 587,
1590) all stay in `make_app` per Decision 4. Helper closures
(lines 388 `_enforce_worker`, 401 `_enforce_in_any_group`, 437
`_stamp_created_by`, 476 `_problem`, 489 `_check_experiment`) move
to `_dependencies.py` per Decision 3.

## 5. Files to touch

### 5.1 New files

| File | Change |
|---|---|
| `reference/packages/eden-wire/src/eden_wire/_dependencies.py` (new) | `RouterDeps` dataclass + module-level `check_experiment` / `enforce_worker` / `enforce_in_any_group` / `stamp_created_by` / `problem_response` helpers (Decision 2 + 3). |
| `reference/packages/eden-wire/src/eden_wire/_artifact_fd.py` (new) | Move `_open_artifact_fd`, `_check_not_symlink`, `_is_symlink`, `_build_content_disposition`, `_open_and_read_artifact`, `_artifact_response_headers`, `_SymlinkRejected`, `_DIR_FLAGS`, `_FILE_FLAGS`, `_REJECT_PATH_COMPONENTS`, `MAX_ARTIFACT_BYTES` (Decision 3). |
| `reference/packages/eden-wire/src/eden_wire/routers/__init__.py` (new) | Empty (package marker). |
| `reference/packages/eden-wire/src/eden_wire/routers/tasks.py` (new) | 10 task-lifecycle routes + `_submission_to_wire` / `_submission_from_wire` helpers. |
| `reference/packages/eden-wire/src/eden_wire/routers/ideas.py` (new) | 4 idea routes. |
| `reference/packages/eden-wire/src/eden_wire/routers/variants.py` (new) | 5 variant routes. |
| `reference/packages/eden-wire/src/eden_wire/routers/dispatch_mode.py` (new) | 2 dispatch_mode routes (GET + PATCH); preserves the `model_extra` walk per chapter-7 §2.8 (see §7.2 below). |
| `reference/packages/eden-wire/src/eden_wire/routers/experiment.py` (new) | 4 experiment-lifecycle routes (terminate / policy-errors / state / read-full-experiment). |
| `reference/packages/eden-wire/src/eden_wire/routers/events.py` (new) | 2 events routes (read-range + subscribe long-poll). |
| `reference/packages/eden-wire/src/eden_wire/routers/workers.py` (new) | 5 worker-registry routes (incl. whoami). |
| `reference/packages/eden-wire/src/eden_wire/routers/groups.py` (new) | 6 group-registry routes. |
| `reference/packages/eden-wire/src/eden_wire/routers/checkpoints.py` (new) | 2 checkpoint routes (export per-experiment + top-level import). |
| `reference/packages/eden-wire/src/eden_wire/routers/reference.py` (new) | 3 `/_reference/` routes (validate-terminal + validate/evaluation + artifact serving). Imports artifact helpers from `_artifact_fd.py`. |

### 5.2 Modified files

| File | Change |
|---|---|
| `reference/packages/eden-wire/src/eden_wire/server.py` | Reduced to ~80–120 LOC: `make_app` + `_install_exception_handlers` helper. Drops the `# slop-allow-file` (line 1) and `# slop-allow` on `make_app` (line 327). |
| `reference/packages/eden-wire/src/eden_wire/__init__.py` | No change to public surface (`make_app` still exported). |
| `reference/packages/control-plane/src/eden_control_plane_server/app.py` | **L-E (wave 6 only)** — symmetric APIRouter regroup with 3 routers (experiments / events / control). Drops the file's `# slop-allow` annotation if any (verify by inspection — L-E was not listed as carrying one in the disposition; if not, no annotation to drop). |

### 5.3 Verification commands

| File | Change |
|---|---|
| `scripts/check-complexity.py` | No code change. Post-refactor, the script's `--list` output should be one or two entries shorter (L-D and the file-level F-3 entries removed). Run `python3 scripts/check-complexity.py` at the end of every wave to confirm no new violators. |

### 5.4 Test files

| File | Change |
|---|---|
| `reference/packages/eden-wire/tests/*.py` | **No change** per Decision 6. Each wave runs the full eden-wire test suite to assert behavior preservation. |
| `reference/services/task-store-server/tests/test_artifacts_cli.py` | **No change** — invokes `make_app(...)` at the HTTP level; signature-compatible. |
| `reference/services/control-plane/tests/test_server.py` | **No change** for waves 1–5. Wave 6 (L-E) extends the same tests against the regrouped control-plane app; tests stay behavioral. |
| `conformance/scenarios/*.py` | **No change** — black-box conformance suite is the contract enforcer. |

### 5.5 Docs

| File | Change |
|---|---|
| `CHANGELOG.md` | Per-wave entries under `[Unreleased]` (chunked refactor; each wave's PR adds a one-liner). On final-wave merge, the entries consolidate into a dated chunk heading per AGENTS.md "Recording chunk completions." |
| `docs/roadmap.md` | This chunk is planless w.r.t. the phased roadmap (it's an audit-deferred refactor). Per AGENTS.md "Recording chunk completions" → planless chunks point at the merged PR; the roadmap one-liner lives in the "Code-quality audit follow-ups" section if one exists, else under a new heading. (Operator decision; flag explicitly in the wave-7 docs PR.) |
| `docs/audits/2026-05-20-phase-c-disposition.md` | Update §F-3 / L-D / L-F entries with the merged-PR refs and a "**resolved YYYY-MM-DD**" annotation. Wave-7 docs PR. |

## 6. Test design

### 6.1 Behavioral preservation — the load-bearing gate

Every wave runs the full set of pre-existing tests. Acceptance =
all green.

| Command | Scope |
|---|---|
| `uv run pytest -q reference/packages/eden-wire/tests` | 9 eden-wire test files (~4800 LOC). Covers tasks/ideas/variants/events/workers/groups/dispatch/lifecycle/checkpoints/auth/artifacts/schema-parity. |
| `uv run pytest -q reference/services/task-store-server/tests` | Service-level tests that invoke `make_app` via CLI. |
| `uv run pytest -q reference/services/control-plane/tests` | Only relevant at wave 6 (L-E); waves 1–5 leave control-plane untouched. |
| `uv run pytest -q reference/services/orchestrator/tests` | e2e tests in `test_e2e.py` / `test_subprocess_e2e.py` spawn the wire server; covers the full request/response loop. |
| `uv run pytest -q conformance/` | Black-box conformance against the reference IUT. **The strongest acceptance signal.** Every chapter-7 §1–§14 MUST is exercised through HTTP. |

### 6.2 Targeted regression tests added during the refactor

Most behavior is already test-covered. The plan adds a small
number of targeted tests to lock in subtle invariants that the
existing tests don't exercise tightly:

| File | Test | Why |
|---|---|---|
| `reference/packages/eden-wire/tests/test_wire_roundtrip.py` (or `test_router_ordering.py` new) | `test_get_experiment_route_does_not_shadow_sub_routes`: `GET /v0/experiments/{id}/tasks` returns the tasks list, NOT the experiment object; `GET /v0/experiments/{id}` returns the experiment object. | Decision 5 says ordering is load-bearing; a regression in `app.include_router(...)` order could silently flip this. The current test suite doesn't have a single test that asserts both paths against the same store and checks they return different payloads. |
| `reference/packages/eden-wire/tests/test_wire_roundtrip.py` | `test_multi_app_isolation`: two `make_app(store_a)` + `make_app(store_b)` instances in one process, each receives only its own experiment's events. | Per module docstring contract — Decision 2 alternative B was rejected to preserve this. Worth a regression test to ensure no per-router module-level state slips in. |
| `reference/packages/eden-wire/tests/test_reassign_dispatch_wire.py` | `test_dispatch_mode_patch_rejects_extra_allow_null`: PATCH with `{"future_key": null}` body returns 400 (the `model_extra` null-collapse bug from chapter-7 §2.8 prose). | The walk over `model_extra` keys in `dispatch_mode.py` is the trickiest piece of logic in any router; if a refactor accidentally drops the `null`-pre-exclude_none walk, this surfaces it. The current test_reassign_dispatch_wire.py covers `auto`/`manual` valid values; the null edge is the regression-risk surface. |
| `reference/packages/eden-wire/tests/test_artifact_route.py` | (existing tests) | Already comprehensive — symlink, ENOENT, ENOTDIR, oversize, percent-encoded filenames, header injection. No changes needed; the move of artifact helpers to `_artifact_fd.py` is locked-in by these passing. |

### 6.3 Static-analysis gates per wave

| Command | Gate |
|---|---|
| `uv run ruff check .` | Lint (config in root `pyproject.toml`). Per AGENTS.md "Commands" canonical list. |
| `uv run pyright` | Type-check. The `RouterDeps` dataclass + `build_router(deps)` signature give pyright more typing surface than the closure-capture form; expect zero new errors. |
| `python3 scripts/check-complexity.py` | Complexity gate. Must pass with NO new `# slop-allow:` annotations after wave 5. |
| `python3 scripts/check-rename-discipline.py` | Rename-discipline gate. F-3 introduces no new identifiers in the EDEN domain — no rename risk. Defense in depth. |
| `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` | Markdown lint (this plan + the wave-7 docs PR). |

### 6.4 Compose smokes (wave-5 + wave-7 only)

The Compose smokes are network-heavy and only meaningful end-to-
end. Run them at the bookends:

| Command | When |
|---|---|
| `bash reference/compose/healthcheck/smoke.sh` | Wave 5 (after the full server.py refactor) + wave 7 (docs PR, final). |
| `bash reference/compose/healthcheck/smoke-subprocess.sh` | Wave 5 + wave 7. |
| `bash reference/compose/healthcheck/e2e.sh` | Wave 7 only (covers the web-UI ideator walkthrough; the wire server is upstream of the Web UI and any regression in route ordering / auth surfacing would manifest here). |

## 7. Tricky areas

### 7.1 Path-parameter resolution order

FastAPI's route matching iterates registration order for routes
that share a path-parameter prefix. The current `make_app`
registers `GET /v0/experiments/{id}` at line 1425 — AFTER all the
more-specific `/v0/experiments/{id}/<sub>` routes (tasks, ideas,
…). The post-F-3 `app.include_router(...)` order in `make_app`
(Decision 5) preserves this by listing `experiment` AFTER all the
sub-resource routers. **Test added in §6.2 locks this in.**

If a future change adds a new sub-resource router and forgets to
add it BEFORE `experiment`, the `GET /v0/experiments/{id}` route
shadows it. The test in §6.2 catches the canonical case (tasks);
new resources need similar coverage when added.

### 7.2 `dispatch_mode` PATCH `model_extra` walk

The current `_update_dispatch_mode` handler (line 1007) walks BOTH
known fields AND `model_extra` BEFORE `exclude_none=True`. This
catches `{"future_key": null}` as a 400 BadRequest (would otherwise
collapse to a vacuous 200 OK). The chapter-7 §2.8 docstring inside
the handler explains why.

When extracting into `routers/dispatch_mode.py`, the temptation is
to simplify by relying on `body.model_dump(exclude_none=True)` once
— this would silently break the null-check. The plan's test
addition (§6.2 `test_dispatch_mode_patch_rejects_extra_allow_null`)
is the regression backstop, but the dispatch_mode wave PR should
keep the explicit `all_keys = {**known_fields, **body.model_extra}`
walk verbatim and call it out in the PR description.

### 7.3 Checkpoints' cross-prefix routes

`POST /v0/checkpoints/import` is the only non-experiment-scoped
endpoint in the wire binding (chapter-7 §1.3 explicitly carves it
out). The router (`routers/checkpoints.py`) handles it alongside
the per-experiment export endpoint. Two shapes considered (§3.4);
Option A (one router, two full paths) chosen for module-signature
uniformity.

The import handler's `X-Eden-Experiment-Id` header is OPTIONAL but
MUST equal the post-rewrite id when present (chapter-7 §1.3
carve-out). The current implementation does this inline; preserve
the inline check — do NOT route through `check_experiment(deps,
...)` because that helper assumes the path-segment is the source
of truth for the experiment_id, which doesn't hold for the import
endpoint.

### 7.4 `_serve_artifact` auth posture vs the middleware

The `install_auth_middleware` function in
[`eden_wire/auth.py`](../../reference/packages/eden-wire/src/eden_wire/auth.py)
skips `/_reference/` paths (the middleware's
`AUTH_BYPASS_PREFIXES`). That means the `serve_artifact` handler
in `routers/reference.py` MUST do its own bearer-auth via
`authenticate(...)` — the middleware will not have set
`request.state.principal`. The current handler does this; the
move into `routers/reference.py` must preserve it byte-for-byte.

`test_artifact_route.py` has comprehensive coverage of:

- 401 on missing/invalid bearer
- 403 on symlink hit
- 503 on `artifacts_dir=None`
- 404 on missing/nonexistent path components
- 413 on oversize

These tests are the gate.

### 7.5 The `_check_experiment` URL parameter drop

The current `_check_experiment(path_exp, header_exp, url)` takes a
third `url` argument that — verified by inspection — is unused.
`ExperimentIdMismatch` exceptions carry their own message; the
`@app.exception_handler(ExperimentIdMismatch)` constructs the
envelope from `request.url` directly. The plan drops the `url`
parameter from the new module-level
`check_experiment(deps, path_exp, header_exp)`.

Verify in wave 1 by `grep -n "url=" reference/packages/eden-wire/src/eden_wire/server.py | grep -i check_exp` (expect zero hits — the third positional is an unused passthrough). If verification surprises (the param IS used in some unobvious way), retain it as a fourth `url: str` kwarg.

### 7.6 The `_problem` helper's narrow surface

The `_problem(status, type_, title, detail, instance)` helper is
called from only TWO places in the current code:
`_request_validation_handler` (line 575) and
`_pydantic_validation_handler` (line 587) — both exception
handlers (Decision 4). Move it to `_dependencies.py` so the
exception handler functions can call it; or inline it in
`_install_exception_handlers`. Recommend `_dependencies.py` for
the small reuse benefit; the choice is cosmetic.

### 7.7 `make_app`'s 9 exception handlers — function-LEN risk

If all 9 handlers stay inline in `make_app`, the post-refactor
function is still ~150 LOC (each handler is 5–10 lines + the
9-arg `RouterDeps` construction). Above the 100-LOC gate. The
solution per §3.6 is `_install_exception_handlers(app)` —
the handlers are stateless (no closure capture beyond the
exception type) so the extraction is mechanical. Wave 5 verifies
post-refactor `make_app` is under 100 LOC; if not, this is the
fix.

### 7.8 Sibling F-4 (client.py) interaction

F-4 ([eden#116](https://github.com/ealt/eden/issues/116)) splits
`eden-wire/client.py`. Both refactors touch the `eden-wire`
package but disjoint files (`server.py` vs `client.py`); no git
conflict expected. The `__init__.py` export surface is unchanged
by both. If F-4 lands first, F-3 reads slightly cleaner (fewer
moving pieces in one chunk); if F-3 lands first, F-4 has the
per-resource server-side pattern to mirror. Either order is fine.

The plan does NOT depend on F-4 in any wave. The wave-7 docs PR
might cross-reference F-4's merged PR if it has landed; otherwise
the cross-ref is a follow-up.

### 7.9 Sibling L-E (control-plane) interaction (wave 6 only)

`reference/services/control-plane/src/eden_control_plane_server/app.py`
is 415 LOC with 8 handlers — proportionally smaller. The refactor
shape is the same: per-resource `routers/` (experiments, events,
control), `RouterDeps` (with control-plane-specific fields like
`lease_duration_seconds`), `_install_exception_handlers`. The
service has its own `tests/test_server.py` which is the
behavioral gate.

L-E is included as wave 6 of this plan to honor the symmetric
intent; if the operator prefers to land L-E as a separate chunk
(its own PR + codex-review cycle), drop wave 6 from this plan and
file a follow-up issue. The default recommendation is to keep
wave 6 because (a) the two regroups share the same shape and a
reviewer reading one can validate the other, and (b) leaving
control-plane half-refactored re-introduces the structural slop the
audit explicitly called out. Surface as an explicit operator
decision in the wave-1 PR description.

## 8. Wave plan

Sequenced for incremental review pressure. Each wave is a separate
PR. Validation gates run per wave; no batch shortcuts.

### Wave 1 — Proof-of-shape (tasks router)

Smallest meaningful slice that proves the `RouterDeps` +
`build_router(deps)` pattern works.

- Add `_dependencies.py` with `RouterDeps` + the four auth/check
  helpers.
- Add `routers/__init__.py` + `routers/tasks.py` with the 10 task
  routes.
- Modify `server.py`: build `RouterDeps`, call
  `app.include_router(tasks.build_router(deps))` for tasks; leave
  every other route inline in `make_app`. The `# slop-allow:`
  annotations stay (still needed — file is still >800 LOC, function
  still >100).
- Tests: `uv run pytest -q reference/packages/eden-wire/tests`,
  `uv run pytest -q reference/services/task-store-server/tests`,
  `uv run pytest -q conformance/`. All green.
- PR title: `Refactor F-3 wave 1 — extract tasks APIRouter (#115)`.

### Wave 2 — ideas + variants + events

- Add `routers/ideas.py`, `routers/variants.py`, `routers/events.py`.
- Strip the corresponding inline route handlers from `make_app`.
- Tests: same as wave 1.
- PR title: `Refactor F-3 wave 2 — extract ideas/variants/events routers (#115)`.

### Wave 3 — workers + groups

- Add `routers/workers.py`, `routers/groups.py`.
- Strip the corresponding inline handlers.
- Tests: same.
- PR title: `Refactor F-3 wave 3 — extract workers/groups routers (#115)`.

### Wave 4 — dispatch_mode + experiment lifecycle

- Add `routers/dispatch_mode.py`, `routers/experiment.py`.
- Strip corresponding inline handlers.
- **Adds** the §6.2 regression tests (`test_get_experiment_route_does_not_shadow_sub_routes`,
  `test_dispatch_mode_patch_rejects_extra_allow_null`,
  `test_multi_app_isolation`) — these are best added when
  `routers/experiment.py` + `routers/dispatch_mode.py` exist.
- Tests: same + new ones.
- PR title: `Refactor F-3 wave 4 — extract dispatch_mode/experiment routers (#115)`.

### Wave 5 — checkpoints + reference + cleanup

- Add `routers/checkpoints.py`, `routers/reference.py`.
- Move artifact helpers into `eden_wire/_artifact_fd.py`.
- `make_app` is now ~80–120 LOC.
- **Remove** the two `# slop-allow:` annotations
  (`server.py:1` file-level F-3, `server.py:327` L-D function-level).
- **Remove** the L-F entry's implicit annotation (none on
  `_serve_artifact` itself — it carried no annotation but was
  audit-flagged; the move resolves it structurally).
- Run `python3 scripts/check-complexity.py` — passes clean (no new
  violators; the F-3/L-D/L-F entries are removed).
- Run all compose smokes: `smoke.sh`, `smoke-subprocess.sh`.
- Tests: same as wave 1–4.
- PR title: `Refactor F-3 wave 5 — extract checkpoints/reference routers + cleanup (#115)`.

### Wave 6 (optional) — L-E control-plane symmetric regroup

- Replicate the same shape for `eden_control_plane_server`:
  `RouterDeps` + per-resource routers + `_install_exception_handlers`.
- Smaller surface (415 LOC, 8 handlers).
- Tests: `uv run pytest -q reference/services/control-plane/tests` + `uv run pytest -q conformance/`.
- PR title: `Refactor L-E — control-plane APIRouter regroup (#115)`.

### Wave 7 — docs + chunk completion

- Append `CHANGELOG.md [Unreleased]` entry consolidating the wave
  PRs.
- Update `docs/audits/2026-05-20-phase-c-disposition.md` F-3 / L-D
  / L-F / L-E entries with merged-PR refs + "**resolved YYYY-MM-DD**".
- Update `docs/roadmap.md` per AGENTS.md "Recording chunk
  completions" planless shape.
- Commit the impl-stage codex-review record at
  `docs/plans/review/refactor-f3-server-router-regroup/impl/<timestamp>/`.
- PR title: `Refactor F-3 wave 7 — docs + chunk completion (#115)`.

## 9. Risks

1. **Auth-context leakage between routers.** The `_enforce_*` /
   `_stamp_created_by` helpers close over `admin_token` + `store`
   in the current monolith. After the refactor they take
   `deps: RouterDeps` — the same `deps` instance threads to every
   router. If a wave's PR accidentally constructs a second `RouterDeps`
   (e.g., one for tasks with `admin_token=None`, another for
   workers with the real token), the workers router silently
   gates and tasks silently bypasses. Mitigation: `RouterDeps` is
   constructed exactly once in `make_app` (the post-refactor
   shape in §3.6 makes this textually obvious); the existing
   `test_auth.py` exercises ~every route under both auth-on and
   auth-off postures; conformance covers the admin/worker
   authority table; the new `test_multi_app_isolation` test
   added in wave 4 catches single-app intra-router cross-talk.

2. **`app.include_router` ordering regression.** Decision 5 +
   §7.1 cover this; the wave-4 test added in §6.2 locks it in.
   Without that test, a future router insertion (e.g., a
   hypothetical "checkpoints alias" router added between groups
   and the experiment-read route) silently shadows `GET /v0/experiments/{id}`.

3. **Conformance regression in error envelope shape.** Every
   handler's error envelope flows through the app-level exception
   handlers (Decision 4). If a router accidentally catches a
   `StorageError` locally (e.g., to convert it to a different
   status code) the global handler is bypassed and the conformance
   envelope-uniformity check fails. Mitigation: the existing
   eden-wire test suite + conformance suite collectively assert
   the envelope on every error type; do NOT add per-router
   `try/except StorageError` blocks in any wave.

4. **Closure-vs-deps semantic drift.** A handler that used to read
   `store` directly from closure may, after the refactor, need to
   read `deps.store`. Search-and-replace catches the obvious cases;
   the type-checker catches the rest (closures over the wrong
   `store` variable simply won't compile). Mitigation: `uv run
   pyright` is a per-wave gate.

5. **`dispatch_mode` PATCH null-collapse regression.** §7.2
   covers; wave-4 test addition is the backstop. Surface
   explicitly in the wave-4 PR description so codex round-0
   examines it.

6. **`_serve_artifact`'s self-auth posture inversion.** §7.4
   covers; `test_artifact_route.py` is the gate.

7. **Helm-base-chart / 13a coordination.** None. F-3 does not
   touch any Helm template, env-var, or compose file; it's a
   structural refactor within the Python wire package.

8. **`# slop-allow:` removal timing.** The annotations must be
   removed in the same PR that brings `server.py` under threshold
   (wave 5). Removing them before — e.g., in wave 1 — causes the
   complexity gate to fail and the wave's CI to red. Removing
   them after — e.g., in wave 7 — leaves the gate flagging a
   no-longer-needed allowance. Wave 5 is the only correct slot.

9. **`/v0/checkpoints/import` route shadowing by a future
   experiment-id-shaped path.** Currently a top-level route; the
   include-router order places it inside `routers/checkpoints.py`.
   If a future router declares `/v0/checkpoints/{id}` (hypothetical
   per-checkpoint endpoint), path-resolution interplay matters.
   Out of scope today; surface in the wave-5 PR description as a
   forward-looking note.

10. **Codex-review round count.** The audit disposition pegs F-3 at
    "~1 day equivalent". The plan's wave-stage codex-review is
    expected at 3–5 rounds because the auth-dispatch threading +
    43-endpoint mapping + L-D/L-F + L-E cleanup is substantial. If
    impl-stage codex-review exceeds 6 rounds on any single wave,
    surface to the operator per stop conditions in the brief.

## 10. Sequencing summary

Recommended PR sequence (in order, one per wave):

1. **Wave 1** — `routers/tasks.py` + `_dependencies.py`. Proof-of-shape.
2. **Wave 2** — `routers/ideas.py` + `routers/variants.py` + `routers/events.py`.
3. **Wave 3** — `routers/workers.py` + `routers/groups.py`.
4. **Wave 4** — `routers/dispatch_mode.py` + `routers/experiment.py` + regression tests.
5. **Wave 5** — `routers/checkpoints.py` + `routers/reference.py` + `_artifact_fd.py` + slop-allow removal + compose smokes.
6. **Wave 6** (optional) — L-E control-plane symmetric regroup.
7. **Wave 7** — docs + CHANGELOG + roadmap + audit-disposition
   resolution annotations + impl-stage codex-review record commit.

Total: 6–7 PRs. Approximate effort: 1 day equivalent (the audit's
estimate), spread across the waves to keep each PR ≤ 500 LOC of
diff + behavior-preserving moves.

## 11. Validation gates summary

The literal AGENTS.md "Commands" pre-push gate runs at the end of
every wave:

```bash
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
uv run ruff check .
uv run pyright
uv run pytest -q
uv run pytest -q conformance/
python3 scripts/check-complexity.py
python3 scripts/check-rename-discipline.py
```

Wave 5 + wave 7 additionally run:

```bash
bash reference/compose/healthcheck/smoke.sh
bash reference/compose/healthcheck/smoke-subprocess.sh
bash reference/compose/healthcheck/e2e.sh   # wave 7 only
```

Per AGENTS.md "The 'Commands' section above is the literal
pre-push validation gate": no narrowed subsets. Every wave runs
the full quartet.

## 12. Stop conditions (per operator brief)

- **Architectural surprise** — auth-context can't decompose
  cleanly into `RouterDeps`, or some route has shared mutable
  state across resources that breaks the closure-capture pattern.
  → surface to operator with options before continuing.
- **Codex round count > 6 on any single wave** — surface.
- **1Password / capability / sandbox blocks** — surface.
- **Conformance suite regression that can't be traced to a
  specific wave** — surface; do NOT silently merge.

## 13. Cross-references

- Audit: [`docs/audits/2026-05-20-code-quality-audit.md`](../audits/2026-05-20-code-quality-audit.md)
  and [`docs/audits/2026-05-20-phase-c-disposition.md`](../audits/2026-05-20-phase-c-disposition.md) §F-3 / §L-D / §L-E / §L-F.
- Issue: [ealt/eden#115](https://github.com/ealt/eden/issues/115).
- Sibling: F-4 (client.py per-resource split) — [ealt/eden#116](https://github.com/ealt/eden/issues/116).
- Spec contract: [`spec/v0/07-storage-binding.md`](../../spec/v0/07-storage-binding.md)
  — the wire surface F-3 must preserve byte-for-byte.
- Tier-1 gate: [`scripts/check-complexity.py`](../../scripts/check-complexity.py) +
  the `complexity-gate` CI job in [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml).
- Slop-prevention discipline: [`CLAUDE.md`](../../CLAUDE.md) §"Slop prevention".
