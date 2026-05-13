# Phase 12a-1b — Worker + group admin UI

## 1. Context

Phase 12a-1 (worker identity foundation) shipped in PR #78 (commit
`84adb50`). It introduced first-class workers, recursive groups,
identity-keyed claim ownership, and per-worker bearer authentication.
Every operator-side mutation of the worker / group registry today
happens via the chapter-7 wire endpoints; there is **no UI surface**.

This chunk — **12a-1b** — is the small UI follow-up the 12a-2 plan
explicitly defers:

> "Operator-creatable workers (the wire endpoint exists from 12a-1; UI
> surface for it is a smaller follow-up, not 12a-2's load)."
> — [`docs/plans/eden-phase-12a-2-orchestrator-as-role.md`](eden-phase-12a-2-orchestrator-as-role.md) §4.3

12a-1b is **orthogonal to 12a-2**: 12a-2 touches the orchestrator and
dispatch logic; 12a-1b touches only the web-ui's `/admin/*` surface
and adds zero wire / storage / spec changes. The two chunks can land
in any order; this plan is written so that the second-to-land
mechanically rebases over the first.

### What this chunk delivers

Two new admin modules on the existing chunk-9e admin surface:

- **`/admin/workers/`** — list/detail/register/reissue-credential.
- **`/admin/groups/`** — list/detail/register/add-member/remove-member/delete.

Plus a link to each from `/admin/` (the existing landing page).

Mirrors the chunk-9e pattern exactly: server-side Jinja, no JS
required (HTMX is available but unused here), `itsdangerous`-signed
session cookies, per-session CSRF, closed-allowlist banner copy on
`?<verb>=ok` / `?error=<key>` redirects, auth-first POST discipline
(session check before CSRF). No new spec changes — the spec is
complete for workers/groups in 12a-1. No new wire endpoints — every
operation the UI invokes already exists on `StoreClient`.

## 2. Decisions captured before drafting

1. **Web-ui needs admin-token access for the new admin module.** The
   write paths (`register_worker`, `reissue_credential`,
   `register_group`, `add_to_group`, `remove_from_group`,
   `delete_group`) are admin-gated at the wire layer
   ([`reference/packages/eden-wire/src/eden_wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py) lines
   642–847). The web-ui's existing worker bearer cannot drive them
   (it would get 403 `eden://error/forbidden`). The web-ui already
   resolves the admin token at startup via
   [`eden_service_common.resolve_admin_token`](../../reference/services/_common/src/eden_service_common/cli.py)
   but only uses it transiently during `bootstrap_worker_credential`.
   12a-1b **retains** the admin token, builds a second `StoreClient`
   bearing `admin:<admin_token>` (alongside the existing
   worker-bearer client), and plumbs it through `make_app` as
   `app.state.admin_store`. The admin module's write paths use
   `admin_store`; reads can use either (the worker bearer is fine
   for `list_workers` / `read_worker` / `list_groups` / `read_group`
   per their either-gated wire status).

2. **No real RBAC.** Same posture as chunk 9e: every signed-in
   session is implicitly an "operator", and the `/admin/*`
   navigation surfaces show to every authenticated user. The
   admin-token-bearer is the deployment-singleton authority; the
   web-ui acts as it whenever a route under `/admin/workers/*` or
   `/admin/groups/*` writes. 12a-2 will replace this with
   group-membership-based RBAC (`admins` group); we deliberately do
   not pre-build the group-based path to avoid stranding half-built
   RBAC if 12a-2 design evolves.

3. **Token display is one-shot, in-response HTML.** The
   `register_worker` and `reissue_credential` endpoints return the
   plaintext `registration_token` exactly once. The UI MUST display
   it directly in the POST response body — **not** redirect-with-
   querystring (would log the token in access logs and the
   referrer header). This breaks the chunk-9e "POST → 303 →
   detail" pattern for these two routes and only these two. The
   detail page after token display contains a prominent "this token
   will not be shown again" notice + a copy-to-clipboard button.
   See §3.4 for the exact shape.

4. **Reserved-identifier rejection is double-gated.** The wire layer
   already enforces the reserved list (`admin`, `system`,
   `internal`) and the grammar `^[a-z0-9][a-z0-9_-]{0,63}$` per
   chapter 02 §6.1. The UI **also** validates client-side before
   POST so that the form rejects locally with a friendly error
   instead of relying on a 409 round-trip. The server-side check
   remains authoritative; if the client-side check is bypassed
   (e.g., the UI's regex drifts) the wire still rejects.

5. **Transitive-membership view is server-walked, client-bounded.**
   `StoreClient` exposes a yes/no `resolve_worker_in_group` probe
   but no "list transitive members" call. The group-detail page
   walks the membership DAG client-side in the admin route (DFS via
   repeated `read_group` calls), with a defensive `visited` set
   guard and a depth/breadth cap (default: ≤10 levels of nesting,
   ≤1000 total leaf workers surfaced). When the cap is hit the
   page shows a truncation notice. Spec §7.3 cycle-detection-at-
   write-time guarantees termination; the cap is just an
   observability / performance gate. The walk is **not** cached
   between requests — every render is a fresh DAG snapshot.

6. **Member-add accepts free-form `member_id` (worker or group).**
   Per chapter 02 §6.1 / §7.1 worker_ids and group_ids share the
   same grammar; the wire's `add_to_group` accepts either. The UI
   form does not pre-validate "is this a worker or a group" — it
   just enforces the shared grammar locally and lets the wire
   decide. Per spec §7.1 "a reference to a non-existent worker /
   group resolves to membership=false", a dangling reference is
   spec-legal so the UI does not reject it; the operator is
   responsible for fixing or accepting the dangling reference.

7. **Worker-detail "recent attribution events" is bounded, not
   exhaustive.** A worker can appear in `task.claim.worker_id`,
   `task.created_by`, `task.submitted_by`, `idea.created_by`,
   `variant.executed_by`, `variant.evaluated_by`. The detail page
   filters `replay()` for events whose `data` dict contains the
   worker_id under any of those keys (single-pass linear scan;
   bounded by the existing chunk-9e `_TRIAL_DETAIL_EVENT_CAP=50`
   constant), and additionally lists the worker's attributed
   artifacts (tasks where `claim.worker_id == X` OR
   `submitted_by == X` OR `created_by == X`; ideas where
   `created_by == X`; variants where `executed_by == X` OR
   `evaluated_by == X`). Both views are read-only; no edits.

8. **No spec changes.** Chapter 02 §6 / §7, chapter 07 §6 / §7 / §13,
   chapter 08 §6 / §7 / §9 are complete for the surface we are
   building. The plan touches **zero** spec files.

9. **Mount unconditional.** Both new admin modules mount in
   `make_app` regardless of `--repo-path`. The existing work-refs
   sub-page (chunk 9e) was conditional on repo-path; the worker /
   group surface is not. The new admin pages render fine on an
   ideator-only deployment.

10. **No `delete_worker`.** The wire surface has `register_worker`
    (idempotent on existing) and `reissue_credential` (rotates the
    token) but **no** `delete_worker` — chapter 02 §6 does not
    define one. The UI honors this absence: workers are
    deregister-by-process-shutdown, not by UI action. (12a-2 / 12a-3
    may introduce deregistration; out of scope here.) Reserved-
    identifier deletion via group-delete is also not exposed
    because `admin`, `system`, `internal` cannot be registered in
    the first place.

## 3. Design

### D.1 Module placement and routing

New routes file:
[`reference/services/web-ui/src/eden_web_ui/routes/admin_workers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_workers.py).
New routes file:
[`reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py).

We split into two files (not a single `admin_registry.py`) for parity
with the chunk-9e shape where each admin sub-surface that has its own
templates / banner allowlist / form contract has its own routes file.
The admin landing page (`routes/admin.py`) gains two new "section"
blocks linking out to each.

| Method | Path                                            | Purpose                                                   |
| ------ | ----------------------------------------------- | --------------------------------------------------------- |
| GET    | `/admin/workers/`                               | List view; filter by label key/value substring            |
| POST   | `/admin/workers/`                               | Register; renders token-display page on success           |
| GET    | `/admin/workers/{worker_id}/`                   | Detail view + attributed-artifacts + reissue form         |
| POST   | `/admin/workers/{worker_id}/reissue-credential` | Reissue; renders token-display page on success            |
| GET    | `/admin/groups/`                                | List view                                                 |
| POST   | `/admin/groups/`                                | Register; 303 → detail on success                         |
| GET    | `/admin/groups/{group_id}/`                     | Detail view + transitive-member walk + add-member form    |
| POST   | `/admin/groups/{group_id}/members`              | Add member; 303 → detail                                  |
| POST   | `/admin/groups/{group_id}/members/{m}/remove`   | Remove member; 303 → detail. Note: POST (not DELETE form) |
| POST   | `/admin/groups/{group_id}/delete`               | Delete group; 303 → groups list                           |

The remove + delete actions use POST (not the HTTP DELETE method)
because HTML forms cannot natively issue DELETE; the chunk-9e
work-refs page uses the same pattern.

### D.2 Auth gate + CSRF discipline

Every handler — GET and POST alike — runs `get_session(request)`
**first**. Missing session → `303 → /signin`. CSRF check runs **after**
session check on POST routes only. Matches the existing chunk-9e
ordering at
[`reference/services/web-ui/src/eden_web_ui/routes/admin.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin.py)
lines 134-137, 313-317, 589-593. The redirect destination on the
`/signin` redirect is `/signin` itself (not the original URL); same
parity choice the existing admin module made.

### D.3 Admin-bearer StoreClient plumbing

[`reference/services/web-ui/src/eden_web_ui/cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py)
currently resolves `args.admin_token` via
`resolve_admin_token(args)` and threads it through
`resolve_worker_bearer` for boot-time credential issuance only. We
extend `cli.py` to **also** build a second `StoreClient` when an
admin token is present:

```python
admin_store: StoreClient | None = None
if admin_token is not None:
    admin_store = StoreClient(
        base_url=args.task_store_url,
        experiment_id=args.experiment_id,
        bearer=f"admin:{admin_token}",
    )
```

`make_app(...)` gains a new keyword-only parameter
`admin_store: StoreClient | None = None`; it's set on
`app.state.admin_store`. The "no admin token" case has **three
distinct postures**, each mapped to admin-module behavior:

| Posture | `app.state.store` (worker bearer) | `app.state.admin_store` | Admin-module read paths | Admin-module write paths |
|---|---|---|---|---|
| A. Admin token set | functional | functional (admin bearer) | available | available |
| B. No admin token, persisted worker credential | functional (worker bearer) | `None` | available | controls rendered `disabled`; POSTs short-circuit with `?error=admin-disabled` |
| C. No admin token, no persisted credential, task-store auth-disabled | functional (no auth header) | `None` | available | controls rendered `disabled`; POSTs short-circuit with `?error=admin-disabled` |
| D. No admin token, no persisted credential, task-store auth-enabled | unreachable — `resolve_worker_bearer` returns `None`, every wire call hits 401 | `None` | startup-time failure (see below) | n/a |

Posture A is the production deployment. Posture B is "operator
removed `EDEN_ADMIN_TOKEN` from the host env after first boot" —
intentional and documented in `resolve_worker_bearer`'s docstring
lines 240-249. Posture C is the local-test / TestClient posture.
Posture D is the misconfiguration we must surface clearly:

- Detection: `cli.py` already calls
  `wait_for_task_store(..., token=None)` then issues the worker
  bootstrap (line 188-220). If the task-store is auth-enabled and
  the worker bearer is `None`, `resolve_worker_bearer` itself
  returns `None`; the subsequent `/whoami` probe that 12a-1
  wave-4 added would fail with 401. **Add an explicit
  startup-time check** in `cli.py`: after constructing
  `app.state.store`, attempt `store.whoami()` (no admin needed,
  bearer authenticates as worker). On 401, log a clear error
  ("task-store is auth-enabled but no usable worker credential
  was provided; set `--admin-token` for first boot or persist a
  worker credential at `<credentials_dir>/<worker_id>.token`")
  and exit non-zero. This prevents Posture D from running as a
  silently-broken service.
- For Postures B and C, the new admin modules render the
  **normal** templates (`admin_workers.html` / `admin_groups.html`
  / `admin_worker_detail.html` / `admin_group_detail.html`) —
  reads work as usual. Inside those templates, every write
  control (register form, reissue button, add-member input,
  remove buttons, delete button) is rendered in a disabled state
  via a Jinja conditional on the boolean flag
  `admin_enabled` (passed through from the route handler reading
  `app.state.admin_store is not None`), with an in-page banner
  "admin token not configured; mutation unavailable — restart
  the web-ui with `--admin-token` or `$EDEN_ADMIN_TOKEN` set."
  Any POST that nevertheless arrives (e.g., a curl bypassing
  the disabled HTML) is short-circuited at the route handler
  with a 303 redirect to the page-with-banner via
  `?error=admin-disabled`. **No separate `*_disabled.html`
  templates**; the disabled shape is a render conditional on
  the normal templates.

Process-shutdown teardown: `cli.py` runs `store.close()` in a
`finally` block already; we add a symmetric `admin_store.close()`
when present. Both close calls are wrapped in
`contextlib.suppress(Exception)` to match existing close discipline.

### D.4 Routes — workers

#### `GET /admin/workers/`

Reads `app.state.store.list_workers()`. Renders a table with:
worker_id, registered_at, registered_by, labels (key=value comma-
joined, truncated to ≤120 chars), "groups containing this worker"
preview (first 3, "+N more" indicator). The groups preview is
computed by listing all groups once and walking the transitive
closure per worker — bounded by total-workers × total-groups; for
the reference-stack scale (≤20 workers, ≤10 groups per experiment)
this is fine. The cap from §3.5 / decision 5 applies here too: if
the walk exceeds 1000 visited groups across all rows, the per-row
preview is truncated and the list-view header shows a "group
membership preview truncated" banner.

Filters: `?q=<substring>` matches against worker_id and label
values (case-insensitive substring on the joined `key=value`
string). Empty / missing → no filter. This is a deliberate
single-input UX: the operator can search for both worker_id and
labels without picking a field. Validation: the filter is read
from `query_params`, lowercased, capped at 64 chars; longer
substrings are truncated (no error, just a truncation badge).

A "register new worker" form section sits at the top: one text
input for `worker_id`, an optional multi-line `key=value` per line
input for labels (lines starting with `#` are ignored as comments).
Client-side regex validation (`^[a-z0-9][a-z0-9_-]{0,63}$`) +
reserved-list check (`admin`, `system`, `internal`). Form action
posts to `/admin/workers/`.

When `app.state.admin_store is None`, the register section renders
as a disabled placeholder explaining the missing admin token.

#### `POST /admin/workers/`

Reads form `worker_id`, `labels` (multi-line key=value parsed
server-side), `csrf_token`. Validation order:

1. Session check → 303 /signin if missing.
2. CSRF check → 403 if invalid.
3. `app.state.admin_store` is `None` → render the register page
   with banner "admin token not configured; cannot register".
4. Grammar regex + reserved-identifier check on `worker_id` → render
   register page with banner if violated.
5. Labels parse: split lines on `\n`, ignore blank or `#`-prefixed;
   each remaining line MUST contain `=`; key + value both trimmed,
   key non-empty, key ≤64 chars, value ≤256 chars. Failures
   render with banner naming the offending line number.
6. `app.state.admin_store.register_worker(worker_id, labels=...)`
   returns a `(Worker, registration_token | None)` tuple per the
   12a-1 wire contract at
   [`reference/packages/eden-wire/src/eden_wire/client.py`](../../reference/packages/eden-wire/src/eden_wire/client.py)
   lines 484–498. Per spec §6.3 / 12a-1 §D.1, `register_worker` is
   **idempotent on the existing record**: a duplicate
   `worker_id` returns the existing `Worker` with
   `registration_token=None`. There is **no `AlreadyExists`**
   surface for `register_worker` (unlike `register_group`).
   Handler branches:
   - `token is not None` (fresh registration): render
     `admin_worker_token.html` with the plaintext token + banner
     "this is the only time you will see this credential".
   - `token is None` (idempotent re-register on existing record):
     render `admin_worker_token.html` with an explanatory banner
     "a worker with this id already existed; no new token was
     issued. Use the 'reissue credential' button on the detail
     page to mint a fresh credential." and a link to the worker
     detail. The template's token block is hidden in this branch.
   - `ReservedIdentifier` (409, wire
     `eden://error/reserved-identifier`): render with banner
     "this identifier is reserved" (defensive — step 4 should have
     caught this client-side).
   - `BadRequest` (400, wire `eden://error/bad-request`): render
     with banner "worker_id failed server-side validation"
     (defensive — step 4 should have caught this client-side).
   - Any other exception (transport, server 5xx, malformed
     response): render with banner "transport / server error;
     check logs" and `get_logger(__name__).exception(...)` the full
     traceback.
7. The success page (token-mint OR idempotent return) links to
   the worker detail (`/admin/workers/<id>/`) and the workers list
   (`/admin/workers/`) but does NOT include the token in those
   links.

Why a render-200 (rather than 303-redirect-with-banner) on success:
the registration token MUST appear exactly once in the response and
nowhere else. A 303 redirect would either lose the token (if it's
not in the querystring) or leak it (if it is). The render-200
shape keeps the token bound to the immediate response, scoped to
that user's session, and outside both server access logs (which log
URLs only, not bodies) and the browser's referrer surface.

#### `GET /admin/workers/{worker_id}/`

Reads `app.state.store.read_worker(worker_id)`; raises
`StorageNotFound` if the worker is unknown (propagates to the
app-wide 404 handler installed in
[`reference/services/web-ui/src/eden_web_ui/app.py`](../../reference/services/web-ui/src/eden_web_ui/app.py)).
Reads `list_groups()` and walks each to determine "groups that
transitively contain this worker" (capped per §3.5). Reads
`replay()` and filters for events whose `data` contains
`worker_id == X` under any of the attribution keys (linear scan;
≤50 most recent rendered, same as chunk-9e).

Additionally lists attributed artifacts. The attribution view has
three subsections; only the first two have "show more" links
because only those two existing admin pages exist today:

- **Tasks**: walks `list_tasks()` filtered by
  `claim.worker_id == X` OR `submitted_by == X` OR
  `created_by == X`. Shows up to 50 newest-first by `updated_at`.
  "Show more" links to `/admin/tasks/?worker=<id>` (the new
  worker filter added in §D.8).
- **Variants**: walks `list_variants()` filtered by
  `executed_by == X` OR `evaluated_by == X`. Shows up to 50
  newest-first. "Show more" links to
  `/admin/variants/?worker=<id>`.
- **Ideas**: walks `list_ideas()` filtered by
  `created_by == X`. Shows up to 50 newest-first. **No
  "show more" link** — the existing admin surface has no
  `/admin/ideas/` page, and adding one is out of scope (would
  expand the chunk meaningfully; see §11). The displayed cap is
  the operator's full view; if they need more, they go through
  the wire directly. A note on the section explains this.

The `worker` querystring on tasks/variants pages is sanitized via
the same regex as the new admin-workers form; an invalid value
renders the empty rowset (the chunk-9e `_INVALID_FILTER`
discipline).

A "reissue credential" button sits below the attribution section.
When `app.state.admin_store is None`, the button is disabled with
a tooltip.

#### `POST /admin/workers/{worker_id}/reissue-credential`

Validation order matches the register POST. Success: render
`admin_worker_token.html` with the plaintext new token, and a
prominent banner "the previous credential is now invalid". On
`NotFound`: render the workers-list page with banner
`?error=not-found`. The new token appears nowhere else; the
worker-detail page after reissue does NOT carry the token.

### D.5 Routes — groups

#### `GET /admin/groups/`

Reads `list_groups()`. Renders a table with: group_id, created_at,
created_by, direct member count, transitive worker count (capped
per §3.5; "≥1000" if exceeded), member preview (first 3 direct
members joined). The transitive worker count requires walking each
group's membership DAG; bounded by §3.5's caps.

Filters: `?q=<substring>` matches group_id only (groups don't have
labels). Same sanitization as workers list.

A "register new group" form sits at the top: text input for
`group_id` (grammar + reserved check client-side), optional
multi-line input for initial members (one member_id per line,
ignore blanks and `#` comments). Form action posts to
`/admin/groups/`.

#### `POST /admin/groups/`

Validation order matches workers register. Wire calls:

- `register_group(group_id, members=...)` once with the full
  initial member list. Wire validates each member's grammar +
  cycle-safety atomically. On `CycleDetected`: render with banner
  "this would create a cycle" naming both the group and the
  conflicting member. On `AlreadyExists`: render with banner.

Success: 303 → `/admin/groups/<group_id>/?registered=ok`.

Why 303 here (different from workers register): there's no
one-shot secret to display. Standard chunk-9e flow.

#### `GET /admin/groups/{group_id}/`

Reads `read_group(group_id)` (raises `NotFound` → app-wide 404
handler). Renders:

- Group metadata (group_id, created_at, created_by).
- Direct members list with per-row "remove" button (each a POST
  form to the remove endpoint, CSRF-tokened).
- Transitive worker closure ("workers who could claim a task
  targeted at this group"), capped per §3.5 — a separate section
  below the direct members so the operator can see the practical
  scope. Each row shows the resolution path (e.g., "via group X
  → group Y"), capped at 3 path elements with "..." otherwise.
- An "add member" form (single text input, grammar-validated).
- A "delete group" button below the form (CSRF-tokened, confirmed
  via the standard `<form>` POST — no JS `confirm()` since we
  cannot rely on JS).

When `app.state.admin_store is None`: read-only render, all write
controls hidden with a placeholder banner explaining the missing
admin token.

#### `POST /admin/groups/{group_id}/members`

Reads form `member_id`, `csrf_token`. Validation:

1. Session + CSRF (per D.2).
2. Admin-store availability (per D.3).
3. Grammar regex on `member_id` AND reserved-list check
   (`admin`/`system`/`internal`). The wire's
   [`_validate_registry_id`](../../reference/packages/eden-storage/src/eden_storage/_base.py)
   helper (line 1810) raises `ReservedIdentifier` for the reserved
   set and `BadRequest` for grammar violations; we mirror both
   client-side to short-circuit with a friendly banner. The
   client-side rejection is the canonical UX path; the server-side
   rejection remains as a defensive backstop if the client-side
   regex drifts.
4. `app.state.admin_store.add_to_group(group_id, member_id)`. On
   `CycleDetected` (409, wire `eden://error/cycle-detected`):
   banner `?error=cycle-detected`. On `NotFound` (group, the
   group_id from the path doesn't exist): banner
   `?error=group-not-found`. On `ReservedIdentifier` (409, wire
   `eden://error/reserved-identifier`): banner
   `?error=reserved-member-id` (defensive — step 3 should catch
   it). On `BadRequest` (400, wire `eden://error/bad-request`):
   banner `?error=invalid-member-id` (defensive). Other: banner
   `?error=transport`.

Success: 303 → `/admin/groups/<id>/?added=ok`.

#### `POST /admin/groups/{group_id}/members/{member_id}/remove`

Same validation prelude. Calls
`app.state.admin_store.remove_from_group(group_id, member_id)`.
Per the shipped 12a-1 storage layer at
[`reference/packages/eden-storage/src/eden_storage/_base.py`](../../reference/packages/eden-storage/src/eden_storage/_base.py)
line 1734, `remove_from_group` is **idempotent on absent member**:
removing a member that is not currently in the group is a no-op
that returns the (unchanged) group. Banner behavior:

- `NotFound` (group): `?error=group-not-found`.
- Wire returns the unchanged group when the member was already
  absent → success path; `?removed=ok` banner is rendered
  uniformly (the operator may notice "nothing changed" via the
  member list, which is fine).
- Other exception: `?error=transport`.

Success: 303 → `/admin/groups/<id>/?removed=ok`.

#### `POST /admin/groups/{group_id}/delete`

Same validation. Calls `app.state.admin_store.delete_group(group_id)`.
On `NotFound`: `?error=not-found`. Success: 303 →
`/admin/groups/?deleted=ok`.

Per spec §7.1, dangling references (other groups that contain the
deleted group_id as a member) are spec-legal and resolve to
membership=false. We do NOT scan for and reject dangling references
here — the spec permits them and the wire's
`delete_group` does not check. The UI's deleted-confirmation
banner does, however, surface a warning: "if other groups
contained this group as a member, they now have a dangling
reference." This is shown unconditionally because computing the
exact list at delete-time is racy and not worth the round-trips.

### D.6 Banner allowlist

Per chunk-9e §A.4 pattern (closed allowlist; unknown keys render no
banner). Each route file defines its own:

```python
_WORKER_REGISTER_OUTCOMES = {
    # Token-display page banners (rendered inline, not via 303):
    "ok": ("ok", "worker registered — token shown below (only time)"),
    "idempotent": ("warn", "worker already existed; no new token issued"),
    # Error banners (rendered via re-render OR ?error= querystring):
    "reserved-identifier": ("error", "this identifier is reserved"),
    "invalid-worker-id": ("error", "worker_id must match [a-z0-9][a-z0-9_-]{0,63}"),
    "invalid-labels": ("error", "label parse error (key=value per line)"),
    "admin-disabled": ("error", "admin token not configured; registration unavailable"),
    "transport": ("error", "transport or server error; refresh to retry"),
}

_GROUP_MEMBER_OUTCOMES = {
    "added": ("ok", "member added"),
    "removed": ("ok", "member removed (idempotent if absent)"),
    "cycle-detected": ("error", "adding this member would create a cycle"),
    "group-not-found": ("error", "group no longer exists"),
    "reserved-member-id": ("error", "member_id is reserved (admin/system/internal)"),
    "invalid-member-id": ("error", "member_id must match [a-z0-9][a-z0-9_-]{0,63}"),
    "admin-disabled": ("error", "admin token not configured; mutation unavailable"),
    "transport": ("error", "transport or server error; refresh to retry"),
}
```

Banner key derivation from wire error: a `_classify_wire_error(exc)`
helper maps `eden_storage.errors` types to the banner-key set. The
helper lives next to the route file (or in `_helpers.py` if shared
between workers and groups; see §5.1). Unknown exception types
classify as `"transport"` and the full exception is logged via
`get_logger(__name__).exception(...)` so the operator can dig.

### D.7 Templates

New templates under
[`reference/services/web-ui/src/eden_web_ui/templates/`](../../reference/services/web-ui/src/eden_web_ui/templates/):

- `admin_workers.html` — list + register form (form disabled when
  `admin_enabled=False` per §D.3).
- `admin_worker_detail.html` — detail + attribution + reissue
  button (button disabled when `admin_enabled=False`).
- `admin_worker_token.html` — one-shot token display (register OR
  reissue success path; render-200, never via redirect).
- `admin_groups.html` — list + register form (form disabled when
  `admin_enabled=False`).
- `admin_group_detail.html` — detail + direct members + transitive
  closure + add-member form + delete button (mutation controls
  disabled when `admin_enabled=False`).

There are no separate `*_disabled.html` templates — the
disabled-controls rendering is conditional inside the normal
templates (per §D.3).

Each extends `base.html`. Jinja autoescape is on globally (existing
default); all user-supplied strings (worker_id, group_id, labels,
member_id) flow through `{{ ... }}` only, never `| safe`.

The token-display template renders the plaintext token inside a
`<code>` block with a copy-to-clipboard button (HTMX-free; uses
inline `navigator.clipboard` if available, falls back to a manual-
select textarea). The page does not auto-redirect, does not store
the token in any link or hidden form field, and shows a fixed
"this is the only time you will see this credential" notice.

The admin landing page (`admin_index.html`) gets a new section:

```html
<section>
    <h2>workers and groups</h2>
    <p>
        <a href="/admin/workers/">workers ({{ worker_count }})</a>
        ·
        <a href="/admin/groups/">groups ({{ group_count }})</a>
    </p>
    {% if admin_disabled %}
    <p class="banner banner-warn">
        admin token not configured; registration / mutation disabled.
    </p>
    {% endif %}
</section>
```

`worker_count` / `group_count` come from
`store.list_workers()` / `list_groups()` called in the existing
`index()` handler. `admin_disabled` is `app.state.admin_store is None`.

### D.8 Worker filter on existing pages

Add a `worker` querystring filter to `/admin/tasks/` and
`/admin/variants/` so the "see all tasks for this worker" link from
the worker-detail page works. The filter is applied **post-fetch**
(no new wire shape): the existing route reads `list_tasks(...)` /
`list_variants(...)` per its existing kind/state/status filters, then
the new worker-filter narrows the result client-side in Python by
matching `task.claim.worker_id`, `task.submitted_by`, or
`task.created_by` for tasks; `variant.executed_by` or
`variant.evaluated_by` for variants. The filter regex is the same
worker_id grammar; an invalid value renders the chunk-9e
`_INVALID_FILTER` empty rowset (same shape as kind/state filters).
The querystring carries through to the list-view "edit search"
inputs so the operator can clear it.

This is the *only* change to existing admin pages outside the new
routes files. Templates `admin_tasks.html` and `admin_variants.html`
gain a small "filtered by worker `<id>`" indicator + a "clear filter"
link when the worker filter is active.

### D.9 What does NOT change

- Wire endpoints. Zero new endpoints; all UI operations use existing
  `StoreClient` methods.
- Storage protocol. Zero new ops.
- Spec chapters. Zero edits.
- The session shape (cookie payload). Sessions carry `worker_id` +
  `csrf` per the existing chunk-9c+ shape; no new fields.
- The existing admin module's routes (`/admin/`, `/admin/tasks/`,
  `/admin/variants/`, `/admin/events/`, `/admin/work-refs/`) other
  than the §D.8 worker-filter addition.
- Authentication mechanism for non-admin operations. The web-ui's
  worker-bearer StoreClient continues to drive every non-admin wire
  call.
- The `MANUAL_UI_ISSUES.md` content. We're not closing a tracked
  issue with this chunk; it's purely a "smaller follow-up" per the
  12a-2 plan.

### D.10 Alternatives considered

#### Alt A: One combined `admin_registry.py` routes file

We could put workers + groups in one file. We chose two because:

- The chunk-9e shape has `admin.py` (everything), but the file is
  already 800 lines; another ~600 lines of workers + groups would
  push it past the project's de-facto file-size norm.
- Tests (§6) are organized one-file-per-routes-file in chunk 9e
  (`test_admin_routes.py` covers everything). Splitting into
  `test_admin_workers_routes.py` and `test_admin_groups_routes.py`
  matches the routes-file split.
- Future chunks (12a-2 dispatch-mode admin page) will add yet more
  files; the precedent matters.

#### Alt B: Admin token only, no second StoreClient

Could the admin module instantiate a fresh `StoreClient` per request
with the admin bearer? Three reasons we don't:

- Connection pooling: `StoreClient` uses an `httpx.Client` that
  pools keep-alive connections. Per-request instantiation throws
  this away.
- Configuration drift: a per-request client has to re-resolve
  `base_url`, `experiment_id`, etc.; doing this in `make_app` once
  keeps the configuration single-sourced.
- Lifecycle: the app's shutdown handler already runs `store.close()`;
  adding a sibling `admin_store.close()` is simpler than tracking
  per-request clients.

#### Alt C: Group-membership-based RBAC now (skip admin-token-bearer)

12a-2 will introduce the `admins` group and replace
admin-token-bearer with group-based gating. We could try to pre-
build the group-gate, then have 12a-1b's admin module read its
session-user's group membership and reject if not in `admins`. We
don't because:

- The wire layer doesn't enforce admins-group membership today
  (only admin-token-bearer). Building a UI-side check that the
  wire ignores creates a false-confidence gap.
- The shipped 12a-1 web-ui has **no per-session worker bearer**
  (see §8.1). Today's session cookie is `{worker_id, csrf}` only;
  the `worker_id` is the deployment's `--worker-id` flag, not a
  per-user worker registered through the cookie. The 12a-1 §D.5b "per-session-user worker"
  mechanism was deferred and is not shipped. So the pre-build
  would also have to bring that deferred work forward — which
  is itself a multi-decision chunk (per-sign-in `reissue_credential`
  vs admin-derived session-only credential; CLI/web-UI
  cross-session compat). That's exactly the 12a-1 §D.5b
  discussion 12a-1 chose to defer.
- 12a-2 will add the wire-layer admins-group gate atomically
  with the reserved-group bootstrap (per
  [`docs/plans/eden-phase-12a-2-orchestrator-as-role.md`](eden-phase-12a-2-orchestrator-as-role.md)
  §5.1). The 12a-2 migration of 12a-1b's admin module is a
  **meaningful sub-chunk**, not a 10-line diff: it requires
  (1) shipping per-session worker bearers (the deferred §D.5b
  work), (2) updating the route layer to pick a bearer per
  request based on the signed-in user, and (3) adding the
  client-side check that the signed-in user is in `admins`
  before showing admin UI controls. 12a-1b's design isolates
  the bearer-construction in `cli.py` and routes all admin
  writes through `app.state.admin_store`, so the migration's
  blast radius is bounded and the rebase target is clearly
  named — but the line count is closer to 200 than to 10.
- 12a-2's RBAC story is still in design review; pre-building
  against it would couple this chunk to design that can still
  change.

#### Alt D: Render the token in a "click to reveal" UX instead of always-shown

We considered hiding the token behind a click in case the operator
shares their screen. We don't because:

- The token is in the rendered HTML regardless of CSS hiding;
  view-source / DOM inspection trivially reveals it. False
  security is worse than no security.
- The "this is the only time you will see this credential" notice
  is the actual mitigation: it tells the operator to scope the
  display moment.

### D.11 Forward-compatibility note for 12a-2

12a-2 (orchestrator-as-role) will:

1. Add an `admins` group and `orchestrators` group at experiment
   setup time (per the cited 12a-2 §5.1 setup-experiment.sh
   change).
2. Replace admin-token-bearer auth with
   `worker-in-admins-group` enforcement at the wire layer.
3. Add a `/admin/dispatch-mode/` page (12a-2 §7.8 reference).

12a-1b is forward-compatible with all three, in the sense that
the 12a-1b surface does not block any of them. The migration
shape is **not** a one-location bearer swap — see §D.10 Alt C
and §8.1 for the corrected scope (per-session worker bearers +
route-layer bearer-picking + client-side admins-group check).
The forward-compat property per item:

1. Existing routes don't care about the `admins` / `orchestrators`
   groups beyond surfacing them in `list_groups()` results.
2. All admin writes route through one named handle
   (`app.state.admin_store`) and the bearer is constructed in one
   place (`cli.py`). 12a-2 will replace the construction logic
   AND introduce per-session bearer plumbing AND wire-layer
   gating; the 12a-1b code is structured so the change is bounded
   to those identified locations, but the change itself is a
   meaningful sub-chunk per §D.10 Alt C.
3. `/admin/dispatch-mode/` is a different URL prefix; no namespace
   collision.

## 4. Scope

### 4.1 In scope

- New admin routes module for workers (CRUD-minus-D: create, read,
  reissue; no delete).
- New admin routes module for groups (full CRUD).
- New templates for both, including the one-shot token display.
- `--admin-token` plumbing into `make_app` for the admin-bearer
  StoreClient.
- Worker filter on `/admin/tasks/` and `/admin/variants/` so the
  worker-detail page's "see all" links work.
- Link from `/admin/` to both new modules with counts.
- Unit + flow + security + partial-write + e2e tests mirroring
  chunk-9e.
- Worker filter validation (grammar regex + invalid-filter empty
  rowset).
- Banner-allowlist closed vocabulary per route module.

### 4.2 Out of scope

- **`delete_worker`** — wire doesn't expose it; not adding a UI
  for an op that doesn't exist.
- **RBAC enforcement at the UI layer** — every signed-in user is
  an implicit operator (per decision 2). 12a-2 will tighten.
- **Admin token rotation through the UI** — the deployment admin
  token is set at the task-store-server CLI; rotating it requires
  a server restart. Not a UI-level operation.
- **Cross-experiment views** — web-ui process is pinned to one
  experiment per chunk-9e (and earlier).
- **Live-updating member counts / event streams** — same snapshot
  semantics as chunk 9e (`§A.2`). Refresh button only.
- **Bulk operations** — single-object per POST. Same posture as
  chunk-9e.
- **`/admin/orchestrator/` or `/admin/dispatch-mode/`** — 12a-2.
- **Validation of label keys / values for protocol-defined keys**
  — the spec doesn't reserve label key names; labels are
  protocol-agnostic per spec §6.2. The UI does not enforce any
  schema on label content beyond key non-empty + ≤64 chars +
  value ≤256 chars.

### 4.3 Non-goals

- A "create worker as user X" mode where the operator signs in as
  someone else after registration — not a UI flow; the operator
  copies the token out-of-band and the new worker uses it on its
  own host.
- Real auth gates per role (only-evaluators-can-see-evaluator-
  metrics, etc.) — well beyond this chunk.
- Operator-driven group membership snapshot export (CSV / JSON
  download). Useful eventually, not load-bearing now.
- Spec changes. The spec is complete for this surface.

## 5. Files to touch

### 5.1 Service code (`reference/services/web-ui/`)

| File                                                       | Change                                                                                                                   |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `src/eden_web_ui/cli.py`                                   | Build the admin-bearer `StoreClient` when admin_token is set; pass `admin_store=` to `make_app`. Close it on shutdown.   |
| `src/eden_web_ui/app.py`                                   | `make_app` gains keyword-only `admin_store: StoreClient \| None = None`; set on `app.state.admin_store`. Mount new routers. |
| `src/eden_web_ui/routes/admin.py`                          | Extend landing page with worker/group counts + the conditional admin-disabled banner. Add `worker` filter to tasks + variants list views. |
| `src/eden_web_ui/routes/admin_workers.py`                  | NEW. Routes per §D.4.                                                                                                    |
| `src/eden_web_ui/routes/admin_groups.py`                   | NEW. Routes per §D.5.                                                                                                    |
| `src/eden_web_ui/routes/_helpers.py`                       | NEW helpers: `_classify_wire_error(exc, banner_table)`, `_walk_group_transitive(store, group_id, *, depth_cap, breadth_cap)`. Both shared between the two new route modules. |
| `src/eden_web_ui/templates/admin_workers.html`             | NEW. List + register form (form `disabled` when `admin_enabled=False`).                                                   |
| `src/eden_web_ui/templates/admin_worker_detail.html`       | NEW. Detail + attribution + reissue button (button `disabled` when `admin_enabled=False`).                                |
| `src/eden_web_ui/templates/admin_worker_token.html`        | NEW. One-shot token display; render-200, no redirect.                                                                     |
| `src/eden_web_ui/templates/admin_groups.html`              | NEW. List + register form (form `disabled` when `admin_enabled=False`).                                                   |
| `src/eden_web_ui/templates/admin_group_detail.html`        | NEW. Detail + direct members + transitive closure + add-member form + delete button (mutation controls `disabled` when `admin_enabled=False`). |
| `src/eden_web_ui/templates/admin_index.html`               | New "workers and groups" section.                                                                                        |
| `src/eden_web_ui/templates/admin_tasks.html`               | Surface `worker` filter input + "clear filter" indicator.                                                                |
| `src/eden_web_ui/templates/admin_variants.html`            | Same.                                                                                                                    |
| `src/eden_web_ui/templates/base.html`                      | No change. The top nav already links to `/admin/` (chunk-9e); per-sub-page nav links would clutter it. Operators navigate workers/groups via the admin landing page added in §D.7. |
| `src/eden_web_ui/README.md`                                | Note the new `--admin-token` requirement for the admin module + the read-only mode when omitted.                         |

### 5.2 Tests (`reference/services/web-ui/tests/`)

| File                                          | Change                                                                                                                                            |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_admin_workers_routes.py`                | NEW. Per-route validation: auth gate, CSRF, filter sanitization, register form, reissue, token-display, disabled state.                           |
| `test_admin_workers_flow.py`                  | NEW. Cross-request: register → list shows it → reissue → list still shows it → token-display once.                                                |
| `test_admin_workers_security.py`              | NEW. Unauthenticated POST redirects to /signin before CSRF; reserved identifiers rejected; grammar regex enforced; token NOT echoed in error paths. |
| `test_admin_workers_partial_write.py`         | NEW. Wire idempotent-re-register / `ReservedIdentifier` / `BadRequest` / transport failures all surface as the right banner; idempotent re-register renders the "no new token" page; no partial state. |
| `test_admin_workers_e2e.py`                   | NEW. `pytest.mark.e2e`. Real subprocess: web-ui + task-store-server with admin token; UI registers a worker; CLI claims with the resulting credential. |
| `test_admin_groups_routes.py`                 | NEW. Per-route validation.                                                                                                                        |
| `test_admin_groups_flow.py`                   | NEW. Register → add member → list shows transitive closure → remove → delete.                                                                     |
| `test_admin_groups_security.py`               | NEW. Cycle attempts rejected; reserved identifiers rejected; CSRF enforced; member-id grammar enforced.                                           |
| `test_admin_groups_partial_write.py`          | NEW. Race: GET lists groups → another admin deletes one → POST add-member to it surfaces `?error=group-not-found`. Transport failures surface as banners. |
| `test_admin_groups_e2e.py`                    | NEW. `pytest.mark.e2e`. Subprocess: register group via UI, add a worker member, verify a task with `target={kind:group, id:G}` is claimable by that worker. |
| `test_admin_index.py`                         | NEW. Verify counts + disabled-banner conditional rendering on the existing admin landing page (the test extends, not replaces, `test_admin_routes.py` coverage). |
| `test_admin_routes.py` (existing)             | Extend with worker-filter tests on `/admin/tasks/` and `/admin/variants/`.                                                                        |
| `conftest.py` (existing)                      | New fixture: `admin_store` and `client_with_admin` for tests that need to drive the new admin paths. The `make_app` `admin_store=` parameter is exercised directly. |

Test-file basename check: `find reference -name 'test_*.py' -exec basename {} \;` confirmed no collisions; the new basenames are unique across all `testpaths` (per AGENTS.md "Adding a new service or package with its own `tests/` directory").

### 5.3 Reference / docs

| File                | Change                                                                                                                                                    |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGENTS.md`         | Append a brief paragraph to "Current phase" noting 12a-1b shipped, pointing at this plan. No commands-table changes.                                      |
| `docs/glossary.md`  | No edits needed; the workers/groups vocabulary is already in place from 12a-1.                                                                            |
| `docs/roadmap.md`   | If 12a-1b appears as a follow-up item, mark complete. If it doesn't (it was implicit in the 12a-2 plan's §4.3), no edit needed; impl PR description names it. |
| `MANUAL_UI_ISSUES.md` | If any tracked issue references "no worker admin UI", strike through with a 12a-1b cite. Likely no entry.                                               |

### 5.4 Compose / setup

No changes. The existing `.env` already plumbs `EDEN_ADMIN_TOKEN`
through to every worker host (per 12a-1); the web-ui service in
[`reference/compose/compose.yaml`](../../reference/compose/compose.yaml)
already has `EDEN_ADMIN_TOKEN` in its `environment:` block (verify
during impl). If it doesn't, add it — that's a one-line compose
edit, not a 12a-1b scope expansion.

### 5.5 Compose smoke

The chunk-9e smoke (`reference/compose/healthcheck/smoke.sh`) is
unchanged.

Decision: **do not extend `e2e.sh`** in this chunk. The chunk-10e
e2e smoke already covers the existing admin module's reclaim
walkthrough; the pytest `pytest.mark.e2e` tests in §6.9 below
provide real-subprocess coverage of the new admin module. A
compose-level smoke addition would (a) lengthen the e2e budget
(which is already a 60s headroom concern per chunk 10e) and
(b) duplicate coverage the `pytest.mark.e2e` suite already gives.
If post-merge observation shows the pytest e2e missing a real-
docker integration gap (e.g., the wire's argon2id verify is too
slow under the compose runtime profile), the smoke extension is a
~30-line follow-up — out of scope here.

## 6. Test design

### 6.1 Test shape per route file

Each routes file gets four pytest test files mirroring chunk-9e:

- `_routes.py`: per-handler unit tests covering each path's
  validation rules, auth gate, filter sanitization, banner-key
  rendering, edge cases on empty inputs.
- `_flow.py`: cross-request scenarios that exercise the
  state-machine transitions through multiple requests (e.g., POST
  register → GET list → POST reissue → GET detail).
- `_security.py`: unauthenticated POST redirects (before CSRF
  check); reserved identifiers (both `admin`/`system`/`internal`
  and the grammar regex); CSRF token validation; no-token-echo
  invariants (the registration token MUST NOT appear in any error
  path render).
- `_partial_write.py`: wire-error classifications, post-error state
  invariants (failed register → no worker in `list_workers()`;
  failed add-member → group membership unchanged).

Plus one `_e2e.py` per module driving real-subprocess.

### 6.2 Token-leak invariants

A dedicated test class in `_security.py` for each module asserts:

- The token is in the response body of the register / reissue 200
  page **once** and **once only**, surrounded by clearly identifiable
  markers (`<code class="token">...</code>`).
- The token is NOT in any URL the page links to (parse links via
  regex, check none contain the token substring).
- The token is NOT in the `Set-Cookie` header (since the session
  cookie doesn't carry it).
- The token is NOT in any subsequent GET response (detail page,
  list view, error page).
- On register-error (e.g., grammar violation), the response does
  NOT contain any token — i.e., the wire was never called.

A second test class asserts that the access log entries the route
produces (via `caplog`) do NOT contain the token. This is a
behavioral assertion against the `get_logger` shape — the wire
client does not log Authorization headers or response bodies (per
12a-1 §8.5 "Token storage hygiene"), so this should hold trivially,
but we test it to prevent future regression.

### 6.3 Reserved-identifier / grammar tests

Three branches per route:

- Reserved name (`admin`, `system`, `internal`) → reject locally,
  surface `?error=reserved-identifier` banner. Wire MUST NOT be
  called (assert via store call counter / monkeypatch).
- Grammar violation (`UPPER`, `with spaces`, `with/slash`, empty
  string, 65-char string, leading hyphen `-foo`, etc.) → reject
  locally, surface `?error=invalid-worker-id` (or `-group-id` /
  `-member-id`) banner. Wire MUST NOT be called.
- Valid identifier passing all client-side gates → wire is called.

The fourth branch — wire-side reject of a value that passed
client-side gates (i.e., bug or grammar regex drift) — is covered
by the `_partial_write.py` file's "wire raises ReservedIdentifier"
case.

### 6.4 Cycle-detection UX test

Set up groups `team-a` containing `team-b`. Set up form to add
`team-a` to `team-b` (would close the cycle). POST returns 303 with
`?error=cycle-detected`. The error banner copy names the source and
destination groups in the message. Group membership is unchanged
(asserted via `read_group(team-b)`).

### 6.5 Transitive-membership walk under churn

Concurrent-mutation race: GET `/admin/groups/team-a/` mid-render
while another admin (the test driver, via direct store call)
deletes `team-b` from `team-a`. The render uses `list_groups()` +
`read_group()` calls in sequence; the response renders **whatever
membership state** the store returned at each call. The test
asserts:

- The render does not raise.
- The displayed direct-member list and transitive-closure may be
  inconsistent (the closure walk happens AFTER `read_group`), but
  this is acceptable — it's a snapshot, refresh resolves.
- The displayed banner-key on a subsequent action (add/remove)
  uses the FRESH state (the POST handler does its own reads).

### 6.6 Transitive-walk cap test

Construct a synthetic membership graph with 1100 transitive workers
(11 nested groups × 100 workers each). GET `/admin/groups/<top>/`.
Render succeeds; the closure section shows the first ≤1000
workers + "showing 1000 of ≥1100; refine filter" indicator. Page
load completes in <1s (assert via `time.monotonic()` bracket).

### 6.7 Disabled-state coverage

A separate test that constructs `make_app(admin_store=None)` and
asserts (postures B and C from §D.3):

- GET `/admin/workers/` renders successfully; the register form
  is rendered as a disabled placeholder (input + button present
  but `disabled` attribute set, with a banner explaining the
  missing admin token).
- POST `/admin/workers/` redirects (303) to
  `/admin/workers/?error=admin-disabled`. **Decision (resolving
  the round-0 finding-5 defer):** redirect-with-banner, NOT 403
  — matches the chunk-9e error-banner shape, gives the operator
  a navigable next step, and avoids the "what just happened?"
  ambiguity of a bare 403. The 403 path is reserved for
  CSRF-token failures and remains so.
- GET `/admin/groups/` same posture: register form disabled, GET
  paths still resolve.
- POST `/admin/groups/`, POST add-member, POST remove-member, POST
  delete-group: all redirect with `?error=admin-disabled`.
- Read paths (`/admin/workers/<id>/`, `/admin/groups/<id>/`) still
  render their content; only write actions are gated.

A second test variant exercises Posture D (auth-enabled
task-store + no usable bearer): assert that `cli.main()` exits
non-zero with the expected log message rather than running a
silently-broken service. This is a `subprocess` test, not a
TestClient one — the startup-time `/whoami` probe is the gate.

### 6.8 Worker-filter on existing pages

In `test_admin_routes.py`:

- `/admin/tasks/?worker=eric` returns only tasks where
  `claim.worker_id == "eric"`, `submitted_by == "eric"`, or
  `created_by == "eric"`.
- `/admin/tasks/?worker=NotAValidId` returns the empty rowset (the
  `_INVALID_FILTER` discipline).
- `/admin/tasks/?worker=eric&kind=execution` composes correctly
  (intersection of both filters).
- `/admin/variants/?worker=eric` returns only variants where
  `executed_by == "eric"` or `evaluated_by == "eric"`.
- The "clear worker filter" link in the rendered HTML points to
  the same URL without the `worker` querystring.

### 6.9 End-to-end (`pytest.mark.e2e`)

Two e2e tests, one per module:

- **workers e2e**: spawns `task-store-server` (with admin token) +
  `web-ui` (with admin token). UI session signs in, POSTs to
  `/admin/workers/` to register `e2e-worker-1`. Test extracts the
  one-shot token from the response HTML, constructs a `StoreClient`
  bearing `e2e-worker-1:<token>`, and verifies the new client can
  call `/whoami`.
- **groups e2e**: spawns the same stack. UI session registers a
  group, adds `e2e-worker-1` as a member, then the test
  constructs a task with `target={kind:"group", id:"G"}` via the
  admin client and asserts `e2e-worker-1`'s `StoreClient.claim()`
  succeeds (and a non-member's `claim()` fails with `WorkerNotEligible`).

Per the multi-process pipe-buffer pitfall (AGENTS.md "Multi-process
e2e tests must not pile log output…"), redirect subprocess stdout
and stderr to per-process files via `Popen(stdout=open(path, "wb"),
stderr=subprocess.STDOUT)`. Use the canonical `_read_port_announcement`
and `_dump_logs` shape from
[`reference/services/orchestrator/tests/test_e2e.py`](../../reference/services/orchestrator/tests/test_e2e.py).

## 7. Verification gates

The literal commands from AGENTS.md "Commands". The chunk is
mergeable when all of:

1. `uv sync` succeeds.
2. `uv run ruff check .` clean.
3. `uv run pyright` clean.
4. `uv run pytest -q` (full suite) green — includes all the new
   per-route + flow + security + partial-write + e2e tests.
5. `uv run pytest -q -m e2e` green.
6. `uv run pytest -q conformance/` green (no conformance impact;
   existing scenarios still pass).
7. `uv run python conformance/src/conformance/tools/check_citations.py`
   clean (no new conformance scenarios added by this chunk).
8. `python3 scripts/spec-xref-check.py` clean.
9. `python3 scripts/check-rename-discipline.py` clean.
10. `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules"
    "#.venv" "#docs/archive/**" "#docs/plans/review/**"` clean.
11. `bash reference/compose/healthcheck/smoke.sh` green.
12. `bash reference/compose/healthcheck/e2e.sh` green (no new
    coverage added; just confirming we didn't break the existing
    admin flow).

The "Commands" section is the literal pre-push validation gate
(per AGENTS.md "The 'Commands' section above is the literal
pre-push validation gate; narrowed subsets are not"). Do not push
on a tighter loop — run the full set.

## 8. Tricky areas

### 8.1 The shipped web-ui has no per-session bearer

Important correction to a tempting misreading: the web-ui's
session cookie does **not** carry a per-user credential today.
Inspect
[`reference/services/web-ui/src/eden_web_ui/sessions.py`](../../reference/services/web-ui/src/eden_web_ui/sessions.py)
— the cookie payload is `{worker_id, csrf}` only; no
bearer field, no admin flag. The sign-in route at
[`reference/services/web-ui/src/eden_web_ui/routes/auth.py`](../../reference/services/web-ui/src/eden_web_ui/routes/auth.py)
sets the cookie's `worker_id` to **the web-ui process's own
`--worker-id` flag** (a single deployment-wide value, NOT a
per-user worker registered through the cookie). The 12a-1 plan
§D.5b's "per-session-user worker authentication" idea was
**not** implemented in the shipped 12a-1; it was deferred (12a-1
notes this as an accepted limitation).

What this means for 12a-1b:

- Every wire call the web-ui makes — on behalf of any signed-in
  user — uses the process-level `StoreClient` constructed once in
  [`reference/services/web-ui/src/eden_web_ui/cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py)
  via `resolve_worker_bearer`. There is no per-session worker
  bearer to thread.
- For the new admin module, we add a **second** process-level
  client (`admin_store`) bearing `admin:<admin_token>`. It is
  shared across every signed-in session. The session is the
  authorization layer ("this browser tab is authenticated to do
  admin things"); the admin token is the wire-layer principal.
- 12a-2's group-based RBAC migration is **not** a simple bearer
  swap — it requires (a) shipping per-session worker bearers (the
  deferred §D.5b work) AND (b) wiring the wire-layer admins-group
  gate AND (c) updating the route layer to pick a bearer per
  request based on the signed-in user's group membership. The
  earlier §D.10 Alt C description ("10-line diff") was incorrect;
  the actual migration is a meaningful 12a-2 sub-chunk. See §D.10
  Alt C for the corrected scope.

Risk: a route accidentally uses `app.state.store` (worker bearer)
instead of `app.state.admin_store` (admin bearer) for a write
operation, sees 403, and surfaces an opaque "transport" banner.
Mitigation: route-by-route impl review + a security-suite test
that asserts admin operations route through `admin_store` (mock
both clients, assert which one was called).

### 8.2 Token-display POST returns 200, not 303

Breaking with the chunk-9e norm requires care. The register / reissue
handlers MUST return a full HTML page on success (not redirect), and
the page MUST display the plaintext token. Two failure modes to
explicitly guard:

- **Double-render**: the token is in the rendered HTML; if the user
  refreshes that page, the browser will repost the form, and the
  wire will return a different token (reissue) or error (register
  is idempotent on existing record — see 12a-1 §D.1 — but the new
  token is then bound to a different request). Mitigation: use
  POST/Redirect/GET only when there's no secret; document the
  refresh-warning in the rendered page; consider issuing a
  `Cache-Control: no-store, no-cache` header on the token page so
  browsers don't aggressively cache it.
- **Browser autofill leak**: if the registration page is in an
  authenticated tab, the rendered token might be picked up by a
  password manager via heuristic. Mitigation: render the token in
  a `<code>` block, not an `<input>` field; the autofill heuristic
  needs an input element.

### 8.3 The "delete a group that's a member of other groups" UX

We don't enforce a no-dangling-references invariant (per spec §7.1
dangling references are legal). But the user might be surprised:
they delete `team-b`, and `team-a` still has `team-b` in its
members list (resolving to membership=false). The delete-banner
explicitly warns. A future enhancement could cascade-warn (list the
specific groups that hold a reference) but doing so at delete-time
is racy; we accept the bounded UX gap.

### 8.4 Filter querystring + new-worker-id propagation

The new `worker` filter on `/admin/tasks/` reads `query_params.get("worker")`.
That value gets reflected back into the rendered HTML (filter
indicator, "clear filter" link). It MUST flow through Jinja's
default autoescape so a `?worker=<script>...` attempt doesn't fire.
The autoescape default is enabled globally; this is a re-assertion,
not a new mechanism. The `_security.py` test for tasks-page filters
asserts an HTML-encoded `&lt;script&gt;` round-trip.

### 8.5 Transitive-walk performance ceiling

The §3.5 walk cap (1000 leaf workers, 10 levels of nesting) is
chosen for the reference-stack scale; real deployments could exceed
it. The cap is a constant in
[`reference/services/web-ui/src/eden_web_ui/routes/_helpers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py)
(`_GROUP_WALK_DEPTH_CAP`, `_GROUP_WALK_BREADTH_CAP`). A future
optimization is to add a wire-side "list transitive workers" op
that does the walk server-side in one call; not in scope. The plan
explicitly chooses client-side walking for the same reason chunk
9e classifies work-refs client-side: simple, no schema additions,
acceptable at reference-stack scale.

### 8.6 The worker-filter on existing pages duplicates filtering logic

The existing kind / state / status filters are pushed into the
store's `list_tasks()` / `list_variants()` calls; the new `worker`
filter is post-fetch in Python. This is fine for the reference
stack (≤500 tasks per experiment), but it sets a precedent that
client-side post-filter is acceptable. We accept this and note it;
if a future deployment hits the limit, the wire-layer filter is the
canonical fix and is independent of this chunk.

### 8.7 Idempotent registration vs. UI expectation

Per 12a-1 §D.1, `register_worker(worker_id)` is **idempotent on the
existing record** — second call returns the existing Worker
without a new token. UI implications:

- The register form silently succeeding when a worker already exists
  could confuse the operator ("I clicked submit, did it work?").
  Mitigation: detect the idempotent return (wire returns the
  worker shape without `registration_token`) and render with a
  banner "this worker already existed; no new token was issued"
  instead of the token-display page. Operator can then click
  "reissue credential" if they actually wanted a fresh token.
- The reissue path is always non-idempotent — it always returns a
  new token, invalidating any previous one.

The two paths converge on the same `admin_worker_token.html`
template; the template renders differently when
`registration_token is None` (the "already existed" branch).

### 8.8 Existing `admin_token` flag wiring

`add_common_arguments` in
[`reference/services/_common/src/eden_service_common/cli.py`](../../reference/services/_common/src/eden_service_common/cli.py)
already registers `--admin-token`. The web-ui's `cli.py` already
inherits it via `add_common_arguments(parser)`. So no new CLI flag
is needed in this chunk. Verify during impl that the flag's help
text mentions the admin module use case.

## 9. Risks / things to watch

- **Token-display refresh repost**: documented in §8.2; mitigations
  in the rendered page + the `Cache-Control` header. A
  determined operator who refreshes will repost; the wire is
  idempotent on register (returns existing record without token —
  fine UX) and non-idempotent on reissue (mints a new token — the
  operator now has TWO tokens and the old one is invalid). The
  "two tokens" outcome is benign: only the latest is valid; the
  earlier one is already invalid. Document in the README.

- **Admin-token leak via screenshots / shoulder-surf**: outside the
  protocol's threat model. We don't gate against this.

- **Forward-compat with 12a-2 group-based RBAC**: documented in
  §D.10 Alt C, §D.11, and §8.1. The migration is a meaningful
  sub-chunk (per-session worker bearers + route-layer bearer-
  picking + client-side admins-group check), not a single-
  location bearer swap. 12a-1b minimizes the migration's blast
  radius by isolating the admin-bearer construction to `cli.py`
  and routing all admin writes through `app.state.admin_store`,
  but the migration's line count is closer to 200 than to 10.
  Risk: 12a-2 design changes between landing 12a-1b and 12a-2,
  forcing a larger rebase. Mitigation: track the 12a-2 plan;
  surface deltas if its RBAC story shifts after 12a-1b lands.

- **Conformance impact**: zero new scenarios. The chunk-11 / chunk-
  12 conformance suite (`conformance/scenarios/`) covers wire-
  contract behavior; the admin UI is reference-impl-internal and
  doesn't sit in the conformance contract. We verify no existing
  scenarios regress.

- **Bootstrap chicken-and-egg**: 12a-1 §8.7 noted that admin-token
  is the bootstrap secret for worker registration. The web-ui
  reads it the same way every other host does (via
  `resolve_admin_token`). The new admin module is just one more
  consumer of the existing plumbing.

- **Tests against the existing chunk-9e admin module**: the §D.8
  worker-filter additions to `admin_tasks.html` /
  `admin_variants.html` could regress existing chunk-9e tests if
  the filter-indicator markup interferes with existing assertions
  (e.g., a test that checks "no extraneous text"). Mitigation:
  run the full pytest suite at each impl checkpoint, not just the
  new test files.

- **The `_helpers.py` shared module**: chunk 9e's
  `_classify_wire_error` doesn't exist yet (each route file inlines
  the exception → banner mapping). Adding it as a helper is a
  light refactor; risk is that we move logic that has chunk-9e-
  specific shape (`_RECLAIM_OUTCOMES`, `_REF_DELETE_OUTCOMES`) into
  a single place and accidentally couple. Mitigation: the helper
  takes the banner table as a parameter — it's pure mapping
  logic; the route file owns the table.

## 10. Sequence within the chunk

Even as a single PR, internal ordering matters. Suggested:

1. **CLI + make_app plumbing**. Pass `admin_token` through; build
   `admin_store`; surface it on `app.state`. Tests: a single
   smoke test that asserts `app.state.admin_store` exists when
   the CLI gets `--admin-token` and is `None` otherwise. Confirm
   `store.close()` + `admin_store.close()` both run on shutdown.
2. **Empty-shell routes**. Mount both new routers with
   placeholder handlers that return 501 / empty templates. Verify
   `/admin/workers/`, `/admin/groups/`, and their detail paths
   route correctly. Verify auth-gate (303 → /signin without
   session).
3. **Workers list + register + token display**. Most-isolated
   surface. Templates + handlers + the token-display invariant.
   Tests: `test_admin_workers_routes.py`, `_security.py`,
   `_partial_write.py`.
4. **Workers detail + attribution + reissue**. Builds on the
   list view; reuses the token-display template. Tests:
   `test_admin_workers_flow.py`.
5. **Worker filter on existing admin pages**. Smallest amount of
   surface change to existing admin routes. Tests:
   extension to `test_admin_routes.py`.
6. **Groups list + register**. Parallels workers list.
7. **Groups detail + add/remove + transitive walk + delete**. The
   biggest single chunk; cycle-detection UX + transitive walk +
   delete-with-dangling-warning.
8. **Admin landing page**. Worker/group counts; admin-disabled
   banner.
9. **E2E tests**. Full real-subprocess walkthroughs.
10. **README + AGENTS update + roadmap delta (if applicable)**.

Expect tests to go red around step 2 (new routes mounted but no
handlers yet) and come back green around step 4 (workers fully
shipped), then red→green again for steps 6–7 (groups).

## 11. Out of scope (followups)

- **Group-based RBAC enforcement at the UI layer** — 12a-2.
- **A `/admin/dispatch-mode/` page** — 12a-2 §7.8.
- **Per-task `created_by` attribution panel on the admin task
  detail page** — explicitly deferred by 12a-2 §4.3 line 705-707;
  the worker-detail page's attribution view covers the reverse
  direction (worker → tasks) but not the forward (task → who
  created it). The 12a-2 plan promises this as "small and
  independent" — leave it for the explicit 12a-2 follow-up rather
  than expanding 12a-1b's scope.
- **`delete_worker`** — wire doesn't expose it; spec doesn't define
  it; not adding a UI.
- **Bulk operations on workers / groups** — single-object per POST.
- **Wire-layer "list transitive workers" op** — see §8.5.
- **Worker activity timeline beyond simple event filtering** —
  beyond the 50 most-recent attribution events on the detail page,
  we don't render a per-worker timeline.
- **Operator-facing config for the transitive-walk caps** — the
  caps are constants in `_helpers.py`. A `--group-walk-depth` /
  `--group-walk-breadth` CLI flag is plausible but out of scope.
- **`admin_token` rotation via UI** — out of scope per §4.2.

## 12. Estimated effort

- **Plumbing (cli.py / app.py / placeholder routes)**: ~0.25 day.
- **Workers module (routes + templates + tests)**: ~0.75 day.
- **Groups module (routes + templates + tests, including
  transitive walk + cycle UX)**: ~1 day.
- **Worker filter on existing admin pages + landing-page section**:
  ~0.25 day.
- **E2E tests (workers + groups)**: ~0.5 day.
- **README / AGENTS update / roadmap delta**: ~0.1 day.

**Realistic total: ~2.5–3 working days** of focused work, plus
the codex-review loop. Smaller than 12a-1 (~6–7 days) and chunk
9e (~3 days) but larger than a single fix-it.

The plan itself is the standard ~half-day; this document is that.
