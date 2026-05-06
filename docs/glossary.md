# EDEN glossary

A canonical list of the terms-of-art used in the EDEN protocol, the
reference implementation, and surrounding design discussions.

The glossary is organized by **kind of thing**, not alphabetically,
because the relationships between terms are part of the meaning.

EDEN is a protocol for **directed evolution** — iterative refinement
of any artifact that can be versioned in git (code, recipes, prompts,
configs, prose, etc.). The vocabulary below uses the four parallel
``-or`` role names (ideator, executor, evaluator, integrator), the
matching artifact nouns (idea, variant, evaluation), and gerund task
kinds (`ideation` / `execution` / `evaluation`).

---

## 1. Worker roles

A **worker** is any actor that participates in the task protocol as a
consumer — claims tasks, does work, submits results. The protocol
defines four worker contracts. Workers MAY be human, agentic
(LLM-driven), or programmatic; the contracts constrain only the
observable effects.

| Term | One-line meaning | Spec ref |
|---|---|---|
| **ideator** | Proposes what to try; output is one or more *ideas* | [`01-concepts.md`](../spec/v0/01-concepts.md) §2.1 |
| **executor** | Turns an idea into git commit(s) on a per-variant *work branch* | §2.2 |
| **evaluator** | Measures an executed variant against the experiment's evaluation schema | §2.3 |
| **integrator** | Promotes a successful variant to the canonical *variant lineage* (writes a `variant/*` commit) | §2.4 |

The integrator is the only worker role with a fixed identity (in the
current spec there is exactly one logical integrator per experiment;
the role contract makes it the sole writer of `variant/*`). Ideator /
executor / evaluator are role *contracts* — many concurrent
workers may claim the same kind of task.

## 2. System actors and components

| Term | One-line meaning | Spec ref |
|---|---|---|
| **orchestrator** | Dispatches tasks and advances the protocol's state machine in response to submissions. No unique authority beyond what the protocol grants. May run with multiple cooperating instances. | [`01-concepts.md`](../spec/v0/01-concepts.md) §11 |
| **auto-orchestrator** | Informal: the automated polling-loop instance of the orchestrator role (`reference/services/orchestrator/`). Distinct from "the orchestrator role" — a deployment may run zero, one, or many. | (informal) |
| **operator** | Human running a deployment, distinct from worker roles. The operator runs `setup-experiment`, brings up the stack, performs admin actions like `reclaim`, etc. | (informal; not normative) |
| **owner** | The authority over an experiment — can configure, terminate, reassign, transfer. Today implicit; should be a first-class concept once authentication and authorization land. | (forward-looking) |
| **observer** / **subscriber** | Read-only consumer of the event log (UIs, dashboards, downstream tooling). Distinct from workers; has no claim/submit capability. | (informal) |

## 3. Data shapes (value objects)

| Term | One-line meaning | Spec ref / schema |
|---|---|---|
| **experiment** | A single directed-evolution run (id + config + starting commit) | [`01`](../spec/v0/01-concepts.md) §1 / [`02`](../spec/v0/02-data-model.md) |
| **idea** | The ideator's output: a description of a change to attempt + dispatch metadata | [`01`](../spec/v0/01-concepts.md) §3 / [`schemas/idea.schema.json`](../spec/v0/schemas/idea.schema.json) |
| **variant** | One attempt to improve the objective; references the idea, commits, evaluation, and status | [`01`](../spec/v0/01-concepts.md) §4 / [`schemas/variant.schema.json`](../spec/v0/schemas/variant.schema.json) |
| **task** | A unit of work dispatched to a worker (kind, payload, state, claim) | [`01`](../spec/v0/01-concepts.md) §5 / [`schemas/task.schema.json`](../spec/v0/schemas/task.schema.json) |
| **event** | An immutable record of a state change, appended to the event log | [`01`](../spec/v0/01-concepts.md) §6 / [`schemas/event.schema.json`](../spec/v0/schemas/event.schema.json) |
| **submission** | The role-specific payload a worker hands back when completing a task. Three shapes: `IdeaSubmission`, `VariantSubmission`, `EvaluationSubmission`. | [`03-roles.md`](../spec/v0/03-roles.md) §§2.4 / 3.4 / 4.4 |
| **claim** | A worker's hold on a task; the task store records the worker_id and an expiry. | [`01`](../spec/v0/01-concepts.md) §8 |
| **claim token** | The opaque secret returned to a worker when it claims; required to submit. | [`01`](../spec/v0/01-concepts.md) §8 |

### 3.1 Sub-fields worth naming

| Term | What it is |
|---|---|
| **slug** | An idea's short kebab-case label; matches `^[a-z0-9][a-z0-9-]*$`. Used in branch naming. |
| **priority** | Per-idea ordering hint (number; "higher dispatches earlier" — currently SHOULD-level, not enforced). |
| **parent_commits** | One or more commit SHAs an idea/variant is based on. |
| **artifacts_uri** | URI pointing at idea-rationale or evaluator artifacts (typically `file://` in the reference impl). |
| **kind** | A task's role-routing label (`ideation` / `execution` / `evaluation`). |
| **payload** | A task's role-specific inner content. |
| **commit_sha** | The worker's tip commit on its `work/*` branch (set on the variant when the executor submits). |
| **variant_commit_sha** | The squashed-and-integrated commit on `variant/*` (set by the integrator). |
| **branch** | The canonical work branch name (`work/<slug>-<variant_id>`); set on the variant at create time. |
| **evaluation** | A dict shaped per the experiment's `evaluation_schema`. |

### 3.2 Task kinds

A task's `kind` field discriminates which role contract claims it,
what its payload looks like, and what shape its submission takes. The
three kinds are noun forms of the role actions:

| `kind` | Claimed by | Payload | Submission shape | On `accept`, the store… |
|---|---|---|---|---|
| `ideation` | ideator | `{experiment_id}` | `IdeaSubmission(status, idea_ids: tuple)` | Marks the task `completed`. The referenced ideas (which the ideator moved to `state="ready"` before submit) are then dispatched by the orchestrator as `execution` tasks. |
| `execution` | executor | `{experiment_id, idea_id}` | `VariantSubmission(status, variant_id, commit_sha?)` | On `success`: writes `commit_sha` onto the referenced variant; variant.status remains `starting`. The orchestrator then dispatches an `evaluation` task referencing the variant. On `error`: variant transitions to `error`. |
| `evaluation` | evaluator | `{experiment_id, variant_id}` | `EvaluationSubmission(status, variant_id, evaluation?, artifacts_uri?)` | On `success`: variant transitions to `success` with the submitted evaluation. The integrator then promotes it (writes `variant_commit_sha`). On `error`: variant transitions to `error`. On `evaluation_error`: variant stays in `starting` (the evaluator didn't form a verdict; the variant remains evaluable). |

The full state-machine semantics — including reject paths,
validation_error handling, and idempotent resubmit — live in
[`spec/v0/04-task-protocol.md`](../spec/v0/04-task-protocol.md) §4.
This table is the at-a-glance summary of the happy path.

A few observations the table makes visible that aren't always obvious:

- The **ideator doesn't directly produce variants**; it produces
  ideas, and the *orchestrator* (in its dispatch role) creates
  the execution tasks that produce variants. The hand-off shape is
  ready idea → execution task → variant.
- A variant's status transitions from `starting` to its terminal
  status (`success` / `error` / `evaluation_error`) at the *evaluator's*
  submission, not the executor's. The executor's submission
  only sets `commit_sha`; the variant stays `starting` between
  the execution-task submission and the evaluation-task submission.
- `evaluation_error` is the only status where the variant doesn't
  terminalize. The evaluator declared the variant unevaluable but
  didn't condemn it; another evaluator (or the same one with more
  context) MAY produce a verdict later. (See
  [`spec/v0/03-roles.md`](../spec/v0/03-roles.md) §4.4.)

## 4. Lifecycle vocabulary (states, statuses, transitions)

EDEN deliberately uses different words for different lifecycle
machines, even when conceptually similar. Worth knowing the mapping:

| Object | Field | Possible values |
|---|---|---|
| Task | `state` | `pending` → `claimed` → `submitted` → `completed` (or `failed`) |
| Idea | `state` | `drafting` → `ready` → `dispatched` → `completed` |
| Variant | `status` | `starting` → `success` (or `error`, `evaluation_error`) |
| IdeaSubmission | `status` | `success`, `error` |
| VariantSubmission | `status` | `success`, `error` |
| EvaluationSubmission | `status` | `success`, `error`, `evaluation_error` |

**state vs. status** is intentional: tasks/ideas use `state` (the
protocol's lifecycle); variants and submissions use `status`
(role-reported outcome). Referring to a "task status" is a category
error.

### 4.1 Verbs

| Term | What it does |
|---|---|
| **claim** (verb) | Reserve a task for a worker; returns a token. |
| **submit** | Worker delivers a submission for a claimed task. |
| **accept** | Orchestrator validates a submission and writes it as committed; task → `completed`. |
| **reject** | Orchestrator rejects a submission; task → `failed`. Reasons include `worker_error` and `validation_error`. |
| **reclaim** | Operator (or sweeper) revokes a claim, returning the task to `pending`. |
| **dispatch** | Orchestrator creates a downstream task from a state transition (e.g. ready idea → execution task; success-with-commit_sha variant → evaluation task). |
| **integrate** | Integrator squashes a successful variant's `work/*` content into a single commit on `variant/*`, attaches the evaluation manifest, and emits `variant.integrated`. |

## 5. Storage components

| Term | One-line meaning | Spec ref |
|---|---|---|
| **task store** | Durable store of tasks, ideas, variants, submissions; provides atomic claim and idempotent submit | [`08-storage.md`](../spec/v0/08-storage.md) |
| **event log** | Append-only ordered log of events; provides replay and subscribe | [`05-event-protocol.md`](../spec/v0/05-event-protocol.md) |
| **artifact store** | Holds rationale documents, evaluator artifacts, etc., addressed by URI | [`08-storage.md`](../spec/v0/08-storage.md) §5 (deferred) |

In the reference impl, all three are backed by Postgres + the
deployment filesystem; the protocol abstracts over the choice.

## 6. Git topology

EDEN maintains three branch namespaces in the experiment's git repo:

| Namespace | What lives there | Writer |
|---|---|---|
| **`main`** | The experiment's starting point. Immutable during an experiment. | Set once at `setup-experiment` time. |
| **`work/*`** | Per-attempt executor branches; named `work/<slug>-<variant_id>`. | Executors (concurrent writes allowed). |
| **`variant/*`** | The canonical lineage; one commit per successfully integrated variant. | Integrator only. |

| Term | What it is |
|---|---|
| **seed commit** / **base commit** | The single commit on `main` at experiment start. Captured as `EDEN_BASE_COMMIT_SHA` in deployment env. |
| **work branch** | A branch in the `work/*` namespace; the executor's tip commit lives here. |
| **variant branch** | A branch in the `variant/*` namespace; the integrator-produced squash commit. |
| **evaluation manifest** | A JSON file at `.eden/variants/<variant_id>/evaluation.json` in the `variant/*` commit's tree, containing the evaluator's evaluation. Spec-authoritative path. |
| **bare repo** | The git repository hosted on the workers' git remote of record (Gitea in the reference deployment). |

## 7. Protocol / spec terms

| Term | What it is |
|---|---|
| **spec** | The normative protocol specification under [`spec/v0/`](../spec/v0/). |
| **chapter** | Numbered file under `spec/v0/` (e.g., `04-task-protocol.md`). Numbering is stable within a spec version. |
| **normative** | Spec text that uses MUST/SHOULD/MAY (RFC 2119); a conforming implementation must obey. |
| **informative** | Spec text that's expository or motivating; a conforming implementation may ignore. |
| **schema** | A JSON Schema definition under `spec/v0/schemas/`. The wire format. |
| **Pydantic model** | The Python-side validating mirror of a schema, in `eden-contracts`. |
| **wire** / **wire protocol** | The HTTP binding of the task/event/integrator/storage operations. [`07-wire-protocol.md`](../spec/v0/07-wire-protocol.md). |
| **reference binding** | A non-normative companion doc describing one of several valid bindings (e.g., the subprocess binding for worker hosts). [`spec/v0/reference-bindings/`](../spec/v0/reference-bindings/). |
| **reference implementation** / **reference impl** | The Python implementation under [`reference/`](../reference/). One conforming impl among possibly many. |
| **conformance suite** | The implementation-agnostic test suite under [`conformance/`](../conformance/) that asserts spec invariants. |
| **conformance scenario** | One test in the conformance suite (`conformance/scenarios/test_*.py`); each cites the spec MUST/SHOULD it exercises. |
| **conformance group** | Index grouping in chapter 9 §5 used to organize scenarios. |
| **role binding** | How a role is hosted (subprocess, HTTP service, in-process adapter, human at a keyboard). Currently informal; deferred to a future spec chapter. |

## 8. Operational vocabulary

| Term | What it is |
|---|---|
| **deployment** | One running instance of the EDEN stack (typically one Compose project, one experiment). |
| **experiment id** | String identifying an experiment. Operator-supplied at `setup-experiment` time. |
| **worker id** | String identifying a worker instance. Today: ad-hoc; supplied via `--worker-id` CLI flag. |
| **shared token** | The deployment-wide bearer token used for wire auth (informally: every worker authenticates as "the deployment", not as a specific worker). |
| **iteration** (orchestrator) | One pass through the orchestrator's loop body (finalize submitted, dispatch execution, dispatch evaluation, finalize submitted, integrate successful). |
| **quiescence** | Heuristic in the current orchestrator: N consecutive iterations with no progress → exit. Not spec-defined. |
| **checkpoint** | Snapshot of an experiment's state for save/restore. Implementation-specific in the reference impl today. |
| **manifest** (in setup-experiment context) | A `.env` + `experiment-config.yaml` + gitea credential helper produced for one experiment. Different from "evaluation manifest" above. |

## 9. Build / packaging vocabulary

| Term | What it is |
|---|---|
| **uv workspace** | The Python-side monorepo layout under [`reference/`](../reference/), with `pyproject.toml` declaring the workspace members. |
| **package** | A library member of the uv workspace under `reference/packages/eden-*` (e.g., `eden-contracts`, `eden-storage`). |
| **service** | A runnable application under `reference/services/*` (e.g., `task-store-server`, `web-ui`). |
| **eden-reference:dev** | The shared docker image all reference services run from (multi-stage `uv sync --frozen --no-dev --all-packages`). |
| **eden-runtime:dev** | A smaller image used as the default sibling-container target in docker-exec mode. |
| **subprocess mode** | Worker host config where the user's `*_command` runs as a child process of the host. |
| **docker-exec mode** | Worker host config where the user's `*_command` runs in a sibling container via DooD. |
