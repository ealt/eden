# Issue #140 — Operator identity as a registered worker (Model B)

GitHub: [#140](https://github.com/ealt/eden/issues/140). Cluster: `identity`. Priority: `2-planned`.

## 1. Context

Today the human operator has no first-class identity in EDEN. Their actions are attributed to whichever **tool** mediated them, not to the human who decided to act:

| Surface | Bearer principal today | What it really represents |
|---|---|---|
| Web UI (signed in) | `web-ui-1` (`app.state.store`) | the web-ui *service*, not the human |
| Skill / raw CLI | `eden-manual` (CLI default `--worker-id`) | the CLI *tool* |
| Auto-host workers | `ideator-1` / `executor-1` / `evaluator-1` | the auto-host *process* |

`Idea.created_by`, `Variant.executed_by`, `Variant.evaluated_by`, `Task.submitted_by` all record the tool. Two humans on the same UI are indistinguishable; one human on UI vs CLI records as two identities.

This plan realizes **Model B** from the issue: the operator IS a worker. One registered `Worker` record carries their identity across UI, CLI, and skill. `web-ui-1` is removed entirely (per the issue refinement comment); `eden-manual` as a shared CLI identity is retired in favor of human-named operator workers. The Phase 12a-1 §D.5b retrofit (per-user session bearers via `reissue_credential` at sign-in) — deferred when 12a-1 shipped ([`docs/plans/eden-phase-12a-1-worker-identity.md`](eden-phase-12a-1-worker-identity.md) §D.5b) — is the web-ui half of this work.

### 1.1 What "Model B" buys, precisely

Three pains, restated as testable properties:

1. **Attribution is correct.** Every `*_by` field on an idea / variant / task records the human's opaque `worker_id`, not the mediating tool's.
2. **Surface continuity.** UI and CLI for the same operator act as the **same opaque `worker_id`**, so their work history threads together under one identity. (Continuity is *attribution-level*: the same id is used on both surfaces. It is **not** a guarantee that both surfaces hold a simultaneously-valid credential — see §4.3 for the credential-rotation semantics this design accepts.)
3. **Multi-operator deployments.** Two humans on the same UI register as distinct workers; their actions don't collide in attribution.

## 2. Decisions captured before drafting

Settled by the issue body + the refinement comment + the discussion that produced them. Not up for re-litigation in codex-review unless review surfaces a load-bearing contradiction with a spec MUST.

1. **Model B, not Model A.** One human → one worker identity, used everywhere. No separate `operator_id` + `worker_id` attribution pair. (Model A rejected in the issue: cleaner tooling provenance, more concepts; Model B matches the operator's mental model.)
2. **The `web-ui-1` *worker* identity is removed, not demoted — but `admin_store` stays.** Two distinct credentials must not be conflated. (i) The **`web-ui-1` worker bearer** (`app.state.store`, `EDEN_WEB_UI_WORKER_ID`) — used today to stand in for human actions — is removed entirely: `web-ui-1` is not registered at setup-experiment time, and operator session bearers become the only *worker* bearers the web UI uses. (ii) The **deployment-admin bearer** (`app.state.admin_store`, `admin:$EDEN_ADMIN_TOKEN`) is **retained** and continues to back every admin-principal-gated wire op the web UI proxies today — `register_worker` (sign-up + `/admin/workers/register`), `reissue_credential` (login + `/admin/workers/<id>/reissue`), `register_group` / `delete_group` / group-membership mutations (`/admin/groups/*`), and the `AdminGateMiddleware` membership-check reads ([`reference/services/web-ui/src/eden_web_ui/routes/admin_workers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_workers.py), [`routes/admin_groups.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py)). (The group-membership-mutation bearer shift from `admin_store` to the session bearer is **#143**'s dual-gate work, not this issue's.) The startup `/whoami` worker-bearer self-verify is removed (it existed only because the `web-ui-1` worker existed — circular); the separate `admin_store` startup probe (`admin_store.list_workers()`, [`cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py)) stays.
3. **`eden-manual` shared identity is retired.** The CLI's `--worker-id eden-manual` default and its auto-registration-on-first-claim path are removed. Operators register an explicitly-named worker and pass `--worker-id` (or `EDEN_WORKER_ID`).
4. **Service workers stay self-registering, out of this issue.** The auto-host services (`ideator-host`, `executor-host`, `evaluator-host`, `orchestrator`) keep their existing `bootstrap_worker_credential` startup flow. They are deployment infrastructure; their identity model is **#141**'s concern, not this issue's.
5. **No `service: bool` flag on the Worker record.** The protocol does not distinguish operator workers from service workers at the data-model level — they are all `Worker` records; the difference is purely *how they are created*. The issue calls a flag a possible nice-to-have but not required; we defer it (a display-only refinement is tracked as a follow-up, §12).
6. **Default-non-admin for new sign-ups is OUT of scope here.** The authorization half (who in the worker population is an admin, the admin-promotion flow, the §13.3 dual-gate) is **#143**. This issue ships only the *principal* half (each operator registers + bears their own credential). #143 layers authorization on top. <!-- rename-discipline:cite -->
7. **Pre-external-user posture: clean break.** No compat shims. Existing experiments under the `web-ui-1`-shared-bearer pattern are re-bootstrapped; pre-cutover checkpoints are non-restorable. Documented in §8.

## 3. Strict prerequisite: #128 must land first

This plan **does not stand alone**. Its entire UX ("operator enters a *name*, the system returns an opaque *id*") is exactly the id/name split that [#128](https://github.com/ealt/eden/issues/128) introduces ([`docs/plans/identity-id-name-disambiguation.md`](identity-id-name-disambiguation.md)). After #128:

- `register_worker(name=…)` **mints** an opaque `worker_id` (`wkr_<ULID>`) and stores the operator-supplied `Worker.name` as a display label. The caller no longer supplies the id.
- `worker_name` MAY collide; the opaque `worker_id` is the stable, copy-paste-able handle the Profile page surfaces and the CLI consumes.

**Dependency direction is one-way (#128 → #140).** If #128 has not merged when this plan reaches impl, **do not start** — surface to the operator and either rebase against an in-flight #128 branch or defer. Co-shipping #128 + #140 in one PR sequence is fine; reversing the order is not.

### 3.1 Fallback if #128 slips (degraded, documented)

If the operator chooses to ship #140 ahead of #128 (the issue acknowledges this is possible but degraded), the sign-up form must accept an operator-supplied kebab-case `worker_id` (the current §6.1 grammar) and enforce uniqueness with a collision error; the "copy your id from Profile → paste into CLI" loop then copies the operator-typed id rather than an opaque one. This plan is written for the post-#128 shape; the fallback is a one-paragraph impl note, not a parallel design. **Recommendation: hold the strict-prerequisite line and do not ship the fallback** unless the operator explicitly de-couples the two.

## 4. Design

### 4.0 Identity model: operator workers vs service workers

Two *conventions* over one `Worker` shape (no protocol distinction):

| Class | Created by | `worker_id` shape | Bears credential where | Drives |
|---|---|---|---|---|
| **operator worker** | human, via web-ui sign-up or `eden-manual register` | opaque `wkr_<ULID>` (post-#128), operator-supplied `name` | session cookie (UI) / `~/.credentials.json`-equiv (CLI) | human claim / submit / create actions |
| **service worker** | auto-host startup `bootstrap_worker_credential` | deployment-chosen (`ideator-host-1`, …) | on-disk `--credentials-dir` | autonomous polling-loop actions |

`web-ui-1` was neither cleanly — it was a service identity standing in for humans. This issue eliminates that conflation by removing `web-ui-1` and giving humans their own operator workers.

### 4.1 Spec / contract impact (minimal)

Per the issue: "the spec contract barely changes." Confirmed against the spec:

- **[`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §13 (auth):** **no normative change.** Per-session bearer issuance is implementation-defined for the web UI; the `register_worker` / `reissue_credential` / `verify_worker_credential` (whoami) ops the web-ui and CLI use already exist (§6.1, §6.3, §6.4) and are unchanged. The §13.3 *authorization* classification is **not** touched here — the admin-OR-`admins`-group dual-gate is #143's amendment.
- **[`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §6 (workers):** **no schema change** beyond #128's name/id split (which #128 owns). No new field (per decision §2.5).
- **[`spec/v0/03-roles.md`](../../spec/v0/03-roles.md):** **no role-binding change.** Operator workers have the same role capabilities as auto-host workers in their target groups.
- **Optional clarifying note (non-normative, SHOULD-author):** a one-paragraph note — in chapter 02 §6 or [`docs/glossary.md`](../glossary.md) — stating that "human operator identity" and "service worker identity" are both modeled as `Worker` records and the distinction is **deployment convention, not protocol structure**. The issue says this "or leave it implicit." **Recommendation: author it in the glossary** (§4.6 below), not the spec — it is a deployment-convention observation, and codifying it as spec prose risks reading as a normative distinction the data model deliberately does not make.

**No JSON-schema changes. No Pydantic-model changes. No wire-binding changes.** This is the key scoping fact: #140 is an impl + docs chunk that rides on contracts #128 and 12a-1 already established.

### 4.2 Web UI changes

Files under [`reference/services/web-ui/src/eden_web_ui/`](../../reference/services/web-ui/src/eden_web_ui/). **Framing correction (codex round 0):** this is an **app-wide identity refactor**, not a narrow per-page route swap. `app.state.store` (the `web-ui-1` worker bearer) and `app.state.worker_id` (the process worker id used as the actor on writes) are threaded through the landing page, the ideator / executor / evaluator pages, the admin dashboard / observability / work-refs / workers / groups routes, the admin reassign + dispatch-mode *actor stamping*, and the `AdminGateMiddleware`. Wave 2 must migrate **all** of them, and the test harness assumes an in-process store rather than a per-session bearer (§4.2.2). The subsections below pin the inventory and the store-factory abstraction that make the migration mechanical.

#### 4.2.1 Session cookie carries the operator's credential

Today [`sessions.py`](../../reference/services/web-ui/src/eden_web_ui/sessions.py) `Session` carries `{worker_id, csrf, selected_experiment_id?}` and **every** wire call uses the process-level `app.state.store` (`web-ui-1`) regardless of who is signed in ([`routes/ideator.py`](../../reference/services/web-ui/src/eden_web_ui/routes/ideator.py) `store = request.app.state.store`). The retrofit:

- Extend `Session` with the operator's `registration_token` (the credential half of `<worker_id>:<token>`), stored in the itsdangerous-signed cookie alongside `worker_id` + `csrf`.
- **Security posture is a real change, not "same envelope, same properties."** [`sessions.py`](../../reference/services/web-ui/src/eden_web_ui/sessions.py) uses `itsdangerous.URLSafeSerializer`, which **signs but does NOT encrypt** — the cookie value is integrity-protected, not confidential. After this change the cookie carries a **bearer-equivalent secret** readable by anyone who can read the raw cookie value. `httponly` keeps it out of page JS but not out of a copied cookie. This **breaks the current [`README.md`](../../reference/services/web-ui/README.md) §security invariant** ("the per-worker bearer and the admin token never reach … any session cookie"). Consequences: (i) the README invariant must be rewritten **in wave 2** (a contract change, not a wave-3 docs-cleanup); (ii) `--secure-cookies` is mandatory for any non-localhost deployment (the token is sniffable over plain HTTP otherwise) — documented + recommend defaulting it on outside localhost; (iii) consider switching the codec to `URLSafeTimedSerializer` + an encrypting layer as a follow-up (§11) — out of scope here, but the signed-not-encrypted limitation is stated plainly rather than glossed.
- **`Session` is constructed in more than the auth route — inventory every site.** `Session(...)` is built in [`routes/auth.py`](../../reference/services/web-ui/src/eden_web_ui/routes/auth.py) (sign-in) **and** [`routes/admin_experiments.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_experiments.py) (the `/admin/experiments/<id>/select` experiment-switch path rebuilds the session) **and** [`sessions.py`](../../reference/services/web-ui/src/eden_web_ui/sessions.py) (decode). Every construction/rewrite site MUST preserve `registration_token` — the experiment-switch path in particular silently drops the operator's bearer if it rebuilds `Session` without copying it. A unit test asserts the token survives an experiment switch.

#### 4.2.2 Request-scoped store factory (the load-bearing feasibility fix)

The naive "add `wire_client_for_session(session) -> StoreClient`" helper does **not** drop into the current architecture, and the plan must say so. Two facts from the code:

- `make_app()` is typed around the **generic `Store` protocol** ([`app.py`](../../reference/services/web-ui/src/eden_web_ui/app.py)), not the concrete `StoreClient`. The bearer is an HTTP-binding concept that only `StoreClient` has; an in-process `Store` (e.g. `InMemoryStore` / `SqliteStore`) has no bearer at all.
- The test suite builds the app with an in-process store for **both** `store` and `admin_store` ([`tests/conftest.py`](../../reference/services/web-ui/tests/conftest.py)). A helper that hard-constructs a real `StoreClient` would bypass those in-process paths and force a test-harness rewrite.

So the abstraction is a **request-scoped store factory** on `app.state`, not a bare `StoreClient` constructor:

- `app.state.session_store_factory: Callable[[Session], Store]`. For the production `StoreClient` backend it returns a `StoreClient` re-bound to `<session.worker_id>:<session.registration_token>` (cheap — same base URL + experiment id, different bearer). For an in-process store it returns a thin `Store` wrapper that carries the session's `worker_id` so attribution (`claim` / `submit` / `create_*` actor) is recorded as the operator, preserving the existing in-process test paths.
- A route helper `session_store(request) -> Store` reads the session, calls the factory, and returns the per-request store. **Routing rule (from 12a-1 §D.5b):** every human-driven wire call (claim / submit / create_idea / create_variant on ideator / executor / evaluator pages; admin reassign + dispatch-mode + reclaim) MUST go through `session_store(request)`, never `app.state.store`. A route without a session can't construct one.
- **Scope guard (codex edge case):** the factory is **default-experiment-only**. The existing `/admin/experiments/<id>/select` flow stores `selected_experiment_id` in the session but does **not** swap the per-route store's experiment binding today ([`routes/admin_experiments.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_experiments.py)). The factory MUST NOT start honoring `selected_experiment_id` as part of this change — doing so silently drags in an unplanned multi-experiment semantic change. Out of scope; stated explicitly so impl doesn't accidentally couple the two.

#### 4.2.3 Inventory: every `app.state.store` and `app.state.worker_id` site

The migration is mechanical but **must be exhaustive** — a missed `app.state.worker_id` actor-stamp leaves Model B broken for that action even when claim/submit are fixed. The two sweeps:

- **`app.state.store` (read + human-driven write):** landing [`routes/index.py`](../../reference/services/web-ui/src/eden_web_ui/routes/index.py), [`routes/ideator.py`](../../reference/services/web-ui/src/eden_web_ui/routes/ideator.py), [`routes/executor.py`](../../reference/services/web-ui/src/eden_web_ui/routes/executor.py), [`routes/evaluator.py`](../../reference/services/web-ui/src/eden_web_ui/routes/evaluator.py), [`routes/admin/index.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin/index.py), `routes/admin/observability.py`, `routes/admin/work_refs.py`, `routes/admin_workers.py`, `routes/admin_groups.py`, and [`middleware.py`](../../reference/services/web-ui/src/eden_web_ui/middleware.py). Each hit is routed to `session_store(request)` (human-driven actions + reads that should be authorized as the operator) or `app.state.admin_store` (the admin-principal-gated proxies enumerated in §2.2 + the middleware membership check). Grep `app.state.store` and classify each; the classification is the wave-2 work.
- **`app.state.worker_id` (actor stamping):** the admin reassign + dispatch-mode handlers stamp the actor from the process worker id today ([`routes/admin/actions.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin/actions.py)), and auth/session bootstrap reads it ([`app.py`](../../reference/services/web-ui/src/eden_web_ui/app.py), [`routes/auth.py`](../../reference/services/web-ui/src/eden_web_ui/routes/auth.py)). These MUST switch to `session.worker_id` so admin-originated actions attribute to the human. Removing `app.state.worker_id` entirely (rather than leaving a stale default) is the forcing function that surfaces any missed site at type-check time.

#### 4.2.4 Sign-up / login / logout / profile

Current auth surface is `/signin` (GET form + POST "Continue as `<worker-id>`") and `/signout` ([`routes/auth.py`](../../reference/services/web-ui/src/eden_web_ui/routes/auth.py)). Replace with:

- **`GET /signup` + `POST /signup`** — operator enters a `name`. Handler calls `register_worker(name=…)` via `app.state.admin_store` (admin-gated; the web-ui is the trusted unauthenticated-registration proxy, the same posture #143 §D.1 preserves). Wire returns `{worker_id, registration_token}`. Handler writes `{worker_id, registration_token, csrf}` into the session cookie and redirects to `/`. The `registration_token` is the **once-only** plaintext from §6.1 — the cookie is where the UI persists it.
- **`GET /login` + `POST /login`** — operator enters an existing opaque `worker_id` (copied from their CLI session, their Profile page on another browser, or the sign-up confirmation). Handler obtains a credential for that id via `reissue_credential(worker_id)` over `app.state.admin_store`, confirms with `whoami` over the freshly-issued bearer, and establishes the session. **This rotates the worker's credential** — see §4.3 for the accepted semantics.
- **`POST /logout`** — clears the **browser** session cookie. Replaces today's `/signout`. **Codex edge case — logout does NOT revoke the credential.** Because the cookie now carries a bearer-equivalent `registration_token`, deleting the cookie locally does not invalidate the server-side credential; a cookie copied/exfiltrated before logout stays usable until the next `reissue_credential` rotates the token. The plan **accepts this explicitly** for v0 (it matches the CLI's on-disk credential, which logout-equivalent `rm` also doesn't revoke server-side). An optional hardening — call `reissue_credential(worker_id)` on logout to rotate-and-orphan the just-cleared token — is noted as a follow-up (§11); it has a cost (it breaks any *other* live session/CLI for that worker, same as login rotation) so it is not the default.
- **`GET /profile/`** — logged-in operator sees + copies their `worker_id` (the handle to paste into the CLI), sees their `worker_name`, and sees their list of created ideas / variants / claimed tasks (filtered by `created_by` / `executed_by` / `evaluated_by` / claim-owner == `session.worker_id`). #143 later layers a "your groups / admin flag" section onto this page.

Naming: the verb family stays `sign*` to avoid a synonym split with the existing `/signin`/`/signout` — see §5. (The issue's prose "login / logout" maps onto the route family the operator review approves.)

#### 4.2.5 Remove the `web-ui-1` worker plumbing (NOT `admin_store`)

- Delete the startup **worker-bearer** `store.whoami()` self-verify in [`cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py). The web-ui's health is observable by whether it serves requests; no separate worker self-credential probe is needed. **Keep** the separate `admin_store.list_workers()` startup probe in the same file — it verifies the deployment admin token, which is still load-bearing (§2.2).
- Remove `app.state.store` (the `web-ui-1` worker `StoreClient`) and its `resolve_worker_bearer(..., labels={"role": "web-ui"})` bootstrap, per §4.2.3's inventory. **Keep `app.state.admin_store`** — it backs the full admin-principal-gated proxy surface enumerated in §2.2 (`register_worker` / `reissue_credential` / `register_group` / `delete_group` / group-membership mutations) **plus** the `AdminGateMiddleware` membership-check reads, **not** merely "sign-up + membership reads" as an earlier draft of this plan claimed.
- Drop the `--worker-id` / `EDEN_WEB_UI_WORKER_ID` CLI flag + env var from the web-ui service.

##### `admin_store is None` posture — the web-ui hard-requires an admin token after #140 (codex round 1)

Today the web-ui has a tested **admin-disabled** posture: when `admin_store is None`, admin writes are disabled but read paths still work ([`README.md`](../../reference/services/web-ui/README.md), [`tests/conftest.py`](../../reference/services/web-ui/tests/conftest.py)). That posture was viable because the `web-ui-1` *worker* bearer (`app.state.store`) backed reads independently of the admin token. Under Model B that independent worker bearer is gone, and **three** load-bearing paths now depend on `app.state.admin_store`: `/signup` (`register_worker`), `/login` (`reissue_credential`), and the `AdminGateMiddleware` membership check (§4.2.6). So "no admin token" stops meaning "admin writes disabled" and starts meaning **"no operator can establish a session at all, and the admin gate is un-evaluable."**

**Decision: the web-ui hard-requires `--admin-token` / `EDEN_ADMIN_TOKEN` at startup after #140.** `make_app` (or `cli.py`) fails fast with a clear error (`web-ui requires a deployment admin token (--admin-token / $EDEN_ADMIN_TOKEN) to proxy operator sign-up/login under the operator-as-worker model`) when it is absent, rather than booting into a half-functional state where the landing page renders but no one can sign in. This **retires the admin-disabled posture for the web-ui** (a pre-user clean break, CLAUDE.md "no compat shims"). The README's admin-disabled documentation + the `conftest.py` admin-disabled fixture are updated/removed in wave 2. (Rejected alternative: keep booting and render an "admin-disabled — sign-up/login unavailable" error on `/signup` `/login` + a read-disabled `/admin/*` — more states to maintain for a posture that has no real use case once the web-ui's only credential is the admin token.)

#### 4.2.6 Admin-route read-leak under per-user bearers (interaction with #144)

[#144](https://github.com/ealt/eden/issues/144) (CLOSED) added `AdminGateMiddleware` which gates `/admin/*` on `session.worker_id ∈ admins` via `resolve_worker_in_group`. That gate already exists and is preserved. **Bearer swap to verify at impl time:** the middleware performs the membership check using `app.state.store` (the `web-ui-1` bearer) today ([`middleware.py`](../../reference/services/web-ui/src/eden_web_ui/middleware.py)). With the `web-ui-1` worker removed, the check must use `app.state.admin_store`. Load-bearing — a missed swap turns the admin gate into a hard 500.

**Correction (codex round 0): the exhaustive per-route admin enumeration test does NOT exist yet.** The current [`tests/test_admin_gate.py`](../../reference/services/web-ui/tests/test_admin_gate.py) is **representative-by-sub-router** and explicitly states per-route exhaustiveness is not needed — enough to prove prefix gating, not enough to prove every route that rewrites session state or stamps `app.state.worker_id` was migrated. The *exhaustive `require_admin` enumeration test* is **#143's** deliverable (its §D.4.1), since #143 owns the `require_admin` route guard. For #140's scope, wave 2 ships a **migration-correctness test** instead (§6): drive a human action through each role page + the admin reassign/dispatch-mode handlers and assert the resulting attribution (`created_by` / `submitted_by` / `reassigned_by` / `updated_by`) equals the **session** `worker_id`, not the process worker id — this is what proves the `app.state.store`/`app.state.worker_id` sweep is complete.

### 4.3 Credential-rotation semantics (load-bearing — surface at plan review)

The 12a-1 §D.5b retrofit offered two shapes for "establish a session as worker `W`":

- **(a) reissue-at-login.** UI calls `reissue_credential(W)` (admin-gated) → fresh token → stored in the session cookie. The prior credential is invalidated. **Single-active-credential-per-worker** is the consequence.
- **(b) admin-derived session delegation.** UI mints a short-lived admin-signed bearer scoped to `W`'s id + session lifetime that the Store accepts as standing in for `W`'s credential. Multiple concurrent sessions; **new wire auth primitive** (a §13 amendment that touches the chapter-9 §6 IUT contract).

**This plan ships (a)**, matching 12a-1 §D.5b's choice and the issue's own login flow ("UI verifies via /whoami" presumes the UI obtained a credential for the typed id, which only `reissue_credential` provides). Consequences, stated plainly — the tradeoff is sharper than "single active credential" (codex round 0):

- "Surface continuity" (pain #2) is delivered at the **attribution level**: UI and CLI act as the *same opaque `worker_id`*, so all work threads under one identity. It is **not** delivered as simultaneous live credentials — logging into the UI rotates the token, invalidating the CLI's cached credential (and vice-versa).
- The real cost is **single-active-credential PLUS admin-token-dependent CLI recovery**: the CLI's self-heal (§4.4) re-mints via `reissue_credential`, which is **admin-gated** (§6.3) — so cross-surface switching is only self-healing on a CLI that holds `EDEN_ADMIN_TOKEN`. A pure-worker CLI (no admin token) that gets its token rotated out from under it by a UI login is **stranded**: it cannot reissue, and a fresh `register` mints a *new* id (§4.4), breaking the very continuity Model B promises. In the reference Compose stack the `eden-manual` CLI always loads `EDEN_ADMIN_TOKEN` from `.env` ([`eden-manual`](../../reference/scripts/manual-ui/eden-manual)), so recovery is feasible there; the plan states the dependency rather than assuming it.
- To keep cross-surface switching self-healing on the reference CLI, the CLI retains a **narrow reissue-on-stale recovery path** (§4.4): a cached credential that 401s triggers `reissue_credential` *for the known `worker_id`* + re-persist. This is distinct from the *auto-registration-on-first-claim* path this issue removes — recovery of an already-registered id stays (admin-token-gated); lazy creation of a brand-new identity goes.

**Why not (b):** option (b) is a new auth primitive whose acceptance rule lives in §13 — amending it touches the IUT-contract boundary (a stop condition for this plan). It is the right answer if multi-session-per-operator becomes a requirement; tracked as a follow-up (§12). **Why not require pasting the full `worker_id:token` bearer at login (no rotation):** it forces the operator to copy a secret through the clipboard (worse UX + secret-handling hazard) and contradicts the issue's id-only login. Recorded as the rejected alternative.

> **Plan-review decision point.** Shipping (a) means "log into the UI ⇒ your CLI's next command must self-heal via reissue." If the operator wants true concurrent UI+CLI sessions for one identity, that is option (b) and a scope expansion that touches §13 — flag at review.

### 4.4 CLI changes

[`reference/scripts/manual-ui/eden-manual`](../../reference/scripts/manual-ui/eden-manual) (a standalone argparse script; credentials at `${EDEN_MANUAL_WORK_ROOT:-/tmp/eden-manual}/.credentials.json`, keyed `worker_id → registration_token`).

- **`eden-manual register <name>`** — NEW subcommand. Calls `register_worker(name=…)`, persists the returned `worker_id → registration_token` to `.credentials.json`, prints the `worker_id` (the copy-paste handle). Post-#128 the name is the only operator input; the id is minted.
- **`eden-manual whoami`** — NEW subcommand. Resolves the active `worker_id` (from `--worker-id` / `EDEN_WORKER_ID` / single-cached-credential), calls `GET /whoami` with that bearer, prints `worker_name` + `worker_id`. The "did I remember to register?" check.
- **`--worker-id <id>`** on **every** existing subcommand (`claim`, `ideation-submit`, `execution-submit`, `evaluation-submit`, and — for symmetry — the read/admin subcommands that take a worker context). Mirrors how `--experiment-id` is wired today (resolved via a shared arg + `_exp(env)` helper). Currently `--worker-id` exists only on `claim` (default `"eden-manual"`); generalize it and **drop the default**.
- **`EDEN_WORKER_ID` env var** — default for `--worker-id` when the flag is omitted, mirroring `EDEN_EXPERIMENT_ID`. Resolution order: explicit `--worker-id` > `EDEN_WORKER_ID` > (if exactly one cached credential) that id > **clear error**.
- **Remove auto-registration-on-first-claim.** Today `_worker_bearer()` (~L261-287) registers/reissues a brand-new `eden-manual` worker on first use. After this: a subcommand that needs a worker bearer and finds no cached credential for the resolved `worker_id` errors with `run 'eden-manual register <name>' first, or pass --worker-id / set EDEN_WORKER_ID`. **Retain** the narrow reissue-on-stale branch (cached token 401s → `reissue_credential` for the *known* id → re-persist) per §4.3 — this is recovery for an existing identity, not lazy creation of a new one. **Recovery requires the admin token** (`reissue_credential` is admin-gated, §6.3); document that the reissue-on-stale path is a no-op on a CLI without `EDEN_ADMIN_TOKEN`.
- **Separate "401 stale token" from "row gone after re-bootstrap" (codex round 0).** The current `_worker_bearer` ladder falls back from a failed `register_worker` to `reissue_credential` (~L247/L283). Under #128, `register_worker(name=…)` **mints a new opaque id** — so it can NOT recover a *wiped* row's identity (a re-bootstrapped experiment has no record of the old `worker_id`; "re-register" produces a different id and orphans the operator's history). The CLI must distinguish: (i) **stale credential, row present** → `reissue_credential(known_id)` recovers in place; (ii) **row absent** (404/`NotFound` on `whoami`/`read_worker` for the cached id) → there is no in-place recovery; the CLI surfaces "your worker_id is no longer registered (the experiment was likely re-bootstrapped); run `eden-manual register <name>` to obtain a new identity — prior work stays attributed to the old id." Collapsing these two into the old register-or-reissue fallback is the bug; the plan calls it out so impl narrows the exception handling (mirrors AGENTS.md's "narrow exception handling on store reads" pitfall).
- The per-task `.claims.json` keying is unchanged; submit subcommands still pick the bearer matching the claim's recorded `worker_id`.

### 4.5 setup-experiment changes

[`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh).

- **Remove `web-ui-1` registration + admins membership** — step 5 (`register_worker(EDEN_WEB_UI_WORKER_ID)`, L757-766) and step 6 (`add_to_group(admins, web-ui-1)`, L769-777). Per decision §2.2, `web-ui-1` does not exist after this. Remove the `EDEN_WEB_UI_WORKER_ID` env-var generation (L244-245) and its `.env` emission (L501). Drop the bootstrap-summary `web-ui admin = …` clause.
- **Initial-admin operator — this is NOT a rename-only change (codex round 0).** Today step 3 posts a caller-supplied `worker_id` (`register_worker(EDEN_ADMINS_INITIAL_MEMBER)`, L734-746) and step 4 reuses that *same string* as `member_id` in `add_to_group(admins, …)` (L748-755). Under #128, `register_worker(name=…)` **mints an opaque `worker_id`** and the caller no longer supplies it, while `add_to_group` still takes the opaque `member_id` ([`identity-id-name-disambiguation.md`](identity-id-name-disambiguation.md)). So the bootstrap must **capture the minted id from the register response and thread it into the `add_to_group` call** — it cannot keep reusing the operator-supplied name as the membership key. Concretely: register with `{name: "$EDEN_OPERATOR_WORKER_NAME"}`, parse `worker_id` out of the 200 response JSON (a `jq -r .worker_id` step the script doesn't have today), then `add_to_group(admins, member_id=<captured-id>)`. The *name* input variable is `EDEN_OPERATOR_WORKER_NAME` (default derived from git user, falling back to `operator`) — see §5. This is the env var #143 §D.3 defers to #140 to pin.

  **Idempotency needs a durable persisted id, not name-lookup (codex round 1).** Under #128 names MAY collide and are NOT uniquely resolvable — the `?name=` query returns 0..N matches ([`identity-id-name-disambiguation.md`](identity-id-name-disambiguation.md)), so "look the operator up by name on rerun" is ambiguous and cannot be the recovery key. The canonical persisted source is a **new `EDEN_OPERATOR_WORKER_ID` written back to `.env`** (the same place setup-experiment already persists generated ids/tokens, read on rerun via the existing `read_env_key` helper). Concretely: on **first** setup, register with `{name: "$EDEN_OPERATOR_WORKER_NAME"}`, capture the minted `worker_id` from the response (`jq -r .worker_id`), persist it as `EDEN_OPERATOR_WORKER_ID` in `.env`, then `add_to_group(admins, member_id=$EDEN_OPERATOR_WORKER_ID)`. On **rerun**, `EDEN_OPERATOR_WORKER_ID` is already in `.env` → skip the (id-minting) register and just re-assert `add_to_group(admins, $EDEN_OPERATOR_WORKER_ID)` (idempotent on member id). This makes the seeded admin stable across reruns against the same data root without relying on collidable names. (If #128 hasn't landed — the §3.1 fallback — step 3/4 stay rename-only because the operator-supplied id is still both the registration key and the membership key, and no minted-id capture is needed.)
- Auto-host worker pre-registration (L799-808, `ideator-1`/`executor-1`/`evaluator-1`) is unchanged here — it is #141's surface.

### 4.6 Skill + docs changes

- **[`.claude/skills/eden-manual-experiment/SKILL.md`](../../.claude/skills/eden-manual-experiment/SKILL.md):** add an **operator-registration step** between "spin up" (Phase 3) and "hand-off to role skills" (Phase 4): detect whether a cached operator credential exists; if not, suggest a worker name (default from `git config user.name`), run `eden-manual register <name>`, confirm + surface the minted `worker_id`. Subsequent invocations reuse the cached identity.
- **[`.claude/skills/eden-manual-{ideator,executor,evaluator}/SKILL.md`](../../.claude/skills/):** replace every `--worker-id eden-manual` with `--worker-id "$EDEN_WORKER_ID"` (or the registered id), and update the credential-lifecycle prose (the "registered lazily on first use" line is now "registered explicitly via `eden-manual register`").
- **[`docs/user-guide.md`](../user-guide.md):** rewrite §2 setup-experiment to include operator registration as the first post-stack-up step; replace the §10 auth-principal matrix (the "Web-UI session acts as `web-ui-1`" rows) with the operator-as-worker model; update the §11 "Credential file lost" + "Scripted worker hosts will out-race you" gotchas for the new register-first flow.
- **[`docs/glossary.md`](../glossary.md):** distinguish **service worker** (auto-host, self-registered) from **operator worker** (human-registered); add the non-normative "both are `Worker` records; the distinction is convention" note (§4.1).
- **[`docs/observability.md`](../observability.md) §2.1 admin-routes table:** add the `/profile/` page (and the `/signup` / `/login` / `/logout` routes if that table enumerates auth routes).
- **[`docs/operations/initial-admin-credential.md`](../operations/initial-admin-credential.md):** update for the operator-registration flow (the initial admin is now a named operator worker, seeded by setup-experiment).
- **[`reference/services/web-ui/README.md`](../../reference/services/web-ui/README.md) — wave 2, not wave 3 (codex round 0).** The README's security section currently asserts "the per-worker bearer and the admin token never reach the browser, the rendered HTML, any session cookie, or any structured log line." Putting the operator's `registration_token` in the session cookie (§4.2.1) **intentionally breaks** the cookie clause. This is a security-contract change that must land **with the code** in wave 2 — the README is rewritten to state the new posture (per-session bearer rides the signed-but-not-encrypted cookie; `--secure-cookies` mandatory off-localhost; the admin token still never reaches the browser). Treating it as deferred wave-3 docs cleanup would leave the security contract describing a property the code no longer has.

## 5. Naming map

Surface for operator review. Per [`docs/glossary.md`](../glossary.md) naming discipline (verb-noun-coherent, role-symmetric, no synonyms).

| Surface | Old | New | Rationale |
|---|---|---|---|
| Web-ui route | `/signin` (GET/POST) | `/login` **or keep `/signin`** | Issue prose says "login"; codebase verb-family is `sign*`. **Recommend: keep `/signin`, `/signout`; add `/signup`** — one verb family, minimal churn, no synonym split. Operator picks. |
| Web-ui route | `/signout` | `/logout` **or keep `/signout`** | Same family decision as above. |
| Web-ui route | — | `/signup` (GET/POST) | New: name-based registration. |
| Web-ui route | — | `/profile/` | New: per-operator id/name + work history. (Issue specifies `/profile/`.) |
| Web-ui session field | `worker_id`, `csrf` | + `registration_token` | Per-session operator credential. |
| Web-ui worker | `web-ui-1` / `EDEN_WEB_UI_WORKER_ID` | *(removed)* | Decision §2.2. |
| CLI subcommand | — | `register <name>` | Issue-specified. |
| CLI subcommand | — | `whoami` | Issue-specified. |
| CLI flag | `--worker-id` (on `claim` only, default `eden-manual`) | `--worker-id` (on all subcommands, no default) | Issue-specified; mirrors `--experiment-id`. |
| CLI env var | — | `EDEN_WORKER_ID` | Issue-specified; mirrors `EDEN_EXPERIMENT_ID`. |
| CLI default identity | `eden-manual` | *(removed; explicit register required)* | Decision §2.3. |
| setup-experiment env var | `EDEN_ADMINS_INITIAL_MEMBER` (default `operator`) | `EDEN_OPERATOR_WORKER_NAME` (default from git user → `operator`) | Operator *name* input under #128; #143 §D.3 defers this naming to #140. Operator confirms. |
| setup-experiment env var | — | `EDEN_OPERATOR_WORKER_ID` (captured minted id, persisted to `.env`) | Durable idempotency key for the seeded admin operator (§4.5); names collide so name-lookup can't be the rerun key. |
| Web-ui startup | `admin_store` optional (admin-disabled posture) | `admin_store` **required** (`--admin-token` mandatory) | Decision §4.2.5 — Model B routes signup/login/membership-check through `admin_store`. |

No JSON-enum / submission-class / task-kind / spec-heading renames — #140 touches no wire vocabulary (those are #128's surface).

## 6. Chunked execution plan

Three impl waves matching the issue's "chunks 1-3 as separate PRs" suggestion, each independently shippable and each its own PR. Dependency: **#128 merged** (§3) is the precondition for wave 1. Waves can land in any order after that, but the docs wave (3) lands last so it describes the merged surface.

### Wave 1 — CLI (`register` / `whoami` / `--worker-id` / `EDEN_WORKER_ID` / auto-registration removal)

Smallest blast radius; no spec/web-ui coupling. Files: `reference/scripts/manual-ui/eden-manual` + its tests (if any) + the eden-manual-* SKILL `--worker-id` lines.

**Validation gate:** `uv run ruff check .`; `uv run pyright`; `uv run pytest -q`; manual CLI walkthrough — `register` a named worker, `whoami` confirms, `claim`+submit a task under `--worker-id`, verify a no-credential subcommand errors clearly; `python3 scripts/check-rename-discipline.py` (no `eden-manual` default survives).

### Wave 2 — Web UI (signup / login / profile / logout + per-session bearer + `web-ui-1` removal)

**App-wide identity refactor (§4.2), not a per-page swap.** Files: `routes/auth.py` (→ signup/login/logout), new `routes/profile.py`, `sessions.py` (add `registration_token`; preserve it in **every** `Session(...)` site incl. the experiment-switch rebuild in `admin_experiments.py`), `routes/_helpers.py` (`session_store(request)` + the `app.state.session_store_factory` abstraction, §4.2.2), `app.py` (factory wiring, `app.state.worker_id` removal), `cli.py` + `app.py` (`web-ui-1` worker removal, worker-bearer startup-whoami removal; keep `admin_store` + its probe), `middleware.py` (membership-check bearer swap to `admin_store`), **every** `app.state.store` + `app.state.worker_id` site from the §4.2.3 inventory (role pages + admin reassign/dispatch-mode actor stamping) routed onto `session_store`, `tests/conftest.py` (factory for the in-process test store), `README.md` (security-invariant rewrite, §4.6), templates (`signin.html` → `signup.html` + `login.html`, new `profile.html`, logout button).

**Validation gate:** full Python quartet (`ruff` / `pyright` / `pytest`) — note `pyright` is a primary gate here: removing `app.state.worker_id` outright (§4.2.3) makes any missed migration site a type error rather than a silent wrong-principal bug. The representative `tests/test_admin_gate.py` must still pass after the `admin_store` bearer swap (it is NOT the exhaustive per-route guard — that's #143; §4.2.6). **New tests this wave ships:** (1) a **migration-correctness / attribution test** — drive a human action through each role page + the admin reassign/dispatch-mode handlers and assert `created_by` / `submitted_by` / `reassigned_by` / `updated_by` == the **session** `worker_id`, not the process id (this is what proves the sweep is complete); (2) a **real-`StoreClient` bearer test** — the suite is heavily in-process + admin-seeded ([`tests/conftest.py`](../../reference/services/web-ui/tests/conftest.py)), so bearer-handling bugs specific to the real client are invisible to the `InMemoryStore` matrix; add at least one path that exercises `session_store` against a real `StoreClient` (the e2e smoke covers this end-to-end, but a focused test localizes failures); (3) a **token-survives-experiment-switch test** (§4.2.1). Then `bash reference/compose/healthcheck/e2e.sh` (web-ui ideator walkthrough + admin-reclaim drill — canonical operator-surface smoke, exercises sign-in → claim → submit against the real wire); manual browser walkthrough: sign up as a new name, confirm the minted id on `/profile/`, log out, log back in by id, verify a submitted idea attributes to the operator's `worker_id` (not `web-ui-1`).

### Wave 3 — setup-experiment + skill + docs

Files: `setup-experiment.sh` (`web-ui-1` removal, operator-name var), eden-manual-experiment SKILL registration step, `docs/user-guide.md`, `docs/glossary.md`, `docs/observability.md`, `docs/operations/initial-admin-credential.md`, glossary note (§4.1). Also the **conformance prose touch-up** if any scenario references `web-ui-1` (§9).

**Validation gate:** `npx markdownlint-cli2 …` (pinned per AGENTS.md Commands table); `python3 scripts/spec-xref-check.py` (if the glossary note cross-references spec); `bash reference/compose/healthcheck/smoke.sh` **and** `smoke.sh`'s setup-experiment path (the `web-ui-1`-removal change is loaded from `.env` by the smokes — per AGENTS.md "Commands section is the literal pre-push gate", the smokes are the ones that catch a leaked/renamed env var); full `uv run pytest -q conformance/ -n auto` if any conformance prose changed.

> **AGENTS.md gate reminder:** before each wave's `git push`, run the literal Commands-table quartet (lint / typecheck / pytest / the relevant smoke), not a narrowed subset. The `web-ui-1` removal in wave 2/3 is exactly the class of change (`.env`-loaded, pipeline-shaped) that only the smoke scripts catch.

## 7. Risks / things to watch

- **Credential-rotation friction (§4.3).** Option (a) means UI login invalidates the CLI's cached credential. Load-bearing for the "continuity" headline; mitigated by the CLI's narrow reissue-on-stale recovery. If the operator wants concurrent sessions, that is option (b) — a §13 amendment touching the IUT contract (scope expansion, surface at review).
- **The `app.state.store` / `app.state.worker_id` sweep is app-wide (codex round 0), not a per-page swap (§4.2.3).** A missed `app.state.store` either 500s or silently acts as the wrong principal; a missed `app.state.worker_id` actor-stamp (admin reassign / dispatch-mode) leaves Model B broken for that action even when claim/submit are fixed. Mitigation: remove `app.state.worker_id` outright so misses are `pyright` errors; the attribution test (§6) asserts every human action attributes to the session id.
- **Per-session `StoreClient` is not a drop-in — it needs the store-factory abstraction (§4.2.2).** `make_app` is typed around the generic `Store`; the test suite uses an in-process store for both `store` and `admin_store`. A bare `StoreClient` constructor would force a test-harness rewrite. Mitigation: the `session_store_factory` returns a re-bound `StoreClient` in prod and a worker-id-carrying in-process `Store` wrapper in tests. If this abstraction proves leaky at impl, surface — it's the highest-risk piece of wave 2.
- **Session is rebuilt outside auth (§4.2.1).** The `/admin/experiments/<id>/select` path reconstructs `Session`; if it drops `registration_token` the operator's bearer silently vanishes mid-session. Mitigation: token-survives-switch test (§6).
- **Middleware membership-check bearer swap (§4.2.6).** `AdminGateMiddleware` uses `app.state.store` (removed) to call `resolve_worker_in_group`. Must swap to `app.state.admin_store`. A miss turns the admin gate into a hard 500. Verify the test fixture provides a configured `admin_store`.
- **`registration_token` in the cookie is signed-but-NOT-encrypted (codex round 0).** `itsdangerous.URLSafeSerializer` integrity-protects but does not hide the value — the token is readable from the raw cookie and is bearer-equivalent. `httponly` blocks page JS, not a copied cookie. This breaks the web-ui README security invariant (rewrite in wave 2, §4.6). `--secure-cookies` is mandatory off-localhost. A cookie-encryption upgrade is a follow-up (§11).
- **Logout does not revoke the credential (§4.2.4).** A cookie copied before logout stays usable until the next reissue. Accepted for v0; reissue-on-logout hardening is a follow-up (§11).
- **#128 not merged at impl time (§3).** Strict prerequisite. The whole sign-up/profile UX assumes minted opaque ids. Surface to operator if #128 is still open when wave 1 is ready.
- **Sign-up is unauthenticated-by-proxy.** `POST /signup` lets anyone reaching the web UI register a worker (via the web-ui's admin bearer). This is intended (matches #143 §D.1's "web-ui is the trusted registration intermediary"), and new sign-ups are non-admin (once #143 lands). Pre-#143, a fresh sign-up's authorization is whatever the worker-gated wire endpoints grant — confirm the default is *not* admin even before #143 (it isn't: §13.3 admin-group gating already excludes non-members). Note the ordering: until #143 ships the default-non-admin + promotion flow, the only admin is the setup-experiment-seeded operator. <!-- rename-discipline:cite -->
- **Conformance `web-ui-1` references.** Grep `conformance/` for `web-ui` — if a scenario hardcodes it, it needs rewriting (§9). The reference adapter spawns its own workers, so this is likely prose-only, but verify.

## 8. Migration / cleanup

Pre-external-user clean break (CLAUDE.md "Project Lifecycle"). No shims.

- Existing experiments under the `web-ui-1`-shared-bearer pattern need **re-bootstrap** — re-run `setup-experiment` against a fresh data root after this lands.
- Pre-cutover **checkpoints are non-restorable** into the new shape — and the migration text must describe the **actual failure mode**, not a generic "non-restorable" (codex round 0). Two compounding causes: (i) **#128 changes registration semantics** — a pre-#128 checkpoint's `worker` rows carry operator-typed ids, while the post-#128 registry expects minted opaque ids + a `name` column, so an import either rejects on shape or produces workers with no `name`; (ii) **#140 changes who bears credentials** — the imported registry contains `web-ui-1` (now meaningless) and no operator workers, so even a shape-compatible import yields a deployment where no human can sign in until they register fresh. The concrete operator symptom is "import succeeds but the UI has no usable operator identities and admin pages 403." Document this exact symptom + the fix (re-bootstrap on a fresh data root; register operators anew) in the user-guide §2 + a CHANGELOG migration note.
- A fresh `setup-experiment` produces an **empty operator-worker registry** (plus the one seeded initial-admin operator). Operators register on first use.
- `eden-manual`'s legacy `.credentials.json` keyed under the `eden-manual` id is abandoned; operators re-`register` a named worker. (No migration of the old keyed entry — pre-user posture.)

## 9. Conformance impact

Per the chapter-9 §6 IUT-contract gate: **#140 introduces no new MUST and asserts no new wire behavior.** The sign-up / login / profile flow is web-ui-internal (implementation-defined per §13); the wire ops it uses (`register_worker`, `reissue_credential`, `whoami`) are already covered.

- **No new conformance scenarios.** (The §13.3 dual-gate scenario is #143's.)
- **Prose-only:** grep `conformance/scenarios/` and the harness for `web-ui-1` / `eden-manual` literals; rewrite any that hardcode them to register-and-use-the-returned-id (this overlaps #128's conformance prose pass — coordinate to avoid double-editing).
- `check_citations.py` is unaffected (no new scenario, no new citation).

If, contrary to this analysis, impl finds itself wanting a new conformance assertion, that signals the design drifted toward a wire-observable behavior change — **stop and surface** (a §6 IUT-contract boundary is a stop condition).

## 10. Relationship to sibling issues (cluster `identity`)

- **#128** (id/name split) — **strict prerequisite** (§3). One-way dependency.
- **#141** (worker registry as deployment-level infrastructure) — **sibling.** Covers *service* worker identity (auto-hosts as deployment-level, explicitly registered with operator-chosen names). #140 deliberately leaves service workers self-registering and per-experiment; #141 migrates them. No code overlap if #140 touches only the operator-worker surface.
- **#143** (non-admin-default sign-ups + admin promotion) — **depends on #140.** <!-- rename-discipline:cite --> Layers *authorization* (who is admin) onto #140's *principal* model. #143's plan ([`docs/plans/issue-143-non-admin-default-signup.md`](issue-143-non-admin-default-signup.md)) already assumes #140's session-bearer plumbing, `/profile/` page, and `eden-manual`-retirement; it also pins the §13.3 dual-gate amendment + the exhaustive `require_admin` enumeration test, both of which #140 explicitly does NOT touch. Co-shipping is fine; dependency direction is one-way (#140 → #143).
  - **Contradiction to resolve before either lands (codex round 0):** #143's plan still describes `web-ui-1` as becoming a "service-only worker" that *remains registered* (its §1 + §3 reference a surviving `web-ui-1`). #140's refinement-driven decision (§2.2) is that the `web-ui-1` **worker** is removed entirely — only the deployment-admin bearer survives. These cannot both be true. **Resolution: #140 wins** (it owns the web-ui identity surface, and the issue refinement explicitly decided full removal). When #140 lands, #143's plan must be amended to drop the "service-only `web-ui-1` remains" framing and rephrase its `admin_store`-proxy discussion in terms of the deployment-admin bearer, not a `web-ui-1` worker. Flagged here so the contradiction is resolved at plan-merge, not discovered at #143 impl time. (This is the AGENTS.md "inter-plan restatement is a conflict surface" pattern — the canonical statement lives in #140; #143's restatement defers.)
- **#144** (admin-route gating at the route layer) — **CLOSED.** `AdminGateMiddleware` exists; #140 must preserve it through the `web-ui-1`-removal bearer swap (§4.2.6).

## 11. Out of scope (followups — file as issues at deferral time)

- **SSO / OAuth / external IdP integration.** Central-platform / Phase 13+ (`docs/prds/eden-experiment-platform.md`). Model B is forward-compatible: the controller mints/reads a worker on the upstream-authenticated operator's behalf.
- **Concurrent multi-session per operator (option (b), §4.3).** The admin-derived session-delegation primitive. File if/when concurrent UI+CLI for one identity becomes a requirement.
- **`service: bool` (or label-based) operator-vs-service display flag (§2.5).** A display-only refinement so admin worker lists can visually separate humans from hosts. File as a small follow-up.
- **Multiple workers per operator (role-switching).** UX assumes one identity at a time; an operator *may* register several but the flow doesn't streamline it.
- **Cross-experiment operator identity.** Each experiment has its own registry (per protocol). Unifying is the central-platform PRD's job.
- **Authorization model beyond admin / non-admin.** #143 keeps the binary model; richer RBAC is deferred there.
- **Encrypting the session cookie (§4.2.1).** v0 ships the bearer in a signed-but-not-encrypted cookie + `--secure-cookies`. An encrypting codec (or server-side session store keyed by an opaque cookie id) removes the bearer-in-browser exposure entirely. File if the signed-not-encrypted posture proves operationally unacceptable.
- **Reissue-on-logout credential revocation (§4.2.4).** Rotating the token at logout would invalidate a copied cookie, at the cost of breaking any other live session/CLI for that worker. Deferred; the same single-active-credential tension as login rotation.

## 12. Estimated effort

Large (issue estimate: ~2-3 weeks), as a 3-PR sequence.

| Wave | Activity | Estimate |
|---|---|---|
| 1 | CLI: `register` / `whoami` / `--worker-id` everywhere / `EDEN_WORKER_ID` / auto-registration removal + retain reissue-on-stale + SKILL `--worker-id` lines | ~2 days |
| 2 | Web UI: signup/login/logout/profile + session-credential + `session_store` factory (§4.2.2) + app-wide `app.state.store`/`app.state.worker_id` sweep + `web-ui-1` removal + middleware swap + README security rewrite + migration/real-client tests + templates | ~6-8 days |
| 3 | setup-experiment + skill registration step + docs (user-guide / glossary / observability / operations) + conformance prose grep | ~2-3 days |
| — | Plan + impl codex-review iterations (per wave) | ~2-3 days |
| — | **Total** | **~2-3 weeks** |

Wave 2 is the dominant cost: the per-session-bearer retrofit + the route-wide `app.state.store` → session-client migration is the load-bearing, highest-blast-radius work.
