# EDEN Protocol — Overview

**Version:** v0 (draft)

EDEN is a protocol for orchestrating **directed evolution**: the iterative, machine-driven generation, implementation, and evaluation of code changes against a declared objective. It specifies the components involved, the messages they exchange, and the invariants they must honor. It does not prescribe a language, transport, or storage technology — those are implementation concerns.

This document is the first chapter of the v0 specification. Later chapters refine each of the concepts introduced here; see [`README.md`](README.md) for the full chapter list and the phase in which each lands.

## 1. Scope

The EDEN protocol defines:

- The **roles** that participate in an EDEN experiment (Chapter [`03-roles.md`](03-roles.md)).
- The **shared data model** — experiment configs, tasks, events, ideas, variants, and metrics schemas (Chapter [`02-data-model.md`](02-data-model.md) and the JSON Schemas in [`schemas/`](schemas/)).
- The **task protocol** — how work is claimed, executed, and submitted (Chapter [`04-task-protocol.md`](04-task-protocol.md)).
- The **event protocol** — how state changes are observed by subscribers (Chapter [`05-event-protocol.md`](05-event-protocol.md)).
- The **integrator contract** — how variant branches are promoted into a canonical lineage (Chapter [`06-integrator.md`](06-integrator.md)).
- The **storage contract** — the durability and consistency guarantees a conforming task store, event log, and artifact store must provide (Chapter [`08-storage.md`](08-storage.md)).
- The **conformance procedure** — how an independent implementation proves it conforms (Chapter [`09-conformance.md`](09-conformance.md)).

The EDEN protocol does **not** define:

- Which language, runtime, or transport an implementation uses.
- Which database, message bus, or object store backs a conforming service.
- The UX of any human-facing tool.
- The internal algorithms of an ideator, executor, or evaluator, beyond the contracts they expose at their role boundary.

## 2. Conformance

### 2.1 Normative language

Normative requirements in this specification use the RFC 2119 keywords **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**. Prose that does not use one of these keywords is informative.

### 2.2 The unit of conformance

Conformance is judged at the **whole implementation under test (IUT)** level: an IUT is a server that exposes the chapter-7 HTTP binding ([`07-wire-protocol.md`](07-wire-protocol.md)) and honors the normative semantics of the chapters that bind to it. The protocol's role names (ideator, executor, evaluator, integrator) and store names (task store, event log, artifact store) introduced in [`01-concepts.md`](01-concepts.md) are *parts of the protocol*; they are not independent conformance units. An IUT that exposes the chapter-7 endpoints internally implements all of them — though deployments are free to back individual parts with separate processes, services, or technologies.

This framing follows from the chapter-7 binding being the only contract a black-box conformance harness can rely on ([`09-conformance.md`](09-conformance.md) §6). A future spec lineage that introduces a second binding (e.g. a transport-neutral semantic layer) will gain finer-grained conformance units; v0 has one binding, so it has one IUT shape.

### 2.3 Conformance levels

Conformance is verified by passing the suite under [`conformance/`](../../conformance/) in this repo. The suite is delivered in three additive **levels**, each adding scenarios on top of the prior level:

- **v1** — task-store and event-log MUSTs.
- **v1+roles** — adds the per-role submission contracts.
- **v1+roles+integrator** — adds the wire-observable projection of the integrator atomicity ladder.

An IUT **MUST qualify its conformance claim with the level it passes** (e.g. "v1 conformant", "v1+roles+integrator conformant"). The full per-level scope, the assertion vocabulary the suite asserts (MUSTs only, not SHOULDs), and the per-level scenario index live in [`09-conformance.md`](09-conformance.md). Where this overview and chapter 09 disagree, chapter 09 wins: this section is conceptual; chapter 09 is normative.

The reference implementation in [`../../reference/`](../../reference/) demonstrates conformance at the highest currently-shipped level. It has no privileged status; a third-party IUT that passes the suite at the same level is equally valid.

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
