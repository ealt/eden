# EDEN glossary

A canonical list of the terms-of-art used in the EDEN protocol, the
reference implementation, and surrounding design discussions.

This document has two parts:

1. **Sections 1–10** describe terms currently in use across the spec,
   reference implementation, and design docs. Descriptive, not
   prescriptive: the names below are what the code says today.
2. The **"Proposed direction"** section at the end records the agreed
   vocabulary the project is moving toward. The rename has not yet
   happened in the spec or impl; until it does, sections 1–10 remain
   the source of truth for what reading the codebase means.

The glossary is organized by **kind of thing**, not alphabetically,
because the relationships between terms are part of the meaning.

---

## 1. Worker roles

A **worker** is any actor that participates in the task protocol as a
consumer — claims tasks, does work, submits results. The protocol
defines four worker contracts. Workers MAY be human, agentic
(LLM-driven), or programmatic; the contracts constrain only the
observable effects.

| Term | One-line meaning | Spec ref |
|---|---|---|
| **planner** | Proposes what to try; output is one or more *proposals* | [`01-concepts.md`](../spec/v0/01-concepts.md) §2.1 |
| **implementer** | Turns a proposal into git commit(s) on a per-trial *work branch* | §2.2 |
| **evaluator** | Measures an implemented trial against the experiment's metrics schema | §2.3 |
| **integrator** | Promotes a successful trial to the canonical *trial lineage* (writes a `trial/*` commit) | §2.4 |

The integrator is the only worker role with a fixed identity (in the
current spec there is exactly one logical integrator per experiment;
the role contract makes it the sole writer of `trial/*`). Planner /
implementer / evaluator are role *contracts* — many concurrent
workers may claim the same kind of task.

## 2. System actors and components

| Term | One-line meaning | Spec ref |
|---|---|---|
| **orchestrator** | Dispatches tasks and advances the protocol's state machine in response to submissions. No unique authority beyond what the protocol grants. May run with multiple cooperating instances. | [`01-concepts.md`](../spec/v0/01-concepts.md) §11 |
| **auto-orchestrator** | Informal: the automated polling-loop instance of the orchestrator role (`reference/services/orchestrator/`). Distinct from "the orchestrator role" — a deployment may run zero, one, or many. | (informal) |
| **operator** | Human running a deployment, distinct from worker roles. The operator runs `setup-experiment`, brings up the stack, performs admin actions like `reclaim`, etc. | (informal; not normative) |

## 3. Data shapes (value objects)

| Term | One-line meaning | Spec ref / schema |
|---|---|---|
| **experiment** | A single directed-code-evolution run (id + config + starting commit) | [`01`](../spec/v0/01-concepts.md) §1 / [`02`](../spec/v0/02-data-model.md) |
| **proposal** | The planner's output: a description of a change to attempt + dispatch metadata | [`01`](../spec/v0/01-concepts.md) §3 / [`schemas/proposal.schema.json`](../spec/v0/schemas/proposal.schema.json) |
| **trial** | One attempt to improve the objective; references the proposal, commits, metrics, and status | [`01`](../spec/v0/01-concepts.md) §4 / [`schemas/trial.schema.json`](../spec/v0/schemas/trial.schema.json) |
| **task** | A unit of work dispatched to a worker (kind, payload, state, claim) | [`01`](../spec/v0/01-concepts.md) §5 / [`schemas/task.schema.json`](../spec/v0/schemas/task.schema.json) |
| **event** | An immutable record of a state change, appended to the event log | [`01`](../spec/v0/01-concepts.md) §6 / [`schemas/event.schema.json`](../spec/v0/schemas/event.schema.json) |
| **submission** | The role-specific payload a worker hands back when completing a task. Three shapes: `PlanSubmission`, `ImplementSubmission`, `EvaluateSubmission`. | [`03-roles.md`](../spec/v0/03-roles.md) §§2.4 / 3.4 / 4.4 |
| **claim** | A worker's hold on a task; the task store records the worker_id and an expiry. | [`01`](../spec/v0/01-concepts.md) §8 |
| **claim token** | The opaque secret returned to a worker when it claims; required to submit. | [`01`](../spec/v0/01-concepts.md) §8 |

### 3.1 Sub-fields worth naming

| Term | What it is |
|---|---|
| **slug** | A proposal's short kebab-case label; matches `^[a-z0-9][a-z0-9-]*$`. Used in branch naming. |
| **priority** | Per-proposal ordering hint (number; "higher dispatches earlier" — currently SHOULD-level, not enforced; see `MANUAL_UI_ISSUES.md` #15). |
| **parent_commits** | One or more commit SHAs a proposal/trial is based on. |
| **artifacts_uri** | URI pointing at proposal-rationale or evaluator artifacts (typically `file://` in the reference impl). |
| **kind** | A task's role-routing label (`plan` / `implement` / `evaluate`). |
| **payload** | A task's role-specific inner content. |
| **commit_sha** | The worker's tip commit on its `work/*` branch (set on the trial when the implementer submits). |
| **trial_commit_sha** | The squashed-and-integrated commit on `trial/*` (set by the integrator). |
| **branch** | The canonical work branch name (`work/<slug>-<trial_id>`); set on the trial at create time. |
| **metrics** | A dict shaped per the experiment's `metrics_schema`. |

### 3.2 Task kinds

A task's `kind` field discriminates which role contract claims it,
what its payload looks like, and what shape its submission takes. The
three kinds today (with vocabulary current as of this glossary; see
"Proposed direction" below for the renamed equivalents):

| `kind` | Claimed by | Payload | Submission shape | On `accept`, the store… |
|---|---|---|---|---|
| `plan` | planner | `{experiment_id}` | `PlanSubmission(status, proposal_ids: tuple)` | Marks the task `completed`. The referenced proposals (which the planner moved to `state="ready"` before submit) are then dispatched by the orchestrator as `implement` tasks. |
| `implement` | implementer | `{experiment_id, proposal_id}` | `ImplementSubmission(status, trial_id, commit_sha?)` | On `success`: writes `commit_sha` onto the referenced trial; trial.status remains `starting`. The orchestrator then dispatches an `evaluate` task referencing the trial. On `error`: trial transitions to `error`. |
| `evaluate` | evaluator | `{experiment_id, trial_id}` | `EvaluateSubmission(status, trial_id, metrics?, artifacts_uri?)` | On `success`: trial transitions to `success` with the submitted metrics. The integrator then promotes it (writes `trial_commit_sha`). On `error`: trial transitions to `error`. On `eval_error`: trial stays in `starting` (the evaluator didn't form a verdict; the trial remains evaluable). |

The full state-machine semantics — including reject paths,
validation_error handling, and idempotent resubmit — live in
[`spec/v0/04-task-protocol.md`](../spec/v0/04-task-protocol.md) §4.
This table is the at-a-glance summary of the happy path.

A few observations the table makes visible that aren't always obvious:

- The **planner doesn't directly produce trials**; it produces
  proposals, and the *orchestrator* (in its dispatch role) creates
  the implement tasks that produce trials. The hand-off shape is
  ready proposal → implement task → trial.
- A trial's status transitions from `starting` to its terminal
  status (`success` / `error` / `eval_error`) at the *evaluator's*
  submission, not the implementer's. The implementer's submission
  only sets `commit_sha`; the trial stays `starting` between
  implement-submit and evaluate-submit.
- `eval_error` is the only status where the trial doesn't
  terminalize. The evaluator declared the trial unevaluable but
  didn't condemn it; another evaluator (or the same one with more
  context) MAY produce a verdict later. (See
  [`spec/v0/03-roles.md`](../spec/v0/03-roles.md) §4.4.)

## 4. Lifecycle vocabulary (states, statuses, transitions)

EDEN deliberately uses different words for different lifecycle
machines, even when conceptually similar. Worth knowing the mapping:

| Object | Field | Possible values |
|---|---|---|
| Task | `state` | `pending` → `claimed` → `submitted` → `completed` (or `failed`) |
| Proposal | `state` | `drafting` → `ready` → `dispatched` → `completed` |
| Trial | `status` | `starting` → `success` (or `error`, `eval_error`) |
| Plan submission | `status` | `success`, `error` |
| Implement submission | `status` | `success`, `error` |
| Evaluate submission | `status` | `success`, `error`, `eval_error` |

**state vs. status** is intentional: tasks/proposals use `state` (the
protocol's lifecycle); trials and submissions use `status`
(role-reported outcome). Referring to a "task status" is a category
error.

### 4.1 Verbs

| Term | What it does |
|---|---|
| **claim** (verb) | Reserve a task for a worker; returns a token. |
| **submit** | Worker delivers a submission for a claimed task. |
| **accept** | Orchestrator validates a submission and writes it as committed; task → `completed`. |
| **reject** | Orchestrator rejects a submission; task → `failed`. Reasons include `worker_error` and `validation_error`. |
| **reclaim** | Operator (or sweeper) revokes a claim, returning the task to `pending`. |
| **dispatch** | Orchestrator creates a downstream task from a state transition (e.g. ready proposal → implement task; success-with-commit_sha trial → evaluate task). |
| **integrate** / **promote** | Integrator squashes a successful trial's `work/*` content into a single commit on `trial/*`, attaches the eval manifest, and emits `trial.integrated`. |

## 5. Storage components

| Term | One-line meaning | Spec ref |
|---|---|---|
| **task store** | Durable store of tasks, proposals, trials, submissions; provides atomic claim and idempotent submit | [`08-storage.md`](../spec/v0/08-storage.md) |
| **event log** | Append-only ordered log of events; provides replay and subscribe | [`05-event-protocol.md`](../spec/v0/05-event-protocol.md) |
| **artifact store** | Holds rationale documents, evaluator artifacts, etc., addressed by URI | [`08-storage.md`](../spec/v0/08-storage.md) §5 (deferred) |

In the reference impl, all three are backed by Postgres + the
deployment filesystem; the protocol abstracts over the choice.

## 6. Git topology

EDEN maintains three branch namespaces in the experiment's git repo:

| Namespace | What lives there | Writer |
|---|---|---|
| **`main`** | The experiment's starting point. Immutable during an experiment. | Set once at `setup-experiment` time. |
| **`work/*`** | Per-attempt implementer branches; named `work/<slug>-<trial_id>`. | Implementers (concurrent writes allowed). |
| **`trial/*`** | The canonical lineage; one commit per successfully integrated trial. | Integrator only. |

| Term | What it is |
|---|---|
| **seed commit** / **base commit** | The single commit on `main` at experiment start. Captured as `EDEN_BASE_COMMIT_SHA` in deployment env. |
| **work branch** | A branch in the `work/*` namespace; the implementer's tip commit lives here. |
| **trial branch** | A branch in the `trial/*` namespace; the integrator-produced squash commit. |
| **eval manifest** | A JSON file at `.eden/trials/<trial_id>/eval.json` in the `trial/*` commit's tree, containing the evaluator's metrics. Spec-authoritative path. |
| **bare repo** | The git repository hosted on the workers' git remote of record (Gitea in the reference deployment). |

## 7. Protocol / spec terms

| Term | What it is |
|---|---|
| **spec** | The normative protocol specification under [`spec/v0/`](../spec/v0/). |
| **chapter** | Numbered file under `spec/v0/` (e.g., `04-task-protocol.md`). Numbering is stable within a spec version. |
| **normative** | Spec text that uses MUST/SHOULD/MAY (RFC 2119); a conforming implementation must obey. |
| **informative** | Spec text that's expository or motivating; a conforming implementation may ignore. |
| **schema** | A JSON Schema definition under `spec/v0/schemas/`. The wire format. |
| **Pydantic model** | The Python-side validating mirror of a schema, in `eden-contracts`. |
| **wire** / **wire protocol** | The HTTP binding of the task/event/integrator/storage operations. [`07-wire-protocol.md`](../spec/v0/07-wire-protocol.md). |
| **reference binding** | A non-normative companion doc describing one of several valid bindings (e.g., the subprocess binding for worker hosts). [`spec/v0/reference-bindings/`](../spec/v0/reference-bindings/). |
| **reference implementation** / **reference impl** | The Python implementation under [`reference/`](../reference/). One conforming impl among possibly many. |
| **conformance suite** | The implementation-agnostic test suite under [`conformance/`](../conformance/) that asserts spec invariants. |
| **conformance scenario** | One test in the conformance suite (`conformance/scenarios/test_*.py`); each cites the spec MUST/SHOULD it exercises. |
| **conformance group** | Index grouping in chapter 9 §5 used to organize scenarios. |
| **role binding** | How a role is hosted (subprocess, HTTP service, in-process adapter, human at a keyboard). Currently informal; deferred to a future spec chapter. |

## 8. Operational vocabulary

| Term | What it is |
|---|---|
| **deployment** | One running instance of the EDEN stack (typically one Compose project, one experiment). |
| **experiment id** | String identifying an experiment. Operator-supplied at `setup-experiment` time. |
| **worker id** | String identifying a worker instance. Today: ad-hoc; supplied via `--worker-id` CLI flag. (Slated to become first-class identity per [`docs/design/orchestrator-and-worker-roles.md`](design/orchestrator-and-worker-roles.md) §1.) |
| **shared token** | The deployment-wide bearer token used for wire auth (informally: every worker authenticates as "the deployment", not as a specific worker). Slated for replacement by per-worker auth — see design doc. |
| **iteration** (orchestrator) | One pass through the orchestrator's loop body (finalize submitted, dispatch implement, dispatch evaluate, finalize submitted, promote successful). |
| **quiescence** | Heuristic in the current orchestrator: N consecutive iterations with no progress → exit. Not spec-defined; widely considered the wrong abstraction (see `MANUAL_UI_ISSUES.md` #1, design doc §10). |
| **checkpoint** | Snapshot of an experiment's state for save/restore. Implementation-specific in the reference impl today; portable format is in design (`docs/design/portable-checkpoints.md`). |
| **manifest** (in setup-experiment context) | A `.env` + `experiment-config.yaml` + gitea credential helper produced for one experiment. Different from "eval manifest" above. |

## 9. Identity and routing (forward-looking)

These terms are not in the current spec but are central to active
design (see [`docs/design/orchestrator-and-worker-roles.md`](design/orchestrator-and-worker-roles.md)):

| Term | What it would mean |
|---|---|
| **target** | A new field on tasks specifying who can claim: a worker_id, a group name, or null (any). |
| **group** | A named set of worker_ids and/or other group names (recursive). |
| **selector** | (Considered and rejected) — the K8s-style label-and-selector approach was sketched then dropped in favor of the simpler RBAC group model. |
| **dispatch_mode** | Per-experiment-per-decision-type flag (`auto` / `manual`) controlling whether the auto-orchestrator handles each routing decision or waits for human action. |
| **termination policy** | A deployment-supplied predicate the orchestrator consults each iteration; replaces the four currently-spec'd-but-unenforced termination fields (`max_trials`, `max_wall_time`, `convergence_window`, `target_condition`). |

## 10. Build / packaging vocabulary

| Term | What it is |
|---|---|
| **uv workspace** | The Python-side monorepo layout under [`reference/`](../reference/), with `pyproject.toml` declaring the workspace members. |
| **package** | A library member of the uv workspace under `reference/packages/eden-*` (e.g., `eden-contracts`, `eden-storage`). |
| **service** | A runnable application under `reference/services/*` (e.g., `task-store-server`, `web-ui`). |
| **eden-reference:dev** | The shared docker image all reference services run from (multi-stage `uv sync --frozen --no-dev --all-packages`). |
| **eden-runtime:dev** | A smaller image used as the default sibling-container target in chunk-10d-followup-A's docker-exec mode. |
| **subprocess mode** | Worker host config where the user's `*_command` runs as a child process of the host (chunk 10d). |
| **docker-exec mode** | Worker host config where the user's `*_command` runs in a sibling container via DooD (chunk 10d follow-up A). |

---

## Proposed direction (agreed; not yet implemented)

The current vocabulary has two coherence problems:

1. **The framework is over-committed to code.** "directed-code-evolution" in
   chapter 1 §1, "implementer" as the role of "turning a proposal into a
   working-tree change", and "metrics" as the evaluator's output all bias
   toward code as the substrate. EDEN should be a general framework for
   directed iteration on any artifact that can be versioned in git —
   code, recipes, prompts, art, configs, prose.
2. **The role-name / artifact-name verbs don't agree.** A "planner submits
   proposals" is a category mismatch — a planner submits plans; a
   proposer submits proposals. Same noun-verb agreement issue applies
   to "trial" (collapses the *act of trying* and the *thing produced*).

The agreed direction is a four-step process with parallel ``-or``
suffix role names and verb-noun-coherent artifact names.

### Process and role names

| Step (verb-noun) | Role | Acts on | Produces |
|---|---|---|---|
| ideation | **ideator** | experiment context (prior variants, evaluations, objective) | **idea** |
| execution | **executor** | an idea | a **variant** |
| evaluation | **evaluator** | a variant | an **evaluation** |
| integration | **integrator** | an evaluated variant | an integrated entry on the canonical lineage |

All four roles share the ``-or`` suffix; verb-noun coherence holds
(ideator → idea, evaluator → evaluation). "Variant" is preferred over
"trial" for the executor's output: it's substrate-agnostic and picks
up the directed-evolution biology metaphor cleanly. "Trial" remains
useful as a *process* word ("the trial of variant X") but is no
longer the artifact's name.

### Other roles

| Role | What they do |
|---|---|
| **orchestrator** | Owns the routing / state-machine-advancing decisions for an experiment. Becomes a role contract per `docs/design/orchestrator-and-worker-roles.md`; humans and automated processes can play it. Name unchanged. |
| **owner** | The authority over an experiment — can configure, terminate, reassign, transfer. Today implicit; should be a first-class concept once authentication and authorization land. |
| **observer** / **subscriber** | Read-only consumer of the event log (UIs, dashboards, downstream tooling). Distinct from workers; has no claim/submit capability. |

**operator** stays as an informal term for the human running the
deployment day-to-day; **admin** is not a role but a permission level
on the operator concept.

### Other terminology updates

| Today | Proposed | Notes |
|---|---|---|
| "directed-code-evolution" | **directed evolution** | Drops "code"; aligns with biology metaphor. |
| `metrics` (the bundle) | **evaluation** (singular noun) | Each evaluator submission carries one ``evaluation``. |
| `metrics_schema` | **evaluation_schema** | Same shape; same type vocabulary (`integer`, `real`, `text`); just no longer narrows the connotation. |
| `Trial` schema | **`Variant`** schema | Same fields. |
| `trial_id`, `trial_commit_sha`, `trial.branch`, `trial/<id>-<slug>` ref | `variant_id`, `variant_commit_sha`, `variant.branch`, `variant/<id>-<slug>` (or keep `trial/*` if the ref name is grandfathered) | Field-level rename. The ref namespace might be grandfathered for git-history readability. |
| `Proposal` schema | **`Idea`** schema | Same fields. |
| Plan task / `Plan` task kind | **`Idea`** task kind (or `ideation`) | Task kinds align with role names. |
| Implement task | **Execute** / **execution** task kind | |
| Evaluate task | **Evaluate** / **evaluation** task kind | |

### Scope of the rename

The rename touches a lot of surfaces. Tracked here for the
implementation pass:

- **Spec** ([`spec/v0/`](../spec/v0/)) — chapter prose, the "directed
  code evolution" framing in chapter 1, role names in chapter 3,
  task-kind enums in chapter 4, schema files in `schemas/`.
- **Pydantic models** ([`reference/packages/eden-contracts/`](../reference/packages/eden-contracts/))
  — class names (`Proposal` → `Idea`, `Trial` → `Variant`,
  `*Submission` shapes), field names.
- **Storage interface and backends**
  ([`reference/packages/eden-storage/`](../reference/packages/eden-storage/))
  — `create_proposal` → `create_idea`, `read_trial` → `read_variant`,
  etc.
- **Wire**
  ([`reference/packages/eden-wire/`](../reference/packages/eden-wire/),
  [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md))
  — endpoint paths (`/v0/experiments/<id>/proposals` →
  `/v0/experiments/<id>/ideas`, etc.).
- **Reference services**
  ([`reference/services/`](../reference/services/)) — directory names
  (`planner` → `ideator`, `implementer` → `executor`), CLI flags,
  log strings.
- **Compose / setup**
  ([`reference/compose/compose.yaml`](../reference/compose/compose.yaml),
  [`reference/scripts/setup-experiment/`](../reference/scripts/setup-experiment/))
  — service names (`planner-host` → `ideator-host`, etc.), env var
  names where they reference roles (`EDEN_PLAN_TASKS` →
  `EDEN_IDEA_TASKS`, etc.).
- **Fixtures** — `tests/fixtures/experiment/{plan,implement,eval}.py`
  → `{ideate,execute,evaluate}.py`; the YAML's `*_command` keys.
- **Conformance suite** ([`conformance/`](../conformance/))
  — scenario file names (`test_planner_submission.py` →
  `test_ideator_submission.py`), assertion strings.
- **Reference bindings**
  ([`spec/v0/reference-bindings/`](../spec/v0/reference-bindings/))
  — the subprocess binding's ``plan`` / ``proposal`` / ``trial``
  protocol message names.
- **Docs** — `MANUAL_UI_ISSUES.md`, the design docs in
  `docs/design/`, the roadmap, AGENTS.md, this glossary, archived
  plans (less critical; can be left for historical accuracy).
- **Skills** — `.claude/skills/eden-manual-{planner,implementer,evaluator}/`
  → `eden-manual-{ideator,executor,evaluator}/` (operator-tooling
  layer; not on origin/main).

The rename is mechanical for most of the surface but touches every
chapter of the spec, every package, and every service. It's best
done as one cohesive change rather than incrementally; partial
states would be confusing to anyone reading the code.

### Predecessor cruft (separate concern)

`workspace` and `planner_root` are dead config keys carried over from
the predecessor; `MANUAL_UI_ISSUES.md` #13. The rename pass is a good
moment to drop them.
