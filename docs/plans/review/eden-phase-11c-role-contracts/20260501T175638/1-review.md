**Missing Context**

Brief assessment: the round-0 blockers are mostly addressed, but one normative gap is still being resolved by interpretation rather than by a clear spec statement.

- The planner `status="error"` wire shape is still not nailed down. The plan now asserts at [docs/plans/eden-phase-11c-role-contracts.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11c-role-contracts.md:174) that omitting `proposal_ids` is conforming on error, but chapter 03 §2.4 still reads as though `proposal_ids` is part of the submission payload generally, not only for success. As written, this test would certify the reference interpretation, not an unambiguous spec rule. Either amend §2.4 in the same chunk, or send explicit `proposal_ids` on the error case and test that shape instead.

**Feasibility**

Brief assessment: aside from the planner error-shape issue above, the plan is now executable. The helper-extension and citation fixes are enough to make the evaluator and implementer scenarios implementable.

- The only remaining feasibility concern is that the planner error test at [docs/plans/eden-phase-11c-role-contracts.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11c-role-contracts.md:174) depends on the unresolved interpretation above. If you do not settle that first, the scenario can pass while still leaving a real interop ambiguity in the spec.

**Completeness**

Brief assessment: once the planner shape is settled, the next gaps are in what the scenario set actually proves.

- After introducing the §4.4 amendment at [docs/plans/eden-phase-11c-role-contracts.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11c-role-contracts.md:71), the plan should test the clarified rule directly. The current evaluator idempotency case at [docs/plans/eden-phase-11c-role-contracts.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11c-role-contracts.md:217) only proves identical resubmit; it does not prove that `artifacts_uri` is excluded from equivalence. A stronger 11c test would resubmit with identical `trial_id`/`status`/`metrics` but a different `artifacts_uri`, and assert 200 plus no second `task.submitted`.
- The per-role scope claims are still broader than the listed tests. The group table promises implementer/evaluator “status vocabulary” and evaluator “per-status trial-side writes” at [docs/plans/eden-phase-11c-role-contracts.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11c-role-contracts.md:99), but the scenario lists at [docs/plans/eden-phase-11c-role-contracts.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11c-role-contracts.md:183) and [docs/plans/eden-phase-11c-role-contracts.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-11c-role-contracts.md:204) still omit a chapter-03-cited implementer `status="error"` case and an evaluator `status="error"` case. Existing v1 composite tests cover parts of the behavior, but 11c’s job is to pin the role-contract MUSTs specifically.

**Overall Assessment**

This revision fixes most of the round-0 problems and is much closer. I would resolve the planner `status="error"` shape explicitly, then strengthen the scenario set so the new evaluator-idempotency amendment and the missing `status="error"` role branches are actually covered.