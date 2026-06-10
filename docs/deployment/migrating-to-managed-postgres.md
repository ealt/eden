# Managed (external) Postgres for the EDEN Helm chart

Phase 13c adds a `postgres.mode` switch to the
[reference Helm chart](../../reference/helm/eden/):

- `embedded` (default) — an in-cluster Postgres `StatefulSet` (13a behavior).
- `external` — an operator-provisioned managed Postgres (RDS, Cloud SQL,
  AlloyDB, Supabase, Neon, Crunchy Bridge, …), reached over the network via a
  DSN.

The values surface is documented in [helm.md §4.4](helm.md#44-postgres-mode-embedded-vs-external);
this runbook is the operational guide: **Path A** stands up a fresh deployment
directly on managed Postgres; **Path B** migrates an existing embedded
deployment onto a managed instance without data loss. Both are docs-only flows —
13c ships no migration automation (see [Why a runbook, not a script](#why-a-runbook-not-a-script)).

> **Scope note.** External mode points all three derived DSN keys at the single
> operator DSN; least-privilege readonly-role and separate control-plane-database
> provisioning are tracked in
> [#299](https://github.com/ealt/eden/issues/299). External + lease mode
> (`orchestrator.leaseMode.enabled`) is not a supported v0 combination (lease
> mode is deferred behind [#281](https://github.com/ealt/eden/issues/281)).

## What the EDEN account needs

EDEN bootstraps its own schema on first connect (`ensure_schema`), so the
managed-Postgres user the DSN authenticates as needs DDL + DML on an **empty**
target database:

- `CREATE TABLE`, `CREATE INDEX` (schema bootstrap), plus `SELECT` / `INSERT` /
  `UPDATE` / `DELETE` at runtime.
- Ownership of (or full privilege on) the target database/schema.

EDEN does **not** issue `CREATE ROLE` / `GRANT` / `CREATE DATABASE` against the
managed instance — provisioning the database + account is upstream of EDEN, done
with your provider's console/CLI.

## TLS

Most managed providers require TLS and publish a CA bundle. Enable verification
in the chart:

```yaml
postgres:
  tls:
    enabled: true
    mode: verify-full            # require | verify-ca | verify-full (weak modes rejected)
    caBundleSecret: eden-rds-ca
    caBundleKey: ca.crt
```

The chart appends `?sslmode=<mode>[&sslrootcert=/etc/eden/postgres-ca/<key>]` to
the DSN it composes and mounts the CA bundle into the task-store-server pod.

Provider CA bundles:

- **AWS RDS** — `curl -O https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`,
  then `kubectl create secret generic eden-rds-ca -n <ns> --from-file=ca.crt=global-bundle.pem`.
  `verify-full` works (RDS endpoints have hostname-matching certs).
- **GCP Cloud SQL** — without the Auth Proxy, the server cert's CN is the
  instance id (not a hostname), so use `mode: verify-ca` with the
  per-instance server CA (`gcloud sql instances describe <inst>
  --format='value(serverCaCert.cert)'`). With the Auth Proxy, see below.
- **Azure Database for PostgreSQL** — DigiCert Global Root G2 / Microsoft RSA
  Root CA (region-dependent; see Azure docs).
- **Supabase / Neon / Crunchy Bridge** — typically Let's Encrypt; download the
  provider's documented root and load it as the CA secret. The chart requires an
  explicit CA secret rather than relying on the image's system trust store, so
  the trust anchor is auditable.

### The Cloud SQL Auth Proxy exception

GCP's Cloud SQL Auth Proxy presents Postgres on `127.0.0.1` (or a UNIX socket)
and handles TLS + auth itself, so the application connects with **`sslmode`
disabled** even though the transport is encrypted. For that topology keep
`postgres.tls.enabled=false` and point the DSN at the proxy
(`postgresql://eden:<pw>@127.0.0.1:5432/eden`). The chart's default
(`tls.enabled=false`) accommodates this with no override. Running the proxy as a
sidecar in the task-store-server pod is provider-specific and is **not** shipped
as a chart template (the chart stays vendor-neutral); deploy it yourself per
Google's docs.

## Path A — fresh deployment on managed Postgres

1. Create an **empty** database + an account with the privileges above via your
   provider's console/CLI. URI-encode any reserved chars in the password.
2. Store the DSN in a Kubernetes Secret. When you reference a Secret via
   `external.existingSecret`, the chart cannot edit it, so **encode the TLS
   params into the DSN yourself** (`?sslmode=…` + `sslrootcert=` pointing at the
   CA path the chart mounts — `/etc/eden/postgres-ca/<caBundleKey>`):

   ```bash
   kubectl create secret generic eden-managed-postgres -n eden-prod \
     --from-literal=EDEN_STORE_URL='postgresql://eden:<encoded-pw>@<host>:5432/eden?sslmode=verify-full&sslrootcert=/etc/eden/postgres-ca/ca.crt'
   ```

3. Set chart values (`values.yaml`). `tlsAlreadyEncodedInSecret: true`
   acknowledges that TLS lives in your DSN (required by `values.schema.json` for
   `existingSecret` + `tls.enabled`); `caBundleSecret` still mounts the CA file
   at the path your DSN references:

   ```yaml
   postgres:
     mode: external
     external:
       existingSecret: eden-managed-postgres
       connectionStringKey: EDEN_STORE_URL
       tlsAlreadyEncodedInSecret: true
     tls:
       enabled: true
       mode: verify-full
       caBundleSecret: eden-rds-ca   # or your provider's
   ```

   > Prefer to let the chart compose the TLS suffix for you? Use inline
   > `external.connectionString` (without `existingSecret`) instead — then set
   > `tls.enabled`/`mode`/`caBundleSecret` and the chart appends the suffix.

4. Bootstrap the experiment exactly as for embedded mode — the setup script is
   mode-agnostic (it skips the embedded-Postgres readiness wait when
   `mode=external`):

   ```bash
   bash reference/scripts/setup-experiment-helm.sh \
     --namespace eden-prod --release eden \
     --image <registry>/eden-reference:<tag> \
     --values values.yaml \
     --experiment-config tests/fixtures/experiment/.eden/config.yaml
   ```

   The task-store-server's first start runs `ensure_schema`, creating all tables
   on the managed instance. No further operator action.

## Path B — migrate an existing embedded deployment

Migrates a release running `postgres.mode=embedded` (13a/13c default) onto a
managed instance. Prerequisites: an empty target database on the managed
instance, and `pg_dump`/`pg_restore` (matching the Postgres major version) plus
network reachability to both endpoints from your workstation.

### 1. Drain workers (mandatory)

A consistent snapshot requires no in-flight writes. The reference worker hosts
claim tasks **without** a TTL, so the orchestrator's expired-claim sweeper does
not reclaim them — the drain is operator-driven. Scale workers to zero (graceful
SIGTERM lets in-flight tasks finish + submit within `terminationGracePeriodSeconds`):

```bash
kubectl scale statefulset -n eden-prod \
  eden-executor-host eden-evaluator-host --replicas=0
kubectl scale deployment -n eden-prod eden-ideator-host --replicas=0
```

`kubectl scale` only sets the desired count — it returns **before** the pods
exit. Wait for the worker pods to actually terminate (so none can still submit or
integrate) before reclaiming:

```bash
kubectl wait --for=delete pod -n eden-prod --timeout=120s \
  -l 'app.kubernetes.io/component in (executor-host,evaluator-host,ideator-host)'
```

Then reclaim any task still `state=claimed` (Web UI admin "reclaim" button, or
the wire `reclaim` endpoint with the admin bearer), confirm none remain, and
scale the orchestrator + web-ui to zero — again waiting for them to terminate:

```bash
kubectl scale statefulset -n eden-prod \
  eden-orchestrator eden-web-ui --replicas=0
kubectl wait --for=delete pod -n eden-prod --timeout=120s \
  -l 'app.kubernetes.io/component in (orchestrator,web-ui)'
```

Leave the **task-store-server running** for the consistency check in step 2.
Confirm every other workload is at zero before snapshotting
(`kubectl get pods -n eden-prod` should show only `task-store-server`, `postgres`,
and `forgejo` running).

> Do **not** snapshot with workers running: `pg_dump` captures a consistent SQL
> snapshot, but the Forgejo-side git refs workers push during the dump are not
> captured, breaking the chapter-6 atomicity invariant on restore.

### 2. Baseline + `pg_dump`

Record the event count before snapshotting so step 5 can verify the round-trip:

```bash
EVENT_COUNT_BEFORE=$(kubectl exec -n eden-prod eden-postgres-0 -- \
  psql --user=eden --dbname=eden -tAc 'SELECT count(*) FROM event;')
echo "baseline events: $EVENT_COUNT_BEFORE"

kubectl exec -n eden-prod eden-postgres-0 -- \
  pg_dump --format=custom --no-owner --no-acl --user=eden --dbname=eden \
  > eden-snapshot.dump
```

`--no-owner --no-acl` strips the embedded `eden` role ownership (the managed
instance uses its own role); `--format=custom` is `pg_restore`-compatible.

### 3. Restore into the managed instance (empty database)

```bash
pg_restore --no-owner --no-acl --dbname='postgresql://eden:<pw>@<host>:5432/eden' \
  eden-snapshot.dump
```

If `pg_restore` fails midway (partial tables, network drop, permissions): drop
the partially-restored objects (or the database), recreate empty, and re-run.
`pg_restore --clean --if-exists` automates this but silently drops objects —
prefer the explicit drop/recreate when you're new to the flow or unsure of the
target.

### 4. Swap the chart value + `helm upgrade` (app tier stays down)

Create the DSN Secret (Path A step 2 — encode the TLS params into the DSN), then
flip `postgres.mode` with a direct `helm upgrade --reuse-values` (retaining the
seed SHA, minted identities, and other secrets from the live release). **Do not
re-run `setup-experiment-helm.sh` here** — its phase-3 upgrade brings the app
tier back up at `replicas: 1`, which would write new events against the restored
database *before* you verify the snapshot. Pin the app-tier replicas to `0` in
this upgrade so only the task-store-server comes up against the managed DB:

```bash
helm upgrade eden ./reference/helm/eden -n eden-prod --reuse-values \
  --set postgres.mode=external \
  --set-string postgres.external.existingSecret=eden-managed-postgres \
  --set postgres.external.tlsAlreadyEncodedInSecret=true \
  --set postgres.tls.enabled=true \
  --set postgres.tls.mode=verify-full \
  --set-string postgres.tls.caBundleSecret=eden-rds-ca \
  --set replicas.orchestrator=0 --set replicas.ideatorHost=0 \
  --set replicas.executorHost=0 --set replicas.evaluatorHost=0 \
  --set replicas.webUi=0 \
  --wait
```

The embedded Postgres `StatefulSet` is no longer rendered. Its PVC is **retained**
(StatefulSet PVCs are never auto-deleted) — keep it until you've verified the
migration, then delete it manually to reclaim storage:
`kubectl delete pvc data-eden-postgres-0 -n eden-prod`.

### 5. Verify the round-trip, then bring the app tier up

The restored dump carried the `schema_version` table, so the task-store-server
sees the version up-to-date and skips re-bootstrap. With the app tier still at
zero, confirm the event count matches the baseline (via the wire, against the new
backend):

```bash
TS_POD=$(kubectl get pod -n eden-prod \
  -l app.kubernetes.io/component=task-store-server -o name | head -n1)
TOKEN=$(kubectl get secret eden-secrets -n eden-prod \
  -o 'jsonpath={.data.EDEN_ADMIN_TOKEN}' | base64 -d)
kubectl exec -n eden-prod "$TS_POD" -- curl -fsS \
  -H "Authorization: Bearer admin:${TOKEN}" \
  -H "X-Eden-Experiment-Id: <exp-id>" \
  "http://localhost:8080/v0/experiments/<exp-id>/events" | jq '.events | length'
```

It should equal `EVENT_COUNT_BEFORE`. **Only after it matches**, bring the app
tier back up with a second `helm upgrade` restoring the replica counts (so the
chart values and the live state agree — a later `kubectl scale` would drift from
values):

```bash
helm upgrade eden ./reference/helm/eden -n eden-prod --reuse-values \
  --set replicas.orchestrator=1 --set replicas.ideatorHost=1 \
  --set replicas.executorHost=1 --set replicas.evaluatorHost=1 \
  --set replicas.webUi=1 \
  --wait
```

### Rollback

If verification fails, you have not lost the embedded data — its PVC is still
attached. Flip back to embedded with an explicit upgrade. Because step 4 used
`--reuse-values`, the release still carries `postgres.tls.enabled=true` and the
`external` values; you **must reset them** in the same command — TLS is rejected
for `mode=embedded` (the schema guard), so a bare `--set postgres.mode=embedded`
would fail validation:

```bash
helm upgrade eden ./reference/helm/eden -n eden-prod --reuse-values \
  --set postgres.mode=embedded \
  --set postgres.tls.enabled=false \
  --set-string postgres.external.existingSecret="" \
  --set-string postgres.external.connectionString="" \
  --set postgres.external.tlsAlreadyEncodedInSecret=false \
  --set replicas.orchestrator=1 --set replicas.ideatorHost=1 \
  --set replicas.executorHost=1 --set replicas.evaluatorHost=1 \
  --set replicas.webUi=1 \
  --wait
```

The original `StatefulSet` reattaches to the retained PVC. Investigate the
divergence before retrying the migration.

## Backup and disaster recovery

EDEN ships no backup automation — managed providers do this better:

- **Point-in-time recovery** (continuous WAL archiving) is the standard answer
  for production EDEN; the append-only event log is recoverable to any commit
  point.
- **Automated snapshots / cross-region replicas** per your provider.

The tables that hold protocol state (back all of them up): `experiment`, `task`,
`submission`, `idea`, `variant`, `event`, `schema_version`, plus the artifact
metadata table. This is distinct from Phase 12b **portable checkpoints**, which
are an EDEN-level export for moving an experiment *between* deployments (a
different cluster / provider) — use 12b to share or relocate an experiment, and
provider backups to recover the same instance to an earlier point in time.

## Why a runbook, not a script

A scripted migration would have to know each operator's managed-provider auth
shape (IAM, passwords, the Cloud SQL Auth Proxy, …), which varies per provider
and per operator — it would become a per-provider matrix or a
least-common-denominator that helps no one. A runbook lets you substitute your
own auth specifics at each step while keeping the EDEN-specific sequencing
(drain → snapshot → restore → swap → verify) authoritative.
