**Findings**

1. Major: the core atomicity section is now correct, but an earlier summary bullet still says the old thing. In the normative idempotency section, the transport-indeterminate rule still says “If the server has no `trial_commit_sha` or a different one, the client MAY delete the ref and re-raise.” That directly contradicts the later round-2 rule that `null` means `IndeterminateIntegration` and a different SHA means `AtomicityViolation`, with no compensation in either case. Right now the plan contains both behaviors. The later section is the right one; the earlier bullet needs to be brought into sync. Ref: [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:170), [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:492)

**Assessment**

Aside from that stale contradiction, the round-2 design is in good shape.

The atomicity story now holds under concurrent reconciliation: same-SHA retries are safe via server-side idempotency; different-SHA observation becomes loud `AtomicityViolation` with no compensating delete; and `null`/failed read-back stays indeterminate rather than being misclassified as rollback. The narrowed retry guidance is also appropriately scoped now; removing the blanket `illegal-transition => prior commit` rule was the right fix.

Once you align the earlier idempotency bullet with the later three-outcome rule, I don’t see a new substantive issue from this round.