# Code-quality audit — 2026-05-20

**Scope**: every Python source file under `reference/` at `main` HEAD (commit
`e8cc233`, post-Phase 12b). Audit-only; no source changes in this commit.

> **Post-rebase note (2026-05-20)** — Phase A merged onto post-Phase-12c
> main (`ff3d4a9`). Re-running the metrics confirmed every Phase A finding
> still applies. 12c added two new files: `eden-control-plane/postgres.py`
> (737 SLOC, MI 26.14 — within budget but the largest 12c-new surface)
> and `eden-control-plane/memory.py` (MI 31.79). Neither introduces a
> new function with CC ≥ 10. The Tier-1 gate added in Phase C will
> guard both files going forward.

**Why this audit exists**: EDEN has stacked ~14 substantive chunks since
Phase 12 started (12a-1 through 12c). Recent research on long-horizon
LLM-driven coding ("SlopCodeBench") flags two quality-decay modes that
correctness-focused review (codex + spec parity) does not catch:

- **Verbosity / duplication** — redundant code that grew across chunks.
- **Structural erosion** — complexity concentrating in a small number of
  very large / very complex functions and files.

This audit measures both, identifies specific refactor candidates, and
notes the process gaps that allowed them through.

## Tools used

| Tool | Purpose | Notes |
|---|---|---|
| `radon cc -j` | Cyclomatic complexity per function | top-of-the-line static metric for branching density |
| `radon mi -j` | Maintainability index per file | rolls CC + Halstead + LOC into a 0–100 score |
| `radon raw -j` | LOC / SLOC / LLOC per file | distinguishes blank+comment lines |
| `lizard --CCN 10 -T length=100` | Function length warnings | catches `length > 100`, `CCN > 10`, `params > 100` |
| `pylint --disable=all --enable=duplicate-code` | Cross-file token duplication | thresholds: 15, 20, 40 similarity-lines |

Reproduction commands are listed in §A of "Reproducing the metrics" below.

## §1 Headline numbers

- **Files analyzed**: 215 (103 IMPL + 112 TEST `.py` files under `reference/`)
- **IMPL SLOC**: 19,655 — **TEST SLOC**: 27,793 (test/impl ratio 1.41:1)
- **Functions/methods/classes analyzed**: 2,895 across all files
- **Average cyclomatic complexity**: 3.22 (rank A — healthy)

Distribution of impl-side cyclomatic complexity:

| Threshold | Count | Severity |
|---|---|---|
| CC ≥ 10 (radon C-grade) | 67 | warning |
| CC ≥ 20 (radon D-grade) | 8 | serious — refactor candidate |
| CC ≥ 30 (radon E/F-grade) | 2 | critical |

Distribution of impl-side file size:

| Threshold | Count | Files |
|---|---|---|
| SLOC > 800 ("god file") | 4 | `_base.py`, `admin.py`, `server.py`, `client.py` (wire) |
| 500 ≤ SLOC ≤ 800 | 5 | `postgres.py`, `executor.py` (routes), `repo.py`, `eden-manual`, `subprocess_mode.py` (executor) |

Distribution of impl-side function length:

| Threshold | Count |
|---|---|
| LEN > 100 | 25 |
| LEN > 200 | 4 |
| LEN > 400 | 1 (degenerate — `make_app` is a 1458-line app factory with 57 nested handlers; see §3.4) |

Overall verdict: the codebase is healthy on average (CC 3.22 across 2,895
blocks). The slop is concentrated in a handful of hotspots, not spread
evenly. That makes it tractable.

## §2 Top hotspots

### §2.1 Top 25 IMPL functions by cyclomatic complexity

| CC | Rank | Location |
|---:|:-:|---|
| 42 | F | `reference/services/web-ui/src/eden_web_ui/routes/executor.py:229` `submit` |
| 35 | E | `reference/services/web-ui/src/eden_web_ui/forms.py:70` `parse_idea_rows` |
| 24 | D | `reference/services/web-ui/src/eden_web_ui/routes/admin.py:304` `tasks_index` |
| 23 | D | `reference/packages/eden-storage/src/eden_storage/_checkpoint.py:512` `_commit_import` |
| 23 | D | `reference/services/web-ui/src/eden_web_ui/routes/admin.py:1068` `work_refs_delete` |
| 22 | D | `reference/services/web-ui/src/eden_web_ui/routes/admin.py:522` `task_reassign` |
| 21 | D | `reference/services/web-ui/src/eden_web_ui/routes/evaluator.py:271` `submit` |
| 20 | C | `reference/services/web-ui/src/eden_web_ui/routes/ideator.py:274` `submit_idea` |
| 19 | C | `reference/packages/eden-wire/src/eden_wire/client.py:1142` `_import_recovery_probe` |
| 18 | C | `reference/services/web-ui/src/eden_web_ui/routes/admin.py:792` `variants_index` |
| 18 | C | `reference/services/web-ui/src/eden_web_ui/routes/_lineage.py:370` `_producing_execution_task` |
| 18 | C | `reference/services/executor/src/eden_executor_host/subprocess_mode.py:151` `_handle_one` |
| 17 | C | `reference/packages/eden-storage/src/eden_storage/_base.py:1851` `_validate_non_no_op_variant` |
| 17 | C | `reference/packages/eden-storage/src/eden_storage/_checkpoint.py:703` `_validate_bundle_cross_references` |
| 17 | C | `reference/packages/eden-storage/src/eden_storage/sqlite.py:484` `_apply_commit` |
| 17 | C | `reference/packages/eden-storage/src/eden_storage/postgres.py:774` `_apply_commit` |
| 17 | C | `reference/packages/eden-git/src/eden_git/integrator.py:129` `integrate` |
| 17 | C | `reference/services/ideator/src/eden_ideator_host/host.py:71` `run_ideator_subprocess_loop` |
| 15 | C | `reference/packages/eden-storage/src/eden_storage/_checkpoint.py:256` `_is_store_empty` |
| 15 | C | `reference/packages/eden-dispatch/src/eden_dispatch/state_view.py:87` `build_experiment_state_view` |
| 15 | C | `reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py:171` `walk_transitive_workers` |
| 15 | C | `reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py:341` `groups_register` |
| 14 | C | `reference/packages/eden-storage/src/eden_storage/memory.py:151` `_apply_commit` |
| 14 | C | `reference/packages/eden-storage/src/eden_storage/postgres.py:93` `ensure_readonly_role` |

### §2.2 Top 10 IMPL files by maintainability index

| MI | File | Verdict |
|---:|---|---|
| 0.00 | `reference/packages/eden-storage/src/eden_storage/_base.py` | god file — 1638 SLOC, 105 methods on `_StoreBase` |
| 5.12 | `reference/services/web-ui/src/eden_web_ui/routes/admin.py` | god file — 32 top-level handlers in one module |
| 21.54 | `reference/packages/eden-wire/src/eden_wire/client.py` | god file — 861 SLOC, 1397 LOC; one big HTTP client |
| 30.35 | `reference/scripts/manual-ui/eden-manual` | one-off operator CLI — see §3 |
| 33.49 | `reference/packages/eden-git/src/eden_git/repo.py` | 574 SLOC; inherent git-binding surface |
| 33.61 | `reference/packages/eden-storage/src/eden_storage/postgres.py` | mirrors sqlite.py shape (duplicates §2.4) |
| 38.53 | `reference/packages/eden-wire/src/eden_wire/server.py` | `make_app` factory — 1458L; see §3.4 |
| 38.69 | `reference/services/web-ui/src/eden_web_ui/routes/executor.py` | `submit` is the CC-42 hotspot |
| 38.77 | `reference/services/web-ui/src/eden_web_ui/routes/admin_workers.py` | borderline; mostly forms |
| 39.12 | `reference/services/evaluator/src/eden_evaluator_host/subprocess_mode.py` | parallel to executor host (duplicates §2.4) |

Everything ≥ 40 is fine on this metric.

### §2.3 Top 10 IMPL files by SLOC

| SLOC | LOC | LLOC | File |
|---:|---:|---:|---|
| 1638 | 2712 | 1216 | `reference/packages/eden-storage/src/eden_storage/_base.py` |
| 1239 | 1632 | 684 | `reference/services/web-ui/src/eden_web_ui/routes/admin.py` |
| 1211 | 1871 | 612 | `reference/packages/eden-wire/src/eden_wire/server.py` |
| 861 | 1397 | 568 | `reference/packages/eden-wire/src/eden_wire/client.py` |
| 690 | 958 | 362 | `reference/packages/eden-storage/src/eden_storage/postgres.py` |
| 633 | 834 | 304 | `reference/services/web-ui/src/eden_web_ui/routes/executor.py` |
| 574 | 939 | 419 | `reference/packages/eden-git/src/eden_git/repo.py` |
| 569 | 799 | 454 | `reference/scripts/manual-ui/eden-manual` |
| 532 | 701 | 329 | `reference/services/executor/src/eden_executor_host/subprocess_mode.py` |
| 484 | 706 | 275 | `reference/services/web-ui/src/eden_web_ui/routes/ideator.py` |

### §2.4 Duplication blocks (IMPL only, ≥ 15 similar lines)

`pylint --disable=all --enable=duplicate-code --min-similarity-lines=15`
flags five impl-touching duplicate blocks:

| Block | Locations | Lines |
|---|---|---:|
| **D-1** Submission ⇄ JSON shape | `eden_storage/sqlite.py:75-124` ↔ `eden_storage/postgres.py:377-426` | ~50 |
| **D-2** Submission ⇄ wire shape | `eden_wire/server.py:1786-1834` ↔ `eden_wire/client.py:1318-1366` | ~48 |
| **D-3** Submit-with-readback ladder | `web-ui/routes/evaluator.py:443-502` ↔ `web-ui/routes/executor.py:648-717` | ~59 |
| **D-4** Subprocess-host CLI bootstrap | `evaluator_host/cli.py:108-156` ↔ `executor_host/cli.py:98-146` | ~48 |
| **D-5** `_apply_commit` shape | `eden_storage/sqlite.py:465-498` ↔ `eden_storage/postgres.py:755-784` | ~33 |

Plus, an unscored sixth — D-1 and D-2 together represent **four** near-identical copies of submission de/serialization (sqlite, postgres, wire client, wire server) because the storage form uses `(kind, json_string)` and the wire form uses `dict-with-kind`, but the body shape is byte-identical.

## §3 Qualitative read of top god-files

For each of the top SLOC offenders, characterize the complexity source:

### §3.1 `eden_storage/_base.py` — 1638 SLOC, 105 methods on `_StoreBase`, MI 0.00

**Verdict**: mostly inherent + intentional, with a refactor seam available.

This module is the canonical home for backend-agnostic state-machine
logic — every Store backend (memory / sqlite / postgres) inherits from
`_StoreBase`. Every public method (`claim`, `submit`, `accept`, `reject`,
`reclaim`, `create_*`, `read_*`, `list_*`, `events`, `validate_*`, …) lives
here so all backends share the same semantics. That decision is correct
and re-implementing it would lose the conformance guarantee.

That said:

- The class is **105 methods on one type** with no internal grouping. Splitting
  the methods into mixins (`_TaskOps`, `_VariantOps`, `_IdeaOps`,
  `_ExperimentOps`, `_WorkerOps`, `_ValidationOps`) — each in its own file —
  is mechanical and lossless for semantics. It would drop the per-file SLOC
  by 3-5x and let radon's MI actually reflect maintenance friction per
  concern.
- A handful of validators (`_validate_non_no_op_variant` CC=17,
  `_validate_evaluation` CC=10) carry case logic that could split into
  named predicates.

**Recommendation**: MEDIUM-risk mixin extraction; or LOW-risk per-validator
predicate naming. Defer; not blocking.

### §3.2 `web-ui/routes/admin.py` — 1239 SLOC, 32 handlers, MI 5.12

**Verdict**: accidental accumulation — needs a sub-module split.

This is the strongest "grew across chunks" signal. Chunks 9e, 12a-1b,
12a-1c, 12c all added admin routes to this one file. The handlers fall
naturally into 4 sub-modules:

- **observability views** (`index`, `tasks_index`, `task_detail`,
  `variants_index`, `variant_detail`, `events_index`, `ideas_index`,
  `idea_detail` — 8 read-only routes)
- **operator actions** (`task_reassign` CC=22, `dispatch_mode_update`,
  `terminate_experiment`, `create_execution_task` — mutation routes)
- **work/* GC** (`work_refs_delete` CC=23, `_classify_work_refs`)
- **the "index" / dashboard** (1 route)

A `routes/admin/` package with `observability.py`, `actions.py`,
`work_refs.py`, `index.py` would drop each sub-module under 400 LOC and
let the high-CC hotspots be addressed in isolation.

**Recommendation**: MEDIUM-risk sub-module split; **first refactor target**.

### §3.3 `web-ui/routes/executor.py:229 submit` — CC 42, 337 lines

**Verdict**: inherent control flow, but extractable.

This is the route that runs the executor spec's pre-submit gate chain:
parse form → claim lookup → store read → form validate → repo
reachability check (with origin fetch) → parent ancestor check → no-op
variant tree check → ref-collision check → create_variant(starting) →
create_ref locally → push_ref to origin → submit with retry-readback →
NoOpVariant fallback → render outcome. Each gate has its own
error-render branch (12 of them).

The structure mirrors `spec/v0/03-roles.md §3.3` literally — every MUST in
the spec is one branch in this function. Collapsing the branches would
lose 1-1 traceability with the spec.

Options:

- **Extract the gate chain into a class with one method per gate**
  (`_PreSubmitGates`), each returning `(error_response | None)`. The
  handler becomes a linear list of `if (resp := gates.X()): return resp`.
  Mechanical; preserves 1-1 spec traceability via method names.
- **Status quo + a `# noqa: PLR0911` comment** (already present at line 229).

**Recommendation**: MEDIUM-risk gate-class extraction. Worth doing because
the duplicate D-3 (the same shape in evaluator.py:271) suggests these
gate sequences would share an interface naturally.

### §3.4 `eden_wire/server.py:326 make_app` — 1458 lines, 57 nested handlers

**Verdict**: degenerate metric — inherent to FastAPI's `app = FastAPI(); @app.get(…)` pattern when each handler closes over `store`.

The function is structurally simple: 57 ~15-line route handlers, each a
closure capturing `store` + `experiment_id`. The 1458-line length is a
metric artifact, not a complexity signal — radon CC = 5 for the
outer function, and the individual handlers are all CC ≤ 5.

A "real" refactor would be one of:

- Move handlers to free functions in a `routes/` sub-package and pass
  `store` via dependency injection (`Depends(get_store)`). Idiomatic
  FastAPI; would reduce `make_app` to a router-registration sketch.
- Group handlers by endpoint family (worker / task / variant / idea /
  experiment / dispatch) into separate `APIRouter`s and assemble them in
  `make_app`. Smaller refactor; preserves the closure-over-store pattern.

Worth doing because it ALSO unblocks splitting the `make_app` test surface
([`test_wire_server.py`](../../reference/packages/eden-wire/tests/) currently exercises everything via one
`make_app` call), but high risk because every test fixture uses the
current factory shape.

**Recommendation**: MEDIUM-HIGH risk APIRouter regroup; **defer** unless
operator explicitly authorizes — it's a meaningful churn against active
chunks (PR #103 / Phase 13a both touch this file).

### §3.5 `eden_wire/client.py` — 861 SLOC, MI 21.54

**Verdict**: inherent — one HTTP client per wire endpoint, plus the
readback ladders (`_import_recovery_probe` CC=19,
`_ls_remote_recovery_probe`, etc.).

The complexity here is honest: every `StoreClient` method has a 3-outcome
read-back ladder per the AGENTS.md "wire/network writes need a 3-outcome
read-back" pitfall. Collapsing those ladders would silently violate the
atomicity contract.

Refactor seam: extract the readback ladder template (try → on-failure
read-back → classify) into a generic helper. Maybe.

**Recommendation**: LOW-risk readback-helper extraction (potential
~30% LOC reduction in client.py); **defer** until needed because the
file isn't a maintenance burden today.

## §4 Process gaps — what existing tooling misses

The audit found 67 functions with CC ≥ 10 and 4 files > 800 SLOC. None
of those were flagged by:

- **ruff** — its complexity rules (`C901`, `PLR0911`-`PLR0915`) are
  disabled in our config. ruff today catches style + bug-shaped issues,
  not structural ones.
- **pyright** — type errors only.
- **pytest** — correctness, not structure.
- **codex-review** (`/codex-review` skill) — prompted to check spec
  parity + atomicity + edge-cases; never asked to evaluate "is this
  function getting too big?" or "did this PR duplicate a shape?".

Specifically:

- Duplication blocks D-1 / D-2 (submission de/serialization × 4) crossed
  package boundaries and grew separately during chunks 8a (wire) +
  8b (storage) + 10b (postgres). No tool we run today catches
  cross-package duplication.
- D-3 (submit-readback ladder × 2) was introduced when chunks 9c
  (executor route) and 9e (evaluator route) each implemented the
  read-back independently. The shape was tested per-route, never
  cross-checked.
- `routes/admin.py` grew from ~400 LOC at chunk 9e to 1239 LOC across
  four follow-ups, none of which triggered a "this file is now too
  large" signal.

## §5 Ranked refactor candidates

Each candidate carries an explicit risk + effort estimate. Risk leans on
two axes: blast radius (how many tests + downstream callers touch the
changed surface) and active-chunk overlap (does PR #103 / Phase 13a
touch this file).

### LOW risk (mechanical, isolated)

| ID | Candidate | Files touched | Lines touched | Notes |
|---|---|---:|---:|---|
| **L-1** | Extract submission ⇄ dict helpers into `eden_storage/submissions.py` (resolves D-1 and D-2 jointly via shared `submission_to_dict`/`submission_from_dict`). Wrappers in sqlite.py/postgres.py become `(kind, json.dumps(submission_to_dict(s)))`; wire-side use the dict directly. | 4 | ~200 → ~60 | net -140 LOC, no behavior change |
| **L-2** | Extract `_credential_secret` + `_ensure_repo_clone` shared between executor/evaluator host CLI into `eden_service_common` (resolves D-4). | 3 | ~96 → ~50 | net -45 LOC |
| **L-3** | Extract `_retry_submit_with_readback` + `_readback` shared between web-ui executor.py and evaluator.py routes into `routes/_submit_readback.py` (resolves D-3). | 3 | ~118 → ~70 | net -50 LOC; both routes already share the shape |
| **L-4** | Name-extract validators inside `_StoreBase._validate_non_no_op_variant` (CC=17 → ~5 per helper). | 1 | ~80 | improves readability without splitting the class |

### MEDIUM risk (touches structure but well-bounded)

| ID | Candidate | Files touched | Notes |
|---|---|---:|---|
| **M-1** | Split `web-ui/routes/admin.py` into `routes/admin/{observability,actions,work_refs,index,__init__}.py`. Each sub-module < 400 LOC. | ~8 new + 1 deleted | first refactor target; fully internal to web-ui; tests group naturally |
| **M-2** | Extract executor `submit` pre-submit gates into a `_PreSubmitGates` class with one method per spec MUST (gates return error response or None; handler is a linear `if (resp := gates.X()): return resp`). | 1 | preserves 1-1 spec traceability; CC drops from 42 → ~8 |
| **M-3** | Same gate-class shape for evaluator `submit` (CC=21) and ideator `submit_idea` (CC=20). | 2 | applies the M-2 pattern symmetrically |
| **M-4** | Resolve D-5 (sqlite/postgres `_apply_commit` shape) by extracting the `_Tx` walk into `_base.py`, with backend hooks for `_upsert_task` etc. | 3 | mechanical; ~70 LOC saved; lets future backends share the walk |
| **M-5** | Split `_StoreBase` (105 methods) into mixins per noun (`_TaskOps`, `_VariantOps`, `_IdeaOps`, `_ExperimentOps`, `_WorkerOps`, `_ValidationOps`). | 7-8 new | reduces per-file MI to readable range; no semantic change; high blast radius (every backend subclass) |

### HIGH risk (defer to dedicated chunk)

| ID | Candidate | Notes |
|---|---|---|
| **H-1** | Reshape `eden_wire/server.py:make_app` into `APIRouter` groups (worker / task / variant / idea / experiment / dispatch). | overlaps PR #103's wire changes; defer to a follow-up |
| **H-2** | Generalize the wire client's readback ladder into a `with_readback(operation, classify)` helper. | requires care to preserve the exact 3-outcome contract per AGENTS.md; defer |

## §6 Recommended safeguard tiers

(Phase C work — not part of this audit commit. Listed here so the operator
can pick tiers before Phase C starts.)

- **Tier-1** — CI gate: hard-fail on new files > 800 SLOC, new functions
  > 100 LOC, CC > 20, MI < 20. Implementable as
  `scripts/check-complexity.py` + a new `complexity` CI job. Catches
  future regressions mechanically regardless of which agent submits the
  PR.
- **Tier-2** — PR warnings (non-blocking): comment diffs on new
  functions with CC ≥ 10 or files > 500 LOC.
- **Tier-3** — Update `/codex-review` prompt to explicitly check for
  duplication / structural erosion / over-large functions, alongside
  the current correctness focus.
- **Tier-4** — Add a "Slop prevention" subsection to AGENTS.md naming
  the patterns this audit caught (cross-package shape duplication;
  routes-file accumulation across chunks; gate-chain handlers exceeding
  CC 20).

## §7 Reproducing the metrics

```bash
# Tools (one-time install into the workspace venv):
uv pip install radon lizard pylint

# Cyclomatic complexity (JSON):
uv run radon cc -s -j reference/ > /tmp/radon_cc.json

# Maintainability index (JSON):
uv run radon mi -s -j reference/ > /tmp/radon_mi.json

# Raw LOC/SLOC (JSON):
uv run radon raw -s -j reference/ > /tmp/radon_raw.json

# Function length + CCN warnings (text):
uv run lizard reference/ -l python -x "*/tests/*" -x "*/.venv/*" \
    --CCN 10 -T length=100

# Duplicate-code blocks (15-line similarity, all):
uv run pylint --disable=all --enable=duplicate-code \
    --min-similarity-lines=15 reference/packages reference/services
```

The pipeline that filtered IMPL-only top-N tables from the JSON is
captured in this commit's command history (see the bash transcript in
the PR description).

## §8 Out of scope for this audit

The audit deliberately does NOT cover:

- Markdown / spec docs (separate `markdownlint` + spec-xref-check
  pipelines).
- Test-file size — tests are expected to be larger than impl (1.41:1
  ratio here is within reason) and test duplication is a different
  conversation than impl duplication.
- The `eden-manual` script (MI 30.35) — it's a manual-UI operator CLI
  with a one-off audience; not in the runtime trust-path.
- `parse_args` functions in CLI modules — these are inherently linear
  `argparse.add_argument` calls, large but CCN=1.
- Conformance suite quality — it's a different change-discipline tier
  (chapter 9 §6 IUT contract restriction).
