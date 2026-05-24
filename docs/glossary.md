# EDEN glossary

A canonical list of the terms-of-art used in the EDEN protocol, the
reference implementation, and surrounding design discussions.

The glossary is organized by **kind of thing**, not alphabetically,
because the relationships between terms are part of the meaning.

EDEN is a protocol for **directed evolution** — iterative refinement
of any artifact that can be versioned in git (code, recipes, prompts,
configs, prose, etc.). The vocabulary below uses the four parallel
``-or`` role names (ideator, executor, evaluator, integrator), the
matching artifact nouns (idea, variant, evaluation), and gerund task
kinds (`ideation` / `execution` / `evaluation`).

## How to use this glossary

This glossary is the **canonical source of truth** for naming in EDEN.
Any new identifier — class name, function name, JSON enum value, CLI
flag, env var, spec heading, doc reference — MUST be consistent with
the patterns below before it lands. When in doubt:

1. Check §1 (worker roles) and §3.2 (task kinds) for the canonical
   role / verb / kind / submission / artifact alignment.
2. Check §3.1 for sub-field naming.
3. Check §4 for the state-vs-status distinction.

If the glossary disagrees with another doc (spec chapter, README,
plan), **the glossary wins** and the other doc is wrong. Surface the
disagreement; don't paper over it.

The `scripts/check-rename-discipline.py` CI guardrail catches the
specific legacy patterns retired by past renames. It is a backstop,
not a substitute for reading the glossary before introducing a new
identifier.

---

## 1. Worker roles

A **worker** is any actor that participates in the task protocol as a
consumer — claims tasks, does work, submits results. The protocol
defines four worker contracts. Workers MAY be human, agentic
(LLM-driven), or programmatic; the contracts constrain only the
observable effects.

| Term | One-line meaning | Spec ref |
|---|---|---|
| **ideator** | Proposes what to try; output is one or more *ideas* | [`01-concepts.md`](../spec/v0/01-concepts.md) §2.1 |
| **executor** | Turns an idea into git commit(s) on a per-variant *work branch* | §2.2 |
| **evaluator** | Measures an executed variant against the experiment's evaluation schema | §2.3 |
| **integrator** | Integrates a successful variant to the canonical *variant lineage* (writes a `variant/*` commit) | §2.4 |

The integrator is the only worker role with a fixed identity (in the
current spec there is exactly one logical integrator per experiment;
the role contract makes it the sole writer of `variant/*`). Ideator /
executor / evaluator are role *contracts* — many concurrent
workers may claim the same kind of task.

## 2. System actors and components

| Term | One-line meaning | Spec ref |
|---|---|---|
| **orchestrator** | A role (not a singleton). Drives the four `dispatch_mode`-gated decision types: ideation-task creation, execution-task dispatch, evaluation-task dispatch, integration. Per [`03-roles.md`](../spec/v0/03-roles.md) §6, zero, one, or many concurrent instances are permitted; each authenticates as a registered worker in the `orchestrators` group. | [`03-roles.md`](../spec/v0/03-roles.md) §6; [`01-concepts.md`](../spec/v0/01-concepts.md) §11 |
| **auto-orchestrator** | Informal: an automated polling-loop instance of the orchestrator role (`reference/services/orchestrator/`). Distinct from "the orchestrator role" — a deployment may run zero, one, or many; per-replica `EDEN_ORCHESTRATOR_WORKER_ID` MUST be unique. | (informal) |
| **operator** | Human running a deployment, distinct from worker roles. Acts through a registered worker that's a member of the `admins` group (operator-mode web UI, or direct wire calls). | (informal; not normative) |
| **owner** | The authority over an experiment — can configure, terminate, reassign, transfer. Today implicit; should be a first-class concept once authentication and authorization land. | (forward-looking) |
| **observer** / **subscriber** | Read-only consumer of the event log (UIs, dashboards, downstream tooling). Distinct from workers; has no claim/submit capability. | (informal) |
| **`admins` group** | Reserved group (chapter 02 §7.5). Members have operator authority: `reassign_task`, `update_dispatch_mode`, `create_task(kind=ideation \| evaluation)`. The deployment-admin bearer (literal `"admin"` principal) is bootstrap-only and CANNOT drive these ops — it's reserved for registry mgmt (`register_worker` / `register_group` / `reissue_credential` etc.). | [`02-data-model.md`](../spec/v0/02-data-model.md) §7.5; [`07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) §13.3 |
| **`orchestrators` group** | Reserved group (chapter 02 §7.5). Members have auto-orchestrator authority: `accept`, `reject`, `integrate_variant`, `create_task(kind=execution)`. Auto-orchestrator instances populate themselves into this group at startup via `_ensure_orchestrators_membership`; setup-experiment creates the group empty. | [`02-data-model.md`](../spec/v0/02-data-model.md) §7.5; [`07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) §13.3 |
| **control plane** | Deployment-level coordination layer (chapter 11). Maintains the experiment registry + issues leases + hosts the deployment-scoped worker/group registry. Distinct from the task-store-server (which holds per-experiment data); the control plane's only data is cross-experiment metadata. v0 ships as a single-replica reference service; HA for the control plane itself is deferred. | [`11-control-plane.md`](../spec/v0/11-control-plane.md) §1 |
| **deployment-scoped registry** | The control plane's own worker + group registry (chapter 11 §6), distinct from chapter 02 §6's per-experiment registry. A `worker_id` registered against the control plane is unrelated to a same-named worker against any per-experiment task-store-server; the two credential domains are independent. The reserved-group identifiers (`admins`, `orchestrators`) apply at the deployment scope; deployment-scoped `orchestrators` is what gates the chapter 11 §4.5 lease ops. | [`11-control-plane.md`](../spec/v0/11-control-plane.md) §6 |

## 3. Data shapes (value objects)

| Term | One-line meaning | Spec ref / schema |
|---|---|---|
| **experiment** | A single directed-evolution run (id + config + starting commit) | [`01`](../spec/v0/01-concepts.md) §1 / [`02`](../spec/v0/02-data-model.md) |
| **idea** | The ideator's output: a description of a change to attempt + dispatch metadata | [`01`](../spec/v0/01-concepts.md) §3 / [`schemas/idea.schema.json`](../spec/v0/schemas/idea.schema.json) |
| **variant** | One attempt to improve the objective; references the idea, commits, evaluation, and status | [`01`](../spec/v0/01-concepts.md) §4 / [`schemas/variant.schema.json`](../spec/v0/schemas/variant.schema.json) |
| **task** | A unit of work dispatched to a worker (kind, payload, state, claim) | [`01`](../spec/v0/01-concepts.md) §5 / [`schemas/task.schema.json`](../spec/v0/schemas/task.schema.json) |
| **event** | An immutable record of a state change, appended to the event log | [`01`](../spec/v0/01-concepts.md) §6 / [`schemas/event.schema.json`](../spec/v0/schemas/event.schema.json) |
| **submission** | The role-specific payload a worker hands back when completing a task. Three shapes: `IdeaSubmission`, `VariantSubmission`, `EvaluationSubmission`. | [`03-roles.md`](../spec/v0/03-roles.md) §§2.4 / 3.4 / 4.4 |
| **claim** | A worker's hold on a task; the task store records the worker_id and an expiry. | [`01`](../spec/v0/01-concepts.md) §8 |
| **claim token** | The opaque secret returned to a worker when it claims; required to submit. | [`01`](../spec/v0/01-concepts.md) §8 |
| **dispatch_mode** | A per-experiment object with four normative keys (`ideation_creation`, `execution_dispatch`, `evaluation_dispatch`, `integration`), each `"auto"` (default) or `"manual"`. Gates the orchestrator-role's four §6.2 decision types. Partial-merge: omitted keys preserved. Idempotent no-diff flip emits no event. | [`02-data-model.md`](../spec/v0/02-data-model.md) §2.5 |
| **lease** | A per-experiment, time-bounded ownership claim issued by the control plane (chapter 11 §4). The lease holder is the unique orchestrator replica authorized to run the chapter 03 §6.2 decisions for the leased experiment; a non-holder MUST NOT. Fields: `lease_id`, `experiment_id`, `holder` (worker_id), `holder_instance` (per-process UUID for §4.7 fencing), `acquired_at`, `expires_at`, `renewed_at`. Renewed every `lease_duration_seconds / 3` (default 10s). | [`11-control-plane.md`](../spec/v0/11-control-plane.md) §4 / [`schemas/lease.schema.json`](../spec/v0/schemas/lease.schema.json) |
| **holder_instance** | A per-process UUID generated at orchestrator startup, supplied on every `acquire_lease` / `renew_lease` / `release_lease`. Defends against two replicas misconfigured to share a `worker_id`: a second process gets a different `holder_instance` and is detected by the chapter 11 §5.2 startup probe. The control plane verifies `holder_instance` matches the stored value on every renew/release; mismatch returns 409 `eden://error/lease-instance-mismatch`. | [`11-control-plane.md`](../spec/v0/11-control-plane.md) §4.7 |
| **ideation policy** | A `Callable[[ExperimentStateView], int]` invoked once per orchestrator iteration when `dispatch_mode.ideation_creation == "auto"`; returns the number of new ideation tasks to create. Reference policies: `maintain_pending(target, max_total)` (bounded-overshoot per §6.4) and `fixed_total(N)` (one-shot equivalent of the retired `--ideation-tasks` static seed). | [`03-roles.md`](../spec/v0/03-roles.md) §6.4; [`reference/packages/eden-dispatch/src/eden_dispatch/policies.py`](../reference/packages/eden-dispatch/src/eden_dispatch/policies.py) |
| **ExperimentStateView** | Snapshot facade over experiment counters (`pending_ideation_count`, `in_flight_ideation_count`, `total_ideation_count`, `running_variant_count`, `integrated_variant_count`) passed to the ideation policy each iteration. Read-only; not a live proxy. | [`reference/packages/eden-dispatch/src/eden_dispatch/state_view.py`](../reference/packages/eden-dispatch/src/eden_dispatch/state_view.py) |

### 3.1 Sub-fields worth naming

| Term | What it is |
|---|---|
| **slug** | An idea's short kebab-case label; matches `^[a-z0-9][a-z0-9-]*$`. Used in branch naming. |
| **priority** | Per-idea ordering hint (number; "higher dispatches earlier" — currently SHOULD-level, not enforced). |
| **parent_commits** | One or more commit SHAs an idea/variant is based on. |
| **artifacts_uri** | URI pointing at idea-content or evaluator artifacts (typically `file://` in the reference impl). |
| **kind** | A task's role-routing label (`ideation` / `execution` / `evaluation`). |
| **payload** | A task's role-specific inner content. |
| **commit_sha** | The worker's tip commit on its `work/*` branch (set on the variant when the executor submits). |
| **variant_commit_sha** | The squashed-and-integrated commit on `variant/*` (set by the integrator). |
| **branch** | The canonical work branch name (`work/<variant_id>-<slug>`); set on the variant at create time. |
| **evaluation** | A dict shaped per the experiment's `evaluation_schema`. |

### 3.2 Task kinds

A task's `kind` field discriminates which role contract claims it,
what its payload looks like, and what shape its submission takes. The
three kinds are noun forms of the role actions:

| `kind` | Claimed by | Payload | Submission shape | On `accept`, the store… |
|---|---|---|---|---|
| `ideation` | ideator | `{experiment_id}` | `IdeaSubmission(status, idea_ids: tuple)` | Marks the task `completed`. The referenced ideas (which the ideator moved to `state="ready"` before submit) are then dispatched by the orchestrator as `execution` tasks. |
| `execution` | executor | `{experiment_id, idea_id}` | `VariantSubmission(status, variant_id, commit_sha?)` | On `success`: writes `commit_sha` onto the referenced variant; variant.status remains `starting`. The orchestrator then dispatches an `evaluation` task referencing the variant. On `error`: variant transitions to `error`. |
| `evaluation` | evaluator | `{experiment_id, variant_id}` | `EvaluationSubmission(status, variant_id, evaluation?, artifacts_uri?)` | On `success`: variant transitions to `success` with the submitted evaluation. The integrator then integrates it (writes `variant_commit_sha`). On `error`: variant transitions to `error`. On `evaluation_error`: variant stays in `starting` (the evaluator didn't form a verdict; the variant remains evaluable). |

The full state-machine semantics — including reject paths,
validation_error handling, and idempotent resubmit — live in
[`spec/v0/04-task-protocol.md`](../spec/v0/04-task-protocol.md) §4.
This table is the at-a-glance summary of the happy path.

A few observations the table makes visible that aren't always obvious:

- The **ideator doesn't directly produce variants**; it produces
  ideas, and the *orchestrator* (in its dispatch role) creates
  the execution tasks that produce variants. The hand-off shape is
  ready idea → execution task → variant.
- A variant's status transitions from `starting` to its terminal
  status (`success` / `error` / `evaluation_error`) at the *evaluator's*
  submission, not the executor's. The executor's submission
  only sets `commit_sha`; the variant stays `starting` between
  the execution-task submission and the evaluation-task submission.
- `evaluation_error` is the only status where the variant doesn't
  terminalize. The evaluator declared the variant unevaluable but
  didn't condemn it; another evaluator (or the same one with more
  context) MAY produce a verdict later. (See
  [`spec/v0/03-roles.md`](../spec/v0/03-roles.md) §4.4.)

## 4. Lifecycle vocabulary (states, statuses, transitions)

EDEN deliberately uses different words for different lifecycle
machines, even when conceptually similar. Worth knowing the mapping:

| Object | Field | Possible values |
|---|---|---|
| Task | `state` | `pending` → `claimed` → `submitted` → `completed` (or `failed`) |
| Idea | `state` | `drafting` → `ready` → `dispatched` → `completed` |
| Variant | `status` | `starting` → `success` (or `error`, `evaluation_error`) |
| Experiment | `state` | `running` → `terminated` (one-way per 12a-3 `02-data-model.md` §2.5) |
| IdeaSubmission | `status` | `success`, `error` |
| VariantSubmission | `status` | `success`, `error` |
| EvaluationSubmission | `status` | `success`, `error`, `evaluation_error` |

**state vs. status** is intentional: tasks/ideas use `state` (the
protocol's lifecycle); variants and submissions use `status`
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
| **reassign** | Operator updates a task's `target` field. Pending → single `task.reassigned` event. Claimed → composite-commit (`task.reclaimed(cause=operator)` + `task.reassigned`); execution tasks with an in-flight starting variant additionally emit `variant.errored`. Submitted/terminal → 409 invalid-precondition. Authority: caller in `admins`. |
| **update_dispatch_mode** | Operator atomically merges a partial `dispatch_mode` object into the experiment's stored state. Idempotent no-diff flips emit no event. The event payload carries the full post-update state + a `changed` diff + `updated_by`. Authority: caller in `admins`. |
| **dispatch** | Orchestrator creates a downstream task from a state transition (e.g. ready idea → execution task; success-with-commit_sha variant → evaluation task). |
| **integrate** | Integrator squashes a successful variant's `work/*` content into a single commit on `variant/*`, attaches the evaluation manifest, and emits `variant.integrated`. |
| **terminate** | Commit the `running → terminated` lifecycle transition on an experiment (12a-3 `02-data-model.md` §2.5 + `04-task-protocol.md` §8.1). Composite-commits the state field update and `experiment.terminated` event atomically. Idempotent on the terminated state — a second call returns success without a second event; the winning caller's `reason` is the one recorded. Authority: caller in `admins`. Two drivers: an operator wire op (`POST /v0/experiments/{E}/terminate`) and the orchestrator's policy-driven branch (decision-type 0). The terminated state is **absorbing** in v0 — no `terminated → running` transition exists; reactivation is reserved for a future spec lineage. Drain semantics: already-claimed tasks may still complete; integration drains success-variants normally; only the three creation/dispatch decisions are suppressed. |
| **intended_executor** | Optional `TaskTarget`-shaped routing hint set on an `Idea` at creation time (12a-3 `02-data-model.md` §5.1). When the orchestrator's `execution_dispatch` decision creates an execution task from the idea, it copies `intended_executor` to `task.target` per `03-roles.md` §6.2 decision-type 2. Resolution is **claim-time**: a deregistered worker / emptied group named in the hint leaves the resulting task pending until an operator reassigns. The admin-driven `create_task(kind=execution)` path (12a-3 §6.5 authority lift) accepts an explicit `target` override that wins over `idea.intended_executor`. |

## 5. Storage components

| Term | One-line meaning | Spec ref |
|---|---|---|
| **task store** | Durable store of tasks, ideas, variants, submissions; provides atomic claim and idempotent submit | [`08-storage.md`](../spec/v0/08-storage.md) |
| **event log** | Append-only ordered log of events; provides replay and subscribe | [`05-event-protocol.md`](../spec/v0/05-event-protocol.md) |
| **artifact store** | Holds content documents, evaluator artifacts, etc., addressed by URI | [`08-storage.md`](../spec/v0/08-storage.md) §5 (deferred) |

In the reference impl, all three are backed by Postgres + the
deployment filesystem; the protocol abstracts over the choice.

## 6. Git topology

EDEN maintains three branch namespaces in the experiment's git repo:

| Namespace | What lives there | Writer |
|---|---|---|
| **`main`** | The experiment's starting point. Immutable during an experiment. | Set once at `setup-experiment` time. |
| **`work/*`** | Per-attempt executor branches; named `work/<variant_id>-<slug>` (reference impl; the spec leaves naming under `work/*` implementation-defined). | Executors (concurrent writes allowed). |
| **`variant/*`** | The canonical lineage; one commit per successfully integrated variant. | Integrator only. |

| Term | What it is |
|---|---|
| **seed commit** / **base commit** | The single commit on `main` at experiment start. Captured as `EDEN_BASE_COMMIT_SHA` in deployment env. |
| **work branch** | A branch in the `work/*` namespace (`work/<variant_id>-<slug>` in the reference impl); records what the executor *wrote* — the executor's tip commit, including any intermediate work commits. Set on the variant at create time as `variant.branch`. |
| **variant branch** | A branch in the `variant/*` namespace (`variant/<variant_id>-<slug>`, spec ch06 §3.2); records the integrator-produced *squash* of the work branch — one commit per successfully integrated variant, with the evaluation manifest attached. Spec-authoritative naming. |
| **evaluation manifest** | A JSON file at `.eden/variants/<variant_id>/evaluation.json` in the `variant/*` commit's tree, containing the evaluator's evaluation. Spec-authoritative path. |
| **bare repo** | The git repository hosted on the workers' git remote of record (Forgejo in the reference deployment). |

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
| **worker id** | A registered worker's identifier within an experiment. Matches `^[a-z0-9][a-z0-9_-]{0,63}$` per [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) §6.1. Each worker host registers itself at startup under its `--worker-id` and the registry per-experiment ([`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) §6) tracks the row. |
| **admin token** | The deployment-wide secret the operator generates at setup time. Used as the secret half of `Authorization: Bearer admin:<token>` for admin-gated operations (`register_worker`, `reissue_credential`, …) per [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) §13.2. NOT used for worker-host wire calls — each host bootstraps a per-worker credential and uses that instead. |
| **registration token** | The per-worker secret issued by `register_worker` (first call) or `reissue_credential` (rotation). Used as the secret half of `Authorization: Bearer <worker_id>:<token>` per chapter 07 §13.2. Persisted by the reference worker hosts under `--credentials-dir`. |
| **group** | A named, recursively-resolved set of workers and other groups within a single experiment ([`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) §7). Tasks may target a group as a routing intent broader than a single worker. |
| **target** (task) | Optional routing hint on a `Task` — either absent (any registered worker may claim), `{kind: "worker", id: <wid>}` (only that worker), or `{kind: "group", id: <gid>}` (any transitive member). Enforced atomically with the claim write per [`spec/v0/04-task-protocol.md`](../spec/v0/04-task-protocol.md) §3.5. |
| **claim ownership** | Identity-keyed ownership of a claimed task: ``task.claim.worker_id`` is the sole identity the §4 submit transition matches against. Per-claim tokens were retired in Phase 12a-1; authentication is binding-layer. |
| **attribution fields** | `submitted_by` (on tasks), `executed_by` / `evaluated_by` (on variants), and `created_by` (on ideas / tasks / groups) — the `worker_id` recorded with the artifact and preserved across terminal transitions ([`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) §3.1, §5.1, §9). |
| **iteration** (orchestrator) | One pass through the orchestrator's loop body (finalize submitted, dispatch execution, dispatch evaluation, finalize submitted, integrate successful). |
| **quiescence** | Heuristic in the current orchestrator: N consecutive iterations with no progress → exit. Not spec-defined. |
| **checkpoint** | Snapshot of an experiment's state for save/restore. The **portable checkpoint format** is the spec-defined archive (tar of a directory tree with a `manifest.json`, JSONL files per object kind, a `git bundle`, and content-addressed `artifacts/sha256/<hex>` files) per [`spec/v0/10-checkpoints.md`](../spec/v0/10-checkpoints.md). Implementations that claim the `v1+checkpoints` conformance level emit and consume this format. Verbs: **export** / **import**. The pre-12b native postgres-dump + forgejo-tar format is retired. |
| **`checkpoint:sha256:<hex>` URI** | Content-addressed scheme used only inside a portable-checkpoint archive: each `<hex>` is the lowercase SHA-256 of an artifact's bytes; the corresponding bytes live at `artifacts/sha256/<hex>` in the archive ([`spec/v0/10-checkpoints.md`](../spec/v0/10-checkpoints.md) §7). On import, the receiving Store rewrites each occurrence to its deployment-local URI (`file://`, `s3://`, …). Not a wire-resolvable scheme outside the archive. |
| **import provenance** | The `Experiment.imported_from` field carrying `{checkpoint_exported_at, checkpoint_format_version}` set at import time; recovery-probe anchor for the lost-201 case in [`spec/v0/10-checkpoints.md`](../spec/v0/10-checkpoints.md) §10. Absent on natively-created experiments. |
| **manifest** (in setup-experiment context) | A `.env` + `experiment-config.yaml` + forgejo credential helper produced for one experiment. Different from "evaluation manifest" above. |

## 9. Build / packaging vocabulary

| Term | What it is |
|---|---|
| **uv workspace** | The Python-side monorepo layout under [`reference/`](../reference/), with `pyproject.toml` declaring the workspace members. |
| **package** | A library member of the uv workspace under `reference/packages/eden-*` (e.g., `eden-contracts`, `eden-storage`). |
| **service** | A runnable application under `reference/services/*` (e.g., `task-store-server`, `web-ui`). |
| **eden-reference:dev** | The shared docker image all reference services run from (multi-stage `uv sync --frozen --no-dev --all-packages`). |
| **eden-runtime:dev** | A smaller image used as the default sibling-container target in docker-exec mode. |
| **subprocess mode** | Worker host config where the user's `*_command` runs as a child process of the host. |
| **docker-exec mode** | Worker host config where the user's `*_command` runs in a sibling container via DooD. |
