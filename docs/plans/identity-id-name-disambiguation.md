# Plan — Disambiguate user-facing names from system-generated ids

Issue: [#128](https://github.com/ealt/eden/issues/128). Cluster: `identity`.
Priority: `2-planned`.

## 1. Context

Three identity-carrying entities in v0 use **operator-typed strings as
their primary identifier**, conflating system identifier with
operator-facing label:

| Entity | Today | Conflation pain |
|---|---|---|
| `experiment_id` | Operator picks at `setup-experiment --experiment-id <X>` | Collisions across the deployment; archived/resurrected names overwrite history; no recourse on typo. |
| `worker_id` | Operator picks at register; grammar `^[a-z0-9][a-z0-9_-]{0,63}$`; reserved `admin` / `system` / `internal`. | Per-deployment naming collisions; "what should I call this worker" is a recurring operator question with no canonical answer; pickers in UIs are forced to render the id directly. |
| `group_id` | Operator picks at register; same grammar; reserved `admins` / `orchestrators`. | Same as workers. |

The principle: **ids are system-minted, opaque, and stable; names are
operator-supplied, mutable-or-not (open), and may collide**. This plan
realizes that split across spec, schemas, contracts, storage, wire,
web UI, CLI, conformance, and docs.

Per [`docs/glossary.md`](../glossary.md) §1 and the issue, three
ids stay where they are:

+ **`idea_id`** / **`variant_id`** / **`task_id`** / **`event_id`** are
  already opaque + system-minted; no change.
+ **`slug`** on `Idea` is already a separate field from `idea_id` (an
  operator-supplied kebab-case label used in branch names per
  [`spec/v0/06-git-protocol.md`](../spec/v0/06-git-protocol.md) §3.2);
  it doesn't conflate id with name. Out of scope for this plan; bounded
  by issue [#121](https://github.com/ealt/eden/issues/121).

This plan is the **foundation for cluster `identity`** —
[#140](https://github.com/ealt/eden/issues/140) (operator-as-worker),
[#141](https://github.com/ealt/eden/issues/141) (deployment-scoped
worker registry), [#143](https://github.com/ealt/eden/issues/143) (sign-up
admin model), and [#144](https://github.com/ealt/eden/issues/144) (admin
route gating) all build on this rename. See §8 for the cascade.

## 2. Decisions captured before drafting

These decisions are settled by the issue body and re-affirmed here. If
codex-review or operator review surfaces a contrary view, the resolution
goes into §11 (risks) before execution starts.

1. **All three id surfaces get the split** — experiment, worker, group.
   Not just experiment + worker. Not just worker. The issue is explicit
   that all three suffer the same conflation; a partial fix locks in the
   inconsistency.
2. **Names MAY collide; the system never resolves a name to a unique
   entity automatically.** `GET /v0/experiments/{E}/workers?name=foo`
   returns 0..N matches; operators disambiguate by id.
3. **Wire references stay opaque.** `Task.target.id`,
   `Idea.intended_executor.id`, attribution fields (`created_by` /
   `submitted_by` / `executed_by` / `evaluated_by` / `reassigned_by`),
   and bearer principals all carry the system-minted id. The name is
   purely a display layer.
4. **The reference impl is pre-external-user** (CLAUDE.md "Project
   Lifecycle"). No backwards-compat shims, no migration tooling for
   pre-rename checkpoints. Clean break; existing experiments are
   abandoned and re-bootstrapped. See §6.
5. **Reserved values move from id-space to name-space** (issue body):
   `admin` / `system` / `internal` become reserved `worker_name`s;
   `admins` / `orchestrators` become reserved `group_name`s with
   system-minted opaque ids. The deployment-admin **bearer principal**
   stays the literal token `"admin"` (it is a deployment-scoped role,
   not a per-worker identity); no `worker_id` is allocated for it.
6. **Name mutability is deferred.** Whether `name` can change after
   create is a design choice (probably yes for human-error recovery)
   that this plan does NOT pin down. The deferred decision is tracked
   in §11.2; no implementation work in this chunk depends on it.

## 3. Survey — every entity that today uses an id as a label

Three columns per entity: today's id semantics, what operators currently
see, what they want to see. Citations are to the in-repo source of
truth; spec sections in this table assume the v0 lineage.

### 3.1 Primary entities (in scope)

| Entity | Today's id | Today's operator view | Desired view |
|---|---|---|---|
| **Experiment** | `experiment_id` operator-typed at `setup-experiment --experiment-id`; primary key on the task-store `experiment` table ([`reference/packages/eden-storage/src/eden_storage/_schema.py`](../../reference/packages/eden-storage/src/eden_storage/_schema.py)); also the key on the control-plane registry entry returned by `register_experiment` / `list_experiments` / `read_experiment_metadata` ([`spec/v0/11-control-plane.md`](../spec/v0/11-control-plane.md) §2.1-§2.2; [`reference/packages/eden-control-plane/src/eden_control_plane/models.py`](../../reference/packages/eden-control-plane/src/eden_control_plane/models.py)). Embedded in every `/v0/experiments/{E}/...` task-store path and `/v0/control/experiments/{E}` control-plane path. Default in fixtures: `manual-ui`. | Operator sees their typed string in URLs, web-UI titles, control-plane experiment lists, `.env` (`EDEN_EXPERIMENT_ID`), and data-root path. | Opaque `exp_<ULID>` everywhere wire-shaped; operator-supplied `Experiment.name` rendered in titles, lists, and dashboards. Task-store and control-plane registry surfaces both carry the opaque id; the control-plane registry also carries the optional display name so cross-experiment views can render it without ad-hoc task-store reads. |
| **Worker** | `worker_id` operator-typed at `register_worker`; grammar `^[a-z0-9][a-z0-9_-]{0,63}$` per [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) §6.1; reserved `admin` / `system` / `internal` ([`reference/packages/eden-storage/src/eden_storage/_base.py`](../../reference/packages/eden-storage/src/eden_storage/_base.py)). PK on the per-experiment `worker` table, and separately on the deployment-scoped control-plane registry (`control_plane_workers`) for chapter-11 bootstrap + lease ownership ([`reference/packages/eden-control-plane/src/eden_control_plane/postgres.py`](../../reference/packages/eden-control-plane/src/eden_control_plane/postgres.py)). Used as bearer principal per [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) §13. Reference deployment seeds: `operator`, `orchestrator`, `web-ui-1`, `ideator-host-1`, `executor-host-1`, `evaluator-host-1`, `auto-orchestrator-1`. | Operator sees the chosen id in worker lists, registration responses, bearer tokens, every event payload (`task.claimed.data.worker_id`, etc.), every attribution field, and the control-plane's deployment-scoped worker registry. | Opaque `wkr_<ULID>` in all wire/storage surfaces. `Worker.name` is the operator-supplied display label (e.g., `"Eric (laptop)"` or `"executor host 1"`). Pickers render `name (wkr_…)`. The deployment-scoped control-plane registry uses the same opaque-id/name split. |
| **Group** | `group_id` operator-typed at `register_group`; same grammar as worker; reserved `admins` / `orchestrators`. PK on the per-experiment `worker_group` table and on the deployment-scoped control-plane group registry (`control_plane_groups`). Targeted by `Task.target.id` (when `kind=="group"`), `Idea.intended_executor.id` / `Idea.intended_evaluator.id`, and the control-plane lease authority gate on the deployment-scoped `orchestrators` group. | Operator sees `admins`, `orchestrators`, and any operator-created group ids directly in admin UI, control-plane admin views, task `target` fields, and idea routing hints. | Opaque `grp_<ULID>`. `Group.name` is the operator-supplied display label. Reserved groups (`admins`, `orchestrators`) auto-created at setup-experiment and in the control-plane bootstrap with system-minted opaque ids and `name == "admins"` / `name == "orchestrators"`. |

The chapter-11 `RegisteredExperiment` projection is not a fourth rename
target; it is the deployment-scoped projection of the same Experiment
identity and therefore rides the Experiment row above. `ExperimentLease`
likewise does not introduce a new human-named entity: its `holder`
field is covered by the Worker row, and its `lease_id` /
`holder_instance` are already opaque in §3.2.

### 3.2 Already-opaque ids (no change)

| Entity | Why already correct |
|---|---|
| `idea_id` | System-minted ULID per [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) §1.3; `slug` carries the operator-supplied label. |
| `variant_id` | System-minted; separate `branch` field (derived from `variant_id` + `slug`). |
| `task_id` | System-minted; no operator-facing label today, none planned. |
| `event_id` | System-minted ULID; no label semantics. |
| `lease_id` | System-minted UUID per [`spec/v0/11-control-plane.md`](../spec/v0/11-control-plane.md) §4; control-plane internal. |
| `holder_instance` | Per-process UUID, not user-facing. |

### 3.3 Borderline / explicitly out of scope

| Surface | Why out of scope |
|---|---|
| `Idea.slug` | Already disjoint from `idea_id`. Operator-supplied label; collision soft-checked per [#121](https://github.com/ealt/eden/issues/121). |
| `Task.kind` (`ideation` / `execution` / `evaluation`) | Enum, not an identifier. No conflation risk; renaming retired by past discipline ([`docs/glossary.md`](../glossary.md) §3.2). |
| `dispatch_mode` keys (`ideation_creation`, etc.) | Enums. No id semantics. |
| `experiment_config` filename / path | Operator-typed, but path-not-id; no wire identifier semantics. |
| `Lease` cross-experiment identity | Lease holders are workers per chapter 11 §4; the lease's `holder` field uses opaque worker_id under the new shape automatically. |
| Forgejo / git remote credentials (`FORGEJO_REMOTE_USER`, etc.) | Backing-store auth, not protocol identity. |

## 4. Naming model

Three coherent options. The recommendation is option **A — every
primary identity entity gets both an opaque id and an optional
operator-supplied name**.

### Option A (recommended) — every entity gets a name

For each of `Experiment`, `Worker`, `Group`:

+ `*_id` is **opaque, system-minted, immutable**. Format: stable
  type-prefix (`exp_` / `wkr_` / `grp_`) + Crockford base32 ULID (26
  chars). Total length 30 chars; fits inside the existing 64-char
  worker/group grammar cap with room. Examples:
  + `exp_01HQS3M4N5P6Q7R8S9T0V1W2X3`
  + `wkr_01HQS3M4N5P6Q7R8S9T0V1W2X4`
  + `grp_01HQS3M4N5P6Q7R8S9T0V1W2X5`
+ `*_name` is **optional**, operator-supplied at create-time. Free-form
  Unicode string with a minimal grammar (see §4.3). Names MAY collide
  inside an entity kind; the system never resolves a name to a unique
  entity automatically. If omitted at create-time, the entity has no
  name (displays render fall back to id-only).
+ Lookups by name (`GET /v0/experiments/{E}/workers?name=<n>`) return
  0..N matches; operators disambiguate by id when reading.
+ Wire references continue to carry the opaque id.

**Pros.** Symmetric across the three entities; matches the issue's
stated principle ("ids should not be conflated with names"); no
hard-to-explain "only some entities have names" carve-out; aligns with
the centralized-platform PRD vocabulary which already presumes
controller-minted experiment ids ([`docs/prds/eden-experiment-platform.md`](../prds/eden-experiment-platform.md)
§3).

**Cons.** Most surface area touched. Slightly more migration work in
the storage backends + schemas + conformance scenarios than the narrower
options.

### Option B — only operator-spawned entities get names

`Experiment` and `Worker` get names; `Group` keeps a single
operator-typed identifier (just renamed to `name` and grammar-validated;
no opaque id). Reserved-group machinery stays where it is today.

**Pros.** Slightly less work. Reflects an intuition that groups are
"more like enums than entities" (since members can be added without
recreating the group).

**Cons.** Asymmetry costs more than it saves: pickers for `target.id`
have to switch on whether the target is a worker (opaque id, render the
name) vs. a group (typed id, render it directly). The picker UI work
([#140](https://github.com/ealt/eden/issues/140)) becomes harder, not
easier. Group collisions across operators remain. Setup-experiment
still has to deal with hard-coded `admins` / `orchestrators` reserved
ids — the cleanup win that motivates this plan is partial.

### Option C — only `Experiment` and `Worker` get names

`Group` skipped entirely. Same shape as B.

**Pros.** Smallest blast radius.

**Cons.** Same asymmetry pain as B, plus: the worker / group dropdown
in the executor / group picker UI (the downstream consumer the issue
flags as the immediate motivator) is structurally broken in a
name-as-display world if groups still expose typed ids. Defers the same
work to a later chunk that has to land before the picker UI.

### Recommendation: Option A

The cluster-`identity` work ([#140](https://github.com/ealt/eden/issues/140),
[#141](https://github.com/ealt/eden/issues/141), [#143](https://github.com/ealt/eden/issues/143))
already presumes symmetric `worker_name` / `group_name` semantics: the
operator-identity-as-worker model (#140) needs a `worker_name` distinct
from a system-minted `worker_id`; the deployment-scoped worker registry
(#141) needs the same for groups. Doing two-of-three now and the third
later would land cluster-identity in an awkward asymmetric state across
chunks. The marginal cost of doing groups now is ~1 wave of additional
work (see §7); the cost of asymmetry is paid every time a downstream
chunk has to reason about it.

### 4.1 Opaque-id format details

| Aspect | Choice | Rationale |
|---|---|---|
| Prefix | `exp_` / `wkr_` / `grp_` | Operator-meaningful (you can see at a glance what kind of thing an id is in a log line or URL); two-token shape (prefix + suffix) is grep-friendly. |
| Suffix | Crockford base32 ULID, 26 chars (lower-case) | Time-ordered (lexicographic sort ≈ creation order); cryptographically random tail (80 bits) for collision avoidance; well-known format with library support in Python (`python-ulid`); operator-readable enough for ad-hoc reference. |
| Total length | 30 chars (4 + 26) | Fits inside the existing `^[a-z0-9][a-z0-9_-]{0,63}$` grammar; existing 64-char column widths and indexes carry. |
| Case | Lower-case throughout | Matches the existing identifier grammar; avoids mixed-case ambiguity in log searches. |
| Uniqueness | Per-deployment for `wkr_*` / `grp_*`; per-control-plane for `exp_*` once chunk 11 lands. v0 enforces per-experiment store. | Matches the storage scope today; no cross-deployment federation. |
| Stability | Immutable after mint. | Foundational invariant; downstream issue #140 attribution sticks. |

**Why ULID, not UUIDv7 / UUIDv4 / nanoid / TSID:**

+ ULID is time-sortable (UUIDv4 is not), important for operator
  scanability in logs and in admin lists.
+ Crockford base32 is shorter than UUID's 36-char dashed form (26 vs
  36) without sacrificing entropy.
+ UUIDv7 would also work; ULID is conventionally lower-case and
  matches the existing grammar without a casing exception.
+ Python ecosystem support is strong (`python-ulid`); already
  zero-dep-cost.
+ The control-plane (chapter 11) already mints opaque `lease_id` /
  `holder_instance` as UUID4. We'll standardize on ULID for the new
  ids; existing UUIDs stay UUIDs (chapter 11 is already shipped, no
  retroactive rename).

### 4.2 Grammar formalization

Add a new identifier grammar to [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md)
§1 (identifier scope):

```text
opaque-id          = type-prefix "_" base32-suffix
type-prefix        = "exp" / "wkr" / "grp"
base32-suffix      = 26 * CROCKFORD-B32-LOWER
CROCKFORD-B32-LOWER = "0"-"9" / "a"-"h" / "j"-"k" / "m"-"n" / "p"-"t" / "v"-"z"
```

The grammar is **normative for chapter 06 §6, §7 (worker / group
identifiers) and chapter 02 §2 (experiment identifier)**. Implementations
MUST mint ids matching this grammar; conforming implementations MAY
accept legacy operator-typed ids in pre-v1 stores during a transition
(NOT supported in this plan; clean break per §6).

### 4.3 Name grammar

Add a parallel name grammar to chapter 02 §1:

```text
display-name       = 1*128 UNICODE-VISIBLE
UNICODE-VISIBLE    = %x21-7E / U+0080-+10FFFF except Cc, Cs, Cn, Co
```

In prose: **1 to 128 code points; no control characters (Unicode
category Cc); no leading or trailing whitespace; not entirely
whitespace; NFC-normalized at the wire boundary.** The wire layer
rejects ill-formed names with 422 `eden://error/invalid-name`.

Names are **not** subject to the kebab-case / opaque-id grammar; they
are free-form display strings.

**Open question deferred to impl review (tracked in §11.1):** Should names be unique within
an experiment for a given entity kind? The recommendation is **no** —
the issue explicitly states "names MAY collide". Soft-checking at
create-time (like slug [#121](https://github.com/ealt/eden/issues/121))
is a reasonable polish follow-up; not in this plan.

### 4.4 Reserved-name semantics

Reserved values move from id-space to name-space:

| Reserved string | Today (id) | After (name) |
|---|---|---|
| `admin` | Reserved `worker_id`. | Reserved `worker_name`; `register_worker(name="admin")` returns 422. The `admin` bearer principal is built into wire auth; no per-worker identity exists. |
| `system` | Reserved `worker_id`. | Reserved `worker_name`; `register_worker(name="system")` returns 422. |
| `internal` | Reserved `worker_id`. | Reserved `worker_name`; same posture. |
| `admins` | Reserved `group_id`, auto-created at setup-experiment. | Reserved `group_name`. Auto-created with system-minted `grp_<ULID>` whose `name == "admins"`. `register_group(name="admins")` after setup-experiment returns 422 (already taken). |
| `orchestrators` | Same. | Reserved `group_name`. Same auto-create posture. |

The reservation is **at register-time**, enforced by the wire binding
and the storage layer (defense-in-depth). Reserved-name comparison is
case-sensitive against the canonical NFC form.

**The `admin` bearer principal remains a literal string sentinel.** It
is a deployment-scoped role, not a per-worker identity. The bearer
format `Authorization: Bearer <principal>:<secret>` still parses
`<principal>` as either the literal `"admin"` or an opaque worker id
(`wkr_<ULID>`); see §5.5.

### 4.5 Pickers, observability, log lines — display conventions

When the operator-facing surface renders an entity:

+ **If a name exists**: render `<name> (<id>)`. Example: `"Eric (wkr_01HQS3M4N5)"`. The id-in-parens MAY be elided when context already disambiguates (a worker detail page, a task-claim row scoped to one claimant).
+ **If no name exists**: render the bare opaque id. Example: `wkr_01HQS3M4N5`.
+ **Log lines and structured events**: opaque id only. Operators reading log files use the id; the name is purely a UI affordance.
+ **Pickers / dropdowns**: render `<name> (<id>)`; on selection, the opaque id is what enters the form field.
+ **Default rendering when name is reserved**: `admins (grp_…)` is fine; no special-casing.

Codify these conventions in [`docs/glossary.md`](../glossary.md) §8 (operational vocabulary) and in [`docs/user-guide.md`](../user-guide.md).

## 5. Migration map

Per-surface, old → new. Field names in italics; types unchanged unless
called out.

### 5.1 Spec prose

| Chapter | Section | Today | After |
|---|---|---|---|
| [`spec/v0/01-concepts.md`](../spec/v0/01-concepts.md) | §1 (experiment), §2.1-2.4 (roles), §10 (registry) | Worker / group described by their operator-typed ids. | Worker / group described by opaque id + optional name; ids are operator-opaque, names are operator-supplied. |
| [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) | §1 (identifier scope) | Per-entity grammar inline. | New §1.x "Opaque identifiers" defining `exp_*` / `wkr_*` / `grp_*` grammar + name grammar (§4.2, §4.3). The pre-rename `^[a-z0-9][a-z0-9_-]{0,63}$` worker/group grammar is retired for new ids; the new opaque grammar still satisfies the old one (intentional, to ease library migration). |
| [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) | §2 (experiment) | `experiment_id: <string>`. | `experiment_id: opaque-id (exp_*)` + `name: display-name?`. |
| [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) | §6 (worker registry) | `worker_id: <kebab grammar>`; reserved `admin` / `system` / `internal`. | `worker_id: opaque-id (wkr_*)`; system-minted at register-time. New optional `name: display-name?`. Reserved names defined in §6.x; old reserved ids retired. |
| [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) | §7 (groups) | `group_id: <kebab grammar>`; reserved `admins` / `orchestrators`. | `group_id: opaque-id (grp_*)`; system-minted at register-time. New optional `name: display-name?`. Reserved names `admins` / `orchestrators` enforced at register-time; the two reserved groups are auto-created by setup-experiment with system-minted opaque ids and the canonical reserved names. |
| [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) | §3 (tasks) | `target.id`: a worker_id or group_id. | `target.id`: an opaque `wkr_*` or `grp_*` id. `target.kind` disambiguates. |
| [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) | §5 (ideas) | `intended_executor.id` / `intended_evaluator.id`: worker_id or group_id. | Same shape, opaque ids. |
| [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) | §9 (variants) | `executed_by`, `evaluated_by`: worker_id. | Opaque `wkr_*` id. |
| [`spec/v0/03-roles.md`](../spec/v0/03-roles.md) | §1-§4 (role inputs/outputs), §6.3, §6.5, §6.6 | Role contracts refer to operator-typed `worker_id`, reserved `admins` / `orchestrators` group literals, and `payload.experiment_id` / routing targets without the opaque-id split. | Update the role contracts and authority text in the same spec wave: claimant / attribution ids are opaque, `admins` / `orchestrators` are reserved names resolved to opaque group ids, and the chapter-11 lease holder continues to be an opaque worker id. |
| [`spec/v0/04-task-protocol.md`](../spec/v0/04-task-protocol.md) | §3 (claim), §4 (submit) | `worker_id` parameter is the kebab-grammar id. | `worker_id` parameter is the opaque `wkr_*` id. State-machine semantics unchanged. |
| [`spec/v0/04-task-protocol.md`](../spec/v0/04-task-protocol.md) | §6 (reassign), §7 (dispatch-mode) | `admins`-group membership check is by group id. | Check is against the deployment's reserved-name-`admins` group (whose system-minted opaque id is looked up at startup). |
| [`spec/v0/05-event-protocol.md`](../spec/v0/05-event-protocol.md) | Event payloads | `worker_id` fields are kebab-grammar strings. | Opaque `wkr_*` ids. `experiment_id` in envelope is opaque `exp_*`. |
| [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) | §1.3 (path scoping) | `/v0/experiments/{experiment_id}/…` carries operator-typed id. | Same path shape; the `{experiment_id}` segment is now opaque `exp_*`. `X-Eden-Experiment-Id` header still mirrors. |
| [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) | §6 (workers), §7 (groups) | Body: `{worker_id, labels?}` → `{worker_id, registration_token}`. | Body: `{name?, labels?}` → `{worker_id, name?, registration_token, …}`. Server mints `worker_id`. New `GET /workers?name=<n>` query (0..N matches). Symmetric for groups. |
| [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) | §13 (auth) | Bearer principal = `admin` or `<worker_id>` (kebab grammar). | Bearer principal = `admin` (literal) or `<wkr_*>` (opaque). Format unchanged. |
| [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) | §14 (checkpoint) | Checkpoint manifest carries `experiment_id`; import takes `as_experiment_id` optional override. | Same shape; manifest's `experiment_id` is opaque `exp_*`. On import without override, the receiving store generates a fresh `exp_*` (the export's id is preserved as `imported_from.source_experiment_id` for provenance, NOT reused as a primary key). This is a small but normative behavior change; call out in chapter 10. |
| [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) | §15 (control-plane endpoints) | `POST /v0/control/experiments` and the deployment-scoped worker/group registry all take or return operator-typed ids, and experiment-registry payloads have no `name` field. | Control-plane experiment create/list/read shapes carry opaque `exp_*` ids plus optional `name`; deployment-scoped worker/group registry endpoints switch to server-minted opaque ids with optional names exactly like the per-experiment registry. |
| [`spec/v0/09-conformance.md`](../spec/v0/09-conformance.md) | §4 (v1+roles scope), §5 (group index) | Scenarios reference operator-typed ids in prose where helpful. | Update prose; the IUT contract (§6) still presents opaque ids, so scenarios that today expect a particular `worker_id` string need rewriting to register-then-use-the-returned-id. |
| [`spec/v0/10-checkpoints.md`](../spec/v0/10-checkpoints.md) | §7 (`checkpoint:sha256:` URIs), §10 (import provenance) | `Experiment.imported_from` carries `{checkpoint_exported_at, checkpoint_format_version}`. | Add `source_experiment_id: opaque-id?` field carrying the export-side `exp_*`. v0 lineage; pre-rename checkpoints are not importable (per §6). |
| [`spec/v0/08-storage.md`](../spec/v0/08-storage.md) | §8 (registry scope), §9.1-§9.4 (worker/group registry ops) | Store contract still names operator-supplied `worker_id` / `group_id`, idempotent re-register-on-same-id, and no name field. | Update the storage contract to the opaque-id/name split in the same spec wave: register ops mint ids, `read_*` / `list_*` return optional names, and the reserved-name / lookup-by-name behavior is pinned here alongside chapter 02. |
| [`spec/v0/11-control-plane.md`](../spec/v0/11-control-plane.md) | §2.1-§2.3 (experiment registry), §4.2-§4.7 (leases), §6 (deployment-scoped registry) | Experiment-registry entries key on typed `experiment_id`; deployment-scoped workers/groups use typed ids; `Lease.holder` is a typed worker id; the registry entry has no display name. | Same shape, but ids are opaque. Add optional experiment / worker / group `name` fields to the deployment-scoped registry where the object is operator-facing; `Lease.holder` becomes opaque `wkr_*`; reserved groups move from id-space to name-space here too. |

### 5.2 JSON Schemas

| File | Today | After |
|---|---|---|
| [`spec/v0/schemas/experiment.schema.json`](../spec/v0/schemas/experiment.schema.json) | `experiment_id: {type: string, minLength: 1}`. | `experiment_id: {type: string, pattern: "^exp_[0-9a-hjkmnp-tv-z]{26}$"}`. Add `name: {type: string, minLength: 1, maxLength: 128, ...}` optional. |
| [`spec/v0/schemas/worker.schema.json`](../spec/v0/schemas/worker.schema.json) | `worker_id: {type: string, pattern: "^[a-z0-9][a-z0-9_-]{0,63}$"}`. | `worker_id: {type: string, pattern: "^wkr_[0-9a-hjkmnp-tv-z]{26}$"}`. Add `name` optional. |
| [`spec/v0/schemas/group.schema.json`](../spec/v0/schemas/group.schema.json) | `group_id: {…kebab grammar}`. | `group_id: {…grp_*}`. Add `name` optional. Member array entries match the union `wkr_*` ∪ `grp_*` opaque grammars. |
| [`spec/v0/schemas/task.schema.json`](../spec/v0/schemas/task.schema.json) | `claim.worker_id` / `submitted_by` / `created_by` / `target.id` / `reassigned_by` use the kebab grammar. | Use the opaque-id grammars; `created_by` is union of `wkr_*` ∪ `"admin"` literal sentinel. |
| [`spec/v0/schemas/idea.schema.json`](../spec/v0/schemas/idea.schema.json) | `created_by` / `intended_executor.id` / `intended_evaluator.id` use kebab grammar. | Use opaque grammars. |
| [`spec/v0/schemas/variant.schema.json`](../spec/v0/schemas/variant.schema.json) | `executed_by` / `evaluated_by` use kebab grammar. | Use opaque `wkr_*` grammar. |
| [`spec/v0/schemas/event.schema.json`](../spec/v0/schemas/event.schema.json) | Envelope `experiment_id`; payloads carry `worker_id` for attribution events. | Envelope `experiment_id` is `exp_*`; payload attribution fields use `wkr_*`. |
| Wire request/response schemas under [`spec/v0/schemas/wire/`](../spec/v0/schemas/wire/) | Register-worker / register-group request bodies require operator-supplied `worker_id` / `group_id`. | Drop the required `worker_id` / `group_id` from create-request bodies; add optional `name`. Response carries the system-minted opaque id. |
| [`spec/v0/schemas/checkpoint-manifest.schema.json`](../spec/v0/schemas/checkpoint-manifest.schema.json) | `experiment_id` is any non-empty string and import prose says the receiver reuses it unless overridden. | Tighten to the opaque `exp_*` grammar and add the provenance behavior from §5.1/§10 (`source_experiment_id` on the imported runtime object; receiver mints a fresh id when no override is supplied). |
| [`spec/v0/schemas/lease.schema.json`](../spec/v0/schemas/lease.schema.json) | `experiment_id` is any non-empty string; `holder` matches the legacy worker-id grammar. | Tighten `experiment_id` to `exp_*` and `holder` to `wkr_*`. `lease_id` / `holder_instance` stay as-is. |
| Control-plane wire / registry schemas | **Not yet authored.** Chapter 07 §15 and chapter 11 §2 / §6 currently rely on prose + JSON examples; there is no normative JSON Schema for `register_experiment`, `RegisteredExperiment`, or the deployment-scoped worker/group registry payloads. | Author these schemas in the rename wave (under `spec/v0/schemas/wire/`) so the control-plane rename participates in the same schema-parity discipline as the task-store wire. Without these files, the control-plane `experiment_id` / `worker_id` / `group_id` rename would be missing the schema leg of the four-way lock. |

### 5.3 Pydantic models (eden-contracts)

| File | Today | After |
|---|---|---|
| [`reference/packages/eden-contracts/src/eden_contracts/_common.py`](../../reference/packages/eden-contracts/src/eden_contracts/_common.py) | `WorkerId` type alias on kebab grammar. | Replace with three opaque-id type aliases: `ExperimentId` (`exp_*`), `WorkerId` (`wkr_*`), `GroupId` (`grp_*`). Add `DisplayName` type alias on the §4.3 grammar. |
| `eden-contracts/experiment.py` | `Experiment.experiment_id: ExperimentId`. | Add `name: DisplayName \| None`. |
| `eden-contracts/worker.py` | `Worker.worker_id: WorkerId`. | Add `name: DisplayName \| None`. |
| `eden-contracts/group.py` | `Group.group_id: WorkerId` (alias). `GroupMember = WorkerId` (covers both worker and group ids, ambiguous). | `Group.group_id: GroupId`. Add `name`. `GroupMember = WorkerId \| GroupId` (the union still has the same string-typed runtime shape, but the static type catches non-opaque inputs). |
| `eden-contracts/task.py`, `eden-contracts/idea.py`, `eden-contracts/variant.py`, `eden-contracts/event.py` | All carry `WorkerId` fields. | Update fields to the new opaque types. `Task.target.id: WorkerId \| GroupId` (matching `target.kind`). |

Schema-parity tests in [`reference/packages/eden-contracts/tests/`](../../reference/packages/eden-contracts/tests/) ride this rename. The corpus fixtures in `tests/cases.py` need new accept/reject fixtures for the opaque grammars (rejects: legacy kebab strings, mismatched type prefixes, wrong length, invalid Crockford alphabet); pre-rename fixtures are removed wholesale (no transitional dual-grammar period — clean break per §6).

### 5.4 Storage backends

[`reference/packages/eden-storage/src/eden_storage/_schema.py`](../../reference/packages/eden-storage/src/eden_storage/_schema.py) (SQLite) and [`reference/packages/eden-storage/src/eden_storage/_postgres_schema.py`](../../reference/packages/eden-storage/src/eden_storage/_postgres_schema.py) (Postgres):

| Table | Column adds / changes | Index changes |
|---|---|---|
| `experiment` | Add `name TEXT`. `experiment_id` column type unchanged (TEXT/text, primary key, still ≤64 chars in practice). | None on PK; add CREATE INDEX on `name` for the `?name=` query path. |
| `worker` | Add `name TEXT`. | Add CREATE INDEX on `(name)` (per-experiment store, so no `(experiment_id, name)` composite is needed at the v0 scope). |
| `worker_group` | Add `name TEXT`. | Add CREATE INDEX on `(name)`. |
| `group_membership` | No columns added; `member_id` is now an opaque id (worker or group). | No index changes; the denormalization rebuilds the same way. |

There is **no in-place migration**. Per §6, pre-rename databases are abandoned; setup-experiment re-bootstraps from scratch. The DDL is the v0-after-rename canonical shape.

Storage protocol surface ([`reference/packages/eden-storage/src/eden_storage/protocol.py`](../../reference/packages/eden-storage/src/eden_storage/protocol.py)) signature changes:

```python
# Before
def register_worker(self, worker_id: str, labels: dict[str, str] | None = None) -> WorkerRegistration: ...
def register_group(self, group_id: str, members: list[str] | None = None) -> GroupRegistration: ...

# After
def register_worker(self, name: str | None = None, labels: dict[str, str] | None = None) -> WorkerRegistration: ...  # mints worker_id
def register_group(self, name: str | None = None, members: list[str] | None = None) -> GroupRegistration: ...  # mints group_id
```

The `WorkerRegistration` / `GroupRegistration` response shapes gain the minted `worker_id` / `group_id` (they already carry it; the change is that the caller no longer supplies it). Reserved-name enforcement lives here: registering with a reserved name raises `ReservedIdentifier`.

`reissue_credential(worker_id)` is unchanged in shape — takes an opaque `wkr_*`. The bootstrap recovery flow in [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) §6.3 / [`12a-1` plan](eden-phase-12a-1-worker-identity.md) §D.1 keeps the same `verify_worker_credential` / `reissue_credential` / first-run `register_worker` ladder; the only change is that first-run `register_worker` mints rather than takes an id.

Deployment-scoped control-plane storage under [`reference/packages/eden-control-plane/src/eden_control_plane/`](../../reference/packages/eden-control-plane/src/eden_control_plane/):

| Surface | Change |
|---|---|
| [`store.py`](../../reference/packages/eden-control-plane/src/eden_control_plane/store.py) | `register_experiment` / `register_worker` / `register_group` signatures switch from caller-supplied ids to minted opaque ids + optional names. The registry entry and worker/group read/list methods return the new `name` fields. |
| [`postgres.py`](../../reference/packages/eden-control-plane/src/eden_control_plane/postgres.py) | `control_plane_experiments` gains `name TEXT`; `control_plane_workers` / `control_plane_groups` gain `name TEXT`; add name indexes for the `?name=` lookup paths. Lease rows keep the same shape but validate `experiment_id` / `holder` against the opaque grammars. |
| [`memory.py`](../../reference/packages/eden-control-plane/src/eden_control_plane/memory.py) | Mirror the same field additions and id-mint semantics as Postgres. |

### 5.5 Wire surface (eden-wire + control plane)

Task-store wire binding in [`reference/packages/eden-wire/src/eden_wire/`](../../reference/packages/eden-wire/src/eden_wire/):

| Endpoint | Today | After |
|---|---|---|
| `POST /v0/experiments/{E}/workers` | Body: `{worker_id, labels?}` required. | Body: `{name?, labels?}`. Response: `{worker_id, name?, registration_token, ...}`. |
| `GET /v0/experiments/{E}/workers` | Returns all workers sorted by `worker_id`. | Same shape; add `?name=<n>` query (case-sensitive, exact match) returning 0..N matches. |
| `GET /v0/experiments/{E}/workers/{W}` | Path-param is the kebab id. | Path-param is the opaque `wkr_*`. |
| `POST /v0/experiments/{E}/workers/{W}/reissue-credential` | Same. | Path-param is opaque. |
| `GET /v0/experiments/{E}/workers/{W}/whoami` | Same. | Path-param is opaque. Response carries opaque id + name. |
| `POST /v0/experiments/{E}/groups` | Body: `{group_id, members?}`. | Body: `{name?, members?}`. Response: `{group_id, name?, members, ...}`. |
| `GET /v0/experiments/{E}/groups?name=<n>` | Not present. | New optional query. |
| `POST /v0/experiments/{E}/groups/{G}/members` | Body: `{member_id}`. | Body: `{member_id}` where `member_id` is opaque `wkr_*` or `grp_*`. (No body-shape change; member-id validity check now expects opaque grammar.) |
| `POST /v0/experiments/{E}/tasks/{T}/reassign` | Body: `{new_target: {kind, id}}`. | Body shape unchanged; `id` is opaque. |
| `POST /v0/experiments/{E}/tasks/{T}/claim` / `submit` | Bearer principal is kebab worker_id. | Bearer principal is opaque `wkr_*` or literal `"admin"`. |

**Bearer parser** ([`reference/packages/eden-wire/src/eden_wire/auth.py`](../../reference/packages/eden-wire/src/eden_wire/auth.py)): the principal grammar check becomes "either the literal `admin` or matches the opaque `wkr_*` grammar". No format-shape change (`<principal>:<secret>`); the only difference is the regex.

Companion wire-model / client / test surfaces that must move with the endpoint handlers:

+ [`reference/packages/eden-wire/src/eden_wire/models.py`](../../reference/packages/eden-wire/src/eden_wire/models.py) — request/response Pydantic types for register-worker / worker-registration / whoami / register-group / add-group-member and any checkpoint or auth-shaped response that carries the renamed fields.
+ [`reference/packages/eden-wire/src/eden_wire/client.py`](../../reference/packages/eden-wire/src/eden_wire/client.py) — `register_worker`, `register_group`, `read_*`, `list_*`, `verify_worker_credential`, `whoami`, checkpoint import/export, and any helper that hardcodes the old request bodies or grammar checks.
+ [`reference/packages/eden-wire/tests/test_wire_schema_parity.py`](../../reference/packages/eden-wire/tests/test_wire_schema_parity.py) — wire schema ↔ Pydantic parity for the rename-affected request/response models.
+ Endpoint-behavior tests under [`reference/packages/eden-wire/tests/`](../../reference/packages/eden-wire/tests/) — at minimum `test_workers_wire.py`, `test_groups_wire.py`, `test_auth.py`, `test_checkpoint_wire.py`, and `test_wire_roundtrip.py`.

Control-plane wire binding in [`reference/packages/eden-control-plane/src/eden_control_plane/`](../../reference/packages/eden-control-plane/src/eden_control_plane/) plus [`reference/services/control-plane/src/eden_control_plane_server/`](../../reference/services/control-plane/src/eden_control_plane_server/):

| Surface | Change |
|---|---|
| `POST /v0/control/experiments` / `GET /v0/control/experiments` / `GET /v0/control/experiments/{E}` | Experiment-registry create/list/read shift to opaque `exp_*` ids and carry optional `name`. Add `?name=<n>` lookup on the list route so cross-experiment admin views can resolve the display name without bespoke task-store calls. |
| `/v0/control/workers*` / `/v0/control/groups*` | Deployment-scoped registry mirrors the per-experiment rename: create bodies take `name?`, server mints opaque ids, reads/lists return optional names, and reserved groups move to name-space. |
| [`models.py`](../../reference/packages/eden-control-plane/src/eden_control_plane/models.py) | Add the `name` field to `RegisteredExperiment`, `Worker`, and `Group` payloads where operator-facing. Tighten `experiment_id` / `holder` to opaque grammars. |
| [`client.py`](../../reference/packages/eden-control-plane/src/eden_control_plane/client.py) | Update control-plane request bodies, response parsing, and any helper that still assumes caller-supplied ids. |
| [`app.py`](../../reference/services/control-plane/src/eden_control_plane_server/app.py), [`auth.py`](../../reference/services/control-plane/src/eden_control_plane_server/auth.py), [`cli.py`](../../reference/services/control-plane/src/eden_control_plane_server/cli.py) | Server request validation, bearer grammar, list filters, and operator-facing CLI / log text must reflect the opaque-id/name split. |
| [`reference/services/orchestrator/src/eden_orchestrator/control_plane_bootstrap.py`](../../reference/services/orchestrator/src/eden_orchestrator/control_plane_bootstrap.py) | Deployment-scoped worker bootstrap and `orchestrators`-group membership must switch from caller-picked ids to name-supplied / id-returned create semantics while preserving the persisted-id credential path. |
| Tests under [`reference/packages/eden-control-plane/tests/`](../../reference/packages/eden-control-plane/tests/) and [`reference/services/control-plane/tests/`](../../reference/services/control-plane/tests/) | Update Pydantic, client, store, and server tests for the renamed fields and the new `name` field on experiment-registry payloads. |

### 5.6 Web UI

[`reference/services/web-ui/src/eden_web_ui/templates/`](../../reference/services/web-ui/src/eden_web_ui/templates/) and corresponding [`routes/`](../../reference/services/web-ui/src/eden_web_ui/routes/):

| Surface | Change |
|---|---|
| `admin_workers.html` / `admin_groups.html` (list views) | Render `<name> (<id>)`; sort by `name`-then-`id`; group reserved-name rows (`admins`, `orchestrators`) into a labelled section. Add a "filter by name" search box that maps to the new `?name=` query. |
| `admin_worker_detail.html` / `admin_group_detail.html` | Show name + id prominently; admin UI surfaces both. Registration form takes name; id is rendered after-mint. |
| `admin_experiments.html` | Add `name` column. List sorted by `name`-then-`id`. |
| `admin_task_detail.html` / `admin_variant_detail.html` / `admin_idea_detail.html` | Attribution fields (`created_by`, `submitted_by`, etc.) resolve worker id → name for display, fall back to id when no name. |
| `ideator.html` / executor / evaluator role views | Same name-resolution discipline. |
| Picker UIs (downstream consumer; will land with [#140](https://github.com/ealt/eden/issues/140) and the post-rename picker follow-up) | This plan does NOT build the executor/group picker. It establishes the data shape (name + id) on which the picker depends; the picker work is downstream. |

Route handlers under [`admin_workers.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_workers.py) / [`admin_groups.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_groups.py) ride the wire rename; create-form posts `name`, not `worker_id`.

### 5.7 CLI / eden-manual

| Surface | Change |
|---|---|
| [`reference/scripts/manual-ui/`](../../reference/scripts/manual-ui/) wrapper scripts | Subcommands that take `--worker-id <id>` continue to do so (the operator can still type the opaque id if they want), but operator-facing helpers like `eden register-worker` accept `--name <name>` and emit the minted opaque id back. |
| [`.claude/skills/eden-manual-ideator/SKILL.md`](../../.claude/skills/eden-manual-ideator/SKILL.md), `-executor`, `-evaluator`, `-experiment` | Update examples: register-then-use the returned `worker_id`, rather than hardcoding `executor-host-1` etc. The skills' Claude-driving prompts already do most ops by reading from `.eden/credentials/`; the change is to display names alongside ids when listing. |
| Service CLIs under [`reference/services/`](../../reference/services/) and shared flag helpers in [`reference/services/_common/src/eden_service_common/cli.py`](../../reference/services/_common/src/eden_service_common/cli.py) | `--worker-id` / `--experiment-id` flags keep their names, but every help string, bootstrap path, and env-var description must state that these are minted opaque ids. The rename also affects the control-plane bootstrap flags on the orchestrator and web UI (`--control-plane-url`, `--control-plane-admin-token`) because those flows hit the deployment-scoped registry. |
| Convenience subcommands `list-workers` / `list-groups` (downstream; [#128](https://github.com/ealt/eden/issues/128) issue body §"Downstream consumers") | Not in this plan; tracked in §11.6 after the rename lands. |

### 5.8 Setup-experiment + compose env

| Surface | Change |
|---|---|
| [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh) | Mints opaque `exp_*` / `wkr_*` / `grp_*` ids. Writes them to `.env` alongside the deployment admin token. Names are operator-supplied via flags (`--name`, `--worker-name <role> <name>`, etc.) or defaulted to the role label for auto-host workers. |
| `.env.example` | `EDEN_EXPERIMENT_ID` becomes `exp_*` opaque. `EDEN_ORCHESTRATOR_WORKER_ID` / `EDEN_ADMINS_INITIAL_MEMBER` / `EDEN_WEB_UI_WORKER_ID` become opaque `wkr_*` ids written by setup-experiment (NOT hand-edited; the file becomes a generated-and-checked-in artifact rather than a hand-curated template). |
| `EDEN_EXPERIMENT_DATA_ROOT/<experiment_id>/` path layout | Path segment is the opaque `exp_*` id. Per [#178](https://github.com/ealt/eden/issues/178)'s substrate-migration audit discipline, the rename PR audits every reference to the old typed-id path. |
| Forgejo repo path (`eden/{EDEN_EXPERIMENT_ID}.git`) | Path is the opaque id. Operator-typed mnemonic disappears from URLs; this is a real operator-UX cost (people who liked typing `manual-ui` in URLs lose that). The plan accepts it; the user-guide §6 mitigates by documenting the `name`-based lookup on the control-plane registry (`GET /v0/control/experiments?name=`) and any UI redirect that resolves name → canonical opaque-id URL. |
| `/var/lib/eden/credentials/<worker_id>.token` | Filename is the opaque id; no change in shape. |

### 5.9 Observability + docs

| Doc | Change |
|---|---|
| [`docs/glossary.md`](../glossary.md) | Rewrite §1 (worker roles), §8 (operational vocabulary) — define `experiment_id` / `worker_id` / `group_id` as opaque system-minted; introduce `experiment_name` / `worker_name` / `group_name` as operator-supplied display labels. Update the AT-A-GLANCE intro to cite this plan. |
| [`docs/user-guide.md`](../user-guide.md) §2 (setup-experiment), §6 (registry ops), §10 (auth principal matrix) | Rewrite to reflect name-supplied / id-returned shape; clarify principals are opaque ids. |
| [`docs/operations/initial-admin-credential.md`](../operations/initial-admin-credential.md) | Updated bootstrap flow: the `admin` bearer principal is unchanged; the seeded `operator` worker now has both name and minted id. |
| [`docs/operations/agent-readonly-db.md`](../operations/agent-readonly-db.md) | Document `worker.name` column; `worker.worker_id` is now opaque. |
| [`docs/observability.md`](../observability.md) §2.1 (admin routes table) | Mention name-vs-id display conventions per §4.5. |
| [`docs/prds/eden-experiment-platform.md`](../prds/eden-experiment-platform.md) §3 (controller responsibilities) | Resolve the open question on operator-supplied vs system-minted experiment ids: system-minted is now load-bearing per §3 here. |
| Operations docs referencing reserved-id literals | Replace `admins` / `orchestrators` / `admin` references with the new shape: reserved names; the deployment-admin bearer principal stays `admin`. This includes `docs/operations/dispatch-mode.md`, `multi-orchestrator.md`, `reassign.md`, and any playbook that still shows typed worker ids in curl examples. |
| Active planning / design docs and roadmap cross-references | Audit `docs/roadmap.md`, active `docs/plans/`, and `docs/design/` pages that still embed `register_worker(worker_id=...)`, literal `admins` / `orchestrators` ids, or mnemonic experiment ids in examples. Historical `docs/archive/` pages stay historical, but any current doc that links to them must make it explicit when the linked artifact is pre-rename. |

### 5.10 Conformance suite

[`conformance/scenarios/`](../../conformance/scenarios/) needs systematic update:

| Pattern | Change |
|---|---|
| Hardcoded `worker_id` strings (e.g., `"executor-host-1"`) | Replace with `worker_id = await harness.register_worker(name="executor-host-1")` — register-then-use. This applies both to the task-store scenarios and to the control-plane `test_deployment_scoped_registry.py` / lease / multi-experiment scenarios that currently seed typed deployment-scoped ids. |
| Hardcoded `group_id` strings (`"admins"`, `"orchestrators"`) | Resolve at fixture-setup time via `?name=` lookup; the test asserts existence + canonical name, not a literal id. This applies to both the per-experiment and deployment-scoped registry harnesses. |
| Reserved-value rejection tests | Update to expect rejection in name-space (`register_worker(name="admin")` → 422) rather than id-space. |
| Bearer-format assertion tests | Update grammar expectations: principal matches `wkr_*` or literal `"admin"`. |
| Conformance-citation tool ([`conformance/src/conformance/tools/check_citations.py`](../../conformance/src/conformance/tools/check_citations.py)) | No tool change; the §-citations in the test docstrings stay valid (chapter 02 §6 / §7 still exist post-rename — just with updated grammar). Verify after spec amendment that every cited section's MUST tokens survive. |
| Harness / adapter implementations ([`conformance/src/conformance/harness/`](../../conformance/src/conformance/harness/), [`conformance/src/conformance/adapters/`](../../conformance/src/conformance/adapters/)) | The reference task-store adapter, the control-plane harness client, and any helper that still posts `{worker_id, group_id}` bodies all ride the rename. Non-reference IUTs need to ship their own opaque-id minter on their side of the IUT contract. |

## 6. Backwards-compatibility posture

Per CLAUDE.md "Project Lifecycle" — EDEN is pre-external-user. **Clean break.**

+ **No dual-grammar transition period.** Schemas, models, conformance fixtures all switch in a single wave (with the spec amendment leading).
+ **No legacy-id acceptance.** The wire layer's grammar check rejects pre-rename kebab ids; there is no compat shim mapping `executor-host-1` → some auto-minted opaque id.
+ **Existing experiments are abandoned.** Any in-flight stack is re-bootstrapped via `setup-experiment.sh` after the rename lands. The `EDEN_EXPERIMENT_DATA_ROOT/<old-experiment-id>/` directory is cleaned up as part of the cutover (documented in the user-guide).
+ **Saved checkpoints from before the rename are NOT importable.** The chapter-10 portable checkpoint format already carries `experiment_id` in the manifest; pre-rename manifests fail validation against the new opaque grammar. No one-shot migration tool is in scope; the plan accepts the loss of any pre-rename saved state.
+ **Pre-rename WIP branches.** Any in-flight feature branch that uses the kebab-id grammar will hit merge conflicts on `02-data-model.md` §1 + schemas. The mitigation is to land the rename early in a quiet window and rebase open branches; this plan doesn't presume coordination on PR ordering beyond opening an early-warning issue at merge time.

The "first external user" threshold has not been crossed, so the operator pain of running setup-experiment once is acceptable.

**Exception note:** If a user reports a real need to import a pre-rename checkpoint between the time this plan lands and the time the chapter-10 wire ships the rename, file a separate follow-up. Don't pre-emptively build a migration tool.

## 7. Chunked execution plan

Five execution waves, but **one atomic rename PR**. The waves below are
review slices / commit boundaries inside that PR, not independently
merged PRs. That posture is load-bearing for rename discipline: the repo
must not sit on `main` in a multi-PR partial-rename state where spec,
schemas, wire, storage, control-plane, UI, and conformance disagree for
days. Every wave is still locally validated before push (the literal
commands in [`AGENTS.md`](../../AGENTS.md) "Commands" table, per the
CLAUDE.md "Commands are the literal pre-push validation gate" pitfall).

### Wave 1 — Spec amendments + grammar definition

PR shape: design-doc-shaped; no impl changes.

+ [`spec/v0/02-data-model.md`](../spec/v0/02-data-model.md) §1 — add opaque-id + display-name grammars (§4.2, §4.3 here).
+ Update §2 (experiment), §6 (worker), §7 (group) per §5.1.
+ Update [`spec/v0/04-task-protocol.md`](../spec/v0/04-task-protocol.md) §3-§7 prose (no normative behavior change — just identifier shape).
+ Update [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) §1.3 (path scoping), §6-§7 (register endpoints), §13 (bearer principal grammar), §14 (checkpoint id handling).
+ Update [`spec/v0/09-conformance.md`](../spec/v0/09-conformance.md) §4 prose for any §-citations that move.
+ Update [`spec/v0/10-checkpoints.md`](../spec/v0/10-checkpoints.md) §7 (URIs unchanged), §10 (import-provenance carries `source_experiment_id`).
+ Update [`docs/glossary.md`](../glossary.md) §1, §3.1, §8.
+ Update [`docs/prds/eden-experiment-platform.md`](../prds/eden-experiment-platform.md) §3 (resolve the open question).

**Validation gates:**

+ `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` — passes.
+ `python3 scripts/spec-xref-check.py` — every `§N.M` cross-reference still resolves.
+ Grep `^[a-z0-9]\[a-z0-9_-\]{0,63}` — the old kebab grammar appears nowhere outside `docs/archive/` and existing `docs/plans/` (archived plans don't ride the rename).

**Cascade:** Wave 1 alone does NOT unblock downstream identity work. The
cluster-identity dependents stay blocked until the full atomic rename PR
(through Wave 5) merges; no downstream issue should plan or land against
the half-renamed intermediate branch state.

### Wave 2 — JSON Schemas + Pydantic + storage backends in lockstep

PR shape: code-heavy; schema-parity tests are the validation backstop.

+ Update [`spec/v0/schemas/*.schema.json`](../spec/v0/schemas/) per §5.2, including `checkpoint-manifest.schema.json`, `lease.schema.json`, and the not-yet-authored control-plane wire / registry schemas needed to close the schema-parity loop.
+ Update [`reference/packages/eden-contracts/src/eden_contracts/_common.py`](../../reference/packages/eden-contracts/src/eden_contracts/_common.py) per §5.3 (new opaque-id type aliases; new `DisplayName` type).
+ Update all `eden-contracts` models per §5.3.
+ Update [`eden-storage/_schema.py`](../../reference/packages/eden-storage/src/eden_storage/_schema.py) + `_postgres_schema.py` per §5.4 — add `name` column to `experiment`, `worker`, `worker_group` tables; new indexes.
+ Update [`eden-storage/protocol.py`](../../reference/packages/eden-storage/src/eden_storage/protocol.py) signatures: `register_worker(name?, labels?) → WorkerRegistration` (minted id), `register_group(name?, members?) → GroupRegistration` (minted id). Implement opaque-id minting (ULID).
+ Reserved-name enforcement in storage: raise `ReservedIdentifier` for `register_worker(name in {"admin", "system", "internal"})` and `register_group(name in {"admins", "orchestrators"})` after setup-experiment's initial mint.
+ Update [`reference/packages/eden-wire/src/eden_wire/models.py`](../../reference/packages/eden-wire/src/eden_wire/models.py) and the control-plane Pydantic layer in [`reference/packages/eden-control-plane/src/eden_control_plane/models.py`](../../reference/packages/eden-control-plane/src/eden_control_plane/models.py) so every schema-touched rename field has its model-side pair in the same wave.
+ Update [`reference/packages/eden-control-plane/src/eden_control_plane/{store.py,memory.py,postgres.py}`](../../reference/packages/eden-control-plane/src/eden_control_plane/store.py) per §5.4 — deployment-scoped registry/lease backends add `name` columns/fields and opaque-id minting where required.

**Validation gates:**

+ `uv run pytest reference/packages/eden-contracts/tests/test_schema_parity.py` — schema ↔ model parity passes.
+ `uv run pytest reference/packages/eden-wire/tests/test_wire_schema_parity.py` — wire schema ↔ model parity passes.
+ `uv run pytest -q` — full reference suite passes (this is the big one; many tests reference the kebab grammar).
+ `pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json` — schemas valid.
+ `python3 scripts/check-rename-discipline.py` — passes (the rename-discipline script catches the specific legacy patterns; this wave will extend its allowlist or its baseline as the kebab grammar is retired).
+ `uv run pytest -q reference/packages/eden-control-plane/tests/test_models.py` — control-plane Pydantic / round-trip guards pass until the new schemas also gain parity tests.

**Cascade:** Wave 2 lands the data-shape change. After this merges, the wire surface (Wave 3) can build on the in-place storage shape.

### Wave 3 — Wire surface + auth principal grammar + setup-experiment

PR shape: medium-heavy; wires the new id-mint flow end-to-end.

+ Update [`eden-wire/server.py`](../../reference/packages/eden-wire/src/eden_wire/server.py) and [`eden-wire/client.py`](../../reference/packages/eden-wire/src/eden_wire/client.py): create endpoints drop the required `worker_id` / `group_id`; add `?name=` query support; return minted ids; client helpers ride the new request/response bodies.
+ Update [`eden-wire/auth.py`](../../reference/packages/eden-wire/src/eden_wire/auth.py): bearer principal grammar checks accept the literal `"admin"` or `wkr_*` opaque.
+ Update the control-plane wire stack — [`reference/packages/eden-control-plane/src/eden_control_plane/client.py`](../../reference/packages/eden-control-plane/src/eden_control_plane/client.py), [`reference/services/control-plane/src/eden_control_plane_server/{app.py,auth.py,cli.py}`](../../reference/services/control-plane/src/eden_control_plane_server/app.py), and [`reference/services/orchestrator/src/eden_orchestrator/control_plane_bootstrap.py`](../../reference/services/orchestrator/src/eden_orchestrator/control_plane_bootstrap.py) — to match the chapter-11 rename surfaces and the new control-plane experiment `name` field.
+ Update [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh): mint `exp_*` for the experiment, `wkr_*` for `operator` / `orchestrator` / `web-ui-1` / `ideator-host-1` / `executor-host-1` / `evaluator-host-1`, `grp_*` for `admins` / `orchestrators` (with names equal to the reserved literals). Write all minted ids to `.env`.
+ Update [`reference/compose/.env.example`](../../reference/compose/.env.example): the file now describes what setup-experiment will write rather than serving as a hand-edit template. Document the rename in a comment block at the top.
+ Update worker-host bootstrap services and CLIs to consume the opaque `EDEN_*_WORKER_ID` env vars unchanged in shape (the change is that the values are now opaque), including the control-plane bootstrap path.

**Validation gates:**

+ `uv run pytest -q` — full reference suite passes.
+ `uv run pytest -q reference/packages/eden-wire/tests reference/packages/eden-control-plane/tests reference/services/control-plane/tests` — task-store wire and control-plane wire/tests pass under the renamed shapes.
+ `bash reference/compose/healthcheck/smoke.sh` — Compose smoke passes (post-rename Forgejo repo path, post-rename worker registrations).
+ `bash reference/compose/healthcheck/smoke-checkpoint.sh` — checkpoint round-trip works under the new manifest shape; `imported_from.source_experiment_id` is stamped.
+ `bash reference/compose/healthcheck/e2e.sh` — end-to-end Web-UI walkthrough.

**Cascade:** Wave 3 lands the wire + bootstrap shape. After this merges, the UI rename (Wave 4) is unblocked because the data is in place.

### Wave 4 — Web UI + eden-manual + observability docs

PR shape: UI / docs-heavy.

+ Update [`reference/services/web-ui/`](../../reference/services/web-ui/) templates + route handlers per §5.6. Render `<name> (<id>)` everywhere; add name-search box to admin worker / group lists.
+ Update [`.claude/skills/eden-manual-*/SKILL.md`](../../.claude/skills/) per §5.7.
+ Update [`docs/user-guide.md`](../user-guide.md), [`docs/operations/initial-admin-credential.md`](../operations/initial-admin-credential.md), [`docs/operations/agent-readonly-db.md`](../operations/agent-readonly-db.md), [`docs/observability.md`](../observability.md) per §5.9.

**Validation gates:**

+ `uv run pytest -q reference/services/web-ui/tests/` — web-ui tests pass.
+ `bash reference/compose/healthcheck/e2e.sh` — UI walkthrough end-to-end.
+ Visual smoke: open the admin worker list locally, verify name-id rendering.

**Cascade:** Wave 4 lands the operator-facing UX inside the rename PR
branch. Downstream issues still stay blocked until Wave 5 closes
conformance and the full atomic rename PR merges.

### Wave 5 — Conformance scenarios + adapter

PR shape: tests-heavy.

+ Update [`conformance/scenarios/`](../../conformance/scenarios/) per §5.10. Replace hardcoded ids with register-then-use; update reserved-value rejection assertions; include the control-plane v1+multi-experiment scenarios (`test_experiment_registry.py`, `test_deployment_scoped_registry.py`, lease / multi-experiment scenarios) because they also assert renamed wire-visible ids.
+ Update [`conformance/src/conformance/adapters/reference/adapter.py`](../../conformance/src/conformance/adapters/reference/adapter.py) and [`conformance/src/conformance/harness/control_plane_client.py`](../../conformance/src/conformance/harness/control_plane_client.py) to ride the task-store and control-plane wire renames.
+ Update [`conformance/src/conformance/tools/check_citations.py`](../../conformance/src/conformance/tools/check_citations.py) — only if the spec amendment moved a §-citation (unlikely; the rename preserves section structure).

**Validation gates:**

+ `uv run pytest -q conformance/` — full suite passes against the reference impl.
+ `uv run python conformance/src/conformance/tools/check_citations.py` — every scenario cites a real spec section.

**Cascade:** Wave 5 closes the rename. Only after the full atomic rename
PR merges is the cluster-identity-foundation milestone complete; chunks
#140 / #141 / #143 / #144 then plan against the post-rename shape with
no mixed-name intermediate state left on `main`.

### Wave ordering rationale

Waves 1-2 are tightly coupled (spec + schemas + Pydantic are the
schema-parity-test-gated invariant); Wave 3 builds on the storage
shape; Wave 4 + Wave 5 can be reviewed in either order after Wave 3,
with the recommendation that Wave 4 lands first inside the branch so
the conformance updates in Wave 5 ride the validated wire shape.

The coupling is exactly why the merge unit is one PR, not five. A finer
split forces downstream work to depend on still-unmerged rename
surfaces; a coarser split at the review level makes the PR too hard to
reason about. The wave structure is the compromise: one atomic merge
unit, five internally reviewable slices.

## 8. Cascade dependents

Per the issue body's "Downstream consumers" section and cluster
`identity`:

| Issue | Unblocked after | What it builds on this plan |
|---|---|---|
| [#140](https://github.com/ealt/eden/issues/140) — Operator identity as a registered worker (Model B) | **After the atomic rename PR (Waves 1-5)** | The web UI / eden-manual sign-in flow needs a `worker_name` field distinct from `worker_id`; the operator types their preferred display name, the system mints their opaque id. |
| [#141](https://github.com/ealt/eden/issues/141) — Worker registry as deployment-level infrastructure | **After the atomic rename PR (Waves 1-5)** | Deployment-scoped registry needs opaque worker ids so that one human's `worker_id` doesn't depend on which experiment they registered against. The rename makes the deployment-scoped move clean. |
| [#143](https://github.com/ealt/eden/issues/143) — Web UI sign-ups non-admin by default | **After the atomic rename PR (Waves 1-5)**, sequenced after [#140](https://github.com/ealt/eden/issues/140) | The `admins` group's reserved name + opaque id is the basis for the admin-integration flow (operator integrates another operator by adding their `wkr_*` to the `admins` `grp_*`). |
| [#144](https://github.com/ealt/eden/issues/144) — `/admin/*` route admins-group enforcement | **After the atomic rename PR (Waves 1-5)** | The route guard checks membership in the reserved-name `admins` group (resolved at startup to the opaque `grp_*`); the rename is what makes the reserved-name lookup canonical. |

Downstream consumers that the issue body flags but that fall **outside cluster `identity`**:

+ Executor / group **picker UI** in ideator draft + admin `create-execution-task` + admin `reassign`. Unblocked by the merged rename PR; tracked in §11.6 as a downstream follow-up.
+ `list-workers` / `list-groups` **eden-manual subcommands** for terminal operators. Unblocked by the merged rename PR; tracked in §11.6 as a downstream follow-up.
+ Experiment-name display in `/admin/experiments/`. Already covered by Wave 4.
+ Renaming after create. Open design choice (yes/no); tracked in §11.2 if a real need emerges.

## 9. Conformance impact summary

The IUT contract (chapter 9 §6) is the wire — every IUT-observable
identifier flowing through the contract is now opaque. Specific §-cited
assertions in [`conformance/scenarios/`](../../conformance/scenarios/) that need adjustment:

| §-citation | Assertion shape today | Adjustment |
|---|---|---|
| `spec/v0/02-data-model.md §6.1` (worker grammar) | `register_worker(worker_id="...")` rejected for grammar violation. | New: `register_worker(name="...")` rejected for reserved-name + name-grammar violations; opaque `worker_id` minted on accept. |
| `spec/v0/02-data-model.md §7.3` (reserved groups) | `register_group(group_id="admins")` rejected. | `register_group(name="admins")` rejected after setup-experiment's initial mint (collision-with-reserved). |
| `spec/v0/04-task-protocol.md §3.5` (target enforcement) | Target id is operator-typed; check matches `claim.worker_id` against `target.id` literally. | Target id is opaque; check is unchanged in shape, the strings are now opaque. |
| `spec/v0/07-wire-protocol.md §13` (auth principal grammar) | Principal matches kebab grammar OR literal `"admin"`. | Principal matches `wkr_*` opaque grammar OR literal `"admin"`. |
| `spec/v0/10-checkpoints.md §10` (import provenance) | `imported_from` carries `{checkpoint_exported_at, checkpoint_format_version}`. | Add `source_experiment_id: opaque-id?`. New scenario: import without `as_experiment_id` mints a fresh `exp_*` and stamps the source for provenance; this is wire-observable via the import response and `read_experiment`, so it stays inside chapter 09 §6 scope. |

Conformance scenarios live in chapter 9 §5 groups; the rename keeps
groups intact. The CI citation-checker
(`conformance/src/conformance/tools/check_citations.py`) ensures every
scenario continues to cite a live MUST. Spec inter-chapter restatement
discipline (per the AGENTS.md pitfall): the worker / group / experiment
identifier MUST is canonically restated only in chapter 02; chapter 07
§13's restatement defers to chapter 02 §1.

## 10. Risks

| Risk | Mitigation |
|---|---|
| **Naming-bikeshed: prefix choice** (`exp_` vs `xp_` vs `experiment_`). | Section 4.1 commits to `exp_` / `wkr_` / `grp_`; the rationale is documented. If codex-review proposes different prefixes, surface as a decision request before Wave 1 PR. |
| **Naming-bikeshed: opaque-suffix format** (ULID vs UUIDv7 vs nanoid). | §4.1 commits to Crockford-base32 ULID. If review surfaces a strong UUIDv7 case (e.g., to align with chapter 11's existing UUID4 leases), reconsider — but the recommendation is to standardize new ids on ULID and let the existing UUIDs stay (no retroactive rename of `lease_id`). |
| **Migration mistakes — schema-parity gap.** A field gets the rename on one side (e.g., Pydantic) but not the other (JSON Schema), and the gap goes undetected. | The schema-parity test in [`eden-contracts/tests/test_schema_parity.py`](../../reference/packages/eden-contracts/tests/test_schema_parity.py) is the backstop. New tests under `tests/cases.py` add an opaque-id accept case and a kebab-id reject case for every renamed field. The CI parity job catches drift; the AGENTS.md "Adding or extending a JSON Schema + Pydantic binding" discipline is re-read during Wave 2. |
| **Wave-2 storage migration mistakes** — column adds but indexes forgotten; the `?name=` query becomes O(N) under load. | Wave 2 explicitly enumerates the index changes; reviewers check the DDL changes match the index list in §5.4. The smoke scripts exercise the registry endpoints, but they don't load-test — the v0 indexes are precautionary for the post-rename steady state. |
| **Operator UX regression** — `EDEN_EXPERIMENT_ID` was a mnemonic operators could remember; opaque ids in path segments aren't memorable. | User-guide §6 documents the `?name=` lookup pattern. The CLI gets a `--name` flag where operators can refer to entities by their display name. Operators who want to bookmark URLs can use the name-resolved redirect (`GET /experiments?name=X` returning the canonical opaque-id URL). |
| **Setup-experiment idempotency under re-run** — running setup-experiment a second time today is idempotent on `experiment_id`; minting opaque ids breaks idempotency without care. | Setup-experiment writes the minted ids to `.env`; on re-run, it reads existing ids and re-uses them. The "fresh experiment" path is via the documented `wipe + setup-experiment` flow ([`docs/operations/experiment-data-durability.md`](../operations/experiment-data-durability.md)). The eden-manual-experiment skill's "fresh experiment" recipe rides this. The same rule applies to the control-plane bootstrap rows keyed by the opaque ids. |
| **Pre-rename WIP feature branches** rebase pain. | Land the rename in a quiet window (no other multi-branch work in flight); the day-of-merge announcement lists which open branches need to rebase. |
| **Chapter 09 §6 IUT-contract over-promise** — the rename is wire-observable, but the conformance-suite scope is bounded. | Per the AGENTS.md "Conformance-plan MUSTs must be filtered through the IUT contract" pitfall, every spec MUST in §5.1 is checked against the chapter 09 §6 IUT contract before drafting a conformance scenario in Wave 5. Off-wire MUSTs (none expected here — the rename is wire-shaped) stay out of scope. |
| **Spec inter-chapter restatement drift** (per AGENTS.md pitfall) — chapter 02 §6 and chapter 07 §13 both currently restate the worker-id grammar, and chapter 03 / chapter 08 / chapter 11 also restate the identity-bearing surfaces. | Wave 1 grep-checks every restatement chapter: chapter 02 §1 is the canonical grammar source; chapters 03, 04, 07, 08, 09, and 11 defer to it or update their inline restatements in the same PR. |
| **Setup-experiment seed-time race** — minting opaque ids for `admins` / `orchestrators` groups before workers exist is fine, but the rename PR has to atomically update the seed-order (group create → worker register → membership add). | Setup-experiment's existing seed-order is preserved; only the create-call shapes change. The smoke script exercises this. |
| **The "schedule wipe" risk** — `EDEN_EXPERIMENT_DATA_ROOT/<old-id>/` directories abandoned in operators' filesystems. | User-guide documents the cleanup recipe (`rm -rf "$EDEN_EXPERIMENT_DATA_ROOT/manual-ui"`). Setup-experiment refuses to mint over an existing data root with the new opaque id (defensive). |

## 11. Open questions

This section is the tracking bucket for every deferred / follow-up item
called out earlier in the plan. The recommendation is to resolve the
decision items before Wave 1 starts and to keep the downstream
follow-ups here until they have their own issue / plan.

1. **Should `name` be unique within an entity-kind per experiment?**
   Recommendation: **no** (issue body's explicit position). Soft-check
   (warn-on-collision) is a polish follow-up post-rename; tracked here
   until it gets its own issue, using the slug-soft-check pattern from
   [#121](https://github.com/ealt/eden/issues/121).
2. **Should `name` be mutable after create?** Recommendation: **defer**;
   tracked here as a downstream follow-up. The rename plan doesn't
   require an answer.
3. **Opaque-id suffix format — ULID vs UUIDv7?** Recommendation: **ULID**
   per §4.1; document the rationale in the spec amendment.
4. **Should `experiment_id` be globally unique across all deployments
   (federation), or per-deployment?** Recommendation: **per-deployment**;
   the chapter 11 control plane scopes ids to one control-plane instance.
   Cross-deployment federation is explicitly out of scope.
5. **What does the wire return when `register_worker` is called with
   `name == "admin"`?** Recommendation: 422
   `eden://error/reserved-name`. Document the error vocabulary in
   chapter 07 §13.
6. **Downstream picker / convenience-list surfaces.** The executor/group
   picker UI and the `list-workers` / `list-groups` terminal helpers are
   intentionally out of scope for this rename PR. They are tracked here
   (and in §8) until separate issues are filed after the cutover lands.
7. **Emergency pre-rename checkpoint import rescue.** If a real operator
   need appears before the cutover is complete, track the exception here
   rather than silently expanding the rename PR to include migration
   tooling.

If any of these questions resolves differently after operator review,
update the plan body before Wave 1 starts. The plan-stage PR is the
forum for that resolution.

## 12. Plan-stage validation gates

Before this plan PR merges:

+ `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` — passes.
+ `python3 scripts/spec-xref-check.py` — passes (the plan cross-references chapter §s; the script's scope is `spec/v0/`, but the principle is the same — verify each cited §-ref resolves).
+ Codex-review (3-5 rounds) confirms:
  + No missed entities in §3 (the survey is exhaustive).
  + No missed surfaces in §5 (every consumer of the renamed fields is enumerated).
  + No schema-parity gaps (every renamed field is paired across spec + schema + Pydantic + storage).
  + No spec inter-chapter restatement drift (worker / group / experiment grammar canonically restated only in chapter 02 §1).
  + Risk register (§10) accounts for the AGENTS.md plan-writing pitfalls.

## 13. Stop conditions

+ **Naming-model decision deadlocks** (option A vs B vs C, prefix choice, or suffix format): surface for operator decision before finalizing.
+ **Codex-review hits 6+ rounds**: surface to operator; consider scoping smaller (e.g., spinning the experiment rename out of cluster `identity` and into its own subsequent chunk, tracked in §11 before execution resumes).
+ **Cascade dependency surprises**: if a downstream cluster-identity issue (#140 / #141 / #143 / #144) reveals a shape this plan doesn't accommodate, pause Wave 1 and amend.

---

This plan is implementation-agnostic — it specifies what changes and where, not the line-by-line edits. Each wave's PR carries its own chunked impl plan (or, for the smaller waves, executes directly against this plan's §5 mapping). The operator approves this plan before any wave's impl starts.
