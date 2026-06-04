# PRD: EDEN as an experiment platform

> **Status: draft (2026-05-21).** This document captures the long-horizon vision for how EDEN should be deployed and operated at scale. It is not yet an executable plan; see [`docs/plans/`](../plans/) and the linked GitHub issues for the work along the way.

## 1. Vision

Teams operating EDEN should interact with **one server** to create, run, and manage experiments — regardless of how distributed the underlying infrastructure is. The operator submits an experiment config; a centralized controller decides which substrates host the experiment (which Postgres holds the task store, which Forgejo holds the repo, which object store holds artifacts, which compute fleet runs the workers) and binds them together. The operator never needs to know where any of those pieces live.

The same controller abstraction has to work for two extremes of deployment shape without changing the operator-facing interface:

- **Laptop:** every substrate is a Compose-managed container on `localhost`. The controller binds them.
- **Cluster:** substrates are spread across managed services (RDS, S3, hardened Forgejo on a separate host) and k8s nodes. The controller binds them.

Same API, same `start-experiment` call, same configuration shape. The substrate adapter underneath changes; the operator's mental model does not.

## 2. What's broken today

The reference deployment conflates **provisioning infrastructure** with **running an experiment**:

- `setup-experiment.sh` provisions a fresh Postgres + Forgejo per experiment.
- Each experiment runs its own task-store-server, orchestrator, and worker hosts, locked to one experiment via startup flags.
- Running N experiments side-by-side means N copies of every substrate, even though substrates can structurally host N experiments (Postgres has databases, Forgejo has repos).
- There is no concept of "infrastructure exists; let me add an experiment to it." Every experiment is a fresh stack.

Phase 12c shipped a control-plane-server that is the first step toward a centralized controller — but it is a metadata registry + lease coordinator. It does **not** know about the substrates, allocate them, or stand experiments up. It tracks experiments that the operator stood up by other means.

## 3. What the controller is responsible for

The platform controller is:

- **A substrate inventory.** Adapters (Postgres provider, git remote provider, object store provider, compute provider, …) register themselves with the controller at deployment time. Each adapter describes what it can host and how to provision a tenant within it.
- **A scheduler.** When an operator submits an experiment config, the controller picks substrates from inventory, asks each one to create the experiment's namespace (database / repo / bucket / pod), and records the bindings.
- **A registry.** The controller is the source of truth for "which experiments exist, where they live, what state they're in." Operators querying the registry get a stable view independent of substrate failures. **Experiment ids are controller-/system-minted, opaque, and immutable** (`exp_*` per [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §1.6); the operator never types the id. The earlier open question of operator-supplied vs. system-minted experiment ids is **resolved in favor of system-minted** — it is now load-bearing: stable opaque ids are what let the registry attribute substrate bindings and history without collisions when an operator reuses a display name across runs. The operator's human-facing label is the OPTIONAL `name` (§1.7), surfaced for selection and resolved to the opaque id via `?name=` lookup. See issue [#128](https://github.com/ealt/eden/issues/128) / plan [`docs/plans/identity-id-name-disambiguation.md`](../plans/identity-id-name-disambiguation.md).
- **A lifecycle manager.** Start / end an experiment without touching other experiments or the substrate layer beneath. Provision / tear down substrate adapters without breaking running experiments.
- **A single endpoint for operators.** Everything an operator does — register an experiment, view its state, terminate it, view its history, list experiments — flows through one well-known URL.

The controller is **not** responsible for:

- Running the task-store / orchestrator / worker logic itself. Those are separate services that the controller allocates and starts.
- Storing protocol state. The task-store-server still owns events, tasks, ideas, variants.
- Authenticating end users. Auth flows through the deployment-level admin identity layer (existing chapter 7 §13).

## 4. Substrate inventory model

A **substrate adapter** is a piece of code that implements a small interface:

- `register(controller, config)` — announce capability to the controller at startup. The adapter describes its kind (`postgres` / `git-remote` / `object-store` / `compute`), capacity, and the connection details needed to provision tenants.
- `provision(experiment_id, spec)` — allocate a namespace for the experiment. Postgres adapter creates a database or schema; git-remote adapter creates a repo; object-store adapter creates a bucket prefix; compute adapter allocates pods or Compose services.
- `release(experiment_id)` — deallocate the namespace.
- `health()` — substrate readiness probe.

The controller keeps an `(adapter_kind, adapter_id, capacity)` table. When an operator submits an experiment, the controller chooses one adapter per required kind, calls `provision` on each, records the bindings, and exposes the resulting substrate endpoints (task-store URL, git remote URL, artifacts URL, …) to the experiment's services.

Adapters are pluggable. A laptop deployment registers one Compose-Postgres adapter, one Compose-Forgejo adapter, one Compose-MinIO adapter. A cluster deployment registers one RDS adapter, one hardened-Forgejo adapter, one S3 adapter. Same controller code; different adapter registrations.

## 5. Operator lifecycle on the platform

The platform replaces today's single conflated `setup-experiment.sh` with a four-step ladder:

| Step | Today | After |
|---|---|---|
| **1. Provision infra** | Conflated with step 2 | Operator runs the laptop or cluster provisioner once. Substrate adapters come up and register with the controller. |
| **2. Start an experiment** | `setup-experiment.sh <config> --experiment-id <id>` provisions everything | `POST /v0/control/experiments` with the config. Controller picks substrates, provisions namespaces, brings up per-experiment services, returns the experiment's endpoint. |
| **3. End an experiment** | No equivalent — only "teardown everything" | `DELETE /v0/control/experiments/<id>`. Controller asks each bound substrate to release its namespace, tears down per-experiment services. Other experiments are unaffected. |
| **4. Teardown infra** | `compose down -v` + `rm -rf $DATA_ROOT` | Tear down substrate adapters. Refuses to run if registered experiments still exist. |

Critical invariants:

- Step 1 is independent of any experiment. Run once per workstation or cluster.
- Step 3 leaves the substrate layer intact for other experiments.
- Step 4 is reserved for actually decommissioning the machine.

## 6. Laptop ↔ cluster portability

The portability requirement is **load-bearing**: an EDEN deployment must be runnable end-to-end on one laptop without changing the operator-facing interface. This constrains the platform design in several ways:

- **No assumption of always-on managed services.** A laptop deployment uses Compose-hosted substrate adapters; the controller treats them identically to managed-service adapters.
- **No assumption of always-on network.** Adapter health probes have to handle local-process failures gracefully (a Compose container restart) the same way they handle managed-service failures.
- **No external auth dependency for the base case.** The laptop deployment's controller authenticates substrates via local secrets (admin tokens generated by the provisioner); the cluster deployment can layer IAM / OAuth / SSO on top without changing the controller's wire surface.
- **Config-driven substrate selection.** The controller picks the laptop substrate when the laptop adapter is the only one of its kind registered; it picks the cluster substrate when the cluster adapter is the only one. Heterogeneous deployments (laptop adapter + cluster adapter both registered) are a future scheduling concern.

A laptop user runs:

```bash
provision-infra.sh --target laptop      # Brings up Compose substrate adapters
                                         # + controller. Idempotent.
start-experiment.sh my-config.yaml       # Talks only to the controller.
                                         # Operator does not know Postgres exists.
```

A cluster operator runs:

```bash
provision-infra.sh --target cluster --postgres-endpoint rds.example.com \
                                    --forgejo-endpoint forgejo.example.com \
                                    --object-store s3://eden-prod/
start-experiment.sh my-config.yaml
```

Same `start-experiment.sh`. Same controller endpoint.

## 7. Non-goals

- **Multi-tenant auth between teams.** Today the deployment admin is one identity. Cross-team isolation, RBAC at the experiment level, and audit trails per-team are out of scope for this PRD. Layer on top.
- **Heterogeneous substrate scheduling.** When multiple Postgres adapters are registered with different capacities / latencies / costs, choosing which one to assign an experiment to is a scheduling problem we are not solving here. The first cut picks arbitrarily.
- **Substrate migration.** Once an experiment is bound to a Postgres, it stays there. Moving an experiment's substrate bindings during its lifetime is out of scope.
- **Cross-controller federation.** One deployment, one controller. Multi-region / multi-controller is a future concern.
- **Spot / preemptible compute.** Compute adapters that can revoke worker pods at runtime require the protocol to handle worker eviction gracefully; this is orthogonal and worth its own design.

## 8. Open questions

- **Controller auth to substrates.** Does the controller hold long-lived credentials for each substrate, or short-lived tokens minted per provisioning request? Trade-off: blast radius if controller is compromised vs. complexity of token rotation.
- **Substrate adapter failure model.** When an adapter's `provision` call fails halfway through (database created, repo creation failed), how does the controller roll back? Two-phase commit across heterogeneous substrates is hard; the alternative is idempotent provision + a reconciliation loop.
- **Controller HA.** Today's control-plane-server is single-process. A real platform controller needs N-replica HA. Does the lease primitive from chapter 11 generalize, or do we need something fully consensus-backed (Raft / etcd / managed equivalent)?
- **Substrate adapter API surface.** Is the `register / provision / release / health` interface enough, or do we need richer queries (capacity, current usage, latency)? Schema evolution as new substrate kinds emerge.
- **Experiment config schema evolution.** When the controller's understanding of a config field changes, what does it do with already-running experiments? Pinned-by-version configs vs. live-migration semantics.
- **State sync between substrate and registry.** Today the chapter 11 §3 state-sync poller mirrors per-experiment state from the task-store. With substrates registered with the controller, more state needs syncing (substrate health, capacity utilization). What's the poller's contract under this expansion?

## 9. Implementation stepping stones

The platform vision lands incrementally. Each step delivers operator-visible value on its own.

| Step | What | Issue |
|---|---|---|
| 1 | Share Postgres + Forgejo substrates across experiments in the reference Compose stack | (to be filed alongside this PRD) |
| 2 | Lift task-store-server from single-experiment to N-experiment dispatch | (to be filed) |
| 3 | Make the orchestrator a persistent multi-experiment service | (to be filed) |
| 4 | Web UI deployment-level base page + experiment-scoped after selection | (to be filed) |
| 5 | Phase 13 substrate adapters (managed Postgres, S3/GCS blob, hardened Forgejo, k8s compute) | [`docs/plans/eden-phase-13a-helm-base-chart.md`](../plans/eden-phase-13a-helm-base-chart.md) + sibling phase-13 plans |
| 6 | Substrate-adapter abstraction layer in the controller | Future |
| 7 | Single `POST /v0/control/experiments` end-to-end provisioning | Future |
| 8 | Substrate inventory + scheduling | Future |

Steps 1–4 are the multi-experiment runtime work — necessary precursors. Step 5 is Phase 13 (already partially planned). Steps 6–8 are the platform synthesis on top of those foundations.

The PRD should be re-reviewed at each step to refine the open questions and possibly split into multiple PRDs as the design surfaces become real.

## 10. References

- [`docs/roadmap.md`](../roadmap.md) — the 13-phase build-up
- [`docs/plans/eden-phase-12c-control-plane.md`](../plans/eden-phase-12c-control-plane.md) — control plane as currently shipped
- [`docs/plans/eden-phase-13a-helm-base-chart.md`](../plans/eden-phase-13a-helm-base-chart.md) — Helm chart (k8s deployment shape)
- [`docs/plans/eden-phase-13c-managed-postgres.md`](../plans/eden-phase-13c-managed-postgres.md) — managed Postgres adapter
- [`docs/plans/eden-phase-13d-blob-backend.md`](../plans/eden-phase-13d-blob-backend.md) — S3/GCS object store adapter
- [`docs/plans/eden-phase-13e-gitea-hardening.md`](../plans/eden-phase-13e-gitea-hardening.md) — hardened git remote adapter
- [`spec/v0/11-control-plane.md`](../../spec/v0/11-control-plane.md) — control plane wire surface as it exists
- [`docs/glossary.md`](../glossary.md) — canonical vocabulary
