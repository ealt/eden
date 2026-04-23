# Role Contracts

This chapter defines the four role contracts that a conforming EDEN
deployment implements: **planner**, **implementer**, **evaluator**,
and **integrator**. For each, it pins what the role observes, what
it produces, and which invariants its output MUST honor.

A role contract does **not** specify how the role is hosted. A
conforming planner MAY run as a local subprocess, a long-lived
service reached over HTTP, an in-process adapter, or a human at a
keyboard. The protocol constrains the *observable effects* a role
has on the task store, the event log, and the artifact store; how a
role is invoked is a deployment concern and is deferred to a
later-phase *role-binding* specification. Within v0, implementations
MAY document their own binding by defining additional top-level
fields on the experiment config per
[`02-data-model.md`](02-data-model.md) §2.4.

The behavioral state machine that all worker roles participate in —
claim, execute, submit, reclaim — is defined in
[`04-task-protocol.md`](04-task-protocol.md). This chapter describes
only the *role-specific* part of each worker's job: what it reads,
what it produces, and what guarantees its output must carry. Where a
rule depends on a state-machine transition, this chapter cites the
transition by name and points to Chapter 04 for its semantics.

## 1. Common worker-role lifecycle

The planner, implementer, and evaluator are **worker roles**: they
participate in the task protocol as consumers. Each worker role
progresses through the same outer lifecycle for every task it
handles:

1. **Discover.** The worker becomes aware of a pending task whose
   `kind` matches its role. Mechanisms include polling the task
   store, subscribing to an event stream, or receiving a dispatch
   call — all are permitted; none are mandated.
2. **Claim.** The worker atomically moves the task from `pending`
   to `claimed` and receives a claim token. Semantics are in
   [`04-task-protocol.md`](04-task-protocol.md) §3.
3. **Execute.** The worker performs its role-specific work using the
   task payload and any context the protocol grants it (§2–§4
   below). The worker MAY report progress. It MUST NOT mutate any
   protocol-owned object other than (a) the task it holds a claim
   on and (b) the outputs its role is specified to produce (§2.2,
   §3.2, §4.2). The restrictions in §1.2 apply to every worker role
   in addition to the per-role rules below.
4. **Submit.** The worker atomically moves the task from `claimed`
   to `submitted`, presenting its claim token and a result (§2–§4
   below). A submitted task then advances to `completed` or `failed`
   per the rules in [`04-task-protocol.md`](04-task-protocol.md) §4.
5. **Release on reclaim.** If the task store reclaims the task
   before submit (task store policy per
   [`04-task-protocol.md`](04-task-protocol.md) §5), the worker's
   token is invalidated. A worker that discovers its token has been
   invalidated MUST NOT subsequently attempt to submit against that
   task; it MUST discard any partial result.

Integrator behavior does **not** follow this worker-role lifecycle.
The integrator is event-driven and is defined separately in §5 below
and in [`06-integrator.md`](06-integrator.md).

### 1.1 What a worker MAY assume about its inputs

Before a task is dispatched to a worker-role queue, the orchestrator
MUST ensure the task's payload references (proposal, trial,
parent commits) satisfy the dispatch preconditions listed per role
below. A worker MAY therefore rely on those preconditions and is not
required to re-validate them. A worker that nevertheless discovers a
violation (e.g. a referenced proposal has been retracted) MUST fail
the task with an error status; it MUST NOT repair store state
itself.

### 1.2 What a worker MUST NOT do

- A worker MUST NOT write to any protocol-owned object other than
  the task it holds a claim on and the outputs that task's role is
  defined to produce (§2–§4).
- A worker MUST NOT write to the canonical trial lineage (`trial/*`);
  that is the integrator's exclusive authority
  ([`01-concepts.md`](01-concepts.md) §9,
  [`06-integrator.md`](06-integrator.md)).
- A worker MUST NOT advance a task's state beyond `submitted`.
  Transitions to `completed` or `failed` are the orchestrator's
  responsibility ([`04-task-protocol.md`](04-task-protocol.md) §4).

## 2. Planner

The planner proposes what to try. Every planner invocation consumes
one `plan` task and produces zero or more proposals.

### 2.1 Inputs

A planner receives:

- The task object (`kind == "plan"`,
  `payload.experiment_id == E`).
- Read access to the experiment config for `E`
  ([`02-data-model.md`](02-data-model.md) §2), including the
  `metrics_schema` and `objective`.
- Read access to the set of proposals and trials already persisted
  for `E`. The mechanism (direct store read, orchestrator-supplied
  snapshot, event-log replay) is a role-binding concern.

A planner MAY additionally use any context the experiment config
surfaces to it via role-binding-specific fields. The protocol does
not require the planner to read beyond what is listed above.

### 2.2 Outputs

For each proposal the planner produces, it MUST:

1. Persist a proposal object (via the protocol's proposal store —
   mechanism is binding-specific) that validates against
   [`schemas/proposal.schema.json`](schemas/proposal.schema.json),
   with `experiment_id == E` and `state == "drafting"`.
2. Upload any artifacts (plan text, rationale, supporting files) to
   the artifact store and populate the proposal's `artifacts_uri`.
3. Transition the proposal's `state` to `"ready"` once the proposal
   is dispatchable. The transition from `drafting` to `ready`
   signals that the proposal's metadata is stable and that the
   implementer MAY consume it.

A planner MAY create multiple proposals under a single `plan` task.
A planner MAY also produce zero proposals if it has no viable
change to suggest; the task still completes normally (see §2.4).

### 2.3 Proposal invariants

The planner MUST honor the structural invariants of proposals
([`02-data-model.md`](02-data-model.md) §5):

- `parent_commits` MUST contain at least one SHA that names a
  commit reachable from either the experiment's starting commit on
  `main` or a completed trial on the canonical lineage
  ([`01-concepts.md`](01-concepts.md) §9).
- `slug` MUST match the documented pattern.
- `priority` is the planner's own ordering hint; higher values
  SHOULD dispatch earlier.

### 2.4 Submission

When its work on a `plan` task is complete, the planner MUST submit
the task (§1 step 4). The submission payload MUST include:

- `proposal_ids` — the set of identifiers of every proposal the
  planner created under this task. Order is not significant;
  dispatch ordering is determined by each proposal's `priority`
  field ([`02-data-model.md`](02-data-model.md) §5.1). MAY be
  empty.
- `status` — one of `"success"` (the planner completed normally,
  regardless of proposal count) or `"error"` (the planner could
  not complete; any partially-written proposals MUST remain in
  `"drafting"` state and MUST NOT be dispatched).

A planner MUST NOT submit a `plan` task while any of its
referenced proposals is still in `"drafting"` state.

## 3. Implementer

The implementer realizes a proposal as a working-tree change on a
per-trial branch.

### 3.1 Inputs

An implementer receives:

- The task object (`kind == "implement"`,
  `payload.proposal_id == P`).
- Read access to the proposal `P`. The orchestrator MUST ensure
  `P.state == "ready"` before dispatch.
- Read and write access to the git repository associated with
  `P.experiment_id`, at a revision reachable from
  `P.parent_commits`. How the repository is located and how
  access is granted is a role-binding concern (§6,
  [`02-data-model.md`](02-data-model.md) §2.4); the protocol
  constrains only that the implementer's writes land under
  `work/*` and that no commit is introduced whose history does
  not descend from `P.parent_commits` (§3.3).
- Read access to the artifacts at `P.artifacts_uri`.

### 3.2 Outputs

The implementer produces a **trial** object
([`02-data-model.md`](02-data-model.md) §7). It MUST:

1. Persist a trial with `status == "starting"`,
   `experiment_id == P.experiment_id`, `proposal_id == P`, and
   `parent_commits == P.parent_commits`, before making any
   repository write observable to other roles.
2. Write the implementation on a branch under `work/*`
   ([`01-concepts.md`](01-concepts.md) §9) whose parent(s) are the
   proposal's `parent_commits`. Set the trial's `branch` field to
   this branch name.
3. On successful completion, set the trial's `commit_sha` to the
   tip of the `work/*` branch. Optionally set `artifacts_uri` and
   `description`.

The implementer MUST NOT write to `trial/*` or `main`; those are
owned by the integrator. The `trial_commit_sha` field on the trial
MUST NOT be set by the implementer.

### 3.3 Worker-branch invariants

- The `work/*` branch MUST be unique to this trial. Two trials
  MUST NOT share a worker branch name.
- Every commit on the worker branch MUST be reachable from the
  declared `parent_commits`. An implementer MUST NOT introduce
  commits whose history does not descend from the proposal's
  declared parents.
- The implementer MAY produce multiple commits on the worker
  branch. The evaluator consumes only the tip (`commit_sha`); the
  integrator's squash rule is defined in
  [`06-integrator.md`](06-integrator.md).

### 3.4 Submission

The implementer submits with:

- `trial_id` — the trial it created.
- `status` — one of:
  - `"success"` — the `work/*` branch tip is `commit_sha` and is
    ready to be evaluated.
  - `"error"` — the implementer could not realize the proposal.
    The trial MUST be persisted with `status == "error"`. No
    evaluate task is dispatched against an errored trial.
- `commit_sha` — required when `status == "success"`; MUST equal
  the tip of the worker branch.

A resubmission of the same `implement` task MUST be idempotent: a
duplicate submit presenting the same `trial_id` and `commit_sha`
MUST be accepted without side effect. A duplicate submit that
disagrees with the already-recorded result MUST be rejected
([`04-task-protocol.md`](04-task-protocol.md) §4.2).

## 4. Evaluator

The evaluator measures a completed trial against the experiment's
metrics schema.

### 4.1 Inputs

An evaluator receives:

- The task object (`kind == "evaluate"`,
  `payload.trial_id == T`).
- Read access to the trial `T`. The orchestrator MUST ensure
  `T.status == "starting"` and `T.commit_sha` is set before
  dispatch.
- Read access to the git repository at `T.commit_sha` on the
  worker branch (`T.branch`). Repository location and access are
  a role-binding concern, as for the implementer (§3.1).
- Read access to the experiment's `metrics_schema` and
  `objective`.

### 4.2 Outputs

The evaluator MUST:

1. Produce a `metrics` object whose keys are a subset of the
   declared `metrics_schema` keys and whose values satisfy the
   per-metric type rules
   ([`02-data-model.md`](02-data-model.md) §1.3, §7.2).
2. Optionally upload supporting artifacts (logs, captured outputs,
   diagnostic files).

The evaluator MUST NOT modify the worker branch or any
protocol-owned mutable state other than the trial fields the
submission writes (§4.4) and the task it holds a claim on. In
particular, the evaluator MUST NOT write to the trial's
`completed_at`, `metrics`, `artifacts_uri`, `description`, or
`status` directly; those writes are performed by the orchestrator
when the submitted task reaches its terminal state (§4.4,
[`04-task-protocol.md`](04-task-protocol.md) §4.3).

### 4.3 Non-interference

The evaluator reads the repository at the trial's worker-branch tip
but MUST NOT push, rewrite, or delete that branch, and MUST NOT
write to the canonical trial lineage. Any side effects of evaluation
(test caches, build outputs, captured logs) MUST NOT be observable
through any protocol-owned store, ref, or artifact location except
via the evaluator's declared outputs (§4.2).

### 4.4 Submission

The evaluator submits with:

- `trial_id` — the trial it evaluated.
- `status` — one of:
  - `"success"` — the trial ran and produced metrics.
  - `"error"` — the trial could not be evaluated for reasons
    attributable to the trial's own code (build failure, test
    failure, etc.). The evaluator MAY still include partial
    metrics.
  - `"eval_error"` — the evaluator itself failed for reasons
    unrelated to the trial's code (infrastructure fault,
    evaluator bug). While a fresh evaluate task MAY still be
    created for this trial, the trial's status MUST remain
    `"starting"`. If the orchestrator's retry policy is
    exhausted (or the operator abandons evaluation), the
    orchestrator MUST transition the trial's status to
    `"eval_error"`, making that status terminal for the trial
    ([`04-task-protocol.md`](04-task-protocol.md) §4.3).
- `metrics` — the metrics object described in §4.2. MAY be absent
  when `status == "eval_error"`.
- `artifacts_uri` — OPTIONAL. A URI the evaluator uploaded
  supporting artifacts to.

On a `submitted → completed` or `submitted → failed` transition
(per [`04-task-protocol.md`](04-task-protocol.md) §4.3), the
orchestrator MUST write the following trial fields atomically with
the event:

- `status` — the trial status implied by the submission: `"success"`
  when the submission's `status == "success"`; `"error"` when the
  submission's `status == "error"`; unchanged from `"starting"`
  when the submission's `status == "eval_error"` (see §4.4 above
  for the terminal-retry case).
- `metrics` — set to the submission's `metrics` when
  `status ∈ {"success", "error"}`. When `status == "eval_error"`
  the orchestrator MUST NOT write `metrics` on the trial; any
  submission-carried `metrics` is discarded.
- `artifacts_uri` — set to the submission's `artifacts_uri` when
  provided and `status ∈ {"success", "error"}`. When
  `status == "eval_error"` the orchestrator MUST NOT write
  `artifacts_uri` on the trial; any submission-carried
  `artifacts_uri` is discarded. (An evaluator that wishes to
  retain diagnostic artifacts from a failed attempt MAY reference
  them in the `task.failed` event for that evaluate task; that
  channel is defined in
  [`05-event-protocol.md`](05-event-protocol.md).)
- `completed_at` — set to the time of the terminal trial
  transition, i.e. written exactly once, when the trial's status
  leaves `"starting"` (either on a `"success"`/`"error"`
  submission, or on the retry-exhausted `"eval_error"`
  transition). Intermediate `eval_error` submissions MUST NOT
  advance `completed_at`.

On the retry-exhausted `"eval_error"` terminal transition itself,
the orchestrator MUST NOT graft metrics or artifacts from any
prior `eval_error` submission onto the trial; the trial's
`metrics` and `artifacts_uri` fields remain unset. This keeps the
trial object canonical: a trial either carries the outputs of a
successful or code-level-failed evaluation, or it carries nothing.

Resubmission is idempotent under the same rules as §3.4: identical
`trial_id` + `status` + `metrics` + `artifacts_uri` MUST be
accepted; inconsistent resubmission MUST be rejected.

## 5. Integrator

The integrator promotes successfully evaluated trials into the
canonical trial lineage.

The integrator's full contract — the squash rule, the eval-manifest
shape, conflict-resolution policy — is deferred to
[`06-integrator.md`](06-integrator.md). This section pins only the
boundary rules that every other role must honor.

### 5.1 Exclusive authority

The integrator is the **sole writer** of the canonical trial lineage
(`trial/*` branches, `trial_commit_sha` fields on trials). Every
other role MUST treat `trial/*` as read-only and MUST NOT set or
modify a trial's `trial_commit_sha`.

### 5.2 Inputs

The integrator observes trials transitioning to `status == "success"`
(as recorded by the evaluator in §4.4). The mechanism of observation
(event subscription, store poll, dispatch) is a binding concern.

### 5.3 Outputs

The integrator writes a single commit on a `trial/*` branch for each
promoted trial and records the resulting SHA in the trial's
`trial_commit_sha`. Exact topology invariants are in
[`06-integrator.md`](06-integrator.md).

### 5.4 Why integration is separate from evaluation

Evaluation and integration are separate roles because a successful
evaluation is a necessary but not sufficient condition for promoting
a trial: the integrator applies experiment-level policy
(squash shape, conflict resolution with concurrent trials, manifest
attachment) that is not meaningful to the evaluator. A conforming
implementation MAY collapse evaluator and integrator into the same
process, but MUST still honor both contracts independently.

## 6. Role binding (deferred)

How a role is invoked, addressed, or configured is **not specified
in v0**. A conforming deployment MAY:

- Run each role as a separate long-lived service reached over some
  wire protocol.
- Spawn each role as a short-lived subprocess per task.
- Collapse multiple roles into a single process that consumes its
  own task queue.
- Host a role as a WebAssembly module, a browser tab, or a human
  interface.

Role-binding information that an implementation needs to invoke its
roles MAY be carried as additional top-level fields on the
experiment config
([`02-data-model.md`](02-data-model.md) §2.4). A future v0 addition
or a v1 chapter will specify a standardized role-binding
representation; until then, the protocol constrains only what each
role observes and produces, not how it is reached.
