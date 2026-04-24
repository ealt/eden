# Spec design notes

This directory holds **non-normative** design notes adjacent to the `spec/v0/` chapters. A design note captures the reasoning, the alternatives considered, and the conditions for revisiting a contested or non-obvious design decision that was made during the drafting of a normative chapter.

Design notes are not part of the normative contract. A conforming implementation needs only to satisfy the chapters in `spec/v0/`. Design notes exist so that:

- Future implementors can understand *why* a clause reads the way it does, beyond what the clause itself states.
- Future editors of the spec can evolve it with full knowledge of what the prior decision was and what was considered but rejected, rather than re-litigating the same tradeoffs from scratch.
- Reviewers of substantive spec changes can check whether a proposed change invalidates assumptions recorded here.

## Relationship to the spec

Each design note pairs with one or more normative clauses. The normative clause MUST stand on its own — a conformance check reads only the chapter, not the design note. The design note documents the *interpretation* the drafters intended when more than one reading of the normative text was plausible, or the *tradeoff* the drafters took when more than one design was feasible.

When a normative clause cross-references a design note, the reference is one-directional: the clause points out that rationale exists, but the clause itself remains the authoritative text.

## When to add one

Not every design decision needs a note. Add one when:

- The normative text is terse in a way that leaves a genuine ambiguity, and the drafters chose a specific reading.
- Multiple plausible mechanisms existed, and the choice was made on tradeoffs rather than correctness.
- Future implementors are likely to ask "why this and not that?" and re-deriving the answer from the chapter alone is expensive.

A passing codex-review finding or a recurring question in reviews is usually a signal that a design note would help.

## Format

Design notes are Markdown files named after the topic, not the chapter — `integrator-atomicity.md`, not `06-integrator-atomicity.md` — because a single design note may touch multiple chapters.

Each note has:

- A short **context** section summarizing the normative text it addresses and what the ambiguity or tradeoff is.
- An **options considered** section enumerating the candidate designs, with the tradeoffs that led to acceptance or rejection.
- A **decision** section naming the chosen path and its justification.
- A **consequences** section describing what implementors and readers should expect, and anything that an operator might observe.
- A **revisit triggers** section listing the conditions that would warrant revisiting the decision in a future spec revision.

There is no required front-matter or template beyond this. The goal is durable context, not structural boilerplate.
