# EDEN reference Helm chart

The Helm packaging of the EDEN reference deployment — the EDEN services the
[Compose stack](../../compose/) runs (task-store-server, orchestrator,
ideator/executor/evaluator hosts, web-ui, Forgejo, Postgres), deployable on any
conforming Kubernetes cluster (1.27+). The **default** deployment runs the
single-experiment path (one orchestrator, no control plane), mirroring the
Compose default stack that `compose-smoke` validates. Lease-driven HA (a
control plane plus multiple contending orchestrator replicas) is an opt-in
toggle — `orchestrator.leaseMode.enabled` — that is **deferred + unvalidated
behind [#281](https://github.com/ealt/eden/issues/281)**; leave it off.

This chart is **parallel** to the Compose deployment, not a replacement. Both
are first-class v0 substrates; Compose is the local-development path, Helm is
the production-ish path. See [`docs/deployment/helm.md`](../../../docs/deployment/helm.md)
for the full operator guide; this README is the chart-local quick reference.

## Prerequisites

**On AWS (EKS):**
[`reference/scripts/setup-aws/setup-aws.sh`](../../scripts/setup-aws/setup-aws.sh)
provisions everything below create-if-absent (EKS cluster + OIDC + EBS CSI
addon, ECR repo + image build/push, RDS Postgres or an existing DSN, S3
bucket + IRSA role) and emits the exact `setup-experiment-helm.sh`
invocation, ready to run. Re-running converges; `--dry-run` prints the full
mutation plan without touching the account (issue
[#309](https://github.com/ealt/eden/issues/309)). The manual checklist
below remains the substrate-agnostic fallback (and the path for every other
cloud / on-prem cluster).

- A Kubernetes cluster (1.27+) with a default `StorageClass` that provisions
  `ReadWriteOnce` PVCs, plus `kubectl` + `helm` (3.x) configured against it.
- The `eden-reference` image **built and pushed** to a registry the cluster
  can pull from. The chart does **not** build the image — the Compose stack
  builds `eden-reference:dev` as a local Dockerfile target
  ([`reference/compose/Dockerfile`](../../compose/Dockerfile)), which a cluster
  cannot pull. Build + push it yourself:

  ```sh
  docker build -t <registry>/eden-reference:<tag> \
    -f reference/compose/Dockerfile .
  docker push <registry>/eden-reference:<tag>
  ```

`image.repository` and `image.tag` are **required** — the chart fails at
template time (via `values.schema.json`) if either is empty, rather than at
pod-pull time with a confusing `ErrImagePull`.

## Quickstart

The canonical bootstrap is `setup-experiment-helm.sh`, which seeds the bare
repo, mints the experiment's opaque worker identities, and brings the stack up
in three phases. Omit `--experiment-id` to mint a fresh opaque `exp_*` id:

```sh
bash reference/scripts/setup-experiment-helm.sh \
  --namespace eden-prod \
  --release eden \
  --image <registry>/eden-reference:<tag> \
  --experiment-config tests/fixtures/experiment/.eden/config.yaml
```

Then reach the Web UI (port-forward offset to avoid Compose's 8090):

```sh
kubectl -n eden-prod port-forward svc/eden-web-ui 18090:8090
open http://localhost:18090/
```

### Why three phases

Two facts shape the bootstrap. First, the reference task-store-server is
single-experiment per process and records its **seed commit** at first
experiment-row creation, so the task-store-server must not start until the seed
SHA is known. Second, under the [#128](https://github.com/ealt/eden/issues/128)
opaque-id model the task-store-server **mints** every worker id (`wkr_*`) — the
orchestrator, worker hosts, and web-ui cannot start until their minted
`worker_id` exists. So `setup-experiment-helm.sh`:

1. `helm upgrade --install` with `baseCommitSha` empty → only the **infra tier**
   (Postgres, Forgejo) comes up.
2. Provisions the Forgejo `eden` user, seeds the bare repo via a one-shot Job
   (the push auto-creates the repo), reads the seed SHA, then
   `helm upgrade --set experiment.baseCommitSha=<sha>` → the **task-store-server**
   comes up (its render gate is `baseCommitSha`).
3. Mints the reserved groups (`admins`, `orchestrators`) + one worker per
   service (`POST {"name": ...}`, capturing each minted `worker_id` +
   one-time `registration_token`), then `helm upgrade` with the minted
   `identity.*` values → the **app tier** (orchestrator, worker hosts, web-ui)
   comes up with pre-provisioned credentials. Each identity-consuming service
   renders only once its `identity.<svc>.workerId` is set.

The chart hands each pod its minted `worker_id` as `--worker-id` and renders a
per-service Secret carrying the token, which an initContainer installs at
`/var/lib/eden/credentials/<workerId>.token`; the service's startup credential
bootstrap verifies it via `/whoami` (no admin reissue). The minted ids + tokens
are stored in the release values, so re-running the script reuses them rather
than minting duplicates. `setup-experiment-helm.sh` is the canonical, supported
bootstrap — a plain `helm install` cannot mint the server-side identities.

## Values

| Key | Default | Notes |
|---|---|---|
| `image.repository` / `image.tag` | `""` (required) | The pushed reference image. |
| `image.pullPolicy` | `IfNotPresent` | `Never` for kind-loaded images. |
| `image.pullSecrets` | `[]` | `[{name: <secret>}]` for private registries. |
| `experiment.id` | `""` (required) | Opaque `exp_*` id; minted by the setup script when `--experiment-id` is omitted. |
| `experiment.config` | fixture | Inline experiment-config YAML → ConfigMap. |
| `experiment.baseCommitSha` | `""` | Store-tier gate; set by the setup script. |
| `identity.<svc>.workerId` / `.token` | `""` | Minted worker id + token per service; set by the setup script (app-tier gate). |
| `orchestrator.leaseMode.enabled` | `false` | Opt-in lease-driven HA + control plane. Deferred + unvalidated behind #281. |
| `replicas.orchestrator` | `1` | Single-experiment. Raising it needs lease mode + per-replica identities (#281). |
| `replicas.*` | `1` | Per-service replica counts. |
| `secrets.*` | `""` | Dev: inline; prod: `secrets.existingSecret`. |
| `storage.className` | `""` | Cluster default `StorageClass` if empty. |
| `storage.*Size` | see values | PVC sizes (Postgres, Forgejo, per-service clones, artifacts). |
| `blob.backend` | `file` | Where the §16 artifact deposit endpoint persists bytes: `file` (chart-managed PVC), `s3`, or `gcs` (issue #174). Invisible on the wire (`eden://artifacts/<id>`); only the task-store-server pod touches the backend. |
| `blob.file.size` / `.className` / `.mountPath` | `10Gi` / `""` / `/var/lib/eden/blob` | File-mode blob PVC (annotated `helm.sh/resource-policy: keep`). |
| `blob.s3.bucket` | `""` (required when `backend=s3`) | Plus exactly one auth path: `blob.s3.irsa.{enabled,roleArn}` (pod identity) or `blob.s3.existingSecret` (static keys). `blob.s3.endpointUrl` targets MinIO / S3-compatibles. |
| `blob.gcs.bucket` | `""` (required when `backend=gcs`) | Plus exactly one auth path: `blob.gcs.workloadIdentity.{enabled,serviceAccount}` or `blob.gcs.existingSecret` (key JSON). |
| `ingress.enabled` | `false` | Web UI Ingress; operator picks the controller. |
| `config.maxQuiescentIterations` | `0` | `0` = never exit on quiescence (k8s; Decision 9). |
| `config.workerMode` | `scripted` | Only `scripted` in 13a (Decision 10). |

See [`values.yaml`](values.yaml) for the full annotated surface and
[`values.schema.json`](values.schema.json) for the validation rules.

### Secrets

- **Dev**: set `secrets.adminToken`, `secrets.postgresPassword`, etc. inline;
  the chart materializes `<release>-secrets`. The Postgres password is
  interpolated into the DSNs verbatim, so it must be URL-safe (the setup script
  generates hex).
- **Prod**: pre-create the Secret externally (Sealed Secrets / External Secrets
  / Vault) and set `secrets.existingSecret`. It must carry every key the
  chart-managed Secret would — see the header comment in
  [`templates/secret.yaml`](templates/secret.yaml).

## Operational notes

- **PVC retention.** StatefulSet `volumeClaimTemplates` PVCs are **not**
  deleted on `helm uninstall`. To fully tear down: `helm uninstall <release> -n
  <ns>` then `kubectl delete pvc -n <ns> --all`. Note: `helm uninstall` DOES
  delete the chart-managed Secret while keeping the PVCs, so a reinstall with
  regenerated secrets can't authenticate against the retained Postgres data —
  use `secrets.existingSecret` for reinstallable deployments (see
  [`docs/deployment/helm.md`](../../../docs/deployment/helm.md) §5).
- **Coexistence.** Use a distinct namespace per release; two releases in the
  same namespace collide on resource names. When running alongside the Compose
  stack on one machine, port-forward to offset ports (e.g. 18090) to avoid
  Compose's published 8090 / 3001.
- **Scaling to zero.** Set `replicas.<service> = 0` to idle a workload; the PVCs
  are retained, so scaling back up reattaches the per-replica clones.
- **Orchestrator restarts.** In the default single-experiment path there is one
  orchestrator; while its pod restarts no dispatch happens (sub-second to a few
  seconds on a StatefulSet rollout). Standby-replica failover needs lease mode,
  which is deferred behind #281.
- **Blob backend migration.** Switching `blob.backend` between `file` and a
  bucket (or between buckets) requires copying the deposited bytes first —
  objects are keyed by opaque id, so the layout maps 1:1. See
  [`docs/deployment/migrating-to-blob-backend.md`](../../../docs/deployment/migrating-to-blob-backend.md).

## Scope (13a)

In scope: the base chart + the `--mode scripted` worker hosts + an embedded
Postgres / Forgejo. The 13d S3/GCS blob backend has since landed (issue #174;
the `blob.*` values above). Still out of scope (later 13 sub-chunks): GPU
executor as a k8s Job (13b), managed Postgres (13c), Forgejo auth +
per-branch ACLs (13e), and `--mode subprocess` + DooD worker hosts. The legacy
`--artifacts-dir` store remains a web-ui-owned RWO PVC; the §16 deposit
endpoint's blob store is the task-store-server-owned `blob.*` backend, and
cross-service artifact access goes over the wire (`eden://artifacts/<id>`).
