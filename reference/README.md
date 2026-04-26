# EDEN Reference Implementation

This directory contains one complete implementation of the EDEN protocol. It is explicitly labeled **reference** — *one* valid implementation, **not** *the* implementation. A third-party impl that passes the conformance suite is equally valid.

**Targets:** `eden-protocol/v0` (chapters 1, 2, 3, 4, 5, 6, 7, 8 written; conformance chapter and control-plane chapter pending).

## Status

Through **Phase 10 chunk 10a**: the [`compose/`](compose/) directory ships a Docker Compose stack that stands up the third-party infrastructure the reference services will consume — Postgres (`postgres:16.6-alpine`, reserved for `PostgresStore` in 10b), Gitea (`gitea/gitea:1.22.6-rootless`, headless via `INSTALL_LOCK=true` + `DISABLE_REGISTRATION=true`), and a one-shot `blob-init` busybox that mounts `eden-blob-data` so Compose actually creates the volume. EDEN services themselves are not yet dockerized; that lands in 10b alongside the `PostgresStore` binding.

Through **Phase 9** (chunks 9a–9e shipped): the reference Web UI hosts the planner / implementer / evaluator modules end-to-end plus an `/admin/*` observability + operator-reclaim surface. A human can sign in, claim a plan / implement / evaluate task, drive it through a browser, submit, and reclaim a stranded claim. Implementer-side, the user does git work in their own checkout, pushes their tip commit to the bare repo, then enters the resulting `commit_sha`; the UI verifies §3.3 reachability, creates the trial in `starting`, writes the canonical `work/<slug>-<trial_id>` ref, and submits with retry-before-orphan + committed-state read-back. Evaluator-side, the user inspects the trial out-of-band, then types metrics into a form generated from the experiment's `metrics_schema`; the UI parses each metric per its declared `MetricType` and submits with retry-before-orphan + read-back where `IllegalTransition` falls through to read-back so a "we won, response lost" sequence classifies as success rather than orphan. Admin-side, the operator browses tasks / trials / events, drives a `Store.reclaim(task_id, "operator")` per [`spec/v0/04-task-protocol.md`](../spec/v0/04-task-protocol.md) §5.1, and (when `--repo-path` is set) garbage-collects orphaned `work/*` refs via CAS-guarded `repo.delete_ref(expected_old_sha=…)`. Submissions round-trip through `eden_wire.StoreClient`. The implementer module is gated on `--repo-path`; planner- and evaluator-only deployments stay supported by omitting it.

### Services

| Path | Role | Lands in |
|---|---|---|
| [`services/_common/`](services/_common/) | Shared scaffolding (logging, signals, readiness, scripted profiles, repo seeding) | Phase 8b |
| [`services/task-store-server/`](services/task-store-server/) | Hosts the `Store` behind uvicorn over the chapter-07 wire binding | Phase 8b |
| [`services/orchestrator/`](services/orchestrator/) | Finalize + dispatch + integrate loop against a `StoreClient` | Phase 5 (in-proc) → Phase 8b (standalone) |
| [`services/planner/`](services/planner/) | Planner worker host (standalone process) | Phase 5 → Phase 8b |
| [`services/implementer/`](services/implementer/) | Implementer worker host (standalone process; writes real git commits) | Phase 5 → Phase 8b |
| [`services/evaluator/`](services/evaluator/) | Evaluator worker host (standalone process) | Phase 5 → Phase 8b |
| [`services/control-plane/`](services/control-plane/) | Experiment registration, lease issuance | Phase 12 |
| [`services/web-ui/`](services/web-ui/) | Browser-based UI shell + planner + implementer + evaluator + admin modules (BFF over `StoreClient`; implementer is opt-in via `--repo-path`; admin is unconditional, with the work-refs sub-page also opt-in via `--repo-path`) | Phase 9 chunks 9a + 9b + 9c + 9d + 9e |

### Packages

| Path | Purpose | Lands in |
|---|---|---|
| [`packages/eden-contracts/`](packages/eden-contracts/) | Pydantic bindings for the JSON Schemas; convenience for Python components | Phase 3 |
| [`packages/eden-dispatch/`](packages/eden-dispatch/) | Reference scripted workers, the orchestrator-iteration body (`run_orchestrator_iteration`), and the expired-claim sweeper (`sweep_expired_claims`); used by the worker hosts and the orchestrator service | Phase 5 / Phase 8b / Phase 8c / Phase 9 |
| [`packages/eden-storage/`](packages/eden-storage/) | Repository interface + concrete backends (in-memory, SQLite) | Phase 6 |
| [`packages/eden-git/`](packages/eden-git/) | Worktree + branch ops + integrator flow | Phase 7 |
| [`packages/eden-wire/`](packages/eden-wire/) | HTTP wire binding (FastAPI server + httpx client) for chapter 07; reference-only shared-token auth | Phase 8a / 8b |
| [`packages/eden-blob/`](packages/eden-blob/) | Blob storage interface + filesystem backend | Phase 10 |

### Scripts

| Path | Purpose | Lands in |
|---|---|---|
| [`scripts/setup-experiment/`](scripts/setup-experiment/) | CLI that registers an experiment, initializes the bare git repo, builds the experiment-specific image, creates per-service sub-configs | Phase 10 |

### Compose

| Path | Purpose | Lands in |
|---|---|---|
| [`compose/`](compose/) | Docker Compose stack — Phase 10a stands up the third-party infrastructure (Postgres, Gitea, blob volume); EDEN services dockerized in 10b | Phase 10 chunk 10a |

## Relationship to the protocol spec

- When the spec and this implementation disagree, the spec wins and the code gets a bug.
- Schema files in [`../spec/v0/schemas/`](../spec/v0/schemas/) are the source of truth for wire formats. Pydantic models in `packages/eden-contracts/` are generated from / aligned with them; CI enforces parity from Phase 3 onward.
- This is not a monopoly. A different orchestrator in Go, a different worker host in Rust, a different storage backend on Postgres — all welcome as long as they pass conformance.

See [`../docs/roadmap.md`](../docs/roadmap.md) for the phase-by-phase build-up plan.
