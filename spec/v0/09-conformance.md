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

Anything else — language, deployment topology, persistence, scaling profile — is a deployment choice. The [`07-wire-protocol.md`](07-wire-protocol.md) §13 authentication scheme is normative as of 12a-1; further latitude is enumerated in [`07-wire-protocol.md`](07-wire-protocol.md) §14.

## 3. Assertion vocabulary

Every assertion in the v1 suite is keyed off a normative MUST in [`02-data-model.md`](02-data-model.md), [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), [`07-wire-protocol.md`](07-wire-protocol.md), or [`08-storage.md`](08-storage.md). The v1+roles suite extends this to include [`03-roles.md`](03-roles.md). The v1+roles+integrator suite further extends to include [`06-integrator.md`](06-integrator.md). The suite does NOT assert SHOULDs; SHOULDs are interop guidance, not interop contracts.

When the spec evolves, the suite evolves with it. A change that removes a MUST or downgrades it to SHOULD MUST be accompanied by a matching scenario removal or rewrite.

The suite intentionally does NOT certify the [`04-task-protocol.md`](04-task-protocol.md) §1.3 atomicity-of-state-change-and-event-emission invariant: black-box testing cannot prove the absence of a sufficiently-narrow window. The v1 suite asserts the testable consequences (event presence, observable ordering) and labels the regression-style test as such.

## 4. Conformance levels

The v0 spec defines exactly one wire binding ([`07-wire-protocol.md`](07-wire-protocol.md)). The suite drives every IUT through it; transport-neutral semantic tests vs. per-binding wire tests cannot be meaningfully separated until a second binding exists. Levels are therefore distinguished by **scenario coverage**, not transport:

- **v1** — task-store and event-log MUSTs from [`02-data-model.md`](02-data-model.md), [`04-task-protocol.md`](04-task-protocol.md), [`05-event-protocol.md`](05-event-protocol.md), [`07-wire-protocol.md`](07-wire-protocol.md), plus the storage MUSTs that the wire binding exposes from [`08-storage.md`](08-storage.md) §1.1, §1.7. v1 does NOT cover the [`03-roles.md`](03-roles.md) role contracts or the [`06-integrator.md`](06-integrator.md) integrator atomicity ladder.
- **v1+roles** — adds [`03-roles.md`](03-roles.md) role-contract scenarios (per-role submission semantics, backpressure, idempotency).
- **v1+roles+integrator** — adds the wire-observable projection of [`06-integrator.md`](06-integrator.md) §2, §3.4, §5.3 — integration preconditions on the variant-status vocabulary; atomicity-of-(field, event) on `integrate_variant`; no-overwrite under repeat integration. The git-side artifacts (squash shape, evaluation-manifest shape, `work/*` discipline, reachability) are part of [`06-integrator.md`](06-integrator.md) but are **not** asserted by a wire-only suite — chapter 9 §6 makes the chapter-7 binding the only IUT contract a conformance harness can rely on, and git refs are not exposed through that binding. A future binding chapter that defines a "conformance + git access" contract MAY add those tests at a higher level.

A future spec lineage that introduces a second wire binding will at that point split the suite into transport-neutral semantic tests + per-binding wire tests, and chapter 9 will gain a `core` level claimable by IUTs implementing only the semantic layer. The marker structure in [`conformance/`](../../conformance/) anticipates that refactor but does not enable it in v0.

## 5. Scenario index

The v1 scenario groups, with their primary spec citations:

| Group | Scope | Spec citations |
|---|---|---|
| Task lifecycle | Every legal/illegal transition. | [`04-task-protocol.md`](04-task-protocol.md) §1, §2, §3, §4, §5 |
| Worker registration | Per-experiment registry; idempotent re-registration; grammar / reserved-identifier rejection; disjoint worker/group namespaces; read / list endpoints don't leak credentials. | [`02-data-model.md`](02-data-model.md) §6, §7.1; [`07-wire-protocol.md`](07-wire-protocol.md) §6.1, §6.2 |
| Group resolution | Direct + transitive membership; cycle rejection; reserved-identifier rejection; disjoint worker/group namespaces; read / list / mutate / delete wire endpoints. | [`02-data-model.md`](02-data-model.md) §6.1, §7.1, §7.2, §7.3; [`07-wire-protocol.md`](07-wire-protocol.md) §7.2, §7.3 |
| Claim ownership | Identity-keyed claim record; no-reclaim-while-claimed; submit-claimant match; claim cleared on reclaim. | [`02-data-model.md`](02-data-model.md) §3.4; [`04-task-protocol.md`](04-task-protocol.md) §3, §4.1, §4.2, §5 |
| Claim eligibility | Claim-time ladder — registration check + target eligibility (null / worker / group); `Task.target` round-trips through create / read / list. | [`02-data-model.md`](02-data-model.md) §3.5; [`04-task-protocol.md`](04-task-protocol.md) §3.5 |
| Worker auth | Cross-application claim — same `worker_id` across distinct clients shares ownership; mismatched claimant rejected. Auth-enabled scenarios additionally cover the chapter-07 bearer middleware: missing/malformed bearer → 401; admin / worker bearer on the wrong endpoint class → 403; `/whoami` returns the authenticated worker_id; reissue invalidates the prior credential; the binding stamps `created_by` on `create_task` / `create_idea` from the authenticated principal. | [`02-data-model.md`](02-data-model.md) §3.1, §5.1; [`04-task-protocol.md`](04-task-protocol.md) §3.3, §4.1; [`07-wire-protocol.md`](07-wire-protocol.md) §6.4, §13, §13.3, §13.4 |
| Attribution persistence | `submitted_by` / `executed_by` / `evaluated_by` survive terminal transitions. | [`02-data-model.md`](02-data-model.md) §3.1, §9 |
| Submit idempotency | Content-equivalent / divergent / post-terminal. | [`04-task-protocol.md`](04-task-protocol.md) §4.2, §4.4 |
| Reclamation | Case matrix; claim clearing; variant reconciliation. | [`04-task-protocol.md`](04-task-protocol.md) §5 |
| Atomicity (regression test) | State + event consistency around a transition. Best-effort, not a certification. | [`04-task-protocol.md`](04-task-protocol.md) §1.3 |
| Event envelope | Envelope shape; uniqueness. | [`05-event-protocol.md`](05-event-protocol.md) §1, §1.1 |
| Per-type event payloads | Each registered type's required fields. | [`05-event-protocol.md`](05-event-protocol.md) §3 |
| Composite commits | Execution-task dispatch, execution-task terminal, evaluate-terminal cases, retry-exhausted `evaluation_error` terminalization, execution-task reclaim with in-flight variant. | [`04-task-protocol.md`](04-task-protocol.md) §4.3; [`05-event-protocol.md`](05-event-protocol.md) §2.2 |
| Event delivery | Total order, replay from cursor 0, at-least-once via subscribe-reconnect, long-poll subscribe. | [`05-event-protocol.md`](05-event-protocol.md) §4; [`07-wire-protocol.md`](07-wire-protocol.md) §8.2 |
| Status codes | Each operation's spec-pinned status, including the [`07-wire-protocol.md`](07-wire-protocol.md) §9 status mappings exercised through duplicate-create / bad-request paths, the [`05-event-protocol.md`](05-event-protocol.md) §4.4 replay binding, and the [`07-wire-protocol.md`](07-wire-protocol.md) §1.1 empty-body rule on 2xx responses without a payload. | [`05-event-protocol.md`](05-event-protocol.md) §4.4; [`07-wire-protocol.md`](07-wire-protocol.md) §1.1, §2, §3, §4, §5, §8, §9 |
| Problem+json envelope | Shape + content-type. | [`07-wire-protocol.md`](07-wire-protocol.md) §9 |
| Error vocabulary closure | Closed `eden://error/<name>` set; observed exhaustively. | [`07-wire-protocol.md`](07-wire-protocol.md) §9 |
| Experiment-id header disagreement | 400 experiment-id-mismatch. | [`07-wire-protocol.md`](07-wire-protocol.md) §1.3 |
| Integrate idempotency | Same-value / different-value / preconditions. | [`07-wire-protocol.md`](07-wire-protocol.md) §5 |
| Experiment durability | Aggregate-over-substrates durability of protocol-owned state across process / host / substrate restart. **Scenario authoring deferred to a follow-up chunk** (a "stop-stack / kill-volume-mount / start-stack / replay" harness driving any conforming IUT). The placeholder row anchors the citation so future scenarios slot in here. | [`01-concepts.md`](01-concepts.md) §13 |

The **v1+roles** level adds the role-contract groups below. The **v1+roles+integrator** level adds the integrator groups further below.

The v1+roles scenario groups (added in chunk 11c), with their primary spec citations:

| Group | Scope | Spec citations |
|---|---|---|
| Ideator submission | Drafting-idea precondition; status vocabulary; idea-set semantics. | [`03-roles.md`](03-roles.md) §2.4 |
| Executor submission | Submission-shape preconditions; variant-binding; status vocabulary; worker-branch uniqueness; non-no-op variant rejection (only the SHA-equality fast path — the wire-observable projection of the tree-shape MUST that the IUT SHOULD enforce per [`04-task-protocol.md`](04-task-protocol.md) §4.2; deeper tree-identity-via-fetch is MAY-level for v0 and not asserted). | [`03-roles.md`](03-roles.md) §3.3, §3.4 |
| Evaluator submission | Status vocabulary; evaluation-schema validation; per-status variant-side writes; evaluation_error non-grafting. | [`03-roles.md`](03-roles.md) §4.2, §4.4 |
| Orchestrator role contract | Decision-types are gated by `dispatch_mode`; manual mode skips orchestrator-driven decisions; orchestrator authority does not impersonate workers on terminal transitions. | [`03-roles.md`](03-roles.md) §6.1, §6.2, §6.3, §6.5 |
| Multi-instance safety | Concurrent execution-task / evaluation-task dispatch and integration are exactly idempotent; concurrent ideation-task creation is bounded by `N * T` and self-corrects in subsequent iterations. | [`03-roles.md`](03-roles.md) §6.4 |
| Reassignment | Pending reassign updates target + emits `task.reassigned`; claimed reassign atomically clears the claim and emits both `task.reclaimed(operator)` and `task.reassigned`; submitted / terminal reassign rejected with 409 invalid-precondition; non-`admins` caller rejected with 403 forbidden. | [`04-task-protocol.md`](04-task-protocol.md) §6; [`05-event-protocol.md`](05-event-protocol.md) §3.1; [`07-wire-protocol.md`](07-wire-protocol.md) §2.7 |
| Dispatch mode | Partial-update merge semantics; `experiment.dispatch_mode_changed` event payload (resulting state + `changed` diff + `updated_by`); manual mode prevents auto-orchestrator from running the gated decision; non-`admins` caller rejected with 403 forbidden. | [`02-data-model.md`](02-data-model.md) §2.5; [`04-task-protocol.md`](04-task-protocol.md) §7; [`05-event-protocol.md`](05-event-protocol.md) §3.4; [`07-wire-protocol.md`](07-wire-protocol.md) §2.8 |

The v1+roles+integrator scenario groups (added in chunk 11d), with their primary spec citations:

| Group | Scope | Spec citations |
|---|---|---|
| Integrator atomicity | Cross-artifact (field, event) consistency on success; no-overwrite under repeat integration. | [`06-integrator.md`](06-integrator.md) §3.4, §5.3 |
| Integration preconditions | Status-vocabulary preconditions for integration (`error`, `evaluation_error`); end-state assertion on rejection. | [`06-integrator.md`](06-integrator.md) §2 |

## 6. Adapter (informative)

A v1 IUT does NOT need to ship a Python adapter. The reference suite's adapter shape — under [`conformance/harness/adapter.py`](../../conformance/harness/adapter.py) — is one convenience for IUTs that prefer to integrate with the reference Python harness; it is not a normative requirement. A non-Python implementation MAY:

- Write a Python `IutAdapter` subclass that spawns their service.
- Run the suite against an already-running IUT pointed at by an environment variable, via a thin Python adapter that simply returns an `IutHandle` to that URL.
- Re-implement the suite in another language entirely, asserting the same MUSTs.

The contract between an IUT and a conformance harness is the chapter-7 HTTP binding. Everything else is convenience.

## 7. Reference posture

The reference implementation in [`reference/`](../../reference/) demonstrates conformance at the highest currently-shipped level — **v1** at the time chapter 9 first lands; **v1+roles** after chunk 11c; **v1+roles+integrator** after chunk 11d. The reference impl's conformance claim is always level-qualified per §1. CI gates the reference impl on every level the suite has shipped, via the `conformance` job. A reference-impl test failure that cannot be cited to a normative MUST is either a suite bug or a spec gap; the resolution is one of: amend the suite, amend the spec, fix the impl. The reference impl does NOT have authority to reinterpret the spec.
