# EDEN Reference Implementation

This directory contains one complete implementation of the EDEN protocol. It is explicitly labeled **reference** — *one* valid implementation, **not** *the* implementation. A third-party impl that passes the conformance suite is equally valid.

**Targets:** `eden-protocol/v0` (all twelve chapters, `00`–`11`, are written; see [`../spec/v0/README.md`](../spec/v0/README.md)).

## Status

The reference implementation is a Python `uv` workspace: protocol libraries under [`packages/`](packages/), deployable processes under [`services/`](services/), and two first-class deployment substrates — Docker Compose ([`compose/`](compose/)) and a Helm chart for Kubernetes ([`helm/eden/`](helm/eden/)). It passes the conformance suite under [`../conformance/`](../conformance/) at every currently-shipped level.

Highlights of what runs today:

- **Full multi-process stack**: task-store-server (in-memory / SQLite / Postgres backends), orchestrator, ideator / executor / evaluator worker hosts, web UI, and an optional control plane, all speaking the chapter-07 HTTP binding with per-worker bearer credentials.
- **Worker hosts in three execution modes**: `--mode scripted` (deterministic built-ins), `--mode subprocess` (user-supplied `*_command` per the [worker-host-subprocess binding](../spec/v0/reference-bindings/worker-host-subprocess.md)), and `--exec-mode docker` (each spawn isolated in a sibling container via DooD).
- **Forgejo as the git remote of record**: workers hold private clones, push `work/*` branches over HTTP, and the integrator publishes canonical `variant/*` refs back per the chapter-06 atomicity ladder.
- **Web UI** hosting human-driven ideator / executor / evaluator flows plus an `/admin/*` surface: task / variant / event observability, operator reclaim + reassign, dispatch-mode toggles, worker/group management, `work/*` ref GC, and (with a control plane) an experiment switcher.
- **Durability + portability**: experiment state survives restarts (host bind-mounts under `EDEN_EXPERIMENT_DATA_ROOT`), portable checkpoint export/import per chapter 10 (manual + automatic), and blob-backed artifacts (`file` / `s3` / `gcs`).
- **Multi-experiment coordination**: the control-plane service implements the chapter-11 experiment registry + per-experiment leases; multi-replica orchestrators hand off on lease expiry.

### Services

| Path | Role |
|---|---|
| [`services/_common/`](services/_common/) | Shared scaffolding (logging, signals, readiness, CLI helpers, credential bootstrap, scripted profiles, subprocess + DooD execution, repo seeding) |
| [`services/task-store-server/`](services/task-store-server/) | Hosts the `Store` behind uvicorn over the chapter-07 wire binding; owns the blob-backed artifact store and checkpoint export/import |
| [`services/orchestrator/`](services/orchestrator/) | Termination / ideation / dispatch / integration decision loop against a `StoreClient`; single-experiment and lease-driven multi-experiment modes; auto-checkpointing |
| [`services/ideator/`](services/ideator/) | Ideator worker host (scripted or long-running subprocess) |
| [`services/executor/`](services/executor/) | Executor worker host (scripted or per-task subprocess; writes real git commits on `work/*`) |
| [`services/evaluator/`](services/evaluator/) | Evaluator worker host (scripted or per-task subprocess; metrics validated against the experiment's `evaluation_schema`) |
| [`services/control-plane/`](services/control-plane/) | Chapter-11 experiment registry, lease issuance, deployment-scoped worker registry, state-sync poller |
| [`services/web-ui/`](services/web-ui/) | Browser BFF over `StoreClient`: ideator + executor + evaluator + admin modules (executor is opt-in via `--repo-path`) |

### Packages

| Path | Purpose |
|---|---|
| [`packages/eden-contracts/`](packages/eden-contracts/) | Pydantic bindings for the JSON Schemas; CI-enforced schema ↔ model parity |
| [`packages/eden-storage/`](packages/eden-storage/) | `Store` protocol + in-memory / SQLite / Postgres backends; artifact backends (`file` / `s3` / `gcs`); checkpoint export/import ops |
| [`packages/eden-wire/`](packages/eden-wire/) | Chapter-07 HTTP binding: FastAPI `make_app(store)` server + httpx `StoreClient`; chapter-07 §13 bearer auth |
| [`packages/eden-dispatch/`](packages/eden-dispatch/) | `run_orchestrator_iteration`, scripted workers, ideation + termination policies, expired-claim sweeper |
| [`packages/eden-git/`](packages/eden-git/) | `GitRepo` subprocess wrapper (local + remote ops) and the chapter-06 `Integrator` |
| [`packages/eden-checkpoint/`](packages/eden-checkpoint/) | Chapter-10 portable checkpoint format: manifest, tar envelope, repo bundle, content-addressed artifacts |
| [`packages/eden-control-plane/`](packages/eden-control-plane/) | Chapter-11 models, `ControlPlaneStore` protocol + backends, `ControlPlaneClient` |
| [`packages/eden-blob/`](packages/eden-blob/) | Unused placeholder — the blob backends shipped in `eden-storage` instead (see its README) |

### Scripts and deployment

| Path | Purpose |
|---|---|
| [`scripts/setup-experiment/`](scripts/setup-experiment/) | One-shot Compose bootstrap: secrets, image build, Forgejo provisioning, bare-repo seed, `.env` generation |
| [`scripts/setup-experiment-helm.sh`](scripts/setup-experiment-helm.sh) | Kubernetes analogue: three-phase `helm upgrade --install` + per-experiment repo-init Job + identity minting |
| [`scripts/setup-aws/`](scripts/setup-aws/) | Idempotent AWS provisioning upstream of the Helm setup (EKS, ECR, RDS, S3 + IRSA) |
| [`compose/`](compose/) | Docker Compose stack (Postgres, Forgejo, all EDEN services) plus overlays: subprocess, docker-exec, logging, multi-orchestrator, multi-experiment |
| [`helm/eden/`](helm/eden/) | Helm chart deploying the same stack on Kubernetes 1.27+; embedded or managed Postgres; file / S3 / GCS blob modes |

## Relationship to the protocol spec

- When the spec and this implementation disagree, the spec wins and the code gets a bug.
- Schema files in [`../spec/v0/schemas/`](../spec/v0/schemas/) are the source of truth for wire formats. Pydantic models in `packages/eden-contracts/` are aligned with them; the `schema-parity` CI job enforces it.
- This is not a monopoly. A different orchestrator in Go, a different worker host in Rust, a different storage backend — all welcome as long as they pass conformance.

See [`../docs/roadmap.md`](../docs/roadmap.md) for the phase-by-phase build-up plan and [`../CHANGELOG.md`](../CHANGELOG.md) for per-chunk completion records.
