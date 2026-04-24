# Task Protocol

This chapter specifies the behavioral contract for tasks: the state machine they advance through, the claim tokens that grant exclusive execution, the idempotency rules for submission, and the reclamation policy that bounds worker liveness. It pins *what* observable behavior a conforming task store and a conforming orchestrator must exhibit; it does not pin *how* either is implemented.

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

- **claim** — `pending → claimed`. Atomic. Issues a fresh claim token. §3.
- **submit** — `claimed → submitted`. Requires presenting the current claim token. Idempotent under identical payload. Performed for every completed attempt, whether the worker declares success or failure. §4.
- **reclaim** — `claimed → pending` or `submitted → pending`. Invalidates the prior claim token. §5.
- **accept** — `submitted → completed`. Orchestrator-initiated. §4.3.
- **reject** — `submitted → failed`. Orchestrator-initiated, including when the worker's own submission declared failure. §4.3.

Any transition not listed above MUST be rejected by the task store. A conforming task store MUST treat concurrent transition attempts as serialized: whichever attempt wins the race succeeds, and the others MUST receive a well-defined rejection that lets the caller distinguish "invalid" from "raced".

### 1.3 Event emission

Every state transition MUST be accompanied by a corresponding event appended to the event log, atomically with the state change. The event types (e.g. `task.claimed`, `task.submitted`, `task.failed`) and the transactional invariant are specified in [`05-event-protocol.md`](05-event-protocol.md). This chapter does not enumerate the event shapes; it requires only that they exist and that no state change is observable without its event.

## 2. Creating a task

A task enters the system in the `pending` state. A conforming orchestrator MUST:

- Populate `task_id`, `kind`, `state = "pending"`, `payload`, `created_at`, `updated_at` ([`02-data-model.md`](02-data-model.md) §3.1).
- Ensure the payload satisfies the dispatch preconditions for the task's `kind`:
  - `plan` — `payload.experiment_id` names an experiment the planner is authorized to work on.
  - `implement` — `payload.proposal_id` names a proposal with `state == "ready"` at creation time. Creating the implement task and transitioning the proposal from `"ready"` to `"dispatched"` MUST be atomic; a proposal's `"dispatched"` state signals that exactly one implement task has been created for it.
  - `evaluate` — `payload.trial_id` names a trial with `status == "starting"` and `commit_sha` set at creation time.
- Emit the corresponding `task.created` event ( [`05-event-protocol.md`](05-event-protocol.md)) atomically with the task insert.

A worker MAY rely on these preconditions ([`03-roles.md`](03-roles.md) §1.1). A task created without them is a protocol violation by the orchestrator.

## 3. Claim

### 3.1 Atomicity

The `pending → claimed` transition MUST be atomic with respect to competing claim attempts: at most one worker MAY succeed for any given task. A conforming task store MUST provide this guarantee as part of its durability contract ([`08-storage.md`](08-storage.md)).

### 3.2 Claim token

A successful claim issues a `claim` object ([`02-data-model.md`](02-data-model.md) §3.4) containing:

- `token` — an opaque value that MUST be:
  - **unique** — no two distinct claims (on the same or different tasks) share the same token within an experiment;
  - **unforgeable** — a worker that did not receive the token MUST NOT be able to construct a byte-equal value with practical effort. Implementations commonly use a cryptographically random value; the protocol does not mandate the mechanism.
- `worker_id` — an identifier the worker supplies at claim time. The task store MAY record it for audit but MUST NOT use it as a substitute for the token when authorizing subsequent operations.
- `claimed_at` — the time the claim was issued.
- `expires_at` — OPTIONAL. If present, the task store MAY reclaim the task when the current time exceeds this value (§5).

### 3.3 Authorization by token

Every subsequent operation on a claimed task (progress update, submit, release) MUST present the current claim token. The task store MUST reject any such operation whose token does not match the token currently recorded on the task. This is the sole mechanism the protocol relies on to guarantee single-writer access to a claimed task.

### 3.4 No re-claim while claimed

A conforming task store MUST reject a claim attempt against a task whose `state` is not `pending`. In particular, a claimed task cannot be "re-claimed" by a different worker; the prior claim must first be released (by submission) or invalidated (by reclamation).

## 4. Submit

### 4.1 Submission operation

A submit operation requires:

- The `task_id`.
- The current claim `token`.
- A result payload whose shape depends on `kind` ([`03-roles.md`](03-roles.md) §2.4, §3.4, §4.4).

A successful submit transitions `claimed → submitted` atomically with persisting the result payload. The terminal transition to `completed` or `failed` is the orchestrator's subsequent step (§4.3), even when the submission itself declared failure.

### 4.2 Idempotency

A resubmission is a second submit carrying the token recorded on the task's `claim` object (which is retained through `submitted` per [`02-data-model.md`](02-data-model.md) §3.4). A resubmit against a task whose claim has been cleared — because the task was reclaimed or reached a terminal state — MUST be rejected regardless of the presented token (§4.4, §5.2).

When the token matches, the task store MUST handle the resubmission as follows:

- If the resubmission's result payload is **content-equivalent** to the already-recorded payload, the task store MUST accept it and MUST NOT change the task's state or recorded result. "Content equivalence" means the normative fields identified per role agree:
  - `plan` — the set of `proposal_ids` (compared as sets; order is not significant per [`03-roles.md`](03-roles.md) §2.4) and `status`.
  - `implement` — `trial_id`, `status`, and `commit_sha` (when present).
  - `evaluate` — `trial_id`, `status`, and `metrics` (compared as JSON values; key order does not matter).
- If the resubmission's result payload is **not** content-equivalent, the task store MUST reject it. The first submission's result is the committed result.

This rule exists so that a worker may safely retry a submit after a network or process failure without risk of advancing state twice or corrupting the recorded result.

### 4.3 From `submitted` to terminal

A submitted task is processed by the orchestrator and transitions to a terminal state. The orchestrator MUST:

- Transition `submitted → completed` when the submission's `status` is `"success"` and the result satisfies the role's success contract (e.g. an implementer submission carries a reachable `commit_sha`).
- Transition `submitted → failed` when either:
  - The result violates the role's success contract (malformed payload, missing required field, failed post-validation).
  - The worker itself declared failure: - `plan` task, worker `status == "error"` → `failed`. - `implement` task, worker `status == "error"` → `failed`. - `evaluate` task, worker `status == "error"` → `failed` AND the referenced trial's `status` MUST be updated to `"error"`. - `evaluate` task, worker `status == "eval_error"` → `failed`, but the referenced trial's `status` MUST remain `"starting"` (the trial itself was not shown to be bad; only the evaluator-side attempt failed). A fresh `evaluate` task MAY be created for the same trial; the protocol does not mandate automatic retry. If the orchestrator's policy exhausts retries for this trial (or the operator abandons it), the orchestrator MUST transition the trial's `status` from `"starting"` to `"eval_error"`, at which point the trial status is terminal.

Every terminal transition produces exactly one state-change event in the log ([`05-event-protocol.md`](05-event-protocol.md)) and MUST clear the task's `claim` object ([`02-data-model.md`](02-data-model.md) §3.4). The orchestrator MAY implement the `claimed → submitted → completed` (or `failed`) sequence as two distinct persisted states or as a single store transaction whose event stream includes both transitions, but subscribers MUST observe both events in order.

### 4.4 Post-terminal writes prohibited

Once a task is `completed` or `failed`, no further writes to its task fields are permitted beyond audit-only metadata that a task store chooses to expose outside the normative schema ([`02-data-model.md`](02-data-model.md) §3.1). In particular, a resubmission against a terminal task MUST be rejected regardless of content equivalence; the claim token that produced the terminal transition is no longer valid.

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

1. Invalidates the prior claim token. Subsequent operations presenting it MUST be rejected.
2. Clears the `claim` object from the task.
3. Sets the task's `state` back to `pending`.
4. Is accompanied by a `task.reclaimed` event in the event log ([`05-event-protocol.md`](05-event-protocol.md)), atomically with the state change.

Any partial result a worker had assembled before reclamation is no longer observable through the task protocol. If the worker created store-resident objects (drafting proposals, starting trials), those objects persist per their own lifecycle and are subject to role-level cleanup rules; reclamation itself does not delete them.

### 5.3 Worker detection of reclamation

A worker that wishes to detect its own reclamation MAY observe its task's state or subscribe to events. If a worker discovers its token has been invalidated, it MUST discontinue work on the task and MUST NOT submit ([`03-roles.md`](03-roles.md) §1 step 5).

### 5.4 Reclamation effect on role outputs

When a reclaimed task had created role-owned objects during its prior execution, the orchestrator MUST reconcile those objects as follows:

- **Implement reclamation.** If the prior execution created a trial with `status == "starting"`, the orchestrator MUST transition that trial's `status` to `"error"` atomically with the reclaim event. A subsequent re-claim of the implement task produces a **new** trial; starting trials are never shared across implementer attempts.
- **Plan reclamation.** If the prior execution persisted proposals in `"drafting"` state, those proposals remain in `"drafting"` and are not dispatched. Implementations MAY expose them to operators for inspection or removal; the task protocol does not mandate automatic cleanup.
- **Evaluate reclamation.** The trial the task referenced is unchanged by reclamation (it was produced by the implementer, not the evaluator).

## 6. Ordering and concurrency

### 6.1 Per-task serialization

All transitions on a single task MUST be serialized: the observable history of a task is a total order. A conforming task store MUST NOT expose a state to readers that has not yet been accompanied by its event.

### 6.2 Cross-task concurrency

Distinct tasks MAY progress concurrently. The protocol imposes no global ordering on transitions across tasks beyond the causal order preserved by the event log ([`02-data-model.md`](02-data-model.md) §4.3, [`05-event-protocol.md`](05-event-protocol.md)).

### 6.3 Observability

A subscriber that reads the event log MUST be able to reconstruct every task's state-machine history exactly. Implementations MAY provide faster paths (direct task reads, push notifications) but MUST NOT let those paths expose states that the event log does not record.

## 7. Proposal and trial lifecycle interactions

The task state machine is not the only lifecycle in the system. Proposals ([`02-data-model.md`](02-data-model.md) §5.1) and trials ([`02-data-model.md`](02-data-model.md) §7) have their own state fields. The task protocol interacts with these lifecycles at the following points:

- **Plan submit.** A successful plan-task submission MAY reference proposals that have been transitioned to `state == "ready"` by the planner ([`03-roles.md`](03-roles.md) §2.2). The task protocol does not itself transition proposal state; it treats the proposal as a value produced by the planner.
- **Implement submit.** A successful implement-task submission requires a trial created and advanced by the implementer ([`03-roles.md`](03-roles.md) §3.2). On any terminal transition of the implement task (whether `submitted → completed` or `submitted → failed`), the orchestrator MUST transition the referenced proposal's `state` from `"dispatched"` to `"completed"`, atomically with the task's terminal event. The proposal's `"completed"` state therefore means *"the implementer's attempt on this proposal has finished"*, not *"the attempt succeeded"*; the trial's `status` field records the outcome. A proposal in `"dispatched"` always names a non-terminal implement task; no proposal remains in `"dispatched"` after its implement task has reached a terminal state.
- **Evaluate submit.** A successful evaluate-task submission populates the trial's `metrics`, `completed_at`, and (per worker-declared status) the trial's `status`. The integrator's subsequent promotion of the trial into the canonical lineage is out of this chapter's scope ([`06-integrator.md`](06-integrator.md)).

## 8. Implementation latitude

The protocol leaves to implementations:

- The mechanism by which a worker discovers claimable tasks (polling, subscription, dispatch).
- The representation of claim tokens, worker IDs, and task IDs.
- The timeout and reclamation policy, including whether to set `expires_at` by default.
- Whether retries are automatic (new task on reclamation) or operator-driven.
- Whether `submitted → completed` is fully synchronous with submit or a subsequent orchestrator step, as long as the observable state machine above is preserved.

What the protocol does **not** leave to implementations:

- The set of states and the permitted transitions among them (§1).
- The atomicity of claim (§3.1) and the token-authorization rule (§3.3).
- The idempotency rule for resubmission (§4.2).
- The requirement that every state change be accompanied by an atomic event (§1.3) and that the event log is the normative observability channel (§6.3).
