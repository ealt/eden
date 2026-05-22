# Refactor F-1 — Split `eden-storage._StoreBase` into a mixin family (#114)

## 1. Context

[PR #105](https://github.com/ealt/eden/pull/105) (code-quality audit, Phase A + C) ships a Tier-1 CI gate that fail-blocks on file > 800 SLOC / function > 100 LEN / CC > 20 / MI < 20. Per the operator's no-grandfather posture, every pre-existing violator was either refactored in Phase C or carries a `# slop-allow:` / `# slop-allow-file:` annotation with an explicit per-item justification reviewed in PR #105. The disposition doc at [`docs/audits/2026-05-20-phase-c-disposition.md`](../audits/2026-05-20-phase-c-disposition.md) is the operator-blessed record of which violators were deferred and where they land — F-1 (this chunk) is the `_StoreBase` mixin split, deferred to issue [#114](https://github.com/ealt/eden/issues/114) with an in-file annotation:

```python
# reference/packages/eden-storage/src/eden_storage/_base.py:1
# slop-allow-file: F-1 _StoreBase mixin split deferred to issue #114
```

The file's current measurements (verified by `uvx radon raw` on this branch):

- LOC 2772 / **SLOC 1670** / LLOC 1236 / MI < 20 (radon's MI parser fails on PEP-695 type-parameter syntax, but Phase A's reported MI was 0.00).
- ~67 methods on `_StoreBase` + 8 module-level helpers (`_no_op_check_inputs`, `_resolve_trees`, etc.) + `_Tx` dataclass + module-level constants.
- The disposition doc projected `_base.py` at ~250 SLOC and 7 mixins at 100-400 SLOC each after the split.

`_StoreBase` is the protocol-correctness core of every reference backend: it owns the public `Store` Protocol surface, all composite-commit staging into `_Tx`, and every validation that's required to satisfy [`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) §6.1-§6.3 atomicity and [`spec/v0/05-event-protocol.md`](../../spec/v0/05-event-protocol.md) §2.2 composite-commit invariants. Three backends — [`InMemoryStore`](../../reference/packages/eden-storage/src/eden_storage/memory.py), [`SqliteStore`](../../reference/packages/eden-storage/src/eden_storage/sqlite.py), [`PostgresStore`](../../reference/packages/eden-storage/src/eden_storage/postgres.py) — inherit `_StoreBase` and supply backend primitives. All three are exercised by the parametrized conformance suite in [`reference/packages/eden-storage/tests/`](../../reference/packages/eden-storage/tests/) via the `make_store` fixture in [`conftest.py`](../../reference/packages/eden-storage/tests/conftest.py); the v1+roles conformance suite in [`conformance/`](../../conformance/) also runs against the reference impl end-to-end.

This refactor is **behavior-preserving by construction**. No spec prose moves; no wire schema changes; no public Protocol method renames. The only observable change is internal file layout — `_StoreBase` becomes a thin composite that inherits its public surface from mixins. The chunk's acceptance is the gate the file is currently exempted from: `python3 scripts/check-complexity.py` passes against `_base.py` without the `# slop-allow-file:` annotation.

### What this chunk delivers

1. `_base.py` becomes a ~150-250 SLOC core: `_Tx`, module constants, the shared abstract base, the `_event` / `_ts` / `_maybe_ts` helpers, and the composite `_StoreBase` class line.
2. Seven new mixin files under `reference/packages/eden-storage/src/eden_storage/_ops/` carry the per-resource operations.
3. `InMemoryStore`, `SqliteStore`, `PostgresStore` all continue to subclass `_StoreBase`. **The backend files do not change** — they bind to `_StoreBase`'s composed surface, not the individual mixins.
4. The `# slop-allow-file:` annotation on `_base.py` is removed. No mixin file carries one; no mixin function carries `# slop-allow:`.
5. L-O sub-refactor: `reassign_task` (LEN=122) is split inside `_TaskOps` so it lands under the function-length threshold (per [`docs/audits/2026-05-20-phase-c-disposition.md`](../audits/2026-05-20-phase-c-disposition.md) L-O — "within F-1's `_TaskOps` mixin: split per-kind reassign branches").

### What this chunk does NOT do

- No spec edits. No schema edits. No wire-protocol edits. No conformance-test edits.
- No backend-primitive changes — `InMemoryStore._apply_commit`, `SqliteStore._apply_commit`, `PostgresStore._apply_commit` keep their exact shapes. The `_Tx` field list stays identical so the per-backend walk stays identical.
- No `Store` Protocol changes. [`protocol.py`](../../reference/packages/eden-storage/src/eden_storage/protocol.py) is the contract; any change there would be a separate plan.
- No address of L-4 (`_validate_non_no_op_variant` CC=17). The disposition doc treats L-4 as a separate readability improvement, not in-scope for F-1. The mixin split leaves the function intact in its new home.
- No public-API rename of `_StoreBase` itself — downstream packages (`eden-checkpoint`, `eden-task-store-server`) import `_StoreBase` as a Python identifier; preserving the name keeps those imports working without a coordinated cross-package change.

## 2. Decisions captured before drafting

These decisions narrow the design surface before the per-mixin breakdown. They are codified here so codex-review and future maintainers see what was deliberate versus open to challenge.

1. **Behavior-preserving refactor (not strictly byte-preserving).** The chunk's correctness story is "every existing test passes byte-identical against every backend". Three classes of necessary mechanical adjustments are NOT byte-preserving and are called out explicitly:
   (a) the L-O `reassign_task` sub-split (§3.6) — extracts a per-state helper inside `_TaskOpsMixin`.
   (b) `_reseed_default_event_counter` compares the bound-method `__func__` of `self._event_id_factory` against `_StoreBase._default_event_id` ([`_base.py:383`](../../reference/packages/eden-storage/src/eden_storage/_base.py)). With `_default_event_id` moving onto `_StoreCore` (§3.1), the comparison MUST change to `_StoreCore._default_event_id`. Failing to update it would silently disable the reseed (an `is not` check that's always `False` because `__func__` no longer matches), corrupting event-id continuity after checkpoint-import. The plan calls this out so codex-review can verify the impl PR updates the comparison.
   (c) `_event_id_factory or self._default_event_id` in `__init__` ([`_base.py:342`](../../reference/packages/eden-storage/src/eden_storage/_base.py)) is fine — bound-method resolution via MRO continues to work, since `_default_event_id` is defined on `_StoreCore` and `_StoreBase` inherits it.
   Beyond these three, no semantic change to any method body.

2. **Mixin family, not delegation.** Composition via Python MRO inheritance (the disposition-doc shape) rather than delegation to attribute-held collaborator objects. Reasons: (a) the public `Store` Protocol expects ~50 methods on a single object; delegation would require boilerplate forwarding for each; (b) MRO is what the disposition doc named ("mixin order matters for MRO"); (c) mixins share the same `self`, so cross-resource reads (`_accept_execution` reads tasks + ideas + variants) work without threading collaborators through every call. Tradeoff: MRO surprises are real (see §8.1) — pyright + a one-time `mro_isinstance` assertion in `_base.py` handle this.

3. **Shared `_Tx` instance per public method, single `_apply_commit` per transaction.** The composite-commit invariant (a method's stages all land atomically or none do) is preserved by keeping `_Tx` as a flat dataclass with one field per resource type, and by requiring every public method to stage everything into one `_Tx` and call `self._apply_commit(tx)` exactly once. The mixins do NOT each get their own private `_Tx` — that would break composite-commit (`_accept_execution` writes to tasks + ideas + variants in one tx; if those three lived in three separate `_Tx` instances, atomicity would be lost). This is the load-bearing invariant; see §3.2.

4. **Place each method by its driving role, not by which resources it touches.** `_accept_execution` (called from `accept`) writes to tasks + ideas + variants, but it lives in `_TaskOps` because the public op is task-lifecycle. Cross-resource **reads** in helper methods use `self._get_idea` / `self._get_variant` (resolved via MRO from `_StoreCore`'s abstract primitives). The alternative — extract a `_Composite` mixin for cross-resource methods — would fragment the task-lifecycle code and make `accept` harder to read; rejected on readability grounds.

5. **Cross-resource read-side predicates and the experiment-state guard live on `_StoreCore`, not on the resource mixin.** This is the round-0 codex correction to the original draft. `_require_running` (called from 7 task-mixin methods + claim), `_require_idea` (called from 2 task-mixin accept/reject paths + idea-mixin `mark_idea_ready`), `_require_variant` (called from 4 task-mixin accept/reject paths + variant-mixin methods), `_find_starting_variant_for_implement_task` (called from `reclaim` + `_stage_reassign_reclaim`), and the worker/group-spanning `resolve_worker_in_group` (called from `claim`) all live on `_StoreCore`. Reasons: (a) these are pure read-side predicates layered atop the existing `_get_*` abstract primitives — the same place they'd belong if they had been written that way originally; (b) placing them on noun mixins forces `_StoreCore` to grow a parallel set of non-primitive stubs just to satisfy pyright, which makes the "thin core + clean per-resource mixins" story do less work than claimed; (c) the cross-mixin call graph (§3.5) is dense enough that any other placement creates avoidable MRO coupling. `_validate_evaluation` / `validate_evaluation` is the one exception: it references `self._evaluation_schema` (an experiment-construction parameter, not a primitive) and is called only from `_ExperimentOps` + `_TaskOps`; it stays on `_ExperimentOps` and `_TaskOps` reaches it via MRO. See §3.3 for the full placement table after this correction.

6. **Module-level helpers (`_no_op_check_inputs`, `_resolve_trees`, `_sha_equality_message`, `_tree_identity_message`, `_all_parents_equal_sha`, `_validated_update`, `_deep`) stay module-level.** They have no `self`; they're pure functions. They move to `_ops/_helpers.py` (a non-mixin module) so each mixin imports only what it uses.

7. **Disposition doc lists 7 mixins; this plan proposes 7.** Idea / Variant / Task / Event / Experiment / Worker / Group. The disposition doc also floated a separate `_ValidationOps` mixin; **this plan rejects that split** and places each validation helper with its primary consumer (see §3.3). Surfacing as a deliberate divergence from the disposition doc.

8. **Backends untouched.** [`memory.py`](../../reference/packages/eden-storage/src/eden_storage/memory.py), [`sqlite.py`](../../reference/packages/eden-storage/src/eden_storage/sqlite.py), [`postgres.py`](../../reference/packages/eden-storage/src/eden_storage/postgres.py) currently inherit `_StoreBase` and override the backend primitives (`_get_*`, `_iter_*`, `_atomic_operation`, `_apply_commit`, `_get_dispatch_mode`, `_get_experiment`). After the split, they still inherit `_StoreBase`. Zero LOC change to backend files. This is the strongest argument for the "name stays `_StoreBase`" decision.

9. **Single PR, no wave-based merge cadence.** The split is mechanical and behavior-preserving; carving it into multiple PRs would require keeping the codebase in a half-refactored state across PR merges, which (a) violates the project-lifecycle "no backwards-compatibility shims in greenfield" posture from BASE.md and (b) doesn't reduce risk because any single mixin extraction depends on the abstract-base scaffolding from the same PR. The chunk still has internal waves (§9) for ordered execution and per-wave validation gates, but they land in one PR.

10. **`_checkpoint.py` is a special downstream consumer.** It uses `_StoreBase` typing and calls `store._iter_*` / `store._get_*` / `store._apply_commit` / `store._reseed_default_event_counter` directly (see [`_checkpoint.py`](../../reference/packages/eden-storage/src/eden_storage/_checkpoint.py) lines 215, 273, 339, 366, 564, 607, 615). These calls all bind to the **abstract primitives on the core** — which stay on the core after the split. `_checkpoint.py` imports `_StoreBase` for typing; that import keeps working because `_StoreBase` is still defined in `_base.py`.

11. **Naming map is fixed before implementation.** §3.4 enumerates every old method → new mixin. The impl wave executes that map mechanically; deviations are surfaced for review before the impl commit.

## 3. Design

### D.1 Inheritance shape — abstract core + 7 mixins composed at `_StoreBase`

```python
# reference/packages/eden-storage/src/eden_storage/_base.py (post-split)
class _StoreCore:
    """Abstract core. Owns __init__, _Tx, shared helpers, primitive declarations.

    Every backend's primitives (_get_*, _iter_*, _atomic_operation,
    _apply_commit, _get_dispatch_mode, _get_experiment) are declared
    here as raise-NotImplementedError stubs; backends override.
    """
    def __init__(self, experiment_id, *, evaluation_schema=None, now=None,
                 event_id_factory=None, tree_resolver=None) -> None: ...
    # Abstract primitives:
    def _atomic_operation(self): raise NotImplementedError
    def _get_task(self, task_id): raise NotImplementedError
    # ... (all 14 primitives listed in §3.2)
    def _apply_commit(self, tx: _Tx): raise NotImplementedError
    # Shared helpers used by every mixin:
    def _event(self, type_, data) -> Event: ...
    def _ts(self) -> str: ...
    def _maybe_ts(self, value) -> str | None: ...
    def _default_event_id(self) -> str: ...
    def _reseed_default_event_counter(self) -> None: ...
    @property
    def experiment_id(self) -> str: ...


class _StoreBase(  # MRO: leftmost wins on method conflict
    _TaskOpsMixin,
    _IdeaOpsMixin,
    _VariantOpsMixin,
    _EventOpsMixin,
    _ExperimentOpsMixin,
    _WorkerOpsMixin,
    _GroupOpsMixin,
    _StoreCore,
):
    """Composite of every per-resource mixin atop _StoreCore.

    Public surface unchanged from pre-refactor _StoreBase; backends
    keep subclassing this class verbatim.
    """
    pass
```

The mixins all inherit from `_StoreCore` so each mixin's method body can call `self._get_task` / `self._event` / `self._ts` / `self._apply_commit` and have pyright resolve them against `_StoreCore`'s declarations. The composite `_StoreBase` flattens the MRO so every backend continues to see one class with the full method set.

**Why every mixin inherits `_StoreCore`** (option A from the disposition doc, not option B's "plain mixins"): pyright needs to resolve `self._get_idea` from inside `_TaskOpsMixin._insert_execution_task`. With `_StoreCore` as a base, that resolves cleanly; without it, you'd need `Protocol`s or `cast` to keep the type-checker happy, both of which add boilerplate.

**Why `_StoreBase` is the composite** (not "rename `_StoreBase` to `_TaskOpsMixin` and pick another name"): backends import `_StoreBase` and subclass it. Preserving that name avoids touching `memory.py` / `sqlite.py` / `postgres.py`. The disposition doc explicitly carries this convention forward.

**MRO order rationale:** Python's C3 linearization walks the MRO left-to-right; the first class in the list that owns a method wins. None of the mixins override one another's methods (the seams are by resource type), so MRO order is cosmetic for method dispatch — but it does fix the order pyright reports under `_StoreBase.__mro__`. The ordering proposed (Task → Idea → Variant → Event → Experiment → Worker → Group → Core) mirrors the chapter order in `spec/v0/` (chapter 3 task / idea / variant flows first; chapter 8 storage primitives last) and matches the order the disposition doc enumerates.

### D.2 Composite-commit semantics — load-bearing, must survive the split

The atomicity invariant from [`spec/v0/05-event-protocol.md`](../../spec/v0/05-event-protocol.md) §2.2 and [`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) §6.1-§6.3 says: every protocol-owned state mutation that pairs with an event MUST land in the same transaction as the event. The current implementation enforces this via three discipline rules that the refactor MUST NOT break:

1. **Single `_Tx` per public method.** Every public method constructs ONE `_Tx`, stages every related write into its fields, and calls `self._apply_commit(tx)` exactly once. Examples: `_accept_execution` stages into `tx.tasks`, `tx.ideas`, `tx.variants`, `tx.events` — all in one `_Tx`. `reclaim` stages into `tx.tasks`, `tx.task_deletes_submission`, optionally `tx.variants` and `tx.events`. The `_Tx` is the unit of atomicity.

2. **Atomic-operation context wraps the staging.** Every public method opens `with self._atomic_operation():`, performs reads + validations + staging inside, and calls `_apply_commit` before the block exits. `_atomic_operation` is the transaction boundary: in-memory takes the RLock; SQLite issues `BEGIN IMMEDIATE`; Postgres issues `BEGIN ISOLATION LEVEL SERIALIZABLE READ WRITE`. The block's exit commits on no-exception, rolls back on exception.

3. **`_apply_commit` walks every `_Tx` field in a deterministic order.** Each backend's `_apply_commit` is one straight-line function that iterates `tx.tasks`, `tx.ideas`, `tx.variants`, `tx.submissions`, `tx.task_deletes_submission`, `tx.workers`, `tx.worker_credentials`, `tx.worker_deletes`, `tx.groups`, `tx.group_deletes`, `tx.dispatch_mode`, `tx.experiment_state`, `tx.imported_from_update`, `tx.events`. The order is identical across backends (see [`memory.py`](../../reference/packages/eden-storage/src/eden_storage/memory.py) `_apply_commit`, [`sqlite.py`](../../reference/packages/eden-storage/src/eden_storage/sqlite.py) `_apply_commit`, [`postgres.py`](../../reference/packages/eden-storage/src/eden_storage/postgres.py) `_apply_commit`).

The refactor preserves all three by **keeping `_Tx` as a single shared dataclass with the same field list in `_base.py`**, importable by every mixin. No per-mixin `_Tx` variant. No per-resource sub-staging. Each mixin's method ends with `self._apply_commit(tx)`; the call dispatches via MRO to whichever backend the runtime composes (`InMemoryStore`, `SqliteStore`, `PostgresStore`) — and that backend's `_apply_commit` walks the same `_Tx` regardless of which mixin built it.

The abstract primitives that each backend must override stay on `_StoreCore`:

| Primitive | Used by | Backend override |
|---|---|---|
| `_atomic_operation` | every mixin | RLock / BEGIN IMMEDIATE / BEGIN SERIALIZABLE |
| `_apply_commit` | every mixin | dict merge / SQLite upsert / Postgres upsert |
| `_get_task` / `_get_idea` / `_get_variant` / `_get_submission` | TaskOps, IdeaOps, VariantOps, ValidationOps | dict.get / SELECT ... WHERE |
| `_iter_tasks` / `_iter_ideas` / `_iter_variants` / `_iter_events` | TaskOps, IdeaOps, VariantOps, EventOps, _checkpoint | dict iteration / SELECT ... ORDER BY |
| `_get_worker` / `_get_worker_credential_hash` / `_iter_workers` | WorkerOps, also TaskOps (claim-time `_get_worker` registration check) | dict.get / SELECT |
| `_get_group` / `_iter_groups` | GroupOps, also WorkerOps (disjoint-namespace check) | dict.get / SELECT |
| `_get_dispatch_mode` / `_get_experiment` | ExperimentOps, also _checkpoint | dict / SELECT |

Note the cross-mixin primitive uses: `_TaskOpsMixin.claim` calls `self._get_worker` (a worker-mixin concern). With `_StoreCore` declaring `_get_worker`, this works without a cyclic import. The mixins do NOT import each other; they all import `_StoreCore` and `_Tx`.

### D.3 Where each validation helper lives — single ValidationOps mixin rejected

The disposition doc floated a separate `_ValidationOps` mixin for the `_validate_*` helpers (`_validate_evaluation`, `_validate_ideation_acceptance`, `_validate_execution_acceptance`, `_validate_evaluate_acceptance`, `_validate_evaluate_error`, `_validate_non_no_op_variant`, `_validate_submission_ref_binding`). This plan rejects that split. Each validation helper is consumed by one or two methods in one mixin; co-locating the validation with its consumer keeps the call sites short and avoids a mixin whose membership is "everything whose name starts with `_validate_`" (membership-by-naming is a code smell).

Final placement:

| Helper | Mixin | Rationale |
|---|---|---|
| `_validate_ideation_acceptance` | `_TaskOps` | called from `_validate_acceptance_locked` (TaskOps) |
| `_validate_execution_acceptance` | `_TaskOps` | gates `_accept_execution` (TaskOps) |
| `_validate_evaluate_acceptance` | `_TaskOps` | gates `_accept_evaluation` (TaskOps) |
| `_validate_evaluate_error` | `_TaskOps` | gates `validate_terminal` (TaskOps) |
| `_validate_non_no_op_variant` | `_TaskOps` | called from `submit` (TaskOps); concerns variant-side spec rule but lives at the submit gate |
| `_validate_submission_ref_binding` | `_TaskOps` | called from `submit` (TaskOps) |
| `_validate_evaluation` / `validate_evaluation` | `_ExperimentOps` | references `self._evaluation_schema` (experiment-scoped); also called from `_validate_evaluate_acceptance`/`_validate_evaluate_error` via MRO (no cyclic import) |

The cross-mixin call from `_TaskOps._validate_evaluate_acceptance` → `self._validate_evaluation` (in `_ExperimentOps`) is fine: both mixins are sibling subclasses of `_StoreCore` and both end up on `_StoreBase`'s MRO; `self._validate_evaluation` resolves via the composite.

### D.4 Cross-resource helper-dependency matrix

Round-0 codex correctly flagged that the original draft hadn't enumerated which helpers cross which seams. The matrix below settles the question. Every row is a "this method calls that helper across a proposed seam" pair; the resolution column shows where the helper actually ends up (most go on `_StoreCore`, per decision #5 in §2).

| Helper | Defined at line (current) | Callers (mixin / method) | Placement after refactor |
|---|---:|---|---|
| `_require_running` | [`_base.py:1516`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | TaskOps (`_insert_ideation_task`, `_insert_execution_task`, `_insert_evaluation_task`, `create_ideation_task`, `create_execution_task`, `create_evaluation_task`, `claim`); ExperimentOps would otherwise be sole owner | `_StoreCore` |
| `_require_idea` | [`_base.py:2259`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | IdeaOps (`mark_idea_ready`); TaskOps (`_accept_execution`, `_reject_execution`) | `_StoreCore` |
| `_require_variant` | [`_base.py:2265`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | VariantOps (`declare_variant_evaluation_error`, `integrate_variant`); TaskOps (`_accept_execution`, `_accept_evaluation`, `_reject_evaluate`) | `_StoreCore` |
| `_require_task` | [`_base.py:2253`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | TaskOps only | `_StoreCore` (for symmetry with the other `_require_*`) |
| `_require_no_task` | [`_base.py:2249`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | TaskOps only | `_StoreCore` (symmetry) |
| `_find_starting_variant_for_implement_task` | [`_base.py:2189`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | TaskOps (`reclaim`, `_stage_reassign_reclaim`, `_reject_execution`); VariantOps does not call it | `_StoreCore` (the helper is a `_iter_variants` walk + filter, so it's a layered read primitive) |
| `_validate_evaluation` / `validate_evaluation` | [`_base.py:2143`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | ExperimentOps (public `validate_evaluation` re-export); TaskOps (`_validate_evaluate_acceptance`, `_validate_evaluate_error`, `_reject_evaluate`) | `_ExperimentOps` — exception case; references `self._evaluation_schema` which is set up in `__init__`. TaskOps' call sites resolve via MRO. |
| `resolve_worker_in_group` | [`_base.py:2519`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | GroupOps (public); TaskOps (`claim` for group-target eligibility) | `_GroupOps` — the helper is intrinsically group-resolution code (DFS over group DAG). TaskOps' call site resolves via MRO. |
| `_validate_registry_id` | [`_base.py:2562`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | WorkerOps (worker register / reissue / verify); GroupOps (group register, member add); TaskOps (`reassign_task` for `reassigned_by`); ExperimentOps (`terminate_experiment` for `terminated_by`, `update_dispatch_mode` for `updated_by`) | `_StoreCore` — the helper is a regex check, no state; widely called |
| `_event` | [`_base.py:2681`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | every mixin | `_StoreCore` (already named in original plan) |
| `_ts` / `_maybe_ts` | [`_base.py:2690-2707`](../../reference/packages/eden-storage/src/eden_storage/_base.py) | every mixin | `_StoreCore` (already named in original plan) |

Net effect of the matrix:

- `_StoreCore` carries: `__init__`, `_Tx`-aware abstract primitives, `_event` / `_ts` / `_maybe_ts`, plus the read-side predicates `_require_running` / `_require_task` / `_require_no_task` / `_require_idea` / `_require_variant` / `_find_starting_variant_for_implement_task` / `_validate_registry_id`. Adding the predicates is ~30 SLOC over the original projection (~150 → ~180 SLOC). Still well under any threshold.
- The seven noun mixins own only the **public ops + private helpers exclusive to them** plus their `_require_*` consumers via MRO. No mixin needs to declare a "stub" for a sibling-mixin's helper, because every cross-seam helper lives on `_StoreCore`.
- The two exceptions (`_validate_evaluation` on ExperimentOps, `resolve_worker_in_group` on GroupOps) are intentionally on the noun mixin: each is intrinsically that resource's concern even though one cross-mixin caller exists. The MRO-resolved call site for the cross-mixin caller is a one-line `self.METHOD(...)` and pyright resolves it via the composite's MRO.

### D.5 Naming map — old method → new mixin

Every method in [`reference/packages/eden-storage/src/eden_storage/_base.py`](../../reference/packages/eden-storage/src/eden_storage/_base.py) gets exactly one new home. The order is alphabetical-within-mixin so the map is searchable. Placement reflects the §D.4 dependency-matrix correction: read-side predicates that are called across mixins live on `_StoreCore`.

**`_StoreCore`** (in `_base.py`; ~180 SLOC):

- `__init__`
- `experiment_id` (property)
- `_default_event_id`
- `_reseed_default_event_counter` (with `__func__` comparison updated to `_StoreCore._default_event_id` — see decision #1)
- `_event`
- `_ts`
- `_maybe_ts`
- `_require_running`, `_require_task`, `_require_no_task`, `_require_idea`, `_require_variant`, `_find_starting_variant_for_implement_task`, `_validate_registry_id` (per §D.4)
- abstract primitives: `_atomic_operation`, `_get_task`, `_get_idea`, `_get_variant`, `_get_submission`, `_iter_tasks`, `_iter_ideas`, `_iter_variants`, `_iter_events`, `_get_worker`, `_get_worker_credential_hash`, `_iter_workers`, `_get_group`, `_iter_groups`, `_get_dispatch_mode`, `_get_experiment`, `_apply_commit`

**`_TaskOpsMixin`** (`_ops/tasks.py`; estimated 600-750 SLOC; fallback split if > 800 — see §3.7):

- public: `read_task`, `read_submission`, `list_tasks`, `create_task`, `create_ideation_task`, `create_execution_task`, `create_evaluation_task`, `claim`, `submit`, `accept`, `reject`, `reclaim`, `reassign_task`, `validate_acceptance`, `validate_terminal`
- private (composite-commit / dispatch / validation): `_insert_ideation_task`, `_insert_execution_task`, `_insert_evaluation_task`, `_accept_ideation`, `_accept_execution`, `_accept_evaluation`, `_reject_ideation`, `_reject_execution`, `_reject_evaluate`, `_validate_acceptance_locked`, `_validate_ideation_acceptance`, `_validate_execution_acceptance`, `_validate_evaluate_acceptance`, `_validate_evaluate_error`, `_validate_non_no_op_variant`, `_validate_submission_ref_binding`, `_stage_reassign_reclaim`, `_reassign_event_payload`, `_targets_equal`, `_require_submission_kind_matches`, `_require_no_live_execution_task_for_idea`, `_require_no_live_evaluation_task_for_variant`
- L-O sub-refactor target: `reassign_task` (LEN=122) — split per-state branches (pending vs claimed) into helpers so the public method stays under 100 LEN. See §3.6.

**`_IdeaOpsMixin`** (`_ops/ideas.py`; estimated 80-120 SLOC):

- public: `read_idea`, `list_ideas`, `create_idea`, `mark_idea_ready`

**`_VariantOpsMixin`** (`_ops/variants.py`; estimated 180-230 SLOC):

- public: `read_variant`, `list_variants`, `create_variant`, `declare_variant_evaluation_error`, `integrate_variant`

**`_EventOpsMixin`** (`_ops/events.py`; estimated 80-120 SLOC):

- public: `events`, `replay`, `read_range`

**`_ExperimentOpsMixin`** (`_ops/experiment.py`; estimated 250-300 SLOC):

- public: `read_experiment`, `read_experiment_state`, `update_experiment_state`, `terminate_experiment`, `emit_policy_error`, `read_dispatch_mode`, `update_dispatch_mode`, `validate_evaluation`, `export_checkpoint`, `import_checkpoint`
- private: `_validate_evaluation`

**`_WorkerOpsMixin`** (`_ops/workers.py`; estimated 250-300 SLOC):

- public: `register_worker`, `reissue_credential`, `verify_worker_credential`, `read_worker`, `list_workers`
- private: `_generate_credential_token`, `_hash_credential`, `_check_credential_hash`
- class constants used only by worker auth: `_PASSWORD_HASHER`, `_UNKNOWN_WORKER_DUMMY_HASH`

**`_GroupOpsMixin`** (`_ops/groups.py`; estimated 200-250 SLOC):

- public: `register_group`, `add_to_group`, `remove_from_group`, `delete_group`, `read_group`, `list_groups`, `resolve_worker_in_group`
- private: `_require_no_cycle_after`

**Module-level non-mixin helpers** in `_ops/_helpers.py` (estimated 80-120 SLOC):

- `_validated_update`, `_deep` (used everywhere; pure functions)
- `_no_op_check_inputs`, `_all_parents_equal_sha`, `_resolve_trees`, `_sha_equality_message`, `_tree_identity_message` (used only by `_TaskOpsMixin._validate_non_no_op_variant`)

The `_no_op_*` helpers could equally live as a private section inside `_ops/tasks.py`; placing them in `_ops/_helpers.py` keeps the task-mixin file leaner. **Operator decision needed** (default: leave them in `_ops/_helpers.py`).

**Module constants** stay in `_base.py`: `RESERVED_IDENTIFIERS`, `_WORKER_ID_RE`, `_LIVE_TASK_STATES`, `_DEFAULT_DISPATCH_MODE`, `_DEFAULT_EXPERIMENT_STATE`, `_METRIC_PY_TYPES`. The backend files (`memory.py`, `sqlite.py`, `postgres.py`) already import `_DEFAULT_DISPATCH_MODE` + `_DEFAULT_EXPERIMENT_STATE` from `_base`; that import keeps working.

The module-level `iter_events_by_type` (test convenience at the very bottom of `_base.py`) moves to `_ops/events.py` next to `_EventOpsMixin`. **`[__init__.py](../../reference/packages/eden-storage/src/eden_storage/__init__.py)` becomes an in-scope edit**: it currently re-exports `iter_events_by_type` from `._base` (line 10); the impl PR updates the import to `from ._ops.events import iter_events_by_type` so the public re-export at the package surface is preserved. This is the only `__init__.py` change required by the refactor; all other re-exports (`Store`, `InMemoryStore`, `SqliteStore`, `PostgresStore`, etc.) flow through unchanged paths.

### D.6 L-O sub-refactor: `reassign_task` length split

Current shape (lines 1236-1302 in `_base.py`): one public method with a docstring + branches by task state (pending vs claimed) + composite-commit staging. LEN=122 (mostly docstring + 40 lines of body); the LEN gate trips on total span including docstring.

Two ways to land under the 100-LEN threshold after the mixin move:

(a) **Shrink the docstring.** Move the long contract description into the chapter-4 cross-reference plus a one-paragraph summary; details live in the spec already.

(b) **Extract per-state helpers.** `_reassign_pending(task, new_target, reason, reassigned_by)` + `_reassign_claimed(task, new_target, reason, reassigned_by)` — the public method dispatches.

This plan picks **(a) + light (b)**: shrink the docstring to ~15 lines and extract `_reassign_pending_or_claimed(task, new_target, now, tx)` for the body's per-state staging. Both changes are mechanical and behavior-preserving. Final LEN should land in the 60-80 range. Codex-review will spot any inadvertent semantic drift.

The plan does NOT take an aggressive splitting approach (one helper per state) because the current code is already factored — `_stage_reassign_reclaim` is the claimed-state composite helper — and over-splitting would make `reassign_task` harder to read for the sake of the LEN gate.

### D.7 `_TaskOpsMixin` size fallback

The estimate for `_TaskOpsMixin` is 600-750 SLOC after extraction. Task create/claim/submit/accept/reject/reclaim/reassign + their validators currently dominate `_base.py` — major method blocks at lines 650 (insert_task family), 875 (claim/submit), 1726 (accept/reject dispatch), 2199 (live-task guards). If the actual extraction lands the mixin over 800 SLOC, the impl PR MUST split `_TaskOpsMixin` along the create-vs-lifecycle line **in the same PR**, not as a follow-up:

- `_TaskCreateOpsMixin` (`_ops/tasks_create.py`; estimated 250-350 SLOC): `create_task`, `_insert_ideation_task`, `_insert_execution_task`, `_insert_evaluation_task`, `create_ideation_task`, `create_execution_task`, `create_evaluation_task`, `_require_no_live_execution_task_for_idea`, `_require_no_live_evaluation_task_for_variant`.
- `_TaskLifecycleOpsMixin` (`_ops/tasks_lifecycle.py`; estimated 350-450 SLOC): `claim`, `submit`, `accept`, `reject`, `reclaim`, `reassign_task`, `validate_acceptance`, `validate_terminal`, `_accept_*`, `_reject_*`, `_validate_acceptance_locked`, `_validate_*_acceptance`, `_validate_evaluate_error`, `_validate_non_no_op_variant`, `_validate_submission_ref_binding`, `_stage_reassign_reclaim`, `_reassign_event_payload`, `_targets_equal`, `_require_submission_kind_matches`, `read_task`, `read_submission`, `list_tasks` (the read APIs naturally live with the lifecycle code that produces the state they observe).

`_StoreBase` then composes 8 mixins instead of 7. **Operator decision needed at impl time** (not now): which split to keep if both happen to land under 800 SLOC. Default: keep the single `_TaskOpsMixin` unless it actually crosses the threshold; over-splitting prophylactically violates the §2 principle that the chunk's correctness story is "behavior-preserving file movement, not architecture redesign". Surfacing as an enforceable fallback so the impl PR doesn't get stuck if the estimate is wrong.

### D.8 `_checkpoint.py` interactions

[`_checkpoint.py`](../../reference/packages/eden-storage/src/eden_storage/_checkpoint.py) is a single-module sidecar that does bulk export/import of store state for the 12b portable-checkpoint feature. It interacts with the store through:

- Type imports: `from ._base import _StoreBase, _Tx, _DEFAULT_DISPATCH_MODE` — survives unchanged; `_StoreBase` is still the composite in `_base.py`.
- Direct calls to **abstract primitives** on the store: `store._iter_tasks`, `store._iter_ideas`, `store._iter_variants`, `store._iter_events`, `store._iter_workers`, `store._iter_groups`, `store._get_submission`, `store._get_dispatch_mode`, `store._get_experiment`, `store._atomic_operation`, `store._apply_commit`, `store._reseed_default_event_counter`. All of these live on `_StoreCore` after the split; the calls keep resolving via MRO.

`_checkpoint.py` does not need to change. The plan calls it out as a verification gate (the existing `test_checkpoint_storage.py` suite covers it).

### D.9 Test strategy

The existing [`conftest.py`](../../reference/packages/eden-storage/tests/conftest.py) parametrizes every test that takes the `make_store` fixture across `memory`, `sqlite`, and `postgres` backends. Every test under [`reference/packages/eden-storage/tests/`](../../reference/packages/eden-storage/tests/) exercises the public `Store` Protocol surface. After the split, **the same tests run unchanged** against `InMemoryStore`, `SqliteStore`, `PostgresStore` — which still subclass `_StoreBase` (now a composite). Pass/fail is the verification gate.

The plan does NOT add per-mixin unit tests. Reasons: (a) mixins are not standalone — they require `_StoreCore`'s primitives at runtime; (b) the existing Protocol-level tests are the conformance contract that matters; (c) adding mixin-level tests would create a second source of truth that drifts.

**Conformance suite + downstream services:** the v1+roles conformance suite (`uv run pytest -q conformance/`) drives the reference impl end-to-end through the wire surface. It runs unchanged. The orchestrator's e2e tests ([`reference/services/orchestrator/tests/test_e2e.py`](../../reference/services/orchestrator/tests/test_e2e.py) and [`test_subprocess_e2e.py`](../../reference/services/orchestrator/tests/test_subprocess_e2e.py)) spawn real `InMemoryStore` / `SqliteStore` instances through the task-store-server; they also run unchanged.

**Postgres-backed tests:** require `EDEN_TEST_POSTGRES_DSN` set. CI sets it in the `python-test-postgres` job; locally, the `make_store[postgres]` parametrization skips when unset. The plan's verification gates include both forms.

### D.10 Imports + circular-import risk

The mixin files all import `_StoreCore` and `_Tx` from `_base.py`. `_base.py` imports the mixins to build `_StoreBase`. This is fine — Python's import order resolves the cycle because `_base.py` imports the mixins at the **bottom** of the file (after `_StoreCore` is defined), and the mixin files only need `_StoreCore` / `_Tx` (already defined by the time the import runs).

Suggested import order in `_base.py`:

```python
# top of file
from .submissions import ...
# ...
@dataclass
class _Tx: ...

class _StoreCore: ...

# bottom of file
from ._ops.tasks import _TaskOpsMixin
from ._ops.ideas import _IdeaOpsMixin
# ... etc

class _StoreBase(
    _TaskOpsMixin, _IdeaOpsMixin, _VariantOpsMixin, _EventOpsMixin,
    _ExperimentOpsMixin, _WorkerOpsMixin, _GroupOpsMixin, _StoreCore,
):
    pass
```

The mixin imports are at the bottom so the mixin files can `from .._base import _StoreCore, _Tx` at their top without circularity.

Alternative: extract `_StoreCore` to `_ops/_core.py` so the import dependency runs one direction (mixins → `_core.py`; `_base.py` → mixins). **Operator decision needed** (default: keep `_StoreCore` in `_base.py` and rely on the bottom-import trick; `_StoreCore` is only ~150 SLOC and pairing it with the composite reads naturally). Surfacing as a divergence the codex-review pass should challenge.

## 4. Scope

### 4.A In scope

- Create `reference/packages/eden-storage/src/eden_storage/_ops/` directory with `__init__.py` + 7 mixin files + `_helpers.py`.
- Move every method from `_base.py:_StoreBase` to its new mixin per §3.4.
- Reshape `_base.py` to contain `_StoreCore`, `_Tx`, module constants + helpers, and the composite `_StoreBase(*mixins, _StoreCore)`.
- Apply L-O sub-refactor (`reassign_task` LEN reduction) inside `_TaskOpsMixin`.
- Remove `# slop-allow-file: F-1 _StoreBase mixin split deferred to issue #114` from `_base.py:1`.
- Update [`docs/audits/2026-05-20-phase-c-disposition.md`](../audits/2026-05-20-phase-c-disposition.md) §1 F-1 row to mark "REFACTOR (landed in PR #N)".
- Update [`CHANGELOG.md`](../../CHANGELOG.md) with a chunk-completion entry.
- Update [`docs/roadmap.md`](../roadmap.md) — F-1 is not in the chunk lineage today (it's a follow-up issue from a chunk-A audit); the roadmap section may not even need an entry. If it does, the planless one-liner shape applies.

### 4.B Out of scope

- Spec edits (any chapter under `spec/v0/`).
- Schema edits (`spec/v0/schemas/*.json`) or Pydantic binding changes.
- Wire protocol changes ([`reference/packages/eden-wire/`](../../reference/packages/eden-wire/)).
- Conformance test additions or edits ([`conformance/`](../../conformance/)).
- Public `Store` Protocol changes ([`protocol.py`](../../reference/packages/eden-storage/src/eden_storage/protocol.py)).
- Backend file changes ([`memory.py`](../../reference/packages/eden-storage/src/eden_storage/memory.py), [`sqlite.py`](../../reference/packages/eden-storage/src/eden_storage/sqlite.py), [`postgres.py`](../../reference/packages/eden-storage/src/eden_storage/postgres.py)). The backends inherit `_StoreBase` and override primitives — both interfaces stay byte-identical.
- L-4 (`_validate_non_no_op_variant` CC=17) and any other CC-improvement that's not directly required to drop a `# slop-allow:` annotation.
- F-3 ([`eden_wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py) APIRouter regroup, issue #115) and F-4 ([`eden_wire/client.py`](../../reference/packages/eden-wire/src/eden_wire/client.py) per-resource split, issue #116) — sibling Phase C refactors, separate plans.

### 4.C Recovery posture

- The refactor lands in one PR, behind a feature-disabled (no-flag) change. If post-merge any regression surfaces, the recovery is `git revert <merge-sha>` — a clean revert restores the byte-identical pre-refactor state.
- During impl, every wave's verification gate must pass before the next wave starts (see §8). A failure mid-impl is recoverable by rolling back the wave's commit on the local branch; nothing is observable upstream until the PR opens.

## 5. Files to touch

| Path | Action | Estimated post-state SLOC |
|---|---|---:|
| `reference/packages/eden-storage/src/eden_storage/_base.py` | rewrite (extract mixin contents; keep `_StoreCore`, `_Tx`, constants, helpers, composite `_StoreBase`) | ~150-250 |
| `reference/packages/eden-storage/src/eden_storage/_ops/__init__.py` | new (empty re-export aggregator if anything needs cross-import; otherwise empty) | ~10 |
| `reference/packages/eden-storage/src/eden_storage/_ops/tasks.py` | new (`_TaskOpsMixin` + helpers; or split into `tasks_create.py` + `tasks_lifecycle.py` per §3.7 fallback) | ~600-750 |
| `reference/packages/eden-storage/src/eden_storage/_ops/ideas.py` | new (`_IdeaOpsMixin`) | ~100-150 |
| `reference/packages/eden-storage/src/eden_storage/_ops/variants.py` | new (`_VariantOpsMixin`) | ~200-250 |
| `reference/packages/eden-storage/src/eden_storage/_ops/events.py` | new (`_EventOpsMixin` + `iter_events_by_type`) | ~80-120 |
| `reference/packages/eden-storage/src/eden_storage/_ops/experiment.py` | new (`_ExperimentOpsMixin`) | ~250-300 |
| `reference/packages/eden-storage/src/eden_storage/_ops/workers.py` | new (`_WorkerOpsMixin`) | ~250-300 |
| `reference/packages/eden-storage/src/eden_storage/_ops/groups.py` | new (`_GroupOpsMixin`) | ~200-250 |
| `reference/packages/eden-storage/src/eden_storage/_ops/_helpers.py` | new (`_validated_update`, `_deep`, `_no_op_*`, `_resolve_trees`) | ~80-120 |
| `reference/packages/eden-storage/src/eden_storage/memory.py` | unchanged | (unchanged) |
| `reference/packages/eden-storage/src/eden_storage/sqlite.py` | unchanged | (unchanged) |
| `reference/packages/eden-storage/src/eden_storage/postgres.py` | unchanged | (unchanged) |
| `reference/packages/eden-storage/src/eden_storage/_checkpoint.py` | unchanged (verifies abstract-primitive contract still resolves) | (unchanged) |
| `reference/packages/eden-storage/src/eden_storage/protocol.py` | unchanged | (unchanged) |
| `reference/packages/eden-storage/src/eden_storage/__init__.py` | rewire `iter_events_by_type` re-export from `._base` → `._ops.events` | (small edit) |
| `reference/packages/eden-storage/tests/*.py` | unchanged | (unchanged) |
| `conformance/scenarios/*.py` | unchanged | (unchanged) |
| `docs/audits/2026-05-20-phase-c-disposition.md` | annotate F-1 row "REFACTOR landed in PR #N" | (small edit) |
| `CHANGELOG.md` | append chunk-completion entry | (small edit) |
| `docs/roadmap.md` | (planless one-liner — see §1.3 of AGENTS.md) | (small edit) |
| `docs/plans/refactor-f1-storebase-split.md` | this file | (this file) |
| `docs/plans/review/refactor-f1-storebase-split/plan/<ts>/` | codex-review record (plan stage) | (new dir) |
| `docs/plans/review/refactor-f1-storebase-split/impl/<ts>/` | codex-review record (impl stage) | (new dir) |

Total: 9 new files, 1 substantial rewrite, 3 small doc edits.

## 6. Test design

No new tests. The plan validates against the existing test surface:

- **Per-backend parametrized suite** at [`reference/packages/eden-storage/tests/`](../../reference/packages/eden-storage/tests/): 17 test files, every `make_store` fixture runs across `memory` / `sqlite` / `postgres`. Pre- and post-refactor pass-fail must be byte-identical.
- **Schema parity check** ([`tests/test_schema_parity.py`](../../reference/packages/eden-contracts/tests/test_schema_parity.py) in `eden-contracts/`): unaffected by storage internals, but in the full-pytest gate.
- **Conformance suite** (`uv run pytest -q conformance/`): runs the chapter-9 §5 v1+roles scenarios against `InMemoryStore` via the wire surface. Same as today.
- **Postgres-backed tests** via `EDEN_TEST_POSTGRES_DSN=postgresql://eden:eden@localhost:5432/eden uv run pytest -q reference/packages/eden-storage/tests` — verifies the SQLite ↔ Postgres parametrization survives.
- **Compose smoke** (`bash reference/compose/healthcheck/smoke.sh`): drives the full stack with real services hitting `task-store-server` which uses `SqliteStore` / `PostgresStore` depending on `--store-url`. Catches anything the unit suite misses (e.g., MRO surprises in import order at service-launch time).
- **Orchestrator e2e** (`uv run pytest -q reference/services/orchestrator/tests/`): subprocess-mode end-to-end against the real reference store binaries.

The plan does NOT introduce mixin-level unit tests; see §3.7 for the rationale.

**Pre-flight assertion**: add a one-time sanity check in `_base.py` or in a single new test that walks `_StoreBase.__mro__` and confirms it contains every mixin + `_StoreCore` in the expected order. This catches MRO surprises early (e.g., someone reorders the bases later). Implementation: tiny — `assert _StoreBase.__mro__[1:8] == (_TaskOpsMixin, _IdeaOpsMixin, _VariantOpsMixin, _EventOpsMixin, _ExperimentOpsMixin, _WorkerOpsMixin, _GroupOpsMixin)` — but value is high. **Operator decision needed**: in `_base.py` (asserted on module import) or in a new `test_mro_shape.py` (asserted at test time)? Default: assert at module-import time so a future refactor that reshuffles MRO fails loud on first import, not after the test suite runs.

## 7. Verification gates (run before commit AND before push)

Per AGENTS.md "literal pre-push validation gate" rule. After every wave:

1. `uv run ruff check .` — lint (must pass)
2. `uv run pyright` — type-check (must pass)
3. `uv run pytest -q reference/packages/eden-storage/tests` — storage suite (must pass; memory + sqlite; postgres parametrization auto-skips locally)
4. `python3 scripts/check-complexity.py` — Tier-1 complexity gate (must pass with `_base.py`'s `# slop-allow-file:` removed)

Before push:

1. `EDEN_TEST_POSTGRES_DSN=postgresql://eden:eden@localhost:5432/eden uv run pytest -q reference/packages/eden-storage/tests` — exercises the postgres parametrization (must pass)
2. `uv run pytest -q` — full reference test suite (must pass)
3. `uv run pytest -q conformance/` — conformance suite (must pass)
4. `bash reference/compose/healthcheck/smoke.sh` — Compose smoke (must pass)
5. `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` — markdownlint over the plan doc + any doc edits

Optional (sanity, not required):

1. `uvx radon raw reference/packages/eden-storage/src/eden_storage/_base.py reference/packages/eden-storage/src/eden_storage/_ops/*.py` — confirms post-refactor SLOC distribution matches §5's table within ±10%.

## 8. Tricky areas

### 8.1 MRO surprises

Python's C3 linearization is deterministic but the surprise mode is **diamond-inherited helpers**. None of the mixins override one another's methods today, so there's no actual conflict — but a future change that adds, say, `_TaskOpsMixin._validate_evaluation` (shadowing `_ExperimentOpsMixin._validate_evaluation`) would silently win because TaskOps is left of ExperimentOps in the MRO. **Mitigation**: the module-load-time MRO assertion (§6) catches a base-list reorder; pyright catches a name shadow if both mixins inherit from `_StoreCore` (pyright reports duplicate definitions). Doc comment on `_StoreBase`'s class definition reminds the next reader that the ordering matters.

### 8.2 Pyright resolution against the abstract base

Each mixin's method body calls `self._get_idea`, `self._event`, `self._apply_commit`, etc. — methods declared on `_StoreCore`. pyright resolves these via the mixin's `_StoreCore` base, but only if the mixin actually inherits from `_StoreCore` (option A from §3.1, not option B). The plan goes with option A explicitly to dodge this.

**Risk**: if a mixin's method calls a sibling-mixin method (e.g., `_TaskOps._validate_evaluate_acceptance` calls `self._validate_evaluation` defined on `_ExperimentOps`), pyright can't resolve it via the mixin's `_StoreCore` base. **Mitigation options**:

(a) Declare a forward-declared `_validate_evaluation` stub on `_StoreCore` (consistent with how the abstract primitives are declared).
(b) Use `cast(StoreCoreProtocol, self)` at the call site (Protocol-based escape hatch).
(c) Let pyright report "unknown attribute" and add a `# type: ignore` at the call site.

The plan picks (a) — declare the validation helpers as abstract stubs on `_StoreCore` that the validation mixin overrides. Symmetric with how the read primitives are declared. **Operator decision needed** if a different option is preferred (default: (a)).

### 8.3 SQLite-vs-Postgres `_apply_commit` divergence

[`sqlite.py`](../../reference/packages/eden-storage/src/eden_storage/sqlite.py) and [`postgres.py`](../../reference/packages/eden-storage/src/eden_storage/postgres.py) each have their own `_apply_commit` (~60 lines each). They look nearly-identical but differ in (a) parameter binding (`?` vs `%s`); (b) cursor handling (`self._conn.execute` vs `with self._conn.cursor() as cur`); (c) idiomatic OnConflict syntax. The audit's M-4 recommendation was to extract the `_Tx` walk into `_base.py` with backend-supplied per-row hooks; **the F-1 refactor does NOT take that on**. The reason: M-4 is a behavior-preserving refactor of `_apply_commit` itself, separate from the mixin split. Bundling it would (a) double the diff size; (b) double the cognitive load on codex-review; (c) increase the risk that a behavior-preservation regression goes unnoticed. M-4 is a clean follow-up issue if anyone wants it.

### 8.4 `_PASSWORD_HASHER` / `_UNKNOWN_WORKER_DUMMY_HASH` class-level state

The two class-level constants currently live on `_StoreBase` (lines 2593-2599). They're used only by `verify_worker_credential` / `_check_credential_hash` / `_hash_credential`. After the move to `_WorkerOpsMixin`, they sit on the mixin. The runtime cost of `_UNKNOWN_WORKER_DUMMY_HASH = _PASSWORD_HASHER.hash("...")` (argon2id KDF, ~few hundred ms with FULL params) runs once at module load — `eden_storage._ops.workers` imports → mixin class body executes → constant computed. Pre-refactor it runs at `_base.py` import; post-refactor at `_ops/workers.py` import. Both happen at the same time (the package's `__init__.py` re-exports `_StoreBase` which imports the mixins). Net: identical startup cost.

### 8.5 Postgres `ensure_readonly_role` and friends stay in `postgres.py`

The 12a-1f readonly Postgres role provisioning (`ensure_readonly_role`, `_provision_readonly_role_in_txn`, `_ensure_role_attrs`, `_scrub_role_memberships`, `_revoke_all_privileges`, `_grant_readonly_safe_set`, `_scalar`, the `_READONLY_GRANT_TABLES` constant) lives in [`postgres.py`](../../reference/packages/eden-storage/src/eden_storage/postgres.py). It's Postgres-binding-specific (psycopg + SQL); it does NOT belong in any mixin. The refactor leaves it where it is.

### 8.6 The `from ._checkpoint import ...` deferred-import pattern in `export_checkpoint` / `import_checkpoint`

The current code does the import inside the method body (lines 2730, 2758) to dodge a circular import (`_checkpoint.py` imports `_StoreBase`; `_StoreBase` exposing `export_checkpoint`/`import_checkpoint` would close the cycle). After the move to `_ExperimentOpsMixin`, the same deferred-import pattern continues to work — the mixin's `export_checkpoint` body still does `from .._checkpoint import export_checkpoint as _export`. Verify: the relative import path becomes `.._checkpoint` (one level up from `_ops/`).

### 8.7 Wave-edit safety: keep `_StoreBase` byte-coverage-equivalent

The impl proceeds wave-by-wave, extracting one mixin at a time. Between waves the file `_base.py` is in a transitional state — some methods extracted, others still on `_StoreBase`. The composite-commit invariant remains intact because every method still goes through `with self._atomic_operation():` + a single `_Tx` + `self._apply_commit(tx)`. **Anti-pattern to avoid**: extracting half a method's body into a mixin while the other half stays on `_StoreBase`. Every extraction moves a method whole.

### 8.8 The `# slop-allow-file:` removal must happen on the last wave

The `# slop-allow-file: F-1` annotation gates the complexity check on `_base.py`. If the annotation is removed before `_base.py` is actually under 800 SLOC, the wave's verification gate (`scripts/check-complexity.py`) fails. The plan removes the annotation in the final wave (the docs/PR wave), after all the mixin extractions have completed and the file is provably under threshold.

### 8.9 Cherry-pick contamination

Per AGENTS.md "Cherry-pick contamination from dev-only workarounds": if any of the wave commits land iteratively, a future rebase / cherry-pick that grabs only some waves leaves the codebase in a half-refactored state. **Mitigation**: every wave commit's title carries the `Phase F-1 wave N: ...` prefix so a partial cherry-pick is visible; the squash-merge default for the PR turns the whole sequence into one commit on `main`.

## 9. Wave plan

The impl runs as 5 internal waves in one PR. Each wave is one commit; each wave's verification gate must pass before the next wave starts.

### Wave 1 — Scaffolding (one commit)

- Create `reference/packages/eden-storage/src/eden_storage/_ops/__init__.py` (empty placeholder).
- Create `reference/packages/eden-storage/src/eden_storage/_ops/_helpers.py` and move `_validated_update`, `_deep`, `_no_op_check_inputs`, `_all_parents_equal_sha`, `_resolve_trees`, `_sha_equality_message`, `_tree_identity_message` from `_base.py` into it.
- Update `_base.py` to import these helpers from `_ops._helpers` (re-export at module scope for backward compat with any external callers — if any exist, which a `git grep _validated_update reference/` should confirm before the wave starts).
- Add `_StoreCore` class at the top of `_base.py` carrying `__init__`, abstract primitives, `_event`, `_ts`, `_maybe_ts`, `_default_event_id`, `_reseed_default_event_counter`, `experiment_id` property.
- Reshape `_StoreBase` to `class _StoreBase(_StoreCore): pass` initially, with every method still inline on `_StoreBase` for now. **The mixin extraction is wave-2 onward**; this wave just stands up the inheritance scaffold.
- Verification gate: full pytest passes. `_base.py` still > 800 SLOC; the `# slop-allow-file:` stays.

### Wave 2 — Extract Worker + Group mixins (one commit)

- Create `_ops/workers.py` with `_WorkerOpsMixin(_StoreCore)` containing the methods listed in §3.4.
- Create `_ops/groups.py` with `_GroupOpsMixin(_StoreCore)` containing the methods listed in §3.4.
- Update `_base.py`: `class _StoreBase(_WorkerOpsMixin, _GroupOpsMixin, _StoreCore): ...` (the rest of `_StoreBase`'s methods stay inline for now).
- Verification gate: full pytest passes; per-mixin SLOC matches §5 table within ±15%.

Reason for ordering Worker + Group first: they have the smallest cross-mixin coupling (Worker reads Group for disjoint-namespace check; Group reads Worker symmetrically; otherwise self-contained). Extracting them first validates the inheritance shape without entangling the larger task/idea/variant operations.

### Wave 3 — Extract Idea + Variant + Event + Experiment mixins (one commit)

- Create `_ops/ideas.py`, `_ops/variants.py`, `_ops/events.py`, `_ops/experiment.py`.
- Move methods per §3.4. `_ExperimentOpsMixin` owns `validate_evaluation` + `_validate_evaluation` (the schema is experiment-scoped).
- Update `_base.py`: the composite gets all 6 mixins so far; the rest of `_StoreBase`'s methods (all task ops) stay inline.
- Verification gate: full pytest passes; cross-mixin calls (e.g., `_TaskOps`'s remaining inline `_validate_evaluate_acceptance` calls `self._validate_evaluation` from `_ExperimentOps`) all resolve via MRO.

### Wave 4 — Extract Task mixin + L-O sub-refactor (one commit)

- Create `_ops/tasks.py` with `_TaskOpsMixin(_StoreCore)` containing every remaining method.
- Apply the L-O `reassign_task` LEN reduction inside the new `_TaskOpsMixin` (docstring shrink + extract `_reassign_pending_or_claimed` helper).
- **Size check at end of extraction**: run `python3 scripts/check-complexity.py` and `uvx radon raw reference/packages/eden-storage/src/eden_storage/_ops/tasks.py`. If `tasks.py` lands at > 800 SLOC, apply the §3.7 fallback split into `_TaskCreateOpsMixin` + `_TaskLifecycleOpsMixin` (two new files instead of one) in the same wave commit before pushing forward.
- Update `_base.py`: the composite `_StoreBase(_TaskOpsMixin, _IdeaOpsMixin, _VariantOpsMixin, _EventOpsMixin, _ExperimentOpsMixin, _WorkerOpsMixin, _GroupOpsMixin, _StoreCore)` (or `_TaskCreateOpsMixin, _TaskLifecycleOpsMixin, ...` if the §3.7 fallback fired).
- Update `__init__.py` `iter_events_by_type` re-export path.
- Add the module-load-time MRO assertion (§6).
- Verification gate: full pytest passes; `_base.py` is now under 800 SLOC; every mixin file is under 800 SLOC; the gate would now pass even without the `# slop-allow-file:` annotation. **Do not remove the annotation yet** — it goes in wave 5 with the docs.

### Wave 5 — Docs, annotation removal, PR (one commit + push + PR)

- Remove `# slop-allow-file: F-1 _StoreBase mixin split deferred to issue #114` from `_base.py:1`.
- Update [`docs/audits/2026-05-20-phase-c-disposition.md`](../audits/2026-05-20-phase-c-disposition.md): F-1 row → "REFACTOR (landed in PR #N)".
- Append the chunk-completion entry to [`CHANGELOG.md`](../../CHANGELOG.md) `[Unreleased]` section.
- Add the planless one-liner to [`docs/roadmap.md`](../roadmap.md) if the F-tier refactors appear in the roadmap structure (they may not, since they're audit-follow-ups not phase chunks; check before adding).
- Verification gate: full pytest passes; `scripts/check-complexity.py` passes; markdownlint passes; smoke + e2e pass.
- Push + open PR.
- Run `/codex-review` impl-stage iterations to convergence (commit the records under `docs/plans/review/refactor-f1-storebase-split/impl/<ts>/`).

### Per-wave verification gate summary

Each wave commit must, at minimum, pass:

```text
uv run ruff check .
uv run pyright
uv run pytest -q reference/packages/eden-storage/tests
python3 scripts/check-complexity.py
```

Waves 4 + 5 additionally pass the full pre-push gate (§7).

## 10. Risks / things to watch

1. **MRO surprise via base-list reorder.** A future PR that reshuffles `_StoreBase`'s bases could silently change method resolution. Mitigated by the module-load-time MRO assertion + pyright shadow-detection. Severity: low.

2. **Pyright `reportUnknownMemberType` on cross-mixin calls.** If `_StoreCore`'s abstract-method declarations omit any helper a mixin transitively calls, pyright fails. Mitigated by exhaustively enumerating the abstracts in `_StoreCore` (§3.1) and running pyright at every wave gate. Severity: low-medium (chases minutes, not hours).

3. **Behavior regression in composite-commit semantics.** A method that was previously staging into `_Tx` correctly could, if accidentally split during the move, stage into a stale `_Tx` and call `_apply_commit` twice — breaking atomicity invisibly. Mitigated by the per-wave parametrized-test gate (every `_apply_commit` path is exercised by [`test_composite_commits.py`](../../reference/packages/eden-storage/tests/test_composite_commits.py)). Severity: medium; the test suite is the safety net.

4. **L-O `reassign_task` length still over threshold.** Aggressive docstring trim + light extraction may not get below LEN=100. Mitigated by §3.5's two-option fallback (more aggressive per-state extraction). Severity: low.

5. **`# slop-allow-file:` removed too early.** Waves 1-3 leave `_base.py` over 800 SLOC; if the annotation is removed before wave 4 lands, the wave's CI fails. Mitigated by §8.8 and the wave-5 ordering. Severity: very low (verification gates would catch it).

6. **Mixin SLOC overshoots projection.** §5 projects `_ops/tasks.py` at 600-700 SLOC; if it lands at 850 it crosses the file-SLOC threshold and trips the gate. Mitigated by the L-O sub-refactor reducing `reassign_task`'s footprint and by docstring auditing during the move. Severity: low-medium; if it hits, the recovery is "further split `_TaskOpsMixin` along submit/accept/reject vs create/claim/reclaim/reassign lines" — a one-commit follow-up.

7. **Postgres parametrization untested locally.** If the impl is developed without `EDEN_TEST_POSTGRES_DSN`, the `make_store[postgres]` rows skip locally. CI catches it, but a costly round trip. Mitigated by §7's mandatory pre-push Postgres run (operator runs the parametrization against a local docker postgres before pushing). Severity: low.

8. **Codex round count.** Refactors of this scope, with composite-commit invariants and 3 backends in play, can absorb 3-5 codex-review rounds at plan stage and the same at impl stage. The chunk's task contract caps at 6 rounds before surfacing; if convergence stalls, the operator decides whether to surface or extend. Severity: process-only.

9. **Downstream consumer breakage.** Outside `eden-storage`, the importers of `_StoreBase` / `_Tx` are [`_checkpoint.py`](../../reference/packages/eden-storage/src/eden_storage/_checkpoint.py) (same package), [`eden-checkpoint`](../../reference/packages/eden-checkpoint/) (verify by grep), and possibly the task-store-server. **Mitigation**: pre-impl, run `git grep -E '_StoreBase|from eden_storage._base import' reference/ conformance/` and ensure every external use is type-only (not behavior-coupled to particular method placement). Severity: low (these consumers bind to the public `Store` Protocol, not `_StoreBase` internals).

## 11. Sequence within the chunk

1. **Plan PR opens** (this doc, before any impl).
2. **Plan-stage codex-review iterations** (`/codex-review` against `docs/plans/refactor-f1-storebase-split.md`) to convergence. Expect 3-5 rounds — composite-commit invariant + MRO discipline + cross-mixin call resolution are non-trivial design surfaces; codex tends to surface at least one concern per round on plans this shape.
3. **Plan-stage review record committed** under `docs/plans/review/refactor-f1-storebase-split/plan/<ts>/`.
4. **Operator approves the plan** (out-of-band; the plan PR's merge is the gate).
5. **Plan PR merges to main.**
6. **Impl branch opens** off the merged plan.
7. **Impl wave 1** (scaffolding) → verification gate → commit.
8. **Impl wave 2** (Worker + Group) → verification gate → commit.
9. **Impl wave 3** (Idea + Variant + Event + Experiment) → verification gate → commit.
10. **Impl wave 4** (Task + L-O) → verification gate → commit.
11. **Impl wave 5** (annotation removal + docs + push + PR).
12. **Impl-stage codex-review iterations** to convergence. Expect 3-5 rounds.
13. **Impl-stage review record committed** under `docs/plans/review/refactor-f1-storebase-split/impl/<ts>/`.
14. **Operator approves the impl PR. Merge.**
15. **Issue #114 closes via the impl PR's commit message** (`Closes #114`).

## 12. Out of scope (followups)

- **M-4** — refactor `_apply_commit` itself (extract the `_Tx` walk into `_base.py` with backend-supplied per-row hooks). Audit estimates ~70 LOC saved. Separate issue if anyone wants it; the F-1 refactor is strictly file-layout-preserving on the backend side.
- **L-4** — `_validate_non_no_op_variant` CC=17 readability split. Audit lists as low-priority; the function lands in `_TaskOpsMixin` unchanged.
- **Per-mixin unit tests.** The Protocol-level parametrized suite covers everything; per-mixin tests would create a second source of truth that drifts. Not planned.
- **Renaming `_StoreBase` → `Store`.** The public Protocol type is `Store` (in [`protocol.py`](../../reference/packages/eden-storage/src/eden_storage/protocol.py)); renaming the implementation base class to share the name would be a separate (cross-package) refactor.
- **Splitting `_base.py` across multiple files even further.** `_StoreCore` could move to `_ops/_core.py` (see §3.8) — not in this plan because it adds two file-renames without obvious gain.

## 13. Estimated effort

- Plan-stage codex-review iterations: ~2-4 hours wall time across 3-5 rounds (operator review at each gate).
- Impl wave 1 (scaffolding): ~1 hour.
- Impl wave 2 (Worker + Group): ~1.5 hours including gate.
- Impl wave 3 (Idea + Variant + Event + Experiment): ~2 hours including gate.
- Impl wave 4 (Task + L-O): ~3 hours including gate (the task mixin is the biggest move + the L-O sub-refactor).
- Impl wave 5 (docs + PR open + push): ~30 minutes.
- Impl-stage codex-review iterations: ~2-4 hours wall time across 3-5 rounds.
- **Total: 1 long working day or 2 shorter sessions**, matching the disposition doc's "~1 day equivalent — touches 15 files, ~3000 lines moved" estimate.
