# Core Concepts

This chapter introduces the vocabulary used throughout the specification. It defines each concept at the level of *what it is* and *what role it plays in the system*, not *how it behaves over time*. Behavioral contracts — state machines, transactional guarantees, claim semantics — are defined in subsequent chapters.

## 1. Experiment

An **experiment** is a single directed-code-evolution run. It has:

- A starting commit on a designated branch of a git repository.
- An **objective** — a scalar function of a trial's measured metrics, to be either maximized or minimized.
- A declared **metrics schema** — the names and types of the metrics evaluators will report.
- Role configurations — how to run the planner, implementer, and evaluator for this experiment.
- Termination conditions (trial-count, wall-clock, convergence target).

The experiment's full configuration is specified by an **experiment-config** object (Chapter [`02-data-model.md`](02-data-model.md); schema: [`schemas/experiment-config.schema.json`](schemas/experiment-config.schema.json)).

## 2. Roles

EDEN defines four role contracts. A conforming implementation of a role MUST honor the protocol surface defined for that role in Chapter [`03-roles.md`](03-roles.md). Within this chapter the roles are introduced at the concept level only.

### 2.1 Planner

The **planner** proposes what to try. Its output is a **proposal** — a document describing a change to attempt, along with metadata (parent commits, priority, artifacts) needed to execute it. Planners are free in their internal strategy (LLM, search, human); the protocol constrains only what a proposal looks like and when one is considered ready.

### 2.2 Implementer

The **implementer** turns a proposal into a working-tree change. It consumes a proposal, produces a commit (or commit sequence) on a per-trial branch, and signals completion. It does **not** judge whether the change is good — it only reifies the plan.

### 2.3 Evaluator

The **evaluator** measures an implemented trial against the experiment's metrics schema. Its output is a set of metrics plus a status indicator (success, error). The evaluator MUST NOT modify the trial branch; it observes only.

### 2.4 Integrator

The **integrator** decides how evaluated trials enter the canonical trial lineage. It squashes a per-trial worker branch into a trial-shaped commit on a canonical branch, attaches an eval manifest, and exposes the resulting commit to downstream consumers. The integrator is the sole writer of the canonical lineage.

## 3. Proposal

A **proposal** is the planner's output: a human-readable description of a change plus the metadata required to dispatch work against it. Schema: [`schemas/proposal.schema.json`](schemas/proposal.schema.json). Proposals have a lifecycle (drafting → ready → dispatched → completed) that is defined in [`04-task-protocol.md`](04-task-protocol.md). This chapter treats a proposal as a value object; the task protocol defines how its lifecycle advances.

## 4. Trial

A **trial** is a single attempt to improve the objective. A trial references:

- The proposal that was implemented.
- The resulting commit(s) on the worker branch and the squashed commit on the canonical branch.
- The metrics reported by the evaluator.
- A status (starting, success, error, eval_error).

Trials are the unit of progress: a terminated experiment is summarized by the sequence of its completed trials and their scores under the objective. Schema: [`schemas/trial.schema.json`](schemas/trial.schema.json).

## 5. Task

A **task** is a unit of work dispatched to a role. Tasks are the protocol's primary verb: "implementer, please realize this proposal"; "evaluator, please score this branch." Every task has:

- A **kind** (plan, implement, evaluate).
- A **payload** shaped per-kind (a proposal reference, a commit to evaluate, etc.).
- A **state** advancing through a state machine ([`04-task-protocol.md`](04-task-protocol.md)).
- A **claim token** that grants a specific worker temporary exclusive right to execute it.

Schema: [`schemas/task.schema.json`](schemas/task.schema.json). The task is *stored data*; the task protocol is *how state transitions happen*.

## 6. Event

An **event** records a state change in the system — a task claimed, a proposal submitted, a trial evaluated. Events are immutable, append to an event log, and are read by subscribers (UIs, schedulers, other roles).

A central EDEN invariant: **every state change that is observable via tasks or trials MUST be accompanied by a corresponding event, and the event write MUST be atomic with the state change it describes.** Subscribers rely on this to reconstruct the system's history without gaps. The event schema and the transactional invariant are defined in [`05-event-protocol.md`](05-event-protocol.md); the event envelope itself is fixed in [`schemas/event.schema.json`](schemas/event.schema.json).

## 7. Objective and metrics schema

An experiment declares:

- A **metrics schema** — the names and storage types of the metrics its evaluator reports. Types are drawn from a small value-type set (integer, real, text). Schema: [`schemas/metrics-schema.schema.json`](schemas/metrics-schema.schema.json).
- An **objective** — a scalar expression over the declared metrics, plus a direction (maximize or minimize).

Conforming implementations MUST validate every reported metrics payload against the experiment's declared schema. A metrics payload that names a field absent from the schema, or that supplies a value incompatible with the declared type, MUST be rejected.

## 8. Claim token

A **claim token** is a value issued when a worker claims a task. It accompanies every subsequent request the worker makes about that task (progress reports, completion submissions, artifact uploads). The token is the system's mechanism for guaranteeing that two workers cannot complete the same task: once a task is claimed, the task store MUST reject any completion attempt that does not present the same token. A task store MAY reclaim a task from a worker that has become unresponsive; reclamation invalidates the prior token.

The token's concrete shape is implementation-defined; the protocol requires only that it be unforgeable by other workers and bound to a single claim.

## 9. Canonical trial lineage

EDEN defines a git topology with three namespaces:

- **`main`** — the experiment's starting point. Immutable during an experiment.
- **`trial/*`** — the canonical lineage, one commit per successfully integrated trial. Written only by the integrator.
- **`work/*`** — per-attempt worker branches. Multiple implementers may write here concurrently; these branches are inputs to the integrator and are not normative outputs.

Consumers (evaluators, external observers, later experiments) read the canonical lineage on `trial/*`. Chapter [`06-integrator.md`](06-integrator.md) defines the exact invariants the integrator preserves across this topology.

## 10. Storage components

Three durable stores support an experiment:

- **Task store** — holds tasks and their state. Provides atomic claim, idempotent submission, and reclamation.
- **Event log** — appends events in causal order. Provides subscribe and replay.
- **Artifact store** — holds per-trial files (plans, code diffs, evaluator outputs, logs). Addressed by URI.

The protocol defines what each store's operations guarantee; it does not define how they are implemented. Chapter [`08-storage.md`](08-storage.md) gives the durability and consistency requirements each store must meet.

## 11. Orchestrator

The **orchestrator** is the component that dispatches tasks to workers and advances the protocol's state machine in response to submissions. It has no unique authority beyond what the protocol grants it: in particular, it MUST persist state changes through the task store and event log like any other component, and an experiment MAY run with multiple cooperating orchestrators provided they share a conforming task store and event log.

## 12. Relationships

The concepts above fit together as follows. A full specification chapter exists for each of the behavioral contracts implied here.

- An **experiment** configures three **roles** and targets a **git repository** with a starting commit.
- An **orchestrator** dispatches **tasks** to role workers via the **task store**.
- A worker of role X claims a task, performs its role-specific work, and submits; the submission produces a result object (proposal, trial, metrics payload) persisted to the appropriate store, and an **event** is appended to the event log.
- The **integrator** consumes completed evaluations and advances the **canonical trial lineage**.
- The experiment terminates per its configured conditions; its outcome is the sequence of trials on the canonical lineage.
