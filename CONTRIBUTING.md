# Contributing to EDEN

Thanks for your interest in EDEN.

EDEN is a **protocol** for directed evolution orchestration. This repo contains three kinds of artifact that call for different contribution discipline: the **specification** (`spec/`), the **reference implementation** (`reference/`), and the **conformance suite** (`conformance/`). Read the section that matches where your change lives.

## Current phase

**Phase 11 complete.** Chapters 00–09 of spec/v0 are on the protected `main`, alongside the full reference implementation (workspace under [`reference/`](reference/) — six services + five packages on a Compose stack with Postgres + Forgejo) and the conformance suite at the **v1+roles+integrator** level (110 scenarios green). CI gates the reference impl on docs-lint, schema-validity, schema-parity, python-lint, python-typecheck, python-test, python-test-postgres, conformance, compose-smoke, compose-smoke-subprocess, compose-smoke-subprocess-docker, and compose-e2e. See [`CHANGELOG.md`](CHANGELOG.md) for the canonical per-chunk "what's done" record, [`AGENTS.md`](AGENTS.md) for the agent contract (commands, naming discipline, pitfalls), and [`docs/roadmap.md`](docs/roadmap.md) for the remaining Phase 12–13 plan.

If you want to contribute, useful areas are: spec gaps surfaced by [`docs/conformance-coverage.md`](docs/conformance-coverage.md), open issues labeled [`manual-ui`](https://github.com/ealt/eden/issues?q=is%3Aopen+label%3Amanual-ui), or scoping work for Phase 12 (multi-experiment / control plane).

## Contributing to the spec

The spec is the authoritative source of protocol semantics. A change to the spec is a change to what "EDEN" means.

### Spec conventions

- **Versioning.** `spec/v0/` is a single lineage. Within a version, changes should be additive or clarifying. Breaking changes go to a new version (`spec/v1/`, …), never in-place.
- **Normative language.** Use RFC 2119 keywords: **MUST**, **SHOULD**, **MAY**. If prose doesn't use one, it's informative.
- **Wire-format changes propagate.** A change to a JSON Schema file under `spec/v*/schemas/` must be reflected in the Markdown chapter and in the Pydantic bindings in `reference/packages/eden-contracts/`. CI's `schema-parity` job enforces that models and schemas agree on the accept/reject corpus and on round-trip emission.
- **No technology choices in normative text.** The spec talks about *semantics*, not mechanisms. "A conforming task store MUST provide atomic claim with linearizable semantics" — yes. "Uses Postgres `SELECT ... FOR UPDATE`" — no, that's reference-impl detail.

### Process

1. Open an issue describing the change and why. Spec changes benefit from discussion before implementation.
2. Draft the prose + schema change in a branch.
3. Open a PR; expect careful review. Reviewers will check: RFC language, cross-reference consistency, whether the change is additive or breaking, and whether the change needs a conformance scenario.

## Contributing to the reference implementation

Standard code-review workflow applies.

### Prerequisites

Contributors touching the reference implementation need:

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (run `uv sync` at the repo root to install the workspace virtualenv)
- Docker (for the Compose stack and the docker-backed CI smoke jobs)

The reference web UI is server-side Jinja with HTMX vendored under `reference/services/web-ui/src/eden_web_ui/static/` — there is **no** Node runtime requirement for the UI. Node is needed only for `npx markdownlint-cli2` (spec contributors); see [`AGENTS.md`](AGENTS.md#commands) for the exact pinned commands (version-matched to CI to avoid works-locally / fails-in-CI drift).

### Impl conventions

- Normative behavior in the reference impl must match the spec. If the impl is correct and the spec is wrong, the spec should change first.
- Pass the conformance suite (Phase 11+).
- Follow the [style guide](STYLE_GUIDE.md).

## Contributing to the conformance suite

The conformance suite lives under [`conformance/`](conformance/) at the v1+roles+integrator level (chunk 11d).

- Scenarios must be **implementation-agnostic** — they drive an implementation-under-test via its advertised protocol surface (the chapter-7 HTTP binding), not via language-specific hooks.
- A scenario must cite the spec paragraph it validates. The first line of its docstring carries the citation in the form `spec/v0/<chapter>.md §<sec>`; [`conformance/src/conformance/tools/check_citations.py`](conformance/src/conformance/tools/check_citations.py) gates this in CI.
- See [`docs/conformance-coverage.md`](docs/conformance-coverage.md) for the current MUST/SHOULD coverage matrix; new scenarios that close uncovered MUSTs are especially welcome.

## Questions

Open an issue.
