## Findings

1. **Bug — `cost_entry_id()` still has a deterministic namespace collision.**  
   **File:** [reference/packages/eden-storage/src/eden_storage/cost.py:215-219](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/cost.py:215)

   The full SHA-256 digest fits the cap and is deterministic:

   > `digest = hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()`

   However, short keys are returned verbatim:

   > `return candidate`

   An opaque short `attempt_key` equal to `sha256:<64-hex-digest>` produces exactly the same `entry_id` as an over-cap key whose digest is that hex value. This is a namespace collision, independent of breaking SHA-256.

   Fix: reserve separate namespaces for raw and digest keys, for example `cost-<role>-raw:<attempt_key>` versus `cost-<role>-sha256:<digest>`, or reject raw keys beginning with the digest prefix. The full hash is otherwise correctly sized and deterministic.

2. **Bug — duplicate model slices can double-count `by_model` derived dollars.**  
   **File:** [reference/services/_common/src/eden_service_common/agent_cost.py:169-198](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/agent_cost.py:169)  
   **File:** [reference/packages/eden-storage/src/eden_storage/pricing.py:307-329](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/pricing.py:307)  
   **File:** [reference/packages/eden-storage/src/eden_storage/rollup.py:222-237](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/rollup.py:222)

   Worker-reported `models` entries are accepted without enforcing unique model labels. `_derive_from_models()` aggregates all duplicate slices into one `per_model[usage.model]` amount, then `_accumulate_model()` adds that aggregate once for each duplicate slice.

   For two token-only slices for `m1`, the attempt total is priced once, but each `m1` slice receives the combined amount, doubling the `by_model` total.

   Fix: reject or merge duplicate model labels during normalization, or retain per-slice derived amounts instead of looking them up only by model name. Add a duplicate-model regression test.

3. **Nit — stale comment contradicts the fixed floor-marker implementation.**  
   **File:** [reference/services/_common/src/eden_service_common/cost_report.py:328-332](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/_common/src/eden_service_common/cost_report.py:328)

   The comment says:

   > `No separate floor marker`

   but the code immediately emits:

   > `f"{_floor_flag(row):<5} "`

   Remove or update the comment so future readers do not infer that the role table intentionally lacks a row-level marker.

## Verified fixes

- Full SHA-256 is used for over-cap keys at [cost.py:218-219](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/cost.py:218), stays under `ENTRY_ID_MAX_LEN`, and short keys remain verbatim.
- `composite_attempt_key()` is genuinely injective for its length-prefixed encoding.
- `DerivedCost.partial_models` is populated for single-model and multi-model derivation, and `_accumulate_model()` consults the per-model set at [rollup.py:234-237](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/packages/eden-storage/src/eden_storage/rollup.py:234).
- The ideator mints `dispatch_nonce` before dispatch at [subprocess_mode.py:419-425](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/ideator/src/eden_ideator_host/subprocess_mode.py:419) and threads it into the recorded key at [subprocess_mode.py:496-504](/home/dev/Documents/eden-worktrees/cost-instrumentation/reference/services/ideator/src/eden_ideator_host/subprocess_mode.py:496).
- Every table section now emits a `floor` column through `_floor_flag()`, including roles, models, variants, and ideas.
- Optional dollar fields, `None` preservation, `n/a` formatting, `entries_partially_priced`, and `is_floor` propagation are consistent across the rollup and JSON report.
- No new structural threshold violation is evident; the added logic remains split across focused files and the brief reports the canonical checks passing.

## Overall assessment

The four requested fixes are substantially implemented and tested. Two additional correctness issues remain: the raw-key/digest namespace collision and duplicate-model double-counting. I recommend one more change-and-review round before approval.