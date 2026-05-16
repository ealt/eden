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
   section linking back to the producing execution task (one hop)
   and the parent idea (one hop; the existing "parent idea" section
   becomes the lineage link), and forward to evaluation tasks
   against this variant (plural). The ideation task is NOT linked
   directly — it's one hop further and reachable via the idea page,
   per the decision-4 one-hop rule (§2).

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

4. **Lineage navigation surfaces only direct neighbors of the
   subject page — one hop in either direction. No embedded
   sub-rendering, no transitive walking.** Each lineage entry is
   a hyperlink with a short one-line descriptor (no nested
   sub-lists). Concretely:

   - The **ideation-task** page lists the ideas its submission
     produced (one hop forward); it does NOT additionally list the
     variants spawned from each of those ideas (that's two hops,
     reachable by clicking through to each idea).
   - The **idea** page lists the ideation task it came from (one
     hop back) and the variants spawned from it (one hop forward);
     it does NOT list those variants' evaluations.
   - The **execution-task** page lists the referenced idea (one hop
     back) and every variant whose `idea_id` matches (one hop
     forward; a single execution task may produce multiple variants
     over reclaims). It does NOT walk back to the ideation task.
   - The **variant** page lists the execution task that produced
     it (one hop back), its parent idea (one hop back, also already
     in the existing "parent idea" section), and the evaluation
     tasks against it (one hop forward).
   - The **evaluation-task** page lists the variant it targets
     (one hop back). It does NOT walk back to the idea or
     ideation task.

   Cardinality at each junction is plural where the data model
   allows it (an ideation submission has many idea_ids; an idea
   may spawn multiple variants if a prior execution task was
   reclaimed; a variant may have multiple evaluation tasks over
   its life). The lineage helper's per-page view model is
   structured to carry these collections explicitly (§D.3).

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

### D.3 Lineage helper module — per-page view models

New helper module:
[`routes/_lineage.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_lineage.py).
Exposes pure functions (no I/O of its own beyond the `Store` calls
they make on the caller's behalf) so the same logic can be tested
unit-style against a fixture `Store`.

The data model from codex round 0 changed: a single `Lineage` struct
cannot carry the fan-out the different pages need. Instead, **five
per-page view models** — one per subject artifact kind — each
carrying exactly the collections decision-4 (§2) allows for that
page:

```python
@dataclass(frozen=True, slots=True)
class LineageLink:
    label: str         # short human label, e.g. "ideation task"
    href: str          # /admin/<...>/<id>/
    descriptor: str    # one-line context, e.g. "slug=foo, status=success"

@dataclass(frozen=True, slots=True)
class IdeationTaskLineage:
    """Subject: an ideation task.

    Forward (one hop): the ideas the ideator submitted.
    No backward link (an ideation task has no upstream artifact).
    """
    ideas: tuple[LineageLink, ...] = ()           # may be empty if the
                                                  # submission was status=error,
                                                  # or the task is pre-submit
    transport_errors: int = 0

@dataclass(frozen=True, slots=True)
class ExecutionTaskLineage:
    """Subject: an execution task.

    Backward (one hop): the referenced idea (single).
    Forward (one hop): every variant whose ``idea_id`` matches
        this task's ``payload.idea_id`` — plural because a reclaim
        can produce a second variant against the same idea.
    """
    idea: LineageLink | None = None
    variants: tuple[LineageLink, ...] = ()
    transport_errors: int = 0

@dataclass(frozen=True, slots=True)
class EvaluationTaskLineage:
    """Subject: an evaluation task.

    Backward (one hop): the referenced variant (single).
    No forward link (the evaluation task is a leaf in the chain).
    """
    variant: LineageLink | None = None
    transport_errors: int = 0

@dataclass(frozen=True, slots=True)
class IdeaLineage:
    """Subject: an idea.

    Backward (one hop): the ideation task that produced it
        (singular; reverse-walked per §D.9). May be ``None`` if
        the originating task is not in a submitted/terminal state.
    Forward (one hop): variants spawned from this idea (plural).
    """
    ideation_task: LineageLink | None = None
    variants: tuple[LineageLink, ...] = ()
    transport_errors: int = 0

@dataclass(frozen=True, slots=True)
class VariantLineage:
    """Subject: a variant.

    Backward (one hop): the execution task that produced this
        variant (singular; disambiguated by the §D.9
        ``_producing_execution_task`` rule — exact submission match
        first; unambiguous attribution match as a fallback;
        otherwise ``None`` rather than guessing). Multiple
        candidate execution tasks against the same idea are
        possible if a prior reclaim produced another variant, but
        *this* variant has at most one producing execution task.
    Backward (one hop): the parent idea (singular; already in
        the existing "parent idea" section — we just hyperlink it).
    Forward (one hop): evaluation tasks whose ``payload.variant_id``
        matches (plural).
    """
    execution_task: LineageLink | None = None
    idea: LineageLink | None = None
    evaluation_tasks: tuple[LineageLink, ...] = ()
    transport_errors: int = 0
```

The five callable helpers:

```python
def lineage_for_ideation_task(store: Store, task: IdeationTask) -> IdeationTaskLineage: ...
def lineage_for_execution_task(store: Store, task: ExecutionTask) -> ExecutionTaskLineage: ...
def lineage_for_evaluation_task(store: Store, task: EvaluationTask) -> EvaluationTaskLineage: ...
def lineage_for_idea(store: Store, idea: Idea) -> IdeaLineage: ...
def lineage_for_variant(store: Store, variant: Variant) -> VariantLineage: ...
```

The `admin.py::task_detail` route dispatches on `task.kind` to call
the right per-kind helper and passes the result into the template
under a single context key `lineage` (the template's `{% if %}` arms
are keyed off attribute presence, not type discrimination).

Each per-page view model carries its own `transport_errors`
counter so a template-side banner can render "lineage may be
incomplete — N transport errors" without losing the partial render.
Per-link transport failures during the lineage build increment the
view model's counter and the corresponding `LineageLink` slot is
left `None` (or that entry is dropped from the collection); a read
that succeeded with `None` (e.g., "this variant has no evaluations
yet") is the empty tuple, not a transport error.

Tuples are ordered:

- `ideas` (in an `IdeationTaskLineage`): order matches the
  submission's `idea_ids` tuple (creation order, deterministic).
- `variants` (in `ExecutionTaskLineage` / `IdeaLineage`): ordered by
  `variant.started_at`, oldest first.
- `evaluation_tasks` (in `VariantLineage`): ordered by
  `task.created_at`, oldest first.

A hard cap of `_LINEAGE_COLLECTION_CAP = 20` applies per tuple; the
template renders "(showing N of M)" when capped. This matches the
chunk-9e `_TRIAL_DETAIL_EVENT_CAP = 50` discipline (smaller cap here
because lineage entries are denser than event rows).

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
- **`lineage`** (always rendered): the **one-hop** linkage for this
  task's kind — direct neighbors only (§2 decision 4). The route
  dispatches on `task.kind`:
  - `kind=ideation` → `lineage_for_ideation_task(store, task)` →
    `IdeationTaskLineage.ideas` (the ideas this submission
    produced; reachable via `store.read_submission(task.task_id)`).
    For pre-submit ideation tasks (state in {`pending`, `claimed`})
    or status=error submissions, this is the empty tuple. We do
    NOT additionally list each idea's spawned variants — that's
    two hops.
  - `kind=execution` → `lineage_for_execution_task(store, task)` →
    `ExecutionTaskLineage.idea` (one back) + `.variants` (one
    forward; plural because a reclaim history can produce more
    than one variant against the same idea). We do NOT walk back
    to the ideation task — that's two hops (operator reaches it
    via the idea page).
  - `kind=evaluation` → `lineage_for_evaluation_task(store, task)`
    → `EvaluationTaskLineage.variant` (one back). We do NOT walk
    back further (variant page exposes its own one-hop neighbors).

The existing `payload` + `related events` sections are preserved
unchanged. The "operator reclaim" section is preserved unchanged.

If any underlying read fails, the link renders as `(unknown — read
error)` and `transport_errors` is incremented; the template renders
a section-scoped banner when `transport_errors > 0`.

### D.7 `/admin/variants/<variant_id>/` — lineage section

[`routes/admin.py::variant_detail`](../../reference/services/web-ui/src/eden_web_ui/routes/admin.py)
gains a lineage section populated via `lineage_for_variant(store,
variant) → VariantLineage`. One hop each direction:

- backward: the execution task that produced this variant
  (`VariantLineage.execution_task`, reverse-walked per §D.9), and
  the parent idea (already loaded into the existing "parent idea"
  section — we just hyperlink the slug there to
  `/admin/ideas/<idea_id>/`, which doubles as the lineage link).
- forward: evaluation tasks whose `payload.variant_id` matches
  (`VariantLineage.evaluation_tasks`, plural, capped at 20).

We do NOT walk back to the ideation task — that's two hops away
and reachable through the idea page (which has its own one-hop
ideation-task link).

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
- Lineage section: populated via `lineage_for_idea(store, idea) →
  IdeaLineage`. One hop back to the ideation task (reverse-walked
  per §D.9; may be `None` if the idea's originating ideation task
  is pre-submit). One hop forward to variants spawned from this
  idea (`IdeaLineage.variants`, plural, capped at 20). We do NOT
  walk forward to variant evaluations — operator reaches those via
  the variant page.

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

The execution-task-for-variant reverse lookup is structurally similar
but has an extra precision step: multiple execution tasks against the
same idea can exist if a prior execution was reclaimed. To pick the
right *producing* execution task for a given variant, we walk
execution tasks whose `payload.idea_id == variant.idea_id` AND whose
`task.submitted_by == variant.executed_by` (when both are set), then
prefer the one whose submission's `VariantSubmission.variant_id`
matches `variant.variant_id` (when the submission is readable). When
none match — or when attribution fields are unset in an auth-disabled
deployment — we return `None` and the lineage renders
"(execution task: unknown)". Concretely:

```python
def _producing_execution_task(
    store: Store, variant: Variant, *, lineage: VariantLineage
) -> str | None:
    """Resolve the producing execution task; mutate ``lineage.transport_errors``
    on any transient read failure during disambiguation. Returns the
    matching ``task_id`` or ``None`` (definitively unknown).
    """
    try:
        candidates: list[ExecutionTask] = [
            t for t in store.list_tasks(kind="execution")
            if t.payload.idea_id == variant.idea_id
        ]
    except Exception:  # transport-shaped
        lineage = lineage._replace_transport_errors(lineage.transport_errors + 1)
        return None
    # Prefer the submission whose variant_id matches.
    for t in candidates:
        if t.state not in {"submitted", "completed", "failed"}:
            continue
        try:
            sub = store.read_submission(t.task_id)
        except Exception:  # transport-shaped
            # Mirror the ideation reverse walk: increment and continue
            # so we still try the remaining candidates instead of
            # short-circuiting.
            lineage = lineage._replace_transport_errors(lineage.transport_errors + 1)
            continue
        if isinstance(sub, VariantSubmission) and sub.variant_id == variant.variant_id:
            return t.task_id
    # Fallback: attribution match alone. We accept this fallback
    # ONLY when it is unambiguous — i.e. exactly one candidate task
    # has matching attribution. If two or more execution tasks
    # against the same idea share `submitted_by == variant.executed_by`
    # (a re-claim scenario where the same worker drove both
    # attempts and the submission record can't be read on at least
    # one), we cannot disambiguate without guessing; return None
    # and let the lineage section render "(execution task: unknown)"
    # rather than silently linking to an arbitrary one.
    if variant.executed_by is not None:
        attr_matches = [
            t for t in candidates if t.submitted_by == variant.executed_by
        ]
        if len(attr_matches) == 1:
            return attr_matches[0].task_id
    return None
```

(`lineage._replace_transport_errors` is shorthand — `VariantLineage`
is a frozen dataclass; the actual impl will pass a mutable
"accumulator" struct through the helper or have the caller
re-construct the view model with an updated counter. The pattern
is the same whether implemented as a mutable accumulator or as
return-value threading — both are tested-against directly.)

The bound is O(N_execution) per render, same complexity class as the
ideation-task reverse-walk above. A single common helper
`_tasks_referencing_idea(store, idea_id, kind)` consolidates the
"walk tasks whose payload references this idea" mechanic; the
disambiguation logic is variant-specific and stays in
`_producing_execution_task`.

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
- Every collection on a per-page view model (`ideas` on
  `IdeationTaskLineage`, `variants` on `ExecutionTaskLineage` /
  `IdeaLineage`, `evaluation_tasks` on `VariantLineage`) shares the
  single hard cap `_LINEAGE_COLLECTION_CAP = 20` from §D.3 (same
  shape as the chunk-9e `_TRIAL_DETAIL_EVENT_CAP`, smaller cap
  because lineage rows are denser than event rows); the rendered
  template shows "(showing N of M)" when capped.

## 4. Scope

### In scope

- New routes module `admin_ideas.py`.
- New helper module `_lineage.py` with five per-page view model
  dataclasses (`IdeationTaskLineage`, `ExecutionTaskLineage`,
  `EvaluationTaskLineage`, `IdeaLineage`, `VariantLineage`) +
  matching `lineage_for_<kind>` functions (§D.3).
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
- `src/eden_web_ui/routes/admin.py` — extend `task_detail` (dispatch
  on `task.kind` to call `lineage_for_ideation_task` /
  `_execution_task` / `_evaluation_task`) and `variant_detail` (call
  `lineage_for_variant`).
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

The relevant validation commands for this chunk, drawn from the
canonical "Commands" table in AGENTS.md (the pitfall "narrowed subsets
are not a substitute" still applies — pre-push runs the *full*
AGENTS.md table, this list is a per-chunk recap of the gates that
actually exercise this chunk's surface):

| Stage | Command |
| ----- | ------- |
| Pre-impl smoke | `uv sync && uv run ruff check . && uv run pyright` |
| Lint markdown | `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` |
| Naming discipline | `python3 scripts/check-rename-discipline.py` |
| Spec xref | `python3 scripts/spec-xref-check.py` |
| Full pytest | `uv run pytest -q` |
| Docker integration tests | `uv run pytest -q -m docker` (gated on a reachable docker daemon; skips otherwise) |
| Compose smoke | `bash reference/compose/healthcheck/smoke.sh` |
| Compose e2e | `bash reference/compose/healthcheck/e2e.sh` |
| Compose subprocess smoke | `bash reference/compose/healthcheck/smoke-subprocess.sh` |
| Compose subprocess-docker smoke | `bash reference/compose/healthcheck/smoke-subprocess-docker.sh` |

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
