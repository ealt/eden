**Findings**

- Risk — [reference/services/orchestrator/tests/test_e2e.py](/Users/ericalt/Documents/eden/reference/services/orchestrator/tests/test_e2e.py:245): the Phase 8c plan says the subprocess e2e test now preserves the deleted real-integrator coverage by asserting the canonical `trial/*` refs as well as parent shape. The new loop only checks `trial.trial_commit_sha` and `repo.commit_parents(trial.trial_commit_sha)`. A regression where `Integrator.integrate` writes the commit object and store field but fails to create/update `refs/heads/trial/<id>-<slug>` would now pass. Fix by resolving the expected `trial/*` ref for each trial and asserting it exists and matches `trial_commit_sha`, as the deleted `test_end_to_end_with_real_integrator` did.

- Nit — [reference/packages/eden-dispatch/tests/test_orchestrator_iteration.py](/Users/ericalt/Documents/eden/reference/packages/eden-dispatch/tests/test_orchestrator_iteration.py:4): the docstring has a stray leading `+` (`+ integrate without invoking workers`), which looks like a patch artifact. Remove the extra character.

**Overall Assessment**

The cutover is mostly aligned with the plan: `run_experiment` is removed cleanly, the public API/doc updates are in place, the in-process e2e file is deleted, and the hardening test rewrite is correct. I also ran the rewritten hardening test and the subprocess e2e test; both passed locally.

The main remaining issue is coverage drift in the replacement e2e test: it no longer proves that integration creates the canonical `trial/*` refs, even though the plan explicitly said that invariant was preserved. Fix that assertion and this looks ready.