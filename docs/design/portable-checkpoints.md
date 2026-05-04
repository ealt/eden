# Portable checkpoints — design discussion

**Status:** design exploration; informs a future spec chapter.
**Origin:** prompted by manual-UI session 2026-05-04 (@ericalt).
**Relation to current implementation:** the reference impl ships an
``eden-experiment checkpoint`` / ``restore`` pair that uses
postgres-dump + gitea-volume-tar. That format is *not* portable across
implementations. This doc proposes the portable format that should
replace it for spec-conforming export/import.

## Premise

> A user should be able to start an experiment on implementation A,
> save a checkpoint, hand the file to a user running implementation B,
> and have B recover the *same experiment state* — without either side
> agreeing on what database, git host, or artifact store the other is
> using.

This is the same posture EDEN already takes for the protocol surface
itself: chapter 7's HTTP wire is normative; chapter 8's storage is a
``Store`` interface with multiple conforming backends. Checkpoints
should follow the same pattern — normative content + format,
implementation-free.

## Goals

1. Capture the **logical state** of an experiment in a form independent
   of the receiving implementation's storage backend, git host, or
   artifact store.
2. Be self-describing: a consumer can verify what's in the checkpoint
   without out-of-band knowledge.
3. Be versioned: the format itself evolves; consumers reject
   unknown-version checkpoints rather than mis-parsing them.
4. Be *content-addressed* where appropriate (artifacts) so duplication
   is detectable and tamper-evident.
5. Be transportable as a single file (tarball / zip) or as a directory
   tree.

## Non-goals

- **Secret portability.** Bearer tokens, postgres passwords, gitea
  credentials, session secrets — all deployment-specific. Not in the
  checkpoint. The receiving deployment supplies its own.
- **Worker identity carry-over** (until the worker-id spec lands per
  `orchestrator-and-worker-roles.md`). Today, ``worker_id`` is an
  ad-hoc string with no spec-level identity; it round-trips fine as a
  string but doesn't transfer across deployments meaningfully. After
  the worker-id chapter lands, this needs revisiting (see open
  question 4).
- **Live-process state.** In-memory session cookies, claim caches not
  persisted to the store, etc. — these are not part of experiment
  state.

## Logical contents

A checkpoint represents a snapshot of the **state observable at some
point in time** (atomicity §). The state is exactly what the
chapter-8 ``Store`` interface holds, plus the underlying git repo and
artifact bytes. By chapter:

| Source | Contents |
|---|---|
| 02 (data model) | The experiment record (id, config) |
| 04 (task protocol) | All tasks, every state |
| 05 (event protocol) | The full event log |
| 06 (integrator) | All variants, including ``variant_commit_sha`` for promoted ones |
| chapter 02 §1 (ideas) | All ideas |
| 02 §2.4 (submissions) | All submissions, both ``read_submission``-able and historical |
| git repo | All refs + reachable objects (seed, ``work/*``, ``variant/*``, anything else) |
| artifacts | Bytes for every ``artifacts_uri`` referenced by ideas/variants |

Importantly: **events are first-class**, not derivable. The event log
is the audit trail; it's not reconstructible from the current task
state. Replay-ability across the round-trip is a hard requirement.

## Format

A checkpoint is a directory tree. Tarball / zip wrapping is a
transport convenience; the *logical* format is the directory.

```
<checkpoint>/
  manifest.json             # required
  experiment-config.yaml    # the experiment config (verbatim)
  tasks.jsonl               # one JSON object per line, schema = task.schema.json
  ideas.jsonl           # schema = idea.schema.json
  variants.jsonl              # schema = variant.schema.json
  submissions.jsonl         # schema = submission shape per role (see 04 §4.2)
  events.jsonl              # schema = event.schema.json, in append order
  repo.bundle               # `git bundle create --all` of the experiment's bare repo
  artifacts/
    sha256/<hex>            # one file per unique artifact, named by its content hash
```

### manifest.json

Required top-level. Self-describes the checkpoint and its components.

```json
{
  "checkpoint_format_version": "1",
  "spec_version": "v0",
  "experiment_id": "genproc-1",
  "created_at": "2026-05-04T17:03:31Z",
  "counts": {
    "tasks": 3,
    "ideas": 0,
    "variants": 0,
    "submissions": 0,
    "events": 4
  },
  "files": {
    "experiment_config": "experiment-config.yaml",
    "tasks": "tasks.jsonl",
    "ideas": "ideas.jsonl",
    "variants": "variants.jsonl",
    "submissions": "submissions.jsonl",
    "events": "events.jsonl",
    "repo_bundle": "repo.bundle",
    "artifacts_dir": "artifacts/sha256"
  }
}
```

- ``checkpoint_format_version`` is the format version of *this doc's
  format*. Bumped on incompatible format changes. Consumers MUST
  reject unknown versions.
- ``spec_version`` is the EDEN spec version the contained data
  conforms to. The two version numbers are independent; format v1
  could carry data conforming to spec v0 or v1.
- ``counts`` is informational, useful for quick verification.

### Artifact addressing

The reference implementation today uses ``file://`` URIs that point
into a deployment-local artifacts directory. That URI is meaningless
in another deployment.

The checkpoint format normalizes artifact references to a
**content-addressed** scheme: every unique artifact is stored as
``artifacts/sha256/<hex>``, where ``<hex>`` is the SHA-256 of the
artifact's bytes. The idea's / variant's ``artifacts_uri`` field in
the dumped JSONL is rewritten to ``checkpoint:sha256:<hex>``.

On import, the receiving implementation:
1. Materializes each ``artifacts/sha256/<hex>`` into its own artifact
   store.
2. Rewrites ``checkpoint:sha256:<hex>`` references back to whatever
   URI scheme its store uses (``file://`` for the reference impl,
   ``s3://`` for a Phase-13 deployment, etc.).

This makes artifacts deployment-portable and deduplicates within a
checkpoint.

### Git repo

The repo is exported with ``git bundle create <path> --all``, which
captures every ref and every object reachable from any ref in a single
file. ``--all`` is load-bearing: ``variant/*``, ``work/*``, ``main``, and
anything else live alongside.

A bundle is universally importable with ``git fetch <bundle>`` or
``git clone <bundle>`` from any git client. No git-host-specific
metadata is involved (gitea repos, GitHub repos, plain bare repos —
all interchangeable).

## Round-trip semantics: what counts as "same state"

The checkpoint preserves **state equivalence**, not byte equivalence.
Specifically, after import:

- The set of tasks MUST be identical: same ids, same kinds, same
  states, same payloads, same claims (including tokens), same
  timestamps.
- The set of ideas/variants/submissions MUST be identical to their
  schema-validated forms — except that ``artifacts_uri`` MAY differ
  (because URIs are rewritten on import to point at the new
  deployment's artifact store). The artifact *content* MUST be
  identical; that's the equivalence anchor.
- The event log MUST replay in the same order with the same
  per-event payload. Event IDs MAY differ if the importer's event-id
  factory differs ([`spec/v0/05-event-protocol.md`](../../spec/v0/05-event-protocol.md)
  treats event IDs as opaque per chapter 8 §3).
- The git repo MUST contain the same objects reachable from the same
  refs. SHAs are content-addressed so this is automatic: if all
  reachable objects are present, the SHAs match.

A conforming implementation's ``import`` MUST produce a state from
which the experiment can resume — workers can claim, the orchestrator
can dispatch, the integrator can promote. ``export`` immediately
followed by ``import`` is a no-op modulo URI rewrites and event-id
reassignment.

## Atomicity

The exporter MUST snapshot the source state atomically with respect to
ongoing operations. Two acceptable strategies:

1. **Quiesce and dump** — pause the orchestrator and any workers,
   take the snapshot, resume. Simple; minor downtime. The reference
   impl's current implementation does this implicitly (gitea is
   stopped briefly during volume tar).
2. **Transactional snapshot** — take a serializable transaction across
   the store, then read the git repo state at the same logical point
   in time. More complex; no downtime.

Either is conformant. The spec should *require* atomicity but not
prescribe the mechanism.

## New wire endpoints

Two operations are added to chapter 7:

- ``POST /v0/experiments/<id>/checkpoint`` — produce a checkpoint of
  the named experiment. Response: a checkpoint stream / archive.
  Request body: optional ``{name?, format_version?}``.
- ``POST /v0/checkpoints/import`` — receive a checkpoint and create the
  experiment on this deployment. Returns the new experiment id (which
  MAY differ from the source's, if the deployment requires unique
  ids; see open question 1).

Both endpoints are new normative surface. Implementations MAY also
support file-based export/import as a deployment convenience, but the
wire ops are required for over-the-network conformance.

## Spec changes (sketch)

| Chapter | Change |
|---|---|
| New chapter | "Checkpoint format" — defines the layout, manifest, addressing, atomicity, round-trip semantics |
| 02 (data model) | Reference the new chapter from the description of artifact URIs (note that they're store-local) |
| 07 (wire) | Add `checkpoint` and `import` operations |
| 09 (conformance) | New conformance scenarios: export+import round-trip; cross-impl interop (export from impl A, import to impl B, run an experiment iteration) |

## Open questions

1. **Experiment-id collision on import.** What if the importing
   deployment already has an experiment with the same id? Reject?
   Auto-rename? Operator-supplied override? Default policy needs to be
   defined; any of these is conformant if specified.

2. **Selective export.** Does ``export`` support partial / filtered
   captures? E.g., "everything except event log older than X", or
   "just the variants and their evaluators". Probably v2; v1 is
   full-state.

3. **Encryption.** Checkpoints contain potentially sensitive
   experiment data (idea text, evaluation rationales). The format
   itself doesn't encrypt; that's a transport concern. Should the spec
   say anything? Probably an informative note.

4. **Worker identity round-trip** (depends on
   `orchestrator-and-worker-roles.md`). Once worker_ids and groups are
   first-class, the checkpoint must carry them. Need to decide whether
   worker identity is portable across deployments (probably not — a
   deployment's worker registry is its own concern) or per-checkpoint
   ephemeral (probably yes, with import-time mapping).

5. **Schema-version skew.** A checkpoint produced under spec v0 needs
   to be importable into a v1 implementation if v1 is
   backward-compatible. The format ought to mark which schema
   versions each component conforms to; a v1 importer that finds a
   v0 checkpoint runs its v0→v1 migration. Spec needs to commit to
   migration discipline.

6. **Streaming vs. archive.** Large experiments could produce
   gigabyte-scale checkpoints (event log alone). The wire endpoint
   should probably be streaming-friendly rather than buffer-the-world.
   Spec implication: define a chunked/streamed encoding alongside the
   directory format.

## Implementation path

Decoupled work:

- **Phase A (spec).** Draft the new chapter; iterate on the format
  manifest; add conformance assertions.
- **Phase B (reference impl).** New ``Store.export_checkpoint`` and
  ``Store.import_checkpoint`` Protocol methods. Reference adapter
  implements both. ``eden-experiment`` script gains
  ``checkpoint --portable`` / ``restore --portable`` flags that use
  the new format alongside the existing native checkpoint.
- **Phase C (deprecate native).** After a transition period, retire
  the postgres-dump-based native checkpoint; portable becomes the
  default and only.

A second implementation that consumes portable checkpoints is the
ground-truth interop test. Without it, the "portable" claim is
asserted but not validated.
