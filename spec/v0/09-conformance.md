# Conformance

This chapter declares what conformance to EDEN protocol v0 means and how it is verified.

## 1. What conformance is

A v0-conforming IUT (implementation under test) implements the HTTP wire binding from [`07-wire-protocol.md`](07-wire-protocol.md) with the normative semantics of [`02-data-model.md`](02-data-model.md), [`03-roles.md`](03-roles.md), [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), [`06-integrator.md`](06-integrator.md), and [`08-storage.md`](08-storage.md).

Conformance is verified by passing the conformance suite under [`conformance/`](../../conformance/) in this repo: a set of black-box scenarios that drive an IUT through the chapter-7 HTTP binding and assert MUSTs from those chapters. The suite is delivered in three increments — **v1**, **v1+roles**, and **v1+roles+integrator** — each adding a group of scenarios to the prior level. **An IUT MUST qualify its conformance claim with the level it passes** (e.g. "v1 conformant", "v1+roles+integrator conformant"); the term "v0 conformance" without a level qualifier is reserved for the final `v1+roles+integrator` level once it exists. Until then, an unqualified "v0 conformance" claim is not meaningful.

The suite is one expression of the contract, not the only one. A future suite — in a different language, against a different harness — that drives the same chapter-7 endpoints and asserts the same MUSTs would be equally authoritative. The spec is the source of truth; the suite is one faithful implementation of "what would test that?"

## 2. The IUT

An IUT MUST:

- Expose the chapter-7 HTTP binding ([`07-wire-protocol.md`](07-wire-protocol.md)) at a deployment-chosen base URL.
- Accept an experiment configuration whose shape matches [`schemas/experiment-config.schema.json`](schemas/experiment-config.schema.json).

Anything else — language, deployment topology, persistence, auth scheme, scaling profile — is a deployment choice. Further latitude is enumerated in [`07-wire-protocol.md`](07-wire-protocol.md) §11.

## 3. Assertion vocabulary

Every assertion in the v1 suite is keyed off a normative MUST in [`02-data-model.md`](02-data-model.md), [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), [`07-wire-protocol.md`](07-wire-protocol.md), or [`08-storage.md`](08-storage.md). The suite does NOT assert SHOULDs; SHOULDs are interop guidance, not interop contracts.

When the spec evolves, the suite evolves with it. A change that removes a MUST or downgrades it to SHOULD MUST be accompanied by a matching scenario removal or rewrite.

The suite intentionally does NOT certify two [`04-task-protocol.md`](04-task-protocol.md) §1.3, §3.2 invariants: the atomicity-of-state-change-and-event-emission invariant and the claim-token unforgeability property. Black-box testing cannot prove the absence of a sufficiently-narrow window or the absence of a sufficiently-weak random source. The v1 suite asserts the testable consequences of these invariants (event presence, uniqueness across observed claims) and labels the regression-style tests as such.

## 4. Conformance levels

The v0 spec defines exactly one wire binding ([`07-wire-protocol.md`](07-wire-protocol.md)). The suite drives every IUT through it; transport-neutral semantic tests vs. per-binding wire tests cannot be meaningfully separated until a second binding exists. Levels are therefore distinguished by **scenario coverage**, not transport:

- **v1** — task-store and event-log MUSTs from [`02-data-model.md`](02-data-model.md), [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), [`07-wire-protocol.md`](07-wire-protocol.md), plus the storage MUSTs that the wire binding exposes from [`08-storage.md`](08-storage.md) §1.1, §1.7. v1 does NOT cover the [`03-roles.md`](03-roles.md) role contracts or the [`06-integrator.md`](06-integrator.md) integrator atomicity ladder.
- **v1+roles** — adds [`03-roles.md`](03-roles.md) role-contract scenarios (per-role submission semantics, backpressure, idempotency).
- **v1+roles+integrator** — adds [`06-integrator.md`](06-integrator.md) §3.4 integrator scenarios (squash shape, eval-manifest shape, `work/*` access discipline, atomicity ladder under transport-indeterminate failures).

A future spec lineage that introduces a second wire binding will at that point split the suite into transport-neutral semantic tests + per-binding wire tests, and chapter 9 will gain a `core` level claimable by IUTs implementing only the semantic layer. The marker structure in [`conformance/`](../../conformance/) anticipates that refactor but does not enable it in v0.

## 5. Scenario index

The v1 scenario groups, with their primary spec citations:

| Group | Scope | Spec citations |
|---|---|---|
| Task lifecycle | Every legal/illegal transition. | [`04-task-protocol.md`](04-task-protocol.md) §1, §2, §3, §4, §5 |
| Claim tokens | Freshness, authorization, no-reclaim-while-claimed. | [`02-data-model.md`](02-data-model.md) §3.4; [`04-task-protocol.md`](04-task-protocol.md) §3, §5 |
| Submit idempotency | Content-equivalent / divergent / post-terminal. | [`04-task-protocol.md`](04-task-protocol.md) §4.2, §4.4 |
| Reclamation | Case matrix; token invalidation; trial reconciliation. | [`04-task-protocol.md`](04-task-protocol.md) §5 |
| Atomicity (regression test) | State + event consistency around a transition. Best-effort, not a certification. | [`04-task-protocol.md`](04-task-protocol.md) §1.3 |
| Event envelope | Envelope shape; uniqueness. | [`05-event-protocol.md`](05-event-protocol.md) §1, §1.1 |
| Per-type event payloads | Each registered type's required fields. | [`05-event-protocol.md`](05-event-protocol.md) §3 |
| Composite commits | Implement-dispatch, implement-terminal, evaluate-terminal cases, retry-exhausted `eval_error` terminalization, implement-reclaim-with-starting-trial. | [`04-task-protocol.md`](04-task-protocol.md) §4.3; [`05-event-protocol.md`](05-event-protocol.md) §2.2 |
| Event delivery | Total order, replay from cursor 0, at-least-once via subscribe-reconnect, long-poll subscribe. | [`05-event-protocol.md`](05-event-protocol.md) §4; [`07-wire-protocol.md`](07-wire-protocol.md) §6.2 |
| Status codes | Each operation's spec-pinned status, including the chapter-7 §7 status mappings exercised through duplicate-create / bad-request paths and the chapter-5 §4.4 replay binding. | [`05-event-protocol.md`](05-event-protocol.md) §4.4; [`07-wire-protocol.md`](07-wire-protocol.md) §2, §3, §4, §5, §6, §7 |
| Problem+json envelope | Shape + content-type. | [`07-wire-protocol.md`](07-wire-protocol.md) §7 |
| Error vocabulary closure | Closed `eden://error/<name>` set; observed exhaustively. | [`07-wire-protocol.md`](07-wire-protocol.md) §7 |
| Experiment-id header disagreement | 400 experiment-id-mismatch. | [`07-wire-protocol.md`](07-wire-protocol.md) §1.3 |
| Integrate idempotency | Same-value / different-value / preconditions. | [`07-wire-protocol.md`](07-wire-protocol.md) §5 |

The **v1+roles** and **v1+roles+integrator** levels add their own scenario groups; their contents are out of scope for the chapter 9 v1 ship and will be appended in chunks 11c and 11d respectively.

## 6. Adapter (informative)

A v1 IUT does NOT need to ship a Python adapter. The reference suite's adapter shape — under [`conformance/harness/adapter.py`](../../conformance/harness/adapter.py) — is one convenience for IUTs that prefer to integrate with the reference Python harness; it is not a normative requirement. A non-Python implementor MAY:

- Write a Python `IutAdapter` subclass that spawns their service.
- Run the suite against an already-running IUT pointed at by an environment variable, via a thin Python adapter that simply returns an `IutHandle` to that URL.
- Re-implement the suite in another language entirely, asserting the same MUSTs.

The contract between an IUT and a conformance harness is the chapter-7 HTTP binding. Everything else is convenience.

## 7. Reference posture

The reference implementation in [`reference/`](../../reference/) demonstrates conformance at the highest currently-shipped level — **v1** at the time chapter 9 first lands; **v1+roles** after chunk 11c; **v1+roles+integrator** after chunk 11d. The reference impl's conformance claim is always level-qualified per §1. CI gates the reference impl on every level the suite has shipped, via the `conformance` job. A reference-impl test failure that cannot be cited to a normative MUST is either a suite bug or a spec gap; the resolution is one of: amend the suite, amend the spec, fix the impl. The reference impl does NOT have authority to reinterpret the spec.
