# Phase 12a-1c — Task transparency + lineage navigation

## 1. Context

Manual UI session on 2026-05-13 surfaced a transparency gap in the
reference Web UI's per-role and admin surfaces:

- **`/executor/`** lists pending execution tasks by `task_id`,
  `idea_id`, `created` only. To see the idea's `slug`, `priority`,
  `parent_commits`, or content (the markdown body referenced by
  `idea.artifacts_uri`), the operator MUST claim first — claiming
  is irreversible during the TTL, so "browse before commit" is not
  possible today.
- **`/evaluator/`** has the same shape: `task_id`, `variant_id`,
  `created`. The variant's `branch`, `commit_sha`, `parent_commits`,
  attribution (`executed_by`), and the executor's `artifacts_uri`
  are only visible after claim.
- **`/admin/tasks/<id>/`** shows `kind`, `state`, claim metadata,
  and `payload` as raw JSON — but does NOT render the linked
  content (the referenced idea for an execution task; the
  referenced variant for an evaluation task) and does NOT surface
  the 12a-1 attribution fields (`task.target`, `created_by`,
  `submitted_by`) in a structured way.
- **`/admin/ideas/`** does not exist — the only way to inspect an
  idea today is through the executor module's draft form (which
  requires claiming an execution task that references it) or by
  reading `/admin/events/?type=idea.drafted` and walking by hand.
- **No lineage navigation.** A reader looking at an evaluation
  task cannot click through to the variant, executor task, idea,
  or ideation task. Each is a separate read in a separate browser
  tab, with no breadcrumbs.

Every piece of data this chunk surfaces is already on the wire
post-12a-1. Ideas carry `created_by`; variants carry `executed_by`
and `evaluated_by`; tasks carry `target`, `created_by`,
`submitted_by`, and `claim.worker_id`. Idea content is reachable
via `idea.artifacts_uri` and rendered through the existing trust-
boundary helper [`_read_inline_artifact`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py).

This is a **pure UI surface** chunk. Zero spec changes, zero new
wire endpoints, zero new storage operations. The pattern mirrors
the 12a-1b worker/group admin UI: read-only views and new admin
sub-modules layered on top of the existing 12a-1 wire surface.

### What this chunk delivers

1. **`/executor/` list page**: per pending task, render an
   inline-collapsed idea preview (slug, priority, `parent_commits`,
   `created_by`, target, plus an expandable section showing the
   markdown content via `read_idea_content`).
2. **`/evaluator/` list page**: per pending task, render an
   inline-collapsed variant preview (`variant_id`, branch,
   `commit_sha`, `parent_commits`, `executed_by`, target, plus
   the executor's `artifacts_uri` rendered through
   `read_variant_artifact`).
3. **`/admin/tasks/<task_id>/` extension**: a new "lineage" section
   linking to the referenced idea / variant / ideation task / execution
   task / evaluator follow-ups; plus a new "attribution" section
   surfacing `target`, `created_by`, `submitted_by`,
   `claim.worker_id`. The existing `payload` block is preserved.
4. **`/admin/ideas/` (new module)**: list view (filterable by state) +
   per-idea detail (full record + lineage to ideation task and forward
   to variants spawned).
5. **`/admin/variants/<variant_id>/` extension**: a new "lineage"
   section linking back to execution task / idea / ideation task and
   forward to evaluation tasks against this variant.

All views are read-only. Submission flows (claim, draft, submit) are
unchanged. No new mutating routes.

## 2. Decisions captured before drafting

These were settled with the operator before this plan was drafted;
the plan honors them and the codex-review loop should not re-litigate
them unless an inter-chapter contract conflict is found.

1. **Operator audience.** The intended reader of these views is the
   human operator running the experiment via the web UI. Programmatic
   agent access to substrate state is 12a-1f's concern; this chunk
   doesn't touch that surface.

2. **Read-only.** No mutating surface introduced. The new
   `/admin/ideas/` module does NOT expose idea-creation /
   idea-deletion (the wire surface has neither in 12a-1, and adding
   them would be a separate chunk with its own spec / RBAC scope).
   The existing reclaim + ref-GC mutating routes on `/admin/tasks/*`
   and `/admin/work-refs/*` are unchanged.

3. **Idea-content preview uses the existing trust-boundary helper.**
   `read_idea_content(idea, artifacts_dir)` already short-circuits
   non-`file://` URIs, rejects path-traversal, rejects
   non-text-decodable bytes, and caps at 1 MiB. The list-page
   preview re-uses that helper exactly; it does NOT add a new
   trust boundary. Variant artifacts use the sibling
   `read_variant_artifact`. The list page renders previews behind
   a `<details>` element so the default render is one row per task
   without forcing every artifact through the helper at page-load
   time — but the helper is still called server-side for every
   row in a single iteration over `pending` (we do NOT lazy-load
   on `<details>` open; see §D.10 for the rationale and bound).

4. **Lineage navigation is plain links between admin detail pages,
   not embedded sub-rendering.** Each step is a hyperlink. We do
   NOT inline-render variants inside the task detail or ideas
   inside the variant detail — that would multiply the surface
   tested per page. The lineage section is just a list of links
   with one-line descriptors for each linked artifact.

5. **`/admin/ideas/` mirrors `/admin/variants/` and `/admin/workers/`
   structurally.** Same banner-allowlist pattern, same auth-first
   GET / POST discipline (though /admin/ideas/ exposes only GET in
   this chunk), same `_read_failure_response` placeholder shape, and
   same closed-error vocabulary. No new patterns introduced.

6. **No spec changes.** Chapter 02 §1 (artifact lineage), §6 (worker
   registry), §7 (groups), §9 (attribution); chapter 04 §2-4 (task
   state machine + claim/submit); chapter 07 §6 (read endpoints) are
   all complete for the surfaces this chunk surfaces. We touch zero
   spec files.

7. **Idea ← ideation-task reverse linkage is reconstructed via the
   submission record, not a new persisted backref.** Ideas carry
   `created_by` (a `worker_id`), not a `task_id`. To answer "which
   ideation task produced this idea?" we scan ideation tasks whose
   submission has `idea_id` in `IdeaSubmission.idea_ids`. See §D.9
   for the bounded-scan posture.

8. **Coordination posture.**
   - 12a-2 (orchestrator-as-role) is a sibling chunk that ALSO
     extends `admin_task_detail.html` — it adds a "reassign" section.
     This chunk's "lineage" + "attribution" sections are additive on
     the same template; the conflict surface is mechanical (textual
     ordering inside one Jinja template). Whichever chunk lands
     second rebases the section ordering.
   - 12a-1b (workers/groups admin) is already merged; this chunk's
     `/admin/ideas/` is a sibling module to `/admin/workers/` and
     `/admin/groups/`, following the same conventions exactly.
   - 12a-1g (durability bind-mounts) shifted the runtime data root
     to `${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/`. This chunk reads
     `app.state.artifacts_dir` (set by `cli.py` at startup) and is
     not aware of the host-side layout; no change.

## 3. Design

### D.1 Module placement and routing

New routes file:
[`reference/services/web-ui/src/eden_web_ui/routes/admin_ideas.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_ideas.py).

Existing routes files are extended (small, additive surface — not
new modules):

- [`routes/executor.py`](../../reference/services/web-ui/src/eden_web_ui/routes/executor.py) —
  `list_pending` extended with idea-preview block (no new routes).
- [`routes/evaluator.py`](../../reference/services/web-ui/src/eden_web_ui/routes/evaluator.py) —
  `list_pending` extended with variant-preview block.
- [`routes/admin.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin.py) —
  `task_detail` and `variant_detail` extended with lineage +
  attribution.

| Method | Path                                | Purpose                                                |
| ------ | ----------------------------------- | ------------------------------------------------------ |
| GET    | `/admin/ideas/`                     | List view; filter by state                             |
| GET    | `/admin/ideas/{idea_id}/`           | Detail view: full record + lineage + spawned variants |

The new module mounts unconditionally in `make_app`, same posture as
`admin_workers` / `admin_groups`. The admin landing page
(`templates/admin_index.html`) gets a new "ideas" link card.

### D.2 Auth gate + CSRF discipline

Every GET runs `get_session(request)` first; absent session → `303 →
/signin`. No mutating routes in this chunk so no CSRF surface to
extend. Mirrors `admin_workers` line 266-272 exactly.

### D.3 Lineage helper module

New helper module:
[`routes/_lineage.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_lineage.py).
Exposes pure functions (no I/O of its own; takes a `Store` and
returns navigable structs) so the same logic can be tested unit-style.

```python
def lineage_for_task(store: Store, task: Task) -> Lineage: ...
def lineage_for_variant(store: Store, variant: Variant) -> Lineage: ...
def lineage_for_idea(store: Store, idea: Idea) -> Lineage: ...
```

`Lineage` is a `dataclass` (frozen, slots) holding optional refs to:

```python
@dataclass(frozen=True, slots=True)
class LineageLink:
    label: str          # short human label
    href: str           # /admin/<...>/<id>/
    descriptor: str     # one-line context (e.g., "slug=foo, status=success")

@dataclass(frozen=True, slots=True)
class Lineage:
    ideation_task: LineageLink | None
    idea: LineageLink | None
    variant: LineageLink | None
    execution_task: LineageLink | None
    evaluation_tasks: tuple[LineageLink, ...] = ()  # 0+ — a variant may have multiple eval tasks over its life
    transport_errors: int = 0                         # count of read failures that left some links unfilled
```

The lineage section in each template renders this list in
fixed order: ideation_task → idea → execution_task → variant →
evaluation_tasks. Each link is omitted (rendered as `(unknown)` or
similar) if the lookup raised `StorageNotFound` (e.g., the variant
was deleted by a future chunk that doesn't exist yet) or if the
reverse-walk found no match.

Transport-shaped reads count `transport_errors` and the rendered
section banner says "lineage may be incomplete — N transport errors"
so the operator can refresh. A read that succeeds with `None` (e.g.,
"this variant has no completed evaluation task yet") is the empty
list, not a transport error.

### D.4 `/executor/` list — idea preview

[`routes/executor.py::list_pending`](../../reference/services/web-ui/src/eden_web_ui/routes/executor.py)
currently returns `pending: list[ExecutionTask]`. Extended to also
build `pending_rows: list[dict]` where each row is:

```python
{
    "task": task,                              # already passed today
    "idea": idea or None,                      # store.read_idea(task.payload.idea_id)
    "idea_content": content or None,           # read_idea_content(idea, artifacts_dir), or None
    "target": task.target,                     # may be None / worker / group
    "lineage_link": "/admin/tasks/<id>/",      # for the operator to dig deeper
    "read_failed": False,                       # true iff the idea read raised transport
}
```

`store.read_idea` is called once per pending row. The fixture
deployment has ≤ ~20 pending tasks at peak; the linear-scan
performance posture is the same as `admin_workers._groups_containing`
(plan §3.5 reference deployments stay small).

If `read_idea` raises `StorageNotFound`, `idea` is `None` and the row
renders "(idea unavailable)". If it raises a transport-shaped
exception, the row sets `read_failed=True` and the whole list shows
a banner "N idea reads failed; refresh to retry".

`idea_content` is rendered behind a `<details>` element so the list
is scannable by default. The preview text is server-rendered into
the response (the helper is called once at list-build time); we do
NOT lazy-load on `<details>` open. Reasons:

1. The trust-boundary helper enforces the 1 MiB cap and stays fast
   (single-stat + bounded read).
2. Lazy-loading would require a new endpoint, multiplying the
   surface.
3. `<details>` is a pure-HTML disclosure widget that works without JS.

Existing `recent_variants` section is unchanged.

### D.5 `/evaluator/` list — variant preview

[`routes/evaluator.py::list_pending`](../../reference/services/web-ui/src/eden_web_ui/routes/evaluator.py)
gets the same treatment. Per row:

```python
{
    "task": task,
    "variant": variant or None,                  # store.read_variant(task.payload.variant_id)
    "idea": idea or None,                        # store.read_idea(variant.idea_id) if variant else None
    "variant_artifact": content or None,         # read_variant_artifact(...)
    "target": task.target,
    "lineage_link": "/admin/tasks/<id>/",
    "read_failed": False,
}
```

The executor's `artifacts_uri` is rendered through
`read_variant_artifact(variant.artifacts_uri, artifacts_dir)` — the
existing helper. The list shows the executor's `executed_by`
attribution + the variant's `branch` and `commit_sha`. Same
`<details>`-based progressive-disclosure pattern as §D.4.

### D.6 `/admin/tasks/<task_id>/` — lineage + attribution sections

[`routes/admin.py::task_detail`](../../reference/services/web-ui/src/eden_web_ui/routes/admin.py)
gains two new template-side sections; the route layer composes them
into the context dict:

- **`attribution`** (always rendered, even on a freshly-created task):
  shows `target` (rendered as `worker:<id>` / `group:<id>` / `anyone`
  via a small `_format_target` helper), `created_by` (or `—`),
  `submitted_by` (or `—`), and `claim.worker_id` (if present —
  hyperlinked to `/admin/workers/<id>/`). All four are display-only.
- **`lineage`** (always rendered): the linkage chain for this task's
  kind:
  - `kind=ideation` → variants whose `idea_id` appears in the
    submission's `idea_ids` are spawned across many follow-up
    execution tasks; we link to each idea in the submission (and
    each idea's spawned variants in turn — bounded one level deep
    to keep the chain small).
  - `kind=execution` → links to the referenced idea
    (`task.payload.idea_id`), the ideation task that produced it
    (reverse-walk per §D.9), and any variant whose `idea_id` matches
    (forward fan-out; a task may have produced multiple variants
    over its life across reclaims, but typically one — we list all).
  - `kind=evaluation` → links to the referenced variant
    (`task.payload.variant_id`), the variant's idea, the ideation
    task, and the execution task that produced the variant
    (reverse-walk over execution tasks whose `payload.idea_id` ==
    variant.idea_id; same scan-pattern as §D.9 but in the execution
    direction).

The existing `payload` + `related events` sections are preserved
unchanged. The "operator reclaim" section is preserved unchanged.

The route layer builds both sections via `lineage_for_task(store,
task)` and passes the `Lineage` struct to the template. If any
underlying read fails, the section renders with the failing link
shown as `(unknown — read error)` and a `transport_errors` count
surfaced as a per-section banner.

### D.7 `/admin/variants/<variant_id>/` — lineage section

[`routes/admin.py::variant_detail`](../../reference/services/web-ui/src/eden_web_ui/routes/admin.py)
gains a lineage section listing: the execution task that produced
this variant (reverse-walked via §D.9), the parent idea
(`variant.idea_id` — already loaded into the context but currently
not linked), the ideation task (reverse-walked via §D.9), and any
evaluation tasks whose `payload.variant_id` matches.

The existing `parent idea` section is preserved as a sibling — but
the slug there becomes a hyperlink to `/admin/ideas/<idea_id>/`.

### D.8 `/admin/ideas/` and `/admin/ideas/<idea_id>/`

New module
[`routes/admin_ideas.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_ideas.py)
follows `admin_workers.py` structurally.

**List view** (`GET /admin/ideas/`):

- Calls `store.list_ideas(state=<filter>)` with the existing
  `_coerce_filter` pattern over the closed enum (`drafting`, `ready`,
  `dispatched`, `completed`); invalid filter → empty rowset
  (`_INVALID_FILTER` sentinel from `admin.py`).
- Each row: `idea_id`, `slug`, `priority`, `state`, `created_by`,
  `parent_commits` (first 8 chars of each), variant count (computed
  via `len([v for v in store.list_variants() if v.idea_id == X])`
  — a single `list_variants()` call shared across rows).
- Renders `admin_ideas.html` mirroring `admin_workers.html` layout
  (filter input top, table below, banner area at top).

**Detail view** (`GET /admin/ideas/<idea_id>/`):

- `store.read_idea(idea_id)` → `Idea`; `StorageNotFound` propagates
  to the app-wide 404 handler (existing chunk-9e pattern).
- Renders the full record (idea_id, slug, priority, state, parent_commits,
  artifacts_uri, created_at, created_by — `created_by` hyperlinked to
  `/admin/workers/<id>/`).
- Renders the markdown content preview via `read_idea_content`
  behind a `<details>` block (same trust-boundary path as the
  executor draft form).
- Lineage section: ideation task (reverse-walk per §D.9), forward
  to variants spawned (each linked to `/admin/variants/<id>/`).

### D.9 Idea ← ideation-task reverse linkage

`Idea` carries no direct `task_id` backref. Reverse lookup walks
ideation tasks:

```python
def _ideation_task_for_idea(store: Store, idea_id: str) -> str | None:
    for t in store.list_tasks(kind="ideation"):
        if t.state not in {"submitted", "completed", "failed"}:
            continue
        submission = store.read_submission(t.task_id)
        if submission is None:  # invariant says this shouldn't happen for terminal+submitted
            continue
        if not isinstance(submission, IdeaSubmission):
            continue
        if idea_id in submission.idea_ids:
            return t.task_id
    return None
```

Posture notes:

- The scan is bounded by the count of ideation tasks (typically
  one or a small handful per experiment), so this is O(N_ideation) per
  render — same complexity class as `admin_workers._groups_containing`.
- For terminal-state tasks the submission is guaranteed present
  per the store invariant; for `pending` / `claimed` tasks (idea
  was just created but ideator hasn't submitted yet) there's no
  submission to read — we return `None` and the lineage section
  renders "(ideation task: in progress)" with no link.
- Transport-shaped failure during `read_submission` increments
  `transport_errors` and continues (best-effort).
- We never cache between requests; each page render walks fresh.
  Cache invalidation against a live store is its own complexity
  cost we don't pay here; the scan is cheap at fixture scale.

The execution-task-for-variant reverse lookup is the same shape but
filters `kind="execution"` and matches `task.payload.idea_id ==
variant.idea_id`. A single helper `_tasks_referencing(...)`
parametrizes both directions.

### D.10 Performance and bounds

- Each list-page render runs **one** `store.list_*` call per kind it
  needs (e.g., executor's list: `list_tasks(kind="execution",
  state="pending")` + per-row `read_idea`). N+1 per-row reads are
  acceptable at fixture scale (≤ ~20 pending rows).
- Each detail-page render runs at most one full `list_tasks()` for
  the reverse-walk (filtered to the relevant `kind` server-side via
  the existing `Store.list_tasks(kind=...)` argument).
- Idea-content + variant-artifact previews are bounded by the
  existing trust-boundary helper (1 MiB cap, single-stat).
- The `Lineage` struct's `evaluation_tasks` field is rendered with a
  hard cap of `_LINEAGE_EVAL_CAP = 20` followups (same shape as the
  chunk-9e `_TRIAL_DETAIL_EVENT_CAP`); the rendered template shows
  "(showing N of M)" when capped.

## 4. Scope

### In scope

- New routes module `admin_ideas.py`.
- New helper module `_lineage.py` with `Lineage` dataclass +
  `lineage_for_*` functions.
- Template changes:
  - `executor_list.html` — idea-preview block per row.
  - `evaluator_list.html` — variant-preview block per row.
  - `admin_task_detail.html` — new `attribution` + `lineage` sections.
  - `admin_variant_detail.html` — new `lineage` section + idea-link.
  - `admin_ideas.html` (new) — list view.
  - `admin_idea_detail.html` (new) — detail view.
  - `admin_index.html` — new "ideas" link card.
- Route-handler changes:
  - `executor.list_pending` — build `pending_rows`.
  - `evaluator.list_pending` — build `pending_rows`.
  - `admin.task_detail` — populate `attribution` + `lineage`.
  - `admin.variant_detail` — populate `lineage`.
  - `admin_ideas.ideas_index` + `admin_ideas.idea_detail` — new.
- App-factory change: include the new router in `make_app`.
- Tests as enumerated in §6.

### Out of scope

- Any mutating route on `/admin/ideas/` (no register / delete /
  edit). Idea creation continues to flow through the existing
  ideator module.
- Streaming / live updates on list pages.
- Programmatic-agent access (12a-1f's concern).
- Spec changes (none required).
- New wire endpoints (none required).
- Re-shaping the existing claim → draft → submit flows on
  `/executor/*` / `/evaluator/*`.
- Server-side query / filter DSL on the new admin module
  (filter-by-state mirrors the existing `_coerce_filter` pattern
  with one closed enum; richer filtering is a separate chunk).
- Persisting an `Idea.ideation_task_id` backref (would require a
  spec change to chapter 02 §1 + a schema migration; out of scope).

## 5. Files to touch

### Spec

None.

### Reference packages

None — `eden_storage` / `eden_contracts` / `eden_wire` are
unchanged.

### Web UI service (`reference/services/web-ui/`)

- `src/eden_web_ui/routes/_lineage.py` — **new** helper module.
- `src/eden_web_ui/routes/admin_ideas.py` — **new** routes module.
- `src/eden_web_ui/routes/executor.py` — extend `list_pending`;
  add `_idea_preview_row` helper.
- `src/eden_web_ui/routes/evaluator.py` — extend `list_pending`;
  add `_variant_preview_row` helper.
- `src/eden_web_ui/routes/admin.py` — extend `task_detail` and
  `variant_detail`; import + call `_lineage.lineage_for_task` /
  `lineage_for_variant`.
- `src/eden_web_ui/app.py` — `app.include_router(admin_ideas_routes.router)`.
- `src/eden_web_ui/templates/executor_list.html` — add per-row
  preview block.
- `src/eden_web_ui/templates/evaluator_list.html` — add per-row
  preview block.
- `src/eden_web_ui/templates/admin_task_detail.html` — add
  `attribution` + `lineage` sections (additive; preserves existing
  `payload`, `operator reclaim`, `related events`).
- `src/eden_web_ui/templates/admin_variant_detail.html` — add
  `lineage` section + hyperlink the existing `parent idea` slug.
- `src/eden_web_ui/templates/admin_ideas.html` — **new** (list).
- `src/eden_web_ui/templates/admin_idea_detail.html` — **new**.
- `src/eden_web_ui/templates/admin_index.html` — add ideas link.

### Tests (`reference/services/web-ui/tests/`)

- `test_admin_ideas_routes.py` — **new**. Per-route validation:
  list filter coercion, detail 404, banner allowlist behavior.
  ~15 tests.
- `test_admin_ideas_flow.py` — **new**. Cross-request flows:
  list → detail → linked variant detail. ~5 tests.
- `test_admin_ideas_security.py` — **new**. Auth-first
  (unauthenticated GET → 303 /signin), Jinja autoescape on
  `idea.slug` / `created_by` / arbitrary querystring filter,
  path-traversal rejected on detail URL (handled by FastAPI's
  path parameter encoding — assertion-only). ~10 tests.
- `test_admin_tasks_lineage.py` — **new**. Renders the lineage +
  attribution sections for each `kind`; verifies links resolve to
  the right targets; verifies "(unknown)" placeholder when an
  upstream artifact is missing. ~10 tests.
- `test_admin_variants_lineage.py` — **new**. Same shape for the
  variant detail page. ~5 tests.
- `test_executor_list_preview.py` — **new**. Idea preview
  rendering: row count matches `pending`, slug / priority /
  parent_commits surface, `<details>` block carries the content
  when the helper succeeds, `(content unavailable)` when the helper
  returns `None`, `(idea unavailable)` when `read_idea` 404s,
  transport-error banner when the read transport-fails. ~8 tests.
- `test_evaluator_list_preview.py` — **new**. Variant preview
  rendering: same shape as executor; additionally verifies
  `executed_by` rendering and the variant.artifacts_uri inline
  rendering. ~8 tests.
- `test_admin_ideas_e2e.py` — **new**, `pytest.mark.e2e`. Spawns
  task-store-server + web-ui as real subprocesses, creates a
  bare-repo seed + an idea via `IdeaSubmission`, then drives the
  `/admin/ideas/` list + detail via real HTTP and verifies the
  lineage links resolve. Follows the existing
  `test_admin_workers_e2e.py` pattern.

Existing `test_executor_routes.py` and `test_evaluator_routes.py`
gain new assertions for the preview block (the existing test
inventory verifies the rest of the list page, so the preview block
is an additive set of assertions; no existing test should break).

## 6. Test design

### Unit-level (per-route)

- **Filter coercion**: every filter parameter is validated via the
  existing `_coerce_filter(raw, allowed)`; `_INVALID_FILTER` sentinel
  drives the empty-rowset render. Mirror the 9e tests.
- **Banner allowlist**: list/detail templates only render banners
  for keys in the closed allowlist; unknown keys render no banner.
  Following 9e's `_outcome` pattern, there are no mutating routes
  in this chunk so the allowlist is small (just a `not-found` /
  `transport` pair for the detail page if we add an outcome
  surface; alternatively no banner allowlist at all if the detail
  page is pure read).
- **Auth-first POST**: trivially satisfied because no POST routes.
- **Auth-first GET**: every GET requires session; missing session
  → 303 to `/signin`. Test for each new route.

### Flow-level

- **Executor list preview**: seed two pending execution tasks; one
  references an idea whose `artifacts_uri` resolves inside
  `artifacts_dir`, one references an idea whose `artifacts_uri` is
  `http://example.com/foo` (helper returns `None`). Assert the
  rendered HTML carries both rows; the first row carries the
  content in a `<details>` block; the second renders
  `(content unavailable)`.
- **Evaluator list preview**: same shape, with variant artifacts.
- **Admin task detail lineage** (per kind): seed a full pipeline
  ideation → idea → execution → variant → evaluation; visit each
  task's `/admin/tasks/<id>/` and assert the lineage section
  renders the expected links in the expected order. Then delete
  the upstream idea (raise `StorageNotFound` via monkeypatch)
  and assert the rendered section degrades gracefully.
- **Admin variant lineage**: same shape, variant-side.
- **Admin ideas list + detail**: seed ideas in each state
  (drafting / ready / dispatched / completed); verify the list
  filter matches; verify detail renders all fields + lineage.
- **Reverse-walk correctness**: an idea that's in `dispatched`
  state has a unique parent ideation task; we walk over all
  submissions and find it. Test the case where there are two
  ideation tasks, only one of which produced this idea — assert
  the correct one is found.
- **Reverse-walk performance**: with 20 ideation tasks and 200
  ideas, a detail-page render completes in under 1 s on the test
  fixture. Soft assertion (skip on CI runners that are slow); the
  real bound is "scales linearly with task count" which we assert
  by reading + reasoning, not benchmarking.

### Security

- **Jinja autoescape**: assert that `idea.slug` / `worker_id` /
  `parent_commits` are rendered with escape; inject `<script>` into
  a slug and confirm the rendered HTML carries `&lt;script&gt;`.
  Same posture as 9e and 12a-1b security tests.
- **Detail-URL path-traversal**: FastAPI's path-parameter parser
  rejects `/` in the `idea_id` slot at routing time. Test with a
  request to `/admin/ideas/foo%2Fbar/` and expect a 404 (or 200
  on a different idea — verify the parameter doesn't decode `%2F`
  into a path segment in our handler).
- **`javascript:` URI scheme**: the variant-detail page already
  has a tested scheme allowlist (chunk 9e §A.6); the lineage links
  go to `/admin/<path>/<id>/` so they're always same-origin and
  this concern doesn't recur.

### e2e

`test_admin_ideas_e2e.py` follows
[`test_admin_workers_e2e.py`](../../reference/services/web-ui/tests/test_admin_workers_e2e.py)
exactly:

1. Spawn task-store-server with `--admin-token` enabled.
2. Spawn web-ui with `--admin-token` (so `app.state.admin_store` is
   populated — though this chunk doesn't write through it).
3. Sign in.
4. Pre-seed an `IdeaSubmission` via direct `StoreClient` over the
   wire (admin bearer).
5. GET `/admin/ideas/` and assert the seeded idea appears.
6. GET `/admin/ideas/<id>/` and assert the detail renders, the
   lineage link to the originating ideation task is present, and
   the link resolves to `/admin/tasks/<task_id>/` 200.

## 7. Verification gates

The literal canonical commands from AGENTS.md (the "Commands" table
is the literal pre-push validation gate per the AGENTS.md pitfall
"narrowed subsets are not a substitute"):

| Stage | Command |
| ----- | ------- |
| Pre-impl smoke | `uv sync && uv run ruff check . && uv run pyright` |
| Lint markdown | `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` |
| Naming discipline | `python3 scripts/check-rename-discipline.py` |
| Spec xref | `python3 scripts/spec-xref-check.py` |
| Full pytest | `uv run pytest -q` |
| Compose smoke | `bash reference/compose/healthcheck/smoke.sh` |
| Compose e2e | `bash reference/compose/healthcheck/e2e.sh` |
| Compose subprocess smoke | `bash reference/compose/healthcheck/smoke-subprocess.sh` |

The e2e smoke is the most important gate for this chunk because the
chunk-9e admin module already has e2e coverage we want to preserve.
The compose-e2e job exercises the web UI's ideator walkthrough and
admin-reclaim drill; both pass through `admin_task_detail.html` and
will catch any rendering regression in the new sections.

`uv run pytest -m e2e` runs the new `test_admin_ideas_e2e.py`
alongside the existing e2e suite.

## 8. Tricky areas

### Idea ← ideation-task reverse linkage

See §D.9. The walk relies on the store invariant "for a terminal
ideation task, `read_submission` returns the submission". Tests
should cover the transient case where an ideation task is `claimed`
or `submitted` mid-flight (no submission yet, link absent) — and
the case where the ideator submitted with `status=error` (submission
present, `idea_ids = ()`, so the walk doesn't match this task).
That's not a bug: an ideation task with `status=error` produced no
ideas, so it's correctly absent from any idea's reverse-walk.

### 12a-2 conflict surface

12a-2 plan §6.1 adds a "reassign" section to `admin_task_detail.html`.
This chunk adds an "attribution" + "lineage" section to the same
template. Both are additive — no shared lines beyond the boilerplate
`<section>` wrapper. The merge conflict (if any) is mechanical
section ordering. We pin section order as:

```text
<h1>task <id></h1>
<banner>
<dl> (kind/state/timestamps/claim — unchanged)
<section>attribution</section>          <-- new this chunk
<section>payload</section>              <-- existing, unchanged
<section>lineage</section>              <-- new this chunk
<section>operator reclaim</section>     <-- existing, unchanged (or extended by 12a-2)
<section>reassign</section>             <-- 12a-2 will add this
<section>related events</section>       <-- existing, unchanged
```

Whichever lands second integrates its section into this fixed order.

### Read-error budget on list pages

Each row's `read_idea` / `read_variant` call can fail with
`StorageNotFound` (degraded render: `(idea unavailable)`) or
transport-shaped (banner). We must not let one row's read failure
crash the whole page. The route layer wraps each per-row read in a
try/except — the per-row failure flips `read_failed=True` on that
row and increments a page-level `transport_errors` counter. The
template renders a per-page banner when the counter > 0.

This is the §D.10 N+1 cost. If the count of pending rows ever grew
to triple digits, we'd need a batched-read endpoint on the wire —
out of scope for this chunk; flagged as a §11 follow-up.

### Auto-escape of fields rendered raw inside `<pre>`

The existing template uses `<pre>{{ payload_json | tojson(indent=2) }}</pre>`.
`tojson` JSON-escapes the value, so the `<pre>` is safe. The new
attribution section renders `task.target.id` directly via `<code>`;
Jinja's default autoescape applies. No new escaping discipline.

### Variant-artifact preview on `/evaluator/`

The 9d evaluator draft form already renders the variant artifact
behind the trust-boundary helper. The new evaluator list page does
the same — we deliberately re-use the helper; we do NOT add a new
trust boundary.

### Path-parameter encoding for `idea_id`

FastAPI's path parameter rejects `/` by default (the route is
defined as `/admin/ideas/{idea_id}/`). A request to
`/admin/ideas/foo%2Fbar/` is decoded to `idea_id="foo/bar"` by
Starlette before the handler runs — but `store.read_idea("foo/bar")`
either returns `NotFound` (good) or some other store-domain error.
We rely on the spec's `idea_id` grammar (chapter 02 §1.3 keeps it
opaque, but the orchestrator and ideator both generate ids using
`uuid4().hex[:12]`, so a `/` is in practice impossible). The test
suite asserts a 404 for the encoded-slash case.

## 9. Risks / things to watch

1. **Codex round-0 may flag the N+1 read pattern.** §D.10 explains
   the bound; the answer is "fixture-scale; flagged as a §11
   followup if it ever matters." Don't pre-shrink to batched reads
   in this chunk.

2. **Codex round-0 may suggest persisting `Idea.ideation_task_id`
   as a backref.** §D.9 explains why we don't: it's a spec change to
   chapter 02 §1, requires a schema migration, and the reverse-walk
   is cheap at fixture scale. The plan's posture is "do not
   re-litigate the spec invariant; reconstruct from submissions."

3. **Codex round-0 may flag that the lineage section duplicates
   logic between `task_detail` and `variant_detail`.** That's
   intentional — the `_lineage` helper module owns the logic; both
   routes call it. The template-side rendering is similar but
   distinct (a task's lineage and a variant's lineage are different
   orderings). Don't try to share template macros yet.

4. **12a-2 merge order.** If 12a-2 lands first, this chunk rebases
   onto its `admin_task_detail.html` changes (the new "reassign"
   section). The expected resolution is to insert this chunk's
   sections in the order described in §8 above; no semantic
   conflict, only positional. If this chunk lands first, 12a-2 will
   rebase the same way.

5. **Idea content rendering inside a `<details>` block has UX cost
   for keyboard users**: tab order traverses the disclosure widget
   before content. Plan posture: this is a fix forward, not a
   blocker; the existing executor draft form has the same shape.

## 10. Sequence within the chunk

Roughly four waves; each a single commit.

1. **Wave 1 — lineage helper + tests.** Add `_lineage.py` with
   the dataclasses and `lineage_for_*` functions, plus pure-unit
   tests (no FastAPI involved). Tests run against a fixture store
   directly.

2. **Wave 2 — admin task/variant detail extensions.** Extend
   `admin.py::task_detail` and `admin.py::variant_detail` to
   populate the new sections. Extend `admin_task_detail.html` and
   `admin_variant_detail.html`. Add `test_admin_tasks_lineage.py`
   and `test_admin_variants_lineage.py`.

3. **Wave 3 — executor + evaluator list preview.** Extend
   `executor.py::list_pending` and `evaluator.py::list_pending`,
   plus the two list templates. Add `test_executor_list_preview.py`
   and `test_evaluator_list_preview.py`.

4. **Wave 4 — new admin ideas module + e2e.** Add `admin_ideas.py`,
   `admin_ideas.html`, `admin_idea_detail.html`. Extend
   `admin_index.html`. Wire `make_app`. Add
   `test_admin_ideas_routes.py`, `test_admin_ideas_flow.py`,
   `test_admin_ideas_security.py`, `test_admin_ideas_e2e.py`.

Each wave runs the full canonical command list as a local validation
gate (per the AGENTS.md "narrowed subsets are not a substitute"
pitfall).

## 11. Out of scope (followups)

- **Batched-read wire endpoint** for list pages. Would replace the
  per-row `read_idea` / `read_variant` calls. Only matters at
  triple-digit list scale. Flagged because §D.10 leaves it open;
  not blocking.
- **`Idea.ideation_task_id` persisted backref**. Would simplify
  §D.9 to an O(1) lookup. Requires a spec change to chapter 02 §1
  plus a schema migration + per-backend migration. Not blocking.
- **Mutating routes on `/admin/ideas/`** (delete, edit). The wire
  surface doesn't define them in 12a-1; would require chapter 02
  §1 + chapter 07 §6 additions. Not blocking — the only operator-
  initiated idea creation today is via the ideator module.
- **Streaming list updates** (HTMX poll + diff). Out of scope for
  the read-only transparency surface; the admin module's existing
  pages don't stream either.
- **Inline-render of variants inside task detail** (and ideas
  inside variant detail). Section §2 decision 4 pins this as
  "always plain links, never embedded sub-render"; reconsidering
  is a separate chunk.
- **Group-membership-based RBAC on `/admin/ideas/`**. Same posture
  as 12a-1b: every signed-in session is implicitly an operator.
  Group-based RBAC lands with 12a-2's `admins` group surface.

## 12. Estimated effort

- Plan + codex-converge: ~1 day (4-7 codex rounds expected).
- Impl + codex-converge: ~2 days (3-5 codex rounds expected; smaller
  scope than 12a-1b, no auth-bearer plumbing).
- Total: ~3 days end-to-end.
