# eden-web-ui

Reference Web UI service for the EDEN protocol. Phase 9 chunks 1
(UI shell + planner module) and 9c (implementer module) ship — a
human can play either role end-to-end through a browser. The
implementer module is gated on the optional `--repo-path` flag; if
omitted, the UI runs as a planner-only deployment.

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

## What this chunk does **not** ship

- Evaluator role module (9d).
- Observability views, admin-reclaim button, orphaned-trial /
  orphaned-`work/*`-ref garbage-collection view (9e).
- Multi-experiment switcher (Phase 12).
- Per-user authentication (Milestone 3).
- Compose / Dockerization (Phase 10).
