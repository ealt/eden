# EDEN Protocol v0

This is the first lineage of the EDEN protocol specification. All twelve chapters are written; v0 is a **draft** (see [`00-overview.md`](00-overview.md) §5) — additive and clarifying changes may still land within the lineage, and a frozen `v0` release will be tagged once the chapter set and conformance suite stabilize together.

## Chapters

| File | Content |
|---|---|
| [`00-overview.md`](00-overview.md) | Protocol scope, conformance framing, versioning, document conventions. |
| [`01-concepts.md`](01-concepts.md) | Experiment, variant, idea, role, artifact, metric, worker, group; the experiment-durability invariant. |
| [`02-data-model.md`](02-data-model.md) | Canonical object shapes; narrative linking to the JSON Schemas. |
| [`03-roles.md`](03-roles.md) | Ideator, executor, evaluator, integrator, and orchestrator contracts and outputs. |
| [`04-task-protocol.md`](04-task-protocol.md) | Task state machine, identity-keyed claim ownership, submit idempotency, reclamation, reassignment, dispatch mode, experiment lifecycle ops. |
| [`05-event-protocol.md`](05-event-protocol.md) | Event log shape, transactional invariant, event registry, delivery guarantees. |
| [`06-integrator.md`](06-integrator.md) | Git topology: `work/*` / `variant/*` / `main` invariants; squash rule; evaluation manifest; integration atomicity. |
| [`07-wire-protocol.md`](07-wire-protocol.md) | HTTP binding for the task, event, idea/variant, worker/group, checkpoint, control-plane, and artifact operations; error vocabulary; auth. |
| [`08-storage.md`](08-storage.md) | Task store / event log / artifact store contracts, durability, per-experiment registries, evaluation-schema enforcement. |
| [`09-conformance.md`](09-conformance.md) | Conformance levels (v1, v1+roles, v1+roles+integrator, v1+checkpoints, v1+multi-experiment), the IUT contract, and the scenario index. |
| [`10-checkpoints.md`](10-checkpoints.md) | Portable checkpoint archive format, export/import semantics, recovery probe. |
| [`11-control-plane.md`](11-control-plane.md) | Deployment-level experiment registry, per-experiment leases, multi-replica orchestrator coordination. |

Two informative subdirectories accompany the normative chapters:

- [`design-notes/`](design-notes/) — rationale records for normative decisions (e.g. the integrator atomicity reading).
- [`reference-bindings/`](reference-bindings/) — non-normative descriptions of how the reference implementation binds implementation-defined surfaces (e.g. the worker-host subprocess protocol).

## Schemas

JSON Schema files live under [`schemas/`](schemas/) (entity shapes) and [`schemas/wire/`](schemas/wire/) (request/response bodies for the chapter-7 HTTP binding). Each is cited from the chapter that defines its semantics, and the `schema-validity` / `schema-parity` CI jobs keep them valid and in lockstep with the reference Pydantic bindings.

Entity schemas: `experiment-config`, `experiment`, `task`, `event`, `idea`, `variant`, `evaluation-schema`, `worker`, `group`, `lease`, `artifact-metadata`, `checkpoint-manifest`.

Wire schemas: the `claim` / `submit` / `reclaim` / `reject` / `reassign` / `integrate` / `terminate` / `dispatch-mode` / `policy-error` request-response pairs, worker and group registration shapes, `whoami`, `events-response`, `experiment-state-response`, `deposit-artifact-response`, and the RFC 7807 `error` profile.

See [`docs/roadmap.md`](../../docs/roadmap.md) for the build-up plan that produced the chapter set and [`CHANGELOG.md`](../../CHANGELOG.md) for per-chunk completion records.
