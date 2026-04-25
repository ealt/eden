**Findings**

- **Critical** — `--claim-ttl-seconds` still does not actually close the stranded-claim gap. The plan now sets `expires_at` on claim in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:247), and treats that as automatic recovery in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:512) and [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:550). But in the current reference stack, `claim()` only records `expires_at` in [eden_storage/_base.py](/Users/ericalt/Documents/eden/reference/packages/eden-storage/src/eden_storage/_base.py:565), `reclaim()` must still be called explicitly in [eden_storage/_base.py](/Users/ericalt/Documents/eden/reference/packages/eden-storage/src/eden_storage/_base.py:749), and `run_orchestrator_iteration()` does not perform expired-claim sweeping in [driver.py](/Users/ericalt/Documents/eden/reference/packages/eden-dispatch/src/eden_dispatch/driver.py:33). So this item is still not addressed unless the plan also adds a sweeper path or an explicit “expired reclaim” trigger.

- **High** — The planner-input fix is only partial. Adding `--experiment-config` in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:232) solves the backend source problem, but the human planner still is not given the full read access required by the contract in [03-roles.md](/Users/ericalt/Documents/eden/spec/v0/03-roles.md:40) and [03-roles.md](/Users/ericalt/Documents/eden/spec/v0/03-roles.md:41). The revised flow only says the UI surfaces `objective` and explicitly says `metrics_schema` is not exposed to the planner in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:381). It also still does not define any planner-visible read path for existing proposals or trials.

- **High** — The partial-write issue is improved but not actually closed. The three-phase ordering in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:404) is materially better, but the plan still accepts a Phase-2→Phase-3 window where proposals can be `ready` before the plan task is submitted in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:421). That means the original spec concern remains: ready proposals may dispatch even though the planner attempt has not durably submitted. The revision now documents the gap instead of overlooking it, which is progress, but it is still an open correctness issue.

- **Medium** — The revised reclaim/error handling now names non-canonical wire error types. The plan says recovery keys off `eden://error/invalid-claim-token` and `eden://error/conflict` in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:439), but the actual closed vocabulary is `eden://error/wrong-token`, `eden://error/illegal-transition`, `eden://error/conflicting-resubmission`, and `eden://error/invalid-precondition` in [07-wire-protocol.md](/Users/ericalt/Documents/eden/spec/v0/07-wire-protocol.md:148). That should be corrected before implementation so the UI recovery logic is pinned to real error codes.

- **Medium** — The new experiment-drift mitigation is not implementable as written. The plan says startup rejects when the loaded config’s `experiment_id` disagrees with `--experiment-id` in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:240), but `ExperimentConfig` has no `experiment_id` field in [config.py](/Users/ericalt/Documents/eden/reference/packages/eden-contracts/src/eden_contracts/config.py:32), and the fixture config likewise lacks one in [config.yaml](/Users/ericalt/Documents/eden/tests/fixtures/experiment/.eden/config.yaml:1). The drift risk is real; this particular check is not.

- **Medium** — The auth section is much better, but the stack rationale still contains a stale claim. “Even if the cookie leaks, it can’t bypass the UI service’s own authorization” remains in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:89), while the later threat model correctly says a leaked cookie can act as that `worker_id` through the UI in [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:366). Those should agree.

**Round-0 Status**

- **Experiment config / artifact source:** mostly addressed. `--experiment-config` and `--artifacts-dir` are good additions. What remains is planner-visible access to the full required inputs, not just backend possession of them.

- **Partial-write handling:** partially addressed. The new phase split is the right mitigation, but the known unsafe window is still there.

- **Stranded claims:** not addressed yet in operational terms. TTL without an actual expired-claim sweeper does not recover anything.

- **Auth / cookie / CSRF / bearer hygiene:** addressed well. This section is now credible, minus the one stale sentence noted above.

- **Alternatives / stack rationale:** addressed well. The revised alternatives section is much less strawmanned and now distinguishes the real BFF-backed SPA alternative from the browser-direct anti-pattern.

- **Tests / verification / risks:** substantially addressed. The added security and partial-write tests are the right shape. The main caveat is that the new stranded-claim verification step currently relies on behavior the reference stack does not have.

**Still Missing**

- A concrete mechanism for expired-claim reclaim. If you want TTL to be real in this chunk, the plan needs one of:
  - a store-side sweep endpoint or helper,
  - an orchestrator-side expired-claim pass,
  - or a UI/admin-triggered reclaim path that explicitly checks `expires_at`.

- A planner context surface for existing proposals and trials, and likely `metrics_schema` too, if the intent is that a human can genuinely “play planner” from the UI alone.

- A Phase-3 submit failure strategy that uses submit idempotency before concluding “orphaned proposals.” Right now [eden-phase-9-web-ui.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-9-web-ui.md:430) jumps straight to operator recovery, but submit retries/read-back are specifically safe per [07-wire-protocol.md](/Users/ericalt/Documents/eden/spec/v0/07-wire-protocol.md:161).

**Overall**

This revision is materially better and it closes most of the round-0 concerns. It is not ready as-is yet because the stranded-claim fix is still aspirational rather than real, and the planner-input contract is still only partially surfaced to the human user. The remaining edits are smaller than before, but I would make them before implementation.