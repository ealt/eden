**Missing Context**

Assessment: resolved. The scope, compatibility policy, and live-vs-historical documentation treatment are clear enough now.

**Feasibility**

Assessment: the approach is still workable, with one implementation detail that should be tightened.

- The new replay/reconstruction check in the e2e test is a good replacement for the deleted in-process lifecycle test, but the plan describes the data source awkwardly. It says the test should read `client.read_range(None)` via the orchestrator’s `StoreClient` at [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:54>), while the current test does not hold that client object; it tears processes down and then inspects a local `SqliteStore` at [test_e2e.py](/Users/ericalt/Documents/eden/reference/services/orchestrator/tests/test_e2e.py:233). The plan should explicitly choose one of these:
  - instantiate a `StoreClient` in the test before terminating the server, or
  - more simply, fold over `store.read_range()` from the reopened `SqliteStore`.
  
  The second option matches the current test structure better.

**Alternatives**

Assessment: the chosen direction still looks right. Removing `run_experiment` entirely is cleaner than keeping a compatibility shim, and moving the lifecycle-replay invariant onto the real subprocess e2e path is the right place for that assertion.

**Completeness**

Assessment: still has significant concerns, so I would stop here.

- The verification gate no longer covers all of the live docs the plan says must be updated. §C explicitly requires editing [reference/README.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:124>), but Verification §7 only greps `reference/packages/` and `reference/services/` at [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:163>). That means a stale Phase-8b status line in `reference/README.md` could slip through with verification still green. Add `reference/README.md` to the automated check, or add a separate explicit verification step for that file.

- The risks/coverage narrative is now internally inconsistent. §B correctly says lifecycle reconstructibility is another distinct invariant that needs replacement coverage via the e2e extension at [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:54>), but the Risks section still says the only unique case from `test_end_to_end.py` is malformed-success routing at [docs/plans/eden-phase-8c-cutover.md](</Users/ericalt/Documents/eden/docs/plans/eden-phase-8c-cutover.md:182>). That was true in the prior draft, but not in this one. The plan should update the risk text so it matches the new coverage story.

I did not evaluate edge cases and risks beyond that because the completeness issues above should be fixed first.

**Overall Assessment**

This is close. The remaining work is mostly consistency polish: make the e2e replay-check implementation path explicit, bring `reference/README.md` into verification, and update the risk section to match the revised coverage plan. After that, the plan should be ready.