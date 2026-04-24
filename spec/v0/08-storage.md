# Storage

This chapter specifies the three durable stores an EDEN deployment depends on — the **task store**, the **event log**, and the **artifact store** — as protocol-level contracts. It pins the operations each store MUST expose, the consistency and durability properties those operations MUST honor, and the invariants spanning stores that a conforming deployment MUST preserve.

The stores are introduced in [`01-concepts.md`](01-concepts.md) §10. The entities they hold are defined in [`02-data-model.md`](02-data-model.md); the behavioral contracts consuming them are in [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), and [`06-integrator.md`](06-integrator.md). This chapter specifies the storage-side contract those behaviors rely on.

A store is specified by **what observable guarantees it provides**, not by any particular mechanism. A conforming task store MAY be backed by a relational database, an embedded key-value engine, or an in-memory structure; the same is true of the event log and artifact store. Conformance is behavioral.

## 1. Task store

The task store holds the set of tasks and their current state for an experiment. It MUST support the operations below, each with the listed atomicity and failure semantics. Wire-level transport is a binding concern and is not pinned here.

### 1.1 Operations

| Operation | Arguments | Effect |
|---|---|---|
| `create_task` | fully-formed `task` object with `state == "pending"` | Atomically inserts the task and appends `task.created` to the event log ([§6](#6-transactional-guarantees)). |
| `claim` | `task_id`, `worker_id`, OPTIONAL `expires_at` | Atomically transitions a `pending` task to `claimed`, issues a fresh claim token, and appends `task.claimed`. |
| `submit` | `task_id`, `token`, role-specific result | Atomically transitions a `claimed` task to `submitted`, persists the result, and appends `task.submitted`. Idempotent per [`04-task-protocol.md`](04-task-protocol.md) §4.2. |
| `accept` | `task_id`, role-specific acceptance payload | Atomically transitions `submitted → completed`, applies the role-specific effect (e.g. proposal completion, trial status write), clears `claim`, and appends `task.completed` plus any composite events. |
| `reject` | `task_id`, `reason`, role-specific failure payload | Atomically transitions `submitted → failed`, applies the role-specific effect, clears `claim`, and appends `task.failed` plus any composite events. |
| `reclaim` | `task_id`, OPTIONAL `cause` | Atomically transitions `claimed`/`submitted` back to `pending` (where permitted; [`04-task-protocol.md`](04-task-protocol.md) §5.1), invalidates the token, clears `claim`, performs §2.2 composite effects where applicable, and appends `task.reclaimed`. |
| `read_task` | `task_id` | Returns the current task object or a well-defined not-found signal. |
| `list_tasks` | filter (`kind`, `state`, `experiment_id`, …) | Returns matching tasks. Ordering is implementation-defined. |

"Atomically" means: the state change, the result-payload write, any cross-entity effect listed in [`05-event-protocol.md`](05-event-protocol.md) §2.2, and the event appends are all durable together or none of them is ([§6](#6-transactional-guarantees)).

### 1.2 Atomic claim (serialization)

For any given `pending` task, at most one concurrent `claim` invocation MUST succeed. A conforming task store MUST expose this as a linearizable operation: after a successful claim, every subsequent reader of the task observes `state == "claimed"` with the new claim object; no reader observes an intermediate state without the claim.

The serialization mechanism (row lock + update, compare-and-set on version, `SELECT … FOR UPDATE SKIP LOCKED`) is implementation-defined. The observable guarantee is not.

### 1.3 Token-authorization rule

`submit` and any other claim-holder operation MUST reject calls whose presented token does not equal the token currently recorded on the task's `claim` object ([`04-task-protocol.md`](04-task-protocol.md) §3.3). A conforming task store MUST NOT use `worker_id` as a substitute for the token when authorizing.

### 1.4 Idempotent resubmit

The idempotency rule for `submit` is specified normatively in [`04-task-protocol.md`](04-task-protocol.md) §4.2. A conforming task store MUST implement it: a resubmit carrying the current token, with a content-equivalent payload, MUST succeed without mutating state; an inconsistent resubmit MUST be rejected.

### 1.5 Terminal immutability

Once a task is `completed` or `failed` ([`04-task-protocol.md`](04-task-protocol.md) §4.4), no subsequent operation MAY change any task field except audit-only metadata the task store documents outside the normative schema. In particular, `reclaim` MUST be rejected on terminal tasks.

### 1.6 Per-task serialization

All mutating operations on a single task MUST be serialized: the observable history of a task is a total order ([`04-task-protocol.md`](04-task-protocol.md) §6.1). A conforming task store MUST NOT expose a state that has not yet been accompanied by its event ([§6](#6-transactional-guarantees)).

### 1.7 Proposal and trial persistence

Proposals ([`02-data-model.md`](02-data-model.md) §5) and trials (§7) are persisted alongside tasks. A conforming deployment MAY back them with the task store itself, with a separate store, or with any combination; the protocol constrains the observable contract, not the physical layout. Every conforming deployment MUST expose, for both proposals and trials, at minimum:

- **Create** — durably insert a new object whose fields validate against the corresponding schema ([`schemas/proposal.schema.json`](schemas/proposal.schema.json), [`schemas/trial.schema.json`](schemas/trial.schema.json)).
- **Read** — return the current object or a well-defined not-found signal.
- **List** — return objects matching a filter (by `experiment_id`, `state`/`status`, etc.). Ordering is implementation-defined.
- **Update** — apply the field writes the role contracts and task- terminal transitions specify ([`03-roles.md`](03-roles.md) §2.2, §3.2, §4.4; [`04-task-protocol.md`](04-task-protocol.md) §4.3, §5.4).

All proposal and trial writes MUST commit atomically with the accompanying event(s) per §6. The durability (§3.1), read-after-write (§3.2), crash-recovery (§3.3), and no-fabrication (§3.4) rules apply uniformly. Terminal immutability applies to proposals in `completed` and trials in `success`/`error`/`eval_error` — the `trial_commit_sha` field is the **one** post-terminal write permitted on a trial, written exclusively by the integrator ([`06-integrator.md`](06-integrator.md) §3.4), and it MUST be written atomically with its event and its `trial/*` ref.

## 2. Event log

The event log holds the per-experiment event stream. It MUST support the operations below and MUST honor the delivery guarantees specified in [`05-event-protocol.md`](05-event-protocol.md) §4.

### 2.1 Operations

| Operation | Arguments | Effect |
|---|---|---|
| `append` | fully-formed event object | Atomically appends the event, together with the state change it describes ([§6](#6-transactional-guarantees)). |
| `read_range` | experiment_id, OPTIONAL cursor | Returns a durable, ordered range of events. |
| `subscribe` | experiment_id, OPTIONAL starting cursor | Returns a stream that delivers events in log order until cancelled. |
| `replay` | experiment_id | Returns the full stream from the first event ([`05-event-protocol.md`](05-event-protocol.md) §4.4). |

`append` MUST NOT be exposed as a standalone operation that the task store, proposal store, or trial store can call independently of the state change the event describes. The atomicity contract ([§6](#6-transactional-guarantees)) requires the append to travel with the state change; the event log operation is a primitive of the composite transaction, not an independent one.

### 2.2 Ordering and causality

The log MUST present a single total order per experiment ([`05-event-protocol.md`](05-event-protocol.md) §4.1) that respects causality ([`05-event-protocol.md`](05-event-protocol.md) §4.2). The ordering mechanism is implementation-defined; the observable guarantee is not.

The log MAY provide a global ordering across experiments as an optimization, but MUST NOT require subscribers to depend on it ([`05-event-protocol.md`](05-event-protocol.md) §4.6).

### 2.3 Delivery

At-least-once delivery is the minimum contract ([`05-event-protocol.md`](05-event-protocol.md) §4.3). Implementations MAY offer exactly-once via durable offset commits.

### 2.4 Retention

For the lifetime of an experiment — from registration until the experiment reaches its operator-declared terminal state — a conforming event log MUST retain **every** appended event for that experiment and MUST serve full replay from the first event ([`05-event-protocol.md`](05-event-protocol.md) §4.4). No compaction, archival, or deletion MAY occur inside this window. A deployment MAY compact or archive an experiment's events once it has reached its operator-declared terminal state; after compaction the log MUST either (a) reject replays older than the compaction horizon with a well-defined error, or (b) serve those replays from an archive that preserves order and content. It MUST NOT silently drop events from a replay.

## 3. Durability

The following applies uniformly to the task store and the event log.

### 3.1 Write durability

An operation that the store acknowledges to its caller MUST survive a subsequent crash of the store's host. "Acknowledgement" is the return value indicating success; a store MUST NOT return success for a write that has not yet been persisted to durable media (or the equivalent quorum in a replicated implementation).

### 3.2 Read-after-write consistency

After a successful write, every subsequent read from any reader MUST observe the effect of the write. A conforming store MUST NOT expose a stale view of data after acknowledging the write that would update it. (Subscribers receive events via the log's delivery mechanism, not by polling the task store, so the read-after-write rule applies to direct reads; event delivery is governed by §4 of the event protocol.)

### 3.3 Crash recovery

A store that crashes mid-operation MUST recover to a state in which every acknowledged write is visible and no partially-applied operation is visible. Implementations that use multi-step transactions MUST ensure rollback on failure so that partial state never becomes observable.

### 3.4 No fabrication

A store MUST NOT materialize an event, a task, a proposal, or a trial from nothing. Every persisted object MUST originate from a caller- driven operation. In particular, a conforming event log MUST NOT synthesize events the caller did not `append`.

## 4. Per-experiment metrics schemas

Every experiment declares a metrics schema in its `experiment_config` ([`02-data-model.md`](02-data-model.md) §1.3, [`schemas/metrics-schema.schema.json`](schemas/metrics-schema.schema.json)). The task store (or an equivalently-scoped component in a conforming deployment) MUST enforce the following at every write that touches trial metrics.

### 4.1 Registration

At experiment registration time, a conforming deployment MUST persist the experiment's metrics schema durably and atomically with the experiment's other configuration. A subsequent write of a trial's `metrics` field MUST be validated against the schema registered for that experiment; no write MAY bypass this validation.

### 4.2 Immutability during an experiment

A metrics schema MUST NOT be mutated for the lifetime of the experiment. An operator who wishes to change the metrics set MUST register a new experiment (Phase 12 defines the control-plane operations that govern this, but the invariant applies from v0 onward). A conforming deployment MUST reject any in-flight mutation of an existing experiment's metrics schema.

The rationale is canonicality: comparing trials across an experiment only has meaning if the metric definitions they are compared on did not move during the experiment.

### 4.3 Per-metric type checks

A successful write of a `trial.metrics` payload MUST satisfy:

- Every key in `metrics` is present in the experiment's metrics schema.
- Every value either satisfies the declared type of its key — per the type mapping in [`02-data-model.md`](02-data-model.md) §1.3 (`integer`, `real`, `text`) — or is `null`.
- No reserved name ([`02-data-model.md`](02-data-model.md) §6.2) appears as a key.

A write that violates any of these MUST be rejected; the store MUST NOT record a trial as `success` with an invalid metrics payload ([`02-data-model.md`](02-data-model.md) §7.2).

## 5. Artifact store

The artifact store holds files the roles produce (plan text, code diffs, evaluator outputs, logs) and exposes them by URI. It is less load-bearing than the task store and event log — the protocol uses it only for content referenced by `artifacts_uri` on proposals and trials — and its contract is accordingly narrower.

### 5.1 Operations

A conforming artifact store MUST support:

- **Upload** — given content bytes and a proposed identity (naming scheme is implementation-defined), persist the bytes and return a URI that will later resolve to them.
- **Fetch** — given a URI the store issued, return the byte content the upload persisted.

URIs MUST be RFC 3986–conformant ([`schemas/proposal.schema.json`](schemas/proposal.schema.json) enforces this on the proposal side).

### 5.2 Durability

Once the artifact store returns a URI to an uploader, the content at that URI MUST remain resolvable until the experiment's retention window elapses. A conforming deployment MUST define its retention window explicitly. A URI recorded on a `trial/*` commit's eval manifest — whether as the optional `artifacts_uri` field ([`06-integrator.md`](06-integrator.md) §4.2) or inside the optional per-file `artifacts` inventory ([`06-integrator.md`](06-integrator.md) §4.4) — MUST be resolvable for at least as long as that `trial/*` commit is retained.

### 5.3 Content integrity

The artifact store MUST NOT serve byte content that differs from what was originally uploaded for a given URI. A store that versions URIs MUST ensure a fetch against a given URI returns the upload that produced it, not a subsequent overwrite.

When the optional per-file `artifacts` inventory ([`06-integrator.md`](06-integrator.md) §4.4) is present on a `trial/*` commit, the manifest's recorded `sha256` and `bytes` fields are the canonical attestation of that file's byte content at integration time; the artifact store is not required to compute or verify them at fetch time, but its no-overwrite rule is what makes the attestation load-bearing.

### 5.4 No protocol-owned mutation after the fact

Once a protocol-owned object (proposal, trial, eval manifest) references an artifact URI, a conforming deployment MUST NOT overwrite the content at that URI. Overwriting would break reproducibility guarantees that subscribers reading the event log rely on.

## 6. Transactional guarantees

The transactional invariant established in [`05-event-protocol.md`](05-event-protocol.md) §2 is the central storage contract:

> Every state change observable via tasks, proposals, or trials MUST > be accompanied by a corresponding event, and the event write MUST > be atomic with the state change it describes.

A conforming deployment MUST satisfy this invariant for every write that mutates protocol-owned state — the task operations in §1.1, the proposal and trial operations in §1.7, and every composite commit in [`05-event-protocol.md`](05-event-protocol.md) §2.2. The mechanism is implementation-defined. The observable guarantee — that no reader observes a state change without its event and vice versa — is not.

### 6.1 Composite commits

For the multi-entity transitions enumerated in [`05-event-protocol.md`](05-event-protocol.md) §2.2, every component of the commit (multiple state changes + multiple event appends) MUST land together or not at all. A subscriber that observes any one event of a composite commit MUST be able to observe all of them.

### 6.2 Failure semantics

If any component of a composite commit fails, the deployment MUST roll back all other components. Operator-facing error reporting is implementation-defined; the observable requirement is that no partial commit reaches any reader.

### 6.3 Recovery after partial commit

If a crash occurs mid-commit, recovery (§3.3) MUST complete or roll back the transaction such that the composite-commit invariant (§6.1) holds for every reader after recovery. A conforming deployment MAY use journal replay, two-phase commit resolution, or equivalent mechanisms.

## 7. Implementation latitude

The protocol leaves to implementations:

- The storage technology for each store (RDBMS, KV store, embedded engine, distributed log, …).
- The wire transport for store access (local library call, HTTP, gRPC, direct SQL).
- Whether the three stores are backed by three separate systems, one system serving all three, or any combination (e.g. a single RDBMS whose outbox table is the event log).
- The retention policies for events, work branches, and artifacts, provided the minimum retention floors in §2.4, §5.2, and [`06-integrator.md`](06-integrator.md) §1.3 are met.
- The concrete representation of claim tokens, event IDs, and URIs.

What the protocol does **not** leave to implementations:

- The set of operations each store MUST expose (§1.1, §2.1, §5.1).
- The atomicity contract of each operation (§1, §6).
- The read-after-write and crash-recovery guarantees (§3).
- The metrics-schema enforcement rules (§4).
- The content-immutability rule on artifacts referenced by protocol-owned objects (§5.4).
