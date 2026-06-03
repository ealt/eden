# Event Protocol

This chapter specifies the event log: the envelope every event MUST carry, the transactional invariant that binds event writes to the state changes they describe, the per-type payload shapes for the v0 event registry, and the delivery guarantees a conforming event log MUST offer to subscribers.

The event envelope's JSON Schema is [`schemas/event.schema.json`](schemas/event.schema.json). The behavioral contracts that produce events are in [`04-task-protocol.md`](04-task-protocol.md) (task transitions) and [`06-integrator.md`](06-integrator.md) (variant integration). The event log's durability and subscription semantics as a *store* are in [`08-storage.md`](08-storage.md); this chapter specifies what events mean, not how they are persisted.

## 1. Envelope

Every event appended to a conforming event log MUST be a JSON object carrying:

| Field | Required | Type | Meaning |
|---|---|---|---|
| `event_id` | yes | string | Unique identifier for this event within the log. Opaque. |
| `type` | yes | string | Dotted type name (`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`). The normative registry is in §3. |
| `occurred_at` | yes | timestamp | When the state change happened. UTC, trailing `Z`, RFC 3339 profile (`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$`). |
| `experiment_id` | yes | string | The experiment the event belongs to. Events are scoped per experiment ([`02-data-model.md`](02-data-model.md) §4.3). |
| `data` | yes | object | Type-specific payload. Shape is fixed by the registry entry for `type` (§3). |

The envelope is closed over these fields for conforming events; a conforming log MAY persist additional audit metadata alongside each event, but MUST NOT expose it under any of the five envelope keys. Implementations MAY include additional top-level keys outside the envelope (e.g. `sequence`, `recorded_at`); subscribers relying only on the envelope MUST remain correct regardless.

### 1.1 `event_id` uniqueness

`event_id` MUST be unique within the log. A subscriber MAY dedupe by `event_id` when recovering from a disconnect.

### 1.2 `occurred_at` vs. log order

`occurred_at` records when the state change happened, which MAY differ from the event's position in the log — clock skew, batched writes, or retried appends can all produce non-monotonic `occurred_at` within an experiment's stream. Causal order is established by the log itself (§4.3), not by timestamps. `occurred_at` is advisory for human debugging; subscribers MUST NOT rely on it for ordering.

## 2. Transactional invariant

The central invariant of the event protocol, stated in [`01-concepts.md`](01-concepts.md) §6:

> **Every state change that is observable via tasks, ideas, or > variants MUST be accompanied by a corresponding event, and the event > write MUST be atomic with the state change it describes.**

"Atomic" means: either both the state change and the event append are durable, or neither is. A conforming implementation MUST NOT expose a state change to readers (direct store reads, cached projections, push notifications, role dispatch) before its event is durable in the log; conversely, it MUST NOT append an event whose state change has not been durably written. If a transaction covering both is aborted, neither is observable.

The invariant applies to the set of entities enumerated in [`02-data-model.md`](02-data-model.md): tasks, ideas, variants, and experiments. The §3.4 experiment-scoped events cover both a configuration-state mutation (`experiment.dispatch_mode_changed`) and the lifecycle-state mutation `experiment.terminated` ([`02-data-model.md`](02-data-model.md) §2.5) under the same atomicity rule. Implementations MAY emit *additional* events outside this set (see §3.6) that do not correspond to a protocol-defined state change; those additional events are not subject to the transactional invariant because there is no state change to bind them to.

### 2.1 Why atomic

The event log is the normative observability channel ([`04-task-protocol.md`](04-task-protocol.md) §9.3). Without atomicity, subscribers reconstructing history can observe either a state without its event or an event whose state the store does not yet expose — either is a protocol violation that leaves subscribers unable to trust the log.

### 2.2 Composite transitions

Several transitions span multiple entities and MUST commit together:

- **Execution-task dispatch** — creating a `task` with `kind=execution` and transitioning its referenced `idea` from `ready` to `dispatched` ([`04-task-protocol.md`](04-task-protocol.md) §2). Events: `task.created` + `idea.dispatched`, in one atomic commit.
- **Execution-task terminal** — the `execution` task's terminal transition (`submitted → completed` or `submitted → failed`) and the matching `idea` transition from `dispatched` to `completed` ([`04-task-protocol.md`](04-task-protocol.md) §4.3, §10). Events: `task.completed` (or `task.failed`) + `idea.completed`, in one atomic commit.
- **Evaluation-task terminal (`success`/`error`)** — the `evaluation` task's terminal transition plus writes to the variant's `status`, `evaluation`, `artifacts_uri`, and `completed_at` ([`03-roles.md`](03-roles.md) §4.4). Events: `task.completed` (or `task.failed`) + `variant.succeeded` / `variant.errored`, in one atomic commit.
- **Execution-task reclaim with in-flight variant** — reclamation of an `execution` task whose prior execution left a variant in `starting` requires transitioning that variant to `error` atomically with the reclaim ([`04-task-protocol.md`](04-task-protocol.md) §5.4). Events: `task.reclaimed` + `variant.errored`, in one atomic commit.
- **Retry-exhausted `evaluation_error` terminal** — the orchestrator's transition of a variant from `starting` to `evaluation_error` ([`04-task-protocol.md`](04-task-protocol.md) §4.3). When the orchestrator persists this transition as a state change on the variant, it MUST emit `variant.evaluation_errored` atomically.
- **Variant integration** — the integrator's write of a `variant/*` commit and the `variant_commit_sha` field on the variant ([`06-integrator.md`](06-integrator.md)). Event: `variant.integrated`.
- **Reassignment of a claimed task** — `reassign_task` on a claimed task is the composite `clear claim` + `update target` ([`04-task-protocol.md`](04-task-protocol.md) §6.1). Events: `task.reclaimed` (with `cause == "operator"`) + `task.reassigned` (with `new_target`), in one atomic commit.

A subscriber processing any of these composite events MUST therefore either observe the full set or observe none; partial visibility is a protocol violation.

### 2.3 What is *not* a state change

Operations that do not change protocol-owned state do not require events:

- Reading a task, idea, variant, or event.
- Uploading artifacts to the artifact store before they are referenced from a protocol-owned object. (Populating `artifacts_uri` on an idea or variant *is* a state change and is covered by the events on those objects.)
- Worker-internal progress (scratch files, local logs).

Implementations MAY expose operational telemetry through other channels without running it through the event log.

## 3. Event registry

Every v0 event `type` defined by the protocol is listed below. For each, the `data` object's required fields are pinned; optional fields are called out per entry. The JSON Schema in [`schemas/event.schema.json`](schemas/event.schema.json) enforces these shapes via `if/then` dispatch on `type`.

Implementations MAY emit additional event types outside this registry (§3.6). The `type` pattern in §1 is the only structural constraint on them.

### 3.1 Task events

Produced atomically with the transitions defined in [`04-task-protocol.md`](04-task-protocol.md). The `task_id` field on every task-event payload names the task whose transition is being recorded.

| Type | Transition | `data` required fields |
|---|---|---|
| `task.created` | — → `pending` | `task_id`, `kind` |
| `task.claimed` | `pending` → `claimed` | `task_id`, `worker_id` |
| `task.submitted` | `claimed` → `submitted` | `task_id` |
| `task.completed` | `submitted` → `completed` | `task_id` |
| `task.failed` | `submitted` → `failed` | `task_id`, `reason` |
| `task.reclaimed` | `claimed`/`submitted` → `pending` | `task_id`, `cause` |
| `task.reassigned` | `pending` → `pending` (with new `target`) | `task_id`, `new_target`, `reason`, `reassigned_by` |

Payload field definitions:

- `task_id` — the `task_id` of the transitioning task.
- `kind` — the task's `kind` ([`02-data-model.md`](02-data-model.md) §3.1); one of `ideation`, `execution`, `evaluation`.
- `worker_id` — the `claim.worker_id` recorded on the successful claim ([`04-task-protocol.md`](04-task-protocol.md) §3.2).
- `reason` — for `task.failed`: one of the strings `"worker_error"` (the worker's submission declared failure), `"validation_error"` (the orchestrator rejected the result as malformed or non-conforming), or `"policy_limit"` (a policy such as retry budget caused the failure). The literal set is closed for v0; an implementation that needs finer granularity MAY add a separate operator-level event under its own type (§3.6). For `task.reassigned`: a free-form string (typical: `"operator"`, `"failed_worker"`, `"misrouted"`); not enumerated by the protocol.
- `cause` — one of `"expired"` (claim `expires_at` passed), `"operator"` (explicit operator action), or `"health_policy"` (task store health policy declared the worker unreachable). The literal set is closed for v0 on the same terms as `reason`.
- `new_target` — the `Task.target` value installed by the reassignment ([`02-data-model.md`](02-data-model.md) §3.5). `null` for "any worker"; otherwise an object with `kind` (`"worker"` / `"group"`) and `id`.
- `reassigned_by` — the `worker_id` of the caller that invoked `reassign_task` ([`04-task-protocol.md`](04-task-protocol.md) §6), drawn from the binding's authenticated principal. The caller MUST be in the `admins` group per [`04-task-protocol.md`](04-task-protocol.md) §6.2.

The payload MAY include additional fields beyond those required; subscribers MUST tolerate them. A conforming orchestrator SHOULD include the submitting worker's `worker_id` on `task.submitted`, `task.completed`, and `task.failed` events when known, as an operational convenience — but this is not required because the worker-task binding is already recoverable from the preceding `task.claimed` event.

### 3.2 Idea events

Produced atomically with the idea `state` transitions defined in [`02-data-model.md`](02-data-model.md) §5 and [`03-roles.md`](03-roles.md) §2.2. Composite commits that bind an idea event to a task event are enumerated in §2.2.

| Type | Transition | `data` required fields |
|---|---|---|
| `idea.drafted` | — → `drafting` | `idea_id` |
| `idea.ready` | `drafting` → `ready` | `idea_id` |
| `idea.dispatched` | `ready` → `dispatched` | `idea_id`, `task_id` |
| `idea.completed` | `dispatched` → `completed` | `idea_id`, `task_id` |

Payload field definitions:

- `idea_id` — the `idea_id` of the transitioning idea.
- `task_id` — for `idea.dispatched`, the `task_id` of the `execution` task created by the same commit (§2.2). For `idea.completed`, the `task_id` of the `execution` task whose terminal transition is being recorded.

### 3.3 Variant events

Produced atomically with variant `status` transitions and with the integrator's integration write. The variant's lifecycle is defined in [`02-data-model.md`](02-data-model.md) §9 and [`03-roles.md`](03-roles.md) §3–§4; the integration step is in [`06-integrator.md`](06-integrator.md).

| Type | Transition | `data` required fields |
|---|---|---|
| `variant.started` | — → `starting` | `variant_id`, `idea_id` (absent for `kind == "baseline"`), `kind` (required when `kind == "baseline"`) |
| `variant.succeeded` | `starting` → `success` | `variant_id`, `commit_sha` |
| `variant.errored` | `starting` → `error` | `variant_id` |
| `variant.evaluation_errored` | `starting` → `evaluation_error` | `variant_id` |
| `variant.integrated` | — (integrator writes `variant_commit_sha`) | `variant_id`, `variant_commit_sha` |

Payload field definitions:

- `variant_id` — the `variant_id` of the transitioning variant.
- `idea_id` — the variant's `idea_id` ([`02-data-model.md`](02-data-model.md) §9.1). REQUIRED on `variant.started` for an ordinary variant; **absent** for a `kind == "baseline"` variant, which has no producing idea ([`02-data-model.md`](02-data-model.md) §9.4).
- `kind` — the variant's `kind` ([`02-data-model.md`](02-data-model.md) §9.1), present on `variant.started`. REQUIRED (not merely recommended) when the variant is a baseline, so an event-only subscriber gets an explicit `kind == "baseline"` signal rather than inferring a baseline from a missing `idea_id`. Absent (or `null`) for an ordinary variant.
- `commit_sha` — the worker-branch tip recorded on the variant at the moment of success ([`02-data-model.md`](02-data-model.md) §9.1). For a baseline this is the seed (`base_commit_sha`).
- `variant_commit_sha` — the canonical-lineage SHA the integrator wrote ([`06-integrator.md`](06-integrator.md)). A 40-hex SHA-1 or a 64-hex SHA-256; the same pattern as commits elsewhere in the data model.

`variant.integrated` is not a variant-`status` transition — integration does not change the variant's `status` field, only its `variant_commit_sha`. The event marks integration so subscribers can reconstruct the canonical lineage without reading git directly. A `kind == "baseline"` variant is never integrated ([`02-data-model.md`](02-data-model.md) §9.4), so no `variant.integrated` event is ever emitted for one.

**Baseline override path.** A `kind == "baseline"` variant created directly in `success` with config-supplied metrics ([`02-data-model.md`](02-data-model.md) §2.7, §9.4, [`08-storage.md`](08-storage.md) §1.7) emits both `variant.started` (with the required `kind` and absent `idea_id`) **and** `variant.succeeded`, atomically, in the single `create_variant` transaction — the same pair the normal start → evaluate → succeed flow produces, collapsed to one commit. A default-path baseline emits `variant.started` at create and `variant.succeeded` later via the normal evaluation-acceptance path. Both events carry only the identifying fields above; neither references an evaluation `task_id` (a baseline on the override path has no evaluation task).

### 3.4 Experiment events

Produced atomically with mutations of experiment-scoped configuration or lifecycle state. The `experiment_id` is implicit (events live in the per-experiment log per §4.6); the payload describes the experiment-level state change.

| Type | Trigger | `data` required fields |
|---|---|---|
| `experiment.dispatch_mode_changed` | `update_dispatch_mode` ([`04-task-protocol.md`](04-task-protocol.md) §7) | `dispatch_mode`, `changed`, `updated_by` |
| `experiment.terminated` | `terminate_experiment` ([`04-task-protocol.md`](04-task-protocol.md) §8.1) or the orchestrator's termination decision ([`03-roles.md`](03-roles.md) §6.2 decision-type 0) | `reason`, `terminated_by` |
| `experiment.policy_error` | A termination policy callable raises ([`03-roles.md`](03-roles.md) §6.2 decision-type 0, fault-tolerance subsection) | `policy_kind`, `error_type`, `error_message` |

Payload field definitions:

- `dispatch_mode` — the **resulting** `dispatch_mode` object on the experiment after the merge ([`02-data-model.md`](02-data-model.md) §2.4). Includes every key (defaults applied), so a subscriber that resyncs from this event observes the full post-update state without needing the prior value.
- `changed` — an object mapping each key whose value flipped to its new value (subset of `dispatch_mode`). Empty if the update was a no-op (every supplied key already matched the stored value); in that case the implementation MAY skip the event entirely per [`04-task-protocol.md`](04-task-protocol.md) §7.1.
- `updated_by` — the `worker_id` of the caller that invoked `update_dispatch_mode`. The caller MUST be in the `admins` group per [`04-task-protocol.md`](04-task-protocol.md) §7.2.
- `reason` — free-form string supplied by the caller of `terminate_experiment` (operator-driven) or by the termination policy's `Terminate(reason)` return (policy-driven). The protocol does not constrain the format; deployments use it as a human-readable explanation. The first commit's `reason` is the one recorded; subsequent idempotent calls' `reason` strings are discarded per [`04-task-protocol.md`](04-task-protocol.md) §8.1.
- `terminated_by` — the `worker_id` of the principal credited with the transition. For operator-driven termination this is the authenticated `admins` caller. For policy-driven termination this is the `worker_id` of the orchestrator instance whose policy callable returned `Terminate`.
- `policy_kind` — a string identifying which policy kind raised. v0 defines only `"termination"`; future decision types that introduce policy callables MAY add new values.
- `error_type` — the exception class name (e.g. `"ValueError"`, `"KeyError"`); free-form per implementation language.
- `error_message` — the exception's `str()` representation; free-form.

Note on ideation-task creation: the orchestrator-role contract's continuous-policy mechanism ([`03-roles.md`](03-roles.md) §6.2) does NOT introduce a new event type. Every ideation-task creation (whether driven by the auto-orchestrator's policy or by an operator under `dispatch_mode.ideation_creation == "manual"`) emits the standard `task.created` event from §3.1, with attribution recorded in `task.created_by` ([`02-data-model.md`](02-data-model.md) §3.1).

Note on `experiment.policy_error`: this event records an orchestrator-side fault, not a state change on the experiment. It is exempt from the §2 transactional invariant: there is no protocol-owned state mutation paired with it. The event is registered to give operators a normative observability channel for policy faults; subscribers that do not consume it MUST NOT therefore miss any state-machine transition.

### 3.5 Subscriber observability guarantee

Every protocol-owned state-machine transition in v0 is covered by exactly one event type in §3.1–§3.4. A subscriber that consumes every event with a registered `type`, in log order, MUST be able to reconstruct the **lifecycle history** of every task, idea, variant, and experiment-scoped configuration in the experiment: which entities exist, in what states, in what order. A conforming implementation MUST NOT expose a state-machine transition that is not marked by its corresponding registered event.

Registered event payloads carry only the fields needed to *identify* the transitioning entity and any cross-entity references (e.g. `idea.dispatched` carries the implement `task_id`). They do not carry full entity snapshots. A subscriber that needs the content of an entity (the idea's `parent_commits` and `artifacts_uri`, the variant's `evaluation` and `completed_at`) MUST read that entity from its store using the identifier the event carries; the read returns the entity's current state ([`08-storage.md`](08-storage.md) §1.7). This boundary is deliberate: events mark what happened; the entity stores hold what the entity *is*. Coupling the two into a full event-sourced projection — a subscriber reconstructing every intermediate entity value from events alone — is a deployment MAY implement on top of v0 but is not a protocol requirement, since v0 does not mandate historical reads on the entity stores.

The transactional invariant (§2) combined with the atomic event + state-change rule guarantees that any entity reachable via an event's identifier is already durable at read time.

### 3.6 Non-registered event types

Implementations MAY emit events whose `type` is not listed in §3.1–§3.4, provided the type conforms to the `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$` pattern. Such events carry no protocol-defined semantics and MUST NOT be required by any conforming subscriber.

A non-registered event MUST NOT carry a registered `type` with a non-conforming payload; that is a protocol violation, not an extension. The registry is closed at v0: a future spec lineage (`v1`) MAY add or revise registered types.

## 4. Delivery guarantees

A conforming event log MUST provide each subscriber the following guarantees, which subscribers MAY rely on without additional coordination.

### 4.1 Total order per experiment

Within a single experiment, a conforming event log MUST present a single total order to subscribers. Two subscribers reading the same experiment's stream MUST observe every pair of events in the same relative order. The ordering mechanism (monotonic sequence, log position, vector clock) is a storage concern; the observable guarantee is not.

### 4.2 Causal consistency

Within the per-experiment total order, the sequence MUST respect causality: if event B describes a state change that depended on a state recorded by event A, A MUST precede B in the log ([`02-data-model.md`](02-data-model.md) §4.3). In particular, for every composite commit enumerated in §2.2, the events produced by that commit MAY appear in any order relative to each other — they were written atomically and neither causes the other — but they MUST all precede any event that causally depends on the committed state.

### 4.3 At-least-once delivery

A conforming log MUST deliver every appended event to every active subscriber at least once. A subscriber MAY observe the same `event_id` more than once under recovery, restart, or reconnect; subscribers MUST dedupe by `event_id` when exact-once semantics are required for their projection.

Implementations MAY offer stronger delivery than at-least-once (e.g. exactly-once via offset commits), but MUST NOT offer weaker than at-least-once.

### 4.4 Replayability

For the **lifetime of an experiment** — from registration until the experiment reaches its operator-declared terminal state (Phase 12 control-plane concern; for v0, treat this as "until the deployment explicitly archives the experiment") — a conforming log MUST retain every appended event for that experiment and MUST allow any subscriber to replay the full stream from the experiment's first event. This is what lets late subscribers reconstruct history and lets existing subscribers recover from projection corruption. The log MAY allow starting from a caller-supplied position (event ID or offset) as an optimization, but the complete replay is required.

A conforming deployment MAY compact or archive an experiment's events *only* after the experiment has reached its operator-declared terminal state. Once compacted, the log MUST either preserve full replay from an archive (preferred), or reject replays older than the compaction horizon with a well-defined error. A log MUST NOT silently drop events from a replay during an experiment's active lifetime.

### 4.5 Durability

A successful append MUST be durable through the next crash of the log implementation. An event the log has acknowledged to the writer MUST NOT later disappear from any subscriber's stream. Durability requirements for the log as a store are pinned in [`08-storage.md`](08-storage.md) §3.

### 4.6 No cross-experiment ordering

Events in different experiments have no mandated relative order. A conforming log MAY expose a global ordering (e.g. a single monotonic sequence across all experiments) as an implementation convenience, but a subscriber that relies on cross-experiment order is relying on something the protocol does not guarantee.

## 5. Implementation latitude

The protocol leaves to implementations:

- The transport and storage of events (append-only table, commit log, message broker).
- The subscription mechanism (long-poll, push, WebSocket, `LISTEN`).
- The concrete `event_id` representation, as long as uniqueness (§1.1) holds.
- The retention policy — a log MAY compact or archive after a policy window, provided replay (§4.4) is preserved for the retained window. Consumers that need history beyond the retention policy's horizon are responsible for their own durable projections.
- The mechanism of atomicity (§2): transactional database, outbox pattern, two-phase commit across task store and log, or a unified store where the log *is* the task store's write-ahead record.

What the protocol does **not** leave to implementations:

- The envelope fields (§1).
- The transactional invariant (§2).
- The registered event types and their payloads (§3.1–§3.4).
- The delivery guarantees in §4.
