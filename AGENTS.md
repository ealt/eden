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

**Phase 8a complete.** `eden-wire` ships an HTTP binding for the
chapter 4 / 5 / 6 §3.4 / 8 §§1.1–2.1 operations specified in the
new [`spec/v0/07-wire-protocol.md`](spec/v0/07-wire-protocol.md)
chapter. The package exposes a FastAPI ``make_app(store)`` that
routes every wire endpoint to a ``Store`` instance, and a
``StoreClient`` that satisfies the same ``Store`` Protocol against
the HTTP surface — so existing callers (dispatch driver,
integrator) work across the process boundary unchanged. Errors
round-trip as RFC 7807 problem+json with a closed vocabulary of
``eden://error/<name>`` types (§7). ``Store.integrate_trial`` is
now same-value idempotent (§5 of the chapter); the ``Integrator``
distinguishes different-SHA divergence (``AtomicityViolation``,
no ref compensation) from other synchronous rejections (normal
§3.4 compensating-delete flow). ``StoreClient.integrate_trial``
reconciles transport-indeterminate failures via read-back: the
three outcomes are confirmed success, confirmed divergence
(``InvalidPrecondition``), or ``IndeterminateIntegration`` when
the server's outcome cannot be determined. Polling +
long-poll-style subscribe are both bound (§6). Worker-process
extraction, SSE/WebSocket push, and cut-over of in-process paths
remain Phase 8b / 8c.

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
Phase 8 / later respectively.

**Phase 7b complete.** `eden-git` now also ships the `Integrator`
that composes `GitRepo` with a `Store` to promote `success` trials
per chapter 6. Given a trial with a recorded `commit_sha`,
`Integrator.integrate` builds the §3.2 single-commit squash
(worker-tip tree plus the eval manifest at
`.eden/trials/<trial_id>/eval.json`), writes the
`refs/heads/trial/<id>-<slug>` ref via zero-oid CAS, and routes the
store's atomic `integrate_trial` write for `trial_commit_sha` and
the `trial.integrated` event. On store failure the ref is
compensatingly deleted per §3.4, matching the post-promotion
reading recorded in
[`spec/v0/design-notes/integrator-atomicity.md`](spec/v0/design-notes/integrator-atomicity.md).
Re-invocation on an already-promoted trial is a verified no-op
(§5.3): ref SHA, squash tree shape, and manifest bytes are
re-derived and compared. §2 preconditions (`status == success`,
`commit_sha` reachable from `branch` tip), §1.4 reachability, and
§2 metrics validity are all enforced up front; the new public
`Store.validate_metrics` closes the §2 MUST-NOT-promote clause even
if upstream orchestrator validation were bypassed. The spec itself
was tightened at §3.4 to make the post-promotion reading explicit.
`eden-dispatch.run_experiment` now takes an `integrate_trial:
Callable[[str], object]` hook in place of the Phase 5
placeholder `integrator_commit_factory` parameter. Eval-manifest
bytes are deterministic (sorted keys, `indent=2`, trailing newline)
to make §5.3 idempotency re-derivation stable.

**Phase 7a complete.** `eden-git`'s subprocess wrapper ships
`GitRepo` covering ref/object inspection (`rev_parse`, `resolve_ref`,
`list_refs`, `is_ancestor`, `ls_tree`), plumbing (`write_blob`,
`write_tree_from_entries`, `write_tree_with_file`, `commit_tree`,
`create_ref`, `update_ref`), worktree management, and branch management.
Author identity and `commit.gpgsign=false` are pinned per-invocation so
the user's ambient git config never leaks into integrator commits. See
[`docs/roadmap.md`](docs/roadmap.md) for the full 13-phase plan.

## Commands

At Phase 8a, markdown linting, JSON Schema validation, and the Python
toolchain for the `eden-contracts`, `eden-dispatch`, `eden-git`,
`eden-storage`, and `eden-wire` reference packages are wired up.

| Command | Purpose |
|---|---|
| `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` | Lint all tracked markdown (pinned to CI's version; matches CI exactly) |
| `pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json` | Validate each schema file against the Draft 2020-12 meta-schema (version pinned to CI) |
| `pipx run 'check-jsonschema==0.29.4' --schemafile spec/v0/schemas/experiment-config.schema.json tests/fixtures/experiment/.eden/config.yaml` | Validate the fixture experiment config against its schema |
| `uv sync` | Install/refresh the workspace virtualenv (root + `reference/packages/eden-contracts` + `reference/packages/eden-dispatch` + `reference/packages/eden-git` + `reference/packages/eden-storage` + `reference/packages/eden-wire`) |
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
