Two issues remain.

1. The test plan now distinguishes the branches correctly, but the main atomicity prose is still too broad about synchronous `409 invalid-precondition`. The test section says there are two cases:
- different stored SHA => `AtomicityViolation`, no compensation
- separate non-transport rejection => compensation But the atomicity section still says compensation runs on a received `409 invalid-precondition` as the example of demonstrable rejection, without carving out the “different SHA already committed” case. That leaves a contradiction between the test plan and the prose. Refs: [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:388), [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:404), [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:524)

2. The new compensation-path example is not a valid one. `integrate_trial` does not reject because `commit_sha` is unreachable, and `validate_metrics` does not check reachability. That example mixes integrator-side preconditions with the store-side endpoint. If you want this test, describe it as an injected/mock synchronous `InvalidPrecondition` from the store, or use a real store-side precondition that `integrate_trial` actually enforces. Ref: [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:406)

So the test plan is closer, but it does not yet match the atomicity rules “throughout” because the prose and the compensation example still need tightening.
