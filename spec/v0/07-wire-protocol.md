# Wire Protocol — HTTP binding

This chapter specifies an **HTTP binding** for the behaviors [chapter 4](04-task-protocol.md) (task state machine), [chapter 5](05-event-protocol.md) (event log), and [chapter 8](08-storage.md) (storage-side contract) already pin normatively. It does not re-specify those behaviors; every endpoint below maps 1:1 to a store-side operation named in those chapters and inherits that operation's semantics.

A conforming EDEN deployment is **not required** to use this HTTP binding. The protocol constrains observable behavior; the transport that carries it is a deployment choice. This chapter exists so that HTTP-based deployments — including the Phase-8a reference deployment — can interoperate byte-for-byte.

The binding intentionally omits the reference orchestrator's decision helpers (`validate_terminal`, `validate_evaluation`). Those are reference-impl conveniences, not chapter-4/5/8 operations. A conforming third-party orchestrator is free to implement their decision logic inline; see §9.

## 1. Transport

### 1.1 HTTP version and content type

A conforming server MUST accept HTTP/1.1 over TCP on a deployment-chosen host and port. HTTP/2 is OPTIONAL. Requests and responses that carry a body MUST use media type `application/json; charset=utf-8`, except for error responses which use `application/problem+json; charset=utf-8` (§6). Empty responses (e.g. 204) MUST NOT carry a body.

### 1.2 Path versioning

All normative paths are rooted at `/v0/`. Paths under `/_reference/` are explicitly non-normative and carry reference-implementation-specific behavior; a conforming client MUST NOT rely on them being present or on their semantics.

Breaking changes to the binding produce a new root (`/v1/`), the same way breaking changes to the spec produce a new directory (`spec/v1/`). Non-breaking additions (new endpoints, new optional fields in existing payloads) stay under `/v0/`.

### 1.3 Experiment scoping

Every normative path segment below `/v0/` begins with `experiments/{experiment_id}/`, where `{experiment_id}` is an opaque, system-minted `exp_*` id ([`02-data-model.md`](02-data-model.md) §1.6); every request MUST additionally send the header `X-Eden-Experiment-Id: {experiment_id}` with a value equal to the path segment. A server MUST reject a request whose header disagrees with its path segment with `eden://error/experiment-id-mismatch` (HTTP 400); see §9. The header is defense-in-depth against misrouted clients and proxies.

**Exception: portable-checkpoint endpoints.** Paths under `/v0/checkpoints/` (currently only `POST /v0/checkpoints/import` per §14.2) are not experiment-scoped at the URL level — the experiment_id appears in the uploaded checkpoint manifest's `experiment_id` field ([`10-checkpoints.md`](10-checkpoints.md) §5), not in the URL. On these endpoints the `X-Eden-Experiment-Id` header is OPTIONAL; when present, it MUST equal the manifest's `experiment_id` after applying any `as_experiment_id` override. A mismatch MUST be rejected with `eden://error/experiment-id-mismatch` (HTTP 400). When the header is absent the server proceeds with the experiment id derived from the manifest + override.

**Exception: control-plane endpoints.** Paths under `/v0/control/` (§15) are deployment-rooted, not experiment-rooted. Control-plane operations target the deployment-level experiment registry, leases, and deployment-scoped worker / group registry ([`11-control-plane.md`](11-control-plane.md) §2, §4, §6). The experiment id, when relevant, appears in the path or body of the operation rather than in `/v0/experiments/{experiment_id}/...`. The `X-Eden-Experiment-Id` header is OPTIONAL on `/v0/control/` endpoints; when present it has no protocol meaning at this surface and is ignored.

## 2. Task operations (chapter 4)

The following endpoints bind the task-store operations in [`04-task-protocol.md`](04-task-protocol.md) §§1–5 and [`08-storage.md`](08-storage.md) §1.1. Each endpoint inherits the normative semantics of the underlying operation; this section lists only the wire shape. The `Auth` column carries the §13.3 classification (`admin` / `worker` / `either`).

| Operation | HTTP | Path | Auth |
|---|---|---|---|
| `create_task` | `POST` | `/v0/experiments/{E}/tasks` | worker (group-gated, see §2.1) |
| `list_tasks` | `GET`  | `/v0/experiments/{E}/tasks` | either |
| `read_task` | `GET`  | `/v0/experiments/{E}/tasks/{T}` | either |
| `read_submission` | `GET`  | `/v0/experiments/{E}/tasks/{T}/submission` | either |
| `claim` | `POST` | `/v0/experiments/{E}/tasks/{T}/claim` | worker |
| `submit` | `POST` | `/v0/experiments/{E}/tasks/{T}/submit` | worker |
| `accept` | `POST` | `/v0/experiments/{E}/tasks/{T}/accept` | worker (group-gated: `orchestrators`) |
| `reject` | `POST` | `/v0/experiments/{E}/tasks/{T}/reject` | worker (group-gated: `orchestrators`) |
| `reclaim` | `POST` | `/v0/experiments/{E}/tasks/{T}/reclaim` | worker |
| `reassign_task` | `POST` | `/v0/experiments/{E}/tasks/{T}/reassign` | worker (group-gated: `admins`) |
| `update_dispatch_mode` | `PATCH` | `/v0/experiments/{E}/dispatch_mode` | worker (group-gated: `admins`) |
| `terminate_experiment` | `POST` | `/v0/experiments/{E}/terminate` | worker (group-gated: `admins`, `orchestrators`) |
| `read_experiment_state` | `GET` | `/v0/experiments/{E}/state` | either |
| `emit_policy_error` | `POST` | `/v0/experiments/{E}/policy-errors` | worker (group-gated: `orchestrators`) |

The mutating task operations are worker-gated; some are additionally **group-gated** by the [`03-roles.md`](03-roles.md) §6 orchestrator-role contract and [`02-data-model.md`](02-data-model.md) §7.5 reserved groups:

- `create_task`: caller MUST be in `admins` OR `orchestrators` for any `kind` (`ideation` / `execution` / `evaluation`). The pre-12a-3 restriction that limited `kind="execution"` to `orchestrators` only is lifted: 12a-3's per-idea `intended_executor` hint ([`02-data-model.md`](02-data-model.md) §5.1) gives operators a normative routing seed for operator-driven execution tasks. The §13.3 dispatcher rejects non-members with 403 `eden://error/forbidden`.
- `accept` / `reject`: caller MUST be in the `orchestrators` group (the [`04-task-protocol.md`](04-task-protocol.md) §4.3 terminal-transition decision is the orchestrator role's job per [`03-roles.md`](03-roles.md) §6).
- `reclaim`: caller MUST be a registered worker; the [`04-task-protocol.md`](04-task-protocol.md) §5.1 cause vocabulary distinguishes operator vs. policy reclamation but the binding does not gate on group membership beyond "any registered worker".
- `reassign_task` / `update_dispatch_mode`: caller MUST be in the `admins` group ([`04-task-protocol.md`](04-task-protocol.md) §6.2, §7.2).
- `terminate_experiment`: caller MUST be in the `admins` OR `orchestrators` group ([`04-task-protocol.md`](04-task-protocol.md) §8.2). The operator-driven path uses an `admins` bearer; the orchestrator's policy-driven termination ([`03-roles.md`](03-roles.md) §6.2 decision-type 0) commits the same `running → terminated` transition through an `orchestrators` bearer. Gating on either group lets the orchestrator run its own termination decision without being over-privileged into `admins` (which would also grant it `reassign_task` / `update_dispatch_mode` authority). This mirrors the `accept` / `reject` and `emit_policy_error` gating, all of which are `orchestrators`-group operations for the same [`03-roles.md`](03-roles.md) §6 rationale.
- `emit_policy_error`: caller MUST be in the `orchestrators` group. The endpoint exists to satisfy chapter 03 §6.2 decision-type 0's fault-tolerance MUST when the orchestrator runs over the wire binding; the policy is invoked by orchestrators, not admins, so the authority gate is `orchestrators`-only (the `admins` path would expose a manual log-spam vector that the protocol does not need).
- `claim` / `submit`: caller MUST be a registered worker satisfying the task's `target` ([`04-task-protocol.md`](04-task-protocol.md) §3.5, §4.1); no group gate beyond the target eligibility check.

The `Auth` column's bare `worker` annotation means "any registered worker"; the group-gated entries above narrow that to a specific reserved-group membership check. Future phases MAY introduce additional group-gating without breaking the wire grammar — the dispatcher rejects on the group check identically to how it rejects on the principal class.

### 2.1 Create

`POST /v0/experiments/{E}/tasks` accepts a JSON request body whose shape matches [`schemas/task.schema.json`](schemas/task.schema.json) with `state == "pending"` and no `claim`. On success the server returns 200 with the created task as the response body (same schema). The composite-commit rules in [`05-event-protocol.md`](05-event-protocol.md) §2.2 apply: creating an `execution` task transitions the referenced idea atomically with the task insert; the server MUST perform both effects in a single transaction.

**Authority (per-kind):**

- `kind == "ideation"`: caller MUST be in the `admins` or `orchestrators` group (operator-driven seed and continuous auto-orchestrator policy both create ideation tasks; the orchestrator-role contract is in [`03-roles.md`](03-roles.md) §6.2).
- `kind == "execution"`: caller MUST be in the `admins` or `orchestrators` group. The orchestrator-role contract drives auto-dispatch ([`03-roles.md`](03-roles.md) §6.2 decision-type 2); operators MAY also drive `create_task(kind="execution")` directly. The request body MUST carry `payload.idea_id`; the body MAY carry an explicit top-level `target` ([`02-data-model.md`](02-data-model.md) §3.5) override, which wins over `idea.intended_executor` per [`04-task-protocol.md`](04-task-protocol.md) §2 (the `kind=="execution"` create-task precondition). When the body's `target` is absent the Store populates it from the referenced idea's `intended_executor` (or `null` when the idea has none). Pre-12a-3 lineages restricted this to `orchestrators` only; 12a-3 lifts the restriction now that the `intended_executor` field gives operators a non-fungible routing seed.
- `kind == "evaluation"`: caller MUST be in the `admins` or `orchestrators` group. The `admins` path enables manual evaluation-dispatch per [`03-roles.md`](03-roles.md) §6.5 when `dispatch_mode.evaluation_dispatch == "manual"`.

A caller outside the authorized group(s) receives 403 `eden://error/forbidden`.

### 2.2 List and read

- `GET /v0/experiments/{E}/tasks` returns an array of tasks matching optional query parameters `kind` (one of `ideation`, `execution`, `evaluation`) and `state` (one of the five task states). Ordering is implementation-defined per [`08-storage.md`](08-storage.md) §1.1.
- `GET /v0/experiments/{E}/tasks/{T}` returns the task object with id `T`, or 404 `eden://error/not-found`.

### 2.3 Claim

`POST /v0/experiments/{E}/tasks/{T}/claim` accepts a JSON body with an OPTIONAL `expires_at` timestamp. The claimant's `worker_id` is taken from the authenticated bearer (§13); the server MUST NOT accept a `worker_id` field in the request body that disagrees with the authenticated identity. On success the server returns 200 with a `claim` object matching the shape from [`02-data-model.md`](02-data-model.md) §3.4 (see also [`schemas/task.schema.json`](schemas/task.schema.json)). The [`04-task-protocol.md`](04-task-protocol.md) §3.5 target-eligibility check is performed atomically with the claim write: a non-`pending` task returns 409 `eden://error/illegal-transition`; a non-registered authenticated worker returns 403 `eden://error/worker-not-registered`; a registered worker who fails the target check returns 403 `eden://error/worker-not-eligible`.

### 2.4 Submit

`POST /v0/experiments/{E}/tasks/{T}/submit` accepts a JSON body containing only the role-specific `payload` (matching the claim's task `kind`). The submitting `worker_id` is taken from the authenticated bearer (§13); the server MUST forward the authenticated `worker_id` to `Store.submit(task_id, worker_id, payload)`, which performs the [`04-task-protocol.md`](04-task-protocol.md) §4.1 atomic claim-match. The binding MUST NOT issue its own pre-flight `read_task → compare` check (the prior token model has been removed; see [`04-task-protocol.md`](04-task-protocol.md) §4.1).

Atomicity, idempotency, and content-equivalence rules from [`04-task-protocol.md`](04-task-protocol.md) §4 apply unchanged. A resubmit by the same authenticated worker with a content-equivalent payload MUST return 200; a resubmit with a divergent payload MUST return 409 `eden://error/conflicting-resubmission`. A submit by a different authenticated worker than the recorded claimant MUST return 403 `eden://error/wrong-claimant`. A submit against a task whose `claim` has been cleared (because of reclamation or a terminal transition) MUST return 409 `eden://error/not-claimed`.

### 2.5 Accept and reject

- `POST /v0/experiments/{E}/tasks/{T}/accept` transitions `submitted → completed` per [`04-task-protocol.md`](04-task-protocol.md) §4.3. The request body is empty (the orchestrator has already persisted the submission; the server re-reads it to apply the composite-commit effects).
- `POST /v0/experiments/{E}/tasks/{T}/reject` transitions `submitted → failed` per [`04-task-protocol.md`](04-task-protocol.md) §4.3. The request body carries a single field `reason` drawn from the closed v0 vocabulary ([`05-event-protocol.md`](05-event-protocol.md) §3.1): `"worker_error"`, `"validation_error"`, or `"policy_limit"`.

**Authority:** caller MUST be in the `orchestrators` group ([`02-data-model.md`](02-data-model.md) §7.5). The [`04-task-protocol.md`](04-task-protocol.md) §4.3 terminal-transition decision is the orchestrator role's responsibility per [`03-roles.md`](03-roles.md) §6; a caller outside the group receives 403 `eden://error/forbidden`.

### 2.6 Reclaim

`POST /v0/experiments/{E}/tasks/{T}/reclaim` transitions a `claimed` or (operator-invoked) `submitted` task back to `pending` per [`04-task-protocol.md`](04-task-protocol.md) §5. The request body carries a single field `cause` drawn from the closed v0 vocabulary: `"expired"`, `"operator"`, or `"health_policy"`.

### 2.7 Reassign

`POST /v0/experiments/{E}/tasks/{T}/reassign` updates a task's `Task.target` per [`04-task-protocol.md`](04-task-protocol.md) §6. The request body shape is:

```json
{"new_target": null | {"kind": "worker"|"group", "id": "<wkr_*|grp_*>"}, "reason": "<string>"}
```

- `new_target` is the [`02-data-model.md`](02-data-model.md) §3.5 target value to install: `null` for "any registered worker", or a tagged object whose `id` is an opaque member identifier (`wkr_*` or `grp_*`, [`02-data-model.md`](02-data-model.md) §1.6) matching `kind`.
- `reason` is a free-form audit string (typical: `"operator"`, `"failed_worker"`, `"misrouted"`); the protocol does not enumerate the set.

On success the server returns 200 with the updated task body. Per [`04-task-protocol.md`](04-task-protocol.md) §6.1, the behavior depends on the task's current state:

- `pending` task: atomic `task.target` update + `task.reassigned` event.
- `claimed` task: composite atomic commit — clear claim, update target, return to `pending`. Both `task.reclaimed` (with `cause == "operator"`) and `task.reassigned` events fire in one commit ([`05-event-protocol.md`](05-event-protocol.md) §2.2).
- `submitted` or terminal (`completed` / `failed`): 409 `eden://error/invalid-precondition`.

**Authority:** caller MUST be in the reserved-name `admins` group ([`04-task-protocol.md`](04-task-protocol.md) §6.2), resolved by name to its `grp_*` id and checked against the caller's opaque `worker_id`. A caller outside the group receives 403 `eden://error/forbidden`. The caller's actor identifier (`admin` or `wkr_*`) is recorded in the `task.reassigned` event's `reassigned_by` field.

### 2.8 Update dispatch mode

`PATCH /v0/experiments/{E}/dispatch_mode` accepts a partial `dispatch_mode` object (any subset of the five keys from [`02-data-model.md`](02-data-model.md) §2.4) and atomically merges it into the experiment's stored `dispatch_mode` per [`04-task-protocol.md`](04-task-protocol.md) §7. The request body shape is the partial object directly (not wrapped):

```json
{"evaluation_dispatch": "manual"}
```

Each value MUST be either `"auto"` or `"manual"`. An unrecognized value, or an unrecognized top-level key whose presence the server cannot reasonably ignore, returns 400 `eden://error/bad-request`. Unspecified keys are unchanged.

On success the server returns 200 with the full resulting `dispatch_mode` object as the response body. A successful update emits exactly one `experiment.dispatch_mode_changed` event ([`05-event-protocol.md`](05-event-protocol.md) §3.4) whose payload carries the resulting state and the `changed` diff; a no-op patch (every supplied key already matched) MAY emit an event with empty `changed` or skip the event entirely.

**Authority:** caller MUST be in the reserved-name `admins` group ([`04-task-protocol.md`](04-task-protocol.md) §7.2), resolved by name to its `grp_*` id and checked against the caller's opaque `worker_id`. A caller outside the group receives 403 `eden://error/forbidden`. The caller's actor identifier (`admin` or `wkr_*`) is recorded in the event's `updated_by` field.

A companion `GET /v0/experiments/{E}/dispatch_mode` MAY be exposed by the binding for read access; v0 does not require it (the field is reachable as part of `read_experiment_config` once that op exists; for now, the event log carries the authoritative history).

### 2.9 Terminate experiment

`POST /v0/experiments/{E}/terminate` binds the [`04-task-protocol.md`](04-task-protocol.md) §8.1 `terminate_experiment` operation. The request body shape is:

```json
{"reason": "<string>"}
```

- `reason` is a free-form string carrying the operator's explanation. The server records it in the `experiment.terminated` event ([`05-event-protocol.md`](05-event-protocol.md) §3.4); the protocol does not enumerate a closed vocabulary.

On success the server returns 200 with the resulting experiment object (matching [`schemas/experiment.schema.json`](schemas/experiment.schema.json)). The operation is idempotent on the terminated state per [`04-task-protocol.md`](04-task-protocol.md) §8.1: a second call against an already-terminated experiment MUST return 200 with the existing state and MUST NOT append a second event; the winning caller's `reason` from the first commit is the one recorded.

The transition is observable through both the response body and the event stream. Subscribers that consume the per-experiment event log observe `experiment.terminated` atomically with the state field update ([`05-event-protocol.md`](05-event-protocol.md) §2).

**Authority:** caller MUST be in the `admins` OR `orchestrators` group ([`04-task-protocol.md`](04-task-protocol.md) §8.2). A caller outside both groups receives 403 `eden://error/forbidden`. The caller's `worker_id` is recorded in the event's `terminated_by` field. The two groups correspond to the two termination paths: an operator drives termination through an `admins` bearer, while the orchestrator commits its policy-driven termination ([`03-roles.md`](03-roles.md) §6.2 decision-type 0) through an `orchestrators` bearer. The terminate op's authority is independent of `dispatch_mode.termination`'s value: an operator MAY drive termination even when `dispatch_mode.termination == "auto"`; the orchestrator's policy-driven path and the operator wire op race-resolve via the §6.4 exact-idempotent rule (see [`03-roles.md`](03-roles.md) §6.4.1).

A companion `GET /v0/experiments/{E}/state` returns the current state as `{"state": "running" | "terminated"}`. The endpoint is available to any registered worker.

### 2.10 Emit policy error

`POST /v0/experiments/{E}/policy-errors` is the wire surface for the [`03-roles.md`](03-roles.md) §6.2 decision-type 0 fault-tolerance MUST: when a deployment-supplied termination policy callable raises rather than returning `Continue` or `Terminate(...)`, the orchestrator MUST emit a registered `experiment.policy_error` event ([`05-event-protocol.md`](05-event-protocol.md) §3.4) so operators see the failure in the event log. This endpoint exists so an orchestrator running over the wire binding (rather than embedded in-process with a `Store`) can append that event without bypassing the protocol.

The request body shape is:

```json
{"policy_kind": "<string>", "error_type": "<string>", "error_message": "<string>"}
```

- `policy_kind` identifies which policy kind raised. v0 defines only `"termination"`; future decision types that introduce policy callables MAY add new values.
- `error_type` is the exception class name (e.g. `"ValueError"`); free-form per implementation language.
- `error_message` is the exception's `str()` representation; free-form.

All three fields MUST be present; `policy_kind` and `error_type` MUST be non-empty. The body MUST NOT carry additional keys (the request schema has `additionalProperties: false`).

On success the server returns 204 with an empty body. The single-event append is exempt from the [`05-event-protocol.md`](05-event-protocol.md) §2 transactional invariant: no protocol-owned state mutation pairs with it, so the endpoint is not a composite commit. The append is at-least-once observable; a transport-indeterminate failure on the emit path is an at-most-once observability gap rather than a state-correctness risk, and the orchestrator-side caller SHOULD log the original fault locally as a fallback (the reference dispatch driver does so).

**Authority:** caller MUST be in the `orchestrators` group ([`02-data-model.md`](02-data-model.md) §7.5). A caller outside the group receives 403 `eden://error/forbidden`. The endpoint is NOT exposed to `admins` — the policy is invoked by orchestrators, not admins, and `admins` access would create a manual log-spam surface the protocol does not need.

## 3. Idea operations

| Operation | HTTP | Path | Auth |
|---|---|---|---|
| `create_idea` | `POST` | `/v0/experiments/{E}/ideas` | worker |
| `list_ideas` | `GET` | `/v0/experiments/{E}/ideas` | either |
| `read_idea` | `GET` | `/v0/experiments/{E}/ideas/{P}` | either |
| `mark_idea_ready` | `POST` | `/v0/experiments/{E}/ideas/{P}/mark-ready` | worker |

Request and response bodies match [`schemas/idea.schema.json`](schemas/idea.schema.json). `list_ideas` accepts an optional `state` query parameter. `mark-ready` has an empty request body.

`create_idea` MAY include an additional advisory field `warnings: list[string]` in its response body alongside the idea fields. Warnings are non-normative diagnostic strings (e.g., a soft-check that the submitted `slug` collides with an existing idea in the same experiment — slug uniqueness is not a protocol invariant per [`02-data-model.md`](02-data-model.md) §5.1, so the request still succeeds 200). Implementations MAY omit the field; clients MUST NOT rely on its presence or contents for correctness.

## 4. Variant operations

| Operation | HTTP | Path | Auth |
|---|---|---|---|
| `create_variant` | `POST` | `/v0/experiments/{E}/variants` | worker (kind-gated: `kind == "baseline"` requires `orchestrators`) |
| `list_variants` | `GET` | `/v0/experiments/{E}/variants` | either |
| `read_variant` | `GET` | `/v0/experiments/{E}/variants/{T}` | either |
| `declare_variant_evaluation_error` | `POST` | `/v0/experiments/{E}/variants/{T}/declare-evaluation-error` | worker |
| `integrate_variant` | `POST` | `/v0/experiments/{E}/variants/{T}/integrate` | worker (group-gated: `orchestrators`) |

`integrate_variant` binds [`06-integrator.md`](06-integrator.md) §3.4 and carries additional idempotency rules (§5 below); the other endpoints are transport-only bindings of their [`08-storage.md`](08-storage.md) §1.7 operations.

**Per-`kind` authority on `create_variant`:** an ordinary variant (`kind` absent — the executor's output) is worker-authenticated as before. Creating a `kind == "baseline"` variant ([`02-data-model.md`](02-data-model.md) §9.4) additionally requires the caller be in the `orchestrators` group ([`02-data-model.md`](02-data-model.md) §7.5); a caller outside the group receives 403 `eden://error/forbidden`. The carve closes a privilege hole: a baseline MAY be created directly in `success` carrying arbitrary `evaluation` metrics (the override path), so allowing any registered worker to create one would let a malicious/buggy executor fabricate a passing baseline. The baseline create body carries `kind: "baseline"`, MAY omit `idea_id`, and MAY carry a terminal `status: "success"` with `evaluation` + `completed_at` (the override path); the store enforces the precondition relaxation and validates the metrics against `evaluation_schema` per [`08-storage.md`](08-storage.md) §1.7.

**Authority on `integrate_variant`:** caller MUST be in the `orchestrators` group ([`02-data-model.md`](02-data-model.md) §7.5). The integration decision is the orchestrator role's job per [`03-roles.md`](03-roles.md) §6.2 decision 4; a caller outside the group receives 403 `eden://error/forbidden`.

## 5. `integrate_variant` — same-value idempotency

The `POST /v0/experiments/{E}/variants/{T}/integrate` endpoint has additional semantics beyond transport-binding because [`06-integrator.md`](06-integrator.md) §3.4's atomicity invariant spans the process boundary. The request body carries the single field `variant_commit_sha`.

- **Success (first call).** The server atomically writes `variant_commit_sha` on the variant and appends `variant.integrated` per [`06-integrator.md`](06-integrator.md) §3.4. Response: 200, empty body.
- **Same-value idempotency.** A repeated call whose `variant_commit_sha` equals the value already stored on the variant MUST return 200 and MUST NOT append a second `variant.integrated` event. This is what lets a client safely retry an `integrate_variant` request after a transport- indeterminate failure without double-commit.
- **Different-value rejection.** A call whose `variant_commit_sha` differs from the value already stored MUST return 409 `eden://error/invalid-precondition`. A conforming client MUST surface this as an atomicity violation (the chapter 6 §1.2 sole-writer rule has been violated somewhere upstream); operator intervention is required.
- **Other preconditions.** Standard [`06-integrator.md`](06-integrator.md) §3.4 preconditions continue to apply: the variant MUST be in `status == "success"`, and the `variant_commit_sha` MUST be a well-formed commit SHA; violations return `invalid-precondition`.
- **Baseline rejection.** A `kind == "baseline"` variant ([`02-data-model.md`](02-data-model.md) §9.4) MUST NOT be integrated: an `integrate_variant` call against one returns 409 `eden://error/invalid-precondition`, writes no `variant_commit_sha`, and emits no `variant.integrated` event. This is the wire-side defense-in-depth behind the integrator's §2 skip ([`06-integrator.md`](06-integrator.md) §2).

A client that issues `integrate_variant` and does not receive a 2xx response (connection reset, read timeout, proxy disconnect) MUST treat the outcome as indeterminate and MUST NOT assume the server has not committed. The reference reconciliation procedure is a read-back (`GET /v0/experiments/{E}/variants/{T}`) that resolves to one of three outcomes:

- observed `variant_commit_sha` equals the sent value → treat as success;
- observed `variant_commit_sha` is set but differs → raise the same atomicity-violation error the server would have;
- observed `variant_commit_sha` is absent, or the read-back itself fails → raise an indeterminate-result error. Absence does **not** prove the request failed; it may still be in flight.

## 6. Worker registry operations

Per-experiment worker registry endpoints. Mutating operations are admin-gated (§13.3); read operations are accessible to any authenticated principal — admin or registered worker — for the experiment.

| Operation | HTTP | Path | Auth |
|---|---|---|---|
| `register_worker` | `POST` | `/v0/experiments/{E}/workers` | admin |
| `list_workers` | `GET` | `/v0/experiments/{E}/workers` | admin or worker |
| `read_worker` | `GET` | `/v0/experiments/{E}/workers/{W}` | admin or worker |
| `reissue_credential` | `POST` | `/v0/experiments/{E}/workers/{W}/reissue-credential` | admin |
| `verify_worker_credential` | `GET` | `/v0/experiments/{E}/whoami` | worker |

### 6.1 Register

`POST /v0/experiments/{E}/workers` accepts a JSON body `{name?, labels?}`. The caller does **not** supply a `worker_id`; the server **mints** an opaque `wkr_*` id ([`02-data-model.md`](02-data-model.md) §1.6) on every call. The optional `name` is the operator-supplied display label ([`02-data-model.md`](02-data-model.md) §1.7). On success the server returns 200 with `{worker_id, name?, registration_token, ...}` — `registration_token` is the plaintext credential, returned **exactly once**, and the client MUST persist it (alongside the minted `worker_id`) locally; subsequent reads MUST NOT return it. The response also carries the `Worker` shape ([`schemas/worker.schema.json`](schemas/worker.schema.json)).

Because the id is system-minted, **every** `register_worker` call mints a fresh worker + credential (names MAY collide); there is no idempotent re-registration by id. A service that must survive restart persists the minted `worker_id` and recovers its credential via `reissue_credential` (§6.3), not by re-registering. If the supplied `name` is ill-formed (the display-name grammar of [`02-data-model.md`](02-data-model.md) §1.7) the server returns 422 `eden://error/invalid-name`; if it equals a reserved worker name (`admin` / `system` / `internal`, [`02-data-model.md`](02-data-model.md) §6.1) the server returns 409 `eden://error/reserved-identifier`.

### 6.2 List and read

- `GET /v0/experiments/{E}/workers` returns `{"workers": [Worker, ...]}` containing every worker registered for the experiment. The wire-visible Worker shape MUST NOT include any credential or hash field ([`02-data-model.md`](02-data-model.md) §6.2). The endpoint accepts an OPTIONAL `?name=<n>` query parameter: when supplied, the server returns only workers whose `name` exactly matches `<n>` (case-sensitive, against the canonical NFC form), 0..N results — names MAY collide, so callers disambiguate by the opaque id. v0 does not define other query-parameter filtering; a future phase (Phase 12a-3, alongside the per-decision-type dispatch hints) MAY introduce label-based filtering as a backward-compatible refinement.
- `GET /v0/experiments/{E}/workers/{W}` returns the named Worker (the `{W}` path segment is an opaque `wkr_*` id) or 404 `eden://error/not-found`.

### 6.3 Reissue credential

`POST /v0/experiments/{E}/workers/{W}/reissue-credential` accepts an empty body. On success the server mints a fresh `registration_token`, replaces the stored credential record, and returns 200 with `{worker_id, registration_token}`. The prior credential is **invalidated** by this operation: any subsequent wire call presenting the prior bearer MUST be rejected with 401.

This is the canonical credential-recovery path. Hosts MUST NOT call `register_worker` to recover a lost credential — `register_worker` mints a brand-new worker with a new opaque id every call (§6.1; [`02-data-model.md`](02-data-model.md) §6.3), so re-registering would orphan the prior identity rather than recover it. A restarting host reissues against its persisted `worker_id`.

### 6.4 Verify credential (whoami)

`GET /v0/experiments/{E}/whoami` is the authenticated probe used by host startup recovery ([`02-data-model.md`](02-data-model.md) §6.3 + reference-binding flow). On success the server returns 200 with `{"worker_id": "<wkr_*>", "name": "<name>" | null}` — the opaque `worker_id` the presented bearer authenticates as, plus the worker's optional display name. Bad/missing credentials return 401 `eden://error/unauthorized`. The `read_worker` endpoint is **not** an auth probe; it returns the worker record without re-authenticating the credential against that specific worker.

A host whose persisted credential authenticates as a `worker_id` other than the one it expects (e.g., the registry was rebuilt with the same id but a different credential generation) MUST treat that as failure-of-equivalent-severity to a 401 and recover via `reissue_credential`.

## 7. Group operations

Per-experiment group registry endpoints. All mutating ops are admin-gated; reads are accessible to any authenticated principal.

| Operation | HTTP | Path | Auth |
|---|---|---|---|
| `register_group` | `POST` | `/v0/experiments/{E}/groups` | admin |
| `list_groups` | `GET` | `/v0/experiments/{E}/groups` | admin or worker |
| `read_group` | `GET` | `/v0/experiments/{E}/groups/{G}` | admin or worker |
| `add_to_group` | `POST` | `/v0/experiments/{E}/groups/{G}/members` | admin |
| `remove_from_group` | `DELETE` | `/v0/experiments/{E}/groups/{G}/members/{M}` | admin |
| `delete_group` | `DELETE` | `/v0/experiments/{E}/groups/{G}` | admin |

### 7.1 Register

`POST /v0/experiments/{E}/groups` accepts `{name?, members?}`. The caller does **not** supply a `group_id`; the server **mints** an opaque `grp_*` id ([`02-data-model.md`](02-data-model.md) §1.6) on every call. The optional `name` is the operator-supplied display label ([`02-data-model.md`](02-data-model.md) §1.7); each entry of `members` is an opaque member identifier (`wkr_*` or `grp_*` per the [`02-data-model.md`](02-data-model.md) §1.6 grammar). On success returns 200 with the `Group` carrying the minted `group_id` and `name?` ([`schemas/group.schema.json`](schemas/group.schema.json)). Cycle violations return 409 `eden://error/cycle-detected`. An ill-formed `name` returns 422 `eden://error/invalid-name`. A `name` equal to a reserved group name (`admins` / `orchestrators`) already minted at setup returns 409 `eden://error/reserved-identifier` (the reserved groups are created at experiment setup through a privileged path, [`02-data-model.md`](02-data-model.md) §7.5).

### 7.2 Mutate

`add_to_group` accepts a body `{member_id}` where `member_id` is an opaque member identifier (`wkr_*` or `grp_*`, [`02-data-model.md`](02-data-model.md) §1.6); the `{G}` and `{M}` path segments on the group endpoints are likewise opaque `grp_*` / member ids. `add_to_group`, `remove_from_group`, and `delete_group` MUST detect cycles atomically with the write per [`02-data-model.md`](02-data-model.md) §7.3. Any mutation that would close a cycle MUST be rejected with `eden://error/cycle-detected`.

### 7.3 List and read

- `GET /v0/experiments/{E}/groups` returns `{"groups": [Group, ...]}`. It accepts an OPTIONAL `?name=<n>` query parameter with the same exact-match, case-sensitive, 0..N semantics as the worker list (§6.2) — useful for resolving a reserved group (`admins` / `orchestrators`) to its system-minted `grp_*` id by name.
- `GET /v0/experiments/{E}/groups/{G}` returns the named Group (the `{G}` path segment is an opaque `grp_*` id) or 404.

## 8. Event log

| Operation | HTTP | Path | Auth |
|---|---|---|---|
| `read_range` / `replay` | `GET` | `/v0/experiments/{E}/events` | either |
| `subscribe` | `GET` | `/v0/experiments/{E}/events/subscribe` | either |

### 8.1 Read-range and replay

`GET /v0/experiments/{E}/events` returns the log in order. It accepts one query parameter:

- `cursor` — cumulative count of events the caller has already observed. Omitted or `0` triggers `replay` per [`05-event-protocol.md`](05-event-protocol.md) §4.4.

The response is `{"events": [...], "cursor": N}` where `events` is the delivered batch (possibly empty) and `cursor` is the new cumulative count the caller should pass to the next call. The endpoint does not block; it returns whatever is available immediately.

### 8.2 Long-poll subscribe

`GET /v0/experiments/{E}/events/subscribe` binds the chapter 8 §2.1 `subscribe` operation as an HTTP long-poll. It accepts the same `cursor` query parameter. The server holds the connection open until one or more events are available after `cursor`, then returns them. If no event arrives within a server-chosen timeout (RECOMMENDED: 30 seconds), the server returns an empty batch so the caller can reconnect. The response body shape is identical to §8.1.

Subscribers iterate this endpoint in a loop, advancing `cursor` by the length of each returned batch. The at-least-once and total- order guarantees from [`05-event-protocol.md`](05-event-protocol.md) §4 are preserved because the underlying `read_range` semantics preserve them.

Push transports (Server-Sent Events, WebSocket) are OPTIONAL alternatives a deployment MAY expose alongside long-poll. They do not change the normative `subscribe` contract.

## 9. Error envelope

Every non-2xx response MUST use media type `application/problem+json; charset=utf-8` with a body matching [`schemas/wire/error.schema.json`](schemas/wire/error.schema.json):

```json
{
  "type":     "eden://error/<name>",
  "title":    "<short human phrase>",
  "status":   <HTTP status>,
  "detail":   "<longer human-readable message>",
  "instance": "<request URL>"
}
```

The `type` URI is the authoritative machine-readable error code. Clients MUST key retry and recovery on `type`, not on HTTP status alone (several conflict-class errors share 409). The closed v0 vocabulary is:

| `type` | HTTP | When |
|---|---|---|
| `eden://error/bad-request` | 400 | request body fails schema validation |
| `eden://error/experiment-id-mismatch` | 400 | header-vs-path experiment-ID disagreement (§1.3) |
| `eden://error/unauthorized` | 401 | authentication failed (missing / malformed / invalid bearer; §13) |
| `eden://error/forbidden` | 403 | authentication succeeded but the principal is not authorized for this endpoint (e.g., a worker bearer on an admin-gated route; §13.3) |
| `eden://error/worker-not-registered` | 403 | the authenticated `worker_id` is not registered for the experiment ([`04-task-protocol.md`](04-task-protocol.md) §3.5 step 2, §11) |
| `eden://error/worker-not-eligible` | 403 | the authenticated worker is registered but does not satisfy `Task.target` ([`04-task-protocol.md`](04-task-protocol.md) §3.5 step 3, §11) |
| `eden://error/wrong-claimant` | 403 | submit's authenticated `worker_id` does not match `task.claim.worker_id` ([`04-task-protocol.md`](04-task-protocol.md) §4.1, §11) |
| `eden://error/not-found` | 404 | referenced entity does not exist |
| `eden://error/already-exists` | 409 | insert collided with existing id |
| `eden://error/illegal-transition` | 409 | state-machine violation |
| `eden://error/not-claimed` | 409 | submit against a task whose claim has been cleared ([`04-task-protocol.md`](04-task-protocol.md) §4.1, §11) |
| `eden://error/conflicting-resubmission` | 409 | resubmit disagreed with committed payload ([`04-task-protocol.md`](04-task-protocol.md) §4.2) |
| `eden://error/invalid-precondition` | 409 | referenced entity not in required state; also the different-SHA branch of `integrate_variant` (§5) |
| `eden://error/no-op-variant` | 409 | execution-task submission whose variant tree is identical to every parent's tree; emitted by IUTs that exercise the SHOULD-level wire-side detection from [`04-task-protocol.md`](04-task-protocol.md) §4.2 (only the type is normative when emitted; emission itself is SHOULD per the chapter 04 rule). See [`03-roles.md`](03-roles.md) §3.3, §3.4. |
| `eden://error/reserved-identifier` | 409 | `register_worker` / `register_group` rejected a reserved **name** — a worker name in `admin` / `system` / `internal` ([`02-data-model.md`](02-data-model.md) §6.1) or a group name in `admins` / `orchestrators` already minted at setup ([`02-data-model.md`](02-data-model.md) §7.5) |
| `eden://error/invalid-name` | 422 | `register_worker` / `register_group` was given a `name` violating the display-name grammar ([`02-data-model.md`](02-data-model.md) §1.7) |
| `eden://error/cycle-detected` | 409 | a group mutation would introduce a cycle ([`02-data-model.md`](02-data-model.md) §7.3) |
| `eden://error/checkpoint-invalid` | 400 | uploaded checkpoint archive is malformed; fails JSONL ↔ bundle cross-reference validation per [`10-checkpoints.md`](10-checkpoints.md) §12, or carries a missing `artifacts/sha256/<hex>` referenced by JSONL data per [`10-checkpoints.md`](10-checkpoints.md) §7 |
| `eden://error/experiment-id-conflict` | 409 | portable-checkpoint import's `as_experiment_id` override collides with an existing `experiment_id` (without an override the receiver mints a fresh `exp_*`, so identity collision is impossible; §14.2, [`10-checkpoints.md`](10-checkpoints.md) §11) |
| `eden://error/spec-version-mismatch` | 409 | the checkpoint manifest's `spec_version` does not match the importer's spec version ([`10-checkpoints.md`](10-checkpoints.md) §13) |
| `eden://error/unsupported-checkpoint-version` | 409 | the checkpoint manifest's `checkpoint_format_version` is not recognized by the importer ([`10-checkpoints.md`](10-checkpoints.md) §13) |
| `eden://error/checkpoint-in-progress` | 409 | OPTIONAL — an exporter that rejects concurrent state-mutating operations during a live export ([`10-checkpoints.md`](10-checkpoints.md) §6) MAY use this code; servers that serialize instead MUST NOT emit it |
| `eden://error/lease-held-by-other` | 409 | `acquire_lease` against an experiment whose lease is still active ([`11-control-plane.md`](11-control-plane.md) §4.5) |
| `eden://error/lease-not-held` | 410 | `renew_lease` / `release_lease` against a `lease_id` that has been replaced by a fresh acquisition ([`11-control-plane.md`](11-control-plane.md) §4.5) |
| `eden://error/lease-expired` | 410 | `renew_lease` against a lease whose `expires_at < now` and which has not yet been replaced — distinct from `lease-not-held` because the lease still nominally belongs to the caller ([`11-control-plane.md`](11-control-plane.md) §4.5) |
| `eden://error/lease-instance-mismatch` | 409 | `renew_lease` / `release_lease` whose body `holder_instance` does not match the lease's stored `holder_instance` ([`11-control-plane.md`](11-control-plane.md) §4.7) |

A server MUST NOT emit a `type` outside this vocabulary for a v0 endpoint. Implementations MAY add custom error types at non-`/v0/` paths (e.g. `/_reference/...`) using the same envelope. The pre-12a-1 `eden://error/wrong-token` is **removed**: per-claim tokens no longer exist.

## 10. Idempotency and retry

### 10.1 Submit

The [`04-task-protocol.md`](04-task-protocol.md) §4.2 submit-idempotency rule is preserved by the binding: a retry of `POST /v0/experiments/{E}/tasks/{T}/submit` by the same authenticated worker, carrying a content-equivalent payload, MUST return 200 identically to the original call. This lets a worker retry a submit after a transport error without risk of double-advance. Bindings MAY accept an optional caller-supplied `submission_id` field on the wire payload as an explicit idempotency key; the content-equivalence rule is the protocol-level backstop. No HTTP-level `Idempotency-Key` header is required.

### 10.2 Integrate

Same-value idempotency on `integrate_variant` is pinned in §5.

### 10.3 Other mutations

Claim, reject, reclaim, and accept are **not** blindly retry-safe on transport failures. A retry after the server already committed the transition will return `illegal-transition`, but that error is not uniquely distinguishable from a racing actor or a reclaim that interleaved between attempts. The binding does **not** standardize an HTTP-level retry convention for these endpoints; a client MUST resolve ambiguous failures via a follow-up `GET /v0/experiments/{E}/tasks/{T}` read and apply operation-specific knowledge. A future spec lineage MAY introduce per-operation idempotency tokens.

## 11. Reference-only helpers (non-normative)

The reference `eden_wire` server also exposes:

- `GET /_reference/experiments/{E}/tasks/{T}/validate-terminal`
- `POST /_reference/experiments/{E}/validate/evaluation`

These are conveniences for the Phase-5 dispatch driver and are **not** part of the normative binding. A conforming third-party client MUST NOT rely on them being present. A conforming third-party orchestrator implementing its own accept/reject decision inline is free to do so; the [`04-task-protocol.md`](04-task-protocol.md) §4.3 decision rules are all that matter for the state machine.

Future phases MAY integrate individual helpers into the normative binding if conformance testing requires it; such integrations follow the same versioning discipline as any other binding change.

## 12. Conformance

A conforming server:

- Implements §§2–8 with the normative semantics of [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), [`06-integrator.md`](06-integrator.md) §3.4, and [`08-storage.md`](08-storage.md).
- Exposes `/v0/` with the exact URL shapes listed above.
- Returns problem+json with `type` from the §9 vocabulary on every non-2xx response.
- Preserves the task-protocol, event-protocol, and integrator-atomicity invariants unchanged by the HTTP transport.
- Implements the §13 authentication scheme: per-worker bearer + admin bearer, with the authenticated principal forwarded to the Store as `worker_id` (or as the admin sentinel for admin-gated operations).

A conforming client:

- Keys retry and recovery on the `type` URI, not on HTTP status alone.
- Resolves transport-indeterminate `integrate_variant` failures via the read-back procedure in §5 and does not compensate ref writes without positive confirmation of server-side non-commit.
- Does not depend on `/_reference/` endpoints.
- Persists per-worker credentials locally; treats credential rotation as a §6.3 operation, not as a re-registration.

## 13. Authentication

Authentication is normative in v0. Every request to a `/v0/` endpoint MUST carry an `Authorization: Bearer <credential>` header; the server MUST reject requests without one (or with a malformed one) using `eden://error/unauthorized` (HTTP 401).

### 13.1 Bearer format

A bearer is the concatenation `<principal>:<secret>`, parsed by splitting on the first `:`. There are two principal namespaces:

- **`admin`** — the deployment's admin principal. The secret half is a deployment-wide admin token (recorded in the deployment's environment as `EDEN_ADMIN_TOKEN`). Rotating this token invalidates all in-flight admin sessions but does NOT invalidate per-worker credentials.
- **`<worker_id>`** — a registered worker's opaque, system-minted id (matching the `wkr_*` grammar of [`02-data-model.md`](02-data-model.md) §1.6). The secret half is the `registration_token` issued by `register_worker` (§6.1) or `reissue_credential` (§6.3). The server MUST verify the secret against the stored argon2id hash for `<worker_id>` and MUST reject mismatches with `eden://error/unauthorized`.

### 13.2 Worker-id grammar disjointness

The opaque worker-id grammar ([`02-data-model.md`](02-data-model.md) §1.6) excludes `:` so that the bearer parser can safely split on the first colon. The literal `admin` is the deployment-admin principal and is a reserved worker **name** ([`02-data-model.md`](02-data-model.md) §6.1) for which no `worker_id` is ever minted; since every minted `worker_id` carries the `wkr_` prefix and can never equal the literal `admin`, a bearer's principal half unambiguously names either the admin principal or a worker.

### 13.3 Authorization

Every `/v0/` endpoint MUST be classified as **admin-gated**, **worker-gated**, or **either**. Worker-gated endpoints MAY additionally be **group-gated** by membership in a [`02-data-model.md`](02-data-model.md) §7.5 reserved group. The classification (principal class + optional group-gate) appears in the `Auth` column of the per-section endpoint tables (§§2–4, §6, §7, §8) and in the per-endpoint authority paragraphs. The wire dispatcher:

- Accepts the request only if the authenticated principal class matches the endpoint class. `either` admits both classes; `worker` rejects admin bearers; `admin` rejects worker bearers.
- For group-gated worker endpoints, additionally checks that the authenticated `worker_id` is a transitive member of the named group via `Store.resolve_worker_in_group` ([`02-data-model.md`](02-data-model.md) §7.2). The membership check is atomic with the rest of the request handler.
- Returns `eden://error/forbidden` (HTTP 403) for principal-class mismatches and for group-membership failures.
- Forwards the authenticated `worker_id` to the Store on `claim` / `submit` so that §3 / §4 enforcement runs against the verified identity, not against a request-body field.

Summary of the v0 classifications:

- **admin-gated** — registry mutations: `register_worker`, `reissue_credential`, `register_group`, `add_to_group` / `remove_from_group` / `delete_group`. Plus the §14 portable-checkpoint mutation endpoints (`export_checkpoint`, `import_checkpoint`); see §14 for the bootstrap-class rationale. The §14 `read_experiment` endpoint is either-auth (not admin-gated) so the orchestrator's per-iteration policy view can read `created_at` over its worker bearer; see §14.3 for the rationale.
- **worker-gated, no group-gate** — `claim`, `submit`, `reclaim`, `create_idea`, `mark_idea_ready`, `create_variant`, `declare_variant_evaluation_error`, and the `whoami` probe (`verify_worker_credential`).
- **worker-gated, `orchestrators` group required** — `accept`, `reject`, `integrate_variant`, and `create_task(kind="execution")` per [`03-roles.md`](03-roles.md) §6.
- **worker-gated, `admins` group required** — `reassign_task`, `update_dispatch_mode`, `create_task(kind="ideation")`, `create_task(kind="evaluation")` ([`02-data-model.md`](02-data-model.md) §7.5; [`04-task-protocol.md`](04-task-protocol.md) §6.2, §7.2). Note that `create_task(kind="ideation")` and `create_task(kind="evaluation")` admit `orchestrators` in addition to `admins` per the per-kind authority in §2.1.
- **either** — every read endpoint: `list_tasks` / `read_task` / `read_submission`, `list_ideas` / `read_idea`, `list_variants` / `read_variant`, `list_workers` / `read_worker`, `list_groups` / `read_group`, `read_range` / `subscribe`.

The §15 control-plane endpoints follow the same classification framework with their own per-endpoint `Auth` column. The deployment-scoped `orchestrators` group used by §15.2 lease ops is a distinct registry from any per-experiment `orchestrators` group — per [`11-control-plane.md`](11-control-plane.md) §6 — so membership in one does NOT imply membership in the other.

The [`04-task-protocol.md`](04-task-protocol.md) §3.3 requirement that the binding verify the credential before invoking the Store is satisfied by this dispatcher. The group-gate adds a [`03-roles.md`](03-roles.md) §6 + [`02-data-model.md`](02-data-model.md) §7.5 layer on top: a worker may be authenticated but still 403 on an `accept` if it is not a member of `orchestrators`.

### 13.4 Credential lifecycle

- Issued by `register_worker` (§6.1), exactly once per registration.
- Rotated by `reissue_credential` (§6.3); the prior credential is invalidated atomically with the rotation.
- Revoked implicitly when an admin deletes the worker (out-of-scope for v0).
- Persisted by the worker locally; the binding does not provide any "fetch my credential" operation.

### 13.5 Token storage hygiene

Bearer tokens are sensitive: any logging that captures HTTP request headers MUST redact the `Authorization` value. A conforming server's structured request logger MUST NOT emit the bearer in plaintext; deployments are responsible for proxy / sidecar log discipline.

## 14. Checkpoint operations

These endpoints bind the portable-checkpoint operations defined in [`10-checkpoints.md`](10-checkpoints.md) and the [`08-storage.md`](08-storage.md) §1.9 Store ops. Implementations that claim the v1+checkpoints conformance level ([`09-conformance.md`](09-conformance.md) §4) MUST expose them; other implementations MAY omit them. The **export and import** endpoints are **admin-gated** in the §13.3 sense — caller MUST present the deployment-admin bearer (literal `admin` principal). Unlike the group-gated operational endpoints (`reassign_task` / `update_dispatch_mode` / `terminate_experiment`), checkpoint export/import operations belong to the **bootstrap class**: a fresh receiving deployment cannot have an `admins`-group member registered before its first `import_checkpoint` (the import is what populates the worker / group registries). Gating import on the literal admin principal sidesteps that chicken-and-egg; gating export the same way keeps the mutation surface uniformly bootstrap-class. The `read_experiment` endpoint is **either-auth** because the orchestrator role's per-iteration policy evaluation reads `created_at` from this surface (`ExperimentStateView.experiment_created_at` in the reference dispatch driver); restricting it to the admin principal would block the orchestrator's worker bearer from ever resolving the policy view. The recovery-probe contract in [`10-checkpoints.md`](10-checkpoints.md) §10 also flows through this endpoint and is operator-driven (admin bearer) — both principal classes legitimately need read access, so the endpoint is either-auth and matches the §2.9 `GET /state` shape.

| Operation | HTTP | Path | Auth |
|---|---|---|---|
| `export_checkpoint` | `POST` | `/v0/experiments/{E}/checkpoint` | admin |
| `import_checkpoint` | `POST` | `/v0/checkpoints/import` | admin |
| `read_experiment` | `GET` | `/v0/experiments/{E}` | either |

The export and import endpoints are bootstrap-class — see the §14 introduction above. `read_experiment` is **either-auth** (any registered worker MAY read), parallel to the §2.9 `GET /state` companion read: the full experiment object's `created_at` and `imported_from` fields are needed by the orchestrator's per-iteration policy evaluation (`ExperimentStateView.experiment_created_at`) and by post-import recovery probes alike, neither of which can authenticate as the deployment-admin principal. The recovery-probe contract in [`10-checkpoints.md`](10-checkpoints.md) §10 is operator-driven and the operator's bearer (typically the admin token) is one valid principal for this either-auth surface; the orchestrator-as-worker path is the other.

The `import_checkpoint` endpoint is the only normative v0 path outside `/v0/experiments/{E}/...`; it is carved out of the §1.3 experiment-scoping rule (see §1.3 paragraph 2). The `X-Eden-Experiment-Id` header is OPTIONAL on `/v0/checkpoints/...` paths.

### 14.1 Export checkpoint

`POST /v0/experiments/{E}/checkpoint` accepts an empty request body. On success the server returns `200 OK` with `Content-Type: application/x-eden-checkpoint+tar` and a body containing the portable-checkpoint archive per [`10-checkpoints.md`](10-checkpoints.md) §§3-4. The server MAY use `Transfer-Encoding: chunked` or buffer the response with `Content-Length`; clients MUST handle both.

Optional query parameters:

- `format_version=<n>` — the desired `checkpoint_format_version` ([`10-checkpoints.md`](10-checkpoints.md) §5). Defaults to the highest version the server supports. An unrecognized value returns 400 `eden://error/bad-request`.

The export is atomic per [`10-checkpoints.md`](10-checkpoints.md) §6. A server that rejects concurrent state-mutating operations during a live export MAY return 409 `eden://error/checkpoint-in-progress` on those mutating operations; servers that serialize them after the export instead MUST NOT emit that code.

**Authority:** caller MUST present the deployment-admin bearer (literal `admin` principal per §13.1). A worker bearer receives 403 `eden://error/forbidden`. See the §14 introduction for the bootstrap-class rationale.

### 14.2 Import checkpoint

`POST /v0/checkpoints/import` accepts a request body with `Content-Type: application/x-eden-checkpoint+tar` carrying a portable-checkpoint archive ([`10-checkpoints.md`](10-checkpoints.md) §§3-4). The server MAY accept chunked or `Content-Length`-buffered uploads; bindings MUST handle both. On success the server returns `201 Created` with a JSON body:

```json
{
  "experiment_id": "<id>",
  "warnings": ["<warning string>", ...]
}
```

- `experiment_id` is the imported experiment's resulting id — the `as_experiment_id` override if one was supplied, otherwise a **freshly-minted** opaque `exp_*` id ([`02-data-model.md`](02-data-model.md) §1.6) the receiver allocates for this import. The manifest's source id is NOT reused as the primary key; it is preserved for provenance in `imported_from.source_experiment_id` (§14.3).
- `warnings` is an array of free-form strings the importer surfaces to the operator. Typical entries: the path of a credentials sidecar file ([`10-checkpoints.md`](10-checkpoints.md) §8 step 4), an indication that one or more refs were ignored because no protocol-owned object referenced them ([`10-checkpoints.md`](10-checkpoints.md) §12 final paragraph).

Optional query parameters:

- `as_experiment_id=<exp_*>` — override the receiver-minted experiment id with a caller-supplied opaque `exp_*` ([`02-data-model.md`](02-data-model.md) §1.6). When supplied, the imported experiment is created with this id throughout (the manifest's source id is replaced everywhere in the stored data) and `imported_from.source_experiment_id` still records the source. When the supplied override collides with an existing experiment, the server returns 409 `eden://error/experiment-id-conflict`. **When absent**, the receiver mints a fresh `exp_*` (no collision is possible by construction, so the import does not 409 on identity); this is a normative behavior change from the pre-rename "reuse the manifest's id unless overridden" rule (see [`10-checkpoints.md`](10-checkpoints.md) §10).

The import is a single composite commit per [`08-storage.md`](08-storage.md) §6 and [`10-checkpoints.md`](10-checkpoints.md) §7. Validation failures (cross-reference checks per [`10-checkpoints.md`](10-checkpoints.md) §12, missing artifact files, malformed JSONL) MUST cause the entire commit to roll back with 400 `eden://error/checkpoint-invalid`. Version-mismatch failures return 409 (`eden://error/spec-version-mismatch`, `eden://error/unsupported-checkpoint-version`). A colliding `as_experiment_id` override returns 409 `eden://error/experiment-id-conflict`.

If the manifest carries `requires_credential_reissue: true` ([`10-checkpoints.md`](10-checkpoints.md) §5), the importer MUST mint fresh credentials for every imported worker as part of the same composite commit. The new credentials are surfaced to the operator via the `warnings` array; the side-channel format is implementation-defined.

**Header carve-out (§1.3).** The `X-Eden-Experiment-Id` header is OPTIONAL on this endpoint. When present, its value MUST equal the imported experiment's resulting id (the manifest's `experiment_id` after any `as_experiment_id` rewrite). A mismatch returns 400 `eden://error/experiment-id-mismatch`. When absent the server proceeds without that check.

**Authority:** caller MUST present the deployment-admin bearer (literal `admin` principal per §13.1). A worker bearer receives 403 `eden://error/forbidden`. See the §14 introduction for the bootstrap-class rationale (the receiving deployment is typically empty when the import lands; an `admins`-group gate would be uncallable).

### 14.3 Read experiment

`GET /v0/experiments/{E}` returns the full experiment runtime object ([`08-storage.md`](08-storage.md) §1.9), shape per [`schemas/experiment.schema.json`](schemas/experiment.schema.json):

```json
{
  "experiment_id": "<exp_*>",
  "name": "<display name>" | null,
  "state": "running" | "terminated",
  "created_at": "<RFC 3339 timestamp>",
  "base_commit_sha": "<commit SHA>",
  "imported_from": null | {"checkpoint_exported_at": "<timestamp>", "checkpoint_format_version": "<string>", "source_experiment_id": "<exp_*>" | null}
}
```

`experiment_id` is the opaque, system-minted `exp_*` id ([`02-data-model.md`](02-data-model.md) §1.6); `name` is the experiment's optional operator-supplied display label ([`02-data-model.md`](02-data-model.md) §1.7). `imported_from` is `null` on natively-created experiments and an object on imported experiments ([`02-data-model.md`](02-data-model.md) §2.5, [`10-checkpoints.md`](10-checkpoints.md) §10); its `source_experiment_id` carries the export-side `exp_*` for provenance (the receiver mints its own primary-key `experiment_id`, §14.2). The endpoint is the recovery-probe surface for the import-response-lost case described in [`10-checkpoints.md`](10-checkpoints.md) §10.

`base_commit_sha` is the experiment seed commit ([`02-data-model.md`](02-data-model.md) §2.5), recorded at registration / repo-init time. The orchestrator reads it (over its worker bearer — see the §14 intro note that `read_experiment` is either-auth, not admin-gated) to create the seed baseline variant ([`02-data-model.md`](02-data-model.md) §9.4). It is omitted from the response when absent (an experiment registered before the field existed).

The companion `GET /v0/experiments/{E}/state` (§2.9) remains worker-accessible and returns only the state projection; this endpoint exposes the full object including `imported_from` and is admin-gated to avoid widening the recovery-probe surface.

**Authority:** **either-auth** — any registered worker MAY read, parallel to the §2.9 `GET /state` companion read. The recovery-probe flow (admin bearer after a dropped import 201) and the orchestrator's per-iteration policy view (worker bearer reading `created_at`) both legitimately need this surface; restricting it to one principal class would block the other. The `imported_from` field is informational provenance (it does not carry secret material), so the broader read surface does not widen attack surface beyond the existing `GET /state` posture. See the §14 introduction above.

## 15. Control-plane operations

These endpoints bind the chapter 11 control-plane operations. Implementations that claim the `v1+multi-experiment` conformance level ([`09-conformance.md`](09-conformance.md) §4) MUST expose them; other implementations MAY omit them.

The control-plane endpoint root is `/v0/control/`. Per §1.3, paths under this root are NOT experiment-scoped at the URL level: the experiment id (when relevant) appears in the path segment for per-experiment registry / lease operations, NOT in `/v0/experiments/{experiment_id}/...`. The `X-Eden-Experiment-Id` header is OPTIONAL on this path family and has no protocol meaning at this surface when present.

The control plane MAY be deployed as a separate HTTP service (the reference impl's choice) or co-hosted with the task-store-server at the same base URL. A conforming control-plane implementation exposes the full `/v0/control/` surface; partial coverage is non-conforming.

| Operation | HTTP | Path | Auth |
|---|---|---|---|
| `register_experiment` | `POST` | `/v0/control/experiments` | admin |
| `unregister_experiment` | `DELETE` | `/v0/control/experiments/{E}` | admin |
| `list_experiments` | `GET` | `/v0/control/experiments` | either |
| `read_experiment_metadata` | `GET` | `/v0/control/experiments/{E}` | either |
| `acquire_lease` | `POST` | `/v0/control/experiments/{E}/leases` | worker (group-gated: `orchestrators`) |
| `renew_lease` | `POST` | `/v0/control/leases/{L}/renew` | worker (group-gated: `orchestrators`) |
| `release_lease` | `POST` | `/v0/control/leases/{L}/release` | worker (group-gated: `orchestrators`) |
| `list_active_leases` | `GET` | `/v0/control/leases` | either (with `?holder=<id>` filter) |
| `register_control_worker` | `POST` | `/v0/control/workers` | admin |
| `reissue_control_credential` | `POST` | `/v0/control/workers/{W}/reissue-credential` | admin |
| `list_control_workers` | `GET` | `/v0/control/workers` | admin |
| `read_control_worker` | `GET` | `/v0/control/workers/{W}` | admin |
| `register_control_group` | `POST` | `/v0/control/groups` | admin |
| `add_to_control_group` | `POST` | `/v0/control/groups/{G}/members` | admin |
| `remove_from_control_group` | `DELETE` | `/v0/control/groups/{G}/members/{W}` | admin |
| `list_control_groups` | `GET` | `/v0/control/groups` | admin |
| `read_control_group` | `GET` | `/v0/control/groups/{G}` | admin |
| `delete_control_group` | `DELETE` | `/v0/control/groups/{G}` | admin |
| `verify_control_credential` | `GET` | `/v0/control/whoami` | worker |

The deployment-scoped registry endpoints (`register_control_worker` and below) mirror the per-experiment §6 / §7 shapes; their grammar, idempotency, error vocabulary, and authorization rules are normative in [`11-control-plane.md`](11-control-plane.md) §6 by reference to chapter 02 §6 / §7 and §6 / §7 of this chapter. This chapter pins only the URL shapes and the authority column.

### 15.1 Experiment registry

`POST /v0/control/experiments` body:

```json
{
  "name": "<display name>" | null,
  "config_uri": "<uri>"
}
```

The caller does **not** supply an `experiment_id`; the control plane **mints** an opaque `exp_*` id ([`02-data-model.md`](02-data-model.md) §1.6) for the experiment. The optional `name` is the operator-supplied display label ([`02-data-model.md`](02-data-model.md) §1.7); an ill-formed name returns 422 `eden://error/invalid-name`. Returns 201 with the new registry entry (carrying the minted `experiment_id` and `name?`) on every successful create; because the id is system-minted, there is no idempotent re-registration by id. Per [`11-control-plane.md`](11-control-plane.md) §2.2.

`DELETE /v0/control/experiments/{E}` (the `{E}` segment is the opaque `exp_*`) returns 204 on success; 409 `eden://error/invalid-precondition` when `last_known_state != "terminated"` OR an active lease exists. Per [`11-control-plane.md`](11-control-plane.md) §2.2.

`GET /v0/control/experiments` returns 200 with `{"experiments": [<entry>, ...]}`. It accepts an OPTIONAL `?name=<n>` query parameter (exact-match, case-sensitive, 0..N results) so cross-experiment admin views can resolve a display name to its opaque id without bespoke task-store calls. `GET /v0/control/experiments/{E}` returns 200 with one entry; 404 `eden://error/not-found` when the experiment id is not registered. Either response MAY carry a `warnings` array surfacing state-sync degradation per [`11-control-plane.md`](11-control-plane.md) §3.4.

### 15.2 Lease operations

`POST /v0/control/experiments/{E}/leases` body:

```json
{
  "holder": "<wkr_*>",
  "holder_instance": "<uuid>"
}
```

`holder` is the opaque, system-minted `wkr_*` id ([`02-data-model.md`](02-data-model.md) §1.6) of the deployment-scoped worker acquiring the lease. Caller MUST be authenticated as `holder` (no impersonation) AND be a member of the reserved-name deployment-scoped `orchestrators` group (resolved by name to its `grp_*` id). Returns 201 with the new lease on success; 409 `eden://error/lease-held-by-other` when an active lease exists; 404 `eden://error/not-found` when the experiment is not registered. Per [`11-control-plane.md`](11-control-plane.md) §4.5.

`POST /v0/control/leases/{L}/renew` and `POST /v0/control/leases/{L}/release` body:

```json
{
  "holder_instance": "<uuid>"
}
```

Caller MUST be the lease's current `holder`. `renew_lease` returns 200 with the renewed lease (including the new `expires_at`); 410 `eden://error/lease-not-held` if the lease has been replaced; 410 `eden://error/lease-expired` if the lease has lapsed but not yet been replaced; 409 `eden://error/lease-instance-mismatch` on `holder_instance` mismatch. `release_lease` returns 200 on success (and idempotently on already-released lease); 409 `eden://error/lease-instance-mismatch` on mismatch. Per [`11-control-plane.md`](11-control-plane.md) §4.5, §4.7.

`GET /v0/control/leases?holder=<wkr_*>` returns 200 with `{"leases": [<lease>, ...]}` containing every active lease (`expires_at >= now`) whose `holder` equals the opaque `wkr_*` query parameter. The caller MUST be authenticated as that `<wkr_*>` OR be the admin principal. Used by the orchestrator's startup-fence probe per [`11-control-plane.md`](11-control-plane.md) §5.2. Per [`11-control-plane.md`](11-control-plane.md) §4.5.

### 15.3 Deployment-scoped registry

The `/v0/control/workers/...`, `/v0/control/groups/...`, and `/v0/control/whoami` endpoints mirror the chapter 02 §6 / §7 and §6 / §7 of this chapter shapes verbatim. The create bodies take an optional `name?` (and `labels?` / `members?`), the control plane **mints** the opaque `wkr_*` / `grp_*` id ([`02-data-model.md`](02-data-model.md) §1.6), reads / lists return the optional `name`, the `?name=<n>` lookup is available on the list routes, and the closed §9 error vocabulary (including 422 `eden://error/invalid-name` and 409 `eden://error/reserved-identifier` in name-space) is unchanged from the per-experiment shapes; the only differences are:

- The URL roots are `/v0/control/...`, not `/v0/experiments/{E}/...`.
- The registry is deployment-scoped (`worker_id` unique across the deployment, not within an experiment) and is **distinct from** the per-experiment registries hosted by the task-store-server. A deployment-scoped worker minted by the control plane is unrelated to any worker registered against a per-experiment task-store-server, even one carrying the same display `name`. The two credential domains are independent.
- The reserved worker / group **names** (`admin` / `system` / `internal`; `admins` / `orchestrators`) apply at the deployment scope; the reserved groups are minted at control-plane bootstrap with system-minted `grp_*` ids whose `name` equals the reserved literal. The per-experiment registries are independent.

`GET /v0/control/whoami` returns 200 with `{"worker_id": "<wkr_*>", "name": "<name>" | null}` for the authenticated worker, mirroring §6.4. Used by the orchestrator's startup credential verification.

## 16. Implementation latitude

The binding leaves to implementations:

- The concrete HTTP server (FastAPI, Starlette, bespoke); the reference impl uses FastAPI.
- The SSE/WebSocket push transport, if any.
- The RECOMMENDED long-poll timeout; the reference impl uses 30 seconds.
- The credential-hash function (the reference impl uses argon2id; bindings MAY adopt a different KDF as long as it is offline-comparison-resistant).
- Transport-level concerns outside the binding's scope (TLS, compression).

What the binding does **not** leave to implementations:

- The URL shapes (§§2–8).
- The problem+json error vocabulary (§9).
- The same-value idempotency on `integrate_variant` (§5).
- The §13 authentication scheme (per-worker bearer + admin bearer; verified before Store invocation).
- Preservation of the [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), [`06-integrator.md`](06-integrator.md) §3.4, and [`08-storage.md`](08-storage.md) invariants through the HTTP transport.
