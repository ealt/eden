# EDEN

**E**ric's **D**irected **E**volution **N**exus — a protocol for
orchestrating directed code evolution, with an open reference
implementation and a conformance suite.

EDEN is not a single system. It is a specification that defines the roles
(planner, implementer, evaluator, integrator), the messages they exchange,
and the invariants they must honor. Anyone can build a conforming planner,
implementer, evaluator, or backing store in any language and interoperate
with other conforming components.

> **Intelligent evolution.**

## What's in this repo

| Directory | Purpose |
|---|---|
| [`spec/`](spec/) | Normative protocol specification. Versioned (`spec/v0/`, `spec/v1/`, …). The authoritative source. |
| [`reference/`](reference/) | One complete implementation of the protocol. Labeled as a reference — *one* valid implementation, not *the* implementation. |
| [`conformance/`](conformance/) | Black-box test suite any third-party component can run against itself to prove it conforms. |
| [`docs/`](docs/) | Non-normative human documentation. Starts with [`docs/naming.md`](docs/naming.md) (what EDEN is) and [`docs/roadmap.md`](docs/roadmap.md) (how we build up to the full protocol). |

## Status

**Phase 2 complete.** Both Phase 2 chapters have landed on the
protected `main`: `spec/v0/03-roles.md` (planner, implementer,
evaluator, integrator contracts) and `spec/v0/04-task-protocol.md`
(task state machine, claim tokens, idempotent submission,
reclamation policy). Together with the Phase 1 core-concepts
chapters, six JSON Schemas, the migrated experiment fixture, and
the `docs-lint` + `schema-validity` CI jobs, the protocol surface
the reference implementation will target is fully specified for
the planner/implementer/evaluator lifecycle. There is still **no
runnable code yet** — the reference implementation lands in
Phase 3. Phase 3 next ports a Pydantic-bound reference contracts
package and wires schema-model parity into CI. See
[`docs/roadmap.md`](docs/roadmap.md) for the full Phase 0–13 plan.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Two paths:

- **Spec contributions** follow RFC-style discipline: versioned, normative
  language (RFC 2119 MUST/SHOULD/MAY), careful review.
- **Reference-implementation contributions** follow standard code-review
  workflow.

## License

[MIT](LICENSE).
