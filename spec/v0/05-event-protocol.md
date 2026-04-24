# Event Protocol

This chapter specifies the event log: the envelope every event MUST carry, the transactional invariant that binds event writes to the state changes they describe, the per-type payload shapes for the v0 event registry, and the delivery guarantees a conforming event log MUST offer to subscribers.

The event envelope's JSON Schema is [`schemas/event.schema.json`](schemas/event.schema.json). The behavioral contracts that produce events are in [`04-task-protocol.md`](04-task-protocol.md) (task transitions) and [`06-integrator.md`](06-integrator.md) (trial promotion). The event log's durability and subscription semantics as a *store* are in [`08-storage.md`](08-storage.md); this chapter specifies what events mean, not how they are persisted.

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

> **Every state change that is observable via tasks, proposals, or > trials MUST be accompanied by a corresponding event, and the event > write MUST be atomic with the state change it describes.**

"Atomic" means: either both the state change and the event append are durable, or neither is. A conforming implementation MUST NOT expose a state change to readers (direct store reads, cached projections, push notifications, role dispatch) before its event is durable in the log; conversely, it MUST NOT append an event whose state change has not been durably written. If a transaction covering both is aborted, neither is observable.

The invariant applies to the set of entities enumerated in [`02-data-model.md`](02-data-model.md): tasks, proposals, trials. Implementations MAY emit *additional* events outside this set (see §3.5) that do not correspond to a protocol-defined state change; those additional events are not subject to the transactional invariant because there is no state change to bind them to.

### 2.1 Why atomic

The event log is the normative observability channel ([`04-task-protocol.md`](04-task-protocol.md) §6.3). Without atomicity, subscribers reconstructing history can observe either a state without its event or an event whose state the store does not yet expose — either is a protocol violation that leaves subscribers unable to trust the log.

### 2.2 Composite transitions

Several transitions span multiple entities and MUST commit together:

- **Implement dispatch** — creating a `task` with `kind=implement` and transitioning its referenced `proposal` from `ready` to `dispatched` ([`04-task-protocol.md`](04-task-protocol.md) §2). Events: `task.created` + `proposal.dispatched`, in one atomic commit.
- **Implement terminal** — the `implement` task's terminal transition (`submitted → completed` or `submitted → failed`) and the matching `proposal` transition from `dispatched` to `completed` ([`04-task-protocol.md`](04-task-protocol.md) §4.3, §7). Events: `task.completed` (or `task.failed`) + `proposal.completed`, in one atomic commit.
- **Evaluate terminal (`success`/`error`)** — the `evaluate` task's terminal transition plus writes to the trial's `status`, `metrics`, `artifacts_uri`, and `completed_at` ([`03-roles.md`](03-roles.md) §4.4). Events: `task.completed` (or `task.failed`) + `trial.succeeded` / `trial.errored`, in one atomic commit.
- **Implement reclaim with in-flight trial** — reclamation of an `implement` task whose prior execution left a trial in `starting` requires transitioning that trial to `error` atomically with the reclaim ([`04-task-protocol.md`](04-task-protocol.md) §5.4). Events: `task.reclaimed` + `trial.errored`, in one atomic commit.
- **Retry-exhausted `eval_error` terminal** — the orchestrator's transition of a trial from `starting` to `eval_error` ([`04-task-protocol.md`](04-task-protocol.md) §4.3). When the orchestrator persists this transition as a state change on the trial, it MUST emit `trial.eval_errored` atomically.
- **Trial promotion** — the integrator's write of a `trial/*` commit and the `trial_commit_sha` field on the trial ([`06-integrator.md`](06-integrator.md)). Event: `trial.integrated`.

A subscriber processing any of these composite events MUST therefore either observe the full set or observe none; partial visibility is a protocol violation.

### 2.3 What is *not* a state change

Operations that do not change protocol-owned state do not require events:

- Reading a task, proposal, trial, or event.
- Uploading artifacts to the artifact store before they are referenced from a protocol-owned object. (Populating `artifacts_uri` on a proposal or trial *is* a state change and is covered by the events on those objects.)
- Worker-internal progress (scratch files, local logs).

Implementations MAY expose operational telemetry through other channels without running it through the event log.

## 3. Event registry

Every v0 event `type` defined by the protocol is listed below. For each, the `data` object's required fields are pinned; optional fields are called out per entry. The JSON Schema in [`schemas/event.schema.json`](schemas/event.schema.json) enforces these shapes via `if/then` dispatch on `type`.

Implementations MAY emit additional event types outside this registry (§3.5). The `type` pattern in §1 is the only structural constraint on them.

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

Payload field definitions:

- `task_id` — the `task_id` of the transitioning task.
- `kind` — the task's `kind` ([`02-data-model.md`](02-data-model.md) §3.1); one of `plan`, `implement`, `evaluate`.
- `worker_id` — the `claim.worker_id` recorded on the successful claim ([`04-task-protocol.md`](04-task-protocol.md) §3.2).
- `reason` — one of the strings `"worker_error"` (the worker's submission declared failure), `"validation_error"` (the orchestrator rejected the result as malformed or non-conforming), or `"policy_limit"` (a policy such as retry budget caused the failure). The literal set is closed for v0; an implementation that needs finer granularity MAY add a separate operator-level event under its own type (§3.5).
- `cause` — one of `"expired"` (claim `expires_at` passed), `"operator"` (explicit operator action), or `"health_policy"` (task store health policy declared the worker unreachable). The literal set is closed for v0 on the same terms as `reason`.

The payload MAY include additional fields beyond those required; subscribers MUST tolerate them. A conforming orchestrator SHOULD include the submitting worker's `worker_id` on `task.submitted`, `task.completed`, and `task.failed` events when known, as an operational convenience — but this is not required because the worker-task binding is already recoverable from the preceding `task.claimed` event.

### 3.2 Proposal events

Produced atomically with the proposal `state` transitions defined in [`02-data-model.md`](02-data-model.md) §5 and [`03-roles.md`](03-roles.md) §2.2. Composite commits that bind a proposal event to a task event are enumerated in §2.2.

| Type | Transition | `data` required fields |
|---|---|---|
| `proposal.drafted` | — → `drafting` | `proposal_id` |
| `proposal.ready` | `drafting` → `ready` | `proposal_id` |
| `proposal.dispatched` | `ready` → `dispatched` | `proposal_id`, `task_id` |
| `proposal.completed` | `dispatched` → `completed` | `proposal_id`, `task_id` |

Payload field definitions:

- `proposal_id` — the `proposal_id` of the transitioning proposal.
- `task_id` — for `proposal.dispatched`, the `task_id` of the `implement` task created by the same commit (§2.2). For `proposal.completed`, the `task_id` of the `implement` task whose terminal transition is being recorded.

### 3.3 Trial events

Produced atomically with trial `status` transitions and with the integrator's promotion write. The trial's lifecycle is defined in [`02-data-model.md`](02-data-model.md) §7 and [`03-roles.md`](03-roles.md) §3–§4; the promotion step is in [`06-integrator.md`](06-integrator.md).

| Type | Transition | `data` required fields |
|---|---|---|
| `trial.started` | — → `starting` | `trial_id`, `proposal_id` |
| `trial.succeeded` | `starting` → `success` | `trial_id`, `commit_sha` |
| `trial.errored` | `starting` → `error` | `trial_id` |
| `trial.eval_errored` | `starting` → `eval_error` | `trial_id` |
| `trial.integrated` | — (integrator writes `trial_commit_sha`) | `trial_id`, `trial_commit_sha` |

Payload field definitions:

- `trial_id` — the `trial_id` of the transitioning trial.
- `proposal_id` — the trial's `proposal_id` ([`02-data-model.md`](02-data-model.md) §7.1).
- `commit_sha` — the worker-branch tip recorded on the trial at the moment of success ([`02-data-model.md`](02-data-model.md) §7.1).
- `trial_commit_sha` — the canonical-lineage SHA the integrator wrote ([`06-integrator.md`](06-integrator.md)). A 40-hex SHA-1 or a 64-hex SHA-256; the same pattern as commits elsewhere in the data model.

`trial.integrated` is not a trial-`status` transition — integration does not change the trial's `status` field, only its `trial_commit_sha`. The event marks promotion so subscribers can reconstruct the canonical lineage without reading git directly.

### 3.4 Subscriber observability guarantee

Every protocol-owned state-machine transition in v0 is covered by exactly one event type in §3.1–§3.3. A subscriber that consumes every event with a registered `type`, in log order, MUST be able to reconstruct the **lifecycle history** of every task, proposal, and trial in the experiment: which entities exist, in what states, in what order. A conforming implementation MUST NOT expose a state- machine transition that is not marked by its corresponding registered event.

Registered event payloads carry only the fields needed to *identify* the transitioning entity and any cross-entity references (e.g. `proposal.dispatched` carries the implement `task_id`). They do not carry full entity snapshots. A subscriber that needs the content of an entity (the proposal's `parent_commits` and `artifacts_uri`, the trial's `metrics` and `completed_at`) MUST read that entity from its store using the identifier the event carries; the read returns the entity's current state ([`08-storage.md`](08-storage.md) §1.7). This boundary is deliberate: events mark what happened; the entity stores hold what the entity *is*. Coupling the two into a full event-sourced projection — a subscriber reconstructing every intermediate entity value from events alone — is a deployment MAY implement on top of v0 but is not a protocol requirement, since v0 does not mandate historical reads on the entity stores.

The transactional invariant (§2) combined with the atomic event + state-change rule guarantees that any entity reachable via an event's identifier is already durable at read time.

### 3.5 Non-registered event types

Implementations MAY emit events whose `type` is not listed in §3.1–§3.3, provided the type conforms to the `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$` pattern. Such events carry no protocol-defined semantics and MUST NOT be required by any conforming subscriber.

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
- The registered event types and their payloads (§3.1–§3.3).
- The delivery guarantees in §4.
