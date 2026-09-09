## Findings

1. **Bug — the over-cap `cost_entry_id` path is not genuinely injective.**  
   **File:** [reference/packages/eden-storage/src/eden_storage/cost.py:201-212](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/cost.py:201)

   `composite_attempt_key()` is injective: its length-prefixed encoding is reversible. However, over-cap keys are reduced to only the first 32 hexadecimal characters of SHA-256:

   > `digest = hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()[:32]`

   Distinct attempt keys can therefore produce the same 128-bit digest, causing first-write-wins to discard one spend row. The regression test only checks two ordinary inputs at [test_cost_pricing.py:531-539](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/tests/test_cost_pricing.py:531); it cannot establish injectivity.

   Fix: avoid treating the digest as mathematically collision-free. Store the full canonical attempt key in a uniquely constrained field, or explicitly constrain the identifier domain. If a digest is retained as the database key, use the full digest and add collision detection rather than silently treating a collision as a duplicate.

2. **Bug — `by_model` marks a fully priced model slice as a floor because another model is incomplete.**  
   **File:** [reference/packages/eden-storage/src/eden_storage/pricing.py:288-333](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/pricing.py:288)  
   **File:** [reference/packages/eden-storage/src/eden_storage/rollup.py:229-235](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/rollup.py:229)

   `DerivedCost.partially_priced` is attempt-wide. For a multi-model entry where `m1` is fully priced and `m2` is unknown, `_derive_from_models()` sets:

   > `partially_priced=bool(gaps)`

   Then `_accumulate_model()` applies that flag to every model slice with derived dollars:

   > `if derived.partially_priced: totals.entries_partially_priced += 1`

   The `by_model["m1"]` bucket consequently reports `is_floor=True` even though all of `m1`’s own token classes were priced. The incomplete `m2` slice should be the floor; `m1` should remain complete.

   Fix: propagate partial-pricing status per model slice, not only per attempt. Add a per-model pricing verdict or calculate each slice’s gaps independently before updating its bucket. Add a regression test with one fully priced model and one unpriced model.

3. **Risk — the ideator’s idempotency key is injective per call, but not stable across retries.**  
   **File:** [reference/services/ideator/src/eden_ideator_host/subprocess_mode.py:383-396](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/ideator/src/eden_ideator_host/subprocess_mode.py:383)

   The delimiter collision is fixed:

   > `attempt_key=composite_attempt_key(task.task_id, uuid.uuid4().hex[:12])`

   But the nonce is created inside `_record_ideation_cost()`. Re-recording the same dispatch creates a new nonce and therefore a new ledger key. A retry after a transport failure would not deduplicate. The implementation currently relies on the assumption that “nothing retries this call,” but the ledger contract is specifically first-write-wins across transport retries.

   Fix: mint the dispatch nonce before the dispatch begins, retain it in the per-dispatch state, and reuse it for any retry of that dispatch. Add a host-level retry/idempotency test for the ideator.

4. **Risk — per-bucket floor status is present in JSON but not visible in several table sections.**  
   **File:** [reference/services/_common/src/eden_service_common/cost_report.py:337-351](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/cost_report.py:337)  
   **File:** [reference/services/_common/src/eden_service_common/cost_report.py:362-374](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/cost_report.py:362)  
   **File:** [reference/services/_common/src/eden_service_common/cost_report.py:378-393](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/cost_report.py:378)

   The JSON report correctly emits `is_floor` for totals, roles, variants, ideas, and models. The table, however, only exposes partial/unpriced counts in the role section. Variant, idea, and model rows render only:

   > `... {_fmt_usd(...)} ... {row['basis']} ...`

   A partially priced variant can therefore display `$3.0000 derived` without a row-level `FLOOR` marker or partial count. The global totals line may say `FLOOR`, but the individual bucket still visually appears complete.

   Fix: add a floor/partial indicator or the relevant counters to every bucket table, and add tests asserting floor visibility for variant, idea, and model rows—not only that the output contains one global `FLOOR`.

## Verified fixes

Finding 1 is fixed for the numeric-zero problem:

- Dollar fields are optional in [rollup.py:75-82](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/rollup.py:75).
- `_refresh_total()` preserves `None` at [rollup.py:164-176](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden-storage/rollup.py:164).
- The report propagates all bucket values and `is_floor` at [cost_report.py:185-228](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/cost_report.py:185).
- `_fmt_usd()` renders `n/a` at [cost_report.py:259-265](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/cost_report.py:259).

Finding 3’s aggregate partial-pricing propagation is also correct for totals and roles:

> `if derived.partially_priced: totals.entries_partially_priced += 1`

and:

> `return bool(self.entries_unpriced or self.entries_partially_priced)`

at [rollup.py:90-93](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/rollup.py:90).

The reported-cost path correctly remains non-floor, and the new tests cover nulls, full pricing, reported pricing, and aggregate partial pricing. The remaining issue is the per-model granularity and incomplete table coverage described above.

## Structural assessment

No new source file exceeds the stated 800-SLOC threshold; the main additions remain decomposed into focused modules. The optional-dollar change is consistently handled in production formatting and rollup arithmetic. The added tests are directionally good, but they currently do not cover digest collision guarantees, ideator retry reuse, per-model partial status, or row-level table floor markers.

## Overall assessment

Most of the prior feedback was addressed successfully, especially the null-versus-zero behavior and aggregate partial-pricing signal. The implementation still needs changes before approval: the over-cap digest is probabilistically collision-prone, ideator retries are not stable-keyed, and per-model/table surfaces can still report misleading completeness.