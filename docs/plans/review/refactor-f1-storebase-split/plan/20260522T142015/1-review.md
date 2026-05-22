**Missing Context**

Assessment: addressed. The new cross-resource helper matrix in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:168) closes the biggest round-0 gap, and the `__init__.py` re-export update in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:251) covers the public `iter_events_by_type` path.

**Feasibility**

Assessment: mostly addressed, but one feasibility issue remains.

- The plan is still internally inconsistent on `resolve_worker_in_group`. Decision #5 says the worker/group-spanning helper lives on `_StoreCore` in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:54), but the dependency matrix and naming map place it on `_GroupOps` in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:181) and [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:239). The current cross-seam caller is `claim` in [_base.py](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/reference/packages/eden-storage/src/eden_storage/_base.py:935). As written, this has the same pyright problem the plan already acknowledges for `_validate_evaluation` in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:432): inside `_TaskOpsMixin`, pyright sees `_StoreCore`, not sibling mixins. Fix it one of three ways: keep `resolve_worker_in_group` on `_StoreCore` as decision #5 says, add a `_StoreCore` stub for it, or explicitly use a protocol/cast at the call site. Until that contradiction is resolved, feasibility is not fully closed.
- The rest of the round-0 feasibility concerns are addressed well. Moving `_require_*`, `_find_starting_variant_for_implement_task`, and `_validate_registry_id` into `_StoreCore` in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:188) is the right correction, the `_reseed_default_event_counter` note in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:42) is the right kind of non-byte-preserving caveat, and the in-PR TaskOps fallback in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:267) makes the size-risk manageable.

Because the remaining feasibility issue is narrow and fixable, I continued to the next levels.

**Alternatives**

Assessment: the chosen approach still looks right.

- I do not see a better primary approach than the current mixin-family split for this specific goal. Delegation would add forwarding boilerplate without reducing risk, and a dedicated validation mixin still looks worse than the revised core-plus-noun-mixins layout.
- The only alternative worth mentioning is the already-noted `_ops/_core.py` extraction in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:324). I do not think it is necessary here; the bottom-import pattern is acceptable if the import order is documented and tested.

**Completeness**

Assessment: mostly complete, with two concrete gaps.

- The MRO assertion needs to account for the fallback split. The assertion example in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:399) hard-codes the 7-mixin shape, but §D.7 and Wave 4 allow the 8-mixin fallback in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:267) and [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:501). The plan should say the assertion changes shape if the fallback fires.
- The verification-gate wording is inconsistent with the wave plan. §7 says the complexity gate must pass “with `_base.py`’s `# slop-allow-file:` removed” after every wave in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:403), but Waves 1-4 explicitly keep the annotation until Wave 5 in [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:479) and [refactor-f1-storebase-split.md](/Users/ericalt/Documents/eden-worktrees/issue-114-storebase-split-plan/docs/plans/refactor-f1-storebase-split.md:505). Rewrite that gate so Waves 1-4 expect pass with the annotation still present, and Wave 5 expects pass after removal.

**Edge Cases And Risks**

Assessment: no new major design risk beyond the remaining seam inconsistency.

- If the `resolve_worker_in_group` placement stays contradictory, the likely failure mode is not runtime semantics but implementation churn: either pyright failures in `_TaskOpsMixin` or a last-minute, undocumented stub added during implementation. That is exactly the kind of drift this plan is supposed to prevent.
- If the TaskOps fallback fires, the plan needs every derived check to stay in sync in the same commit: `_StoreBase` base order, the MRO assertion, file-count expectations, and any prose that still assumes “7 mixins.” Right now only part of that sync story is written down.

**Overall Assessment**

The revised plan substantially improves on round 0 and addresses most of the real feasibility concerns. I would not block the overall direction anymore. I would, however, fix the `resolve_worker_in_group` placement/type-checking contradiction and the two completeness mismatches before treating the plan as implementation-ready. Once those are corrected, the remaining concerns are normal execution risks rather than design flaws.