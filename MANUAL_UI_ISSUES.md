# Manual UI session — issues found

Local scratchpad. Each entry is a candidate GitHub issue; convert when ready.
Worktree: `/Users/ericalt/Documents/eden-worktrees/test-main` (detached at
`2e7c4eb`, manual-ui-1 experiment).

---

## 1. Orchestrator should be persistent infra, not bound to one experiment's lifetime

**Owner's mental model.** The orchestrator is a piece of infrastructure
that runs experiments — many of them, concurrently or sequentially. One
experiment going idle / quiescing / being archived doesn't mean the
orchestrator's job is over. It should stick around to host the next
experiment, accept new plan tasks injected by operators, etc.

**Current code.** `run_orchestrator_loop` (`reference/services/
orchestrator/src/eden_orchestrator/loop.py:46-84`) is hard-wired to a
single experiment passed via the required `--experiment-id` CLI flag.
It seeds plan tasks at startup (line 52-59), runs the loop, and exits 0
after `max_quiescent_iterations` consecutive zero-progress iterations
(line 80-82). `restart: "on-failure"` on the compose service lets exit-0
stick. Lifetime of orchestrator process == lifetime of one experiment.

**Two things wrong with this, somewhat independent.**

**1a. Within-experiment: "no work right now" ≠ "experiment over".** Even
for a single experiment, an operator can inject new plan tasks via the
admin panel, replan after looking at trial results, or just be slow. The
30s quiescence-exit is a heuristic that conflates "no work in flight" with
"done forever", which is wrong even with the current single-experiment
scope. Smoke-test deployments (where every plan task is auto-claimed by a
worker host within milliseconds) hide this; manual sessions and any
operator-in-the-loop workflow expose it.

**1b. Cross-experiment: the orchestrator should be multi-tenant.** The
orchestrator is the dispatcher for the deployment, not the runner of a
single experiment. Phase 12 already names this in the roadmap (control
plane + lease data model + multi-replica orchestrator + experiment
switcher in Web UI + cross-experiment views), but the current 1:1
binding via `--experiment-id` is a Phase-8b expedient that needs unwinding
when Phase 12 lands.

**Possible fixes.**
- Short-term, just for 1a: drop the quiescence-exit branch entirely (or
  make `--max-quiescent-iterations 0` mean "never exit"). The orchestrator
  becomes a pure event loop that runs until SIGTERM. Operator declares
  "experiment done" through some other mechanism (admin action, control
  plane, or just `compose stop`).
- Long-term, for 1b: align with Phase 12. Orchestrator subscribes to a
  control plane and runs a loop *per active experiment*, not bound to one.
  Quiescence becomes meaningful again — but it's per-experiment status
  ("this experiment has nothing to do; archive when ready"), not a process
  exit signal.
- Honor `experiment_config.max_wall_time` / `max_trials` as the termination
  signal for "this experiment is done"; that's a state transition, not a
  process exit, and other experiments keep running.

**Workaround applied in this worktree.** Bumped `--max-quiescent-iterations`
to 36000 in `reference/compose/compose.yaml` (~10 hours at 1s poll). Crude
but unblocks manual sessions today.

---

## 2. Planner draft form loses typed state if you navigate after a validation error

**What happened.** Filled in proposals on the planner page, hit submit
without parent_commits SHA → got a validation error. Couldn't recover by
editing the form; the only way to submit successfully was to reclaim the
task from `/admin/`, claim again from `/planner/`, and re-type everything.

**Root cause.** The validation-error path in
`reference/services/web-ui/src/eden_web_ui/routes/planner.py:244-258` does
re-render `planner_claim.html` with the typed values populated, so
edit-and-resubmit on the same response page works. BUT if the user navigates
anywhere — back button, refresh, clicking a nav link — and lands on
`GET /planner/<task_id>/draft` (planner.py:111-138), that handler always
renders a blank form (`form_state: [_empty_row()]`, planner.py:135) and the
typed state is gone. The session still holds a valid claim in `_CLAIMS`, but
none of the typed input.

**Fix.** The `draft_form` GET handler should re-hydrate from a
session-scoped draft buffer (write on every submit, including failed
validation) rather than always rendering an empty row.

**Smaller adjacent fixes.**
- The error-rendered form has no "your form state is volatile, don't navigate
  away" warning. A banner would help.
- Consider rendering the draft state in `localStorage` so an accidental
  refresh doesn't lose work even before the first submit.

---

## 3. Planner form gives no hint where to find a valid commit SHA

**What happened.** Faced the `parent_commits` field on the very first plan
draft and had no idea what to put there. The setup-experiment script printed
the seed SHA at install time, but that's gone from the terminal by the time
you're in the UI.

**Fix.** Surface available commit SHAs on the planner page:
- A "Recent integrated trials" panel (mirroring what's on the admin trial
  list) with copy-to-clipboard SHAs.
- The seed/base commit SHA as a labeled option ("Base: `666b95f0...`").
- Possibly a dropdown / autocomplete on the parent_commits field instead of
  a free-text input.

**Adjacent.** `EDEN_BASE_COMMIT_SHA` is already in `.env`; the web-ui has
`--repo-path` (so it can `git rev-parse HEAD` on each integrated branch).
Both are available without new wire endpoints.

---

## 4. Implementer submit doesn't fetch from origin before commit_exists check

**What happened.** Played the implementer role: cloned from gitea, made
changes, pushed `my-work` branch back to gitea, pasted the resulting SHA
into the implementer form. The web-ui's local clone hadn't fetched since
startup, so `repo.commit_exists(sha)` returned False and the form rejected
with "commit not found in the bare repo; did you push it?".

**Workaround.** Run a manual fetch before submitting:
```
docker compose --env-file .env exec web-ui \
    git -C /var/lib/eden/repo fetch origin '+refs/heads/*:refs/heads/*'
```

**Root cause.** `reference/services/web-ui/src/eden_web_ui/routes/
implementer.py:225` calls `repo.commit_exists(draft.commit_sha)` directly
without a prior `repo.fetch_ref(...)` or `repo.fetch_all_heads()`. Per
Phase 10d follow-up B's roadmap delta, the integrator was patched to
fetch before the chapter-6 §2 reachability check; the same fix needs to
apply to the web-ui implementer submit.

**Fix.** Before `commit_exists`, call something like
`repo.fetch_all_heads()` (or, more targeted, fetch by commit SHA if
available). Same posture as the integrator's per-promote-call fetch.

**Adjacent.** The admin work-refs page got a similar fix; the implementer
submit is the matching gap.

---

## 5. Implementer page tells you to push to a container path

**What happened.** Implementer page instructions say:

> 2. Push your tip commit (any branch name works) to the bare repo at
>    `/var/lib/eden/repo`. The UI will create a canonical ref...

That path is INSIDE the web-ui container. Users push to gitea (the
actual remote-of-record per Phase 10d follow-up B), not to the web-ui's
local clone.

**Fix.** Update `implementer_claim.html` to display the gitea remote URL
(host-accessible — `http://localhost:3001/eden/<experiment-id>.git` in
Compose) and a copy-pastable clone command, including credentials or a
note about how to obtain them. Or, more ambitious: render a per-task
shell snippet that runs the full workflow (clone, checkout, push,
fetch-on-server, paste SHA).

---

## 6. Orchestrator crashes the whole process on a malformed trial

**What happened.** Built a CLI implement-submit that POSTed `create_trial`
without setting the `branch` field. The first such trial reached
`status=success` after evaluation. The integrator's
`_require_promotion_preconditions` raised `NotReadyForIntegration: trial
'<id>' has no branch`. That exception propagates up through
`_promote_successful_trials` → `run_orchestrator_iteration` →
`run_orchestrator_loop` → `main()`, crashing the orchestrator process
entirely. `restart: on-failure` brings it back, the loop re-encounters
the same trial, and crashes again. Persistent crash loop until the
malformed trial is hand-patched in postgres.

**Workaround applied.** Patched the trial's `branch` field directly in
postgres so the integrator could promote it. After that, the orchestrator
recovered.

**Root cause / design issue.** A single malformed trial state (caused by
an exotic submission path the spec presumably didn't anticipate) bricks
the orchestrator. The reasonable behavior is "log + skip + continue" so
one trial's brokenness doesn't take down the whole dispatcher.

**Fix candidates.**
- Wrap each `integrate_trial(trial.trial_id)` call in
  `try/except NotReadyForIntegration` (and arguably `Exception` more
  broadly) inside `_promote_successful_trials`. Log + continue.
- Add a server-side validity check at `create_trial` time that rejects
  trials missing `branch` when `status=starting` (depends on whether
  branch is normatively required at create_trial — see
  `spec/v0/03-roles.md` §3.2 step 1).
- Surface `NotReadyForIntegration` as a recoverable trial state
  (`integration_failed`?) rather than an unhandled exception.

**Note.** I caused this by writing a buggy CLI; the bug is in my CLI
(now fixed). But the orchestrator's blast radius for any *future*
malformed trial state is what makes this a real issue.

---

## 7. `_dispatch_evaluate` requires `trial.branch` indirectly via integration

**What happened.** When the broken trial blocked integration, evaluate
tasks for *other* trials were still dispatched correctly — confirmed by
the orchestrator continuing to log dispatches even while the integration
exception was firing. Good. But the per-iteration crash means every
N seconds of crash-restart costs ~1 iteration of progress. So new work
*does* eventually move forward, just slowly.

**Not a separate bug** — same root cause as #6. Listed for completeness:
the impact of #6 isn't "everything stops"; it's "everything proceeds at
~1/restart-interval the normal rate".

---

## 8. `compose down -v` doesn't remove `eden-repo-init-staging`

**What happened.** Tearing down the manual-ui-1 stack with `docker compose
--env-file .env down -v` correctly wiped the postgres / gitea / artifacts
/ orchestrator-repo / web-ui-repo / blob-data volumes. But the
`eden-repo-init-staging` volume *survived*. Re-running setup-experiment
against the same volume → `repo_init.py` saw an existing seeded bare repo
in the staging volume and short-circuited with `EDEN_REPO_ALREADY_SEEDED`,
so the new `--seed-from <dir>` was silently ignored. I had to manually
`docker volume rm eden-reference_eden-repo-init-staging` before the new
seed would take.

**Hypothesis.** `eden-repo-init` is in `profiles: ["setup"]` (so it's
hidden from plain `compose up`). `compose down -v` may treat profile-gated
services' volumes as out-of-scope unless the profile is explicitly named
on the down command. Worth confirming with `compose down -v --profile
setup` or similar.

**Impact for manual sessions.** Any "wipe and re-seed with new content"
workflow silently no-ops unless you also remove the staging volume. The
silent-failure mode (no warning, just an old SHA) makes this easy to miss.

**Fix candidates.**
- setup-experiment.sh: when `--seed-from` is set, pre-remove the staging
  volume (or call `_existing_seed_sha` and refuse to short-circuit when
  the seed source has changed).
- Document the `--profile setup` flag on `compose down -v` (if that's
  actually the right invocation).
- Move `eden-repo-init-staging` out of profile-gated volumes so it's
  always tracked by `down -v`.

---

## 9–12. Orchestrator-and-worker-roles design

Concerns about plan-task budget (static N), worker affinity
(humans/agents/specific ids), orchestrator-as-a-role (humans
playing orchestrator), and worker attribution (knowing who did
what) are consolidated into a single design doc:

- [`docs/design/orchestrator-and-worker-roles.md`](docs/design/orchestrator-and-worker-roles.md)

Issues #9–#12 are tracked there. This file keeps only the
lower-level UX/infra issues; the spec-level discussion lives in
the design doc.

---

## 13. `planner_root` and `workspace` are dead keys in the experiment config

**What's there.** The fixture
[`tests/fixtures/experiment/.eden/config.yaml`](tests/fixtures/experiment/.eden/config.yaml)
declares:

```yaml
planner_root: "./planner"
workspace: "./workspace"
```

**What's missing.** Neither key is defined in
[`spec/v0/schemas/experiment-config.schema.json`](spec/v0/schemas/experiment-config.schema.json)
(top-level properties are
``parallel_trials, max_trials, max_wall_time, metrics_schema,
objective, convergence_window, target_condition``). Neither key is
read anywhere in the reference implementation. They are tolerated
silently because the schema's ``additionalProperties`` is permissive
by default.

**Why this matters.** The keys *look meaningful* — a user (or
another Claude session) reasonably assumes the planner role has its
own scoped directory to operate in. There's no way to discover
they're dead without grep'ing the codebase. Documentation-impl
drift: the fixture promises something the code doesn't deliver.

**Three resolution options** (need a design call):

1. **Remove the keys.** Easiest. Clean up the fixture, tighten the
   schema with ``additionalProperties: false`` so future stray keys
   get caught.

2. **Implement them.** Give the planner role a workspace directory.
   Candidate uses (none normative today):
   - **Read context.** A snapshot of integrated trials' content
     (diffs, evaluator metrics) the planner consults while drafting.
   - **Scratch / persistent notes.** Cross-session planner notes
     preserved across plan tasks.
   - **Rationale authoring.** A directory the planner writes
     ``rationale.md`` etc. into, which the host bundles into the
     proposal artifact at submission time.

3. **Leave the schema permissive but document the keys as
   "informative only / for forks to interpret"**. Weakest;
   preserves confusion.

**Connects to** the role-binding spec note in
``experiment-config.schema.json`` itself — the description mentions
that role-binding fields are deferred to a future chapter. Both
``planner_root`` and ``workspace`` are role-binding fields in
spirit; clarifying them is part of that chapter's scope.

---

## 14. Spec-impl drift: termination fields are spec'd but unenforced

**Triggered by** the dead-key audit prompted by #13. While confirming
that `planner_root` / `workspace` are dead, found a much bigger gap:
**four spec'd experiment-config fields have no consumer in any
orchestrator code**.

### What the spec promises

[`spec/v0/02-data-model.md`](spec/v0/02-data-model.md) §3 documents
four termination-related fields:

| Field | Spec text |
|---|---|
| ``max_trials`` | (required) implicit termination when reached |
| ``max_wall_time`` | (required) implicit termination when exceeded |
| ``convergence_window`` | "Terminate if the objective has not improved in this many trials." |
| ``target_condition`` | "A condition over metrics; when satisfied, the experiment terminates early." |

All four are present in
[`spec/v0/schemas/experiment-config.schema.json`](spec/v0/schemas/experiment-config.schema.json)
and
[`reference/packages/eden-contracts/src/eden_contracts/config.py`](reference/packages/eden-contracts/src/eden_contracts/config.py).

### What the impl actually does

`grep -rn` across the reference impl outside of contracts and tests
shows zero consumers of any of these fields. The orchestrator's
*only* termination mechanism is the 30-iteration quiescence heuristic
(``--max-quiescent-iterations``) — see issue #1. That heuristic
doesn't reference ``max_trials``, ``max_wall_time``,
``convergence_window``, or ``target_condition`` at all.

### Why this matters

A user setting ``max_trials: 20`` in the experiment config reasonably
expects the experiment to stop after 20 trials. It doesn't. The
termination semantics they got is whatever the orchestrator's
quiescence heuristic produces, which is unrelated to any of the
documented bounds.

This is more concerning than #13's dead keys because:
- The keys *are* in the spec (so a conforming implementer would expect
  them to be enforced).
- The Pydantic models *do* validate them (so they pass schema parity
  tests, hiding the impl gap).
- The behavior they encode is fundamental to the experiment lifecycle.

### Resolution direction

After discussion, the preferred direction is **none of the above —
remove the four fields from the spec entirely**. Termination is
deployment policy, not protocol. The spec defines a *mechanism* (a
termination-decision callback the orchestrator consults each
iteration; input = read-only experiment state; output =
terminate/continue); the deployment supplies the *policy* (whatever
predicate it wants — could be max_trials, could be metric thresholds,
could be operator-driven).

See [`docs/design/orchestrator-and-worker-roles.md`](docs/design/orchestrator-and-worker-roles.md)
§9 ("Termination is deployment policy, not spec mechanism") for the
full design. In that model:

- The four fields disappear from the normative spec. Reference impls
  may ship a library of pluggable policies (one of which honors those
  fields) but they're no longer part of the protocol surface.
- Quiescence-exit goes away — it's superseded by an explicit
  termination decision.
- A new ``experiment.terminated`` event records the decision.

The drift documented in this issue stays a real bug *for the current
spec*, since the four fields ARE in v0 today. Resolution either ships
the implementation that matches the spec, or ships the spec change
that aligns with the design doc. Latter is preferred per design.

### Other audit findings (lower-severity)

- **Both fixtures carry `planner_root` / `workspace`**:
  `tests/fixtures/experiment/.eden/config.yaml` AND
  `conformance/src/conformance/fixtures/minimal-experiment.yaml`.
  Both should be updated when #13 is resolved.
- **`*_command` keys are intentional drift**, not cruft. Per
  [`docs/plans/review/eden-protocol-bootstrap/impl/20260423T095344/1.md`](docs/plans/review/eden-protocol-bootstrap/impl/20260423T095344/1.md)
  these were deliberately removed from the normative schema during
  bootstrap; they live on as fixture-only additional properties for
  the subprocess-mode hosts to read. Documented design choice; flag
  the fact in role-binding chapter 2 when written, but not a bug.
- **Predecessor lives at `/Users/ericalt/Documents/direvo`** on this
  machine. EDEN repo doesn't reference it directly (other than
  historical roadmap notes). Cross-checking against direvo's source
  for additional carryover would be a useful exercise but out of
  scope here.

---

## 15. Proposal `priority` field is unused for dispatch ordering

**Spec text** ([`spec/v0/02-data-model.md`](spec/v0/02-data-model.md)
§5.1 line 158, [`spec/v0/03-roles.md`](spec/v0/03-roles.md) §2.4 line
67 / §2.3 line 61):

> `priority` | yes | number | Ordering hint; higher dispatches earlier.
>
> dispatch ordering is determined by each proposal's `priority` field.

**Impl reality.** `_dispatch_implement_tasks` in
[`reference/packages/eden-dispatch/src/eden_dispatch/driver.py:84`](reference/packages/eden-dispatch/src/eden_dispatch/driver.py)
iterates ``store.list_proposals(state="ready")`` and creates implement
tasks in *list order*. ``list_proposals`` (sqlite + postgres backends)
sorts by ``proposal_id`` (alphabetical UUID hex), not by priority.
Memory backend has no documented ordering at all. Priority is read on
the proposal object and stored, but never consulted.

**Severity.** SHOULD-level claim per the spec wording ("higher values
SHOULD dispatch earlier"), so a deployment that ignores priority is
not strictly nonconformant. But the field's *only* documented
purpose is ordering, so leaving it unused makes it dead weight on
proposals.

**Resolution.** Either:
1. Sort by ``-priority`` then ``proposal_id`` (stable tiebreak) in the
   dispatch loop. Trivial change to `_dispatch_implement_tasks`.
2. Remove ``priority`` from the proposal schema if there's no real use
   case. The fixture sets it to 1.0 for every proposal, suggesting no
   one has needed differentiation.

Option 1 is the spec-aligned answer. Option 2 is honest about what
the impl actually delivers.

---

## 16. `.env.example` is severely out-of-date and gate-keeps the wrong information

**What's there.** [`reference/compose/.env.example`](reference/compose/.env.example)
documents 7 environment variables (postgres + gitea ports/secrets).

**What's actually used by compose.** A fully-populated ``.env`` (as
written by setup-experiment) carries 24+ variables, including
critical ones like ``EDEN_SHARED_TOKEN`` (bearer auth),
``EDEN_SESSION_SECRET`` (web UI cookie signing), ``EDEN_STORE_URL``
(postgres DSN), ``EDEN_EXPERIMENT_ID``, ``EDEN_BASE_COMMIT_SHA``,
``EDEN_PLAN_TASKS`` and ``EDEN_PROPOSALS_PER_PLAN`` (orchestrator
policy), and the docker-exec / subprocess overlay variables
(``EDEN_EXPERIMENT_DIR_HOST``, ``EDEN_GITEA_CREDS_DIR_HOST``,
``EDEN_EXEC_*``, ``EDEN_DOCKER_GID``, ``EDEN_CIDFILES_DIR_HOST``).
None of these are in `.env.example`.

**Why this matters.** Phase-10a documentation
([`docs/plans/eden-phase-10a-compose-infrastructure.md`](docs/plans/eden-phase-10a-compose-infrastructure.md))
tells operators they can run ``cp .env.example .env && docker compose
up -d`` for a quickstart. That fails today because the example is
missing required variables. The actual workflow (run
``setup-experiment.sh`` first, which generates ``.env`` from scratch)
is correct, but `.env.example` no longer matches the current shape and
is misleading rather than helpful.

**Updated in this audit.** I extended `.env.example` to cover all 24
current variables with explanatory comments, plus a note clarifying
that the file is now documentation-of-shape rather than a
copy-and-go template. setup-experiment remains the authoritative
generator.

**Connects to** the broader pattern of stale design / quickstart docs
referencing workflows that have since evolved. The Phase-10a plan
doc itself is a candidate for either updating or moving to
`docs/archive/`.

---

## 17. ✅ Resolved. Top-level `README.md` and `CONTRIBUTING.md` "phase" claims are
~7 phases stale

**What's there.** [`README.md`](README.md) line 20 ("Status" section) and
[`CONTRIBUTING.md`](CONTRIBUTING.md) line 9 ("Current phase") both open
with **"Phase 4 complete."** README's prose then describes Phase 4's
Pydantic bindings as the latest milestone and points at Phase 5 as
"next". CONTRIBUTING says "the most useful next area is Phase 5 (the
in-memory reference dispatch loop)".

**Reality.** Per [`AGENTS.md`](AGENTS.md) and `git log`, the codebase is
at **Phase 11 chunk 11d complete** (v1+roles+integrator conformance
suite shipped, 110/110 scenarios). Phases 5, 6, 7, 8, 9, 10, 11 are all
complete. The next area is Phase 12 (multi-experiment / control plane).
[`docs/roadmap.md`](docs/roadmap.md) is correctly up to date — only the
two top-level docs are stale.

**Why this matters.** README is the first impression for anyone landing
on the repo. CONTRIBUTING is the first impression for anyone wanting to
help. Both currently misrepresent ~7 phases of progress and route would-
be contributors at work that's been done. Worse: CONTRIBUTING line 11
says "Phase 5 is the most useful next area" — a contributor who follows
that guidance would re-implement work that's been shipped.

**Adjacent.** CONTRIBUTING.md line 40 says **"Node.js 20+ for the
reference web UI"** as a prerequisite. The reference web UI is
server-side Jinja with HTMX 1.9.12 vendored under `static/`; there is
no Node runtime requirement and no Node-side build step. (Node IS used
for `npx markdownlint-cli2` per AGENTS.md, but that's a doc-lint
prerequisite for spec contributors, not a web-ui prerequisite.)

**Severity.** Cosmetic but high-visibility. Top-of-funnel docs.

**Resolution direction.** Replace both "Phase 4 complete" blocks with
short one-paragraph summaries that mirror the AGENTS.md "Current phase"
lead (or simply point at it). Drop the "Phase 5 is next" guidance.
Remove the bogus Node prerequisite from CONTRIBUTING; if any Node
pre-req remains relevant (markdownlint-cli2 for docs contributors), word
it as such.

**Resolved.** README.md "Status" and CONTRIBUTING.md "Current phase"
sections rewritten to reflect Phase 11 completion + the next-phase
landscape (Phase 12 / 13). Bogus Node-for-web-UI prereq removed;
Node clarified as docs-lint-only. Routed contributors at the
conformance-coverage matrix and MANUAL_UI_ISSUES instead of the
no-longer-meaningful "Phase 5 is next" call to action.

---

## 18. ✅ Resolved. Duplicate `load_experiment_config` in two packages, called
inconsistently across services

**What's there.** Two byte-equivalent 5-line implementations of
`load_experiment_config`:

- [`reference/services/_common/src/eden_service_common/experiment_config.py:19`](reference/services/_common/src/eden_service_common/experiment_config.py)
  — re-exported from `eden_service_common`'s public `__init__`.
- [`reference/services/task-store-server/src/eden_task_store_server/app.py:18`](reference/services/task-store-server/src/eden_task_store_server/app.py)
  — re-exported from `eden_task_store_server`'s public `__init__`.

Both: open the path, `yaml.safe_load`, `ExperimentConfig.model_validate`.

**Inconsistent callers.** Of the four worker hosts + web-ui that load
configs, the import paths split:

| Service | Imports from |
|---|---|
| `planner` | `eden_service_common` |
| `implementer` | `eden_service_common` |
| `evaluator` | `eden_task_store_server` |
| `web-ui` | `eden_task_store_server` |
| `task-store-server` | `eden_task_store_server.app` (its own) |

**Why this matters.** Two sources of truth for the same loader. A
future change to YAML loading (env-var interpolation, schema-version
gating, etc.) has to land in two files or the services drift apart by
loader. Also: `eden_task_store_server` exporting `load_experiment_config`
makes the server look like a re-usable library when its real job is to
host a uvicorn app.

**Severity.** SHOULD-level (DRY / single source of truth).

**Resolution direction.** Drop the `load_experiment_config` definition
from `eden_task_store_server.app`. Have the task-store-server's `cli.py`
and the evaluator + web-ui CLIs import from `eden_service_common`
instead. Remove the export from
`eden_task_store_server/__init__.py`'s `__all__`.

**Resolved.** `eden_service_common.load_experiment_config` is now the
single source of truth. `eden_task_store_server.app` re-exports it
from there (kept the re-export for backward compat with any external
caller that imports from the old path; the `__all__` entry stays so
the alias is documented). Web-ui + evaluator CLIs and the
web-ui test conftest now import directly from `eden_service_common`.
222 service-side tests pass (`pytest -q reference/services/{task-store-server,web-ui,evaluator}/tests`).

---

## 19. ✅ Resolved. Empty placeholder packages and test directories — roadmap-tracked
vs. abandoned scaffolding

**Disambiguation pass** (refined after roadmap cross-reference):

| Path | Roadmap status | Triage |
|---|---|---|
| [`reference/packages/eden-blob/`](reference/packages/eden-blob/) | **Phase 13** — `S3/GCS blob backend` ([`docs/roadmap.md:252`](docs/roadmap.md)) | Roadmap-tracked. Add a one-line README pointing at the Phase-13 entry; do NOT delete. |
| [`reference/services/control-plane/`](reference/services/control-plane/) | **Phase 12** — control plane service + lease data model ([`docs/roadmap.md:233`](docs/roadmap.md)) | Roadmap-tracked. Same posture: README pointing at Phase 12. |
| [`tests/integration/.gitkeep`](tests/integration/) | **No roadmap mention.** Phase-0 scaffolding aspiration. | Abandoned. The actual test layout (per-package `reference/*/tests/` + `conformance/scenarios/`) made this dir obsolete; `pyproject.toml`'s `testpaths` never visits it. Safe to delete. |
| [`tests/unit/.gitkeep`](tests/unit/) | Same as above. | Same as above. |

**Why this matters.** Conflating roadmap-tracked placeholders with
abandoned scaffolding leads to wrong cleanup decisions. Deleting
`eden-blob/` would orphan the Phase-13 plan's "where it lands";
keeping `tests/integration/` invites contributors to add a test there
that never runs.

**Severity.** Cosmetic.

**Resolution direction.**

1. `tests/integration/`, `tests/unit/` — delete the directories
   outright. Document the per-package test layout in
   `CONTRIBUTING.md` if it isn't already.
2. `reference/packages/eden-blob/`, `reference/services/control-plane/`
   — replace each `.gitkeep` with a one-paragraph README pointing at
   the roadmap entry that will populate it. The directory then
   advertises its own deferral instead of looking abandoned.

(Original #19 advised "delete or document" without separating the
two cases; the roadmap cross-reference makes the right answer
case-specific.)

**Resolved.** Both halves done:
- `tests/integration/` and `tests/unit/` deleted (along with their
  `.gitkeep` placeholders). Per-package test layout is already
  documented in [`AGENTS.md`](AGENTS.md) "Adding a new service or
  package with its own `tests/` directory" and is implicit in
  [`CONTRIBUTING.md`](CONTRIBUTING.md)'s pointer to the conformance
  suite.
- `reference/packages/eden-blob/.gitkeep` replaced with a README
  naming Phase 13 + the chapter-8 §5 contract this package will
  implement.
- `reference/services/control-plane/.gitkeep` replaced with a README
  naming Phase 12 + that scope.

Both READMEs say explicitly that the directory is intentionally NOT
a workspace member yet, so a contributor who lands here understands
the "deferred, not abandoned" posture.

---

## 20. ✅ Partially resolved (Dockerfile typo); plumbing held for Phase 13. Compose stack ships a `blob-init` service + `eden-blob-data`
volume that no service consumes

**What's there.**
[`reference/compose/compose.yaml:72`](reference/compose/compose.yaml)
defines a `blob-init` service (busybox one-shot) whose only job is
to ensure `eden-blob-data` is initialized; line 375 declares the
named volume; the README at
[`reference/compose/README.md:47`](reference/compose/README.md) lists
it as "implementer-host artifact storage (10d)".

**Reality.** No service mounts `eden-blob-data`. `grep -nE "blob"
reference/compose/compose*.yaml` shows the volume is declared and
the init runs, but no `volumes:` block on any of the EDEN services
references it. Implementer artifacts in the current impl flow through
the shared bare-repo + Gitea remote (chunk 10d follow-up B); the blob
volume is an unused future-implementer placeholder paired with the
empty `eden-blob` package (#19).

**Adjacent typo — investigated and fixed inline.**
[`reference/compose/Dockerfile:62`](reference/compose/Dockerfile)
pre-created `/var/lib/eden/blob` (singular) — but the actual mount
path that `blob-init` advertises (`compose.yaml:84`,
`/var/lib/eden/blobs`, plural) is the path the eventual consumer will
mount at. So the Dockerfile line was **latently wrong**: it
pre-created a sibling of the real mount point with `eden:eden`
ownership, while the real mount point doesn't exist in the image and
would inherit root ownership from the docker volume driver on first
mount — exactly the failure mode the comment block at
`Dockerfile:57–61` says the pre-create exists to prevent.

Currently harmless (no consumer), but it would have caused first-write
failures the moment the first consumer landed — the time when nobody
would think to check the Dockerfile. **Patched inline:** the
Dockerfile now pre-creates `/var/lib/eden/blobs`. Same triage as #21:
fix is one character, the bug is silent until it's not.

**Why the rest still matters.** Same shape as #19: the stack carries
a service + volume + healthcheck dependency for a consumer that
doesn't exist yet. Every `compose up` waits on `blob-init` to exit
successfully (postgres + gitea both
`depends_on: blob-init: condition: service_completed_successfully`).

**Severity.** Latent bug fixed; the unconsumed plumbing itself is
cosmetic / SHOULD-level.

**Resolution direction.** Either (a) drop `blob-init`,
`eden-blob-data`, and the `depends_on: blob-init` lines until a real
consumer lands (then revert the Dockerfile fix above too); or (b)
defer cleanup to Phase 13, when the consumer ships per #19's
roadmap-tracked posture and earns the plumbing.

**Resolution call: option (b).** Holding the plumbing for Phase 13.
The `blob-init` + `eden-blob-data` plumbing pairs with the
`reference/packages/eden-blob/` placeholder (resolved in #19 with a
README pointing at Phase 13). Removing the Compose-side wiring now
would force re-introducing it in Phase 13 — same churn, no shipped
benefit. The Dockerfile typo (the only active bug) was already
patched inline (`/blob` → `/blobs`); the README + Dockerfile are
now internally consistent for whoever lands the Phase-13 consumer.

When Phase 13 ships:
- Wire the actual blob backend to mount `eden-blob-data` at
  `/var/lib/eden/blobs`.
- The `chown eden:eden /var/lib/eden/blobs` line in
  [`reference/compose/Dockerfile`](reference/compose/Dockerfile)
  is already in place.
- `blob-init` can either stay (idempotent) or be replaced by the
  real backend's startup.

---

## 21. ✅ Resolved (during initial audit). `tests/fixtures/experiment/README.md` is stale and has been
patched in this audit

**What was there.** The README claimed "`plan.py`, `implement.py`,
`eval.py`, and the planner workspace are not part of the protocol-layer
fixture; they will be migrated with the reference implementation in
a later phase." But Phase 10d added them as the canonical
subprocess-mode role-script fixtures, consumed by
`compose-smoke-subprocess` and `compose-smoke-subprocess-docker`. The
README also said nothing about `planner_root` / `workspace` being dead
(connects to #13).

**Patched in this audit.** The README now lists each script + the
Dockerfile, names the CI jobs that consume them, points at
`spec/v0/reference-bindings/worker-host-subprocess.md`, and explicitly
acknowledges the dead `planner_root` / `workspace` carryover with a
pointer to #13.

**Severity.** Cosmetic; was actively misleading.

---

## 22. ✅ Resolved. Inconsistent direct-vs-transitive declaration of `eden-git`
across services

**What's there.** The four EDEN services that use git all import
`eden_git` directly:

| Service | imports `eden_git`? | declares `eden-git` in pyproject? |
|---|---|---|
| `orchestrator` | yes | yes |
| `implementer` | yes | yes |
| `evaluator` | yes (`cli.py:9`, `subprocess_mode.py:75`) | **no** |
| `web-ui` | yes (transitively via `eden_dispatch`/`eden_service_common` paths) | **no** |
| `_common` | yes (4 modules) | yes |

Evaluator and web-ui pick up `eden-git` transitively through their
declared dep on `eden-service-common`. It works because Python
resolves any installed module regardless of which `pyproject.toml`
brought it in.

**Why this matters.** Direct deps document intent; transitive deps
are a side effect. If `eden-service-common` ever drops `eden-git`,
the evaluator and web-ui silently break. Same posture as a missing
`peerDependency` declaration in JS land. Inconsistent across services
that have the same actual relationship to `eden-git` is the
clearest tell that the inconsistency is accidental.

**Severity.** Cosmetic; SHOULD-level dependency hygiene.

**Resolution direction.** Add `"eden-git"` to the `dependencies` list
in `reference/services/evaluator/pyproject.toml` and
`reference/services/web-ui/pyproject.toml`.

**Resolved.** Added `eden-git` to the `dependencies` list in both
pyproject.toml files. `uv sync` re-resolves cleanly; 220 service-side
tests still pass.

---

## 23. ✅ Resolved (during initial audit). `.gitignore` phase comments are stale; "Node" comment was
misleading (patched inline)

**What was there.** Two `.gitignore` section comments referenced
phases as if they were future:

- `# Python (lands in Phase 3)` — Phase 3 has been done for ~8 phases.
- `# Node (reference web-ui, lands in Phase 9)` — implies the web-ui
  needs Node, which is wrong (server-side Jinja + vendored HTMX).

**Patched in this audit.** First comment becomes `# Python`. Second
becomes a clarifying note that Node is for `npx markdownlint-cli2` and
ad-hoc tooling, not for the web-ui.

**Severity.** Cosmetic; mildly misleading to new contributors.

---

## What I checked that came up clean

To save the next session re-hunting the same ground, I noted these
and found no drift:

- **Event types.** All 15 spec-registered types
  ([`spec/v0/05-event-protocol.md`](spec/v0/05-event-protocol.md)
  §3.1–§3.3) are emitted by the reference impl.
- **Wire endpoints.** Every endpoint in
  [`spec/v0/07-wire-protocol.md`](spec/v0/07-wire-protocol.md) §§2–6
  has a matching `@app.{post,get}` route in `eden_wire/server.py`.
- **Reserved metric names.**
  [`reference/packages/eden-contracts/src/eden_contracts/metrics.py:21`](reference/packages/eden-contracts/src/eden_contracts/metrics.py)
  enforces all 10 reserved names from
  [`02-data-model.md`](spec/v0/02-data-model.md) §6.2.
- **Timestamp `Z` suffix and SHA length.**
  [`reference/packages/eden-contracts/src/eden_contracts/_common.py`](reference/packages/eden-contracts/src/eden_contracts/_common.py)
  enforces both via `DateTimeStr` (regex + `fromisoformat`) and
  `CommitSha` (40 or 64 hex chars).
- **Integrator commit subject pattern.**
  [`reference/packages/eden-git/src/eden_git/integrator.py:434`](reference/packages/eden-git/src/eden_git/integrator.py)
  writes `f"trial: {trial_id} {slug}\n"` matching
  [`spec/v0/06-integrator.md`](spec/v0/06-integrator.md) §3.5.
- **Stale TODOs.** The only `TODO` markers in the tree are in
  `.github/workflows/ci.yml` lines 165 / 270, both linking the
  tracked GitHub issue eden#38 (branch-protection bump after green
  runs) — intentional per commit `c9576f4`.
- **Predecessor (direvo) carryover.** A diff against
  `~/Documents/direvo/` confirmed only the fixture was ported
  verbatim (#13 covers the carryover keys). The reference impl
  itself is greenfield; no source files match.
- **Compose env-var coverage.** Every `${VAR}` in `compose.yaml`,
  `compose.subprocess.yaml`, and `compose.docker-exec.yaml` is
  written by `setup-experiment.sh`. The two vars setup writes that
  don't appear in compose YAML (`EDEN_EXEC_MODE`,
  `GITEA_REMOTE_PASSWORD`) are consumed by the healthcheck shell
  scripts — not dead.

---

## 24. Scheduled work item — line-by-line MUST/SHOULD audit against
the conformance suite

**Status: first-pass matrix delivered + chapter-04 per-claim pilot.**
[`docs/conformance-coverage.md`](docs/conformance-coverage.md)
generated from [`scripts/conformance-coverage.py`](scripts/conformance-coverage.py).
Headline: 323 MUST/MUST-NOT lines, 73 with at least one citing
scenario, 142 with no citation (~23% line-coverage). The matrix
includes a "How to read the gap list" section that classifies the
142 gaps into three kinds: structurally-coverage-immune chapters
(chapter 00/01/09), citation gaps (chapter 08 storage MUSTs are
asserted via wire chapters but not cited from 08), and
schema-enforced MUSTs (data-shape rules the JSON Schema covers).

**Chapter-04 per-claim pilot landed** (in the same matrix doc, new
section above the auto-generated tables). Headline for chapter 04:
27 MUST/MUST-NOT rows; 18 `(scenario)`, 2 `(consequence)` (chapter
09 §3 black-box-impossible), 2 `(restatement)`, 5 `(uncovered)`.
**Effective coverage 74%** for chapter 04 — much better than the
auto-generator's 23% line-coverage suggests, because the line-coverage
counts MAY rows and doesn't credit `(consequence)`. Two of the five
`(uncovered)` rows are actually exercised by tests in
`test_composite_commits.py` that cite `05-event-protocol.md §2.2`;
intra-chapter ancestor-walk doesn't surface them. Three are real
gaps. Methodology refinements that surfaced during the pilot:

1. **Five tags, not four.** Added `(consequence)` for chapter 09 §3
   black-box-impossible MUSTs (atomicity, unforgeability) where the
   scenario asserts a testable proxy.
2. **List-header lines are NOT independent MUSTs.** "X MUST be:"
   followed by sub-bullets is one structural element, not two.
3. **Cross-chapter coverage is real and structurally hidden.** The
   intra-chapter ancestor-walk misses composite-commit citations.
4. **Multi-MUST lines need finer claim-counting.** Multi-claim rows
   tag-as-net but lose per-claim detail.

**Time-to-tag and chapter projection.** Chapter 04 took ~30 minutes
once test files were in cache. Total for remaining 7 chapters
projects to ~3-4 hours, NOT half-a-day-per-chapter. The estimate held
for the highest-density chapter; smaller chapters are quicker.

**What's left — the per-chapter pass.** Apply the same per-claim
breakdown to chapters 02 / 03 / 05 / 06 / 07 / 08 / 09. The
recommendation in the pilot section: resolve cross-chapter
composite-commit ancestry first (mechanical: either teach the
generator or multi-cite the relevant tests), then proceed in order
of MUST density.

**Why this is its own entry, not a "checked clean" line.** The
audit's category I (spec MUST/SHOULD claims not covered by the
conformance suite) is the highest-signal category but also the most
expensive. The original audit punted on it with "highest-effort path
forward, worth its own pass." That deferral is honest, but the gap
compounds: every spec amendment that lands without a matching
conformance assertion makes the eventual line-by-line audit larger
and the gap-set fuzzier.

**Scope of the work.**

- Walk every `\bMUST\b` / `\bSHOULD\b` token in
  [`spec/v0/01-concepts.md`](spec/v0/01-concepts.md) through
  [`spec/v0/08-storage.md`](spec/v0/08-storage.md) (≈218 claims per
  the original audit prompt's count).
- For each, search [`conformance/scenarios/`](conformance/scenarios/)
  for an assertion that exercises it. The
  [`tools/check_citations.py`](conformance/src/conformance/tools/check_citations.py)
  helper already enforces the inverse direction (every scenario
  cites a real MUST); the work here is the forward direction
  (every MUST is asserted somewhere).
- Per chapter-9 §3, only MUSTs are normatively asserted; SHOULDs
  are interop guidance. The audit should still note SHOULD gaps
  (separate column) for visibility, but classify them differently.
- Output: a coverage matrix (chapter × MUST/SHOULD × asserted-by /
  uncovered) committed to the repo. Each uncovered MUST becomes
  either a new conformance scenario, a new `MANUAL_UI_ISSUES.md`
  entry, or a note explaining why it's untestable through the
  black-box wire surface (chapter 9 §3 already exempts two such
  invariants — atomicity-window and token-unforgeability).

**Estimated effort.** Half-day for a thorough first pass; recurring
~1–2h per spec amendment thereafter to keep the matrix current.

**Why this entry exists vs. just doing the work now.** The audit
session that filed #17–#23 had a fixed budget and chose breadth over
depth. Scheduling this as its own work item is the alternative to
letting the deferral drift. A future session that picks this up
should produce the matrix as its first artifact, not a list of
findings — that way the work is incremental and the next session
after THAT inherits a partial matrix instead of starting from zero.

**Severity.** None on its own — this is a process commitment, not a
bug. The bugs it would surface have unknown severity until the audit
runs.

**Resolution direction.** First-pass matrix delivered at
[`docs/conformance-coverage.md`](docs/conformance-coverage.md);
generator script at [`scripts/conformance-coverage.py`](scripts/conformance-coverage.py).
Re-run the script after spec edits to refresh. The next pass —
per-claim assertion coverage — is the open work.

---

## 25. ✅ Resolved (option 2 — level-based wins). Chapter 00 promises class-based conformance; chapter 09
delivers level-based conformance with a single IUT contract

**What's there.**
[`spec/v0/00-overview.md`](spec/v0/00-overview.md) §2.2 enumerates
**eight conformance classes** — Planner, Implementer, Evaluator,
Integrator, Task store, Event log, Artifact store, Orchestrator —
and tells the reader: *"An implementation MAY conform to one or
more classes. It need not conform to all."* §2.3 says *"An
implementation conforms to a class iff it passes every scenario in
[`09-conformance.md`](spec/v0/09-conformance.md) that targets that
class."*

**What chapter 09 actually defines.**
[`spec/v0/09-conformance.md`](spec/v0/09-conformance.md) does not
have per-class scenarios. §1 defines **three additive levels** —
**v1**, **v1+roles**, **v1+roles+integrator** — and §6 (the IUT
contract) explicitly anchors conformance to the **chapter-7 HTTP
binding** as a whole:

> *"The contract between an IUT and a conformance harness is the
> chapter-7 HTTP binding. Everything else is convenience."*

There is no path to claim "Planner conformance" independently of
the rest of an HTTP server that exposes the chapter-7 endpoints.
A standalone Planner that only implemented the planner-side
operations could not be exercised by the conformance suite, because
the suite drives every IUT through the full chapter-7 binding from
the outside.

**Why this matters.** The class-based reading in chapter 00 is
load-bearing for the project's stated framing — it's what justifies
the claim that someone can "build a conforming planner ... in any
language and interoperate". If a third-party implementer reads
chapter 00, decides to ship a Python planner, and only later opens
chapter 09 to find that conformance is actually whole-IUT, they've
spent their effort on the wrong shape.

The two chapters were written in different phases (chapter 00 in
Phase 1; chapter 09 in Phase 11) and the framing was never
reconciled. Class-vs-level isn't a typo — it's a different theory
of what conformance means.

**Severity.** SHOULD-level for the spec (the inconsistency is
load-bearing for what an implementer can claim), but no impl bug
yet because no third-party impl exists.

**Resolution direction.** Two clean options, both spec-only:

1. **Class-based wins.** Rewrite chapter 09 to define per-class
   conformance: a Planner-only implementation that exposes only
   the planner-relevant chapter-7 operations satisfies the planner
   class. Per-class scenario subsets need to be re-grouped from the
   current v1 / v1+roles / v1+roles+integrator levels. Suite
   harness needs an "IUT advertises which classes" mode.

2. **Level-based wins.** Rewrite chapter 00 §2.2 / §2.3 to say what
   chapter 09 actually delivers: an IUT is the whole chapter-7
   server, conformance is per-level, the eight role-and-store
   names are entities the protocol defines but not units of
   conformance. (This is the cheaper change and matches the
   reference impl's actual posture.)

Either is conformant; option 2 is honest about today, option 1 is
ambitious about tomorrow. The choice is a small design call,
worth making before any third-party implementer reads chapter 00
and acts on its current promise.

**Found while.** Spec consistency audit, walking chapter 00 against
chapter 09 for concept drift.

**Resolution: picked option 2 (level-based wins).** Rewrote
[`spec/v0/00-overview.md`](spec/v0/00-overview.md) §2.2 and §2.3:

- §2.2 retitled "The unit of conformance" and now says the IUT is
  the whole chapter-7 HTTP server. The role and store names are
  identified as parts of the protocol introduced by chapter 01,
  *not* independent conformance units. The "v0 has one binding,
  so it has one IUT shape" framing names why this is the right
  posture today and points at where finer-grained units would
  show up if a future binding were added.
- §2.3 retitled "Conformance levels" and lists v1 / v1+roles /
  v1+roles+integrator with the level-qualification rule from
  chapter 09 §1. Defers normative detail to chapter 09 with an
  explicit "where this overview and chapter 09 disagree, chapter
  09 wins" tiebreaker.

Why option 2: option 1 (rewrite chapter 09 to deliver per-class
conformance) would have been ~weeks of scenario-grouping +
harness work for an architectural shape no IUT has asked for. The
reference impl is whole-IUT today and Phase 12 / 13 don't argue
for splitting it. Option 2 honestly describes what we ship and
keeps the option of growing finer units in v1 if/when a transport
split arrives.

Verified: `python3 scripts/spec-xref-check.py` reports all 334
§-references resolve (was 333; the rewrite added one §6 link).
markdownlint-cli2 clean.

- Should the implementer page show `EDEN_BASE_COMMIT_SHA` as the
  default-implicit parent for first-round trials?
- Is there a pattern for "infra services that should never quiesce-exit"
  vs "experiment runners that should"? Worth pulling apart?
- Should completed phase plans under `docs/plans/eden-phase-*.md`
  rotate into `docs/archive/` once the phase ships? Current state is
  every chunk plan back to Phase 7 lives alongside active work.
  Not strictly stale — they're history — but they grow without bound.
