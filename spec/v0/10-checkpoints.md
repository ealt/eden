# Portable checkpoint format

This chapter specifies the **portable checkpoint format** — a self-describing, implementation-independent archive that captures the logical state of an experiment such that another conforming implementation can resume it.

A checkpoint is the spec-level analog of a snapshot: every persistent protocol object (task, idea, variant, submission, event, worker, group, experiment runtime record) round-trips through it. Backend-specific concerns (database engine, git host, artifact-store URI scheme) are NOT carried; the receiving deployment supplies its own substrate.

This chapter is normative for implementations that claim the v1+checkpoints conformance level ([`09-conformance.md`](09-conformance.md) §4). The wire bindings of the operations defined here live in [`07-wire-protocol.md`](07-wire-protocol.md) §14; the Store-side contract is in [`08-storage.md`](08-storage.md) §1.9.

## 1. Purpose

An EDEN experiment's persistent state lives across three substrates (task store, event log, artifact store; see [`08-storage.md`](08-storage.md)) plus a git repository. The portable checkpoint format captures all four into a single archive with an addressing scheme independent of any specific backend. An operator running an experiment on implementation A can hand a checkpoint to an operator running implementation B; B can import and resume the experiment without either side agreeing on what database, git host, or artifact-store URI scheme the other uses.

A checkpoint preserves **structural state**: every persistent protocol object round-trips with all its protocol-defined fields. It deliberately does NOT preserve **volatile auth + ownership state** — bearer-token hashes, claim tokens, in-flight claim bindings — per the rules in §6 below. The receiving deployment reissues credentials; claimed tasks are reverted to `pending`.

## 2. Logical contents

A checkpoint contains the following per-experiment state:

| Source | Contents |
|---|---|
| [`02-data-model.md`](02-data-model.md) §2 | The experiment config (declarative input) and the experiment runtime object (`state`, `created_at`, `base_commit_sha`, `imported_from`) |
| [`02-data-model.md`](02-data-model.md) §3, [`04-task-protocol.md`](04-task-protocol.md) | All tasks, every state |
| [`02-data-model.md`](02-data-model.md) §5 | All ideas |
| [`02-data-model.md`](02-data-model.md) §9, [`06-integrator.md`](06-integrator.md) | All variants, including `variant_commit_sha` for integrated variants |
| [`03-roles.md`](03-roles.md) §2.4, [`03-roles.md`](03-roles.md) §3.4, [`03-roles.md`](03-roles.md) §4.4 | All submissions (the role-specific payloads `read_submission` returns) |
| [`05-event-protocol.md`](05-event-protocol.md) | The full event log, in append order |
| [`02-data-model.md`](02-data-model.md) §6 | The worker registry (without credential hashes; see §6) |
| [`02-data-model.md`](02-data-model.md) §7 | The group registry |
| Git repository | All refs and reachable objects (seed, `work/*`, `variant/*`, and any other refs) |
| [`08-storage.md`](08-storage.md) §5 | The bytes of every artifact referenced by an idea's or variant's `artifacts_uri` |

The event log is **first-class**, not derivable. Replay across the round-trip is a hard requirement; consumers MUST emit the imported events in the order the checkpoint preserves them ([`05-event-protocol.md`](05-event-protocol.md) §4).

## 3. Format

A checkpoint is a directory tree wrapped in a tarball for transport (§4). The directory layout MUST be:

```text
<checkpoint>/
  manifest.json             # required (§5)
  experiment-config.yaml    # the experiment config, verbatim
  experiment.json           # the runtime experiment object (state, created_at, base_commit_sha, imported_from)
  tasks.jsonl               # one Task per line; schema = task.schema.json
  ideas.jsonl               # one Idea per line; schema = idea.schema.json
  variants.jsonl            # one Variant per line; schema = variant.schema.json
  submissions.jsonl         # one Submission per line (any role)
  events.jsonl              # one Event per line, in append order
  workers.jsonl             # one Worker per line; schema = worker.schema.json
  groups.jsonl              # one Group per line; schema = group.schema.json
  repo.bundle               # `git bundle create --all` of the experiment's bare repo
  artifacts/
    sha256/<hex>            # one file per unique artifact (content-addressed; §7)
```

Every JSONL file MUST contain one JSON object per line, terminated by `\n`. Empty lines and trailing whitespace MUST NOT appear. Each object MUST validate against the schema named in the manifest's `files` block.

If a registry is empty (no workers, no groups, no ideas, …), the corresponding JSONL file MUST exist with zero lines. Absent files indicate format malformation.

## 4. Transport envelope

The on-wire and on-disk transport envelope is a POSIX tar archive (`ustar` or `pax` extension; either is conforming). The archive MUST contain exactly the directory tree from §3, rooted at a single top-level directory whose name is implementation-defined.

The media type is `application/x-eden-checkpoint+tar`. Implementations MAY additionally accept zip in a future minor revision; v0 conforming implementations MUST accept tar and MAY reject other archive formats.

The exporter MAY use HTTP `Transfer-Encoding: chunked` to stream the archive (RECOMMENDED for large experiments). Consumers MUST handle both chunked and `Content-Length`-buffered responses.

## 5. Manifest

The `manifest.json` file MUST be a JSON object validating against [`schemas/checkpoint-manifest.schema.json`](schemas/checkpoint-manifest.schema.json) with the following shape:

```json
{
  "checkpoint_format_version": "1",
  "spec_version": "v0",
  "experiment_id": "exp_01hqs3m4n5p6q7r8s9t0v1w2x3",
  "exported_at": "2026-05-06T15:00:00Z",
  "exporter": {
    "implementation": "eden-reference/0.x",
    "atomicity_mechanism": "transactional_snapshot"
  },
  "requires_credential_reissue": true,
  "counts": {
    "tasks": 42,
    "ideas": 12,
    "variants": 8,
    "submissions": 8,
    "events": 60,
    "workers": 4,
    "groups": 2
  },
  "files": {
    "experiment_config": "experiment-config.yaml",
    "experiment": "experiment.json",
    "tasks": "tasks.jsonl",
    "ideas": "ideas.jsonl",
    "variants": "variants.jsonl",
    "submissions": "submissions.jsonl",
    "events": "events.jsonl",
    "workers": "workers.jsonl",
    "groups": "groups.jsonl",
    "repo_bundle": "repo.bundle",
    "artifacts_dir": "artifacts/sha256"
  }
}
```

Field semantics:

- `checkpoint_format_version` — string. The format version of this chapter's specification. v0 of the EDEN spec ships format version `"1"`. An importer MUST reject an unrecognized version with `eden://error/unsupported-checkpoint-version`.
- `spec_version` — string. The EDEN spec version the contained data conforms to (currently `"v0"`). An importer MUST reject a mismatched value with `eden://error/spec-version-mismatch`. The two version fields are independent; a future format `"2"` could carry data conforming to spec `"v0"` or `"v1"`.
- `experiment_id` — string. The source experiment's opaque, system-minted `exp_*` id ([`02-data-model.md`](02-data-model.md) §1.6). On import the receiving deployment does NOT reuse this value as its primary key: absent an `as_experiment_id` override the receiver imports under **its own** experiment id (a freshly-minted `exp_*` for a multi-experiment receiver, or its single configured `experiment_id` for a single-experiment receiver) and records the source value in `imported_from.source_experiment_id` for provenance (§10; [`07-wire-protocol.md`](07-wire-protocol.md) §14.2). Pre-rename manifests carrying a non-`exp_*` id fail manifest validation and are not importable (clean break; [plan](../../docs/plans/identity-id-name-disambiguation.md) §6).
- `exported_at` — RFC 3339 UTC timestamp ([`02-data-model.md`](02-data-model.md) §1.2) at which the source snapshot was taken. This value is normative: it is the recovery-probe anchor in §10 below.
- `exporter` — informative object describing the producing implementation. Consumers MUST NOT key behavior on its contents; it exists for operator debugging.
- `requires_credential_reissue` — boolean. When `true`, the importer MUST mint fresh credentials for every imported worker before the experiment can resume. v0 producers MUST set this to `true` (no v0 conformant flow carries credential hashes; see §6).
- `counts` — informative object listing how many of each object the checkpoint contains. Consumers MAY use this for early validation; the authoritative count is the actual line count of each JSONL file.
- `files` — required object naming the file path of each logical component. Implementations MUST emit each file at the named path. The fixed shape above is the v0 contract; future format versions MAY add fields.

## 6. Atomicity contract

A conforming exporter assembles the archive from three categories of input:

1. **Store-managed state** — tasks, ideas, variants, submissions, events, workers, groups, runtime experiment object. The Store-side `export_checkpoint` operation ([`08-storage.md`](08-storage.md) §1.9) provides this stream.
2. **Caller-supplied substrate-external pieces** — the experiment-config text (carried verbatim into `experiment-config.yaml`) and the git repository state (carried verbatim into `repo.bundle`). The wire binding's export endpoint ([`07-wire-protocol.md`](07-wire-protocol.md) §14.1) composes these from deployment-local substrates; an implementation whose server process has no access to the experiment-config file or the git repo MAY emit zero-byte placeholders (the resulting archive is structurally valid but the receiver MUST treat it as non-resumable — chapter 10 §12's cross-reference validation MAY skip when the bundle is empty).
3. **Content-addressed artifacts** — bytes of every `artifacts_uri` reference (§7).

The exporter MUST snapshot the source state at a single logical instant. Two strategies are conforming:

1. **Quiesce-and-dump.** The exporter pauses the orchestrator and all workers (e.g., via a deployment-level lock or by flipping every `dispatch_mode` key to `"manual"`), captures the snapshot, and resumes. Simple; introduces brief downtime.

2. **Transactional snapshot.** The exporter takes a serializable transaction over the [`08-storage.md`](08-storage.md) §1.1 task store + reads the event log, idea/variant tables, worker/group registries, and git repository state at the same logical instant. No downtime; complexity is in the exporter.

The atomicity guarantee is normative; the choice of mechanism is implementation-defined. Operations that would mutate state during a live export MUST either be serialized after the export completes or rejected with `eden://error/checkpoint-in-progress`.

The materialization boundary is the exporter's choice. An implementation MAY:

- Stream the archive directly from the open transaction (the transaction stays open for the entire HTTP response).
- Materialize the archive to a temporary file under the transaction, close the transaction, then stream the file to the client (decouples slow clients from source-state write throughput).

Both are conforming. From a consumer's perspective, the archive bytes reflect a single atomic snapshot of the source state regardless of which strategy was used.

## 7. Content-addressed artifacts

The content-addressed scheme described in this section is **deferred from v0** at the normative-MUST level. v0 conforming implementations carry `artifacts_uri` values verbatim through the archive's JSONL files; the receiving deployment is responsible for resolving them (typically by side-loading the artifact bytes out-of-band, or by accepting that deployment-local URIs are inert on the receiver until an operator-supplied substrate connects them). The scheme below is the **target shape** that a future protocol revision — `v1+checkpoints+artifacts` — will make MUST-strength; v0 implementations MAY emit content-addressed artifacts following this layout, and a conforming reader MUST tolerate the layout when present, but a v0 exporter that ships verbatim deployment-local URIs is conforming.

The deferral acknowledges the substrate-coupling cost: a fully content-addressed import requires the receiving deployment to plumb an artifact backend (file://, s3://, …) through the wire layer's import handler, parallel to the git-bundle substrate plumbing in §6. That work belongs alongside the Phase 13d `Backend` abstraction. See the issue linked from the v0 conformance level entry in [`09-conformance.md`](09-conformance.md) §4.

**Target shape (informative for v0; MUST for `v1+checkpoints+artifacts`).** Every unique artifact referenced by an `artifacts_uri` field on an idea or variant is stored at `artifacts/sha256/<hex>` where `<hex>` is the lowercase hexadecimal SHA-256 of the artifact's byte content (64 characters). The exporter rewrites every `artifacts_uri` value in the JSONL files (`ideas.jsonl`, `variants.jsonl`, `submissions.jsonl`) to the URI form `checkpoint:sha256:<hex>` matching the corresponding `artifacts/sha256/<hex>` file. Two ideas or variants referencing artifacts with identical byte content share a single `artifacts/sha256/<hex>` entry (dedup within the archive).

On import in the future-revision contract, the receiving Store:

1. Materializes each `artifacts/sha256/<hex>` into its own artifact backend, generating a deployment-local URI (`file://`, `s3://`, etc.).
2. Walks every `checkpoint:sha256:<hex>` reference in the JSONL data and rewrites it to the matching deployment-local URI.

The rewrites are applied within the same transaction that creates the protocol-owned rows; an import either commits fully or rolls back fully ([`08-storage.md`](08-storage.md) §6). A missing artifact reference (a `checkpoint:sha256:<hex>` URI in the JSONL with no matching file under `artifacts/sha256/`) causes the importer to reject the entire checkpoint with `eden://error/checkpoint-invalid`. NO partial state may be committed.

**v0 behavior.** When the exporter emits `artifacts_uri` values verbatim (the deferred path), the archive's `artifacts/` directory MAY be empty and the JSONL `artifacts_uri` strings retain whatever scheme the source deployment used. The importer accepts them verbatim into the receiving Store. The receiver's wire reads return the original (likely non-resolvable on the receiver) URIs; operator tooling that needs to surface the underlying bytes is responsible for an out-of-band materialization pass.

## 8. Worker and group portability

Worker rows ([`02-data-model.md`](02-data-model.md) §6) round-trip through `workers.jsonl` in their wire-visible shape: the `auth_credential_hash` ([`08-storage.md`](08-storage.md) §9) is **stripped** by the exporter (it is not part of the wire-visible Worker schema; see [`02-data-model.md`](02-data-model.md) §6.2). Group rows round-trip through `groups.jsonl` verbatim.

On import, the receiving Store:

1. Creates every group (without resolving members yet).
2. Creates every worker.
3. Re-establishes group memberships from the checkpoint data.
4. When `manifest.requires_credential_reissue` is `true`, mints a fresh credential ([`08-storage.md`](08-storage.md) §9.1 `reissue_credential`) for each imported worker. The implementation surfaces the new credentials to the operator via an implementation-defined side channel (e.g., a file in the import response's `warnings` array; the reference impl uses this pattern).
5. Reverts every `claimed` task to `pending` (the source's claim tokens are no longer valid).

The credential reissue and claim revert are atomic with the rest of the import: a failure at any step rolls back the entire commit.

The receiving deployment can then resume the experiment from the imported state. Workers re-register their fresh credentials, claim now-pending tasks, submit, integrate, etc. In-flight worker activity that was holding a source-side claim token at export time is NOT resumable through the original claim — the worker is expected to re-claim a now-pending task and start over.

## 9. Round-trip semantics

A conforming implementation MUST preserve every protocol-defined field of every protocol-owned object through the export → import round-trip per the per-object contracts below. Export immediately followed by import yields a structurally-equivalent experiment, modulo:

(a) `artifacts_uri` rewrites on every idea, variant, and submission;
(b) event-id reassignment if the importer's event-id factory differs from the exporter's (event ids are opaque per [`02-data-model.md`](02-data-model.md) §4.1);
(c) claim-state normalization (claimed → pending; the `claim` object is cleared);
(d) credential reissue for every imported worker when `requires_credential_reissue` is true;
(e) `experiment_id` rewrite — every object's `experiment_id` is rewritten from the source `exp_*` to the receiver's minted (or `as_experiment_id`-supplied) `exp_*` (§11). Worker `worker_id`s and group `group_id`s are already opaque and round-trip **verbatim** (they are not rewritten); only the `experiment_id` scoping field changes.

The contract per object kind:

**Tasks.** Every `task_id` in the source is present in the import. `kind`, `payload`, `target`, `created_by`, `submitted_by`, `created_at`, `updated_at` round-trip verbatim. `state` round-trips verbatim EXCEPT `claimed` becomes `pending` per (c) above. The `claim` field is empty on every imported task.

**Ideas, variants, submissions.** Round-trip identical to their schema-validated forms, except `artifacts_uri` per (a). Variant `evaluation`, `commit_sha`, `variant_commit_sha`, `branch`, `parent_commits`, `description`, `executed_by`, `evaluated_by`, `completed_at`, `status` all round-trip verbatim.

**Events.** Replay in the same order with the same per-event `type` / `occurred_at` / `data` payload. The envelope `experiment_id` is rewritten to the receiver's `exp_*` per (e). The `event_id` MAY differ per (b). Attribution ids inside `data` (worker `worker_id`, actor `reassigned_by` / `updated_by` / `terminated_by`, member ids) are opaque and round-trip verbatim.

**Git repo.** The bundle contains every object reachable from any ref in the source repo. The importer's repo, after `git fetch <bundle>`, has the same SHAs reachable from the same refs.

**Experiment runtime.** The `state` field ([`02-data-model.md`](02-data-model.md) §2.5) round-trips verbatim. A checkpoint of a `terminated` experiment imports as `terminated` (subsequent task-creation attempts are rejected per [`02-data-model.md`](02-data-model.md) §2.5; the [`06-integrator.md`](06-integrator.md) drain semantics continue to apply to any `success` variants without `variant_commit_sha`). A checkpoint of a `running` experiment imports as `running`. The `created_at` and `base_commit_sha` fields round-trip verbatim (`base_commit_sha` is absent on the import when it was absent on the source). The `imported_from` field on the receiving Experiment is set by the importer per §10.

**Worker and group registries.** Round-trip per §8 above.

## 10. Recovery on lost import response

The `POST /v0/checkpoints/import` wire endpoint commits state-mutating writes; it is not idempotent in the strict HTTP sense. Because the receiver imports an unkeyed checkpoint under **its own** experiment id — a freshly-minted `exp_*` for a multi-experiment receiver, or its single configured `experiment_id` for a single-experiment receiver (§5; [`07-wire-protocol.md`](07-wire-protocol.md) §14.2) — rather than the source manifest's id, a naive retry could create a duplicate experiment (multi-experiment receiver) or re-import the same source (single-experiment receiver). To make safe retry possible after a transport-indeterminate failure, a conforming receiving Store MUST record provenance on the imported experiment that lets a client probe whether a given source archive has already been imported:

- The imported Experiment row MUST carry an `imported_from` field of shape `{checkpoint_exported_at: timestamp, checkpoint_format_version: string, source_experiment_id: opaque-id}` ([`02-data-model.md`](02-data-model.md) §2.5).
- The `checkpoint_exported_at` value MUST be taken from the manifest's `exported_at` field verbatim; `source_experiment_id` MUST be the manifest's `experiment_id` (the source `exp_*`) verbatim.
- Natively-created experiments (never imported) MUST have `imported_from` absent (`null` on the wire).

A client whose import call lost its `201 Created` response probes the receiver's `imported_from` provenance and matches on the pair `(source_experiment_id, checkpoint_exported_at)`. A multi-experiment client enumerates `list_experiments` ([`07-wire-protocol.md`](07-wire-protocol.md) §15.1); a **single-experiment** client reads back its one configured experiment (`read_experiment`, [`07-wire-protocol.md`](07-wire-protocol.md) §14.3) directly — it already knows the receiving id:

- If the probed experiment's `imported_from.source_experiment_id` equals the local manifest's `experiment_id` AND its `checkpoint_exported_at` equals the local manifest's `exported_at`, the import has already committed; the missing 201 was a transport blip and recovery is complete. The client reads back that experiment's `experiment_id` (the receiver's id) from the probe.
- If no such experiment exists (or the single configured experiment carries no matching provenance), the prior call did not commit; the client retries the import normally (which imports under the receiver's own id again).

This is "bounded idempotency": full content-addressed-by-bytes idempotency would require hashing the upload before any commit, which defeats streaming. The `(source_experiment_id, exported_at)` probe gives operators a recovery path without that cost.

## 11. Experiment-id minting and override

The default import path lands the imported experiment under **the receiver's own** opaque `exp_*` id — a freshly-minted id for a multi-experiment receiver, or the single configured `experiment_id` for a single-experiment receiver ([`07-wire-protocol.md`](07-wire-protocol.md) §14.2); because the receiving id is the receiver's own, never the source's, the default path never returns `409 eden://error/experiment-id-conflict` on identity. The receiver rewrites the source id to its own receiving id everywhere it appeared in the archive (in `tasks.jsonl`'s `experiment_id` fields, `ideas.jsonl`, etc.) and records the source id in `imported_from.source_experiment_id` (§10).

The operator MAY pass `as_experiment_id=<exp_*>` ([`07-wire-protocol.md`](07-wire-protocol.md) §14.2) to pin the imported experiment to a specific opaque id (matching the [`02-data-model.md`](02-data-model.md) §1.6 grammar) instead of letting the receiver mint one; the imported experiment then carries that id verbatim everywhere the source's id appeared. When the supplied override collides with an existing experiment the importer MUST reject with `409 eden://error/experiment-id-conflict` — silently choosing a different non-colliding id under an explicit override is non-conforming.

## 12. Cross-reference validation

Before committing any imported state, the importer MUST validate cross-references between the JSONL files and the git bundle:

1. Every variant's `branch` (when set) MUST resolve to a commit in the bundle via `git rev-parse refs/heads/<branch>`.
2. Every variant's `commit_sha` (when set) MUST be reachable in the bundle.
3. Every variant's `variant_commit_sha` (when set) MUST be reachable in the bundle.
4. Every idea's `parent_commits` MUST all be reachable in the bundle.

A failure of any check MUST cause the importer to reject the entire checkpoint with `400 eden://error/checkpoint-invalid`. NO partial state may be committed.

The opposite direction — refs present in the bundle but unreferenced by any task/idea/variant/submission — is permitted (the bundle MAY be a superset of what the imported objects reference; the importer ignores extras).

## 13. Versioning

The `checkpoint_format_version` and `spec_version` manifest fields together describe what a consumer must support to interpret the archive. The rules:

- A consumer encountering a `checkpoint_format_version` it does not recognize MUST reject the checkpoint with `409 eden://error/unsupported-checkpoint-version`.
- A consumer encountering a `spec_version` that does not match its own MUST reject the checkpoint with `409 eden://error/spec-version-mismatch` and SHOULD reference any implementation-provided migration mechanism in the error `detail`.

A future v1 will have its own `spec_version` value (`"v1"`). Detection is at major-version granularity. Within a major version, schema additions MUST be backward-compatible (no required-field additions); subtractions or incompatible changes bump the major.

The v0 spec does not define a migration tool. A cross-version migration is out of scope; an operator with a v0 checkpoint to import into a v1 deployment uses an implementation-supplied migration utility (or re-runs the experiment).

## 14. What this chapter does NOT cover

- **Selective export.** "Just the variants" or "everything except events older than X" partial captures are deferred to a future spec lineage.
- **Encryption.** The format itself is unencrypted. Confidentiality is a transport-layer concern (HTTPS; encrypted-at-rest archives). Operators with sensitive checkpoint contents are expected to manage this externally; the spec carries no contract.
- **Cross-version migration.** See §13.
- **Operator-visible discovery.** The spec does not define a `list_checkpoints` endpoint. Checkpoints are typically external artifacts; a deployment that wants a server-side catalog ships it as a non-normative extension.
- **Signed checkpoints.** No PKI assumption in v0. A future spec lineage MAY add a signature field to the manifest; the format reserves no slot for it today.

## 15. Conformance

An implementation that claims the v1+checkpoints conformance level ([`09-conformance.md`](09-conformance.md) §4) MUST:

- Implement the export and import wire operations from [`07-wire-protocol.md`](07-wire-protocol.md) §14 with the semantics of this chapter.
- Implement the Store-side `export_checkpoint` and `import_checkpoint` operations ([`08-storage.md`](08-storage.md) §1.9).
- Snapshot the source state atomically per §6.
- Round-trip every protocol object per §9.
- Validate cross-references per §12.
- Reject mismatched `spec_version` / unsupported `checkpoint_format_version` / malformed archives with the closed error vocabulary in [`07-wire-protocol.md`](07-wire-protocol.md) §9.

The wire-observable contract is the conformance surface: a checkpoint emitted by any conforming implementation MUST be importable by any other conforming implementation at the same `spec_version`.
