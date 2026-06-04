# Deploying EDEN on Kubernetes with Helm

This guide covers deploying the EDEN reference services on a Kubernetes cluster
via the Helm chart at [`reference/helm/eden/`](../../reference/helm/eden/). It is
the Kubernetes analogue of the Compose deployment documented in
[`reference/compose/README.md`](../../reference/compose/README.md).

The Helm deployment is **parallel** to Compose, not a replacement. Both are
first-class v0 substrates running the full Phase-12 protocol (workers + groups +
leases + control plane + portable checkpoints). Compose is the
local-development path; Helm is the production-ish path. The two are independent
deployments — you cannot "upgrade" a Compose stack to Helm without exporting via
[12b portable checkpoints](../../reference/compose/healthcheck/smoke-checkpoint.sh)
and re-importing.

## 1. Prerequisites

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
It runs the install in two phases (see §3) and registers the experiment:

```sh
bash reference/scripts/setup-experiment-helm.sh \
  --namespace eden-prod \
  --release eden \
  --image <registry>/eden-reference:<tag> \
  --experiment-config tests/fixtures/experiment/.eden/config.yaml \
  --experiment-id exp-1
```

What it does:

1. `helm upgrade --install` with `experiment.baseCommitSha` empty → the infra
   tier (Postgres, Forgejo, control-plane) comes up.
2. Provisions the Forgejo `eden` user (idempotent) and seeds the bare repo via
   a one-shot Job; the push auto-creates the experiment repo on Forgejo.
3. `helm upgrade --set experiment.baseCommitSha=<seed-sha>` → the app tier
   (task-store-server, orchestrator, worker hosts, web-ui) comes up.
4. Registers the experiment with the control plane and bootstraps the reserved
   `orchestrators` / `admins` groups + the initial admin and web-ui workers.

When no `--values` file is supplied, the script generates fresh dev secrets
(read back from the cluster Secret on re-run, so re-running is idempotent) and
requires `--image`. Supply `--values <file>` (e.g. for production secrets via
`existingSecret`) to drive secrets yourself.

**One experiment per release in v0.** The reference task-store-server is
single-experiment per process, and the release-wide `experiment.id` value points
the whole stack at one experiment. Do **not** re-run the script with a different
`--experiment-id` against the same release — that rewrites `experiment.id`,
re-points the single task-store/worker stack at the new experiment, and orphans
the first (it stays registered with the control plane but is no longer served).
To run a **second** experiment, install a **separate release in a separate
namespace** (e.g. `--release eden-b --namespace eden-b`). True
multiple-experiments-per-release hosting is tracked in
[#254](https://github.com/ealt/eden/issues/254).

Reach the Web UI:

```sh
kubectl -n eden-prod port-forward svc/eden-web-ui 18090:8090
open http://localhost:18090/
```

### Plain `helm install`

Operators who have already seeded Forgejo and know the seed SHA can skip the
script:

```sh
helm install eden reference/helm/eden \
  --namespace eden-prod --create-namespace \
  -f my-values.yaml \
  --set image.repository=<registry>/eden-reference \
  --set image.tag=<tag> \
  --set experiment.id=exp-1 \
  --set experiment.baseCommitSha=<seed-sha>
```

## 3. The two-phase bootstrap (why)

The reference task-store-server is single-experiment per process and records the
experiment's **seed commit** at first experiment-row creation — a row created
without a seed never acquires a baseline (02-data-model.md §9.4), and the seed is
ignored on a later reopen. So the app tier must not start before the seed SHA is
known.

`experiment.baseCommitSha` is the gate: the app-tier templates render only when
it is non-empty (`{{ include "eden.appEnabled" . }}`). This is deliberate
bootstrap gating, distinct from scaling a service to zero (§5) — during phase 1
nothing references the app-tier Services, so their absence is harmless.

## 4. Values reference

See [`reference/helm/eden/values.yaml`](../../reference/helm/eden/values.yaml)
for the fully-annotated surface and the
[chart README](../../reference/helm/eden/README.md) for the summary table. Key
groups:

- **`image`** — required repository + tag; `pullPolicy`; `pullSecrets`.
- **`experiment`** — `id`, inline `config` (→ ConfigMap), `baseCommitSha`.
- **`replicas`** — per-service counts; orchestrator defaults to 2 (HA).
- **`secrets`** — dev inline or `existingSecret` (§4.1).
- **`storage`** — `className` + per-PVC sizes.
- **`ingress`** — Web UI Ingress, off by default (§4.2).
- **`config`** — Compose-mirror knobs (lease duration, quiescence, worker mode).

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

## 5. Operational notes

- **PVC retention.** StatefulSet `volumeClaimTemplates` PVCs are **not** deleted
  on `helm uninstall` (Kubernetes leaves them for manual cleanup). To fully tear
  down a deployment and its data: `helm uninstall eden -n eden-prod` then
  `kubectl delete pvc -n eden-prod --all`. Operators who only `helm uninstall`
  keep their Postgres / Forgejo / clone data for the next install.
- **Scaling to zero.** Set `replicas.<service>=0` to idle a workload (e.g. pause
  the orchestrator). The StatefulSet keeps its PVCs, so scaling back up
  reattaches the per-replica clones. Postgres and Forgejo cannot scale to zero
  in v0.
- **Orchestrator lifecycle.** On Kubernetes the orchestrator runs forever:
  `config.maxQuiescentIterations=0` disables the quiescence exit, because a
  Deployment/StatefulSet only supports `restartPolicy: Always` and a clean
  quiescence exit would CrashLoopBackOff. The orchestrator ends only on SIGTERM
  (pod termination).
- **Rolling restarts vs leases.** `kubectl rollout restart
  statefulset/eden-orchestrator` terminates pods one at a time; each terminating
  pod's lease expires, leaving a sub-`leaseDurationSeconds` window where the
  experiment is un-leased. `replicas.orchestrator=2` keeps a standby available
  to re-acquire during the restart.

## 6. Coexistence

- **Namespace per release.** Resource names are prefixed by the release name via
  `eden.fullname`, so two releases in **different** namespaces don't collide.
  Two releases in the **same** namespace will — use a distinct namespace per
  release.
- **Alongside Compose.** When running both a Compose stack and a Helm release on
  one machine, port-forward to offset ports (e.g. 18090 / 13001) to avoid
  Compose's published 8090 / 3001.

## 7. Upgrades

`helm upgrade eden reference/helm/eden -n eden-prod --reuse-values` rolls the
workloads with the new chart/image. The PVCs (Postgres, Forgejo, per-service
clones) are retained across upgrades, so experiment state survives. The
`appVersion` in `Chart.yaml` is documentation only; the actual image is whatever
`image.tag` points at — keep them in sync to avoid drift.

## 8. Troubleshooting

- **`ErrImagePull`** — `image.repository`/`image.tag` point at an image the
  cluster can't pull. Confirm the image is pushed and `image.pullSecrets` is set
  for private registries. (Empty image values fail earlier, at `helm template`.)
- **PVC `Pending`** — no default `StorageClass`, or the requested size exceeds
  the provisioner's limit. `kubectl get pvc -n <ns>` and `kubectl describe pvc`.
- **task-store-server `CrashLoopBackOff`** — usually a Postgres connectivity or
  DSN issue. Check the `eden-postgres-0` pod is `Ready` and that
  `EDEN_STORE_URL` (in the Secret) matches the Postgres credentials.
- **No `variant.integrated` events** — confirm the orchestrator holds the lease
  (`kubectl logs statefulset/eden-orchestrator -n <ns>`) and the worker hosts
  registered (they self-register via the admin token on startup). The Forgejo
  `eden` user + repo must exist — re-run `setup-experiment-helm.sh`.
- **Reproduce the CI smoke locally** — with kind + helm + kubectl + docker + jq +
  python3 installed, run `bash reference/helm/eden/ci-smoke.sh`.

## 9. What 13a does not cover

GPU executor as a k8s Job (13b), managed external Postgres (13c), S3/GCS blob
backend (13d), Forgejo auth + per-branch ACLs + native PR review (13e), and
`--mode subprocess` + DooD worker hosts (a later 13 sub-chunk). The artifact
store is a web-ui-owned `ReadWriteOnce` PVC; cross-service artifact serving needs
a `ReadWriteMany` or external blob backend (13d). Operators who need
user-supplied LLM workers via subprocess + DooD stay on the Compose stack until
13b lands.
