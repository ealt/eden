# EDEN Roadmap

Incremental build-up from empty repo to a complete EDEN protocol specification, reference implementation, and conformance suite.

## Two orthogonal concepts

- **Units** — the fine-grained decomposition. Each unit is a distinct, named piece of work with its own exit criterion. Units are the progress-tracking granularity: a unit can be crossed off even when it ships in a shared commit with siblings.
- **Chunks** — execution grouping. Units that must be designed and implemented together (cross-referencing spec chapters, protocol alongside its first consumer, shared commit-sized work) travel as one chunk. Chunks are the review/commit granularity.

**Rule:** a chunk can span multiple units; no unit spans chunks. If implementation runs over context mid-chunk, stop at the next unit boundary — never mid-unit. That keeps handoffs, resumes, and partial progress legible across sessions.

Each phase also has explicit **non-goals** to prevent creep.

---

## Phase 0 — Bootstrap

Repo shape, documentation shell, CI, GitHub hygiene. No runnable code and no spec content.

**Units:**

- **0a** — Repo scaffold (directory tree + section READMEs + root docs + CI workflow + `.gitignore` + `.markdownlint.json` + `CLAUDE.md` symlink). First `main` commit on GitHub; branch protection requiring `docs-lint`.

**Chunk:** 0a is one chunk — this bootstrap.

**Non-goals:** any spec chapter content; any JSON Schema files; any Python; any reference code; any conformance scenarios.

**Exit:** `docs-lint` CI job green on `main`; protected branch in place; all root and section READMEs accurately describe what's in the repo and what's next.

---

## Phase 1 — Spec v0 core concepts + schemas + fixture migration

Writes the first normative chapters and the JSON Schemas they describe.

**Units:**

- **1a** — `spec/v0/00-overview.md`, `01-concepts.md`, `02-data-model.md` prose. Defines experiment, trial, proposal, role, artifact, metric, worker.
- **1b** — JSON Schemas for config, task, event, proposal, trial, metrics-meta under `spec/v0/schemas/`. Each cited from the data-model chapter.
- **1c** — Migrate `tests/fixtures/experiment/.eden/config.yaml` from direvo; assert `experiment-config.schema.json` validates it. Add a `schema-validity` CI job.

**Chunk:** 1a + 1b + 1c — one chunk. Prose, schemas, and fixture cross-constrain each other; splitting would produce inconsistency.

**Non-goals:** role contracts (Phase 2); event/integrator/storage chapters (Phase 4); any Pydantic code (Phase 3).

**Exit:** schemas validate the fixture; `schema-validity` CI green; `02-data-model.md` cross-references all six schemas.

---

## Phase 2 — Spec v0 role contracts + task protocol

Two more chapters. Defines what each role does and the task lifecycle they operate against.

**Units:**

- **2a** — `03-roles.md`. Planner, implementer, evaluator contracts: discovery, claiming, context reading, execution, submission, release. Per-role outputs and their expected schema shapes.
- **2b** — `04-task-protocol.md`. Full task state machine (legal and illegal transitions enumerated), claim-token semantics, submit idempotency rule, wire format for task objects.

**Chunk:** 2a + 2b — one chunk. Role contracts reference task lifecycle and vice versa.

**Non-goals:** event protocol (Phase 4); any implementation (Phase 5+).

**Exit:** state machine is pinned; `04-task-protocol.md` enumerates every legal transition with its pre- and post-conditions.

---

## Phase 3 — Reference contracts package (`eden-contracts`)

First real Python code: Pydantic bindings for the v0 JSON Schemas. Introduces the uv workspace + Python toolchain.

**Units:**

- **3a** — Pydantic models for the six v0 schemas, along with the root `pyproject.toml` (uv workspace root), a per-package `reference/packages/eden-contracts/pyproject.toml`, and a `.python-version` file. Ruff/pyright config ported from direvo. CI jobs: `python-lint` (ruff), `python-typecheck` (pyright), `python-test` (pytest).
- **3b** — Schema ↔ model parity check. CI job `schema-parity` that loads each JSON Schema and asserts the matching Pydantic model can round-trip any instance; fails CI on drift.

**Chunk:** 3a + 3b — one chunk.

**Non-goals:** any domain logic, just data types; storage, git, dispatch (later phases).

**Exit:** all six Pydantic models exist and parity CI is green against the Phase 1 schemas.

---

## Phase 4 — Spec v0 events + integrator + storage

Three more chapters. Heavy cross-referencing: the integrator emits events; storage persists them.

**Units:**

- **4a** — `05-event-protocol.md` + `event.schema.json` refinement. Transactional invariant (state change + event insert must commit atomically); delivery guarantees subscribers can rely on.
- **4b** — `06-integrator.md`. Git topology invariants: `work/*`, `trial/*`, `main` namespaces; sole-integrator rule; squash rule; eval-manifest shape (metrics + blob URIs + hashes).
- **4c** — `08-storage.md`. Repository interface as protocol-level contract; durability requirements; per-experiment metrics schemas.

**Chunk:** 4a + 4b + 4c — one chunk. Cross-references are dense and splitting risks inconsistency.

**Non-goals:** control-plane (Phase 12); conformance chapter (Phase 11); any code.

**Exit:** three chapters land with consistent cross-references; schema updates (if any) pass parity.

---

## Phase 5 — In-memory reference dispatch loop

First executable implementation. Proves the spec's state machines are implementable. Single process; no git yet; no persistence.

**Units:**

- **5a — complete.** `eden_dispatch.InMemoryStore` collapses the task store, event log, and proposal/trial persistence into one in-process object. Every mutating operation stages writes in a `_Tx` and applies them in a single `_commit`; precondition failures raise before commit, so readers never observe a partial state change. All six composite-commit patterns from `05-event-protocol.md` §2.2 land atomically.
- **5b — complete.** `ScriptedPlanner`, `ScriptedImplementer`, and `ScriptedEvaluator` participate in the real task protocol (claim → execute → submit) with deterministic fake outputs. `run_experiment` orchestrates a full plan → implement → evaluate → integrate cycle.
- **5c — complete.** 36 pytest scenarios cover every legal transition in §1.2, every negative rule, claim-token authorization (§3.3), idempotent resubmit (§4.2), terminal immutability (§4.4), the reclamation policy (§5), every composite commit (§2.2), and a 3-trial end-to-end experiment whose event log alone reconstructs every entity's lifecycle.

**Chunks:** 5a one chunk; 5b + 5c one chunk (scenarios need a worker harness to drive).

**Non-goals:** git integration; SQLite persistence; cross-process.

**Exit achieved:** the in-memory loop runs a 3-trial experiment end to end; 36 conformance scenarios pass; all CI gates green.

---

## Phase 6 — Reference storage backend (`eden-storage`) — complete

Makes Phase 5 persist across restarts.

**Units:**

- **6a — complete.** `Store` Protocol in `reference/packages/eden-storage/ src/eden_storage/protocol.py` covers the task store, event log reads (`replay`, `read_range`; `events()` retained as alias), and proposal/trial persistence sides of chapter 8; the spec-literal `create_task(task)` sits alongside typed convenience helpers (`create_plan_task`, etc.). The artifact store (§5), `subscribe` streaming (§2.1), and cross-process wire transport are out of scope for Phase 6 and are tracked for Phase 10 / Phase 8 respectively. Shared transition logic lives in `_base.py`; `InMemoryStore` (moved from `eden-dispatch`) and the new `SqliteStore` both satisfy the Protocol and inherit the same validation and composite-commit paths.
- **6b — complete.** SQLite-backed backend in `eden_storage/sqlite.py` with a linear-migration schema in `_schema.py`. Every public op opens a `BEGIN IMMEDIATE` transaction; commit on success, rollback on exception. `WAL` + `synchronous = FULL` provides fsync-on-commit durability matching the strict reading of chapter 8 §3.1 ("survives a subsequent crash of the store's host"). A reopened store resumes the event-id counter from the persisted event count (via the AUTOINCREMENT `seq` column — format-independent), inherits the metrics schema, and rejects a reopen whose `experiment_id` mismatches or whose `metrics_schema` differs semantically (compared via `sort_keys=True`, so key-order noise doesn't false-trigger §4.2). Migrations run inside an explicit `BEGIN`/`COMMIT` so a partial DDL failure rolls back cleanly. Conformance scenarios (65 per backend) run via a parametrized `make_store` fixture, and 9 restart-safety tests close/reopen a SQLite store mid-experiment and verify state/events/tokens survive. A monkey-patched `_apply_commit` failure test asserts rollback.

**Chunk:** 6a + 6b — one chunk, shipped together.

**Non-goals:** Postgres (later); blob storage (Phase 10).

**Exit:** restart-safe 3-trial experiment; existing conformance scenarios still pass.

---

## Phase 7 — Reference git integrator (`eden-git`)

Git topology: `work/*` branches for implementers, `trial/*` for canonical records, squashed trial commits with eval manifests.

**Units:**

- **7a — complete.** `eden-git` package with `GitRepo` subprocess wrapper: ref/object inspection (`rev_parse`, `resolve_ref`, `list_refs`, `is_ancestor`, `ls_tree`, `tree_entry_exists`, `commit_parents`), plumbing (`write_blob`, `write_tree_from_entries`, `write_tree_with_file`, `commit_tree`, `create_ref`, `update_ref`, `delete_ref`) and worktree/branch management. `commit.gpgsign=false` and explicit `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env vars per invocation isolate integrator runs from the user's ambient git config.
- **7b — complete.** `eden_git.Integrator` composes `GitRepo` with a `Store` to promote `success` trials: builds the §3.2 single-commit squash, writes the `refs/heads/trial/<id>-<slug>` ref via zero-oid CAS, routes the store's atomic `integrate_trial` write for `trial_commit_sha` + `trial.integrated`, and compensating-deletes the ref on store-side failure per §3.4. Re-invocation on a promoted trial is a verified no-op (§5.3). §2 preconditions, §1.4 reachability, and metrics validity are all enforced via the new public `Store.validate_metrics`. Spec §3.4 was tightened to make the post-promotion atomicity reading explicit; rationale in [`spec/v0/design-notes/integrator-atomicity.md`](../spec/v0/design-notes/integrator-atomicity.md). `eden-dispatch.run_experiment` now takes `integrate_trial: Callable[[str], object]` in place of the Phase 5 placeholder factory.

**Chunks:** 7a one chunk (complete); 7b one chunk (complete).

**Non-goals:** Gitea / remote hosts (Phase 10); multi-parent proposals (deferred beyond v0).

**Exit:** trials produce canonical `trial/*` commits against a local bare repo; eval manifest shape matches spec chapter 6.

---

## Phase 8 — Cross-process reference

Extracts the dispatch loop into separate processes over a wire protocol. First point at which a third-party component in any language could participate.

**Units:**

- **8a — complete.** [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) pins the HTTP binding for chapters 4, 5, 6 §3.4, and 8 §§1.1–2.1. The `eden-wire` package ships a FastAPI `make_app(store)` server plus an httpx-backed `StoreClient` that satisfies the same `Store` Protocol. `Store.integrate_trial` is now same-value idempotent so HTTP retries are safe; the `Integrator` distinguishes different-SHA divergence (`AtomicityViolation`) from compensable synchronous rejections; `StoreClient` resolves transport-indeterminate failures via read-back. Long-poll and non-blocking polling both bind the §2.1 `subscribe` operation. Errors round-trip as RFC 7807 problem+json with a closed `eden://error/<name>` vocabulary (chapter §7).
- **8b — complete.** Five reference services under [`reference/services/`](../reference/services/): `task-store-server` (uvicorn-hosted `Store`), `orchestrator` (finalize + dispatch + integrate loop driven by a `StoreClient`), and standalone worker hosts `planner`, `implementer`, `evaluator`. Shared scaffolding lives in `reference/services/_common/` (JSON-line logger, SIGTERM stop flag, readiness probe, common argparse, scripted role profiles, `seed_bare_repo`). `eden-dispatch.run_experiment` was split into `run_orchestrator_iteration` (now public) so the standalone orchestrator runs only the orchestrator side. `eden-wire.make_app(store, *, shared_token=…)` and `eden-wire.StoreClient(…, token=…)` add reference-only bearer-token auth; misauth surfaces as `eden://reference-error/unauthorized` (HTTP 401), kept out of the normative `eden://error/…` vocabulary by design. The new informative §12 in [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) documents the scheme. Implementer host writes real git commits (single-parent or merge, honoring `proposal.parent_commits`) on `work/*` branches in the bare repo the orchestrator integrates from. A `pytest.mark.e2e` test forks all five processes on an ephemeral port and drives a 3-trial experiment to quiescence over real HTTP + SQLite + git in ~3 seconds.
- **8c** — Cut-over: remove all in-process code paths; the only way components talk is the wire protocol.

**Chunks:** 8a one chunk (protocol + first consumer coupled); 8b one chunk; 8c one chunk.

**Non-goals:** UI (Phase 9); Compose packaging (Phase 10); k8s (Phase 13).

**Exit:** 3-trial experiment runs with each role in its own process; conformance scenarios pass against the HTTP surface.

---

## Phase 9 — Reference Web UI

Browser-based claim/submit flows for each role, plus observability.

**Units:**

- **9a** — UI shell: routing, auth stub, navigation, experiment list.
- **9b** — Planner module: claim / markdown form / submit.
- **9c** — Implementer module: claim / manifest / submit SHA.
- **9d** — Evaluator module: claim / metrics form / artifact upload.
- **9e** — Observability views (trial timeline, task queue filtered by kind + claim status) + admin-reclaim action on stranded claims.

**Chunks:** 9a + 9b one chunk (shell + first role module together — the first role establishes the component pattern); 9c one; 9d one; 9e one.

**Non-goals:** full auth / multi-tenancy (Milestone 3); in-UI code editing (implementers work in their own environment).

**Exit:** a human can fully play any one role for at least one trial via the Web UI; admin-reclaim on a stranded claim works end-to-end.

---

## Phase 10 — Reference Compose stack

Everything runs locally via `docker compose up`. Equivalent to the old microservices plan's Milestone 1, now explicitly "one valid deployment topology."

**Units:**

- **10a** — Infrastructure containers (Postgres for durable backend, Gitea for git host, blob volume) + Compose skeleton that stands them up.
- **10b** — Each reference service dockerized with its own image.
- **10c** — `setup-experiment` script: registers an experiment end-to-end (builds experiment-specific image, initializes the bare git repo, writes per-service sub-configs, registers with the control plane).
- **10d** — LLM worker hosts: planner host (context-accumulating Claude session), implementer host (spawns per-task sandbox containers running `implement_command`), evaluator host.
- **10e** — End-to-end Compose integration test exercising the full loop including Web UI, admin-reclaim, and termination.

**Chunks:** 10a one; 10b + 10c one chunk (dockerize + setup script co-evolve — each image's env vars and entrypoints feed the script); 10d one; 10e one.

**Non-goals:** k8s (Phase 13); S3 blob backend (Phase 13); Gitea auth (Phase 13).

**Exit:** the fixture experiment runs to completion in Compose with comparable results to direvo's monolith; e2e test green in CI.

---

## Phase 11 — Conformance suite v1

Formalizes black-box scenarios that any component can run against itself.

**Units:**

- **11a** — Harness: fixture infrastructure + implementation-under-test adapter + scenario-execution driver. First scenarios validate the harness itself (bootstrap problem).
- **11b** — State-machine scenarios (task lifecycle, claim tokens, transactional event invariant) — expands on Phase 5's scenarios.
- **11c** — Role-contract scenarios (per-role submission semantics, backpressure, idempotency).
- **11d** — Integrator scenarios (squash shape, eval-manifest shape, `work/*` access discipline).

**Chunks:** 11a + 11b one chunk (harness needs first scenarios to validate itself); 11c one; 11d one.

**Non-goals:** multi-experiment scenarios (Phase 12); k8s-specific tests (Phase 13).

**Exit:** reference impl passes the full v1 suite; suite is documented as the conformance contract for `eden-protocol/v0`.

---

## Phase 12 — Multi-experiment (leases, control plane, switcher)

Units and chunking to be named closer to execution — too far ahead to estimate coupling accurately. Scope:

- Control plane service + lease data model.
- Multi-replica orchestrator; chaos test: kill lease-holder, another replica takes over.
- Cross-experiment views in the shared planner.
- Experiment switcher in the Web UI.
- Multi-experiment conformance scenarios.

---

## Phase 13 — Kubernetes reference deployment

Units and chunking to be named closer to execution. Scope:

- Base Helm chart for the reference services.
- Implementer as a k8s Job (GPU node selection).
- Managed Postgres migration.
- S3/GCS blob backend.
- Gitea with auth + per-branch ACLs + native PR review enabled.
