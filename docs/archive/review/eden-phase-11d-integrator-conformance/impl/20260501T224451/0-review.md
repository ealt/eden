**Findings**
- Risk: [test_promotion_preconditions.py](/Users/ericalt/Documents/eden/conformance/scenarios/test_promotion_preconditions.py:26) treats `trial_commit_sha == ""` as equivalent to “unset”. That weakens the conformance assertion: the trial schema only permits `trial_commit_sha` to be absent or a SHA-shaped string, not an empty string ([trial.schema.json](/Users/ericalt/Documents/eden/spec/v0/schemas/trial.schema.json:39), [02-data-model.md](/Users/ericalt/Documents/eden/spec/v0/02-data-model.md:203)). As written, a non-conforming IUT could serialize `""` after a rejected integrate and still pass both new precondition tests.

**Assessment**
Needs revisions. The chunk is otherwise aligned with the plan, but the negative promotion tests should not accept an invalid wire shape.

Per point:

1. Spec amendment correctness: good. [09-conformance.md](/Users/ericalt/Documents/eden/spec/v0/09-conformance.md:36) now scopes `v1+roles+integrator` to the wire-observable projection of chapter 06, and that is consistent with the binding-only harness contract in [09-conformance.md](/Users/ericalt/Documents/eden/spec/v0/09-conformance.md:86). The new §5 sub-table is well-formed and parseable.

2. Test correctness:
   - `test_cross_artifact_consistency_on_success`: correct. It asserts exactly one `trial.integrated` for the seeded trial, `field == sent SHA`, `event == sent SHA`, and `field == event` ([test_integrator_atomicity.py](/Users/ericalt/Documents/eden/conformance/scenarios/test_integrator_atomicity.py:15)).
   - `test_divergent_resubmit_leaves_no_second_event`: correct for §5.3. It pins `409 invalid-precondition`, unchanged stored field, and no second `trial.integrated` event ([test_integrator_atomicity.py](/Users/ericalt/Documents/eden/conformance/scenarios/test_integrator_atomicity.py:59)).
   - `test_integrate_against_error_trial_rejected`: logic is right for the claimed composite end-state, but weakened by the empty-string allowance noted above ([test_promotion_preconditions.py](/Users/ericalt/Documents/eden/conformance/scenarios/test_promotion_preconditions.py:41)).
   - `test_integrate_against_eval_error_trial_rejected`: same conclusion ([test_promotion_preconditions.py](/Users/ericalt/Documents/eden/conformance/scenarios/test_promotion_preconditions.py:60)).

3. Helper hardening: `drive_to_error_trial()` correctly uses `reject`, not `accept`, and both new helpers follow the planned hardening pattern of `raise_for_status()` plus terminal-status assertion ([\_seed.py](/Users/ericalt/Documents/eden/conformance/src/conformance/harness/_seed.py:327), [\_seed.py](/Users/ericalt/Documents/eden/conformance/src/conformance/harness/_seed.py:370)). For their intended use, the postconditions are sufficient.

4. Group-relevance discipline: passes. `uv run python conformance/src/conformance/tools/check_citations.py` succeeded, and the new groups in [09-conformance.md](/Users/ericalt/Documents/eden/spec/v0/09-conformance.md:71) match the module `CONFORMANCE_GROUP` values and cited sections. The checker reported 109 citing test functions; pytest’s 110 total is consistent with parametrized expansion.

5. Reference impl correctness: expected. `uv run pytest -q conformance/` passed with `110 passed in 67.56s`, which fits the narrowed chapter-06 wire-projection scope rather than git-side integrator behavior.

6. Anything missed: I didn’t find off-by-one issues, citation/group drift, or plan/spec mismatch beyond the `""` leniency in the negative helper assertion.

Verification run:
- `uv run python conformance/src/conformance/tools/check_citations.py`
- `uv run pytest -q conformance/scenarios/test_integrator_atomicity.py conformance/scenarios/test_promotion_preconditions.py`
- `uv run pytest -q conformance/`