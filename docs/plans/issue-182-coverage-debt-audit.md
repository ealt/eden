# Issue #182 — Coverage-debt audit: exercise every untested operator surface

## 1. Context

[Issue #182](https://github.com/ealt/eden/issues/182) is a **process** chunk, not a code or spec chunk. The 2026-05-23 EDEN demo session drove a slice of the operator surface (ideator + executor + evaluator + integrator across UI / CLI / skill) and surfaced 50+ bugs — almost all latent for months, surfaced only because someone finally walked the system as an operator. By the session's own calibrated-confidence assessment, **more bugs exist in the surfaces nobody walked across.**

This plan lays out a **one-time, structured catch-up audit**: systematically drive every untested operator surface through its documented happy path, note every moment of confusion / failure / surprise, and file an issue per surprise at the moment of discovery. The deliverable is **filed issues + a live coverage checklist in #182's body** — not fixes, not new tests, not refactors. Those are explicitly out of scope (the fixes that fall out of the audit are separate, individually-tracked work).

This chunk is distinct from three sibling process issues, all closed:

- **#179** (scheduled operator dogfooding) — the *recurring going-forward* ritual. This issue is the *one-time catch-up* that closes the existing gap before the recurring ritual takes over.
- **#180** (fresh-operator walkthrough per PR) — the *per-PR* discipline. This issue is the *cross-PR* catch-up over surfaces that shipped before that discipline existed.
- **#178** (substrate-migration audit discipline) — the *authoring* discipline for refactors. This issue is the *running-what-already-exists* complement.

### 1.1 What "the plan" is for a process chunk

There is no impl PR that lands code. The "implementation" of this chunk is **running the audit** — bringing up real stacks, driving real surfaces, filing real issues. This plan is the contract a fresh agent (or the operator) executes against: it enumerates the surfaces (verified against the current codebase, §4), fixes the audit protocol (§5), and chunks the walk-through into stack-sharing waves with completion gates (§10). The plan PR goes through codex-review-to-convergence like any other; the audit sessions that follow are gated on operator approval of this plan.

### 1.2 Surface-table reconciliation (verified against the codebase at plan-authoring time)

The issue's surface table is a *starting point* (the issue says so explicitly). Mapping each row against the current tree surfaced three corrections that this plan bakes in — consistent with the "verify before claiming a UX/code bug" discipline:

- **Adminer is a documented bring-your-own workflow, not a built-in service.** The issue lists "Postgres readonly role + Adminer connection." The reference Compose stack ships **no `adminer` service** in [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) — but that is *by design*, not a gap: [`docs/observability.md`](../observability.md) §3.1 documents Adminer as a BYO sibling container (`docker run --rm -d --name eden-demo-adminer --network eden-reference_default … adminer:4`) attached to the EDEN network and pointed at the **Postgres readonly role** (`eden_readonly` / `EDEN_READONLY_PASSWORD` on `localhost:5433`), provisioned by `ensure_readonly_role()` ([`reference/packages/eden-storage/src/eden_storage/postgres.py`](../../reference/packages/eden-storage/src/eden_storage/postgres.py)) when task-store-server is passed `--readonly-password`. So the surface to audit is the **documented Adminer BYO workflow itself** (follow observability.md §3.1 verbatim; confirm `SELECT` works, the `variant_unpacked` view is present, and `worker.credential_hash` is blocked) — *not* a bogus "no Adminer service" issue. Out of scope to *add* Adminer as a built-in Compose service; the BYO posture is intentional.
- **`eden-experiment restore` is no longer blocked.** The issue lists it "Blocked on #177." #177 is fixed; [`reference/scripts/manual-ui/eden-experiment`](../../reference/scripts/manual-ui/eden-experiment) now carries `assert_snapshot_nonempty()` and a `restore` subcommand. This row is **unblocked** and in-scope for this audit.
- **Reclaim/reassign live at two layers.** The issue writes `POST /tasks/<id>/reclaim` loosely. Reality: the **wire** endpoints are `POST /v0/experiments/{E}/tasks/{T}/reclaim` (body `{"cause": …}`) and `POST /v0/experiments/{E}/tasks/{T}/reassign` (body `{"new_target": …, "reason": …}`) in [`reference/packages/eden-wire/src/eden_wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py); the **UI** wrappers are `POST /admin/tasks/{task_id}/reclaim` and `/reassign` in [`reference/services/web-ui/src/eden_web_ui/routes/admin/actions.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin/actions.py). The audit drives **both** layers (UI happy path + a direct-wire pass to confirm `cause=operator`, the `reason` audit string, and `reassigned_by` principal-stamping).

## 2. Decisions captured before drafting

Listed so codex-review and the operator can see what was deliberate vs. proposable. Per BASE.md these default to the **agentic** framing (agent drives, operator observes / intervenes); if the cautious version is right, course-correct at plan review.

1. **Agent-driven, operator-observed.** The audit is driven by an agent session using the in-repo manual skills ([`eden-manual-experiment`](../../.claude/skills/eden-manual-experiment/), [`eden-manual-ideator`](../../.claude/skills/eden-manual-ideator/), [`eden-manual-executor`](../../.claude/skills/eden-manual-executor/), [`eden-manual-evaluator`](../../.claude/skills/eden-manual-evaluator/)) plus `curl` for wire-only surfaces and a browser for UI-only surfaces. The operator observes and intervenes on judgment calls (is this a bug or intended?). *Proposable:* operator-driven instead, if the operator prefers to be the one hitting the surfaces.

2. **Happy-path primary, light adversarial probing.** Each surface is driven through its **documented happy path** first (the issue's explicit instruction). Where an obvious error input is one keystroke away (empty required field, stale CAS token, malformed id, double-submit), probe it too — the issue's own predicted bug classes include "UI 500s," which happy-path-only would miss. The audit does **not** attempt exhaustive fuzzing or adversarial security testing; that's a separate effort. *Proposable:* strict happy-path-only if the operator wants to keep scope tight.

3. **Issue-per-surprise, filed at the moment.** Every confusion / failure / surprise becomes a GitHub issue **at the moment of discovery**, using the established issue-workflow patterns (comprehensive body with repro, triage labels at file time, cross-references). Batching-at-end is rejected — context is freshest at the moment, and the demo session's own filing discipline worked because it filed live.

4. **Cross-reference existing CI issues; don't re-file.** Several CI-improvement issues already exist (#152 checkpoint smoke, #147 multi-experiment smoke, #156). When the audit hits a surface those cover, it **cross-references** the existing issue rather than filing a duplicate. New test/CI gaps the audit discovers may be *recommended in a filed issue*, but the audit itself adds no tests or CI jobs (issue §"Out of scope").

5. **Chunked across sessions, grouped by stack configuration.** The audit is chunked into waves (§10) that each share one stack bring-up (base host-mode / control-plane / subprocess+docker-exec overlays / failure-injection). Bringing up a stack is the expensive setup step; grouping surfaces by the stack they need minimizes redundant bring-ups. Sessions can span calendar days; the live checklist in #182 is the cross-session state.

6. **Blockers don't halt the audit.** If a broken surface (e.g. a 500 that prevents reaching a downstream page) blocks further walk-through, file the blocker, attempt a documented workaround to continue, and record the coverage gap in the checklist (`blocked by #__`) rather than stopping the wave.

These six are NOT up for re-litigation in codex-review unless review surfaces a load-bearing contradiction with the issue's stated scope.

## 3. Scope

**In scope:**

- Enumerate every operator surface (§4 is the verified inventory; expand if the audit discovers more).
- Drive each surface through its documented happy path (+ light adversarial probing per §2.2).
- File an issue per surprise at the moment of discovery (§5 protocol).
- Maintain the live coverage checklist in #182's body; post per-session findings as comments.
- Triage every filed issue (triage / priority / cluster labels per [`docs/triage.md`](../../docs/triage.md)).
- Final sweep for surfaces missed during the walk-through (grep the route tree against the checklist).

**Out of scope (per issue §"Out of scope"; the audit files, it does not fix):**

- Fixing any bug the audit surfaces (each fix is separate, individually-tracked work).
- Adding new tests or CI smoke jobs (separate issues; cross-reference existing #152 / #147 / #156).
- Refactoring untested surfaces.
- Adding a built-in Adminer Compose service (the BYO posture in [`docs/observability.md`](../observability.md) §3.1 is intentional; the audit *exercises* that documented workflow rather than replacing it).
- Spec / schema / Pydantic / wire-binding changes (the audit *reads* these to judge correctness; any change is a filed issue, executed elsewhere).

## 4. Surface inventory (verified against the tree)

This is the audit's coverage contract. Each surface below was located in the current tree; the exact route / endpoint / handler / template is named so the auditor drives the real thing, not an approximation. Grouped by the stack configuration that exercises it (drives the wave structure in §10).

**Authoritative cross-check.** [`docs/observability.md`](../observability.md) is the repo's own enumeration of operator/observability surfaces ("enumerates every surface," §intro; [`docs/user-guide.md`](../user-guide.md) points auditors there). §4 below MUST be reconciled against it before Wave 0 seeds the checklist — every surface observability.md documents (§2 first-party, §3 bring-your-own) is either inventoried here or explicitly marked already-covered (§4.7). The §4.6 group captures the observability surfaces that aren't admin-UI routes.

### 4.1 Base host-mode stack — per-experiment admin UI

All under [`reference/services/web-ui/src/eden_web_ui/routes/`](../../reference/services/web-ui/src/eden_web_ui/routes/). Auth: every route runs `get_session()` (missing → 303 to `/signin`); POST routes check CSRF; worker-registry writes route through `app.state.admin_store` (disabled-control behavior when admin bearer is `None`).

| Surface | Route(s) | Handler | Notes |
|---|---|---|---|
| **Session flow** | `GET /signin`, `POST /signin`, `POST /signout` | `routes/auth.py:signin_form` / `signin_submit` / `signout` | the gate every other surface depends on. NB there are **no per-user accounts / no credential check** (Phase 9 §1): `GET /signin` renders a single "continue as `<worker_id>`" button, `POST /signin` always mints a session for the configured `worker_id`, `POST /signout` clears the cookie. Drive sign-in → sign-out → the unauthenticated→303-redirect path (do NOT look for a bad-credential branch — there isn't one) |
| **Authenticated artifact reader** | `GET /artifacts?…` | `routes/artifacts.py:serve_artifact` | serves an artifact-bundle entry (distinct from the `/admin/artifacts/` *listing*); exercise path-traversal guard (`_safe_entry_name`) + content-type handling |
| Admin dashboard | `GET /admin/` | `admin/index.py:index` | cross-tab counts, recent events |
| Observability (read-only) | `GET /admin/tasks/`, `/admin/tasks/{id}/`, `/admin/variants/`, `/admin/variants/{id}/`, `/admin/events/`, `/admin/ideas/`, `/admin/ideas/{id}/`, `/admin/experiment/` | `admin/observability.py` | filters, lineage, inline content |
| Artifacts listing | `GET /admin/artifacts/` | `admin_artifacts.py` | directory listing of artifacts-dir |
| **Work-refs GC** | `GET /admin/work-refs/`, `POST /admin/work-refs/delete` | `admin/work_refs.py:work_refs_index` / `work_refs_delete` | CAS-guarded (`expected_old_sha`), ref-grammar + eligibility validation; **needs orphan/eligible refs to exist** (drive a lifecycle first) |
| **Dispatch-mode** | `GET /admin/dispatch-mode/`, `POST /admin/dispatch-mode/` | `admin/actions.py:dispatch_mode_form` / `dispatch_mode_update` | 4 keys: `ideation_creation`, `execution_dispatch`, `evaluation_dispatch`, `integration`; each `auto`/`manual` — **toggle every key** |
| **Groups (per-experiment)** | `GET /admin/groups/`, `POST /admin/groups/`, `GET /admin/groups/{id}/`, `POST /admin/groups/{id}/members`, `POST /admin/groups/{id}/members/{mid}/remove`, `POST /admin/groups/{id}/delete` | `admin_groups.py` | register, detail, **add/remove member, delete** |
| **Workers (per-experiment)** | `GET /admin/workers/`, `POST /admin/workers/`, `GET /admin/workers/{id}/`, `POST /admin/workers/{id}/reissue-credential` | `admin_workers.py` | **reissue-credential** renders one-shot token (`admin_worker_token.html`) |
| **Task reclaim** | `POST /admin/tasks/{id}/reclaim` | `admin/actions.py:task_reclaim` | claimed/submitted → pending |
| **Task reassign** | `GET /admin/tasks/{id}/reassign`, `POST /admin/tasks/{id}/reassign` | `admin/actions.py:task_reassign_form` / `task_reassign` | target none/worker/group + **required reason** |
| Create-execution-task | `POST /admin/ideas/{idea_id}/create-execution-task` (form on `GET /admin/ideas/{id}/`) | `admin/actions.py:create_execution_task` | manual idea→execution-task |
| Terminate-experiment | `POST /admin/experiment/terminate` | `admin/actions.py:terminate_experiment` | experiment lifecycle terminal |

**Note — `/admin/experiments/` is NOT a base-stack surface.** The experiments dashboard (`admin_experiments.py`) and the `/admin/control/*` pages mount **only when the web-ui is started with a control plane** (`if control_plane is not None:` in [`app.py`](../../reference/services/web-ui/src/eden_web_ui/app.py)); the base template hides the nav link unless `control_plane_enabled`, and the route **404s on the default stack** ([`docs/observability.md`](../observability.md) §2.1). It is therefore inventoried under §4.3 (control-plane stack), not here. On the base stack, the auditor should confirm the documented 404 (expected, not a bug).

### 4.2 Base host-mode stack — wire + data-lifecycle (no full UI)

| Surface | Endpoint / command | Location | What to verify |
|---|---|---|---|
| **Reclaim (wire)** | `POST /v0/experiments/{E}/tasks/{T}/reclaim` body `{"cause":"operator"}` | `eden-wire/server.py` (`_reclaim`) | **worker-gated** (any registered worker bearer); `cause=operator` accepted; task → pending |
| **Reassign (wire)** | `POST /v0/experiments/{E}/tasks/{T}/reassign` body `{"new_target":…,"reason":…}` | `eden-wire/server.py` (`_reassign_task`) | **gated to a worker in the `admins` group — NOT the bootstrap `admin:<token>` bearer** (that gets 403); must mint/reissue a worker bearer first (see the `ADMIN_WORKER_BEARER` pattern in [`smoke-manual-mode.sh`](../../reference/compose/healthcheck/smoke-manual-mode.sh)). Verify `reason` recorded + `reassigned_by` stamped from the authenticated principal |
| **Adminer BYO + Postgres readonly role** | follow [`docs/observability.md`](../observability.md) §3.1 (BYO `adminer:4` sibling container) **and** a direct desktop-client connect to `localhost:5433` as `eden_readonly` | `eden-storage/postgres.py:ensure_readonly_role` (provisioned via task-store-server `--readonly-password`) | Adminer attaches + queries; `SELECT` works on non-worker tables + the `variant_unpacked` view; `worker.credential_hash` column blocked. Audit the *documented workflow*, not a "no Adminer" gap |
| **Checkpoint** | `eden-experiment checkpoint <name> [--force]` | `manual-ui/eden-experiment` (`cmd_checkpoint`) | non-empty postgres.sql / forgejo.tar.gz / artifacts.tar.gz (post-#177) |
| **Restore** | `eden-experiment restore <name>` | `manual-ui/eden-experiment` (`cmd_restore`) | `assert_snapshot_nonempty()` passes; round-trips to a recovered stack (unblocked since #177) |
| Wire checkpoint/import | `POST /v0/experiments/{id}/checkpoint`, `/v0/checkpoints/import` | covered by smoke-checkpoint.sh / #152 | cross-ref, don't re-file |

### 4.3 Control-plane stack — chapter 11 lease primitive + deployment-scoped registry

Requires the control-plane server ([`reference/services/control-plane/src/eden_control_plane_server/app.py`](../../reference/services/control-plane/src/eden_control_plane_server/app.py)) and web-ui constructed with `control_plane=…`. Spec: [`spec/v0/11-control-plane.md`](../../spec/v0/11-control-plane.md), wire binding [`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §15. The **lease primitive** is a per-experiment, time-bounded ownership claim (default 30s) ensuring exactly one orchestrator replica drives an experiment at a time; expired leases let another replica take over.

| Surface | Endpoint(s) | What to verify |
|---|---|---|
| **`/admin/experiments/` UI** | `GET /admin/experiments/`, `POST /admin/experiments/register`, `POST /admin/experiments/{id}/unregister`, `POST /admin/experiments/{id}/select` | `admin_experiments.py` — **register + select + unregister** (only `select` partially exercised in demo). Control-plane-gated (404s on base stack) |
| Experiment registry (wire) | `POST/GET /v0/control/experiments`, `GET/DELETE /v0/control/experiments/{E}` | register / list / read / unregister |
| **Lease ops** | `POST /v0/control/experiments/{E}/leases`, `POST /v0/control/leases/{L}/renew`, `POST /v0/control/leases/{L}/release`, `GET /v0/control/leases[?holder=]` | acquire → renew → release; expiry hand-off; list/filter |
| Control whoami | `GET /v0/control/whoami` | authenticated worker identity |
| Deployment-scoped workers | `POST/GET /v0/control/workers`, `GET /v0/control/workers/{W}`, `POST …/reissue-credential` | register / list / detail / reissue |
| Deployment-scoped groups | `POST/GET /v0/control/groups`, `GET /v0/control/groups/{G}`, `POST …/members`, `DELETE …/members/{W}`, `DELETE /v0/control/groups/{G}` | full member lifecycle |
| **`/admin/control/workers/` UI** | `GET /admin/control/workers/`, `POST /admin/control/workers/`, `GET /admin/control/workers/{id}/`, `POST /admin/control/workers/{id}/reissue-credential` | `admin/control/workers.py` — deployment-scoped worker pages added by 0b67b1d (#146) |
| **`/admin/control/groups/` UI** | `GET /admin/control/groups/`, `POST /admin/control/groups/`, `GET /admin/control/groups/{id}/`, `POST …/members`, `POST …/members/{mid}/remove`, `POST …/delete` | `admin/control/groups.py` — deployment-scoped group pages added by 0b67b1d (#146) |
| **Multi-experiment side-by-side** | two experiments registered + leased simultaneously | port isolation (`FORGEJO_HOST_PORT`/`WEB_UI_HOST_PORT`), data-root isolation; cross-ref #147 for the smoke gap |

### 4.4 Deployment-mode overlays + failure injection

| Surface | How exercised | Location |
|---|---|---|
| **Subprocess mode (e2e, operator-driven)** | bring up `compose.yaml + compose.subprocess.yaml`, drive a full lifecycle as operator (not just the smoke) | [`compose.subprocess.yaml`](../../reference/compose/compose.subprocess.yaml); smoke ref `smoke-subprocess.sh` |
| **Docker-exec mode (e2e, operator-driven)** | bring up `… + compose.docker-exec.yaml` (DooD); drive a lifecycle; confirm sibling-container spawns + no orphans | [`compose.docker-exec.yaml`](../../reference/compose/compose.docker-exec.yaml); smoke ref `smoke-subprocess-docker.sh` |
| **`reissue_credential` recovery** | force a worker bearer mismatch at startup (delete/corrupt the persisted `<credentials_dir>/<worker_id>.token`, or rotate the admin token), restart the worker host, confirm `bootstrap_worker_credential()` detects the 401 on `whoami` and recovers via admin-gated reissue (NOT fresh-register) | [`reference/services/_common/src/eden_service_common/auth.py`](../../reference/services/_common/src/eden_service_common/auth.py) (`bootstrap_worker_credential`, §8.2 no-fall-through rule) |

### 4.5 Bootstrap / lifecycle CLIs (cross-cutting — exercised during every wave's bring-up, but audited as first-class surfaces)

The operator guide ([`docs/user-guide.md`](../user-guide.md)) names the Web UI, Forgejo, `setup-experiment.sh`, and `eden-manual` as the primary operator workflows. The bring-up CLIs are walked incidentally to stand each wave's stack up, but the audit treats their **operator-facing flag surface** as a first-class target (a confusing flag, a missing default, an unhelpful error is a finding):

| Surface | Command / flags | Location | What to verify |
|---|---|---|---|
| **`setup-experiment.sh`** | `--experiment-id`, `--exec-mode {host\|docker}`, `--data-root`, `--env-file`, `--seed-from`, `--ideas-per-ideation`, `--admin-token`, `--postgres-password`, `--no-auto-host-workers` | [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh) | each flag's documented effect; idempotent re-run; `--no-auto-host-workers` actually skips pre-registration; error messages on bad input |
| **`eden-experiment` lifecycle** | `up`, `down [--purge]`, `reset`, `status` (checkpoint/restore/list-checkpoints covered in §4.2) | [`reference/scripts/manual-ui/eden-experiment`](../../reference/scripts/manual-ui/eden-experiment) | clean bring-up/teardown; `reset` round-trip; `status` accuracy; `down --purge` data removal |
| **`eden-manual` role CLI** | `list-tasks`, `show`, `claim`, `checkout`, `push`, `ideation-submit`, `execution-submit`, `evaluation-submit`, `creds` | [`reference/scripts/manual-ui/eden-manual`](../../reference/scripts/manual-ui/eden-manual) + the `eden-manual-*` skills | the skill-driven happy paths (partially exercised in demo); note any flag/error-text gaps surfaced while driving the lifecycle carrier |

### 4.6 Observability surfaces (per [`docs/observability.md`](../observability.md) — not admin-UI routes)

These are documented operator/observability surfaces that aren't HTTP admin routes. Exercised on the base stack (Wave 2) except where a surface needs another stack. The artifacts reader (§2.3), readonly Postgres role + Adminer (§2.7/§3.1), and control-plane enablement (§3.4) are inventoried elsewhere (§4.1/§4.2/§4.3) and cross-referenced here, not duplicated.

| Surface | observability.md ref | What to verify |
|---|---|---|
| **Forgejo Web UI** (`http://localhost:3001`) | §2.2 | browse the canonical repo / branches / commits as the operator would; the documented login + read path works |
| **Wire API raw + Swagger UI** | §2.4, §3.2 | the raw wire endpoints documented for direct `curl`, and the BYO Swagger-UI sibling-container workflow against the OpenAPI doc |
| **Container logs** | §2.5 | `docker compose logs` per-service + cross-experiment dispatch-decision filters render the documented structured JSON |
| **Read-only local clone of Forgejo** | §2.6 | clone the remote read-only and inspect refs as documented |
| **Desktop DB / HTTP clients** | §3.3, §3.5 | the documented `localhost:5433` readonly-DB and HTTP-client connection recipes (cross-ref the readonly role §4.2) |

### 4.7 Surfaces deliberately treated as already-covered (cross-reference, don't re-walk)

- Ideator / executor / evaluator / integrator happy-path lifecycle through UI/CLI/skill — exercised in the 2026-05-23 demo. The audit *re-walks the lifecycle only as the carrier* needed to populate task/variant/ref state for the untested surfaces above; surprises noticed in passing are still filed, but the lifecycle itself is not the audit target.

## 5. Audit protocol (per-surface discipline)

For each surface in §4:

1. **Read the documented happy path first.** The UI text, the skill `SKILL.md`, the service `README.md`, the operator playbooks under `docs/operations/`, or the spec section. Drive the surface following *only* the public docs / UI text — the fresh-operator posture from #180. A moment of "where do I get this value?" is itself a finding.
2. **Drive the happy path.** Record what happened. If it matched the docs, tick the checklist with a one-line "clean" note.
3. **Light adversarial probe** (per §2.2): one obvious bad input (empty required field, stale CAS token, malformed id, double-submit). Record behavior.
4. **File every surprise at the moment.** Each confusion / failure / 500 / doc-drift / surprise → a GitHub issue with: repro steps, observed vs. expected, the surface's file:line, screenshots/log excerpts where useful, triage labels at file time (`type:*`, `triage:*`, `priority:*`, `cluster:*` per [`docs/triage.md`](../../docs/triage.md)), and a cross-reference back to #182.
5. **Tick the checklist** in #182's body: `- [x] <surface> — <date>, notes / bugs filed: #__`. If blocked, `- [ ] <surface> — blocked by #__` and move on (§2.6).
6. **Post a per-session findings comment** on #182 at session end summarizing what was walked, what was filed, what remains.

The recording template (in the session comment):

```text
### Session <date> — Wave <n> (<stack config>)
Walked: <surfaces>
Clean: <surfaces that matched docs>
Filed: #__ (<one-line>), #__ (<one-line>), …
Blocked / deferred: <surface> — <why>, blocked by #__
Coverage delta: <checklist items newly ticked> / <total>
```

## 6. Spec / contract impact

**None directly.** The audit *reads* the spec, JSON Schemas, Pydantic bindings, and wire bindings to judge whether a surface behaves correctly, but changes none of them. Any divergence the audit finds (e.g. the issue's predicted "spec-impl divergence on rarely-tested MUSTs, ~50% confident") is **filed as an issue**; the spec/schema/binding fix is executed in that issue's own chunk, under the normal spec-edit discipline (spec first, then impl, schema↔model parity in lockstep). This plan introduces no normative change.

## 7. Naming map

**None — no identifiers are introduced or renamed.** A process audit creates no classes, functions, enum values, CLI flags, env vars, or spec headings. The only naming-adjacent output is the §1.2 reconciliation of the *issue's* loose surface names against the canonical ones in the code (e.g. the issue's `POST /tasks/<id>/reclaim` → the real `POST /v0/experiments/{E}/tasks/{T}/reclaim`); those corrections live in this plan and in #182's checklist, not in any renamed identifier. If the audit discovers a vocabulary drift in an existing surface, it's filed against [`docs/glossary.md`](../../docs/glossary.md) discipline as a separate issue.

## 8. Migration / cleanup

**None — nothing is retired.** No code, schema, or doc is removed by the audit (CLAUDE.md no-backwards-compat-shims posture is irrelevant here: there's nothing to shim and nothing to migrate). The only persistent artifact the audit *creates* is the live checklist in #182's body and the filed issues; both are intended to outlive the audit sessions. When the audit completes, #182 is closed with a final summary comment; the filed issues carry the going-forward work.

## 9. Conformance impact

**None directly.** The conformance suite asserts spec MUSTs through the chapter-7 IUT contract; this audit neither adds nor edits scenarios. Where the audit finds that a rarely-tested MUST diverges from the impl, the filed issue may *recommend* a conformance scenario — but authoring it is that issue's work, gated on whether the MUST is observable through the chapter-9 §6 IUT contract (per the AGENTS.md "Conformance-plan MUSTs must be filtered through the IUT contract" pitfall). No `§`-reference or assertion changes in this chunk.

## 10. Chunked execution plan

Waves are grouped by **stack configuration** so each wave shares one bring-up (the expensive step). Within a wave the surfaces are sequential; the **completion gate** for a process audit is *coverage + filing discipline*, not CI green. Waves are otherwise independent and could be parallelized across agent sessions on separate ports/data-roots if throughput matters (§12 risk: port/volume collision) — default is sequential for a single coherent filing pass.

### Wave 0 — Audit harness + checklist scaffold

- Seed the full §4 surface inventory into #182's body as the live checklist (one line per surface, grouped by wave).
- Bring up a clean base stack via `eden-experiment up <fixture-config> --experiment-id <id>`; confirm reproducible bring-up and that the manual skills (`eden-manual-*`) drive the lifecycle.
- Confirm the recording template (§5) and the issue-filing label set (`type:*` / `triage:*` / `priority:*` / `cluster:*`) are ready, so the *first real finding* files cleanly against the template — no dummy/placeholder issue is filed (that would cut against the issue-per-surprise rule §2.3).

**Gate:** checklist posted to #182; clean base stack reproducibly up; recording template + label set confirmed ready.

### Wave 1 — Base-stack per-experiment admin UI (§4.1)

- Drive a full ideator→executor→evaluator→integrator lifecycle as the *carrier* to populate tasks/variants/ideas/refs in varied states (claimed, submitted, integrated, orphaned).
- Walk every §4.1 surface: signin/signout session flow, authenticated `/artifacts` reader, dashboard, observability pages, the `/admin/artifacts/` listing, work-refs (list + CAS delete on a real orphan ref), dispatch-mode (toggle all 4 keys), groups (register/detail/add/remove/delete), workers (register/detail/reissue-credential), task reclaim, task reassign (none/worker/group + reason), create-execution-task, terminate-experiment. Confirm `/admin/experiments/` 404s on the base stack (documented; it moves to Wave 3 where the control plane is enabled).

**Gate:** every §4.1 surface ticked or `blocked by #__`; per-session comment posted; all surprises filed + triaged.

### Wave 2 — Base-stack wire + data-lifecycle (§4.2)

- Direct-wire reclaim via `curl` (worker-gated; `cause=operator`) and reassign via `curl` — reassign requires a **worker bearer in the `admins` group**, not the bootstrap `admin:<token>` (which 403s on that route). First reissue/mint that worker bearer (the `ADMIN_WORKER_BEARER` pattern in [`smoke-manual-mode.sh`](../../reference/compose/healthcheck/smoke-manual-mode.sh)), then verify `reason` + `reassigned_by` stamping.
- Adminer + Postgres readonly role: follow [`docs/observability.md`](../observability.md) §3.1 to attach a BYO `adminer:4` sibling container, plus a direct desktop-client connect to `localhost:5433` as `eden_readonly`; confirm `SELECT` + the `variant_unpacked` view work and `worker.credential_hash` is blocked. (No "no Adminer" issue — the BYO posture is documented and intentional.)
- `eden-experiment checkpoint` then `restore` round-trip (post-#177 non-empty assertion); cross-ref #152 for the wire checkpoint/import smoke rather than re-walking it.
- The §4.5 bootstrap/lifecycle CLI flag surfaces walked while standing up / tearing down this wave's stack: `setup-experiment.sh` flags, `eden-experiment up/down/reset/status`, `eden-manual` subcommands.
- The §4.6 observability surfaces (per [`docs/observability.md`](../observability.md)): Forgejo Web UI, raw wire + Swagger UI, container logs, read-only Forgejo clone, desktop DB/HTTP clients.

**Gate:** every §4.2 + §4.5 + §4.6 surface ticked or `blocked by #__`; comment posted; surprises filed + triaged.

### Wave 3 — Control-plane stack (§4.3)

- **Concrete bring-up** (per [`docs/observability.md`](../observability.md) §3.4): since #147 the control-plane-server is a first-class always-on Compose service; set `EDEN_CONTROL_PLANE_URL=http://control-plane:8081` in `.env` and recreate `web-ui` so `--control-plane-url` is set and the `/admin/experiments/` + `/admin/control/*` routes register. The web-ui routes are 404 on the default stack — that env var is the gate.
- Walk the `/admin/experiments/` dashboard (register + select + unregister — now reachable), the lease primitive (acquire → renew → release → expiry hand-off + list/filter), the deployment-scoped worker/group registry (wire `/v0/control/*` + `/admin/control/workers/` + `/admin/control/groups/` UI incl. reissue), `GET /v0/control/whoami`, and multi-experiment side-by-side (two experiments registered + leased, port/data-root isolation; cross-ref #147).

**Gate:** every §4.3 surface ticked or `blocked by #__`; comment posted; surprises filed + triaged.

### Wave 4 — Deployment-mode overlays + failure injection (§4.4)

- Subprocess mode: bring up `compose.yaml + compose.subprocess.yaml`, drive a full lifecycle as operator, note any divergence from the smoke's automated path.
- Docker-exec mode: layer `compose.docker-exec.yaml` (DooD), drive a lifecycle, confirm sibling-container spawns + no orphans post-quiescence.
- `reissue_credential` recovery: force a worker bearer mismatch at startup (delete/corrupt the persisted token, or rotate the admin token), restart the worker host, confirm `bootstrap_worker_credential()` recovers via admin-gated reissue and does NOT fall through to fresh-register.

**Gate:** every §4.4 surface ticked or `blocked by #__`; comment posted; surprises filed + triaged.

### Wave 5 — Sweep, triage, close

- Final sweep against the §4 checklist, covering **all** surface kinds — not just HTTP routes: (a) the web-ui route tree (`reference/services/web-ui/.../routes/`, including `auth.py` + `artifacts.py`), (b) the wire server + control-plane app endpoints, **and** (c) the operator CLI/deployment surfaces — `setup-experiment.sh` flags, `eden-experiment` subcommands, `eden-manual` subcommands, and the compose overlays (`compose.subprocess.yaml` / `compose.docker-exec.yaml` / `compose.control-plane.yaml` / `compose.multi-orchestrator.yaml`). Tick any surface walked-in-passing; file + retroactively add any surface discovered but not in §4.
- Confirm every filed issue carries triage / priority / cluster labels.
- Post a final summary comment on #182 (total surfaces walked, total issues filed, coverage %, deferred surfaces with reasons) and close #182.

**Gate:** all §4 checklist items ticked or explicitly `deferred — <reason>`; all filed issues triaged; #182 closed with summary.

## 11. Tracking format

Per the issue, #182's body grows a **live checklist**. The sample below is **illustrative, not exhaustive** — Wave 0 seeds the checklist from the *full* §4 inventory (every row of §4.1–§4.6), one line per surface, reconciled against [`docs/observability.md`](../observability.md). The sample shows the shape and the load-bearing surfaces:

```markdown
## Surfaces audited (live checklist)  — illustrative; Wave 0 seeds the full §4 inventory

### Wave 1 — base-stack admin UI
- [ ] `/signin` + `/signout` session flow (no-credential "continue as <worker_id>"; unauth→303 redirect) — date, notes / bugs filed: #__
- [ ] `GET /artifacts` authenticated reader (path-traversal guard) — date, notes / bugs filed: #__
- [ ] `/admin/work-refs/` (list + CAS delete) — date, notes / bugs filed: #__
- [ ] `/admin/dispatch-mode/` (all 4 keys) — date, notes / bugs filed: #__
- [ ] `/admin/groups/<id>/` (add/remove/delete) — date, notes / bugs filed: #__
- [ ] `/admin/workers/<id>/reissue-credential` — date, notes / bugs filed: #__
- [ ] admin reclaim + reassign (UI) — date, notes / bugs filed: #__
- [ ] create-execution-task + terminate-experiment (UI) — date, notes / bugs filed: #__
- [ ] confirm `/admin/experiments/` 404s on base stack (control-plane-gated; walked in Wave 3) — date, notes / bugs filed: #__
### Wave 2 — base-stack wire + data lifecycle + CLIs + observability
- [ ] reclaim/reassign (wire; reassign needs `admins`-group worker bearer) — …
- [ ] Adminer BYO workflow + postgres readonly role at `:5433` — …
- [ ] `eden-experiment checkpoint` + `restore` round-trip (post-#177) — …
- [ ] `setup-experiment.sh` flag surface (`--exec-mode` / `--data-root` / `--no-auto-host-workers` / …) — …
- [ ] `eden-experiment up/down/reset/status` lifecycle — …
- [ ] `eden-manual` role CLI subcommands — …
- [ ] observability §4.6: Forgejo Web UI, raw wire API + Swagger UI, container logs, read-only clone, desktop clients — …
### Wave 3 — control-plane
- [ ] `/admin/experiments/` register + select + unregister (now reachable) — …
- [ ] `/v0/control/*` lease primitive (acquire/renew/release/list + whoami) — …
- [ ] `/v0/control/experiments` + deployment-scoped workers/groups (wire) — …
- [ ] `/admin/control/workers/` pages (list/register/detail/reissue) — …
- [ ] `/admin/control/groups/` pages (list/register/detail/add/remove/delete) — …
- [ ] multi-experiment side-by-side — …
### Wave 4 — overlays + failure injection
- [ ] subprocess mode e2e — …
- [ ] docker-exec mode e2e — …
- [ ] `reissue_credential` recovery (forced bearer mismatch) — …
```

Per-session findings are posted as **comments** below the issue (template in §5). The checklist is the cross-session state of record.

## 12. Risks / things to watch

- **The audit surfaces a blocker that prevents reaching downstream surfaces.** Mitigation: §2.6 — file the blocker, attempt a documented workaround, record the coverage gap as `blocked by #__`, keep going. Don't let one 500 halt the wave.
- **Issue-filing fatigue → batching at end.** The demo's filing discipline worked *because* it filed live. Mitigation: §2.3 / §5 — file at the moment, one issue per surprise; the per-session comment is a summary, not the filing mechanism.
- **Duplicate issues against already-known gaps.** Several CI issues exist (#152 / #147 / #156). Mitigation: §2.4 — search before filing; cross-reference, don't duplicate.
- **Surface inventory is incomplete.** §4 is verified-at-authoring but the tree moves. Mitigation: Wave 5's grep sweep catches surfaces added between plan and execution; the checklist is appended retroactively.
- **Control-plane and overlay stacks are the least-trodden bring-ups** (Wave 3 / Wave 4) — the bring-up itself may fail before any surface is reachable. That failure *is* a finding (file it). Use the AGENTS.md "CI failure diagnosis: local repro beats log-tail reading" posture; the smoke scripts (`smoke-subprocess*.sh`, `smoke-multi-orchestrator.sh`) are the reference for how each stack is brought up.
- **Multi-experiment side-by-side has no automatic port-collision detection** (confirmed: compose project name is hardcoded `eden-reference`; isolation is manual via `FORGEJO_HOST_PORT` / `WEB_UI_HOST_PORT` + distinct `--data-root`). Running two stacks needs explicit port assignment; a collision is itself a finding worth filing (operator ergonomics gap). Cross-ref #147.
- **`reissue_credential` failure-injection is destructive to a running worker's identity.** Do it on a throwaway experiment, not one mid-audit on another surface. The §8.2 no-fall-through-to-fresh-register rule is the specific behavior to confirm.
- **Scope creep into fixing.** The strongest pull during a dogfood audit is to fix the bug you just found. Mitigation: issue §"Out of scope" + §3 — file and move on; fixes are separate chunks.

## 13. Estimated effort

| Activity | Estimate |
|---|---|
| Wave 0 (harness + checklist scaffold) | ~0.25 day |
| Wave 1 (base-stack admin UI — the largest surface count) | ~0.5 day |
| Wave 2 (wire + data lifecycle) | ~0.25 day |
| Wave 3 (control-plane stack — least-trodden bring-up) | ~0.5 day |
| Wave 4 (overlays + failure injection) | ~0.5 day |
| Wave 5 (sweep + triage + close) | ~0.25 day |
| **Total (the walking-through)** | **~2.25 days** |

Consistent with the issue's "~1-2 dedicated days of focused dogfood-style work," skewing slightly higher because the control-plane + overlay bring-ups (Waves 3-4) are the least-exercised and most likely to need diagnosis before any surface is reachable. **This tracks the audit itself, not the fixes** — the issues filed will take far longer to resolve, and each is its own tracked chunk.
