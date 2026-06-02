# Control plane

This chapter specifies the **control plane** — the deployment-level coordination layer that lets a single EDEN deployment host multiple experiments concurrently and run multiple orchestrator replicas against them with high availability.

A deployment without a control plane runs exactly one experiment under exactly one orchestrator instance (the pre-12c topology; the task-store-server binds an experiment id at startup). A deployment with a control plane runs **N orchestrator replicas** against **M experiments**, where each experiment is owned by at most one replica at any wall-clock instant via a per-experiment **lease**. The control plane MAY be omitted from a deployment that does not need multi-experiment or multi-replica capability; chapter 11 is normative only for deployments that expose the operations defined here.

The wire bindings of the operations defined in this chapter live in [`07-wire-protocol.md`](07-wire-protocol.md) §15. The conformance level that adds control-plane scenarios is `v1+multi-experiment` ([`09-conformance.md`](09-conformance.md) §4).

## 1. Purpose

The control plane provides three deployment-level services:

1. **Experiment registry.** A durable, deployment-wide catalog of registered experiments. Each entry carries the experiment id, a URI to the experiment-config resource, a creation timestamp, and a cached projection of the experiment's lifecycle state (running / terminated). The registry is the source of truth for "which experiments exist in this deployment"; the per-experiment task-store-server data is the source of truth for *the contents* of each experiment.
2. **Leases.** A time-bounded ownership claim issued to an orchestrator replica for a specific experiment. The lease holder is the unique replica authorized to run the five orchestrator decisions ([`03-roles.md`](03-roles.md) §6.2) for the leased experiment. A non-holder MUST NOT run any of those decisions for that experiment.
3. **Cross-experiment reads.** Aggregate read endpoints (`list_experiments`, `read_experiment_metadata`) that operator UIs use to enumerate experiments. The control plane does NOT proxy per-experiment task / idea / variant / event reads; those continue to flow to the per-experiment task-store-server endpoints from [`07-wire-protocol.md`](07-wire-protocol.md) §§2–8.

The control plane has **no orchestrator-decision authority of its own**. It does not create tasks, integrate variants, or invoke termination policies. All decisions still happen in orchestrator replicas running the chapter 03 §6 contract; the control plane only decides *which replica* may run those decisions for a given experiment, by issuing leases.

## 2. Experiment registry

### 2.1 Fields

Each registered experiment carries:

| Field | Type | Description |
|---|---|---|
| `experiment_id` | string (`exp_*`) | Opaque, system-minted id matching the [`02-data-model.md`](02-data-model.md) §1.6 grammar; the control plane mints it at `register_experiment`. Unique within the deployment by construction. |
| `name` | string \| null | OPTIONAL operator-supplied display label ([`02-data-model.md`](02-data-model.md) §1.7) so cross-experiment admin views can render a human label. Resolvable via the `?name=` lookup ([`07-wire-protocol.md`](07-wire-protocol.md) §15.1). |
| `config_uri` | URI string | URI pointing at the experiment-config resource ([`02-data-model.md`](02-data-model.md) §2). The control plane does not interpret the URI; operators choose a scheme that the orchestrator can resolve. |
| `created_at` | timestamp | Wall-clock time at which the registry entry was created. Independent of the task-store-server's `experiment.created_at`; the two MAY differ when an experiment is registered before its task-store-server side is populated, or imported with its task-store-server side preceding the registry entry. |
| `last_known_state` | enum | `"running"` or `"terminated"`. A cached projection of the task-store-server's authoritative `experiment.state` ([`02-data-model.md`](02-data-model.md) §2.5). The control plane updates this field per the §3 sync rules. |
| `lease` | object \| null | The currently-active `ExperimentLease` for the experiment, or `null` if no lease is held. Shape per §4. |

The schema is [`schemas/lease.schema.json`](schemas/lease.schema.json) for the `lease` subfield and ad hoc per the registry-entry fields above (no separate registry-entry schema is normative; entries surface through the wire shapes in [`07-wire-protocol.md`](07-wire-protocol.md) §15).

### 2.2 Mutations

The registry supports four operations:

- `register_experiment(name?, config_uri)` — admin-gated. **Mints a fresh opaque `exp_*`** ([`02-data-model.md`](02-data-model.md) §1.6); the caller does NOT supply an id. Creates a new entry with the minted `experiment_id`, the optional display `name` ([`02-data-model.md`](02-data-model.md) §1.7; an ill-formed name MUST be rejected with 422 `eden://error/invalid-name`), `created_at = now`, `last_known_state = "running"`, `lease = null`. Returns the new entry. Because the id is system-minted, every call creates a distinct entry — there is no idempotent re-registration by id (the pre-rename caller-supplied-id idempotency is retired). The import path supplies the experiment id it already minted on the task-store side rather than minting a second one; see §7.
- `unregister_experiment(experiment_id)` — admin-gated. Removes the entry. MUST reject with 409 `eden://error/invalid-precondition` when `last_known_state != "terminated"` OR when an active lease exists (§4.4 "active" definition). Operators MUST terminate the experiment via the operator-driven wire op ([`07-wire-protocol.md`](07-wire-protocol.md) §2.9) before unregistering.
- `list_experiments()` — read. Returns every registered experiment, including its current lease. Authentication required (admin OR registered control-plane worker per Decision 12 below).
- `read_experiment_metadata(experiment_id)` — read. Returns one registry entry. Authentication required as for `list_experiments`. The response MAY carry a `warnings` array surfacing operator-visible state-sync degradation per §3.4.

### 2.3 Registry vs. task-store-server data

The control plane's registry is a deployment-level catalog. The per-experiment task-store-server (chapters 04, 05, 07, 08) is the source of truth for the *contents* of each experiment — its tasks, ideas, variants, events, workers, groups. These are kept distinct:

- The control plane does NOT replicate task-store-server data. A `read_experiment_metadata` call returns registry-level fields only; UIs that want task counts call the task-store-server's [`07-wire-protocol.md`](07-wire-protocol.md) §2 / §3 / §4 endpoints once per experiment.
- The task-store-server's authoritative `experiment.state` is mirrored into the control plane's `last_known_state` per §3, with a bounded staleness window.
- A deployment MAY run the control plane and task-store-server against the same physical Postgres instance with distinct schemas; the reference impl keeps the schemas separate.

## 3. State synchronization

### 3.1 The mirrored projection

The control plane's `last_known_state` field is a **cache** of the task-store-server's authoritative `experiment.state` ([`02-data-model.md`](02-data-model.md) §2.5). The control plane MUST NOT use `last_known_state` as a write target for state transitions: the `running → terminated` transition is committed by the task-store-server's `terminate_experiment` op ([`04-task-protocol.md`](04-task-protocol.md) §8) atomically with an `experiment.terminated` event ([`05-event-protocol.md`](05-event-protocol.md) §3.4), and the control plane observes the result via the sync mechanism below.

### 3.2 Pull-based sync

The control plane MUST run a periodic poller that, at a deployment-configured interval (default: 30 seconds), iterates every registered experiment and calls `read_experiment` ([`07-wire-protocol.md`](07-wire-protocol.md) §14.3) on the task-store-server. For each experiment:

- If the response carries `state == "terminated"` and the registry's `last_known_state` is `"running"`, the control plane MUST update the registry row atomically. The control plane MUST NOT emit any event for this update: it is a cache refresh, not a state transition.
- If the response carries `state == "running"`, no change.
- If the call raises a transport error or returns 5xx, the control plane MUST increment a per-experiment consecutive-failure counter. A successful read resets the counter to zero.

### 3.3 On-demand sync at lease acquisition

The `acquire_lease` op (§4.5) MUST trigger a one-shot state refresh for the target experiment before returning. This guarantees that a freshly-leased experiment has up-to-date `last_known_state` regardless of the polling cadence — bounded by one round-trip to the task-store-server, not by one polling interval.

### 3.4 Bounded staleness, operator-visible warning

The pull-based sync mechanism produces eventually-consistent `last_known_state` with a staleness window bounded by the polling interval (default 30s). For operator-facing display this is acceptable; for the `unregister_experiment` precondition (§2.2) it MAY require an operator to wait one polling interval after termination before unregistering.

When the per-experiment consecutive-failure counter reaches a deployment-configured threshold (default: 10) — i.e. once `consecutive_failures >= threshold` — the control plane MUST surface an operator-visible warning in `read_experiment_metadata`'s response via a `warnings` array. Typical entry shape: `"state-sync-stale: last successful read at <timestamp>"`. The warning is informational; the registry entry remains queryable and the existing `last_known_state` is returned unchanged.

The control plane does NOT terminate the lease, fail subsequent reads, or unregister the experiment on persistent sync failure. The task-store-server's outage is a deployment incident; the control plane's role is to surface it, not to escalate.

A future spec lineage MAY define a push-based sync (the task-store-server notifies the control plane on every state transition with explicit ack); v0 is pull-only.

## 4. Leases

### 4.1 Purpose

A lease is a per-experiment, time-bounded ownership claim. The lease holder is authorized to run the five orchestrator decisions ([`03-roles.md`](03-roles.md) §6.2) for the leased experiment; a non-holder MUST NOT.

Leases serve two operational goals:

- **Concurrency safety.** With N orchestrator replicas and M experiments, each experiment has exactly one replica running its decision loop at any instant. Replicas without a lease for a given experiment sit idle relative to that experiment.
- **High availability.** A failed lease holder's lease eventually expires, at which point another replica acquires it. The hand-off is bounded by the lease duration (default 30s); the §5 hand-off contract pins safe behavior during the brief window when both the old and new holder might be running decisions.

### 4.2 Fields

```text
ExperimentLease {
  lease_id          : string      (opaque; assigned by control plane)
  experiment_id     : string      (opaque exp_*; matches the registry entry)
  holder            : worker_id   (opaque wkr_*; the orchestrator replica's deployment-scoped worker_id)
  holder_instance   : string      (per-process UUID; §4.7)
  acquired_at       : timestamp   (when this lease was issued)
  expires_at        : timestamp   (acquired_at + lease_duration initially)
  renewed_at        : timestamp   (most recent successful renew, or acquired_at if never renewed)
}
```

`experiment_id` and `holder` are opaque, system-minted ids (the `exp_*` and `wkr_*` grammars of [`02-data-model.md`](02-data-model.md) §1.6); `lease_id` / `holder_instance` are opaque control-plane-internal values.

The schema is [`schemas/lease.schema.json`](schemas/lease.schema.json).

### 4.3 Lease duration

A deployment-wide `lease_duration` parameter controls how long a lease is valid before it expires. The protocol does not prescribe a value; the reference impl defaults to 30 seconds. A lease's `expires_at` is `acquired_at + lease_duration` initially and `now + lease_duration` after each renewal. Changing `lease_duration` requires restarting the control plane; live reconfiguration is out of scope for v0.

The control plane is the source of truth for lease timestamps. A renewal response carries the control-plane-computed `expires_at`; the orchestrator MUST use that value, not its locally-computed `now + lease_duration`. This defends against clock skew between replicas and the control plane.

### 4.4 Active vs. expired

A lease is **active** when `expires_at >= now` (the control plane's `now`). A lease is **expired** when `expires_at < now`.

The registry entry's `lease` field (§2.1, [`02-data-model.md`](02-data-model.md) §2.6) exposes the **currently-active lease only**: when no active lease exists for the experiment, the field MUST be `null` regardless of whether an expired lease row is still stored. The control plane does NOT delete expired lease rows unilaterally — the `acquire_lease` op (§4.5) atomically replaces an expired row with a fresh one — but expired rows MUST NOT surface through `read_experiment_metadata` / `list_experiments`. This rule keeps clients (the web-ui, the orchestrator's planning, third-party tooling) from treating an experiment as actively-leased while store-side operations would happily proceed against it.

The "at most one active lease per experiment" invariant is the load-bearing contract: at every wall-clock instant, an experiment has zero or one active lease. The control plane MUST serialize concurrent `acquire_lease` calls against the same experiment such that exactly one wins.

### 4.5 Operations

- `acquire_lease(experiment_id, holder, holder_instance)` — worker-gated; caller MUST be in the deployment-scoped reserved-name `orchestrators` group (§6 below), resolved by name to its system-minted `grp_*` id; the `holder` field MUST equal the authenticated opaque `worker_id`. Atomically:
  - If no lease exists for the experiment OR the current lease is expired (`expires_at < now`), creates a new lease record (replacing any expired one), returns the new lease.
  - Otherwise, returns 409 `eden://error/lease-held-by-other`.
  Also triggers an on-demand state refresh per §3.3.
- `renew_lease(lease_id, holder_instance)` — worker-gated; caller MUST be the lease's current `holder`. Atomically:
  - If the lease exists AND has not been replaced (the stored `lease_id` matches) AND the stored `holder_instance` matches the caller's, updates `expires_at = now + lease_duration` and `renewed_at = now`, returns the renewed lease.
  - If the stored `lease_id` no longer matches (a replacement happened), returns 410 `eden://error/lease-not-held`.
  - If the lease exists but is expired (the holder's renew was too late and no replacement has happened yet), returns 410 `eden://error/lease-expired`. The two 410 codes are distinct: `lease-not-held` means the lease has been replaced; `lease-expired` means it has lapsed but is still nominally the caller's.
  - If the stored `holder_instance` differs from the caller's, returns 409 `eden://error/lease-instance-mismatch`. See §4.7.
- `release_lease(lease_id, holder_instance)` — worker-gated; caller MUST be the lease's current `holder`. Atomically deletes the lease record. Mismatched `holder_instance` returns 409 `eden://error/lease-instance-mismatch`. Idempotent: releasing an already-released lease (`lease_id` not found) returns 200 with no state change.
- `list_active_leases(holder)` — read; caller MUST be authenticated as `holder` OR be the admin principal. Returns every active lease whose `holder` field equals the argument. The orchestrator's startup duplicate-`worker_id` probe (§4.7) consumes this op.

### 4.6 Concurrency

The control plane MUST enforce the §4.4 "at most one active lease per experiment" invariant under concurrent `acquire_lease` calls. The reference impl uses a Postgres `INSERT … ON CONFLICT (experiment_id) DO UPDATE SET … WHERE leases.expires_at < EXCLUDED.acquired_at` shape, which atomically replaces an expired lease or fails on an active one under PRIMARY KEY constraint plus SERIALIZABLE isolation. A different backend MAY use a different mechanism; the invariant is the contract.

### 4.7 Holder-instance fencing

The `worker_id` field identifies the *replica's registered identity*; it does NOT uniquely identify a *running process*. Two replicas misconfigured to share a `worker_id` would otherwise appear to the control plane as a single principal — each could renew the same lease, each could release it, and each could "recover" the other's leases after a notional restart.

To prevent this, every orchestrator process MUST generate a fresh `holder_instance` UUID at startup and supply it on `acquire_lease`. The control plane stores it on the lease record. `renew_lease` and `release_lease` MUST verify that the caller's `holder_instance` matches the stored value; a mismatch returns 409 `eden://error/lease-instance-mismatch`.

Effects of the fence:

- A second process started with the same `worker_id` calls `acquire_lease` with a fresh `holder_instance`. The control plane observes the active lease with a *different* `holder_instance` and returns 409 `lease-held-by-other`. The second process cannot acquire (correct).
- On lease expiration, either process MAY re-acquire (the acquire-over-expired path); whichever succeeds gets the lease with its own `holder_instance`. The other process cannot renew with its old `holder_instance` even if it observes the lease record's `lease_id` is unchanged from the prior generation — the `holder_instance` mismatch fires.
- On restart of the original process, it generates a NEW `holder_instance`. Its old leases are stranded under the old `holder_instance`; those leases naturally expire per §4.4. The recovered process acquires fresh leases under its new instance UUID after expiration.

The runtime defense is the orchestrator's startup probe (§5.2): before claiming leases, the orchestrator queries `list_active_leases(holder=self.worker_id)` and, if any returned lease has a different `holder_instance`, MUST exit with a non-zero status rather than proceed. The control plane is one half of the fence; the orchestrator's startup check is the other.

## 5. Orchestrator interaction

### 5.1 Lease ownership invariant

An orchestrator replica MUST NOT run any of the five [`03-roles.md`](03-roles.md) §6.2 decisions (termination, ideation-task creation, execution-task dispatch, evaluation-task dispatch, integration) for an experiment unless it currently holds an active lease for that experiment. The lease check MUST happen at the start of every iteration; a stale lease check is not sufficient.

A non-holder running a decision is a protocol violation. The downstream task-store-server's same-value idempotency ([`07-wire-protocol.md`](07-wire-protocol.md) §5) and exact-idempotent decision invariants ([`03-roles.md`](03-roles.md) §6.4) provide a safety net during the brief hand-off window (§5.3), but they do not authorize a non-holder to run decisions outside that window.

### 5.2 Startup flow

An orchestrator replica's startup sequence:

1. Generate `self.holder_instance = uuid4()`.
2. Authenticate against the control plane as `worker_id`.
3. Call `list_active_leases(holder=self.worker_id)`. If any returned lease's `holder_instance != self.holder_instance` and `expires_at >= now`, another live replica is misconfigured to share this `worker_id`. The orchestrator MUST exit non-zero rather than proceed.
4. Begin the acquisition + renewal threads (§5.3).

The probe is a runtime defense; the control plane's `lease-instance-mismatch` rejection (§4.7) is the per-op defense. Both are required.

### 5.3 Acquisition and renewal

An orchestrator runs two background concerns alongside its dispatch loop:

- An **acquisition** concern (e.g., a periodic poll): every `poll_interval`, iterate `list_experiments()` and call `acquire_lease(experiment_id, holder, holder_instance)` for every experiment not in the orchestrator's owned set AND not in its drained-terminated set (§5.5). On 409 `lease-held-by-other`, no-op. On success, add to the owned set.
- A **renewal** concern: every `lease_duration / 3` (with default `lease_duration=30s`, every 10s), iterate the owned set and call `renew_lease(lease_id, holder_instance)`. On 410 `lease-not-held` or `lease-expired` or 409 `lease-instance-mismatch`, remove the experiment from the owned set. On transport error, log a warning and apply the self-fence rule below.

**Self-fence under control-plane partition.** If the orchestrator cannot reach the control plane for `lease_duration` consecutive seconds (i.e., its `now - last_successful_renew >= lease_duration` for a held lease), it MUST drop the experiment from its owned set even without observing a `lease-not-held`. The control plane will issue the lease to another replica after `lease_duration` of non-renewal; the orchestrator MUST stop dispatching within the same window to bound the split-brain interval.

### 5.4 Hand-off semantics under expiration

When replica A's lease expires (A crashes, hangs, or partitions), replica B's acquisition thread eventually attempts `acquire_lease` for the same experiment and succeeds. From the moment B's lease is granted:

- B MAY run all five [`03-roles.md`](03-roles.md) §6.2 decisions for the experiment.
- A (if still alive) attempts to renew; receives 410 `lease-not-held` or 409 `lease-instance-mismatch`; drops the experiment.
- A partitioned A self-fences per §5.3 after `lease_duration` of consecutive failures.
- The hand-off window between A's lease expiring and A observing the failure is bounded by `lease_duration` worst-case.

During the hand-off window, both A and B MAY attempt the same decision. The [`03-roles.md`](03-roles.md) §6.4 safety classes resolve the race:

- The four **exact-idempotent** decisions (termination, execution-task dispatch, evaluation-task dispatch, integration) collapse to a single committed outcome per the task-store-server's idempotency contracts. No data corruption.
- The one **bounded-overshoot** decision (ideation-task creation) MAY produce up to `N * T` ideation tasks where `N` is the number of replicas racing and `T` is the policy target; subsequent iterations self-correct downward as the bounded-overshoot policy reads the pending count. One-iteration overshoot is the documented bound.

This is the same race [`03-roles.md`](03-roles.md) §6.4 describes; the lease primitive makes it rare-by-construction (only during hand-offs) without changing the underlying safety classes.

### 5.5 Lease release after termination drain

Per [`02-data-model.md`](02-data-model.md) §2.5 and [`03-roles.md`](03-roles.md) §6.2 decision-type 4, a terminated experiment's loop runs only the integration decision until no `status == "success"` variants without `variant_commit_sha` remain (the integration drain).

After the drain completes for an experiment whose `last_known_state == "terminated"`, the lease holder MUST:

1. Call `release_lease(lease_id, holder_instance)`.
2. Add the experiment id to a local in-memory "drained-terminated" set. The acquisition thread MUST consult this set and skip re-acquiring the lease for experiments in it.

The drained-terminated set is process-local: it is cleared on orchestrator restart, on which the orchestrator re-discovers terminated-and-drained experiments by reading their `last_known_state` and observing no eligible integration work. The set exists only to bound the orchestrator's polling cost in the common case where an unrelated `unregister_experiment` op has not yet removed the registry entry.

The post-drain release unblocks the §2.2 `unregister_experiment` precondition ("no active lease") for drained terminated experiments without operator intervention.

### 5.6 Shutdown

On graceful shutdown (SIGTERM), the orchestrator MUST `release_lease` for every held lease before exiting. Failures during shutdown release are logged but not retried; the lease's `expires_at` is the backstop.

## 6. Deployment-scoped worker registry

The chapter 02 §6 worker registry is **per-experiment**: a `worker_id` is unique within an experiment, not deployment-wide. A control plane needs **deployment-scoped** identity for orchestrator replicas: a single replica MAY hold leases across multiple experiments, so its identity MUST transcend any single experiment.

The control plane therefore maintains its own deployment-scoped worker registry, distinct from the per-experiment registries hosted by the task-store-server. The registry's shape and operations mirror chapter 02 §6 / §7 and [`07-wire-protocol.md`](07-wire-protocol.md) §6 / §7 verbatim — `register_worker` / `register_group` **mint** opaque `wkr_*` / `grp_*` ids ([`02-data-model.md`](02-data-model.md) §1.6), take optional display names ([`02-data-model.md`](02-data-model.md) §1.7), expose the `?name=` lookup, and enforce reserved **names** (rejecting with 409 `reserved-identifier` / 422 `invalid-name`) — with the following differences:

- The registry is **deployment-scoped**: `worker_id` is unique across the deployment by construction (system-minted), not within an experiment. Wire paths under [`07-wire-protocol.md`](07-wire-protocol.md) §15 are deployment-rooted (`/v0/control/workers/...`), not experiment-rooted.
- The group registry is **deployment-scoped** identically. The reserved group **names** from [`02-data-model.md`](02-data-model.md) §7.5 (`admins`, `orchestrators`) apply at the deployment scope; these reserved groups are minted at control-plane bootstrap with system-minted `grp_*` ids whose `name` equals the reserved literal, resolved by name. The per-experiment registries hosted by the task-store-server have their own (independent) reserved-name groups.
- The authority model: `register_worker` / `reissue_credential` / `register_group` / `add_to_group` / `remove_from_group` / `delete_group` are admin-gated. `verify_worker_credential` (the deployment-scoped `whoami` probe) is worker-gated. `list_workers` / `read_worker` / `list_groups` / `read_group` are admin-gated.

The control plane's `acquire_lease` / `renew_lease` / `release_lease` ops authenticate against the deployment-scoped registry. The task-store-server's per-experiment ops authenticate against the per-experiment registry. An orchestrator replica that holds leases for K experiments therefore has K+1 credentials: one deployment-scoped credential for control-plane ops, plus one per-experiment credential for each task-store-server it touches. The two credential domains are independent — rotating one does NOT invalidate the other.

The double-registration model is unfortunate complexity. A future spec lineage MAY widen the chapter 02 §6 model to support deployment-scoped workers natively, at which point the control plane's separate registry could collapse into the task-store-server's. v0 keeps them separate to avoid breaking the chapter 02 §6 / §7 contracts.

## 7. Experiment registration on import

The portable-checkpoint import op ([`10-checkpoints.md`](10-checkpoints.md) §7 / [`07-wire-protocol.md`](07-wire-protocol.md) §14.2) creates an experiment in the task-store-server, minting (or accepting the `as_experiment_id` override for) the imported experiment's opaque `exp_*`. In a deployment that runs the control plane, the import handler MUST ALSO create the registry entry for that **same** `exp_*` after the task-store-server commit succeeds — the registry entry adopts the import-minted id rather than minting a second one, so the control-plane entry and the task-store experiment share one identity.

The two ops are NOT atomic across the two services; true two-phase commit across independent Postgres connections is out of scope for v0. The import handler treats control-plane registration failure as a **partial success**:

- The data is committed in the task-store-server (per the existing [`10-checkpoints.md`](10-checkpoints.md) §7 atomicity).
- The control plane registry entry is absent.
- The import response's `warnings` array MUST include an entry naming the registration failure with operator-actionable text (e.g., `"control-plane-register-failed: <error detail>; recover with POST /v0/control/experiments"`).

The operator's recovery is an explicit `POST /v0/control/experiments` carrying the import-minted experiment id from the warning (the registry entry adopts that id; the control-plane create accepts the already-allocated `exp_*` for this import-recovery path). Without recovery, the imported experiment exists in the task-store-server but is invisible to `list_experiments`, so orchestrator replicas will not acquire leases for it. This is a deliberate weak-contract choice; the alternative (block the import on control-plane availability) was rejected because it couples export/import to control-plane uptime more tightly than necessary.

A natively-created experiment follows the inverse order: the operator calls `register_experiment(name?, config_uri)`, the control plane mints the `exp_*`, and the operator then drives task-store-server population (`create_task(kind=ideation)`, etc.) against that minted id.

## 8. Implementation latitude

The protocol leaves to implementations:

- The concrete HTTP server framework (FastAPI, Starlette, bespoke).
- The storage backend (the reference impl uses a separate Postgres schema; an alternative MAY use a different physical database, an in-memory store for ephemeral deployments, or a shared schema with the task-store-server).
- The polling interval for §3 state sync (the reference impl uses `EDEN_STATE_SYNC_INTERVAL_SECONDS`, default 30s).
- The consecutive-failure threshold for §3.4 stale-warning emission (the reference impl uses `EDEN_STATE_SYNC_FAILURE_THRESHOLD`, default 10).
- The reference impl's choice to run the control plane as a single replica in v0 (deployment-level SPOF). HA for the control plane itself uses the same lease pattern recursively and is deferred to a later phase.
- The orchestrator's choice of polling interval, acquisition strategy (greedy vs. deterministic sharding — the reference impl uses greedy per the design notes), and active vs. stand-by replica accounting.

The protocol does NOT leave to implementations:

- The §4.4 active-vs-expired contract and the at-most-one-active-lease invariant.
- The §4.7 holder-instance fencing on `renew_lease` / `release_lease`.
- The §5.1 lease-ownership requirement that gates the [`03-roles.md`](03-roles.md) §6.2 decisions.
- The §5.3 self-fence rule under control-plane partition.
- The §5.5 release-after-drain requirement.
- The §6 deployment-scoped worker registry separate from [`02-data-model.md`](02-data-model.md) §6's per-experiment registry.
- The §7 partial-success contract on import.

## 9. Future amendments

The following are deliberate non-goals for v0; a future spec lineage MAY add them:

- **Per-decision leases.** v0 leases are per-experiment; a future amendment MAY scope leases to individual decision types (e.g., one replica for ideation, another for integration).
- **Lease pre-emption.** v0 has no admin-driven force-release of an active lease; a replica MUST wait for expiration. An admin force-release MAY land later.
- **Hot reconfiguration of `lease_duration`.** v0 requires a control-plane restart.
- **Push-based state sync.** v0 is pull-only; a future amendment MAY define a task-store-server-to-control-plane push protocol.
- **Cross-deployment leases / federation.** v0 leases are per-deployment.
- **Control-plane HA.** v0 is a single replica.
- **Active load rebalancing.** v0 acquisition is greedy ("first replica to ask wins, others stand by"); a future amendment MAY add lease-stealing for fairness.
- **Bulk aggregation reads.** Operator UIs that want "all variants across all experiments" iterate `list_experiments` and call per-experiment reads against the task-store-server; v0 has no `/v0/aggregate/...` shortcut.
