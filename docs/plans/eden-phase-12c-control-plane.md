# Phase 12c — Control plane

**Status.** Draft.

**Predecessors.** Phase 12a (PRs #57, #61, #62) and Phase 12b
(PR #63) all merged as plans. 12c assumes the data shapes those
plans landed: workers and groups are first-class (12a-1), the
orchestrator is a 5th role with `dispatch_mode`-gated decisions
(12a-2), `experiment.state` is a runtime field with a
`running → terminated` lifecycle (12a-3), and experiments are
exportable / importable as portable checkpoints (12b).

**Roadmap.** [`docs/roadmap.md`](../roadmap.md) §"Phase 12 —
Multi-experiment" enumerates the scope: control plane service +
lease data model; multi-replica orchestrator with HA + chaos test;
cross-experiment views in the shared ideator; experiment switcher
in the Web UI; multi-experiment conformance scenarios.

**Naming.** Pre-draft check against
[`docs/glossary.md`](../glossary.md) and AGENTS.md "Naming discipline":

- "Control plane" is the canonical noun for the cross-experiment
  coordination layer. The verb is "register an experiment with
  the control plane"; the operator-facing UI affordance is
  "experiment switcher".
- "Lease" is the canonical noun for a time-bounded ownership
  claim. Operators "acquire", "renew", and "release" leases.
  `lease_id` and `lease_expires_at` are the two persistent
  fields.
- "Owning orchestrator" or "lease holder" denotes the
  orchestrator replica currently holding a lease for a given
  experiment. No collision with 12a-2's "orchestrators" group
  (which is the deployment-level group of all orchestrator
  workers); the lease holder is one specific member of that
  group, scoped per-experiment-per-instant.

## 1. Context

Today the reference impl runs one task-store-server, one
orchestrator, and one set of worker hosts — all bound to a
single experiment via `--experiment-id`. Two scaling pressures
push beyond that:

1. **Multiple experiments.** A research team wants to run
   experiments A, B, C concurrently. Today, that means three
   independent deployments (three Compose stacks, three Postgres
   schemas, three Forgejo instances). The operational overhead is
   linear in experiment count.

2. **Multi-replica orchestrator with HA.** A single orchestrator
   process is a SPOF for any one experiment. 12a-2's idempotent-
   decisions model permits N replicas per experiment, but it
   doesn't address *which* replica should make decisions when
   multiple are running — they all run the loop and rely on Store
   CAS to deduplicate. That works for a small N but doesn't
   scale: every replica reads the full task list, every replica
   computes the dispatch decisions, every replica's write
   collides with every other replica's. CPU and Store-read load
   scales linearly with replica count even though only one
   replica's writes ever commit.

12c addresses both with a **control plane** that:

- Tracks the deployment's experiment registry (each experiment's
  id, config, lifecycle state, owning lease).
- Issues **leases** to orchestrator replicas. Each experiment has
  at most one lease holder at any moment; the holder runs the
  full dispatch loop for that experiment. Other replicas wait or
  hold leases for other experiments.
- Surfaces cross-experiment data: aggregated dashboards, an
  experiment switcher, and multi-experiment conformance.

The 12a-2 idempotent-decisions safety net stays in place — it
catches the hand-off race when a lease expires and a new replica
takes over. Normal-case operation has a single owner per
experiment.

### 1.1 Spec baseline + reconciliation

12c introduces normative surface for the deployment-level
control plane. The current spec has experiment-scoped operations
(every wire path is `experiments/{id}/...` per chapter 07 §1.3,
modulo 12b's `/v0/checkpoints/` carve-out). 12c adds a second
non-experiment-scoped path family: `/v0/control/` for control-
plane ops.

| Existing site | Current text | 12c disposition |
|---|---|---|
| [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §2.5 (12a-3) | Experiment runtime state: `experiment_id`, `state`, `created_at` (+ 12b's `imported_from`) | Add `lease: ExperimentLease \| null` field. The lease shape is defined alongside in §2.6 (new). |
| [`spec/v0/03-roles.md`](../../spec/v0/03-roles.md) §6 (12a-2) | Orchestrator is the 5th role with four operational decisions + termination decision (12a-3) | Add §6.5 "Lease ownership": each iteration starts with a lease check; if the orchestrator does not hold the lease for the target experiment, it MUST NOT run any of the five decisions. |
| [`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §1.3 + 12b's carve-out for `/v0/checkpoints/` | Path-scoping rules with one exception | Extend §1.3 carve-out to include `/v0/control/`. The control-plane path family is also non-experiment-scoped; the experiment_id is in the request body or implicit (e.g., listing all experiments). |
| [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §3 conformance levels | v1, v1+roles, v1+roles+integrator, v1+checkpoints (12b) | Add fifth level: `v1+multi-experiment`. The level adds the control-plane endpoints + lease-handoff scenarios. |
| New chapter (proposed §11) | n/a | "Control plane" — full normative spec for experiment registry, leases, lease lifecycle, lease-handoff semantics, cross-experiment data aggregation. |

Reconciliation rule: chapter 11 is the canonical spec for
control-plane behavior; chapters 02 / 03 / 07 / 09 cross-
reference it without restating the contract.

### 1.2 Naming-discipline baseline

PR #60's strengthened guardrail applies. The new "lease" and
"control plane" terms don't collide with existing identifiers.
The endpoint root `/v0/control/` is added to the glossary
alongside `/v0/checkpoints/` (the existing 12b carve-out).

## 2. Decisions

These are the load-bearing design calls; §3 unpacks each.

1. **The control plane is a new reference service, not a
   sidecar inside an existing service.** A new
   `eden-control-plane` service exposes the experiment registry
   and lease ops. It runs as a single replica in the v0 reference
   deployment (deployment-level SPOF; HA for the control plane
   itself is deferred to a later phase).

2. **Leases are per-experiment, time-bounded, renewable.** Each
   experiment has at most one active lease at any moment. Lease
   holders heartbeat to extend; if a lease expires (no
   heartbeat), another replica may acquire it. The model is
   directly inspired by Kubernetes `coordination.k8s.io/v1`
   Lease objects.

3. **The orchestrator's loop is gated on lease ownership.**
   Every iteration starts with a lease check. If the
   orchestrator doesn't hold the lease for a given experiment,
   it MUST skip all five decisions for that experiment in that
   iteration. The 12a-2 idempotent-decisions model is the
   safety net for the hand-off race (the brief window between
   one replica's lease expiring and another's acquiring it).

4. **The control plane does NOT proxy task/idea/variant ops.**
   Worker hosts, the orchestrator, and the Web UI continue to
   talk to the task-store-server directly for those ops. The
   control plane's only responsibilities are: experiment
   registry, lease management, and aggregated cross-experiment
   reads. Adding a proxy would centralize traffic that doesn't
   need centralizing.

5. **Experiment registry is the cross-experiment source of
   truth.** Today, an experiment's existence is implicit in the
   task-store-server's data. Post-12c, the control plane
   maintains an explicit registry: `(experiment_id, config,
   created_at, last_known_state)` rows. The task-store-server
   still owns the per-experiment task/idea/variant data; the
   control plane owns the *which-experiments-exist* metadata.

6. **Cross-experiment views are read-only aggregations.** The
   Web UI's experiment-switcher, dashboard, and "all
   experiments" views read from the control plane. Mutations
   (create variant, etc.) go to the task-store-server as
   before. The split keeps the control plane's load
   proportional to operator-driven reads, not worker-driven
   writes.

7. **Multi-replica orchestrator behavior — greedy with
   stand-bys** (the 12a-2 multi-instance HA model is reframed
   as redundancy, not as parallelism). Replicas no longer race
   per-experiment; instead, the first replica to call
   `acquire_lease` for an experiment wins, others wait on
   stand-by. A deployment with N orchestrator replicas + M
   experiments may end up with one replica holding all M
   leases and N-1 replicas idle — that's HA via redundancy. If
   the active replica fails, a stand-by acquires the orphaned
   leases. Active load-rebalancing across replicas (so all N
   share the M-leases workload) is a future improvement;
   v0 ships greedy-with-stand-bys.

8. **Lease scoping**. Leases are PER experiment, not per
   decision type. A single lease grants the holder authority
   over all five operational decisions (`ideation_creation`,
   `execution_dispatch`, `evaluation_dispatch`, `integration`,
   `termination`) for that experiment. Per-decision-type leases
   would be more flexible but add complexity for no v0 use case.

9. **Checkpoint import auto-registers with the control plane —
   best-effort, not atomic.** 12b's
   `POST /v0/checkpoints/import` creates the experiment in the
   task-store-server. 12c extends the import flow to ALSO call
   `control_plane.register_experiment` after the
   task-store-server commit succeeds. The two operations are
   NOT atomic across the two services (true 2PC across
   independent Postgres connections is heavy and out of scope
   for v0); the import handler treats control-plane
   registration failure as a recoverable partial success per
   12b's existing `warnings` array. The operator's recovery is
   `POST /v0/control/experiments` with a body carrying the
   experiment_id from the partial-success warning. Without
   recovery, the imported experiment exists in the
   task-store-server but is invisible to `list_experiments`
   and therefore invisible to lease acquisition — orchestrator
   replicas will not pick it up. This is a deliberate weak-
   contract choice; the alternative (block the import on
   control-plane availability) was rejected because it
   couples export/import to control-plane uptime more
   tightly than necessary.

10. **The control plane has no orchestrator-decision authority
    of its own.** It is purely a registry + coordination layer.
    All decisions still happen in orchestrator replicas
    (running 12a-2's five-decision loop). The control plane
    does NOT, e.g., issue dispatch commands; it only assigns
    leases and lets the lease-holder do its job.

11. **One multi-experiment task-store-server in v0**
    (data-plane topology). The current task-store-server is
    constructed with a single `experiment_id` per
    [`reference/services/task-store-server/src/eden_task_store_server/app.py`](../../reference/services/task-store-server/src/eden_task_store_server/app.py).
    12c extends the Store Protocol + backends to be
    multi-experiment-aware: the task-store-server hosts ALL of
    a deployment's experiments behind one HTTP endpoint, and
    the wire layer's existing `experiments/{id}/...` path
    structure routes per-call. The control plane's
    `config_uri` field (§3.1 below) names the
    *experiment-config resource*, not the task-store-server
    URL — there is one task-store-server URL deployment-wide.
    A future amendment MAY add per-experiment task-store-server
    sharding (the registry would carry the URL); v0 stays
    single-deployment-single-task-store-server.

12. **Lease ops authenticate as the orchestrator worker, not
    the deployment admin.** Workers in 12a-1 are
    *per-experiment*: a `worker_id` is unique within an
    experiment, not deployment-wide. 12c needs
    deployment-scoped identity for orchestrator replicas (a
    single replica may hold leases for multiple experiments,
    so its identity must transcend any single experiment).
    The control plane therefore maintains its OWN
    deployment-scoped worker registry, distinct from the
    per-experiment registries in the task-store-server. This
    separate registry lives in the control plane's Postgres
    schema (§3.4); registration is admin-token-gated. An
    orchestrator replica authenticates against the control
    plane using its deployment-scoped credential and against
    the task-store-server using its per-experiment credentials
    (one credential per experiment it holds a lease for, all
    minted from the same admin bootstrap).

    Authority rules on the control plane:
    - `acquire_lease`: caller MUST be in the deployment-
      scoped `orchestrators` group; `holder` field MUST equal
      the authenticated deployment-scoped `worker_id` (no
      impersonation).
    - `renew_lease`: caller MUST be the lease's current
      `holder`.
    - `release_lease`: caller MUST be the lease's current
      `holder`.
    - `register_experiment` / `unregister_experiment`:
      admin-token only.
    - `list_experiments` / `read_experiment_metadata`:
      authenticated deployment-scoped worker OR admin-token.
    - `register_worker` / `add_to_group` (deployment-scoped):
      admin-token only.
    The orchestrator's startup flow: register a
    deployment-scoped worker (admin-token) → join the
    deployment-scoped `orchestrators` group (admin-token) →
    for each experiment it acquires a lease for, also
    register a per-experiment worker (admin-token, mints a
    per-experiment credential) → use the per-experiment
    credential for task-store-server calls about that
    experiment, and the deployment-scoped credential for all
    control-plane calls.

    The double-registration is unfortunate complexity but
    follows from 12a-1's per-experiment-worker design. A
    future amendment MAY collapse the two if 12a-1's worker
    model is widened to support deployment-scoped workers
    natively; v0 keeps them separate to avoid breaking 12a-1's
    existing contract.

## 3. Design

### 3.1 Spec chapter 11 — control plane

Sketch:

> ### (Proposed Chapter 11) Control plane
>
> #### 11.1 Purpose
>
> Coordinate multi-experiment deployments. The control plane
> tracks the deployment's experiment registry and issues leases
> that grant orchestrator replicas exclusive ownership of an
> experiment's dispatch loop.
>
> #### 11.2 Experiment registry
>
> The control plane maintains an experiment registry. Each
> entry has fields:
>
> - `experiment_id`
> - `config_uri` — URI pointing at the experiment-config
>   resource (typically a path served by the task-store-server)
> - `created_at`
> - `last_known_state` — `running` or `terminated`; mirrors the
>   task-store-server's authoritative `experiment.state` value
>   per the §3.4a sync mechanism below. Mirrors the
>   task-store-server's `experiment.state` (eventually
>   consistent; the task-store-server's value is authoritative)
> - `lease` — current `ExperimentLease | null`
>
> The registry is mutable on these ops:
>
> - `register_experiment(experiment_id, config_uri)` — admin
>   creates a new entry. Returns the entry.
> - `unregister_experiment(experiment_id)` — admin removes
>   an entry. Allowed only when `last_known_state ==
>   "terminated"` AND no active lease exists.
> - `list_experiments(filter?)` — read.
> - `read_experiment_metadata(experiment_id)` — read.
>
> #### 11.3 Lease data model
>
> ```text
> ExperimentLease {
>   lease_id: string         (opaque; assigned by control plane)
>   experiment_id: string
>   holder: worker_id        (the orchestrator replica's worker_id)
>   holder_instance: string  (per-process UUID generated at
>                             startup by the orchestrator process;
>                             defends against duplicate worker_id
>                             across replicas — see §11.6)
>   acquired_at: timestamp
>   expires_at: timestamp    (acquired_at + lease_duration)
>   renewed_at: timestamp    (last successful renew)
> }
> ```
>
> A lease's `expires_at` is `acquired_at + lease_duration`
> initially, where `lease_duration` is a deployment-wide config
> parameter (default: 30s). Renewals reset `expires_at` to
> `now + lease_duration`.
>
> Lease lifecycle:
>
> - `acquire_lease(experiment_id, holder, holder_instance)` —
>   creates a lease. Succeeds iff no active lease exists for
>   the experiment OR the existing lease's `expires_at < now`.
>   Returns the new lease. Atomic.
> - `renew_lease(lease_id, holder_instance)` — extends
>   `expires_at` to `now + lease_duration`. Succeeds iff the
>   lease is the currently-active one AND
>   `holder_instance` matches the stored value. Atomic.
> - `release_lease(lease_id, holder_instance)` — explicitly
>   relinquishes the lease. Same `holder_instance` check as
>   renew. Used at orchestrator shutdown. Atomic.
> - Implicit expiration: if `expires_at < now`, the lease is
>   "expired" but not deleted; it occupies the experiment's
>   `lease` field until acquired by another replica (which
>   transitions the lease to a new holder atomically).
>
> #### 11.4 Lease ownership invariant
>
> At any wall-clock instant, an experiment has at most one
> *active* lease. "Active" means `expires_at >= now`. Multiple
> orchestrator replicas may attempt `acquire_lease` for the
> same experiment simultaneously; the control plane MUST
> serialize them and let exactly one succeed.
>
> An orchestrator replica MUST NOT run any of the five decision
> types for an experiment unless it currently holds an active
> lease for that experiment.
>
> #### 11.5 Cross-experiment reads
>
> The control plane exposes aggregated reads:
>
> - `list_experiments` returns all registered experiments
>   (paginated for large deployments).
> - `read_experiment_metadata` returns one experiment's
>   registry entry including its current lease.
>
> Aggregated *task/idea/variant/event* reads are NOT in the
> control plane's surface (Decision 4: no proxy). UI code that
> wants to show "all running variants across all experiments"
> calls `list_experiments` to enumerate, then calls the
> deployment's single task-store-server (per Decision 11) once
> per experiment via the multi-experiment Store factory. There
> is one task-store-server URL deployment-wide; the calls vary
> only by `experiment_id`.
>
> #### 11.6 Holder-instance fencing
>
> The `worker_id` field in a lease identifies the *replica's
> registered identity*, not its *running process*. Two
> replicas misconfigured to share a `worker_id` would otherwise
> appear to the control plane as a single principal — both
> could renew the same lease, both could release it, and both
> could "recover" it after restart. To prevent this, every
> orchestrator process generates a fresh `holder_instance` UUID
> at startup and supplies it on `acquire_lease`. The control
> plane stores it on the lease record. `renew_lease` and
> `release_lease` both require the caller's `holder_instance`
> to match the stored value; mismatch returns
> `409 eden://error/lease-instance-mismatch`. Effects:
>
> - A second process started with the same `worker_id` calls
>   `acquire_lease` with a fresh `holder_instance`. The control
>   plane sees an active lease with a *different*
>   `holder_instance` and returns `LeaseHeldByOther` — the
>   second replica cannot acquire (correct).
> - On lease expiration, either process can re-acquire (the
>   acquire-over-expired path); whichever succeeds gets the
>   lease with its own `holder_instance`. The other process
>   cannot renew the lease with its old `holder_instance`.
> - On restart of the original process, it generates a NEW
>   `holder_instance`. Its old leases are stranded under the
>   old `holder_instance` value — those leases naturally expire
>   per §11.3. The recovered process then acquires fresh leases
>   under its new instance UUID.
>
> The runtime defense is the orchestrator's startup probe (see
> §3.2 below): on startup, query the control plane for
> existing leases held by `worker_id == self.worker_id` AND
> `holder_instance != self.holder_instance` AND
> `expires_at >= now`. Any such lease indicates a duplicate
> live replica; the orchestrator MUST exit with a clear error
> rather than running.
>
> #### 11.7 Lease-release after termination drain
>
> Per 12a-3 §3.5, a `terminated` experiment's loop runs only
> the integration decision until no `status="success"`
> variants without `variant_commit_sha` remain. After the
> drain completes, the lease holder MUST `release_lease` for
> the terminated experiment, AND MUST add the experiment_id
> to a local "drained-terminated" set so the
> `new_lease_acquisition_thread` skips re-acquiring it. The
> drained-terminated set is cleared when the operator runs
> `unregister_experiment` (which removes the registry entry
> entirely) OR when a deployment-side reset happens (e.g.,
> the orchestrator process restarts with a fresh in-memory
> set; the experiment will then be skipped at first iteration
> if the drain check still passes, immediately released). This
> rule unblocks the §11.2 `unregister_experiment` precondition
> ("`last_known_state == terminated` AND no active lease") for
> drained terminated experiments without operator
> intervention.

### 3.2 Lease acquisition + renewal flow

An orchestrator replica's loop becomes:

```text
on startup:
  # worker_id is supplied per replica via --worker-id (12a-2 §3.8);
  # operators MUST give each replica a unique id. Default
  # "auto-orchestrator-1" is OK only when running a single replica.
  self.holder_instance = uuid4()  # per-process UUID; §11.6 fence
  self.drained_terminated = set()  # §11.7 skip set
  register_worker(worker_id)               # 12a-1; admin token at boot
  add_to_group(worker_id, "orchestrators") # 12a-2; admin token at boot
  # §11.6 startup fence: any active lease held by self.worker_id
  # under a DIFFERENT holder_instance means another live replica
  # is misconfigured to share our worker_id. Exit hard rather
  # than running.
  for lease in list_active_leases(holder=worker_id):
    if lease.holder_instance != self.holder_instance \
       and lease.expires_at >= now:
      log error "duplicate worker_id detected; another replica is live"
      exit(2)
  # Subsequent ops use the worker's own credential, not the admin token.
  for experiment_id in list_experiments():
    try:
      lease = acquire_lease(experiment_id, holder=worker_id,
                            holder_instance=self.holder_instance)
      claim_experiment(lease)              # adds to local owned set
    except LeaseHeldByOther:
      pass
  start_renewal_thread()
  start_dispatch_loop()

renewal_thread (every lease_duration / 3):
  for lease in self.held_leases:
    try:
      renew_lease(lease.lease_id, self.holder_instance)
      lease.last_successful_renew = now
    except LeaseNotHeld:        # we lost it (clock skew, control-plane mutation)
      drop_experiment(lease.experiment_id)
    except LeaseInstanceMismatch:  # §11.6: another replica took over with a fresh instance
      drop_experiment(lease.experiment_id)
    except TransportError:
      log warning
      # Self-fence: if we cannot reach the control plane for >= lease_duration,
      # we MUST stop dispatching even though we never got LeaseNotHeld.
      # This bounds the split-brain window during a control-plane partition:
      # the control plane will issue the lease to another replica after
      # lease_duration; we must self-stop within the same window so we
      # don't continue dispatching against an experiment another replica
      # now owns.
      if now - lease.last_successful_renew >= lease_duration:
        drop_experiment(lease.experiment_id)

dispatch_loop (every poll_interval):
  for experiment_id in self.held_experiments:
    state = read_experiment_state(experiment_id)
    if state == "terminated":
      drained = run_integration_only(experiment_id)  # 12a-3 §3.5 drain
      # §11.7: once integration drains and no success variants remain
      # without variant_commit_sha, release the lease and mark this
      # experiment as drained-terminated so the acquisition thread
      # doesn't pick it back up.
      if drained:
        try:
          release_lease(lease_for(experiment_id), self.holder_instance)
        except: pass
        self.held_experiments.remove(experiment_id)
        self.drained_terminated.add(experiment_id)
    else:
      run_orchestrator_iteration(experiment_id, ...)

on shutdown:
  for lease in self.held_leases:
    try:
      release_lease(lease.lease_id, self.holder_instance)
    except: pass

# every poll_interval, also try to acquire leases for unleased experiments:
new_lease_acquisition_thread (every poll_interval):
  for experiment_id in list_experiments():
    if experiment_id in self.held_experiments:
      continue
    if experiment_id in self.drained_terminated:
      continue  # §11.7: drained terminated; don't re-acquire
    try:
      lease = acquire_lease(experiment_id, holder=worker_id,
                            holder_instance=self.holder_instance)
      self.held_experiments.add(experiment_id)
      except LeaseHeldByOther:
        pass
```

The renewal interval (`lease_duration / 3`) gives three chances
to renew before expiration. With default `lease_duration = 30s`,
that's 10s renewals — short enough to detect failures within a
minute, long enough to avoid flooding the control plane.

### 3.3 Hand-off semantics under lease expiration

When orchestrator replica A's lease expires (e.g., A crashes,
hangs, or partitions), replica B's
`new_lease_acquisition_thread` eventually attempts to acquire
the same experiment's lease and succeeds. From the moment B's
lease is granted:

- B can run all five decisions for the experiment.
- A (if still alive) attempts to renew its lease, gets
  `LeaseNotHeld`, drops the experiment from its owned set,
  and stops running decisions. (If A is partitioned and
  cannot reach the control plane at all, A's renewal-thread
  self-fence — see the §3.2 pseudocode — drops the experiment
  after `lease_duration` of consecutive transport failures,
  bounding the split-brain window even without an explicit
  `LeaseNotHeld`.)
- The hand-off window — between A's lease expiring and A
  observing the renewal failure (or hitting the self-fence) —
  is bounded by `lease_duration` worst-case. During this
  window, both A and B might be running decisions for the
  experiment. The 12a-2 idempotent-decisions model handles
  this for the four exact-idempotent decisions
  (`execution_dispatch`, `evaluation_dispatch`, `integration`,
  termination): one CAS commit wins; the other is no-ops. The
  fifth decision (`ideation_creation`) is bounded-overshoot
  per 12a-2 §6.4, not exact-idempotent: a hand-off race may
  produce up to `N * T` ideation tasks (where `T` is the
  policy target, `N` is the count of replicas racing — usually
  2 during hand-off). The next iteration self-corrects per
  12a-2's §3.4 worked example. No data corruption; one-
  iteration overshoot is the documented bound.

This is the "rare race" 12a-2 §3.4 describes; 12c's lease model
makes it rare-by-construction (only during hand-offs), with
12a-2's exact-idempotent + bounded-overshoot guarantees as the
safety net.

### 3.4 Control plane storage

The control plane needs durable storage for the experiment
registry + active leases. Two reasonable options:

**Option A: dedicated control-plane Postgres.** New schema
`eden_control_plane` with tables `experiments` and `leases`. The
control plane service connects to this schema. Pros: separation
of concerns; control-plane traffic doesn't hit the
task-store-server's Postgres. Cons: another database to
operate.

**Option B: shared task-store-server tables.** The control
plane reads/writes via the task-store-server's wire endpoints
(new Store ops `register_experiment`, `acquire_lease`, etc.).
Pros: one Postgres instance; simpler ops. Cons: control-plane
operations contend with task-store-server transactions; the
control plane's atomicity contracts depend on the
task-store-server's.

**Decision: Option A.** The control plane's data model is
distinct from the per-experiment task/idea/variant data; mixing
them complicates both. A separate Postgres schema is cheap (one
extra logical schema, same physical Postgres instance in v0;
migrate to a separate instance if scale demands it).

The control plane Postgres schema:

```sql
CREATE TABLE control_plane_experiments (
  experiment_id text PRIMARY KEY,
  config_uri text NOT NULL,
  created_at timestamptz NOT NULL,
  last_known_state text NOT NULL CHECK (last_known_state IN ('running', 'terminated'))
);

CREATE TABLE control_plane_leases (
  experiment_id text PRIMARY KEY REFERENCES control_plane_experiments(experiment_id),
  lease_id text NOT NULL,
  holder text NOT NULL,
  holder_instance text NOT NULL,    -- §11.6 fencing: per-process UUID
  acquired_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  renewed_at timestamptz NOT NULL
);

CREATE INDEX idx_leases_expires ON control_plane_leases (expires_at);
CREATE INDEX idx_leases_holder ON control_plane_leases (holder);  -- for list_active_leases(holder=...)
```

The `experiment_id` PRIMARY KEY on `control_plane_leases`
enforces "at most one lease per experiment". `acquire_lease`
uses a single transaction with `INSERT ... ON CONFLICT (experiment_id)
DO UPDATE SET ... WHERE leases.expires_at < EXCLUDED.acquired_at`,
which atomically replaces an expired lease or fails on an
active one. Postgres SERIALIZABLE isolation + the row-level
PRIMARY KEY constraint together give the §11.4 invariant.

### 3.4a `last_known_state` sync mechanism

The control plane's `last_known_state` field is a cache of the
task-store-server's authoritative `experiment.state` (12a-3
§3.2). The sync is **pull-based** from the control plane:

- A background worker thread in the control plane service
  polls each registered experiment's task-store-server every
  `EDEN_STATE_SYNC_INTERVAL_SECONDS` (default 30s) by calling
  `read_experiment(<id>)`.
- If the task-store-server returns `state="terminated"` and the
  registry's `last_known_state` is `"running"`, the control
  plane updates the registry row atomically. No event is
  emitted at the control-plane level (it's a cache update, not
  a state transition).
- If the task-store-server returns `state="running"`, no
  change.
- Transport errors are logged + retried on the next tick;
  consecutive failures > N (default 10) trigger an
  operator-visible warning in `read_experiment_metadata`'s
  response (`{warnings: ["state-sync-stale: last successful
  read at <timestamp>"]}`).

This means `last_known_state` may lag the authoritative value
by up to one polling interval (30s). For UI dashboard purposes
this is acceptable; for `unregister_experiment`'s gating check
it means an operator may briefly need to wait after termination
before unregistering. The alternative (push from the
task-store-server on every `terminate_experiment`) would
couple the two services more tightly. Pull-based with bounded
staleness is the v0 choice; push-based with explicit ack is a
future amendment.

A second sync trigger: every `acquire_lease` call also pulls
the latest state, so a freshly-leased experiment has up-to-
date `last_known_state` regardless of polling cadence.

### 3.5 Experiment registration on checkpoint import

12b's `POST /v0/checkpoints/import` creates the experiment in
the task-store-server. 12c extends this:

- After the task-store-server side commits, the import handler
  ALSO calls `control_plane.register_experiment(experiment_id,
  config_uri)`. Both calls happen as part of the same wire
  request handling; the operator's single `POST .../import`
  call results in both the data import AND the registry entry.
- If the control plane registration fails, the import overall
  is treated as a partial success: the data is in the
  task-store-server, but the experiment is invisible to
  cross-experiment views until manually registered. The import
  response includes a `warnings` array (per 12b §3.4) listing
  the failure with operator-actionable text.

A natively-created experiment (operator-driven `register_experiment`
followed by `create_task(kind=ideation)`) follows the inverse
order: control plane register first, then task-store-server
ops.

### 3.6 Cross-experiment views in the Web UI

The Web UI gains:

**Experiment switcher.** A dropdown in the top nav, listing all
experiments from `list_experiments`. The currently-selected
experiment is the scope for all subsequent page navigation.
Default: the most-recently-created experiment.

**Cross-experiment dashboard.** A new `/admin/all-experiments`
page that shows:

- One row per experiment: `experiment_id`, state, lease holder
  (if any), counts of pending tasks / running variants /
  integrated variants.
- Counts come from per-experiment `list_tasks` /
  `list_variants` calls; the page makes one call per
  experiment in the registry. For a deployment with many
  experiments, this is N parallel calls (HTTP/2 connection
  pooling helps; v0 doesn't try to optimize further).

**Per-experiment ops still scoped.** All existing Web UI
modules (ideator, executor, evaluator, admin) operate on the
currently-selected experiment. Switching experiments
re-points the per-experiment links at the new
`experiment_id`.

### 3.7 Conformance: multi-experiment scenarios

The new `v1+multi-experiment` conformance level adds:

- **Lease acquisition.** Replica A acquires a lease for
  experiment E. Replica B's `acquire_lease` for E fails with
  `LeaseHeldByOther`. After A releases, B's retry succeeds.
- **Lease renewal.** Replica A acquires; renews three times
  successfully; eventually releases.
- **Lease expiration + hand-off.** Replica A acquires;
  `lease_duration` elapses without renewal; replica B's
  acquire succeeds; A's subsequent renew fails.
- **Decision gating.** A replica without a lease running its
  iteration produces NO state changes (no `task.created`,
  `idea.dispatched`, etc.) for the un-leased experiment.
- **Multi-experiment dispatch.** Two experiments E1, E2 with
  different ideators; replica A holds lease for E1, replica B
  for E2; both run independently and produce the right
  outcomes for their respective experiments.
- **Hand-off race.** Replica A's lease is about to expire
  (within `lease_duration / 3`). Replica B's
  `acquire_lease` succeeds. During the brief window before A
  detects the loss, both replicas attempt
  `create_execution_task` for the same idea. The 12a-2 §6.4
  exact-idempotent contract ensures exactly one task is
  created.
- **Checkpoint import + auto-register (control plane
  reachable).** Importing a checkpoint via
  `POST /v0/checkpoints/import` while the control plane is
  reachable results in the imported experiment appearing in
  `list_experiments`. (Per Decision 9, this is best-effort,
  not atomic; the test's precondition is that the control
  plane is reachable AND register succeeds. A separate
  scenario covers the partial-success path.)
- **Checkpoint import + control-plane-unreachable.** Importing
  a checkpoint while the control plane is unreachable
  (simulated via a network-error fixture) results in (a) the
  experiment present in the task-store-server, (b) absent from
  `list_experiments`, (c) the import response's `warnings`
  array carries an explicit "control-plane-register-failed"
  entry. After bringing the control plane back, an explicit
  `POST /v0/control/experiments` recovers the registry entry.

### 3.8 Compose smoke for multi-experiment

A new `compose-smoke-multi-experiment` smoke:

- Brings up the Compose stack with two orchestrator replicas
  alongside the control plane service.
- Registers two experiments E1, E2 via the control plane.
- Drives both to 2 integrated variants each.
- Asserts:
  - Both experiments end with `state=terminated` (assuming
    `max_variants_policy(2)` for both).
  - Each experiment has exactly one lease holder at any moment
    in the event log.
  - Cross-experiment dashboard shows both experiments in the
    final state.
- Stops one orchestrator replica mid-run; the other replica
  picks up its leases. Both experiments still complete.

### 3.9 Alternatives considered

Three architectural choices in this chunk benefit from explicit
compare-and-reject paragraphs.

**Per-experiment leases (chosen) vs. deployment-level leader
election.** Decision 7 explicitly accepts "greedy with
stand-bys" — one replica may hold all leases, others sit idle.
That makes the chosen design primarily an HA/failover
mechanism rather than a scaling one. The natural alternative
is a single deployment-wide leader: one replica is elected
leader, runs all dispatch loops for all experiments, others
stand by for failover.

Why per-experiment leases anyway:

1. **Future scaling story.** A future v1 amendment can add
   active load-rebalancing across replicas (cross-replica
   gossip, lease-stealing, etc.) without changing the lease
   primitive. A leader-election design forecloses that path —
   moving from "one leader" to "leader-per-experiment" is a
   larger refactor than evolving v0's "greedy" acquisition
   into "rebalanced".
2. **Operator visibility.** Per-experiment leases let
   operators see "experiment E is owned by replica A" in the
   dashboard and alert on per-experiment failures. With a
   single leader, the only visible state is "leader is
   replica X" — useful but coarser.
3. **Failure domains.** If one experiment's dispatch loop
   misbehaves (e.g., a poorly-written termination policy
   raises every iteration), it's contained to its lease
   holder. With a single leader, a misbehaving experiment
   crashes the leader's process and re-elects.

The cost is the lease machinery itself — acquire/renew/release
plus the per-experiment registry. v0 accepts that cost as the
investment that pays off in v1's scaling story.

**Greedy acquisition (chosen) vs. deterministic sharding by
`experiment_id`.** §3.2's acquisition loop has every replica
polling `list_experiments` and opportunistically trying to
acquire anything unheld. The alternative is consistent-hash
sharding: `experiment_id → hash → replica index`, so each
experiment has a deterministic owner. Pros of sharding:
predictable distribution; no acquisition scans; "replica A
owns E1, replica B owns E2" is contract, not coincidence.

Why greedy anyway:

1. **Replica-set churn is hard with sharding.** Adding a
   replica reshards experiments, which means leases must be
   handed off to new owners. The hand-off coordination is
   non-trivial — every replica needs to know the current
   replica-set membership, and adding a replica means every
   experiment whose hash now points elsewhere must be
   released by its current holder. Greedy acquisition
   sidesteps this: replicas come and go; leases drift
   greedily; the system converges naturally.
2. **HA simplicity.** A failed replica's leases expire and
   get re-acquired greedily. With sharding, a failed
   replica's leases need a fallback rule (next replica in
   the hash ring, etc.) which adds yet another mechanism.
3. **Fairness is OK at v0 scale.** A deployment with 10
   experiments and 3 replicas may end up with one replica
   holding all 10 leases. That's idle capacity — not data
   corruption. v1 can add active rebalancing on top of the
   greedy mechanism.

If a future deployment hits a real load-imbalance problem,
the right path is to add rebalancing (lease-stealing across
replicas) on top of the existing lease primitive, not to
replace the primitive with sharding.

**Separate control-plane service + deployment-scoped worker
registry (chosen) vs. sidecar in task-store-server vs.
admin-only lease ops.** Decision 1 picks a separate service;
Decision 12 adds a deployment-scoped worker registry distinct
from 12a-1's per-experiment registry. The combined complexity
is meaningful. Two narrower alternatives:

- **Sidecar in the shared task-store-server.** The
  task-store-server adds endpoints under `/v0/control/...`
  alongside its existing `/v0/experiments/...`. Pros: one
  service to deploy + auth-policy. Cons: muddles the
  task-store-server's role (it becomes the data plane AND
  the coordination plane); a control-plane crash takes the
  data plane down with it. The plan's separation-of-concerns
  argument (Decision 1) is the load-bearing reason to split;
  the cost is one extra service. Net: separate service wins.

- **Admin-only lease ops, no deployment-scoped worker
  registry.** Every orchestrator replica uses the deployment
  admin-token directly to call `acquire_lease` etc. Pros:
  no deployment-scoped registry; orchestrator startup is
  simpler. Cons: every replica holds full deployment-admin
  power for its lifetime, which is a much weaker security
  posture than 12a-1's per-worker credentials offer for
  every other op. A compromised orchestrator could
  unregister experiments, force-release leases, etc. The
  deployment-scoped registry confines orchestrator authority
  to lease ops over its own holds.

The chosen design (separate service + deployment-scoped
worker registry) is the most expensive of the three but
offers the best operational separation + security posture
for v0. A future amendment may collapse the deployment-
scoped registry into 12a-1's worker model if 12a-1 widens
to support deployment-scoped workers natively.

### 3.10 What 12c does NOT do

- **Sub-experiment leases.** A future need might be "lease
  parts of an experiment to different replicas" (e.g., one
  replica handles ideation, another handles integration). v0
  per-experiment leases are coarser; per-decision leases are
  out of scope.
- **Cross-experiment task migration.** Moving tasks between
  experiments is not supported. The right path is "export E1,
  import as E2, drop E1".
- **Control-plane HA.** The control plane is a single replica
  in v0 (deployment-level SPOF). HA for the control plane
  itself uses the same lease pattern recursively; deferred to
  Phase 13+.
- **Cross-deployment leases.** The control plane is per-
  deployment. Multi-deployment coordination (federated EDEN)
  is not in v0.
- **Hot reconfiguration of `lease_duration`.** Changing the
  parameter requires restarting the control plane.
- **Lease pre-emption.** A replica cannot forcibly take a
  lease from another active holder; it must wait for
  expiration. An admin-driven "force release" op MAY land
  later; not in v0.

## 4. Scope

### 4.1 In scope

Spec edits:

- **New chapter 11** "Control plane" (§3.1 above).
- Chapter 02: add `lease: ExperimentLease | null` to the
  Experiment runtime shape.
- Chapter 03: add §6.5 "Lease ownership" gating the
  orchestrator's five decisions.
- Chapter 07: extend §1.3 carve-out to include `/v0/control/`.
  Add §9 "Control-plane operations" with the new endpoints.
- Chapter 09: add `v1+multi-experiment` conformance level.
- Schema files: new `lease.schema.json`; small additions to
  `experiment.schema.json` for the `lease` field.

Code (reference impl):

- **New `eden-control-plane` reference service.** FastAPI app
  exposing the registry + lease ops. Backed by a new Postgres
  schema (`eden_control_plane`).
- **New `eden-control-plane` package** (under
  `reference/packages/`): client library for the new
  endpoints, used by the orchestrator + Web UI.
- `eden_orchestrator.loop`: lease-acquire + renewal threads;
  decision gating per §3.2.
- `eden_storage.Store`: extend the Protocol + backends to be
  multi-experiment-aware per Decision 11. Today the Store is
  constructed with one `experiment_id` baked in
  ([`reference/services/task-store-server/src/eden_task_store_server/app.py`](../../reference/services/task-store-server/src/eden_task_store_server/app.py)
  lines 21-57); 12c lifts that to a per-call parameter (every
  Store op gains an `experiment_id` argument) AND a Store
  factory that owns multiple experiments at once. The
  task-store-server constructs ONE Store factory at startup
  and routes incoming wire requests' `experiments/{id}/...`
  paths to the right experiment's data via the factory. The
  control plane has its own separate Postgres (per §3.4
  Option A).
- `eden_wire.server.py` / `client.py`: extend the import
  endpoint per §3.5 to call the control plane.
- `eden_web_ui.routes.admin`: experiment switcher in top nav;
  `/admin/all-experiments` cross-experiment dashboard.
- `eden_web_ui` session: track the currently-selected
  experiment_id in the session cookie alongside `worker_id`.

Conformance:

- New scenarios under
  `conformance/scenarios/test_lease_*.py`:
  - `test_lease_acquisition.py` — acquire / renew / release.
  - `test_lease_expiration.py` — hand-off semantics.
  - `test_lease_decision_gating.py` — un-leased replica skips
    decisions.
  - `test_multi_experiment_dispatch.py` — two replicas, two
    experiments, independent loops.
  - `test_lease_handoff_race.py` — hand-off window race
    safety (12a-2 §6.4 idempotency).
  - `test_checkpoint_import_registers.py` — 12b import →
    registry entry appears.

Compose / smokes:

- New `compose-smoke-multi-experiment` (§3.8).
- Existing smokes need an extra control-plane container in
  the stack; they remain single-experiment.

### 4.2 Cross-references to followups

- Control-plane HA itself — Phase 13 (Helm chart + multi-
  control-plane-replica with bootstrap leader election).
- Cross-deployment federation — out of scope, possibly v1.
- Per-decision leases — out of scope.
- S3/GCS artifact backend (referenced in cross-experiment
  views) — Phase 13.

### 4.3 Out of scope

- Authentication for the control plane separate from the
  task-store-server's per-worker auth (12a-1). The control
  plane uses the same admin-token + worker-credential model;
  no new auth layer.
- Web UI customization of the experiment-switcher's default
  (e.g., "pin this experiment as default"). Default = most-
  recently-created.
- Real-time updates to the cross-experiment dashboard. The
  page refreshes on demand; SSE/WebSocket updates are a v2
  improvement.

### 4.4 Non-goals

- A "kill experiment" emergency op at the control-plane level.
  Termination goes through the per-experiment 12a-3 path
  (`POST .../experiments/<E>/terminate`).
- Migrating an experiment's data from one task-store-server to
  another. Use 12b portable checkpoints + a fresh import.
- Quota / resource-limit enforcement at the control plane. v0
  is unrestricted; quotas land in Phase 13.

## 5. Files to touch

### 5.1 Spec

| File | Change |
|---|---|
| `spec/v0/11-control-plane.md` (new) | Full chapter from §3.1 above. ~250 lines. |
| `spec/v0/02-data-model.md` | Extend the §2.5 Experiment runtime shape with `lease: ExperimentLease \| null`. Define `ExperimentLease` shape inline or cross-reference chapter 11. |
| `spec/v0/03-roles.md` | Add §6.5 "Lease ownership". The orchestrator's five decisions (§6.2) MUST NOT run for an experiment unless the orchestrator holds an active lease. |
| `spec/v0/07-wire-protocol.md` | Extend §1.3 carve-out to include `/v0/control/` (parallel to 12b's `/v0/checkpoints/`). Add §9 "Control-plane operations" with per-endpoint authority per Decision 12. **Experiment registry**: `POST /v0/control/experiments` (register; admin-token), `DELETE /v0/control/experiments/<E>` (unregister; admin-token), `GET /v0/control/experiments` (list; any authenticated control-plane worker or admin-token), `GET /v0/control/experiments/<E>` (read metadata; same as list). **Lease ops** (per §11.6 holder-instance fencing — every lease op carries `holder_instance: string` in addition to `lease_id`): `POST /v0/control/experiments/<E>/leases` (acquire; body includes `holder_instance`; caller in deployment-scoped `orchestrators` group with `holder == authenticated worker_id`); `GET /v0/control/leases?holder=<worker_id>` (list active leases held by `worker_id`; used by the §3.2 startup-fence probe; caller MUST be authenticated as `worker_id`); `POST /v0/control/leases/<L>/renew` (body includes `holder_instance`; returns 409 `eden://error/lease-instance-mismatch` if the stored value differs); `POST /v0/control/leases/<L>/release` (body includes `holder_instance`; same mismatch rule). **Deployment-scoped worker registry** (per Decision 12): `POST /v0/control/workers` (register; admin-token; mirrors 12a-1 §D.1 shape but at the deployment scope), `POST /v0/control/workers/<W>/reissue-credential` (admin-token), `GET /v0/control/workers` (list; admin-token), `POST /v0/control/groups` (register; admin-token), `POST /v0/control/groups/<G>/members` (add_to_group; admin-token), `DELETE /v0/control/groups/<G>/members/<W>` (remove_from_group; admin-token), `GET /v0/control/groups` (list; admin-token), `POST /v0/control/verify-credential` (authenticated; mirrors 12a-1's verify_worker_credential). |
| `spec/v0/09-conformance.md` | Add `v1+multi-experiment` level. |
| `spec/v0/schemas/lease.schema.json` (new) | JSON Schema for ExperimentLease. |
| `spec/v0/schemas/experiment.schema.json` (extension) | Add `lease` property (oneOf [`null`, `$ref: lease.schema.json`]). |

### 5.2 eden-control-plane (new package + service)

| File | Change |
|---|---|
| `reference/packages/eden-control-plane/src/eden_control_plane/__init__.py` | Public API: `ControlPlaneClient` (http client over the new endpoints). |
| `reference/packages/eden-control-plane/src/eden_control_plane/client.py` | The `ControlPlaneClient` class wrapping `httpx`; methods mirror the wire endpoints (experiments, leases, workers, groups, verify-credential). |
| `reference/packages/eden-control-plane/src/eden_control_plane/models.py` | Pydantic models: `Experiment` (registry entry), `ExperimentLease`, `ImportProvenance` (extended from 12b), `DeploymentWorker`, `DeploymentGroup`, shared response shapes (including the `warnings` array used by `read_experiment_metadata`'s stale-state warning per §3.4a). |
| `reference/packages/eden-control-plane/tests/` | Unit tests for the client (request shape, response parsing, error handling for all four endpoint families: experiments, leases, workers, groups). |
| `reference/services/control-plane/src/eden_control_plane_server/server.py` (new) | FastAPI app. All endpoints per the §5.1 spec table: experiment registry + lease ops + deployment-scoped worker/group registry + verify-credential. Postgres-backed (psycopg). |
| `reference/services/control-plane/src/eden_control_plane_server/db.py` | Schema definitions + migrations (a thin idempotent CREATE-TABLE-IF-NOT-EXISTS at startup). Tables: `control_plane_experiments`, `control_plane_leases` (§3.4 above), plus `control_plane_workers` and `control_plane_groups` for the Decision 12 deployment-scoped registry (mirroring 12a-1's `workers` / `groups` table shapes but in the control-plane's schema, separate from the per-experiment task-store-server tables). |
| `reference/services/control-plane/src/eden_control_plane_server/lease.py` | Lease acquire/renew/release logic per §3.4 + §11.6 fencing. The atomic-replace SQL on acquire is the load-bearing piece. The lease row schema includes the `holder_instance` column; renew/release verify it and raise `LeaseInstanceMismatch` (409 `eden://error/lease-instance-mismatch`) on a mismatch. The new `list_active_leases(holder=worker_id)` op (used by the §3.2 startup-fence probe) selects leases by holder + `expires_at >= now`. |
| `reference/services/control-plane/src/eden_control_plane_server/workers.py` (new) | Deployment-scoped worker + group registry handlers per Decision 12. Reuses 12a-1's argon2id credential-hashing scheme; the Postgres tables are control-plane-scoped (no cross-talk with the task-store-server's per-experiment registries). |
| `reference/services/control-plane/src/eden_control_plane_server/state_sync.py` (new) | Background poller per §3.4a: every `EDEN_STATE_SYNC_INTERVAL_SECONDS`, walk `list_experiments` and call each experiment's `read_experiment(id)` on the task-store-server. Update `last_known_state` atomically. Track per-experiment "consecutive failures" counter; emit stale-warning entries when threshold (default 10) is exceeded. The `acquire_lease` handler also calls into `state_sync` for an on-demand refresh. |
| `reference/services/control-plane/tests/` | Unit + integration tests for all four endpoint families. Postgres-backed integration tests gated on `EDEN_TEST_POSTGRES_DSN`. **Specific coverage** (matches §6.1 below): lease semantics, worker/group registration round-trip, state-sync polling convergence, stale-warning emission, acquire-lease-also-refreshes-state. |

### 5.3 eden-orchestrator

| File | Change |
|---|---|
| `reference/services/orchestrator/src/eden_orchestrator/cli.py` | Add `--control-plane-url` flag (default `http://control-plane:8080`). Add `--lease-duration-seconds` (default 30). The 12a-2 `--worker-id` flag is **preserved** (no regression): operators MUST supply a unique `worker_id` per replica per 12a-1 §D.1's grammar (default `auto-orchestrator-1`). The single-experiment `--experiment-id` flag is now optional: if set, the orchestrator only attempts to lease that experiment; if absent, it leases any experiment in the registry. **Dual-credential bootstrap inputs (per Decision 12)**: add `--admin-token <path-or-env>` for bootstrap admin operations (control-plane `register_worker` / `add_to_group` for the orchestrator's deployment-scoped identity, plus per-experiment `register_worker` calls when acquiring a new lease). Add `--control-plane-credential-path <path>` and `--task-store-credential-dir <dir>` for persisted credentials (the deployment-scoped credential is one file; per-experiment credentials are one file per leased experiment under the dir). Defaults: `$XDG_STATE_HOME/eden/control-plane/<worker_id>.cred` and `$XDG_STATE_HOME/eden/task-store/<experiment_id>/<worker_id>.cred`. Re-running with the same paths reuses persisted credentials. |
| `reference/services/orchestrator/src/eden_orchestrator/loop.py` | Major rework: replace single-experiment loop with multi-experiment lease-driven loop per §3.2. Add `LeaseManager` class managing the held-leases set + renewal thread + acquisition thread. The `run_orchestrator_iteration` per-experiment call **shape** is unchanged from 12a-2 / 12a-3 (still takes one experiment_id at a time); the outer driver instantiates a per-experiment `StoreClient` view (see §5.4 below) per held lease and calls `run_orchestrator_iteration` once per held experiment per outer-loop tick. |
| `reference/services/orchestrator/src/eden_orchestrator/leases.py` (new) | `LeaseManager` implementation. |
| `reference/services/orchestrator/tests/test_lease_manager.py` (new) | Unit tests covering acquire/renew/release, expiration handling, hand-off detection. |
| `reference/services/orchestrator/tests/test_e2e.py` | Add a multi-experiment scenario that drives 2 experiments through 2 orchestrator replicas. |

### 5.4 eden-wire

| File | Change |
|---|---|
| `server.py` | Extend the `POST /v0/checkpoints/import` handler per §3.5: after the task-store-server side commits, call `control_plane.register_experiment`. Failures here add to the `warnings` array in the response. The handler reads the control plane URL from a new `--control-plane-url` flag. |
| `client.py` | The existing experiment-bound `StoreClient(base_url, experiment_id, ...)` constructor signature is **preserved** (so 12a-2 / 12a-3 / 12b-shaped callers don't break). 12c adds a thin factory pattern: `StoreClientFactory(base_url, ...)` produces per-experiment `StoreClient` views on demand. Multi-experiment callers (orchestrator's `LeaseManager`, the cross-experiment Web UI dashboard) construct one `StoreClient` per held experiment via the factory. The Store Protocol's per-call `experiment_id` parameter (Decision 11 + §5.2) lives at the Store layer; the `StoreClient` layer keeps the experiment-bound view for callers that don't need multi-experiment access. The new `ControlPlaneClient` (separate package) is unchanged. |

### 5.5 Reference services

| File | Change |
|---|---|
| `reference/services/web-ui/src/eden_web_ui/cli.py` | Add `--control-plane-url` flag. |
| `reference/services/web-ui/src/eden_web_ui/sessions.py` | Track `selected_experiment_id` in the session cookie alongside `worker_id`, `csrf`. |
| `reference/services/web-ui/src/eden_web_ui/routes/admin.py` | New `/admin/all-experiments` page (read-only cross-experiment dashboard). Top-nav experiment switcher (a dropdown that POSTs to a new `/switch-experiment` endpoint to update the session). |
| `reference/services/web-ui/src/eden_web_ui/templates/_layout.html` | New top-nav switcher dropdown. |
| `reference/services/web-ui/src/eden_web_ui/templates/admin_all_experiments.html` (new) | The cross-experiment dashboard table. |

### 5.6 Compose / setup

| File | Change |
|---|---|
| `reference/compose/compose.yaml` | New `control-plane` service (FastAPI; port 8081 internal). Existing `orchestrator` service gains `--control-plane-url`. Existing `web-ui` service gains `--control-plane-url`. |
| `reference/compose/.env.example` | Add `CONTROL_PLANE_PORT=8081`, `EDEN_LEASE_DURATION_SECONDS=30`, `EDEN_STATE_SYNC_INTERVAL_SECONDS=30`, `EDEN_STATE_SYNC_FAILURE_THRESHOLD=10` (consecutive-failure count before a stale-warning fires per §3.4a). |
| `reference/scripts/setup-experiment/setup-experiment.sh` | After the task-store-server seed, call `control-plane register_experiment` (admin-token-authenticated). Idempotent: re-running setup against an existing experiment is a no-op on the registry side. |
| `reference/compose/healthcheck/smoke-multi-experiment.sh` (new) | The §3.8 smoke. |

## 6. Test design

### 6.1 Unit tests (per-package)

`eden_control_plane.tests.test_client`:

- Request-shape coverage for each endpoint.
- Error parsing (`LeaseHeldByOther`, `LeaseNotHeld`,
  `ExperimentNotFound`).
- httpx connection errors → typed transport exceptions.

`eden_control_plane_server.tests.test_lease_logic`
(parametrized SQLite + Postgres):

- Fresh experiment + first acquire → 201; lease has correct
  `expires_at`.
- Concurrent acquires (two threads) → exactly one succeeds.
- Renew within `lease_duration` → 200; `expires_at` advances.
- Renew after expiration → 410 `LeaseExpired` (different from
  `LeaseNotHeld`, because the holder might still think it
  holds; renewing an expired-but-unreplaced lease is a clear
  case).
- Renew with wrong `lease_id` → 410 `LeaseNotHeld`.
- Release → 200; subsequent acquire succeeds.
- Acquire over expired lease (no explicit release) → 201 with
  new `lease_id`, replacing the expired entry atomically.
- **§11.6 holder-instance fencing**: a renew with the wrong
  `holder_instance` (same `lease_id` and same `worker_id` but
  different UUID) returns 409
  `eden://error/lease-instance-mismatch`. Same for release.
- **§11.6 startup-fence probe**: `list_active_leases(holder=W)`
  returns only leases where holder == W AND expires_at >= now.
  Verifies the orchestrator's startup duplicate-`worker_id`
  detection.

`eden_control_plane_server.tests.test_workers_registry`
(parametrized SQLite + Postgres; mirrors 12a-1's worker-registry
tests but at the deployment scope):

- `register_worker(worker_id)` succeeds; second call with the
  same `worker_id` is idempotent (returns the same record
  without minting a new credential).
- `verify_worker_credential` succeeds with the issued
  credential; rejects with bad credential.
- `reissue_credential` mints a fresh credential and invalidates
  the prior one.
- `register_group` + `add_to_group` round-trip; `list_groups`
  returns the membership.
- Authority: `register_worker` without admin-token → 403;
  `verify_worker_credential` without authentication → 401.

`eden_control_plane_server.tests.test_state_sync` (per §3.4a):

- Polling loop: register experiment E; task-store-server's
  `experiment.state` is `"running"`; wait one polling
  interval; assert `last_known_state == "running"`. Transition
  the task-store-server's state to `"terminated"`; wait one
  more polling interval; assert
  `last_known_state == "terminated"`.
- Stale warning: simulate task-store-server unreachable for >
  N polling intervals; assert `read_experiment_metadata`
  surfaces a stale-state warning in its `warnings` array.
- `acquire_lease` triggers state refresh: register experiment;
  set task-store-server state to `"terminated"` between two
  polling ticks; immediately call `acquire_lease`; assert the
  acquire response carries the up-to-date
  `last_known_state == "terminated"` (the on-demand refresh
  ran).

`eden_orchestrator.tests.test_lease_manager`:

- Manager acquires available leases on startup; each lease
  carries a fresh `holder_instance` UUID per process.
- Renewal thread renews held leases on schedule.
- Renewal failure → drop experiment from owned set.
- Acquisition thread retries on `LeaseHeldByOther`.
- Shutdown → all leases released.
- Decision gating: an iteration with no held leases produces
  no Store writes (mock the Store to assert).
- **§11.6 startup duplicate-`worker_id` detection**: with the
  control plane reporting an active lease held by
  `self.worker_id` AND a different `holder_instance`, the
  manager's `_startup_fence_check()` exits the process with
  code 2.
- **§11.6 instance-mismatch handling**: renew thread receives
  `LeaseInstanceMismatch` from the control plane (a different
  replica took over with a fresh instance UUID); manager
  drops the experiment from its owned set without retrying.
- **§11.7 release-after-drain**: when `run_integration_only`
  reports `drained=True` for a terminated experiment, the
  manager releases the lease, removes the experiment_id from
  `held_experiments`, and adds it to `drained_terminated`.
- **§11.7 acquisition skip**: with experiment_id in
  `drained_terminated`, the acquisition thread does NOT call
  `acquire_lease` for it on subsequent ticks.

### 6.2 Cross-request flow tests

`reference/services/web-ui/tests/test_experiment_switcher.py`:

- Switcher dropdown lists all registered experiments.
- Selecting an experiment updates the session's
  `selected_experiment_id`.
- Subsequent page navigation scopes to the selected
  experiment.
- `/admin/all-experiments` renders one row per experiment with
  correct counts.

`reference/services/orchestrator/tests/test_multi_experiment.py`:

- Two orchestrator replicas, two experiments. Each replica
  acquires one lease. Each runs an iteration. Both experiments
  advance.
- One replica crashes (simulated). Lease expires. Other
  replica acquires the orphaned lease. Experiment continues.

### 6.3 Conformance scenarios

`conformance/scenarios/test_lease_*.py`:

- See §3.7 above for the seven scenarios.

### 6.4 Verification gates

Before merge:

- `uv run ruff check .` clean.
- `uv run pyright` 0 errors.
- `uv run pytest -q` (full suite) passes.
- `uv run pytest -q conformance/` passes (existing levels +
  v1+multi-experiment).
- `uv run python conformance/src/conformance/tools/check_citations.py`
  clean.
- `python3 scripts/spec-xref-check.py` clean (chapter-11
  cross-references resolve).
- `python3 scripts/check-rename-discipline.py` clean.
- `bash reference/compose/healthcheck/smoke.sh` passes.
- `bash reference/compose/healthcheck/smoke-multi-experiment.sh`
  passes (new).
- Markdownlint clean.
- Manual UI smoke: spin up the stack with 2 orchestrator
  replicas + control plane; register 2 experiments; verify the
  switcher works; kill one orchestrator; confirm the other
  takes over both.

## 7. Tricky areas

### 7.1 Clock skew between control plane and orchestrator

Lease `expires_at` is a wall-clock timestamp set by the control
plane. Orchestrator replicas compare it to their local clock to
decide when to renew. If the orchestrator's clock is ahead of
the control plane's by `delta`, it renews `delta` early — fine.
If behind, it renews `delta` late — possibly past the actual
expiration. Resolution:

- The control plane is the source of truth: every renew op
  returns the new `expires_at` from the control plane's clock,
  which the orchestrator uses going forward (rather than its
  local computation).
- The renewal interval (`lease_duration / 3`) leaves margin
  for skew up to ~10s (with default `lease_duration = 30s`).
  Larger skew is a deployment problem (NTP misconfigured); the
  contract is "skew < lease_duration / 3 is tolerable".

### 7.2 Lease + checkpoint-import atomicity

§3.5 has the import endpoint sequentially (a) commit the
task-store-server data and (b) call the control plane to
register the experiment. These are two separate services with
separate Postgres connections. True atomicity (XA/2PC) is
heavy and out of scope for v0; the plan's "partial success
with warnings" approach (Decision 9) is a soft guarantee, NOT
an atomicity claim.

The risk: an import that commits to the task-store-server but
fails to register with the control plane leaves the experiment
in a half-state (data exists; not in `list_experiments`). The
operator's recovery is `POST /v0/control/experiments` with the
experiment_id from the partial-success response. Documented in
the import handler's text.

A future amendment could add explicit two-phase commit: the
import handler first reserves a slot in the control plane,
then commits the data, then finalizes the registry. The cost
is ~3x latency for the common case. Defer to v1.

### 7.3 Experiment switcher state during orchestrator replica change

When an orchestrator replica crashes and another takes over, the
Web UI's experiment switcher shows the experiment as still
existing (the registry entry is unchanged). The currently-
selected experiment continues to work. The only operator-
visible change is the "lease holder" column on the cross-
experiment dashboard updating from replica-A to replica-B.

This is the right behavior — the UI doesn't need to know which
replica owns which lease for normal operation. The "lease
holder" surfacing is for diagnostic purposes only.

### 7.4 Aggregated reads scaling

`/admin/all-experiments` makes one task-store-server call per
experiment. For 100 experiments that's 100 HTTP calls per page
load. Mitigation:

- HTTP/2 connection pooling (httpx default) handles the
  multiplexing.
- The page caches counts for 5s; refreshes are operator-
  driven.
- A future amendment may add a bulk endpoint
  `GET /v0/aggregate/experiment-counts` to the task-store-
  server that returns `(experiment_id, counts)` for all
  experiments in one call. v0 stays simple.

The 5s cache is configurable via the Web UI's CLI flag.

### 7.5 Per-orchestrator-replica `held_leases` consistency

Each orchestrator replica tracks its own `held_leases` set. If
a replica crashes mid-renewal-cycle, the in-memory set is lost;
the control plane's leases survive (they expire naturally).
When the replica restarts, it queries `list_experiments`, sees
which experiments have leases held by its own `worker_id` (the
replica's stable identity), and attempts to renew each. Renews
that succeed: the replica recovers its prior set. Renews that
fail (lease already taken by another replica): the replica
acknowledges the loss and moves on.

This makes orchestrator restarts cheap — no special "I'm
recovering" state to reconcile.

### 7.6 Cross-experiment ideator

The roadmap mentions "cross-experiment views in the shared
ideator". The Web UI's ideator module currently scopes to one
experiment. Extending it to "show me ideas across all my
experiments" is a UI improvement that doesn't need protocol
changes — it's just `for each experiment in registry: list_ideas`
plus aggregation.

The cross-experiment ideator is in scope for the §3.6 dashboard
pattern but NOT in scope for ideator-side mutations (creating
an idea still requires a specific experiment, scoped via the
switcher). v0 doesn't support "create this idea in any
available experiment".

## 8. Risks

1. **Lease expiration during long-running decision.** If the
   orchestrator's `run_orchestrator_iteration` takes longer
   than `lease_duration` (e.g., the integration step blocks on
   a slow git push), the renewal thread might miss its window
   and the lease expires mid-decision. Mitigation: (a) the
   default `lease_duration = 30s` is much longer than typical
   iteration time (sub-second); (b) the renewal thread runs
   every `lease_duration / 3 = 10s` so even a 25-second
   iteration gets two renewals; (c) operators with chronically
   slow iterations should bump `EDEN_LEASE_DURATION_SECONDS`.

2. **Control-plane downtime stops all dispatch.** If the
   control plane crashes, orchestrator replicas can't renew
   their leases and eventually drop their held experiments. No
   new dispatch happens until the control plane is back. This
   is the v0 SPOF acknowledged in §3.9. Mitigation: ship a
   monitoring recommendation in the deployment guide; a control
   plane crash should page on-call. Recovery is fast (the
   service restarts; replicas re-acquire leases).

3. **Experiment-id collisions with existing
   single-experiment deployments.** A pre-12c deployment
   running one experiment doesn't have a registry entry. A
   12c-upgraded deployment querying `list_experiments` returns
   empty until the operator manually registers the existing
   experiment. Documented in the upgrade notes; a one-shot
   migration script `eden-control-plane register --from-task-store-server`
   handles it.

4. **Concurrent imports register the same experiment_id.** Per
   12b §7.9 the import endpoint serializes; per §3.5 the
   control-plane register call happens after the
   task-store-server commit; so two concurrent imports with
   the same id → first imports + registers, second gets 409
   from the task-store-server. Consistent.

5. **Re-introducing legacy vocab in the new spec text.** The
   strengthened guardrail catches the patterns; this chunk
   adds a substantial new chapter. Mitigation: pre-submit
   `python3 scripts/check-rename-discipline.py` clean.

## 9. Sequencing

Recommended PR shape (in order):

1. **Spec PR.** Chapter 11 (new) + chapter 02 / 03 / 07 / 09
   amendments + schema files. No code.

2. **eden-control-plane package PR.** New package: client +
   models. Standalone (no service yet).

3. **eden-control-plane service PR.** New FastAPI service +
   Postgres schema + lease logic. Postgres-backed integration
   tests gated on `EDEN_TEST_POSTGRES_DSN`.

4. **Orchestrator lease-manager PR.** Major rework of the
   orchestrator loop: replace single-experiment with multi-
   experiment lease-driven model. New `LeaseManager` class.
   Per-experiment `run_orchestrator_iteration` unchanged.

5. **eden-wire import-handler extension PR.** The
   checkpoint-import endpoint also calls
   `control_plane.register_experiment` per §3.5.

6. **Web UI PR.** Experiment switcher in top nav; cross-
   experiment dashboard at `/admin/all-experiments`; session
   tracking for `selected_experiment_id`.

7. **Conformance PR.** New scenarios under
   `test_lease_*.py`.

8. **Compose smoke PR.** New `compose-smoke-multi-experiment`.
   Existing smokes get a control-plane container in the stack.

9. **Docs PR.** Glossary update; roadmap delta (one-line status flip per chunk); `CHANGELOG.md [Unreleased]` entry (per-chunk completion prose lives here, not in AGENTS.md — see AGENTS.md "Recording chunk completions").

A reviewer going from PR 1 to PR 9 should expect tests to go
red around PR 4 (orchestrator rework) and come back green at
PR 7 (conformance scenarios).

## 10. Estimated effort

- **Spec prose** (PR 1): ~2 days. Chapter 11 is ~250 normative
  lines + cross-references in 4 other chapters. Schema files
  and the v1+multi-experiment conformance level add work.
- **eden-control-plane package** (PR 2): ~1 day. Just a thin
  http client.
- **eden-control-plane service** (PR 3): ~2.5 days. Postgres
  schema + lease atomic-replace SQL + integration tests are
  the heavy lift.
- **Orchestrator lease-manager** (PR 4): ~2.5 days. The loop
  rework is intrusive: replacing the existing single-
  experiment driver with a multi-experiment lease-driven one
  touches a lot of test fixtures. The `LeaseManager` itself is
  ~200 LOC.
- **eden-wire import-handler extension** (PR 5): ~0.5 day.
- **Web UI** (PR 6): ~1.5 days. Top-nav switcher + dashboard +
  session tracking.
- **Conformance** (PR 7): ~1.5 days. Seven scenarios; some
  require multi-replica setup.
- **Compose smoke** (PR 8): ~0.5 day.
- **Docs** (PR 9): ~0.5 day.

**Realistic total: ~12.5 working days** of focused work. The
heaviest chunk in the 12 series — comparable to 12b but with
the additional integration overhead of a new reference service
running alongside the existing six.

## 11. Followups (out of scope)

- **Control-plane HA.** Phase 13.
- **Cross-deployment federation.** Possibly v1.
- **Per-decision leases.** Out of scope.
- **Bulk aggregation endpoint** (`GET /v0/aggregate/...`) for
  the cross-experiment dashboard. v2 if scaling demands.
- **Lease pre-emption** (admin-driven force-release). Possibly
  v1.
- **Real-time UI updates** for the dashboard (SSE/WebSocket).
  v2.

## 12. What lands at the end of Phase 12

After 12c merges, the Phase 12 plan from the roadmap is
complete:

- **12a-1**: workers are first-class identifiable principals.
- **12a-2**: the orchestrator is a defined role with five
  decision types, gated by per-experiment `dispatch_mode`.
- **12a-3**: experiments have a lifecycle (`running →
  terminated`) and termination is a deployment-supplied
  policy.
- **12b**: experiments are exportable / importable as portable
  checkpoints.
- **12c**: deployments host multiple experiments simultaneously
  with HA orchestrator replicas via leases.

A research team can run dozens of experiments across a
multi-replica orchestrator pool, observe them in a unified Web
UI, kill replicas without losing dispatch progress, and
export/import experiments between deployments. Phase 13
(Kubernetes reference deployment) builds the production
substrate; Phase 12c is the protocol-level foundation that
makes that work.
