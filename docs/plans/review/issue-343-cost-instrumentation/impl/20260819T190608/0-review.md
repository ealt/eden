## Findings

1. **Bug — unpriced model totals are emitted as `$0.00`, violating F3.**  
   **File:** [reference/packages/eden-storage/src/eden_storage/rollup.py:59-65](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/rollup.py:59)  
   **File:** [reference/services/_common/src/eden_service_common/cost_report.py:210-219](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/cost_report.py:210)

   `CostTokenTotals.total_cost_usd` defaults to `0.0`. An unknown model increments `entries_unpriced`, but the JSON report still emits `"total_cost_usd": 0.0`, and the table renders `$0.0000 [unpriced]` at [cost_report.py:315-321](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/cost_report.py:315).

   The test explicitly codifies this behavior at [test_cost_pricing.py:489-494](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/tests/test_cost_pricing.py:489).

   Fix: represent unavailable dollar totals as `null` or omit them, and expose an explicit gap reason in every bucket, including `by_model`. Do not make consumers infer “unknown” from a separate basis field while presenting a numeric zero.

2. **Bug — evaluator idempotency keys are not injective.**  
   **File:** [reference/services/evaluator/src/eden_evaluator_host/subprocess_mode.py:183-193](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/evaluator/src/eden_evaluator_host/subprocess_mode.py:183)  
   **File:** [reference/packages/eden-storage/src/eden_storage/cost.py:169-176](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/cost.py:169)

   The implementation uses `f"{task.task_id}-{variant_id}"`, while the ledger documentation describes the key as `<task_id>:<variant_id>`. Because IDs are only constrained to be non-empty strings, distinct pairs can collide:

   `("task-a", "variant-b-c")` and `("task-a-variant-b", "c")`.

   A collision causes first-write-wins to discard a real attempt, violating the requirement that retries/reclaims cannot be double-counted or lost.

   Fix: encode the pair unambiguously, such as a JSON/length-prefixed encoding or a digest of `(task_id, variant_id)`, and use the same canonical scheme in documentation and tests.

3. **Risk — partially priced attempts are classified as fully derived.**  
   **File:** [reference/packages/eden-storage/src/eden_storage/pricing.py:250-274](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/pricing.py:250)  
   **File:** [reference/packages/eden-storage/src/eden_storage/rollup.py:146-155](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/rollup.py:146)

   If input tokens have a rate but output tokens do not, `derive_cost()` returns `basis="derived"` with a partial dollar amount and a gap. The rollup counts the entry as `entries_derived`, not incomplete/unpriced. Consequently, the table can say “all of it DERIVED” at [cost_report.py:266-275](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/cost_report.py:266), while only a subset was priced.

   Fix: track partial/incomplete pricing separately, propagate bucket-level gaps, and label the total as a floor whenever any token class remains unpriced—not only when an entire entry is unpriced.

## Contract and integration assessment

The implementation otherwise satisfies the main scope:

- Executor `total_cost_usd` capture, ideator token capture, and per-experiment role/variant/idea/model rollups are present.
- Derived dollars remain separate from provider-reported dollars.
- Cache pricing distinguishes 5-minute writes, 1-hour writes, and reads.
- Capture failures are swallowed as intended.
- SQLite and Postgres migrations are mirrored.
- The spec change is confined to the informative worker-host reference binding; no normative `spec/v0` or conformance changes were added.
- Host-level tests exercise real `_handle_one` paths and worktree lifetime behavior.
- The ideator’s `finally` placement is a reasonable tradeoff: it preserves submission success over cost-row durability, and the loss window is documented.

The new files remain below the stated file-size thresholds, and no unannotated structural-erosion issue is evident. However, the two F3 issues above require changes before approval.

**Overall assessment: Request changes.**