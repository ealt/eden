# EDEN Conformance Suite

Black-box test suite that any third-party implementation of an EDEN
component can run against itself to prove conformance with the protocol
specification.

## Status

**Stub — lands in Phase 11.** No scenarios are implemented yet. This
directory currently exists only to reserve the location and frame the
intent.

## Intent

- **Implementation-agnostic.** Scenarios drive an
  implementation-under-test via its advertised protocol surface (HTTP,
  wire messages), not via language-specific hooks. This is what lets a
  suite written in Python validate a component written in Go.
- **Per-component.** Separate scenarios for each role — task store,
  orchestrator, planner worker host, integrator, event log, storage.
  You run the subset that matches the component you're testing.
- **Spec-anchored.** Every scenario cites the spec paragraph it
  validates. When the spec changes, the suite changes.

## Planned scenarios

See [`../docs/roadmap.md`](../docs/roadmap.md#phase-11--conformance-suite-v1).
Summary:

- State-machine scenarios (task lifecycle, claim-token semantics,
  transactional event invariant).
- Role-contract scenarios (planner submission, implementer submission,
  evaluator submission, backpressure, idempotency).
- Integrator scenarios (squash shape, eval-manifest shape, `work/*`
  access discipline).

The reference implementation in [`../reference/`](../reference/) will be
the first subject run through the suite when Phase 11 lands, but the
harness is designed to run against any implementation.
