# Phase 13d — S3/GCS blob backend

**Status.** Draft.

**Predecessors.** [`docs/plans/eden-phase-13a-helm-base-chart.md`](eden-phase-13a-helm-base-chart.md)
(merged plan), [`docs/plans/eden-phase-13b-executor-k8s-job.md`](eden-phase-13b-executor-k8s-job.md)
(merged plan), and [`docs/plans/eden-phase-13c-managed-postgres.md`](eden-phase-13c-managed-postgres.md)
(merged plan). 13a forecast the base Helm chart with a
per-replica artifacts PVC mounted into the web-ui pod (13a §5.1's
`web-ui-statefulset.yaml`); 13c forecast the managed-Postgres
switch that codified the substrate-plan posture this chunk
inherits (operator-required values + `values.schema.json`
enforcement, no fictional defaults, secrets via `existingSecret`
for production, single-pre-composed-Secret-key contract, "no
code changes" where feasible). **None of 13a/13b/13c's
implementations have shipped yet** — the plans are merged but
`reference/helm/eden/`, `reference/scripts/setup-experiment-helm.sh`,
the new CLI flags, and the `helm-smoke*` CI jobs do not exist
in the current checkout. Whenever this plan refers to those
paths it is referring to **the substrate the predecessor plans
forecast**, not to existing code; readers verifying 13d cross-
references should consult the predecessor plan files
themselves, not the repo's current `reference/` tree. 13d will
land alongside (or after) the 13a/13b/13c implementations. 13d
replaces the forecast artifacts PVC path with an
operator-selectable blob backend (file / S3 / GCS) that
satisfies chapter-8 §5's already-normative artifact-store
contract.

**Roadmap.** [`docs/roadmap.md`](../roadmap.md) §"Phase 13 —
Kubernetes reference deployment" lists "S3/GCS blob backend"
as the fourth post-13a sub-project. 13d is the **fourth** chunk
in the substrate sequence (13a → 13b → 13c → 13d → 13e).
The `blob-init` service + `eden-blob-data` volume that previously
shipped in Compose as a Phase-13-tracked placeholder (formerly
`MANUAL_UI_ISSUES.md` §20) were removed entirely in Phase 12a-1g;
13d ships the real consumer for the post-12a-1g `${EDEN_EXPERIMENT_DATA_ROOT}/blobs`
bind-mount instead.

**Naming.** Pre-draft check against
[`docs/glossary.md`](../glossary.md) and AGENTS.md "Naming discipline":

- "S3", "GCS", "MinIO", "IRSA", "Workload Identity", "bucket",
  "prefix" are upstream vocabulary; no collisions with EDEN
  identifiers.
- The package name `eden-blob` is already reserved as a
  placeholder ([`reference/packages/eden-blob/README.md`](../../reference/packages/eden-blob/README.md));
  13d populates it. `blob` is consistent with the existing
  Compose volume name `eden-blob-data` and the placeholder's
  pyproject member name. The protocol vocabulary uses
  "artifact store" (chapter 8 §5); the implementation calls
  the package `eden-blob` because "artifact" already means a
  specific protocol-level thing (the file referenced by
  `artifacts_uri`) and "blob" is the storage-mechanism word.
  Both terms are present in the codebase; the chart values
  prefer `blob.*` because the Helm-side knob is about WHERE
  artifacts live, not WHAT they are.
- The chart's existing knob (after 13a) is
  `storage.artifactsSize` (a PVC size). 13d adds a sibling
  `blob.backend ∈ {file, s3, gcs}` discriminator plus
  per-backend sub-blocks (`blob.s3.*`, `blob.gcs.*`).
  `blob.backend=file` is the default and preserves 13a's
  PVC behavior exactly.
- The reference `Backend` Protocol method names are
  `upload(content, key) -> uri` and `fetch(uri) -> bytes`,
  matching chapter-8 §5.1's vocabulary verbatim. Existing
  helper names like `read_idea_rationale` and
  `read_variant_artifact` keep their action-on-the-artifact
  shape; new helpers `upload_idea_artifact` /
  `upload_variant_artifact` follow the same pattern (verb +
  artifact noun). Pre-submit
  `scripts/check-rename-discipline.py` clean.

## 1. Context

### 1.0 Substrate baseline (post-13a / 13b / 13c)

After 13a + 13b + 13c the reference k8s deployment runs:

- A single artifacts PVC mounted into the **web-ui** pod at
  `/var/lib/eden/artifacts` (13a Decision 3 + §5.1's
  `web-ui-statefulset.yaml`'s second `volumeClaimTemplates`
  entry sized `storage.artifactsSize`, default `50Gi`). The
  CLI flag is `--artifacts-dir /var/lib/eden/artifacts`
  ([`reference/services/web-ui/src/eden_web_ui/cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py)
  line 68).
- The web-ui's
  [`reference/services/web-ui/src/eden_web_ui/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/artifacts.py)
  writes ideator rationales to
  `<artifacts-dir>/<idea_id>.md` and emits a `file://` URI
  via `path.as_uri()`. The trust-boundary helper at
  [`reference/services/web-ui/src/eden_web_ui/routes/_helpers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py)
  lines 69-113 (`_read_inline_artifact`) enforces:
  `file://` only, contained in `--artifacts-dir`, ≤ 1 MiB,
  is_file (not symlink to a directory or socket).
- A separate `/artifacts` HTTP route at
  [`reference/services/web-ui/src/eden_web_ui/routes/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/routes/artifacts.py)
  serves any file confined to the artifacts dir, gated on
  authenticated session. This works around the
  browser-refuses-to-navigate-to-`file://`-from-`http://`
  problem.
- Worker-host artifact-write paths today (the plan's
  baseline; round-0 codex review caught the v0 misstatement
  here):
  - **Ideator subprocess host.** Per the binding at
    [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
    §"ideator subprocess outcome" and the impl at
    [`reference/services/ideator/src/eden_ideator_host/subprocess_mode.py`](../../reference/services/ideator/src/eden_ideator_host/subprocess_mode.py)
    line 250, the role emits one of two shapes per idea:
    (a) a `rationale` string — the host writes it via
    `eden_web_ui.artifacts.write_idea_artifact` to
    `<artifacts-dir>/<idea_id>.md` and emits a `file://`
    URI; (b) a final `artifacts_uri` string — the host
    passes it through verbatim. Today the host is therefore
    a write-side participant in (a).
  - **Evaluator subprocess host.** Per
    [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
    §"evaluator subprocess outcome" and the impl at
    [`reference/services/evaluator/src/eden_evaluator_host/subprocess_mode.py`](../../reference/services/evaluator/src/eden_evaluator_host/subprocess_mode.py)
    line 246, the role emits a final `artifacts_uri`
    directly; the host passes it through verbatim. Today
    the host is NOT a write-side participant — the role
    process is responsible for placing the bytes wherever
    the URI points. (For Compose deployments with a
    role-process that runs in the same container as the
    host, "wherever the URI points" is the artifacts
    volume.)
  - **Executor subprocess host.** Does NOT emit an
    artifact URI; its work is the git commit. But it is a
    READ-side participant in idea.artifacts_uri: it reads
    the URI from the dispatched idea, derives a local
    filesystem path via
    [`reference/services/executor/src/eden_executor_host/subprocess_mode.py`](../../reference/services/executor/src/eden_executor_host/subprocess_mode.py)
    line 493's `_rationale_path_from_uri` (which only
    accepts `file://` — anything else returns None), and
    writes that path into the `rationale_path` key of the
    per-task `EDEN_TASK_JSON` brief
    ([`reference/services/executor/src/eden_executor_host/subprocess_mode.py`](../../reference/services/executor/src/eden_executor_host/subprocess_mode.py)
    line 392; surface defined at
    [`spec/v0/reference-bindings/worker-host-subprocess.md`](../../spec/v0/reference-bindings/worker-host-subprocess.md)
    §"executor subprocess inputs"). The role process opens
    that path directly.
  - **Web-ui ideator route.** Calls
    `eden_web_ui.artifacts.write_idea_artifact` directly
    on operator-typed rationale text — the same helper
    the ideator subprocess host uses for shape (a). Same
    `file://` URI shape.

  So the v0 artifact-write pipeline today has TWO writers
  (web-ui's ideator route + ideator subprocess host's
  shape-(a) branch) and TWO read-side participants
  (executor host's `_rationale_path_from_uri` + the web-ui's
  `_read_inline_artifact` helper). The evaluator
  subprocess host is a passthrough on both sides. 13d's
  redesign in §3.4 enumerates each path explicitly.
- A placeholder package
  [`reference/packages/eden-blob/`](../../reference/packages/eden-blob/)
  carries only a README pointing at Phase 13. It is
  intentionally NOT a `pyproject.toml` workspace member.
- Compose ships an unconsumed `blob-init` busybox service +
  `eden-blob-data` named volume
  ([`reference/compose/compose.yaml`](../../reference/compose/compose.yaml)
  lines 72-84, 385) — created so postgres + forgejo's
  `depends_on: blob-init` succeeds. Per
  the prior `MANUAL_UI_ISSUES.md` §20 (resolved by Phase 12a-1g)
  this is a Phase-13-tracked placeholder; nothing currently
  reads or writes the volume. The actual artifacts volume
  the web-ui consumes is `eden-artifacts-data`
  ([`reference/compose/compose.yaml`](../../reference/compose/compose.yaml)
  line 359 + 409, named explicitly so the chunk-10d
  follow-up A docker-exec wrap forwards it).
- The chapter-8 §5 artifact-store contract is **already
  normative** (upload + fetch operations, durability per
  retention window, content-immutability rule); the
  protocol does NOT mandate a scheme but a conforming
  store MUST document which schemes it issues
  ([`spec/v0/08-storage.md`](../../spec/v0/08-storage.md)
  §5.1, [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md)
  §1.5). 13d does NOT change the spec; it makes the
  reference impl a first-class implementation of an
  already-pinned contract.

### 1.1 What 13d changes

13d adds:

1. A new package
   [`reference/packages/eden-blob/`](../../reference/packages/eden-blob/)
   that ships a `Backend` Protocol matching the chapter-8
   §5 operations, plus three reference implementations:
   `LocalFsBackend` (file://), `S3Backend` (s3://, with
   MinIO compatibility for testing), `GcsBackend` (gs://).
   Promoted to a workspace member.
2. A new chart values block `blob.backend ∈ {file, s3,
   gcs}` plus per-backend sub-blocks. Default `file`
   preserves 13a's PVC-backed behavior exactly. Operator
   opt-in to `s3` / `gcs` via values; chart upgrade from
   13c to 13d is a no-op for any operator who doesn't opt
   in.
3. A `values.schema.json` clause that fails `helm install` /
   `helm template` at lint time when `blob.backend ∈ {s3,
   gcs}` and required per-backend values are empty — same
   AGENTS.md substrate-plan posture 13a / 13c established.
4. Web-ui CLI gains backend-selection flags (`--blob-backend
   file|s3|gcs` plus per-backend flags) that map onto
   `eden-blob`'s `Backend` factory. The existing
   `--artifacts-dir` flag stays valid and is the
   `LocalFsBackend`'s mount-root knob.
5. Trust-boundary helper updates: `_read_inline_artifact` is
   replaced (or extended) by an `inline_artifact_text`
   that dispatches by URI scheme through the configured
   backend, applying the same size cap (≤ 1 MiB) and the
   appropriate scoping check per backend (file: containment
   in `--artifacts-dir`; s3: bucket+prefix membership; gcs:
   bucket+prefix membership). The `/artifacts` HTTP route
   gains a backend-aware path that streams from
   `Backend.fetch` when the URI scheme is non-file.
6. Worker-host services receive **per-role** changes
   (per Decision 4 + §3.4):
   - The **ideator host** gains `--blob-backend` and
     replaces its local-file write in the
     `rationale`-shape branch with `Backend.upload`. The
     `artifacts_uri`-shape passthrough is unchanged.
   - The **executor host** gains `--blob-backend` (and
     `--blob-fallback-*` for §3.6 composite mode) and
     adds a fetch-to-temp step in
     `_rationale_path_from_uri` for non-`file://` URIs.
   - The **evaluator host** stays a passthrough on the
     artifact path in v0. The CLI does NOT add
     `--blob-backend`; the host process never imports
     `eden_blob`. The evaluator POD does receive
     blob-auth wiring at the chart layer per §3.4.2 so
     the role subprocess can upload directly. Host-
     mediated evaluator upload is captured as a future
     amendment in §11.

   The subprocess JSON-line protocol's outcome-record
   shape and the `EDEN_TASK_JSON` brief surface are
   unchanged across the three hosts.
7. Auth posture: AWS uses IAM-role-via-IRSA preferred,
   static credentials via `existingSecret` as fallback; GCP
   uses Workload Identity preferred, service-account-key
   via `existingSecret` as fallback. Same `existingSecret`
   shape 13a/13c established for production deployments.
8. A migration runbook at
   `docs/deployment/migrating-to-blob-backend.md` covering
   `aws s3 sync` / `gcloud storage cp -r` shapes plus the
   delicate question of what happens to existing `file://`
   URIs in stored data (Decision 7).
9. Compose-side cleanup: the unused `blob-init` service +
   `eden-blob-data` volume are retired (the actual
   `eden-artifacts-data` volume the web-ui consumes stays).
   Compose deployments stay on `LocalFsBackend` by default;
   operators MAY override via `BLOB_BACKEND` env vars.
10. A new CI job `helm-smoke-blob-s3` that mirrors 13a's
    `helm-smoke` but pins `blob.backend=s3` against an
    in-cluster MinIO Deployment — exercising the S3 wire
    against a real S3-compatible server without needing
    AWS credentials in CI.

13d does NOT change:

- The wire protocol or any conformance scenario. Chapter 8
  §5 is already normative; backend choice doesn't surface
  on the wire (each variant's `artifacts_uri` is just a URI
  string that the role wrote).
- The Compose deployment's default behavior for any
  operator who doesn't opt into S3/GCS — they continue
  using local file paths.
- The chapter-8 §5 spec text.

### 1.2 Spec baseline + reconciliation

13d touches no normative spec text. Chapter 8 §5
([`spec/v0/08-storage.md`](../../spec/v0/08-storage.md))
is already normative on upload + fetch + durability +
content-immutability. Chapter 2 §1.5
([`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md))
permits any URI scheme but requires the deployment to
document which schemes it issues — 13d's chart values pin
the scheme per-backend, satisfying that documentation
requirement at the deployment-config level.

| Existing artifact | 13d disposition |
|---|---|
| [`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) §5 | Unchanged. The contract was already normative. |
| [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §1.5 | Unchanged. The "deployment MUST document which schemes" requirement is satisfied by the chart's values block (operator picks the backend). |
| [`reference/packages/eden-blob/README.md`](../../reference/packages/eden-blob/README.md) | Replaced. The package becomes a real workspace member with `pyproject.toml`, `src/`, `tests/`. |
| [`reference/services/web-ui/src/eden_web_ui/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/artifacts.py) | Refactored to call `Backend.upload` instead of writing to a local file directly. Same atomic-write semantics for `LocalFsBackend`; S3/GCS backends do their own atomicity. |
| [`reference/services/web-ui/src/eden_web_ui/routes/_helpers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py) lines 69-113 | Generalized. `_read_inline_artifact` becomes scheme-aware; size cap stays at 1 MiB. |
| [`reference/services/web-ui/src/eden_web_ui/routes/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/routes/artifacts.py) | Generalized to dispatch through `Backend.fetch` for non-file schemes. The trust boundary becomes per-backend rather than file-only. |
| [`reference/services/web-ui/src/eden_web_ui/cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py) | Adds `--blob-backend` plus per-backend flags. `--artifacts-dir` remains as the LocalFsBackend's root-path knob. |
| [`reference/services/ideator/`](../../reference/services/ideator/) | Adds `--blob-backend` per §3.4.1; the `rationale`-shape branch in `subprocess_mode.py` line 250 calls the new `Backend.upload`-based `write_idea_artifact`. |
| [`reference/services/executor/`](../../reference/services/executor/) | Adds `--blob-backend` (and `--blob-fallback-*` flags for §3.6 composite mode); `_rationale_path_from_uri` (line 493) gains fetch-to-temp dispatch by URI scheme per §3.4.3. The `rationale_path` written into `EDEN_TASK_JSON` (line 392) is unchanged in shape. |
| [`reference/services/evaluator/`](../../reference/services/evaluator/) | Per §3.4.2: NO functional change in v0. The CLI does NOT add `--blob-backend`; the host stays a passthrough on the evaluator's `artifacts_uri` field. The pod receives blob-auth wiring at the chart layer per §5.3 so role-side direct upload works. |
| [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) | `blob-init` + `eden-blob-data` volume removed; `eden-artifacts-data` stays as the web-ui's bind. The `depends_on: blob-init` lines on postgres + forgejo become unnecessary and are removed. |
| [`reference/compose/Dockerfile`](../../reference/compose/Dockerfile) | The `chown eden:eden /var/lib/eden/blobs` line (added during the prior §20 inline fix (file since deleted; see CHANGELOG.md Phase 12a-1g)) is removed; nothing mounts at that path anymore. |
| [`reference/helm/eden/values.yaml`](../../reference/helm/eden/values.yaml) | Adds `blob.backend`, `blob.file.*`, `blob.s3.*`, `blob.gcs.*`, and the new `blob.migration.fileFallback.*` block per §3.6. The legacy `storage.artifactsSize` becomes effective only when `blob.backend=file`. |
| [`reference/helm/eden/templates/web-ui-statefulset.yaml`](../../reference/helm/eden/templates/web-ui-statefulset.yaml) | The artifacts PVC `volumeClaimTemplates` entry becomes conditional on `blob.backend=file` OR `blob.migration.fileFallback.enabled=true` (per §3.6). When migration-mode is active, the Pod's `volumeMounts` entry is `readOnly: true`. The CLI args block adds the per-backend flags plus `--blob-fallback-*` when migration-mode is active. |

### 1.3 Naming-discipline baseline

PR #60's strengthened guardrail applies. New identifiers
introduced by 13d:

- Package: `eden_blob` (Python module name; the
  `pyproject.toml` workspace member name is `eden-blob`).
- Public surface: `Backend` Protocol; `LocalFsBackend`,
  `S3Backend`, `GcsBackend` concrete classes; `make_backend`
  factory function that maps a config dict to a `Backend`
  instance.
- CLI flags: `--blob-backend`, `--blob-s3-bucket`,
  `--blob-s3-region`, `--blob-s3-prefix`, `--blob-s3-endpoint-url`,
  `--blob-s3-access-key-id-env`, `--blob-s3-secret-access-key-env`,
  `--blob-gcs-bucket`, `--blob-gcs-prefix`,
  `--blob-gcs-credentials-file`. The `*-env` shape is the
  same one 13c used for the shared-token / store-url
  envs — the literal credential is read from an env var
  whose name is the flag's value, never the flag's value
  itself.
- Chart values: `blob.backend`, `blob.file.size`,
  `blob.file.className`, `blob.s3.bucket`, `blob.s3.region`,
  `blob.s3.prefix`, `blob.s3.endpointUrl`,
  `blob.s3.existingSecret`, `blob.s3.accessKeyIdKey`,
  `blob.s3.secretAccessKeyKey`, `blob.s3.irsa.enabled`,
  `blob.s3.irsa.roleArn`, `blob.gcs.bucket`,
  `blob.gcs.prefix`, `blob.gcs.workloadIdentity.enabled`,
  `blob.gcs.workloadIdentity.serviceAccount`,
  `blob.gcs.existingSecret`, `blob.gcs.serviceAccountKeyKey`.
- New CI job: `helm-smoke-blob-s3`.
- New runbook: `docs/deployment/migrating-to-blob-backend.md`.

None of these reintroduce retired vocabulary (`promote`,
`eval_error`, verb-on-verb helpers); pre-submit
`scripts/check-rename-discipline.py` clean.

## 2. Decisions

These are the load-bearing design calls; §3 unpacks each.

1. **Three coexisting blob backends via `blob.backend` enum,
   default `file`.** The chart's `blob.backend ∈ {file, s3,
   gcs}` switch picks among the LocalFsBackend (PVC-backed,
   13a-equivalent), S3Backend (AWS S3 + MinIO + any
   S3-compatible service), and GcsBackend (Google Cloud
   Storage). Default is `file` — chart upgrade from 13c to
   13d is a no-op for any operator who doesn't opt in.
   Rejected: making the chart S3-by-default with file as
   opt-in (would break every existing 13a/13c deployment on
   chart upgrade); shipping only S3 (operators on
   greenfield clusters need a working stack BEFORE they
   provision a bucket); shipping a "blob-store-as-a-service"
   sidecar with chart-managed MinIO (covered as an
   alternative in §3.1).

2. **Per-backend operator-required values; no fictional
   defaults.** When `blob.backend=s3`, `blob.s3.bucket` MUST
   be non-empty (and `region` SHOULD; some S3-compatible
   services don't require it). When `blob.backend=gcs`,
   `blob.gcs.bucket` MUST be non-empty.
   `values.schema.json`'s `if/then` clause enforces.
   `helm install` with `blob.backend=s3` and an empty
   bucket fails at lint time with a clear message — same
   13a/13c posture. See §3.2.

3. **`Backend` Protocol matches chapter-8 §5.1 vocabulary
   exactly.** Two methods:
   `upload(content: bytes, key: str) -> str` returns a
   URI; `fetch(uri: str) -> bytes` returns the bytes. Plus
   `exists(uri: str) -> bool` for the `/artifacts` HEAD
   path and the integrator's reachability check.
   Implementations are NOT required to support arbitrary
   key shapes — the `key` parameter is implementation-
   advisory; the Protocol's URI is what the rest of EDEN
   sees. This keeps the surface narrow and lets each
   backend pick its on-disk / on-bucket layout. See §3.3.

4. **Worker-host code changes are scoped per role's actual
   participation in the artifact-write pipeline.** Per the
   §1.0 baseline, the four artifact-touching paths today
   participate differently:
   - **Web-ui ideator route + ideator subprocess host's
     `rationale`-shape branch.** Both are write-side
     participants today (they write a local file and emit
     a `file://` URI). 13d replaces the local-file write
     with `Backend.upload`. No subprocess-protocol change
     needed — the role still emits `rationale` as a
     string; only the host's storage call changes.
   - **Evaluator subprocess host.** Today the role emits
     a final `artifacts_uri` directly; the host is a
     passthrough. 13d does NOT change this for v0 — the
     role process retains responsibility for placing the
     bytes the URI references. For Compose deployments,
     this means the role writes to the artifacts volume;
     for k8s `--mode k8s-job` deployments (13b), the role
     uploads via its own SDK. A future amendment MAY
     extend the subprocess binding to let the role emit
     a local-path string that the host uploads; that's an
     informative-spec change deferred from 13d. Captured
     in §11. See §3.4 for the v0-shape rationale.
   - **Executor subprocess host's `_rationale_path_from_uri`
     read path.** Today only accepts `file://`. 13d adds
     a fetch-to-temp step: when the URI is `s3://` /
     `gs://`, the host calls `Backend.fetch(uri)`, writes
     the bytes to a per-task temp file in a
     worktree-sibling directory, and writes that path
     into the `rationale_path` key of the per-task
     `EDEN_TASK_JSON` brief — the same surface used today.
     The temp file is cleaned up after the subprocess
     exits. The subprocess-protocol shape (the role still
     receives a filesystem path via `EDEN_TASK_JSON`'s
     `rationale_path` field, never a URI) is unchanged.
     See §3.4.

   So 13d adds CLI flag plumbing PLUS three real code
   changes: replace local-file writes with `Backend.upload`
   in the two writer paths; add fetch-to-temp in the
   executor read path. The subprocess JSON-line protocol
   shape is unchanged. The §3.4 walkthrough enumerates
   each.

5. **Auth: pod-identity preferred (IRSA / Workload
   Identity); secrets-via-existingSecret as fallback.**
   IRSA on AWS and Workload Identity on GCP are the
   correct production posture (no static credentials baked
   into images or Secrets; rotation handled by the cloud
   provider; least-privilege per pod-service-account). The
   chart MAY ALSO accept static AWS access-key-id +
   secret-access-key OR a GCP service-account-key JSON via
   `existingSecret` — for operators on clusters without
   IRSA / WI, or for local dev (kind + MinIO). Both paths
   are first-class; the runbook recommends pod-identity.
   See §3.5.

6. **`file://` URIs in stored data MUST keep resolving
   forever after migration; an explicit
   `blob.migration.fileFallback.*` block is the chart switch
   that drives the composite-backend rendering.** The
   chapter-8 §5.4 spec text ("Once a protocol-owned object
   references an artifact URI, a conforming deployment
   MUST NOT overwrite the content at that URI") + §5.2
   ("URIs MUST remain resolvable until the experiment's
   retention window elapses") means migration cannot drop
   or rewrite the `file://` URIs. The runbook's migration
   path is **two-phase**: (a) copy bytes from the local
   filesystem to the new backend; (b) the chart deploys
   with `blob.backend=s3` (or `gcs`) AND
   `blob.migration.fileFallback.enabled=true` — the latter
   is a NEW chart values switch that drives the
   composite-backend rendering. When set:
   - The web-ui StatefulSet's artifacts PVC is rendered
     even though `blob.backend != file` (gates expand to
     "render PVC iff `blob.backend=file` OR
     `blob.migration.fileFallback.enabled=true`").
   - The PVC mount in the web-ui Pod becomes `readOnly:
     true`.
   - The web-ui CLI receives `--blob-fallback-backend file
     --blob-fallback-mount-path <path>` so the runtime
     factory builds a `CompositeBackend(primary=S3,
     fallback=LocalFs(read_only=True))`.
   - The same fallback wiring extends to worker hosts
     that read artifacts (executor's
     `_rationale_path_from_uri` path per Decision 4) so
     those also dispatch by URI scheme through the
     composite.
   - When `blob.migration.fileFallback.enabled=false`
     (default), the composite backend is NOT instantiated;
     deployments run with a single backend.
   When the operator has confirmed all in-flight `file://`
   URIs are no longer being read (e.g., the experiment's
   retention window elapsed), they set
   `blob.migration.fileFallback.enabled=false` and `helm
   upgrade`; the chart unmounts the PVC. Alternative
   rejected: rewriting the URIs in the task store —
   would violate §5.4. Alternative rejected: implicit
   "always render PVC when prior PVC exists" — chart
   templates have no visibility into prior installations,
   and conditional-on-`backend=s3` would over-render for
   fresh S3 installs that have nothing to fall back to.
   Fresh S3 installs leave `fileFallback.enabled=false`
   (the default) so no PVC is provisioned. See §3.6 for
   the renderable migration mode + the values surface.

7. **The unused Compose `blob-init` + `eden-blob-data` are
   retired.** Per
   the prior `MANUAL_UI_ISSUES.md` §20 (resolved by Phase 12a-1g)
   resolution direction (b): "defer cleanup to Phase 13,
   when the consumer ships". 13d ships the consumer; the
   placeholder volume + bootstrap service get removed. The
   real artifacts volume `eden-artifacts-data` (which the
   web-ui DOES bind today) stays. The
   `chown eden:eden /var/lib/eden/blobs` line in the
   Dockerfile is removed; nothing mounts at that path
   anymore. Compose stays on `LocalFsBackend` by default;
   the new env-var knobs let operators override to S3/GCS
   on Compose if they want. See §3.7.

8. **CI: `helm-smoke-blob-s3` uses an in-cluster MinIO,
   not real AWS.** MinIO speaks the S3 wire protocol and
   runs as a single-pod Deployment in the kind cluster.
   The CI job stands up MinIO before installing the EDEN
   chart, configures `blob.backend=s3` with
   `blob.s3.endpointUrl` pointing at the MinIO Service,
   and runs the same end-state assertions as `helm-smoke`.
   This proves the S3Backend's wire works against a real
   S3-compatible server without needing AWS credentials in
   CI. GCS does NOT have an equivalent local emulator that
   speaks the real `storage.googleapis.com` wire (the
   `fake-gcs-server` project exists but its compatibility
   matrix is incomplete — see §3.10); the GcsBackend gets
   unit-test coverage via mocked `google.cloud.storage`
   clients. Manual operator verification covers the real
   GCS wire. Rejected: standing up real AWS S3 in CI
   (cost + secrets-in-CI + flaky network egress);
   skipping S3 integration tests entirely (loses the
   single biggest feasibility check the chunk needs). See
   §3.10.

9. **The trust-boundary helper extends to all schemes.**
   The 13a-era `_read_inline_artifact` only accepted
   `file://`; 13d generalizes to dispatch on scheme,
   delegating containment / size enforcement to the
   backend. The 1 MiB cap stays (it's a UI-side rendering
   choice, not a backend concern). The HTTP `/artifacts`
   route similarly streams from `Backend.fetch` for
   non-file schemes; for `file://` it keeps using
   `FileResponse` (cheaper, kernel-side sendfile). See
   §3.8.

10. **Coexistence with 13c is preserved by chart-default
    file mode.** Operators on chart 0.2.0 (13c) upgrade to
    chart 0.3.0 (13d) without changing `values.yaml`; the
    default `blob.backend=file` keeps the existing
    artifacts PVC + LocalFsBackend behavior exactly.
    Existing PVCs attach unchanged. See §3.9.

## 3. Design

### 3.1 Alternatives considered and rejected

Three architectural choices benefit from explicit
compare-and-reject paragraphs.

**Three vs four vs N backends.**

- *Three (file / s3 / gcs) — chosen:* covers the operator
  population that the roadmap targets (Compose dev + AWS
  prod + GCP prod) plus a local-PVC fallback. MinIO and
  any other S3-compatible service ride on the S3Backend
  via `blob.s3.endpointUrl`, so this is effectively four
  configurations using three implementations.
- *Two (file / s3) — rejected:* GCS deployments would have
  to layer Cloud Storage's interoperability mode (S3-style
  XML API) on top of S3Backend. Cloud Storage's S3
  emulation is incomplete (no multipart, no presigned-URL
  ergonomics, different auth model) and works for read-only
  workloads but not reliably for the upload path. Worth the
  extra ~150 lines of GcsBackend code.
- *One (chart-managed MinIO sidecar) — rejected:* would
  ship a MinIO StatefulSet as part of the chart and route
  every deployment through it. Pros: zero operator-side
  cloud config. Cons: defeats the "managed-storage off the
  cluster" goal — the data still lives on a chart-managed
  PVC, so the operator still owns durability. The chart
  becomes more complex (HA MinIO needs a cluster, or
  operators run a single-replica MinIO and we ship a
  fragile single-point-of-failure). Better to let
  operators stand up MinIO themselves if they want it, and
  point the chart's S3Backend at it via `endpointUrl`.

**Backend Protocol shape.**

- *upload + fetch + exists — chosen:* matches chapter-8
  §5.1's two operations plus the one operation the
  reachability check needs (`/artifacts` HTTP HEAD path
  and integrator's "is this URI still resolvable" check).
- *upload + fetch only (no exists) — rejected:* forces
  callers to implement existence-check via try/except on
  fetch, which (a) wastes a full round-trip on the happy
  path AND (b) confuses 404-vs-transport-error
  classification (the same trap the 13c
  reconcile_remote_orphans path hit; cite AGENTS.md
  pitfall on narrow-exception-handling-on-store-reads).
- *Larger surface (list, delete, copy) — rejected:* the
  reference impl doesn't need them today. EDEN never
  iterates buckets (every `artifacts_uri` is referenced
  from a known idea/variant) and never deletes (chapter-8
  §5.4 makes content-immutability normative). `copy` is
  occasionally useful for migration, but the
  `aws s3 sync` / `gcloud storage cp -r` shapes the
  runbook covers handle that without an EDEN-side
  primitive.
- *Async surface (`upload_async`, `fetch_async`) —
  rejected:* the sync surface is what the existing
  callers (web-ui's artifacts.py, subprocess-mode hosts)
  use today. Async would force a refactor of every
  caller. A future amendment MAY add async variants if a
  caller benefits from them.

**Single-backend at a time vs composite ("primary +
read-only fallback").**

- *Single — usually correct:* operator picks one backend;
  all uploads + reads go to it.
- *Composite — chosen for the migration window only:*
  during the embedded-PVC → managed-bucket transition
  (per Decision 6), the LocalFsBackend stays mounted
  read-only as a fallback for `file://` URIs in stored
  data. The S3Backend (or GcsBackend) is the primary;
  reads dispatch by URI scheme. Outside the migration
  window, deployments run with a single backend.

### 3.2 `blob.backend` switch

#### 3.2.1 Values surface

```yaml
blob:
  # 13d adds this knob. Default mirrors 13c's file-only behavior.
  backend: "file"             # ∈ {file, s3, gcs}

  file:
    # Active when backend=file. Sized PVC + StorageClass.
    # 13a's `storage.artifactsSize` is now `blob.file.size`
    # (with one-version backward-compat fallback per §3.9).
    size: 50Gi
    className: ""            # cluster default if empty
    # The mount path inside the web-ui pod. The web-ui's
    # --artifacts-dir CLI flag is set to this same path.
    mountPath: /var/lib/eden/artifacts

  s3:
    # Active when backend=s3. REQUIRED bucket; SHOULD region.
    bucket: ""               # required (e.g., my-eden-artifacts)
    region: ""               # SHOULD; required by AWS, optional for MinIO
    prefix: ""               # OPTIONAL; namespacing within the bucket
    # OPTIONAL endpoint URL — set for MinIO / S3-compatible
    # services (e.g., "http://minio:9000"). Empty means use
    # the AWS-default regional endpoint.
    endpointUrl: ""

    # IRSA — pod-identity preferred for AWS production.
    irsa:
      enabled: false
      # When enabled, the chart annotates the web-ui +
      # worker-host pods' ServiceAccount with
      # `eks.amazonaws.com/role-arn: <roleArn>`. Operator
      # creates the IAM role + trust policy out-of-band.
      roleArn: ""

    # Static-credentials fallback. Used when irsa.enabled=false.
    # Prefer existingSecret in production.
    existingSecret: ""       # referenced Secret name
    accessKeyIdKey: "AWS_ACCESS_KEY_ID"
    secretAccessKeyKey: "AWS_SECRET_ACCESS_KEY"

  gcs:
    # Active when backend=gcs.
    bucket: ""               # required
    prefix: ""               # OPTIONAL

    # Workload Identity — pod-identity preferred for GCP production.
    workloadIdentity:
      enabled: false
      # When enabled, the chart annotates the web-ui +
      # worker-host pods' ServiceAccount with
      # `iam.gke.io/gcp-service-account: <serviceAccount>`.
      # Operator binds the GCP SA out-of-band.
      serviceAccount: ""

    # Service-account-key fallback. Mounted as a file from
    # the existingSecret; the GcsBackend reads
    # GOOGLE_APPLICATION_CREDENTIALS to find it.
    existingSecret: ""
    serviceAccountKeyKey: "google-credentials.json"

  migration:
    # Per Decision 6 + §3.6. When fileFallback.enabled=true
    # AND blob.backend ∈ {s3, gcs}, the chart additionally
    # renders the file PVC (read-only) so legacy `file://`
    # URIs in stored data keep resolving via a
    # CompositeBackend(primary=<s3|gcs>, fallback=file).
    # Default false means deployments run with a single
    # backend.
    fileFallback:
      enabled: false
      # When fileFallback.enabled=true, these fields drive
      # the read-only PVC mount and the runtime fallback
      # CLI flags. Defaults match blob.file's primary-mode
      # values so an operator migrating from
      # backend=file → backend=s3 doesn't have to redeclare.
      size: 50Gi
      className: ""
      mountPath: /var/lib/eden/artifacts
```

#### 3.2.2 `values.schema.json` clauses

```json
{
  "if": { "properties": { "blob": { "properties": { "backend": { "const": "s3" } } } } },
  "then": {
    "properties": {
      "blob": {
        "properties": {
          "s3": {
            "required": ["bucket"],
            "properties": {
              "bucket": { "type": "string", "minLength": 1 },
              "region": { "type": "string" }
            }
          }
        }
      }
    }
  }
},
{
  "if": { "properties": { "blob": { "properties": { "backend": { "const": "gcs" } } } } },
  "then": {
    "properties": {
      "blob": {
        "properties": {
          "gcs": {
            "required": ["bucket"],
            "properties": {
              "bucket": { "type": "string", "minLength": 1 }
            }
          }
        }
      }
    }
  }
}
```

`helm install` with `blob.backend=s3` and an empty bucket
fails at lint time. Same posture for `blob.backend=gcs`.

The schema additionally enforces:

- `blob.s3.irsa.enabled=true` ↔ `blob.s3.irsa.roleArn`
  non-empty (acknowledgement: turning IRSA on without an
  ARN breaks the pod-identity path; turning it off and
  also leaving `existingSecret` empty is rejected as no
  auth is configured).
- `blob.gcs.workloadIdentity.enabled=true` ↔
  `blob.gcs.workloadIdentity.serviceAccount` non-empty.
- Exactly ONE of `irsa.enabled=true` /
  `existingSecret`-non-empty must hold for `blob.backend=s3`
  (likewise for gcs); no auth configured at all is a lint
  failure.
- `blob.migration.fileFallback.enabled=true` is rejected
  when `blob.backend=file` — composite mode is meaningful
  only when the primary backend is non-file. (A
  fileFallback under a file primary would be redundant.
  Rejecting at lint time keeps the values matrix tight.)

### 3.3 `Backend` Protocol and reference impls

#### 3.3.1 Protocol

```python
# reference/packages/eden-blob/src/eden_blob/_protocol.py
from typing import Protocol


class Backend(Protocol):
    """The artifact-store backend chapter-8 §5.1 contract.

    Every method is synchronous; existing callers (web-ui's
    artifacts.py, subprocess-mode hosts) are sync.
    """

    def upload(self, content: bytes, key: str) -> str:
        """Persist content; return a URI that fetch will resolve.

        ``key`` is implementation-advisory: backends MAY use it
        to derive the on-disk / on-bucket name. The returned
        URI is what callers persist (in idea.artifacts_uri,
        variant.artifacts_uri).

        Per chapter-8 §5.4, two uploads with the same key MUST
        either both produce the same URI (idempotent) OR
        produce distinct URIs; they MUST NOT overwrite a
        previously-uploaded URI's content. The reference
        impls take the second route — uploading the same key
        twice produces distinct URIs (versioned by content
        hash; see §3.3.2).
        """
        ...

    def fetch(self, uri: str) -> bytes:
        """Return the bytes uploaded for this URI.

        Raises NotFound if the URI was never uploaded (or has
        been pruned past the retention window — but conforming
        deployments keep URIs resolvable per §5.2). Other
        backend errors propagate as `BackendError` (transport-
        indeterminate; caller's choice of retry).
        """
        ...

    def exists(self, uri: str) -> bool:
        """True iff fetch(uri) would succeed.

        Used by the /artifacts HTTP HEAD path (so a 404 vs 5xx
        distinction can be made cheaply) and the integrator's
        reachability check. Implementations route to the
        backend's native HEAD / metadata API; for
        LocalFsBackend it's `os.path.isfile`.
        """
        ...
```

The Protocol uses `typing.Protocol` (matches the existing
`Store` Protocol shape in
[`reference/packages/eden-storage/src/eden_storage/protocol.py`](../../reference/packages/eden-storage/src/eden_storage/protocol.py)).
Two error types live alongside the Protocol:

- `class NotFound(Exception)`: the URI was never uploaded
  OR has been pruned. Distinguishable from a transport
  error so callers can route the "URI missing" branch
  separately.
- `class BackendError(Exception)`: transport-indeterminate
  failure. Callers decide retry policy. A failed fetch
  that the backend's HTTP layer reported as 5xx wraps as
  BackendError; a definitive 404 raises NotFound.

#### 3.3.2 LocalFsBackend

Mounts at the configured `mountPath` (default
`/var/lib/eden/artifacts`). On upload: writes to
`<mountPath>/<sha256(content)[:24]>/<key>` atomically
(tmp-and-rename). The URI is `file:///<absolute-path>`.

The content-addressed-by-sha256 layout makes
"upload-same-key-twice produces distinct URIs" automatic:
two uploads with different content but the same key get
different sha256 prefixes, hence different URIs. Two
uploads with identical content collide on the same path
(by design — content immutability per §5.4 means it
doesn't matter; both URIs point at the same bytes).

Trust boundary: the URI's path MUST be contained within
`mountPath`'s resolved-real-path. `fetch` and `exists`
both enforce this (mirroring the existing
`_read_inline_artifact` containment check).

#### 3.3.3 S3Backend

Uses `boto3` (already a transitive dep via FastAPI? — no,
new dep: `boto3>=1.34,<2`). On upload:
`PutObject(Bucket, Key=<prefix>/<sha256(content)[:24]>/<key>,
Body=content)`. The URI is
`s3://<bucket>/<prefix>/<sha256>/<key>`.

Auth posture:

- IRSA path: `boto3.client("s3")` picks up the
  pod-identity credentials automatically via the AWS SDK's
  default credential chain (no explicit credentials
  passed). Works because the pod's ServiceAccount has the
  `eks.amazonaws.com/role-arn` annotation that the AWS
  SDK detects.
- Static path: read `AWS_ACCESS_KEY_ID` +
  `AWS_SECRET_ACCESS_KEY` from env vars (whose names match
  the operator-supplied flag values, defaulting to those
  literals). The SDK picks them up via the env-var part of
  the credential chain.

Region resolution:

- `blob.s3.region` non-empty → passed as `region_name` to
  `boto3.client("s3", region_name=region, endpoint_url=...)`.
- `blob.s3.region` empty AND `endpointUrl` non-empty → uses
  `us-east-1` as a placeholder (the AWS SDK requires
  *some* region; MinIO accepts any region string and
  ignores it).
- `blob.s3.region` empty AND `endpointUrl` empty → picks
  up from `AWS_REGION` env (chart-injected via values
  passthrough).

Trust boundary: the URI's bucket MUST equal the configured
bucket; the URI's key MUST start with the configured
prefix. `fetch` and `exists` reject URIs that don't match
(returns NotFound, not BackendError — these aren't
transport failures, they're "this URI isn't ours").

#### 3.3.4 GcsBackend

Uses `google-cloud-storage` (new dep:
`google-cloud-storage>=2.18,<3`). On upload:
`bucket.blob(<prefix>/<sha256>/<key>).upload_from_string(content)`.
The URI is `gs://<bucket>/<prefix>/<sha256>/<key>`.

Auth posture:

- Workload Identity: the GCP SDK's default credential
  chain detects the pod's ServiceAccount-annotation-bound
  GCP SA automatically.
- Service-account-key: the operator's existingSecret is
  mounted at `/var/run/secrets/eden/gcs/<keyName>`; the
  pod's `GOOGLE_APPLICATION_CREDENTIALS` env points at
  that path; the SDK picks it up.

Trust boundary: same shape as S3Backend (URI bucket +
prefix membership).

#### 3.3.5 Factory

```python
# reference/packages/eden-blob/src/eden_blob/_factory.py
def make_backend(config: BackendConfig) -> Backend:
    """Build the configured backend.

    BackendConfig is a discriminated union: file | s3 | gcs.
    """
    if config.backend == "file":
        return LocalFsBackend(mount_path=config.file.mount_path)
    if config.backend == "s3":
        return S3Backend(
            bucket=config.s3.bucket,
            region=config.s3.region or None,
            prefix=config.s3.prefix or "",
            endpoint_url=config.s3.endpoint_url or None,
        )
    if config.backend == "gcs":
        return GcsBackend(
            bucket=config.gcs.bucket,
            prefix=config.gcs.prefix or "",
        )
    raise ValueError(f"unknown blob backend {config.backend!r}")
```

The composite "primary + fallback" backend (§3.6 migration)
is a separate `CompositeBackend` factory:

```python
def make_migration_backend(
    primary: Backend, fallback: Backend
) -> Backend:
    """Reads dispatch by URI scheme. Writes ALWAYS go to primary."""
    return CompositeBackend(primary=primary, fallback=fallback)
```

### 3.4 Worker-host integration

Per Decision 4, the four artifact-touching paths today
(documented in §1.0 baseline) participate differently;
13d's change is per-path, not uniform.

#### 3.4.1 Web-ui ideator route + ideator subprocess host's `rationale`-shape branch

Today: both call
[`reference/services/web-ui/src/eden_web_ui/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/artifacts.py)'s
`write_idea_artifact(artifacts_dir, idea_id, markdown)`,
which writes `<artifacts-dir>/<idea_id>.md` atomically and
returns a `file://` URI via `path.as_uri()`.

13d: replace `write_idea_artifact`'s body with a
`Backend.upload` call. The new helper signature:

```python
def write_idea_artifact(
    backend: Backend,
    idea_id: str,
    markdown: str,
) -> str:
    """Upload `markdown` and return the backend-issued URI."""
    return backend.upload(
        content=markdown.encode("utf-8"),
        key=f"ideas/{idea_id}.md",
    )
```

For LocalFsBackend, `upload` writes to
`<mountPath>/<sha256-prefix>/ideas/<idea_id>.md` (the
content-addressed layout per §3.3.2) and returns a
`file://` URI — the literal path shape changes (the file
is no longer at the directly-predictable
`<artifacts-dir>/<idea_id>.md` location), but trust-
boundary code is by-resolved-path-containment, not
by-name, so it remains correct. For S3Backend / GcsBackend,
the URI is `s3://...` / `gs://...`.

Subprocess-protocol shape (the ideator emits `rationale`
as a string in the per-idea outcome record) is unchanged.
Only the host's storage call changes.

#### 3.4.2 Evaluator subprocess host

Today: the role's outcome JSON emits a final
`artifacts_uri` directly; the host passes it through to
the wire submission verbatim. The role is responsible for
placing the bytes wherever the URI points.

13d v0: **unchanged**. The host stays a passthrough on
the evaluator artifact path. Why:

- Evaluator role processes today (especially in the
  k8s-job mode forecast by 13b) often run in their own
  pod with their own credentials. Letting the role
  upload directly is the correct posture there.
- For Compose deployments, the role process runs in the
  same container as the host and writes to the
  artifacts volume; the URI it emits is `file://` and
  resolves on the volume mount. 13d's
  composite-backend migration window (§3.6) keeps that
  resolvable post-migration.
- Adding host-mediated upload would require the
  subprocess binding to grow a "local-path-the-host-
  uploads" outcome shape AND the role to switch
  away from the existing `artifacts_uri` shape — an
  informative-spec change that's not load-bearing for
  v0.

Future amendment captured in §11: extend the subprocess
binding so the evaluator role MAY emit a local-path
string the host uploads. Out of 13d's scope.

What 13d DOES add for the evaluator path:

- **Chart-level credential wiring on the evaluator pod.**
  When `blob.backend ∈ {s3, gcs}`, the evaluator
  StatefulSet template (per §5.3) sets
  `serviceAccountName` to the chart-managed `eden-blob`
  SA AND mounts the same auth env (IRSA-injected
  `AWS_ROLE_ARN` / `AWS_WEB_IDENTITY_TOKEN_FILE`, OR
  static creds via `valueFrom: secretKeyRef:`, OR GCP
  Workload Identity annotations OR a mounted SA-key
  Secret). The host process never uses these credentials
  — they sit in the Pod's environment for the role
  subprocess the host spawns. So a role that runs
  `aws s3 cp ...` or imports `boto3.client("s3")` finds
  working credentials and a configured pod identity
  without any role-side config.
- **Documentation in the runbook + NOTES.txt.** When
  `blob.backend=s3` AND
  `blob.migration.fileFallback.enabled=false`, an
  evaluator role that emits a `file://` URI produces a
  URI the deployment cannot resolve. The chart's
  NOTES.txt warns; the runbook flags this as an
  operator-config error (the role MUST emit `s3://` /
  `gs://`). When `fileFallback.enabled=true`, `file://`
  evaluator URIs continue resolving via the LocalFs
  fallback during the migration window.

The evaluator host process itself stays a passthrough on
the artifact path; the pod-level wiring is what makes
role-side direct upload feasible without operator
chart-template edits.

#### 3.4.3 Executor subprocess host's `_rationale_path_from_uri` read path

Today: the host reads `idea.artifacts_uri` from the
dispatched task. The helper at
[`reference/services/executor/src/eden_executor_host/subprocess_mode.py`](../../reference/services/executor/src/eden_executor_host/subprocess_mode.py)
line 493's `_rationale_path_from_uri` only accepts
`file://` URIs (anything else returns `None`). The
returned path is written into the `rationale_path` key
of the per-task `EDEN_TASK_JSON` brief at
[`reference/services/executor/src/eden_executor_host/subprocess_mode.py`](../../reference/services/executor/src/eden_executor_host/subprocess_mode.py)
line 392. The role process reads `EDEN_TASK_JSON` and
opens the path directly.

13d: when the URI is `s3://` / `gs://` (or a
`CompositeBackend`-resolved URI per §3.6), the host
fetches the bytes via `Backend.fetch(uri)` BEFORE
spawning the subprocess, writes them to a per-task temp
file, and writes that path into the brief's
`rationale_path` field. The temp file lives in a
worktree-sibling directory (NOT inside the worktree
itself — the role's `git add` / `git commit` would
otherwise capture it). After the subprocess exits the
temp file is unlinked.

Sketch:

```python
def _rationale_path_from_uri(
    uri: str | None, backend: Backend, work_dir: Path
) -> Path | None:
    """Return a local filesystem path the user subprocess can read.

    For ``file://`` URIs: returns the parsed path directly
    (zero-copy, matches today's behavior).
    For ``s3://`` / ``gs://`` URIs: fetches via Backend,
    writes a per-task temp file under
    ``<work_dir>/.eden-rationale-<task_id>``, returns that
    path.
    Returns None if the URI is None / unrecognized scheme /
    fetch fails.
    """
    if uri is None:
        return None
    if uri.startswith("file://"):
        # Existing behavior.
        ...
    try:
        content = backend.fetch(uri)
    except (NotFound, BackendError):
        return None
    tmp = work_dir / f".eden-rationale-{uuid4().hex}"
    tmp.write_bytes(content)
    return tmp
```

The cleanup is the host's responsibility (a `try/finally`
around the subprocess invocation; the cleanup also
runs on the SIGKILL escalation branch via the existing
`Subprocess.cleanup_callback` posture documented in
chunk-10d follow-up A's `container_exec.py`).

The subprocess-protocol shape (the role still receives
a filesystem path via `EDEN_TASK_JSON`'s `rationale_path`
field, never a URI) is unchanged.

#### 3.4.4 Future-amendment scope: write-side host-mediated evaluator upload

A future amendment MAY extend the subprocess binding so
the evaluator (and ideator's pass-through-`artifacts_uri`
shape) emits a local-path string that the host uploads
via `Backend.upload`. The extension would be: the role
emits an outcome shape `{"artifacts_path": "/tmp/eval-out.json"}`
(new field) and the host transforms it to
`{"artifacts_uri": backend.upload(read_bytes(path))}`
before wire submission. Compose deployments with the
existing `artifacts_uri` shape continue working (the
binding allows EITHER shape). Out of 13d's scope to
keep this chunk focused; captured in §11.

### 3.5 Auth posture

#### 3.5.1 IRSA (AWS)

The chart's web-ui + worker-host StatefulSet/Deployment
templates render a per-pod `ServiceAccount` and annotate
it with
`eks.amazonaws.com/role-arn: {{ .Values.blob.s3.irsa.roleArn }}`
when `blob.s3.irsa.enabled=true`. The operator creates the
IAM role and its trust policy out-of-band (per
[AWS IRSA docs](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html));
the runbook walks through it.

The role's trust policy MUST include the OIDC provider
URL of the operator's EKS cluster + a `Condition` block
on the pod's ServiceAccount name. The IAM policy
attached to the role grants
`s3:PutObject + s3:GetObject + s3:HeadObject` on
`arn:aws:s3:::<bucket>/<prefix>/*` and (if prefix is
empty) `arn:aws:s3:::<bucket>/*`. The runbook supplies a
ready-made policy JSON template.

Pods get the credentials via the AWS-provided webhook
that injects `AWS_ROLE_ARN` +
`AWS_WEB_IDENTITY_TOKEN_FILE` env vars. boto3's default
credential chain picks them up automatically.

#### 3.5.2 Workload Identity (GCP)

Equivalent shape on GCP. The chart annotates the
ServiceAccount with
`iam.gke.io/gcp-service-account: {{ .Values.blob.gcs.workloadIdentity.serviceAccount }}`
when enabled. The operator creates the GCP service
account + its IAM binding to the Kubernetes ServiceAccount
out-of-band. The IAM role grants
`storage.objects.create + storage.objects.get +
storage.objects.list` on the bucket. Runbook walks
through `gcloud iam service-accounts add-iam-policy-binding`.

#### 3.5.3 Static-credentials fallback

For operators on clusters without IRSA / Workload Identity
(self-managed Kubernetes; some on-prem installs) OR for
local dev (kind + MinIO):

- AWS: the chart wires `AWS_ACCESS_KEY_ID` +
  `AWS_SECRET_ACCESS_KEY` env vars from the Secret
  referenced by `blob.s3.existingSecret` (key names from
  `blob.s3.accessKeyIdKey` / `blob.s3.secretAccessKeyKey`,
  defaulting to the literal AWS env-var names).
- GCP: the chart mounts the Secret referenced by
  `blob.gcs.existingSecret` at
  `/var/run/secrets/eden/gcs/<key>` and sets
  `GOOGLE_APPLICATION_CREDENTIALS` to that path.

Both follow the same `valueFrom: secretKeyRef:` pattern
13c established for `EDEN_STORE_URL` (no envFrom of the
whole Secret; explicit single-variable references for
clarity + ordering).

#### 3.5.4 No auth configured

Per §3.2.2's schema clause: at least one of
`irsa.enabled=true` / `existingSecret` non-empty MUST
hold when `blob.backend=s3` (likewise for gcs).
`helm install` rejects the no-auth-configured case at
lint time. The runtime path additionally verifies
credentials at S3Backend / GcsBackend init: a `head_bucket`
call is the cheapest way to confirm "we have access";
failure exits the pod with a clear "credentials not
working" message rather than failing on first upload.

### 3.6 Migration of `file://` URIs in stored data

The chapter-8 §5.4 invariant ("MUST NOT overwrite the
content at that URI") and §5.2 ("MUST remain resolvable
until the experiment's retention window elapses") together
mean that pre-existing `file://` URIs in stored ideas /
variants MUST keep resolving forever after migration —
the runbook can't just rewrite them.

Decision 6's two-phase migration, driven by the new
`blob.migration.fileFallback.enabled` chart switch:

1. **Bytes copy.** Operator runs
   `aws s3 sync /var/lib/eden/artifacts/ s3://<bucket>/<prefix>/`
   (or the GCS equivalent) to seed the new backend with a
   copy of every existing artifact. The seeded layout
   PRESERVES the `<sha256>/<key>` shape so the
   LocalFsBackend's URIs and the S3Backend's URIs both
   resolve to the same content.
2. **Composite-backend deploy.** The operator updates
   `values.yaml`:

   ```yaml
   blob:
     backend: s3            # was: file
     s3:
       bucket: my-eden-artifacts
       region: us-west-2
       prefix: prod/
       irsa:
         enabled: true
         roleArn: arn:aws:iam::123456789012:role/eden-artifacts
     migration:
       fileFallback:
         enabled: true       # NEW: opt into composite mode
         # mountPath / size / className inherit from
         # blob.file by default; override here if needed.
   ```

   `helm upgrade` re-renders:
   - Web-ui StatefulSet keeps the artifacts PVC mounted
     because `fileFallback.enabled=true`, but the volume
     mount becomes `readOnly: true`.
   - The web-ui Pod's CLI args grow
     `--blob-fallback-backend file --blob-fallback-mount-path /var/lib/eden/artifacts`.
   - The runtime factory builds
     `CompositeBackend(primary=S3, fallback=LocalFs(read_only=True))`.
   - Every NEW upload goes to S3 → URI is `s3://...`.
   - Existing `file://` URIs dispatch to the LocalFs
     fallback by URI scheme; the read-only PVC mount
     means a write-side bug can't accidentally mutate the
     fallback bytes.
   - Worker hosts that read artifacts (executor) get the
     same fallback wiring (per Decision 4) so the
     fetch-to-temp step picks the right backend.
3. **Decommission window.** The operator monitors how often
   the LocalFsBackend fallback is hit (a counter exposed
   via `/metrics` or just structured-log lines tagged
   `blob_fallback=true`). Once that's been zero for the
   experiment's retention window (per chapter-8 §5.2),
   the operator sets `blob.migration.fileFallback.enabled=false`
   and `helm upgrade`. The PVC's `volumeClaimTemplates`
   entry no longer renders; per StatefulSet semantics
   (per chunk-13a §7.1) the PVC stays behind for manual
   cleanup. The runtime factory drops the CompositeBackend
   and runs with a single primary backend.

The fresh-S3-install Path A (§3.11.1) leaves
`migration.fileFallback.enabled=false` (the default) — no
PVC, no fallback, single backend. The `helm template`
matrix covers both shapes (§6.4).

Alternative rejected: rewriting `file://` URIs to `s3://`
in the task store. Would violate §5.4 (you'd be replacing
the content that an existing protocol-owned object
references). Even if the bytes round-trip, the wire-side
read-back from any subscriber that captured the old URI
would now fail.

The runbook makes the three phases explicit + the
"continuing access via fallback" + the decommission
trigger. See §3.11.

### 3.7 Compose cleanup

the prior `MANUAL_UI_ISSUES.md` §20 (resolved by Phase 12a-1g)
documented the unconsumed `blob-init` + `eden-blob-data`
volume + the latent `/blob` vs `/blobs` Dockerfile typo
(already fixed inline). 13d removes:

- The `blob-init` service definition
  ([`reference/compose/compose.yaml`](../../reference/compose/compose.yaml)
  lines 72-84).
- The `eden-blob-data` named volume declaration (line
  385).
- The `depends_on: blob-init: condition:
  service_completed_successfully` lines on `postgres`
  (line 14-16) and `forgejo` (line 38-40).
- The `chown eden:eden /var/lib/eden/blobs` line in
  [`reference/compose/Dockerfile`](../../reference/compose/Dockerfile)
  (added by the §20 inline fix; no longer needed).

The actual artifacts volume `eden-artifacts-data`
([`reference/compose/compose.yaml`](../../reference/compose/compose.yaml)
lines 359, 409-410) STAYS — the web-ui binds it as the
`LocalFsBackend`'s mount root.

Compose stays on `blob.backend=file` by default. Operators
opt into S3/GCS via new `BLOB_BACKEND`, `BLOB_S3_BUCKET`,
etc. env vars in `.env` plus a new
`compose.blob-s3.yaml` overlay (or `.gcs.yaml`) layered via
`docker compose -f compose.yaml -f compose.blob-s3.yaml`.
The overlay shape mirrors the chunk-10d
`compose.subprocess.yaml` overlay
([`reference/compose/compose.subprocess.yaml`](../../reference/compose/compose.subprocess.yaml)).
Default plain `compose up` keeps the existing experience.

### 3.8 Trust-boundary helper extension

The 13a `_read_inline_artifact` helper at
[`reference/services/web-ui/src/eden_web_ui/routes/_helpers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/_helpers.py)
lines 69-113 only accepts `file://`. 13d generalizes it:

```python
def inline_artifact_text(
    uri: str | None, backend: Backend, max_bytes: int = 1 << 20
) -> str | None:
    """Return artifact text iff backend.fetch(uri) is small + UTF-8.

    Trust-boundary helper used by both the idea-rationale
    rendering (chunk 9c §A.1) and the variant-side artifact
    rendering (chunk 9d §A.1):

    - Backend.exists(uri) MUST return True (bucket+prefix
      containment for S3/GCS; --artifacts-dir containment
      for file).
    - Backend.fetch(uri) MUST return ≤ max_bytes.
    - The bytes MUST decode as UTF-8.
    Any of those failing returns None so the template
    renders the URI as a plain link.
    """
    if uri is None:
        return None
    try:
        if not backend.exists(uri):
            return None
        # Backends MAY support a ranged fetch (boto3's
        # GetObject Range= parameter; GCS's
        # download_as_bytes(start=, end=)) — the Backend
        # Protocol's optional `fetch_range` method (§3.3.1
        # bonus) covers it. When unavailable, fall through
        # to a full fetch + post-hoc size check.
        content = backend.fetch(uri)
    except (NotFound, BackendError):
        return None
    if len(content) > max_bytes:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None
```

The 1 MiB cap stays. The size check moves AFTER the fetch
in the no-ranged-fetch path; ranged-fetch is preferred for
large files (lets us reject before paying the full
download cost). The Protocol's `fetch_range` method is
optional; backends MAY implement it for efficiency.

The `/artifacts` HTTP route at
[`reference/services/web-ui/src/eden_web_ui/routes/artifacts.py`](../../reference/services/web-ui/src/eden_web_ui/routes/artifacts.py)
similarly dispatches. For `file://`, it keeps using
`FileResponse` (kernel-side sendfile is cheaper than
read-into-memory). For `s3://` / `gs://`, it streams
bytes from `Backend.fetch` via FastAPI's
`StreamingResponse` with a chunked content-iterator that
internally calls a hypothetical `Backend.fetch_stream` (the
Protocol's optional streaming variant).

Per-backend trust boundaries:

- File: containment in `--artifacts-dir` (existing).
- S3: bucket equals configured bucket; key starts with
  configured prefix.
- GCS: bucket equals configured bucket; object name
  starts with configured prefix.

URIs that fail the per-backend boundary check return
NotFound, NOT BackendError. The /artifacts route renders
a 404 (not 403) so we don't leak whether the bucket
exists.

### 3.9 Coexistence with 13c

Chart 0.3.0 (13d) on top of an unmodified 13c
`values.yaml`:

- `blob.backend` defaults to `file` — 13c behavior
  preserved exactly.
- `storage.artifactsSize` is now `blob.file.size`. The
  `_helpers.tpl` `eden.blob.fileSize` helper prefers the
  new namespaced key and falls back to the legacy
  `storage.artifactsSize` for one chart version. NOTES.txt
  emits a deprecation warning. Same posture 13c took for
  the embedded-Postgres-flat-keys migration (per 13c
  §3.1.2). Chart 0.4.0 (a future chunk) drops the
  fallback.
- The artifacts PVC name + StatefulSet name unchanged
  across 13c → 13d, so existing PVCs reattach without
  data loss.

### 3.10 CI: `helm-smoke-blob-s3` (MinIO-backed)

A new CI job mirrors 13a's `helm-smoke` but uses
`blob.backend=s3` against an in-cluster MinIO. The job
lives in [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml):

```yaml
helm-smoke-blob-s3:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - uses: docker/setup-buildx-action@v3
    - uses: helm/kind-action@v1.10
    - uses: azure/setup-helm@v3
    - name: Build + load chart's image into kind
      run: |
        docker build -t eden-reference:dev \
          -f reference/compose/Dockerfile .
        kind load docker-image eden-reference:dev
    - name: Deploy MinIO inside the kind cluster
      run: |
        kubectl create namespace eden-test
        # Single-pod MinIO Deployment + Service. The Service
        # name `minio` is what the EDEN chart's
        # blob.s3.endpointUrl points at.
        kubectl -n eden-test apply -f \
          reference/helm/eden/ci/minio-deployment.yaml
        kubectl -n eden-test wait --for=condition=available \
          deployment/minio --timeout 120s
        # Pre-create the bucket via the mc client (no IRSA
        # needed against MinIO; uses the static admin creds
        # MinIO comes up with).
        kubectl -n eden-test run -i --rm --restart=Never \
          mc-init --image=minio/mc:RELEASE.2024-12-08T19-12-09Z \
          --command -- /bin/sh -c \
          'mc alias set local http://minio:9000 minioadmin minioadmin
           mc mb local/eden-artifacts'
    - name: Helm install with blob.backend=s3
      run: |
        # MinIO admin creds become the EDEN deployment's
        # static credentials via existingSecret.
        kubectl -n eden-test create secret generic eden-blob-s3 \
          --from-literal=AWS_ACCESS_KEY_ID=minioadmin \
          --from-literal=AWS_SECRET_ACCESS_KEY=minioadmin
        helm install eden ./reference/helm/eden \
          -f reference/helm/eden/ci-values-blob-s3.yaml \
          -n eden-test --wait --timeout 5m
    - name: Run setup-experiment + assert end-state
      run: |
        bash reference/scripts/setup-experiment-helm.sh \
          --namespace eden-test \
          --experiment-config tests/fixtures/experiment/.eden/config.yaml \
          --experiment-id exp-1
        # Same end-state assertions as helm-smoke (13a §3.6).
        # Plus: assert at least one s3://eden-artifacts/...
        # URI shows up on a stored idea (proves the upload
        # path actually wrote to MinIO).
        kubectl -n eden-test exec deployment/eden-task-store-server \
          -- env "TOKEN=${TOKEN}" "EID=exp-1" bash -c '
            curl -s -H "Authorization: Bearer $TOKEN" \
              "http://localhost:8080/v0/experiments/$EID/ideas" \
            | jq ".[] | select(.artifacts_uri | startswith(\"s3://\"))" \
            | head -1'
    - name: Cleanup
      if: always()
      run: |
        helm uninstall eden -n eden-test || true
        kind delete cluster
```

`ci-values-blob-s3.yaml` (new) sets:

```yaml
blob:
  backend: s3
  s3:
    bucket: eden-artifacts
    region: us-east-1   # MinIO accepts any non-empty region
    endpointUrl: http://minio:9000
    irsa:
      enabled: false
    existingSecret: eden-blob-s3
```

`reference/helm/eden/ci/minio-deployment.yaml` (new) is
out-of-tree (NOT under `templates/`) — it's a CI helper,
not a chart template (per the AGENTS.md substrate-plan
posture: per-experiment-or-per-CI artifacts live outside
`templates/`).

GcsBackend's CI coverage is unit-only because no
high-fidelity local emulator exists for the real
`storage.googleapis.com` wire. The `fake-gcs-server`
project is read-mostly; `fake-storage` etc. have known
gaps on auth, multipart, and conditional-headers. Manual
operator verification covers the production GCS path.
The runbook's verification-checklist section walks the
operator through a one-shot upload + fetch test against
their bucket before declaring the migration complete.

Branch protection is **not** updated to require
`helm-smoke-blob-s3` in 13d (same posture
13a / 13b / 13c took for newly-added CI jobs).

### 3.11 Migration runbook

`docs/deployment/migrating-to-blob-backend.md` covers:

#### 3.11.1 Path A: fresh deployment on S3

Operator-side prerequisites: an S3 bucket they own; an
IAM role configured for IRSA OR a static-credentials
Secret. Walk-through:

1. Create the bucket via the AWS console / CLI.
2. Configure IRSA: create the IAM role with the trust
   policy + the bucket-scoped policy (runbook supplies
   ready-made JSON templates).
3. Set chart values:

   ```yaml
   blob:
     backend: s3
     s3:
       bucket: my-eden-artifacts
       region: us-west-2
       prefix: prod/
       irsa:
         enabled: true
         roleArn: arn:aws:iam::123456789012:role/eden-artifacts
   ```

4. `helm install eden ./reference/helm/eden -f values.yaml`.
5. Verify on first idea-write: the resulting
   `artifacts_uri` starts with
   `s3://my-eden-artifacts/prod/`.

Sibling Path A' for GCS uses Workload Identity + a GCS
bucket; same shape.

#### 3.11.2 Path B: migrating an existing 13c deployment

Per Decision 6's two-phase plan:

1. **Drain artifact writers (worker hosts + web-ui's
   ideator).** Same shape as 13c's drain (scale to 0,
   operator-reclaim any stuck claims). The
   task-store-server stays up.
2. **Bytes copy.** From the operator's workstation. The
   sync is a one-way mirror of the legacy on-disk tree
   into the bucket; the on-disk tree is whatever the
   pre-13d writers produced (flat `<idea_id>.md` from
   the web-ui ideator route plus the ideator host's
   `rationale`-shape branch). The composite backend's
   read-side dispatch is by URI scheme, NOT by path-shape
   parity — pre-existing `file://` URIs continue resolving
   via the LocalFs fallback regardless of where they
   live in the source tree. The bucket layout from this
   sync is for forward-compat (so a future GC of the PVC
   doesn't lose the bytes), not for resolving the old
   URIs.

   ```bash
   # Tunnel the artifacts PVC to the workstation via
   # `kubectl cp` (or temporarily expose it via a debug
   # pod). Then copy to S3.
   kubectl -n eden-prod cp \
     eden-web-ui-0:/var/lib/eden/artifacts ./artifacts-dump

   aws s3 sync ./artifacts-dump/ s3://my-eden-artifacts/prod/ \
     --no-progress
   # OR for GCS:
   gcloud storage cp -r ./artifacts-dump/ gs://my-eden-artifacts/prod/
   ```

3. **Update chart values to composite mode.** Set
   `blob.backend=s3` (or `gcs`) AND
   `blob.migration.fileFallback.enabled=true` per §3.6.
   The chart re-renders: artifacts PVC stays mounted
   (now `readOnly: true`); the web-ui Pod's CLI args grow
   `--blob-fallback-backend file --blob-fallback-mount-path /var/lib/eden/artifacts`;
   the runtime factory builds
   `CompositeBackend(primary=S3, fallback=LocalFs(read_only=True))`.
   `helm upgrade`; bring workers back up.
4. **Soak.** Run for the experiment's retention window
   (chapter-8 §5.2 minimum). Watch the
   `blob_fallback=true` log line counter. Once that's been
   zero for an entire retention period, the operator
   knows nothing reads `file://` URIs anymore.
5. **Decommission.** Set
   `blob.migration.fileFallback.enabled=false` and
   `helm upgrade`. The chart's `volumeClaimTemplates`
   entry stops rendering; per StatefulSet semantics
   (chunk-13a §7.1) the PVC stays behind for manual
   cleanup. `kubectl delete pvc -n eden-prod
   <pvc-name>` reclaims the storage out-of-band per the
   cluster's StorageClass `reclaimPolicy`.

#### 3.11.3 Why a runbook, not a script

Same reason as 13c (substrate-plan pitfall on multi-tenant
lifecycle): the operator's bucket-creation auth, IAM
shape, network topology, retention window are
deployment-specific. A script would be a per-provider
matrix or a least-common-denominator that doesn't help
anyone.

### 3.12 Conformance impact

13d does not change the wire surface, the protocol, or any
spec text. Chapter 8 §5 was already normative; conformance
scenarios are binding-agnostic per chapter 9 §6. A
deployment running on any of the three backends passes the
same scenarios as a deployment running on the others. No
new conformance scenarios are added in 13d.

The `helm-smoke-blob-s3` CI job is the substrate-level
proof; it asserts the same end-state invariants the
existing helm smokes do (variant.integrated count,
task.completed count) plus the new "at least one s3://
URI shows up" check.

## 4. Scope

### 4.1 In scope

Package implementation:

- New `reference/packages/eden-blob/` workspace member with
  `pyproject.toml`, `src/eden_blob/`, `tests/`. Promoted
  from placeholder; the README is replaced.
- `Backend` Protocol + `LocalFsBackend` + `S3Backend` +
  `GcsBackend` + `CompositeBackend` + `make_backend` /
  `make_migration_backend` factories.
- New deps: `boto3>=1.34,<2`,
  `google-cloud-storage>=2.18,<3`. Both pinned to specific
  major versions per the existing dep-version posture.

Service integration:

- `reference/services/web-ui/src/eden_web_ui/artifacts.py`
  refactored to call `Backend.upload`.
- `_read_inline_artifact` → `inline_artifact_text` per
  §3.8.
- `routes/artifacts.py` dispatches by URI scheme.
- `cli.py` adds `--blob-backend` plus per-backend flags.
- Worker hosts get per-role plumbing per §3.4: ideator
  and executor wire `--blob-backend`; evaluator host CLI
  is unchanged in v0. The evaluator's k8s POD however
  receives the same SA + auth env wiring as the other
  blob-touching pods (per §3.4.2 + §5.3) so role-side
  direct upload works on `blob.backend ∈ {s3, gcs}`
  deployments — the evaluator HOST process remains a
  passthrough; the POD's credentials are for the role
  process the host spawns.

Chart additions:

- `reference/helm/eden/values.yaml`: add `blob.*` block
  including `blob.backend`, `blob.{file,s3,gcs}.*`, and
  the new `blob.migration.fileFallback.*` block per §3.6;
  rename `storage.artifactsSize` → `blob.file.size` (with
  one-version backward-compat fallback per §3.9).
- `reference/helm/eden/values.schema.json`: add `if/then`
  clauses per §3.2.2 (auth XOR; backend-specific required
  fields; fileFallback rejection under `backend=file`).
- `reference/helm/eden/templates/_helpers.tpl`: add
  `eden.blob.serviceAccountName` /
  `eden.blob.serviceAccountAnnotations` (IRSA / WI),
  `eden.blob.envVars` (static creds), `eden.blob.fileSize`
  (legacy fallback).
- Chart templates: web-ui StatefulSet's artifacts PVC
  becomes conditional on `blob.backend=file` OR
  `blob.migration.fileFallback.enabled=true` (per §3.6 —
  migration-mode keeps the PVC mounted read-only).
  Worker-host templates: ideator + executor templates get
  per-backend wiring; evaluator template is unchanged in
  v0 per §3.4.2. New ServiceAccount templates with
  IRSA / WI annotations.
- `reference/helm/eden/templates/serviceaccount-blob.yaml`
  (new) — per §3.5.1 / §3.5.2.
- `reference/helm/eden/ci-values-blob-s3.yaml` (new) — for
  the helm-smoke-blob-s3 job.
- `reference/helm/eden/ci/minio-deployment.yaml` (new,
  out-of-tree, not under `templates/`).

Compose cleanup:

- `reference/compose/compose.yaml`: remove `blob-init`
  service + `eden-blob-data` volume + the
  `depends_on: blob-init` lines.
- `reference/compose/Dockerfile`: remove the
  `chown eden:eden /var/lib/eden/blobs` line.
- `reference/compose/compose.blob-s3.yaml` (new) — opt-in
  overlay for operators who want S3 on Compose.
- `reference/compose/compose.blob-gcs.yaml` (new).
- `reference/compose/.env.example`: document new
  `BLOB_BACKEND`, `BLOB_S3_BUCKET`, etc. variables.
- `reference/scripts/setup-experiment/setup-experiment.sh`:
  no change needed (`.env` rendering uses operator-supplied
  values; the new vars default to backend=file).
- §20 (no longer tracked; resolved by Phase 12a-1g — see CHANGELOG.md).

CI:

- New `helm-smoke-blob-s3` job per §3.10.
- The existing `compose-smoke*` jobs continue passing
  unchanged.

Docs:

- `docs/deployment/migrating-to-blob-backend.md` (new)
  per §3.11.
- `docs/deployment/helm.md` (extend) — document
  `blob.backend` reference, IRSA / WI guidance, the
  composite-backend migration window.
- `AGENTS.md` "Commands" table: add the helm-smoke-blob-s3
  invocation alongside the existing helm-smoke entries.
- `docs/roadmap.md` Phase 13 entry: mark 13d complete;
  cross-link to this plan.

### 4.2 Cross-references to followups

- **Forgejo auth + per-branch ACLs + native PR review** —
  13e. Independent of blob backend; touches Forgejo.
- **Multi-region / cross-region replication** — future
  amendment; provider-side concern.
- **Backend-side encryption key management** — future
  amendment; operator concern (most providers default
  to provider-managed keys).
- **Lifecycle policies (TTL / cold-storage)** — future
  amendment; operator concern (provider-side bucket
  lifecycle rules, not EDEN's domain).

### 4.3 Out of scope

- **Backend-side encryption key management.** Operators
  configure SSE-S3 / SSE-KMS / CMEK at the bucket level
  out-of-band. EDEN doesn't pass `ServerSideEncryption`
  parameters today; if an operator's bucket policy
  requires SSE-KMS, the operator wires it via the bucket
  policy or via a future amendment.
- **Cross-region replication.** Provider-side bucket
  replication; no chart changes in scope.
- **Lifecycle policies (TTL, cold storage).** The
  chapter-8 §5.2 retention floor + the operator's bucket
  lifecycle config handle this; EDEN doesn't manage it.
- **Async backend variants.** Sync surface only in v0.
- **Bundled MinIO chart.** Operators stand up MinIO
  themselves if they want it.
- **The `eden_blob` package on PyPI.** The package stays
  a workspace member only; not a separately-published
  artifact in v0.

### 4.4 Non-goals

- **Backend-managed retention enforcement.** Chapter-8
  §5.2 specifies a retention floor; EDEN does not enforce
  it, the operator does. (A backend-side TTL config would
  enforce; that's the bucket-lifecycle-rules out-of-scope
  item.)
- **Cross-backend migration tooling beyond the runbook.**
  S3 → GCS, GCS → S3, etc. — same shape as the
  PVC → S3 runbook, just substitute the source/dest
  shapes.
- **Strict resource requests/limits on the backend
  client's container.** boto3 / google-cloud-storage are
  in-process Python libs; no separate container.

## 5. Files to touch

### 5.1 New package

| File | Change |
|---|---|
| `reference/packages/eden-blob/pyproject.toml` (new) | Workspace member declaration; deps `boto3>=1.34,<2` + `google-cloud-storage>=2.18,<3`. |
| `reference/packages/eden-blob/src/eden_blob/__init__.py` (new) | Public re-exports: `Backend`, `LocalFsBackend`, `S3Backend`, `GcsBackend`, `CompositeBackend`, `BackendConfig`, `make_backend`, `make_migration_backend`, `NotFound`, `BackendError`. |
| `reference/packages/eden-blob/src/eden_blob/_protocol.py` (new) | `Backend` Protocol + `NotFound` / `BackendError` exception types. |
| `reference/packages/eden-blob/src/eden_blob/_local.py` (new) | `LocalFsBackend` per §3.3.2. |
| `reference/packages/eden-blob/src/eden_blob/_s3.py` (new) | `S3Backend` per §3.3.3. |
| `reference/packages/eden-blob/src/eden_blob/_gcs.py` (new) | `GcsBackend` per §3.3.4. |
| `reference/packages/eden-blob/src/eden_blob/_composite.py` (new) | `CompositeBackend` per §3.6. |
| `reference/packages/eden-blob/src/eden_blob/_factory.py` (new) | `BackendConfig` (Pydantic discriminated union) + `make_backend` + `make_migration_backend`. |
| `reference/packages/eden-blob/tests/` (new) | Unit tests per backend; LocalFs against tmp_path; S3 against `moto` (mock-S3 library) with a `pytest.mark.s3_integration` marker for tests that point at a real MinIO via `EDEN_TEST_S3_ENDPOINT_URL` env var; GCS against mocked `google.cloud.storage`. |
| `reference/packages/eden-blob/README.md` | Replace placeholder with usage docs (Protocol surface, factory shape, integration with the chart). |
| `pyproject.toml` (workspace root) | Add `eden-blob` to `tool.uv.workspace` members + the dev group's package list. |
| `reference/compose/Dockerfile` | Add a `RUN uv pip install ...` step for the new boto3 + google-cloud-storage deps (or rely on `uv sync --all-packages` already pulling them via the workspace). |

### 5.2 Service integration

| File | Change |
|---|---|
| `reference/services/web-ui/src/eden_web_ui/artifacts.py` | Refactored to call `Backend.upload`; takes `backend: Backend` instead of `artifacts_dir: Path`. |
| `reference/services/web-ui/src/eden_web_ui/routes/_helpers.py` | `_read_inline_artifact` becomes `inline_artifact_text(uri, backend, max_bytes)` per §3.8. `read_idea_rationale` and `read_variant_artifact` updated to pass through. |
| `reference/services/web-ui/src/eden_web_ui/routes/artifacts.py` | Dispatch by URI scheme: `file://` keeps `FileResponse`; `s3://` / `gs://` use `Backend.fetch` (full-fetch in v0; `fetch_stream` is a future amendment). |
| `reference/services/web-ui/src/eden_web_ui/cli.py` | Add `--blob-backend` plus per-backend flags. `--artifacts-dir` becomes the LocalFsBackend's mount-path knob (still required when `--blob-backend file`). |
| `reference/services/web-ui/src/eden_web_ui/app.py` | Wire `Backend` into `app.state` (replaces `app.state.artifacts_dir`). |
| `reference/services/ideator/src/eden_ideator_host/cli.py` | Add `--blob-backend` plumbing per §3.4.1. |
| `reference/services/ideator/src/eden_ideator_host/subprocess_mode.py` | The `rationale`-shape branch (line 250) calls the new `Backend.upload`-based `write_idea_artifact` instead of the local-file helper. The `artifacts_uri`-shape passthrough is unchanged. |
| `reference/services/executor/src/eden_executor_host/cli.py` | Add `--blob-backend` (and `--blob-fallback-*` flags for §3.6 composite mode). |
| `reference/services/executor/src/eden_executor_host/subprocess_mode.py` | Update `_rationale_path_from_uri` (line 493) per §3.4.3: dispatch by URI scheme, fetch-to-temp for `s3://` / `gs://`, register cleanup. |
| `reference/services/evaluator/src/eden_evaluator_host/cli.py` | NO `--blob-backend` flag in v0 — the evaluator host is a passthrough on the artifact path per §3.4.2. The host process never imports `eden_blob`. |
| `reference/services/evaluator/src/eden_evaluator_host/subprocess_mode.py` | Unchanged in v0 per §3.4.2. |
| `reference/services/_common/src/eden_service_common/blob_args.py` (new) | Shared argparse helper that adds the `--blob-*` flags. Used by web-ui + ideator + executor CLIs (per §3.4.1 / §3.4.3); NOT used by the evaluator CLI in v0 per §3.4.2. |

### 5.3 Chart additions

| File | Change |
|---|---|
| `reference/helm/eden/values.yaml` | Add `blob.backend` (default `file`); add `blob.file.*`, `blob.s3.*`, `blob.gcs.*` blocks per §3.2.1. Move `storage.artifactsSize` → `blob.file.size` with one-version backward-compat fallback. |
| `reference/helm/eden/values.schema.json` | Add `blob.backend` enum; per-backend `if/then` requirements per §3.2.2; `irsa.enabled ↔ roleArn` paired-requirement; `workloadIdentity.enabled ↔ serviceAccount` paired-requirement; auth-configured-XOR per §3.5.4. |
| `reference/helm/eden/templates/_helpers.tpl` | Add `eden.blob.fileSize` (with legacy fallback), `eden.blob.serviceAccountAnnotations` (IRSA / WI), `eden.blob.envVars` (static creds). |
| `reference/helm/eden/templates/serviceaccount-blob.yaml` (new) | ServiceAccount + (when irsa.enabled OR workloadIdentity.enabled) the cloud-provider annotation. |
| `reference/helm/eden/templates/web-ui-statefulset.yaml` | Artifacts PVC `volumeClaimTemplates` entry conditional on `blob.backend=file` OR `blob.migration.fileFallback.enabled=true` (per §3.6 — the migration mode renders the PVC even when the primary backend is non-file). When `blob.migration.fileFallback.enabled=true`, the Pod's `volumeMounts` entry for the artifacts volume is `readOnly: true`. ServiceAccountName references the new `eden-blob` SA when backend ∈ {s3, gcs}. CLI args block adds the per-backend flags via `eden.blob.cliArgs` helper, plus `--blob-fallback-*` flags when migration mode is active. |
| `reference/helm/eden/templates/ideator-host-deployment.yaml` | Per-backend CLI args + ServiceAccountName (when backend ∈ {s3, gcs}) per §3.4.1. |
| `reference/helm/eden/templates/executor-host-statefulset.yaml` | Per-backend CLI args + `--blob-fallback-*` when migration-mode active + ServiceAccountName per §3.4.3. |
| `reference/helm/eden/templates/evaluator-host-statefulset.yaml` | Host CLI args unchanged in v0 per §3.4.2 (the evaluator host is a passthrough on the artifact path). BUT the Pod still gets the same SA + auth wiring as the other blob-touching pods when `blob.backend ∈ {s3, gcs}` so the role process the host spawns can upload directly: `serviceAccountName` references the new `eden-blob` SA; static-cred env vars (when `existingSecret` is set) are mounted into the role process's environment via the same envFrom shape used for ideator + executor. The host process never imports `eden_blob` and never calls `Backend.upload`; the credentials are for the role subprocess. |

### 5.4 Compose changes

| File | Change |
|---|---|
| `reference/compose/compose.yaml` | Remove `blob-init` service (lines 72-84); remove `eden-blob-data` volume (line 385); remove `depends_on: blob-init: condition: service_completed_successfully` from `postgres` (lines 14-16) and `forgejo` (lines 38-40). Keep `eden-artifacts-data` and the web-ui's bind. |
| `reference/compose/Dockerfile` | Remove the `chown eden:eden /var/lib/eden/blobs` line (line 62, added by §20's inline fix). The `chown` for `/var/lib/eden/artifacts` STAYS — that volume's mount is the LocalFsBackend's root. |
| `reference/compose/compose.blob-s3.yaml` (new) | Opt-in overlay setting `BLOB_BACKEND=s3` env vars on web-ui + worker-host services. |
| `reference/compose/compose.blob-gcs.yaml` (new) | Sibling for GCS. |
| `reference/compose/.env.example` | Document new `BLOB_BACKEND`, `BLOB_S3_BUCKET`, `BLOB_S3_REGION`, etc. variables (commented-out by default). |

### 5.5 New CI

| File | Change |
|---|---|
| `.github/workflows/ci.yml` | Add `helm-smoke-blob-s3` job per §3.10. Add an `eden-blob-tests` job that runs `uv run pytest reference/packages/eden-blob/tests/` against `moto` for S3 and mocked GCS. |
| `reference/helm/eden/ci/minio-deployment.yaml` (new) | Out-of-tree MinIO Deployment + Service for the helm-smoke-blob-s3 job. NOT under `templates/` per the AGENTS.md substrate-plan posture. |

### 5.6 Docs

| File | Change |
|---|---|
| `docs/deployment/migrating-to-blob-backend.md` (new) | Full runbook per §3.11 (Path A fresh + Path B migration). Includes IAM-policy + Workload-Identity-binding templates. |
| `docs/deployment/helm.md` (extend) | Add a "Blob backend" section under the values reference; cover `file` (default, 13a behavior) / `s3` / `gcs`. Cross-reference the migration runbook. |
| §20 (historical) | Already resolved by Phase 12a-1g; this row retained for plan-history continuity. |
| `docs/roadmap.md` Phase 13 entry | Mark 13d complete; cross-link to this plan. |
| `reference/packages/eden-blob/README.md` | Replaces the placeholder with a real package README. |

## 6. Test design

### 6.1 Unit tests (per backend)

`reference/packages/eden-blob/tests/`:

- `test_local_backend.py` — round-trip upload/fetch/exists
  against tmp_path; trust-boundary containment (URIs
  outside the mount path raise NotFound); content-
  addressed-by-sha256 layout verified; atomic-write
  (tmp + rename) verified.
- `test_s3_backend.py` — round-trip against `moto`'s
  mocked S3 (no network); auth-via-env-vars verified;
  bucket+prefix containment verified; 404-vs-5xx routing
  verified; `pytest.mark.s3_integration` test row that
  points at a live MinIO when `EDEN_TEST_S3_ENDPOINT_URL`
  is set (mirrors the existing `EDEN_TEST_POSTGRES_DSN`
  posture).
- `test_gcs_backend.py` — round-trip against
  `unittest.mock`-patched `google.cloud.storage` clients
  (no network).
- `test_composite_backend.py` — read dispatch by URI
  scheme; write always to primary; fallback NotFound
  routing.
- `test_factory.py` — `make_backend` dispatches correctly
  by `BackendConfig.backend`; rejects unknown values;
  rejects S3 config with empty bucket.

### 6.2 Service integration tests

- `reference/services/web-ui/tests/test_artifacts_routes.py`
  — exercise the `/artifacts` route against each backend
  (file via tmp_path; s3 via `moto`).
- `reference/services/web-ui/tests/test_ideator_artifacts.py`
  — the ideator route's artifact-write path uses
  `Backend.upload` correctly; the resulting URI shape
  matches what `inline_artifact_text` consumes.
- `reference/services/{executor,evaluator,ideator}/tests/`
  — each gets a row that drives the subprocess flow with
  `--blob-backend file` (default) and one with
  `--blob-backend s3` (against `moto`).

### 6.3 `helm-smoke-blob-s3` integration test

Per §3.10. Mirrors `helm-smoke`'s shape with the MinIO
preamble. End-state assertions match `helm-smoke` plus the
new "at least one s3:// URI" check.

### 6.4 `helm template` matrix

Lint-time assertions:

- `blob.backend=file` (default) — renders the artifacts
  PVC + LocalFs CLI args. NO composite-backend args.
- `blob.backend=s3` + IRSA + `migration.fileFallback.enabled=false`
  — renders the SA annotation; no PVC; CLI args do NOT
  include `--blob-fallback-*`.
- `blob.backend=s3` + IRSA + `migration.fileFallback.enabled=true`
  (Path B migration mode) — renders the SA annotation
  AND the PVC with `readOnly: true`; CLI args include
  `--blob-fallback-backend file --blob-fallback-mount-path /var/lib/eden/artifacts`.
- `blob.backend=s3` + static creds — renders the env vars
  with `valueFrom: secretKeyRef:`; no SA annotation.
- `blob.backend=s3` + neither IRSA nor existingSecret —
  install fails at lint time per §3.5.4.
- `blob.backend=s3` + both IRSA and existingSecret —
  install fails at lint time (the schema's mutex clause).
- `blob.backend=gcs` + WI — renders the SA annotation
  with the `iam.gke.io/gcp-service-account` shape.
- `blob.backend=gcs` + WI + `migration.fileFallback.enabled=true`
  — same as the S3 migration combination, but with
  GCS-side annotations.
- `blob.backend=s3` + empty bucket — install fails at
  lint time per §3.2.2.
- `blob.backend=file` + `migration.fileFallback.enabled=true`
  — install fails at lint time (composite mode is
  meaningful only when the primary is non-file; the
  schema's `if/then` rejects the combo).

Each combination runs in the existing `helm-lint` fast CI
job (introduced in 13a §6.1).

### 6.5 Migration runbook walk-through (manual)

The runbook itself is unit-equivalent-tested by walking
the §3.11.2 Path B steps against a local kind + MinIO
setup. The reviewer verifies once before merge.

### 6.6 Verification gates

Before merge:

- `helm lint reference/helm/eden` passes for all matrix
  combinations.
- `helm template ... | kubectl apply --dry-run=client -f -`
  passes for each backend.
- `helm-lint` CI job passes (fast).
- `helm-smoke` (existing 13a embedded smoke) continues
  passing.
- `helm-smoke-blob-s3` (new) passes.
- `eden-blob-tests` (new) passes.
- All existing CI jobs continue passing
  (`compose-smoke*`, conformance, lint, type-check).
- `python3 scripts/check-rename-discipline.py` clean.
- `python3 scripts/spec-xref-check.py` clean.
- `npx --yes markdownlint-cli2@0.14.0 docs/plans/eden-phase-13d-blob-backend.md docs/deployment/migrating-to-blob-backend.md` passes.
- Manual verification: install on a local kind + MinIO
  (and optionally a real GCS bucket); walk through the
  README's quickstart; confirm round-trip works.

## 7. Verification gates

Same as §6.6 (consolidated above).

## 8. Tricky areas

### 8.1 `boto3`'s eager auth resolution at client construction

`boto3.client("s3", ...)` does NOT validate credentials at
construction time — it only validates on the first API
call. So a misconfigured IRSA role or empty
existingSecret produces a Pod that starts cleanly but
fails on first upload. To catch this at startup, the
S3Backend's `__init__` runs a `head_bucket` call as a
self-check; failure exits the pod with a clear
`S3 credentials not working: <error-text>` message. Same
posture for GcsBackend (calls `bucket.exists()`).

This is similar to the chunk-13c `ensure_schema`
on-first-connect pattern — fail fast at startup with a
clear message rather than at first user action.

### 8.2 `moto` and `google-cloud-storage` mocks share imports

The `eden-blob-tests` job's `moto` mock works by
monkey-patching `botocore` HTTP transport. If a test
imports `boto3` BEFORE `moto.mock_s3()` is applied, the
monkey-patch misses. The pytest fixtures use `mock_s3` as
a context manager around the test function, ensuring the
patch is active before any boto3 client is constructed.

The GCS mock pattern is different: `unittest.mock.patch`
on `google.cloud.storage.Client`. Tests that import the
S3 backend module shouldn't import the GCS module at the
same time; each test row stays narrow.

### 8.3 Trust-boundary regression on prefix-relative key paths

If `blob.s3.prefix=prod/` and an attacker submits a
`s3://my-eden-artifacts/staging/secret-data` URI to the
trust-boundary helper, the prefix check rejects it. But
if the prefix isn't set (`prefix=""`), every key in the
bucket is allowed — which means an attacker who controls
ANY upload path into the bucket could write a key whose
URI bypasses the helper. Mitigation: the runbook STRONGLY
recommends a non-empty prefix per deployment, and the
chart's NOTES.txt warns when `blob.s3.prefix=""`.

### 8.4 `aws s3 sync` directionality and partial copies

The §3.11.2 Path B step 2 `aws s3 sync` is one-way; it
doesn't delete keys on the destination that don't exist
on the source. Operators running it AFTER initial migration
risk re-uploading already-migrated bytes (idempotent, but
wastes bandwidth + clutters the access log). The runbook's
verification step uses `aws s3 ls --recursive --summarize`
to compare the source byte count against the destination
byte count post-sync.

### 8.5 GCS service-account-key mount path collisions

Multiple Secrets mounted at the same path collide silently
in Kubernetes (the second one wins). The chart's GCS
mount path `/var/run/secrets/eden/gcs/<key>` is unique to
the blob-store SA-key Secret and avoids collision with
the chunk-12c control-plane secrets that may also live
under `/var/run/secrets/eden/`. Documented in the chart's
README.

### 8.6 IRSA + Web Identity Token File mounts

EKS's IRSA webhook mutates pod specs to inject the
`AWS_WEB_IDENTITY_TOKEN_FILE` env var + mount the
service-account-token projected volume. If the chart
ALSO mounts a volume at `/var/run/secrets/eks.amazonaws.com/serviceaccount`,
the webhook's projected volume gets shadowed and IRSA
breaks silently. The chart's IRSA path adds NO volumes at
that path; documented in the runbook.

### 8.7 `file://` URI portability across pod restarts

A LocalFsBackend on a PVC produces URIs like
`file:///var/lib/eden/artifacts/abc123/idea.md`. After a
pod restart, the same PVC reattaches at the same
mountPath, so the URI remains resolvable. After a chart
upgrade that changes `blob.file.mountPath`, existing URIs
break (they hard-code the old path). Mitigation: the
chart's `mountPath` knob defaults to a stable value;
NOTES.txt warns when it's changed.

S3 / GCS URIs are mountpoint-independent, so this trap
only applies to `LocalFsBackend`.

### 8.8 Compose-side `eden-artifacts-data` vs `eden-blob-data`

the prior `MANUAL_UI_ISSUES.md` §20 (resolved by Phase 12a-1g)
notes the Dockerfile typo (`/blob` vs `/blobs`) was
already fixed inline. 13d removes the now-orphaned line
entirely. The DIFFERENT volume `eden-artifacts-data` (the
one the web-ui actually consumes) STAYS — it's the
LocalFsBackend's mount root in Compose. Don't confuse
the two during the cleanup; both names show up in the
compose file.

### 8.9 Chart upgrade trap: artifacts-PVC retention

When an operator upgrades chart 0.3.0 → 0.4.0 with
`blob.backend=file` → `blob.backend=s3`, the chart's
template rendering removes the artifacts PVC's
volumeClaimTemplates entry. StatefulSet does NOT delete
the orphaned PVC automatically (per chunk-13a §7.1's
documentation), so the operator's data is safe — but a
careless `helm uninstall && helm install --reset-values`
combination would recreate the StatefulSet without the
PVC mount and the bytes would still be on the underlying
storage but no longer accessible. Per §3.6, the migration
runbook walks operators through unmounting only AFTER
the soak window confirms no `file://` reads.

### 8.10 boto3's region-vs-endpoint-url precedence

When BOTH `region_name` and `endpoint_url` are passed,
boto3 uses the `endpoint_url` for routing and ignores
the region (it's still required for signing).
For MinIO (which doesn't actually look at the region
during signing), `region="us-east-1"` is a safe
placeholder. For real AWS S3 with a custom
`endpoint_url` (e.g., a VPC endpoint), the region MUST
match the bucket's actual region or signing fails with
`SignatureDoesNotMatch`. Documented in the runbook.

### 8.11 `helm template` rendering the SA annotation when irsa is off

The `eden.blob.serviceAccountAnnotations` helper returns
an empty map when `blob.s3.irsa.enabled=false`. The
ServiceAccount template's `metadata.annotations:` block
needs to handle the empty case gracefully — Helm's
`{{- with ... }}` block skips the entire `annotations:`
key when the map is empty, which is what we want
(empty annotations: section would be invalid YAML).
Verified via the §6.4 helm-template matrix.

### 8.12 The "no auth configured" runtime check

The `values.schema.json` mutex clause catches the
no-auth case at lint time (per §3.5.4). But for
operators on `helm install --set blob.s3.irsa.enabled=true`
without setting `roleArn`, the schema's paired-
requirement clause catches it. The runtime check via
`head_bucket` is the second line of defense — if the
operator somehow bypasses lint (manual kubectl apply),
the pod fails fast on first start with a clear message.

## 9. Risks

1. **boto3 / google-cloud-storage version drift.** Both
   SDKs release frequently; the pin `>=1.34,<2` /
   `>=2.18,<3` lets minor / patch updates flow through
   `uv lock`. A future SDK API break could surface in CI;
   mitigation: dependabot is configured at the
   repository level (per existing convention) and surfaces
   incompatible bumps before merge.

2. **MinIO version drift in CI.** The
   `helm-smoke-blob-s3` job pins a specific
   `minio:RELEASE.2024-12-08T19-12-09Z` tag (see §3.10's
   `mc-init` step). MinIO releases monthly; a future
   release might break the wire compat. Mitigation: pin
   the tag, bump deliberately.

3. **IRSA / WI configuration drift.** Operators who
   misconfigure their IAM role's trust policy (wrong OIDC
   provider URL, wrong SA name) get pods that fail with
   AssumeRoleWithWebIdentity errors. The runbook's
   troubleshooting section walks through diagnosis (`aws
   sts get-caller-identity` from inside a debug pod is
   the canonical first check). Same shape for GCP WI.

4. **Migration window data loss.** If an operator runs
   `aws s3 sync` (Path B step 2) BEFORE draining writers
   (step 1), in-flight uploads can produce `file://` URIs
   that the sync step misses. The resulting URI is
   resolvable via the LocalFsBackend fallback for the
   soak window, then disappears when the operator
   decommissions the PVC. The runbook's step ordering is
   load-bearing; the verification checklist post-step-2
   greps the task store for `file://` URIs created
   AFTER the sync to catch this.

5. **MinIO's S3 wire incompatibility on edge cases.**
   MinIO emulates S3's most-used surface but has known
   gaps on complex features (lifecycle, replication,
   object-lock). EDEN doesn't use any of those; the
   risk surface is narrow. Documented + the
   `helm-smoke-blob-s3` job is the canary.

6. **fake-gcs-server / GCS emulator parity.** No CI
   coverage for the real GCS wire; manual operator
   verification fills the gap. Risk mitigated by GCS
   client library being the same (well-tested) library
   used by manual-verification operators.

7. **The composite-backend "fallback" branch becomes
   load-bearing forever.** §3.6's migration plan assumes
   operators eventually decommission the PVC. Real-world
   experience: operators sometimes don't. The chart's
   composite-backend wiring stays simple enough that
   keeping it indefinitely is fine; documented as a
   first-class deployment shape, not just a transitional
   one.

8. **Empty prefix on shared buckets.** Per §8.3, a
   shared bucket without a prefix per deployment exposes
   trust-boundary risk. The chart's NOTES.txt warns;
   operators on shared buckets MUST set a per-deployment
   prefix.

## 10. Sequencing

Recommended PR shape (in order):

1. **`eden-blob` package PR.** New workspace member;
   Backend Protocol; LocalFs + S3 + GCS + Composite
   impls; unit tests against `moto` + mocked GCS.
   Lints clean; passes existing tests; not yet wired
   into any service.

2. **Web-ui integration PR.** Refactor `artifacts.py`,
   `_helpers.py`, `routes/artifacts.py`, `cli.py`,
   `app.py`. Behavior change: `file://` URIs go through
   `LocalFsBackend.upload`'s content-addressed layout.
   Existing tests pass; new tests cover backend dispatch.

3. **Worker-host integration PR.** Per-role per §3.4:
   ideator host's `rationale`-shape branch switches to
   `Backend.upload`; executor host adds fetch-to-temp in
   `_rationale_path_from_uri` plus `--blob-fallback-*`
   flags for §3.6 composite mode; evaluator host is
   unchanged in v0.

4. **Chart values + schema PR.** `blob.*` block;
   `values.schema.json` clauses; `_helpers.tpl` updates;
   NOTES.txt deprecation warnings.

5. **Chart templates PR.** ServiceAccount template;
   web-ui StatefulSet conditional PVC; worker-host
   conditionals; CLI-args passthrough.

6. **Compose cleanup PR.** Remove `blob-init` +
   `eden-blob-data` + the depends_on lines + the
   Dockerfile chown line. Add `compose.blob-s3.yaml` /
   `compose.blob-gcs.yaml` overlays. The prior `MANUAL_UI_ISSUES.md`
   §20 marked resolved.

7. **CI PR.** `helm-smoke-blob-s3` job;
   `eden-blob-tests` job; `ci-values-blob-s3.yaml`;
   `ci/minio-deployment.yaml`.

8. **Runbook + docs PR.** `migrating-to-blob-backend.md`
   (new); `helm.md` extended with the blob-backend
   reference; roadmap delta; manual-walkthrough
   verification.

A reviewer going from PR 1 to PR 8 should expect
`helm-smoke-blob-s3` to first appear in PR 7 and go green
on first run; if it doesn't, the failure mode is most
likely the MinIO startup race (the chart installs
faster than MinIO's bucket pre-creation) — the §3.10
job uses `kubectl wait --for=condition=available` to
sequence the bucket creation correctly.

## 11. Out of scope (future amendments)

- **Backend-side encryption key management.** Operators
  configure SSE-KMS / CMEK at the bucket level
  out-of-band. EDEN doesn't pass `ServerSideEncryption`
  parameters today.
- **Cross-region replication.** Provider-side bucket
  replication.
- **Lifecycle policies.** Provider-side bucket lifecycle
  rules.
- **Async backend variants.** Sync surface only in v0.
- **Bundled MinIO chart.** Operators stand up MinIO
  themselves.
- **GCS HMAC-key auth.** GCS supports both service-
  account-key and HMAC-key auth; v0 ships SA-key only.
  HMAC adds a second auth row; deferred.
- **Streaming `Backend.fetch`.** v0's `fetch` is full-
  buffer; very large artifacts would need streaming. The
  Protocol's optional `fetch_stream` and `fetch_range`
  are the future-amendment surface.
- **Per-service backends.** v0 uses one backend
  deployment-wide; a future amendment MAY let the
  ideator write to one backend and the evaluator to
  another. Not motivated today.
- **Host-mediated evaluator artifact upload.** Per §3.4.2
  and §3.4.4: today the evaluator role emits a final
  `artifacts_uri` directly. A future amendment MAY
  extend the subprocess binding so the role emits a
  local-path string the host uploads via `Backend.upload`.
  Compose deployments retain the existing shape;
  k8s-job-mode evaluators on `blob.backend=s3` without
  fileFallback would gain host-mediated upload. The
  extension is informative-spec + worker-host code; out
  of 13d to keep scope focused.
- **Evaluator role config helper for direct backend
  upload.** v0 documents that operators running
  `blob.backend=s3` without fileFallback MUST configure
  the evaluator role process to upload to S3 itself. A
  future amendment MAY ship a small helper library
  (e.g., `eden_blob.role.upload_evaluator_artifact(...)`)
  that the role process imports + the runbook documents.
  Out of 13d.
- **Backend-managed retention enforcement.** Chapter 8
  §5.2's retention floor is operator-enforced.
- **The `eden_blob` package on PyPI.** Stays a workspace
  member.

## 12. Estimated effort

- **eden-blob package** (PR 1): ~2 days. Backend Protocol
  is small; the three impls share a lot of structure
  (URL parsing, error mapping, content-addressed key
  derivation). Tests are ~1 day of the total.
- **Web-ui integration** (PR 2): ~1 day. Refactor is
  mechanical given the Protocol; tests cover the
  dispatch.
- **Worker-host integration** (PR 3): ~1 day. Three
  similar surfaces; the shared `blob_args.py` factors
  the per-service repetition.
- **Chart values + schema** (PR 4): ~1 day. Helper
  functions, NOTES.txt copy.
- **Chart templates** (PR 5): ~1 day. SA template +
  conditional PVC + per-backend wiring.
- **Compose cleanup** (PR 6): ~0.5 day. Mechanical
  removal + overlay add.
- **CI** (PR 7): ~2 days. MinIO Deployment shape,
  bucket pre-creation race-handling, end-state
  assertions.
- **Runbook + docs** (PR 8): ~1.5 days. Detail-heavy
  (per-cloud IRSA / WI walkthroughs, IAM-policy
  templates, migration verification checklist).

**Realistic total: ~10 working days** of focused work.
The heaviest single items are the eden-blob package + CI;
the chart and compose changes are mechanical given the
13a/13c foundation.

## 13. What lands at the end of 13d

After 13d merges, an operator can run
`helm install eden ./reference/helm/eden -f values.yaml`
against any conforming Kubernetes cluster, point
`blob.backend=s3` at an AWS S3 bucket (with IRSA) or
`blob.backend=gcs` at a GCS bucket (with Workload
Identity), and get a production-grade EDEN deployment
where artifact bytes live in operator-managed cloud
storage with provider-managed durability + access control.
The PVC-backed `LocalFsBackend` path stays for greenfield
clusters and test environments. The Compose deployment
keeps `LocalFsBackend` by default; operators MAY override
to S3/GCS via the new env vars + opt-in overlays.
the prior `MANUAL_UI_ISSUES.md` §20 (resolved by Phase 12a-1g) is
resolved (the unconsumed `blob-init` + `eden-blob-data`
plumbing is gone). 13e (Forgejo auth + ACLs + native PR
review) is the remaining 13-series substrate chunk; it
is independent of 13d and proceeds in parallel.
