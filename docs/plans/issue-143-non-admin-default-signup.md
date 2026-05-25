# Issue #143 — Web UI sign-ups non-admin by default; admins promote via `admins` group membership

GitHub: [#143](https://github.com/ealt/eden/issues/143).

## 1. Context

Today the web UI is itself a single registered worker (`worker_id=web-ui-1`). Every browser session — regardless of who is at the keyboard — bears the `web-ui-1` credential. `setup-experiment.sh` seeds two workers into the `admins` group at bootstrap:

- `EDEN_ADMINS_INITIAL_MEMBER` (default `operator`) — the "human admin" the operator is supposed to act as
- `EDEN_WEB_UI_WORKER_ID` (`web-ui-1`) — the web-ui service itself

Both are added explicitly at [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh) L740-783. The consequence is that **every web UI user is transitively an admin** — admin-gated wire ops succeed for any signed-in browser session because the bearer says `web-ui-1` and `web-ui-1 ∈ admins`. CLI users under the auto-registered `eden-manual` are NOT in `admins`, so CLI lacks the symmetry.

Issue [#140](https://github.com/ealt/eden/issues/140) (operator identity as a registered worker, "Model B") is the strict prerequisite that fixes the principal half: each operator registers their own `Worker` record, the session cookie carries their per-user bearer, and `web-ui-1` becomes a service-only worker used for the web-ui's own auto-actions. Once #140 lands, the web-ui session bearer IS the operator's bearer; what's left is the **authorization** question — who in that population is an admin? — and the operator-facing flow that promotes / demotes.

This issue codifies the privilege model on top of #140 and ships the operator-facing promotion surface. The intent: **web UI sign-ups are non-admin by default**, admins are explicitly admitted by membership in `admins`, the operator who runs `setup-experiment` is the bootstrap admin, and subsequent admins are promoted by an existing admin via the wire or the web UI.

## 2. Decisions captured before drafting

These were settled in [#143](https://github.com/ealt/eden/issues/143)'s issue body + the discussion that produced it; codex-review and future maintainers can see what was deliberate vs. proposable.

1. **Default is non-admin.** A new operator who signs up via the web UI gets a fresh `Worker` record that is NOT added to `admins`. Admin authority is opt-in by an existing admin, never implicit.
2. **Single initial admin.** `setup-experiment` seeds **one** admin: the operator running the bootstrap. The `web-ui-1` service worker is NOT added to `admins` under the new model — its previous admin membership was a Phase-9-shaped shortcut that this issue retires.
3. **Promotion is explicit and symmetric.** Any existing admin can promote another registered operator by adding them to the `admins` group; demotion is by removal. Both flows exist on the web UI (`/admin/groups/admins/`) and the CLI (the wire endpoint).
4. **No role-based access control beyond admin / non-admin.** Read-only-observer / evaluator-only / etc. are deferred until use cases emerge.
5. **No audit-trail UI in scope.** Wire emits `group.member_added` / `group.member_removed` events today (chapter 05 §3); a dedicated promotion-audit page is a separate concern.
6. **Pre-external-user posture.** Migration is by re-bootstrap. No deprecation shim for the "web-ui-1 in admins" pattern — it's removed in lockstep with the setup-experiment change. Operators of in-flight experiments re-run setup against a fresh data root per [`docs/operations/experiment-data-durability.md`](../operations/experiment-data-durability.md).

These five decisions are NOT up for re-litigation in codex-review unless review surfaces a load-bearing contradiction with a spec MUST or with the #140 plan.

## 3. Strict prerequisite: #140 must land first

This plan **does not** stand alone. Its preconditions are the deliverables of #140:

- Per-user session bearers in the web UI (sign-up / login form; session cookie carrying the operator's `worker_id` + token; `reissue_credential` on sign-in for credential rotation).
- The `/profile/` page on which #143 layers the "your groups" / "admin flag" display.
- The retirement of the `eden-manual` auto-registration-on-first-claim path.
- The convention that `web-ui-1` / `eden-manual-*` are service-only workers; human-driven actions never use them.

If #140 has not merged when this plan reaches impl, **do not start implementation**. Surface to the operator and either rebase against an in-flight #140 branch or defer until #140 lands. The plan deliberately assumes #140's session-bearer plumbing and Profile page exist; co-shipping #140 + #143 in a single PR is fine but the dependency direction is one-way (#140 → #143, never the reverse).

[#128](https://github.com/ealt/eden/issues/128) (id/name disambiguation) is a **partial** prerequisite. The "Add member" form on `/admin/groups/admins/` accepts a `worker_name` under #128's surface; without #128 it accepts a kebab-case `worker_id` per the §6.1 grammar. The plan covers both shapes (§D.4) so impl can land either way #128 has been resolved by then.

## 4. Design

### D.1 Wire-level authorization: who can call `add_to_group` / `remove_from_group`

This is the load-bearing spec amendment.

Today (chapter 07 §13.3): `add_to_group / remove_from_group / delete_group / register_group` are **admin-principal-gated** — only the literal `admin:` bearer can call them. That means an "existing admin" (a worker whose `worker_id` is a member of `admins`) cannot promote anyone using their session bearer; they would have to hold the deployment admin token, which #140's Model B explicitly retires for human operators.

Two possible shapes:

(a) **Trust the web UI as a proxy.** Keep wire `add_to_group` admin-principal-gated. Web-UI route handler performs a session-level `resolve_worker_in_group(session.worker_id, "admins")` check; if it passes, the handler issues the wire call using `app.state.admin_store` (deployment admin bearer, already plumbed). Net: the web-ui service holds an admin-grade credential indefinitely; the operator's session bearer doesn't.

(b) **Dual-gate at the wire.** Amend chapter 07 §13.3 so `add_to_group / remove_from_group / delete_group` accept **either** the deployment admin principal OR a worker principal whose `worker_id` resolves into `admins`. Net: any admin-group member can promote / demote via their own session bearer; the web-ui no longer needs to hold a deployment admin credential for the promotion path. CLI gets the same shape for free.

**Recommendation: option (b).** Reasons:

- Symmetry with the issue body: "An existing admin can promote another registered operator by adding them to the `admins` group" reads most naturally as the existing admin's bearer carrying the authority, not the web-ui proxying.
- Removes the "web-ui service holds a deployment admin credential" footgun. Under (a), every web-ui deployment is one bug away from leaking admin authority to non-admin sessions; under (b) the only thing web-ui-1 carries is its own worker bearer.
- CLI parity: a CLI user who is in `admins` can promote others without exporting the deployment admin token to their shell.
- Bootstrap remains intact: setup-experiment still calls `add_to_group` with the deployment admin bearer to seed the initial admin (chicken-and-egg case where there is no admin-group member yet). The dual-gate accepts both, so bootstrap doesn't change.

Spec amendment shape (§13.3 classification list):

- Existing: `add_to_group / remove_from_group / delete_group` listed under "admin-gated — registry mutations."
- New: extract these three into a new bullet "**admin-OR-`admins`-group-gated** — worker-class operations on the admins group / mutation of the membership graph". Mirrors the bootstrap-class language used for §14 checkpoint operations: same prose pattern, different rationale (operator-promotion of peers, not deployment-bootstrap).
- Note that `register_group` stays admin-principal-only. The decision: registering a new group is a deployment-scope action (defining the group's identity in the experiment), not a member-graph mutation, and is rare enough that bootstrap-class gating is acceptable. If a future use case demands admin-group members to register groups, it can be re-classified then.
- Note that `register_worker` and `reissue_credential` stay admin-principal-only **at the wire level**. The web-ui sign-up flow proxies sign-up through its own deployment-admin-bearing credential (carried as `app.state.admin_store`, the same plumbing that exists today). #140 owns the sign-up route; #143 just preserves the constraint that the web-ui service is the one trusted intermediary for unauthenticated registration. (See §D.5 below for the alternative shape and why it's deferred.)

The §13.3 amendment is the **only normative wire-spec change** this issue makes. It is forward-compatible with v0: existing IUTs that don't admit the worker-class caller continue to be conforming for the bootstrap path; the dual-gate is purely an opt-in expansion.

### D.2 Spec amendments — chapters 02 and 07

**[`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §13.3**:

- Extract `add_to_group / remove_from_group / delete_group` from the "admin-gated — registry mutations" bullet.
- New bullet: "**admin-OR-`admins`-group-gated** — `add_to_group`, `remove_from_group`, `delete_group`. Either the deployment admin principal OR a worker principal whose `worker_id` resolves into the experiment's `admins` group ([`02-data-model.md`](02-data-model.md) §7.5) MAY call these. The dual-gate permits operator-promotion of peers without exposing the deployment admin credential to operator sessions, and preserves the bootstrap path (the initial admin is seeded by the deployment admin before any admin-group member exists)."
- Wire-dispatcher prose paragraph (after the existing "for group-gated worker endpoints" bullet) gains a new bullet: "For admin-OR-group-gated endpoints, accepts the request if EITHER the principal class is `admin` OR the principal is a worker whose id resolves into the named group via `Store.resolve_worker_in_group`."

**[`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §7.5**:

- The reserved-name row for `admins` already says "A deployment SHOULD seed at least one worker into this group at experiment creation; an empty `admins` group makes the affected ops uncallable." Extend with one sentence: "Once seeded, subsequent membership changes MAY be driven by any member of `admins` (chapter 07 §13.3)." Cross-reference back to chapter 07 §13.3.

**[`spec/v0/05-event-protocol.md`](../../spec/v0/05-event-protocol.md)**: no change. The existing `group.member_added` / `group.member_removed` events already carry the `actor` field (the caller's `worker_id` or `admin`) which is sufficient to trace promotions.

**[`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §5**: add a new scenario row under the existing `Workers and groups` group: "admin-OR-group-gated mutation of `admins` — worker in `admins` can `add_to_group`; worker NOT in `admins` is forbidden; deployment admin can `add_to_group`." Primary citation: chapter 07 §13.3 (which is amended above to carry the MUST). Group cluster: continues to belong to v1+roles since admin-group-gated worker behavior is already in that level's scope.

No Pydantic / JSON-schema changes. The `Worker`, `Group`, and `Submission` shapes are unchanged. The amendment is purely behavioral, in the wire dispatcher.

### D.3 setup-experiment changes

[`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh) currently seeds `operator` and `web-ui-1` into the `admins` group at L740-783. The change:

1. **Drop the `web-ui-1 → admins` seeding.** L763-783 (registration of `EDEN_WEB_UI_WORKER_ID` followed by `add_to_group(admins, web-ui-1)`) — keep step 5 (register `web-ui-1` as a worker; the web-ui service still needs its own worker record for the per-service bearer), drop step 6 (`add_to_group`). The bootstrap-summary line at L819 drops the `web-ui admin = …` clause.
2. **Rename the bootstrap variable.** `EDEN_ADMINS_INITIAL_MEMBER` (default `operator`) becomes the operator-name pulled from the human running setup. #140's plan introduces the operator-name argument; #143's setup-experiment changes consume it. If #140 ships the variable as `EDEN_OPERATOR_WORKER_NAME` (or whatever the chosen name is), `EDEN_ADMINS_INITIAL_MEMBER` is rebound to that value (or removed in favor of using the new var directly). **Open: #140's exact variable name; the plan defers final naming to #140's surface and the rename map below pins the EDEN-side identifier.**
3. **Bootstrap idempotency unchanged.** The existing `register_worker` (admin-principal-gated, idempotent on duplicate `worker_id`) + `add_to_group` (admin-principal-gated, idempotent on duplicate `member_id`) pattern works as-is. Under the §D.1 dual-gate amendment, setup-experiment continues to use the deployment admin bearer (only one bearer available at this point) — no change to the call itself.
4. **Migration note in setup-experiment's comment block.** L243-247's existing comment ("The web-ui service. Adding it to `admins` lets operators flip dispatch-mode etc.") becomes outdated and is replaced with a comment explaining the new model: web-ui-1 is service-only; admin authority lives on the operator's session bearer, not on web-ui-1's service bearer.

### D.4 Web UI changes

#### D.4.1 `require_admin` route guard

New helper in [`reference/services/web-ui/src/eden_web_ui/routes/_helpers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py):

```python
def require_admin(request: Request, session: Session) -> RedirectResponse | None:
    """Return None if session.worker_id ∈ admins; else a 403 (or 303 to a `/forbidden` page).

    Reads the membership via app.state.store.resolve_worker_in_group(worker_id, "admins").
    The check is read-only (either-gated wire endpoint), so the session's
    own worker bearer is sufficient — no need for app.state.admin_store.
    """
```

Every `/admin/*` route in `routes/admin_*.py` and `routes/admin/*.py` calls this immediately after `get_session(request)`. On non-admin: render a friendly 403 page (`_error.html` with a "you are not an admin in this experiment" message + link back to `/`) OR redirect to a `/forbidden` page; the choice is cosmetic. Recommendation: render-in-place 403 (avoid an extra round-trip and the redirect-loop hazard if a future change introduces forbidden-redirects-to-admin).

This is the [#NEW-admin-route-gating] check the issue body references. It is **load-bearing**: without it, non-admin operators would see admin pages render (the link in the nav, the GETs would 200) and then get a confusing 403 from the wire on the first POST. The route-level guard surfaces the authorization decision before render.

#### D.4.2 `/admin/groups/admins/` page polish

[`reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py) and [`reference/services/web-ui/src/eden_web_ui/templates/admin_group_detail.html`](../../reference/services/web-ui/src/eden_web_ui/templates/admin_group_detail.html) already exist (chunk 12a-1b). The page already supports `list / detail / register / mutate / delete`. The new work is UX polish:

- **Add a "Promote" affordance.** The current "Add member" form takes a generic `member_id`. On the `/admin/groups/admins/` detail page specifically, the form heading should read "Promote operator to admin" and the input label "Worker name (or id)". The wire call is unchanged (`POST /v0/.../groups/admins/members` with `member_id: <id>`).
- **Confirm-before-promote.** A confirm checkbox or modal — "I confirm I want to grant admin authority to <worker_name>." Mirrors the existing terminate-experiment confirmation pattern (`admin_dispatch_mode.html`).
- **Symmetric demote affordance.** A "Revoke admin" button per member row. Confirmation modal. Wire: `DELETE /v0/.../groups/admins/members/<id>`. The current `admin_group_detail.html` already lists members; add the per-row revoke button.
- **Self-protection rail.** The page MUST NOT allow the signed-in admin to revoke themselves (the last admin can otherwise lock everyone out). Route-level check: if `member_id == session.worker_id`, redirect with `?error=self-revoke-forbidden`. (Less defensive but simpler than "last admin" detection; an admin who wants to revoke themselves first promotes another admin.)
- **Wire bearer**: under §D.1 dual-gate, the wire call uses the session bearer (the admin's worker bearer), NOT `app.state.admin_store`. This is a semantic shift from the existing admin_groups.py — today's mutations route through `admin_store`; with the dual-gate, all `add_to_group / remove_from_group` route through `app.state.store` with the session bearer. Refactor the relevant handler logic to use `request.app.state.store` for these calls (the bearer is the session's per-request store; #140's plan introduces a `request_store(request)` helper or equivalent for binding the session bearer to a per-request StoreClient).

#### D.4.3 Profile-page integration

Issue #140 owns `/profile/`. #143 adds one section: **"Groups."** Lists the operator's transitive group memberships, with `admins` highlighted ("you are an admin" badge) when present. Implementation: the page reads `list_groups` (either-gated) and filters by membership via `resolve_worker_in_group(session.worker_id, group_id)` for each (depth caps from chunk 12a-1b: depth ≤10, breadth ≤1000). For non-admins, the admin nav link is hidden.

Cross-link: a "Promote / revoke other operators" link next to the admin badge points to `/admin/groups/admins/`.

#### D.4.4 Sign-up flow — what changes (relative to #140)

Issue #140 introduces the sign-up form. #143 imposes **two constraints** on it:

1. **MUST NOT add the new worker to any group.** The wire `register_worker` call by itself does not add to any group (verified — the §6.1 endpoint is registry-only). The sign-up handler MUST NOT follow registration with an `add_to_group` call. The constraint is "trust but verify" — codify as a unit test of the sign-up handler that asserts no `add_to_group` is invoked.
2. **The first sign-up is NOT promoted to admin by side-effect.** Even on a fresh deployment with an empty `admins` group, the first sign-up is non-admin. The deployment is unusable for admin ops until setup-experiment seeds an admin or the operator manually promotes via the deployment admin bearer. (The setup-experiment seed handles this on the standard path.)

### D.5 Why `register_worker` stays admin-principal-gated

The alternative is to make `register_worker` unauthenticated (or self-signup-token-gated) so the web-ui sign-up route doesn't need to proxy. Rejected for v0 because:

- Unauthenticated `register_worker` is a public registration endpoint — any external caller can spam-register workers, bloating the registry and the credential-issuance log. The wire spec doesn't currently offer rate-limiting primitives.
- Self-signup tokens (a `signup_token` issued at deployment-bootstrap, distributed out-of-band) would add a third bearer namespace to chapter 07 §13.1; the complexity isn't load-bearing for the v0 experiment-cluster deployments.
- The web-ui proxying model is the existing pattern (today's `app.state.admin_store` plumbing). #140's sign-up form uses it; #143 doesn't disturb it.

The trust-delegation footgun (the web-ui service holds a deployment admin credential) is acknowledged. It is bounded: the credential is used ONLY for `register_worker` on sign-up (and possibly `reissue_credential` on sign-in if #140 routes that through the service rather than directly). Every other admin-gated path uses the session bearer under §D.1.

A future issue can revisit unauthenticated or token-gated sign-up if the trust-delegation surface becomes operationally unpleasant. Not in scope here.

### D.6 CLI changes

The existing `eden-manual` CLI does not need new commands for #143. Under #140's per-user worker identity model, an operator who is in `admins` can already call `add_to_group` / `remove_from_group` via the existing wire — once the §D.1 dual-gate amendment lands, the CLI's call succeeds with the operator's worker bearer.

Two CLI ergonomics touches in scope (both small, both nice-to-have):

1. **`eden-manual whoami` surfaces admin flag.** If `resolve_worker_in_group(self, "admins")` returns true, the CLI prints "you are an admin" in addition to the existing worker_id + worker_name. Mirrors the Profile page's admin badge.
2. **`eden-manual admins list` / `eden-manual admins promote <id-or-name>` / `eden-manual admins revoke <id-or-name>`** — thin wrappers over `list_groups / add_to_group(admins) / remove_from_group(admins)`. The CLI already has the wire plumbing; this is one new subcommand-group covering three operations.

The CLI work is **bundled** in this plan (single PR) per CLAUDE.md "small impl changes alongside spec/UI changes." If the CLI scope grows in implementation, split into a follow-up.

### D.7 Docs

- [`docs/user-guide.md`](../../docs/user-guide.md) §10 (auth principal matrix): rewrite to reflect Model B. The "Web-UI session can" column becomes "Operator session can (non-admin / admin)" with the per-row distinction. The bottom paragraph ("The Web UI is itself a worker (`worker_id=web-ui-1`) …") is replaced with the new model: per-user sessions; `web-ui-1` is service-only; admin authority comes from `admins`-group membership.
- [`docs/user-guide.md`](../../docs/user-guide.md) §8 (admin operations): new subsection "Promoting an operator to admin / revoking admin authority." Covers the web UI flow + the CLI flow + the wire endpoint. Includes a "first admin" note pointing at setup-experiment's seeding.
- [`docs/observability.md`](../../docs/observability.md) §2.1 (admin routes): one-line clarification — `/admin/groups/admins/` is the promotion surface.
- [`.claude/skills/eden-manual-experiment/SKILL.md`](../../.claude/skills/eden-manual-experiment/SKILL.md): one bullet in the setup walk-through — "the operator running setup-experiment is the initial admin; subsequent admins are promoted via `/admin/groups/admins/`."
- [`AGENTS.md`](../../AGENTS.md): no Current-phase change (#143 is a planless-shaped feature on top of #140); CHANGELOG entry suffices.

### D.8 Conformance

One new scenario file under [`conformance/scenarios/`](../../conformance/scenarios/), in the existing workers-and-groups group:

- **`test_admin_or_group_gated_membership_mutation.py`** — asserts:
  - A worker in `admins` can `add_to_group(admins, X)` via the wire.
  - A worker NOT in `admins` (a freshly-registered non-admin worker) is forbidden (403 `eden://error/forbidden`) on `add_to_group(admins, X)`.
  - The deployment admin can `add_to_group(admins, X)` (bootstrap path, unchanged).
  - Symmetric assertions for `remove_from_group`.
  - The `delete_group` operation follows the same dual-gate; one positive test on a non-reserved group is sufficient (reserved groups can't be deleted per §7.5).

Cites chapter 07 §13.3 (which carries the new MUST). Group: `Workers and groups`. Belongs to v1+roles. No level expansion needed.

Run `python conformance/src/conformance/tools/check_citations.py` after authoring.

## 5. Naming map

The plan introduces / renames the following identifiers. Per CLAUDE.md and [`docs/glossary.md`](../../docs/glossary.md):

| Old | New | Why |
|---|---|---|
| Web-UI behavior: every session = `web-ui-1`, transitively admin | Per-operator session bearer (per #140); admin authority by `admins`-group membership | Issue #143 |
| `EDEN_ADMINS_INITIAL_MEMBER` (setup-experiment env var, default `operator`) | Same variable, but bound to the operator's worker name / id from #140's setup flow | Bootstrap operator is the initial admin |
| `web-ui-1 ∈ admins` (setup-experiment seeding) | (retired — `web-ui-1` is service-only, not in `admins`) | §D.3 step 1 |
| Wire `add_to_group / remove_from_group / delete_group` admin-principal-gated | admin-OR-`admins`-group-gated | §D.1 spec amendment |
| `app.state.admin_store` used for `add_to_group` calls | `app.state.store` (session bearer) used; `admin_store` retained ONLY for `register_worker` / `reissue_credential` on sign-up | §D.4.2 wire-bearer shift |
| (new) Web-UI `require_admin(request, session)` helper | n/a | §D.4.1 route guard |
| (new) Profile-page "Groups" section | n/a | §D.4.3 |
| (new) CLI `eden-manual admins {list,promote,revoke}` subcommand-group | n/a | §D.6 |
| (new) Conformance scenario `test_admin_or_group_gated_membership_mutation.py` | n/a | §D.8 |

No EDEN role / verb / kind / artifact identifiers are introduced or renamed; the canonical role pattern in [`docs/glossary.md`](../../docs/glossary.md) is unchanged. The `check-rename-discipline.py` allowlist needs no update.

## 6. Migration / cleanup (pre-external-user posture)

Per CLAUDE.md "No backwards-compatibility shims in greenfield / pre-external-user projects":

- **No deprecation shim for `web-ui-1 ∈ admins`.** The setup-experiment step is removed in the same PR; fresh experiments don't seed it; existing experiments running against the old shape continue to work (web-ui-1 in admins is harmless extra membership) but won't gain the new sign-up flow until they re-bootstrap.
- **No compat path for the old wire gating.** The §13.3 amendment is forward-compatible (it strictly widens the accepted caller set), so existing IUTs don't break; no shim needed.
- **No "transitional" hidden flag** to leave web-ui-1 in admins for a deprecation window. Either you've migrated to per-user sessions (Model B) or you haven't.
- **Operators of in-flight experiments** re-run `setup-experiment` against a fresh data root if they want the new shape (per [`docs/operations/experiment-data-durability.md`](../operations/experiment-data-durability.md)).

The cleanup of `web-ui-1`'s admin role + the route-level admin guard land together. Partial states (admin-route guard without dual-gated wire, or vice versa) are non-goals.

## 7. Files to touch

**Spec (3 files):**

- [`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) — extract `add_to_group / remove_from_group / delete_group` from admin-gated bullet; add "admin-OR-`admins`-group-gated" classification; amend wire-dispatcher prose.
- [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) — append sentence to §7.5 reserved-name table row for `admins`.
- [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) — add scenario row under Workers-and-groups group.

**Setup-experiment (1 file):**

- [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh) — drop the `web-ui-1 → admins` seeding (L763-783, step 6 portion); rebind `EDEN_ADMINS_INITIAL_MEMBER` to the operator name pulled from #140's bootstrap surface (final variable name TBD by #140's resolution); rewrite the comment block at L243-247.

**Web UI (5 files):**

- [`reference/services/web-ui/src/eden_web_ui/routes/_helpers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py) — add `require_admin(request, session)` helper.
- [`reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py) — route `add_to_group / remove_from_group` through session bearer instead of `admin_store`; add self-protection guard on `/admin/groups/admins/`; add `require_admin` call.
- [`reference/services/web-ui/src/eden_web_ui/routes/admin_workers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_workers.py) — add `require_admin` call (every admin route under `/admin/workers/*`).
- [`reference/services/web-ui/src/eden_web_ui/routes/admin/*.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin/) — add `require_admin` call to every handler in `index.py`, `observability.py`, `actions.py`, `work_refs.py` (these are the chunk-12a-1b-split admin sub-package; each handler already calls `get_session`, gain a `require_admin` after).
- [`reference/services/web-ui/src/eden_web_ui/templates/admin_group_detail.html`](../../reference/services/web-ui/src/eden_web_ui/templates/admin_group_detail.html) — "Promote operator to admin" affordance; per-member revoke button; confirm modal; self-protection rail; non-admin nav-link hidden.
- [`reference/services/web-ui/src/eden_web_ui/templates/base.html`](../../reference/services/web-ui/src/eden_web_ui/templates/base.html) — hide the "Admin" nav entry when `session.worker_id ∉ admins`. The template already has `admin_enabled` from `app.state.admin_store is not None`; gain a parallel `is_admin_session` set per-request.
- (And the Profile-page template for the "Groups" section — owned by #140; #143 adds the admins-badge line.)

**CLI (1 file):**

- [`reference/packages/eden-cli/src/eden_cli/`](../../reference/packages/eden-cli/src/) (or wherever `eden-manual` lives) — add `admins {list,promote,revoke}` subcommand-group; surface admin flag in `whoami`.

**Conformance (1 file):**

- [`conformance/scenarios/`](../../conformance/scenarios/) — new `test_admin_or_group_gated_membership_mutation.py`.

**Docs (3 files):**

- [`docs/user-guide.md`](../../docs/user-guide.md) — §10 matrix rewrite; new §8 subsection on promotion.
- [`docs/observability.md`](../../docs/observability.md) — one-line clarification in §2.1.
- [`.claude/skills/eden-manual-experiment/SKILL.md`](../../.claude/skills/eden-manual-experiment/SKILL.md) — initial-admin bullet in setup walk-through.

**CHANGELOG (1 file):**

- [`CHANGELOG.md`](../../CHANGELOG.md) — `[Unreleased]` entry for the issue.

Approximate diff size: ~80 lines spec (mostly §13.3 amendment + prose), ~30 lines setup-experiment, ~250 lines web-ui (route guard + per-route call + template polish + self-protection), ~80 lines CLI, ~100 lines conformance scenario, ~60 lines docs. Total ~600 lines added, ~30 lines removed.

## 8. Test design

**Reference-package + service tests (must pass before push):**

- New unit test in `reference/services/web-ui/tests/` exercising `require_admin`:
  - admin session → admin route renders.
  - non-admin session → admin route returns 403 with the friendly error template.
  - signed-out → admin route redirects to `/signin`.
- New unit test in `reference/services/web-ui/tests/` on the `/admin/groups/admins/` self-protection rail:
  - `POST /admin/groups/admins/members/<session.worker_id>` (revoke-self attempt) → redirect with `?error=self-revoke-forbidden`; underlying wire call not invoked.
- New unit test on the sign-up handler (collaborates with #140's test suite) asserting `register_worker` is NOT followed by `add_to_group` — covers the "non-admin by default" constraint.
- New unit test on `admin_groups.py` confirming `add_to_group` / `remove_from_group` route through `app.state.store` (session bearer), not `app.state.admin_store` — covers the §D.1 dual-gate shift.
- Conformance scenario per §D.8 — new test file.

**Existing tests that need updating:**

- `reference/services/web-ui/tests/` — any test that assumed the session-as-web-ui-1 model needs updating to the per-session-worker model. Most of those are #140's surface; #143 inherits any leftovers.
- `reference/packages/eden-wire/tests/` — server.py dispatcher tests for `add_to_group` etc. need a new case for the dual-gate.
- `reference/scripts/setup-experiment/tests/` (if any) — the `web-ui-1 → admins` assertion must be removed.

**Smokes (must pass before push):**

- `bash reference/compose/healthcheck/smoke.sh` — scripted-mode smoke. Web-ui still operational under the new model; the smoke doesn't test admin flows directly but verifies nothing regresses.
- `bash reference/compose/healthcheck/e2e.sh` — Web UI ideator + admin-reclaim drill. **This is the load-bearing smoke for this chunk** because it exercises the admin-reclaim drill from a session bearer. Today's e2e probably uses the deployment admin bearer or the `web-ui-1`-as-admin pathway; under #140 + #143 it needs to use a session bearer that is in `admins` (so step 1 of the drill probably calls setup-experiment with the operator-name = the e2e's test-bot, and the e2e signs in as that operator).
- `bash reference/compose/healthcheck/smoke-subprocess.sh` and `smoke-subprocess-docker.sh` — no admin-flow surface; should pass unchanged.

**Manual smoke (not automated):**

The operator runs through this scenario once locally before the PR is ready for review:

1. `setup-experiment.sh --experiment-id smoke-143 …` with the operator's git name.
2. Bring up Compose stack; sign in to web UI as the operator.
3. Confirm: Profile page shows "admin" badge; `/admin/*` pages load.
4. Open an incognito browser; sign up as a new operator "bob"; confirm: Profile page does NOT show admin badge; `/admin/*` returns 403; nav-link for Admin is hidden.
5. As the original operator (first browser), go to `/admin/groups/admins/` → promote `bob`.
6. In Bob's browser, refresh; confirm: Profile shows admin badge; `/admin/*` accessible.
7. As Bob, attempt to revoke self → expect self-protection error.
8. As Bob, revoke the original operator → original operator's next page load shows non-admin Profile + 403 on admin pages.
9. As Bob, re-promote original operator (so the deployment still has multiple admins for the rest of the smoke).
10. CLI: `eden-manual --worker-id bob admins list` → returns `[bob, <original operator>]`. `eden-manual --worker-id bob admins revoke <original-operator>` → 200. `eden-manual --worker-id <original-operator> admins promote bob` → 403 (no longer admin).

Document this scenario in a `docs/operations/admin-promotion-walkthrough.md` follow-up if the manual scenario stays useful operationally; otherwise leave it in the PR description for the reviewer.

## 9. Verification gates

Run before any push from impl branch:

```text
uv sync
uv run ruff check .
uv run pyright
uv run pytest -q
uv run pytest -q conformance/
uv run python conformance/src/conformance/tools/check_citations.py
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json
python3 scripts/spec-xref-check.py
python3 scripts/check-rename-discipline.py
python3 scripts/check-complexity.py
bash reference/compose/healthcheck/smoke.sh
bash reference/compose/healthcheck/e2e.sh
```

`smoke-subprocess.sh` and `smoke-subprocess-docker.sh` are not load-bearing for this chunk (no admin-flow surface in the subprocess overlay) but the AGENTS.md "Commands section is the literal pre-push validation gate" rule says run them anyway.

If any smoke fails, do NOT push. Diagnose locally first.

## 10. Tricky areas

### 10.1 Co-shipping vs sequencing with #140

This plan **declares #140 as a strict prerequisite** (§3). The cleanest sequencing:

(a) Land #140 first; rebase #143 against the merged state; run #143 as a follow-up PR.

(b) Co-ship in a single PR (the per-user session bearers + the non-admin-by-default behavior together) if #140's impl PR is in flight and #143 work is ready to layer on it.

(c) Two PRs against a shared integration branch.

The plan does not pre-decide; impl-time selects based on #140's state. The dependency direction is one-way (#143 depends on #140, not vice versa); any deviation needs to be surfaced to the operator before drafting impl.

### 10.2 Self-protection rail vs "last admin"

§D.4.2 chooses "never let the signed-in admin revoke themselves" as the self-protection invariant. The stricter alternative is "never let the LAST admin revoke themselves OR anyone else." The looser invariant (always preserve at least one admin) requires the wire to know who's calling; the chosen invariant is purely UI-side.

The wire spec does NOT enforce a minimum on `admins` membership — an empty `admins` group is permitted (the §7.5 SHOULD says "deployment SHOULD seed at least one worker"). A deployment that wants the stricter rail can implement it at the route layer; the spec is intentionally permissive.

The chosen UI rail is sufficient for the "I accidentally revoked myself" footgun (the most common operator mistake). The "all admins revoke each other simultaneously" failure mode is exotic and not load-bearing for v0; surface as a known limitation in `docs/user-guide.md`.

### 10.3 Trust delegation: web-ui holds deployment admin bearer for sign-up

§D.5 deliberately keeps `register_worker` admin-principal-gated, which means the web-ui service continues to hold `app.state.admin_store` (deployment admin bearer) ONLY to call `register_worker` on sign-up and `reissue_credential` on sign-in. This is a footgun: the web-ui process compromise leaks the deployment admin credential.

Mitigations explicitly NOT shipped in this issue (deferred):

- A separate "sign-up bearer" with a narrower scope (only `register_worker`).
- An unauthenticated-`register_worker` shape (rejected per §D.5).
- A web-ui-process credential isolation pass (file mode, in-memory only, etc.).

These are tracked as follow-up issues at impl time. The current shape is "we already have this footgun pre-#143; this issue doesn't make it worse and doesn't fix it."

### 10.4 Dispatcher classification ordering in chapter 07 §13.3

The amendment adds a new bullet "admin-OR-`admins`-group-gated" between the existing "admin-gated" and "worker-gated" bullets. Ordering matters: the wire dispatcher checks classifications in a fixed order; misordering means the dual-gate either short-circuits to admin-only (if checked after "admin-gated" passes for the admin-principal but the worker-class branch is never tried) or to group-only (if checked before "admin-gated"). The amendment must include explicit prose: "The dispatcher accepts the request if the principal class matches the endpoint class OR (for admin-OR-group-gated endpoints) the principal is a worker resolving into the named group." Either-branch evaluation, not in-order short-circuit.

The conformance scenario (§D.8) exercises both branches explicitly (admin-principal + admin-group-worker + non-admin-worker) to catch misordering.

### 10.5 Reissue-credential is admin-principal-gated; #140 may want to widen

Today `reissue_credential` is admin-principal-gated. #140's sign-in flow may want every operator to reissue their own credential without an admin. If #140 widens reissue_credential to "either admin or self," #143 inherits that change; no §13.3 amendment from #143 is needed for it. Document the dependency in the PR description so impl-time can verify alignment.

### 10.6 `app.state.admin_store` retention

§D.4.2 says "retain `admin_store` ONLY for sign-up / reissue." The cleanup is partial; admin_store stays plumbed through `app.py` and the CLI. Future cleanup (e.g., if `register_worker` shifts to a different bearer scheme) can remove it entirely. Do not remove the plumbing in this PR — it's load-bearing for the sign-up proxy.

### 10.7 Existing admin route handlers' `admin_store`-shape mutations

Today every `admin_*` route handler invokes `app.state.admin_store.<mutation>`. Under §D.4.2 the mutation calls shift to `app.state.store` (session bearer) for `add_to_group / remove_from_group / delete_group`. The shift is per-endpoint, not whole-handler — some routes call multiple wire endpoints, only the membership-mutation ones shift. Audit each handler in `admin_groups.py` to find the affected lines. For handlers that bundle a `register_group` + `add_to_group` (the group-create flow), the `register_group` stays on `admin_store` and the `add_to_group` shifts to `store`. Two distinct calls under the same handler, with two different bearers.

### 10.8 CLI's existing `eden-manual` worker identity

Today's CLI auto-registers `eden-manual` on first use and is NOT in `admins`. Under #140 this auto-register path goes away. #143's CLI changes (the `admins` subcommand-group) assume the operator has registered explicitly. If the operator runs `eden-manual admins promote …` without a registered worker_id, the CLI should error with a clear "register first" message — same shape #140 sets up.

## 11. Risks / things to watch

- **#140's exact shape is in flux.** This plan assumes Model B's session-bearer surface, the Profile page, and the operator-name-driven setup-experiment variable exist. If #140's plan resolves any of these differently (e.g., session-bearer-on-cookie vs in-memory-only; profile-page route under `/me/` instead of `/profile/`; operator-name env var name), #143 inherits the variants. The plan deliberately defers final naming to #140's surface (§5 naming map column).
- **Spec §13.3 amendment review.** Codex-review may push back on introducing a third classification ("admin-OR-group-gated") to the wire spec. Recommendation: hold the line; the alternative (option (a) in §D.1, web-ui proxying) is worse on the trust-delegation axis and asymmetric with CLI. If review surfaces a cleaner shape (e.g., shifting `add_to_group` to plain `admins`-group-gated and adding a one-time deployment-bootstrap path to seed the first admin without going through `admins`-group-gating), evaluate against the bootstrap chicken-and-egg.
- **Dispatcher implementation symmetry.** §10.4 calls out the either-branch evaluation requirement. Conformance scenario covers the misordering case; impl-time codex pass should verify the dispatcher code matches the spec prose. If the existing dispatcher is structured around "admin-gated vs worker-gated" as an exclusive choice, the dual-gate may require restructuring `_check_authorization` in `eden_wire/server.py`. The restructure is small (one new branch) but needs a careful unit test.
- **Self-protection rail false-positives.** §D.4.2's "can't revoke self" is a simple rule but could surprise an operator who legitimately wants to step down. Workaround documented in §10.2. If review pushes back, the alternative is a "confirm with extra friction" modal instead of an outright forbid. The plan picks "forbid" for v0; the operator can post-process by promoting someone first.
- **`/admin/groups/admins/` is one of many admin pages.** The route guard (§D.4.1) must be applied to every admin handler — `/admin/workers/`, `/admin/groups/<G>/`, `/admin/work-refs/`, `/admin/tasks/<T>/reassign`, `/admin/dispatch_mode`, `/admin/events`, `/admin/experiments/*`, `/admin/artifacts/`, plus the chunk-12a-1b-split subpackage. A missed handler is a latent privilege-escalation. Codification: a unit test that enumerates every `/admin/*` route from `app.routes` and verifies each returns 403 for a non-admin session. Mechanical, catches future regression too.
- **Conformance scenario citation grounding.** Per the AGENTS.md "three-legged traceability" pitfall, the new scenario's docstring MUST cite a real MUST in §13.3 (the amendment introduces one — "the dispatcher MUST accept the request if either …"). Verify with `check_citations.py` before push.
- **Migration friction.** Existing experiments running the pre-#143 shape continue to work (web-ui-1 in admins is harmless extra membership); new sign-up flow only activates on fresh setups. If a deployed experiment depends on the "every web-ui session is admin" implicit behavior — and someone re-runs setup or re-bootstraps — they lose admin access until promoting their operator. Document in the user-guide §8 promotion subsection and the CHANGELOG.

## 12. Sequence within the chunk

Execution shape (single impl PR, no multi-wave split needed at this scope):

1. **Confirm #140 has merged.** If not, surface to operator before starting.
2. **Spec amendments first.** Author chapter 07 §13.3, chapter 02 §7.5, chapter 09 §5 row. Run `spec-xref-check.py`, `markdownlint-cli2`.
3. **Wire dispatcher second.** Implement the dual-gate branch in `eden_wire/server.py`. Add unit test covering both branches + the denial case.
4. **Setup-experiment third.** Drop `web-ui-1 → admins` step; rebind `EDEN_ADMINS_INITIAL_MEMBER`.
5. **Web-UI route guard fourth.** Add `require_admin` to `_helpers.py`; call from every `/admin/*` handler; add the enumeration-test that catches future-added handlers without the guard.
6. **`/admin/groups/admins/` UX polish fifth.** Promote / revoke affordances, self-protection rail, wire-bearer shift to session-store.
7. **Profile-page Groups section sixth.** Layered on #140's profile route.
8. **CLI changes seventh.** `whoami` admin flag + `admins {list,promote,revoke}` subcommands.
9. **Conformance scenario eighth.** Author + run `check_citations.py`.
10. **Docs ninth.** §10 matrix + §8 promotion subsection + observability one-liner + SKILL.md.
11. **Validation gates.** Run the full validation suite per §9. Address any failures. Re-run smokes after fixes.
12. **Manual smoke walkthrough.** Run §8 manual scenario locally once.
13. **Codex-review to convergence (plan + impl).** Iterate until no substantive findings remain.
14. **Open impl PR.** Body: spec amendment summary, web-ui changes, migration story, test-plan checklist (verification gates), codex round count, link to #140 + #128.

## 13. Out of scope (followups)

- **Multi-tier RBAC.** Read-only-observer, evaluator-only, integrator-only, etc. Defer until use cases emerge.
- **Promotion-event audit-trail UI.** The events fire today (`group.member_added` / `group.member_removed`); a dedicated audit page is a separate concern.
- **Web-ui credential isolation hardening.** The trust-delegation footgun (§10.3) is acknowledged and unmitigated; a future issue can revisit.
- **Self-signup token / unauthenticated `register_worker`.** §D.5 deferred.
- **`reissue_credential` widening to self-rotate.** Belongs to #140 if it ships there; #143 doesn't require it.
- **CLI `admins {list,promote,revoke}` UX expansion.** The plan covers the minimum surface; richer CLI (e.g., interactive admin selection, batch promotion) is post-MVP.
- **Last-admin protection at the wire.** §10.2 rejects this; an empty `admins` group remains permitted by the spec.
- **#128 final naming resolution.** This plan accepts either kebab-case `worker_id` or `worker_name` in the promotion form; #128's resolution determines which.

## 14. Estimated effort

| Activity | Estimate |
|---|---|
| Spec amendments (chapter 07 §13.3 + chapter 02 §7.5 + chapter 09 §5 row) | ~0.5 day |
| Wire dispatcher dual-gate branch + unit tests | ~0.5 day |
| setup-experiment.sh changes + test updates | ~0.25 day |
| Web-UI `require_admin` + per-handler call + enumeration test | ~0.5 day |
| `/admin/groups/admins/` UX polish + self-protection rail + wire-bearer shift | ~0.75 day |
| Profile-page Groups section (layered on #140) | ~0.25 day |
| CLI `whoami` + `admins` subcommands | ~0.5 day |
| Conformance scenario + citations | ~0.25 day |
| Docs (§10 matrix + §8 subsection + observability + SKILL.md) | ~0.5 day |
| Validation gates (lint, type, conformance, smokes, manual walkthrough) | ~0.5 day |
| Codex-review iterations (plan + impl, ~2 rounds each) | ~1 day |
| **Total** | **~5.5 days** |

Mid-sized for a feature chunk: spec touches one chapter substantively, wire one branch, web-ui multiple files, conformance one new scenario, docs three files. Codex-review iteration on the spec amendment is the dominant variable.
