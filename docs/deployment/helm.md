# Deploying EDEN on Kubernetes with Helm

This guide covers deploying the EDEN reference services on a Kubernetes cluster
via the Helm chart at [`reference/helm/eden/`](../../reference/helm/eden/). It is
the Kubernetes analogue of the Compose deployment documented in
[`reference/compose/README.md`](../../reference/compose/README.md).

The Helm deployment is **parallel** to Compose, not a replacement. Both are
first-class v0 substrates. The chart's **default** is the single-experiment path
(one orchestrator drives one experiment directly; no control plane), mirroring
the Compose default stack that `compose-smoke` validates. Lease-driven HA (a
control plane plus multiple contending orchestrator replicas) is an opt-in
toggle, `orchestrator.leaseMode.enabled`, that is **deferred + unvalidated behind
[#281](https://github.com/ealt/eden/issues/281)** — leave it off until #281
reconciles the #128 opaque-id minting with #147's lease-mode orchestrator. The
two substrates are independent deployments — you cannot "upgrade" a Compose stack
to Helm without exporting via
[12b portable checkpoints](../../reference/compose/healthcheck/smoke-checkpoint.sh)
and re-importing.

## 1. Prerequisites

**AWS shortcut.** On EKS, all of the prerequisites below can be provisioned
in one idempotent pass by
[`reference/scripts/setup-aws/setup-aws.sh`](../../reference/scripts/setup-aws/setup-aws.sh)
(issue [#309](https://github.com/ealt/eden/issues/309)): EKS cluster
verify-or-create (+ OIDC provider + the `aws-ebs-csi-driver` addon that PVC
provisioning needs), ECR repo + image build/push, RDS Postgres
(`postgres.mode=external`) or an operator-supplied DSN, and the S3 bucket +
IRSA role for `blob.backend=s3`. It writes a ready Helm values file and
prints the exact `setup-experiment-helm.sh` invocation. Every step is
create-if-absent, so re-running converges; `--dry-run` prints every
mutating command instead of executing it. The list below is the
substrate-agnostic contract the script satisfies — and the manual path for
non-AWS clusters.

- **A cluster (Kubernetes 1.27+)** — kind, k3s, EKS, GKE, or AKS. The chart is
  Kubernetes-vanilla: no CRDs, no cloud-provider assumptions baked in.
- **A default `StorageClass`** that provisions `ReadWriteOnce` PVCs. Confirm
  with `kubectl get storageclass`; pin a specific class via `storage.className`
  if there is no default.
- **`kubectl` and `helm` (3.x)** configured against the cluster.
- **The `eden-reference` image, built and pushed** to a registry the cluster
  can pull from. The chart does not build images — the Compose stack builds
  `eden-reference:dev` as a local Dockerfile target, which a cluster cannot
  pull. Build + push it (or wire it into your CI):

  ```sh
  docker build -t <registry>/eden-reference:<tag> \
    -f reference/compose/Dockerfile .
  docker push <registry>/eden-reference:<tag>
  ```

`image.repository` and `image.tag` are **required**; the chart fails at
template time (not at pod-pull time) if either is empty.

## 2. Install walkthrough

The canonical bootstrap is
[`setup-experiment-helm.sh`](../../reference/scripts/setup-experiment-helm.sh).
It runs the install in three phases (see §3), minting the experiment's opaque
worker identities. Omit `--experiment-id` to mint a fresh `exp_*` id:

```sh
bash reference/scripts/setup-experiment-helm.sh \
  --namespace eden-prod \
  --release eden \
  --image <registry>/eden-reference:<tag> \
  --experiment-config tests/fixtures/experiment/.eden/config.yaml
```

What it does:

1. `helm upgrade --install` with `experiment.baseCommitSha` empty → the infra
   tier (Postgres, Forgejo) comes up.
2. Provisions the Forgejo `eden` user (idempotent) and seeds the bare repo via
   a one-shot Job; the push auto-creates the experiment repo on Forgejo. Then
   `helm upgrade --set experiment.baseCommitSha=<seed-sha>` → the
   **task-store-server** comes up.
3. Mints the reserved `admins` / `orchestrators` groups + one worker per service
   against the task-store-server (`POST {"name": ...}`, under #128 the server
   mints the opaque `worker_id` and returns a one-time `registration_token`),
   then `helm upgrade` with the minted `identity.*` values → the **app tier**
   (orchestrator, worker hosts, web-ui) comes up with pre-provisioned
   credentials. With `orchestrator.leaseMode.enabled=true` (deferred #281) it
   additionally deploys the control plane and registers the experiment with it.

The experiment id must be an opaque `exp_*` id (the event model rejects any id
not matching `^exp_[0-9a-hjkmnp-tv-z]{26}$`); the script mints one when
`--experiment-id` is omitted. When no `--values` file is supplied, the script
generates fresh dev secrets
(read back from the cluster Secret on re-run, so re-running is idempotent) and
requires `--image`. Supply `--values <file>` (e.g. for production secrets via
`existingSecret`) to drive secrets yourself.

**One experiment per release in v0.** The reference task-store-server is
single-experiment per process, and the release-wide `experiment.id` value points
the whole stack at one experiment. Do **not** re-run the script with a different
`--experiment-id` against the same release — that rewrites `experiment.id` and
re-points the single task-store/worker stack at the new experiment, orphaning
the first. To run a **second** experiment, install a **separate release in a
separate namespace** (e.g. `--release eden-b --namespace eden-b`). True
multiple-experiments-per-release hosting is tracked in
[#254](https://github.com/ealt/eden/issues/254).

Reach the Web UI:

```sh
kubectl -n eden-prod port-forward svc/eden-web-ui 18090:8090
open http://localhost:18090/
```

### Plain `helm install` (chart resources only — NOT a complete bootstrap)

A bare `helm install` cannot produce a functional deployment: under the #128
opaque-id model the task-store-server **mints** every worker id, so the chart
cannot know the orchestrator / worker-host / web-ui `worker_id`s (and their
tokens) until the script has minted them — and the app-tier templates render
only once `identity.<svc>.workerId` is set. A bare install also skips the
Forgejo `eden` user, the bare-repo seed, and the reserved `admins` /
`orchestrators` group bootstrap. There is no hand-set values incantation that
substitutes for the minting round-trip; use `setup-experiment-helm.sh`, which is
the canonical, supported bootstrap. (An operator wiring the chart by hand would
have to mint the workers against a running task-store-server themselves and feed
the returned ids + tokens back as `identity.*` values — exactly what the script
automates.)

## 3. The three-phase bootstrap (why)

Two facts force the multi-phase shape:

1. **Seed gate.** The reference task-store-server is single-experiment per
   process and records the experiment's **seed commit** at first experiment-row
   creation — a row created without a seed never acquires a baseline
   (02-data-model.md §9.4). So the task-store-server must not start before the
   seed SHA is known. `experiment.baseCommitSha` is its render gate
   (`{{ include "eden.appEnabled" . }}`).
2. **Identity gate.** Under #128 the task-store-server mints each service's
   opaque `worker_id`; the orchestrator, worker hosts, and web-ui cannot start
   before their id exists. Each renders only once its `identity.<svc>.workerId`
   value is set (`{{ include "eden.identityEnabled" ... }}`).

So phase 1 brings up infra (Postgres, Forgejo), phase 2 seeds the repo and
starts the task-store-server (`baseCommitSha` set), and phase 3 mints the
identities and starts the app tier (`identity.*` set). This is deliberate
bootstrap gating, distinct from scaling a service to zero (§5) — during the
earlier phases nothing references the not-yet-rendered Services, so their
absence is harmless.

## 4. Values reference

See [`reference/helm/eden/values.yaml`](../../reference/helm/eden/values.yaml)
for the fully-annotated surface and the
[chart README](../../reference/helm/eden/README.md) for the summary table. Key
groups:

- **`image`** — required repository + tag; `pullPolicy`; `pullSecrets`.
- **`experiment`** — `id` (opaque `exp_*`), inline `config` (→ ConfigMap), `baseCommitSha`.
- **`identity`** — per-service minted `workerId` + `token`; set by the setup
  script (the app-tier gate). Leave empty for hand-wired installs to mint
  yourself.
- **`orchestrator.leaseMode.enabled`** — opt-in lease-driven HA + control plane;
  default `false`, deferred + unvalidated behind #281.
- **`replicas`** — per-service counts; orchestrator defaults to 1
  (single-experiment). Multi-replica HA needs lease mode + per-replica
  identities (#281).
- **`secrets`** — dev inline or `existingSecret` (§4.1).
- **`storage`** — `className` + per-PVC sizes.
- **`ingress`** — Web UI Ingress, off by default (§4.2).
- **`config`** — Compose-mirror knobs (lease duration, quiescence, worker mode).
- **`postgres.mode`** — `embedded` (default, in-cluster StatefulSet) or
  `external` (operator-managed Postgres via DSN); see §4.4.

### 4.1 Secrets

**Development.** Set the secret values inline; the chart materializes a single
`<release>-secrets` Secret and derives the Postgres DSNs from
`secrets.postgresPassword`. The password is interpolated into the DSNs verbatim,
so it must be URL-safe (the setup script generates hex; supply pre-encoded DSNs
via `existingSecret` for passwords with reserved URI characters).

**Production.** Pre-create the Secret externally (Sealed Secrets, External
Secrets Operator, Vault) and set `secrets.existingSecret=<name>`. It must carry
every key the chart-managed Secret would: `EDEN_ADMIN_TOKEN`,
`EDEN_SESSION_SECRET`, `POSTGRES_PASSWORD`, `EDEN_READONLY_PASSWORD`,
`FORGEJO_REMOTE_PASSWORD`, `FORGEJO_SECRET_KEY`, `FORGEJO_INTERNAL_TOKEN`,
`EDEN_STORE_URL`, `EDEN_READONLY_STORE_URL`, `EDEN_CONTROL_PLANE_STORE_URL`.

The Forgejo auth posture mirrors Compose's HTTP Basic + git-credential-helper
exactly; 13e replaces it with per-branch ACLs + native PR review.

### 4.2 Ingress

`ingress.enabled=false` by default — services are `ClusterIP` and reached via
`kubectl port-forward`. To expose the Web UI, set `ingress.enabled=true`,
`ingress.className=<your-controller>`, and `ingress.hosts.webUi=<hostname>`
(defaults to a per-release `<release>-webui.eden.local` to avoid cross-release
hostname collisions). Supply `ingress.tls` cert refs as your controller expects.

### 4.3 Private registries

Pre-create an `imagePullSecret` and reference it:

```sh
kubectl -n eden-prod create secret docker-registry regcred \
  --docker-server=<registry> --docker-username=<u> --docker-password=<p>
helm upgrade eden reference/helm/eden -n eden-prod --reuse-values \
  --set image.pullSecrets[0].name=regcred
```

### 4.4 Postgres mode (embedded vs external)

`postgres.mode` selects how the task-store-server's Postgres is provisioned
(Phase 13c). Both modes are first-class; pick per release.

**`embedded` (default).** The chart deploys an in-cluster Postgres
`StatefulSet` + headless `Service` and composes the DSNs from `postgres.user` /
`postgres.database` + `secrets.postgresPassword`. This is exactly 13a's
behavior — upgrading a 13a release to 13c without touching `values.yaml`
renders byte-for-byte the same and triggers no rollout. Best for greenfield
clusters and test environments.

**`external`.** The chart deploys **no** Postgres; you point the
task-store-server at a managed Postgres you provisioned out-of-band (RDS, Cloud
SQL, AlloyDB, Supabase, Neon, Crunchy Bridge, …) via a libqp **URL-form** DSN
(`postgresql://user:pass@host:5432/dbname`; the libpq keyword form is not
accepted). The embedded-only knobs (`postgres.image`, `postgres.database`,
`postgres.user`, …) are ignored.

Supply the DSN one of two ways — `values.schema.json` fails `helm install` at
template time if mode is `external` and **neither** is set (no fictional default
host):

```yaml
# (a) inline — convenient for dev; URI-encode reserved chars in the password
postgres:
  mode: external
  external:
    connectionString: "postgresql://eden:s3cr3t@db.example.com:5432/eden"
```

```yaml
# (b) reference a pre-created Secret — recommended for production
postgres:
  mode: external
  external:
    existingSecret: eden-managed-postgres   # holds key EDEN_STORE_URL
    connectionStringKey: EDEN_STORE_URL
```

The managed database must be **empty** on first install; the task-store-server
runs the schema bootstrap (`ensure_schema`) on first connect — there is no
separate "run migrations" step. See §4.5.

**TLS** (mode=external only — the embedded StatefulSet runs stock Postgres with
no server-side TLS, so the schema rejects `tls.enabled=true` for `embedded`).
`postgres.tls.enabled=false` by default (also accommodates the GCP Cloud SQL Auth
Proxy, which is itself the secure transport). For a managed provider over the
network, enable it:

```yaml
postgres:
  tls:
    enabled: true
    mode: verify-full            # ∈ {require, verify-ca, verify-full}; weak modes rejected
    caBundleSecret: eden-rds-ca  # Secret with the provider CA bundle (PEM)
    caBundleKey: ca.crt
```

The chart appends `?sslmode=<mode>[&sslrootcert=/etc/eden/postgres-ca/<key>]` to
the DSN it composes and mounts the CA bundle into the task-store-server pod. The
chart can only append the suffix when it composes the DSN itself
(`external.connectionString` with chart-managed secrets). When the DSN lives in a
Secret the chart does not render — `postgres.external.existingSecret` **or** the
whole-chart `secrets.existingSecret` — you encode the TLS params into that DSN
yourself and set `postgres.external.tlsAlreadyEncodedInSecret: true` (the schema
requires the acknowledgement in both cases).

**Migration from embedded to external** (drain → `pg_dump` → `pg_restore` →
swap the value → `helm upgrade`), provider-specific CA-bundle guidance, the
Cloud SQL Auth Proxy combination, and the rollback path live in the
[managed-Postgres runbook](migrating-to-managed-postgres.md).

**Single-DSN posture (v0).** External mode points all three DSN keys
(`EDEN_STORE_URL`, `EDEN_READONLY_STORE_URL`, `EDEN_CONTROL_PLANE_STORE_URL`) at
the one operator DSN — separate readonly-role / control-plane-database
provisioning on a managed instance is the operator's concern, tracked in
[#299](https://github.com/ealt/eden/issues/299). External + lease mode is not a
supported v0 combination (lease mode is deferred behind #281).

**Connection pooler threshold.** 13c ships no pooler and needs none: the
task-store-server is single-replica with one psycopg connection per process, far
below any managed provider's connection limit. A pooler (PgBouncer in
transaction mode) becomes load-bearing only if `replicas.taskStoreServer` ever
exceeds 1 (out of scope for the 13-series). When you reach that point, point the
DSN at the pooler instead of the upstream — no chart change needed.

### 4.5 Schema management and the future migration-tool hand-off

The reference impl manages the Postgres schema with `ensure_schema` on
first connect: it creates a `schema_version` table, applies the linear migration
list, and is idempotent on re-run. v0 has one schema version, so both modes
bootstrap identically — and a `pg_dump`/`pg_restore` migration carries the
`schema_version` table across, so the next start sees the version up-to-date and
skips re-bootstrap.

This is deliberately **not** a standalone migration tool (Alembic/sqitch); that
lands with the first non-trivial schema migration. **Load-bearing constraint on
that future tool:** it MUST recognize a populated `schema_version` table as
"v1 already applied" and import the `ensure_schema`-applied state rather than
re-migrating — otherwise 13c-era deployments that never ran the operator-side
tool would break. (Tracked as a constraint here so the chunk introducing the
tool sees it up front.)

## 5. Operational notes

- **PVC retention.** StatefulSet `volumeClaimTemplates` PVCs are **not** deleted
  on `helm uninstall` (Kubernetes leaves them for manual cleanup). To fully tear
  down a deployment and its data: `helm uninstall eden -n eden-prod` then
  `kubectl delete pvc -n eden-prod --all`. Operators who only `helm uninstall`
  keep their Postgres / Forgejo / clone data for the next install.
- **Secrets + retained PVCs (reinstall hazard).** `helm uninstall` deletes the
  chart-managed `<release>-secrets` Secret, but the PVCs are retained. A
  reinstall with chart-managed (inline) secrets regenerates a fresh
  `POSTGRES_PASSWORD`, while the retained Postgres data dir still holds the
  **old** role password — so the new DSNs fail authentication (the same hazard
  as rotating the Compose `.env` password against an existing data volume). For
  any deployment you intend to uninstall **and reinstall against the same
  PVCs**, use `secrets.existingSecret` so the credentials outlive the release.
  Otherwise, wipe the PVCs (`kubectl delete pvc -n <ns> --all`) for a clean
  reinstall.
- **Scaling to zero.** Set `replicas.<service>=0` to idle a workload (e.g. pause
  the orchestrator). The StatefulSet keeps its PVCs, so scaling back up
  reattaches the per-replica clones. Postgres and Forgejo cannot scale to zero
  in v0.
- **Orchestrator lifecycle.** On Kubernetes the orchestrator runs forever:
  `config.maxQuiescentIterations=0` disables the quiescence exit, because a
  Deployment/StatefulSet only supports `restartPolicy: Always` and a clean
  quiescence exit would CrashLoopBackOff. The orchestrator ends only on SIGTERM
  (pod termination).
- **Orchestrator restarts (single-experiment default).** There is one
  orchestrator; while its pod restarts (a StatefulSet rollout, sub-second to a
  few seconds) no dispatch happens, then it resumes. The orchestrator verifies
  its provisioned credential via `/whoami` and self-joins the `orchestrators`
  group under the admin token on each start, so a restart is self-healing.
- **Scaling the orchestrator beyond one replica.** Not supported on the default
  path: a second replica sharing the single minted `worker_id` would contend for
  the same identity with no lease coordinator to arbitrate. Multi-replica HA is
  the job of lease mode (`orchestrator.leaseMode.enabled`), which is **deferred +
  unvalidated behind [#281](https://github.com/ealt/eden/issues/281)** — that
  issue covers reconciling the #128 per-replica opaque-id minting with #147's
  lease-mode orchestrator self-registration. Keep `replicas.orchestrator=1`.

## 6. Coexistence

- **Namespace per release.** Resource names are prefixed by the release name via
  `eden.fullname`, so two releases in **different** namespaces don't collide.
  Two releases in the **same** namespace will — use a distinct namespace per
  release.
- **Alongside Compose.** When running both a Compose stack and a Helm release on
  one machine, port-forward to offset ports (e.g. 18090 / 13001) to avoid
  Compose's published 8090 / 3001.

## 7. Upgrades

`helm upgrade eden reference/helm/eden -n eden-prod --reset-then-reuse-values`
(helm ≥ 3.14) rolls the workloads with the new chart/image. The PVCs (Postgres,
Forgejo, per-service clones) are retained across upgrades, so experiment state
survives. The `appVersion` in `Chart.yaml` is documentation only; the actual
image is whatever `image.tag` points at — keep them in sync to avoid drift.

Use `--reset-then-reuse-values`, **not** plain `--reuse-values`, when the chart
itself changed: `--reset-then-reuse-values` re-applies your previously supplied
values on top of the **new** chart's defaults, while `--reuse-values` discards
the new chart's `values.yaml` entirely — any value the new chart added (and its
templates reference) renders empty and the upgrade breaks. Plain
`--reuse-values` is fine for same-chart values tweaks (e.g. the §4.3 registry
example).

This procedure is validated in CI: the `helm-upgrade-smoke` job
([`ci-upgrade-smoke.sh`](../../reference/helm/eden/ci-upgrade-smoke.sh))
installs the chart at the merge-base with `main`, drives a live experiment,
upgrades in place to the PR's chart with the command above, and asserts the
experiment state survived.

## 8. Troubleshooting

- **`ErrImagePull`** — `image.repository`/`image.tag` point at an image the
  cluster can't pull. Confirm the image is pushed and `image.pullSecrets` is set
  for private registries. (Empty image values fail earlier, at `helm template`.)
- **PVC `Pending`** — no default `StorageClass`, or the requested size exceeds
  the provisioner's limit. `kubectl get pvc -n <ns>` and `kubectl describe pvc`.
- **task-store-server `CrashLoopBackOff`** — usually a Postgres connectivity or
  DSN issue. In `postgres.mode=embedded`, check the `eden-postgres-0` pod is
  `Ready` and that `EDEN_STORE_URL` (in the Secret) matches the Postgres
  credentials. In `mode=external` there is **no** `eden-postgres-0` pod — verify
  the managed instance is reachable from the cluster, the DSN/credentials are
  correct, TLS params match the provider (`sslmode`/`sslrootcert` + the CA bundle
  Secret), and check the task-store-server logs
  (`kubectl logs deployment/eden-task-store-server -n <ns>`).
- **No `variant.integrated` events** — confirm the orchestrator is running
  (`kubectl logs statefulset/eden-orchestrator -n <ns>`) and that each
  identity-consuming pod's `provision-credential` initContainer succeeded
  (`kubectl get pod -n <ns>`; a pod stuck in `Init:` means its identity Secret
  or token is missing). The worker hosts verify their provisioned `worker_id`
  via `/whoami` at startup (no self-registration under #128). The Forgejo `eden`
  user + repo must exist — re-run `setup-experiment-helm.sh`.
- **Reproduce the CI smoke locally** — with kind + helm + kubectl + docker + jq +
  python3 installed, run `bash reference/helm/eden/ci-smoke.sh`.

## 9. What this chart does not cover yet

GPU executor as a k8s Job (13b), S3/GCS blob backend (13d), Forgejo auth +
per-branch ACLs + native PR review (13e), and `--mode subprocess` + DooD worker
hosts (a later 13 sub-chunk). The artifact store is a web-ui-owned
`ReadWriteOnce` PVC; cross-service artifact serving needs a `ReadWriteMany` or
external blob backend (13d). Operators who need user-supplied LLM workers via
subprocess + DooD stay on the Compose stack until 13b lands.

Managed external Postgres **is** covered as of 13c — see §4.4 and the
[managed-Postgres runbook](migrating-to-managed-postgres.md).
