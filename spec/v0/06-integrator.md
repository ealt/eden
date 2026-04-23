# Integrator

This chapter specifies the integrator role in full: the git topology
it operates on, the promotion trigger, the squash rule, the
eval-manifest shape, and the invariants it preserves across the
canonical trial lineage.

The integrator is introduced in [`03-roles.md`](03-roles.md) §5,
which pins its boundary rules — exclusive authority over `trial/*`,
exclusive authority over `trial_commit_sha`. This chapter specifies
what the integrator *produces* inside those boundaries.

The three-namespace topology (`main`, `trial/*`, `work/*`) is defined
in [`01-concepts.md`](01-concepts.md) §9. This chapter pins the
invariants on each namespace and the transformation from worker
branch to canonical trial commit.

## 1. Topology invariants

A conforming EDEN deployment operates on a single git repository per
experiment. That repository carries three ref namespaces:

### 1.1 `main`

- Names the experiment's starting commit.
- MUST be present at experiment registration time and MUST name a
  commit reachable by every proposal's `parent_commits`
  ([`02-data-model.md`](02-data-model.md) §5.2).
- Is **immutable** for the duration of the experiment. A conforming
  integrator MUST NOT move, rewrite, or delete `main`; neither MAY
  any worker. External tooling (e.g. repo maintainers merging work
  into `main` outside the experiment) is the operator's concern and
  lies outside the protocol. An experiment MAY tolerate such
  external moves only if it does not depend on `main` as a
  persistent parent; conforming implementations SHOULD record the
  experiment's starting commit SHA explicitly and use that SHA, not
  the current `main`, when validating parent reachability.

### 1.2 `trial/*`

- Holds the canonical trial lineage, one commit per integrated trial.
- The integrator is the **sole writer** ([`03-roles.md`](03-roles.md)
  §5.1). No other role — planner, implementer, evaluator,
  orchestrator, operator tooling invoked through protocol-owned
  channels — MAY write to `trial/*`.
- Each `trial/*` branch MUST name exactly one trial. Branch naming
  conforms to §3.1.
- A `trial/*` branch MUST NOT be deleted or rewritten once written.
  If an integrator discovers a bug in an earlier promotion, it MAY
  record corrective state in subsequent events or operator-level
  channels, but MUST NOT rewrite history on `trial/*`.

### 1.3 `work/*`

- Holds per-trial worker branches written by implementers
  ([`03-roles.md`](03-roles.md) §3.3).
- Each `work/*` branch MUST be unique to a single trial. A trial's
  `branch` field ([`02-data-model.md`](02-data-model.md) §7.1)
  records the exact branch name.
- Worker branches are **inputs** to the integrator, not normative
  outputs. A conforming deployment MAY retain them for audit and
  debugging after promotion, MAY garbage-collect them after a
  retention window, and MAY delete them eagerly once a trial is
  integrated — the retention policy is a deployment concern. No
  protocol-owned reader MAY depend on a worker branch after the
  trial's promotion.
- The evaluator reads a worker branch at the trial's `commit_sha`
  during evaluation ([`03-roles.md`](03-roles.md) §4.1); the
  implementer's read-your-writes guarantee applies only until
  promotion, after which the worker branch's reachability is no
  longer a protocol-owned concern.

### 1.4 Reachability rule

Every commit on a `work/*` branch MUST descend from the proposal's
declared `parent_commits` ([`03-roles.md`](03-roles.md) §3.3). The
integrator MUST reject a trial whose `commit_sha` does not satisfy
this rule; rejection produces no `trial/*` commit and no
`trial.integrated` event. The orchestrator MAY transition the trial
to `error` via the normal channels if a reachability violation is
discovered, but the integrator MUST NOT fabricate a `trial.errored`
event itself — events are paired with state changes, and the
integrator is not the writer for trial `status`.

## 2. Promotion trigger

The integrator promotes a trial iff:

- The trial's `status` is `success`
  ([`02-data-model.md`](02-data-model.md) §7.1).
- The trial's `commit_sha` is set and resolves to a commit on the
  trial's `branch` in the experiment repository.
- The trial has not yet been promoted (`trial_commit_sha` is absent).

The mechanism by which the integrator observes the trigger —
subscribing to `trial.succeeded` events, polling the trial store, or
receiving a dispatch call — is a binding concern and is not pinned.
[`03-roles.md`](03-roles.md) §5.2.

A conforming integrator MUST NOT promote trials in any other status.
In particular, `error`, `eval_error`, and `starting` trials MUST NOT
receive a `trial/*` commit.

The integrator MUST NOT promote a trial whose `metrics` do not
validate against the experiment's `metrics_schema`
([`02-data-model.md`](02-data-model.md) §7.2,
[`08-storage.md`](08-storage.md) §4). The orchestrator's acceptance
of a `success` submission is the primary guard for this; the
integrator MAY additionally re-validate as defense in depth but MUST
NOT silently drop or coerce invalid metrics.

## 3. Promotion output

### 3.1 `trial/*` branch name

The integrator creates a branch under `trial/` named:

```text
trial/<trial_id>-<slug>
```

where `<trial_id>` is the trial's `trial_id`
([`02-data-model.md`](02-data-model.md) §7.1) and `<slug>` is the
`slug` of the trial's parent proposal
([`02-data-model.md`](02-data-model.md) §5.1). The slug is already
constrained to `^[a-z0-9][a-z0-9-]*$`, and `trial_id` is opaque; the
resulting branch name MUST be valid under git's ref-format rules
([git check-ref-format]). If an implementation's `trial_id` choice
could violate ref-format rules, the implementation MUST choose a
different `trial_id` representation — it MUST NOT mutate the stored
`trial_id` in the trial object to accommodate its branch-naming
scheme.

[git check-ref-format]: https://git-scm.com/docs/git-check-ref-format

### 3.2 Squash rule

The trial's worker branch MAY contain multiple commits (implementer
step-by-step work, partial progress, iterated attempts). The
canonical trial commit MUST be a **single commit** whose tree is the
worker branch's tip tree plus exactly the eval-manifest file.

Concretely: the integrator MUST produce a commit `T` with:

- `tree(T)` equal to `tree(commit_sha)` with exactly one path added:
  the eval-manifest file at the path given in §4.1. The added file's
  bytes MUST be the manifest defined by §4.2. `commit_sha` is the
  trial's recorded worker-branch tip. No other path — not a file,
  not a directory entry — MAY be added, removed, or modified in the
  squash.
- `parents(T) == <parent_commits>` of the trial, in the order
  recorded on the trial
  ([`02-data-model.md`](02-data-model.md) §7.1 inherits from the
  proposal, §5.1).
- A commit message whose first line (subject) records the trial
  identity in a machine-parseable form. §3.3 pins the format.

The implementer is not required to carry the eval manifest in its
worker branch; the manifest is exclusively the integrator's write.
If a worker branch incidentally contains a file at the eval-manifest
path (e.g. an implementer mistake), the integrator MUST reject the
trial rather than silently overwrite the file. Rejection produces
no `trial/*` commit and no `trial.integrated` event; the orchestrator
MAY transition the trial via normal channels.

The squash is a single merge-commit-shaped object (parents may be
one or many, per the proposal's `parent_commits`); intermediate
work-branch commits MUST NOT appear in `T`'s ancestor chain along a
first-parent path when read from `trial/*`. Readers of the
canonical lineage see one commit per trial, not the worker's
intermediate history.

A conforming implementation MAY additionally preserve the worker
branch's history elsewhere (retained `work/*` ref, archive) for
audit, but that preservation MUST NOT make intermediate commits
reachable from `trial/*`.

### 3.3 Commit message

The `trial/*` commit message's first line (subject) MUST match:

```text
trial: <trial_id> <slug>
```

The subject MUST carry both the `trial_id` and the parent proposal's
`slug`, separated by a single space. The body MAY include additional
human-readable context. The machine-parseable header allows a
subscriber reading git alone — without the event log — to recover the
`trial_id` of any `trial/*` commit.

### 3.4 Atomic write

The promotion MUST be atomic with two other state changes:

1. Writing the `trial_commit_sha` field on the trial to the SHA of
   the new `trial/*` commit
   ([`02-data-model.md`](02-data-model.md) §7.1).
2. Appending a `trial.integrated` event to the event log
   ([`05-event-protocol.md`](05-event-protocol.md) §3.3).

If any of the three steps fails — git ref write, trial-object
field write, event append — the integrator MUST roll back any
already-performed step. In particular, a dangling `trial/*` ref with
no `trial_commit_sha` field and no `trial.integrated` event is a
protocol violation. Implementations MAY use store-level
transactions, outbox patterns, compensating deletes, or any other
mechanism; the observable invariant is that a reader of any one of
the three artifacts (ref, field, event) MUST observe the other two.

## 4. Eval manifest

Each `trial/*` commit MUST carry an **eval manifest** at a fixed path
inside its tree. The manifest is the canonical, git-embedded record
of the trial's evaluator outputs: what was measured, which worker
commit was measured, where the supporting artifacts live.

### 4.1 Path

The eval manifest is a JSON file at:

```text
.eden/trials/<trial_id>/eval.json
```

relative to the root of the committed tree. The `<trial_id>`
component matches the trial's `trial_id`. An integrator MUST NOT add
any file under `.eden/trials/<other_trial_id>/` to a trial's commit;
each `trial/*` commit carries exactly one eval-manifest path,
corresponding to its own trial.

### 4.2 Shape

The manifest is a JSON object with the following required fields.
Each required field is taken from state the protocol already writes
via the role contracts in [`03-roles.md`](03-roles.md) — no role
submission is extended by this chapter.

| Field | Type | Source |
|---|---|---|
| `trial_id` | string | The trial's `trial_id` ([`02-data-model.md`](02-data-model.md) §7.1). |
| `proposal_id` | string | The trial's `proposal_id` ([`02-data-model.md`](02-data-model.md) §7.1). |
| `commit_sha` | string | The worker-branch tip the evaluator measured ([`02-data-model.md`](02-data-model.md) §7.1). |
| `parent_commits` | array of string | The trial's `parent_commits`, in order ([`02-data-model.md`](02-data-model.md) §7.1). |
| `metrics` | object | The evaluator's metrics payload ([`03-roles.md`](03-roles.md) §4.4), conforming to the experiment's `metrics_schema`. |
| `completed_at` | timestamp | The trial's `completed_at` ([`02-data-model.md`](02-data-model.md) §7.1). UTC, RFC 3339 profile as elsewhere in the data model. |

Optional fields:

| Field | Type | Source |
|---|---|---|
| `artifacts_uri` | string (URI) | The trial's `artifacts_uri` if the evaluator supplied one ([`03-roles.md`](03-roles.md) §4.4). |
| `description` | string | The trial's `description`, copied from the trial object if present. |
| `evaluator` | object | Implementation-defined identity of the evaluator (§4.3). Present when the evaluator's role-binding surfaces this metadata. |
| `artifacts` | object | Implementation-defined inventory of per-file artifact metadata (§4.4). Present when the role-binding surfaces it. |

Every required field's value MUST equal the corresponding field on
the trial object at the moment of promotion. A conforming integrator
MUST NOT synthesize or transform required-field values; they are a
canonical, in-tree snapshot of the trial state, not a separate
derivation.

Implementations MAY include additional informational fields beyond
those listed; consumers MUST tolerate them.

### 4.3 `evaluator` shape (optional)

When present, the `evaluator` object has the form:

```json
{
  "name": "<implementation-chosen identifier for the evaluator>",
  "version": "<version string the evaluator presented at submission>"
}
```

Both fields are required when the `evaluator` object itself is
present. `name` and `version` together identify *which* evaluator
measured the trial. Because v0 does not surface evaluator identity
through [`03-roles.md`](03-roles.md) §4.4, this field is optional:
populate it when a role-binding or deployment convention carries
evaluator metadata; omit it otherwise.

### 4.4 `artifacts` shape (optional)

When present, `artifacts` is a JSON object whose values describe
per-file metadata for individual artifacts referenced from the trial
(commonly the files under the evaluator's `artifacts_uri`). Each
entry has the form:

```json
{
  "uri": "<URI of the specific file>",
  "sha256": "<hex sha256 of the file's byte content>",
  "bytes": <non-negative integer>
}
```

Keys are operator-chosen short labels (e.g. `"log"`, `"output.csv"`)
and MUST be unique within the manifest. This inventory is **optional
in v0** because [`03-roles.md`](03-roles.md) §4.4 does not require
an evaluator to enumerate per-file hashes; a deployment whose role-
binding surfaces that inventory MAY populate this field, and
downstream consumers SHOULD prefer the inventory (when present) to
blind re-fetches of `artifacts_uri` for reproducibility checks.

### 4.5 Manifest immutability

Because the eval manifest lives inside the `trial/*` commit's tree,
it inherits the immutability of the `trial/*` branch (§1.2): once
written, it MUST NOT be rewritten. A conforming implementation that
discovers a manifest error after the fact MAY record corrective
state outside git (operator-level event, separate annotation store)
but MUST NOT mutate the `trial/*` commit.

## 5. Failure modes

### 5.1 Metrics-schema violation at promotion time

If the integrator re-validates metrics (per §2) and finds them in
violation of the experiment's `metrics_schema`, the integrator MUST
NOT produce a `trial/*` commit, MUST NOT write `trial_commit_sha`,
and MUST NOT append `trial.integrated`. It MUST surface the problem
through an implementation-defined operator channel; since the trial's
`status` was written to `success` by the orchestrator at the evaluate
terminal, the promotion-side rejection is a protocol-level drift
between the two that an operator MUST resolve. A conforming
orchestrator makes this case rare by enforcing schema conformance at
the evaluate terminal.

### 5.2 Partial git write

If the underlying git ref write appears to succeed but later fails
durability (crashed filesystem, partial replication), the
integrator's atomicity contract (§3.4) is violated. Implementations
MUST ensure ref durability before considering a promotion complete;
conforming storage requirements are pinned in
[`08-storage.md`](08-storage.md) §5 (artifact store) and §3
(durability semantics applied at the git layer by the integrator).

### 5.3 Repeat promotion

If the integrator is invoked twice for the same trial (replay of a
`trial.succeeded` event, operator retry), it MUST be idempotent:

- If the trial already has `trial_commit_sha` set and that SHA
  resolves to a `trial/*` commit whose tree satisfies §3.2 (the
  worker-tip tree with exactly the eval-manifest path added, no other
  path changes) and whose eval manifest matches §4, the second
  invocation MUST be a no-op (no new ref, no new event).
- If the trial already has `trial_commit_sha` set but the recorded
  SHA does not resolve or disagrees with the trial, the state is
  corrupt; the integrator MUST NOT silently overwrite. An operator
  channel per §5.1 applies.

## 6. Implementation latitude

The protocol leaves to implementations:

- The git host (local bare repo, Gitea, GitHub, etc.) and the
  transport used to write `trial/*` refs.
- The retention policy for `work/*` branches after promotion.
- The exact author/committer identity stamped on the integrator's
  commits.
- The mechanism of atomicity in §3.4 — outbox, store-level
  transaction, compensating writes.

What the protocol does **not** leave to implementations:

- The three-namespace topology and sole-integrator rule (§1).
- The single-commit squash shape and the tree rule in §3.2
  (worker-tip tree plus exactly the eval-manifest path, no other
  path changes).
- The commit-message format (§3.3).
- The eval-manifest path and required fields (§4).
- The atomic-three invariant (§3.4).
