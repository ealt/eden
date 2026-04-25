# Phase 9 chunk 9e — Web UI observability views + admin actions

## Goal

Close out Phase 9 by adding the cross-role **observability** surface
and the **operator-side actions** to the reference Web UI. The
web-ui process is pinned to one `--experiment-id` (Phase 12 brings
the multi-experiment switcher), so "observability" here means
"see all task kinds, trials, and events for *this* experiment in one
place" — not cross-experiment.
After 9a–9d a human can play any single role for one trial; chunk 9e
makes it possible for the same human to *see* what the system is
doing and *unstick* it when something goes wrong:

- A read-only set of tables under `/admin/*` that snapshot the live
  state (tasks by kind/state/claim-age, trials by status, recent
  event log, per-trial detail page).
- An operator-driven **task reclaim** action exposed in the UI,
  routed through `Store.reclaim(task_id, "operator")` per
  `spec/v0/04-task-protocol.md` §5.1. Covers both the
  abandoned-tab `claimed` case and the stuck `submitted` case (which
  the automatic sweeper deliberately does not touch — §5.1 forbids
  automatic reclaim against `submitted`).
- A `work/*` ref garbage-collection page that surfaces refs whose
  owning trial is terminal (`success` *and* integrated, or
  `error`/`eval_error`) so the operator can delete them and reclaim
  disk + namespace. The integrator's §1.3 explicitly permits this;
  chunk 9e is the first reference-stack place where it actually
  happens.

This is the "Exit" criterion in `docs/roadmap.md` for Phase 9: a
human can fully play any one role *and* admin-reclaim works
end-to-end. Chunk 9e clears the second half.

## Non-goals

- **Real auth / RBAC.** Every signed-in user is implicitly an
  operator in chunk 9e; the navigation surfaces `/admin/*` to all
  sessions. RBAC + per-user scopes are Milestone 3 / Phase 12+. We
  *do* gate `/admin/*` behind the existing signed-in cookie (the
  same gate the planner / implementer / evaluator routes use), so
  unauthenticated requests redirect to `/signin`.
- **Live updates / push.** All views render the snapshot at request
  time. No WebSocket, no SSE, no HTMX polling. A "refresh" link or
  the browser's reload button is the entire freshness mechanism.
  Phase 8 long-poll subscribe exists on the wire surface but is not
  bound into the UI; doing so is a Phase 12+ concern alongside the
  control plane.
- **Cross-experiment views.** The web-ui process is already pinned
  to one `--experiment-id`. `/admin/*` shows that experiment's tasks,
  trials, events. Cross-experiment dashboards land with the
  multi-experiment switcher in Phase 12.
- **Pagination beyond a fixed cap.** The reference-stack experiments
  are small (the fixture has ≤ 6 trials over the full run). Lists
  render up to a configurable cap (default 200) with a clear
  "showing N of M; refine the filter" footer; we do not build true
  pagination. Phase 10 / Phase 12 might.
- **Editing trials, proposals, or events.** The admin surface is
  read-only with two narrow exceptions: (a) `reclaim` on a non-
  terminal task, and (b) deleting a `work/*` ref. No "edit
  description", no "fix metrics", no "rewind state". The store's
  terminal-immutability invariant (`spec/v0/08-storage.md` §3.4)
  forbids it for terminal objects, and chunk 9e does not relax it
  for non-terminal ones either — once a thing is in the store,
  the only state changes the UI can drive go through the existing
  state-machine entry points.
- **Bulk reclaim / bulk ref-delete.** Each action is a single object
  per POST. Bulk operations risk the kind of footguns this chunk is
  explicitly trying to *give* the operator — e.g., reclaiming 40
  submitted tasks at once would replay 40 partial executions.
  Single-object actions keep the blast radius tight; if a use case
  warrants bulk later, it's a Phase 10+ addition.
- **Garbage-collecting `trial/*` refs.** `trial/*` is the canonical
  lineage namespace per `spec/v0/06-integrator.md` §1.1: those refs
  are normative and MUST NOT be deleted by the UI. The work-ref GC
  page deliberately filters them out and has no path that could
  match them.
- **Triggering a fresh `evaluate` task on `eval_error`.** The spec
  (§4.4) says a new `evaluate` task MAY be created for the same
  trial after `eval_error`; the orchestrator owns that decision.
  The admin surface does not have a "retry evaluate" button.

## Backwards-compatibility policy

Chunk 9e adds:

- A new routes module (`routes/admin.py`) and a new template family
  (`admin_*.html`).
- One new consumer of an **already-shipped** primitive,
  `eden_git.GitRepo.delete_ref(refname, *, expected_old_sha=None)`
  (lives at `reference/packages/eden-git/src/eden_git/repo.py:511`).
  No new code in `eden-git`; see §B.
- The `--repo-path` CLI flag, previously **optional and only used
  by the implementer module**, is now **also consumed by the
  work-ref GC page** when present. When the flag is omitted the
  GC sub-page renders an explanatory "work-ref GC requires
  --repo-path" placeholder — the rest of the admin surface still
  works. No behavior change for deployments that already pass
  `--repo-path`; no new required flags.

It does **not** change:

- The existing planner / implementer / evaluator routes.
- The wire binding (`eden-wire`) or the storage backend
  (`eden-storage`).
- The session / CSRF / cookie machinery.
- The `Store` Protocol surface (we use `list_tasks`, `list_trials`,
  `replay`, `reclaim` — all already exposed).

## Tech-stack decision

Reuse everything the prior chunks established:

- HTMX 1.9.12 vendored under `static/` for progressive enhancement.
  Chunk 9e introduces zero new HTMX behavior; every action is a
  plain form-POST + 303 redirect, every list renders server-side.
- `itsdangerous`-signed session cookies (`HttpOnly`, `SameSite=Lax`,
  `Path=/`; opt-in `Secure` via `--secure-cookies`).
- Per-session CSRF token validated in constant time on every
  mutating route.
- Closed wire-error vocabulary as the source of error banner text:
  `eden://error/wrong-token`, `eden://error/illegal-transition`,
  `eden://error/conflicting-resubmission`,
  `eden://error/invalid-precondition`. Admin reclaim's only
  legitimate user-facing error from the store is
  `IllegalTransition` (e.g., reclaim against terminal).

No new dependencies. No new vendored assets.

## §A New routes module

`reference/services/web-ui/src/eden_web_ui/routes/admin.py`,
prefix `/admin`. Routes:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/` | Landing page: counts by category + recent events |
| `GET` | `/admin/tasks/` | Task table; querystring filters `kind`, `state` |
| `GET` | `/admin/tasks/{task_id}/` | Task detail (claim, payload, related events) |
| `POST` | `/admin/tasks/{task_id}/reclaim` | Operator reclaim → 303 to detail |
| `GET` | `/admin/trials/` | Trial table; querystring filter `status` |
| `GET` | `/admin/trials/{trial_id}/` | Trial detail (events for this trial) |
| `GET` | `/admin/events/` | Recent event log (capped) |
| `GET` | `/admin/work-refs/` | `work/*` refs grouped by GC-eligibility |
| `POST` | `/admin/work-refs/delete` | Delete one ref → 303 to listing |

Module-level state: **none.** Unlike the role-module routes,
`/admin/*` is stateless — no `_CLAIMS` dict, no per-session state.
Every request reads the store fresh.

The router is included **unconditionally** in `make_app` (no repo
gate; the work-refs sub-page handles the no-repo case in-template).
Navigation in `base.html` gains a permanent "admin" link.

### §A.1 Auth gate

Every `/admin/*` route — `GET` and `POST` alike — calls
`get_session(request)` **first**, before any CSRF check. A missing
session returns `303 → /signin`. This matches the existing
planner / implementer / evaluator route pattern (see e.g.
`reference/services/web-ui/src/eden_web_ui/routes/planner.py:94-96`
where the unauthenticated POST also redirects to /signin), and keeps
the admin surface from leaking task IDs / trial IDs / payload
contents to an unauthenticated visitor.

Order in every mutating handler is therefore:

1. `session = get_session(request)`; if `None` → `303 /signin`.
2. `if not csrf_ok(session, csrf_token)` → 403 (or HTMX-aware
   equivalent via `_csrf_failure_response`).
3. The action proper.

The redirect destination on the `/signin` redirect is `/signin`
itself (not the original URL); the existing modules do not preserve
the deep-link target either, and we keep parity rather than
introducing a different convention.

### §A.2 Listing snapshot semantics

Every list view calls `store.list_tasks(...)` / `list_trials(...)` /
`replay()` once at the top of the handler and renders that snapshot.
We deliberately do **not** correlate against the live event log
between the list call and the render — the snapshot is a point-in-
time read. Two consequences:

- A task that gets reclaimed by the sweeper between the list and
  the render is shown as "claimed" by the snapshot. The operator
  refreshes; it shows "pending". This is fine and matches how the
  planner / evaluator list views behave today.
- The numeric counts on `/admin/` come from the same single
  `list_tasks(...)` snapshot, not from `len(replay())` arithmetic,
  so the counts and the table view are internally consistent within
  one render.

For the `/admin/events/` view we take the **last** N events from
`store.replay()` (default N=200, configurable per-request via
`?limit=`). Render in **reverse chronological order** (newest
first) — the operator nearly always wants "what just happened",
not "what happened first".

### §A.3 Task table filters

Querystring params on `/admin/tasks/`:

- `kind` ∈ {`plan`, `implement`, `evaluate`, `*`}; default `*`.
- `state` ∈ {`pending`, `claimed`, `submitted`, `completed`,
  `failed`, `*`}; default `*`.

`*` (or absent) means "no filter". The handler maps the form's
`*` sentinel to `kind=None` / `state=None` for the
`store.list_tasks(...)` call. Any other value is passed verbatim
(the contracts package's `Literal` types validate it implicitly via
the store's own checks; an unknown value → empty list, which the
template renders as "no tasks match these filters").

Task rows surface: task_id (links to detail page), kind, state,
worker_id (when claimed), claim age (now − claimed_at, computed
server-side), expires_at (raw timestamp), updated_at. Claimed
tasks whose claim is past `expires_at` get a `⚠ claim expired`
badge so the operator can see at a glance which ones the next
sweeper iteration will reclaim.

### §A.4 Task reclaim action

`POST /admin/tasks/{task_id}/reclaim` issues
`store.reclaim(task_id, "operator")`. Per `04-task-protocol.md`
§5.1 this is permitted from `claimed` and `submitted` states.
Outcome handling:

| Store outcome | UI outcome |
|---|---|
| Returns successfully | 303 → `/admin/tasks/{task_id}/` with `?reclaimed=1` |
| `IllegalTransition` (terminal task or already-pending) | 303 → `/admin/tasks/{task_id}/` with `?error=illegal-transition` |
| Transport-shaped (`Exception` from `StoreClient`) | 303 → `/admin/tasks/{task_id}/` with `?error=transport` |

The detail page reads its querystring and shows a green / red
banner accordingly. The reason for routing the success and error
cases through a 303 + querystring (rather than re-rendering the
detail HTML inline from the POST handler) is that the detail page
needs to do its own fresh `read_task` to show post-reclaim state;
the redirect is the cheapest way to force that. The
`?reclaimed=1` / `?error=…` query parameters are validated against
a closed allowlist before being interpolated into the banner — no
arbitrary text echo. Symbols, not text:

```python
_RECLAIM_OUTCOMES = {
    "ok":                 ("ok",    "task reclaimed"),
    "illegal-transition": ("error", "this task cannot be reclaimed (terminal or not claimed)"),
    "transport":          ("error", "transport failure; refresh and try again if the task did not move to pending"),
}
```

CSRF: the reclaim form on the detail page POSTs the per-session
CSRF token in a hidden input; the route validates with the same
constant-time check used elsewhere.

**`reclaim` against `submitted` is operationally significant.**
Per §5.1 this is the only path that can move a `submitted` task
back to `pending`, and the use case is "the orchestrator's response
got lost; the worker thinks it succeeded but the orchestrator never
finalized". The detail page surfaces a *separate* warning above the
button when `task.state == "submitted"`: "this task is in submitted
state; reclaiming will replay the work and may produce a different
trial". The button text changes from "reclaim" to "force-reclaim
(replays work)". The wire call is identical — the warning is a
pure-UX guardrail.

### §A.5 Trial table + detail

`GET /admin/trials/` filters by `status` ∈ {`starting`,
`success`, `error`, `eval_error`, `*`} via a querystring. Rows
surface: trial_id (links to detail), proposal_id (links to the
relevant work-refs entry when present), status, branch,
commit_sha (truncated to 12 chars + tooltip on full),
trial_commit_sha (when integrated), started_at, completed_at.

A trial in `starting` whose owning implement task is terminal
(`failed` / `completed`) **with no other live claim** is flagged
with an `⚠ orphaned starting trial` badge. That can only legitimately
happen if a prior operator-reclaim hit a non-implement code path
that didn't drive the §5.2 starting-trial-→-error transition (the
`reclaim` path itself is fine — `_base.py:790` already does the
atomic transition for implement tasks). The badge is informational;
no GC button. If the operator wants to force the trial terminal,
the path is "reclaim the implement task" or "wait for the
orchestrator to advance it" — not a direct trial-edit.

`GET /admin/trials/{trial_id}/`:

- Trial fields (read-only).
- The proposal that produced it (`store.read_proposal(proposal_id)`).
- A filtered slice of `store.replay()` correlating to this trial.
  Correlation algorithm (concrete; the `Event` envelope has
  `data: dict[str, Any]`, not `payload` — see
  `reference/packages/eden-contracts/src/eden_contracts/event.py:36`):

  1. Build the trial-id match set: every event whose
     `event.data.get("trial_id") == trial_id`. This catches
     `trial.started`, `trial.errored`, `trial.eval_errored`,
     `trial.succeeded`, `trial.integrated`, plus any task event
     whose `data` already carries `trial_id`.
  2. Build the task-id match set for **the implement task that
     produced this trial**: query
     `store.list_tasks(kind="implement")`, find the one whose
     `payload.proposal_id == trial.proposal_id`. If found,
     include events whose `event.data.get("task_id")` equals
     that implement task's id (this surfaces `task.created`,
     `task.claimed`, `task.submitted`, `task.completed`,
     `task.failed`, `task.reclaimed` for the implement leg).
  3. Build the task-id match set for **the evaluate task(s) for
     this trial** the same way: `store.list_tasks(kind="evaluate")`
     filtered by `payload.trial_id == trial_id`. There may be
     more than one (a fresh evaluate task can be created after
     `eval_error`); include task events for all of them.
  4. Union the three sets, sort by **replay-index position in the
     full `store.replay()` list** (computed before slicing — see
     §A.6's natural-index-before-slice discipline). Replay order
     is the only authoritative monotonic ordering; `event.event_id`
     comes from a pluggable factory
     (`reference/packages/eden-storage/src/eden_storage/_base.py`'s
     `event_id_factory`) and is **not** a reliable ordering
     contract. Cap at 50, render with newest first. "showing N of M"
     footer when `M > 50`.

  This is implementable without any new fold helper in
  `eden-dispatch`; it composes already-public Store methods.
  An optional `_helpers.events_for_trial(store, trial_id)` helper
  may be extracted from the route handler if the test surface
  benefits, but it is not load-bearing.

- Inline trial.description (autoescape) when set, identical to the
  evaluator-page rendering.
- `trial.artifacts_uri`: same scheme allowlist + `_read_inline_artifact`
  envelope chunk 9d already applies.

### §A.6 Event log view

`GET /admin/events/`:

- `events_full = store.replay()`; capture `total = len(events_full)`
  *before* slicing.
- Apply optional `?type=` exact-match filter against `events_full`,
  preserving each event's natural index in the full log
  (`for idx, ev in enumerate(events_full, start=1): …`). The natural
  index is what makes the rendered position stable across
  filter / limit / reverse operations — we compute it before any
  of those.
- Take the last N entries of the filtered list (default N=200,
  configurable via `?limit=` capped at 1000). Reverse for display
  so newest renders first.
- Each row carries the natural index recorded in the previous
  step (1-based), `event.type`, `event.event_id`, `event.occurred_at`
  (the spec-canonical timestamp field on the `Event` envelope —
  `reference/packages/eden-contracts/src/eden_contracts/event.py:34`),
  and `event.data` rendered as a `<details>` toggle.
- Footer: when the filtered list's length exceeds N, show
  "showing 200 of M filtered events (full log: total entries);
  pass `?limit=N` to see more."

The cap exists so a runaway experiment doesn't exhaust the
operator's browser. Two distinct totals matter:

- The full-log size (`total`) — observability into "how big is
  this experiment's history?".
- The filtered-list size (after `?type=`) — observability into
  "how many of these events match my filter?".

Both render. The natural index is stable per event regardless of
filter, so an operator copy-pasting "event #4711" can reproduce
the position deterministically across reloads.

### §A.7 Work-ref GC

`GET /admin/work-refs/`:

- When `app.state.repo is None`: render an explanatory placeholder
  ("work-ref GC requires `--repo-path`; restart the web-ui with
  the flag if you want to manage refs from this UI").
- When `repo` is set: build a `branch → trial` map by walking
  `store.list_trials()` and indexing each trial that has a non-null
  `trial.branch` (the `Trial` model exposes `branch` as a stored
  field — see
  `reference/packages/eden-contracts/src/eden_contracts/trial.py:33`).
  Then call `repo.list_refs("refs/heads/work/*")` and classify each
  `(refname, current_sha)` pair by **exact branch equality**, not
  by parsing the ref name:

  - Compute `branch_name = refname.removeprefix("refs/heads/")`.
  - Look up `trial = branch_index.get(branch_name)`. Three
    outcomes:
    - **Trial found** and `trial.status` is terminal-and-handled
      (`error`, `eval_error`, or `success` with `trial_commit_sha`
      set) **and** `current_sha == trial.commit_sha` →
      **GC-eligible**. Show a delete button.
    - **Trial found** but is `starting`, or is `success` without
      `trial_commit_sha` (integrator hasn't promoted yet), or
      `current_sha != trial.commit_sha` → **Not eligible**. List
      the reason; no delete button. The SHA-mismatch case is the
      dangerous one — it means a third party rewrote the work
      branch and the operator should investigate before deleting.
    - **No trial owns this branch** (`branch_index.get(branch_name)`
      is `None`) → **Orphan**. Most likely a leftover from a
      Pre-Phase-1 ref-collision retry that succeeded on a
      different name, or a manual `git update-ref` outside the
      system. Show a delete button + "no trial owns this ref"
      warning.

  This exact-branch-equality discipline is the trust boundary:
  it does not depend on a ref-name parser (ruling out edge cases
  where `trial_id` itself contains `-`, e.g. `trial-<hex>` —
  splitting on the rightmost `-` would have parsed
  `work/foo-trial-abc123` as `(slug=foo-trial, trial_id=abc123)`,
  which is wrong). A SHA collision between two trials cannot
  cause us to associate a ref with the wrong trial; a manual ref
  rewrite either lands on a known branch (caught by the SHA
  mismatch) or on an unknown branch (caught by the orphan case).
  The SHA check remains as the eligibility predicate for the
  found-trial-and-terminal case.

`POST /admin/work-refs/delete`:

- Form body has `ref_name` (e.g. `refs/heads/work/slug-T-7`) +
  CSRF.
- Validate `ref_name` matches `^refs/heads/work/[A-Za-z0-9_\-/.]+$`
  and that the path's `work/<slug>-<trial_id>` subpath does not
  contain `..` or path-separator escapes. **Critical**: any value
  not matching the regex (in particular anything starting with
  `refs/heads/trial/`, `refs/tags/*`, `HEAD`, or relative refs)
  → 303 → `/admin/work-refs/?error=invalid-ref-name`. The regex
  is the trust boundary; we do not concatenate user input into a
  git plumbing command without it.
- Re-evaluate eligibility server-side from a fresh `list_refs` +
  `list_trials` snapshot. If the ref is no longer eligible (the
  trial moved back to `starting` somehow, or the ref no longer
  exists), redirect with `?error=not-eligible` / `?error=not-found`.
  The eligibility check at the **POST** site is what makes the
  GET-time eligibility filter a UX hint rather than a security
  boundary.
- Call `repo.delete_ref(ref_name, expected_old_sha=<the SHA we just
  read>)` (the existing primitive's kwarg is `expected_old_sha`,
  not `expected_sha`). The `expected_old_sha` argument turns the
  deletion into a CAS via `git update-ref -d <ref> <oldvalue>` —
  if a third party rewrote the ref between the GET and the POST,
  the deletion fails with `?error=ref-changed` and the operator
  has to re-confirm.

### §A.8 Landing page

`GET /admin/`:

- Counts: total tasks by `(kind × state)` cross-tab, total trials
  by `status`, count of events, count of `work/*` refs (when
  `--repo-path` set; "—" otherwise).
- "Last 10 events" reverse-chronological table.
- Quick links to each sub-page.
- A small "stranded claims" callout at the top when any claimed
  task's `expires_at` is past `now`: "N claims have expired and
  will be reclaimed by the next sweeper iteration." Pure
  observability — no action button (the sweeper handles it; the
  callout is just a "you don't need to intervene" hint).

## §B `eden-git` additions

**None.** The required primitive already exists at
`reference/packages/eden-git/src/eden_git/repo.py:511`:

```python
def delete_ref(self, refname: str, *, expected_old_sha: str | None = None) -> None:
    """Delete a ref, optionally guarded by ``expected_old_sha``."""
    args = ["update-ref", "-d", refname]
    if expected_old_sha is not None:
        args.append(expected_old_sha)
    self._run(args)
```

`git update-ref -d <ref> <oldvalue>` deletes the ref *only if* its
current value equals `<oldvalue>`. If the ref does not exist or has
a different SHA, git exits non-zero and `_run` (with `check=True`
by default) raises `GitError`. Chunk 9e classifies the error in
the route layer and turns it into the `?error=ref-changed` /
`?error=not-found` redirect.

The route layer **always** passes `expected_old_sha` from a
fresh server-side `list_refs` lookup (never `None`, never trusting
the form-hidden field) — the optional-kwarg form is fine to call
because the route's responsibility is to decide whether to provide
it. See §F (Security boundaries).

`Integrator` already calls `delete_ref` for §3.4 compensating
deletes; chunk 9e is the second consumer in the codebase. No
change to integrator code; no change to delete_ref's surface.

## §C Templates

Eight new templates under `reference/services/web-ui/src/eden_web_ui/templates/`:

- `admin_index.html` — landing dashboard.
- `admin_tasks.html` — task table.
- `admin_task_detail.html` — task detail + reclaim form.
- `admin_trials.html` — trial table.
- `admin_trial_detail.html` — trial detail + filtered events.
- `admin_events.html` — event log.
- `admin_work_refs.html` — work-ref GC.
- `admin_action_result.html` — *not actually a separate template*;
  the result banners render inline in each detail page. Listed
  here only to flag we considered and rejected a separate template.

All extend `base.html` (so the topbar nav, sign-out, brand, and
HTMX script tag are inherited). Filter forms post to themselves
via GET (querystring) so back/forward / deep-linking works. The
reclaim and ref-delete forms POST to a distinct action URL and
303-redirect.

## §D Navigation

`base.html` gains a permanent "admin" link (the same way "evaluator"
got a permanent link in chunk 9d). Position: rightmost, after
"evaluator", so the role-progression read of the topbar (planner
→ implementer → evaluator → admin) matches the experiment
lifecycle.

## §E CLI changes

None. The web-ui's existing flags are sufficient. Specifically:

- `--repo-path` was already optional; chunk 9e widens its consumer
  set (work-ref GC reads it) without changing the flag's
  optionality or default.
- No new admin-secret, no new admin-bind-host, no new RBAC config.
  Auth remains the chunk-9 cookie + sign-in form.

## §F Security boundaries

| Surface | Trust boundary |
|---|---|
| `/admin/*` GET | Signed-in cookie required; 303 → `/signin` otherwise |
| `/admin/*` POST | Signed-in cookie + CSRF token both required |
| `?limit=` / `?kind=` / `?state=` | Coerced + clamped at parse time; no echo into HTML |
| `?error=…` banner | Looked up in a closed dict; no arbitrary text echo |
| `ref_name` POST | Regex-validated to `^refs/heads/work/...$` + path-traversal check |
| `delete_ref` | CAS via `expected_old_sha` from the **request-time** read, not the POST body |
| Trial / proposal description | Jinja autoescape (same as evaluator page) |
| Trial / proposal artifacts_uri | Same `_read_inline_artifact` envelope as chunks 9c/9d |
| Trial / proposal artifacts_uri href | Same scheme allowlist (`http`/`https`/`file` only) |

The `expected_old_sha` source matters: we read the ref SHA at the
GET-time render and stash it in a hidden form field, but the POST
handler **does not trust the form's hidden field** — it does its
own fresh `list_refs("refs/heads/work/<name>")` lookup, uses *that*
SHA as `expected_old_sha`, and only then calls `delete_ref`. The
hidden field is an optimization that lets us show a stale-state
banner ("the ref changed since you loaded the page") earlier; the
authoritative read is server-side.

## §G Errors

`/admin/*` routes catch:

- `IllegalTransition` (storage-domain) on reclaim → banner.
- `NotFound` (from `eden_storage.errors` and re-raised by the
  `eden_wire` `StoreClient` for HTTP 404 problem+json responses) on
  every detail-route read (`store.read_task`, `read_trial`,
  `read_proposal`). The existing `@app.exception_handler(404)` in
  `reference/services/web-ui/src/eden_web_ui/app.py:90` only catches
  HTTP 404 *responses* — it does not catch raised
  `eden_storage.NotFound` / `eden_wire.NotFound` exceptions. Chunk
  9e adds an explicit handler in `make_app`:

  ```python
  from eden_storage.errors import NotFound as StorageNotFound

  @app.exception_handler(StorageNotFound)
  async def _storage_not_found(request, exc):
      return templates.TemplateResponse(
          request, "_error.html",
          {"title": "Not found", "message": str(exc)},
          status_code=404,
      )
  ```

  Routes raise it implicitly via the `Store` calls; one app-wide
  handler covers every `/admin/*/<id>/` detail route plus any
  similar use elsewhere in the UI. The wire-side `NotFound` and
  the storage-side `NotFound` are the same exception class
  (`eden_storage.errors.NotFound`, re-raised by `StoreClient`),
  so a single handler suffices.

- `GitError` on `list_refs` / `delete_ref` → re-raise as a 5xx
  via FastAPI's default; the operator sees the standard error
  page. Logging captures the git stderr.
- Transport-shaped `Exception` from `StoreClient` on every read
  *and* write call (per `feedback_impl_review_lenses.md` lens 11):
  surfaces a `?error=transport` banner on actions; renders an
  inline "transport failure; refresh to retry" placeholder on
  read views.

## §H Tests

Five test files mirroring the chunk 9c/9d coverage taxonomy:

### `test_admin_routes.py` — per-route unit tests

- `GET /admin/` renders counts that match the seeded store state.
- `GET /admin/tasks/` with each (`kind`, `state`) combination
  including the `*` sentinel → expected rowset.
- `GET /admin/tasks/<id>/` for each task state (pending, claimed,
  submitted, completed, failed) renders the right reclaim button
  variant (or none, for terminal).
- `GET /admin/trials/` for each status filter.
- `GET /admin/trials/<id>/` for `starting`, `success`-non-integrated,
  `success`-integrated, `error`, `eval_error`. Asserts the events
  filter only includes events whose `event.data` mentions this
  trial_id (or that belong to the related implement / evaluate
  task by `task_id`, per §A.5's correlation algorithm).
- `GET /admin/events/` default + `?limit=` clamped at the cap.
- `GET /admin/work-refs/` with `repo=None`, with no refs, with
  eligible refs, with not-eligible refs, with orphan refs.
- `POST /admin/tasks/<id>/reclaim` — happy path (claimed → pending
  via redirect), submitted → pending (operator-reclaim of submitted),
  terminal → `?error=illegal-transition`, no session → 303 to
  `/signin`, with-session-but-no-CSRF → 403.
- `POST /admin/work-refs/delete` — happy path, regex rejection
  (`refs/heads/trial/...`, `..` in path, empty), missing-CSRF, ref
  not eligible at POST time, ref vanished between GET and POST,
  ref SHA changed (CAS miss).

### `test_admin_flow.py` — cross-request flow

- Sign in → claim a task via planner → admin dashboard reflects
  the claim → operator-reclaim from admin → planner page no
  longer claims that task. End-to-end flows that span modules.
- Sign in → submit a task via implementer → admin sees `submitted`
  → operator force-reclaim → admin sees `pending`.
- Sign in → planner submits a proposal → admin trial detail page
  for the resulting trial reflects the proposal context.

### `test_admin_security.py` — security invariants

- Unauthenticated `GET /admin/*` redirects to `/signin` (every
  route).
- Unauthenticated `POST /admin/*` redirects to `/signin` with
  status 303 (auth check fires *before* the CSRF check — the same
  order the planner / implementer / evaluator routes use; tests
  exercise this order explicitly).
- Authenticated `POST /admin/*` with a missing or wrong CSRF token
  returns 403 (after the auth check passes).
- `?error=<arbitrary-string>` does not echo into the HTML; only
  the closed-allowlist values render banners.
- `ref_name` regex: hand-crafted POSTs with each of
  `refs/heads/trial/x`, `refs/tags/x`, `HEAD`, `../etc/passwd`,
  `refs/heads/work/../trial/x`, `refs/heads/work/$(rm -rf /)`
  are rejected. The last two are the trust-boundary cases — we
  test both that the regex rejects them and that no subprocess
  invocation occurs (monkeypatched `subprocess.run` is asserted
  uncalled).
- The `expected_old_sha` passed to `delete_ref` comes from the
  route's fresh `list_refs` lookup, not the form's hidden field.
  Verified by setting the hidden field to a different SHA and
  confirming the deletion still uses the live SHA.
- CSRF token mismatch on every mutating route returns 403.
- Trial / proposal artifacts_uri scheme allowlist applies to admin
  rendering too (no scheme injection via a `javascript:` href on
  the trial detail page).
- The `_read_inline_artifact` confinement test from chunk 9d is
  re-run against the admin trial detail route to confirm the
  envelope holds at this call site.

### `test_admin_partial_write.py` — partial-write recovery

- `store.reclaim` raises `IllegalTransition` on a terminal task →
  redirect with the right error code; the task is unchanged in
  the store.
- `store.reclaim` raises a transport-shaped `Exception` →
  redirect with `?error=transport`. The task may or may not have
  been reclaimed; we verify the route does not double-call.
- `repo.delete_ref` raises `GitError` mid-deletion → 5xx via
  default handler; ref is unchanged. Operator can refresh and
  retry.
- The work-ref GET-time eligibility filter and POST-time
  re-validation diverge (the trial moved back to `starting`
  between GET and POST) → POST refuses with `?error=not-eligible`;
  ref is preserved.

### `test_admin_e2e.py` — `pytest.mark.e2e`

One test that:

1. Spawns task-store-server + web-ui (no orchestrator;
   admin-reclaim is the only state-changing action exercised, and
   the orchestrator wouldn't see it cleanly).
2. Drives a planner submit through real HTTP (real session, real
   CSRF) so a task moves to `submitted`.
3. Hits `/admin/tasks/` and confirms the row appears with state
   `submitted`.
4. Hits `POST /admin/tasks/<id>/reclaim` and follows the redirect.
5. Confirms via the wire surface (a fresh `StoreClient.read_task`)
   that the task is back in `pending` and a `task.reclaimed` event
   with `cause=operator` was logged.

This test does not exercise the work-ref GC path (which would
require seeding a bare repo + creating refs); the unit tests in
`test_admin_routes.py` cover that surface against an in-memory
`GitRepo` double.

## §I Recovery / failure-mode taxonomy

The reclaim action sits adjacent to the chunk-9c/9d
retry-before-orphan + read-back machinery, but it is **not** that
machinery — it's a single one-shot call. Recovery mode summary:

| Failure | Detection | UI outcome |
|---|---|---|
| `IllegalTransition` (terminal) | Store raises | `?error=illegal-transition` banner |
| `IllegalTransition` (already-pending) | Store raises | `?error=illegal-transition` banner (same banner; benign) |
| Transport timeout / disconnection | `Exception` from `StoreClient` | `?error=transport`; operator refreshes detail page to see whether the call landed |
| CSRF / session expired | route returns 403 | browser shows the 403 page (existing behavior) |
| Ref deletion CAS miss | `GitError` with stderr containing `expected ... but is ...` | `?error=ref-changed` |
| Ref vanished | `GitError` exit 1 with empty list | `?error=not-found` |

The transport case deliberately does **not** auto-retry. Reclaim
is a state-mutating one-off; a silent retry could double-fire
(if the first call landed but the response was lost, the second
call hits a now-pending task and raises `IllegalTransition` —
which the operator sees as the "this task cannot be reclaimed"
banner, which is *operationally fine* but confusing).
Manual refresh-then-retry by the operator is the intended UX, and
the banner copy explains it.

## §J Integration points with prior chunks

- **Chunk 1's expired-claim sweeper** (`eden_dispatch.sweep_expired_claims`)
  is unchanged; chunk 9e exposes the *result* of that sweeper (the
  "claim age past expires_at" badge) but does not invoke it.
- **Chunk 9c's implementer module** is unchanged. The work-ref GC
  page can affect implementer outputs (deleting a ref the
  implementer module created); the implementer's read paths do not
  re-fetch ref lists, so this is purely an admin/integrator-facing
  cleanup.
- **Chunk 9d's evaluator module** is unchanged. The trial detail
  page in chunk 9e re-uses `_read_inline_artifact` and the scheme
  allowlist that chunk 9d generalized.
- **The orchestrator service** (Phase 8b) is unchanged. The admin
  reclaim path goes through `Store.reclaim`, which the orchestrator
  observes as a `task.reclaimed` event in its next iteration and
  reacts to via the existing reconcile-on-reclaim logic.

## §K Known limitations / future work

- **§K-1 No real-time updates.** Operators must refresh. SSE / WS
  push lands in Phase 12 with the control plane; the admin views
  are designed to render fast enough (single in-memory snapshot per
  request) that hammering F5 is acceptable.
- **§K-2 Reclaim-of-submitted is sharp.** §A.4's force-reclaim
  warning is the only guardrail. A misclick replays partial work.
  Phase 12's RBAC will narrow the operator scope; until then, the
  warning copy is what we have.
- **§K-3 Work-ref GC is one-at-a-time.** Bulk delete is out of
  scope (see Non-goals). If reference experiments grow large, a
  follow-up plan can layer a "delete all eligible" action on top
  of the per-ref primitive without changing it.
- **§K-4 Event log filter is exact-match only.** `?type=task.claimed`
  works; `?type=task.*` does not. Wildcard filters add surface
  area for misuse and aren't load-bearing for any current debug
  workflow. Easy to add later.
- **§K-5 No "delete proposal in drafting"** action. §5.2's note
  ("Implementations MAY expose them to operators for inspection or
  removal") is a MAY; chunk 9e does not exercise it. Drafting
  proposals from a reclaimed plan task are visible on
  `/admin/trials/` (indirectly, via the trial that didn't form)
  but cannot be deleted from the UI. A follow-up can add it once
  there is a concrete reason to.
- **§K-6 No event-log search-by-trial-id from the events page.**
  `/admin/events/` filters by `type` only. The trial detail page
  already does the trial-aware filter. Dual-filter (type AND
  trial_id) is a follow-up.

## §L Implementation order

1. **§A.1–§A.2**: scaffold `routes/admin.py` with the auth-first
   gate and stateless snapshot pattern; wire it into `make_app`;
   add a permanent "admin" nav link in `base.html`. Land a
   trivial `GET /admin/` that returns counts.
2. **§A.3 + §A.4**: tasks table + detail + reclaim. Land the
   chunk's most-load-bearing action first.
3. **§A.5**: trials table + detail (including the §A.5 trial-event
   correlation algorithm).
4. **§A.6**: event log view (with the natural-index-before-slice
   discipline from §A.6).
5. **§A.7**: work-refs page (GET + POST), consuming the existing
   `GitRepo.delete_ref` primitive.
6. **§A.8**: dashboard summary refinements (cross-tab + stranded-
   claim callout).
7. Templates land alongside their routes; nothing is rendered
   before its route exists.
8. Tests file-by-file, in the same order as the routes.
9. AGENTS.md (canonical) / reference README / roadmap status
   update. (`CLAUDE.md` is a symlink to `AGENTS.md` so editing the
   canonical file is sufficient.)

## §M Acceptance criteria

- All unit, flow, security, partial-write, and e2e tests pass.
- `uv run ruff check .`, `uv run pyright`, full suite green.
- A live walkthrough: spawn task-store-server + web-ui, sign in,
  claim a planner task in one tab, observe it on `/admin/tasks/`
  in another tab as `claimed`, click reclaim from admin, verify
  the planner tab's claim breaks on next form-POST, verify
  `task.reclaimed` event with `cause=operator` is in
  `/admin/events/`.
- `docs/roadmap.md` updated: Phase 9 marked complete.
- `AGENTS.md` (the canonical root guidance file; `CLAUDE.md` is
  the symlink) "Current phase" section gains the 9e paragraph
  with the same level of detail as the existing 9d paragraph.

## §N Out of scope

Same as the Non-goals section above, restated in compact form for
end-of-doc reference: real auth/RBAC; real-time updates; cross-
experiment views; full pagination; trial / proposal / event
editing; bulk actions; `trial/*` ref deletion; a "retry evaluate"
button; CLI changes beyond consuming the existing `--repo-path`
flag in one new place.
