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

**Phase 6 complete.** The `eden-storage` package ships the `Store`
structural interface for the task store, event log, and
proposal/trial persistence sides of chapter 8 — collapsed into a
single Protocol per §7 implementation latitude. Two conforming
backends satisfy it: `InMemoryStore` (fast, non-durable; moved from
`eden-dispatch`) and `SqliteStore` (durable across restarts via a
WAL-mode SQLite database with `synchronous=FULL`, matching §3.1's
crash-survival requirement). The Protocol covers the spec-literal
`create_task` / `replay` / `read_range` operations alongside the
typed convenience helpers. Both backends share the transition logic
in `_base.py`, and the same conformance scenarios are parametrized
across both — drift from the Protocol surfaces in tests, not in
production. Restart-safety tests close and reopen a SQLite store
mid-experiment to confirm state, event log, and claim tokens
survive, and a monkey-patched `_apply_commit` failure verifies
rollback. The artifact store (§5), `subscribe` streaming (§2.1),
and Postgres remain non-goals for Phase 6; they land in Phase 10 /
Phase 8 / later respectively. Phase 7 is next: the reference git
integrator (`eden-git`). See [`docs/roadmap.md`](docs/roadmap.md)
for the full 13-phase plan.

## Commands

At Phase 6, markdown linting, JSON Schema validation, and the Python
toolchain for the `eden-contracts`, `eden-dispatch`, and `eden-storage`
reference packages are wired up.

| Command | Purpose |
|---|---|
| `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` | Lint all tracked markdown (pinned to CI's version; matches CI exactly) |
| `pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json` | Validate each schema file against the Draft 2020-12 meta-schema (version pinned to CI) |
| `pipx run 'check-jsonschema==0.29.4' --schemafile spec/v0/schemas/experiment-config.schema.json tests/fixtures/experiment/.eden/config.yaml` | Validate the fixture experiment config against its schema |
| `uv sync` | Install/refresh the workspace virtualenv (root + `reference/packages/eden-contracts` + `reference/packages/eden-dispatch` + `reference/packages/eden-storage`) |
| `uv run ruff check .` | Lint Python (config in root `pyproject.toml`) |
| `uv run pyright` | Type-check the reference Python packages |
| `uv run pytest -q` | Run the reference-package test suite (includes schema ↔ model parity) |
| `uv run pytest reference/packages/eden-contracts/tests/test_schema_parity.py` | Run only the schema ↔ Pydantic model parity check |
| `python3 scripts/spec-xref-check.py` | Validate every `§N.M` reference in `spec/v0/*.md` resolves to a real section heading in its target chapter. Run before committing a normative spec change. |

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

### Adding or extending a JSON Schema + Pydantic binding

The `schema-parity` CI job is only as strong as what both sides of the
test actually enforce. Several Pydantic and `jsonschema` defaults let
drift through silently. When adding a new schema — or a new field type
to an existing one — evaluate each of the following; reusable
implementations live in
[`reference/packages/eden-contracts/src/eden_contracts/_common.py`](reference/packages/eden-contracts/src/eden_contracts/_common.py).

+ **Strict numeric parsing.** Top-level models set
  `ConfigDict(strict=True, extra="allow")`. Non-strict mode coerces
  `True`/`"2"` into int, but the schemas treat `type: integer` /
  `type: number` as strict JSON types. New models must keep
  `strict=True`.
+ **Format assertions on both sides.** `format` keywords (`uri`,
  `date-time`, …) are advisory by default in both the `jsonschema`
  library and Pydantic. The schema-side validator wires a custom
  `FormatChecker` in
  [`tests/conftest.py`](reference/packages/eden-contracts/tests/conftest.py),
  and the model side uses the reusable types in `_common.py`. A new
  `format` keyword in any schema requires handlers on *both* sides;
  the `test_format_coverage` test fails loudly if the schema-side
  handler is missing.
+ **Real date-time / URI validation, not just regex or `urlparse`.**
  The regex on `DateTimeStr` accepts impossible values like
  `2026-99-99T…Z`; an `AfterValidator` runs `datetime.fromisoformat`
  to reject those. `UriStr` uses `rfc3986-validator`, not
  `urllib.parse.urlparse`, which accepts malformed schemeful URIs
  (e.g., spaces in the host).
+ **Null vs absent.** JSON Schema's `type: X` rejects explicit `null`,
  but Pydantic's `X | None = None` accepts it. Wrap every optional
  typed field with `NotNone` from `_common.py` so absent is accepted
  and explicit null is rejected.
+ **Round-trip emission.** `model.model_dump(mode="json",
  exclude_none=True)` must re-validate against the schema. Add any
  new model to `tests/test_roundtrip.py` so this is checked.
+ **Corpus coverage.** Parity is asserted over the fixture corpus in
  `tests/cases.py`. A new field type deserves at least one accept
  fixture and one reject fixture per constraint the schema imposes
  (required, pattern, enum, min/max, format, cross-field).

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
