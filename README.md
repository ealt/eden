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

**Phase 13 in progress.** The v0 spec covers chapters 00–11 (`spec/v0/`), including portable checkpoints (ch. 10) and the multi-experiment control plane (ch. 11). The reference implementation under [`reference/`](reference/) ships the full set of services (task-store-server, orchestrator, ideator / executor / evaluator hosts, web UI, control plane) on two first-class substrates — a Docker Compose stack and a Helm chart for Kubernetes — with Postgres (embedded or managed), Forgejo as the git remote of record, and file / S3 / GCS artifact backends. The conformance suite under [`conformance/`](conformance/) covers every shipped level (v1, v1+roles, v1+roles+integrator, v1+checkpoints, v1+multi-experiment; ~270 scenarios green against the reference impl). Remaining Phase 13 chunks: Forgejo auth hardening (13e) and Kubernetes-native worker modes (13f). See [`AGENTS.md`](AGENTS.md) for the current-phase narrative, [`docs/roadmap.md`](docs/roadmap.md) for the per-phase plan, and [`CHANGELOG.md`](CHANGELOG.md) for per-chunk completion records.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Two paths:

- **Spec contributions** follow RFC-style discipline: versioned, normative language (RFC 2119 MUST/SHOULD/MAY), careful review.
- **Reference-implementation contributions** follow standard code-review workflow.

## License

[MIT](LICENSE).
