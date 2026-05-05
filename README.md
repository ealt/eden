# EDEN

**E**ric's **D**irected **E**volution **N**exus — a protocol for orchestrating directed evolution, with an open reference implementation and a conformance suite.

EDEN is not a single system. It is a specification that defines the roles (ideator, executor, evaluator, integrator), the messages they exchange, and the invariants they must honor. Anyone can build a conforming ideator, executor, evaluator, or backing store in any language and interoperate with other conforming components.

> **Intelligent evolution.**

## What's in this repo

| Directory | Purpose |
|---|---|
| [`spec/`](spec/) | Normative protocol specification. Versioned (`spec/v0/`, `spec/v1/`, …). The authoritative source. |
| [`reference/`](reference/) | One complete implementation of the protocol. Labeled as a reference — *one* valid implementation, not *the* implementation. |
| [`conformance/`](conformance/) | Black-box test suite any third-party component can run against itself to prove it conforms. |
| [`docs/`](docs/) | Non-normative human documentation. Starts with [`docs/naming.md`](docs/naming.md) (what EDEN is) and [`docs/roadmap.md`](docs/roadmap.md) (how we build up to the full protocol). |

## Status

**Phase 11 complete.** The v0 spec covers chapters 00–09 (`spec/v0/`); the reference implementation under [`reference/`](reference/) ships the full set of services (task-store-server, orchestrator, planner / implementer / evaluator hosts, web UI) on a Compose stack with Postgres + Gitea; and the conformance suite under [`conformance/`](conformance/) is at the **v1+roles+integrator** level (110 scenarios green against the reference impl). Phase 12 (multi-experiment / control plane / leases) and Phase 13 (Kubernetes + S3 blob backend) are the remaining build-up phases. See [`AGENTS.md`](AGENTS.md) for the current-phase narrative and [`docs/roadmap.md`](docs/roadmap.md) for the per-phase plan.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Two paths:

- **Spec contributions** follow RFC-style discipline: versioned, normative language (RFC 2119 MUST/SHOULD/MAY), careful review.
- **Reference-implementation contributions** follow standard code-review workflow.

## License

[MIT](LICENSE).
