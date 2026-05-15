# Role Contracts

This chapter defines the four role contracts that a conforming EDEN deployment implements: **ideator**, **executor**, **evaluator**, and **integrator**. For each, it pins what the role observes, what it produces, and which invariants its output MUST honor.

A role contract does **not** specify how the role is hosted. A conforming ideator MAY run as a local subprocess, a long-lived service reached over HTTP, an in-process adapter, or a human at a keyboard. The protocol constrains the *observable effects* a role has on the task store, the event log, and the artifact store; how a role is invoked is a deployment concern and is deferred to a later-phase *role-binding* specification. Within v0, implementations MAY document their own binding by defining additional top-level fields on the experiment config per [`02-data-model.md`](02-data-model.md) §2.4.

The behavioral state machine that all worker roles participate in — claim, execute, submit, reclaim — is defined in [`04-task-protocol.md`](04-task-protocol.md). This chapter describes only the *role-specific* part of each worker's job: what it reads, what it produces, and what guarantees its output must carry. Where a rule depends on a state-machine transition, this chapter cites the transition by name and points to Chapter 04 for its semantics.

## 1. Common worker-role lifecycle

The ideator, executor, and evaluator are **worker roles**: they participate in the task protocol as consumers. Each worker role progresses through the same outer lifecycle for every task it handles:

1. **Discover.** The worker becomes aware of a pending task whose `kind` matches its role. Mechanisms include polling the task store, subscribing to an event stream, or receiving a dispatch call — all are permitted; none are mandated.
2. **Claim.** The worker atomically moves the task from `pending` to `claimed`; the task store records the worker's `worker_id` as the claim owner. Semantics are in [`04-task-protocol.md`](04-task-protocol.md) §3.
3. **Execute.** The worker performs its role-specific work using the task payload and any context the protocol grants it (§2–§4 below). The worker MAY report progress. It MUST NOT mutate any protocol-owned object other than (a) the task it holds a claim on and (b) the outputs its role is specified to produce (§2.2, §3.2, §4.2). The restrictions in §1.2 apply to every worker role in addition to the per-role rules below.
4. **Submit.** The worker atomically moves the task from `claimed` to `submitted` by authenticating as the recorded claimant; the task store performs the atomic claim-match per [`04-task-protocol.md`](04-task-protocol.md) §4.1. A submitted task then advances to `completed` or `failed` per the rules in [`04-task-protocol.md`](04-task-protocol.md) §4.
5. **Release on reclaim.** If the task store reclaims the task before submit (task store policy per [`04-task-protocol.md`](04-task-protocol.md) §5), the claim is cleared. A worker that discovers its claim has been cleared MUST NOT subsequently attempt to submit against that task; it MUST discard any partial result.

Integrator behavior does **not** follow this worker-role lifecycle. The integrator is event-driven and is defined separately in §5 below and in [`06-integrator.md`](06-integrator.md).

### 1.1 What a worker MAY assume about its inputs

Before a task is dispatched to a worker-role queue, the orchestrator MUST ensure the task's payload references (idea, variant, parent commits) satisfy the dispatch preconditions listed per role below. A worker MAY therefore rely on those preconditions and is not required to re-validate them. A worker that nevertheless discovers a violation (e.g. a referenced idea has been retracted) MUST fail the task with an error status; it MUST NOT repair store state itself.

### 1.2 What a worker MUST NOT do

- A worker MUST NOT write to any protocol-owned object other than the task it holds a claim on and the outputs that task's role is defined to produce (§2–§4).
- A worker MUST NOT write to the canonical variant lineage (`variant/*`); that is the integrator's exclusive authority ([`01-concepts.md`](01-concepts.md) §9, [`06-integrator.md`](06-integrator.md)).
- A worker MUST NOT advance a task's state beyond `submitted`. Transitions to `completed` or `failed` are the orchestrator's responsibility ([`04-task-protocol.md`](04-task-protocol.md) §4).

## 2. Ideator

The ideator proposes what to try. Every ideator invocation consumes one `ideation` task and produces zero or more ideas.

### 2.1 Inputs

An ideator receives:

- The task object (`kind == "ideation"`, `payload.experiment_id == E`).
- Read access to the experiment config for `E` ([`02-data-model.md`](02-data-model.md) §2), including the `evaluation_schema` and `objective`.
- Read access to the set of ideas and variants already persisted for `E`. The mechanism (direct store read, orchestrator-supplied snapshot, event-log replay) is a role-binding concern.

An ideator MAY additionally use any context the experiment config surfaces to it via role-binding-specific fields. The protocol does not require the ideator to read beyond what is listed above.

### 2.2 Outputs

For each idea the ideator produces, it MUST:

1. Persist an idea object (via the protocol's idea store — mechanism is binding-specific) that validates against [`schemas/idea.schema.json`](schemas/idea.schema.json), with `experiment_id == E` and `state == "drafting"`.
2. Upload any artifacts (idea content, supporting files) to the artifact store and populate the idea's `artifacts_uri`.
3. Transition the idea's `state` to `"ready"` once the idea is dispatchable. The transition from `drafting` to `ready` signals that the idea's metadata is stable and that the executor MAY consume it.

An ideator MAY create multiple ideas under a single `ideation` task. An ideator MAY also produce zero ideas if it has no viable change to suggest; the task still completes normally (see §2.4).

### 2.3 Idea invariants

The ideator MUST honor the structural invariants of ideas ([`02-data-model.md`](02-data-model.md) §5):

- `parent_commits` MUST contain at least one SHA that names a commit reachable from either the experiment's starting commit on `main` or a completed variant on the canonical lineage ([`01-concepts.md`](01-concepts.md) §9).
- `slug` MUST match the documented pattern.
- `priority` is the ideator's own ordering hint; higher values SHOULD dispatch earlier.

### 2.4 Submission

When its work on a `ideation` task is complete, the ideator MUST submit the task (§1 step 4). The submission payload MUST include:

- `idea_ids` — the set of identifiers of every idea the ideator created under this task. Order is not significant; dispatch ordering is determined by each idea's `priority` field ([`02-data-model.md`](02-data-model.md) §5.1). MAY be empty.
- `status` — one of `"success"` (the ideator completed normally, regardless of idea count) or `"error"` (the ideator could not complete; any partially-written ideas MUST remain in `"drafting"` state and MUST NOT be dispatched).

An ideator MUST NOT submit a `ideation` task while any of its referenced ideas is still in `"drafting"` state.

## 3. Executor

The executor realizes an idea as a working-tree change on a per-variant branch.

### 3.1 Inputs

An executor receives:

- The task object (`kind == "execution"`, `payload.idea_id == P`).
- Read access to the idea `P`. The orchestrator MUST ensure `P.state == "ready"` before dispatch.
- Read and write access to the git repository associated with `P.experiment_id`, at a revision reachable from `P.parent_commits`. How the repository is located and how access is granted is a role-binding concern (§6, [`02-data-model.md`](02-data-model.md) §2.4); the protocol constrains only that the executor's writes land under `work/*` and that no commit is introduced whose history does not descend from `P.parent_commits` (§3.3).
- Read access to the artifacts at `P.artifacts_uri`.

### 3.2 Outputs

The executor produces a **variant** object ([`02-data-model.md`](02-data-model.md) §9). It MUST:

1. Persist a variant with `status == "starting"`, `experiment_id == P.experiment_id`, `idea_id == P`, and `parent_commits == P.parent_commits`, before making any repository write observable to other roles.
2. Write the implementation on a branch under `work/*` ([`01-concepts.md`](01-concepts.md) §9) whose parent(s) are the idea's `parent_commits`. Set the variant's `branch` field to this branch name.
3. On successful completion, set the variant's `commit_sha` to the tip of the `work/*` branch. Optionally set `artifacts_uri` and `description`.

The executor MUST NOT write to `variant/*` or `main`; those are owned by the integrator. The `variant_commit_sha` field on the variant MUST NOT be set by the executor.

### 3.3 Worker-branch invariants

- The `work/*` branch MUST be unique to this variant. Two variants MUST NOT share a worker branch name.
- Every commit on the worker branch MUST be reachable from the declared `parent_commits`. An executor MUST NOT introduce commits whose history does not descend from the idea's declared parents.
- The executor MAY produce multiple commits on the worker branch. The evaluator consumes only the tip (`commit_sha`); the integrator's squash rule is defined in [`06-integrator.md`](06-integrator.md).
- **Non-no-op variant.** A `VariantSubmission` with `status == "success"` MUST satisfy: there exists at least one entry `p` in `idea.parent_commits` such that the git tree of `commit_sha` differs from the git tree of `p`. A variant whose tree is identical to *every* parent's tree is not a candidate — it is the absence of a candidate — and the task store MUST reject it (see §3.4 below). The rule applies only to `status == "success"`; `status == "error"` submissions are not constrained by tree-shape, since an `error` submission records a failed execution attempt rather than a candidate. ([`schemas/idea.schema.json`](schemas/idea.schema.json) requires `parent_commits` to be non-empty, so the rule has at least one parent to compare against on every well-formed idea.)

### 3.4 Submission

The executor submits with:

- `variant_id` — the variant it created.
- `status` — one of:
  - `"success"` — the `work/*` branch tip is `commit_sha` and is ready to be evaluated.
  - `"error"` — the executor could not realize the idea. The variant MUST be persisted with `status == "error"`. No evaluation task is dispatched against an errored variant.
- `commit_sha` — required when `status == "success"`; MUST equal the tip of the worker branch.

A resubmission of the same `execution` task MUST be idempotent: a duplicate submit presenting the same `variant_id` and `commit_sha` MUST be accepted without side effect. A duplicate submit that disagrees with the already-recorded result MUST be rejected ([`04-task-protocol.md`](04-task-protocol.md) §4.2).

A submission that violates the §3.3 non-no-op invariant MUST be rejected. The rejection MAY surface at submit time (`POST /v0/.../tasks/{T}/submit` returns 4xx) or at accept time (the orchestrator routes the submitted task to `failed` via the validation-error path); the observable end-state guarantee is that the referenced variant MUST NOT terminalize as `status == "success"`. When the rejection surfaces a wire error envelope, the `type` MUST be `eden://error/no-op-variant` ([`07-wire-protocol.md`](07-wire-protocol.md) §9). The check is content-derived (variant tree vs parent tree) and does not depend on task state, so it is safe to perform either before or after the §4.1 atomic claim-match. The rule is content-derived (it depends only on the submission's `commit_sha` and the idea's `parent_commits`), so resubmitting the same shape will be rejected the same way; no idempotency special-casing is required.

## 4. Evaluator

The evaluator measures a completed variant against the experiment's evaluation schema.

### 4.1 Inputs

An evaluator receives:

- The task object (`kind == "evaluation"`, `payload.variant_id == T`).
- Read access to the variant `T`. The orchestrator MUST ensure `T.status == "starting"` and `T.commit_sha` is set before dispatch.
- Read access to the git repository at `T.commit_sha` on the worker branch (`T.branch`). Repository location and access are a role-binding concern, as for the executor (§3.1).
- Read access to the experiment's `evaluation_schema` and `objective`.

### 4.2 Outputs

The evaluator MUST:

1. Produce a `metrics` object whose keys are a subset of the declared `evaluation_schema` keys and whose values satisfy the per-metric type rules ([`02-data-model.md`](02-data-model.md) §1.3, §9.2).
2. Optionally upload supporting artifacts (logs, captured outputs, diagnostic files).

The evaluator MUST NOT modify the worker branch or any protocol-owned mutable state other than the variant fields the submission writes (§4.4) and the task it holds a claim on. In particular, the evaluator MUST NOT write to the variant's `completed_at`, `metrics`, `artifacts_uri`, `description`, or `status` directly; those writes are performed by the orchestrator when the submitted task reaches its terminal state (§4.4, [`04-task-protocol.md`](04-task-protocol.md) §4.3).

### 4.3 Non-interference

The evaluator reads the repository at the variant's worker-branch tip but MUST NOT push, rewrite, or delete that branch, and MUST NOT write to the canonical variant lineage. Any side effects of evaluation (test caches, build outputs, captured logs) MUST NOT be observable through any protocol-owned store, ref, or artifact location except via the evaluator's declared outputs (§4.2).

### 4.4 Submission

The evaluator submits with:

- `variant_id` — the variant it evaluated.
- `status` — one of:
  - `"success"` — the variant ran and produced metrics.
  - `"error"` — the variant could not be evaluated for reasons attributable to the variant's own code (build failure, test failure, etc.). The evaluator MAY still include partial metrics.
  - `"evaluation_error"` — the evaluator itself failed for reasons unrelated to the variant's code (infrastructure fault, evaluator bug). While a fresh evaluation task MAY still be created for this variant, the variant's status MUST remain `"starting"`. If the orchestrator's retry policy is exhausted (or the operator abandons evaluation), the orchestrator MUST transition the variant's status to `"evaluation_error"`, making that status terminal for the variant ([`04-task-protocol.md`](04-task-protocol.md) §4.3).
- `metrics` — the evaluation object described in §4.2. MAY be absent when `status == "evaluation_error"`.
- `artifacts_uri` — OPTIONAL. A URI the evaluator uploaded supporting artifacts to.

On a `submitted → completed` or `submitted → failed` transition (per [`04-task-protocol.md`](04-task-protocol.md) §4.3), the orchestrator MUST write the following variant fields atomically with the event:

- `status` — the variant status implied by the submission: `"success"` when the submission's `status == "success"`; `"error"` when the submission's `status == "error"`; unchanged from `"starting"` when the submission's `status == "evaluation_error"` (see §4.4 above for the terminal-retry case).
- `metrics` — set to the submission's `metrics` when `status ∈ {"success", "error"}`. When `status == "evaluation_error"` the orchestrator MUST NOT write `metrics` on the variant; any submission-carried `metrics` is discarded.
- `artifacts_uri` — set to the submission's `artifacts_uri` when provided and `status ∈ {"success", "error"}`. When `status == "evaluation_error"` the orchestrator MUST NOT write `artifacts_uri` on the variant; any submission-carried `artifacts_uri` is discarded. (An evaluator that wishes to retain diagnostic artifacts from a failed attempt MAY reference them in the `task.failed` event for that evaluation task; that channel is defined in [`05-event-protocol.md`](05-event-protocol.md).)
- `completed_at` — set to the time of the terminal variant transition, i.e. written exactly once, when the variant's status leaves `"starting"` (either on a `"success"`/`"error"` submission, or on the retry-exhausted `"evaluation_error"` transition). Intermediate `evaluation_error` submissions MUST NOT advance `completed_at`.

On the retry-exhausted `"evaluation_error"` terminal transition itself, the orchestrator MUST NOT graft metrics or artifacts from any prior `evaluation_error` submission onto the variant; the variant's `metrics` and `artifacts_uri` fields remain unset. This keeps the variant object canonical: a variant either carries the outputs of a successful or code-level-failed evaluation, or it carries nothing.

Resubmission is idempotent under the same rules as §3.4 and [`04-task-protocol.md`](04-task-protocol.md) §4.2: identical normative fields (`variant_id`, `status`, `metrics`) MUST be accepted; inconsistent resubmission MUST be rejected. `artifacts_uri` is NOT part of equivalence — the first submission's `artifacts_uri` is the committed one. (Earlier drafts of this section listed `artifacts_uri` as part of the equivalence formula; the §4.2 statement is canonical and this section now defers to it.)

## 5. Integrator

The integrator integrates successfully evaluated variants into the canonical variant lineage.

The integrator's full contract — the squash rule, the evaluation-manifest shape, conflict-resolution policy — is deferred to [`06-integrator.md`](06-integrator.md). This section pins only the boundary rules that every other role must honor.

### 5.1 Exclusive authority

The integrator is the **sole writer** of the canonical variant lineage (`variant/*` branches, `variant_commit_sha` fields on variants). Every other role MUST treat `variant/*` as read-only and MUST NOT set or modify a variant's `variant_commit_sha`.

### 5.2 Inputs

The integrator observes variants transitioning to `status == "success"` (as recorded by the evaluator in §4.4). The mechanism of observation (event subscription, store poll, dispatch) is a binding concern.

### 5.3 Outputs

The integrator writes a single commit on a `variant/*` branch for each integrated variant and records the resulting SHA in the variant's `variant_commit_sha`. Exact topology invariants are in [`06-integrator.md`](06-integrator.md).

### 5.4 Why integration is separate from evaluation

Evaluation and integration are separate roles because a successful evaluation is a necessary but not sufficient condition for integrating a variant: the integrator applies experiment-level policy (squash shape, conflict resolution with concurrent variants, manifest attachment) that is not meaningful to the evaluator. A conforming implementation MAY collapse evaluator and integrator into the same process, but MUST still honor both contracts independently.

## 6. Orchestrator

The orchestrator drives the task protocol. Unlike the worker roles (§2–§4), the orchestrator does not claim and submit tasks; it creates them, finalizes their submitted state, and integrates successful variants. A conforming deployment MAY run zero, one, or multiple orchestrator instances. Multi-instance deployments rely on §6.4 for safety.

The orchestrator is **a role**, not a singleton process. Anything authenticated as a member of the `orchestrators` group ([`02-data-model.md`](02-data-model.md) §7.5) that respects this contract is an orchestrator: an automated polling service is one instance, a human driving the same wire ops manually is another, a single-shot batch script that runs one iteration of the loop is another. The spec constrains the **decisions** and their **idempotency / authority** envelopes; the mechanism is binding-defined.

### 6.1 Decisions are gated by `dispatch_mode`

The orchestrator MUST execute the four decision types defined in §6.2 below. Each decision type is independently gated by the experiment's `dispatch_mode.<decision>` field ([`02-data-model.md`](02-data-model.md) §2.5).

- When `dispatch_mode.<decision>` is `"auto"`, an orchestrator instance MAY run the decision.
- When `dispatch_mode.<decision>` is `"manual"`, every orchestrator instance MUST NOT run the decision. The decision is reserved for an authorized external caller (typically a human via the Web UI or an automation script) using the same wire ops the orchestrator would have used — see §6.5.

The mode is per-experiment and per-decision; flipping `evaluation_dispatch` to manual does not affect the orchestrator's authority to run the other three decisions.

### 6.2 Decision types

1. **Ideation-task creation** (`dispatch_mode.ideation_creation`). The orchestrator MAY create new `kind == "ideation"` tasks per a deployment-defined policy. The policy mechanism is binding-specific: the reference impl exposes a pluggable callable that consumes an experiment-state view and returns a count of new tasks to create per iteration (see §6.4 "bounded-overshoot" class). The protocol does not prescribe a specific policy.
2. **Execution-task dispatch** (`dispatch_mode.execution_dispatch`). For each idea with `state == "ready"` that has no live `kind == "execution"` task referencing it (no task in `pending`, `claimed`, or `submitted` whose `payload.idea_id` equals the idea's id), an orchestrator instance MUST eventually create exactly one `kind == "execution"` task with `payload.idea_id` set. The "exactly one" property is enforced by the task store under §6.4's exact-idempotent class.
3. **Evaluation-task dispatch** (`dispatch_mode.evaluation_dispatch`). For each variant with `status == "starting"` and `commit_sha` set that has no live `kind == "evaluation"` task referencing it (no task in `pending`, `claimed`, or `submitted` whose `payload.variant_id` equals the variant's id), an orchestrator instance MUST eventually create exactly one `kind == "evaluation"` task with `payload.variant_id` set. Same "exactly one" property as execution dispatch.
4. **Integration** (`dispatch_mode.integration`). For each variant with `status == "success"` and `variant_commit_sha` unset, an orchestrator instance MUST eventually invoke the integrator ([`06-integrator.md`](06-integrator.md)) — which writes the §3.4 (variant_commit_sha, `variant.integrated`) composite under same-value idempotency ([`07-wire-protocol.md`](07-wire-protocol.md) §5).

### 6.3 Authority boundary

The orchestrator MUST authenticate as a registered worker per [`07-wire-protocol.md`](07-wire-protocol.md) §13. The orchestrator's `worker_id` MUST be a member of the reserved `orchestrators` group ([`02-data-model.md`](02-data-model.md) §7.5); wire endpoints that gate on `orchestrators` membership ([`07-wire-protocol.md`](07-wire-protocol.md) §13.3) MUST refuse calls from workers outside the group.

The orchestrator MUST NOT impersonate other workers when finalizing submissions. The `submitted_by` field on a terminalized task ([`02-data-model.md`](02-data-model.md) §3.1) reflects the **claimant**'s `worker_id` written at §4.1 submit time, not the orchestrator's. Similarly the `executed_by` / `evaluated_by` attribution on variants ([`02-data-model.md`](02-data-model.md) §9) is written from `task.submitted_by` on the accept and reject paths, never overridden by whoever invoked `accept` / `reject`.

### 6.4 Multi-instance safety

The four decision types fall into two safety classes under concurrent execution by N orchestrator instances:

**Exact-idempotent decisions.** `execution_dispatch`, `evaluation_dispatch`, and `integration` MUST be exactly idempotent under concurrent execution: repeated or concurrent invocation MUST converge on a single outcome. The task store MUST enforce uniqueness constraints sufficient to make this property mechanical:

- At most one **live** (`pending` / `claimed` / `submitted`) `kind == "execution"` task per `payload.idea_id`. A second concurrent `create_execution_task(idea_id=I)` MUST observe the first's commit and either no-op (returning the existing task) or fail with `eden://error/already-exists`; it MUST NOT produce a second distinct task.
- At most one **live** `kind == "evaluation"` task per `payload.variant_id`. Same shape.
- Exactly one `variant_commit_sha` assignment per variant. Concurrent `integrate_variant` calls with the same SHA MUST collapse to one wire-visible `variant.integrated` event per [`06-integrator.md`](06-integrator.md) §3.4 and [`07-wire-protocol.md`](07-wire-protocol.md) §5's same-value idempotency.

**Bounded-overshoot decisions.** `ideation_creation` MUST be bounded under concurrent execution but is NOT required to be exactly idempotent. With N concurrent orchestrator instances each applying a policy that targets a pending count of `T`, the post-iteration pending count MUST be ≤ `N * T`. Each orchestrator MUST read the experiment's pending-ideation-task count before deciding how many tasks to create, so that subsequent iterations self-correct downward as pending exceeds `T`. A deployment that requires exact control over pending-task count MUST supply a policy callable that implements its own coordination (e.g., advisory locks via the store); the protocol does not require that coordination at the role level.

The split is intentional: dispatch and integration are CAS-friendly (a single CAS commit decides the outcome), so demanding exactness costs nothing. Ideation creation is not CAS-friendly (the policy returns a count, not a single resource), so demanding exactness would force every orchestrator into a global lock — exactly the lease mechanism this contract deliberately avoids. A deployment that adds a non-CAS-friendly decision type in a later spec lineage MAY introduce a lease primitive at that point; v0 does not.

Conformance scenarios for §6.4 assert the four decision types under simulated multi-instance contention; see [`09-conformance.md`](09-conformance.md) §5.

### 6.5 Manual mode

When `dispatch_mode.<decision>` is `"manual"`, the decision is driven by an authorized external caller using the same wire ops the orchestrator would have used:

- Manual `ideation_creation` / `evaluation_dispatch`: a caller in `admins` ([`02-data-model.md`](02-data-model.md) §7.5) calls `create_task` ([`07-wire-protocol.md`](07-wire-protocol.md) §2.1) with the appropriate `kind` and `payload`.
- Manual `execution_dispatch`: v0 with 12a-2 applied does not define a non-orchestrator caller path for `create_task(kind="execution")` — the [`07-wire-protocol.md`](07-wire-protocol.md) §13.3 authority requirement is `orchestrators` for execution tasks (a future spec lineage that introduces an `intended_executor` hint on ideas will add an admin-gated path). Configuring `execution_dispatch == "manual"` is permitted but no caller can drive it without temporarily flipping back to `"auto"`; a deployment that wants per-task routing control SHOULD instead leave the flag `"auto"` and `reassign_task` ([`04-task-protocol.md`](04-task-protocol.md) §6) after creation.
- Manual `integration`: a caller in `orchestrators` calls `integrate_variant` ([`07-wire-protocol.md`](07-wire-protocol.md) §5).

The orchestrator-role contract is mechanism-neutral: the spec does not distinguish "human created this task" from "auto-orchestrator created this task" beyond the `task.created_by` attribution ([`02-data-model.md`](02-data-model.md) §3.1). The `dispatch_mode` flag exists so an operator can carve out a window of authority for themselves on a specific decision type without forking the orchestrator off-protocol.

## 7. Role binding (deferred)

How a role is invoked, addressed, or configured is **not specified in v0**. A conforming deployment MAY:

- Run each role as a separate long-lived service reached over some wire protocol.
- Spawn each role as a short-lived subprocess per task.
- Collapse multiple roles into a single process that consumes its own task queue.
- Host a role as a WebAssembly module, a browser tab, or a human interface.

Role-binding information that an implementation needs to invoke its roles MAY be carried as additional top-level fields on the experiment config ([`02-data-model.md`](02-data-model.md) §2.4). A future v0 addition or a v1 chapter will specify a standardized role-binding representation; until then, the protocol constrains only what each role observes and produces, not how it is reached.
