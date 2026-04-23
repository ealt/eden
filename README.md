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

**Phase 3 complete.** The reference repository now ships its first
Python package, `reference/packages/eden-contracts` — Pydantic v2
bindings for all six spec/v0 JSON Schemas, with a discriminated-union
task model, strict numeric parsing, URI and date-time format
validation, and enforcement of the cross-field invariants the schemas
express via `if/then/else` (task claim-presence, per-kind payloads,
reserved metric names). A uv workspace wires ruff, pyright, and pytest
at the repo root, and CI now runs six jobs — `docs-lint`,
`schema-validity`, `python-lint`, `python-typecheck`, `python-test`,
and `schema-parity`. The `schema-parity` job enforces that every
fixture accepted by the models is also accepted by the JSON Schemas
and vice versa, plus a round-trip check on `model_dump(exclude_none=
True)`. There is still **no end-to-end runnable orchestration** —
that starts landing in Phase 5. Phase 4 is next: event protocol,
integrator, and storage specification. See
[`docs/roadmap.md`](docs/roadmap.md) for the full Phase 0–13 plan.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Two paths:

- **Spec contributions** follow RFC-style discipline: versioned, normative
  language (RFC 2119 MUST/SHOULD/MAY), careful review.
- **Reference-implementation contributions** follow standard code-review
  workflow.

## License

[MIT](LICENSE).
