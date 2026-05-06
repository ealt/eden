# EDEN Protocol v0

This is the first lineage of the EDEN protocol specification. Chapters 00–08 are written as of Phase 8a (07-wire-protocol.md lands with the HTTP binding; the originally-slotted control-plane content will land as a later chapter in Phase 12). Chapter 09 (conformance) lands in Phase 11.

## Planned chapters

| File | Content | Target phase |
|---|---|---|
| `00-overview.md` | Protocol scope, goals, non-goals, terminology. | Phase 1 |
| `01-concepts.md` | Experiment, variant, idea, role, artifact, metric, worker. | Phase 1 |
| `02-data-model.md` | Canonical object shapes; narrative linking to JSON Schemas. | Phase 1 |
| `03-roles.md` | Ideator, executor, evaluator: contracts and outputs. | Phase 2 |
| `04-task-protocol.md` | Task state machine, claim tokens, submit idempotency, wire format. | Phase 2 |
| `05-event-protocol.md` | Event log shape, transactional invariant, delivery guarantees. | Phase 4 |
| `06-integrator.md` | Git topology: `work/*` / `variant/*` / `main` invariants; squash rule; evaluation manifest. | Phase 4 |
| `07-wire-protocol.md` | HTTP binding for chapters 4, 5, 6, and 8 (the storage-side operations the binding exposes). | Phase 8a |
| `08-storage.md` | Repository interface, durability, per-experiment metrics schemas. | Phase 4 |
| `09-conformance.md` | What a conforming implementation must prove. | Phase 11 |

## Planned schemas

JSON Schema files live under [`schemas/`](schemas/). None exist yet; the first six land in Phase 1 alongside the data-model chapter:

| File | Describes |
|---|---|
| `experiment-config.schema.json` | Experiment configuration YAML shape |
| `task.schema.json` | Task object (the unit of work dispatched to workers) |
| `event.schema.json` | Event log entry |
| `idea.schema.json` | Ideator-produced idea |
| `variant.schema.json` | Canonical variant record |
| `evaluation-schema.schema.json` | Meta-schema: how an experiment declares its own evaluation schema |

See [`docs/roadmap.md`](../../docs/roadmap.md) for the full build-up plan and the unit-level decomposition of each phase.
