# Integrator

This chapter specifies the integrator role in full: the git topology it operates on, the integration trigger, the squash rule, the evaluation-manifest shape, and the invariants it preserves across the canonical variant lineage.

The integrator is introduced in [`03-roles.md`](03-roles.md) §5, which pins its boundary rules — exclusive authority over `variant/*`, exclusive authority over `variant_commit_sha`. This chapter specifies what the integrator *produces* inside those boundaries.

The three-namespace topology (`main`, `variant/*`, `work/*`) is defined in [`01-concepts.md`](01-concepts.md) §9. This chapter pins the invariants on each namespace and the transformation from worker branch to canonical variant commit.

## 1. Topology invariants

A conforming EDEN deployment operates on a single git repository per experiment. That repository carries three ref namespaces:

### 1.1 `main`

- Names the experiment's starting commit.
- MUST be present at experiment registration time and MUST name a commit reachable by every idea's `parent_commits` ([`02-data-model.md`](02-data-model.md) §5.2).
- Is **immutable** for the duration of the experiment. A conforming integrator MUST NOT move, rewrite, or delete `main`; neither MAY any worker. External tooling (e.g. repo maintainers merging work into `main` outside the experiment) is the operator's concern and lies outside the protocol. An experiment MAY tolerate such external moves only if it does not depend on `main` as a persistent parent; conforming implementations SHOULD record the experiment's starting commit SHA explicitly and use that SHA, not the current `main`, when validating parent reachability.

### 1.2 `variant/*`

- Holds the canonical variant lineage, one commit per integrated variant.
- The integrator is the **sole writer** ([`03-roles.md`](03-roles.md) §5.1). No other role — ideator, executor, evaluator, orchestrator, operator tooling invoked through protocol-owned channels — MAY write to `variant/*`.
- Each `variant/*` branch MUST name exactly one variant. Branch naming conforms to §3.1.
- A `variant/*` branch MUST NOT be deleted or rewritten once written. If an integrator discovers a bug in an earlier integration, it MAY record corrective state in subsequent events or operator-level channels, but MUST NOT rewrite history on `variant/*`.

### 1.3 `work/*`

- Holds per-variant worker branches written by executors ([`03-roles.md`](03-roles.md) §3.3).
- Each `work/*` branch MUST be unique to a single variant. A variant's `branch` field ([`02-data-model.md`](02-data-model.md) §9.1) records the exact branch name.
- Worker branches are **inputs** to the integrator, not normative outputs. A conforming deployment MAY retain them for audit and debugging after integration, MAY garbage-collect them after a retention window, and MAY delete them eagerly once a variant is integrated — the retention policy is a deployment concern. No protocol-owned reader MAY depend on a worker branch after the variant's integration.
- The evaluator reads a worker branch at the variant's `commit_sha` during evaluation ([`03-roles.md`](03-roles.md) §4.1); the executor's read-your-writes guarantee applies only until integration, after which the worker branch's reachability is no longer a protocol-owned concern.

### 1.4 Reachability rule

Every commit on a `work/*` branch MUST descend from the idea's declared `parent_commits` ([`03-roles.md`](03-roles.md) §3.3). The integrator MUST reject a variant whose `commit_sha` does not satisfy this rule; rejection produces no `variant/*` commit and no `variant.integrated` event. The orchestrator MAY transition the variant to `error` via the normal channels if a reachability violation is discovered, but the integrator MUST NOT fabricate a `variant.errored` event itself — events are paired with state changes, and the integrator is not the writer for variant `status`.

## 2. Integration trigger

The integrator integrates a variant iff:

- The variant's `status` is `success` ([`02-data-model.md`](02-data-model.md) §9.1).
- The variant's `commit_sha` is set and resolves to a commit on the variant's `branch` in the experiment repository.
- The variant has not yet been integrated (`variant_commit_sha` is absent).

The mechanism by which the integrator observes the trigger — subscribing to `variant.succeeded` events, polling the variant store, or receiving a dispatch call — is a binding concern and is not pinned. [`03-roles.md`](03-roles.md) §5.2.

A conforming integrator MUST NOT integrate variants in any other status. In particular, `error`, `evaluation_error`, and `starting` variants MUST NOT receive a `variant/*` commit.

A conforming integrator MUST NOT integrate a `kind == "baseline"` variant ([`02-data-model.md`](02-data-model.md) §9.4), regardless of its `status`. A baseline has no `work/*` branch to squash and already points at the seed on `main`, so it receives no `variant/*` commit, no `variant_commit_sha`, and no `variant.integrated` event. This carve is paired with the `integration` decision predicate and the termination-drain rule ([`02-data-model.md`](02-data-model.md) §2.4, §2.5), both of which exclude baselines so a successful baseline does not block termination. A binding MAY additionally reject a manual/operator `integrate_variant` call against a baseline with `eden://error/invalid-precondition` ([`07-wire-protocol.md`](07-wire-protocol.md) §5) as defense in depth.

The integrator MUST NOT integrate a variant whose `evaluation` does not validate against the experiment's `evaluation_schema` ([`02-data-model.md`](02-data-model.md) §9.2, [`08-storage.md`](08-storage.md) §4). The orchestrator's acceptance of a `success` submission is the primary guard for this; the integrator MAY additionally re-validate as defense in depth but MUST NOT silently drop or coerce an invalid evaluation payload.

## 3. Integration output

### 3.1 `variant/*` branch name

The integrator creates a branch under `variant/` named:

```text
variant/<variant_id>-<slug>
```

where `<variant_id>` is the variant's `variant_id` ([`02-data-model.md`](02-data-model.md) §9.1) and `<slug>` is the `slug` of the variant's parent idea ([`02-data-model.md`](02-data-model.md) §5.1). The slug is already constrained to `^[a-z0-9][a-z0-9-]*$`, and `variant_id` is opaque; the resulting branch name MUST be valid under git's ref-format rules ([git check-ref-format]). If an implementation's `variant_id` choice could violate ref-format rules, the implementation MUST choose a different `variant_id` representation — it MUST NOT mutate the stored `variant_id` in the variant object to accommodate its branch-naming scheme.

[git check-ref-format]: https://git-scm.com/docs/git-check-ref-format

### 3.2 Squash rule

The variant's worker branch MAY contain multiple commits (executor step-by-step work, partial progress, iterated attempts). The canonical variant commit MUST be a **single commit** whose tree is the worker branch's tip tree plus exactly the evaluation-manifest file.

Concretely: the integrator MUST produce a commit `T` with:

- `tree(T)` equal to `tree(commit_sha)` with exactly one path added: the evaluation-manifest file at the path given in §4.1. The added file's bytes MUST be the manifest defined by §4.2. `commit_sha` is the variant's recorded worker-branch tip. No other path — not a file, not a directory entry — MAY be added, removed, or modified in the squash.
- `parents(T) == <parent_commits>` of the variant, in the order recorded on the variant ([`02-data-model.md`](02-data-model.md) §9.1 inherits from the idea, §5.1).
- A commit message whose first line (subject) records the variant identity in a machine-parseable form. §3.3 pins the format.

The executor is not required to carry the evaluation manifest in its worker branch; the manifest is exclusively the integrator's write. If a worker branch incidentally contains a file at the evaluation-manifest path (e.g. an executor mistake), the integrator MUST reject the variant rather than silently overwrite the file. Rejection produces no `variant/*` commit and no `variant.integrated` event; the orchestrator MAY transition the variant via normal channels.

The squash is a single merge-commit-shaped object (parents may be one or many, per the idea's `parent_commits`); intermediate work-branch commits MUST NOT appear in `T`'s ancestor chain along a first-parent path when read from `variant/*`. Readers of the canonical lineage see one commit per variant, not the worker's intermediate history.

A conforming implementation MAY additionally preserve the worker branch's history elsewhere (retained `work/*` ref, archive) for audit, but that preservation MUST NOT make intermediate commits reachable from `variant/*`.

### 3.3 Commit message

The `variant/*` commit message's first line (subject) MUST match:

```text
variant: <variant_id> <slug>
```

The subject MUST carry both the `variant_id` and the parent idea's `slug`, separated by a single space. The body MAY include additional human-readable context. The machine-parseable header allows a subscriber reading git alone — without the event log — to recover the `variant_id` of any `variant/*` commit.

### 3.4 Atomic write

The integration MUST be atomic with two other state changes:

1. Writing the `variant_commit_sha` field on the variant to the SHA of the new `variant/*` commit ([`02-data-model.md`](02-data-model.md) §9.1).
2. Appending a `variant.integrated` event to the event log ([`05-event-protocol.md`](05-event-protocol.md) §3.3).

If any of the three steps fails — git ref write, variant-object field write, event append — the integrator MUST roll back any already-performed step. In particular, a dangling `variant/*` ref with no `variant_commit_sha` field and no `variant.integrated` event is a protocol violation. Implementations MAY use store-level transactions, outbox patterns, compensating deletes, or any other mechanism; the observable invariant is that a reader of any one of the three artifacts (ref, field, event) MUST observe the other two.

This invariant applies to completed integrations: once a integration has returned — whether by success or by rollback — the three artifacts MUST reconcile, with all three present on success and none present on rollback. A integration that is still running MAY transiently expose intermediate states to an external reader (for example, a `variant/*` ref written before its compensating delete, or a store-side field written before the git ref). The compensating-delete mechanism named above creates such states by construction; zero-width multi-artifact consistency during a running integration is not required, and conforming implementations MAY rely on compensating deletes rather than two-phase commit or outbox patterns.

See [`design-notes/integrator-atomicity.md`](design-notes/integrator-atomicity.md) for the rationale behind this reading, the alternatives considered, and the conditions under which a future revision of this chapter might tighten the invariant.

## 4. Evaluation manifest

Each `variant/*` commit MUST carry an **evaluation manifest** at a fixed path inside its tree. The manifest is the canonical, git-embedded record of the variant's evaluator outputs: what was measured, which worker commit was measured, where the supporting artifacts live.

### 4.1 Path

The evaluation manifest is a JSON file at:

```text
.eden/variants/<variant_id>/evaluation.json
```

relative to the root of the committed tree. The `<variant_id>` component matches the variant's `variant_id`. An integrator MUST NOT add any file under `.eden/variants/<other_variant_id>/` to a variant's commit; each `variant/*` commit carries exactly one evaluation-manifest path, corresponding to its own variant.

### 4.2 Shape

The manifest is a JSON object with the following required fields. Each required field is taken from state the protocol already writes via the role contracts in [`03-roles.md`](03-roles.md) — no role submission is extended by this chapter.

| Field | Type | Source |
|---|---|---|
| `variant_id` | string | The variant's `variant_id` ([`02-data-model.md`](02-data-model.md) §9.1). |
| `idea_id` | string | The variant's `idea_id` ([`02-data-model.md`](02-data-model.md) §9.1). |
| `commit_sha` | string | The worker-branch tip the evaluator measured ([`02-data-model.md`](02-data-model.md) §9.1). |
| `parent_commits` | array of string | The variant's `parent_commits`, in order ([`02-data-model.md`](02-data-model.md) §9.1). |
| `evaluation` | object | The evaluator's evaluation payload ([`03-roles.md`](03-roles.md) §4.4), conforming to the experiment's `evaluation_schema`. |
| `completed_at` | timestamp | The variant's `completed_at` ([`02-data-model.md`](02-data-model.md) §9.1). UTC, RFC 3339 profile as elsewhere in the data model. |

Optional fields:

| Field | Type | Source |
|---|---|---|
| `artifacts_uri` | string (URI) | The variant's `artifacts_uri` if the evaluator supplied one ([`03-roles.md`](03-roles.md) §4.4). |
| `description` | string | The variant's `description`, copied from the variant object if present. |
| `evaluator` | object | Implementation-defined identity of the evaluator (§4.3). Present when the evaluator's role-binding surfaces this metadata. |
| `artifacts` | object | Implementation-defined inventory of per-file artifact metadata (§4.4). Present when the role-binding surfaces it. |

Every required field's value MUST equal the corresponding field on the variant object at the moment of integration. A conforming integrator MUST NOT synthesize or transform required-field values; they are a canonical, in-tree snapshot of the variant state, not a separate derivation.

Implementations MAY include additional informational fields beyond those listed; consumers MUST tolerate them.

### 4.3 `evaluator` shape (optional)

When present, the `evaluator` object has the form:

```json
{
  "name": "<implementation-chosen identifier for the evaluator>",
  "version": "<version string the evaluator presented at submission>"
}
```

Both fields are required when the `evaluator` object itself is present. `name` and `version` together identify *which* evaluator measured the variant. Because v0 does not surface evaluator identity through [`03-roles.md`](03-roles.md) §4.4, this field is optional: populate it when a role-binding or deployment convention carries evaluator metadata; omit it otherwise.

### 4.4 `artifacts` shape (optional)

When present, `artifacts` is a JSON object whose values describe per-file metadata for individual artifacts referenced from the variant (commonly the files under the evaluator's `artifacts_uri`). Each entry has the form:

```json
{
  "uri": "<URI of the specific file>",
  "sha256": "<hex sha256 of the file's byte content>",
  "bytes": <non-negative integer>
}
```

Keys are operator-chosen short labels (e.g. `"log"`, `"output.csv"`) and MUST be unique within the manifest. This inventory is **optional in v0** because [`03-roles.md`](03-roles.md) §4.4 does not require an evaluator to enumerate per-file hashes; a deployment whose role- binding surfaces that inventory MAY populate this field, and downstream consumers SHOULD prefer the inventory (when present) to blind re-fetches of `artifacts_uri` for reproducibility checks.

### 4.5 Manifest immutability

Because the evaluation manifest lives inside the `variant/*` commit's tree, it inherits the immutability of the `variant/*` branch (§1.2): once written, it MUST NOT be rewritten. A conforming implementation that discovers a manifest error after the fact MAY record corrective state outside git (operator-level event, separate annotation store) but MUST NOT mutate the `variant/*` commit.

## 5. Failure modes

### 5.1 Evaluation-schema violation at integration time

If the integrator re-validates the evaluation (per §2) and finds it in violation of the experiment's `evaluation_schema`, the integrator MUST NOT produce a `variant/*` commit, MUST NOT write `variant_commit_sha`, and MUST NOT append `variant.integrated`. It MUST surface the problem through an implementation-defined operator channel; since the variant's `status` was written to `success` by the orchestrator at the evaluation-task terminal, the integration-side rejection is a protocol-level drift between the two that an operator MUST resolve. A conforming orchestrator makes this case rare by enforcing schema conformance at the evaluation-task terminal.

### 5.2 Partial git write

If the underlying git ref write appears to succeed but later fails durability (crashed filesystem, partial replication), the integrator's atomicity contract (§3.4) is violated. Implementations MUST ensure ref durability before considering a integration complete; the durability semantics that apply at the git layer mirror the storage-side rules in [`08-storage.md`](08-storage.md) §3 (write durability, read-after-write, crash recovery).

### 5.3 Repeat integration

If the integrator is invoked twice for the same variant (replay of a `variant.succeeded` event, operator retry), it MUST be idempotent:

- If the variant already has `variant_commit_sha` set and that SHA resolves to a `variant/*` commit whose tree satisfies §3.2 (the worker-tip tree with exactly the evaluation-manifest path added, no other path changes) and whose evaluation manifest matches §4, the second invocation MUST be a no-op (no new ref, no new event).
- If the variant already has `variant_commit_sha` set but the recorded SHA does not resolve or disagrees with the variant, the state is corrupt; the integrator MUST NOT silently overwrite. An operator channel per §5.1 applies.

## 6. Implementation latitude

The protocol leaves to implementations:

- The git host (local bare repo, Forgejo, GitHub, etc.) and the transport used to write `variant/*` refs.
- The retention policy for `work/*` branches after integration.
- The exact author/committer identity stamped on the integrator's commits.
- The mechanism of atomicity in §3.4 — outbox, store-level transaction, compensating writes.

What the protocol does **not** leave to implementations:

- The three-namespace topology and sole-integrator rule (§1).
- The single-commit squash shape and the tree rule in §3.2 (worker-tip tree plus exactly the evaluation-manifest path, no other path changes).
- The commit-message format (§3.3).
- The evaluation-manifest path and required fields (§4).
- The atomic-three invariant (§3.4).
