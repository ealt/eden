# Phase 13c — impl-stage codex-review record

Chunk: Managed (external) Postgres migration (issue #173).
Reviewer: Codex (`codex-cli 0.130.0`), run synchronously in the foreground via
the codex companion `review --wait --base main --scope branch`. Six rounds; each
finding was fixed and the branch re-reviewed until clean.

Durable artifacts only (per the top-level `.gitignore`, the regenerable
`*.jsonl` / `*.stderr` / `*.stdout` / `prompt.txt` transcripts are not committed).

## Round 1 — 2 × P2 (docs/process)

1. **Runbook TLS examples rejected by the schema.** The Path A/B
   `external.existingSecret` + `tls.enabled=true` examples omitted
   `tlsAlreadyEncodedInSecret: true` and showed an un-encoded DSN — following
   them would fail `helm install`. Fixed: encode TLS in the DSN, set the ack
   flag, note the inline-`connectionString` alternative.
2. **CHANGELOG narrated deferrals without issue links** (pooler, migration tool,
   read-replica). Fixed: filed #300 (scaling/pooler/read-replica) + #301
   (migration tool) and linked them. (#299 for the readonly/control-plane
   single-DSN posture was already filed.)

## Round 2 — 1 × P2 (correctness)

3. **`external.existingSecret` did not truly win over `connectionString`.** When
   both were set, `secret.yaml` still wrote the inline DSN into the chart Secret
   while the task-store-server read `EDEN_STORE_URL` from the existingSecret —
   two conflicting sources, leaking a stale inline password into the
   readonly/control-plane keys. Fixed: gate the inline write on `existingSecret`
   being absent. Verified the inline DSN appears nowhere when both are set.

## Round 3 — 3 × P2 (correctness)

4. **`secrets.existingSecret` (whole-chart) + external + inline
   `connectionString`** silently dropped the DSN (the chart Secret does not
   render in that case). Fixed: moved the "require external.\*" gate to a
   top-level schema clause that fires only when `secrets.existingSecret` is
   empty, and reject `connectionString` when it is set.
5. **`ensure_readonly_role` ran in external mode** when a whole-chart
   `secrets.existingSecret` carried `EDEN_READONLY_PASSWORD` — issuing
   CREATE/ALTER ROLE/GRANT against the managed instance (fails for accounts
   without role-management privilege). Fixed: blank `EDEN_READONLY_PASSWORD` on
   the task-store-server in external mode so the provision step is skipped.
6. **Path B runbook re-ran `setup-experiment-helm.sh`** for the mode swap, which
   reconciles the app tier back to `replicas: 1` and writes new events against
   the restored DB before verification. Fixed: swap via
   `helm upgrade --reuse-values` with app-tier replicas pinned to `0`, verify,
   then bring the tier up with a second upgrade.

## Round 4 — 3 × P2 (correctness/safety)

7. **`tls.enabled` accepted for `mode=embedded`** — the embedded StatefulSet runs
   stock Postgres with no server-side TLS, so the task-store-server would fail to
   connect. Fixed: schema rejects `tls.enabled=true` unless `mode=external`.
8. **`secrets.existingSecret` (whole-chart) + external + `tls.enabled`** did not
   require the TLS acknowledgement (the chart cannot append the suffix to a
   Secret it does not render). Fixed: extended the ack requirement to the
   whole-chart-secret path.
9. **`ci-smoke-managed-postgres.sh` could delete a pre-existing container** named
   `eden-ci-managed-pg` on an early exit. Fixed: track `CREATED_PG_CONTAINER`
   and refuse to reuse the name (mirrors the kind-cluster guard).

## Round 5 — 2 × P2 (process/correctness)

10. **Missing impl-stage review record** (this file) — AGENTS.md requires it for
    a plan-backed chunk marked shipped. Fixed: this record.
11. **external + `existingSecret` + lease mode** rendered a control-plane that
    crashes on a missing `EDEN_CONTROL_PLANE_STORE_URL`. Fixed: schema rejects
    `orchestrator.leaseMode.enabled=true` when `mode=external` (external + lease
    is documented-unsupported in v0, deferred behind #281 / #299).

## Round 6 — 1 × P2 (docs, self-inflicted by the round-4 guard)

12. **Rollback command broken by the new embedded+TLS guard.** The Path B
    rollback used `--reuse-values --set postgres.mode=embedded`, but the retained
    `postgres.tls.enabled=true` then trips the round-4 embedded+TLS rejection.
    Fixed: the rollback command explicitly resets `tls.enabled=false` and clears
    the `external.*` overrides while restoring replicas.

## Outcome

All findings resolved; round-7 re-review clean. Embedded-mode render verified
byte-for-byte identical to 13a (no upgrade rollout); the external-mode
`helm template` matrix + schema fail-closed assertions are wired into the
`helm-lint` CI job, and a `helm-smoke-managed-postgres` job exercises the path
end-to-end against a sibling Postgres on the kind network.
