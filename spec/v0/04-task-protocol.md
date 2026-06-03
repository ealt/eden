# Task Protocol

This chapter specifies the behavioral contract for tasks: the state machine they advance through, the identity-keyed claim ownership that grants exclusive execution, the idempotency rules for submission, and the reclamation policy that bounds worker liveness. It pins *what* observable behavior a conforming task store and a conforming orchestrator must exhibit; it does not pin *how* either is implemented.

The task object's shape is defined in [`02-data-model.md`](02-data-model.md) §3; its JSON Schema is [`schemas/task.schema.json`](schemas/task.schema.json). The role contracts that drive the transitions defined here are in [`03-roles.md`](03-roles.md).

## 1. State machine

### 1.1 States

A task's `state` field takes one of five values. Each has a well-defined meaning in the state machine:

| State | Meaning |
|---|---|
| `pending` | The task has been created and is eligible to be claimed. No worker currently holds it. |
| `claimed` | A worker holds an active claim on the task and is expected to execute it. The task carries a `claim` object. |
| `submitted` | The claiming worker has presented a result. The task awaits processing by the orchestrator. |
| `completed` | The orchestrator has accepted the result. The task is terminal. |
| `failed` | The orchestrator has rejected the result, the worker declared failure, or a policy limit (e.g. retry budget) has been exceeded. The task is terminal. |

`completed` and `failed` are **terminal**: a conforming task store MUST NOT transition a task out of either state.

### 1.2 Transitions

The permitted transitions are exactly:

```text
          claim                 submit                accept
pending ─────────▶ claimed ─────────────▶ submitted ─────────▶ completed
   ▲                 │                       │
   │ reclaim         │ reclaim               │ reject
   └─────────────────┴───────────────────────┤           ──▶ failed
                                             │
```

- **claim** — `pending → claimed`. Atomic. Records the claimant's `worker_id` on the task. §3.
- **submit** — `claimed → submitted`. Requires the authenticated `worker_id` to match the recorded claim. Idempotent under identical payload. Performed for every completed attempt, whether the worker declares success or failure. §4.
- **reclaim** — `claimed → pending` or `submitted → pending`. Clears the recorded claim. §5.
- **accept** — `submitted → completed`. Orchestrator-initiated. §4.3.
- **reject** — `submitted → failed`. Orchestrator-initiated, including when the worker's own submission declared failure. §4.3.

Any transition not listed above MUST be rejected by the task store. A conforming task store MUST treat concurrent transition attempts as serialized: whichever attempt wins the race succeeds, and the others MUST receive a well-defined rejection that lets the caller distinguish "invalid" from "raced".

### 1.3 Event emission

Every state transition MUST be accompanied by a corresponding event appended to the event log, atomically with the state change. The event types (e.g. `task.claimed`, `task.submitted`, `task.failed`) and the transactional invariant are specified in [`05-event-protocol.md`](05-event-protocol.md). This chapter does not enumerate the event shapes; it requires only that they exist and that no state change is observable without its event.

## 2. Creating a task

A task enters the system in the `pending` state. A conforming orchestrator MUST:

- Populate `task_id`, `kind`, `state = "pending"`, `payload`, `created_at`, `updated_at` ([`02-data-model.md`](02-data-model.md) §3.1).
- Ensure the payload satisfies the dispatch preconditions for the task's `kind`:
  - `ideation` — `payload.experiment_id` names an experiment the ideator is authorized to work on.
  - `execution` — `payload.idea_id` names an idea with `state == "ready"` at creation time. Creating the execution task and transitioning the idea from `"ready"` to `"dispatched"` MUST be atomic; an idea's `"dispatched"` state signals that exactly one execution task has been created for it. The created task's top-level `target` field ([`02-data-model.md`](02-data-model.md) §3.5) MUST be populated from the referenced idea's `intended_executor` ([`02-data-model.md`](02-data-model.md) §5.1) when the create operation does not supply an explicit `target` override; an explicit `target` on the create request wins over `idea.intended_executor`.
  - `evaluation` — `payload.variant_id` names a variant with `status == "starting"` and `commit_sha` set at creation time.
- Emit the corresponding `task.created` event ( [`05-event-protocol.md`](05-event-protocol.md)) atomically with the task insert.

A worker MAY rely on these preconditions ([`03-roles.md`](03-roles.md) §1.1). A task created without them is a protocol violation by the orchestrator.

**Terminated-experiment guard.** When the experiment's `state == "terminated"` ([`02-data-model.md`](02-data-model.md) §2.5), every `create_task` call MUST be rejected with `eden://error/illegal-transition` (HTTP 409); the Store MUST NOT insert the task or emit `task.created`. This applies to all three kinds and to both orchestrator-driven and operator-driven calls. The guard is mechanical: a deployment running concurrent orchestrators that race a terminate transition relies on this guard to converge on "no new work after termination" without inter-orchestrator coordination.

## 3. Claim

### 3.1 Atomicity

The `pending → claimed` transition MUST be atomic with respect to competing claim attempts: at most one worker MAY succeed for any given task. A conforming task store MUST provide this guarantee as part of its durability contract ([`08-storage.md`](08-storage.md)).

### 3.2 Claim record

A successful claim issues a `claim` object ([`02-data-model.md`](02-data-model.md) §3.4) containing:

- `worker_id` — the registered worker's id ([`02-data-model.md`](02-data-model.md) §6) on whose behalf the claim was made. The task store records this value as the canonical claim ownership; it is the sole identity the §4 submit transition matches against.
- `claimed_at` — the time the claim was issued.
- `expires_at` — OPTIONAL. If present, the task store MAY reclaim the task when the current time exceeds this value (§5).

The pre-12a-1 opaque per-claim `token` is **removed**: claim ownership is no longer authenticated by token equality. Authentication of the calling actor is a binding-layer concern (§3.3); claim-ownership matching is a Store-layer invariant on the submit transition (§4).

### 3.3 Authentication is a binding-layer concern

The Store Protocol takes `worker_id` as input on `claim` and `submit`; it trusts the binding to have already authenticated the caller as that worker. A conforming binding (HTTP wire, in-process, gRPC, …) MUST verify the presented credential and reject mismatched authenticated-id-vs-Store-call-id BEFORE invoking `Store.claim` or `Store.submit`. The set of credential schemes is binding-defined; for the reference HTTP binding, [`07-wire-protocol.md`](07-wire-protocol.md) §13 defines per-worker bearer + admin-bearer auth. In-process callers (tests and other adapters that bypass the binding) pass `worker_id` directly; the trust boundary is the binding edge.

### 3.4 No re-claim while claimed

A conforming task store MUST reject a claim attempt against a task whose `state` is not `pending`. In particular, a claimed task cannot be "re-claimed" by a different worker; the prior claim must first be released (by submission) or invalidated (by reclamation).

### 3.5 Target eligibility

In addition to the §3.4 state precondition, a `claim(task_id, worker_id)` operation MUST satisfy the task's `Task.target` constraint ([`02-data-model.md`](02-data-model.md) §3.5). The store MUST evaluate the following preconditions in order, atomically with the claim write:

0. The experiment's `state == "running"` ([`02-data-model.md`](02-data-model.md) §2.5). A claim against a `pending` task whose experiment has been terminated MUST be rejected with `IllegalTransition` (wire mapping: 409 `eden://error/illegal-transition`); the pending task remains in storage but is unreachable.
1. The task is in state `pending` (§3.4).
2. `worker_id` names a worker registered for the experiment ([`02-data-model.md`](02-data-model.md) §6). A claim by a non-registered `worker_id` MUST be rejected with `WorkerNotRegistered`.
3. `worker_id` satisfies `Task.target`:
   - if `target` is absent → pass;
   - if `target.kind == "worker"` → pass iff `worker_id == target.id`;
   - if `target.kind == "group"` → pass iff `worker_id` is transitively a member of `target.id` ([`02-data-model.md`](02-data-model.md) §7.2).

   A claim that fails this step MUST be rejected with `WorkerNotEligible`.
4. The atomic claim-write per §3.1.

Already-claimed tasks at the moment of termination MAY still be submitted, accepted, or rejected normally — the terminated-experiment guard applies only to **new** claim attempts. This is what [`02-data-model.md`](02-data-model.md) §2.5's drain semantics depend on: termination stops new work; it does not abandon committed work in flight.

`WorkerNotRegistered` and `WorkerNotEligible` are typed errors at the Store layer ([§13](#13-worker-eligibility-errors)). The binding maps them to HTTP statuses per [`07-wire-protocol.md`](07-wire-protocol.md).

## 4. Submit

### 4.1 Submission operation

A submit operation requires:

- The `task_id`.
- The `worker_id` of the claimant on whose behalf the submit is being made — supplied by the binding from the authenticated identity (§3.3); in-process callers pass it directly.
- A result payload whose shape depends on `kind` ([`03-roles.md`](03-roles.md) §2.4, §3.4, §4.4).

A successful submit transitions `claimed → submitted` atomically with persisting the result payload, with three preconditions checked AS PART OF the same store transaction:

1. The task's current `state` is `claimed`. Otherwise the store raises `NotClaimed`.
2. `task.claim.worker_id == worker_id` (the call-supplied claimant matches the recorded claim). Otherwise the store raises `WrongClaimant`. The atomicity requirement is load-bearing: a non-atomic "read claim, compare, then write" sequence introduces a TOCTOU race where another actor could reclaim between the binding's check and the store's write.
3. The role-specific payload shape and content-equivalence rules in §4.2.

The store MUST atomically also write `task.submitted_by = worker_id` ([`02-data-model.md`](02-data-model.md) §3.1) so that the claimant identity is preserved across the terminal transitions that clear `claim`.

The terminal transition to `completed` or `failed` is the orchestrator's subsequent step (§4.3), even when the submission itself declared failure.

The binding MUST NOT perform a pre-flight read-then-compare against `task.claim.worker_id`; it MUST only authenticate the request and forward the authenticated `worker_id` to `Store.submit`. The Store performs the claim-match atomically.

### 4.2 Idempotency

A resubmission is a second submit, on behalf of the same claimant, against a task still in `submitted` (i.e. `task.claim.worker_id` is unchanged). A resubmit against a task whose claim has been cleared — because the task was reclaimed or reached a terminal state — MUST be rejected regardless of the presented `worker_id` (§4.4, §5.2). A resubmit by a different `worker_id` than the recorded claimant MUST be rejected with `WrongClaimant`.

When the claimant matches, the task store MUST handle the resubmission as follows:

- If the resubmission's result payload is **content-equivalent** to the already-recorded payload, the task store MUST accept it and MUST NOT change the task's state or recorded result. "Content equivalence" means the normative fields identified per role agree:
  - `ideation` — the set of `idea_ids` (compared as sets; order is not significant per [`03-roles.md`](03-roles.md) §2.4) and `status`.
  - `execution` — `variant_id`, `status`, and `commit_sha` (when present).
  - `evaluation` — `variant_id`, `status`, and `metrics` (compared as JSON values; key order does not matter).
- If the resubmission's result payload is **not** content-equivalent, the task store MUST reject it. The first submission's result is the committed result.

This rule exists so that a worker may safely retry a submit after a network or process failure without risk of advancing state twice or corrupting the recorded result. Bindings MAY additionally accept an optional caller-supplied `submission_id` field on the wire payload to act as an explicit idempotency key; that is a binding-layer extension and does not weaken the content-equivalence rule.

The role-side success-contract MUSTs in [`03-roles.md`](03-roles.md) bind the executor's submissions, not the task store's acceptance of them. In particular, the §3.3 non-no-op variant rule is a MUST on the executor's role contract; a conforming executor produces no no-op submissions, so the wire surface only sees them when a non-conforming or malicious client connects to the task store directly. For that wire surface, the IUT SHOULD reject the trivially-detectable case where an `execution`-task submission with `status == "success"` carries a `commit_sha` bytewise equal to **every** entry of the idea's `parent_commits` (the SHA-equality fast path). For single-parent ideas this collapses to `commit_sha == parent_commits[0]`. For multi-parent ideas the fast path rejects only when the submission SHA matches *every* parent — submitting `commit_sha == parent_commits[0]` for a multi-parent idea is NOT necessarily a no-op (the variant may legitimately keep `parent_commits[0]`'s tree while diverging from `parent_commits[1]`'s tree, which still satisfies §3.3's "differs from at least one parent" condition). The IUT MAY perform a deeper tree-identity comparison when the task store has the means to resolve git trees, but the deeper check is NOT REQUIRED: forcing the task store to maintain a git clone (and to fetch from a remote on resolve-miss) would couple the chapter-04 / chapter-07 contract to chapter-06 git semantics that the rest of those chapters do not assume. When a wire-level rejection does surface a problem+json envelope, the `type` MUST be `eden://error/no-op-variant` ([`07-wire-protocol.md`](07-wire-protocol.md) §9). The rule is content-derived and idempotent: a content-equivalent retry of a no-op submission resolves the same way.

This SHA-equality SHOULD-reject applies to `execution`-task submissions. A task store that implements the deeper tree-identity check MUST exempt a `kind == "baseline"` variant ([`02-data-model.md`](02-data-model.md) §9.4): a baseline's `commit_sha` equals its single `parent_commits` entry by construction (the seed framed as its own parent), and it is created directly by the orchestrator via `create_variant`, not via an executor task submission, so it never traverses this submission path. The carve ensures a task store performing the deeper check does not reject a legitimate baseline at create time ([`08-storage.md`](08-storage.md) §1.7).

### 4.3 From `submitted` to terminal

A submitted task is processed by the orchestrator and transitions to a terminal state. The orchestrator MUST:

- Transition `submitted → completed` when the submission's `status` is `"success"` and the result satisfies the role's success contract (e.g. an executor submission carries a reachable `commit_sha`).
- Transition `submitted → failed` when either:
  - The result violates the role's success contract (malformed payload, missing required field, failed post-validation).
  - The worker itself declared failure: - `ideation` task, worker `status == "error"` → `failed`. - `execution` task, worker `status == "error"` → `failed`. - `evaluation` task, worker `status == "error"` → `failed` AND the referenced variant's `status` MUST be updated to `"error"`. - `evaluation` task, worker `status == "evaluation_error"` → `failed`, but the referenced variant's `status` MUST remain `"starting"` (the variant itself was not shown to be bad; only the evaluator-side attempt failed). A fresh `evaluation` task MAY be created for the same variant; the protocol does not mandate automatic retry. If the orchestrator's policy exhausts retries for this variant (or the operator abandons it), the orchestrator MUST transition the variant's `status` from `"starting"` to `"evaluation_error"`, at which point the variant status is terminal.

Every terminal transition produces exactly one state-change event in the log ([`05-event-protocol.md`](05-event-protocol.md)) and MUST clear the task's `claim` object ([`02-data-model.md`](02-data-model.md) §3.4). The orchestrator MAY implement the `claimed → submitted → completed` (or `failed`) sequence as two distinct persisted states or as a single store transaction whose event stream includes both transitions, but subscribers MUST observe both events in order.

The variant status transitions above assume the variant was created `starting` and reaches `success` at an evaluation task's terminal transition. A `kind == "baseline"` variant on the override path (config-supplied metrics, [`02-data-model.md`](02-data-model.md) §2.7, §9.4) is the one exception: it is created directly in `status == "success"` via `create_variant` (the precondition relaxation in [`08-storage.md`](08-storage.md) §1.7), with no evaluation task and therefore no task-terminal transition. Its `variant.started` + `variant.succeeded` events are emitted atomically at create time ([`05-event-protocol.md`](05-event-protocol.md) §3.3). The default-path baseline behaves like any other variant here: created `starting`, transitioning to `success` at its evaluation task's terminal transition.

### 4.4 Post-terminal writes prohibited

Once a task is `completed` or `failed`, no further writes to its task fields are permitted beyond audit-only metadata that a task store chooses to expose outside the normative schema ([`02-data-model.md`](02-data-model.md) §3.1). In particular, a resubmission against a terminal task MUST be rejected regardless of content equivalence; the claim that produced the terminal transition has been cleared and the recorded `submitted_by` is the authoritative claimant identity.

## 5. Reclamation

### 5.1 When reclamation is permitted

A conforming task store MAY reclaim a task — move it from `claimed` back to `pending` — in any of the following cases:

- The `claim.expires_at` timestamp has passed.
- An operator invokes an explicit reclaim action on the task.
- A task-store-defined health policy determines the claiming worker is unreachable.

A task in `submitted` state MAY be reclaimed **only** by an explicit operator action; automatic reclaim (expires_at, health policy) MUST NOT apply to `submitted` tasks. After submit the worker's reachability is no longer a relevant liveness signal; the orchestrator is expected to advance the task to a terminal state (§4.3).

A task store MUST NOT reclaim a task in a terminal state.

### 5.2 Effect of reclamation

Reclamation:

1. Clears the `claim` object from the task. Subsequent submit operations against the task fail `NotClaimed` until a fresh claim is taken.
2. Sets the task's `state` back to `pending`.
3. Is accompanied by a `task.reclaimed` event in the event log ([`05-event-protocol.md`](05-event-protocol.md)), atomically with the state change.

Any partial result a worker had assembled before reclamation is no longer observable through the task protocol. If the worker created store-resident objects (drafting ideas, starting variants), those objects persist per their own lifecycle and are subject to role-level cleanup rules; reclamation itself does not delete them.

### 5.3 Worker detection of reclamation

A worker that wishes to detect its own reclamation MAY observe its task's state or subscribe to events. If a worker discovers its `claim` has been cleared (or replaced by a different `worker_id`'s claim), it MUST discontinue work on the task and MUST NOT submit ([`03-roles.md`](03-roles.md) §1 step 5).

### 5.4 Reclamation effect on role outputs

When a reclaimed task had created role-owned objects during its prior execution, the orchestrator MUST reconcile those objects as follows:

- **Execution-task reclamation.** If the prior execution created a variant with `status == "starting"`, the orchestrator MUST transition that variant's `status` to `"error"` atomically with the reclaim event. A subsequent re-claim of the execution task produces a **new** variant; starting variants are never shared across executor attempts.
- **Ideation-task reclamation.** If the prior execution persisted ideas in `"drafting"` state, those ideas remain in `"drafting"` and are not dispatched. Implementations MAY expose them to operators for inspection or removal; the task protocol does not mandate automatic cleanup.
- **Evaluation-task reclamation.** The variant the task referenced is unchanged by reclamation (it was produced by the executor, not the evaluator).

## 6. Reassignment

Reassignment is the orchestrator-/operator-driven counterpart to §5 reclamation: where reclamation invalidates a claim and returns the task to `pending` without changing its routing intent, reassignment **updates the task's `target`** ([`02-data-model.md`](02-data-model.md) §3.5) while ensuring no in-flight worker can finish a stale claim against the new routing.

### 6.1 The `reassign_task` operation

`reassign_task(task_id, new_target, *, reason)` accepts:

- The `task_id`.
- A new `Task.target` value (`null` for "any worker", a worker target, or a group target — same shape as create-time targeting per [`02-data-model.md`](02-data-model.md) §3.5).
- A free-form `reason` string for audit (typical values: `"operator"`, `"failed_worker"`, `"misrouted"`; not enumerated by the protocol).

The behavior depends on the task's current state:

- **`pending`** — atomic update of `task.target`; the task remains `pending`. Exactly one `task.reassigned` event ([`05-event-protocol.md`](05-event-protocol.md) §3.1) fires with the new target and `reason` recorded.
- **`claimed`** — composite commit equivalent to `reclaim(reason="operator") + target update`. The claim is cleared, the task returns to `pending`, the target is updated. Two events fire atomically: a `task.reclaimed` event with `cause == "operator"` ([`05-event-protocol.md`](05-event-protocol.md) §3.1) AND a `task.reassigned` event with the new target. Subscribers MUST observe both events together with the task's `pending` + new-target state; partial observability is forbidden by [`05-event-protocol.md`](05-event-protocol.md) §2.2.
- **`submitted`, `completed`, `failed`** — rejected with `InvalidPrecondition` (wire mapping: 409 `eden://error/invalid-precondition`). A submitted task has produced an artifact whose attribution the orchestrator is in the middle of finalizing; reassignment would race that finalization. Terminal tasks are immutable per §4.4.

The new target is validated like any other `Task.target`: it MUST satisfy the §3.5 schema (kind / id grammar); `register_worker` / `register_group` referenced by it MAY be absent at reassignment time (resolves to membership=false at the next claim attempt).

### 6.2 Authority

`reassign_task` is restricted to callers in the `admins` group ([`02-data-model.md`](02-data-model.md) §7.5). The wire binding enforces this before invoking the Store; the Store assumes authority has been enforced upstream (§3.3 binding-layer-authentication discipline).

The reassigning caller's identity is recorded in the `task.reassigned` event payload (`reassigned_by`) for audit. The protocol does not require the caller to be in the new target's worker / group; reassignment is an authority operation, not a self-target.

### 6.3 Effect on stale claims

The claimed-task case (`reclaim + target update`) means a worker holding the prior claim observes its claim cleared per §5.3. Any subsequent submit attempt by that worker fails the §4.1 step-1 `NotClaimed` precondition; the worker's partial result is discarded per §5.2. The post-reassignment task is freshly claimable by workers satisfying the new target.

## 7. Dispatch mode

The orchestrator role contract ([`03-roles.md`](03-roles.md) §6) is gated per-decision-type by the experiment's `dispatch_mode` field ([`02-data-model.md`](02-data-model.md) §2.4). This section defines the task-store operation that updates that field.

### 7.1 The `update_dispatch_mode` operation

`update_dispatch_mode(experiment_id, patch)` accepts a partial `dispatch_mode` object (any subset of the four keys defined in [`02-data-model.md`](02-data-model.md) §2.4) and atomically merges it into the experiment's stored `dispatch_mode`. Unspecified keys are unchanged. Each value in the patch MUST be either `"auto"` or `"manual"`; an unrecognized value MUST be rejected (`BadRequest`; wire mapping: 400 `eden://error/bad-request`).

A successful update emits exactly one `experiment.dispatch_mode_changed` event ([`05-event-protocol.md`](05-event-protocol.md) §3.4) whose payload records the **resulting** `dispatch_mode` object plus a `changed` object listing the keys whose values flipped. A no-op patch (every supplied key already matches the stored value) MUST still be accepted — it MAY emit an event whose `changed` is empty, or MAY skip the event entirely (implementation-defined; the §1.3 atomicity requirement applies only to state changes, and a no-op is not a state change).

### 7.2 Authority

`update_dispatch_mode` is restricted to callers in the `admins` group ([`02-data-model.md`](02-data-model.md) §7.5). The wire binding enforces this; the Store assumes authority has been enforced upstream.

### 7.3 Effect on in-flight orchestrator decisions

Flipping a `dispatch_mode.<decision>` key from `"auto"` to `"manual"` does **not** abort decisions already in flight (e.g., a `kind=="execution"` task already created remains valid). Subsequent iterations of any orchestrator instance MUST observe the new value and refrain from running the gated decision per [`03-roles.md`](03-roles.md) §6.1. Flipping back from `"manual"` to `"auto"` resumes orchestrator activity at the next iteration.

The `experiment.dispatch_mode_changed` event lets subscribers (UI, dashboards, audit) observe the change without polling. Multi-instance orchestrator deployments observe the event through their own event-log polling; the protocol does not require push-based reconfiguration.

## 8. Experiment lifecycle ops

The experiment lifecycle state ([`02-data-model.md`](02-data-model.md) §2.5) is mutated by exactly one public task-store operation in v0.

### 8.1 The `terminate_experiment` operation

`terminate_experiment(experiment_id, reason)` atomically transitions the experiment's `state` from `"running"` to `"terminated"` and appends an `experiment.terminated` event ([`05-event-protocol.md`](05-event-protocol.md) §3.4) carrying the supplied `reason: string`. The state update and the event append MUST commit in a single transaction: observers MUST NOT see one without the other.

The operation is idempotent on the terminated state. A `terminate_experiment` call against an experiment whose state is already `"terminated"` MUST succeed without committing a second state transition and MUST NOT append a second `experiment.terminated` event. The winning caller's `reason` is the one recorded; subsequent callers' `reason` strings are discarded.

There is no `terminated → running` transition in v0. The protocol does not define a `resume_experiment` op; reactivation of a terminated experiment is reserved for a future spec lineage.

### 8.2 Authority

`terminate_experiment` requires the caller to be in the `admins` OR `orchestrators` group ([`02-data-model.md`](02-data-model.md) §7.5). The caller MUST be a member of one of those two groups; a caller outside both receives 403 `eden://error/forbidden`. The wire binding for the operation is [`07-wire-protocol.md`](07-wire-protocol.md) §2.9 (`POST /v0/experiments/{E}/terminate`).

The two groups correspond to the two termination paths: an operator drives termination through an `admins` bearer, while the orchestrator commits its policy-driven termination ([`03-roles.md`](03-roles.md) §6.2 decision-type 0) through an `orchestrators` bearer. Gating on `orchestrators` as well as `admins` is what lets the orchestrator run decision-type 0 over the wire binding without being placed in `admins` (which would over-grant it `reassign_task` / `update_dispatch_mode` authority). This mirrors the `accept` / `reject` gating (§4.3) and the `emit_policy_error` gating ([`07-wire-protocol.md`](07-wire-protocol.md) §2.9), both `orchestrators`-group operations for the same [`03-roles.md`](03-roles.md) §6 rationale.

The op's authority is independent of `dispatch_mode.termination` ([`02-data-model.md`](02-data-model.md) §2.4): an operator (`admins`) MAY drive termination even when `dispatch_mode.termination == "auto"`, and the orchestrator's (`orchestrators`) wire call is accepted regardless of the mode. `dispatch_mode.termination` gates only whether the orchestrator's policy-driven path ([`03-roles.md`](03-roles.md) §6.2 decision-type 0) *runs the decision*, not whether the wire op *accepts the call*.

### 8.3 Internal `update_experiment_state` primitive

The Store layer exposes `update_experiment_state(experiment_id, new_state)` as an internal primitive used by both `terminate_experiment` and the policy-driven path ([`03-roles.md`](03-roles.md) §6.2 decision-type 0). The primitive is not a public wire op in v0; its only callers are the higher-level lifecycle ops defined in this section and the orchestrator's policy-driven termination branch. The full storage-layer contract is in [`08-storage.md`](08-storage.md) §1.8.

## 9. Ordering and concurrency

### 9.1 Per-task serialization

All transitions on a single task MUST be serialized: the observable history of a task is a total order. A conforming task store MUST NOT expose a state to readers that has not yet been accompanied by its event.

### 9.2 Cross-task concurrency

Distinct tasks MAY progress concurrently. The protocol imposes no global ordering on transitions across tasks beyond the causal order preserved by the event log ([`02-data-model.md`](02-data-model.md) §4.3, [`05-event-protocol.md`](05-event-protocol.md)).

### 9.3 Observability

A subscriber that reads the event log MUST be able to reconstruct every task's state-machine history exactly. Implementations MAY provide faster paths (direct task reads, push notifications) but MUST NOT let those paths expose states that the event log does not record.

## 10. Idea and variant lifecycle interactions

The task state machine is not the only lifecycle in the system. Ideas ([`02-data-model.md`](02-data-model.md) §5.1) and variants ([`02-data-model.md`](02-data-model.md) §9) have their own state fields. The task protocol interacts with these lifecycles at the following points:

- **Ideation-task submission.** A successful ideation-task submission MAY reference ideas that have been transitioned to `state == "ready"` by the ideator ([`03-roles.md`](03-roles.md) §2.2). The task protocol does not itself transition idea state; it treats the idea as a value produced by the ideator.
- **Execution-task submission.** A successful execution-task submission requires a variant created and advanced by the executor ([`03-roles.md`](03-roles.md) §3.2). On any terminal transition of the execution task (whether `submitted → completed` or `submitted → failed`), the orchestrator MUST transition the referenced idea's `state` from `"dispatched"` to `"completed"`, atomically with the task's terminal event. The idea's `"completed"` state therefore means *"the executor's attempt on this idea has finished"*, not *"the attempt succeeded"*; the variant's `status` field records the outcome. An idea in `"dispatched"` always names a non-terminal execution task; no idea remains in `"dispatched"` after its execution task has reached a terminal state.
- **Evaluation-task submission.** A successful evaluation-task submission populates the variant's `evaluation`, `completed_at`, and (per worker-declared status) the variant's `status`. The integrator's subsequent integration of the variant into the canonical lineage is out of this chapter's scope ([`06-integrator.md`](06-integrator.md)).

## 11. Implementation latitude

The protocol leaves to implementations:

- The mechanism by which a worker discovers claimable tasks (polling, subscription, dispatch).
- The representation of worker IDs and task IDs (within the §6.1 grammar of [`02-data-model.md`](02-data-model.md)).
- The timeout and reclamation policy, including whether to set `expires_at` by default.
- Whether retries are automatic (new task on reclamation) or operator-driven.
- Whether `submitted → completed` is fully synchronous with submit or a subsequent orchestrator step, as long as the observable state machine above is preserved.
- The credential scheme used by the binding to authenticate the calling actor (§3.3); the §3 / §4 contracts only require that the binding produce a verified `worker_id` for the Store call.
- The ideation-creation policy callable (§6 of [`03-roles.md`](03-roles.md)) and the mechanism for invoking it.
- The termination policy callable ([`03-roles.md`](03-roles.md) §6.2 decision-type 0) and the mechanism for invoking it.

What the protocol does **not** leave to implementations:

- The set of states and the permitted transitions among them (§1).
- The atomicity of claim (§3.1) and the §3.5 target-eligibility check.
- The atomic claim-match on submit (§4.1 step 2) and its error vocabulary (§13).
- The idempotency rule for resubmission (§4.2).
- The orchestrator-role idempotency classes for the five decision types ([`03-roles.md`](03-roles.md) §6.4).
- The atomicity of `reassign_task` on claimed tasks (§6.1) and the authority requirement (§6.2).
- The atomicity of `terminate_experiment` (§8.1) and the idempotent no-op on already-terminated experiments.
- The requirement that every state change be accompanied by an atomic event (§1.3) and that the event log is the normative observability channel (§9.3).
- The terminated-experiment guard on `create_task` (§2) and `claim` (§3.5).

## 12. Worker registry preconditions

Before a worker can claim or submit, it MUST be registered in the experiment's worker registry ([`02-data-model.md`](02-data-model.md) §6). `Store.claim` enforces this at §3.5 step 2; `Store.submit` likewise rejects calls whose `worker_id` is not registered (the binding's authentication step typically catches this earlier, but the Store MUST defend against in-process callers that bypass the binding).

## 13. Worker eligibility errors

The Store-layer typed errors raised on §3 / §4 enforcement form a closed vocabulary. Wire bindings map them to transport-specific status codes ([`07-wire-protocol.md`](07-wire-protocol.md) §7).

| Error | Raised by | Meaning |
|---|---|---|
| `WorkerNotRegistered` | `claim`, `submit` | The supplied `worker_id` is not registered in the experiment's registry. |
| `WorkerNotEligible` | `claim` | The worker is registered but does not satisfy `Task.target` per §3.5 step 3. |
| `NotClaimed` | `submit` | The task is not in `claimed` state — it is `pending` (no claim), `submitted` with a different submission already in flight whose claim has been cleared, or terminal. |
| `WrongClaimant` | `submit` | The supplied `worker_id` does not match `task.claim.worker_id`. The atomic match is performed as part of the submit transition; pre-flight binding-side checks would race. |
| `ReservedIdentifier` | `register_worker`, `register_group` | The supplied id is one of the reserved values (`admin`, `system`, `internal`) per [`02-data-model.md`](02-data-model.md) §6.1's reservation list. Distinct from grammar-rejection: a syntactically invalid id surfaces as `InvalidPrecondition` (§6.1 grammar; chapter 07 §6.1 maps that to 400 `eden://error/bad-request`). |
| `WorkerAlreadyRegistered` | `register_worker` | A different actor attempted to register a `worker_id` whose record already exists. (Idempotent re-registration by the same caller is a non-error per [`02-data-model.md`](02-data-model.md) §6.3.) |
| `CycleDetected` | `register_group`, group-mutation ops | The mutation would introduce a cycle in the group DAG ([`02-data-model.md`](02-data-model.md) §7.3). |
| `InvalidPrecondition` | `reassign_task` (§6), `update_dispatch_mode` (§7) | Reassignment of a `submitted` or terminal task is rejected; an unrecognized `dispatch_mode` value is rejected. |
