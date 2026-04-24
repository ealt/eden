# EDEN Protocol — Overview

**Version:** v0 (draft)

EDEN is a protocol for orchestrating **directed code evolution**: the iterative, machine-driven generation, implementation, and evaluation of code changes against a declared objective. It specifies the components involved, the messages they exchange, and the invariants they must honor. It does not prescribe a language, transport, or storage technology — those are implementation concerns.

This document is the first chapter of the v0 specification. Later chapters refine each of the concepts introduced here; see [`README.md`](README.md) for the full chapter list and the phase in which each lands.

## 1. Scope

The EDEN protocol defines:

- The **roles** that participate in an EDEN experiment (Chapter [`03-roles.md`](03-roles.md)).
- The **shared data model** — experiment configs, tasks, events, proposals, trials, and metrics schemas (Chapter [`02-data-model.md`](02-data-model.md) and the JSON Schemas in [`schemas/`](schemas/)).
- The **task protocol** — how work is claimed, executed, and submitted (Chapter [`04-task-protocol.md`](04-task-protocol.md)).
- The **event protocol** — how state changes are observed by subscribers (Chapter [`05-event-protocol.md`](05-event-protocol.md)).
- The **integrator contract** — how trial branches are promoted into a canonical lineage (Chapter [`06-integrator.md`](06-integrator.md)).
- The **storage contract** — the durability and consistency guarantees a conforming task store, event log, and artifact store must provide (Chapter [`08-storage.md`](08-storage.md)).
- The **conformance procedure** — how an independent implementation proves it conforms (Chapter [`09-conformance.md`](09-conformance.md)).

The EDEN protocol does **not** define:

- Which language, runtime, or transport an implementation uses.
- Which database, message bus, or object store backs a conforming service.
- The UX of any human-facing tool.
- The internal algorithms of a planner, implementer, or evaluator, beyond the contracts they expose at their role boundary.

## 2. Conformance

### 2.1 Normative language

Normative requirements in this specification use the RFC 2119 keywords **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**. Prose that does not use one of these keywords is informative.

### 2.2 Conformance classes

A single EDEN experiment involves several distinct components. Each can be implemented independently; each is evaluated against its own conformance class:

- **Planner** — proposes trials.
- **Implementer** — realizes a proposal as a working-tree change.
- **Evaluator** — scores a realized proposal against the experiment's objective.
- **Integrator** — promotes evaluated proposals into the canonical trial lineage.
- **Task store** — the durable queue that holds tasks between roles.
- **Event log** — the durable record of state changes, read by subscribers.
- **Artifact store** — the durable home for files produced during a trial (plans, code, evaluation outputs).
- **Orchestrator** — the component that dispatches tasks and advances the state machine.

An implementation MAY conform to one or more classes. It need not conform to all.

### 2.3 What "conforming" means

An implementation conforms to a class iff it passes every scenario in [`09-conformance.md`](09-conformance.md) that targets that class. The conformance suite is black-box: it drives an implementation through its advertised protocol surface and observes the results. An implementation that passes is conforming regardless of how it is built.

The reference implementation in [`../../reference/`](../../reference/) is **one** conforming implementation. It has no privileged status; a third-party implementation that passes the conformance suite is equally valid.

## 3. Versioning

### 3.1 Version lineage

An EDEN spec version is a single lineage identified by a directory under `spec/` (e.g. `spec/v0/`, `spec/v1/`). Within a version, changes MUST be either additive (new optional fields, new events, new roles) or clarifying (wording, examples, fixed typos). Breaking changes MUST go to a new version; `v0` MUST NOT be mutated in a way that would invalidate a previously-conforming implementation.

### 3.2 Declaring a targeted version

An implementation MUST declare the EDEN spec version it targets. How the declaration is surfaced is implementation-defined; typical forms include a `protocol_version` field in a service's health endpoint, a string in a package's metadata, or a header on protocol messages. The conformance suite for version N runs only against implementations that declare targeting of version N.

### 3.3 Wire-format changes

A change to any JSON Schema file under `schemas/` is a change to the wire format. Within a version, such changes MUST be additive. Schema changes MUST be reflected in the corresponding prose chapter (and, once the reference implementation has them, in its language bindings) in the same change — they MUST NOT drift.

## 4. Document conventions

### 4.1 Schemas and prose

Wire-format objects are specified in two complementary forms:

- A **JSON Schema** file under [`schemas/`](schemas/) that an implementation can validate messages against mechanically.
- A **prose description** in this spec that records what each field means, which invariants apply across fields, and what the surrounding role contract expects.

If prose and schema disagree, the prose describes intent and the schema MUST be updated to match. Automated tooling (see [`AGENTS.md`](../../AGENTS.md)) MAY enforce parity between the two.

### 4.2 Cross-references

Chapters cite each other by filename (e.g. "see [`04-task-protocol.md`](04-task-protocol.md)"). Numbered section anchors within a chapter are stable within a version.

### 4.3 Examples

Informative examples appear in fenced code blocks and are marked as such. Examples MUST NOT be read as normative — if an example contradicts the surrounding prose, the prose wins.

## 5. Status of this version

EDEN `v0` is a **draft**. Chapters are filled in over Phases 1–12 of the roadmap; see [`../../docs/roadmap.md`](../../docs/roadmap.md) for the per-chapter target phase. An implementation MAY target `v0-draft` during this period, but SHOULD expect additive changes between successive `v0-draft` commits. A frozen `v0` release will be tagged once the full chapter set and conformance suite are in place.
