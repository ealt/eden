# Wire Protocol — HTTP binding

This chapter specifies an **HTTP binding** for the behaviors [chapter 4](04-task-protocol.md) (task state machine), [chapter 5](05-event-protocol.md) (event log), and [chapter 8](08-storage.md) (storage-side contract) already pin normatively. It does not re-specify those behaviors; every endpoint below maps 1:1 to a store-side operation named in those chapters and inherits that operation's semantics.

A conforming EDEN deployment is **not required** to use this HTTP binding. The protocol constrains observable behavior; the transport that carries it is a deployment choice. This chapter exists so that HTTP-based deployments — including the Phase-8a reference deployment — can interoperate byte-for-byte.

The binding intentionally omits the reference orchestrator's decision helpers (`validate_terminal`, `validate_metrics`). Those are reference-impl conveniences, not chapter-4/5/8 operations. A conforming third-party orchestrator is free to implement their decision logic inline; see §9.

## 1. Transport

### 1.1 HTTP version and content type

A conforming server MUST accept HTTP/1.1 over TCP on a deployment-chosen host and port. HTTP/2 is OPTIONAL. Requests and responses that carry a body MUST use media type `application/json; charset=utf-8`, except for error responses which use `application/problem+json; charset=utf-8` (§6). Empty responses (e.g. 204) MUST NOT carry a body.

### 1.2 Path versioning

All normative paths are rooted at `/v0/`. Paths under `/_reference/` are explicitly non-normative and carry reference-implementation-specific behavior; a conforming client MUST NOT rely on them being present or on their semantics.

Breaking changes to the binding produce a new root (`/v1/`), the same way breaking changes to the spec produce a new directory (`spec/v1/`). Non-breaking additions (new endpoints, new optional fields in existing payloads) stay under `/v0/`.

### 1.3 Experiment scoping

Every normative path segment below `/v0/` begins with `experiments/{experiment_id}/`, and every request MUST additionally send the header `X-Eden-Experiment-Id: {experiment_id}` with a value equal to the path segment. A server MUST reject a request whose header disagrees with its path segment with `eden://error/experiment-id-mismatch` (HTTP 400); see §6. The header is defense-in-depth against misrouted clients and proxies.

## 2. Task operations (chapter 4)

The following endpoints bind the task-store operations in [`04-task-protocol.md`](04-task-protocol.md) §§1–5 and [`08-storage.md`](08-storage.md) §1.1. Each endpoint inherits the normative semantics of the underlying operation; this section lists only the wire shape.

| Operation | HTTP | Path |
|---|---|---|
| `create_task` | `POST` | `/v0/experiments/{E}/tasks` |
| `list_tasks` | `GET`  | `/v0/experiments/{E}/tasks` |
| `read_task` | `GET`  | `/v0/experiments/{E}/tasks/{T}` |
| `read_submission` | `GET`  | `/v0/experiments/{E}/tasks/{T}/submission` |
| `claim` | `POST` | `/v0/experiments/{E}/tasks/{T}/claim` |
| `submit` | `POST` | `/v0/experiments/{E}/tasks/{T}/submit` |
| `accept` | `POST` | `/v0/experiments/{E}/tasks/{T}/accept` |
| `reject` | `POST` | `/v0/experiments/{E}/tasks/{T}/reject` |
| `reclaim` | `POST` | `/v0/experiments/{E}/tasks/{T}/reclaim` |

### 2.1 Create

`POST /v0/experiments/{E}/tasks` accepts a JSON request body whose shape matches [`schemas/task.schema.json`](schemas/task.schema.json) with `state == "pending"` and no `claim`. On success the server returns 200 with the created task as the response body (same schema). The composite-commit rules in [`05-event-protocol.md`](05-event-protocol.md) §2.2 apply: creating an `implement` task transitions the referenced proposal atomically with the task insert; the server MUST perform both effects in a single transaction.

### 2.2 List and read

- `GET /v0/experiments/{E}/tasks` returns an array of tasks matching optional query parameters `kind` (one of `plan`, `implement`, `evaluate`) and `state` (one of the five task states). Ordering is implementation-defined per [`08-storage.md`](08-storage.md) §1.1.
- `GET /v0/experiments/{E}/tasks/{T}` returns the task object with id `T`, or 404 `eden://error/not-found`.

### 2.3 Claim

`POST /v0/experiments/{E}/tasks/{T}/claim` accepts a JSON body with at minimum the field `worker_id` and an OPTIONAL `expires_at` timestamp. On success the server returns 200 with a `claim` object matching the shape from [`02-data-model.md`](02-data-model.md) §3.4 (see also [`schemas/task.schema.json`](schemas/task.schema.json)) — notably including the fresh `token`. On a non-`pending` task the server returns 409 `eden://error/illegal-transition`. The claim-token secrecy rule ([`04-task-protocol.md`](04-task-protocol.md) §3.2) implies that intermediary proxies and logs MUST NOT persist claim tokens in plaintext; that is a deployment concern and the binding does not enforce it.

### 2.4 Submit

`POST /v0/experiments/{E}/tasks/{T}/submit` accepts a JSON body containing the current `token` and a role-specific `payload` (matching the claim's task `kind`). Atomicity, idempotency, and content-equivalence rules from [`04-task-protocol.md`](04-task-protocol.md) §4 apply unchanged. A resubmit with a content-equivalent payload MUST return 200; a resubmit with a divergent payload MUST return 409 `eden://error/conflicting-resubmission`. A wrong token MUST return 403 `eden://error/wrong-token`.

### 2.5 Accept and reject

- `POST /v0/experiments/{E}/tasks/{T}/accept` transitions `submitted → completed` per [`04-task-protocol.md`](04-task-protocol.md) §4.3. The request body is empty (the orchestrator has already persisted the submission; the server re-reads it to apply the composite-commit effects).
- `POST /v0/experiments/{E}/tasks/{T}/reject` transitions `submitted → failed` per [`04-task-protocol.md`](04-task-protocol.md) §4.3. The request body carries a single field `reason` drawn from the closed v0 vocabulary ([`05-event-protocol.md`](05-event-protocol.md) §3.1): `"worker_error"`, `"validation_error"`, or `"policy_limit"`.

### 2.6 Reclaim

`POST /v0/experiments/{E}/tasks/{T}/reclaim` transitions a `claimed` or (operator-invoked) `submitted` task back to `pending` per [`04-task-protocol.md`](04-task-protocol.md) §5. The request body carries a single field `cause` drawn from the closed v0 vocabulary: `"expired"`, `"operator"`, or `"health_policy"`.

## 3. Proposal operations

| Operation | HTTP | Path |
|---|---|---|
| `create_proposal` | `POST` | `/v0/experiments/{E}/proposals` |
| `list_proposals` | `GET` | `/v0/experiments/{E}/proposals` |
| `read_proposal` | `GET` | `/v0/experiments/{E}/proposals/{P}` |
| `mark_proposal_ready` | `POST` | `/v0/experiments/{E}/proposals/{P}/mark-ready` |

Request and response bodies match [`schemas/proposal.schema.json`](schemas/proposal.schema.json). `list_proposals` accepts an optional `state` query parameter. `mark-ready` has an empty request body.

## 4. Trial operations

| Operation | HTTP | Path |
|---|---|---|
| `create_trial` | `POST` | `/v0/experiments/{E}/trials` |
| `list_trials` | `GET` | `/v0/experiments/{E}/trials` |
| `read_trial` | `GET` | `/v0/experiments/{E}/trials/{T}` |
| `declare_trial_eval_error` | `POST` | `/v0/experiments/{E}/trials/{T}/declare-eval-error` |
| `integrate_trial` | `POST` | `/v0/experiments/{E}/trials/{T}/integrate` |

`integrate_trial` binds [`06-integrator.md`](06-integrator.md) §3.4 and carries additional idempotency rules (§5 below); the other endpoints are transport-only bindings of their [`08-storage.md`](08-storage.md) §1.7 operations.

## 5. `integrate_trial` — same-value idempotency

The `POST /v0/experiments/{E}/trials/{T}/integrate` endpoint has additional semantics beyond transport-binding because [`06-integrator.md`](06-integrator.md) §3.4's atomicity invariant spans the process boundary. The request body carries the single field `trial_commit_sha`.

- **Success (first call).** The server atomically writes `trial_commit_sha` on the trial and appends `trial.integrated` per [`06-integrator.md`](06-integrator.md) §3.4. Response: 200, empty body.
- **Same-value idempotency.** A repeated call whose `trial_commit_sha` equals the value already stored on the trial MUST return 200 and MUST NOT append a second `trial.integrated` event. This is what lets a client safely retry an `integrate_trial` request after a transport- indeterminate failure without double-commit.
- **Different-value rejection.** A call whose `trial_commit_sha` differs from the value already stored MUST return 409 `eden://error/invalid-precondition`. A conforming client MUST surface this as an atomicity violation (the chapter 6 §1.2 sole-writer rule has been violated somewhere upstream); operator intervention is required.
- **Other preconditions.** Standard [`06-integrator.md`](06-integrator.md) §3.4 preconditions continue to apply: the trial MUST be in `status == "success"`, and the `trial_commit_sha` MUST be a well-formed commit SHA; violations return `invalid-precondition`.

A client that issues `integrate_trial` and does not receive a 2xx response (connection reset, read timeout, proxy disconnect) MUST treat the outcome as indeterminate and MUST NOT assume the server has not committed. The reference reconciliation procedure is a read-back (`GET /v0/experiments/{E}/trials/{T}`) that resolves to one of three outcomes:

- observed `trial_commit_sha` equals the sent value → treat as success;
- observed `trial_commit_sha` is set but differs → raise the same atomicity-violation error the server would have;
- observed `trial_commit_sha` is absent, or the read-back itself fails → raise an indeterminate-result error. Absence does **not** prove the request failed; it may still be in flight.

## 6. Event log

| Operation | HTTP | Path |
|---|---|---|
| `read_range` / `replay` | `GET` | `/v0/experiments/{E}/events` |
| `subscribe` | `GET` | `/v0/experiments/{E}/events/subscribe` |

### 6.1 Read-range and replay

`GET /v0/experiments/{E}/events` returns the log in order. It accepts one query parameter:

- `cursor` — cumulative count of events the caller has already observed. Omitted or `0` triggers `replay` per [`05-event-protocol.md`](05-event-protocol.md) §4.4.

The response is `{"events": [...], "cursor": N}` where `events` is the delivered batch (possibly empty) and `cursor` is the new cumulative count the caller should pass to the next call. The endpoint does not block; it returns whatever is available immediately.

### 6.2 Long-poll subscribe

`GET /v0/experiments/{E}/events/subscribe` binds the chapter 8 §2.1 `subscribe` operation as an HTTP long-poll. It accepts the same `cursor` query parameter. The server holds the connection open until one or more events are available after `cursor`, then returns them. If no event arrives within a server-chosen timeout (RECOMMENDED: 30 seconds), the server returns an empty batch so the caller can reconnect. The response body shape is identical to §6.1.

Subscribers iterate this endpoint in a loop, advancing `cursor` by the length of each returned batch. The at-least-once and total- order guarantees from [`05-event-protocol.md`](05-event-protocol.md) §4 are preserved because the underlying `read_range` semantics preserve them.

Push transports (Server-Sent Events, WebSocket) are OPTIONAL alternatives a deployment MAY expose alongside long-poll. They do not change the normative `subscribe` contract.

## 7. Error envelope

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
| `eden://error/wrong-token` | 403 | presented claim token ≠ stored token ([`04-task-protocol.md`](04-task-protocol.md) §3.3) |
| `eden://error/not-found` | 404 | referenced entity does not exist |
| `eden://error/already-exists` | 409 | insert collided with existing id |
| `eden://error/illegal-transition` | 409 | state-machine violation |
| `eden://error/conflicting-resubmission` | 409 | resubmit disagreed with committed payload ([`04-task-protocol.md`](04-task-protocol.md) §4.2) |
| `eden://error/invalid-precondition` | 409 | referenced entity not in required state; also the different-SHA branch of `integrate_trial` (§5) |

A server MUST NOT emit a `type` outside this vocabulary for a v0 endpoint. Implementations MAY add custom error types at non-`/v0/` paths (e.g. `/_reference/...`) using the same envelope.

## 8. Idempotency and retry

### 8.1 Submit

The [`04-task-protocol.md`](04-task-protocol.md) §4.2 submit-idempotency rule is preserved by the binding: a retry of `POST /v0/experiments/{E}/tasks/{T}/submit` carrying the same token and content-equivalent payload MUST return 200 identically to the original call. This lets a worker retry a submit after a transport error without risk of double- advance. No HTTP-level `Idempotency-Key` header is required.

### 8.2 Integrate

Same-value idempotency on `integrate_trial` is pinned in §5.

### 8.3 Other mutations

Claim, reject, reclaim, and accept are **not** blindly retry-safe on transport failures. A retry after the server already committed the transition will return `illegal-transition`, but that error is not uniquely distinguishable from a racing actor or a reclaim that interleaved between attempts. The binding does **not** standardize an HTTP-level retry convention for these endpoints; a client MUST resolve ambiguous failures via a follow-up `GET /v0/experiments/{E}/tasks/{T}` read and apply operation- specific knowledge. A future spec lineage MAY introduce per- operation idempotency tokens.

## 9. Reference-only helpers (non-normative)

The reference `eden_wire` server also exposes:

- `GET /_reference/experiments/{E}/tasks/{T}/validate-terminal`
- `POST /_reference/experiments/{E}/validate/metrics`

These are conveniences for the Phase-5 dispatch driver and are **not** part of the normative binding. A conforming third-party client MUST NOT rely on them being present. A conforming third-party orchestrator implementing its own accept/reject decision inline is free to do so; the [`04-task-protocol.md`](04-task-protocol.md) §4.3 decision rules are all that matter for the state machine.

Future phases MAY promote individual helpers into the normative binding if conformance testing requires it; such promotions follow the same versioning discipline as any other binding change.

## 10. Conformance

A conforming server:

- Implements §§2–7 with the normative semantics of [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), [`06-integrator.md`](06-integrator.md) §3.4, and [`08-storage.md`](08-storage.md).
- Exposes `/v0/` with the exact URL shapes listed above.
- Returns problem+json with `type` from the §7 vocabulary on every non-2xx response.
- Preserves the task-protocol, event-protocol, and integrator-atomicity invariants unchanged by the HTTP transport.

A conforming client:

- Keys retry and recovery on the `type` URI, not on HTTP status alone.
- Resolves transport-indeterminate `integrate_trial` failures via the read-back procedure in §5 and does not compensate ref writes without positive confirmation of server-side non-commit.
- Does not depend on `/_reference/` endpoints.

## 11. Implementation latitude

The binding leaves to implementations:

- The concrete HTTP server (FastAPI, Starlette, bespoke); the reference impl uses FastAPI.
- The SSE/WebSocket push transport, if any.
- The RECOMMENDED long-poll timeout; the reference impl uses 30 seconds.
- Transport-level concerns outside the binding's scope (TLS, compression, authentication). Authentication scaffolding is a later-phase concern.

What the binding does **not** leave to implementations:

- The URL shapes (§§2–6).
- The problem+json error vocabulary (§7).
- The same-value idempotency on `integrate_trial` (§5).
- Preservation of the [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), [`06-integrator.md`](06-integrator.md) §3.4, and [`08-storage.md`](08-storage.md) invariants through the HTTP transport.
