# Phase 13b — Executor as a k8s Job (GPU node selection)

**Status.** Draft.

**Predecessor.** [`docs/plans/eden-phase-13a-helm-base-chart.md`](eden-phase-13a-helm-base-chart.md)
(merged). 13a established the base Helm chart with the executor-host
running as a long-running StatefulSet that calls user `*_command` only
in `--mode scripted`. Decision 10 of 13a explicitly deferred the
`--mode subprocess` and `--exec-mode docker` paths to a later 13
chunk because a Kubernetes-native packaging mechanism was needed:
DooD's `/var/run/docker.sock` mount is non-portable across containerd
/ cri-o clusters, and `--mode subprocess`'s shared-`worktrees` PVC
assumption doesn't translate to per-pod scheduling. 13b is that
chunk.

**Roadmap.** [`docs/roadmap.md`](../roadmap.md) §"Phase 13 —
Kubernetes reference deployment" lists the unit as "Executor as a
k8s Job (GPU node selection)". 13b delivers BOTH halves: the
Job-per-execution-task workload kind, and the GPU scheduling knobs
that motivate it.

**Naming.** Pre-draft check against
[`docs/glossary.md`](../glossary.md) and AGENTS.md "Naming discipline":

- The role / verb / kind / submission alignment is unchanged: the
  *executor* role claims *execution* tasks and produces *variant*
  artifacts via a `VariantSubmission`. Nothing about Job-mediated
  execution renames any of those terms.
- "Job" is Kubernetes vocabulary (`batch/v1` `Job` resource) and
  has no collision with EDEN identifiers. The chart's value is
  `executor.mode` ∈ {`scripted`, `k8sJob`}; the camelCase enum
  member matches Helm convention.
- The new CLI flag is `--mode k8s-job` (kebab-case for
  argparse, mirrors the existing `--mode scripted` /
  `--mode subprocess` shape).
- The wrapper binary that runs inside the Job pod is
  `eden-execute-wrapper` — verb-noun shape per the glossary's
  `submit_variant` precedent (artifact noun preferred over
  the role verb in helper names; here "execute" is the verb the
  wrapper is performing inside the pod, so it follows the
  `evaluation_command` / `execution_command` pattern from the
  fixture YAML).
- No EDEN protocol vocabulary changes. The `script-rename-discipline`
  guardrail catches legacy patterns; pre-submit run is clean.

## 1. Context

### 1.0 Substrate baseline (post-13a)

After 13a, the reference k8s deployment runs:

- `executor-host` as a `StatefulSet` (per 13a Decision 3 — each
  replica holds a per-replica local clone PVC).
- Mode is `--mode scripted` (per 13a Decision 10 — subprocess +
  DooD are out of scope for 13a).
- Compose deployment retains all four worker-host modes
  (`scripted`, `subprocess`, `subprocess`+`exec-mode docker`).
- The chart's `executor.mode` value does not yet exist; the
  StatefulSet is hard-coded to scripted-mode args.

13b's job is to add a fourth executor mode — `--mode k8s-job` — that
is **only available on the Helm/k8s substrate**, and to expose the
GPU-scheduling knobs (`nodeSelector`, `tolerations`,
`resources.limits.nvidia.com/gpu`) that motivated the chunk.

### 1.1 What 13b changes

13b adds:

1. A new `--mode k8s-job` to `eden-executor-host` that, instead of
   running the user `execution_command` in-process or as a
   subprocess on the host pod, **creates one Kubernetes `Job` per
   execution task** in the same namespace. The Job's pod runs the
   user's command in a per-task pod that k8s schedules
   independently.
2. The chart's executor StatefulSet grows an `executor.mode`
   value; when set to `k8sJob`, the chart additionally renders a
   `ServiceAccount`, `Role`, and `RoleBinding` granting the
   executor pod RBAC to create/get/delete `Job` and `Pod` (in the
   release namespace only).
3. Helm values for the per-Job pod template:
   `executor.jobTemplate.image.repository`,
   `executor.jobTemplate.image.tag`,
   `executor.jobTemplate.nodeSelector`,
   `executor.jobTemplate.tolerations`,
   `executor.jobTemplate.resources` (the GPU surface).
4. A new informative reference-binding chapter at
   [`spec/v0/reference-bindings/worker-host-k8s-job.md`](../../spec/v0/reference-bindings/worker-host-k8s-job.md)
   documenting the protocol-level shape of Job-mediated
   execution. The chapter is **informative** — chapter 3 is
   binding-agnostic, so 13b changes no normative spec text.
5. A new CI job `helm-smoke-executor-job` that mirrors `helm-smoke`
   from 13a but pins `executor.mode=k8sJob` and asserts the
   end-state still matches (≥3 `variant.integrated` events,
   ≥9 `task.completed` events, ≥3 ideation-task `task.completed`
   events).

13b does NOT change the Compose stack. Compose users keep
`--mode subprocess` + `--exec-mode docker`; they don't lose
anything.

### 1.2 Spec baseline + reconciliation

13b touches no normative spec text. Chapter 3 §3.2 step 1 (variant
created with `status="starting"` BEFORE any observable repo write)
constrains the design — the host-side `Store.create_variant` call
MUST run before the Job is created, since the Job pod's git push
to Forgejo is itself an observable repo write. The Job pod's wrapper
performs the push only after the user command produces a commit;
the variant exists in `starting` for the duration. This is the
same ordering the existing `--mode subprocess` flow follows, just
with a Kubernetes Job between the host and the user command.

| Existing artifact | 13b disposition |
|---|---|
| [`spec/v0/03-roles.md`](../../spec/v0/03-roles.md) | Unchanged; the role contract is binding-agnostic. |
| [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md) | Unchanged; the subprocess binding still applies to Compose deployments. 13b adds a sibling document at the same level. |
| [`reference/services/executor/src/eden_executor_host/cli.py`](../../reference/services/executor/src/eden_executor_host/cli.py) | Gets a new `--mode k8s-job` choice plus k8s-Job-specific flags (§5.2). |
| [`reference/services/executor/src/eden_executor_host/subprocess_mode.py`](../../reference/services/executor/src/eden_executor_host/subprocess_mode.py) | Unchanged. The new Job mode lives in a sibling module `k8s_job_mode.py`. |
| [`reference/helm/eden/values.yaml`](../../reference/helm/eden/values.yaml) | Adds `executor.mode` + `executor.jobTemplate` blocks. |
| [`reference/helm/eden/templates/executor-host-statefulset.yaml`](../../reference/helm/eden/templates/executor-host-statefulset.yaml) | Conditional rendering on `executor.mode`. When `k8sJob`, sets `serviceAccountName` to the chart-managed SA and passes `--mode k8s-job` plus the job-template knobs. |

### 1.3 Naming-discipline baseline

PR #60's strengthened guardrail applies. New identifiers introduced
by 13b:

- CLI: `--mode k8s-job`, `--job-namespace`, `--job-image-repository`,
  `--job-image-tag`, `--job-image-pull-policy`,
  `--job-service-account`, `--job-template-config`,
  `--job-active-deadline-seconds`, `--job-poll-interval`.
- Chart values: `executor.mode`, `executor.jobTemplate.*`,
  `executor.jobTemplate.image.{repository,tag,pullPolicy}`,
  `executor.jobTemplate.nodeSelector`,
  `executor.jobTemplate.tolerations`,
  `executor.jobTemplate.resources.{requests,limits}`,
  `executor.rbac.create`, `executor.serviceAccount.name`.
- k8s resource labels: `eden.task_id`, `eden.role`,
  `eden.experiment_id`, `eden.host` (parallel to the DooD
  labels — same vocabulary, lets the reaper code share a
  filter shape).
- New module: `reference/services/executor/src/eden_executor_host/k8s_job_mode.py`.
- New script: `eden-execute-wrapper` (the in-pod wrapper; baked
  into `eden-runtime:dev` AND ConfigMap-mounted from the chart
  per Decision 8 / §3.6). The name follows the `execution_command`
  / `evaluation_command` precedent from the experiment YAML.

None of these reintroduce retired vocabulary (`promote`, `eval_error`,
verb-on-verb helpers like `submit_execute`); pre-submit
`scripts/check-rename-discipline.py` clean.

## 2. Decisions

These are the load-bearing design calls; §3 unpacks each.

1. **Job-per-execution-task, not Pod-per-task or Deployment-per-task.**
   Each pending `execution` task that the host claims becomes one
   Kubernetes `batch/v1` `Job`. The Job has `parallelism: 1`,
   `completions: 1`, `backoffLimit: 0` (the host owns retry
   semantics via the existing claim-TTL / sweeper machinery, not
   k8s). The Job's pod runs the user's `execution_command` and
   exits; the Job is then deleted by the host. Why Job — not Pod:
   the Job controller exposes terminal state via
   `.status.{succeeded, failed, conditions[?].type=="Complete|Failed"}`
   that's controller-maintained and race-free, gives us
   `ttlSecondsAfterFinished` for Pod log GC, and is the standard
   ecosystem shape for "run-to-completion" workloads (kubectl,
   monitoring, audit tooling all understand `Job`). With
   `backoffLimit: 0` we explicitly opt out of the controller's
   retry behavior because the host already owns retry. **What
   `backoffLimit: 0` does NOT give us:** node-loss resilience.
   Per the [Kubernetes Job docs](https://kubernetes.io/docs/concepts/workloads/controllers/job/),
   a pod disruption (node drain, eviction, preemption) on a
   `backoffLimit: 0` Job marks the Job `Failed` with a
   `DisruptionTarget` condition rather than rescheduling the pod.
   The host treats that exactly as it treats any other Job-failed
   path: submit `VariantSubmission(status="error")` per chapter 3
   §3.3, the variant terminalizes as `error`, and the next
   ideation cycle MAY produce a new idea/variant pair that
   re-attempts the same change. The k8s docs additionally warn
   that even `completions: 1` Jobs can occasionally start the same
   user program twice (controller restart edge cases); §8.10
   covers how the host's existing idempotent-submit machinery
   (`submissions_equivalent` + `ConflictingResubmission` from
   chapter 4 §4.2) handles that. (See §3.1 alternatives.)

2. **Executor-host StatefulSet keeps the claim+submit role.** The
   host pod's loop continues to claim execution tasks from the
   task-store-server, do `Store.create_variant(status="starting")`
   per chapter 3 §3.2 step 1, and run the Phase 3 submit with
   retry-before-orphan + read-back. What changes is the middle
   step: instead of `subprocess.Popen("execution_command")` or
   `docker run … execution_command`, the host calls the k8s API
   to create a Job, watches the Job to completion, reads the
   outcome from the Pod's terminal log line, and then submits.
   The host does NOT delegate claim ownership to a controller.
   (See §3.1 alternatives — pure-controller pattern rejected.)

3. **Outcome plumbing via stdout sentinel line.** The Job pod's
   main container emits, as the LAST line on stdout before exit,
   a sentinel of the form `EDEN_OUTCOME <single-line JSON>`. The
   wrapper binary owns this line; the user's `execution_command`
   never writes it directly. The host reads the outcome via the
   k8s `Pod log` API after observing `Job.status.succeeded == 1`
   (or `.failed > 0`). Rejected alternatives: per-task RWX PVC
   (portability cost — RWX storage classes aren't universal,
   and a GPU node may not be on the same node as the host's
   PVC); HTTP callback to the host (introduces a new internal
   service surface and per-task auth tokens); annotation-patch
   via the Pod's own ServiceAccount (requires giving the user
   pod RBAC to patch its own annotations — bigger blast radius
   than read-only logs). See §3.1 alternatives.

4. **All store-side writes happen on the host. The Job pod
   never talks to the task-store-server.** The Job pod's
   ServiceAccount has *no* network access to the task-store-server
   (gated by NetworkPolicy if the operator opts in); it talks
   only to Forgejo (to clone + push) and writes the outcome to
   stdout. This keeps the chapter 3 submit-side discipline
   (claim-TTL, retry-before-orphan, committed-state read-back)
   on the host, where it already is. See §3.4 for the per-task
   sequence.

5. **Per-pod ephemeral worktree on EmptyDir; init container
   does the clone.** Each Job pod gets an EmptyDir volume
   `/var/lib/eden/work` that holds the bare repo + worktree.
   An `initContainer` running `eden-runtime:dev` clones the
   bare repo from Forgejo (using the credential helper mounted
   from the chart's `git-credential-helper-configmap` per 13a
   §3.3a), then `git worktree add --detach` at
   `idea.parent_commits[0]`. The main container starts with
   the worktree already populated and `cwd=worktree`. This is
   the k8s-native equivalent of the host-side worktree
   creation that `--mode subprocess` does today. See §3.5.

6. **GPU node selection is purely a Helm-values concern.** The
   chart exposes `executor.jobTemplate.nodeSelector`,
   `executor.jobTemplate.tolerations`, and
   `executor.jobTemplate.resources` (with `requests.cpu`,
   `requests.memory`, `limits.cpu`, `limits.memory`,
   `limits.nvidia.com/gpu`, `limits.amd.com/gpu`). These are
   pass-through to the Job's pod template; the chart does NOT
   prescribe a default GPU class or a default node label.
   Operators set them per their cluster's GPU-node taint /
   label conventions. The chart's `values.schema.json` validates
   shape (objects/arrays) but **does not require** GPU values
   — a deployment running CPU-only experiments leaves them all
   empty. Spec-level: GPU requests are non-normative, so the
   protocol stays silent. See §3.7.

7. **Per-experiment `execution_resources` override is OUT OF
   SCOPE for 13b.** Some experiments may want different GPU
   asks per task (e.g., a small ideator's evaluation needs no
   GPU; a large model fine-tune needs 4 H100s). 13b ships
   only the deployment-level override (one set of resources
   for all execution tasks in this release). A follow-up
   chunk MAY add a non-normative `execution_resources` block
   to the experiment-config YAML that the host reads and
   merges into the Job template per task. Punting it from
   13b keeps the scope honest and avoids cross-chunk coupling
   with 12c (the control plane's policy mechanism is the
   right place to land per-experiment overrides). See §11.

8. **The Job's main container runs an EDEN-compatible image with
   a documented minimum surface; the wrapper script is mounted
   from a chart-managed ConfigMap.** The plan's earlier framing
   of "experiment image has no eden-specific responsibilities"
   was wrong; the wrapper.sh shim does need shell + git +
   python3 + ca-certificates available at runtime, AND it runs
   git operations against the worktree the init container
   populated. So the main container's image MUST satisfy a
   minimum-surface contract:
   - `/bin/sh` (POSIX shell — not bash; the wrapper is
     POSIX-shell-only).
   - `git` ≥ 2.20 on `PATH`.
   - `python3` ≥ 3.8 on `PATH` (used by the wrapper for
     outcome JSON parsing — POSIX shell has no JSON parser).
   - `ca-certificates` (so `git push` to a TLS-fronted Forgejo
     works).
   - SHOULD: `eden:1000` user matching the executor-host's
     identity, so commits the user command produces are
     uid-owned compatibly with the integrator's reads (same
     posture as the DooD `eden-runtime:dev` image; chunk-10d
     follow-up A §"Identity").

   Distroless and `FROM scratch` images do NOT satisfy this
   contract; operators using those substitute a static
   `eden-execute-wrapper` binary (out of scope for 13b — see
   §11) or layer `eden-runtime:dev` underneath. The chart's
   default `executor.jobTemplate.image` points operators at
   `eden-runtime:dev` (via the 13a operator-built+pushed image
   strategy), and the README documents the recommended pattern
   `FROM ghcr.io/<org>/eden-runtime:<tag>` for experiment-
   specific images.

   The wrapper script itself ships in TWO places by design:
   - **Baked into `eden-runtime:dev`** at `/usr/local/bin/eden-execute-wrapper`
     (so an experiment image that derives from `eden-runtime:dev`
     gets it for free).
   - **Mounted from a chart-managed ConfigMap** at
     `/etc/eden/wrapper.sh` and used as the container's
     `command:`. This makes `command:` always override the
     image's `entrypoint:` AND lets operators bump the wrapper
     by re-applying the chart without rebuilding their experiment
     image. The two copies SHOULD have the same content; the
     ConfigMap version is the source of truth for the chart's
     version of EDEN.

   This dual-shipping is the same pattern the 13a base chart
   uses for the git credential helper (ConfigMap-mounted into
   workers; 13a §3.3a). See §3.6 for the wrapper itself.

9. **`executor.mode=scripted` remains the chart default.**
   13a's hard-coded `--mode scripted` becomes a values knob
   with the same default; existing 13a operators see no
   behavior change on chart upgrade. `executor.mode=k8sJob`
   is opt-in. Setting it requires `executor.jobTemplate.image.repository`
   and `executor.jobTemplate.image.tag` to be non-empty
   (enforced by `values.schema.json`'s `if/then` clause),
   per the 13a-codified pitfall "no fictional defaults for
   operator-required values". The chart ALSO supports
   `executor.mode=subprocess` as a forward-looking enum
   value (rendered as a no-op error at template time) to
   reserve the slot for a future chunk that brings DooD to
   k8s with sysbox or per-pod docker daemons; 13b does NOT
   ship the subprocess mode on k8s. See §3.8.

10. **Multi-tenant Job lifecycle: Jobs are
    per-experiment-namespaced via labels, NOT per-experiment
    namespace.** Each Job carries
    `metadata.labels.eden.experiment_id=<id>` and
    `metadata.labels.eden.host=<host pod name>` so the host's
    orphan reaper at startup can `kubectl delete jobs -l
    eden.host=<this-host>` (mirrors the DooD
    `reap_orphaned_containers(role, host=...)` pattern). The
    host releases all per-task Jobs synchronously on its own
    SIGTERM (`finally`-block deletion). A future amendment
    aligned with 12c MAY move Job ownership to a per-experiment
    namespace; 13b uses labels to keep the chart vanilla.
    See §7.4.

## 3. Design

### 3.1 Alternatives considered and rejected

Three architectural choices in this chunk benefit from explicit
compare-and-reject paragraphs.

**Job vs Pod vs Deployment.**

- *Pod-per-task (rejected):* the host creates a bare `Pod`
  resource directly. Pros: simplest mental model; no
  controller in the loop. Cons: terminal state has to be
  inferred from `.status.phase` transitions plus container
  exit codes, with no controller-maintained invariant; the
  host has to GC the Pod itself (no `ttlSecondsAfterFinished`
  on bare Pods); ecosystem tooling (kubectl, monitoring)
  reasons in terms of higher-level controllers. None of
  these are showstoppers, but Job's controller-maintained
  status fields and standard ecosystem shape are
  meaningfully easier to integrate against. The
  *eviction-resilience* argument that earlier drafts of
  this plan cited is NOT a Job advantage with `backoffLimit: 0`
  (per Decision 1 / §8.10 — disrupted Pods fail the Job, so
  Job and bare-Pod have the same recovery story under
  disruption: host treats it as transport-failure → submit
  `error` → the next ideation cycle re-attempts).
- *Job-per-task (chosen):* see Decision 1.
- *Deployment-per-task (rejected):* a Deployment is the
  wrong shape — it implies steady-state replicas. We'd
  fight the controller to scale to 0 after the user
  command exits. No upside.

**Who creates the Job.**

- *Executor-host-as-Job-creator (chosen):* see Decision 2.
- *Pure-controller pattern (rejected):* a new "execution-job-
  controller" Deployment that watches the task store and
  reconciles by creating Jobs. Pros: cleaner separation,
  could use a CRD-based reconciliation loop (`Reconcile`
  pattern). Cons: (a) the controller would have to claim
  tasks on behalf of an abstract "executor identity",
  duplicating the executor-host's existing role; (b) two
  agents racing for the same task is a real failure mode
  to design around (lease, leader election); (c) a lot
  more substrate (controller binary, RBAC for
  `tasks.eden.io` watch + `Job` create, controller
  versioning) for marginal benefit. The host already
  polls the wire; it can create Jobs in the same loop
  iteration. Rejected as overengineering for v0.
- *Orchestrator-creates-Job (rejected):* would violate
  the role separation in
  [`spec/v0/03-roles.md`](../../spec/v0/03-roles.md) §1
  — the orchestrator dispatches but does not claim or
  execute tasks. The orchestrator's role is to advance
  the state machine, not to perform the work.

**Outcome plumbing.**

- *Stdout sentinel line (chosen):* see Decision 3. The
  k8s log API is rate-limit-friendly, requires no extra
  RBAC beyond `pods/log` read, and survives Pod GC for
  the default `terminationGracePeriodSeconds`. The host
  parses the *last* line matching `^EDEN_OUTCOME⎵` (where
  `⎵` is a literal space) —
  user code that emits the prefix on its own is treated
  as a violation (the wrapper deliberately uses a prefix
  that begins with an uppercase identifier unlikely in
  application logs; a paranoid wrapper could rotate to a
  per-Job random nonce, but the namespace boundary already
  protects against the cross-tenant version of this attack).
- *Per-task RWX PVC (rejected):* the host and the Job
  pod both mount a per-task PVC; the wrapper writes
  outcome.json there. Cons: RWX storage classes aren't
  universal (NFS, CephFS, EFS — varies by cluster);
  forcing RWX would break GKE-without-Filestore, EKS-without-EFS,
  bare-metal-without-NFS clusters out of the gate. RWO
  doesn't help because the Job pod and the host pod may
  be on different nodes (especially with GPU node taints).
- *HTTP callback to the host (rejected):* the Job pod
  POSTs `{outcome}` to the executor-host's pod IP via
  the headless StatefulSet Service. Cons: introduces a
  new HTTP surface on the host (mini-uvicorn server),
  per-task one-time tokens for auth, NetworkPolicy
  carve-outs to allow Job→host traffic. Mostly: more
  surface area for a problem that `kubectl logs` solves
  trivially.
- *Annotation patch via Pod's own ServiceAccount
  (rejected):* the wrapper inside the Job pod calls the
  k8s API to set its own pod annotations. Cons: the user
  pod's SA needs `pods.patch` on its own pod (or
  `pods/status.patch`); a buggy or hostile user command
  could now mutate pod state. Rejected as widening the
  Job pod's blast radius.

### 3.2 Helm chart additions

#### 3.2.1 New / changed values

```yaml
executor:
  # 13b adds this knob; default mirrors 13a's hardcoded scripted mode.
  mode: "scripted"            # ∈ {scripted, k8sJob}

  rbac:
    # When mode=k8sJob, the chart renders a Role + RoleBinding granting
    # the executor-host pod the ability to manage Jobs and read Pod
    # logs in the release namespace. Default true; operators with
    # external RBAC (Sealed Secrets, GitOps-managed) set false and
    # provide their own.
    create: true

  serviceAccount:
    # SA name the executor-host StatefulSet runs as. When
    # rbac.create=true, the chart renders this SA. When false, the
    # operator pre-creates the SA with the same name.
    name: ""                  # default: <release>-eden-executor

  jobTemplate:
    # The pod template the host stamps out per execution task.
    # Required when mode=k8sJob; values.schema.json's if/then enforces.
    image:
      repository: ""          # required when mode=k8sJob (e.g., ghcr.io/your-org/eden-experiment-foo)
      tag: ""                 # required when mode=k8sJob (e.g., 0.1.0)
      pullPolicy: IfNotPresent
      pullSecrets: []
    # The init container that clones from Forgejo + creates the
    # worktree always uses the chart-level image (eden-runtime:dev);
    # operators do NOT override the init image.
    activeDeadlineSeconds: 600  # k8s kills the Job after this many seconds; mirrors --execution-task-deadline
    # GPU node selection — pure pass-through to the pod template.
    nodeSelector: {}          # e.g., { gpu-class: "a100" }
    tolerations: []           # e.g., [{ key: "nvidia.com/gpu", operator: "Exists", effect: "NoSchedule" }]
    resources:
      requests:
        cpu: ""               # operator-supplied
        memory: ""
      limits:
        cpu: ""
        memory: ""
        # GPU surface — empty by default; operators set per their
        # device plugin (nvidia.com/gpu, amd.com/gpu, gpu.intel.com/i915).
        # values.schema.json validates shape, NOT specific keys; the
        # chart is GPU-vendor-agnostic.
    # Optional: env to inject into the Job pod's main container,
    # alongside the wrapper-injected EDEN_* env. Values may
    # reference existing Secrets via valueFrom.
    env: []
    # poll interval the host uses to watch Job status; applies only
    # to mode=k8sJob. Default 1s; smaller values increase k8s API load.
    pollIntervalSeconds: 1.0
    # If a Job's pod is stuck in ImagePullBackOff / ErrImagePull /
    # InvalidImageName for longer than this many seconds since pod
    # creation, the host gives up early (instead of waiting for
    # activeDeadlineSeconds). See §8.2.
    imagePullDeadlineSeconds: 60.0
```

#### 3.2.2 `values.schema.json` clauses

```json
{
  "if": { "properties": { "executor": { "properties": { "mode": { "const": "k8sJob" } } } } },
  "then": {
    "properties": {
      "executor": {
        "properties": {
          "jobTemplate": {
            "required": ["image"],
            "properties": {
              "image": {
                "required": ["repository", "tag"],
                "properties": {
                  "repository": { "type": "string", "minLength": 1 },
                  "tag":        { "type": "string", "minLength": 1 }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

`helm install` with `executor.mode=k8sJob` and an empty
`executor.jobTemplate.image.repository` fails at lint time
with a clear error — per the 13a-codified
"operator-required values, no fictional defaults" pitfall.

#### 3.2.3 New templates

| File | Purpose |
|---|---|
| `templates/executor-rbac.yaml` (new) | `ServiceAccount` + `Role` + `RoleBinding` rendered when `executor.mode == "k8sJob"` AND `executor.rbac.create == true`. The Role grants verbs `get`, `list`, `watch`, `create`, `delete` on `batch/v1.Job` and `get`, `list`, `watch` on `Pod` (incl. `pods/log`) in the release namespace only. |
| `templates/executor-wrapper-configmap.yaml` (new) | `ConfigMap` containing the wrapper shell script `wrapper.sh` per §3.6. Mounted into every Job pod's main container at `/etc/eden/wrapper.sh`. Rendered unconditionally — harmless when `mode != k8sJob`. |
| `templates/executor-host-statefulset.yaml` (modified) | When `executor.mode == "k8sJob"`: passes `--mode k8s-job`, `--job-namespace {{ .Release.Namespace }}`, `--job-image-repository {{ .Values.executor.jobTemplate.image.repository }}`, `--job-image-tag {{ .Values.executor.jobTemplate.image.tag }}`, plus a JSON-blob `--job-template-config` rendering the nodeSelector + tolerations + resources + env from values. Sets `serviceAccountName` to the chart-managed SA. |

The Helm-side conditional is a single `{{- if eq .Values.executor.mode "k8sJob" -}}` guard around the new resources. The
StatefulSet template's `args:` block uses the same conditional to
pick between the two arg sets — no duplication of the shared args.

### 3.3 Executor-host CLI additions

Per §5.2, `parse_args` gains:

```text
--mode {scripted, subprocess, k8s-job}
--job-namespace TEXT          # required when --mode k8s-job
--job-image-repository TEXT   # required when --mode k8s-job
--job-image-tag TEXT          # required when --mode k8s-job
--job-image-pull-policy TEXT  # default "IfNotPresent"
--job-service-account TEXT    # default "default"
--job-template-config PATH    # JSON file with nodeSelector/tolerations/resources/env
--job-active-deadline-seconds FLOAT  # default 600.0
--job-poll-interval FLOAT     # default 1.0
--job-image-pull-deadline-seconds FLOAT  # default 60.0; see §8.2
```

The argparse-time validation mirrors `--mode subprocess`: when
`--mode k8s-job` is set, the four `--job-*` required flags must
also be set, otherwise `parser.error(...)` exits with a clear
message. The `--job-template-config` flag is OPTIONAL — empty
JSON `{}` is fine — but when present must parse as a JSON
object with the four optional keys.

### 3.4 Per-task sequence

For each pending execution task that the host claims:

1. **Pre-Phase-1 (host).** Fetch `task` and `idea` from the
   task-store-server. Generate `variant_id` host-side. Compute
   the canonical `branch = work/<slug>-<variant_id>`. Pre-Phase-1
   ref-collision guard — if `refs/heads/<branch>` already exists
   on the host's local clone, submit `error` and return (mirrors
   `subprocess_mode._handle_one`).
2. **Claim (host).** `store.claim(task_id, worker_id)`.
3. **Phase 1 (host).** `Store.create_variant(status="starting",
   ...)` per chapter 3 §3.2 step 1. Failure → submit `error` via
   the existing retry-before-orphan + read-back path; return.
4. **Job spec build (host).** Render the Job manifest:
   - `metadata.name`: `eden-execute-{task_id}` truncated to 63
     chars per k8s naming. If truncation collides (very rare —
     would need same prefix), append a 6-char random suffix.
   - `metadata.labels`: `eden.task_id`, `eden.role=executor`,
     `eden.experiment_id`, `eden.host=<own pod name>`.
   - `spec.backoffLimit: 0`.
   - `spec.activeDeadlineSeconds`: from `--job-active-deadline-seconds`.
   - `spec.ttlSecondsAfterFinished: 60` (lets the host read
     pod logs before k8s reaps).
   - `spec.template.spec.serviceAccountName`: from
     `--job-service-account`.
   - `spec.template.spec.restartPolicy: Never`.
   - `spec.template.spec.nodeSelector + tolerations`: from
     `--job-template-config`.
   - `spec.template.spec.initContainers[0]`: runs
     `eden-runtime:dev` with the Forgejo credential helper mounted;
     clones bare from `--forgejo-url`, `git worktree add --detach
     <wt> <parent_commits[0]>`, writes `.eden/task.json` per
     §3.5.
   - `spec.template.spec.containers[0]`: runs the user
     `execution_command` via the wrapper.sh shim per §3.6.
     `image` from `--job-image-repository:--job-image-tag`.
     `resources` from `--job-template-config.resources`. `env`
     prepended with the four `EDEN_*` vars.
   - `spec.template.spec.volumes`: one `emptyDir` for the
     work dir, one `configMap` mount for the wrapper script,
     one `configMap` mount for the credential helper.
5. **Job create (host).** `kubernetes.client.BatchV1Api.create_namespaced_job(...)`.
6. **Job watch (host).** Loop with `--job-poll-interval` cadence:
   `read_namespaced_job_status(...)`. The pod selection that
   underpins each "read pod logs" sub-step below follows the
   §8.10 duplicate-Pod selection algorithm (label-filter on
   `batch.kubernetes.io/job-name` + `controller-uid`, partition
   by `.status.phase`, prefer Succeeded, fail closed on
   contradictory Succeeded outcomes). Three terminal cases:
   - `status.succeeded == 1`: select the Succeeded pod per
     §8.10, read its log, parse the last `EDEN_OUTCOME ...`
     line as JSON. Validate shape: `status` ∈ {success, error};
     if `success`, `commit_sha` is a valid hex SHA. Validate
     reachability via the host's local clone (`fetch_ref(branch)`
     against Forgejo, then `repo.is_ancestor(parent, commit_sha)`
     for every parent — same shape as
     `subprocess_mode._validate_commit`). On reachability
     failure, treat as `error`. On parse failure (no sentinel
     line, JSON malformed), treat as `error`.
   - `status.failed > 0`: select the most-recent Failed pod
     per §8.10, capture last 1KB of its log to host structured
     log for diagnostics (per
     [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
     §5 — failure context goes to the host log, not the wire),
     then submit `error`.
   - `Job.spec.activeDeadlineSeconds` exceeded: same as failed.
7. **Phase 3 (host).** Run the existing `_submit_with_readback`
   path with `VariantSubmission(status, variant_id, commit_sha)`.
8. **Cleanup (host, finally).** `delete_namespaced_job(name,
   propagation_policy="Background")`. The Pod and EmptyDir go
   away with the Job.

The host's claim TTL is configured to be longer than
`activeDeadlineSeconds` + a small buffer for the read+submit
steps, so the sweeper never reclaims a task whose Job is still
running. If the host pod itself is restarted mid-task (SIGTERM
via Helm rolling update), the claim TTL expires, the sweeper
reclaims the task, and the new host instance's startup-time
orphan reaper deletes the now-abandoned Job.

### 3.5 Init container: clone + worktree

The init container runs `eden-runtime:dev` with the same git
credential helper that 13a wires into the executor-host
StatefulSet. Sequence (run via a small inline shell command —
no new binary needed):

```bash
set -euo pipefail
git clone --bare "${EDEN_FORGEJO_URL}" /var/lib/eden/work/repo.git
cd /var/lib/eden/work/repo.git
git fetch --prune origin '+refs/heads/*:refs/heads/*'
git worktree add --detach /var/lib/eden/work/wt "${EDEN_PARENT_COMMIT}"
mkdir -p /var/lib/eden/work/wt/.eden
cat > /var/lib/eden/work/wt/.eden/task.json <<EOF
{
  "task_id": "${EDEN_TASK_ID}",
  "variant_id": "${EDEN_VARIANT_ID}",
  ...
}
EOF
```

Env vars are populated from the host's Job-build step. The
worktree lives in EmptyDir, so it's gone when the pod
terminates — same lifecycle as the per-pod scratch space the
DooD path uses today.

### 3.6 Main container: wrapper shim

Per Decision 8, the main container's image MUST satisfy the
EDEN-compatible minimum surface (`/bin/sh`, `git`, `python3`,
`ca-certificates`). The chart ships a `ConfigMap`
`executor-wrapper-configmap` with a `wrapper.sh` shell script
mounted at `/etc/eden/wrapper.sh`; the Job's pod template sets
`containers[0].command: ["/bin/sh", "/etc/eden/wrapper.sh"]`,
overriding whatever `entrypoint:` the experiment image has.

The same wrapper content is also baked into `eden-runtime:dev`
at `/usr/local/bin/eden-execute-wrapper` so a derived experiment
image (`FROM ghcr.io/<org>/eden-runtime:<tag>`) carries it
inline and can be invoked directly from a non-Helm context (CI
test, local dev):

```bash
#!/bin/sh
# /etc/eden/wrapper.sh
# Run the user's execution_command, then emit EDEN_OUTCOME on stdout.
set -e
cd "${EDEN_WORKTREE}"
# Run the user command; outcome.json is the convention from
# `worker-host-subprocess.md` §3.
sh -c "${EDEN_EXECUTION_COMMAND}" || rc=$? ; rc=${rc:-0}
if [ -f "${EDEN_WORKTREE}/.eden/outcome.json" ]; then
  outcome=$(cat "${EDEN_WORKTREE}/.eden/outcome.json")
else
  outcome='{"status":"error","reason":"missing outcome.json"}'
fi
# If outcome status is success, push the work/* ref to Forgejo.
status=$(printf '%s' "${outcome}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')
commit_sha=$(printf '%s' "${outcome}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("commit_sha",""))')
if [ "${status}" = "success" ] && [ -n "${commit_sha}" ]; then
  cd /var/lib/eden/work/repo.git
  if ! git update-ref "refs/heads/${EDEN_BRANCH}" "${commit_sha}" 2>&1; then
    outcome='{"status":"error","reason":"local update-ref failed"}'
  elif ! git push origin "refs/heads/${EDEN_BRANCH}" 2>&1; then
    git update-ref -d "refs/heads/${EDEN_BRANCH}" || true
    outcome='{"status":"error","reason":"push failed"}'
  fi
fi
# Final line on stdout — load-bearing.
printf 'EDEN_OUTCOME %s\n' "${outcome}"
exit "${rc}"
```

The wrapper is invoked as the main container's command:
`["sh", "/etc/eden/wrapper.sh"]`. The user's
`execution_command` is supplied through env (`EDEN_EXECUTION_COMMAND`)
because expanding it as `args:` in the pod spec would be hostile
to the shell-quoting the experiment YAML expects.

If the experiment image has a non-bash `entrypoint` (e.g., a
custom Python launcher), the wrapper still runs as the
container's `command:` — `command:` overrides `entrypoint:`
in k8s. The chart documents this trade-off in the README.

### 3.7 GPU values shape

The chart's `executor.jobTemplate.{nodeSelector, tolerations,
resources}` fields are pure pass-through to the Pod template.
No interpretation, no defaults. Operators set them per their
cluster:

```yaml
# values-gke-a100.yaml
executor:
  mode: k8sJob
  jobTemplate:
    image:
      repository: us-docker.pkg.dev/myorg/eden/experiment-llm-finetune
      tag: 0.3.2
    nodeSelector:
      cloud.google.com/gke-accelerator: nvidia-tesla-a100
    tolerations:
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
    resources:
      requests:
        cpu: "4"
        memory: "32Gi"
      limits:
        cpu: "8"
        memory: "64Gi"
        nvidia.com/gpu: "1"
```

The chart's `values.schema.json` validates that
`resources.{requests, limits}` are objects (string-keyed
`additionalProperties: { type: string }`) but does NOT
enumerate which device-plugin keys are allowed; that lets the
same chart work against `nvidia.com/gpu`, `amd.com/gpu`,
`gpu.intel.com/i915`, and any future device plugin without
chart updates. Documented in `docs/deployment/helm.md` with
worked examples for GKE A100, EKS A10G, on-prem nodefeature-
labeled GPUs, and CPU-only.

### 3.8 Forward-looking enum value: `executor.mode=subprocess` reservation

The values schema accepts `executor.mode ∈ {scripted, k8sJob,
subprocess}` but the chart's `_helpers.tpl` errors at
template-time if `subprocess` is set, with a message pointing
at this plan's §11 ("subprocess + DooD on k8s is not yet
implemented; use mode=k8sJob or stay on Compose"). This
**reserves** the enum slot so the future chunk that lands
DooD-on-k8s can flip the error to a real path without breaking
operators' values files. It also makes `helm template` produce
a comprehensible error rather than rendering a broken pod
spec. Per the AGENTS.md "no half-finished implementations"
discipline, the chart does NOT ship a no-op partial subprocess
mode.

### 3.9 Spec reference-binding chapter

A new informative chapter at
[`spec/v0/reference-bindings/worker-host-k8s-job.md`](../../spec/v0/reference-bindings/worker-host-k8s-job.md)
documents the protocol-level shape:

- Cross-references to chapter 3 §3.2 (variant created with
  `status="starting"` BEFORE any observable repo write —
  enforced by host doing `create_variant` before Job create).
- The Job's pod has no protocol-level role; it is purely a
  runner for the user's `execution_command`.
- The stdout sentinel convention (`EDEN_OUTCOME <json>`).
- Failure-mode mapping to chapter 3 §3.3 (transport failures
  → status=error, mirroring the executor's existing
  failure-status vocabulary).
- Cross-host worktree isolation: §6 of the subprocess
  binding doesn't apply (each pod has its own EmptyDir);
  the section is replaced by "the worktree's lifecycle is
  the Pod's lifecycle".
- Security boundary: the Job pod runs in the same namespace
  as the executor-host with the chart-managed
  `<release>-eden-execute` SA (Role-bound to no resources;
  ephemeral). The trust boundary is the namespace; this is
  the same posture the Compose deployment carries with its
  shared docker daemon.

The existing
[`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
gains a one-line cross-reference to the new k8s-job binding;
no other normative changes.

## 4. Scope

### 4.1 In scope

Reference deployment artifacts:

- New chart values: `executor.mode`, `executor.rbac.create`,
  `executor.serviceAccount.name`, `executor.jobTemplate.*`.
- New chart templates: `executor-rbac.yaml`,
  `executor-wrapper-configmap.yaml`.
- Modified template:
  `executor-host-statefulset.yaml` (mode-conditional args).
- New CLI / module:
  `reference/services/executor/src/eden_executor_host/k8s_job_mode.py`
  with the per-task Job-create/watch/read flow.
- Updated CLI: `eden_executor_host.cli.parse_args` accepts
  `--mode k8s-job` and the nine `--job-*` flags.
- New runtime artifact: `wrapper.sh` shipped via ConfigMap (the
  chart's version is authoritative) AND ALSO baked into
  `eden-runtime:dev` at `/usr/local/bin/eden-execute-wrapper`
  per Decision 8 (so derived experiment images carry it
  inline). The ConfigMap-mounted copy is what the Job's
  `command:` invokes at runtime.
- New CI job: `helm-smoke-executor-job` (kind cluster, opt-in
  GPU label simulation).
- Chart upgrade test: `helm-upgrade-smoke` from 13a is
  extended to assert mode-switch (scripted → k8sJob) is
  non-breaking.

Spec:

- New informative
  `spec/v0/reference-bindings/worker-host-k8s-job.md`.
- One-line cross-reference added to
  `spec/v0/reference-bindings/worker-host-subprocess.md`.

Docs:

- `docs/deployment/helm.md` (from 13a) extended with a "GPU
  scheduling" section.
- `AGENTS.md` "Commands" table extended with the helm-smoke-
  executor-job local-run command.
- `docs/roadmap.md` Phase 13 entry — 13b marked complete.

Tests:

- Unit tests for `k8s_job_mode.py` against a fake
  `kubernetes.client` (mock the BatchV1Api / CoreV1Api).
- Cross-request flow test using a fake Kubernetes that
  simulates Job → Pod → log read.
- `helm template` + `kubectl apply --dry-run=client` against
  the new templates with realistic GPU values files.

### 4.2 Cross-references to followups (out of scope for 13b)

- **Per-experiment GPU resources** (deferred). A non-normative
  `execution_resources` block in the experiment-config YAML
  that the host merges into the Job template per task. The
  control plane (12c) is the right place to land this; 13b
  ships only deployment-level defaults.
- **Evaluator-as-Job** (parallel design; deferred). Same
  shape as executor-as-Job but the per-task pod runs
  `evaluation_command`. The protocol contract is symmetric;
  the design lift is mostly module-copy. Lands in 13b-followup
  or its own 13 chunk.
- **Ideator-as-Job** (probably never). The ideator is a
  long-running JSON-line protocol that holds session state
  across tasks per
  [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
  §2. Job-per-task fights that semantics; ideator stays a
  long-running Deployment.
- **DooD-on-k8s subprocess mode**. The reserved
  `executor.mode=subprocess` enum value awaits a future
  chunk that picks a concrete mechanism (sysbox, per-pod
  docker daemon, Kata, Firecracker). Out of scope here.
- **Multi-experiment per-experiment Job namespace**. 13b
  uses labels on a release-shared namespace (Decision 10).
  A 12c-aligned chunk MAY move per-experiment isolation
  to per-namespace Jobs; deferred.
- **Pod log streaming** for live task progress. The 13b
  host reads pod logs only at terminal-state. Streaming
  pod logs through to the Web UI's admin page would need
  the Web UI to also have `pods/log` RBAC; deferred to a
  followup if operators ask.
- **NetworkPolicy templates.** The Job pod's network
  egress is namespace-default (any); a 13-followup may
  add a NetworkPolicy template that allows only Forgejo
  traffic. Deferred — k8s NetworkPolicy needs a CNI plugin
  that supports it (Calico, Cilium), which is non-portable.

### 4.3 Non-goals

- **Backwards compatibility for the chart's `executor.mode`
  rename.** 13a has no public users yet (it shipped recently).
  The chart's `executor.mode` value is added cleanly with
  default `scripted` matching 13a's hardcoded behavior; no
  shim, no deprecation period, no "old name" alias. (Per
  AGENTS.md "No backwards-compatibility shims in greenfield
  projects".)
- **GPU-on-kind CI**. The `helm-smoke-executor-job` CI job
  uses a synthetic node label (set on the kind worker via
  `kubectl label node`) and a mock GPU resource limit (a
  non-existent device-plugin key like
  `eden.test/fake-gpu`). Real GPU testing is operator-side.
- **Vendor-specific GPU operators.** The chart does not
  install / require the NVIDIA GPU Operator, AMD Device
  Plugin, etc. Operators install those separately; the
  chart pulls in the resources they expose.
- **Per-task image build.** The 13a "operator builds + pushes
  experiment image" posture stands. 13b doesn't add a build
  pipeline; the chart pulls a published image.

## 5. Files to touch

### 5.1 Chart additions

| File | Change |
|---|---|
| `reference/helm/eden/values.yaml` | Add the `executor.mode`, `executor.rbac`, `executor.serviceAccount`, `executor.jobTemplate.*` blocks per §3.2.1. Default `executor.mode: "scripted"` to preserve 13a behavior. |
| `reference/helm/eden/values.schema.json` | Add the if/then clause per §3.2.2 enforcing `image.repository` + `image.tag` non-empty when `mode=k8sJob`. Reject `mode=subprocess` with a clear error pointing at this plan's §11. |
| `reference/helm/eden/templates/executor-rbac.yaml` (new) | `ServiceAccount` + `Role` + `RoleBinding` rendered when `mode=k8sJob` AND `rbac.create=true`. Verbs scoped per §3.2.3. |
| `reference/helm/eden/templates/executor-wrapper-configmap.yaml` (new) | `ConfigMap` containing `wrapper.sh` per §3.6. Rendered unconditionally; small footprint. |
| `reference/helm/eden/templates/executor-host-statefulset.yaml` (modify) | Add a `{{- if eq .Values.executor.mode "k8sJob" -}}` branch in `args:` that passes the nine `--job-*` flags. Set `serviceAccountName: {{ include "eden.executorServiceAccountName" . }}` (gated on the same conditional). |
| `reference/helm/eden/templates/_helpers.tpl` (modify) | Add `eden.executorServiceAccountName` helper. Add `eden.assertExecutorMode` that raises `fail "..."` on `subprocess` mode. |
| `reference/helm/eden/ci-values.yaml` (modify) | Keep `mode: scripted` for the existing `helm-smoke` job; the new `helm-smoke-executor-job` job uses `ci-values-k8sjob.yaml`. |
| `reference/helm/eden/ci-values-k8sjob.yaml` (new) | Pinned values for the k8s-Job smoke: `executor.mode: k8sJob`, `jobTemplate.image.repository: ghcr.io/eden-protocol/eden-runtime`, `tag: dev`, no GPU asks, no nodeSelector (kind has one node). |
| `reference/helm/eden/README.md` (modify) | Add a "Modes" section enumerating `scripted` vs `k8sJob`, with operator-facing pros/cons. Add a "GPU scheduling" subsection with the worked GKE/EKS examples from §3.7. |

### 5.2 Executor-host code changes

| File | Change |
|---|---|
| `reference/services/executor/src/eden_executor_host/cli.py` | Extend `--mode` to accept `k8s-job`. Add the nine `--job-*` flags. Argparse-time validation: when `--mode k8s-job`, require `--job-namespace` + `--job-image-repository` + `--job-image-tag`. Add a top-level branch in `main()` that, when `args.mode == "k8s-job"`, calls `run_executor_k8s_job_loop(...)` from the new module. |
| `reference/services/executor/src/eden_executor_host/k8s_job_mode.py` (new) | Per-task Job create/watch/read flow per §3.4. Public entry point `run_executor_k8s_job_loop(*, store, worker_id, repo_path, forgejo_url, credential_helper, k8s_config, poll_interval, image_pull_deadline_seconds, stop)`. Internal helpers: `_build_job_manifest`, `_watch_job_terminal`, `_select_outcome_pod`, `_read_outcome_from_pod_logs`, `_check_image_pull_stuck`, `_handle_one`, `_orphan_reap`. Mirrors the structure of `subprocess_mode.py` but the inner work is k8s-API-bound. |
| `reference/services/executor/pyproject.toml` | Add `kubernetes>=29.0.0,<30` to `[project].dependencies`. (The official client; pinned major to avoid surprise breaks.) |
| `reference/services/executor/tests/test_k8s_job_mode.py` (new) | Unit tests against a fake `kubernetes.client` mocked via `unittest.mock`. Coverage: happy-path success outcome, parse-failure outcome → error, missing-sentinel-line outcome → error, Job timeout (activeDeadline) → error, reachability failure → error, orphan reap on host startup, host SIGTERM mid-task → Job deleted, claim-TTL expiry → sweeper recovers, image-pull-stuck-past-deadline → host gives up early per §8.2, duplicate-Pod selection rule §8.10 (one pod Succeeded + one Failed → success; two pods Succeeded with same SHA → success; two pods Succeeded with different SHA → error; only Failed pods → error; phase-Unknown after settle → error). |
| `reference/services/_common/src/eden_service_common/k8s.py` (new) | Tiny module exposing `make_k8s_clients()` (in-cluster config via `kubernetes.config.load_incluster_config`; falls back to `load_kube_config` for `pytest.mark.e2e` tests run from a kind context). Returns `(BatchV1Api, CoreV1Api)`. Avoids spreading the kubernetes-client import across multiple modules. |

### 5.3 New scripts / CI

| File | Change |
|---|---|
| `reference/helm/eden/ci-values-k8sjob.yaml` (new) | See §5.1. |
| `reference/helm/eden/healthcheck/smoke-executor-job.sh` (new) | Mirrors `13a's` ci-smoke shape: kind up; `helm install` with `ci-values-k8sjob.yaml`; run `setup-experiment-helm.sh` with the fixture; wait for quiescence; assert ≥3 `variant.integrated` events, ≥9 `task.completed` events, ≥3 ideation-task `task.completed` events. Additionally asserts: ≥3 `Job` resources with `eden.role=executor` were created in the namespace during the run, AND zero such Jobs remain after quiescence (the host's cleanup deleted them all — point-in-time `kubectl get jobs -l eden.role=executor` returns 0 rows; full assertion shape in §6.2). |
| `.github/workflows/ci.yml` (modify) | Add `helm-smoke-executor-job` GitHub Actions job that runs `smoke-executor-job.sh`. Matrix-skip on PRs that don't touch `reference/helm/`, `reference/services/executor/`, or `reference/services/_common/k8s.py` (cost control — kind-on-CI is slow). |

### 5.4 Spec / docs

| File | Change |
|---|---|
| `spec/v0/reference-bindings/worker-host-k8s-job.md` (new) | Informative chapter per §3.9. |
| `spec/v0/reference-bindings/worker-host-subprocess.md` (modify) | Add a one-line "see also" cross-reference to the new k8s-job binding. |
| `docs/deployment/helm.md` (modify) | Add §"Executor as a k8s Job" with the GKE / EKS / on-prem worked examples from §3.7. Add a §"Modes" subsection cross-referencing this plan's §3.8. |
| `AGENTS.md` "Commands" table | Add `bash reference/helm/eden/healthcheck/smoke-executor-job.sh` (the helm-smoke-executor-job local equivalent). |
| `docs/roadmap.md` Phase 13 entry | Mark 13b complete; cross-link to this plan. |

## 6. Test design

### 6.1 Unit tests against a fake k8s client

`reference/services/executor/tests/test_k8s_job_mode.py` mocks
`kubernetes.client.BatchV1Api` and `kubernetes.client.CoreV1Api`
via `unittest.mock.MagicMock`. Each test:

- Constructs a fake `Store` with a single pending execution
  task.
- Calls `_handle_one(...)` directly with the mock APIs.
- Asserts the BatchV1Api received a `create_namespaced_job`
  with the expected manifest shape (labels, args, mounts).
- Configures the BatchV1Api's `read_namespaced_job_status`
  to return scripted sequences (Pending → Running →
  Succeeded; or Pending → Failed; or Pending → activeDeadline
  exceeded).
- Configures the CoreV1Api's `read_namespaced_pod_log` to
  return scripted log content (with EDEN_OUTCOME sentinel,
  without sentinel, malformed JSON, etc.).
- Asserts the host's downstream `Store.submit` call has the
  expected `VariantSubmission`.

The mocks let us cover every code path without a real cluster.
Coverage target: every branch in `_handle_one` (≥10 cases per
the §5.2 list).

### 6.2 helm-smoke-executor-job integration test

Per §5.3:

- Spin up kind via `actions/setup-kind`.
- `kubectl label node kind-control-plane eden.test/fake-gpu=present`
  (simulates a GPU node label).
- `helm install eden reference/helm/eden -f
  reference/helm/eden/ci-values-k8sjob.yaml --create-namespace
  --namespace eden-test --wait --timeout 5m`.
- `bash reference/scripts/setup-experiment-helm.sh
  --namespace eden-test --experiment-config
  tests/fixtures/experiment/.eden/config.yaml --experiment-id exp-1`.
- Poll the task-store-server's wire endpoint via `kubectl exec`
  until quiescence (orchestrator exits 0 OR 3 `variant.integrated`
  events seen — same shape as `helm-smoke` from 13a).
- End-state assertions (mirrors 13a's helm-smoke):
  - ≥3 `variant.integrated` events.
  - ≥9 `task.completed` events.
  - ≥3 ideation-task `task.completed` events.
  - **New:** ≥3 `Job` resources with
    `metadata.labels.eden.role=executor` were observed
    during the run. Mechanism (concrete; no audit-log
    hand-wave): the smoke script runs `kubectl get jobs
    -n eden-test -l eden.role=executor -w
    -o jsonpath='{.metadata.name} {.metadata.creationTimestamp}'`
    in the background as soon as the namespace exists,
    appending each observed Job to a file. After
    quiescence the script counts unique Job names in
    that file. This catches Jobs that were created and
    then deleted by the host even if `kubectl get` at
    end-of-run returns 0 rows.
  - **New:** zero `Job` resources with `eden.role=executor`
    remain after quiescence (cleanup actually fired —
    point-in-time `kubectl get jobs -l eden.role=executor`
    returns 0 rows).
  - **New:** zero `Pod` resources with `eden.role=executor`
    remain after quiescence (Job ttlSecondsAfterFinished
    eventually fired — point-in-time `kubectl get pods
    -l eden.role=executor` returns 0 rows; allow up to
    `ttlSecondsAfterFinished + 30s` slack).
- `helm uninstall eden -n eden-test && kind delete cluster`.

CI runtime budget: 8 minutes (vs 5 minutes for 13a's helm-smoke,
because Job-per-task adds ≥3× pod scheduling latency).

### 6.3 Chart upgrade test

The 13a `helm-upgrade-smoke` extends to cover mode-switch:

1. Install at 13a-level chart (`mode: scripted`).
2. Bring up a 1-variant fixture; let it complete.
3. `helm upgrade ... -f mode-k8sjob-values.yaml` — switch
   to `mode: k8sJob`.
4. Assert the upgrade completes without error (no PVC
   re-creation, no data loss).
5. Submit a SECOND ideation task via the wire; assert it
   completes via the new Job mode.

This catches mode-switch regressions (e.g., a stale lease
held by a scripted-mode replica that the new k8sJob replica
can't claim).

### 6.4 `helm template` + `kubectl apply --dry-run=client` parity

A new fast CI job `helm-lint-k8sjob` extends 13a's `helm-lint`:

- `helm template eden reference/helm/eden -f
  reference/helm/eden/ci-values-k8sjob.yaml | kubectl apply
  --dry-run=client -f -`: passes.
- `helm template ... -f values-gke-a100-example.yaml`: passes
  (GPU values shape).
- `helm template ... --set executor.mode=subprocess` fails
  with the §3.8 reserved-mode error message.

### 6.5 Spec-xref + rename-discipline

`python3 scripts/spec-xref-check.py` runs against the new
reference-binding chapter; verifies every `§N.M` reference
in the new chapter resolves.

`python3 scripts/check-rename-discipline.py` runs over the
whole repo; verifies the new identifiers (`k8s-job`,
`jobTemplate`, etc.) don't reintroduce retired patterns.

## 7. Verification gates

Before merge:

- `helm lint reference/helm/eden` — passes against both
  ci-values files.
- `helm template ... | kubectl apply --dry-run=client -f -`
  — passes against both ci-values files.
- `uv run ruff check . && uv run pyright && uv run pytest -q`
  — passes (executor unit tests + the new k8s-job-mode
  module).
- `uv run pytest -m e2e` — passes (the existing real-subprocess
  e2e test is unchanged; 13b doesn't add a new e2e marker
  because the helm-smoke covers the substrate path).
- `uv run pytest -q conformance/` — passes (no conformance
  scenarios changed).
- `bash reference/compose/healthcheck/smoke.sh` — Compose
  smoke unchanged.
- `bash reference/compose/healthcheck/smoke-subprocess.sh` —
  passes (Compose subprocess mode unchanged by 13b).
- `bash reference/compose/healthcheck/smoke-subprocess-docker.sh`
  — passes (Compose DooD mode unchanged).
- `bash reference/compose/healthcheck/e2e.sh` — Compose e2e
  unchanged.
- `bash reference/helm/eden/healthcheck/smoke.sh` (the 13a
  helm-smoke, scripted mode) — passes.
- `bash reference/helm/eden/healthcheck/smoke-executor-job.sh`
  — **the new helm-smoke-executor-job**; passes.
- `python3 scripts/spec-xref-check.py` — passes (new
  reference-binding chapter's xrefs resolve).
- `python3 scripts/check-rename-discipline.py` — passes.
- `npx --yes markdownlint-cli2@0.14.0 docs/plans/eden-phase-13b-executor-k8s-job.md
  spec/v0/reference-bindings/worker-host-k8s-job.md` — passes.
- Manual verification: `kind create cluster && helm install`
  on a local cluster, walk through the README's GPU
  scheduling example with a synthetic GPU label, confirm
  Jobs get created on the labeled node only.

## 8. Tricky areas

### 8.1 Job-name collision under task-id reclaim

The 13a-codified pitfall about reclaimed task ids and stale
cidfiles applies: if a task is reclaimed (claim TTL expired,
sweeper reset to pending, a new host claims it), its `task_id`
is unchanged. A naive Job name `eden-execute-{task_id}` would
collide with the prior host's still-being-cleaned-up Job.

Mitigation: the host generates a per-attempt suffix using the
host's pod name + a 6-char random nonce:
`eden-execute-{task_id_short}-{host_pod_short}-{nonce}`.
The k8s 63-char limit forces shortening; the labels carry the
full `task_id` for filtering.

### 8.2 Image-pull errors don't terminate the Job quickly

A pod stuck in `ImagePullBackOff` will sit there until
`activeDeadlineSeconds` fires (default 600s). For a typo'd
`image.tag` value, that's a long time.

Mitigation: the host's Job watch checks the *Pod's*
`status.containerStatuses[*].state.waiting.reason` (the
init container's status block AND the main container's
status block — image-pull stalls happen on either) in
addition to the Job's status. When the reason is one of
`{ImagePullBackOff, ErrImagePull, InvalidImageName}` AND the
Pod has been `Pending` for more than `--job-image-pull-deadline-seconds`
(default 60s; chart value `executor.jobTemplate.imagePullDeadlineSeconds`),
the host deletes the Job and submits `VariantSubmission(status="error")`.
Operators with slow registries override via the chart value
or the host's CLI flag. The flag is wired through §3.3 (CLI),
§3.2.1 (chart values), §5.1 (StatefulSet args), and §6.1
(unit-test coverage of the early-give-up path).

This logic is asymmetric (we proactively give up on
image-pull failures but trust k8s for runtime issues) because
image-pull failures are non-transient and 100% of the time
indicate misconfiguration.

### 8.3 Pod log read race vs Pod GC

`ttlSecondsAfterFinished: 60` gives the host 60s after Job
completion to read pod logs. If the host happens to miss the
window (k8s control-plane lag, Pod evicted before the host's
poll), the pod logs become unreadable.

Mitigation: the host's poll loop reads pod logs *as soon as*
`Job.status.succeeded == 1` (not on the next poll iteration),
so the worst-case latency is one `poll-interval` (default 1s)
rather than one `ttlSecondsAfterFinished`. If the log read
itself fails (404 — pod already GC'd), the host falls back
to "treat as error", logs a structured warning, and the
chapter-3 §3.3 transport-failure → status=error path applies.

### 8.4 Stdout sentinel emitted from inside the user command

A buggy or hostile user `execution_command` could emit
`EDEN_OUTCOME {"status":"success", ...}` on stdout itself,
bypassing the wrapper's actual outcome read. The wrapper's
defense is to emit its own sentinel as the LAST line of pod
log, AFTER the user command has exited. The host parses the
LAST `EDEN_OUTCOME⎵`-prefixed line (with a literal trailing
space after the token), so a user-emitted sentinel is
always overridden.

This is documented in the new reference-binding chapter.

A paranoid future hardening: rotate to a per-Job random
sentinel prefix (passed to the wrapper via env, not visible
to the user command). Punted from 13b because the namespace
boundary is the trust boundary; a hostile sibling pod doesn't
exist at this isolation level. The same chunk that lifts the
trust boundary (DooD-on-k8s with sysbox, etc.) should harden
the sentinel.

### 8.5 RBAC scope: Role vs ClusterRole

The chart renders a `Role` (namespace-scoped) not a
`ClusterRole`. This means an operator who runs multiple
releases in different namespaces gets isolated executor RBAC
per release, which is the right shape (a release in
`eden-prod` shouldn't be able to manage Jobs in `eden-staging`).

The trade-off: the chart's templates can't reference resources
in other namespaces (e.g., a centrally-managed image-pull
secret). Operators with that pattern set
`executor.rbac.create=false` and supply their own
ClusterRoleBinding.

### 8.6 `kubernetes` Python client version compatibility

The official `kubernetes>=29.0.0` client supports k8s API
1.27+ (the version the chart targets per 13a §7.5). Pinning
`<30` keeps the chart's API surface stable; major-version
bumps in this client historically rename `V1Job` fields.
Documented in `pyproject.toml` and the README.

The integration test runs against kind 0.23+ (which ships
k8s 1.30 by default); local verification on k8s 1.27 + 1.28
is the operator's responsibility.

### 8.7 `wrapper.sh` portability across base images

The wrapper uses POSIX `sh` (not bash) and avoids GNU-only
flags so it runs on Alpine, Debian, RHEL, and other
distributions whose default `/bin/sh` is dash, busybox-sh,
or similar. The Decision-8 minimum-surface contract ensures
`/bin/sh`, `git`, `python3`, and `ca-certificates` are
present on every supported Job image.

All distroless images (`gcr.io/distroless/static`,
`gcr.io/distroless/base`, `gcr.io/distroless/cc`,
`gcr.io/distroless/python3`, etc.) lack `/bin/sh` by default
per the [distroless project's own README](https://github.com/GoogleContainerTools/distroless)
(only the `:debug` variants ship a shell, and those are not
intended for production use). `FROM scratch` images
similarly lack everything. 13b explicitly does NOT support
those image bases for the Job's main container; operators
using them MUST switch to a shell-bearing distribution
(Alpine, Debian-slim, Ubuntu-minimal, RHEL-UBI-minimal) OR
derive from `eden-runtime:dev` directly. A follow-up that
ships a static `eden-execute-wrapper` Go binary (no shell,
no python3, no git — the binary calls libgit2 internally)
would lift this constraint, but is out of scope here. See
§11.

### 8.8 Helm-rolling-update vs Job-in-flight

A Helm-driven rolling restart of the executor-host StatefulSet
SIGTERMs the pod mid-task. The host's `finally` block deletes
its in-flight Job (Decision 10). However, if the host pod is
SIGKILLed (SIGTERM grace period expired), the Job survives
without owner.

Mitigation: the new replica's startup-time orphan reaper
deletes Jobs labeled `eden.host=<this-host-pod-name>` whose
`task_id` is no longer in `claimed` state. The pod-name is
stable across StatefulSet rolling updates (the
`eden-executor-host-0` pod retains its name on restart),
so the reaper reliably cleans up its predecessor's leaked
Jobs. Documented in `worker-host-k8s-job.md` §"Crash recovery".

### 8.9 Executor-host pod's RBAC blast radius

The chart-managed Role grants `Job` create/get/list/watch/delete
and `Pod` get/list/watch/log in the release namespace. An
attacker who gains code execution inside the executor-host pod
could create arbitrary Jobs in that namespace (mining
cryptocurrency, etc.). This is the same shape as the Compose
DooD trust boundary: a soft isolation, not hostile-code
containment. Documented in `worker-host-k8s-job.md` §"Security
boundary".

A 13-followup may add a NetworkPolicy template that confines
Jobs to Forgejo egress only, narrowing the blast radius. Out of
scope here; NetworkPolicy needs cluster CNI support.

### 8.10 Node-disruption + duplicate-Pod semantics

Two distinct Kubernetes-side hazards bear on the chunk's
correctness story:

**Node disruption (eviction, drain, preemption).** With
`backoffLimit: 0` (Decision 1), the Job controller does NOT
reschedule a disrupted Pod; it transitions the Job to
`status.failed: 1` with a `DisruptionTarget` Pod condition.
The host's Job watch detects `failed > 0` (the §3.4 step 6
"Job failed" arm) and runs the existing chapter 3 §3.3
transport-failure mapping → `VariantSubmission(status="error")`.
The variant terminalizes as `error`; the next ideation cycle
MAY produce a successor idea/variant pair that re-attempts.
This is the same end-state as a `*_command` exit-nonzero or a
malformed outcome.json — there is no "node-loss" special case
in the host's submit logic.

A future amendment MAY use `podFailurePolicy` (GA in k8s
1.31; feature-gated in 1.27-1.30) to *ignore* `DisruptionTarget`
conditions, allowing the controller to re-create the Pod once
on infra-shaped failures. The chart targets k8s 1.27+ per 13a
§7.5, so making `podFailurePolicy` load-bearing would force a
1.31+ floor. 13b stays at the lower-common-denominator and
treats every Job-failure as host-visible, accepting
"variant-error → re-ideate" as the recovery path.

**Duplicate Pod execution.** The Kubernetes Job documentation
warns that `completions: 1` Jobs may *very rarely* run the
user program twice on the same Job — typically when the Job
controller restarts during a Pod's terminating window and
re-creates a replacement before observing the original's
exit. The host therefore cannot assume "one Job → one Pod →
read its log". The §3.4 step 6 log-read sub-step is more
precisely:

```text
1. List pods filtered by the well-known Job labels:
     batch.kubernetes.io/job-name=<job-name>
     batch.kubernetes.io/controller-uid=<job uid>
   (Older clusters expose `job-name` / `controller-uid`
   without the prefix; the host's selector uses BOTH so
   1.27+ and pre-1.27 clusters both resolve.)
2. Partition pods by `.status.phase`:
     A = phase Succeeded
     B = phase Failed
     C = phase Running | Pending | Unknown
   The host runs this read AFTER observing
   Job.status.{succeeded > 0 OR failed > 0}, so |C| should be
   0 or transient. If |C| > 0 after a 5-second settle, the
   host treats the Job as `error` (transport-shaped — k8s
   itself is in an inconsistent state) and proceeds to
   submit error.
3. Selection rule:
     |A| == 0 AND |B| > 0 → Job-failed; pick the most-
       recently-created Pod in B; read its log; classify per
       §3.4 step 6 (failed → error).
     |A| == 1 AND |B| == 0 → happy path; read that Pod's
       log; classify per §3.4 step 6 (success path).
     |A| == 1 AND |B| > 0 → one succeeded, one or more
       failed (the canonical "duplicate-Pod ran but at
       least one finished cleanly" case); read the
       succeeded Pod's log AND verify the commit_sha is
       reachable on the current Forgejo ref tip (§3.4 step
       6 reachability). If reachable, treat as success.
     |A| > 1 → at least two Pods both reported Succeeded
       independently; this is the anomaly the k8s docs
       warn about. The host reads ALL succeeded Pods'
       EDEN_OUTCOME lines, requires byte-equality across
       them, and submits success only if all agree on the
       same commit_sha. Otherwise submits error.
```

What this gives:

- *Both Pods produce the same outcome.* `|A|` is 1 or 2;
  selection rule lands on success; chapter 4 §4.2
  idempotent-resubmit applies if Phase 3 also got duplicated.
- *Pods produce different outcomes.* `|A|` is 0 or 1;
  selection rule prefers Succeeded; the `|A|>1`-with-
  disagreement branch terminalizes as error, deliberately
  conservative.
- *Both Pods OOM mid-push.* `|A|` is 0; both Pods are in B;
  selection rule reads the most recent failed Pod's log;
  treats as error.

The host does NOT attempt to dedupe Pods at the k8s level
(it would have to do leader-election among Pods, which is
not idempotent itself). It relies on the chapter 4 §4.2
submission-equivalence rule plus chapter 3 §3.3 reachability
plus the explicit `|A|>1`-with-disagreement-fails-closed
rule above to keep store-side state coherent under
duplicate-Pod execution. Documented in the new reference-
binding chapter under "Duplicate Pod handling".

## 9. Risks

1. **Per-Job pod scheduling latency drowns the smoke budget.**
   Each Job adds ~10-20s of scheduling + image-pull on a
   cold kind cluster. Three execution tasks → 30-60s extra
   on top of 13a's helm-smoke 5-minute budget. Mitigation:
   ci-values-k8sjob.yaml uses `imagePullPolicy: IfNotPresent`
   and pre-pulls `eden-runtime:dev` into the kind cluster
   via `kind load docker-image` BEFORE `helm install` (so
   the test exercises the runtime path, not the image-pull
   path). Even so, budget is 8 minutes; if the smoke flakes
   in CI, the first knob to tune is the kind cluster's
   `eden-test/fake-gpu` label cardinality (one labeled node
   means scheduling is deterministic).

2. **kubernetes-client API drift across versions.** The
   `kubernetes>=29.0.0,<30` pin is the v0 stake. A future
   chart deployed on k8s 1.32 against an older client may
   fail at install time on `apiVersion` mismatches.
   Mitigation: pin in `pyproject.toml`; bump on deliberate
   chunk boundaries; the chart README documents the supported
   k8s version range (1.27+ for the chart, 1.27-1.31 for
   the client).

3. **GPU device-plugin key churn.** NVIDIA, AMD, Intel each
   ship their own device-plugin and the limit-key string
   differs. If a future device plugin renames the key (e.g.,
   `nvidia.com/gpu` → `nvidia.com/A100`), operators have to
   update values files. Mitigation: the chart is
   vendor-agnostic by design; the README documents that
   the operator confirms the device-plugin's resource key
   matches what they're requesting.

4. **Wrapper.sh shell-injection on user-supplied
   `EDEN_EXECUTION_COMMAND`.** The wrapper invokes the
   user's command via `sh -c "${EDEN_EXECUTION_COMMAND}"`
   per §3.6. This gives the user command full shell access
   — same posture as the existing
   `--mode subprocess` flow which uses `shell=True` Popen.
   The user is the experiment author and is trusted at this
   level. No mitigation; documented in the new
   reference-binding chapter.

5. **Chart values churn during 13b implementation.** As the
   k8s_job_mode flow is built, the optimal CLI/values
   shape may differ from §3.2 / §3.3. Mitigation: 13a is
   greenfield; per AGENTS.md "no backwards-compatibility
   shims in pre-user projects", values may be renamed
   freely. The README is updated in the same PR that
   changes the values; no migration story.

6. **Re-introducing legacy vocab in chart docs / wrapper.**
   PR #60's guardrail catches the patterns. Pre-submit
   `python3 scripts/check-rename-discipline.py` clean is
   the merge gate.

7. **`helm-smoke-executor-job` flake under CI cluster
   resource constraints.** kind on GH Actions runners has
   ~7GB RAM; nine EDEN services + the per-task Job pod
   may push the limit. Mitigation: ci-values-k8sjob.yaml
   sets `replicas.taskStoreServer = 1`, `replicas.controlPlane
   = 1`, `replicas.orchestrator = 1` (no HA); fixture is
   3 ideation tasks (matches existing); job
   `resources.requests` left empty (k8s schedules
   best-effort).

8. **Spec drift on the new reference-binding chapter.** The
   k8s-job binding is a sibling document to the subprocess
   binding; if the protocol's role contract evolves
   (chapter 3) and one binding gets updated but not the
   other, the bindings drift. Mitigation: both bindings'
   "Failure modes" sections cite chapter 3 §3.3 explicitly,
   so a future chunk that touches §3.3 is discoverable
   from grep.

## 10. Sequence within the chunk

Recommended PR shape (in order):

1. **Spec / docs PR.** New informative
   `worker-host-k8s-job.md` reference-binding chapter; a
   one-line cross-reference added to `worker-host-subprocess.md`;
   `docs/deployment/helm.md` extended with the GPU
   scheduling section (mostly placeholder text — the
   running examples are added in PR 6). Runs CI's spec-xref-check
   and markdownlint. Reviewable as a "design lands first"
   foundation; nothing executable yet. Sets the protocol
   shape for the chunk.

2. **Executor-host code PR.** New `k8s_job_mode.py` plus the
   `cli.py` extensions (`--mode k8s-job`, nine `--job-*`
   flags). New `_common/k8s.py`. New unit tests. The
   executor-host StatefulSet is unchanged at the chart level;
   this PR exercises the new mode only via unit tests.
   Verifies the design works in code before any deployment
   plumbing.

3. **Chart wrapper + RBAC PR.** New
   `executor-wrapper-configmap.yaml` (with `wrapper.sh`)
   and `executor-rbac.yaml`. Both rendered conditionally;
   the existing `helm-smoke` is unaffected because
   `executor.mode` defaults to `scripted`.

4. **Chart values + StatefulSet wiring PR.** The
   `executor.mode` value, the if/then `values.schema.json`
   clause, the StatefulSet `args:` mode-conditional. Now
   `helm install ... --set executor.mode=k8sJob` produces
   a StatefulSet that passes the new flags; the host
   actually creates Jobs.

5. **CI PR.** New `ci-values-k8sjob.yaml`,
   `smoke-executor-job.sh`, the `helm-smoke-executor-job`
   GitHub Actions job, the `helm-lint-k8sjob` extension,
   the `helm-upgrade-smoke` mode-switch test.
   `helm-smoke-executor-job` is NOT branch-protection-required
   in this chunk (same posture 13a took — let it run cleanly
   for a few iterations on `main` first).

6. **Docs PR.** Worked GPU values examples (GKE A100, EKS
   A10G, on-prem nodefeature labels) added to
   `docs/deployment/helm.md`. AGENTS.md "Commands" extended.
   `docs/roadmap.md` Phase 13 entry — 13b marked complete.

A reviewer going from PR 1 to PR 6 should expect:

- PR 1 lints clean (markdownlint + spec-xref-check); no
  CI smoke regressions.
- PR 2 passes ruff + pyright + pytest; the new tests use
  mocked kubernetes-client, so they don't need a real
  cluster. The existing helm-smoke (scripted mode) is
  unaffected because the executor-host args haven't changed.
- PR 3 lints the chart (helm lint passes); the new
  conditional templates render to nothing under
  `mode: scripted`, so the existing helm-smoke is
  unaffected.
- PR 4 MAY break the existing helm-smoke if the
  StatefulSet template's mode-conditional has a typo;
  caught in PR-CI, fixed in-place.
- PR 5 runs the new helm-smoke-executor-job for the first
  time. Most likely failure modes: image-pull on kind
  (mitigated via `kind load docker-image`); pod
  scheduling timeout (tune budget); RBAC permission
  surprise (caught by the in-test assertion that Jobs
  were created).
- PR 6 lints clean; no behavioral changes.

## 11. Out of scope (followups)

- **Per-experiment GPU resources.** A non-normative
  `execution_resources` block in the experiment-config YAML
  that the host merges into the Job template per task. Lands
  after 12c's policy mechanism crystallizes — the same
  `terminationPolicy` extension surface is the right place.
- **Evaluator-as-Job.** Symmetric design; same shape minus
  the work/* push (evaluator never writes to git). Lands
  after 13b ships, probably as 13b-followup.
- **Ideator-as-Job.** Almost certainly never; the ideator's
  long-running JSON-line protocol is the wrong shape for
  Job-per-task. Ideator stays a long-running Deployment.
- **DooD-on-k8s subprocess mode.** Reserved enum slot in
  `executor.mode=subprocess`; awaits a future chunk that
  picks sysbox / per-pod docker daemon / Kata. Operators
  who need user-supplied LLM workers TODAY use the
  Compose stack until 13b lands; after 13b they use
  `executor.mode=k8sJob` with an experiment-specific image.
- **Per-experiment-namespace Jobs.** 13b uses labels in a
  shared release namespace; 12c-aligned chunk MAY move
  to per-experiment namespaces.
- **Pod-log streaming to the Web UI.** The Web UI's
  admin-task-detail page could surface live pod logs via
  k8s `pods/log` streaming; needs Web UI to gain `pods/log`
  RBAC. Operator-requested followup.
- **NetworkPolicy template.** Confines the Job pod's egress
  to Forgejo + (optionally) the operator's experiment
  external services. Needs cluster CNI support; deferred.
- **Static `eden-execute-wrapper` binary** for
  experiment images without Python. Tracked as a §8.7
  followup.
- **Per-Job random sentinel prefix** for hardening against
  user-emitted EDEN_OUTCOME lines. Tracked as a §8.4
  followup; relevant once the namespace trust boundary is
  lifted.
- **Branch protection on `helm-smoke-executor-job`.** 13a
  and prior compose-`*` / python-`*` jobs each had a "let it
  run cleanly for a few iterations" follow-up; 13b's
  helm-smoke-executor-job follows the same posture.

## 12. Estimated effort

- **Spec / docs (PR 1):** ~1 day. New
  reference-binding chapter; cross-references; placeholder
  in deployment/helm.md.
- **Executor-host code (PR 2):** ~2.5 days. New
  `k8s_job_mode.py` is the bulk; the k8s-API client
  patterns for "create Job + watch to terminal + read
  log + delete" are well-trodden but the per-task error
  classification is the chunk's most subtle code.
- **Chart wrapper + RBAC (PR 3):** ~1 day. RBAC template
  is small; wrapper.sh is shell so iteration is fast.
- **Chart values + StatefulSet wiring (PR 4):** ~1.5 days.
  values.schema.json's if/then clauses; StatefulSet's
  mode-conditional args block.
- **CI (PR 5):** ~2 days. kind cluster provisioning;
  image pre-load; per-Job kubectl assertions; budget
  tuning.
- **Docs (PR 6):** ~1 day. Worked GPU examples need
  attention to detail across cloud providers.

**Realistic total: ~9 working days** of focused work. The
heaviest single PR is PR 2 (executor-host code); PR 5 (CI)
is the one most likely to need debugging cycles because
kind's image-pull and scheduling timing differs from a
production cluster.

## 13. What lands at the end of 13b

After 13b merges, an operator on a Kubernetes cluster with GPU
nodes can:

```yaml
# values-prod.yaml
executor:
  mode: k8sJob
  jobTemplate:
    image:
      repository: ghcr.io/myorg/eden-experiment-foo
      tag: 0.1.0
    nodeSelector:
      cloud.google.com/gke-accelerator: nvidia-tesla-a100
    tolerations:
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
    resources:
      limits:
        nvidia.com/gpu: "1"
        memory: "64Gi"
```

```bash
helm upgrade eden ./reference/helm/eden -f values-prod.yaml
```

…and have every `execution_command` invocation run as a
GPU-scheduled Kubernetes Job, with the executor-host pod
itself remaining a tiny CPU-only StatefulSet that just
manages the Job lifecycle. The Compose deployment is
unaffected; operators who want DooD-on-k8s subprocess mode
continue to wait for the followup chunk that picks an
isolation mechanism.
