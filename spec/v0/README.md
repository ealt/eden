# EDEN Protocol v0

This is the first lineage of the EDEN protocol specification. It is
currently **in development** — no normative chapters exist yet.

## Planned chapters

| File | Content | Target phase |
|---|---|---|
| `00-overview.md` | Protocol scope, goals, non-goals, terminology. | Phase 1 |
| `01-concepts.md` | Experiment, trial, proposal, role, artifact, metric, worker. | Phase 1 |
| `02-data-model.md` | Canonical object shapes; narrative linking to JSON Schemas. | Phase 1 |
| `03-roles.md` | Planner, implementer, evaluator: contracts and outputs. | Phase 2 |
| `04-task-protocol.md` | Task state machine, claim tokens, submit idempotency, wire format. | Phase 2 |
| `05-event-protocol.md` | Event log shape, transactional invariant, delivery guarantees. | Phase 4 |
| `06-integrator.md` | Git topology: `work/*` / `trial/*` / `main` invariants; squash rule; eval manifest. | Phase 4 |
| `07-control-plane.md` | Experiment registration, leases, single-writer discipline. | Phase 12 |
| `08-storage.md` | Repository interface, durability, per-experiment metrics schemas. | Phase 4 |
| `09-conformance.md` | What a conforming implementation must prove. | Phase 11 |

## Planned schemas

JSON Schema files live under [`schemas/`](schemas/). None exist yet;
the first six land in Phase 1 alongside the data-model chapter:

| File | Describes |
|---|---|
| `experiment-config.schema.json` | Experiment configuration YAML shape |
| `task.schema.json` | Task object (the unit of work dispatched to workers) |
| `event.schema.json` | Event log entry |
| `proposal.schema.json` | Planner-produced proposal |
| `trial.schema.json` | Canonical trial record |
| `metrics-schema.schema.json` | Meta-schema: how an experiment declares its own metrics schema |

See [`docs/roadmap.md`](../../docs/roadmap.md) for the full build-up
plan and the unit-level decomposition of each phase.
