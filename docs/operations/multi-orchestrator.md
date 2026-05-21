# Multi-orchestrator deployment

The orchestrator is a role, not a singleton; the reference deployment
can run two or more auto-orchestrator replicas concurrently. The
`compose.multi-orchestrator.yaml` overlay layered on top of
`compose.yaml` adds a second container (`worker_id=orchestrator-2`)
with per-replica repo + credentials volumes.

## When to run multiple replicas

- **HA / fault tolerance.** A single orchestrator crash stalls the
  experiment until restart; a second replica keeps making progress
  while the first recovers.
- **Verification of multi-instance correctness.** The
  `compose-smoke-multi-orchestrator` CI job exercises §6.4 invariants
  on the deployed substrate; running this overlay locally lets you
  reproduce the same configuration.

A single replica is sufficient for most reference deployments.
Multi-orchestrator is an opt-in posture.

## Spec guarantees (chapter 03 §6.4)

The four §6.2 decision types split into two safety classes:

- **Exact-idempotent** (execution dispatch, evaluation dispatch,
  integration). The task store's uniqueness constraints (at most one
  live execution task per idea, at most one live evaluation task per
  starting variant, exactly one `variant_commit_sha` per variant)
  guarantee concurrent invocations collapse to one outcome. A second
  replica calling `create_execution_task(idea_id=I)` after the first
  has committed gets `AlreadyExists` or `InvalidPrecondition` and
  treats it as success.
- **Bounded-overshoot** (ideation creation). The reference policy
  `maintain_pending(target=T)` may overshoot to `N * T` pending tasks
  when N replicas all observe the same low pending count
  simultaneously; subsequent iterations self-correct downward
  (each replica observes `pending >= T` and returns 0). For
  deployments where overshoot is unacceptable, supply a policy
  callable that implements its own coordination (e.g., advisory lock
  via the store).

## Bringing up the overlay

```bash
cd reference/compose
docker compose \
    -f compose.yaml \
    -f compose.multi-orchestrator.yaml \
    --env-file .env \
    up -d --wait
```

The `EDEN_ORCHESTRATOR_WORKER_ID` env var sets the primary replica's
worker_id (default `orchestrator`); the overlay hardcodes the
secondary at `orchestrator-2`. Both register themselves into the
`orchestrators` group at startup via the `_ensure_orchestrators_membership`
helper.

For more than two replicas, either author another overlay file
(`compose.multi-orchestrator-3.yaml` etc.) or fold the second
container's shape into a deployment-specific compose file. Each
replica MUST hold a unique `worker_id` — the registry is identity-keyed
and a duplicate id would corrupt the credential.

## Validating the deployment

After `compose up --wait` returns, check both replicas are members of
the `orchestrators` group:

```bash
curl -fsS \
  -H "Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}" \
  -H "X-Eden-Experiment-Id: ${EDEN_EXPERIMENT_ID}" \
  http://localhost:8080/v0/experiments/${EDEN_EXPERIMENT_ID}/groups/orchestrators \
  | jq '.members'
```

Expected: `["orchestrator", "orchestrator-2"]` (order not significant).

The `compose-smoke-multi-orchestrator` CI job's chaos test
(`docker kill eden-orchestrator` mid-experiment; assert secondary
drives to quiescence) is a more thorough smoke; run it locally before
deploying:

```bash
bash reference/compose/healthcheck/smoke-multi-orchestrator.sh
```

## Per-replica volumes

The overlay creates two volumes specific to `orchestrator-2`:

- `eden-orchestrator-2-repo` — its private bare clone of the Forgejo-hosted
  repo. Per-replica clones is the design posture (the wire is the only
  synchronization point); no shared git state between replicas.
- `eden-orchestrator-2-credentials` — its per-worker bootstrap credential
  (`/var/lib/eden/credentials/orchestrator-2.token`).

`docker compose down -v` removes both alongside the primary's volumes.

## Spec references

- Chapter 03 §6 — orchestrator role contract.
- Chapter 03 §6.4 — multi-instance safety.
- Chapter 07 §5 — same-value-idempotent `integrate_variant` for the
  integration decision's exact-idempotent guarantee.
