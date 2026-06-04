# Storage

This chapter specifies the three durable stores an EDEN deployment depends on — the **task store**, the **event log**, and the **artifact store** — as protocol-level contracts. It pins the operations each store MUST expose, the consistency and durability properties those operations MUST honor, and the invariants spanning stores that a conforming deployment MUST preserve.

The stores are introduced in [`01-concepts.md`](01-concepts.md) §10. The entities they hold are defined in [`02-data-model.md`](02-data-model.md); the behavioral contracts consuming them are in [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), and [`06-integrator.md`](06-integrator.md). This chapter specifies the storage-side contract those behaviors rely on.

A store is specified by **what observable guarantees it provides**, not by any particular mechanism. A conforming task store MAY be backed by a relational database, an embedded key-value engine, or an in-memory structure; the same is true of the event log and artifact store. Conformance is behavioral.

## 1. Task store

The task store holds the set of tasks and their current state for an experiment. It MUST support the operations below, each with the listed atomicity and failure semantics. Wire-level transport is a binding concern and is not pinned here.

### 1.1 Operations

| Operation | Arguments | Effect |
|---|---|---|
| `create_task` | fully-formed `task` object with `state == "pending"` | Atomically inserts the task and appends `task.created` to the event log ([§6](#6-transactional-guarantees)). The optional `target` field is set here and not mutated again in v0. |
| `claim` | `task_id`, `worker_id`, OPTIONAL `expires_at` | Atomically transitions a `pending` task to `claimed`, records `claim.worker_id`, and appends `task.claimed`. Enforces [`04-task-protocol.md`](04-task-protocol.md) §3.5 (`WorkerNotRegistered` / `WorkerNotEligible`) atomically. |
| `submit` | `task_id`, `worker_id`, role-specific result | Atomically transitions a `claimed` task to `submitted`, persists the result, writes `task.submitted_by = worker_id`, and appends `task.submitted`. Atomically rejects mismatches with `WrongClaimant` / `NotClaimed` per [`04-task-protocol.md`](04-task-protocol.md) §4.1. Idempotent per [`04-task-protocol.md`](04-task-protocol.md) §4.2. |
| `accept` | `task_id`, role-specific acceptance payload | Atomically transitions `submitted → completed`, applies the role-specific effect (e.g. idea completion, variant status write), clears `claim` (preserving `submitted_by`), and appends `task.completed` plus any composite events. |
| `reject` | `task_id`, `reason`, role-specific failure payload | Atomically transitions `submitted → failed`, applies the role-specific effect, clears `claim` (preserving `submitted_by`), and appends `task.failed` plus any composite events. |
| `reclaim` | `task_id`, OPTIONAL `cause` | Atomically transitions `claimed`/`submitted` back to `pending` (where permitted; [`04-task-protocol.md`](04-task-protocol.md) §5.1), clears `claim`, performs §2.2 composite effects where applicable, and appends `task.reclaimed`. |
| `read_task` | `task_id` | Returns the current task object or a well-defined not-found signal. |
| `list_tasks` | filter (`kind`, `state`, `experiment_id`, …) | Returns matching tasks. Ordering is implementation-defined. |

"Atomically" means: the state change, the result-payload write, any cross-entity effect listed in [`05-event-protocol.md`](05-event-protocol.md) §2.2, and the event appends are all durable together or none of them is ([§6](#6-transactional-guarantees)).

### 1.2 Atomic claim (serialization)

For any given `pending` task, at most one concurrent `claim` invocation MUST succeed. A conforming task store MUST expose this as a linearizable operation: after a successful claim, every subsequent reader of the task observes `state == "claimed"` with the new claim object; no reader observes an intermediate state without the claim.

The serialization mechanism (row lock + update, compare-and-set on version, `SELECT … FOR UPDATE SKIP LOCKED`) is implementation-defined. The observable guarantee is not.

### 1.3 Atomic claim-match on submit

`submit` MUST reject calls whose supplied `worker_id` does not equal the `worker_id` currently recorded on the task's `claim` object, **as part of the same store transaction that performs the state transition** ([`04-task-protocol.md`](04-task-protocol.md) §4.1). A non-atomic "read claim, compare, then write" sequence is non-conforming because it introduces a TOCTOU window (another actor could reclaim between the read and the write). The Store-layer typed errors raised on mismatches are `WrongClaimant` (claim exists, different worker) and `NotClaimed` (claim cleared / task not in `claimed`).

The Store trusts the supplied `worker_id` as data; authentication of the *caller* against that `worker_id` is a binding-layer concern ([`04-task-protocol.md`](04-task-protocol.md) §3.3), not a Store concern.

### 1.4 Idempotent resubmit

The idempotency rule for `submit` is specified normatively in [`04-task-protocol.md`](04-task-protocol.md) §4.2. A conforming task store MUST implement it: a resubmit by the same claimant, with a content-equivalent payload, MUST succeed without mutating state; an inconsistent resubmit MUST be rejected.

### 1.5 Terminal immutability

Once a task is `completed` or `failed` ([`04-task-protocol.md`](04-task-protocol.md) §4.4), no subsequent operation MAY change any task field except audit-only metadata the task store documents outside the normative schema. In particular, `reclaim` MUST be rejected on terminal tasks.

### 1.6 Per-task serialization

All mutating operations on a single task MUST be serialized: the observable history of a task is a total order ([`04-task-protocol.md`](04-task-protocol.md) §9.1). A conforming task store MUST NOT expose a state that has not yet been accompanied by its event ([§6](#6-transactional-guarantees)).

### 1.7 Idea and variant persistence

Ideas ([`02-data-model.md`](02-data-model.md) §5) and variants ([`02-data-model.md`](02-data-model.md) §9) are persisted alongside tasks. A conforming deployment MAY back them with the task store itself, with a separate store, or with any combination; the protocol constrains the observable contract, not the physical layout. Every conforming deployment MUST expose, for both ideas and variants, at minimum:

- **Create** — durably insert a new object whose fields validate against the corresponding schema ([`schemas/idea.schema.json`](schemas/idea.schema.json), [`schemas/variant.schema.json`](schemas/variant.schema.json)).
- **Read** — return the current object or a well-defined not-found signal.
- **List** — return objects matching a filter (by `experiment_id`, `state`/`status`, etc.). Ordering is implementation-defined.
- **Update** — apply the field writes the role contracts and task- terminal transitions specify ([`03-roles.md`](03-roles.md) §2.2, §3.2, §4.4; [`04-task-protocol.md`](04-task-protocol.md) §4.3, §5.4).

All idea and variant writes MUST commit atomically with the accompanying event(s) per §6. The durability (§3.1), read-after-write (§3.2), crash-recovery (§3.3), and no-fabrication (§3.4) rules apply uniformly. Terminal immutability applies to ideas in `completed` and variants in `success`/`error`/`evaluation_error` — the `variant_commit_sha` field is the **one** post-terminal write permitted on a variant, written exclusively by the integrator ([`06-integrator.md`](06-integrator.md) §3.4), and it MUST be written atomically with its event and its `variant/*` ref.

**Variant-create precondition and the baseline relaxation.** An ordinary variant MUST be created in `status == "starting"`; the executor's `commit_sha` and the evaluator's `evaluation` are written by later transitions, not at create. A `kind == "baseline"` variant ([`02-data-model.md`](02-data-model.md) §9.4) relaxes this in one way: it MAY be created directly in `status == "success"` carrying its `evaluation` payload and `completed_at` (the override path, [`02-data-model.md`](02-data-model.md) §2.7). When a baseline is created directly in `success`, the store MUST validate the `evaluation` payload against the experiment's `evaluation_schema` ([`02-data-model.md`](02-data-model.md) §9.2) at create time — exactly the check the orchestrator applies when accepting an evaluation submission — and reject a payload that does not validate. A baseline created directly in `success` emits both `variant.started` and `variant.succeeded` atomically in the create transaction; a baseline created `starting` (the default path) emits only `variant.started` and reaches `success` via the normal evaluation-acceptance path ([`05-event-protocol.md`](05-event-protocol.md) §3.3). Creating a `kind == "baseline"` variant requires `orchestrators`-group authority at the binding layer ([`07-wire-protocol.md`](07-wire-protocol.md) §4); a baseline's `commit_sha == parent_commits[0]` (the seed framed as its own parent) is exempt from any no-op rejection a store implements ([`04-task-protocol.md`](04-task-protocol.md) §4.2).

### 1.8 Experiment persistence and lifecycle ops

Experiments ([`02-data-model.md`](02-data-model.md) §2.5) are persisted alongside tasks, ideas, and variants. A conforming deployment MAY back them with the task store itself, with a separate store, or with any combination; the protocol constrains the observable contract, not the physical layout. Every conforming deployment MUST expose:

| Operation | Arguments | Effect |
|---|---|---|
| `read_experiment` | `experiment_id` | Returns the full experiment runtime object (`experiment_id`, `state`, `created_at`, `base_commit_sha`, `imported_from`). See [§1.9](#19-checkpoint-operations) for the full definition; state-only callers project the `state` field from the result. The 12a-3 `read_experiment_state` primitive is subsumed by this op. |
| `update_experiment_state` | `experiment_id`, `new_state` | Internal primitive: atomically transitions `state` to `new_state`. Used by `terminate_experiment` and the orchestrator's policy-driven termination branch ([`03-roles.md`](03-roles.md) §6.2 decision-type 0). Not a public wire op in v0. |
| `terminate_experiment` | `experiment_id`, `reason`, `terminated_by` | Public lifecycle op ([`04-task-protocol.md`](04-task-protocol.md) §8.1). Atomically transitions `state` from `"running"` to `"terminated"` and appends `experiment.terminated` ([`05-event-protocol.md`](05-event-protocol.md) §3.4) carrying `reason` and `terminated_by`. Idempotent on the terminated state: a call against an already-terminated experiment MUST return success without appending a second event. |

The state update and the `experiment.terminated` event MUST commit in a single transaction per §6.1's composite-commit rule: subscribers MUST NOT observe one without the other. The same applies to the orchestrator's policy-driven termination — the state update + event append are a single atomic commit regardless of whether the trigger was operator-driven or policy-driven.

`update_experiment_state` is the underlying primitive both code paths share. It is described here so that conforming implementations have a single reference for the storage-layer contract; the wire layer (chapter 07) does not expose it.

**Terminated-experiment guard.** A conforming task store MUST reject every `create_task` op and every `claim` op against a `pending` task whose experiment is in state `"terminated"`, with `eden://error/illegal-transition` ([`04-task-protocol.md`](04-task-protocol.md) §2, §3.5 step 0). Already-claimed tasks MAY still complete normally (`submit`/`accept`/`reject`); the integrator's `integrate_variant` op also continues to run per the drain semantics ([`02-data-model.md`](02-data-model.md) §2.5). The guard is the Store's responsibility, not the binding's: in-process callers that bypass the binding MUST still observe it.

The §6 transactional invariant applies uniformly to experiment-state transitions: `terminate_experiment` (and the orchestrator's policy-driven path) commits the state field update and the `experiment.terminated` event in a single transaction, observably atomic to subscribers.

### 1.9 Checkpoint operations

Implementations that claim the v1+checkpoints conformance level ([`09-conformance.md`](09-conformance.md) §4) MUST expose the following operations. Implementations that do not claim that level MAY omit them.

| Operation | Arguments | Effect |
|---|---|---|
| `read_experiment` | `experiment_id` | Returns the full experiment runtime object: `experiment_id`, `state`, `created_at`, `base_commit_sha` (the seed commit, absent on pre-field experiments — [`02-data-model.md`](02-data-model.md) §2.5), and `imported_from` (`null` on natively-created experiments). Replaces the 12a-3 `read_experiment_state` projection ([§1.8](#18-experiment-persistence-and-lifecycle-ops)); state-only callers project the `state` field from the result. |
| `export_checkpoint` | `experiment_id`, output stream | Materializes an atomic snapshot of `experiment_id`'s full protocol state (tasks, ideas, variants, submissions, events, workers, groups, experiment runtime, git repo, artifacts) into the portable-checkpoint format defined in [`10-checkpoints.md`](10-checkpoints.md). The snapshot MUST satisfy the atomicity contract in [`10-checkpoints.md`](10-checkpoints.md) §6. Read-only with respect to source state. |
| `import_checkpoint` | input stream, OPTIONAL `as_experiment_id` | Reads a portable-checkpoint archive and creates a new experiment whose state matches the round-trip equivalence rules of [`10-checkpoints.md`](10-checkpoints.md) §9. Returns the imported `experiment_id` (the manifest's value, or `as_experiment_id` if supplied). Atomic: either every protocol-owned row, ref, and artifact commits, or none does. Sets the new experiment's `imported_from` field per [`02-data-model.md`](02-data-model.md) §2.5. |

`read_experiment` is exposed on the wire at [`07-wire-protocol.md`](07-wire-protocol.md) §14.3.

`export_checkpoint` reads from every store the deployment uses (task store, event log, artifact store) plus the git repository. Implementations MAY apply either of the §6 atomicity strategies in [`10-checkpoints.md`](10-checkpoints.md). The op MUST NOT mutate any persistent state.

`import_checkpoint` writes to every store. The implementation MUST validate every cross-reference between the JSONL contents and the git bundle per [`10-checkpoints.md`](10-checkpoints.md) §12 BEFORE committing any state. Validation failure rejects the archive with `eden://error/checkpoint-invalid`; collision on the resulting `experiment_id` rejects with `eden://error/experiment-id-conflict`; the `requires_credential_reissue` semantics in [`10-checkpoints.md`](10-checkpoints.md) §8 apply. The single composite commit observes the §6 transactional invariant uniformly across all touched stores.

## 2. Event log

The event log holds the per-experiment event stream. It MUST support the operations below and MUST honor the delivery guarantees specified in [`05-event-protocol.md`](05-event-protocol.md) §4.

### 2.1 Operations

| Operation | Arguments | Effect |
|---|---|---|
| `append` | fully-formed event object | Atomically appends the event, together with the state change it describes ([§6](#6-transactional-guarantees)). |
| `read_range` | experiment_id, OPTIONAL cursor | Returns a durable, ordered range of events. |
| `subscribe` | experiment_id, OPTIONAL starting cursor | Returns a stream that delivers events in log order until cancelled. |
| `replay` | experiment_id | Returns the full stream from the first event ([`05-event-protocol.md`](05-event-protocol.md) §4.4). |

`append` MUST NOT be exposed as a standalone operation that the task store, idea store, or variant store can call independently of the state change the event describes. The atomicity contract ([§6](#6-transactional-guarantees)) requires the append to travel with the state change; the event log operation is a primitive of the composite transaction, not an independent one.

### 2.2 Ordering and causality

The log MUST present a single total order per experiment ([`05-event-protocol.md`](05-event-protocol.md) §4.1) that respects causality ([`05-event-protocol.md`](05-event-protocol.md) §4.2). The ordering mechanism is implementation-defined; the observable guarantee is not.

The log MAY provide a global ordering across experiments as an optimization, but MUST NOT require subscribers to depend on it ([`05-event-protocol.md`](05-event-protocol.md) §4.6).

### 2.3 Delivery

At-least-once delivery is the minimum contract ([`05-event-protocol.md`](05-event-protocol.md) §4.3). Implementations MAY offer exactly-once via durable offset commits.

### 2.4 Retention

For the lifetime of an experiment — from registration until the experiment reaches its operator-declared terminal state — a conforming event log MUST retain **every** appended event for that experiment and MUST serve full replay from the first event ([`05-event-protocol.md`](05-event-protocol.md) §4.4). No compaction, archival, or deletion MAY occur inside this window. A deployment MAY compact or archive an experiment's events once it has reached its operator-declared terminal state; after compaction the log MUST either (a) reject replays older than the compaction horizon with a well-defined error, or (b) serve those replays from an archive that preserves order and content. It MUST NOT silently drop events from a replay.

## 3. Durability

The following applies uniformly to the task store and the event log. The per-store rules in this section compose to the aggregate experiment-durability invariant in [`01-concepts.md`](01-concepts.md) §13.

### 3.1 Write durability

An operation that the store acknowledges to its caller MUST survive a subsequent crash of the store's host. "Acknowledgement" is the return value indicating success; a store MUST NOT return success for a write that has not yet been persisted to durable media (or the equivalent quorum in a replicated implementation).

### 3.2 Read-after-write consistency

After a successful write, every subsequent read from any reader MUST observe the effect of the write. A conforming store MUST NOT expose a stale view of data after acknowledging the write that would update it. (Subscribers receive events via the log's delivery mechanism, not by polling the task store, so the read-after-write rule applies to direct reads; event delivery is governed by §4 of the event protocol.)

### 3.3 Crash recovery

A store that crashes mid-operation MUST recover to a state in which every acknowledged write is visible and no partially-applied operation is visible. Implementations that use multi-step transactions MUST ensure rollback on failure so that partial state never becomes observable.

### 3.4 No fabrication

A store MUST NOT materialize an event, a task, an idea, or a variant from nothing. Every persisted object MUST originate from a caller- driven operation. In particular, a conforming event log MUST NOT synthesize events the caller did not `append`.

## 4. Per-experiment metrics schemas

Every experiment declares an evaluation schema in its `experiment_config` ([`02-data-model.md`](02-data-model.md) §8, [`schemas/evaluation-schema.schema.json`](schemas/evaluation-schema.schema.json)). The task store (or an equivalently-scoped component in a conforming deployment) MUST enforce the following at every write that touches variant metrics.

### 4.1 Registration

At experiment registration time, a conforming deployment MUST persist the experiment's evaluation schema durably and atomically with the experiment's other configuration. A subsequent write of a variant's `evaluation` field MUST be validated against the schema registered for that experiment; no write MAY bypass this validation.

### 4.2 Immutability during an experiment

A evaluation schema MUST NOT be mutated for the lifetime of the experiment. An operator who wishes to change the metrics set MUST register a new experiment (Phase 12 defines the control-plane operations that govern this, but the invariant applies from v0 onward). A conforming deployment MUST reject any in-flight mutation of an existing experiment's evaluation schema.

The content is canonicality: comparing variants across an experiment only has meaning if the metric definitions they are compared on did not move during the experiment.

### 4.3 Per-metric type checks

A successful write of a `variant.evaluation` payload MUST satisfy:

- Every key in the `evaluation` payload is present in the experiment's evaluation schema.
- Every value either satisfies the declared type of its key — per the type mapping in [`02-data-model.md`](02-data-model.md) §1.3 (`integer`, `real`, `text`) — or is `null`.
- No reserved name ([`02-data-model.md`](02-data-model.md) §8.2) appears as a key.

A write that violates any of these MUST be rejected; the store MUST NOT record a variant as `success` with an invalid evaluation payload ([`02-data-model.md`](02-data-model.md) §9.2).

## 5. Artifact store

The artifact store holds files the roles produce (plan text, code diffs, evaluator outputs, logs) and exposes them by URI. It is less load-bearing than the task store and event log — the protocol uses it only for content referenced by `artifacts_uri` on ideas and variants — and its contract is accordingly narrower.

### 5.1 Operations

A conforming artifact store MUST support:

- **Upload** — given content bytes and a proposed identity (naming scheme is implementation-defined), persist the bytes and return a URI that will later resolve to them.
- **Fetch** — given a URI the store issued, return the byte content the upload persisted.

URIs MUST be RFC 3986–conformant ([`schemas/idea.schema.json`](schemas/idea.schema.json) enforces this on the idea side).

> *Reference deployment note (informative).* The naming scheme stays implementation-defined here. The reference deployment exposes Upload / Fetch as the two wire endpoints in [`07-wire-protocol.md`](07-wire-protocol.md) §16 (`deposit_artifact` / `fetch_artifact`), issues an **opaque** `eden://artifacts/<opaque-id>` URI from each deposit ([`02-data-model.md`](02-data-model.md) §1.5), and resolves it server-side behind a private blob backend (a local-file backend today; an S3 / GCS backend is deferred to Phase 13d). The physical storage layout is server-internal and never exposed to clients — there is no client-supplied path, so the opaque single-segment id can carry no path-traversal payload. That binding is a reference detail, not a normative requirement: the abstract Upload / Fetch operations above and the §5.2–§5.4 contracts are scheme-agnostic.

### 5.2 Durability

Once the artifact store returns a URI to an uploader, the content at that URI MUST remain resolvable until the experiment's retention window elapses. A conforming deployment MUST define its retention window explicitly. A URI recorded on a `variant/*` commit's evaluation manifest — whether as the optional `artifacts_uri` field ([`06-integrator.md`](06-integrator.md) §4.2) or inside the optional per-file `artifacts` inventory ([`06-integrator.md`](06-integrator.md) §4.4) — MUST be resolvable for at least as long as that `variant/*` commit is retained.

### 5.3 Content integrity

The artifact store MUST NOT serve byte content that differs from what was originally uploaded for a given URI. A store that versions URIs MUST ensure a fetch against a given URI returns the upload that produced it, not a subsequent overwrite.

When the optional per-file `artifacts` inventory ([`06-integrator.md`](06-integrator.md) §4.4) is present on a `variant/*` commit, the manifest's recorded `sha256` and `bytes` fields are the canonical attestation of that file's byte content at integration time; the artifact store is not required to compute or verify them at fetch time, but its no-overwrite rule is what makes the attestation load-bearing.

### 5.4 No protocol-owned mutation after the fact

Once a protocol-owned object (idea, variant, evaluation manifest) references an artifact URI, a conforming deployment MUST NOT overwrite the content at that URI. Overwriting would break reproducibility guarantees that subscribers reading the event log rely on.

### 5.5 Reference metadata row (informative)

The protocol's artifact-store contract (§5.1–§5.4) is byte-level: it mandates Upload, Fetch, durability, content integrity, and no-overwrite, but does not mandate any metadata sidecar. The reference deployment records a per-artifact metadata row ([`schemas/artifact-metadata.schema.json`](schemas/artifact-metadata.schema.json)) alongside the bytes — the minted opaque id, the depositing principal (`created_by`), the byte size, and the content type. The `created_by` field is the sole key for the [`07-wire-protocol.md`](07-wire-protocol.md) §16.2 fetch ACL (a depositor or an admin-class principal may fetch); the size and content type drive delivery. Because each deposit mints a fresh id, no client request can target an existing id for overwrite, which is how the reference store satisfies §5.4 without a versioning layer. The metadata row is a reference-binding detail; a conforming alternative store MAY carry different metadata or none.

## 6. Transactional guarantees

The transactional invariant established in [`05-event-protocol.md`](05-event-protocol.md) §2 is the central storage contract:

> Every state change observable via tasks, ideas, or variants MUST > be accompanied by a corresponding event, and the event write MUST > be atomic with the state change it describes.

A conforming deployment MUST satisfy this invariant for every write that mutates protocol-owned state — the task operations in §1.1, the idea and variant operations in §1.7, and every composite commit in [`05-event-protocol.md`](05-event-protocol.md) §2.2. The mechanism is implementation-defined. The observable guarantee — that no reader observes a state change without its event and vice versa — is not.

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
- The concrete representation of event IDs and URIs.
- The credential-hash function and storage layout for the worker registry (§9), as long as the credential is recoverable only through a fresh issuance (no plaintext storage, no reversible encoding).

What the protocol does **not** leave to implementations:

- The set of operations each store MUST expose (§1.1, §1.8, §2.1, §5.1, §9.1; additionally §1.9 for implementations claiming the v1+checkpoints conformance level).
- The atomicity contract of each operation (§1, §6).
- The read-after-write and crash-recovery guarantees (§3).
- The evaluation-schema enforcement rules (§4).
- The content-immutability rule on artifacts referenced by protocol-owned objects (§5.4).
- The worker-registry registration discipline (§9.2) and the cycle-detection invariant on group writes (§9.3).

## 8. Per-experiment registry scope

The worker registry (§9) and the group registry (§9.3) are **per-experiment**: each experiment owns a separate, isolated set of workers and groups. A `worker_id` registered for experiment `E1` and a `worker_id` of the same string registered for experiment `E2` name two distinct registry rows. A conforming deployment MUST NOT share registry state across experiments. (Cross-experiment identity is a deployment-level orchestration concern, not a protocol concept.)

## 9. Worker registry

The worker registry holds the set of registered workers and groups for a single experiment ([`02-data-model.md`](02-data-model.md) §6, §7). It MAY share its physical storage with the task store, the event log, both, or neither; the protocol constrains the observable contract.

### 9.1 Operations

| Operation | Arguments | Effect |
|---|---|---|
| `register_worker` | `worker_id`, OPTIONAL `labels` | If `worker_id` already exists, returns the existing record and does NOT mint a new credential (idempotent on existing record). Otherwise, atomically inserts the worker, mints a fresh `registration_token` (≥256 bits of entropy), stores its argon2id hash on the row, and returns `{worker_id, registration_token, ...}`. Rejects reserved or grammar-violating ids per [`02-data-model.md`](02-data-model.md) §6.1. |
| `reissue_credential` | `worker_id` | Atomically generates a fresh `registration_token`, replaces the stored hash on the existing record, and returns the new plaintext token. The prior credential becomes invalid atomically with this write. The worker registry row's `worker_id` and `registered_at` are unchanged. |
| `verify_worker_credential` | `worker_id`, `registration_token` | Returns `True` iff `registration_token` matches the stored credential hash for `worker_id`, and `False` for an unknown `worker_id` or a wrong secret. The Store MUST use a constant-time KDF comparison (argon2id `verify` is itself constant-time) and MUST equalize the unknown-worker branch's timing with the wrong-secret branch (e.g., verify against a class-level dummy hash before returning `False`). The binding layer ([`07-wire-protocol.md`](07-wire-protocol.md) §13) parses the bearer and supplies the two arguments separately. |
| `read_worker` | `worker_id` | Returns the wire-visible Worker shape (no credential / hash). |
| `list_workers` | — | Returns every registered worker in implementation-defined order. v0 does not define any filter parameter; future phases MAY add one as a backward-compatible refinement. |
| `register_group` | `group_id`, OPTIONAL `members` | Atomically creates the group; MUST reject cycles (§9.3). |
| `add_to_group`, `remove_from_group` | `group_id`, `member_id` | Atomically mutate the membership list; MUST reject cycles. |
| `read_group`, `list_groups`, `delete_group` | as named | Read / enumerate / remove a group. `delete_group` does not affect membership of its members in other groups (members are referenced by id, not owned). |
| `resolve_worker_in_group` | `worker_id`, `group_id` | Returns whether `worker_id` is transitively a member of `group_id` ([`02-data-model.md`](02-data-model.md) §7.2). MUST terminate. |

The credential-bearing operations (`register_worker` and `reissue_credential`) return the plaintext `registration_token` to the caller exactly once. A conforming Store MUST NOT expose a "fetch credential" operation; recovery from credential loss is via `reissue_credential`.

### 9.2 Registration discipline

`register_worker` is **idempotent on the existing record**: a second registration of an already-registered `worker_id` MUST return the existing record and MUST NOT generate or rotate a credential. This is what makes service-restart-after-crash cheap. Credential rotation is a separate explicit operation (`reissue_credential`).

### 9.3 Cycle detection on group writes

The set of group definitions for an experiment forms a directed graph ([`02-data-model.md`](02-data-model.md) §7.2). A conforming Store MUST reject any group-mutation operation that would close a cycle, **atomically with the write attempt**. Detection MUST be performed at write time; resolution at read time is therefore safe by construction. Rejected mutations raise `CycleDetected`.

A non-atomic "read graph, check, then write" detector is non-conforming because two concurrent mutations whose individual graphs are cycle-free can compose into a cycle if both writes commit. Implementations typically detect via DFS-on-write inside the same transaction that performs the membership update.

### 9.4 Durability and read-after-write

The §3 durability and read-after-write rules apply to registry operations uniformly: a `register_worker` that returns success guarantees the record (and its credential hash) survive a host crash; a subsequent `verify_worker_credential` MUST see the hash that the registration just stored.
