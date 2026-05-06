**Feasibility**

Assessment: clear. The round-2 fix is the right shape. Binding-layer auth plus store-layer atomic claimant matching closes the submit race cleanly.

**Alternatives**

Assessment: no major objection. This is the right split:
the binding proves identity, the store enforces claim ownership atomically during `submit`.

**Completeness**

1. `verify_worker_credential` is now load-bearing, but it is not fully propagated through the implementation inventory. It is introduced as a required wire op in [D.1](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:167), but the files-to-touch list still only generically names new worker/group endpoints in [§5.1](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:625) and does not explicitly add the corresponding client/server/test work for `verify_worker_credential`. Suggestion: list the endpoint, `StoreClient` method, server handler, and dedicated wire tests explicitly.

2. The doc still contains one stale contradiction about where `WrongClaimant` / `NotClaimed` are raised. D.6 correctly says `Store.submit(task_id, worker_id, payload)` raises them in [§D.6](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:485), and the storage/wire file tables agree in [§5.3](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:652) and [§5.4](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:668). But the “What’s added” bullet still says the wire-binding submit handler raises them before calling the store in [§D.6](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:548). That should be fixed; otherwise the plan describes both architectures.

3. There is still some terminology drift that should be cleaned up before implementation. Scope still says `update_worker_credential` in [§4.1](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:575), while the design consistently uses `reissue_credential` in [D.1](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:157). The spec file table also still references `proposal.schema.json` / `trial.schema.json` in [§5.1](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:628), even though the repo now uses `idea.schema.json` / `variant.schema.json`. These are small, but they are the kind of drift that causes mechanical implementation mistakes.

**Edge Cases and Risks**

1. Add an explicit end-to-end test for credential rotation during an in-flight claim. The important case is: worker A claims, admin reissues A’s credential, old credential fails auth, new credential for the same `worker_id` still submits successfully. That behavior is implied by the new design, but I don’t see it called out in the test design yet.

2. Add an explicit `verify_worker_credential` recovery test for the “wrong worker_id returned” branch, not just 401. [D.1](/Users/ericalt/Documents/eden/docs/plans/eden-phase-12a-1-worker-identity.md:178) treats both as escalation triggers, and that branch is easy to miss if testing only the obvious bad-token case.

**Overall Assessment**

The feasibility-level architecture now looks sound. I would not block on the design anymore; I’d tighten the plan by carrying `verify_worker_credential` through the concrete file/test inventory and removing the remaining stale wording before implementation starts.