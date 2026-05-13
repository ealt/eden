# Initial-admin credential recovery

`setup-experiment.sh` seeds an initial admin worker (default
`worker_id=operator`, override via `EDEN_ADMINS_INITIAL_MEMBER`) into
the `admins` group. The initial registration emits a one-time
`registration_token` per chapter 02 §6.3, but **setup-experiment
intentionally discards it** — the script's job is to bring the stack
up, not to issue operator credentials. This playbook is the canonical
recovery path for minting a usable bearer.

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

# Reissue the initial admin's credential. The response carries the
# fresh registration_token; capture it before the response scrolls off.
curl -fsS \
  -X POST \
  -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
  -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
  http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/workers/operator/reissue-credential \
  | jq -r '.registration_token'
```

Output: a 64-character hex string. Store it as the operator's bearer:

```bash
OPERATOR_TOKEN="<that-hex-string>"
OPERATOR_BEARER="operator:${OPERATOR_TOKEN}"
```

The operator can now drive admins-gated ops:

```bash
# Verify the bearer authenticates as the operator worker_id.
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
# Register a NEW admin worker (different worker_id), capture its
# fresh token, add it to the `admins` group.
NEW_ADMIN_RESP="$(curl -fsS -X POST \
    -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
    -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
    -H "Content-Type: application/json" \
    -d '{"worker_id":"operator-bob"}' \
    http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/workers)"
NEW_TOKEN="$(echo "$NEW_ADMIN_RESP" | jq -r '.registration_token')"

curl -fsS -X POST \
    -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
    -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
    -H "Content-Type: application/json" \
    -d '{"member_id":"operator-bob"}' \
    http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/groups/admins/members
```

`admins` is just a group; membership is additive. The deployment can
have any number of operator workers.

## Web UI auth

The reference web UI's auth flow uses the host's own worker
credential (the web-ui container is itself a registered worker,
bootstrapped at startup via `bootstrap_worker_credential`). The
operator's web-UI sign-in is a session-cookie posture, not a per-user
bearer; the UI carries its own credential to the task-store-server.
Per-user session bearers (where each operator's web-UI session would
authenticate as their own worker via `reissue_credential` at sign-in)
remain a deferred 12a-1b follow-up — see [`AGENTS.md`](../../AGENTS.md)
"Current phase" for the carry-over.

## Spec references

- Chapter 02 §6.3 — registration is idempotent on existing record;
  reissue is the credential-recovery path.
- Chapter 07 §6.3 — `reissue_credential` wire endpoint.
- Chapter 07 §13.4 — credential lifecycle.
