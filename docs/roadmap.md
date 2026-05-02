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
- **5b — complete.** `ScriptedPlanner`, `ScriptedImplementer`, and `ScriptedEvaluator` participate in the real task protocol (claim → execute → submit) with deterministic fake outputs. The Phase 5 in-process `run_experiment` driver orchestrated a full plan → implement → evaluate → integrate cycle in a single process; it was deleted in Phase 8c, with the same flow now exercised end-to-end by the real-subprocess `pytest.mark.e2e` test.
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
- **7b — complete.** `eden_git.Integrator` composes `GitRepo` with a `Store` to promote `success` trials: builds the §3.2 single-commit squash, writes the `refs/heads/trial/<id>-<slug>` ref via zero-oid CAS, routes the store's atomic `integrate_trial` write for `trial_commit_sha` + `trial.integrated`, and compensating-deletes the ref on store-side failure per §3.4. Re-invocation on a promoted trial is a verified no-op (§5.3). §2 preconditions, §1.4 reachability, and metrics validity are all enforced via the new public `Store.validate_metrics`. Spec §3.4 was tightened to make the post-promotion atomicity reading explicit; rationale in [`spec/v0/design-notes/integrator-atomicity.md`](../spec/v0/design-notes/integrator-atomicity.md). The dispatch driver accepted `integrate_trial: Callable[[str], object]` in place of the Phase 5 placeholder factory; in Phase 8c that hook moved to `run_orchestrator_iteration`.

**Chunks:** 7a one chunk (complete); 7b one chunk (complete).

**Non-goals:** Gitea / remote hosts (Phase 10); multi-parent proposals (deferred beyond v0).

**Exit:** trials produce canonical `trial/*` commits against a local bare repo; eval manifest shape matches spec chapter 6.

---

## Phase 8 — Cross-process reference

Extracts the dispatch loop into separate processes over a wire protocol. First point at which a third-party component in any language could participate.

**Units:**

- **8a — complete.** [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) pins the HTTP binding for chapters 4, 5, 6 §3.4, and 8 §§1.1–2.1. The `eden-wire` package ships a FastAPI `make_app(store)` server plus an httpx-backed `StoreClient` that satisfies the same `Store` Protocol. `Store.integrate_trial` is now same-value idempotent so HTTP retries are safe; the `Integrator` distinguishes different-SHA divergence (`AtomicityViolation`) from compensable synchronous rejections; `StoreClient` resolves transport-indeterminate failures via read-back. Long-poll and non-blocking polling both bind the §2.1 `subscribe` operation. Errors round-trip as RFC 7807 problem+json with a closed `eden://error/<name>` vocabulary (chapter §7).
- **8b — complete.** Five reference services under [`reference/services/`](../reference/services/): `task-store-server` (uvicorn-hosted `Store`), `orchestrator` (finalize + dispatch + integrate loop driven by a `StoreClient`), and standalone worker hosts `planner`, `implementer`, `evaluator`. Shared scaffolding lives in `reference/services/_common/` (JSON-line logger, SIGTERM stop flag, readiness probe, common argparse, scripted role profiles, `seed_bare_repo`). `eden-dispatch` exposes `run_orchestrator_iteration` as its orchestrator-side entry point so the standalone orchestrator drives only that half; the in-process `run_experiment` driver was kept through 8b and removed in 8c. `eden-wire.make_app(store, *, shared_token=…)` and `eden-wire.StoreClient(…, token=…)` add reference-only bearer-token auth; misauth surfaces as `eden://reference-error/unauthorized` (HTTP 401), kept out of the normative `eden://error/…` vocabulary by design. The new informative §12 in [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) documents the scheme. Implementer host writes real git commits (single-parent or merge, honoring `proposal.parent_commits`) on `work/*` branches in the bare repo the orchestrator integrates from. A `pytest.mark.e2e` test forks all five processes on an ephemeral port and drives a 3-trial experiment to quiescence over real HTTP + SQLite + git in ~3 seconds.
- **8c — complete.** In-process dispatch path removed: `eden-dispatch.run_experiment` and the Phase 5 / 7b in-process end-to-end tests in `reference/packages/eden-dispatch/tests/test_end_to_end.py` are deleted; `eden-dispatch` keeps `run_orchestrator_iteration` plus the scripted workers as its public surface. The lifecycle-reconstruction invariant from the deleted file moved into `reference/services/orchestrator/tests/test_e2e.py` as a `_reconstruct_lifecycle` fold over the post-teardown `SqliteStore.read_range()`. The malformed-success → `validation_error` routing case (`test_store_hardening`) was rewritten to drive `run_orchestrator_iteration` directly through the legitimate `Store` API. The real-subprocess `pytest.mark.e2e` test from 8b is now the only end-to-end coverage path.

**Chunks:** 8a one chunk (protocol + first consumer coupled); 8b one chunk; 8c one chunk.

**Non-goals:** UI (Phase 9); Compose packaging (Phase 10); k8s (Phase 13).

**Exit:** 3-trial experiment runs with each role in its own process; conformance scenarios pass against the HTTP surface.

---

## Phase 9 — Reference Web UI

Browser-based claim/submit flows for each role, plus observability.

**Units:**

- **9a — complete.** UI shell shipped: FastAPI + Jinja2 BFF over `eden_wire.StoreClient`, routing, sign-in stub, navigation, experiment-overview index. Server-side rendered, no JS framework. Session cookie is `itsdangerous`-signed with HttpOnly + SameSite=Lax + Path=/, plus per-session CSRF token validated on every mutating route.
- **9b — complete.** Planner module shipped: claim-with-TTL, markdown rationale form, 3-phase write (`drafting` → `ready` → `submit`), retry-before-orphan on transport failures via chapter 07 §2.4 / §8.1 idempotent resubmit. Cross-cutting prerequisite: `eden_dispatch.sweep_expired_claims` runs once per orchestrator iteration so abandoned UI claims are recovered automatically.
- **9c — complete.** Implementer module shipped, gated on the new optional `--repo-path` CLI flag: claim with TTL, draft form rendering proposal context (slug / priority / parent_commits / `artifacts_uri` plus inline rationale when the file resolves inside `--artifacts-dir` and is ≤ 1 MiB per the §A.1 trust boundary), §3.3 reachability checks via `GitRepo.commit_exists` + `is_ancestor` against every `parent_commits` entry, Pre-Phase-1 ref-collision guard, then Phase-1 (`create_trial(starting)`) → Phase-2 (`create_ref work/<slug>-<trial_id>`) → Phase-3 (`submit` with retry-before-orphan plus committed-state read-back via `read_task` + `read_submission` + `submissions_equivalent`). `trial_id` is server-only (in-process `_CLAIMS` dict; never round-trips). Orphaned `starting` trials auto-recover through the chunk-1 expired-claim sweeper's `_base.reclaim` composite-commit.
- **9d — complete.** Evaluator module shipped, mounted unconditionally (no CLI flag): claim with TTL (the `trial_id` is read from `task.payload.trial_id` and stashed in an in-process `_CLAIMS` dict, never round-tripping through the request surface), draft form rendering trial-under-evaluation context (trial_id / branch / commit_sha / parent_commits, plus the implementer-set `trial.description` and `trial.artifacts_uri` per §3.2 step 3, with the chunk-9c scheme allowlist + the same ≤ 1 MiB confinement applied to `trial.artifacts_uri`) + proposal context + one input per metric in `experiment_config.metrics_schema` typed by `MetricType`. Submission validates (status ∈ {success, error, eval_error}; per-metric type rules including the wire-legal integer form `1.0` per `02-data-model.md` §1.3 and rejection of non-finite reals; `status=success` requires ≥ 1 metric value), then runs `store.submit` with retry-before-orphan + read-back where `WrongToken` / `ConflictingResubmission` short-circuit, `InvalidPrecondition` re-renders the form (fixable), and **`IllegalTransition` falls through to read-back** so a "we won, response lost, orchestrator already terminalized" sequence correctly classifies as success rather than orphan. The chunk-9c trust-boundary helper was generalized to `_read_inline_artifact(uri, artifacts_dir)`; `read_proposal_rationale` is a thin wrapper and a new `read_trial_artifact` covers the trial-side surface.
- **9e — complete.** Admin / observability surface shipped under `/admin/*`: read-only task / trial / event tables (filterable, capped, with claim-age + claim-expired badges and an orphaned-starting-trial badge), a per-task detail page exposing operator `reclaim` via `Store.reclaim(task_id, "operator")` per [`spec/v0/04-task-protocol.md`](../spec/v0/04-task-protocol.md) §5.1 (separate "reclaim" / "force-reclaim (replays work)" UI variants for `claimed` vs `submitted`; closed allowlist of banner outcomes), a per-trial detail page whose related-events filter unions `event.data.trial_id == trial_id` with task-id matches for the implement task that produced this trial and any evaluate task that references it, ordered by replay-index position (the `event_id` factory is pluggable and not a reliable ordering contract), and a `work/*` ref GC page (when `--repo-path` is set) that classifies refs by exact `trial.branch` equality — not by parsing the ref name — and offers CAS-guarded `repo.delete_ref(ref_name, expected_old_sha=…)` deletion only for terminal-and-handled trials whose `commit_sha` matches the live ref SHA, plus orphan-ref deletion when no trial owns the branch. Auth-first POST discipline matches the existing modules (`get_session` runs before `csrf_ok`, so unauth → 303 `/signin`, missing-CSRF → 403). `make_app` adds an `@app.exception_handler(eden_storage.errors.NotFound)` so raised storage/wire `NotFound` renders the 404 error page (the prior handler only caught HTTP 404 *responses*).

**Chunks:** 9a + 9b one chunk (complete); 9c one chunk (complete); 9d one chunk (complete); 9e one chunk (complete).

**Non-goals:** full auth / multi-tenancy (Milestone 3); in-UI code editing (implementers work in their own environment).

**Exit:** a human can fully play any one role for at least one trial via the Web UI; admin-reclaim on a stranded claim works end-to-end. **Met.**

---

## Phase 10 — Reference Compose stack

Everything runs locally via `docker compose up`. Equivalent to the old microservices plan's Milestone 1, now explicitly "one valid deployment topology."

**Units:**

- **10a — complete.** [`reference/compose/`](../reference/compose/) ships a Compose stack standing up Postgres (`postgres:16.6-alpine`, reserved for `PostgresStore` consumption in 10b — *not* used by Gitea), Gitea (`gitea/gitea:1.22.6-rootless`, headless), and a one-shot `blob-init` (`busybox:1.36.1`) whose mount triggers creation of the `eden-blob-data` named volume (a top-level `volumes:` declaration alone does *not* create the volume — verified empirically). Long-running services declare `depends_on: { blob-init: service_completed_successfully }` so `compose up --wait` interprets the one-shot's exit-0 as success. Required secrets use `${VAR:?msg}` so a missing `.env` fails loudly; defaults dodge the well-known host-side ports (5433/3001/2222). A new `compose-smoke` CI job runs [`reference/compose/healthcheck/smoke.sh`](../reference/compose/healthcheck/smoke.sh) which asserts named-volume existence, blob-init exit code via `docker inspect`, Postgres connectivity, and Gitea's `/api/v1/version` response. 10a's blob-volume contract is "volume exists" only; ownership/permissions are 10d's responsibility.
- **10b + 10c — complete.** Six EDEN reference services (`task-store-server`, `orchestrator`, `planner-host`, `implementer-host`, `evaluator-host`, `web-ui`) plus a setup-time `eden-repo-init` profile-`setup` service are wired into [`reference/compose/compose.yaml`](../reference/compose/compose.yaml), backed by a single shared `eden-reference:dev` image (multi-stage `uv sync --frozen --no-dev --all-packages`). New `eden-storage.PostgresStore` consumes the 10a-deferred Postgres backend (psycopg v3, SERIALIZABLE per-op) and is conformance-tested across the same Protocol scenarios as `InMemoryStore`/`SqliteStore` via a new `python-test-postgres` CI job. New [`reference/scripts/setup-experiment/setup-experiment.sh`](../reference/scripts/setup-experiment/setup-experiment.sh) bootstraps a runnable stack from an experiment-config YAML in one shot: generates/preserves secrets, builds the shared image, runs `eden-repo-init` synchronously to seed the bare-repo volume, captures the seed SHA, and writes a complete `.env`. Operator workflow: `setup-experiment.sh <config> --experiment-id <id> && docker compose --env-file .env up -d --wait`. `task-store-server` gains a URL-based `--store-url` flag (`:memory:` / `sqlite:///<path>` / `postgresql://…` / bare-path-for-compat); the web-ui gains an unauthenticated `/healthz` endpoint for compose healthchecks. **Roadmap deltas committed with this chunk:** (1) one shared image, not per-service-image, because the workspace shares most deps and N images is a Phase-13/Helm concern; (2) experiment-specific image deferred to 10d (its only consumer is the LLM implementer's sandbox); (3) control-plane registration deferred to Phase 12 (no control plane yet); (4) workers integrate with Gitea as their actual git remote deferred to a follow-up sub-chunk after 10d (workers currently use a Compose-shared bare-repo volume; HTTPS-against-Gitea is its own refactor). The extended `compose-smoke` CI job runs setup-experiment, asserts the seeded ref before `compose up`, brings up the full stack with `--wait`, waits up to 180s for the orchestrator to exit 0 on quiescence, and verifies the final state on the wire endpoint: ≥3 `trial.integrated` events, ≥9 `task.completed` events (3 plan + 3 implement + 3 evaluate), and ≥3 `task.completed` events whose `task_id` begins with `plan-` (each seeded plan task reached a terminal state). Branch protection is **not** updated to require `python-test-postgres` in this chunk — that lands in a follow-up after the job has a few clean runs.
- **10d — complete (subprocess pass).** Each worker host gains `--mode subprocess` that invokes a user-supplied command (`plan_command` / `implement_command` / `evaluate_command`) read from the experiment-config YAML. The planner subprocess is **long-running** with a JSON-line stdin/stdout protocol (so user code can hold accumulating LLM context across plan tasks); the implementer + evaluator subprocesses are **per-task short-lived** with cwd = a per-task git worktree under `<worktrees-dir>/<container_hostname>/<task_id>/`. The implementer flow honors [`spec/v0/03-roles.md`](../spec/v0/03-roles.md) §3.2 step 1 — `Store.create_trial(status="starting")` runs **before** any repository write. Cross-host worktree races are eliminated by construction: each host has a private subdir, startup-time cleanup uses path-scoped `git worktree remove --force` (not the repo-global `git worktree prune`). Worker-side failure context (timeout, malformed outcome JSON) goes to the host's structured logger only; the chapter-3 submission shapes carry no free-form `description`. Documented as a non-normative reference binding at [`spec/v0/reference-bindings/worker-host-subprocess.md`](../spec/v0/reference-bindings/worker-host-subprocess.md). New fixture scripts (`tests/fixtures/experiment/{plan,implement,eval}.py`) exercise the protocol deterministically; the new `compose-smoke-subprocess` CI job runs end-to-end alongside the existing `compose-smoke`. Compose plumbing is a non-invasive overlay [`reference/compose/compose.subprocess.yaml`](../reference/compose/compose.subprocess.yaml). **Roadmap delta:** container isolation of the subprocess (docker-in-docker, experiment-specific image build, auth-secret mounting) is **deferred to a 10d follow-up sub-chunk** — the subprocess.Popen call is the wrappable layer. Branch protection is not updated to require `compose-smoke-subprocess` in this chunk.
- **10e — complete.** New `compose-e2e` CI job runs [`reference/compose/healthcheck/e2e.sh`](../reference/compose/healthcheck/e2e.sh), which exercises the stack-shape coverage missing from `compose-smoke` / `compose-smoke-subprocess`: a Web UI planner walkthrough (sign-in → claim → submit), an admin-reclaim drill (claim → admin-reclaim with a `task.reclaimed` `cause=operator` event), and a termination drill (`compose stop --timeout 10`; every container in `compose ps -a` ends `exited` with `ExitCode != 137` — the SIGKILL marker — chosen over `== 0` because external images don't always install graceful SIGTERM handlers and 143 from a default-handler is also a clean termination). Bring-up is **staged** to defeat the planner-host-claim race: stage 1 omits both planner-host and gitea (gitea is the deferred 10d follow-up; not consumed here), the python driver in [`reference/compose/healthcheck/e2e_drive.py`](../reference/compose/healthcheck/e2e_drive.py) runs the UI walkthroughs against the seeded-but-unclaimed plan tasks, then stage 2 brings planner-host online so the experiment proceeds to quiescence (the orchestrator's `--max-quiescent-iterations` × `--poll-interval` = 30s of zero progress before exit 0). `EDEN_PLAN_TASKS=4` is overridden in the staged `.env` (vs smoke's default 3) so plan-0001 / plan-0002 carry the UI walkthrough + admin-reclaim drill while plan-0003 / plan-0004 (and the reclaimed plan-0002) flow through the headless host. The python driver polls `/planner/` for all 4 seeded IDs to land before claiming, because the orchestrator has no compose healthcheck so `compose up --wait` returns as soon as the container is `running` — which can precede the seed completing. Sign-in must happen **before** the poll because both `/planner/` and `/admin/tasks/` redirect unauthed sessions to `/signin` (a polling loop without sign-in would just see 303s). Driver assertions match the actual route shapes (codex round-1 corrections from the plan): planner submit returns 200 with `planner_submitted.html` (NOT 303); admin reclaim redirects to `/admin/tasks/<id>/?reclaimed=ok` *with the trailing slash*; orchestrator-seeded plan tasks are `plan-{i:04d}` not bare-int. **Roadmap delta:** container isolation of the `*_command` subprocess (docker-in-docker, experiment-specific image build, auth-secret mounting) and Gitea-as-the-workers'-actual-git-remote remain deferred per 10d's plan. Branch protection is **not** updated to require `compose-e2e` in this chunk — same posture 10c / 10d took for newly-added compose jobs (let it run cleanly for a few iterations on `main` first).
- **10d follow-up A — complete (container isolation).** Each subprocess-mode worker host gains an opt-in `--exec-mode docker` that wraps every spawn in a sibling container via Docker outside of Docker (host `/var/run/docker.sock` mounted into the worker). The wrap shape (`docker run --rm -i --init --cidfile <path> --label eden.{role,host,task_id} --mount …  -w <cwd> -e <KEYS> <image> bash -lc '<original>'`) lives in [`reference/services/_common/src/eden_service_common/container_exec.py`](../reference/services/_common/src/eden_service_common/container_exec.py); deployment supplies the mount set via repeatable `--exec-volume name:target[:ro|rw]` and `--exec-bind host-path:target[:ro|rw]` flags. Mount targets inside the spawned child match the worker host's exact paths so worker-internal env vars (`EDEN_TASK_JSON` / `EDEN_OUTPUT` / `EDEN_WORKTREE` / `EDEN_EXPERIMENT_DIR`) resolve consistently in both views. **Lifecycle discipline:** unique-per-spawn cidfile (`<dir>/<role>-<uuid>.cid`); cleanup callback unlinks it on every terminal exit branch (graceful, fast-path, SIGKILL); a separate post_kill_callback runs `docker kill && docker rm -f` on the SIGKILL escalation branch only — load-bearing because killing the local `docker run` client doesn't kill the spawned container. Both stored on the `Subprocess` instance at spawn time. Each host runs `reap_orphaned_containers(role, host=gethostname())` at startup, scoped by `eden.host=<this>` so cross-host races are impossible by construction. **Identity:** `eden-runtime:dev` ([`reference/compose/Dockerfile.runtime`](../reference/compose/Dockerfile.runtime)) mirrors the `eden:1000` user from `eden-reference:dev` — without this, worktree files written by the worker host (uid 1000) trip git's `dubious ownership` check inside the spawned child running as root, and any commits the child produces would be uid-0-owned breaking integrator reads. The wrap deliberately doesn't pass `--user` so experiment-image USER overrides are honored. **DooD socket gid:** setup-experiment probes the in-VM gid via a throwaway container that bind-mounts the socket (host-side stat returns the wrong gid on Docker Desktop / Colima); the compose overlay applies `group_add: ["${EDEN_DOCKER_GID}"]`. **Image strategy:** if the experiment dir contains a Dockerfile, setup-experiment builds it as `eden-experiment-<id>:dev` and writes that into `.env`; otherwise the default `eden-runtime:dev` is used. **Volume name discipline:** `eden-bare-repo`, `eden-worktrees`, and `eden-artifacts-data` get explicit `name:` in compose so the host docker daemon resolves the wrap's `--mount source=<literal>` against the same volume the worker sees — without this, compose's auto-prefix (`eden-reference_<volume>`) creates a fresh empty volume on the wrap path. **Compose overlay split:** [`compose.subprocess.yaml`](../reference/compose/compose.subprocess.yaml) is the host-mode subprocess overlay (chunk-10d soft isolation, no docker socket access); a new [`compose.docker-exec.yaml`](../reference/compose/compose.docker-exec.yaml) layered ON TOP is the only file that mounts the docker socket and applies `group_add` — so a layering bug can't accidentally grant DooD privilege to the host-mode path. Both smoke scripts assert this directly via `docker inspect eden-implementer-host`. **Security boundary:** DooD with a shared socket is a soft boundary — secrets visible via `docker inspect` to any other concurrent `*_command`; documented as informative §7 in [`spec/v0/reference-bindings/worker-host-subprocess.md`](../spec/v0/reference-bindings/worker-host-subprocess.md). The reference deployment is for bug + dependency isolation, not hostile-code containment. New `compose-smoke-subprocess-docker` CI job mirrors `compose-smoke-subprocess`'s end-state assertions plus a per-task orphan-container check (no `eden.role=implementer|evaluator` containers post-quiescence) and a clean-teardown check (after `compose stop --timeout 15`, no `eden.role=planner` sibling remains — clean-SIGTERM or SIGKILL-escalation is acceptable for the smoke). The dedicated SIGKILL-escalation invariant (post_kill_callback actually `docker kill`s a sibling whose user command ignored SIGTERM) is exercised by the `pytest.mark.docker` integration test `test_terminate_sigkill_path_invokes_post_kill_callback` in [`reference/services/_common/tests/test_container_exec_integration.py`](../reference/services/_common/tests/test_container_exec_integration.py), which also drives the actual production worktree-with-gitlink mount shape (bare repo at one path + worktree's `.git` gitlink pointing into it, both mounted into the spawned child). Other `pytest.mark.docker` tests cover stale-cidfile-rejection, helper-level `kill_via_cidfile`, and `reap_orphaned_containers` against a real daemon. Branch protection is **not** updated to require the new job in this sub-chunk.

- **10d follow-up B — complete (Gitea as the workers' git remote).** Workers stop sharing the `eden-bare-repo` named volume and clone a private bare copy from the in-network Gitea container over plain HTTP; integrator publishes `trial/*` refs back to Gitea per a four-step ladder; remote-orphan reconciliation at orchestrator startup catches the rare "step 4a (compensating remote-delete) failed" case via spec-authoritative `.eden/trials/<trial_id>/eval.json` tree reads (NOT ref-name parsing — chapter 2 §1.3 keeps `trial_id` opaque). New `eden_git` ops (`clone_from`, `push_ref`, `fetch_ref`, `fetch_all_heads`, `delete_remote_ref`, `ls_remote`) plus typed `RefRefused` / `GitTransportError` exceptions distinguish definite remote rejection (local rollback only) from transport-indeterminate failure (immediate `ls-remote` read-back disambiguates → 4a + 4b vs. 4b only). Role-specific transport-failure mapping per [chapter 3](../spec/v0/03-roles.md): implementer → `error`, evaluator → `eval_error`, integrator → §3.4 ladder. setup-experiment.sh provisions Gitea idempotently (admin user via `gitea admin user create` / `change-password`, repo via `POST /api/v1/user/repos`, credential-helper script under `reference/compose/.gitea-creds-<id>/credential-helper.sh`, seed pushed via the new `eden_service_common.repo_init --push-to` flag); compose.yaml drops the shared bare-repo volume in favor of per-service clones (`eden-orchestrator-repo` / `eden-implementer-repo` / `eden-evaluator-repo` / `eden-web-ui-repo`). Volume name discipline same as sub-chunk A: `eden-implementer-repo` and `eden-evaluator-repo` get explicit `name:` so the docker-exec wrap resolves `--mount source=<literal>` against the same volume the worker sees. Coverage: 13 new unit tests in `test_remote_ops.py` against a `file://` remote (plus a real-http credential-helper round-trip via in-process `http.server`); 8 new integration tests in `test_remote_integrator.py` drive `Integrator.integrate()` through the public method for each failure mode (push race, store failure post-push, 4a transport-fails, malformed orphan, valid integrated). The existing `compose-smoke` / `compose-smoke-subprocess` / `compose-smoke-subprocess-docker` smokes pass end-to-end through Gitea, with a `git ls-remote` post-quiescence assertion confirming ≥3 `refs/heads/trial/*` on the remote match the ≥3 `trial.integrated` events in the store. Auth posture: plain HTTP on the compose internal network (same trust boundary as the existing `EDEN_SHARED_TOKEN` plain-HTTP traffic between workers and the task-store-server); a hardened deployment substitutes a TLS-fronted Gitea behind the same `--gitea-url`. Documented as informative §7 in [`spec/v0/reference-bindings/worker-host-subprocess.md`](../spec/v0/reference-bindings/worker-host-subprocess.md). Branch protection is **not** updated in this sub-chunk; existing CI jobs gain the new assertions in-place.

**Chunks:** 10a one; 10b + 10c one chunk (dockerize + setup script co-evolve — each image's env vars and entrypoints feed the script); 10d one; 10e one. 10d follow-ups A and B each one chunk (sub-chunks of 10d, shipped after 10e).

**Non-goals:** k8s (Phase 13); S3 blob backend (Phase 13); Gitea auth (Phase 13).

**Exit:** the fixture experiment runs to completion in Compose with comparable results to direvo's monolith; e2e test green in CI.

---

## Phase 11 — Conformance suite v1

Formalizes black-box scenarios that any component can run against itself.

**Units:**

- **11a — complete.** Harness: fixture infrastructure + implementation-under-test adapter + scenario-execution driver. First scenarios validate the harness itself (bootstrap problem).
- **11b — complete.** State-machine scenarios (task lifecycle, claim tokens, transactional event invariant) — expands on Phase 5's scenarios.
- **11c — complete.** Role-contract scenarios live in [`conformance/scenarios/test_planner_submission.py`](../conformance/scenarios/test_planner_submission.py), [`test_implementer_submission.py`](../conformance/scenarios/test_implementer_submission.py), and [`test_evaluator_submission.py`](../conformance/scenarios/test_evaluator_submission.py), grouped under three new chapter-9 §5 v1+roles index entries (`Planner submission`, `Implementer submission`, `Evaluator submission`). The 20 new scenarios cite [`spec/v0/03-roles.md`](../spec/v0/03-roles.md) §2.4 / §3.4 / §4.2 / §4.4 MUSTs and assert: planner — drafting-proposal rejection, unknown-proposal rejection, zero-proposal success, status=error keeps partially-drafted proposals in `drafting`; implementer — unknown-trial rejection, cross-proposal trial-id rejection, success-without-commit_sha MUST NOT terminalize trial as success (where the rejection surfaces is implementation-defined), accepted success writes `commit_sha` onto the trial, status=error errors the trial AND no evaluate task is dispatched against an errored trial, idempotent same-shape resubmit + 409 on divergent commit_sha; evaluator — mismatched trial_id rejection, undeclared metric key MUST NOT terminalize as success, type-violating metric MUST NOT terminalize as success, accepted success writes `metrics`+`artifacts_uri`+`completed_at` atomically with `trial.succeeded`, status=error writes trial metrics+artifacts_uri (distinct from eval_error which discards them), eval_error keeps trial `starting` and discards submission metrics+artifacts, retry-exhausted `declare-eval-error` does NOT graft prior eval_error metrics, baseline idempotent role-rule resubmit AND vary-only-artifacts_uri-resubmit also accepted (one `task.submitted`; first submission's artifacts_uri wins). **Spec amendment:** [`spec/v0/03-roles.md`](../spec/v0/03-roles.md) §4.4 was tightened to drop `artifacts_uri` from the resubmit-equivalence formula — codex-review round-0 surfaced an internal conflict between §4.4 (had listed `artifacts_uri`) and chapter-04 §4.2 (canonical, doesn't list it); §4.4 now defers to §4.2. Chapter 9 §3 prose extended to acknowledge the v1+roles + v1+roles+integrator MUST citation surface; §5 gains a v1+roles index table with the three groups. Helper additions: [`_seed.submit_plan`](../conformance/src/conformance/harness/_seed.py) always sends `proposal_ids` (was omitted on `status=error`, which is non-conforming under the literal-spec reading of §2.4); [`_seed.submit_evaluate`](../conformance/src/conformance/harness/_seed.py) accepts explicit `metrics` for any status and a new `artifacts_uri` parameter so eval_error-with-metrics + vary-artifacts_uri tests can drive real wire payloads. Reference impl passes the full v1+roles suite (106/106 conformance scenarios green). Plan + 4-round codex-review record at [`docs/plans/eden-phase-11c-role-contracts.md`](plans/eden-phase-11c-role-contracts.md) + [`docs/plans/review/eden-phase-11c-role-contracts/`](plans/review/eden-phase-11c-role-contracts/).
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
