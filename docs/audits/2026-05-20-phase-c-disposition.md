# Phase C disposition list — per-violator refactor / slop-allow proposals

**Companion to**: [`2026-05-20-code-quality-audit.md`](2026-05-20-code-quality-audit.md)

**Scope**: every existing threshold violator at post-Phase-12c HEAD
(`ff3d4a9`). Per operator's correction (no blanket grandfathering),
each violator below carries either (a) a proposed refactor with the
expected post-refactor metric, or (b) a proposed `# slop-allow:`
annotation with a specific justification.

**Thresholds** (Tier-1 gate fails on):

- File SLOC > 800
- File MI < 20
- Function CC > 20
- Function length > 100

**Status**: awaiting operator approval. Nothing in this list is applied
yet; the previously-approved L-1 helpers in `eden_storage/submissions.py`
are uncommitted and harmless (new functions, no caller rewires yet).

---

## §1 Files with SLOC > 800 (4 entries)

| ID | File | SLOC | MI | Disposition |
|---|---|---:|---:|---|
| **F-1** | `reference/packages/eden-storage/src/eden_storage/_base.py` | 1638 | 0.00 | **REFACTOR** (M-5 mixin split — see below; large blast radius) |
| **F-2** | `reference/services/web-ui/src/eden_web_ui/routes/admin.py` | 1239 | 5.12 | **REFACTOR** (M-1; already approved) |
| **F-3** | `reference/packages/eden-wire/src/eden_wire/server.py` | 1211 | 38.53 | **REFACTOR** (H-1 APIRouter regroup; PR #103 merged so coordination concern is gone) |
| **F-4** | `reference/packages/eden-wire/src/eden_wire/client.py` | 861 | 21.54 | **REFACTOR** (per-resource client split: tasks / variants / ideas / experiment / dispatch / workers / groups) |

### F-1 detail — `_base.py` mixin split

`_StoreBase` has 105 methods covering all backend-agnostic state-machine
logic. Proposed split into 7 mixins, each in its own file under
`eden_storage/_ops/`:

- `_ops/tasks.py` — `_TaskOps` (`create_task`, `read_task`, `list_tasks`,
  `claim`, `submit`, `accept`, `reject`, `reclaim`, `reassign_task`,
  `_insert_*_task`, `create_ideation_task`, `create_execution_task`,
  `create_evaluation_task`, `validate_acceptance`, `validate_terminal`,
  `_accept_*`, `_reject_*`)
- `_ops/ideas.py` — `_IdeaOps` (`create_idea`, `read_idea`,
  `list_ideas`, `mark_idea_ready`)
- `_ops/variants.py` — `_VariantOps` (`create_variant`, `read_variant`,
  `list_variants`, `declare_variant_evaluation_error`,
  `integrate_variant`, `_validate_non_no_op_variant`,
  `_validate_submission_ref_binding`, `_find_starting_variant_for_implement_task`)
- `_ops/experiment.py` — `_ExperimentOps` (`read_experiment`,
  `read_experiment_state`, `update_experiment_state`,
  `read_dispatch_mode`, `update_dispatch_mode`, `terminate_experiment`,
  `emit_policy_error`, `_require_running`)
- `_ops/workers.py` — `_WorkerOps` (worker + group methods)
- `_ops/events.py` — `_EventOps` (`events`, `replay`, `read_range`)
- `_ops/validation.py` — `_ValidationOps` (`_validate_evaluation`,
  `_validate_*_acceptance`, etc.)
- `_base.py` keeps `_StoreBase(*mixins)`, `_Tx`, the `__init__`, abstract
  primitives (`_get_*`, `_iter_*`, `_atomic_operation`, `_apply_commit`),
  and any helpers used across mixins.

**Expected post-refactor**: `_base.py` ~250 SLOC; each mixin 100-400
SLOC; no file > 500. MI of each fragment should be > 40.

**Risk**: every backend (memory / sqlite / postgres) subclasses
`_StoreBase`; mixin order matters for MRO. Every test that goes through
the store surface exercises this. Behavior is preserved — no semantic
change. Blast radius: ~15 files touched; ~3000 lines moved (not
rewritten).

### F-3 detail — `eden_wire/server.py` APIRouter regroup

`make_app` is a 1458-line factory closure with 57 nested route handlers.
Proposed: extract handlers into APIRouter modules grouped by resource:

- `routes/tasks.py` — task lifecycle endpoints
- `routes/ideas.py` — idea endpoints
- `routes/variants.py` — variant endpoints
- `routes/experiment.py` — experiment / state / dispatch / checkpoint endpoints
- `routes/workers.py` — worker + credential endpoints
- `routes/groups.py` — group endpoints
- `routes/events.py` — event-stream + subscribe endpoints
- `make_app` becomes a 50-line assembler: build the FastAPI app, attach
  middleware + exception handlers, mount each router.

Closure capture of `store` becomes `app.state.store` + a
`Depends(get_store)`. The auth helpers (`_enforce_worker`,
`_enforce_in_any_group`) stay at module-private scope and are imported
by each router.

**Expected post-refactor**: `server.py` → ~120 SLOC; each router 80-200
SLOC.

**Risk**: every test in `eden-wire/tests/` and downstream services calls
`make_app(store)`. Signature preserved → existing tests still pass.
Larger churn surface than F-1 / F-2 because each handler also moves.

### F-4 detail — `eden_wire/client.py` per-resource split

`StoreClient` is one class with ~30 HTTP methods. Each method has its
3-outcome readback ladder per AGENTS.md. Proposed: keep `StoreClient`
as the public facade but compose it from per-resource clients in
`_client/` (parallel to F-3's server-side split):

- `_client/tasks.py` — task method bodies
- `_client/ideas.py` — idea method bodies
- ... (one per resource)

`StoreClient` becomes a thin assembler that delegates to each section's
methods.

**Expected post-refactor**: `client.py` → ~100 SLOC; each section 100-200
SLOC.

**Risk**: lower than F-3 because the public surface (`StoreClient`)
stays identical; only the internal organization moves. ~12 files
touched.

---

## §2 Files with MI < 20

Both are also in §1 (F-1 + F-2). Same dispositions.

---

## §3 Functions with CC > 20 (7 entries)

| ID | Function | CC | LEN | Disposition |
|---|---|---:|---:|---|
| **C-1** | `routes/executor.py:229 submit` | 42 | 336 | **REFACTOR** (M-2 `_PreSubmitGates`; already approved) |
| **C-2** | `forms.py:70 parse_idea_rows` | 35 | 120 | **REFACTOR** (extract `_parse_idea_row(...)` per-row; main loop calls it) |
| **C-3** | `routes/admin.py:304 tasks_index` | 24 | 77 | **REFACTOR** (within M-1: extract filter parsing + result coloring helpers) |
| **C-4** | `_checkpoint.py:512 _commit_import` | 23 | 143 | **REFACTOR** (extract `_parse_archive(reader)`, `_validate_rows(...)`, `_execute_import(store, tx)`) |
| **C-5** | `routes/admin.py:1068 work_refs_delete` | 23 | 89 | **REFACTOR** (within M-1: extract delete-gate-ladder into a helper class like M-2's `_PreSubmitGates`) |
| **C-6** | `routes/admin.py:522 task_reassign` | 22 | 130 | **REFACTOR** (within M-1: extract per-kind reassign helpers + readback ladder) |
| **C-7** | `routes/evaluator.py:271 submit` | 21 | 117 | **REFACTOR** (M-3 — symmetric `_PreSubmitGates` for evaluator; same shape as M-2) |

**No slop-allow candidates here.** Every CC > 20 function has a
mechanical refactor path. All are gate-ladder or per-row-validator
shapes whose complexity comes from chained early-returns.

Note on `routes/ideator.py:274 submit_idea` (CC=20, 120L): exactly at
the boundary — passes the Tier-1 gate (which is `> 20`). The same
`_PreSubmitGates` extraction (M-2 family) would drop it to ~5. Worth
doing for symmetry with executor + evaluator and to nudge it well
below threshold. Adding as **C-8** (refactor, scope-creep but
symmetric).

| **C-8** | `routes/ideator.py:274 submit_idea` | 20 | 120 | **REFACTOR** (M-3 family — same shape as M-2 / C-7) |

---

## §4 Functions with length > 100 (28 entries)

Many overlap §3 (those have higher-precedence refactors). The
remaining ones split into three buckets:

### §4.1 — argparse `parse_args` boilerplate (3 entries) — **PROPOSED SLOP-ALLOW**

| ID | Function | LEN | CC | Disposition |
|---|---|---:|---:|---|
| **L-A** | `orchestrator/cli.py:70 parse_args` | 166 | 1 | `# slop-allow: argparse builder, one add_argument per flag` |
| **L-B** | `web-ui/cli.py:54 parse_args` | 132 | 1 | `# slop-allow: argparse builder, one add_argument per flag` |
| **L-C** | `task-store-server/cli.py:24 parse_args` | 115 | 1 | `# slop-allow: argparse builder, one add_argument per flag` |

**Proposed justification (for operator approval before applying)**:

```python
# slop-allow: argparse builder is linear (CC=1) — one add_argument
# per CLI flag with no branching. Splitting into per-group helpers
# adds invocation indirection without reducing logic; the flat list
# is the most readable shape for a flag manifest. See AGENTS.md
# "Slop prevention" §3 for the operator-approved exception.
```

These all have **CC=1**, which is the strongest signal of "no logic to
fragment." Each `add_argument` call is independent metadata.

**Operator decision needed**: approve this slop-allow shape for all 3?
(They are the only CC=1 length violators; this exception is narrow
and defensible.)

### §4.2 — FastAPI `make_app` app factories (2 entries) — **PARTIAL REFACTOR**

| ID | Function | LEN | CC | Disposition |
|---|---|---:|---:|---|
| **L-D** | `eden-wire/server.py:325 make_app` | 1458 | 5 | **REFACTOR** (covered by F-3) — post-refactor will be ~50 LOC |
| **L-E** | `control-plane/app.py:121 make_app` | 415 | 2 | **REFACTOR** (symmetric APIRouter regroup; only 8 nested handlers — proportionally smaller) |
| **L-F** | `eden-wire/server.py:1664 make_app._serve_artifact` | 117 | 14 | **REFACTOR** (covered by F-3 — `_serve_artifact` moves to its own module) |

### §4.3 — service main / loop / handler bodies (21 entries) — case-by-case

Each entry below carries the proposed disposition. The recurring
patterns are:

1. **service `main()`** (4 entries — ideator / evaluator / executor /
   web-ui CLI entrypoints) — Extract `_build_runtime(args)` helper that
   does config-parse + logger-config + store-client construction; main()
   then becomes ~30 LOC.
2. **per-task `_handle_one`** (2 entries — executor + evaluator) —
   Decompose by phase: `_claim_phase`, `_validate_phase`, `_execute_phase`,
   `_submit_phase`.
3. **subprocess wrappers `_run_subprocess`** (2 entries — executor +
   evaluator) — Extract `_build_env(...)`, `_run_and_capture(...)`,
   `_classify_outcome(...)`.

| ID | Function | LEN | CC | Disposition |
|---|---|---:|---:|---|
| **L-G** | `executor/subprocess_mode.py:151 _handle_one` | 279 | 18 | **REFACTOR** (phase decomposition) |
| **L-H** | `eden-storage/postgres.py:92 ensure_readonly_role` | 274 | 13 | **REFACTOR** (extract `_create_readonly_user`, `_grant_table_access`, `_create_readonly_views`) — the DDL clusters into 3 phases naturally |
| **L-I** | `orchestrator/cli.py:480 _run_multi_experiment` | 238 | 9 | **REFACTOR** (extract `_bootstrap_experiment(...)`, `_run_iteration(...)`, `_finalize(...)`) — added by 12c |
| **L-J** | `ideator/cli.py:187 main` | 171 | 11 | **REFACTOR** (extract `_build_runtime(args)` helper) |
| **L-K** | `eden-git/integrator.py:129 integrate` | 160 | 16 | **REFACTOR** (extract per-step methods matching chapter 6 §3 ladder: `_check_reachability`, `_push_squash`, `_handle_push_outcome`, `_emit_event`) |
| **L-L** | `web-ui/cli.py:214 main` | 143 | 11 | **REFACTOR** (extract `_build_runtime(args)` helper) |
| **L-M** | `eden-wire/client.py:1141 _import_recovery_probe` | 141 | 19 | **REFACTOR** (extract `_probe_remote_id`, `_classify_probe_outcome`) — covered by F-4 client split |
| **L-N** | `executor/subprocess_mode.py:488 _run_subprocess` | 131 | 11 | **REFACTOR** (extract `_build_env`, `_run_and_capture`) |
| **L-O** | `eden-storage/_base.py:1148 reassign_task` | 122 | 11 | **REFACTOR** (within F-1's `_TaskOps` mixin: split per-kind reassign branches) |
| **L-P** | `orchestrator/lease_manager.py:228 refresh` | 119 | 14 | **REFACTOR** (extract `_compute_targets`, `_apply_refresh`) — added by 12c |
| **L-Q** | `evaluator/subprocess_mode.py:303 _run_subprocess` | 118 | 11 | **REFACTOR** (mirrors L-N) |
| **L-R** | `evaluator/cli.py:153 main` | 116 | 11 | **REFACTOR** (mirrors L-J) |
| **L-S** | `evaluator/subprocess_mode.py:136 _handle_one` | 114 | 8 | **REFACTOR** (mirrors L-G) |
| **L-T** | `ideator/host.py:71 run_ideator_subprocess_loop` | 107 | 16 | **REFACTOR** (extract `_handle_message`, `_advance_state`) |
| **L-U** | `dispatch/driver.py:43 run_orchestrator_iteration` | 103 | 12 | **REFACTOR** (extract per-role iteration helpers) |
| **L-V** | `web-ui/routes/admin_groups.py:171 walk_transitive_workers` | 101 | 15 | **PROPOSED SLOP-ALLOW** — see below |

### §4.3.A — L-V proposed slop-allow

```python
# slop-allow: graph traversal closure is most readable as a single
# function. Extraction would force a separate helper to maintain
# (visited, worklist) state — making the recursion harder to follow,
# not easier. Length is 101 (1 over threshold).
```

**Operator decision needed**: justified or refactor anyway?

---

## §5 12c-new violators (already merged into the lists above)

Phase 12c added 4 length-violators (all in §4.3):

- **L-I** `orchestrator/cli.py _run_multi_experiment` (238L)
- **L-P** `orchestrator/lease_manager.py refresh` (119L)
- **L-A** `orchestrator/cli.py parse_args` (166L) — slop-allow candidate
- **L-E** `control-plane/app.py make_app` (415L)

No new CC > 20 functions. No new files > 800 SLOC (`control-plane/postgres.py`
at 737 is below threshold but the largest 12c-new surface — worth
keeping an eye on; not actionable today).

---

## §6 Summary

| Category | Count | Refactor | Slop-allow proposed |
|---|---:|---:|---:|
| Files > 800 SLOC | 4 | 4 | 0 |
| Files MI < 20 | 2 (subset) | 2 | 0 |
| Functions CC > 20 | 7 | 7 | 0 |
| Functions length > 100 | 28 | 24 | 4 (L-A, L-B, L-C, L-V) |

**Total refactor surface**: 4 file-level splits + ~22 function-level
refactors (some overlapping — e.g. C-3/C-5/C-6 live inside F-2/M-1;
L-D/L-F live inside F-3).

**Total slop-allow candidates**: 4 (3 argparse + 1 graph traversal).
All require explicit operator approval per the new policy.

---

## §7 Effort estimate (rough)

If everything is refactored as proposed:

- **Tier-1**: ~3-4 small refactors per package
- **F-1 _base.py mixin split**: ~1 day equivalent — touches 15 files, ~3000 lines moved
- **F-3 server.py APIRouter regroup**: ~1 day equivalent — touches every wire-binding test
- **F-4 client.py per-resource split**: ~0.5 day — internal organization
- **F-2/M-1 admin.py 4-way split + C-3/C-5/C-6 internals**: ~0.5 day
- **C-1/C-7/C-8 submit-handler gate extractions**: ~0.5 day (symmetric pattern, one helper class reused)
- **L-G/L-H/L-I/L-J … (15-20 function refactors)**: ~0.5 day each cluster

Total: this is now a multi-day refactor chunk well beyond the original
LOW + 2 MEDIUM scope. Comparable in size to a typical Phase chunk.

---

## §8 Decisions needed from operator

1. **Approve / reject each refactor proposal in §1 (F-1, F-2, F-3, F-4)**. F-2 was already approved (M-1). F-1, F-3, F-4 are the new larger-scope items.
2. **Approve / reject each refactor proposal in §3 (C-1 through C-8)**. C-1 was already approved (M-2). The other six are mechanical follow-ons.
3. **Approve / reject the §4 length-violator refactors (L-G through L-V minus L-V).** These are 15-ish function refactors with mostly the same shape (phase decomposition / runtime-build extraction).
4. **Approve / reject the 4 slop-allow candidates (L-A, L-B, L-C, L-V)**. The argparse trio has a CC=1 case for exception; L-V (graph traversal) is the closest to "refactoring would make it worse."
5. **Confirm the safeguard plan**: Tier-1 gate ships only AFTER all approved refactors land + the 4 (or fewer) slop-allow annotations are added with operator-blessed justifications. Tier 2-4 ship alongside.

I will not apply anything in §1-§4 until each item above is approved
(or rejected, with operator's chosen alternative).
