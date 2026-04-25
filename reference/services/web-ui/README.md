# eden-web-ui

Reference Web UI service for the EDEN protocol. Phase 9 chunks 1
(UI shell + planner module), 9c (implementer module), 9d
(evaluator module), and 9e (admin / observability) ship — a human
can play any of the three worker roles end-to-end through a
browser, observe the experiment's task / trial / event state from
`/admin/*`, and operator-reclaim a stranded claim. The implementer
module and the work-ref GC sub-page of `/admin/*` are gated on the
optional `--repo-path` flag; if omitted, the UI runs as a
planner + evaluator + (read-only) admin deployment.

The UI service is a **backend-for-frontend (BFF)**: it holds the
`--shared-token` (the chapter 07 §12 reference bearer the
orchestrator and worker hosts already use), runs `eden_wire.StoreClient`
in-process to talk to the task-store-server, and exposes only
server-rendered HTML to the browser. The browser never sees the
shared token; it gets a signed session cookie.

Rendering uses Jinja2 templates with [HTMX](https://htmx.org/) as a
progressive-enhancement layer. Every mutating route works without
JS (plain form-POST + 303-redirect / re-render); HTMX-aware routes
additionally return a fragment when the browser sends
`HX-Request: true`. The chunk-1 example is "add another proposal
row" — HTMX appends one new row inline; without JS the same
button does a full-page re-render. HTMX is vendored at
`src/eden_web_ui/static/htmx-1.9.12.min.js`
(SHA-256 `449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452`)
so the UI works offline and in CI without external network.

## Run locally

```bash
python3 -m eden_task_store_server \
    --db-path /tmp/eden.sqlite \
    --experiment-id exp-1 \
    --experiment-config tests/fixtures/experiment/.eden/config.yaml \
    --port 0 \
    --shared-token devtoken \
    &

# (Read EDEN_TASK_STORE_LISTENING from stdout to learn the port.)

python3 -m eden_web_ui \
    --task-store-url http://127.0.0.1:<port> \
    --experiment-id exp-1 \
    --experiment-config tests/fixtures/experiment/.eden/config.yaml \
    --shared-token devtoken \
    --session-secret "$(openssl rand -hex 32)" \
    --artifacts-dir /tmp/eden-artifacts \
    --port 0
```

The web-ui announces `EDEN_WEB_UI_LISTENING host=... port=...` on stdout
on bind so harnesses (and the test suite) can discover the ephemeral
port without scraping logs.

## Auth model

- The session cookie holds `{worker_id, csrf}` and is signed with
  `--session-secret` via `itsdangerous`.
- Cookie attributes: `HttpOnly`, `SameSite=Lax`, `Path=/`. `Secure`
  is opt-in via `--secure-cookies` (use behind TLS).
- Every mutating route validates a `csrf_token` form field in
  constant time. The cookie's `SameSite=Lax` is **not** treated as
  sufficient on its own.
- The shared bearer never reaches the browser, the rendered HTML,
  any session cookie, or any structured log line.

## Stranded-claim recovery

Every UI claim sets `expires_at = now + --claim-ttl-seconds`
(default 1 hour). The orchestrator service runs
`eden_dispatch.sweep_expired_claims` once per iteration so claims
abandoned by closing the tab are reclaimed automatically — no
operator action required.

## Planner submit flow

The planner module pins three phases:

1. **Phase 1 — drafting.** For every proposal: write rationale
   markdown to `<artifacts-dir>/<proposal_id>.md` (atomically, via
   tmp-and-rename), build a `file://` URI, then call
   `store.create_proposal(state="drafting")`. Drafting proposals
   are invisible to the orchestrator's dispatch path.
2. **Phase 2 — ready.** Loop over the just-created proposals and
   call `store.mark_proposal_ready` for each.
3. **Phase 3 — submit.** `store.submit(...)` with retry-before-orphan
   on transport-shaped failures (3 attempts, exponential backoff,
   leveraging chapter 07 §2.4 / §8.1 idempotent resubmit). On a
   definitive divergent response or after the retries are
   exhausted, the orphaned-proposals error page lists the
   `ready`-but-unreferenced proposal IDs for operator recovery.

The narrowest unsafe window is between Phase 2 and Phase 3:
proposals are `ready` but not yet referenced by a submitted plan
task. We accept this for the reference impl; it applies equally
to the existing scripted planner host. A spec-level fix (atomic
ready-and-submit) is out of scope for chunk 1.

## Implementer module (chunk 9c)

The implementer module is registered when `--repo-path <path>` is
set on the CLI; it points at the same bare git repo the
`eden_implementer_host` service writes `work/*` refs into. The
top-level navigation hides the "implementer" link otherwise and
the routes return 404.

Trust model and assumption: the **user does git work in their own
checkout out-of-band**, then pushes their tip commit to the bare
repo (any branch — the UI creates the canonical `work/<slug>-<trial_id>`
ref pointing at the commit when the form is submitted). The UI
never accepts credentials, never runs `git push`, and never
proxies a remote. Multi-machine deployments where the user's
checkout and the bare repo are not on the same filesystem are out
of scope until Phase 10's Compose stack and a later remote-repo
story.

**Spec-to-code map for `POST /implementer/{task_id}/submit`:**

1. **Validate** the form: `status ∈ {success, error}`, `commit_sha`
   is 40 lowercase hex when `status=success`.
2. **§3.3 reachability check** (status=success only):
   - `repo.commit_exists(commit_sha)` — rejects refs that were not
     pushed to the bare repo with a clear "did you push it?" error.
   - For every parent in `proposal.parent_commits`,
     `repo.is_ancestor(parent, commit_sha)` — rejects commits whose
     history does not descend from the proposal's declared parents
     (per `spec/v0/03-roles.md` §3.3).
3. **Pre-Phase-1 ref-collision guard** (status=success only):
   `repo.ref_exists("refs/heads/work/<slug>-<trial_id>")` short-
   circuits with a form re-render and no store mutation. Branch
   uniqueness is required by §3.3 ("worker branch MUST be unique
   to this trial"); the guard turns a vanishing edge case into a
   clean form error.
4. **Phase 1 — `store.create_trial`** with `status="starting"`,
   no `commit_sha`. The orchestrator's `accept` handler is what
   writes `commit_sha` onto the trial later, per
   `eden_storage._base._accept_implement`. This ordering honors
   `03-roles.md` §3.2 step 1 ("trial persisted before observable
   repo writes").
5. **Phase 2 — `repo.create_ref`** (status=success only): writes
   `refs/heads/work/<slug>-<trial_id>` pointing at the user's
   `commit_sha`. On status=error this step is skipped (no work
   branch exists).
6. **Phase 3 — `store.submit`** with retry-before-orphan plus a
   committed-state read-back. The retry policy is identical to the
   planner's: 3 attempts, backoff `(0.05, 0.2, 0.5)`, definitive
   store-domain errors short-circuit. After retries are exhausted
   on transport-shape failures, the route does
   `read_task` + `read_submission` + `submissions_equivalent` to
   distinguish "the prior attempt actually committed and the
   response was lost" (renders the success page) from "no submit
   committed" (renders the orphan page).

**Per-error recovery summary** (renders prose on the orphan page):

- *Phase 2 failure* (`create_ref` raises): trial sits in
  `starting`, no `work/*` ref. Recovery: claim TTL → sweeper →
  `reclaim` composite-commits the orphaned trial to `error`.
- *Phase 3 retry exhaustion, server committed*: success page (the
  read-back found our equivalent submission already on file).
- *Phase 3 retry exhaustion, server never committed*: orphan
  page; "auto-recovers via reclaim" prose.
- *Phase 3 `WrongToken` / `IllegalTransition`*: the prior reclaim
  that invalidated our token already errored our `starting`
  trial; orphan page "auto-recovers via reclaim".
- *Phase 3 `ConflictingResubmission`*: a different submission won
  the race. Orphan page surfaces `trial_id` / `commit_sha` for
  operator triage.

**Artifact-rendering trust boundary.** The draft form may inline a
proposal's rationale markdown if (a) `proposal.artifacts_uri`
starts with `file://`, (b) the resolved path is contained within
the UI service's `--artifacts-dir`, (c) the file is not larger
than 1 MiB. Any other shape renders as a link only. This guards
against a malicious / careless `artifacts_uri` pointing the UI at
arbitrary local files.

**`trial_id` is server-only.** Generated at claim time and stored
alongside the claim token in the in-process `_CLAIMS` dict; never
appears in the request surface (no hidden form field, no URL
parameter). A forged `trial_id` form value is ignored.

## Evaluator module (chunk 9d)

The evaluator module mounts unconditionally — the evaluator never
touches a repo through the UI (per `spec/v0/03-roles.md` §4.3 it
reads the trial at `commit_sha` out-of-band and submits metrics
back). The top-level navigation always shows the "evaluator" link.

The draft page surfaces:

- **Trial fields** (read-only): trial_id, branch, commit_sha,
  parent_commits, status, started_at. `commit_sha` is what the
  operator checks out from the bare repo to evaluate.
- **Trial-side optional fields set by the implementer per §3.2
  step 3**: `trial.description` (rendered as a read-only `<pre>`
  block, escaped via Jinja2 autoescape) and `trial.artifacts_uri`
  (rendered with the chunk-9c scheme allowlist + the same trust-
  boundary helper as the proposal rationale).
- **Proposal context** (slug, priority, artifacts_uri, rationale).
- **Metrics form**: one input per metric in
  `experiment_config.metrics_schema`, typed by the declared
  `MetricType` (`<input type="number" step="1">` for `integer`,
  `step="any"` for `real`, plain text otherwise).
- **Submission status**: radio for `success` / `error` /
  `eval_error` per §4.4.
- **Optional `artifacts_uri`** text input (the operator's URI for
  their eval logs / outputs, uploaded out-of-band).

**Spec-to-code map for `POST /evaluator/{task_id}/submit`:**

1. **Validate** the form. `parse_evaluate_form` returns `(None,
   errors)` on:
   - `status` outside `{"success", "error", "eval_error"}`.
   - Per-metric type drift (integer accepts `1.0` per
     `02-data-model.md` §1.3 but rejects `1.5`; real rejects
     `nan`/`±inf`; text must be non-empty after strip).
   - Unknown metric key (only reachable from a hand-crafted POST;
     the template emits inputs only for declared metrics).
   - `status="success"` with zero metric values (UI-side
     guardrail; the wire allows empty).
2. **`store.submit`** with retry-before-orphan + read-back. The
   exception classification differs from chunk 9c by design:
   - `WrongToken` → orphan `recovery_kind="auto"` (definitive).
   - `ConflictingResubmission` → orphan
     `recovery_kind="conflict"` (definitive).
   - `InvalidPrecondition` → re-render the form with the wire-
     error banner. Fixable; not an orphan.
   - `IllegalTransition` → fall through to read-back. The store
     raises this when `task.state ∉ {"claimed", "submitted"}`,
     which can mean state==pending (we lost), state==completed/
     failed (we won — orchestrator already terminalized), or
     state==submitted by another worker (conflict). Read-back
     resolves it.
   - Other transport-shaped exceptions → retry with backoff
     `(0.05, 0.2, 0.5)`; on exhaustion, jump to read-back.
3. **Read-back**. `read_task` + `read_submission` +
   `submissions_equivalent`. For `EvaluateSubmission`,
   equivalence is `status + trial_id + metrics` per chapter 04
   §4.2 — `artifacts_uri` is **not** part of equivalence (the
   first submission's value wins).

**`trial_id` is server-only.** Read from
`task.payload.trial_id` at claim time and stored alongside the
claim token in the in-process `_CLAIMS` dict; never appears in
the request surface (no hidden form field, no URL parameter). A
forged `trial_id` form value is ignored.

**No git work in the UI.** The evaluator reads the repository
out-of-band (`git clone …` / `git fetch && git checkout
<commit_sha>`); the UI surfaces `trial.commit_sha` and
`trial.branch` as the load-bearing fields and never proxies git
commands.

**Trust-boundary helper.** Chunk 9d generalizes the chunk-9c
helper into `_read_inline_artifact(uri, artifacts_dir)` in
[`routes/_helpers.py`](src/eden_web_ui/routes/_helpers.py).
`read_proposal_rationale` is now a thin wrapper, and a sibling
`read_trial_artifact` covers the trial-side `artifacts_uri`
surface. The envelope is identical: `file://` only, contained in
`--artifacts-dir`, ≤ 1 MiB.

## What chunks 1 + 9c + 9d do **not** ship

- Observability views, admin-reclaim button, orphaned-trial /
  orphaned-`work/*`-ref garbage-collection view (9e).
- Multi-experiment switcher (Phase 12).
- Per-user authentication (Milestone 3).
- Compose / Dockerization (Phase 10).
