# Issue #294 impl-stage codex-review — round 0 (2026-06-11)

Codex (via the codex-companion runtime, codex-cli 0.130.0) reviewing
the diff on `impl/issue-294-checkpoint-bundle` vs `main` at the
reviewed commit `0a2d1c8` — the planless checkpoint-bundle chunk
(issue #294). Verdict: 3 blocking + 2 non-blocking + 1
deferred-needs-issue. All three blocking findings addressed in the
same PR (fix commit follows the reviewed commit); the non-blocking
and deferred items are filed as issues.

## [Blocking 1] Snapshot↔bundle race + late `exported_at` stamp

`eden_storage._checkpoint.export_checkpoint` read git state (the
provider fetch) after the store transaction closed, and stamped
`exported_at` after that read — chapter 10 §6 requires one logical
snapshot instant, and §5 makes `exported_at` the §10 recovery-probe
anchor for the snapshot. The superset argument (roles publish refs
before committing rows, §12 permits superset bundles) covers the
common path, but a ref **deleted or force-moved** between snapshot
and fetch could still leave the bundle missing something the snapshot
references.

**Resolution:** (a) `exported_at` is now stamped inside the snapshot
transaction. (b) Non-empty provider bundles are self-validated
against the frozen snapshot with the importer's own §12 check —
`_validate_bundle_covers_snapshot` reuses
`_validate_bundle_cross_references` over the snapshot's re-validated
variant/idea rows. Mirroring the importer exactly means the check can
never reject an archive that would have imported; it only moves the
failure to export time, while the source still exists to retry
against. The wire route maps the resulting `CheckpointInvalid` to the
retryable 503 `eden://reference-error/checkpoint-repo-unavailable`.
Regression tests: `test_repo_bundle_provider_bundle_must_cover_snapshot`
(bundle missing a snapshot branch → raise, zero archive bytes) and
`test_repo_bundle_provider_covering_bundle_passes` in
`test_checkpoint_storage.py`.

## [Blocking 2] Bundle-creation failure still collapsed to the silent zero-byte placeholder

`_compose_repo_bundle` swallowed `CheckpointInvalid` from
`create_bundle` into `b""` unconditionally — with a remote of record
configured, that re-creates the #294 silent-non-resumable-archive
failure mode (200 with an empty bundle), and the importer skips §12
validation on zero-byte bundles, so the rows would import without git
history.

**Resolution:** the swallow now survives only in the no-remote
posture (a local test repo with no refs is a legitimate empty
bundle). When `checkpoint_repo_refresh` is configured, any bundle
failure after a successful sync raises `CheckpointRepoUnavailable`
(503) — a healthy seeded remote always carries at least the seed ref.
Regression tests: `test_export_bundle_failure_with_remote_maps_to_503`
and `test_export_empty_local_repo_without_remote_keeps_placeholder`
in `test_checkpoint_wire.py`.

## [Blocking 3] §14.1 `format_version` query param ignored

`spec/v0/07-wire-protocol.md` §14.1 defines the optional
`format_version` query and mandates 400 for unrecognized values; the
export handler ignored it entirely (pre-existing wave-4 gap, surfaced
by this review).

**Resolution:** the route now parses `format_version` and rejects any
value other than `CHECKPOINT_FORMAT_VERSION` with `BadRequest`
(`eden://error/bad-request`). Tests:
`test_export_accepts_current_format_version` /
`test_export_rejects_unrecognized_format_version`.

## [Non-blocking 1] Smoke round-trip equality is counts/id-sets, not full objects

`smoke-checkpoint.sh` Phase 6 compares counts and sorted id sets;
chapter 10 §9 promises field-level round-trip modulo the documented
import stamps. Filed as
[#312](https://github.com/ealt/eden/issues/312) (smoke-depth
improvement; production path already exercised).

## [Non-blocking 2] Ordering test didn't prove bundle-satisfies-snapshot

Addressed by the Blocking-1 resolution's new coverage tests; the
post-snapshot-mutation test now also documents that the §12
self-validation runs against the frozen snapshot, not a re-read.

## [Deferred] Streaming export for very large archives

The route docstring deferred a streaming temp-file materialization
model without a tracking issue. Filed as
[#313](https://github.com/ealt/eden/issues/313) (chapter 10 §6
explicitly permits the buffering model; this is scalability
hardening, not a conformance gap).
