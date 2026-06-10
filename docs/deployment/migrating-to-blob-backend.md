# Migrating the artifact blob store to S3 / GCS

This runbook covers operating the Phase 13d blob backend (issue [#174](https://github.com/ealt/eden/issues/174)): selecting `blob.backend` in the Helm chart, wiring credentials, and migrating deposited bytes between backends.

## 1. What the blob backend is (and is not)

The task-store-server's §16 artifact deposit endpoint ([`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §16; issue [#166](https://github.com/ealt/eden/issues/166)) persists deposited bytes through an `ArtifactBackend` ([`reference/packages/eden-storage/src/eden_storage/artifact_backend.py`](../../reference/packages/eden-storage/src/eden_storage/artifact_backend.py)): a metadata-free, bytes-in/bytes-out store keyed by the server-minted opaque id. Three durable reference backends ship:

| `blob.backend` | Implementation | Bytes live in |
|---|---|---|
| `file` (default) | `FileArtifactBackend` | A chart-managed PVC mounted into the task-store-server pod |
| `s3` | `S3Backend` (boto3) | An operator-supplied S3 bucket (or any S3-compatible service, e.g. MinIO via `blob.s3.endpointUrl`) |
| `gcs` | `GcsBackend` (google-cloud-storage) | An operator-supplied GCS bucket |

Three properties make this migration low-drama:

1. **The backend is invisible on the wire.** The deposit endpoint always returns `eden://artifacts/<id>`; fetches go through the same endpoint. Workers and the web-ui never see `s3://` / `gs://` URIs and never hold bucket credentials — **only the task-store-server pod touches the backend**, so credential wiring is scoped to that one pod.
2. **Objects are keyed by opaque id.** `FileArtifactBackend` stores one file named `<opaque_id>` under its root; `S3Backend` / `GcsBackend` store one object at `<prefix>/<opaque_id>`. The layout maps 1:1, so migration is a plain recursive copy.
3. **Legacy `file://` rows are orthogonal.** Pre-#166 `artifacts_uri` values (`file://...` under the legacy `--artifacts-dir`) are served by the unchanged `/_reference` route and the web-ui's artifacts mount. Switching `blob.backend` does not touch them, and no stored row is rewritten — they keep resolving exactly as before.

## 2. Choosing and configuring a backend

`values.schema.json` enforces the per-mode requirements at `helm lint` / `helm template` time — a missing bucket or missing auth path fails loud before any pod starts (the same no-fictional-defaults posture as `image.repository`).

### 2.1 `file` (default)

No values required. The chart provisions a `<release>-blob-data` PVC (`blob.file.size`, default `10Gi`), mounts it at `blob.file.mountPath`, and passes `--artifact-blob-dir`. The PVC is annotated `helm.sh/resource-policy: keep`, so `helm uninstall` preserves deposited bytes; delete the PVC explicitly to reclaim the space.

### 2.2 `s3`

```yaml
blob:
  backend: s3
  s3:
    bucket: my-eden-artifacts        # REQUIRED
    region: us-west-2                # recommended (AWS requires a region)
    prefix: prod/exp-7               # optional namespacing
    # endpointUrl: http://minio.minio.svc:9000   # MinIO / S3-compatibles
    irsa:
      enabled: true                  # pod identity — production posture
      roleArn: arn:aws:iam::123456789012:role/eden-blob
```

Exactly **one** auth path must be configured:

- **IRSA (preferred).** Create an IAM role whose trust policy allows the EKS cluster's OIDC provider to assume it from the `<release>-task-store-server` ServiceAccount, grant it `s3:GetObject` + `s3:PutObject` (and `s3:ListBucket` for HEAD) on the bucket, and set `blob.s3.irsa.{enabled,roleArn}`. The chart renders the annotated ServiceAccount; the AWS SDK's default credential chain picks the identity up. No static secret exists anywhere.
- **Static keys (fallback** — clusters without IRSA; local dev against MinIO**).** Pre-create a Secret carrying the key pair and set `blob.s3.existingSecret`:

  ```bash
  kubectl -n <ns> create secret generic eden-s3-creds \
    --from-literal=AWS_ACCESS_KEY_ID=... \
    --from-literal=AWS_SECRET_ACCESS_KEY=...
  ```

  The chart injects the two keys as env vars (key names configurable via `blob.s3.accessKeyIdKey` / `blob.s3.secretAccessKeyKey`); the literal secret never reaches a CLI argv.

### 2.3 `gcs`

```yaml
blob:
  backend: gcs
  gcs:
    bucket: my-eden-artifacts        # REQUIRED
    prefix: prod/exp-7               # optional
    workloadIdentity:
      enabled: true                  # pod identity — production posture
      serviceAccount: eden-blob@my-project.iam.gserviceaccount.com
```

Exactly **one** auth path:

- **Workload Identity (preferred).** Create a GCP service account with `roles/storage.objectCreator` + `roles/storage.objectViewer` on the bucket, bind it to the Kubernetes ServiceAccount (`gcloud iam service-accounts add-iam-policy-binding ... --role roles/iam.workloadIdentityUser --member "serviceAccount:<project>.svc.id.goog[<ns>/<release>-task-store-server]"`), and set `blob.gcs.workloadIdentity.{enabled,serviceAccount}`.
- **Service-account key (fallback).** Pre-create a Secret carrying the key JSON under the `blob.gcs.serviceAccountKeyKey` key (default `google-credentials.json`) and set `blob.gcs.existingSecret`. The chart mounts it read-only and points `GOOGLE_APPLICATION_CREDENTIALS` at it.

### 2.4 Outside Helm

The same selection exists as task-store-server flags: `--blob-backend file|s3|gcs`, `--artifact-blob-dir` (file), `--blob-s3-bucket` / `--blob-s3-region` / `--blob-s3-prefix` / `--blob-s3-endpoint-url` (s3), `--blob-gcs-bucket` / `--blob-gcs-prefix` (gcs). Credentials always come from the SDK default chains (env vars / pod identity / instance profile), never argv. The Compose stack stays on the file posture; its wire-deposit cutover is tracked by [#290](https://github.com/ealt/eden/issues/290).

## 3. Migrating `file` → bucket

The §16 contract requires every previously-returned `eden://artifacts/<id>` to keep fetching, so copy the bytes **before** flipping the backend. The opaque-id-keyed layout makes the copy a plain `sync`/`cp -r`.

1. **Quiesce deposits.** Scale the writers down (or accept a brief deposit outage — fetches of already-deposited artifacts are the contract to protect):

   ```bash
   kubectl -n <ns> scale deployment <release>-task-store-server --replicas=0
   ```

2. **Copy the PVC contents to the bucket.** Run a one-shot pod that mounts the blob PVC and has bucket credentials, then:

   ```bash
   # S3 (prefix must match blob.s3.prefix, here prod/exp-7):
   aws s3 sync /var/lib/eden/blob s3://my-eden-artifacts/prod/exp-7

   # GCS (prefix must match blob.gcs.prefix):
   gcloud storage cp -r '/var/lib/eden/blob/*' gs://my-eden-artifacts/prod/exp-7
   ```

   A minimal migration pod: `kubectl -n <ns> apply -f -` with a Pod spec that mounts `persistentVolumeClaim: <release>-blob-data` at `/var/lib/eden/blob` and uses the `amazon/aws-cli` (or `gcr.io/google.com/cloudsdktool/google-cloud-cli`) image plus the same auth posture as §2. Verify the object count matches: `ls /var/lib/eden/blob | wc -l` vs `aws s3 ls --recursive s3://.../prod/exp-7 | wc -l`.

3. **Flip the backend.** `helm upgrade` with the §2.2/§2.3 values. The blob PVC stops being rendered, but `helm.sh/resource-policy: keep` preserves it (and the bytes) as a rollback path.

4. **Verify.** Fetch a known artifact through the wire (`GET /v0/experiments/<exp_id>/artifacts?uri=eden://artifacts/<opaque_id>` with the depositor's or an admin bearer — the client presents the full `artifacts_uri` verbatim per §16.2) and confirm 200 + matching bytes.

5. **Reclaim (later).** Once satisfied — and never before the experiment's retention window allows — delete the retained PVC: `kubectl -n <ns> delete pvc <release>-blob-data`.

Bucket → bucket (or bucket → file) migrations are the same shape: copy `<old-prefix>/*` → `<new-prefix>/*`, flip values, verify, reclaim.

## 4. Operational notes

- **No-overwrite is enforced by every backend** (`08-storage.md` §5.4): `file` uses exclusive-create hard-links, `s3` HEAD-then-PUT against a 128-bit random id, `gcs` the native `if_generation_match=0` precondition. A migration copy never overwrites a live object with different bytes unless the source itself was corrupted — `sync` re-uploading identical bytes is harmless.
- **Bucket lifecycle policies must respect the retention window** ([`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) §5.2). Don't attach an expiry rule shorter than your experiment's retention expectations.
- **MinIO for dev/CI.** Point `blob.s3.endpointUrl` at the MinIO service and use the static-keys auth path. The backend defaults the SDK region to `us-east-1` when only an endpoint is set (MinIO ignores it).
- **Checkpoints do NOT carry blob bytes.** Per [`spec/v0/10-checkpoints.md`](../../spec/v0/10-checkpoints.md) §7, v0 checkpoints carry `artifacts_uri` values verbatim; content-addressed artifact carriage is deferred to `v1+checkpoints+artifacts`. Moving an experiment to another deployment therefore moves the bucket (or a copy of it) out-of-band, exactly like the §3 migration copy.
