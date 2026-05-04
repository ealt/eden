# Rename pass: directed evolution + ideator/executor vocabulary

## Goals

Bring EDEN's vocabulary in line with the agreed direction in
[`docs/glossary.md`](../glossary.md):

1. Drop the code-specific framing ("directed-code-evolution") in favor
   of substrate-agnostic "directed evolution". EDEN should read as a
   general framework for iterative refinement of any
   git-versionable artifact.
2. Resolve the verb-noun coherence problem: planner-submits-proposals
   becomes ideator-submits-ideas; the implementer-produces-trial
   collapse splits cleanly into executor-produces-variant.
3. Generalize the evaluator's output: rename `metrics` to `evaluation`
   so qualitative results aren't second-class citizens.

The user has directed:

- **Single cohesive change**, no incremental stages — partial states
  would leave the codebase incoherent. One PR.
- **Greenfield treatment** — no compat shims, no aliasing, no
  legacy-name fallbacks. Breaking changes are fine because no real
  experiments are running on this codebase yet.

## Scope

### In scope

- All spec chapters (`spec/v0/*.md`) and JSON schemas
  (`spec/v0/schemas/*.json`).
- All Pydantic models (`reference/packages/eden-contracts/`).
- Storage interface + all three backends
  (`reference/packages/eden-storage/`).
- Wire client + server (`reference/packages/eden-wire/`).
- All reference services (`reference/services/*`).
- Compose configuration (`reference/compose/compose*.yaml`,
  `setup-experiment.sh`, env files).
- Test fixtures (`tests/fixtures/`).
- Conformance suite (`conformance/`).
- Reference bindings (`spec/v0/reference-bindings/`).
- Documentation: `docs/glossary.md`, `docs/roadmap.md`,
  `docs/naming.md`, `docs/plans/` ACTIVE plans (not archived),
  `AGENTS.md`, design docs in `docs/design/`.
- Operator tooling: `reference/scripts/manual-ui/eden-manual`,
  `reference/scripts/manual-ui/eden-experiment`. (These are on the
  `manual-ui-wip` branch only; rename them there for consistency.)

### Out of scope

- `docs/archive/` — historical, leave with old vocabulary.
- `docs/plans/review/` — codex-review records; historical.
- Old phase plan docs (`docs/plans/eden-phase-*`). They reference
  pre-rename vocabulary in their archived prose; updating them
  retroactively isn't useful and risks revising history. Leave alone.
- Any external mentions in commit messages, PR descriptions, etc.
  Past commits reference old names; that's fine.
- Git branch / ref namespace renames in the wild: there are none
  (greenfield).

## Naming map

This is the source of truth for the rename. Anything not in this map
is unchanged.

### Roles (and their directories / class prefixes)

| Old | New |
|---|---|
| planner | ideator |
| implementer | executor |
| evaluator | (unchanged) |
| integrator | (unchanged) |

| Old | New |
|---|---|
| `reference/services/planner/` | `reference/services/ideator/` |
| `reference/services/implementer/` | `reference/services/executor/` |
| `eden_planner_host` | `eden_ideator_host` |
| `eden_implementer_host` | `eden_executor_host` |
| `eden_evaluator_host` | (unchanged) |
| `ScriptedPlanner` | `ScriptedIdeator` |
| `ScriptedImplementer` | `ScriptedExecutor` |
| `planner-host` (compose service) | `ideator-host` |
| `implementer-host` (compose service) | `executor-host` |

### Data shapes

| Old | New |
|---|---|
| `Proposal` (class, schema, JSON object) | `Idea` |
| `Trial` (class, schema, JSON object) | `Variant` |
| `PlanSubmission` | `IdeateSubmission` |
| `ImplementSubmission` | `ExecuteSubmission` |
| `EvaluateSubmission` | (class name unchanged; field renamed below) |

| Old field | New field |
|---|---|
| `proposal_id` | `idea_id` |
| `proposal_ids` | `idea_ids` |
| `trial_id` | `variant_id` |
| `trial_commit_sha` | `variant_commit_sha` |
| `metrics` (on `EvaluateSubmission` and on the `Variant` schema) | `evaluation` |
| `metrics_schema` (on `ExperimentConfig`) | `evaluation_schema` |
| `intended_implementer` (proposed in design doc) | `intended_executor` |
| `implemented_by` (proposed) | `executed_by` |
| `evaluated_by` (proposed) | (unchanged) |

### Schema files

| Old | New |
|---|---|
| `spec/v0/schemas/proposal.schema.json` | `spec/v0/schemas/idea.schema.json` |
| `spec/v0/schemas/trial.schema.json` | `spec/v0/schemas/variant.schema.json` |
| `spec/v0/schemas/metrics-schema.schema.json` | `spec/v0/schemas/evaluation-schema.schema.json` |
| `spec/v0/schemas/task.schema.json` | (unchanged) |
| `spec/v0/schemas/event.schema.json` | (unchanged) |
| `spec/v0/schemas/experiment-config.schema.json` | (unchanged at file level; `metrics_schema` field becomes `evaluation_schema`) |

### Task kinds

| Old | New |
|---|---|
| `kind: "plan"` | `kind: "ideate"` |
| `kind: "implement"` | `kind: "execute"` |
| `kind: "evaluate"` | (unchanged) |

Verb form chosen for parallelism with `evaluate`. All three task
kinds are now imperative verbs.

### Event types

The event-type vocabulary changes mechanically per the data-shape
renames:

| Old | New |
|---|---|
| `proposal.drafted` | `idea.drafted` |
| `proposal.ready` | `idea.ready` |
| `proposal.dispatched` | `idea.dispatched` |
| `proposal.completed` | `idea.completed` |
| `trial.starting` (or wherever it appears) | `variant.starting` |
| `trial.integrated` | `variant.integrated` |
| `trial.errored` | `variant.errored` |
| `trial.eval_errored` | `variant.eval_errored` |
| `trial.succeeded` | `variant.succeeded` |
| `task.*` | (unchanged) |

### Wire endpoints

| Old | New |
|---|---|
| `/v0/experiments/<id>/proposals` | `/v0/experiments/<id>/ideas` |
| `/v0/experiments/<id>/proposals/<pid>` | `/v0/experiments/<id>/ideas/<idea-id>` |
| `/v0/experiments/<id>/proposals/<pid>/mark-ready` | `/v0/experiments/<id>/ideas/<idea-id>/mark-ready` |
| `/v0/experiments/<id>/trials` | `/v0/experiments/<id>/variants` |
| `/v0/experiments/<id>/trials/<tid>` | `/v0/experiments/<id>/variants/<variant-id>` |
| `/v0/experiments/<id>/trials/<tid>/integrate` | `/v0/experiments/<id>/variants/<variant-id>/integrate` |
| `/v0/experiments/<id>/trials/<tid>/declare-eval-error` | `/v0/experiments/<id>/variants/<variant-id>/declare-eval-error` |

### Storage method names

| Old | New |
|---|---|
| `create_proposal` | `create_idea` |
| `mark_proposal_ready` | `mark_idea_ready` |
| `read_proposal` | `read_idea` |
| `list_proposals` | `list_ideas` |
| `create_trial` | `create_variant` |
| `read_trial` | `read_variant` |
| `list_trials` | `list_variants` |
| `integrate_trial` | `integrate_variant` |
| `declare_trial_eval_error` | `declare_variant_eval_error` |
| `create_plan_task` | `create_ideate_task` |
| `create_implement_task` | `create_execute_task` |
| `create_evaluate_task` | (unchanged) |
| `validate_metrics` | `validate_evaluation` |

### Dispatch / orchestrator internals

| Old | New |
|---|---|
| `_dispatch_implement_tasks` | `_dispatch_execute_tasks` |
| `_dispatch_evaluate_tasks` | (unchanged) |
| `_promote_successful_trials` | `_promote_successful_variants` |
| `_finalize_submitted` (with kind="plan"/"implement"/"evaluate") | (function unchanged; kind values change) |
| `_accept_implement` | `_accept_execute` |
| `_accept_evaluate` | (unchanged) |
| `_reject_implement` | `_reject_execute` |
| `_validate_implement_acceptance` | `_validate_execute_acceptance` |

### Postgres / SQLite tables

| Old | New |
|---|---|
| `proposal` table | `idea` table |
| `trial` table | `variant` table |
| `task`, `event`, `submission`, `experiment`, `schema_version` | (unchanged) |

### Git refs

| Old | New |
|---|---|
| `refs/heads/work/<slug>-<id>` (executor's working branch) | (unchanged — "work" is generic) |
| `refs/heads/trial/<id>-<slug>` (integrator's canonical ref) | `refs/heads/variant/<id>-<slug>` |

The `work/*` namespace is generic enough to keep. The `trial/*`
namespace is renamed to `variant/*` to align with the data-shape
rename.

### CLI flags / env vars

| Old | New |
|---|---|
| `--plan-tasks` | `--ideate-tasks` |
| `--proposals-per-plan` | `--ideas-per-ideation` |
| `EDEN_PLAN_TASKS` | `EDEN_IDEATE_TASKS` |
| `EDEN_PROPOSALS_PER_PLAN` | `EDEN_IDEAS_PER_IDEATION` |
| `plan_command` (experiment-config) | `ideate_command` |
| `implement_command` | `execute_command` |
| `evaluate_command` | (unchanged) |

### Fixtures

| Old | New |
|---|---|
| `tests/fixtures/experiment/plan.py` | `tests/fixtures/experiment/ideate.py` |
| `tests/fixtures/experiment/implement.py` | `tests/fixtures/experiment/execute.py` |
| `tests/fixtures/experiment/eval.py` | (unchanged) |

### Conformance scenarios

| Old | New |
|---|---|
| `conformance/scenarios/test_planner_submission.py` | `test_ideator_submission.py` |
| `conformance/scenarios/test_implementer_submission.py` | `test_executor_submission.py` |
| `conformance/scenarios/test_evaluator_submission.py` | (unchanged) |
| `conformance/scenarios/test_integrator_atomicity.py` | (unchanged) |
| `conformance/scenarios/test_promotion_preconditions.py` | (unchanged; may reference renamed fields internally) |

The `CONFORMANCE_GROUP` declaration string in each file follows
chapter 9 §5 group names; those will need to track whatever the
spec-side group naming becomes (likely "ideator", "executor", etc.).

### Reference binding doc

| Old | New |
|---|---|
| `spec/v0/reference-bindings/worker-host-subprocess.md` | (unchanged path) |
| Protocol message names: `plan` / `proposal` / `proposal-done` | `ideate` / `idea` / `ideate-done` |

### Operator tooling (on `manual-ui-wip` branch)

| Old | New |
|---|---|
| `eden-manual list-tasks --kind plan` | `eden-manual list-tasks --kind ideate` |
| `eden-manual plan-submit` | `eden-manual ideate-submit` |
| `eden-manual implement-submit` | `eden-manual execute-submit` |
| `eden-manual evaluate-submit` | (unchanged) |
| `.claude/skills/eden-manual-planner/` | `.claude/skills/eden-manual-ideator/` |
| `.claude/skills/eden-manual-implementer/` | `.claude/skills/eden-manual-executor/` |
| `.claude/skills/eden-manual-evaluator/` | (unchanged) |

### Spec prose

| Old phrasing | New phrasing |
|---|---|
| "directed-code-evolution" | "directed evolution" |
| "code evolution" (where it appears) | "directed evolution" |
| "the implementer turns a proposal into a working-tree change" | "the executor turns an idea into a variant" |
| "trial" used as the artifact | "variant" |
| "trial" used as the process | (kept as "trial of variant X" / "iteration"; rare) |
| "metrics" used loosely as "the evaluator's output" | "evaluation" |
| "metrics schema" | "evaluation schema" |

## Order of operations within the single PR

Even though this lands as one PR, internal sequencing keeps the
intermediate states tractable for the implementer:

1. **Spec prose** — chapter 1 framing first, then chapters 2–8
   working through. This is non-code; can land as the first commit
   on the rename branch.
2. **JSON schemas** — rename files; update field names; verify
   meta-schema validity.
3. **Pydantic contracts** (`eden-contracts`) — class renames; field
   renames; the schema-parity tests will fail until both are
   aligned. Update the test cases.
4. **Storage** — Protocol method rename; backend method rename;
   table renames; events emitted with new type names. Conformance
   tests against memory/sqlite/postgres backends will fail until
   their queries use the new table names.
5. **Wire** — endpoint paths; client + server in lockstep.
6. **Dispatch driver** — function renames; kind enum value updates.
7. **Reference services** — directory renames; CLI flag renames;
   compose service renames; per-service module renames.
8. **Compose config** — service definitions, env var renames,
   experiment-config field renames.
9. **Fixtures** — file renames; YAML content updates.
10. **Tests** — module renames; assertion updates; in-memory
    state-checking that depends on type/kind strings.
11. **Conformance suite** — scenario renames; group declarations;
    docstring spec citations.
12. **Reference bindings** — subprocess binding doc message names.
13. **Operator tooling** — `eden-manual` / `eden-experiment`
    subcommands and skills (on the `manual-ui-wip` branch).
14. **Docs** — glossary (move "Proposed direction" content into the
    main sections, retire the proposed section), roadmap, naming
    doc, AGENTS.md, design docs (the orchestrator-and-worker-roles
    doc has many proposed renames already noted; consolidate).

The implementer running this pass should expect the test suite to
go red until step 11 or so, then come back to green when the rename
reaches consistency.

## Validation gates

The change is "done" when all of the following pass:

1. `uv sync` succeeds.
2. `uv run ruff check .` clean.
3. `uv run pyright` clean.
4. `uv run pytest -q` (full suite) green.
5. `uv run pytest -q -m e2e` (real-subprocess test) green.
6. `uv run pytest -q -m docker` (docker-backed integration) green.
7. `uv run pytest -q conformance/` (against the reference impl) green.
8. `uv run python conformance/src/conformance/tools/check_citations.py`
   clean (every scenario's spec citation still resolves).
9. `python3 scripts/spec-xref-check.py` clean (every `§N.M`
   reference resolves).
10. `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules"
    "#.venv" "#docs/archive/**" "#docs/plans/review/**"` clean.
11. `pipx run 'check-jsonschema==0.29.4' --check-metaschema
    spec/v0/schemas/*.schema.json` clean.
12. `pipx run 'check-jsonschema==0.29.4'
    --schemafile spec/v0/schemas/experiment-config.schema.json
    tests/fixtures/experiment/.eden/config.yaml` clean.
13. `bash reference/compose/healthcheck/smoke.sh` green.
14. `bash reference/compose/healthcheck/smoke-subprocess.sh` green.
15. `bash reference/compose/healthcheck/e2e.sh` green.

Compose smoke tests catch the "stack actually still works
end-to-end after the rename" question that unit tests miss.

## Tricky areas

### `evaluate` task kind already uses the verb form

The current task kinds are `plan`, `implement`, `evaluate`. The
rename target is `ideate`, `execute`, `evaluate`. Note that
`evaluate` is unchanged — when sed-replacing `"plan"` and
`"implement"` to `"ideate"` and `"execute"`, an unscoped sed could
accidentally hit unrelated occurrences of the words. Use bounded
patterns (e.g., grep for `kind="plan"` not `"plan"`).

### `metrics` is a common Python word

`metrics` appears in places that aren't EDEN-specific (logging
metrics, observability metrics). Don't blanket-replace; do
field-name-scoped renames only. The rename target is the
EvaluateSubmission and Variant fields plus the experiment-config
`metrics_schema` field.

### `Trial` is a real English word

`Trial` appears in some prose contexts where it's not the data
shape (e.g., "trial of variant X"). Keep these. Only rename the
data-shape uses.

### Submission `metrics` → `evaluation` field

The `EvaluateSubmission.metrics` field becomes
`EvaluateSubmission.evaluation`. The class name doesn't change. The
rename touches:

- `submissions.py` field declaration
- `submissions_equivalent` content equivalence check
- All wire-payload JSON keys (`{"metrics": ...}` → `{"evaluation": ...}`)
- All test assertions that read `.metrics` from an
  EvaluateSubmission

### Spec section numbers

Cross-references like "[`02-data-model.md`](02-data-model.md) §3"
embed section numbers. The spec rename will rearrange some prose.
After the rename, run `scripts/spec-xref-check.py` to catch any
broken `§N.M` refs and fix them. The conformance suite cites these
too via docstring; the citation checker
(`conformance/src/conformance/tools/check_citations.py`) catches
breakage there.

### Conformance group identity

Each conformance scenario declares
`CONFORMANCE_GROUP = "<group name>"` matching a chapter 9 §5 group.
The group names will track the role rename (e.g., "planner
contracts" → "ideator contracts"). Both sides must change in
lockstep so the citation checker keeps passing.

### Compose volume names

Volume names embed the project name plus a logical name (e.g.,
`eden-reference_eden-orchestrator-repo`,
`eden-reference_eden-implementer-repo`,
`eden-reference_eden-evaluator-repo`). The implementer-related
volume becomes `eden-reference_eden-executor-repo`. This is one of
the places where the AGENTS.md "explicit name:" discipline
matters — a fresh `compose up` after the rename will create
new-named volumes; the old ones stay until manually wiped. Document
this in the rename PR description for any operator who'd been
running on a pre-rename stack.

### Postgres table renames

Renaming the `proposal` and `trial` tables in the schema means any
existing postgres database from a pre-rename run can't be reused
as-is. Greenfield treatment per the user's direction → no migration
needed; operators wipe the volume and re-seed. Note in the
SchemaVersion bump (the `_schema.py` / `_postgres_schema.py`
modules track this).

### Operator tooling on `manual-ui-wip` branch

The `manual-ui-wip` branch has its own copies of bug-fix code and
tooling that touch the renamed surfaces. Two options:

1. Rebase the branch onto the rename PR after it lands (manual
   conflict resolution touching every surface; tedious but
   self-contained).
2. Drop the branch and re-author the bug fixes against the renamed
   spec (more work but lands cleaner code).

Recommend option 2 for the bug-fix code since the renames are
mechanical and the rebase noise dominates. The audit findings
captured in `MANUAL_UI_ISSUES.md` and the design docs are content
that survives the rename intact (they reference both old and new
vocabulary; need a quick pass to update prose).

## Open questions

1. **Field-name on EvaluateSubmission**: `evaluation` is the
   straightforward choice but mildly tautological ("the evaluation
   submission has an evaluation"). Alternatives: `result`,
   `output`, `data`, `findings`. Recommend `evaluation` for
   consistency with the role-noun pattern; flag if the redundancy
   reads worse than expected once the prose is in front of us.

2. **`work/*` ref namespace**: kept generic above, but if we want
   role-naming consistency we could rename to `exec/*` or
   `executor/*`. "work" is so well-understood across git workflows
   that the exception probably reads cleanly; recommend keeping.

3. **`intended_implementer` field naming** (proposed in
   orchestrator-and-worker-roles design doc, not yet implemented):
   should this rename ride along, or wait for that design's
   implementation? Recommend including it in the rename map even
   though the field doesn't exist yet — captures the intent.

4. **Reference binding (subprocess) message names**: the JSON-line
   protocol uses `plan` / `proposal` / `proposal-done` etc. as
   message types. Renaming these is consistent with the rest of
   the pass but is technically a wire-protocol change for any
   bespoke planner script someone might have. Greenfield treatment
   means we change them; flag clearly in the rename PR description.

5. **Conformance group rename strategy**: chapter 9 §5 group names
   need to update. Should the chapter 9 update happen in the same
   PR (yes, per "single cohesive change") or as a separate
   conformance-only PR (no, per the user's direction)?

## Acceptance for "done"

The PR is mergeable when:

- All 15 validation gates pass.
- `grep -rin '\bplanner\b\|\bimplementer\b\|\bproposal\b\|\btrial\b\|\bmetrics_schema\b'`
  returns only matches in `docs/archive/`, `docs/plans/eden-phase-*`,
  `docs/plans/review/`, and historical commit metadata. (Not in
  current spec, code, fixtures, conformance, or active docs.)
- The reviewer can re-read chapter 1's "core concepts" without
  encountering "code" as a substrate assumption.
- A representative non-code use case (e.g., recipe iteration) reads
  naturally against the renamed spec without further translation.

## Estimated effort

- **Mechanical search-replace pass**: ~1 hour for an LLM with focus.
- **Test failures + fixup**: ~2-4 hours iterating until green.
- **Spec prose review**: ~2 hours reading every chapter for
  naturalness in the new vocabulary.
- **Doc cleanup**: ~1 hour pass through glossary, AGENTS.md,
  design docs.
- **Smoke / e2e validation**: ~1 hour running the compose suites
  and confirming they still work end-to-end.

Realistic total: **a focused day's work**.

## Out-of-band notes

- The rename happens on a dedicated branch off origin/main. PR
  title: "Rename to directed-evolution vocabulary
  (ideator/executor/variant/evaluation)". Long body listing the
  breaking changes per the naming map.
- After merge, new experiments need `setup-experiment` re-run and
  fresh `compose up -d` (the volume names changed).
- The `manual-ui-wip` branch's bug fixes need to re-author per
  Tricky Areas above.
