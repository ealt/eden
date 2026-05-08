**Findings**
- Risk — [test_implementer_submission.py](/Users/ericalt/Documents/eden/conformance/scenarios/test_implementer_submission.py:96): `test_success_without_commit_sha_must_not_complete_trial` now distinguishes `4xx` from `5xx`, but its submit-time `4xx` branch still returns immediately without checking the trial end-state. The plan’s “assert end-state, not endpoint” section says a conforming rejection should still be followed by an end-state assertion. Fix: read `trial_id` and assert `trial["status"] != "success"` before returning from the submit-time `4xx` branch.

- Risk — [_seed.py](/Users/ericalt/Documents/eden/conformance/src/conformance/harness/_seed.py:327): `drive_to_starting_trial()` is now hardened, but `drive_to_success_trial()` still calls evaluator `/accept` without checking the response or verifying that the returned trial is actually `status == "success"`. This leaves the same kind of setup-obscuring failure mode in helpers used by other conformance scenarios. Fix: `raise_for_status()` on the evaluate accept response and assert the resulting trial has `status == "success"` before returning.

- Nit — [test_planner_submission.py](/Users/ericalt/Documents/eden/conformance/scenarios/test_planner_submission.py:88): the inline comment still says `submit_plan` omits `proposal_ids` on `status="error"`, but this chunk changed `_seed.submit_plan()` to always send `proposal_ids`. The test behavior is correct; the comment is stale and should be updated or removed.

**Overall assessment**

Round 1 fixed the five issues I called out previously: the `4xx`/`5xx` handling is tightened in the evaluator tests, the implementer error-path test now asserts `task.failed`, the evaluator success test no longer overclaims atomicity, and `drive_to_starting_trial()` now validates its setup. The targeted checks are still green (`check_citations`, 20 scenario tests, `ruff`, `pyright`).

I wouldn’t call this fully closed yet because there’s still one real conformance-coverage gap in the implementer negative test and one remaining helper-hardening gap in `_seed.py`. The planner comment is just cleanup.