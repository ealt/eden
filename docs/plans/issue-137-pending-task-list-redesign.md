# Issue #137 — Redesign pending-task lists (executor + evaluator)

> Plan-stage doc for [#137](https://github.com/ealt/eden/issues/137). Web-ui-module-only
> change. No spec / wire / schema impact. Operator reviews + approves before impl spawn.

## 1. Context

The executor and evaluator pending-task list pages
([`executor_list.html`](../../reference/services/web-ui/src/eden_web_ui/templates/executor_list.html),
[`evaluator_list.html`](../../reference/services/web-ui/src/eden_web_ui/templates/evaluator_list.html))
surface every pending task with low-signal columns (`task_id` prominent; the high-signal
`slug` buried in small text inside an `idea` cell), no sort, no filter, no grouping, and an
inline `<details>` content-preview block per row. The default store ordering is
`ORDER BY task_id` — alphabetic over random hex, effectively random.

For an operator deciding *which pending task to claim*, the real signal is: **slug** (what is
this about), **priority** (how promising), **target** (am I eligible), **created_by**
(provenance). Everything else is cross-reference data — one click away, not crowding the table.

This plan redesigns both lists to a high-signal 5-column table with priority-default sort,
eligibility/target filters, optional group-by-creator, an eligibility-aware claim button, and a
click-to-expand context-links row (replacing the inline preview). The design follows the
**refined shape in the issue's [design-refinement comment](https://github.com/ealt/eden/issues/137#issuecomment-4523177196)**
(context-links-only expansion, claim stays in main row, no inline preview), which supersedes the
preview/`<details>` shape in the original issue body.

### 1.1 Two premises in the issue body that are now stale — verified against source

The issue was filed before two related changes landed. **Both were checked against the current
tree during planning; the plan scopes around them rather than re-implementing fixed behavior.**

1. **"Clicking claim on an ineligible task … propagates as HTTP 500. The route's existing
   `try/except (IllegalTransition, InvalidPrecondition)` does NOT catch `WorkerNotEligible`."**
   — **Stale.** `WorkerNotEligible` and `WorkerNotRegistered` both subclass `StorageError`
   ([`reference/packages/eden-storage/src/eden_storage/errors.py:43-60`](../../reference/packages/eden-storage/src/eden_storage/errors.py)),
   and **both claim handlers already catch `StorageError`** and route it through
   `wire_error_banner`
   ([executor.py:471-475](../../reference/services/web-ui/src/eden_web_ui/routes/executor.py),
   [evaluator.py:217-221](../../reference/services/web-ui/src/eden_web_ui/routes/evaluator.py)).
   `wire_error_banner`'s `WIRE_ERROR_NAMES` map already includes both
   ([`_submit_readback.py:59-73`](../../reference/services/web-ui/src/eden_web_ui/routes/_submit_readback.py)).
   So the server-side "graceful fallback" prong is **already satisfied** — an ineligible claim
   already renders the banner-redirect, not a 500. The impl's server-side work is therefore
   **a regression test that locks this in**, not a code change. (This is the same broadening
   that resolved [#134](https://github.com/ealt/eden/issues/134), now closed.)

2. **Slug-prominence ([#135](https://github.com/ealt/eden/issues/135)) and one-click artifact
   navigation ([#138](https://github.com/ealt/eden/issues/138)) are CLOSED.** The "view content"
   link the expansion relies on (#138) already exists; #135's slug-in-text fix becomes a no-op
   under this redesign (slug becomes its own column). [#132](https://github.com/ealt/eden/issues/132)
   (artifact path divergence) is also closed.

3. **[#128](https://github.com/ealt/eden/issues/128)** (id/name disambiguation) is **OPEN**.
   The `created_by` column ships showing `worker_id` now; when #128 lands it evolves to
   `worker_name` with `worker_id` as hover detail. The two are independent — #137 does not block
   on #128 and #128 does not block on #137. (See §10 sequencing.)

## 2. Decisions captured before drafting

Surface these at plan review; they are load-bearing and not re-litigated in codex-review unless a
contradiction with a spec MUST or an existing route contract surfaces.

1. **Web-ui-module-only.** No spec chapter, JSON schema, Pydantic model, wire binding, or store
   query changes. Sorting and eligibility filtering happen **in the route handler in Python**
   over the already-fetched pending list — the store's `ORDER BY task_id` is left untouched
   (the manual-UI pending list is small; an in-Python re-sort is correct and avoids a wire/schema
   change). This is explicitly *not* a fix to `postgres.py:644-658` ordering.

2. **List-page state lives in query params, not the session cookie.** The issue says "persist
   sort choice in the session." The `Session` is a *signed cookie*
   ([`sessions.py:24-36`](../../reference/services/web-ui/src/eden_web_ui/sessions.py)) carrying
   `worker_id` / `csrf` / `selected_experiment_id`; mutating it per-navigation means re-issuing
   the cookie on every sort click. **Recommendation: drive all list state (sort key, sort
   direction, filter, group toggle) from URL query params** — stateless, shareable, bookmarkable,
   and consistent with the existing `?banner=` pattern. Sort/filter chips and column headers are
   plain links that set query params. Cookie-backed persistence-across-sessions is deferred (see
   §11); flag at plan review if the operator wants true cross-visit persistence in v1.

3. **Eligibility resolution is wire-available but costs round-trips for group targets.**
   `store.resolve_worker_in_group(worker_id, group_id)` is a `Store`-protocol method
   ([`protocol.py:545`](../../reference/packages/eden-storage/src/eden_storage/protocol.py))
   that `StoreClient` implements by walking the group DAG over HTTP via repeated `read_group`
   calls ([`client.py:692`](../../reference/packages/eden-wire/src/eden_wire/client.py)). Null
   targets and worker targets are resolved with zero wire calls; only group-targeted rows pay.
   The row-builder **memoizes group-resolution by `group_id` within a single render** so N
   group-targeted tasks against the same group cost one DAG walk, not N.

4. **Symmetry is mandatory.** Executor and evaluator lists get the identical redesign. The
   ideator list is out of scope (no task-target; one row per ideation task; already uncrowded) —
   per the issue, "unless trivially analogous," which it is not.

5. **No backwards-compat shims.** Per CLAUDE.md's pre-user posture, the old column shape and the
   inline `<details>` preview are deleted outright — no feature flag, no dual-render path.

## 3. Design

### D.1 Column shape (both lists)

Default collapsed row — 5 columns + expand affordance:

| Column | Source (executor) | Source (evaluator) | Behavior |
|---|---|---|---|
| **slug** | `idea.slug` | `idea.slug` (via `variant.idea_id`) | prominent; sortable (header is the sort link). Expansion is via the dedicated ▸ toggle, not the slug cell |
| **priority** | `idea.priority` | `idea.priority` | sortable; **default sort key DESC** |
| **target** | `task.target` | `task.target` | filterable; renders `any` / `worker:<id>` / `group:<id>` |
| **created by** | `idea.created_by` | `idea.created_by` | filterable + groupable |
| **claim** | n/a | n/a | eligibility-aware button (see D.4) |
| ▸ | n/a | n/a | expand toggle (see D.3) |

The evaluator list additionally tracks `variant` (the artifact under evaluation) — that surfaces
in the **expansion**, not a top-level column, to keep the two tables visually identical.

Degraded rows (idea read failed / idea unavailable / variant unavailable) keep the existing
graceful-render behavior from the current row-builders
([executor.py:411-452](../../reference/services/web-ui/src/eden_web_ui/routes/executor.py),
[evaluator.py:79-142](../../reference/services/web-ui/src/eden_web_ui/routes/evaluator.py)): when
`idea is None`, the slug/priority/created_by cells render `—` / `(idea unavailable)`. Degraded
rows always sort to the bottom **regardless of direction** — achieved by partitioning, not a
sentinel value (see §D.2). The page-level read-failure warning banner is preserved (§D.6).

### D.2 Sort

- Default: `(idea.priority DESC, task.created_at ASC)` — highest-priority first, ties broken by
  oldest first.
- **Sortable columns: `slug` and `priority` only.** Their headers are links that set
  `?sort=<key>&dir=<asc|desc>`; clicking the active column flips direction. `created_at` is **not**
  a user-selectable sort axis — it is always the implicit secondary tiebreak under every primary
  sort. `created_by` is filterable/groupable (§D.4), **not** sortable. This removes the
  column-header-vs-allow-list ambiguity flagged in plan-review round 0.
- Allowed `sort` values are an explicit allow-list (`priority`, `slug`); unknown or absent → the
  default. No user input reaches a comparator unchecked (also closes the reflected-`?sort=` href
  injection surface).
- **Direction-safe degraded-row placement.** Sorting partitions rows into *present* (idea
  resolved) and *degraded* (idea unavailable / read-failed). Only present rows go through the
  comparator (keyed on the chosen axis + the `created_at` tiebreak, with `dir` applied); degraded
  rows are concatenated **after**, in stable original order, for both `asc` and `desc`. No
  `-inf`/`""` sentinel — sentinels move degraded rows to the top under ascending sorts, which is
  why partitioning is used instead.

### D.3 Click-to-expand context row (replaces inline preview)

The `▸`/`▾` affordance toggles a `<details>` block spanning the row (same primitive the current
preview uses — repurposed, not added). **No inline content preview.** The expansion is
navigation-only.

**Scoped to surfaces the web-ui module already exposes** (verified during round-0 revision — only
`/admin/tasks/{id}/` and `/admin/workers/{id}/` are per-id *detail* pages today; ideas/variants
have *index* pages only; artifacts are served by `GET /artifacts`). The links are:

- **task** → `/admin/tasks/<task_id>/` (existing `lineage_link`, per-id detail page).
- **creator** → `/admin/workers/<created_by>/` (existing per-id detail page).
- **idea** → display `idea_id` (copyable), linked to the admin ideas index
  `/admin/ideas/` (no per-id idea-detail page exists today). The bulk of idea context is reachable
  from the task detail page already.
- **variant** (evaluator only) → display `variant_id` + `variant.branch`, linked to the admin
  variants index `/admin/variants/`.
- **artifacts** → **artifact-shape-aware** per-entry "view content" links. The row-builder reads
  the artifact *manifest* (executor:
  [`_read_artifact_manifest`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py);
  evaluator: `read_variant_artifact_manifest`) and emits one `GET /artifacts?...` link per actual
  manifest entry. **No hardcoded `idea.md`** — the artifact model allows direct-file and
  upload-only bundles with no guaranteed single-file name; if the manifest is empty or unreadable,
  render "(no artifacts)" / "(artifacts unavailable)".

**Deliberately deferred (not in this issue):** a browser-facing Forgejo *browse* URL for the
parent commit / work branch. The current CLI exposes only an in-network `--forgejo-url` and an
informational `--clone-url` ([`cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py)) —
there is **no browser browse-URL contract**, and adding one would widen scope past web-ui-only
(§2.1). The parent-commit SHA is shown as text in the expansion for cross-reference; linking it to
a repo browser is a §11 followup. This was the round-0 over-reach: the original draft promised
Forgejo browse links that no surface backs.

Multiple rows may be expanded simultaneously (no accordion exclusivity — native `<details>`
gives this for free). No JS required for the baseline.

### D.4 Filter + group-by-creator

Filter chips above the table, all driven by query params:

- **"Eligible for me"** (`?eligible=1`, **default ON**) — show only tasks the session's worker can
  claim. The predicate mirrors the **full** §3.5 claim ladder
  ([`spec/v0/04-task-protocol.md`](../../spec/v0/04-task-protocol.md) §3.5), which checks
  *registration first, then target* — not target-match alone:

  ```text
  eligible(task) := registered(session.worker_id)
                    AND ( task.target is None
                          OR (target.kind=="worker" AND target.id==session.worker_id)
                          OR (target.kind=="group"  AND resolve_worker_in_group(session.worker_id, target.id)) )
  ```

  **Registration is resolved once per render**, not per row: the handler calls
  `store.read_worker(session.worker_id)` once and caches the boolean. If the session worker is not
  registered, *every* row is ineligible (matching the ladder's step-2 `WorkerNotRegistered`).
  (In practice a signed-in worker is registered, but the projection must not assume it — the
  ladder doesn't.) Group resolution is memoized by `group_id` across rows (§2.3).
- **Error model (corrected in round-0 revision).** `StoreClient.resolve_worker_in_group` does
  **not** raise on an unknown worker/group — it `read_worker`s first and returns `False` on
  `NotFound`, and a dangling group ref is skipped; only **transport / auth errors propagate**
  ([`client.py:692`](../../reference/packages/eden-wire/src/eden_wire/client.py)). So the
  row-builder catches only the *transport-shaped* exception around the eligibility resolution and
  treats it as **"eligibility unknown"** — render the row with a disabled claim button + an
  "eligibility unknown" note, and increment a **separate** eligibility-resolution warning counter
  (§D.6). It does **not** catch `NotFound` (that is a legitimate `False`, not an error), and it
  must not collapse a real transport outage into "ineligible-and-hidden".
- When the filter is OFF (`?eligible=0`), all pending tasks show regardless of eligibility, and
  ineligible rows get a **disabled** claim button with a tooltip (D.5).
- **Target tri-state** (`?target=all|targeted|untargeted`, default `all`) — both / only tasks with
  a target / only free-pool (null-target) tasks.
- **Group by creator** (`?group=1`, default OFF) — wraps rows in a `<details>` per unique
  `idea.created_by`, each group independently present. Implementation: group the already-built +
  sorted rows by `created_by` in the handler; the template iterates groups.

Each row carries a computed `eligible: bool` flag set by the row-builder (so the template doesn't
re-resolve). The "eligible for me" filter and the per-row `eligible` flag share one resolution
pass.

### D.5 Eligibility-aware claim button

- **Pre-filter (default).** With "Eligible for me" ON, every visible row is claimable; the button
  is a normal submit.
- **Graceful fallback (filter OFF).** Ineligible rows render the claim button **disabled**
  (`<button disabled title="This task is targeted at <target>; you are not in its target.">`).
- **Server-side.** Already correct (§1.1.1) — the claim route catches `StorageError` (covering
  `WorkerNotEligible` / `WorkerNotRegistered`) and renders the banner-redirect. The impl adds a
  **regression test** asserting an ineligible POST returns a 303 banner-redirect, not a 500. No
  handler code change.

### D.6 Warning banners — content-read vs eligibility-resolution are separate counters

The current pages show one warning counter — "N idea read(s) failed" (executor) / "N variant/idea
read(s) failed" (evaluator)
([executor_list.html:6](../../reference/services/web-ui/src/eden_web_ui/templates/executor_list.html),
[evaluator_list.html:6](../../reference/services/web-ui/src/eden_web_ui/templates/evaluator_list.html)).
This redesign adds a **second** failure mode (eligibility-resolution transport failure, §D.4). Do
**not** fold it into the existing read-failure counter — the copy would lie ("idea read failed"
when the idea read fine but the *eligibility* probe timed out). Keep two counters with two banner
lines: the existing read-failure warning (unchanged copy) and a new
"N task eligibility check(s) could not be resolved; those rows are shown as not-claimable —
refresh to retry." Each increments independently in the row-builder.

## 4. Scope

**In scope:**

- Redesign [`executor_list.html`](../../reference/services/web-ui/src/eden_web_ui/templates/executor_list.html)
  and [`evaluator_list.html`](../../reference/services/web-ui/src/eden_web_ui/templates/evaluator_list.html):
  5-column table, sort-header links, filter chips, group-by-creator, eligibility-aware claim
  button + tooltip, click-to-expand context row (delete inline preview).
- Route-handler changes in
  [`routes/executor.py`](../../reference/services/web-ui/src/eden_web_ui/routes/executor.py)
  (`list_pending`, `_build_executor_pending_rows`) and
  [`routes/evaluator.py`](../../reference/services/web-ui/src/eden_web_ui/routes/evaluator.py)
  (`list_pending`, `_build_evaluator_pending_rows`): parse `sort`/`dir`/`eligible`/`target`/`group`
  query params (allow-listed); resolve per-row eligibility (memoized group resolution); attach
  `eligible` flag; sort + optionally group in Python.
- A shared helper (in
  [`routes/_helpers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py)) for
  eligibility resolution with per-render group memoization, used by both row-builders.
- CSS for chips / disabled-button / expand affordance in
  [`static/style.css`](../../reference/services/web-ui/src/eden_web_ui/static/style.css).
- Per-route tests (sort, filter tri-state, group toggle, eligibility flag + disabled button,
  ineligible-claim regression) and one e2e test driving the ineligible-claim path asserting a
  clean banner (no 500).
- Docs: `eden-manual-executor` / `eden-manual-evaluator` SKILL.md walkthrough updates;
  `docs/user-guide.md` §6/§7 list-page UX note; CHANGELOG `[Unreleased]` + roadmap flip; review
  record under `docs/plans/review/issue-137/`.

**Out of scope (deferred / non-goals — each filed or already tracked):**

- Spec / JSON-schema / Pydantic / wire-binding changes — none needed (§5).
- Store query ordering change (`postgres.py` / `sqlite.py` `ORDER BY task_id`) — left as-is by
  decision §2.1.
- Ideator list redesign — explicitly out per the issue.
- Saved query / filter presets ("show me only tasks from $worker") — deferred (§11).
- Pagination for very large pending lists — deferred (§11).
- Cookie-backed cross-visit persistence of sort/filter — deferred (§11); query params only in v1.
- Drag-to-claim / other interactive UX — out.
- Inline content preview — **removed**, replaced by expansion "view content" links (#138, landed).
- #128 naming evolution of the `created_by` column — independent; #137 ships `worker_id`.

## 5. Spec / contract impact

**None.** This is verified, not assumed:

- The columns (`slug`, `priority`, `target`, `created_by`) are all existing canonical glossary
  terms ([`docs/glossary.md`](../glossary.md) lines 96-97, 236, 238) already present on `Idea` /
  `Task`. No new data-model field.
- No wire endpoint is added: `resolve_worker_in_group` is an existing `Store`-protocol method with
  an existing `StoreClient` implementation; `list_tasks` is already used by both routes. The list
  page reads only data the chapter-7 binding already exposes.
- No JSON schema or Pydantic model is touched; `schema-parity` CI is unaffected.
- The eligibility predicate mirrors the store's §3.5 RBAC ladder
  ([`spec/v0/04-task-protocol.md`](../../spec/v0/04-task-protocol.md) §3.5) but does **not**
  re-specify it — the store remains authoritative; the UI pre-filter is an advisory projection
  and the claim write is still the enforcement point.

If codex-review surfaces a reason the eligibility projection must be normatively pinned, that is a
scope-escalation to flag — the default is no spec touch.

## 6. Naming map

No protocol vocabulary changes. New identifiers are local route/query/template names; validated
against [`docs/glossary.md`](../glossary.md) (artifact-noun discipline; gerund task kinds):

| Kind | New identifier | Rationale / glossary check |
|---|---|---|
| query param | `sort` ∈ {`priority`,`slug`} | the two sortable columns; `created_at` is the implicit always-on secondary tiebreak, not a selectable axis (§D.2) |
| query param | `dir` ∈ {`asc`,`desc`} | sort direction |
| query param | `eligible` ∈ {`0`,`1`} | matches the "Eligible for me" chip |
| query param | `target` ∈ {`all`,`targeted`,`untargeted`} | tri-state target filter |
| query param | `group` ∈ {`0`,`1`} | group-by-creator toggle |
| row dict key | `eligible: bool` | per-row claimability flag |
| helper fn | `resolve_eligibility(store, worker_id, target, *, group_cache)` | artifact-neutral; in `_helpers.py` |

Old → removed (no rename, deletion per §2.5): the `idea_content` row key + the `<details><summary>preview</summary>`
block; the `task id` / `idea` / `created` columns as currently shaped.

## 7. Conformance impact

**None.** Conformance asserts only the chapter-7 HTTP binding
([`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §6); the web-ui list page is not an
IUT surface. No `§`-reference, scenario, or `check_citations` entry changes. The conformance suite
runs unchanged as a regression gate (§8 verification).

## 8. Chunked execution plan

Single impl PR is feasible (one module, ~medium), but the work is staged into waves with
per-wave validation gates so a mid-PR checkpoint is meaningful. Executor and evaluator are done
**together within each wave** (symmetry is easier to keep correct when the two are edited side by
side than when one list lands a wave ahead).

### Wave 1 — Route-handler data layer (no template change yet)

- Add `resolve_eligibility(...)` to `_helpers.py` with per-render group-resolution memoization.
- Extend `_build_executor_pending_rows` / `_build_evaluator_pending_rows` to attach `eligible`
  and to expose `slug` / `priority` / `created_by` / `target` / context-link fields cleanly.
- Parse + allow-list `sort`/`dir`/`eligible`/`target`/`group` in both `list_pending`; apply
  in-Python sort + filter + optional grouping; pass structured context to the template (templates
  unchanged this wave — assert via the route's returned context in tests).
- **Gate:** `uv run ruff check . && uv run pyright && uv run pytest -q`
  (esp. `reference/services/web-ui/tests/test_executor_routes.py`,
  `test_evaluator_routes.py`); `python3 scripts/check-rename-discipline.py`;
  `python3 scripts/check-complexity.py` (watch `list_pending` CC — extract the param-parse +
  sort/filter into helpers if it crosses the gate).

### Wave 2 — Template redesign + CSS

- Rewrite both `*_list.html`: 5-column table, sort-header links, filter chips, group-by-creator
  `<details>`, eligibility-aware claim button + disabled tooltip, click-to-expand context row;
  delete the inline preview block.
- Add chip / disabled-button / expand-affordance CSS to `static/style.css`.
- **Gate:** route-render tests assert the new columns, the sort-link hrefs, the tri-state filter,
  the group toggle, the `disabled` attribute + tooltip on ineligible rows, and that the inline
  preview is gone. `markdownlint` n/a (HTML). Re-run `pytest -q`.

### Wave 3 — Server-side regression + e2e

- Add a regression test: ineligible-worker POST to `/executor/<id>/claim` (and evaluator) returns
  303 banner-redirect with the `worker-not-eligible` banner, **not** 500 (locks in §1.1.1; no
  handler change).
- Add one e2e test driving the "claim ineligible task with filter OFF" path through the rendered
  page asserting the banner renders cleanly. Follow the multi-subprocess log-drain discipline in
  AGENTS.md (file-redirect, not undrained `PIPE`) if it spawns the stack.
- **Gate:** `uv run pytest -q` full suite + `uv run pytest -q conformance/ -n auto` (regression
  only — no conformance change expected). If the e2e drives the Compose stack, run the relevant
  smoke per AGENTS.md "literal validation gate" rule.

### Wave 4 — Docs + completion record (final "docs PR" wave)

- Update `eden-manual-executor` / `eden-manual-evaluator` SKILL.md "list-tasks" walkthroughs to
  describe the column / filter / sort / expand surface.
- Add a `docs/user-guide.md` §6/§7 note on the new list UX. (`docs/observability.md` §2.1
  unchanged — admin dashboards already separate concerns.)
- CHANGELOG `[Unreleased]` entry (reference every deferral by issue number per AGENTS.md);
  roadmap one-liner flip (planless shape → merged PR, since #137 has no roadmap chunk-plan slot);
  commit the impl-stage codex-review record under
  `docs/plans/review/issue-137/impl/<timestamp>/`.
- **Gate:** `markdownlint-cli2` on touched markdown; full pre-push command quartet from AGENTS.md.

## 9. Risks — load-bearing / silent-break surfaces

- **Eligibility-resolution wire cost (group targets).** Default "Eligible for me" ON resolves every
  group-targeted row via a DAG walk over HTTP. Without the per-render `group_cache` memo (D.3 in
  decisions §2.3), a list with many group-targeted tasks fans out into many `read_group`
  round-trips and the page feels slow. **Mitigation:** memoize by `group_id` per render; null /
  worker targets cost zero calls. Watch this in the e2e wave.
- **Eligibility resolution must not raise into a 500 — and must not mis-handle `NotFound`.**
  `resolve_worker_in_group` returns `False` (not raises) for an unknown worker/group; only
  transport/auth errors propagate (verified at
  [`client.py:692`](../../reference/packages/eden-wire/src/eden_wire/client.py), §D.4). So the
  row-builder catches **only the transport-shaped exception** around the eligibility probe and
  renders the row as **"eligibility unknown"** (disabled button + note + the §D.6 counter). It
  must **not** catch `NotFound` (a legitimate `False`) and must **not** collapse a real transport
  outage into "ineligible-and-hidden" — that would silently hide claimable work during an outage
  (the AGENTS.md "narrow exception handling on store reads" pitfall: distinguish definitive
  result from transport-indeterminate).
- **Sort over degraded rows.** Missing `priority`/`slug` (idea unavailable) must sort
  deterministically (bottom), never crash the comparator on `None`. Explicit sentinel keys.
- **Query-param injection into hrefs.** Sort/filter values are echoed into the page as link hrefs;
  allow-list every param value (§D.2) so an attacker-supplied `?sort=` can't reflect into markup.
- **`complexity-gate` regression.** `list_pending` already does try/except transport handling;
  adding param-parse + sort + filter + group risks crossing CC > 20 or the 100-line gate. Plan to
  extract `_parse_list_view(request)` and `_sort_and_group(rows, view)` helpers up front rather
  than bolting branches onto `list_pending` (AGENTS.md slop-prevention).
- **Symmetry drift.** Executor and evaluator row shapes differ (`idea` direct vs. via
  `variant.idea_id`; variant artifact in the evaluator expansion). A shared eligibility helper
  plus a shared template partial for the table/chips/expansion minimizes divergence; if a partial
  is impractical, a side-by-side diff check is a wave-2 review item.
- **#128 collision.** If #128 lands first and renames the worker-display surface, the `created_by`
  column's link/label shape may shift under the impl. Coordinate at impl-start: rebase against
  main and adopt whatever worker-display helper #128 introduced; otherwise ship `worker_id` and
  let #128 evolve it. Independent, not blocking (§10).
- **"Persist sort in session" deviation.** The plan deviates from the issue's literal
  "persist in session" to query-params (§2.2). If the operator wants true cross-visit persistence,
  that is a cookie-write per navigation — flag at plan review; deferring it is the recommendation.

## 10. Sequencing vs. related issues

- **#134** (CLOSED) — the `StorageError`-broadening that already satisfies #137's server-side
  prong. No coordination needed.
- **#135 / #138 / #132** (CLOSED) — subsumed / landed. The expansion's "view content" links rely
  on #138's shipped surface.
- **#128** (OPEN) — independent. #137 ships `created_by` as `worker_id`; #128 later evolves the
  display. Whichever lands first, the other rebases. Neither blocks.

## 11. Out of scope (followups — file as issues at impl-time per the deferral rule)

- **Cookie-backed cross-visit persistence** of sort/filter/group choices (v1 is query-params only).
- **Saved query / filter presets** ("show me only tasks from $worker").
- **Pagination** for very large pending-task lists.
- **Store-side priority ordering** — if the in-Python re-sort ever becomes a scale problem, push
  `ORDER BY (priority DESC, created_at ASC)` into the store query (touches `postgres.py` +
  `sqlite.py` + the `Store` contract); deliberately deferred (§2.1).
- **Ideator list parity** — only if a later operator session finds the ideator list crowded.
- **Browser-facing Forgejo browse-URL contract** — to make the expansion's parent-commit /
  work-branch references *clickable* links to a repo browser (today they render as text). No
  browse-URL surface exists in the web-ui CLI (only in-network `--forgejo-url` / informational
  `--clone-url`); adding one widens past web-ui-only and is deferred per §D.3.
- **Per-id idea / variant admin detail pages** — the expansion links to the admin *index* pages
  today because no per-id idea-detail or variant-detail page exists. If those land, the expansion
  links tighten to the per-id surfaces.

Each deferral above is narrated here; the impl PR files a tracking issue for any that survive to
merge and references it in the CHANGELOG entry (AGENTS.md deferral-tracking rule).

## 12. Files to touch (impl reference)

| File | Change |
|---|---|
| `reference/services/web-ui/src/eden_web_ui/routes/executor.py` | `list_pending` param parse + sort/filter/group; `_build_executor_pending_rows` eligibility + context fields |
| `reference/services/web-ui/src/eden_web_ui/routes/evaluator.py` | symmetric to executor |
| `reference/services/web-ui/src/eden_web_ui/routes/_helpers.py` | `resolve_eligibility` + group memo; param-parse / sort-group helpers |
| `reference/services/web-ui/src/eden_web_ui/templates/executor_list.html` | full redesign |
| `reference/services/web-ui/src/eden_web_ui/templates/evaluator_list.html` | full redesign |
| `reference/services/web-ui/src/eden_web_ui/static/style.css` | chips / disabled button / expand affordance |
| `reference/services/web-ui/tests/test_executor_routes.py` | sort / filter / group / eligibility / ineligible-claim regression |
| `reference/services/web-ui/tests/test_evaluator_routes.py` | symmetric |
| `reference/services/web-ui/tests/` (e2e) | ineligible-claim clean-banner e2e |
| `.claude/skills/eden-manual-executor/SKILL.md` | list-tasks walkthrough |
| `.claude/skills/eden-manual-evaluator/SKILL.md` | list-tasks walkthrough |
| `docs/user-guide.md` | §6/§7 list-page UX note |
| `CHANGELOG.md`, `docs/roadmap.md` | completion record (planless shape) |

## 13. Verification gates (impl pre-push)

```text
uv run ruff check .
uv run pyright
uv run pytest -q
uv run pytest -q conformance/ -n auto
python3 scripts/check-complexity.py
python3 scripts/check-rename-discipline.py
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
```

Compose smoke / e2e per AGENTS.md only if the e2e wave drives the Compose stack; the per-route +
single e2e tests are the primary coverage. The full `pytest -q` (not a web-ui-scoped subset) is
the literal gate per AGENTS.md.
