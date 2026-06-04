# EDEN reference Helm chart

The Helm packaging of the EDEN reference deployment — the same nine services
the [Compose stack](../../compose/) runs (task-store-server, control-plane,
orchestrator, ideator/executor/evaluator hosts, web-ui, Forgejo, Postgres),
deployable on any conforming Kubernetes cluster (1.27+).

This chart is **parallel** to the Compose deployment, not a replacement. Both
are first-class v0 substrates; Compose is the local-development path, Helm is
the production-ish path. See [`docs/deployment/helm.md`](../../../docs/deployment/helm.md)
for the full operator guide; this README is the chart-local quick reference.

## Prerequisites

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
repo, brings the stack up in two phases, and registers the experiment:

```sh
bash reference/scripts/setup-experiment-helm.sh \
  --namespace eden-prod \
  --release eden \
  --image <registry>/eden-reference:<tag> \
  --experiment-config tests/fixtures/experiment/.eden/config.yaml \
  --experiment-id exp-1
```

Then reach the Web UI (port-forward offset to avoid Compose's 8090):

```sh
kubectl -n eden-prod port-forward svc/eden-web-ui 18090:8090
open http://localhost:18090/
```

### Why two phases

The reference task-store-server is single-experiment per process and records
its **seed commit** at first experiment-row creation; a row created without a
seed never acquires a baseline. So the **app tier** (task-store-server,
orchestrator, worker hosts, web-ui) must not start until the seed SHA is known.
`experiment.baseCommitSha` is the bootstrap gate: the app tier renders only when
it is non-empty. `setup-experiment-helm.sh` therefore:

1. `helm upgrade --install` with `baseCommitSha` empty → only the **infra tier**
   (Postgres, Forgejo, control-plane) comes up.
2. Provisions the Forgejo `eden` user, seeds the bare repo via a one-shot Job
   (the push auto-creates the repo), reads the seed SHA, then
   `helm upgrade --set experiment.baseCommitSha=<sha>` → the app tier comes up.
3. Registers the experiment with the control plane and bootstraps the reserved
   groups + initial admin / web-ui workers.

Operators who pre-seeded Forgejo can skip the script and supply
`experiment.baseCommitSha` directly to a single `helm install`.

## Values

| Key | Default | Notes |
|---|---|---|
| `image.repository` / `image.tag` | `""` (required) | The pushed reference image. |
| `image.pullPolicy` | `IfNotPresent` | `Never` for kind-loaded images. |
| `image.pullSecrets` | `[]` | `[{name: <secret>}]` for private registries. |
| `experiment.id` | `""` (required) | The single experiment this release hosts. |
| `experiment.config` | fixture | Inline experiment-config YAML → ConfigMap. |
| `experiment.baseCommitSha` | `""` | App-tier gate; set by the setup script. |
| `replicas.orchestrator` | `2` | HA per the chapter 11 lease model. |
| `replicas.*` | `1` | Per-service replica counts. |
| `secrets.*` | `""` | Dev: inline; prod: `secrets.existingSecret`. |
| `storage.className` | `""` | Cluster default `StorageClass` if empty. |
| `storage.*Size` | see values | PVC sizes (Postgres, Forgejo, per-service clones, artifacts). |
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
- **Orchestrator restarts.** A rolling restart of the orchestrator StatefulSet
  briefly un-leases the experiment; `replicas.orchestrator = 2` keeps a standby
  available to re-acquire within `leaseDurationSeconds`.

## Scope (13a)

In scope: the base chart + the `--mode scripted` worker hosts + an embedded
Postgres / Forgejo. Out of scope (later 13 sub-chunks): GPU executor as a k8s
Job (13b), managed Postgres (13c), S3/GCS blob backend (13d), Forgejo auth +
per-branch ACLs (13e), and `--mode subprocess` + DooD worker hosts. The artifact
store is a web-ui-owned RWO PVC in 13a; cross-service artifact serving needs a
RWX/blob backend (13d).
