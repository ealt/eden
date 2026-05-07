# Phase 12b — Portable checkpoints

**Status.** Draft.

**Predecessors.** Phase 12a (the three lifecycle/identity chunks
landed via PRs #57, #61, #62). 12b assumes 12a's data shapes are in
place: workers and groups are first-class, tasks have tagged
`target`, ideas have `intended_executor`, the experiment has a
runtime `state` field, and the four legacy termination fields are
gone.

**Design source.**
[`docs/design/portable-checkpoints.md`](../design/portable-checkpoints.md).
The design doc identifies the format, the round-trip contract, and
six open questions; this plan answers the open questions and
produces an executable contract.

**Naming.** Pre-draft check against
[`docs/glossary.md`](../glossary.md) and AGENTS.md "Naming discipline":

- "Checkpoint" is the noun for the file/archive; "export" /
  "import" are the verbs (parallel to git's bundle vocabulary).
  No collision with existing identifiers.
- The format's content-addressed scheme uses
  `sha256:<hex>` (already the conventional shape used for
  `commit_sha` etc.); the new wire URL prefix
  `checkpoint:sha256:<hex>` echoes existing conventions without
  introducing a competing scheme. <!-- rename-discipline:cite -->
- "Native checkpoint" is the historical postgres-dump+gitea-tar
  pair; "portable checkpoint" is the new format. The existing
  `eden-experiment checkpoint` / `restore` commands at
  [`reference/scripts/manual-ui/eden-experiment`](../../reference/scripts/manual-ui/eden-experiment)
  retain their CLI shape; the `--portable` flag selects format.

## 1. Context

Today the reference impl supports a "checkpoint" workflow that
bundles `pg_dump` output + a `gitea` data-volume tar + an
`artifacts` data-volume tar
([`reference/scripts/manual-ui/eden-experiment`](../../reference/scripts/manual-ui/eden-experiment)
lines 244-318). It's deployment-portable in the trivial sense
(operator can carry the file to another host running the same
deployment), but it is NOT spec-portable — a third-party
implementation that doesn't run postgres + gitea cannot consume
the file. A user starting an experiment on impl A and handing the
checkpoint to a user on impl B has no path forward.

The design doc proposes a portable format that captures the
**logical** state — task / idea / variant / submission / event JSON,
a git bundle, and content-addressed artifacts — plus a round-trip
contract that makes "export from A, import to B, resume" the
working scenario.

12b makes that design a contract:

- A new spec chapter defines the format, manifest, atomicity
  contract, and round-trip semantics.
- New wire endpoints (`POST .../checkpoint` and
  `POST /v0/checkpoints/import`) bind the operations.
- The reference impl gains `Store.export_checkpoint` /
  `Store.import_checkpoint` Protocol methods + corresponding
  backends + e2e smoke that exports from the SQLite-backed stack
  and imports into the Postgres-backed stack.
- The native postgres-dump/gitea-tar checkpoint is **removed** in
  the same chunk: greenfield posture (no external users), and
  keeping two checkpoint mechanisms in the script doubles the test
  surface for no benefit.

12b explicitly does NOT cover:

- Selective / partial export (open question 2 → 12c or v2).
- Built-in encryption (open question 3 → informative note in the
  spec; transport-layer concern).
- Streaming server-side encoding for huge experiments (open
  question 6 → addressed at the protocol level by chunked transfer
  encoding; spec recommendation but no normative streaming
  requirement in v0).

### 1.1 Spec baseline + reconciliation

| Existing site | Current text | 12b disposition |
|---|---|---|
| [`spec/v0/02-data-model.md`](../../spec/v0/02-data-model.md) §5.1 (idea) and §7.1 (variant) | `artifacts_uri` is "a URI" (any scheme; deployment-defined) | Add a normative note: `artifacts_uri` MUST be deployment-local; the portable-checkpoint format normalizes references to `checkpoint:sha256:<hex>` per chapter 10 §X. |
| [`spec/v0/07-wire-protocol.md`](../../spec/v0/07-wire-protocol.md) §1 endpoint catalog | Wire ops in §§2-7 | Add §8 "Checkpoint operations" with the two new endpoints. |
| [`spec/v0/08-storage.md`](../../spec/v0/08-storage.md) §1 | `Store` ops + atomicity guarantees | Add `export_checkpoint` / `import_checkpoint` to the Store Protocol; restate atomicity for export ("snapshot-consistent"). |
| [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §3 conformance levels | Three levels: v1, v1+roles, v1+roles+integrator | Add a fourth level: `v1+checkpoints`. The level adds the two new endpoints + round-trip equivalence assertions. |
| New chapter (proposed §10) | n/a | "Portable checkpoint format" — full normative spec for layout, manifest, addressing, atomicity, round-trip semantics. |
| [`reference/scripts/manual-ui/eden-experiment`](../../reference/scripts/manual-ui/eden-experiment) lines 244-318 | Native postgres+gitea-tar checkpoint | Removed. Replaced with portable-format export. |

Reconciliation rule: chapter 10 is the canonical spec for the
checkpoint format; chapters 02 / 07 / 08 / 09 cross-reference it
without restating the format.

### 1.2 Naming-discipline baseline

PR #60's strengthened guardrail applies. The new
`checkpoint:sha256:<hex>` URI prefix is added to the glossary as
a recognized URI scheme; the script command names (`checkpoint`,
`restore`) are unchanged from the existing UX.

## 2. Decisions

These are the load-bearing design calls; §3 unpacks each.

1. **The portable checkpoint format is the only checkpoint format
   in v0.** The existing native postgres+gitea-tar checkpoint is
   removed in this chunk. Greenfield: no transition period, no
   compat shim. The script's `checkpoint` / `restore` commands
   produce / consume the new format directly.

2. **A new spec chapter (proposed §10 "Portable checkpoint
   format")** defines the layout, manifest, addressing, atomicity,
   and round-trip semantics. This is normative; conforming impls
   that support checkpoints MUST follow it.

3. **The format mirrors the design doc's directory tree** with
   one refinement: the `events.jsonl` file is **append-only** (one
   event per line in the order they were committed), and consumers
   MUST replay them in that order. This makes the event log's
   role-as-audit-trail (chapter 05 §1) survive the round-trip.

4. **Artifacts are content-addressed** as `artifacts/sha256/<hex>`
   per the design doc. On import, references to
   `checkpoint:sha256:<hex>` in the JSONL files are rewritten to
   the receiving deployment's URI scheme (e.g., `file://` for the
   reference impl). The artifact bytes are dedup'd at the source.

5. **Experiment-id collision on import is operator-resolved with
   bounded idempotency** (open question 1). The default policy is
   **reject with 409 `eden://error/experiment-id-conflict`**; the
   operator passes `?as_experiment_id=<new_id>` to import under a
   different id. Auto-rename was rejected as too magical; the
   explicit override makes the operator's intent legible.

   **Idempotency on retry.** The wire endpoint is non-idempotent
   in the strict sense (it commits state-mutating writes). To
   make safe retry possible after a transport failure, this
   chunk extends the 12a-3 `Experiment` shape with one new field
   and adds one new read op:

   - **Schema extension.** `Experiment.imported_from:
     ImportProvenance | null` (optional). The
     `ImportProvenance` shape carries `checkpoint_exported_at:
     timestamp` and `checkpoint_format_version: string`. Set at
     import time; absent on natively-created experiments. (The
     `ImportProvenance` shape is added to
     `experiment.schema.json` per §5.1 below.)
   - **Read op.** `read_experiment(experiment_id) → Experiment`
     extends the 12a-3 `read_experiment_state` op to return the
     full experiment object (state + created_at + imported_from).
     The op is added to the Store Protocol per §5.2 and bound
     in chapter 07 per §5.1.

   With both in place, a client whose import call lost its
   `201 Created` queries `read_experiment(id)`:
   - If the experiment exists with
     `imported_from.checkpoint_exported_at` matching the local
     manifest's `exported_at`, the import already succeeded;
     the operator treats the missing 201 as a transport blip.
   - If the field is absent or mismatched, the import did not
     commit — operator retries with `as_experiment_id=<new>`.

   This is "bounded idempotency": full-fledged
   content-addressed-by-bytes idempotency would require the
   importer to hash the upload before committing, which
   defeats streaming. The exported_at probe gives operators the
   recovery path without that cost.

   The new id is reflected in the import response (which the
   client may or may not receive — hence the recovery probe
   above).

6. **Worker-identity round-trip** (open question 4). The
   checkpoint carries the source experiment's worker registry +
   group registry as additional JSONL files (`workers.jsonl`,
   `groups.jsonl`). On import, the receiving Store creates the
   workers + groups verbatim, with one twist: the source's
   bearer-token hashes are NOT carried (per the §1.2 secret-
   portability non-goal). The receiving deployment MUST reissue
   credentials for each imported worker before the experiment
   resumes, OR the checkpoint MUST be marked
   `requires_credential_reissue: true` in the manifest so consumers
   can detect this and prompt the operator.

7. **Schema-version skew is detected, not migrated** (open
   question 5). The manifest carries a `spec_version` field. An
   importer MUST reject a checkpoint whose `spec_version` doesn't
   match the importer's spec version, with an informative error
   pointing the operator at a separate migration tool. v0 has no
   migration tool yet; the rejection is the safety net.

8. **HTTP chunked transfer encoding is RECOMMENDED, not
   required** (open question 6). The wire endpoint
   `POST .../checkpoint` returns `Content-Type:
   application/x-eden-checkpoint+tar`. Servers MAY use
   `Transfer-Encoding: chunked` (recommended for large
   checkpoints to avoid memory pressure) or buffered responses
   with `Content-Length`. Clients MUST handle both. Tests
   assert "the response is parseable as the checkpoint format",
   not "the response uses chunked encoding".

   The "export = materialize the artifact" boundary (§3.7
   below) lives entirely server-side; HTTP delivery happens
   after materialization is complete. So a server that
   materializes to a temp file, closes its source-state
   transaction, and then streams the temp file back is
   equivalent to a server that streams directly from the
   transaction — both yield the same atomic snapshot.

9. **Atomicity is required but mechanism is not.** The exporter
   MUST snapshot the source state at a single logical instant.
   Either "quiesce + dump" or "transactional snapshot" is
   conformant. The reference impl uses a serializable
   `BEGIN TRANSACTION` over the SQLite/Postgres connection (the
   git repo + artifacts read alongside it). The atomicity rule is
   normative; the choice of mechanism is implementation-defined.

10. **Import preserves resume-safe state, not exact live state.**
    Cementing this so it's easy to find: a checkpoint preserves
    *structural* state (every persistent object — task, idea,
    variant, submission, event, worker, group, experiment runtime
    record — round-trips with all its protocol-defined fields).
    It deliberately does NOT preserve *volatile auth + ownership
    state* (bearer-token hashes, claim tokens, in-flight claim
    bindings). On import:
    - Every imported worker MUST have a freshly minted
      credential (12a-1 §D.1 reissue path); the source's
      credential hashes are NOT carried.
    - Every claimed task is reverted to `pending` at import
      time; the original claim token is invalidated.
    - In-flight session credentials (12a-1 §D.5b web-ui per-
      session credentials) are not part of state and not
      preserved.
    The receiving deployment can resume the experiment from the
    imported state — workers register fresh credentials, claim
    tasks, submit, integrate — but it cannot resume in-flight
    work that was holding a source-side claim token. This
    "structural-not-volatile" contract is the import promise.

### 2.1 Field-by-field preservation map

The 12a series introduces several persistent protocol objects
this chunk must serialize. Authoritative shapes per the 12a
plans:

| Object | Source | Carry verbatim | Strip | Reason |
|---|---|---|---|---|
| `Task` | 12a-1 §D.3, chapter 02 §3 | `task_id`, `kind`, `state`, `payload`, `target`, `created_by`, `submitted_by`, `created_at`, `updated_at` | `claim` (cleared on import; tasks revert to `pending` if `claimed` at export time) | Claim tokens are volatile auth material per Decision 10. |
| `Idea` | chapter 02 §5, 12a-3 §3.6 | `idea_id`, `experiment_id`, `state`, `slug`, `priority`, `parent_commits`, `artifacts_uri` (rewritten to `checkpoint:sha256:<hex>`), `intended_executor`, `created_by`, `created_at` | none | All idea fields are structural. |
| `Variant` | chapter 02 §7, 12a-1 §D.4 | `variant_id`, `experiment_id`, `idea_id`, `branch`, `commit_sha`, `variant_commit_sha`, `parent_commits`, `status`, `metrics`, `artifacts_uri` (rewritten), `description`, `executed_by`, `evaluated_by`, `completed_at` | none | All variant fields are structural. |
| `Submission` | chapter 04 §4.2 | the full submission payload per role | none | The shape is role-specific but fully serializable. |
| `Event` | chapter 05 | `event_id`, `type`, `occurred_at`, `experiment_id`, `data`, plus the position in the log | none | Event IDs MAY be reassigned on import per chapter 05 (factory is impl-defined); the order MUST be preserved. Event field names match `spec/v0/05-event-protocol.md` envelope. |
| `Worker` (12a-1 §D.1) | 12a-1 §D.1 | `worker_id`, `experiment_id`, `registered_at`, `registered_by`, `labels` | `auth_credential_hash` | The credential hash is volatile auth material per Decision 10. The receiving deployment mints a fresh credential; the new credential is surfaced in a sidecar file (§3.6). |
| `Group` (12a-1 §D.2) | 12a-1 §D.2 | `group_id`, `experiment_id`, `members`, `created_at`, `created_by` | none | All group fields are structural. |
| `Experiment` (12a-3 §3.2 + 12b extension) | 12a-3 §3.2 (base shape: `experiment_id`, `state`, `created_at`) + 12b extension (`imported_from: ImportProvenance \| null`) | All fields preserved | none | Runtime state is structural. The `imported_from` field is set at import time on the receiving Experiment; the source's `imported_from` (if any) is NOT copied — the new value reflects the most recent import. |
| `dispatch_mode` (12a-2 §3.2) | 12a-2 §3.2 | the four-key object on the experiment-config | none | Carried verbatim within the experiment config. |
| ExperimentConfig (chapter 02 §2) | chapter 02 §2 | the full YAML/JSON, post-12a-3 (with the four legacy termination fields removed) | n/a | Carried verbatim in `experiment-config.yaml`. |

The previous prose "may already exist post-12a-1; verify when
implementing" wording in §5.4 is replaced by this table — the
plan now pins shapes authoritatively.

## 3. Design

### 3.1 Spec chapter 10 — portable checkpoint format

The new chapter walks the design doc's content but at normative
strength. Section sketch:

> ## (Proposed Chapter 10) Portable checkpoint format
>
> ### 10.1 Purpose
>
> [What/why — design doc Premise + Goals.]
>
> ### 10.2 Logical contents
>
> A checkpoint represents a snapshot of the experiment's logical
> state at a single instant, sufficient to resume the experiment
> on any conforming implementation.
>
> [Table of source-chapter ↔ contents per design doc §Logical
> contents, plus workers.jsonl + groups.jsonl from §3.6.]
>
> ### 10.3 Format
>
> A checkpoint is a directory tree wrapped in a tarball or zip
> for transport. The directory layout MUST be:
>
> [Tree + manifest layout from design doc §Format, with
> workers.jsonl + groups.jsonl added.]
>
> ### 10.4 Manifest
>
> [JSON sketch + field semantics; explicitly include
> `spec_version`, `checkpoint_format_version`,
> `requires_credential_reissue`, and the `counts` block.]
>
> ### 10.5 Atomicity
>
> The exporter MUST capture the source state atomically with
> respect to ongoing operations. [Quiesce-and-dump vs
> transactional-snapshot per design doc §Atomicity, both
> conformant.] Operations that would mutate state during a live
> export MUST either be serialized after the export completes or
> rejected with a `eden://error/checkpoint-in-progress` (the
> implementation chooses).
>
> ### 10.6 Round-trip semantics
>
> [State equivalence per design doc §Round-trip semantics.
> Plus: the receiving deployment MUST reissue credentials for
> each imported worker before the experiment resumes if the
> manifest carries `requires_credential_reissue: true`.]
>
> ### 10.7 Wire bindings
>
> [Cross-reference chapter 07 §8 for the HTTP endpoints.]
>
> ### 10.8 Versioning
>
> A consumer encountering a `checkpoint_format_version` it does
> not recognize MUST reject the checkpoint with
> `eden://error/unsupported-checkpoint-version`. A consumer
> encountering a `spec_version` that does not match its own
> SHOULD reject with
> `eden://error/spec-version-mismatch` and reference any
> migration mechanism the implementation provides.

The chapter is ~200 lines. It cross-references chapters 02 /
04 / 05 / 06 / 07 / 08 throughout for the underlying object
schemas; it does not redefine them.

### 3.2 The format directory tree

Per design doc §Format, with two additions:

```text
<checkpoint>/
  manifest.json             # required (§3.3 below)
  experiment-config.yaml    # the experiment config (verbatim)
  experiment.json           # the runtime experiment object (state, created_at) — 12a-3 addition
  tasks.jsonl               # one Task per line; schema = task.schema.json
  ideas.jsonl               # one Idea per line; schema = idea.schema.json
  variants.jsonl            # one Variant per line; schema = variant.schema.json
  submissions.jsonl         # one Submission per line (any role)
  events.jsonl              # one Event per line, in append order
  workers.jsonl             # one Worker per line — 12a-1 addition (worker registry)
  groups.jsonl              # one Group per line — 12a-1 addition (group registry)
  repo.bundle               # `git bundle create --all` of the experiment's bare repo
  artifacts/
    sha256/<hex>            # one file per unique artifact (content-addressed)
```

The two new JSONL files (`workers.jsonl`, `groups.jsonl`) are
required for any 12a+ checkpoint. A pre-12a-1 checkpoint omits
them; an importer that lands the 12a-1 registry semantics MAY
treat their absence as "no workers, no groups" but MUST flag it
as a `requires_credential_reissue: true` situation in the manifest.

The `experiment.json` file (12a-3 addition) carries the runtime
state — `experiment_id`, `state ∈ {"running", "terminated"}`,
`created_at`. A pre-12a-3 checkpoint omits it; importers default
to `state="running"`.

### 3.3 Manifest schema

```json
{
  "checkpoint_format_version": "1",
  "spec_version": "v0",
  "experiment_id": "exp-abc",
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

Extensions:

- `exporter.atomicity_mechanism` is informative; a consumer that
  needs to know which atomicity strategy was used can read it,
  but no normative behavior depends on the value.
- `requires_credential_reissue` is normative: when `true`, the
  importer MUST run `reissue_credential` for every imported
  worker before the experiment resumes, and any `claimed` tasks
  are reverted to `pending` during import. This flag does NOT
  affect the imported experiment's `state` field — that is
  preserved verbatim from `experiment.json` per §3.6 (a
  terminated source experiment imports as terminated; a running
  source imports as running). The credential-reissue and
  claim-revert semantics apply equally to both states; the
  `state` field is independent of those mechanics.

### 3.4 Wire endpoints

```text
POST /v0/experiments/<E>/checkpoint
```

Produces a checkpoint of experiment E. Response:
`200 OK` with `Content-Type: application/x-eden-checkpoint+tar`,
streaming the tar directly. Authority: `admins`.

Optional query parameters:

- `format_version=1` (defaults to current; future versions add
  `2`, `3`, etc.)

```text
POST /v0/checkpoints/import
```

Accepts a checkpoint upload. Request:
`Content-Type: application/x-eden-checkpoint+tar`,
streaming tar body. Response: `201 Created` with JSON:
`{experiment_id: "<id>", warnings: [...]}`. Authority: `admins`.

Optional query parameters:

- `as_experiment_id=<new_id>` — override the source's experiment
  id. If absent and a collision exists, returns
  `409 eden://error/experiment-id-conflict`.

### 3.5 Content-addressed artifacts

Each unique artifact (deduped by content hash) lives at
`artifacts/sha256/<hex>` where `<hex>` is the SHA-256 of the
artifact's bytes (lowercase hex, 64 chars). The exporter computes
the hash at export time. JSONL files in the checkpoint contain
artifact references as `checkpoint:sha256:<hex>` URIs (a new
recognized scheme).

On import, the receiving Store walks `artifacts/sha256/` and
materializes each into its own artifact backend (file://, S3,
etc.). It then walks every `checkpoint:sha256:<hex>` reference in
the JSONL and rewrites it to the local URI. The rewrites are
applied during the same transaction that creates the rows; an
import either completes fully or rolls back fully.

### 3.6 Worker + group portability

12a-1 introduced worker and group registries. The checkpoint
captures both:

- `workers.jsonl`: one line per worker, in the 12a-1 §D.1
  shape: `{worker_id, experiment_id, registered_at,
  registered_by, labels}`. The `auth_credential_hash` field
  defined by 12a-1 is **stripped** (per §2 Decision 10's
  volatile-auth-material rule); the manifest's
  `requires_credential_reissue: true` flag signals the importer
  to reissue.
- `groups.jsonl`: one line per group, in the 12a-1 §D.2 shape:
  `{group_id, experiment_id, members, created_at, created_by}`.
  No fields stripped.

On import:

1. Materialize all groups (without members).
2. Materialize all workers (without group memberships, no
   credentials).
3. Re-establish group memberships from the checkpoint data.
4. Mint fresh credentials for each worker via
   `reissue_credential` (12a-1 §D.1). The new credentials are
   recorded in a sidecar file the operator gets out-of-band.
5. All `claimed` tasks are reverted to `pending` (since the
   original claim tokens are no longer valid). The experiment's
   `state` field is preserved verbatim from the checkpoint
   manifest's `experiment.json`:
   - If the source experiment was `running`, the imported
     experiment is `running`; the reverted-pending tasks are
     freshly claimable.
   - If the source experiment was `terminated`, the imported
     experiment is `terminated`; the reverted-pending tasks are
     unreachable per 12a-3 §3.2 (terminated experiments reject
     claim). Integration of `status="success"` variants without
     `variant_commit_sha` continues to drain post-import per
     12a-3 §6.2 decision-type 4.

   Importing a terminated experiment is therefore "audit/replay-
   only" with one exception: integration drain. The operator
   gets the structural state and the event-log replay; they do
   NOT get a runnable experiment unless they explicitly
   transition state out of terminated, which is not a v0
   transition (12a-3 reserves `terminated → running` for a
   future amendment).

The credential-reissue side channel (step 4) is implementation-
defined: the reference impl writes the new credentials to a temp
file and prints the path on stdout. A spec-conformant alternative
is "the importer requires the operator to run `reissue_credential`
manually for each imported worker"; both are valid.

### 3.7 Atomicity contracts

The exporter MUST snapshot the source state at a single logical
instant. Two strategies are acceptable:

**Quiesce-and-dump.** The exporter pauses the orchestrator and
all workers (e.g., via a deployment-level lock or a dispatch_mode
flip), captures the snapshot, and resumes. Simple; small
downtime. The reference impl does NOT use this strategy.

**Transactional snapshot.** The exporter takes a serializable
transaction over the Store + reads the git repo state at the
same logical point, and **materializes the checkpoint artifact**
(temp file or in-memory buffer) before closing the transaction.
"Export complete" in the protocol sense means "the materialized
artifact is finalized"; HTTP delivery to the client happens
*after* the transaction closes. This is the reference impl's
strategy:

```python
with store.read_snapshot() as snapshot:
    # snapshot is a frozen view; no writes can interleave
    tasks = snapshot.list_tasks()
    ideas = snapshot.list_ideas()
    variants = snapshot.list_variants()
    events = snapshot.read_range(0, ...)
    repo_sha_at_snapshot = repo.head()
    materialize_to_tempfile(tasks, ideas, variants, events, ...)
# transaction closed; temp file is the atomic snapshot artifact
stream_tempfile_to_client()  # HTTP delivery; not part of atomicity contract
```

For SQLite the snapshot is a `BEGIN IMMEDIATE` transaction with
deferred commit; for Postgres it's a `BEGIN ISOLATION LEVEL
SERIALIZABLE` transaction. The git bundle is generated outside
the transaction (git is content-addressed, so a bundle taken
"after" the transaction ends still represents the same logical
state — newly-added refs are visible but irrelevant since no
new task/idea/variant references them).

The atomicity guarantee is normative; the mechanism is not.

### 3.8 Round-trip semantics

Per §2 Decision 10, the round-trip preserves **structural state**,
not exact live state. The contract:

**Tasks.**

- Same set: every `task_id` in the source is present in the
  import.
- Each task's `kind`, `payload`, `target`, `created_by`,
  `submitted_by`, `created_at`, `updated_at` are preserved
  verbatim.
- Each task's `state` is preserved EXCEPT: `claimed` tasks are
  imported as `pending` (their original claim tokens are
  invalidated per Decision 10). All other states (`pending`,
  `submitted`, `completed`, `failed`) are preserved verbatim.
- Each task's `claim` field is empty on import. The source's
  claim token is not preserved.

**Ideas, variants, submissions.**

- Identical to their schema-validated form, except
  `artifacts_uri` is rewritten to the receiving deployment's
  URI scheme.
- Variant `metrics`, `commit_sha`, `variant_commit_sha`,
  `branch`, `parent_commits`, `description`, `executed_by`,
  `evaluated_by`, `completed_at`, `status` all preserved.

**Events.**

- Replay in the same order with the same payloads. Event IDs
  MAY differ if B's event-id factory differs (chapter 05 §1).
- The event log is treated as audit-trail data; it survives the
  round-trip in its entirety.

**Git repo.**

- Same objects reachable from the same refs (verified by SHA
  equality on `git rev-parse <ref>` for every ref in the
  checkpoint).

**Experiment runtime.**

- `experiment.state` (the 12a-3 lifecycle field) is preserved
  verbatim from the checkpoint manifest's `experiment.json`. A
  checkpoint of a `terminated` experiment imports as
  `terminated`; a checkpoint of a `running` experiment imports
  as `running`. Per §3.6 the importer behavior diverges based
  on this value.

**Worker + group registries.**

- Worker `worker_id`, `experiment_id`, `registered_at`,
  `registered_by`, `labels` preserved verbatim.
  `auth_credential_hash` stripped; freshly minted credentials
  recorded in the sidecar file (§3.6).
- Group `group_id`, `experiment_id`, `members`, `created_at`,
  `created_by` preserved verbatim.

**Net round-trip property.** Export immediately followed by
import yields a structurally-equivalent experiment, modulo (a)
URI rewrites for artifacts, (b) event-id reassignment if the
importer's factory differs, (c) claim-state normalization
(claimed → pending), (d) credential reissue. Workers can register
fresh credentials, claim tasks (if the experiment is `running`),
submit, and integrate. Resuming in-flight worker activity that
was holding a source-side claim token is NOT promised — the
worker is expected to re-claim a now-pending task and start
over.

### 3.9 Alternatives considered

Three normative choices in this chunk benefit from explicit
compare-and-reject paragraphs.

**Archive encoding: tar (chosen) vs. zip vs. directory-only.**
The plan hard-codes `application/x-eden-checkpoint+tar` as the
media type for both export and import (§3.4). The design source
treats the directory tree as the *logical* format with tar/zip
as transport conveniences ([`docs/design/portable-checkpoints.md`](../design/portable-checkpoints.md)
§Format). For v0 the plan picks tar:

- Tar is streamable in one pass (write headers + bytes
  inline, no central directory at the end). Zip's central
  directory at the end of the archive prevents pure
  forward-streaming generation; a server would have to seek
  back to update the directory after writing all entries. That
  defeats the §3.7 phase-2 streaming pattern.
- Tar is universally available in standard library / OS
  tooling; zip is also universal but adds compression-format
  ambiguity (deflate vs zip64 vs encryption fields).
- Directory-only (no archive wrapper) is a fine logical
  format but loses the "single file" UX — operators can't `scp`
  a single artifact, can't drop one file into the Web UI's
  upload form. The directory layout stays as the canonical
  *logical* format (per chapter 10 §10.3); tar is just the
  wire envelope.

A future v0 minor MAY add zip as an additional accepted media
type for client convenience; v0 starts with tar only to keep
the conformance surface narrow.

**Artifact addressing: SHA-256 (chosen) vs. BLAKE3 vs.
git-style.** Artifacts are content-addressed as
`artifacts/sha256/<hex>` (§3.5). Alternatives:

- **BLAKE3** is faster than SHA-256 (~5x at multi-GB) and the
  resulting hash is the same 256-bit length. It's less
  universal: SHA-256 is in every standard library + OS toolchain;
  BLAKE3 needs a third-party dependency in many languages. For
  v0 the universality wins; performance is rarely the
  bottleneck for checkpoint workloads (export I/O dominates).
- **Git-style object IDs** (currently SHA-1, transitioning to
  SHA-256 in upstream git) couple artifact identity to git's
  hash family + content-encoding (length-prefix headers). v0
  artifacts are not git objects — they're arbitrary bytes
  (rationale documents, evaluation logs, etc.). Borrowing git's
  ID format would create a confusing pseudo-coupling without
  semantic benefit.
- The chosen `sha256:<hex>` matches the existing project
  vocabulary (`commit_sha` is also a hash hex string elsewhere)
  and is the obvious default.

A future v1 MAY add an alternative scheme (e.g.,
`blake3:<hex>`) if performance becomes a real concern; v0 stays
single-scheme.

**Import endpoint root: global `/v0/checkpoints/import`
(chosen) vs. experiment-scoped two-step flow.** Chapter 07
§1.3's experiment-scoping rule normally requires every path to
begin with `experiments/{id}/`. The chosen import endpoint
violates that rule and requires an explicit §1.3 amendment
(§5.1). Alternatives considered:

- **Two-step experiment-scoped flow.** The client first calls
  `POST /v0/experiments/<chosen_id>/create-from-checkpoint`
  (creating an empty experiment row), then
  `POST /v0/experiments/<chosen_id>/restore-checkpoint` (uploading
  the bytes). Pros: no §1.3 amendment needed; experiment_id is
  always in the URL; the two-step shape mirrors the existing
  `register_worker` + `add_to_group` pattern from 12a-1. Cons:
  the two calls aren't atomic; if step 1 succeeds and step 2
  fails, the operator has a half-created experiment to clean
  up; the client has to invent + supply the chosen_id BEFORE
  reading the manifest, which is awkward when the operator's
  intent is "import as the source's id if free, otherwise as
  `<new>`".
- **Global root with §1.3 amendment** (chosen). Pros: single
  atomic call; the experiment_id comes from the manifest (no
  client-side guessing); collision handling is a first-class
  parameter. Cons: requires the §1.3 amendment, which is mild
  but real spec surface.

The single-call experience for operators outweighs the §1.3
amendment cost. The amendment is scoped narrowly to
`/v0/checkpoints/...` (the only non-experiment-scoped path
family in v0), which keeps the chapter 07 rule clean.

### 3.10 What 12b does NOT do

- **Selective export.** A "just the variants" or "without the
  event log" partial format is deferred to a later spec
  amendment.
- **Built-in encryption.** The format itself is unencrypted;
  operators handle confidentiality at the transport layer
  (HTTPS, encrypted tarballs at rest, etc.). The spec carries an
  informative note advising this.
- **Cross-version migration.** A v0 checkpoint imported into a
  v1 implementation is rejected with
  `eden://error/spec-version-mismatch`. A separate migration
  tool would handle that case; it is not part of v0.
- **Backward compatibility with the native checkpoint format.**
  The native format is removed in this chunk. Pre-12b
  checkpoints CANNOT be imported into a 12b deployment; the
  operator must export the running experiment via the old format
  before the upgrade and reimport via the new format after.
  Greenfield: no external users, this is acceptable.

## 4. Scope

### 4.1 In scope

Spec edits:

- **New chapter 10** "Portable checkpoint format" (§3.1 above).
- Chapter 02: small note on `artifacts_uri`'s deployment-local
  semantics + cross-reference to chapter 10.
- Chapter 07: §8 "Checkpoint operations" with the two new endpoints.
- Chapter 08: `export_checkpoint` / `import_checkpoint` added to
  the Store Protocol; atomicity restated.
- Chapter 09: new conformance level `v1+checkpoints`; new
  conformance index entries.
- Schema files: new `manifest.schema.json` (the manifest format);
  small additions to `idea.schema.json` and `variant.schema.json`
  for the `artifacts_uri` deployment-local note (the schema rule
  doesn't change; just a description field).

Code (reference impl):

- `eden_storage.Store` Protocol gains
  `export_checkpoint(stream: BinaryIO) -> CheckpointMetadata` and
  `import_checkpoint(stream: BinaryIO, as_experiment_id:
  str | None = None) -> str` (returns the new experiment_id).
- `_base.py` implements the export/import logic shared across
  backends. Backend-specific atomicity (SQLite `BEGIN IMMEDIATE`,
  Postgres `SERIALIZABLE`, in-memory snapshot copy) is in
  `sqlite.py` / `postgres.py` / `_memory.py`.
- `eden_wire.server.py` adds the two endpoints. `client.py` adds
  `StoreClient.export_checkpoint(path)` and
  `StoreClient.import_checkpoint(path, as_experiment_id=None)`.
- `eden_checkpoint` (new package under `reference/packages/`)
  contains the format reader/writer: tar packing, manifest
  serialization, content-addressed artifact dedup, JSONL emission,
  validation. Used by both the Store backends and the script.
- `reference/scripts/manual-ui/eden-experiment` rewrites the
  `checkpoint` and `restore` commands to use the new format. The
  postgres-dump and gitea-tar code is removed.

Conformance:

- New scenarios under `conformance/scenarios/test_checkpoint_*.py`:
  - **Round-trip equivalence.** Export a small experiment;
    import into a fresh Store; assert all tasks/ideas/variants/
    submissions/events match by content equivalence; assert the
    git bundle round-trips with same refs.
  - **Cross-impl interop.** Export from SQLite-backed Store;
    import into Postgres-backed Store; same equivalence.
  - **Schema-version mismatch.** A checkpoint with
    `spec_version: v1` against a v0 importer returns 409.
  - **Experiment-id collision.** A checkpoint whose
    `experiment_id` already exists on the importer returns 409
    `eden://error/experiment-id-conflict`; with
    `as_experiment_id=<new>`, the import succeeds.
  - **Atomicity.** A live experiment under concurrent worker
    activity exports successfully; the resulting checkpoint is
    self-consistent (e.g., every `task_id` referenced by an
    event exists in `tasks.jsonl`).
  - **Worker + group portability.** A checkpoint with workers
    in `orchestrators` group imports correctly; the
    `requires_credential_reissue` flag is respected (importer
    re-mints credentials).
  - **Terminated-experiment round-trip.** A checkpoint of a
    terminated experiment imports as terminated; subsequent
    task-creation attempts are rejected per chapter 02 §2.5.

Compose / smokes:

- `compose-smoke-checkpoint` (new). Drives an experiment to N
  variants, exports, brings down the stack, brings up a fresh
  stack with a different store backend, imports, asserts the
  experiment can resume (one more variant lands).

### 4.2 Cross-references to followups

- **Selective export** — v2 or 12c.
- **Built-in encryption** — informative note in chapter 10; no
  spec contract.
- **Streaming server-side encoding** — chapter 10 §10.7
  recommends chunked transfer-encoding but doesn't normatively
  require it; future amendment may require it for very-large
  experiments.
- **Cross-version migration tool** — separate effort post-v1.

### 4.3 Out of scope

- Spec-mandated checkpoint discoverability (e.g., a
  `GET /v0/checkpoints/` endpoint listing available checkpoints).
  Checkpoints are typically external artifacts; no need for a
  store-side catalog in v0.
- A signed-checkpoint format. No public-key infrastructure assumed.
- `import` resuming partial imports. An import either completes
  fully or rolls back fully.

### 4.4 Non-goals

- Backward compatibility with the native postgres-dump+gitea-tar
  format. Removed in this chunk.
- A Python-language-specific format. The format is JSON + tar +
  git-bundle, all language-agnostic; the reference impl is in
  Python, but conforming impls in any language can produce /
  consume it.

## 5. Files to touch

### 5.1 Spec

| File | Change |
|---|---|
| `spec/v0/10-checkpoints.md` (new) | The full chapter from §3.1 above. ~200 lines. |
| `spec/v0/02-data-model.md` | Add note on `artifacts_uri` deployment-local semantics with cross-reference to chapter 10. |
| `spec/v0/07-wire-protocol.md` | Add §8 "Checkpoint operations": `POST .../experiments/<E>/checkpoint` and `POST /v0/checkpoints/import` endpoints. Authority: admins. **Amend §1.3 ("Experiment scoping")** to carve out global checkpoint endpoints: §1.3 currently requires every `/v0/` path to begin with `experiments/{experiment_id}/` and to carry an `X-Eden-Experiment-Id` header. The import endpoint creates a new experiment and cannot be experiment-scoped at the URL level (the experiment_id is in the uploaded checkpoint manifest, not the URL). The amendment adds an explicit exception: paths under `/v0/checkpoints/` (currently just `/v0/checkpoints/import`) are not experiment-scoped at the URL level; the experiment_id appears in the request body's checkpoint manifest, and the `X-Eden-Experiment-Id` header is OPTIONAL on these endpoints (when present, it MUST match the manifest's `experiment_id` after applying any `as_experiment_id` override). |
| `spec/v0/08-storage.md` | Add `export_checkpoint` and `import_checkpoint` to the Store Protocol's op list. Restate atomicity. **Extend `read_experiment` op** (12a-3 added `read_experiment_state` returning just the state value; 12b promotes it to `read_experiment` returning the full Experiment object including the new `imported_from` field per §2 Decision 5). |
| `spec/v0/02-data-model.md` (additional) | Extend the Experiment runtime shape (12a-3 §2.5) with `imported_from: ImportProvenance \| null`. Define `ImportProvenance` as `{checkpoint_exported_at: timestamp, checkpoint_format_version: string}`. |
| `spec/v0/schemas/experiment.schema.json` (extension) | Add the `imported_from` property + `ImportProvenance` definition. |
| `spec/v0/09-conformance.md` | Add v1+checkpoints conformance level. Add conformance index entries. |
| `spec/v0/schemas/checkpoint-manifest.schema.json` (new) | JSON Schema for the manifest. |

### 5.2 eden-checkpoint (new package)

| File | Change |
|---|---|
| `reference/packages/eden-checkpoint/src/eden_checkpoint/__init__.py` | Public API: `Checkpoint`, `Manifest`, `read_checkpoint`, `write_checkpoint`. |
| `reference/packages/eden-checkpoint/src/eden_checkpoint/manifest.py` | Pydantic model + serializer for the manifest. |
| `reference/packages/eden-checkpoint/src/eden_checkpoint/format.py` | Tar packing/unpacking, JSONL writer/reader, content-addressed artifact handling. |
| `reference/packages/eden-checkpoint/src/eden_checkpoint/repo_bundle.py` | Wraps `git bundle create --all` and `git fetch <bundle>`. |
| `reference/packages/eden-checkpoint/tests/` | Unit tests for each module. |

### 5.3 eden-storage

| File | Change |
|---|---|
| `protocol.py` | Add `export_checkpoint(stream)` and `import_checkpoint(stream, as_experiment_id=None)` to the Store Protocol. Add `read_experiment(experiment_id) -> Experiment` (extends 12a-3's `read_experiment_state` to return the full Experiment object). |
| `_base.py` | Implement export/import skeleton (the heavy lifting is in `eden_checkpoint`; this is a thin wrapper that calls `eden_checkpoint.write_checkpoint` / `read_checkpoint` after taking the atomic snapshot). On import, set `experiment.imported_from = ImportProvenance(checkpoint_exported_at=manifest.exported_at, checkpoint_format_version=manifest.checkpoint_format_version)` atomically with the experiment row. Implement `read_experiment` to return the full object. |
| `sqlite.py` | Atomicity via `BEGIN IMMEDIATE`. |
| `postgres.py` | Atomicity via `BEGIN ISOLATION LEVEL SERIALIZABLE`. |
| `_memory.py` | Atomicity via `copy.deepcopy` under the existing `RLock`. |
| `tests/test_checkpoint_roundtrip.py` (new) | Round-trip tests parametrized across all three backends. |

### 5.4 eden-contracts

| File | Change |
|---|---|
| `manifest.py` (new) | The `CheckpointManifest` Pydantic model. |
| `worker.py` | The `Worker` and `Group` Pydantic models (12a-1 §D.1 / §D.2) ship as part of 12a-1's contracts package. 12b adds no new fields to either; the export path serializes them with `model_dump(mode="json", exclude={"auth_credential_hash"})` per §2.1's preservation map. |
| `experiment.py` | The `Experiment` model (12a-3) gains an optional `imported_from: ImportProvenance \| None = None` field. New `ImportProvenance` model with `checkpoint_exported_at: datetime` + `checkpoint_format_version: str`. |

### 5.5 eden-wire

| File | Change |
|---|---|
| `server.py` | New endpoints: `POST .../experiments/<E>/checkpoint` (admin auth, streams tar response), `POST /v0/checkpoints/import` (admin auth, accepts tar body), `GET .../experiments/<E>` (admin auth; binds the new `read_experiment` Store op per §2 Decision 5's recovery probe). The import endpoint is the only path under `/v0/checkpoints/`; per the §5.1 chapter 07 §1.3 amendment, this path is not experiment-scoped at the URL level. The handler accepts `X-Eden-Experiment-Id` as OPTIONAL: if absent, the experiment_id comes from the request body's manifest; if present, it MUST match the manifest's `experiment_id` after applying any `as_experiment_id` override (mismatch returns 400 `eden://error/experiment-id-mismatch`). |
| `client.py` | `StoreClient.export_checkpoint(path)` (returns checkpoint metadata), `StoreClient.import_checkpoint(path, as_experiment_id=None)` (returns the new experiment_id as a string), and `StoreClient.read_experiment(experiment_id)` (returns the full Experiment object including `imported_from`; binds the recovery probe). The export and import paths use `httpx`'s streaming I/O for memory-friendly transfer; `read_experiment` is a tiny GET with no streaming concern. |

### 5.6 Reference services

| File | Change |
|---|---|
| `reference/services/web-ui/src/eden_web_ui/routes/admin.py` | New `/admin/checkpoint/export` form with a "Download checkpoint" button. New `/admin/checkpoint/import` form accepting a file upload. Both admin-gated. |

### 5.7 Compose / setup + scripts

| File | Change |
|---|---|
| `reference/scripts/manual-ui/eden-experiment` | Rewrite `checkpoint` and `restore` commands. Remove postgres-dump / gitea-tar code (lines 244-318 + the corresponding `restore` code). New implementation calls the wire endpoint via `curl` (or via a thin Python helper that imports `eden_wire.client`). |
| `reference/compose/healthcheck/smoke-checkpoint.sh` (new) | The cross-backend smoke test from §4.1. |

## 6. Test design

### 6.1 Unit tests (eden-checkpoint package)

- `test_manifest`: Pydantic model round-trip; rejection of unknown
  `checkpoint_format_version`; rejection of malformed
  `spec_version`.
- `test_format_tar`: Tar pack/unpack of a directory; verify file
  list matches manifest's `files` block.
- `test_format_jsonl`: One-object-per-line writer + reader; verify
  ordering preserved.
- `test_artifacts`: Content-addressed dedup (two ideas with the
  same artifact bytes produce one `artifacts/sha256/<hex>` file);
  rewrite of `checkpoint:sha256:<hex>` ↔ `file://` on
  export/import.
- `test_repo_bundle`: Round-trip a small bare repo through `git
  bundle` and back; assert refs are equal.

### 6.2 Storage backend tests

`reference/packages/eden-storage/tests/test_checkpoint_roundtrip.py`
parametrized across SQLite + Postgres + in-memory:

- Empty experiment exports + imports cleanly.
- Experiment with N tasks across all kinds, M ideas, K variants,
  some integrated, some not — exports + imports with full
  equivalence assertion.
- An export taken under concurrent worker activity (a thread
  running `claim` + `submit` while the export thread runs)
  produces a self-consistent checkpoint (every event-referenced
  task_id exists in tasks.jsonl).
- Import into a Store that already has the experiment_id returns
  `AlreadyExists`.
- Import with `as_experiment_id="new"` succeeds and the new
  experiment has all the source's data under the new id.

### 6.3 Wire tests

`reference/packages/eden-wire/tests/test_checkpoint_endpoints.py`:

- `POST /experiments/<E>/checkpoint` as admin → 200, streams a
  parseable tar.
- `POST /experiments/<E>/checkpoint` as non-admin → 403.
- `POST /v0/checkpoints/import` as admin with valid checkpoint →
  201 with `{experiment_id, warnings}` body.
- Schema-version-mismatch checkpoint → 409
  `eden://error/spec-version-mismatch`.
- Experiment-id-conflict → 409
  `eden://error/experiment-id-conflict`.
- Memory bound: a 10MB checkpoint export does not buffer the
  full archive in server memory. The reference impl materializes
  to a temp file before HTTP delivery (per §3.7); the test
  asserts server memory peak during the response phase is
  bounded (not proportional to checkpoint size). Whether the
  HTTP body is delivered with chunked transfer-encoding or
  buffered with `Content-Length` is implementation-defined per
  Decision 8; the test does NOT assert one or the other. Memory
  bound asserted via `tracemalloc`.
- **Recovery probe.** After a successful import, `GET
  /experiments/<E>` returns the Experiment with
  `imported_from.checkpoint_exported_at` matching the source
  manifest's `exported_at`. A natively-created experiment
  (never imported) has `imported_from = null`. (Tests the
  Decision 5 recovery contract.)
- **Lost-201 simulation.** Trigger an import that commits but
  has its 201 response dropped (use a test-only flag to drop
  the response before transmission); the client's follow-up
  `GET /experiments/<E>` returns the imported state with the
  matching `exported_at`. Operator can detect prior success.
- **Chapter 07 §1.3 carve-out: header absence.**
  `POST /v0/checkpoints/import` with NO `X-Eden-Experiment-Id`
  header → 201 (the experiment_id comes from the manifest).
- **Chapter 07 §1.3 carve-out: header matches manifest.**
  `POST /v0/checkpoints/import` with `X-Eden-Experiment-Id:
  <id>` matching the manifest's `experiment_id` → 201.
- **Chapter 07 §1.3 carve-out: header mismatches manifest.**
  `POST /v0/checkpoints/import` with `X-Eden-Experiment-Id:
  <wrong-id>` → 400 `eden://error/experiment-id-mismatch`. With
  `as_experiment_id=<override>`, the header MUST match the
  override (post-rewrite), not the source manifest's id.

### 6.4 Cross-impl interop test

A new e2e test `test_cross_backend_checkpoint.py`:

- Spin up a SQLite-backed task-store-server.
- Drive a fixture experiment to 3 integrated variants + a
  pending ideation task.
- Export the checkpoint to a temp file.
- Stop the SQLite server.
- Spin up a Postgres-backed task-store-server (or vice versa).
- Import the checkpoint.
- Assert the imported state matches the source state on every
  observable axis.
- Resume the experiment: the pending ideation task can be
  claimed; subsequent decisions advance the experiment.

### 6.5 Conformance scenarios

`conformance/scenarios/test_checkpoint_roundtrip.py`:

- **Round-trip equivalence.** Export and re-import on the same
  IUT; assert state equivalence.
- **Cross-impl interop** (only runs when two IUT adapters are
  configured). Export from one, import into the other.
- **Schema-version mismatch.** A v1 checkpoint against a v0 IUT
  is rejected with the right error code.
- **Experiment-id collision** with and without
  `as_experiment_id` override.
- **Atomicity.** A live IUT exports under concurrent
  workers; the result is self-consistent.
- **Worker + group portability.** Workers + groups round-trip;
  `requires_credential_reissue: true` is respected.
- **Terminated experiment.** A checkpoint of a terminated
  experiment imports as terminated; task-creation rejects.

### 6.6 Verification gates

Before merge:

- `uv run ruff check .` clean.
- `uv run pyright` 0 errors.
- `uv run pytest -q` (full suite) passes.
- `uv run pytest -q conformance/` passes (existing scenarios +
  new v1+checkpoints).
- `uv run python conformance/src/conformance/tools/check_citations.py`
  clean.
- `python3 scripts/spec-xref-check.py` clean (chapter-10
  cross-references resolve).
- `python3 scripts/check-rename-discipline.py` clean.
- `bash reference/compose/healthcheck/smoke.sh` passes.
- `bash reference/compose/healthcheck/smoke-checkpoint.sh`
  passes (new).
- Markdownlint clean.
- Manual UI smoke: drive 3 variants, click "Download checkpoint"
  in the admin UI, save the file, bring up a fresh stack, click
  "Import checkpoint", upload the file, confirm the experiment
  appears with all its state.

## 7. Tricky areas

### 7.1 Atomicity of the git bundle vs. the Store snapshot

The Store snapshot (in a transaction) and the git bundle (a
filesystem read of `.git`) are separate operations. If a worker
pushes a new ref between the transaction commit and the bundle
generation, the bundle will contain a ref the Store doesn't know
about.

Resolution: this is OK. Workers cannot push to refs the Store
doesn't track (Store-side `create_variant` runs first per
chapter 03 §3.2 step 1; the work-branch creation follows). A
"surprise" ref in the bundle is from a worker that crashed before
the Store committed; the importer ignores refs unreferenced by
any task/idea/variant/submission. The bundle being a superset of
what the Store needs is fine; the Store being a superset of what
the bundle has is a real bug (would mean a variant references a
ref that doesn't exist), but that case is impossible given the
chapter-03 ordering.

The exporter optionally includes the bundle's `git rev-parse`
result for every ref in the manifest, so importers can spot
inconsistency early.

### 7.2 Materializing the checkpoint artifact before HTTP delivery

Per Decision 8 + §3.7, "export complete" means "the checkpoint
artifact is materialized"; HTTP delivery happens after the
source-state transaction has closed. The reference impl
implements this in two phases:

**Phase 1 (atomic): materialize.** The exporter opens a
serializable transaction over the Store, reads the full
snapshot, generates the git bundle, and writes everything to a
temp file on disk under
`$XDG_CACHE_HOME/eden/exports/<request_id>.tar`. The transaction
commits at the end of phase 1. Memory use during materialization
is bounded by the largest single object the exporter holds at
once (typically a single event row, plus the
streaming-tar-writer's internal buffer); it does NOT grow with
total checkpoint size. Disk use scales with checkpoint size.

**Phase 2 (best-effort streaming): deliver.** The HTTP handler
reads the temp file and streams it to the client. If the client
is slow or disconnects, the temp file lifecycle is independent
of the source-state transaction (which has already closed). The
exporter cleans up the temp file when the response completes
(or fails). Even a 10GB checkpoint with a slow client doesn't
block writes on the source experiment.

Memory bound during phase 2 is constant (one tar-block buffer at
a time, ~64KB). Disk use peaks at the checkpoint size for the
duration of the response.

**Why not direct streaming.** A "stream directly from the
transaction" implementation would hold the snapshot open for
the lifetime of the HTTP response. A 10GB checkpoint over a
slow client could block source-state writes for minutes. The
two-phase materialize-then-stream pattern decouples the two
concerns at the cost of disk space for the temp file.

This is a reference-impl detail; conforming impls MAY use direct
streaming if they're willing to accept the source-state
write-throughput cost. The atomicity rule (§3.7) and the
HTTP-streaming-is-recommended rule (Decision 8) are both
honored either way.

### 7.3 Content-addressed artifacts and existing references

The reference impl's `artifacts_uri` today is `file:///path`
referring to a deployment-local file. To compute the SHA-256 for
the content-addressed scheme, the exporter reads each referenced
file. If any reference points at a missing file, the export
either fails (strict mode) or warns and includes a
"missing artifact" sentinel (lenient mode).

Decision: strict mode by default. A missing artifact reference is
a Store-state inconsistency, not a transient failure. The export
fails with a clear error pointing at the referencing object's
id. Operators fix the underlying issue (typically by reaping
orphaned variants) before re-trying.

### 7.4 Importer worker-credential reissue side channel

§3.6 says the importer mints fresh credentials for each imported
worker. The new credentials need to land somewhere the operator
can read them — the wire response can't carry them in plaintext
(the post-import response is small JSON), and printing them to
the importer's stdout is opaque if the import is initiated from
the Web UI.

Resolution: the reference impl writes the new credentials to a
file at a path returned in the import response's `warnings`
array: `{"warning": "credentials reissued", "path":
"/var/lib/eden/imports/<experiment_id>/credentials.json"}`. The
file is owned by the eden user and readable only by them. The Web
UI's `/admin/checkpoint/import` page surfaces a "credentials
file path" link after a successful import.

### 7.5 Spec-version mismatch detection vs migration

§3 Decision 7: spec-version mismatch is rejected, not migrated.
The naive concern: "what about cross-version round-trip in CI?"
— the test suite would fail spec-version-mismatch on every
run that exports under one v0-minor and imports under another.

Resolution: v0 is one schema family; "spec_version: v0" matches
across all v0 minors. A future v1 will have its own value
("v1"). The detection is at major-version granularity. Within a
major version, schema additions are backward-compatible
(additionalProperties is permissive); subtractions or
incompatible changes bump the major.

If a future minor adds a required field, that's a hard bump (a
v0 checkpoint produced before the field was added would
deserialize without it; a v0 importer expecting the field would
fail). That's a real risk; the spec's stability discipline (no
required-field additions within a major version) is the
protection. Any future minor that adds a required field MUST
either bump major OR include a default value in the manifest's
`spec_version` so older checkpoints get the default.

### 7.6 Removing the native checkpoint without a transition

12b removes the postgres-dump+gitea-tar code in the same chunk
that adds the portable format. An operator running an experiment
mid-upgrade:

1. Old script's `checkpoint` produces native format → operator
   has the bytes but cannot import them with the new script.
2. Operator runs old script's `restore` → fine, restores the
   stack to pre-upgrade state.
3. After upgrade, operator runs new script's `checkpoint` →
   produces portable format.

The procedure is "run the old script's checkpoint before
upgrading; after the upgrade, the old checkpoint is unrestorable
but the running stack is fine." Greenfield: no users with native
checkpoints they need to import into 12b. Documented in the
roadmap delta.

If a user has a native checkpoint they need to import into
post-12b, the recovery path is "run the pre-12b script binary
against a temp deployment to restore, then export from that
temp deployment via the new script, then import into your real
deployment via the new script." Bridging via a temp deployment
is awkward but works.

### 7.7 Web UI download/upload for very large checkpoints

The `/admin/checkpoint/export` form returns a file download. For
a 10GB experiment, the browser's download is the bottleneck —
it needs to buffer the streaming response or write to disk
progressively. Modern browsers handle this fine via
`Content-Disposition: attachment; filename=...`.

The `/admin/checkpoint/import` form is harder: a 10GB upload
through a browser's multipart form-data submission is slow and
unreliable. The reference impl recommends operators use the
script (`eden-experiment restore <path>`) for large checkpoints;
the Web UI form is sized for development-scale experiments
(tens of MB).

This is a UX limitation, not a spec problem. Documented in the
admin page's text.

### 7.8 Multiple concurrent exports

If two operators trigger `POST /experiments/<E>/checkpoint`
simultaneously, both transactions start, both produce a tar, the
client gets two responses. The Store transactions don't conflict
(both are read-only). The two tars are byte-equal (same source
state) modulo timestamp differences in the manifest's
`exported_at`.

This is fine. No need for a lock or a "export-in-progress" guard;
read snapshots are cheap.

If a write transaction (e.g., a `submit`) happens between the
two exports, the second export sees the post-submit state; the
two tars differ. Also fine — both reflect a consistent snapshot
of the source.

### 7.9 Multiple concurrent imports of the same checkpoint

If two operators trigger `POST /v0/checkpoints/import`
simultaneously with the same checkpoint bytes (or two different
checkpoints with the same `experiment_id`), the import endpoint
serializes them via the Store's experiment-creation transaction.
Exactly one wins:

- The first import to commit creates the experiment row,
  populates all referenced objects, and returns 201 with the
  new `experiment_id`.
- The second import sees an experiment with that id already
  exists (per Decision 5's collision rule) and returns
  `409 eden://error/experiment-id-conflict`. The operator can
  retry with `as_experiment_id=<new>`.

If both calls supply the same `as_experiment_id=<new>`, the
same serialization applies — exactly one wins, the other gets
409. There is no "merge" semantics; concurrent imports are
mutually exclusive.

Recovery probe (Decision 5) handles the race-loser-recovery
case: the loser queries `read_experiment(<id>)`, sees
`imported_from.checkpoint_exported_at` matches the local
manifest's `exported_at`, and concludes the import already
succeeded — perhaps via the racing operator. If the timestamps
mismatch, the loser knows a different import won; they retry
with `as_experiment_id=<new>`.

**Test (in `test_checkpoint_endpoints.py`):** two threads call
`POST /v0/checkpoints/import` with the same checkpoint bytes
simultaneously. Assert: exactly one returns 201; the other
returns 409 `eden://error/experiment-id-conflict`. Subsequent
`read_experiment(id)` returns the structurally-imported state
with `imported_from.checkpoint_exported_at` matching.

### 7.10 Malformed checkpoint: JSONL references refs missing from bundle

§7.1 covers the case where the git bundle has refs the JSONL
files don't reference (legitimate; importer ignores them). The
opposite direction — JSONL files reference refs that don't
exist in the bundle — was treated as "impossible per the
exporter's ordering". That assumption breaks when the
checkpoint comes from a non-reference implementation (whose
ordering may differ) OR when the file is corrupted in transit.

**Contract.** The importer MUST validate cross-references
between the JSONL files and the bundle before committing any
state. Validation:

1. Walk all variants in `variants.jsonl`. Each variant's
   `branch` (if set) MUST resolve to a commit in the bundle
   via `git rev-parse refs/heads/<branch>`.
2. Each variant's `commit_sha` (if set) MUST be reachable in
   the bundle.
3. Each variant's `variant_commit_sha` (if set) MUST be
   reachable in the bundle.
4. Each idea's `parent_commits` MUST all be reachable in the
   bundle.

If any check fails, the importer rejects the entire checkpoint
with `400 eden://error/checkpoint-invalid`. The 400 status
reflects that the request body (the uploaded checkpoint) is
malformed, not that the server is in a bad state. NO partial
state is committed; the import either fully validates and
commits, or rejects entirely.

**Test (in `test_checkpoint_endpoints.py`):** craft a
deliberately malformed checkpoint where `variants.jsonl` lists
a `commit_sha` that does NOT exist in the bundle. Assert the
import fails with 400 `eden://error/checkpoint-invalid` and
no experiment is created (the target `experiment_id` is still
absent post-call).

## 8. Risks

1. **The atomic transaction holds for the duration of the
   export, throttling writes.** For a small experiment (~100KB
   tar), this is sub-second; for a 10GB experiment with thousands
   of events, it could be many seconds. Mitigation: the §7.2
   "buffer to temp file then close transaction" pattern. The
   risk is the temp file: disk pressure during a large export
   could fail it. Operators with disk-constrained deployments
   need to monitor.

2. **The git bundle for a long-lived experiment can be huge.**
   `git bundle create --all` includes every object reachable from
   any ref. For an experiment with thousands of `work/*` and
   `variant/*` branches, this is a problem. Mitigation: the
   exporter MAY garbage-collect refs that no longer have a
   referencing variant (e.g., reclaimed work/* branches whose
   variant transitioned to `error`). v1 spec is silent on this;
   operators MAY ship deployment-local GC.

3. **Schema additions in 12a-3 may not be carried through.**
   The plan adds `experiment.json` and depends on workers.jsonl
   / groups.jsonl from 12a-1. If 12a-1 / 12a-3 implementations
   landed a different shape than what this plan expects, the
   format breaks. Mitigation: implementation step 1 is to grep
   for the actual shape and update the plan + format docs in
   lockstep.

4. **`pytest.mark.e2e` cross-backend test is flaky on CI.** The
   test runs two task-store-servers (SQLite and Postgres);
   bringing both up, exporting from one, stopping it, importing
   into the other, and resuming is a long-running test. The
   subprocess-management lessons from `eden#39` (drain stdout to
   files, not PIPEs) apply. Mitigation: follow the
   `_spawn`/`_read_port_announcement`/`_dump_logs` pattern from
   `reference/services/orchestrator/tests/test_e2e.py`.

5. **Re-introducing legacy vocab in the new chapter 10.** The
   strengthened guardrail catches the patterns; this chunk adds
   substantial new spec surface. Mitigation: pre-submit
   guardrail check.

## 9. Sequencing

Recommended PR shape (in order):

1. **Spec PR.** Chapter 10 (new), chapter 02 / 07 / 08 / 09
   amendments, plus the manifest schema. No code.

2. **eden-checkpoint package PR.** New package with format
   reader/writer + manifest model + tests. Standalone.

3. **eden-storage Protocol + backend PR.** Store Protocol gains
   `export_checkpoint` / `import_checkpoint`; backend
   implementations land; per-backend tests.

4. **eden-wire endpoints PR.** Server endpoints + client methods +
   wire tests.

5. **Web UI PR.** Admin export/import forms.

6. **Native-format removal PR.** Rewrite `eden-experiment
   checkpoint` / `restore` to use the new format; remove the
   postgres-dump and gitea-tar code paths.

7. **Conformance PR.** New scenarios under
   `test_checkpoint_*.py`.

8. **Compose smoke PR.** New cross-backend smoke.

9. **Docs PR.** Glossary; roadmap delta; AGENTS.md "Current phase".

A reviewer going from PR 1 to PR 9 should expect tests to go red
around PR 6 (when the native format is removed) and come back
green at PR 8.

## 10. Estimated effort

- **Spec prose** (PR 1): ~2 days. Chapter 10 is ~200 normative
  lines + cross-reference work in 4 other chapters. Manifest
  schema is small but needs careful field-by-field semantics.
- **eden-checkpoint package** (PR 2): ~2 days. Tar/JSONL/git-bundle
  handling + content-addressed dedup + tests.
- **eden-storage Protocol + backends** (PR 3): ~2 days. Three
  backends, per-backend atomicity, the temp-file streaming
  pattern from §7.2.
- **eden-wire** (PR 4): ~1 day. Streaming endpoints + auth +
  client streaming I/O.
- **Web UI** (PR 5): ~1 day. The two forms + the credentials-file
  surfacing.
- **Native-format removal + script rewrite** (PR 6): ~1 day.
- **Conformance** (PR 7): ~1.5 days. Cross-impl interop is the
  big-ticket item.
- **Compose smoke** (PR 8): ~0.5 day.
- **Docs** (PR 9): ~0.5 day.

**Realistic total: ~11.5 working days** of focused work. The
heaviest chunk so far — three new normative chapters worth of
work spread across spec and impl, plus the cross-impl interop
test that requires running two backends in CI.

## 11. Followups (out of scope)

- **Selective export.** "Just the variants" / "everything except
  events older than X" — v2.
- **Built-in encryption.** Format itself stays unencrypted; the
  spec carries an informative note suggesting transport-layer
  encryption.
- **Cross-version migration tool.** A v0→v1 migration is a
  separate effort; v0 importers reject v1 checkpoints with a
  clear error.
- **Streaming-only protocol.** The chunked-transfer-encoding
  recommendation is a SHOULD, not a MUST. A future amendment
  may upgrade it to MUST for large checkpoints.
- **GC of unreferenced git refs.** Deployment-local concern in
  v0; spec silent.
- **Signed checkpoints.** No PKI assumption in v0.

## 12. What lands at the end of 12b

After 12b merges, an experiment can be exported from any
conforming Store and imported into any other (modulo
spec-version mismatches, which are rejected explicitly). This is
the foundation for the Phase 12c "control plane" work, which
will need to migrate experiments between Store backends as
deployments scale.
