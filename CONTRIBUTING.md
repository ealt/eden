# Contributing to EDEN

Thanks for your interest in EDEN.

EDEN is a **protocol** for directed-code-evolution orchestration. This repo
contains three kinds of artifact that call for different contribution
discipline: the **specification** (`spec/`), the **reference
implementation** (`reference/`), and the **conformance suite**
(`conformance/`). Read the section that matches where your change lives.

## Current phase

**Phase 3 complete.** The spec/v0 core concepts, role contracts, task
protocol, and six JSON Schemas are on the protected `main` alongside
the Pydantic reference bindings in `reference/packages/eden-contracts`
and a full Python toolchain (uv workspace + ruff + pyright + pytest).
CI runs six checks — `docs-lint`, `schema-validity`, `python-lint`,
`python-typecheck`, `python-test`, and `schema-parity`. See
[`docs/roadmap.md`](docs/roadmap.md) for the remaining Phase 4–13 work.

If you want to contribute, the most useful next areas are the Phase 4
chapters (event protocol, integrator, storage) and any gaps you spot
in the spec or the contracts-package bindings.

## Contributing to the spec

The spec is the authoritative source of protocol semantics. A change to
the spec is a change to what "EDEN" means.

### Spec conventions

- **Versioning.** `spec/v0/` is a single lineage. Within a version,
  changes should be additive or clarifying. Breaking changes go to a new
  version (`spec/v1/`, …), never in-place.
- **Normative language.** Use RFC 2119 keywords: **MUST**, **SHOULD**,
  **MAY**. If prose doesn't use one, it's informative.
- **Wire-format changes propagate.** A change to a JSON Schema file
  under `spec/v*/schemas/` must be reflected in the Markdown chapter
  and in the Pydantic bindings in `reference/packages/eden-contracts/`.
  CI's `schema-parity` job enforces that models and schemas agree on
  the accept/reject corpus and on round-trip emission.
- **No technology choices in normative text.** The spec talks about
  *semantics*, not mechanisms. "A conforming task store MUST provide
  atomic claim with linearizable semantics" — yes. "Uses Postgres `SELECT
  ... FOR UPDATE`" — no, that's reference-impl detail.

### Process

1. Open an issue describing the change and why. Spec changes benefit from
   discussion before implementation.
2. Draft the prose + schema change in a branch.
3. Open a PR; expect careful review. Reviewers will check: RFC language,
   cross-reference consistency, whether the change is additive or
   breaking, and whether the change needs a conformance scenario.

## Contributing to the reference implementation

Standard code-review workflow applies.

### Prerequisites

Contributors touching the reference implementation need:

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (run `uv sync` at the repo root to
  install the workspace virtualenv)
- (Phase 9+) Node.js 20+ for the reference web UI
- (Phase 10+) Docker for the Compose stack

Spec-only contributors need only the Node-based markdown linter; see
[`AGENTS.md`](AGENTS.md#commands) for the exact pinned commands
(version-matched to CI to avoid works-locally / fails-in-CI drift).

### Impl conventions

- Normative behavior in the reference impl must match the spec. If the
  impl is correct and the spec is wrong, the spec should change first.
- Pass the conformance suite (Phase 11+).
- Follow the [style guide](STYLE_GUIDE.md).

## Contributing to the conformance suite

The conformance suite lands in Phase 11 and is not yet implementable.
Once it exists:

- Scenarios must be **implementation-agnostic** — they drive an
  implementation-under-test via its advertised protocol surface, not via
  language-specific hooks.
- A scenario must cite the spec paragraph it validates.

## Questions

Open an issue.
