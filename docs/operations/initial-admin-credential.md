# Initial-admin credential recovery

`setup-experiment.sh` seeds an initial admin worker (default display
name `operator`, override via `EDEN_ADMINS_INITIAL_MEMBER`) into the
`admins` group. Since [#128](https://github.com/ealt/eden/issues/128)
the worker's `worker_id` is **system-minted and opaque**
(`wkr_<26-char-ULID>`); the operator-facing label `operator` is its
display *name*. Setup mints the id and writes it to `.env` (the
`EDEN_ADMINS_INITIAL_MEMBER` value carries the minted `wkr_*` id, not a
typed string). The reserved `admins` group is likewise auto-created at
setup with a minted opaque `grp_*` id and `name == "admins"`. The
initial registration emits a one-time `registration_token` per
chapter 02 §6.3, but **setup-experiment intentionally discards it** —
the script's job is to bring the stack up, not to issue operator
credentials. This playbook is the canonical recovery path for minting a
usable bearer.

The deployment-admin **bearer principal** (`admin:${EDEN_ADMIN_TOKEN}`)
is unchanged by the rename: it stays the literal token `admin` and has
no `worker_id` minted for it.

## Why the initial token isn't captured

Two reasons:

1. **Setup-experiment runs idempotently.** Re-running on an
   already-bootstrapped deployment doesn't emit a fresh token
   (chapter 02 §6.3: idempotent re-registration returns the existing
   record without a new token). If setup were the authoritative
   token source, a second run would silently leave the operator
   without a credential.
2. **The credential delivery posture is operator-chosen.** Some
   deployments persist operator credentials in a secrets manager,
   some hand them out via terminal, some use the web UI's
   `reissue_credential` flow at first sign-in. The script staying
   out of credential delivery keeps that operator policy clean.

## The recovery path: admin-token `reissue_credential`

The deployment-admin bearer (`admin:${EDEN_ADMIN_TOKEN}`) is
authorized to mint a fresh credential for ANY registered worker —
including the initial admin. Use this to bootstrap an operator
session.

```bash
# Read EDEN_ADMIN_TOKEN from your .env (or pass via --admin-token
# at the time of setup-experiment if you wanted to pin it).
EDEN_ADMIN_TOKEN="$(grep '^EDEN_ADMIN_TOKEN=' reference/compose/.env | cut -d= -f2-)"
EDEN_EXPERIMENT_ID="$(grep '^EDEN_EXPERIMENT_ID=' reference/compose/.env | cut -d= -f2-)"

# The reissue path-param is the operator worker's opaque wkr_* id, which
# setup wrote to .env. (If you don't have it, look it up by display name:
#   curl -fsS -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
#     -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
#     "http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/workers?name=operator" \
#     | jq -r '.workers[].worker_id'   # 0..N — names MAY collide
# .)
OPERATOR_WORKER_ID="$(grep '^EDEN_ADMINS_INITIAL_MEMBER=' reference/compose/.env | cut -d= -f2-)"

# Reissue the initial admin's credential. The response carries the
# fresh registration_token; capture it before the response scrolls off.
curl -fsS \
  -X POST \
  -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
  -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
  "http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/workers/${OPERATOR_WORKER_ID}/reissue-credential" \
  | jq -r '.registration_token'
```

Output: a 64-character hex string. Store it as the operator's bearer:

```bash
OPERATOR_TOKEN="<that-hex-string>"
# The bearer principal is the operator worker's opaque wkr_* id (NOT the
# display name "operator") — the same id used in the reissue path above.
OPERATOR_BEARER="${OPERATOR_WORKER_ID}:${OPERATOR_TOKEN}"
```

The operator can now drive admins-gated ops:

```bash
# Verify the bearer authenticates as the operator worker (whoami returns
# its opaque worker_id + display name).
curl -fsS \
  -H "Authorization: Bearer ${OPERATOR_BEARER}" \
  -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
  http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/whoami

# Flip dispatch_mode (an admins-only op):
curl -fsS \
  -X PATCH \
  -H "Authorization: Bearer ${OPERATOR_BEARER}" \
  -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
  -H "Content-Type: application/json" \
  -d '{"integration":"manual"}' \
  http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/dispatch_mode
```

## Important: reissue invalidates prior tokens

Per chapter 07 §6.3, `reissue_credential` invalidates the prior
credential atomically with minting the new one. If someone (you, a
prior operator, the web UI) was already using a credential for this
worker, **that credential will start returning 401 unauthorized on the
next request**. Reissue is the canonical recovery path; it's also a
hard cut.

If you don't know whether the credential is in use elsewhere and want
to be safe, register a new admin worker instead:

```bash
# Register a NEW admin worker (the server mints a fresh wkr_* id),
# capture its id + fresh token, then add it to the `admins` group.
NEW_ADMIN_RESP="$(curl -fsS -X POST \
    -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
    -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
    -H "Content-Type: application/json" \
    -d '{"name":"operator-bob"}' \
    http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/workers)"
NEW_WORKER_ID="$(echo "$NEW_ADMIN_RESP" | jq -r '.worker_id')"   # opaque wkr_*
NEW_TOKEN="$(echo "$NEW_ADMIN_RESP" | jq -r '.registration_token')"

# Resolve the reserved `admins` group's opaque grp_* id by its name.
ADMINS_GROUP_ID="$(curl -fsS \
    -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
    -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
    "http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/groups?name=admins" \
    | jq -r '.groups[0].group_id')"

# Add the new worker (by its opaque id) to the admins group.
curl -fsS -X POST \
    -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
    -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
    -H "Content-Type: application/json" \
    -d "{\"member_id\":\"${NEW_WORKER_ID}\"}" \
    "http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/groups/${ADMINS_GROUP_ID}/members"
```

`admins` is a reserved group *name* resolving to a system-minted opaque
`grp_*` id; membership is additive and keyed on opaque member ids
(`wkr_*` or `grp_*`). The deployment can have any number of operator
workers.

## Web UI auth

The reference web UI's auth flow uses the host's own worker
credential (the web-ui container is itself a registered worker,
bootstrapped at startup via `bootstrap_worker_credential`). The
operator's web-UI sign-in is a session-cookie posture, not a per-user
bearer; the UI carries its own credential to the task-store-server.

To let the UI drive admins-gated ops (PATCH `/dispatch_mode`, POST
`/tasks/{T}/reassign`), `setup-experiment.sh` pre-registers the
web-ui's worker (display name `web-ui-1`; its minted opaque `wkr_*` id
is written to `.env` as `EDEN_WEB_UI_WORKER_ID`) and adds it to the
`admins` group during bootstrap. The web-ui's own startup
`bootstrap_worker_credential` then sees the existing row and reissues
to obtain its token per §8.2. Until per-user session bearers land,
the web-ui acts as a single deployment-level admin actor — anyone
signed into the UI inherits its admins authority.

Per-user session bearers (where each operator's web-UI session would
authenticate as their own worker via `reissue_credential` at sign-in)
remain a deferred 12a-1b follow-up — see [`AGENTS.md`](../../AGENTS.md)
"Current phase" for the carry-over.

## Spec references

- Chapter 02 §6.3 — registration is idempotent on existing record;
  reissue is the credential-recovery path.
- Chapter 07 §6.3 — `reissue_credential` wire endpoint.
- Chapter 07 §13.4 — credential lifecycle.
