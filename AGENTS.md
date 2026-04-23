# AGENTS.md

This file provides guidance to AI agents working with this repository.

## What this repo is

EDEN is a **protocol** for directed-code-evolution orchestration, not a
single product. The repo contains three distinct kinds of artifact, each
with different change discipline:

| Layer | Path | Authoritative? | Change discipline |
|---|---|---|---|
| Protocol specification | [`spec/`](spec/) | Yes — the source of truth | RFC-style: versioned, normative (MUST/SHOULD/MAY), carefully reviewed |
| Reference implementation | [`reference/`](reference/) | No — *one* valid impl | Normal code-review discipline |
| Conformance suite | [`conformance/`](conformance/) | Normative for tests | Black-box; must be implementation-agnostic |

Non-normative human docs live in [`docs/`](docs/).

## Current phase

**Phase 3 complete.** The `reference/packages/eden-contracts` package
ships Pydantic v2 bindings for the six spec/v0 JSON Schemas, along with
the first Python toolchain wiring (uv workspace, ruff, pyright, pytest)
and four new CI jobs — `python-lint`, `python-typecheck`, `python-test`,
and `schema-parity`. All six CI checks on `main` are green. See
[`docs/roadmap.md`](docs/roadmap.md) for the full 13-phase plan. Phase 4
is next: the event protocol, integrator chapter, and storage chapter —
and the corresponding event schema.

## Commands

At Phase 3, markdown linting, JSON Schema validation, and the Python
toolchain for the `eden-contracts` reference package are wired up.

| Command | Purpose |
|---|---|
| `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` | Lint all tracked markdown (pinned to CI's version; matches CI exactly) |
| `pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json` | Validate each schema file against the Draft 2020-12 meta-schema (version pinned to CI) |
| `pipx run 'check-jsonschema==0.29.4' --schemafile spec/v0/schemas/experiment-config.schema.json tests/fixtures/experiment/.eden/config.yaml` | Validate the fixture experiment config against its schema |
| `uv sync` | Install/refresh the workspace virtualenv (root + `reference/packages/eden-contracts`) |
| `uv run ruff check .` | Lint Python (config in root `pyproject.toml`) |
| `uv run pyright` | Type-check the reference Python packages |
| `uv run pytest -q` | Run the reference-package test suite (includes schema ↔ model parity) |
| `uv run pytest reference/packages/eden-contracts/tests/test_schema_parity.py` | Run only the schema ↔ Pydantic model parity check |

### Commands that will exist in later phases

These are listed for orientation; the tooling is not wired up yet.

| Command | Lands in |
|---|---|
| `docker compose up` end-to-end | Phase 10 |

## Contribution conventions

### Spec edits

+ Every cross-component contract (wire format, state machine, invariant)
  is defined in `spec/` first, then implemented in `reference/`. When
  they disagree, the spec wins and the impl gets a bug.
+ Spec chapters use RFC 2119 language: MUST, SHOULD, MAY.
+ Spec versions are frozen once stable (`spec/v0/` is a single lineage;
  breaking changes go to `spec/v1/`).
+ Schema changes must update the spec prose, the JSON Schema file, and
  (from Phase 3 onward) the Pydantic bindings in lockstep. CI will
  enforce schema ↔ model parity once both exist.

### Reference implementation

+ Normal code-review discipline.
+ Normative behavior is tested against the conformance suite (once it
  exists in Phase 11).
+ The reference impl is explicitly not a monopoly on correctness. A
  third-party implementation that passes conformance is equally valid.

## Commit guidelines

+ Short imperative subjects (e.g., "Add event protocol chapter",
  "Pin task state machine").
+ For pull requests, include: what the change is, which phase it
  advances, and any spec ↔ impl implications.

## Related docs

+ [`docs/naming.md`](docs/naming.md) — what EDEN is, at the concept level.
+ [`docs/roadmap.md`](docs/roadmap.md) — the 13-phase build-up plan with
  unit-level decomposition.
+ [`docs/plans/`](docs/plans/) — active implementation plans.
+ [`docs/archive/`](docs/archive/) — historical reference docs.
+ [`STYLE_GUIDE.md`](STYLE_GUIDE.md) — formatting and naming conventions.
+ [`CONTRIBUTING.md`](CONTRIBUTING.md) — setup and PR workflow.
