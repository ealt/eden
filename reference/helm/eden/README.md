# EDEN reference Helm chart

A Helm chart that brings up the EDEN reference deployment on Kubernetes —
the same nine services that the reference Compose stack at
[`reference/compose/`](../../compose/) packages for `docker compose`, but
targeting any conforming Kubernetes cluster (kind, k3s, EKS, GKE, AKS, …).

The chart is **parallel** to the Compose deployment, not a replacement. Both
are first-class reference deployment substrates in v0. Operators pick
whichever fits their environment. See [`docs/deployment/helm.md`](../../../docs/deployment/helm.md)
for the full deployment guide.

## Status

Phase 13a, base chart. Subsequent Phase 13 chunks layer:

- **13b** — executor as Kubernetes Job (GPU node selection, per-task isolation).
- **13c** — managed Postgres (RDS / Cloud SQL connection-string knob).
- **13d** — S3 / GCS blob backend for the artifacts substrate.
- **13e** — Gitea hardening (per-branch ACLs, native PR review).

## What this chart deploys

| Workload | Kind | Replicas (default) | Notes |
|---|---|---|---|
| `task-store-server` | Deployment | 1 | Singleton in v0; clustered in 13c. |
| `control-plane`     | Deployment | 1 | 12c control plane. |
| `orchestrator`      | StatefulSet | 2 | HA via 12c leases; per-replica clone PVC. |
| `ideator-host`      | Deployment | 1 | No git clone (does not touch refs). |
| `executor-host`     | StatefulSet | 1 | Per-replica clone PVC. |
| `evaluator-host`    | StatefulSet | 1 | Per-replica clone PVC. |
| `web-ui`            | StatefulSet | 1 | Per-replica clone PVC + artifacts PVC. |
| `postgres`          | StatefulSet | 1 | Bundled stopgap (13c migrates). |
| `gitea`             | StatefulSet | 1 | Bundled stopgap (13e hardens). |

The StatefulSet / Deployment split follows from the substrate baseline:
orchestrator, executor-host, evaluator-host, and web-ui each hold their own
local clone of the experiment's bare repo. Multi-replica deployments need
one PVC per replica, which `StatefulSet.volumeClaimTemplates` provides
automatically.

## Prerequisites

- Kubernetes 1.27+.
- Helm 3.x.
- A default `StorageClass` configured on the cluster (or override
  `storage.className` in values).
- A container image registry the cluster can pull from, with a built +
  pushed `eden-reference` image. The chart's `values.schema.json` enforces
  `image.repository` and `image.tag` as non-empty — `helm install` fails
  loudly if either is missing.
- For Ingress: an ingress controller (chart-agnostic; operator's choice).

## Image build

The reference Compose stack builds `eden-reference:dev` as a local
Dockerfile target ([`reference/compose/Dockerfile`](../../compose/Dockerfile)).
A Kubernetes cluster cannot pull that local image. Operators must build +
push the image to a registry they control:

```bash
docker build -f reference/compose/Dockerfile -t ghcr.io/your-org/eden-reference:0.1.0 .
docker push ghcr.io/your-org/eden-reference:0.1.0
```

Then reference it in `values.yaml`:

```yaml
image:
  repository: ghcr.io/your-org/eden-reference
  tag: "0.1.0"
```

## Quickstart

```bash
# 1) Create a values file with required secrets + image.
cat > my-values.yaml <<'EOF'
image:
  repository: ghcr.io/your-org/eden-reference
  tag: "0.1.0"
secrets:
  adminToken: "change-me"
  postgresPassword: "change-me"
  giteaAdminPassword: "change-me"
  giteaRemotePassword: "change-me"
  webUiSessionSecret: "change-me"
EOF

# 2) Install the chart.
helm install eden ./reference/helm/eden \
  -f my-values.yaml \
  --create-namespace \
  --namespace eden-dev \
  --wait --timeout 5m

# 3) Wait for stateful services to come up.
kubectl -n eden-dev rollout status statefulset/eden-postgres
kubectl -n eden-dev rollout status statefulset/eden-gitea
kubectl -n eden-dev rollout status deployment/eden-task-store-server
kubectl -n eden-dev rollout status deployment/eden-control-plane

# 4) Bootstrap an experiment.
bash reference/scripts/setup-experiment-helm.sh \
  --namespace eden-dev \
  --experiment-config tests/fixtures/experiment/.eden/config.yaml \
  --experiment-id exp-1

# 5) Reach the Web UI.
kubectl -n eden-dev port-forward svc/eden-web-ui 18090:8090
# Open http://localhost:18090
```

To bootstrap a second experiment later, run `setup-experiment-helm.sh`
again with a different `--experiment-id`; each experiment gets its own
ad-hoc repo-init Job, independent of the chart's lifecycle.

## Values reference

See [`values.yaml`](values.yaml) for the full default values with inline
documentation, and [`values.schema.json`](values.schema.json) for the
JSON-Schema-enforced shape. Field naming mirrors
[`reference/compose/.env.example`](../../compose/.env.example) keys
where applicable (camelCase per Helm convention).

### Required values

| Key | Description |
|---|---|
| `image.repository` | Container image repository (operator-built; chart cannot use Compose's local `eden-reference:dev`). |
| `image.tag` | Container image tag. |
| `secrets.adminToken` | EDEN admin token (chapter 7 §13). Required unless `secrets.existingSecret` is set. |
| `secrets.postgresPassword` | Postgres password (used in connection string and bundled Postgres init). |
| `secrets.giteaAdminPassword` | Gitea admin user password. |
| `secrets.giteaRemotePassword` | HTTP Basic credential workers use against Gitea (per the git-credential-helper wiring in §3.3a of the plan). |
| `secrets.webUiSessionSecret` | Web UI session-cookie signing key. |

### Production secrets

For production, pre-create a Secret externally (Sealed Secrets, External
Secrets Operator, Vault, etc.) with the same key shape and set
`secrets.existingSecret` to its name. The chart's templates use
`envFrom: secretRef:` so no secret values flow through Helm itself.

### Ingress

`ingress.enabled` is `false` by default. To expose the Web UI and Gitea:

```yaml
ingress:
  enabled: true
  className: nginx                       # or your controller's class
  hosts:
    webUi: eden-webui.example.com
    gitea: eden-gitea.example.com
  tls:
    - hosts: [eden-webui.example.com]
      secretName: eden-webui-tls
```

### Resource tuning

Each service exposes a `resources` map under `resources.<service>`. Empty
maps (the default) disable both `requests` and `limits`. Operators tune per
cluster.

## Upgrades

```bash
helm upgrade eden ./reference/helm/eden -f my-values.yaml -n eden-dev
```

StatefulSet PVCs are **not** deleted on `helm uninstall` (Kubernetes design);
data survives release reinstall as long as the namespace + PVCs are still
there. To fully tear down:

```bash
helm uninstall eden -n eden-dev
kubectl delete pvc --all -n eden-dev
kubectl delete namespace eden-dev
```

## Coexistence with the Compose deployment

The chart and the Compose stack are independent. An operator can run BOTH on
the same machine (Compose on local Docker, Helm on a local kind/k3s
cluster). Some rules:

- Each `helm install` carries an operator-chosen release name + namespace.
  Resource names are prefixed with `<release>-eden-` so two releases in
  different namespaces don't collide.
- Two releases in the SAME namespace WILL collide on Service / StatefulSet
  names — use distinct namespaces per release.
- The chart's `kubectl port-forward` examples use ports offset by 10000
  (18090 / 13001) to avoid conflicts with the Compose stack's default
  host ports (8090 / 3001).
- Ingress hostnames: if a release shares a hostname with the Compose stack
  or another release, the ingress controller serves whichever was created
  first.

## Out of scope for 13a

- **GPU node selection on the executor** — 13b.
- **Managed Postgres** — 13c.
- **S3 / GCS artifact backend** — 13d.
- **Gitea auth changes (per-branch ACLs, PR review)** — 13e.
- **Subprocess workers (`--mode subprocess`)** — deferred to 13b. The chart
  ships only `--mode scripted` workers in v0; user-supplied LLM workers
  via Compose's subprocess + DooD mode stay on the Compose stack until 13b
  lands.
- **Operator pattern (CRDs, controller)**. v0 stays Helm-vanilla; bootstrap
  uses `kubectl apply`-ed ad-hoc Jobs via [`bootstrap/repo-init-job.yaml.tmpl`](bootstrap/repo-init-job.yaml.tmpl).
- **Operator chart packaging (Helm OCI registry push)**. Chart lives in the
  repo as source; operators clone or vendor.

See [`docs/deployment/helm.md`](../../../docs/deployment/helm.md) for the
operator-facing deployment guide and [`docs/plans/eden-phase-13a-helm-base-chart.md`](../../../docs/plans/eden-phase-13a-helm-base-chart.md)
for the chunk plan.
