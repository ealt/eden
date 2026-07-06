# Ground-up design review — July 2026

> **Status: review record (2026-07-04).** An outside-in architecture review of the whole repo, commissioned as: *"if you were to design this project from the ground up, what would you do differently? any suboptimal design decisions and other slop that should be cleaned up?"* Findings are evidence-based (file/line citations, measured duplication) and deliberately opinionated. Nothing here is a plan yet; the intent is to agree on the verdicts first, then advance selected items into plans/issues.

## 1. The verdict in one paragraph

The protocol core is genuinely well-designed and worth keeping almost verbatim: the event-sourced transactional invariant, the task state machine, opaque typed ids, the integrator's compensating-delete ladders, the wire client's read-back reconciliation, and the conformance-with-citations discipline are all better than what most human teams ship. Function-level code quality is consistently disciplined — the complexity gates worked. The accumulated damage is concentrated one level up, where no gate was looking: **interface breadth** (a 50-method god-`Store`), **cross-file and cross-package duplication** (the identity registry implemented ~4×; two SQL backends copy-pasted in two packages; three worker hosts ~85% clones; the admin registry UI written twice), **process-scoped assumptions** (one experiment per task-store-server, contradicting the platform vision the PRD itself states), and **scaffolding accretion** (~3,800 lines of copy-pasted bash smokes; a 308KB CHANGELOG absorbing design-doc content). The repeated failure mode is precisely the one the maintainer suspected: agents added the minimally-disruptive version next to existing code instead of reshaping it — and the repo's own guardrails channeled the slop to exactly the dimensions they didn't measure.

## 2. What a ground-up design would do differently

Ordered by architectural depth, not urgency.

### 2.1 Experiment-scoped, not process-scoped

The wire protocol got this right — every path is `/v0/experiments/{E}/…` — but the implementation binds one experiment per process at startup (`--experiment-id` on every service; `build_store` is single-experiment; the store schema assumes it). The result: N experiments require N full stacks, and chapter 11's multi-experiment machinery coordinates orchestrators over a store layer that can't actually host multiple experiments (#254). The PRD ([`eden-experiment-platform.md`](../prds/eden-experiment-platform.md) §2) already names this: "the reference deployment conflates provisioning infrastructure with running an experiment." Ground-up, `experiment_id` would be a key in every table and every store operation from day one, and per-experiment binding would happen per-request, not per-process. This is the single costliest remaining correction, and it blocks the platform vision more than any missing feature.

### 2.2 One identity system

Worker/group identity is implemented approximately four times: the shared `eden-storage` mixin, plus independent re-implementations in `eden-control-plane`'s memory and postgres backends (including two more copies of cycle detection and transitive group resolution — `control_plane/memory.py:434-524`, `control_plane/postgres.py:702-897` vs `eden_storage/_ops/groups.py:192-273`). The spec itself calls the per-experiment + deployment-scoped dual registry "unfortunate complexity" (ch. 11 §6). Ground-up: one `eden-identity` library (minting, credential hash/verify, cycle detection, transitive resolution) over a small storage-primitive interface, consumed by both scopes — and, longer-term, the #141 model (deployment-level principals; experiments grant memberships) makes the dual registry disappear entirely.

### 2.3 One source of truth for wire shapes

Every field is written twice — hand-written JSON Schema + hand-written Pydantic model — with drift caught (not prevented) by a 3,200-line parity corpus. The semantic validators are already single-sourced through shared `format` handlers; only the field structure is doubly maintained. Ground-up: Pydantic models are canonical, schemas are generated (`model_json_schema()` + a post-processor re-injecting the custom `format` keywords), deleting ~1,700 lines of hand-written schema and most of the parity harness. Gate: verify no external IUT depends on the exact hand-written schema shapes before switching.

### 2.4 Pick one concurrency story at the wire

Every `eden-wire` route handler is `async def` and calls the synchronous `Store` directly — sync database I/O runs on the event loop, so one slow query stalls the entire server. The intermittent `ReadTimeout` failures documented as a pitfall in AGENTS.md are the fingerprint. Ground-up: either plain `def` handlers (FastAPI's threadpool) with the sync store, or an async store. The former is a small, mechanical change.

### 2.5 Python, not bash, as the integration-test language

The smoke fleet is 14 scripts / ~3,800 SLOC of bash with measured 8-10/10 duplication of preflight, teardown, id-minting, and event-polling code, all constrained to bash 3.2. The repo already proved the right pattern twice: `e2e.sh` delegates its real work to `e2e_drive.py`, and the Helm fleet extracted `ci-smoke-lib.sh` — it just was never generalized. Ground-up: one Python harness (`SmokeStack` context manager + `EventFeed` assertion helpers), each smoke a 30-80-line declaration. Similarly, `setup-experiment.sh` (1,026 lines of bash with embedded Python heredocs) wants to be Python with the same probe/mock testability seam `setup-aws.sh` already has.

### 2.6 One worker-host chassis

`executor/subprocess_mode.py` (760) and `evaluator/subprocess_mode.py` (581) are ~85% line-for-line clones; the three host CLIs repeat the same `main()` shape and a verbatim 3× docker-network warning block; the ideator is a migration laggard carrying byte-identical local copies of helpers that were hoisted to `_common` (and — a real robustness gap — it never adopted the submit read-back the other two hosts use). Ground-up: one per-task chassis in `_common` parameterized by role (env-build, docker-wrap, spawn, capture, outcome-parse, read-back), with each role contributing only its `_handle_one` and status vocabulary.

### 2.7 Store protocol scoped to its own atomicity argument

`Store` has 50 methods; its docstring defends the single interface via the composite-commit argument, which genuinely covers only ~28 of them. The 12 identity methods and the checkpoint pair never co-commit with task state (checkpoint is already implemented as free functions that the protocol methods merely delegate to). `validate_terminal` pushes orchestrator policy into the storage layer. Ground-up: `Store` (composite-commit core) + `IdentityRegistry` + checkpoint free functions; the 1,511-line wire client splits along the same seams.

### 2.8 Resolve the chapter-6 conformance tension

Chapter 6's git-side MUSTs (squash shape, evaluation manifest, `work/*` discipline) are normative but unverifiable — chapter 9 §6 makes the HTTP binding the only IUT contract, so conformance asserts only their wire-observable projection. A spec that mandates what its own conformance procedure cannot observe invites drift. Ground-up: either add a git-access binding as a conformance level, or demote the git shapes to reference-binding status. Either is coherent; the current halfway point is not.

### 2.9 Leaner process artifacts

The discipline scaffolding caught real regressions and should stay — but three habits accreted: (1) CHANGELOG entries became multi-KB essays duplicating plan-doc reasoning and codex-review round tables that already live in `docs/plans/review/` (308KB total); (2) `docs/plans/` + `docs/archive/` hold 43MB of point-in-time prose in the working tree that agents and newcomers wade through; (3) AGENTS.md restates constraints (bash-3.2, tests-dir rules) that scripts also restate per-file, and its pitfalls list grows append-only. Ground-up: terse changelog entries (what + issue link), review dispositions only in the review dirs, completed-phase plans archived out of the working tree, and pitfalls distilled into mechanical checks where possible (several already were — that's the success template).

### 2.10 Comments that state constraints, not provenance

A large fraction of otherwise-clean modules is comment sediment narrating PR history to past reviewers ("Codex round 7 MINOR…", "per plan §6.1", "12a-3 wave 7"). The next reader needs the constraint, not the archaeology; git blame carries provenance. The repo's own best modules (`container_exec.py`, `subprocess_runner.py`) demonstrate the right style — docstrings that explain *why*, without the changelog.

## 3. What a ground-up design would keep

Explicitly validated by this review; do not "fix":

- The event-sourced core: transactional invariant, composite commits, replayable per-experiment log.
- The task state machine, identity-keyed claims, idempotent-resubmit equivalence rules.
- Opaque typed ids with display-name separation (#128's model).
- The `_ops/` mixin split and `_Tx` staging in eden-storage.
- `run_orchestrator_iteration`'s five-decision decomposition, and `dispatch_mode` as per-decision auto/manual gates (the human/agent hybrid knob).
- The `Integrator` compensation ladders and the wire client's `Indeterminate*` read-back pattern.
- The checkpoint format and recovery-probe design.
- `checkpoint_scheduler.py`, `auto_checkpoint.py`, `baseline.py`, `lease_manager.py`, `store_factory.py`/`repo_factory.py` — the cleanest service modules.
- The conformance suite's citation discipline and the CI path-filter design (three-clause fail-safe gate).
- The wire error vocabulary and problem+json posture.

## 4. Defects found during review (fix regardless of any restructure)

1. **Auto-checkpoint is silently dead in multi-experiment mode** — the scheduler is wired only into `loop.py`; `multi_loop.py` never invokes it. Enabling `auto_checkpoint` under a control plane does nothing.
2. **Ideator host lacks submit read-back** — `handle_ideation_task` calls naked `store.submit()` (3 attempts, no committed-state read-back), so a lost response strands the task `claimed` until TTL sweep; executor/evaluator already use `_submit_with_readback`.
3. **Two admin-authority models coexist in eden-wire** — `require_admin` (principal check) on groups/workers/checkpoints vs `enforce_in_any_group("admins")` (group membership) on reassign. Semantically different; should converge on one.
4. **web-ui duplicate credential-dir flag** — `--credential-dir`/`$EDEN_CREDENTIAL_DIR` (web-ui-local) shadows the shared `--credentials-dir`/`$EDEN_WORKER_CREDENTIALS_DIR`.
5. **`workers.py` docstring rot** — eden-dispatch's scripted workers are described as "Phase 5 deterministic fakes"; they are the production worker bodies (the executor host injects a real git-backed implement fn).
6. **`_now_iso` implemented 5× with two different spellings**; two logging conventions (stdlib vs structured) split across services.

## 5. The cleanup program, ranked by leverage

Tier 1 — structural, highest ROI (each independently landable):

| # | Item | Size signal |
|---|---|---|
| 1 | Dedupe the admin registry UI (Store-vs-ControlPlane groups/workers routes + templates) behind one registry-parameterized blueprint | ~2,270 → ~700 lines |
| 2 | Extract the per-task worker-host chassis; collapse executor/evaluator `subprocess_mode.py`; unify host CLIs; fix ideator laggards (§4.2) | ~1,340 lines + 3× CLI dup |
| 3 | Extract `eden-identity` (one registry implementation for both scopes) | kills ~4× duplication, 2 god-protocols shrink |
| 4 | Dialect-parameterized `_SqlStore` shared by sqlite/postgres — in eden-storage AND eden-control-plane | ~400 lines/package; ends fixed-in-one-forgot-the-other drift |
| 5 | Python smoke harness replacing the compose bash fleet; matrix the 10 copy-pasted CI smoke jobs | ~3,800 bash → ~1,300 py; ~250 CI lines → ~40 |

Tier 2 — moderate:

- Orchestrator: move `cli.py`'s ~720 lines of bootstrap/runtime-factory into modules; extract shared `run_one_iteration` for both loops; wire auto-checkpoint into multi mode (§4.1).
- web-ui: shared `routes/_worker_flow.py` (claim store, CSRF-failure, render helpers — `_csrf_failure_response` exists 8×); migrate ideator onto shared read-back; split the 1,043-line `routes/executor.py` (its `slop-allow-file` was added at 816 and it grew +227 since); finish the flat→nested `routes/admin/` migration.
- Store split per §2.7; contracts single-source per §2.3; wire sync-handler fix per §2.4.
- Control-plane `app.py`: 421-line closure-monolith `make_app` → `APIRouter` + `Depends`, matching the web-ui pattern.
- Tests-dir restructure AGENTS.md already specifies (unique package names + `--import-mode=importlib`), or minimally the 20-line basename-uniqueness CI check.

Tier 3 — mechanical / process:

- `now_iso()` in `_common`; one logging convention; delete the web-ui credential-dir duplicate; move `artifact_backend.py` to `eden-blob`; retire the legacy unauthenticated `--artifacts-dir` route once #290 lands.
- CHANGELOG diet + archive completed plans out of the working tree + prune AGENTS.md restatements (§2.9).
- Single `required-checks` aggregation job so branch protection references one context; sunset or shrink `check-rename-discipline.py` (485 lines guarding a finished one-time rename).

## 6. Why it happened, and what prevents the next round

The pattern is consistent: agents optimized for "smallest diff that passes the gates," and the gates measured cyclomatic complexity, function length, and file size — so the debt migrated to the unmeasured dimensions: near-identical siblings (new file, no gate), interface growth (no gate), cross-package copies (no gate), and scaffolding (bash, YAML — no gate). Twice the repo *diagnosed* a fix precisely in prose and the writing-down substituted for doing it (the tests-dir restructure; back-porting `ci-smoke-lib.sh` to the compose smokes). And one `slop-allow-file` shows a gate being silenced and the file continuing to grow.

Countermeasures worth adopting:

1. **Duplication gate**: add cross-file near-duplicate detection (e.g. `pylint --disable=all --enable=duplicate-code` or `jscpd`) to the complexity-gate job, with the same annotated escape hatch.
2. **Consolidation cadence**: every N feature chunks, one refactor chunk whose plan starts from the "diagnosed but not done" ledger — make writing-it-down create a scheduled obligation instead of a substitute.
3. **Plan-template question**: add to the chunk-plan template: *"What existing structure should be reshaped before this addition? If 'none,' justify."* — forces the reshape-vs-append decision to be explicit at plan-review time, where codex-review can challenge it.
4. **Interface budget**: any protocol/ABC gaining a method beyond a stated cap triggers the same stop-and-think discipline as the complexity thresholds.
5. **Sunset review for guardrails**: each bespoke check (rename-discipline, etc.) gets a revisit-by note so one-time-migration guards don't accrue forever.
