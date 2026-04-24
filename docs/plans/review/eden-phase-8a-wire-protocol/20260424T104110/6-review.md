I don’t see a remaining contradiction.

The three places that mattered now line up:

- Summary/idempotency section: expected SHA => success, different SHA => `AtomicityViolation`, absent SHA/read failure => `IndeterminateIntegration`, and compensation only for non-transport, non-divergence rejection. Refs: [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:170)
- Test plan: success/divergence/indeterminate branches are covered explicitly, and the separate compensation test is limited to the non-divergence rejection case. Refs: [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:388)
- Detailed atomicity section: same carveout, same outcomes, same compensation rule. Refs: [eden-phase-8a-wire-protocol.md](/Users/ericalt/Documents/eden/docs/plans/eden-phase-8a-wire-protocol.md:552)

At this point the document reads coherently throughout on the integrate-over-HTTP atomicity story.