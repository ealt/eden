# Phase 13d impl-stage codex-review — round 0 (2026-06-09)

`codex exec` (read-only sandbox, codex-cli 0.130.0) reviewing
`git diff origin/main...HEAD` on `impl/issue-174-blob-backend` — the
S3/GCS blob backend chunk (issue #174) against the #166
`ArtifactBackend` Protocol. Verdict: **fix-then-ship**, 2×P1 + 2×P2 +
3×P3. All seven addressed in the same PR (fix commit follows the
reviewed commit `625759d`).

## [P1] `_s3_is_absent()` over-classified bucket-level 404s as artifact absence

`S3Backend.load` mapped any 404-shaped `ClientError` (including
`NoSuchBucket`) to `eden_storage.NotFound`, so a misconfigured or
deleted bucket would surface as client-facing artifact 404s instead of
a deployment error — the AGENTS.md
narrow-exception-handling-on-store-reads pitfall, on the read path.

**Resolution:** `load` now maps **only** `Error.Code == "NoSuchKey"`
(which implies the bucket exists and the request was authorized) to
`NotFound`; everything else propagates. The helper was renamed to
`_s3_error_code` and the status-code fallback removed. Regression test:
`test_missing_bucket_propagates_not_notfound` (fake raises
`NoSuchBucket`/404 on GET and PUT; both must propagate `ClientError`).

## [P1] `GcsBackend.load()` mapped bucket-level `NotFound` to artifact absence

`google.api_core.exceptions.NotFound` covers both object-level and
bucket-level 404s; the blanket `except` hid "the bucket does not exist"
behind "artifact absent".

**Resolution:** on the `GcsNotFound` path the backend now disambiguates
with one extra round-trip — `if not self._bucket.exists(): raise`
(propagating the bucket-level error) before mapping to `NotFound`. The
extra HEAD only happens on the absent path, never the happy path.
Regression test: `test_missing_bucket_propagates_not_notfound`
(GCS class) with a fake whose blobs raise bucket-level `GcsNotFound`
and whose `bucket.exists()` returns `False`.

## [P2] S3 no-overwrite was HEAD-then-PUT (check-then-write window)

Two concurrent `store()` calls with the same opaque id could both pass
the HEAD and the later PUT would overwrite — a §5.4 violation at the
backend level, even though the 128-bit random server-minted id makes
the window practically negligible.

**Resolution:** `store` now PUTs with `IfNoneMatch="*"` — S3's native
create-only conditional write — and maps `Error.Code ==
"PreconditionFailed"` (412) to `FileExistsError`. Atomic server-side;
same shape as GCS's `if_generation_match=0`. The HEAD precheck is
removed (one fewer round-trip). The `eden-storage[s3]` extra's floor is
bumped `boto3>=1.34` → `>=1.36` (the conditional-write parameter needs
the Nov-2024 botocore surface). AWS S3 and MinIO ≥ RELEASE.2024-08
support the precondition; an S3-compatible service that doesn't errors
loudly rather than silently overwriting (documented in the class
docstring + runbook). The fake S3 client asserts `IfNoneMatch="*"` is
present on every PUT so a regression to unconditional writes fails the
suite.

## [P2] `replicas.taskStoreServer` was uncapped against a single RWO blob PVC

`strategy: Recreate` fixes rollout deadlocks but not an operator
intentionally scaling to >1 replica — two pods can't share the RWO PVC,
and there is no multi-replica task-store/blob design regardless of
backend mode.

**Resolution:** `values.schema.json` caps `replicas.taskStoreServer` at
1 (unconditionally, matching the `webUi`/`ideatorHost`
operator-singleton precedent) with a description naming the RWO PVC and
the missing multi-replica design; `values.yaml` comment updated.

## [P3] `S3Backend.load()` leaked the `StreamingBody`

**Resolution:** `body.read()` now runs in a `try/finally` that closes
the body.

## [P3] Missing regression coverage for the highest-risk mappings

**Resolution:** added `NoSuchBucket` propagation tests (S3 load+store),
GCS bucket-level-`NotFound` propagation test, and the fake-enforced
`IfNoneMatch="*"`/412 conditional-write contract (the duplicate-store
test now exercises the 412 path).

## [P3] Runbook overstated migration-copy safety

The §4 no-overwrite bullet implied `aws s3 sync` / `gcloud storage cp
-r` couldn't clobber; those tools bypass the backend's preconditions
entirely.

**Resolution:** the bullet now states that deposit-time no-overwrite is
backend-enforced but migration copy tools bypass it, and prescribes an
empty-destination-prefix preflight plus "never point two deployments at
the same bucket+prefix".

## Explicitly clean

Codex found no issues in the CLI stray-flag validation or the Helm auth
rendering, and locally probed helm merged-values semantics: active
IRSA+secret and active WI+secret both fail schema validation; inactive
stray values render-and-ignore as intended.
