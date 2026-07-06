# EDEN architecture overview

A newcomer-facing map of how EDEN is designed, how the pieces fit together, and what actually runs in a deployment. Nothing here is normative — the spec chapters under [`spec/v0/`](../spec/v0/) win on any disagreement. For vocabulary, see [`glossary.md`](glossary.md); for hands-on operation, see [`user-guide.md`](user-guide.md).

## The big idea

EDEN orchestrates **directed evolution** of code: the iterative, machine-driven generation, implementation, and evaluation of code changes against a declared objective. The loop mirrors the lab technique (diversify → screen → amplify):

1. An **ideator** (human, AI, or hybrid) reads experiment state and drafts **ideas** — each with a slug, priority, and the parent commits it builds on.
2. An **executor** implements one idea as a **variant**: real commits on an isolated `work/*` branch.
3. An **evaluator** scores the variant against the experiment's declared, typed **evaluation schema**.
4. The **integrator** squashes each successful variant into the canonical `variant/*` lineage, embedding its evaluation manifest.
5. Results flow back to the ideator; the **orchestrator** turns the crank.

The orchestrator runs five decision types per iteration — termination, ideation-task creation, execution dispatch, evaluation dispatch, integration — each independently switchable between `auto` and `manual` via the experiment's `dispatch_mode`. That switch lets one protocol serve fully autonomous experiments, fully human-driven ones (through the web UI), or any hybrid.

## Three artifacts, three disciplines

The repo shape (inspired by OpenAI's [Symphony](https://github.com/openai/symphony)) pairs a normative spec with one reference implementation and a black-box conformance suite:

| Layer | Path | Discipline |
|---|---|---|
| **Specification** | [`spec/v0/`](../spec/v0/) | RFC-style, versioned lineage: twelve chapters (00–11) + 12 entity and 20 wire JSON Schemas. Additive/clarifying changes only within v0. |
| **Reference implementation** | [`reference/`](../reference/) | Normal code review. Explicitly *one* valid implementation, not *the* implementation. |
| **Conformance suite** | [`conformance/`](../conformance/) | Black-box: drives an implementation-under-test purely through the chapter-7 HTTP binding; every test cites the spec MUST it asserts. |

Why a protocol rather than a product: strong contracts make modular, independently-developed components easy; the spec doubles as the natural-language explanation of the project and as the alignment mechanism for coding agents; conforming components are interchangeable; and outsiders can adopt either the working reference or the validated idea.

## Core data model ([spec ch. 02](../spec/v0/02-data-model.md))

| Entity | What it is | Lifecycle |
|---|---|---|
| `Experiment` | The declared search: `parallel_variants`, typed `evaluation_schema`, `objective` (expr + direction), `dispatch_mode`, ideation/termination policy blocks. | `running → terminated` (one-way in v0) |
| `Idea` | A proposed change: slug, priority, `parent_commits` (≥1 — merge ideas are legal), artifacts, optional routing hints. | `drafting → ready → dispatched → completed` |
| `Variant` | One attempt at an idea: `branch`, work-tip `commit_sha`, evaluation results, canonical `variant_commit_sha` once integrated. `kind="baseline"` makes the seed itself evaluatable. | `starting → success \| error \| evaluation_error` |
| `Task` | The dispatched unit of work (`ideation` / `execution` / `evaluation`), with kind-specific payload, optional target, and a claim. | see task protocol below |
| `Event` | Append-only log entry from a closed v0 registry; replaying the log reconstructs every entity's lifecycle. | immutable, totally ordered per experiment |
| `Worker` / `Group` | Per-experiment identity registry; argon2id-hashed credentials; transitive (cycle-checked) groups. Reserved names: `admins`, `orchestrators`. | registered once; credentials reissuable |

Identity-carrying entities use opaque, system-minted, typed-prefix ids (`exp_*`, `wkr_*`, `grp_*`; Crockford base32) with optional display names that are never resolved as references.

## Roles ([spec ch. 03](../spec/v0/03-roles.md))

Naming is verb-noun-coherent (see [`glossary.md`](glossary.md)): role *-or*, gerund task kind, artifact noun. Worker roles share one outer lifecycle — discover → claim → execute → submit → release-on-reclaim — and the spec constrains only their *observable effects*, never their hosting or algorithms.

| Role | Consumes | Produces | Failure vocabulary |
|---|---|---|---|
| Ideator | `ideation` task | ≥ 0 ideas | `success` / `error` |
| Executor | `execution` task | variant + `work/*` commits | `success` / `error` (no-op trees rejected) |
| Evaluator | `evaluation` task | schema-validated metrics | `success` / `error` / `evaluation_error` (retryable) |
| Integrator | `variant.succeeded` | canonical `variant/*` commit | compensating-delete ladder; never "submits" |
| Orchestrator | experiment state | five decisions per iteration | a role, not a singleton — N replicas are safe |

## Task protocol ([spec ch. 04](../spec/v0/04-task-protocol.md)–[05](../spec/v0/05-event-protocol.md))

```text
pending → claimed → submitted → completed
   ↑         │           │    ↘ failed
   └─reclaim─┴───────────┘  (accept / reject by the orchestrator)
```

- **Claims are single-winner and identity-keyed**: submit must come from the authenticated claimant (there is no per-claim token). Expired or stranded claims are reclaimed back to `pending`; `submitted` tasks only by explicit operator action.
- **Resubmits are idempotent** when content-equivalent, rejected when divergent.
- **The transactional event invariant** is the protocol's central guarantee: every observable state change commits atomically with its event — both durable or neither. Multi-entity effects are *composite commits* that subscribers observe all-or-none. Delivery is at-least-once, totally ordered per experiment, and replayable for the experiment's lifetime.

## Git topology ([spec ch. 06](../spec/v0/06-integrator.md))

- `main` — the immutable seed.
- `work/*` — per-attempt executor branches; inputs, not normative outputs; deletable after integration.
- `variant/*` — the canonical lineage. Integrator-only, one squashed commit per integrated variant, never rewritten. Each commit has the worker tip's exact tree plus exactly one added path — `.eden/variants/<id>/evaluation.json`, the evaluation manifest — and the idea's `parent_commits` as parents.

Integration atomically couples three artifacts: the git ref, the store's `variant_commit_sha`, and the `variant.integrated` event. Because remote pushes can fail indeterminately, the spec pins a compensating-delete ladder with read-back disambiguation (rationale: [`design-notes/integrator-atomicity.md`](../spec/v0/design-notes/integrator-atomicity.md)). In the reference deployment the remote of record is an in-network **Forgejo**; every service works from a private clone that re-syncs immediately before each ref-sensitive operation.

## Wire and storage ([spec ch. 07](../spec/v0/07-wire-protocol.md)–[08](../spec/v0/08-storage.md))

Chapter 7 binds everything to HTTP/1.1 + JSON under `/v0/experiments/{E}/…`: task lifecycle ops, ideas, variants, workers/groups, event read-range + long-poll subscribe, artifact deposit/fetch, checkpoint export/import, and the control-plane surface. Errors are RFC 7807 `problem+json` with a **closed** `eden://error/…` vocabulary — clients key on the type URI, not the status code. Auth is a bearer scheme with two principals: the deployment `admin` token and per-worker `<worker_id>:<secret>` credentials, with group-gated authority for orchestrator- and admin-only operations.

Chapter 8 specifies the three stores — task store, event log, artifact store — behaviorally (atomic single-winner claims, no-TOCTOU submits, fsync-grade durability, read-after-write), never mechanically. Two later chapters extend the surface:

- **Portable checkpoints** ([ch. 10](../spec/v0/10-checkpoints.md)): a self-describing tar (manifest + JSONL tables + git bundle + content-addressed artifacts) any conforming implementation can import to resume an experiment.
- **Control plane** ([ch. 11](../spec/v0/11-control-plane.md)): an optional deployment-level experiment registry plus per-experiment leases so N orchestrator replicas can safely share M experiments (at-most-one holder, holder-instance fencing, pull-only state sync).

## The deployed stack ([`reference/`](../reference/))

```text
humans/agents   web-ui (BFF + /admin)      eden-manual CLI        control-plane (ch. 11)
                     │                          │                      │
decision loop   orchestrator ──── ideator-host / executor-host / evaluator-host
                     │            (scripted · subprocess · docker sibling)
                     │                          │
state of record task-store-server (Store: postgres · sqlite · :memory:)
                     │  ├── blob artifact backend (file · s3 · gcs)
                     │  └── checkpoint export/import
                forgejo (git remote of record: work/* + variant/*)
```

Every arrow between EDEN services is the chapter-7 HTTP binding with per-worker bearer credentials; git flows through Forgejo over HTTP with a credential helper.

**Worker hosts run in three execution modes**: `--mode scripted` (deterministic built-ins), `--mode subprocess` (user-supplied `ideation_command` / `execution_command` / `evaluation_command` per the [worker-host-subprocess binding](../spec/v0/reference-bindings/worker-host-subprocess.md) — the ideator is long-running with a JSON-line protocol so an LLM session can persist across tasks; executor/evaluator spawn per-task in a git worktree), and `--exec-mode docker` (each spawn isolated in a sibling container via DooD).

**Two first-class substrates, one bootstrap idiom** (probe, create-if-absent, converge on re-run):

- **Compose** — [`setup-experiment.sh`](../reference/scripts/setup-experiment/) → `docker compose up`, with opt-in overlays: subprocess workers, DooD isolation (the only overlay that mounts the docker socket), Loki + Grafana log search, multi-orchestrator, lease-mode multi-experiment. Durable state lives in host bind-mounts under `~/.eden/experiments/<id>/`.
- **Helm** — [`setup-experiment-helm.sh`](../reference/scripts/setup-experiment-helm.sh) + the chart at [`reference/helm/eden/`](../reference/helm/eden/) for Kubernetes 1.27+, with embedded or managed Postgres and file/S3/GCS blob modes; [`setup-aws.sh`](../reference/scripts/setup-aws/) provisions EKS/ECR/RDS/S3+IRSA idempotently.

Roughly 26 CI jobs guard all of this — lint/typecheck/tests, schema ↔ model parity, spec cross-reference checks, a complexity gate, rename discipline, the conformance suite, nine Compose smokes and four Helm jobs asserting wire-observable end-states.

## Conformance ([spec ch. 09](../spec/v0/09-conformance.md))

The unit of conformance is a whole implementation-under-test exposing the chapter-7 binding — the suite relies on nothing else, asserts MUSTs only, and a claim must name its level:

`v1` → `v1+roles` → `v1+roles+integrator`, plus the parallel `v1+checkpoints` and `v1+multi-experiment` levels. The reference implementation passes every shipped level (~270 scenarios) and holds no privileged status for doing so.

## Current status

See the top of [`CHANGELOG.md`](../CHANGELOG.md) for the in-flight chunk, [`roadmap.md`](roadmap.md) for per-phase status, and the issue tracker for open threads. As of mid-2026: Phases 0–12 complete; Phase 13 (Kubernetes) in progress with 13e (Forgejo hardening) and 13f (k8s-native worker modes) remaining.
