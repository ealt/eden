# EDEN

**E**ric's **D**irected **E**volution **N**exus — a protocol for orchestrating directed code evolution, with an open reference implementation and a conformance suite.

EDEN is not a single system. It is a specification that defines the roles (planner, implementer, evaluator, integrator), the messages they exchange, and the invariants they must honor. Anyone can build a conforming planner, implementer, evaluator, or backing store in any language and interoperate with other conforming components.

> **Intelligent evolution.**

## What's in this repo

| Directory | Purpose |
|---|---|
| [`spec/`](spec/) | Normative protocol specification. Versioned (`spec/v0/`, `spec/v1/`, …). The authoritative source. |
| [`reference/`](reference/) | One complete implementation of the protocol. Labeled as a reference — *one* valid implementation, not *the* implementation. |
| [`conformance/`](conformance/) | Black-box test suite any third-party component can run against itself to prove it conforms. |
| [`docs/`](docs/) | Non-normative human documentation. Starts with [`docs/naming.md`](docs/naming.md) (what EDEN is) and [`docs/roadmap.md`](docs/roadmap.md) (how we build up to the full protocol). |

## Status

**Phase 4 complete.** The spec now covers the full v0 cross- component contract: three new chapters — `05-event-protocol.md` (event registry, transactional invariant, delivery guarantees), `06-integrator.md` (git topology, squash rule, eval manifest), and `08-storage.md` (task store / event log / artifact store contracts) — plus a refined `event.schema.json` that pins per-type `data` payload shapes for all 15 registered event types. The `eden-contracts` package gained a discriminated-union `RegisteredEvent` model and round-trip coverage for every registered type; schema-parity and round-trip CI remain green. The spec now covers chapters 00–06 and 08 (control plane lands in Phase 12, conformance in Phase 11). Phase 5 is next: the first executable reference implementation — an in-memory dispatch loop proving the state machines are implementable. See [`docs/roadmap.md`](docs/roadmap.md) for the full Phase 0–13 plan.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Two paths:

- **Spec contributions** follow RFC-style discipline: versioned, normative language (RFC 2119 MUST/SHOULD/MAY), careful review.
- **Reference-implementation contributions** follow standard code-review workflow.

## License

[MIT](LICENSE).
