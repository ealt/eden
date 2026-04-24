# EDEN Protocol Specification

This directory contains the normative specification for the EDEN protocol. The spec is the authoritative source of protocol semantics — if the spec and the reference implementation disagree, the spec wins.

## Versioning

Spec versions live in subdirectories: `v0/`, `v1/`, …

- Within a version, changes should be **additive** or **clarifying**.
- **Breaking changes go to a new version**, never mutated in place.
- The reference implementation declares which spec version(s) it targets; third-party implementations do the same.

Current version: **v0 (draft, in development)**. See [`v0/`](v0/) for the list of planned chapters.

## Conventions

- Prose uses [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119) normative keywords (MUST, SHOULD, MAY) where behavior is prescriptive. Informative prose uses ordinary English and says so.
- Every wire-format object has a corresponding JSON Schema under the version's `schemas/` directory. The Markdown chapter cites the schema file by path.
- The spec describes **semantics**, not mechanisms. Specific technologies (databases, queues, transports) are out of scope; conforming implementations are free to choose.

## What conforms

A conforming **component** (planner, implementer, evaluator, integrator, storage backend, git host, control plane, web UI) implements the contract(s) defined in the spec version it targets. A conforming **system** is any composition of conforming components that together satisfy the end-to-end invariants.

The [conformance suite](../conformance/) (landing in Phase 11 per [`docs/roadmap.md`](../docs/roadmap.md)) validates components against their contracts.
