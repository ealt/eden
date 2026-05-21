# Phase 12a chunk 1 — Worker identity

## 1. Context

Phase 12 implements the orchestrator-and-worker-roles design from
[`docs/design/orchestrator-and-worker-roles.md`](../design/orchestrator-and-worker-roles.md).
The design doc is split into three layers (identity / orchestrator-as-role /
lifecycle policy) which this phase realizes as three sequential chunks.

This chunk — 12a-1 — is the **identity foundation**: workers become
first-class registered entities, tasks gain a `target` field that
constrains which worker(s) can claim, attribution becomes data on the
artifacts (not just the event log), and `submit` shifts from
per-claim-token authentication to authenticated-worker-id matching.

12a-2 (orchestrator-as-role) and 12a-3 (lifecycle policy) build on
this foundation and are intentionally out of scope here.

## 2. Decisions captured before drafting

The design doc lists five open questions; four were settled during
the scoping discussion before this plan:

1. **Worker registry scope** — **per-experiment.** Each experiment
   has its own worker / group registry. Workers are local to the
   experiment that registers them; the same `worker_id` string in
   two experiments refers to two distinct registry entries.
2. **Default groups (`humans` / `agents`)** — **none.** No
   well-known group names; deployments configure whatever groups
   make sense for them. The protocol defines the mechanism, not the
   policy.
3. **Migration of existing experiments** — **none.** EDEN is
   greenfield; no real experiments are running on the protocol yet,
   so no compat shims for pre-12a state. Any in-flight experiments
   will be re-seeded after this chunk lands.
4. **RBAC on the RBAC** — **admins create groups, reassign tasks,
   flip `dispatch_mode`.** A new `admin` capability gate (handled
   alongside per-worker auth in §D.5) controls who can perform
   group-CRUD, task reassignment (12a-2), and dispatch-mode flips
   (12a-2). For 12a-1 the only admin-gated operations are
   `register_worker` and `register_group`.

The fifth open question — multi-orchestrator HA — is **deferred**
to 12a-2 since it depends on the orchestrator-as-role contract that
chunk introduces.

## 3. Design

### D.0 Auth-layer boundary (read this first)

Authentication is a **binding-layer concern**, NOT a Store Protocol
concern. The Store Protocol takes `worker_id` as input data on
operations like `claim` and `submit`; the binding (HTTP wire,
in-process, future ones) is responsible for verifying that the
caller was authorized to act as the named worker before invoking
the Store.

This **changes** the chapter-7 wire framing: today's §11 places
authentication outside the normative binding (§12 documents the
optional reference-impl shared-bearer as informative), but 12a-1
makes per-worker authentication a normative requirement of the
HTTP binding. Conforming non-HTTP bindings still implement their
own auth verification with the same authenticated-id-vs-Store-call-id
invariant. After 12a-1, the wire binding gains:

- A specified auth scheme (per-worker bearer + admin bearer; §D.5).
- A normative requirement that the binding MUST verify the
  presented credential and reject mismatched authenticated-id-vs-
  Store-call-id BEFORE calling the Store.

The Store Protocol's `claim` and `submit` signatures keep the
existing `worker_id` parameter (no new auth context arg) — they
trust the binding to have already verified. In-process callers
(tests, etc.) bypass the binding and pass `worker_id` directly;
that's a deliberate trust boundary at the binding edge.

A consequence: third-party bindings (gRPC, in-VM IPC, etc.) MUST
implement their own auth verification and apply the same
"authenticated-id matches Store-call-id" invariant. The spec
chapter for that binding documents how, just as chapter 7 will do
for HTTP.

### D.1 Worker as a first-class registered entity

Today's `worker_id` is an ad-hoc string passed via `--worker-id` at
service startup. After 12a-1, every worker that participates in the
task protocol MUST be registered with the experiment's store.

**Worker-id grammar.** ``worker_id`` MUST match
``^[a-z0-9][a-z0-9_-]{0,63}$``: lowercase alphanumeric, hyphen,
underscore; ≤64 chars; first character non-hyphen. This excludes
`:` (load-bearing for the bearer format in §D.5), whitespace, and
URI-unsafe characters. Same posture as the existing `slug`
constraint on ideas. Group IDs follow the same grammar.

The following identifiers are **reserved** and MUST NOT be used as
either `worker_id` or `group_id`:

- `admin` — used as the bearer-format auth-principal sentinel (§D.5).
- `system`, `internal` — reserved for future protocol use.
- Anything starting with `_` (underscore) — reserved for
  reference-impl internals (parallel to `_reference/` in
  chapter 7).

The reservation is a grammar-layer rule enforced at register-time
by both `register_worker` and `register_group`. Attempting to
register a reserved name MUST raise `ReservedIdentifier`.

**Worker shape.** A `Worker` is a small persistent record:

```json
{
  "worker_id": "<unique-string-per-experiment>",
  "experiment_id": "<experiment-id>",
  "registered_at": "<RFC 3339 timestamp>",
  "registered_by": "<admin-credential-identifier>",
  "labels": {},
  "auth_credential_hash": "<argon2id hash>"
}
```

- `worker_id` is operator-supplied; MUST be unique within the
  experiment (per-experiment scope per §2 decision 1).
- `experiment_id` ties the worker to the experiment whose registry
  it lives in.
- `labels` is a free-form key→string map for deployment-specific
  metadata (e.g., `{"role": "ideator", "model": "claude-opus-4-7"}`).
  The protocol does not interpret labels — they are operational
  context only.
- `auth_credential_hash` stores the argon2id hash of the worker's
  registration token (issued at register-time; see §D.5). Never
  returned in any read API.

**Register operation.** A new normative operation:

```text
register_worker(worker_id, labels?) → {worker_id, registration_token}
```

The store generates a fresh `registration_token` (opaque secret,
≥256 bits of entropy), stores its argon2id hash, and returns the
plaintext token to the caller exactly once. The token is the
worker's authentication credential for all subsequent submit /
claim / etc. operations.

**Read operation.** `read_worker(worker_id) → Worker` (without
hash); `list_workers(filter?) → [Worker]`. Both are
**not worker-scoped reads**: available to any caller with
experiment-level access (admin token OR any registered worker for
the experiment), matching today's posture for `list_tasks`. They
are NOT a substitute for `verify_worker_credential` as a
credential-validity probe — they don't authenticate the
credential's binding to a specific worker_id.

**Restart safety + credential rotation.** Two distinct operations:

- `register_worker(worker_id, labels?)` is **idempotent on the
  existing record**: if `worker_id` is already registered, return
  the existing `Worker` shape (without a new token). The original
  token remains valid. This makes service-restart-after-crash
  cheap — the host's startup logic can call `register_worker`
  unconditionally.
- `reissue_credential(worker_id) → {worker_id, registration_token}`
  is a **separate admin-gated operation** that mints a fresh token
  and invalidates the old one. This is the documented recovery
  path when a worker's local credential is lost.

Conflating these (the prior draft did) leaves the recovery story
broken: a host that has lost its local token but still has a
registry row can't recover via re-register. The split is
load-bearing.

**Startup recovery flow.** A new wire op is needed to make this
work cleanly: `verify_worker_credential() → {worker_id}` returns
the authenticated worker's id (or 401 if the credential is bad).
This is an **authenticated** ping; `read_worker` is an
unauthenticated experiment-level read and CANNOT serve as the
auth check. Worker hosts at startup:

1. If a persisted credential exists at the configured path,
   attempt `verify_worker_credential` against the wire.
2. If that returns the expected `worker_id`: continue with the
   existing credential.
3. If that returns 401 / the wrong worker_id (admin rotated the
   token, registry was wiped, server identity drift, etc.): the
   host MUST escalate to `reissue_credential` rather than
   `register_worker`. Re-registering would either be a no-op (if
   the row exists per §D.1's idempotency rule) without yielding a
   new token, OR fail (if the row is gone but persisted state
   says it should exist). The reissue path is the canonical one
   for credential recovery; document it as such.
   - **Auto** (admin-token in the host's env): call
     `reissue_credential(worker_id)` and persist the new
     credential. Reference deployment uses this path since the
     admin token is in `.env` at host bootstrap.
   - **Manual**: log an error and exit non-zero. Operator runs
     `reissue_credential` out-of-band and re-injects the
     credential.
4. If no persisted credential exists at all (first run): call
   `register_worker`; persist the returned credential.

This makes `register_worker` (idempotent on existing record) and
`reissue_credential` (explicit credential rotation) play
non-overlapping roles, with `verify_worker_credential` as the
discriminator.

### D.2 Group as a named set

A `Group` is a recursive set whose members can be `worker_id`s
or other `group_id`s.

```json
{
  "group_id": "<unique-string-per-experiment>",
  "experiment_id": "<experiment-id>",
  "members": ["<worker_id-or-group_id>", "..."],
  "created_at": "<RFC 3339 timestamp>",
  "created_by": "<admin-credential-identifier>"
}
```

**Resolution semantics.** Membership is transitive. A worker `eric`
is in group `team-a` if:

- `eric ∈ team-a.members`, OR
- ∃ group_id `g` such that `g ∈ team-a.members` and `eric` is in
  `g` (transitively).

**Cycle detection.** Group definitions form a directed graph. The
store MUST reject any `register_group` or `update_group` operation
that would introduce a cycle (e.g., `team-a` ∋ `team-b` ∋ `team-a`).
Detection happens at write time via DFS-on-write; resolution at
read time is a topo-walk that's safe by construction.

**No default groups.** Per §2 decision 2, the protocol does not
define `humans` / `agents` / etc. as well-known group names. A
deployment that wants such groups creates them explicitly.

**Mutation operations.** `register_group`, `add_to_group`,
`remove_from_group`, `delete_group`. All admin-gated.

### D.3 `Task.target` field

Tasks gain a single optional field. Because worker IDs and group
IDs are independently unique within an experiment but share the
same string grammar (§D.1), the field is **tagged** to disambiguate
namespaces — a plain `worker_id | group_id | null` would be
ambiguous if a worker and group ever had the same id:

```text
Task.target: { kind: "worker", id: "<worker_id>" }
            | { kind: "group",  id: "<group_id>" }
            | null
```

- `{kind: "worker", id: "<wid>"}`: only that specific worker can claim.
- `{kind: "group", id: "<gid>"}`: any worker transitively in that
  group can claim.
- `null` (or absent): any registered worker matching the task `kind`
  can claim. This is the "open" case and matches today's behavior
  (modulo the registration requirement).

The field is set at task-creation time. 12a-2 will add reassignment
(`reassign_task(task_id, new_target)`); 12a-1 just adds the field
and the claim-time enforcement.

**Claim-time enforcement.** `Store.claim(task_id, worker_id, ...)`
checks, in order (per the §D.0 layer split, the binding has
already verified that the request was authorized to act as
`worker_id` before this Store call runs):

1. Task is in `pending` state (existing check).
2. Worker is registered for the experiment (new — `worker_id`
   exists in the registry).
3. Worker satisfies the task's target:
   - If `target` is null: pass.
   - If `target.kind == "worker"`: pass iff `worker_id == target.id`.
   - If `target.kind == "group"`: pass iff `worker_id` is
     transitively in `target.id` (group membership resolved
     transitively per §D.2).
4. Atomic claim-write (existing).

A claim attempt that fails step 3 raises `WorkerNotEligible`
(new typed error, joins `IllegalTransition` family). A claim by a
non-registered worker (step 2 failure) raises `WorkerNotRegistered`.

### D.4 Attribution as data on artifacts

Today, who-did-what is observable only via the event log
(`task.claimed`, `task.submitted` carry `worker_id`). For
heterogeneous-worker pools (a primary motivation for this phase),
attribution becomes load-bearing data and gets promoted to the
artifact level.

**New fields on existing schemas:**

| Schema | New optional field | Set when |
|---|---|---|
| `Task` | `created_by` | At task creation; identifies the actor (admin, orchestrator, operator) that created the task |
| `Task` | `submitted_by` | At submit time; preserves the claimant's `worker_id` after the task reaches a terminal state (today only `claim.worker_id` is recorded, and `claim` is cleared on accept) |
| `Idea` (was `Proposal`) | `created_by` | At `create_idea` time; the ideator's `worker_id` |
| `Variant` (was `Trial`) | `executed_by` | At `submit` time on the executor's task; the executor's `worker_id` |
| `Variant` | `evaluated_by` | At `submit` time on the evaluator's task; the evaluator's `worker_id` |

All five fields are optional in the wire schema (so a checkpoint
imported from a pre-12a impl validates), but populated by every
12a+ implementation. They survive past terminal state.

**Why on artifacts, not just event log.** "Who executed variant
V?" should be a single read, not an event-log fold. Same posture as
git's `author` and `committer` fields — attribution is data, not
log content.

### D.5 Per-worker authentication

Today every wire request carries the deployment-shared
`EDEN_SHARED_TOKEN` bearer. After 12a-1, the wire requires
per-worker credentials: each worker authenticates as itself.

**Auth flow.**

1. **Registration**: `register_worker` (admin-authenticated; see
   below) issues a `registration_token` (opaque, ≥256 bits, returned
   exactly once). The worker stores this locally.
2. **Worker requests**: every wire call from a worker carries
   `Authorization: Bearer <worker_id>:<registration_token>`. The
   server splits on `:`, looks up the worker by id, and verifies
   the argon2id hash.
3. **Submit / claim**: the authenticated `worker_id` is used as the
   identity for claim eligibility (§D.3), submit attribution
   (§D.4), and the now-implicit `submit_by` field.

**Admin authentication.** A separate `admin_token` (one per
deployment, set in env as `EDEN_ADMIN_TOKEN`) gates registration
operations:

- `register_worker`
- `register_group`, `add_to_group`, etc.
- `reassign_task` (12a-2)
- `set_dispatch_mode` (12a-2)

Admin requests carry `Authorization: Bearer admin:<admin_token>`.
The "admin" sentinel disambiguates worker-creds from admin-creds at
parse time.

**Token retention.** The shared bearer (`EDEN_SHARED_TOKEN`) is
**removed** in 12a-1. Greenfield treatment per §2 decision 3: no
compat shim for clients still presenting the old shared bearer.
All in-flight services must re-authenticate as workers after the
phase lands.

**Reference-binding implication.** The reference subprocess binding
(`spec/v0/reference-bindings/worker-host-subprocess.md`) gains a
new env var that worker hosts pass to their `*_command` children:
the worker's own credential. The host registers itself at startup
(idempotent, so `compose up` after a restart re-uses the existing
worker_id) and threads the credential into the child's env.

### D.5b Web UI: per-session-user worker authentication

The web-ui has two distinct auth identities post-12a-1:

1. **Process-level admin worker** — registered at startup as
   `web-ui` (or similar). Used for endpoints that don't have a
   session (`/healthz`, the admin pages whose access is
   controlled by the admin token). Holds an admin-issued
   credential persisted on disk.
2. **Per-session user worker** — when a user signs in via the
   existing session model
   ([`sessions.py`](../../reference/services/web-ui/src/eden_web_ui/sessions.py))
   the UI registers them as a worker (idempotent if they've signed
   in before) and stores the returned credential in the
   itsdangerous-signed session cookie alongside the existing
   `csrf` and `worker_id` fields.

**Routing rule.** Wire calls made on behalf of a signed-in user
(claim / submit on ideator / executor / evaluator pages, admin
reclaim / dispatch-mode flips that 12a-2 will add) MUST use the
session-user's credential, not the process-level admin
credential. The route layer's `wire_client_for_session(session)`
helper enforces this at the type level — routes that don't have a
session can't construct a session-authenticated client.

**Why this is load-bearing.** Without per-session auth, every
human action through the web-ui collapses to "the web-ui process"
as the actor — defeating attribution (§D.4) and the
claims-scoped-to-worker property of the design doc §8. The
session-cookie route was already there in the codebase
([`auth.py`](../../reference/services/web-ui/src/eden_web_ui/routes/auth.py)
threads `worker_id` from the sign-in form into the session); we're
extending it to also issue and store a real credential.

**Sign-in flow.**

1. User submits the sign-in form with `worker_id` (today's
   "Continue as `<X>`" button).
2. Web-ui calls `register_worker(worker_id)` via its
   process-level admin credential. If the worker exists already,
   this is a no-op; the UI then needs to obtain a credential to
   act as that worker. Two options:
   - **(a) admin-issued credential, returned at sign-in.** The UI
     calls `reissue_credential(worker_id)` on every sign-in
     (admin-gated, allowed because the web-ui process IS the admin
     for sign-in purposes). The user gets a fresh credential per
     session; the prior session's credential is invalidated.
     Single-active-session-per-worker is the consequence; that's
     usually fine for human users.
   - **(b) admin-derived session-only credential.** The UI mints
     a short-lived bearer signed by the admin token, scoped to
     that user's `worker_id` and the session lifetime, that the
     Store accepts as standing in for the per-worker credential.
     Cleaner UX (multiple sessions OK) but adds a new auth
     primitive.

   12a-1 ships with **(a)** for simplicity. **(b)** is a viable
   12a-2 expansion if multi-session-per-user matters.
3. Session cookie carries `{worker_id, credential, csrf,
   expires_at, ...}`. Existing claim/CSRF checks continue to work.

**Sign-out** invalidates the session cookie locally; the
worker registration persists in the registry (so the next sign-in
is `register_worker` returning the existing record).

**Accepted limitation in 12a-1: web-UI sessions are NOT
cross-app-compatible.** Per-sign-in `reissue_credential`
invalidates the prior credential, so:

- A user who had an active claim in a CLI session and signs into
  the web-ui: the CLI's submit will fail with stale-credential.
  They have to re-acquire the credential in the CLI.
- A second concurrent web-ui sign-in as the same worker_id
  invalidates the first session's credential; the first session's
  in-flight claim is stranded (the user has to sign in again to
  resubmit).

This contradicts the design-doc §8 promise of cross-application
claim, but only for the web-UI side of the boundary. Pure-CLI
cross-app claim still works (different terminals on the same
machine sharing a credential file).

**The cleaner fix** is option (b) from above — admin-derived
session-only credential that doesn't rotate the worker's primary
credential. That preserves cross-app claim end-to-end. **Deferred
to 12a-2** with a tracker entry; the simpler-to-ship option (a) is
correct for 12a-1 because:

- The wire / Store layer changes in 12a-1 are already substantial
  and (a) avoids introducing a new auth primitive (admin-derived
  session token).
- The cross-app limitation is documentable and operational, not a
  protocol soundness issue.
- Most reference-deployment users are CLI-driven (CLI claim, CLI
  submit); the web-UI is the secondary path and the limitation is
  only visible when the same user is on both at once.

12a-2 can revisit if multi-app concurrent claim becomes important.

**Implication for operators.** Document in the operator guide
(in the existing `eden-manual` skill or a new doc): web-UI
sign-in is "session-exclusive" — owning a session for worker `X`
in browser tab A invalidates worker `X`'s credential in any other
session. Users who want to keep CLI work alongside web-UI should
operate as different `worker_id`s, or use one app at a time.

### D.6 Claim semantics under per-worker auth

Today's claim returns a per-claim token; submit checks
`presented_token == task.claim.token`. This is what causes the
"claim is application-scoped" friction documented in
[`MANUAL_UI_ISSUES.md`](../../MANUAL_UI_ISSUES.md) §1 / design doc §8.

**Layering — two distinct concerns.** Authentication
("who are you?") and claim-ownership integrity ("does this submit
match the current claim?") split across layers:

- **Authentication is binding-only** (per §D.0). The binding
  verifies the presented credential and extracts an
  authenticated `worker_id`. The Store does not see credentials.
- **Claim-ownership integrity is a Store invariant**, enforced
  atomically with the submit transition. The Store cannot
  delegate this to the binding without opening a TOCTOU race
  where another worker reclaims between the binding's check and
  the Store's write.

Concretely:

- `Store.claim(task_id, worker_id, ...)` — unchanged signature
  modulo the new §D.3 enforcement. The Store assumes the binding
  has already authenticated the caller as `worker_id`. **No token
  returned.**
- `Store.submit(task_id, worker_id, payload)` — `worker_id` is a
  **new required parameter** representing the claimant on whose
  behalf the submit is being made. The `token` parameter is
  removed. The Store atomically checks
  `task.claim.worker_id == worker_id` AS PART OF the submit
  transition (single transaction with the state write); a
  mismatch raises `WrongClaimant`. A submit on an unclaimed task
  raises `NotClaimed`.

**Binding's job on submit:**

```text
authenticated_worker_id = binding.verify_credential(request)
                                  # raises 401 on bad creds
store.submit(task_id, authenticated_worker_id, payload)
                                  # Store does atomic claim-match
```

The binding does NOT do its own pre-flight `read_task → compare`
check — that introduces the TOCTOU hole. It just authenticates
and forwards the authenticated `worker_id` to the Store. The
Store handles claim-match atomically.

**Error vocabulary placement:**

- `WorkerNotEligible`, `WorkerNotRegistered` — Store-layer typed
  errors; raised from `Store.claim` per §D.3.
- `WrongClaimant`, `NotClaimed` — **Store-layer** typed errors
  raised by `Store.submit`. The atomicity requirement (no TOCTOU)
  forces this layer placement; the binding surfaces them as
  appropriate HTTP status codes (likely 403 / 409) but does not
  detect them itself.
- Authentication errors (bad bearer, missing bearer) — binding
  only; never reach the Store.
- `WrongToken` — **removed** from the protocol; tokens no longer
  exist.

The chapter-7 wire spec enumerates how each Store-layer error
maps to HTTP status; chapter 4 / chapter 8 own the error
definitions themselves.

**Idempotency without tokens.** Today's per-claim token doubles
as an idempotency key (a re-submitted submission with the same
token is a no-op). 12a-1 replaces this with an explicit optional
`submission_id` (UUID) field on the wire payload — same
equivalence semantics as `submissions_equivalent` in
[`spec/v0/04-task-protocol.md`](../../spec/v0/04-task-protocol.md)
§4.2, but tagged by an explicit caller-supplied id rather than a
server-issued token. The Store retains the existing
content-equivalence path (§4.2's
"submissions_equivalent" check) as a backstop for clients that
don't supply `submission_id`.

**What's removed:**

- `Store.claim` no longer returns a token.
- `Task.claim.token` field removed from the schema.
- `submit`'s `token` parameter removed.
- `WrongToken` removed from the protocol error vocabulary.

**What's added:**

- `Store.claim` raises `WorkerNotRegistered` / `WorkerNotEligible`
  on §D.3 enforcement failure.
- `Store.submit` gains a required `worker_id` parameter and raises
  `WrongClaimant` / `NotClaimed` atomically with the submit
  transition (the binding does NOT pre-check; it just authenticates
  and forwards the authenticated `worker_id` to the Store).
- New wire op `verify_worker_credential` for authenticated startup
  probes (per §D.1 startup recovery flow).

### D.7 What does NOT change in 12a-1

- Task lifecycle states (`pending` → `claimed` → `submitted` →
  `completed` / `failed`). Same transitions; just newly
  authentication-aware.
- Idea/Variant/Submission shapes (other than the new attribution
  fields).
- The `Idea`/`Variant`/event-log core operations.
- `experiment_config` shape (12a-3 changes this).
- The integrator (12a-2 may touch indirectly via reassignment; 12a-3
  changes via termination policy).
- Reference deployment shape (still Compose; still per-experiment
  postgres / forgejo).

## 4. Scope

### 4.1 In scope

- New schemas: `worker.schema.json`, `group.schema.json`.
- Schema additions: `Task.target`, `Task.created_by`,
  `Task.submitted_by`, `Idea.created_by`, `Variant.executed_by`,
  `Variant.evaluated_by`.
- Pydantic models: `Worker`, `Group`, all attribution fields.
- Storage protocol additions: `register_worker`,
  `reissue_credential`, `verify_worker_credential`, `read_worker`, `list_workers`,
  `register_group`, `add_to_group`, `remove_from_group`,
  `read_group`, `list_groups`, `delete_group`,
  `resolve_worker_in_group(worker_id, group_id) → bool` (with
  cycle-safe transitive walk).
- Wire endpoints (chapter 7): new register/read/list/delete for
  workers + groups; auth scheme changes documented.
- `Store.claim` and `Store.submit` semantics shift per §D.6.
- Reference-impl services: each worker host registers itself at
  startup and authenticates per-request.
- Reference-binding doc updated for the credential threading.
- Conformance scenarios: target matching, group resolution,
  attribution survives terminal state, worker-auth required for
  submit, cycle detection on group registration.
- Documentation updates: spec chapters 02 / 04 / 07 / 08, glossary,
  AGENTS.md.

### 4.2 Out of scope (deferred to 12a-2)

- Orchestrator becomes a role.
- Per-decision-type `dispatch_mode` flags.
- Task reassignment (`reassign_task`).
- Per-item manual-flag opt-out.
- Multi-orchestrator HA decision.

### 4.3 Out of scope (deferred to 12a-3)

- Operator-driven ideation-task creation.
- Per-idea `intended_executor` hint.
- Termination policy as deployment callback.
- Removal of `max_trials` / `max_wall_time` / `convergence_window` /
  `target_condition` from spec.

### 4.4 Non-goals

- Cross-experiment workers (per §2 decision 1).
- Default group names (per §2 decision 2).
- Migration of pre-12a experiments (per §2 decision 3).
- Backwards-compat for the shared-bearer auth (per project's
  greenfield stance).

## 5. Files to touch

### 5.1 Spec

| File | Change |
|---|---|
| `spec/v0/01-concepts.md` | New §11 "Workers and groups"; cross-ref from §2 (roles) and §8 (claim token → claim+auth). |
| `spec/v0/02-data-model.md` | New §6 "Worker registry"; new §7 "Groups"; attribution fields added to §3 (task), §4 (idea), §5 (variant). |
| `spec/v0/04-task-protocol.md` | §3 (claim) revised: target enforcement, no per-claim token. §4 (submit) revised: authenticated-worker matching. New §10 "Worker eligibility errors". |
| `spec/v0/07-wire-protocol.md` | New endpoints `register_worker`, `register_group`, etc. New auth scheme (per-worker bearer + admin bearer). Existing endpoints' auth shifts. |
| `spec/v0/08-storage.md` | New §9 "Worker registry"; cycle-detection requirement for groups. |
| `spec/v0/schemas/task.schema.json` | Add `target`, `created_by`, `submitted_by`. Remove `claim.token` (breaking). |
| `spec/v0/schemas/idea.schema.json` | Add `created_by`. |
| `spec/v0/schemas/variant.schema.json` | Add `executed_by`, `evaluated_by`. |
| `spec/v0/schemas/worker.schema.json` | NEW. |
| `spec/v0/schemas/group.schema.json` | NEW. |
| `spec/v0/reference-bindings/worker-host-subprocess.md` | Document the new credential env var threaded into `*_command` children. |

### 5.2 Pydantic models (`reference/packages/eden-contracts/`)

| File | Change |
|---|---|
| `src/eden_contracts/worker.py` | NEW. `Worker`, `WorkerLabels`. |
| `src/eden_contracts/group.py` | NEW. `Group`, `GroupMember`. |
| `src/eden_contracts/task.py` | Add `target`, `created_by`, `submitted_by`. Remove `claim.token`. |
| `src/eden_contracts/idea.py` | Add `created_by`. |
| `src/eden_contracts/variant.py` | Add `executed_by`, `evaluated_by`. |
| `src/eden_contracts/__init__.py` | Re-export `Worker`, `Group`. |
| `tests/cases.py` | Add accept/reject corpus for new shapes. |
| `tests/test_roundtrip.py` | Add round-trip cases for `Worker`, `Group`, attribution fields. |

### 5.3 Storage (`reference/packages/eden-storage/`)

| File | Change |
|---|---|
| `src/eden_storage/protocol.py` | Add `register_worker`, `reissue_credential`, `read_worker`, `list_workers`, `verify_worker_credential` (returns the worker_id whose hashed credential matches the presented token, or raises a typed error), `register_group`, `add_to_group`, `remove_from_group`, `read_group`, `list_groups`, `delete_group`, `resolve_worker_in_group`. Update `submit` signature to add required `worker_id` parameter. |
| `src/eden_storage/_base.py` | In-memory implementation of all new ops, including transitive group resolution + cycle detection. Update `claim` semantics (no token returned; `WorkerNotEligible` / `WorkerNotRegistered` raised on §D.3 enforcement failure). Update `submit` signature to drop `token` and add required `worker_id` (the claimant on whose behalf the submit happens). The Store atomically checks `task.claim.worker_id == worker_id` as part of the submit transition (single transaction); raises `WrongClaimant` / `NotClaimed` on mismatch. Authentication of the request itself is the binding's job per §D.0; the Store trusts the `worker_id` parameter as data. Remove `WrongToken`. |
| `src/eden_storage/sqlite.py` | New tables: `worker`, `group_membership`. Schema migration to drop `task.claim.token` field from JSON. |
| `src/eden_storage/postgres.py` | Same as sqlite, with postgres-specific syntax. |
| `src/eden_storage/_postgres_schema.py` | Schema bump (new tables; field removal). |
| `src/eden_storage/_schema.py` | Schema bump (sqlite). |
| `src/eden_storage/errors.py` | Add `WorkerNotEligible`, `WorkerNotRegistered`, `WrongClaimant`, `NotClaimed`, `CycleDetected`, `ReservedIdentifier`, `WorkerAlreadyRegistered`. Remove `WrongToken`. The atomicity requirement (no TOCTOU between check and submit) puts `WrongClaimant`/`NotClaimed` at the Store layer. |
| `tests/test_workers.py` | NEW. Unit + protocol-conformance tests for worker registry. |
| `tests/test_groups.py` | NEW. Group resolution + cycle-detection tests. |
| `tests/test_claim_eligibility.py` | NEW. Claim under per-worker auth, target matching. |

### 5.4 Wire (`reference/packages/eden-wire/`)

| File | Change |
|---|---|
| `src/eden_wire/server.py` | New `/workers`, `/groups`, `/verify-credential` endpoints; auth dispatch reads `worker_id:token` or `admin:token`. The `/verify-credential` endpoint is the authenticated-ping op used by host startup recovery (§D.1) — returns the authenticated `worker_id` on success, 401 on bad credential. |
| `src/eden_wire/client.py` | `StoreClient` gains worker auth fields; new methods `register_worker`, `read_worker`, `list_workers`, `reissue_credential`, `verify_worker_credential`, `register_group` / `add_to_group` / `remove_from_group` / `read_group` / `list_groups` / `delete_group`. |
| `src/eden_wire/auth.py` | NEW. Bearer parser + admin/worker dispatch. Authenticates incoming requests and extracts the authenticated `worker_id`; the submit handler passes that `worker_id` through to `Store.submit` (Store does the atomic claim-match per §D.6, eliminating the read-then-write TOCTOU). |
| `src/eden_wire/server.py` (already listed) | Submit endpoint extracts authenticated `worker_id` from auth dispatch, passes to `Store.submit(task_id, worker_id, payload)`. Surfaces Store-raised `WrongClaimant`→403 and `NotClaimed`→409. |
| `tests/test_workers_wire.py` | NEW. Round-trip register/read/list workers; `reissue_credential` rotates and old credential fails; `verify_worker_credential` returns expected `worker_id`. |
| `tests/test_verify_credential.py` | NEW. Dedicated coverage of `verify_worker_credential` outcomes: success returns `worker_id`; expired/wrong credential returns 401; admin-token (not a worker bearer) is rejected; "wrong worker_id returned" branch when registry was rebuilt with same id but new credentials. |
| `tests/test_groups_wire.py` | NEW. Round-trip group ops. |
| `tests/test_auth.py` | NEW. Auth dispatch + 401 on missing/wrong creds. |

### 5.5 Reference services

Each worker-host service registers itself at startup, captures the
returned credential, threads it into all subsequent wire calls and
into spawned `*_command` children's environment.

| File | Change |
|---|---|
| `reference/services/_common/src/eden_service_common/auth.py` | NEW. Helper for "register self, return credential string". |
| `reference/services/ideator/src/eden_ideator_host/cli.py` | Register at startup (idempotent on restart). |
| `reference/services/executor/src/eden_executor_host/cli.py` | Same. |
| `reference/services/evaluator/src/eden_evaluator_host/cli.py` | Same. |
| `reference/services/orchestrator/src/eden_orchestrator/cli.py` | Same (will become more substantive in 12a-2). |
| `reference/services/web-ui/src/eden_web_ui/cli.py` | Process-level: register a deployment-wide `web-ui` admin worker for endpoints that don't have a session (admin pages, healthcheck). |
| `reference/services/web-ui/src/eden_web_ui/sessions.py` | Per-session: when a user signs in, register them as a worker (idempotent on existing `worker_id`) and store their credential in the session payload. The signed-in `worker_id` is the user-supplied name (existing sign-in shape). |
| `reference/services/web-ui/src/eden_web_ui/routes/_helpers.py` | New helper `wire_client_for_session(session)` that returns a `StoreClient` authenticated as the session's worker. ALL human-driven wire calls (claim/submit on ideator / executor / evaluator / admin pages) MUST route through this helper, NOT through the process-level admin client. |
| `reference/services/_common/src/eden_service_common/container_exec.py` | New env var passed into spawned children: `EDEN_WORKER_CREDENTIAL`. |

### 5.6 Compose / setup

| File | Change |
|---|---|
| `reference/compose/compose.yaml` | Each service gets `EDEN_ADMIN_TOKEN` env (read from `.env`). Remove `EDEN_SHARED_TOKEN` references. |
| `reference/compose/.env.example` | Replace `EDEN_SHARED_TOKEN` with `EDEN_ADMIN_TOKEN`; add `EDEN_WORKER_CREDENTIAL` placeholder (set per-service after registration). |
| `reference/scripts/setup-experiment/setup-experiment.sh` | Generate `EDEN_ADMIN_TOKEN`. The startup-time auto-registration of worker hosts handles credential issuance; nothing for setup-experiment to do beyond admin-token generation. |
| `reference/compose/healthcheck/smoke.sh` | Validate that worker hosts register + claim + submit successfully. |

### 5.7 Conformance suite (`conformance/`)

| File | Change |
|---|---|
| `scenarios/test_worker_registration.py` | NEW. Register worker; idempotent re-register; admin-only enforcement. |
| `scenarios/test_group_resolution.py` | NEW. Direct membership; transitive membership; cycle rejection. |
| `scenarios/test_claim_eligibility.py` | NEW. Target=null (any worker); target=worker_id (only that worker); target=group_id (members only); target+missing-registration. |
| `scenarios/test_attribution_persistence.py` | NEW. `submitted_by` / `executed_by` / `evaluated_by` survive task / idea / variant terminal state. |
| `scenarios/test_worker_auth.py` | NEW. Submit fails when authenticated worker_id ≠ claim's worker_id. Submit succeeds across applications (claim from one, submit from another) when both auth as same worker. |
| `src/conformance/harness/_seed.py` | Update fixture to register a default `eric`-worker for tests that need a single-worker happy path. |

### 5.8 Docs

| File | Change |
|---|---|
| `AGENTS.md` | Note the per-worker auth in "Current phase". |
| `docs/glossary.md` | Promote the §9 "Identity and routing (forward-looking)" content to first-class, since it's now real. |
| `docs/roadmap.md` | Mark Phase 12a-1 complete with a roadmap delta. |
| `docs/design/orchestrator-and-worker-roles.md` | Mark §1 (worker identity), §5 (attribution), §8 (claims-scoped) as resolved by 12a-1. |

## 6. Test design

### 6.1 Cycle detection

The group cycle-detection logic is the highest-risk piece because
it's an invariant the rest of the system depends on. Three test
shapes:

- **Direct cycle**: register `team-a` with `team-b` as member, then
  attempt to register `team-b` with `team-a` as member → MUST
  raise `CycleDetected`.
- **Indirect cycle**: `team-a → team-b → team-c` exists; attempt to
  add `team-a` as member of `team-c` → MUST raise.
- **No false positives**: diamond-shaped membership (`team-a` and
  `team-b` both contain `worker-x`) MUST NOT raise; `worker-x` is
  legitimately in both groups.

### 6.2 Resolution under churn

Group membership can change over the experiment's lifetime:
add_to_group, remove_from_group, delete_group. Each mutation must
be observable atomically. A claim attempt happening concurrently
with `remove_from_group(worker-x, team-a)` MUST EITHER see
`worker-x` in `team-a` (and succeed if `target=team-a`) OR not see
them (and fail) — never a torn intermediate state.

### 6.3 Attribution survives terminal state

For each role's terminal state (task `completed` / `failed`):
construct a task, claim, submit, accept; then read the task and
verify `submitted_by` is populated and matches the claimant's
worker_id. Same for idea (`created_by` survives `dispatched`) and
variant (`executed_by` / `evaluated_by` survive `success` / `error`
/ `evaluation_error`).

### 6.4 Cross-application claim (CLI-to-CLI)

Spec implication of §D.6: an authenticated worker can claim from
one application and submit from another, **provided both
applications hold the same valid credential**. The conformance test:

1. Register worker `eric` (issuing credential C).
2. Authenticate as `eric` with credential C from client A; claim
   task T.
3. Disconnect client A.
4. Authenticate as `eric` with credential C from client B
   (different process / machine); submit T.
5. Submit MUST succeed.

Today this fails because client B doesn't have the per-claim token.
After 12a-1 it MUST succeed for CLI-to-CLI flows.

**Web-UI sessions are explicitly out-of-scope for cross-app
claim** in 12a-1 (per §D.5b accepted limitation). A separate
conformance test asserts this is documented behavior:

1. Register worker `eric` (credential C).
2. CLI client A authenticates as `eric` with C; claims task T.
3. Browser signs into the web-UI as `eric` (via
   `reissue_credential`, mints new credential C').
4. Web-UI's signed-in session can submit T (the claim's
   `worker_id` is `eric`; the web-UI's session-authenticated
   request matches).
5. CLI client A's submit attempt with credential C fails with
   401 / stale-credential — the credential was rotated by step 3.

Test 5 documents the asymmetry; future work (12a-2) lifts it.

### 6.5 Auth-required for submit

Submit a task without any `Authorization` header → 401.
Submit a task with admin auth (not worker auth) on a worker-scoped
endpoint → 403 (admins are not workers; the wire enforces the
distinction).
Submit as a different worker than the claimant → `WrongClaimant`
raised by `Store.submit` atomically with the transition.

### 6.6 Credential rotation during in-flight claim

Captures the same-`worker_id` continuity property the new design
implies — important to assert explicitly because the obvious
"old credential fails" check doesn't cover it:

1. Register worker `eric` (credential C₁).
2. Worker authenticates with C₁; claims task T.
3. Admin calls `reissue_credential("eric")` → mints C₂; the
   registry's stored hash now corresponds to C₂; C₁ is invalid.
4. A retry of T's submit with C₁ → 401 (binding-layer auth fail).
5. Submit of T using C₂ → succeeds; `Store.submit` finds
   `task.claim.worker_id == "eric"` and the authenticated
   worker_id from C₂ is also `"eric"`, so the atomic claim-match
   passes. The claim itself was never invalidated; only the
   credential authenticating into it was rotated.

This is the property that lets cross-app claim work (CLI-to-CLI
per §6.4) even after admin-driven credential rotation.

### 6.7 `verify_worker_credential` "wrong worker_id" branch

The startup probe (§D.1) returns the worker_id the credential
authenticates as. Two failure modes need test coverage:

1. **Bad/expired credential** → 401 (the obvious case).
2. **Wrong worker_id returned** → e.g., the registry was rebuilt
   with the same `worker_id` but new credentials issued to a
   different deployment generation. The presented credential is
   valid (200) but the response says it authenticates as a
   different `worker_id` than the host expected (the persisted
   credential file says it's `worker-A`'s, but the server now
   thinks the credential belongs to `worker-B`). Host MUST treat
   this as failure-of-equivalent-severity and escalate to
   `reissue_credential` per the §D.1 recovery flow.

The second branch is easy to miss in implementation if testing
only the obvious 401 path.

## 7. Verification gates

The chunk is mergeable when all of the following pass:

1. `uv sync` succeeds.
2. `uv run ruff check .` clean.
3. `uv run pyright` clean.
4. `uv run pytest -q` (full suite) green.
5. `uv run pytest -q -m e2e` (real-subprocess) green.
6. `uv run pytest -q -m docker` green.
7. `uv run pytest -q conformance/` green (existing scenarios still
   green; new ones added per §5.7).
8. `uv run python conformance/src/conformance/tools/check_citations.py`
   clean (every new scenario cites a real spec § per chapter 09).
9. `python3 scripts/spec-xref-check.py` clean.
10. `npx --yes markdownlint-cli2@0.14.0 "**/*.md"
    "#node_modules" "#.venv" "#docs/archive/**"
    "#docs/plans/review/**"` clean.
11. `pipx run 'check-jsonschema==0.29.4' --check-metaschema
    spec/v0/schemas/*.schema.json` clean.
12. `pipx run 'check-jsonschema==0.29.4' --schemafile
    spec/v0/schemas/experiment-config.schema.json
    tests/fixtures/experiment/.eden/config.yaml` clean.
13. `bash reference/compose/healthcheck/smoke.sh` green.
14. `bash reference/compose/healthcheck/smoke-subprocess.sh` green.
15. `bash reference/compose/healthcheck/e2e.sh` green.

The smokes will need updating to include worker-registration
checks; these aren't gates beyond their existing pass/fail.

## 8. Tricky areas

### 8.1 Per-experiment registry vs. service-process identity

A single physical service container (e.g., `ideator-host`) hosts
work for one experiment. So service ↔ experiment is 1:1 in the
reference deployment. But registration is per-experiment; if an
operator runs two experiments in two separate Compose stacks, each
stack's `ideator-host` registers separately. There's no
cross-experiment worker identity. This MUST be clear in the spec
prose.

### 8.2 Idempotent re-registration vs explicit reissue

`compose up` after a crash re-creates the worker-host containers.
The startup recovery flow is canonical (§D.1): `register_worker`
is idempotent on existing rows (returns no new token);
`reissue_credential` is the explicit recovery op when the local
credential is stale; `verify_worker_credential` is the
authenticated probe used to discriminate. Concretely, the host's
startup logic:

1. Check if there's a persisted credential at a known path
   (e.g., `/var/lib/eden/worker-credential`).
2. If yes, call `verify_worker_credential` against the wire
   using that credential. The Store must return the matching
   `worker_id`, or the credential is stale.
3. If verify succeeds: continue.
4. If verify fails (admin rotated the token, registry was wiped,
   server identity drift): the host MUST escalate to
   `reissue_credential(self_id)`, NOT to `register_worker`.
   `register_worker` is idempotent on existing rows and would
   return no new token; `reissue_credential` is the canonical
   credential-recovery path.
5. If no persisted credential exists: `register_worker(self_id)`
   for first-run; persist the returned credential.

This persistence is per-service-volume and must survive container
recreation but not volume deletion. Document the recovery posture
explicitly. Note in particular: there is **no fall-through to
fresh register** on credential failure — the existing registry
row is the authority on the worker's identity, and the only
documented escape from a stale credential is the explicit
admin-gated reissue.

### 8.3 Admin-token rotation

Rotating `EDEN_ADMIN_TOKEN` invalidates all in-flight admin sessions
but does NOT invalidate worker credentials (those are independent).
Worth documenting because it's a useful operational property — if
the admin token leaks, you rotate without disturbing running
worker fleets.

### 8.4 Wire-payload ordering

Two new top-level URL paths (`/v0/experiments/<id>/workers`,
`/v0/experiments/<id>/groups`). Existing paths
(`/v0/experiments/<id>/tasks`, etc.) keep their shape; only the
auth scheme on them changes. The wire-protocol spec chapter must
spell out the auth-scheme change as a versioning event — even
though we're not bumping the spec version (greenfield), a third
party reading the spec needs to see clearly that pre-12a clients
are incompatible.

### 8.5 Token storage hygiene

Worker credentials are bearer tokens. Any logging that captures
HTTP request headers MUST redact `Authorization`. Reference impl's
structured logger needs an explicit redaction rule. The wire test
suite gains a "no Authorization in logs" check.

### 8.6 Conformance test cross-pollination

Existing conformance scenarios assume the old shared-bearer model
(harness creates a single bearer; everything claims with it). All
existing scenarios need updating to:

- Register a default test worker at fixture-setup time.
- Use that worker's credential for claims instead of the shared
  bearer.
- The harness gains a `default_worker` helper.

This is mechanical but touches every scenario. Plan accordingly.

### 8.7 Deployment-bootstrap chicken-and-egg

To register a worker, you need an admin token. To get an admin
token, the deployment has to be set up. Setup-experiment generates
the admin token and writes it to `.env`. Worker hosts that come up
read it from env, register themselves, persist their credentials,
and from then on don't need the admin token. Documenting this
boot-time-only use clearly avoids confusion ("why do my services
have the admin token in their env?").

## 9. Risks / things to watch

- **Wire-protocol breaking change.** Every existing wire client
  (including the reference services) needs updating in lockstep.
  A partial rollout where some services use shared-bearer and
  others use per-worker auth is a hard-to-debug state. Land in
  one PR; smoke validates the end-state.
- **Postgres schema migration on a live registry.** Per §2.3, no
  migration is needed (greenfield). But the `_postgres_schema.py`
  bump still has to drop the now-removed `claim.token` field
  cleanly; verify the schema-bump path on a wiped volume, since
  there's no "rollback to old shape" once the new tables exist.
- **Per-claim token removal as a SHOULD-vs-MUST question.** The
  current spec at chapter 4 frames the per-claim token as
  authentication. Removing it requires careful spec prose so a
  third-party impl reading 12a+ knows the field is gone.
- **Conformance suite churn.** Updating every scenario to use
  per-worker auth touches a lot of files. Risk: some scenario
  silently keeps the old auth shape and "passes" only because the
  harness defaults are forgiving. Mitigation: `WorkerNotEligible`
  and `WrongClaimant` should be raised loudly (not silently
  fallback to allow), so any scenario that's not actually
  authenticating shows up as a failure.

## 10. Sequence within the chunk

Even as a single PR, the work has internal ordering. Suggested:

1. **Spec prose first** (chapters 02 / 04 / 07 / 08 plus new
   schemas). This is the contract; everything else implements it.
2. **Pydantic models** for the new shapes + attribution fields.
   Schema parity tests will fail until both sides line up.
3. **Storage protocol + in-memory backend**. Transitive group
   resolution + cycle detection are the load-bearing logic; get them
   right here.
4. **SQLite + postgres backends**. Same Protocol, schema bumps.
5. **Wire server + client**. Auth dispatch, new endpoints.
6. **Reference services**. Each host registers + authenticates.
7. **Reference binding doc**. Subprocess-binding env-var update.
8. **Conformance scenarios**. Existing ones updated to use
   per-worker auth via the harness; new scenarios added.
9. **Compose + smokes**. End-to-end validation.
10. **Docs**. Glossary, AGENTS.md, roadmap delta.

An agent running this chunk should expect tests to go red around
step 2 and come back green around step 8.

## 11. Out of scope (followups)

- **Multi-orchestrator HA** (12a-2). Whether multiple
  auto-orchestrators can run concurrently is a 12a-2 question
  because it depends on the orchestrator-as-role contract.
- **`intended_executor` hint on ideas** (12a-3). Per §D.4 there's
  no `intended_executor` field on ideas in 12a-1; it lands in
  12a-3 alongside the operator-driven ideation-task creation flow.
- **Per-decision dispatch_mode flags** (12a-2). Tasks gain
  `target` here, but the orchestrator doesn't yet honor a
  per-decision-type opt-out for auto-dispatch.

## 12. Estimated effort

- **Spec prose**: ~1 day. Four chapters + two new schemas.
- **Pydantic + storage**: ~2 days. Including cycle-detection
  correctness + protocol-conformance tests across three backends.
- **Wire**: ~1 day. Auth dispatch is the load-bearing piece.
- **Reference services + reference binding**: ~1 day. Mostly
  mechanical (each service adds the same registration boilerplate).
- **Conformance**: ~1 day. Updating existing scenarios + writing
  the new ones.
- **Compose + smokes + docs**: ~0.5 day.

**Realistic total: ~6–7 working days** of focused work.
The chunk plan itself takes the standard ~half-day; this document
is that.
