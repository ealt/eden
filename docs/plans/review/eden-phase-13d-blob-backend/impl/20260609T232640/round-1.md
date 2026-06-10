# Phase 13d impl-stage codex-review — round 1 (2026-06-09)

`codex exec` (read-only sandbox) verifying commit `0d535c3`'s round-0
fixes against `git diff 625759d..HEAD` plus a fresh full-diff sweep.
**All seven round-0 resolutions verified** (codex confirmed against AWS
docs that `If-None-Match: *` returns 412 with `Error.Code`
`PreconditionFailed`, and that `Bucket.exists()` is a real no-arg
method). Verdict: **fix-then-ship** on two NEW findings, both addressed
in the same PR:

## [P2] GCS runbook IAM was insufficient for the new `bucket.exists()` path

The round-0 fix made `GcsBackend.load` call `bucket.exists()` on the
absent path, but the runbook's recommended roles
(`roles/storage.objectCreator` + `roles/storage.objectViewer`) lack
`storage.buckets.get` — so a least-privilege deployment following the
runbook would turn every absent artifact into a `Forbidden` error
instead of `NotFound`.

**Resolution (both sides):** the backend now catches `Forbidden` from
`exists()` and falls back to the common-case classification
(object-level absence → `NotFound`) so object-only roles keep working;
the runbook recommends additionally granting `storage.buckets.get`
(e.g. `roles/storage.legacyBucketReader` or a custom role) for better
misconfig diagnostics, with the degraded-but-functional behavior
documented. Regression test:
`test_exists_forbidden_falls_back_to_not_found`.

## [P3] S3 conditional PUT didn't handle 409 `ConditionalRequestConflict`

AWS documents 409 as the retryable "another conditional write to this
key is in flight" response to an `If-None-Match` PUT; the round-0 code
only mapped 412.

**Resolution:** `store` retries the PUT once on
`ConditionalRequestConflict`; after the retry the competing write has
either landed (412 → `FileExistsError`) or failed (the PUT succeeds); a
second consecutive conflict propagates the `ClientError`. Regression
tests: `test_conditional_conflict_retries_once_then_succeeds`,
`test_persistent_conditional_conflict_propagates`.

## Explicitly clean

"No other full-diff blockers found." Round-0 items 1–7 each marked
**verified** (item 6 statically — the sandbox is read-only so codex
could not run pytest; the suite runs in this repo's gates instead).
