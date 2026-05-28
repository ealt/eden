# Issue #122 — Evaluatable baseline variant (seed becomes a `kind="baseline"` Variant)

GitHub issue: [#122](https://github.com/ealt/eden/issues/122). Labels: `enhancement`, `manual-ui`, `priority:2-planned`.

## 1. Context

Today the experiment seed — the single commit on `main` at experiment start, captured as `EDEN_BASE_COMMIT_SHA` ([`docs/glossary.md`](../glossary.md) §8) — exists only as a git ref. It is **not** a `Variant` record. The first `Variant` in any experiment is the executor's output of the first dispatched idea ([`02-data-model.md`](../../spec/v0/02-data-model.md) §9; lineage hand-off `ready idea → execution task → variant`, glossary §3.2).

This leaves a gap: operators want to compare a variant's metrics against "what did the seed score?", but the seed has no metrics because it was never evaluated. For the lineage-tree visualization (a separate issue) the seed is a colorless root with no comparison point.

The fix is to promote the seed to a first-class `Variant` with a new discriminator `kind == "baseline"`. The orchestrator creates it at experiment startup with `commit_sha = base_commit_sha`. From there, three paths:

- **Default — real evaluation.** The baseline rides the orchestrator's existing `evaluation_dispatch` decision ([`02-data-model.md`](../../spec/v0/02-data-model.md) §2.4 decision-type 3): it is created `starting` with `commit_sha` set, so the orchestrator dispatches an `evaluation` task for it like any other variant. The evaluator scores the seed against `evaluation_schema`; the baseline ends up `success` with real metrics, via the same mechanism as every other score.
- **Optional override — config-supplied metrics.** When the config carries a `baseline.metrics` block, the orchestrator creates the baseline already terminal (`success` + the supplied metrics), skipping the evaluation dispatch. Useful when the evaluator is expensive (LLM/human) or a deterministic known-good baseline is wanted without spending evaluation budget.
- **Suppression.** `baseline.enabled: false` suppresses baseline creation entirely; the seed stays out of the variant table and the tree shows it as a colorless root.

The work is a real but tractable spec amendment plus a focused reference-impl change. The evaluator role is **unchanged** — from its perspective a baseline is just another variant with a `commit_sha` (confirmed: the reference evaluator host reads the `Variant` and passes it to the scoring fn with no kind/provenance branching, [`reference/services/evaluator/src/eden_evaluator_host/host.py`](../../reference/services/evaluator/src/eden_evaluator_host/host.py)).

## 2. Decisions captured before drafting

These shape the plan's naming map, scope, and schema surfaces. They follow the issue's proposal; the operator was offered the alternatives and these are the defaults selected. **They are open to override at plan-PR review** — flagged here so codex-review and the operator can challenge them rather than missing them in the diff.

1. **Field name: `Variant.kind`.** Matches the issue. `kind` already names the task role-routing field (`ideation`/`execution`/`evaluation`, glossary §3.1); reusing it on `Variant` overloads the term across two entities. Resolution: the glossary gains two scoped sub-entries ("task kind" vs "variant kind") and the data-model prose names the field as `Variant.kind` unambiguously. Alternatives considered and rejected: `variant_kind` (verbose, diverges from issue wording), `origin` (new vocabulary not in the issue). If review prefers disambiguation-by-name, `variant_kind` is the fallback — call it out before impl.

2. **On by default.** Every experiment auto-creates and evaluates a seed baseline unless suppressed. Matches the issue's "default = real evaluation." Opt-out via `baseline.enabled: false`. Blast radius on the reference fixtures/smokes is bounded because the smoke assertions are lower-bounds (`≥`) — see §8.4. Alternative (opt-in) rejected because it diverges from the issue and the eval-budget concern is already handled by the `metrics` override + the suppression flag.

3. **Unified `baseline:` config block.** One coherent place for both the suppression flag and the optional metrics override:

   ```yaml
   baseline:
     enabled: true            # default true; false suppresses baseline creation
     metrics:                 # optional; when present the orchestrator stamps these and skips evaluation dispatch
       score: 0.5
   ```

   The issue showed a flat `baseline_metrics:` field; the unified block keeps suppression and override adjacent and avoids two loosely-related top-level keys. An absent `baseline:` block is equivalent to `{enabled: true}` (default-on, real evaluation).

4. **No new wire op for the override stamp.** The override path reuses `create_variant` by relaxing its status precondition for `kind == "baseline"` (a baseline MAY be created directly in `success` with metrics; non-baseline variants still MUST start `starting`). This avoids a second wire endpoint. Alternative (a dedicated `stamp-baseline-metrics` wire op, or synthesizing a self-submitted evaluation task) rejected for surface cost — see §D.4.

5. **Baseline is never integrated; it does not block termination.** The integrator skips `kind == "baseline"` (the baseline already points at the seed on `main`; it gets no `variant/*` ref). Because a baseline can reach `success` without a `variant_commit_sha`, the spec's termination-drain rule ([`02-data-model.md`](../../spec/v0/02-data-model.md) §2.5) and the `integration` decision predicate (§2.4) both gain a `kind != "baseline"` carve so the baseline does not block termination forever — see §D.5. This is load-bearing and is the single subtlest part of the change.

These five are not up for re-litigation in codex-review unless review surfaces a load-bearing contradiction with another spec MUST.

## 3. Background facts established by exploration

Pinned here so the plan's design choices are auditable against the actual surfaces (not spec prose alone — per the AGENTS.md "verify wire-touching shapes against the actual dataclasses" pitfall).

- **Variant status lifecycle** (glossary §3.2, [`02-data-model.md`](../../spec/v0/02-data-model.md) §9, [`04-task-protocol.md`](../../spec/v0/04-task-protocol.md) §4.3):
  - execution `success` → writes `commit_sha`, variant **stays `starting`**; orchestrator then dispatches an evaluation task.
  - evaluation `success` → variant transitions `starting → success` with the evaluation payload; integrator then integrates it (writes `variant_commit_sha`).
  - `evaluation_error` → variant stays `starting` (re-evaluable).
- **`evaluation_dispatch` predicate** ([`reference/packages/eden-dispatch/src/eden_dispatch/driver.py`](../../reference/packages/eden-dispatch/src/eden_dispatch/driver.py), `_list_variants_needing_evaluation`): `status == "starting"` AND `commit_sha is not None` AND no live evaluation task. The baseline (created `starting` with `commit_sha = seed`) matches this with no code change — that is how it "rides the path."
- **`integration` predicate** (same file, `_integrate_successful_variants`): `status == "success"` AND `variant_commit_sha is None`. The baseline reaches this state after evaluation, so it **must** be excluded here (decision 5).
- **`create_variant`** ([`reference/packages/eden-storage/src/eden_storage/_base.py`](../../reference/packages/eden-storage/src/eden_storage/_base.py)): enforces `status == "starting"`, experiment-id match, uniqueness; **no no-op tree check**. Emits `variant.started`.
- **Variant contract model** ([`reference/packages/eden-contracts/src/eden_contracts/variant.py`](../../reference/packages/eden-contracts/src/eden_contracts/variant.py)): `model_config = ConfigDict(strict=True, extra="allow")`; the evaluation payload field is named **`evaluation`** (the data-model §9.1 prose calls it `metrics`; the on-tree manifest field is `metrics` per [`06-integrator.md`](../../spec/v0/06-integrator.md) §4.2 — this prose/field drift is pre-existing and out of scope). `idea_id` is currently **required**.
- **ExperimentConfig model** ([`reference/packages/eden-contracts/src/eden_contracts/config.py`](../../reference/packages/eden-contracts/src/eden_contracts/config.py)): `strict=True, extra="allow"`; optional blocks (`dispatch_mode`, `ideation_policy`) are the pattern `baseline` follows.
- **Seed SHA is not stored on the experiment.** It flows as `EDEN_BASE_COMMIT_SHA` to the ideator host. The orchestrator does **not** currently receive it. This is a wiring gap the plan must close (§D.2): the orchestrator needs the base commit SHA to create the baseline.
- **Evaluator host** needs **no change** (uniform variant contract).
- **Wire variant representation** ([`07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §4) is a transport binding of the [`08-storage.md`](../../spec/v0/08-storage.md) §1.7 ops; the variant JSON matches [`variant.schema.json`](../../spec/v0/schemas/variant.schema.json) exactly, so a new `kind` field is wire-observable on `read_variant` / `list_variants` automatically.

## 4. Design

### D.1 Spec: `Variant.kind` field + baseline semantics (`02-data-model.md` §9)

Add an optional `kind` field to the variant data model (§9.1 table) and a new subsection §9.4 "Baseline variants" describing the semantics. Proposed §9.1 row:

| Field | Required | Type | Description |
|---|---|---|---|
| `kind` | no | string | Variant classifier. Absent (the default) for ordinary executor-produced variants. `"baseline"` marks the experiment seed promoted to a first-class variant (§9.4). |

§9.4 prose (normative) covers:

- A `kind == "baseline"` variant represents the experiment seed (`commit_sha == base_commit_sha`). At most one baseline variant per experiment (single-baseline; multi-baseline is out of scope).
- For a baseline variant, `idea_id` MAY be absent (the seed has no producing idea). For every other variant `idea_id` MUST be present (carve in invariant #2, §D.6).
- `parent_commits` for a baseline is `[base_commit_sha]` (the seed framed as its own parent). The baseline's `commit_sha` therefore equals its single parent — the no-op case — which is permitted for baselines (§D.3).
- A baseline variant is **never integrated**: it receives no `variant/*` commit and no `variant_commit_sha`. It is already reachable on `main` (§D.5, [`06-integrator.md`](../../spec/v0/06-integrator.md) §2 carve).
- A baseline reaches a terminal `success` either by ordinary evaluation (default path) or by the orchestrator stamping config-supplied metrics at creation (override path). The metrics MUST validate against `evaluation_schema` (§9.2 applies unchanged).
- `error` / `evaluation_error` are reachable for a baseline exactly as for any variant (e.g. the evaluator fails to score the seed); no special-casing.

`variant.schema.json` changes:

- Add `kind`: `{"type": "string", "enum": ["baseline"]}` (optional; absent = ordinary).
- Make `idea_id` conditionally required: drop it from top-level `required`, add an `if/then` — `if kind != "baseline"` (expressed as `not: {properties: {kind: {const: "baseline"}}}` or an `allOf` branch) `then: {required: ["idea_id"]}`. The simplest robust shape is an `allOf` with two branches: when `kind == "baseline"`, `idea_id` is optional; otherwise `required`. Pin the exact shape during impl and assert it with schema-parity fixtures (§D.8).

### D.2 Orchestrator: create the baseline at startup

The orchestrator service gains the base commit SHA and creates the baseline once, before/at the start of its decision loop.

- **Wiring.** Thread `EDEN_BASE_COMMIT_SHA` into the orchestrator service (CLI flag `--base-commit-sha` + env fallback, mirroring how the ideator host receives it). Without it, the orchestrator cannot create a baseline; if the config has `baseline.enabled: true` and the SHA is absent, fail fast at startup with a clear error (do not silently skip).
- **Creation step.** At startup, if `baseline.enabled` (default true):
  - Compute a **deterministic** baseline `variant_id` (e.g. `baseline` or `baseline-<experiment_id>`) so concurrent orchestrator instances converge on the same record.
  - If `baseline.metrics` is **absent** (default path): `create_variant(kind="baseline", status="starting", commit_sha=<seed>, parent_commits=[<seed>], started_at=now)`. The existing `evaluation_dispatch` decision then picks it up.
  - If `baseline.metrics` is **present** (override path): `create_variant(kind="baseline", status="success", commit_sha=<seed>, parent_commits=[<seed>], evaluation=<metrics>, completed_at=now, started_at=now)` (see §D.4 for the precondition relaxation + events).
  - Wrap in `try/except AlreadyExists: pass` for multi-instance idempotency (deterministic id makes the second instance's create a no-op).
- **Placement.** A dedicated `ensure_baseline_variant(store, config, base_commit_sha)` helper invoked from the orchestrator startup path ([`reference/services/orchestrator/src/eden_orchestrator/cli.py`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py) / `loop.py`), kept small to stay under the complexity gate.

### D.3 No-op rule exception (`03-roles.md` §3.3, §4.2)

The baseline's `commit_sha` equals its single parent — a literal no-op. The existing prohibition is a role-side executor MUST (§3.3) and a task-store SHOULD (§4.2, the `eden://error/no-op-variant` rejection). Both are scoped to **executor submissions**, not `create_variant`. The reference `create_variant` performs no no-op check today, so there is no impl conflict — but the spec prose must be made coherent:

- §3.3: append a sentence — the no-op prohibition applies to executor `VariantSubmission`s; it does **not** apply to `kind == "baseline"` variants, which represent the unmodified seed and are created directly by the orchestrator, not via executor submission.
- §4.2: the SHA-equality SHOULD-reject MUST exempt `kind == "baseline"` so a task store that implements the deeper check does not reject a legitimate baseline at `create_variant`.

### D.4 `create_variant` precondition relaxation for the override path

Relax the store's `create_variant` precondition: a `kind == "baseline"` variant MAY be created directly in `status == "success"` (carrying `evaluation` + `completed_at`); all non-baseline variants MUST still be created `starting`. On the direct-success path the store:

- validates `evaluation` against `evaluation_schema` (the §9.2 rule applies at create time, mirroring evaluation acceptance);
- emits the same variant-success event the evaluation-acceptance path emits **in addition to** (or composed with) `variant.started`. The exact event name(s) MUST be read from [`05-event-protocol.md`](../../spec/v0/05-event-protocol.md) and [`reference/packages/eden-contracts/src/eden_contracts/event.py`](../../reference/packages/eden-contracts/src/eden_contracts/event.py) during impl — do not assume; per the AGENTS.md "check event.py before assuming a field exists" pitfall.

Spec touch: [`08-storage.md`](../../spec/v0/08-storage.md) §1.7 (`create_variant`) and [`04-task-protocol.md`](../../spec/v0/04-task-protocol.md) §4.3 note that a baseline variant MAY be created terminal-`success`. [`07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §4 `create_variant` body gains the documented baseline shape.

Rejected alternatives:

- **Dedicated `stamp-baseline-metrics` wire op** — a new endpoint just for the override stamp. More wire surface than reusing `create_variant`.
- **Synthesize a self-submitted evaluation task** — the orchestrator creates an `evaluation` task for the baseline and self-claims/submits/accepts with config metrics. Reuses all machinery but the orchestrator has to act as a claiming worker, which is awkward and adds a fake task to the store.

### D.5 Integrator skip + termination-drain carve (`06-integrator.md` §2, `02-data-model.md` §2.4 / §2.5)

A baseline reaches `success` without a `variant_commit_sha`. Three places must learn that a baseline is intentionally never integrated:

1. **Integrator trigger** ([`06-integrator.md`](../../spec/v0/06-integrator.md) §2): add to the "MUST NOT integrate" list — a conforming integrator MUST NOT integrate a `kind == "baseline"` variant (it has no `work/*` branch to squash and already points at the seed on `main`).
2. **`integration` decision predicate** ([`02-data-model.md`](../../spec/v0/02-data-model.md) §2.4 decision-type 4): "for each `success` variant with `variant_commit_sha` unset **and `kind != "baseline"`**." Impl: `_integrate_successful_variants` skips `kind == "baseline"`.
3. **Termination-drain rule** ([`02-data-model.md`](../../spec/v0/02-data-model.md) §2.5): the drain clause currently reads "integration MUST continue to run until no `status == "success"` variants without `variant_commit_sha` remain." Amend to exclude baselines: "...no `status == "success"` variants with `kind != "baseline"` and `variant_commit_sha` unset remain." **This is the load-bearing carve** — without it a default-on baseline that lands in `success` blocks experiment termination forever.

Defense-in-depth: the `integrate_variant` wire op ([`07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §5) rejects `kind == "baseline"` with `eden://error/invalid-precondition`, so a manual/operator integrate of a baseline fails loudly rather than producing a malformed `variant/*` ref.

### D.6 `parallel_variants` and invariant carves

- **`parallel_variants` budget.** The baseline has no execution task and is not a candidate the executor produced; it MUST NOT count against the `parallel_variants` in-flight bound used by `execution_dispatch`. Audit the in-flight/running-variant counter ([`ExperimentStateView`](../../reference/packages/eden-dispatch/src/eden_dispatch/state_view.py) `running_variant_count` and the execution-dispatch budget check) and exclude `kind == "baseline"`. (It legitimately occupies one evaluation slot on the default path — that is intended.)
- **Invariant #2 (reference integrity)** ([`02-data-model.md`](../../spec/v0/02-data-model.md) §10): "Every `idea_id` referenced by ... a variant MUST name an idea" — add "(a `kind == "baseline"` variant MAY omit `idea_id`)."

### D.7 Experiment config: `baseline` block (`02-data-model.md` §2, schema, contracts)

- [`02-data-model.md`](../../spec/v0/02-data-model.md) §2.3 / a new §2.7 documents the optional `baseline` block (`enabled: bool` default true; `metrics: object` optional, subset of `evaluation_schema` keys/types). Posture mirrors `ideation_policy`: a conforming orchestrator MUST accept its absence (≡ `{enabled: true}`); when `metrics` is present and `enabled: false`, that is a config error (suppressing a baseline while supplying its metrics) — fail config validation.
- [`experiment-config.schema.json`](../../spec/v0/schemas/experiment-config.schema.json): add `baseline` object property (`enabled` boolean, `metrics` object), with the enabled-false-with-metrics conflict expressed as an `allOf`/`not` branch (pin exact shape during impl).
- [`config.py`](../../reference/packages/eden-contracts/src/eden_contracts/config.py): add `baseline: Annotated[BaselineConfig | None, NotNone] = None` with a small `BaselineConfig` model + a model validator for the enabled/metrics conflict. Note `metrics` cannot be type-checked against `evaluation_schema` generically in the schema (same limitation as variant `evaluation`, §9.2) — the orchestrator validates it at runtime.

### D.8 Contracts + schema-parity

- [`variant.py`](../../reference/packages/eden-contracts/src/eden_contracts/variant.py): add `kind: Annotated[Literal["baseline"] | None, NotNone] = None`; make `idea_id` optional (`str | None`) with a `model_validator` enforcing presence unless `kind == "baseline"`.
- [`tests/cases.py`](../../reference/packages/eden-contracts/tests/cases.py): add variant fixtures — accept `{kind: "baseline", no idea_id, ...}`, accept `{kind absent, idea_id present}`, accept `{kind: "baseline", status: "success", evaluation: {...}}`, reject `{kind: "unknown"}`, reject `{kind absent, idea_id absent}` (idea_id now conditionally required), reject `{kind: "baseline" present but commit_sha malformed}` as applicable. Add experiment-config fixtures for the `baseline` block (accept enabled/metrics combos; reject `enabled:false`+`metrics`).
- The `schema-parity` job asserts both sides agree on every fixture ([`test_schema_parity.py`](../../reference/packages/eden-contracts/tests/test_schema_parity.py)); the conditional-required `idea_id` is the riskiest parity surface (Pydantic model_validator vs JSON-Schema if/then) — give it the most fixtures.

### D.9 Docs

- [`docs/glossary.md`](../glossary.md): add "baseline variant" to §3 (data shapes); disambiguate §3.1 `kind` into "task kind" and "variant kind" sub-entries; cross-reference the existing "seed commit / base commit" entry (§8).
- [`docs/user-guide.md`](../user-guide.md) §2: document the `baseline` config block in the experiment-config field table + a short example.
- [`tests/fixtures/experiment/.eden/config.yaml`](../../tests/fixtures/experiment/.eden/config.yaml): default-on means no change is strictly required; optionally add an explicit `baseline:` block as a representative example (decide during impl based on smoke determinism — §8.4).
- [`CHANGELOG.md`](../../CHANGELOG.md) `[Unreleased]` entry + [`docs/roadmap.md`](../roadmap.md) line per the AGENTS.md chunk-completion discipline.

## 5. Naming map (old → new)

| Surface | Identifier | Disposition |
|---|---|---|
| Variant data model / schema / contract | `Variant.kind` (`"baseline"` \| absent) | **new** field |
| Experiment config | `baseline` block (`enabled`, `metrics`) | **new** optional block |
| Orchestrator | `ensure_baseline_variant(...)` helper | **new** |
| Orchestrator CLI/env | `--base-commit-sha` / `EDEN_BASE_COMMIT_SHA` (orchestrator now consumes it) | **new** consumer of an existing env var |
| Contracts | `BaselineConfig` model | **new** |
| Glossary | "baseline variant" entry; "task kind" / "variant kind" sub-entries | **new / disambiguation** |
| Conformance | `CONFORMANCE_GROUP = "Baseline variant"` | **new** group |

No identifiers are renamed or retired. `kind` is reused on a new entity (decision 1); the glossary disambiguation is the mitigation. The `rename-discipline` CI job is unaffected (no legacy-vocab patterns introduced) but must still pass. Validate every new identifier against [`docs/glossary.md`](../glossary.md) before introducing it (memory: cold-re-read the glossary after the change).

## 6. Migration / cleanup

EDEN is pre-external-user (CLAUDE.md no-backwards-compat-shims posture), so this lands as **one cohesive change** with no migration scaffolding:

- No deprecation/alias for the absent-`kind` state — ordinary variants simply omit `kind`.
- Existing experiments started before this change have no baseline variant; that is acceptable (no backfill tooling). A re-created experiment gets one.
- The `idea_id`-now-optional change is a relaxation (no existing variant becomes invalid).
- No data migration on the store: `kind` is additive and optional; existing rows read back with `kind` absent.
- The default-on choice means the reference fixtures/smokes acquire a baseline automatically; the smoke assertions are audited (§8.4) rather than gated behind a compat flag.

Nothing is retired in this chunk. (If review flips decision 2 to opt-in, the only delta is the config default + the smoke audit collapses to zero.)

## 7. Conformance impact

Per [`09-conformance.md`](../../spec/v0/09-conformance.md) §6, the suite may only assert what is observable through the chapter-7 HTTP binding. Filter each candidate MUST through `IUT contract → wire endpoint → readable artifact` (AGENTS.md "conformance-plan MUSTs filtered through the IUT contract" pitfall):

**Wire-observable (in scope — new `CONFORMANCE_GROUP = "Baseline variant"`):**

- `kind` round-trips: `create_variant(kind="baseline")` then `read_variant` / `list_variants` returns `kind == "baseline"`. (Cite [`02-data-model.md`](../../spec/v0/02-data-model.md) §9.4.)
- A `kind == "baseline"` variant in `success` without `variant_commit_sha` is **not** integratable: `integrate_variant` on it returns `eden://error/invalid-precondition`, writes no `variant_commit_sha`, emits no `variant.integrated`. (Cite [`06-integrator.md`](../../spec/v0/06-integrator.md) §2.)
- Override-path metrics validation: `create_variant(kind="baseline", status="success", evaluation=<bad metrics>)` is rejected against `evaluation_schema`; `<good metrics>` accepted. (Cite [`02-data-model.md`](../../spec/v0/02-data-model.md) §9.2.)
- `idea_id` conditional requirement: `create_variant` without `idea_id` is rejected unless `kind == "baseline"`. (Cite [`02-data-model.md`](../../spec/v0/02-data-model.md) §9.4 / §10 invariant 2.)

**Not wire-observable (explicitly deferred / out of scope):**

- The **default-path auto-dispatch** ("the orchestrator automatically dispatches an evaluation task for the baseline") is an orchestrator-role decision, not a task-store wire MUST. The conformance suite drives the wire API and does not assert orchestrator auto-decisions in general (it asserts the *preconditions* the store enforces). This is covered by reference-impl integration tests (orchestrator e2e), not by a conformance scenario. State this deferral in the §5 group entry rather than silently dropping it.

**Group placement:** v1+roles+integrator (the most distinctive MUST — integrator-skips-baseline — is integrator-level). Add one row to [`09-conformance.md`](../../spec/v0/09-conformance.md) §5 citing `02-data-model.md §9.4` + `06-integrator.md §2`. Ensure the three-legged traceability ([`check_citations.py`](../../conformance/src/conformance/tools/check_citations.py)): (1) the scenario file declares `CONFORMANCE_GROUP = "Baseline variant"`; (2) each test's first docstring line cites a section whose text (or an ancestor) contains a `MUST`; (3) the citation lies within the declared group's §5 entry. §9.4 and §2 must carry explicit `MUST` tokens (they do under this design).

Existing variant scenarios (`test_executor_submission.py`, `test_evaluator_submission.py`, `test_integration_preconditions.py`) need an audit but should **not** require behavioral changes — they create ordinary variants (`kind` absent), which is unchanged. Confirm none assert "`idea_id` always required" in a way the relaxation breaks.

## 8. Tricky areas / things to watch

### 8.1 Termination deadlock (most load-bearing)

A default-on baseline that reaches `success` without `variant_commit_sha` will block termination unless §2.5's drain clause is carved (§D.5). This is the failure that would silently hang every experiment's termination. The §D.5 carve plus a reference-impl test that runs an experiment to termination *with* a baseline present is the catch.

### 8.2 The `starting → success` window on the override path

On the override path the baseline must not be transiently visible to `evaluation_dispatch` as a `starting` variant with `commit_sha` set (which would dispatch a redundant evaluation). The chosen design (direct-`success` `create_variant`, §D.4) closes the window: the baseline is never `starting`. The default path deliberately *is* `starting` so it rides dispatch. Verify the orchestrator creates the baseline before the first dispatch iteration.

### 8.3 Orchestrator does not know the seed SHA today

§D.2 wiring is a genuine new surface. Confirm where `EDEN_BASE_COMMIT_SHA` is set in Compose ([`reference/compose/`](../../reference/compose/)) and add it to the orchestrator service env + CLI. Fail-fast when `baseline.enabled` and the SHA is missing.

### 8.4 Smoke/e2e blast radius (default-on)

A baseline adds one `evaluation` `task.completed` and one variant that is **not** integrated. The Compose smokes assert lower-bounds (`≥3 variant.integrated`, `≥9 task.completed`, `≥3 ideation-task task.completed`, per AGENTS.md Commands): adding a baseline only *increases* `task.completed` and leaves `variant.integrated` unchanged, so the `≥` assertions should still hold. **But** with `parallel_variants: 1` the baseline evaluation competes for the single evaluation slot and could perturb quiescence timing — run all four smokes (`smoke.sh`, `smoke-subprocess.sh`, `smoke-subprocess-docker.sh`, `e2e.sh`) and watch for ordering/quiescence regressions, not just count assertions. If a smoke proves flaky, the fallback is an explicit `baseline: {enabled: false}` in the fixture config plus a dedicated baseline-on smoke variant — decide during impl. (AGENTS.md: the smokes are the literal pre-push gate; narrowed pytest subsets are not.)

### 8.5 `evaluation` vs `metrics` field-name drift

The variant object's payload field is `evaluation` (schema/model); the data-model §9.1 prose and the integrator manifest call it `metrics`. The config block uses `baseline.metrics` and the orchestrator writes it into `variant.evaluation`. Be consistent: config key `metrics`, variant field `evaluation`. Do not attempt to fix the pre-existing prose drift here (out of scope; would be its own deferral + issue).

### 8.6 Conditional-required `idea_id` parity

JSON-Schema `if/then` and Pydantic `model_validator` must agree on every fixture, including edge cases (`kind: null` explicit vs absent; `kind: "baseline"` with `idea_id` present — allowed, baselines MAY carry one but needn't). The `NotNone` wrapper interacts with explicit-null handling (AGENTS.md schema-parity guidance). Give this surface the most fixtures.

### 8.7 Multi-instance baseline creation

Deterministic baseline `variant_id` + `create_variant` `AlreadyExists` catch makes concurrent orchestrator startup idempotent. Confirm `create_variant` raises `AlreadyExists` (it does, [`_base.py`](../../reference/packages/eden-storage/src/eden_storage/_base.py)) and that the override path's direct-`success` create is also idempotent (second instance observes the existing record and no-ops; it must not double-emit the success event).

### 8.8 Spec inter-chapter restatement

The baseline carve touches §2.4, §2.5 (data model), §3.3, §4.2 (roles), §2 (integrator), §1.7 (storage), §4.3 (task protocol). Per the AGENTS.md "spec inter-chapter restatement is a conflict surface" pitfall, grep every chapter for the no-op rule and the integration-drain rule and ensure the baseline carve is stated canonically once and merely cross-referenced elsewhere — do not let two chapters restate it with divergent wording.

## 9. Files to touch

**Spec (7 files):**

- [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) — §9.1 `kind` row; new §9.4 baseline semantics; §2 (new §2.7) `baseline` config block; §2.4 integration-decision predicate carve; §2.5 termination-drain carve; §10 invariant-2 carve.
- [`spec/v0/03-roles.md`](../../spec/v0/03-roles.md) — §3.3 + §4.2 no-op exception.
- [`spec/v0/04-task-protocol.md`](../../spec/v0/04-task-protocol.md) — §4.3 note on direct-`success` baseline create.
- [`spec/v0/06-integrator.md`](../../spec/v0/06-integrator.md) — §2 baseline-skip clause.
- [`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) — §4 `create_variant` baseline shape; §5 `integrate_variant` baseline rejection.
- [`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) — §1.7 `create_variant` precondition relaxation.
- [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) — §5 "Baseline variant" group row (+ deferred-auto-dispatch note).

**Schemas (2 files):**

- [`spec/v0/schemas/variant.schema.json`](../../spec/v0/schemas/variant.schema.json) — `kind` enum; conditional-required `idea_id`.
- [`spec/v0/schemas/experiment-config.schema.json`](../../spec/v0/schemas/experiment-config.schema.json) — `baseline` block.

**Contracts (3 files):**

- [`reference/packages/eden-contracts/src/eden_contracts/variant.py`](../../reference/packages/eden-contracts/src/eden_contracts/variant.py) — `kind`; conditional `idea_id` validator.
- [`reference/packages/eden-contracts/src/eden_contracts/config.py`](../../reference/packages/eden-contracts/src/eden_contracts/config.py) — `BaselineConfig`; `baseline` field + conflict validator.
- [`reference/packages/eden-contracts/tests/cases.py`](../../reference/packages/eden-contracts/tests/cases.py) — variant + config fixtures.

**Storage (2 files):**

- [`reference/packages/eden-storage/src/eden_storage/_base.py`](../../reference/packages/eden-storage/src/eden_storage/_base.py) — `create_variant` precondition relaxation + baseline metrics validation + success-event emission.
- [`reference/packages/eden-storage/src/eden_storage/protocol.py`](../../reference/packages/eden-storage/src/eden_storage/protocol.py) — docstring update for the baseline create contract.

**Dispatch / orchestrator (3 files):**

- [`reference/packages/eden-dispatch/src/eden_dispatch/driver.py`](../../reference/packages/eden-dispatch/src/eden_dispatch/driver.py) — `_integrate_successful_variants` skip baseline; in-flight counter excludes baseline.
- [`reference/packages/eden-dispatch/src/eden_dispatch/state_view.py`](../../reference/packages/eden-dispatch/src/eden_dispatch/state_view.py) — `running_variant_count` excludes baseline (confirm).
- [`reference/services/orchestrator/src/eden_orchestrator/cli.py`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py) + `loop.py` — `--base-commit-sha` wiring; `ensure_baseline_variant` at startup.

**Compose (as needed):**

- [`reference/compose/`](../../reference/compose/) — pass `EDEN_BASE_COMMIT_SHA` to the orchestrator service (audit `compose.yaml` + overlays).

**Conformance (1+ files):**

- `conformance/scenarios/test_baseline_variant.py` (new) — the wire-observable scenarios from §7.

**Docs (4 files):**

- [`docs/glossary.md`](../glossary.md), [`docs/user-guide.md`](../user-guide.md), [`CHANGELOG.md`](../../CHANGELOG.md), [`docs/roadmap.md`](../roadmap.md). Optionally [`tests/fixtures/experiment/.eden/config.yaml`](../../tests/fixtures/experiment/.eden/config.yaml).

Approximate diff: ~120 lines spec, ~40 schema, ~120 contracts+fixtures, ~80 storage, ~80 dispatch/orchestrator, ~80 conformance, ~50 docs.

## 10. Chunked execution plan (per-wave validation gates)

Single impl PR, sequenced internally. Each wave has a gate that must pass before the next.

**Wave 1 — Spec + schemas.**
Author §9.4, the `kind` row, the `baseline` config block, the no-op exception, the integration/termination carves, the §1.7 relaxation note, the §5 conformance row. Update both JSON schemas.
Gate: `markdownlint-cli2`, `python3 scripts/spec-xref-check.py`, `check-jsonschema --check-metaschema spec/v0/schemas/*.schema.json`, `check-jsonschema --schemafile experiment-config.schema.json tests/fixtures/.../config.yaml`, `python3 scripts/check-rename-discipline.py`.

**Wave 2 — Contracts + schema-parity.**
Add `kind` + conditional `idea_id` to `Variant`; `BaselineConfig` + `baseline` to `ExperimentConfig`; fixtures in `cases.py`.
Gate: `uv run pytest reference/packages/eden-contracts/tests/test_schema_parity.py`, `uv run ruff check .`, `uv run pyright`.

**Wave 3 — Storage + dispatch + orchestrator.**
`create_variant` relaxation + metrics validation + events; integration-skip + in-flight-counter carve; orchestrator `--base-commit-sha` wiring + `ensure_baseline_variant`; Compose env wiring.
Gate: `uv run pytest -q` (full suite — includes orchestrator e2e; add a test that runs to termination with a baseline present, per §8.1), `uv run ruff check .`, `uv run pyright`, `python3 scripts/check-complexity.py`.

**Wave 4 — Conformance.**
Add `test_baseline_variant.py` with the §7 wire-observable scenarios.
Gate: `uv run pytest -q conformance/ -n auto`, `uv run python conformance/src/conformance/tools/check_citations.py`.

**Wave 5 — Docs + smokes + completion record.**
Glossary, user-guide, CHANGELOG `[Unreleased]`, roadmap line; commit the impl-stage codex-review record per AGENTS.md.
Gate (full pre-push quartet + smokes, the literal AGENTS.md Commands gate — narrowed subsets are not a substitute):

```text
uv sync
uv run ruff check .
uv run pyright
uv run pytest -q
uv run pytest -q conformance/ -n auto
uv run python conformance/src/conformance/tools/check_citations.py
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json
python3 scripts/spec-xref-check.py
python3 scripts/check-rename-discipline.py
python3 scripts/check-complexity.py
bash reference/compose/healthcheck/smoke.sh
bash reference/compose/healthcheck/smoke-subprocess.sh
bash reference/compose/healthcheck/smoke-subprocess-docker.sh
bash reference/compose/healthcheck/e2e.sh
```

If any smoke fails, diagnose locally (AGENTS.md: local repro beats log-tail reading) before pushing.

## 11. Risks

- **Termination deadlock from a non-integrated baseline (§8.1).** Highest-severity. Mitigated by the §D.5 §2.5 carve + a run-to-termination test. If the carve is missed, every default-on experiment hangs at termination — silent and severe.
- **Schema-parity drift on conditional-required `idea_id` (§8.6).** JSON-Schema if/then vs Pydantic validator are easy to desynchronize; the `schema-parity` job catches it only if fixtures cover the edges. Mitigated by fixture density.
- **Smoke perturbation under `parallel_variants: 1` (§8.4).** The baseline evaluation competes for the evaluation slot; quiescence timing could shift. Lower-bound count assertions should hold, but ordering-sensitive smokes need a full run. Fallback: fixture suppression + a dedicated baseline-on smoke.
- **Orchestrator seed-SHA wiring (§8.3).** New surface; fail-fast when missing avoids a silently-skipped baseline. Compose env plumbing must reach the orchestrator container.
- **Spec restatement drift (§8.8).** The carve spans seven sections across five chapters; divergent restatement is the classic codex-review finding. Grep + state-once-cross-reference-elsewhere.
- **Event-name assumption on the override path (§D.4).** Do not assume the success-event name; read it from `event.py` / `05-event-protocol.md`. A wrong/missing event on the direct-`success` create breaks any subscriber (and the integrator-drain reasoning).
- **`kind` overload acceptance (decision 1).** If review rejects reusing `kind`, the fallback is `variant_kind` — a mechanical rename across schema/contracts/spec/glossary/conformance. Surface at plan-PR review before impl to avoid re-work.

## 12. Estimated effort

| Activity | Estimate |
|---|---|
| Spec amendments (§9.4, config block, no-op/integration/termination carves, conformance row) | ~1 day |
| Schemas + contracts + schema-parity fixtures (conditional-required `idea_id` is the long pole) | ~1 day |
| Storage + dispatch + orchestrator wiring + run-to-termination test | ~1.5 days |
| Conformance scenarios | ~0.5 day |
| Docs + smokes + full validation | ~0.5 day |
| Codex-review iterations (plan + impl) | ~1 day |
| **Total** | **~5.5 days** (~1 week, matching the issue estimate) |

The conditional-required `idea_id` parity surface and the termination-drain carve are the dominant risk variables; everything else is mechanical.
