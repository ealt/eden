One contradiction remains, but it’s no longer between the two main integrate sections.

The idempotency summary and the later atomicity section now agree: expected SHA means success, different SHA means `AtomicityViolation`, and absent SHA / failed read-back means `IndeterminateIntegration`, all without compensation on the transport-indeterminate path. Refs: [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:170), [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:497)

The remaining stale contradiction is in the test plan. The “second test inverts the case” still says `trial_commit_sha is None` and “the Integrator compensates as normal,” which no longer matches the round-3 rule. That case should now expect `IndeterminateIntegration` and no compensation. If you still want a compensation-path test, it needs to use a demonstrable non-transport rejection, not a `None` read-back after transport ambiguity. Ref: [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:386)

So: the two sections you asked about are consistent now, but there is still one stale contradiction elsewhere in the document.
