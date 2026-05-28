# F-3 impl-stage review — `eden-wire/server.py` APIRouter regroup

**Issue.** [#115](https://github.com/ealt/eden/issues/115). **PR.** [#237](https://github.com/ealt/eden/pull/237).
**Date.** 2026-05-27. **Branch.** `impl/issue-115-server-regroup`.
**Scope reviewed.** `git diff origin/main...HEAD` (21 files: the per-resource
router split + `_dependencies.py` + `_artifact_fd.py` + thin `server.py` +
test retargets/additions + docs).

## Method

High-effort multi-angle review of the diff: 3 correctness angles
(line-by-line scan, removed-behavior auditor, cross-file tracer) + 3 cleanup
angles (reuse / simplification / efficiency) + 1 altitude angle. Each angle
compared the new modules against the pre-refactor monolith
(`git show origin/main:.../server.py`).

## Outcome — converged clean (0 confirmed/plausible findings)

### Correctness (A/B/C) — zero defects

- **Route coverage.** All 43 routes present in exactly one new router; no
  missing or duplicated route. Each `APIRouter(prefix=...)` + suffix
  reconstructs the original fully-qualified path.
- **Exception handlers.** All 9 `@app.exception_handler(...)` registrations
  preserved. The 5 identical chapter-7 handlers collapse onto one shared
  `_error_envelope_handler` registered against the same exact type set
  (`StorageError`/`BadRequest`/`ExperimentIdMismatch`/`Unauthorized`/`Forbidden`),
  so MRO resolution is unchanged and an unrecognized `WireError` still 500s.
- **Auth-dispatch matrix.** Every per-route guard preserved verbatim:
  `enforce_worker` / `enforce_in_any_group(...)` group tuples, the direct
  `require_admin`/`require_worker`/`authenticate` callers (workers / groups /
  checkpoints / reference), either-auth inline sites, `POST /tasks`
  split-by-kind authority, `whoami` as the sole direct `require_worker`.
- **Behavior-preservation subtleties confirmed.** `_submission_from_wire`
  keeps `HTTPException(400)` (not `BadRequest`); checkpoint import keeps the
  empty-body check, optional-header mismatch, `CheckpointExperimentIdMismatch`
  → `ExperimentIdMismatch` conversion, 201 status, and credential-reissue
  persistence/warnings; dispatch_mode PATCH keeps the `model_extra` walk before
  `exclude_none`; artifact route keeps auth-first ordering + experiment-id
  guard + `artifacts_dir is None` → 503.
- **Cross-file.** No remaining importer of the moved symbols
  (`_open_artifact_fd`, `_FILE_FLAGS`, the guard closures,
  `_submission_*`, `_slug_conflict_warnings`, `_persist_reissued_credentials`)
  from `eden_wire.server`. `__init__.py` public surface unchanged; `make_app`
  signature unchanged (incl. `checkpoint_import_credentials_dir`). The only
  test coupling — `test_artifact_route.py`'s monkeypatches — was retargeted to
  `eden_wire._artifact_fd`.

### Cleanup / altitude — no actionable findings

Candidates surfaced were either (a) pre-existing helpers moved **verbatim**
from the monolith (`_submission_to_wire`/`_submission_from_wire`,
`_slug_conflict_warnings`'s O(n) advisory scan, `_build_content_disposition`,
`_persist_reissued_credentials`) — changing them is out of a no-behavior-change
refactor's scope and would risk a behavior change; or (b) self-refuted on
verification (the experiment_lifecycle/experiment_read shared-prefix split, the
checkpoints full-path router, and the multiple `/v0/experiments/{experiment_id}`
prefix routers were all confirmed safe under FastAPI single-segment path-param
scoping — no shadowing, no registration-order dependence; this invariant is
pinned by the new `test_path_segment_scoping_no_shadow`). The closure-factory
repetition across ~40 handlers is warranted by the complexity-gate constraint
that each handler be an independently-measured function.

## Validation gates (all green except a pre-existing unrelated failure)

- `uv run ruff check .` — pass
- `uv run pyright` — 0 errors
- `uv run pytest -q reference/packages/eden-wire/tests/` — 242 passed
- `uv run pytest -q conformance/ -n auto` — 250 passed, 13 skipped
- `uv run pytest -q` — 1949 passed, 221 skipped
- `python3 scripts/check-complexity.py` — 0 blocking (F-3 + L-D slop-allow
  entries removed)
- `bash reference/compose/healthcheck/smoke.sh` — PASS
- markdownlint (changed docs) — 0 errors
- `scripts/check-rename-discipline.py` — **pre-existing failure on `origin/main`**,
  not introduced by this PR; tracked in [#236](https://github.com/ealt/eden/issues/236).

## Deferrals filed

- [#235](https://github.com/ealt/eden/issues/235) — L-E control-plane
  symmetric APIRouter regroup (deferred per plan §7.9).
- [#236](https://github.com/ealt/eden/issues/236) — pre-existing
  `rename-discipline` false positives in conformance harness.
