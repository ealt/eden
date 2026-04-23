# EDEN Reference Implementation

This directory contains one complete implementation of the EDEN
protocol. It is explicitly labeled **reference** — *one* valid
implementation, **not** *the* implementation. A third-party impl that
passes the conformance suite is equally valid.

**Targets:** `eden-protocol/v0` (draft — spec chapters not yet written).

## Status

Phase 0 scaffolding only. Nothing is runnable. Each component below is
a placeholder (`.gitkeep` only) until its phase lands.

### Services

| Path | Role | Lands in |
|---|---|---|
| [`services/control-plane/`](services/control-plane/) | Experiment registration, lease issuance | Phase 12 |
| [`services/orchestrator/`](services/orchestrator/) | Per-experiment dispatch + integrator | Phase 5 (in-proc) → Phase 8 (standalone) |
| [`services/planner/`](services/planner/) | Planner worker host | Phase 5 → Phase 8 |
| [`services/implementer/`](services/implementer/) | Implementer worker host | Phase 5 → Phase 8 |
| [`services/evaluator/`](services/evaluator/) | Evaluator worker host | Phase 5 → Phase 8 |
| [`services/web-ui/`](services/web-ui/) | Browser-based observability + role claim/submit | Phase 9 |

### Packages

| Path | Purpose | Lands in |
|---|---|---|
| [`packages/eden-contracts/`](packages/eden-contracts/) | Pydantic bindings for the JSON Schemas; convenience for Python components | Phase 3 |
| [`packages/eden-storage/`](packages/eden-storage/) | Repository interface + one concrete backend (SQLite MVP) | Phase 6 |
| [`packages/eden-git/`](packages/eden-git/) | Worktree + branch ops + integrator flow | Phase 7 |
| [`packages/eden-blob/`](packages/eden-blob/) | Blob storage interface + filesystem backend | Phase 10 |

### Scripts

| Path | Purpose | Lands in |
|---|---|---|
| [`scripts/setup-experiment/`](scripts/setup-experiment/) | CLI that registers an experiment, initializes the bare git repo, builds the experiment-specific image, creates per-service sub-configs | Phase 10 |

### Compose

| Path | Purpose | Lands in |
|---|---|---|
| [`compose/`](compose/) | Docker Compose stack for running the full reference system locally | Phase 10 |

## Relationship to the protocol spec

- When the spec and this implementation disagree, the spec wins and the
  code gets a bug.
- Schema files in [`../spec/v0/schemas/`](../spec/v0/schemas/) are the
  source of truth for wire formats. Pydantic models in
  `packages/eden-contracts/` are generated from / aligned with them;
  CI enforces parity from Phase 3 onward.
- This is not a monopoly. A different orchestrator in Go, a different
  worker host in Rust, a different storage backend on Postgres — all
  welcome as long as they pass conformance.

See [`../docs/roadmap.md`](../docs/roadmap.md) for the phase-by-phase
build-up plan.
