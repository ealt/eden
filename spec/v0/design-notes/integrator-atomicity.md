# Integrator atomicity — design note

This note records the reasoning behind [`06-integrator.md`](../06-integrator.md) §3.4's atomicity invariant, the interpretation the drafters intended for a subtle ambiguity in the normative text, and the alternatives that were considered but not chosen. Future spec revisions touching §3.4 should read this first to understand what is already load-bearing.

The normative clause in §3.4 stands on its own. This note explains why it reads the way it does.

## Context

§3.4 requires that the three artifacts produced by a successful promotion — the `trial/*` git ref, the `trial_commit_sha` field on the trial, and the `trial.integrated` event in the log — remain consistent with one another. The original text of the paragraph named four rules:

1. On failure of any step, the integrator MUST roll back any already-performed step.
2. A dangling `trial/*` ref with no field and no event is a protocol violation.
3. Implementations MAY use store-level transactions, outbox patterns, compensating deletes, or any other mechanism.
4. The observable invariant is that a reader of any one of the three artifacts MUST observe the other two.

Rules 1 and 2 are uncontroversial: failure requires rollback, and a permanently dangling ref is a bug. Rule 3 explicitly lists mechanisms that implementations MAY choose from. Rule 4 states an observable invariant.

The ambiguity is a tension between rules 3 and 4. A *compensating delete* — explicitly named in rule 3 — writes an artifact and, if a downstream step fails, deletes it. That pattern *necessarily* creates a window during which the artifact exists before the compensating deletion occurs. If rule 4 is read as an instantaneous invariant (applying at every point in time, including during a running promotion), then no mechanism named in rule 3 — compensating deletes specifically — can satisfy rule 4 in the face of an external filesystem reader that walks refs directly while a promotion is in flight. Rule 4 would, under that reading, outlaw the very mechanism rule 3 permits.

Two readings of rule 4 resolve the tension:

- **Post-promotion reading.** Rule 4 applies after the integrator's promotion call returns, whether by success or rollback. Intermediate states during a running promotion are permitted.
- **Instantaneous reading.** Rule 4 applies at every point in time, including while a promotion is running. Intermediate states are not permitted.

This note explains why the drafters chose the post-promotion reading and tightened §3.4 to state it explicitly.

## Options considered

### Option 1 — Post-promotion reading with compensating deletes *(chosen)*

The integrator performs, in order: write the commit object (an orphan, content-addressed write visible only via the ref), create the `trial/*` ref via a compare-and-swap against the zero-oid, then call the store's atomic operation that writes `trial_commit_sha` and appends `trial.integrated` together. If the store operation raises, the integrator compensates by deleting the ref with an expected-old-sha precondition.

On success, all three artifacts exist when the promotion returns. On failure, the ref exists briefly and is then deleted; by the time the promotion returns, none of the three exist. A reader observing the repository after the promotion returns sees a consistent state. A reader that happens to walk refs during the ~microseconds or milliseconds the promotion is running MAY observe a ref that is about to be compensated away.

**Pros:**

- Satisfies rule 3 literally by using a mechanism rule 3 explicitly names as permitted.
- Implementable against the Phase 6 reference backends (in-memory and SQLite) without extending the `Store` protocol.
- The chosen order matches what §3.4's rollback rule requires: the failure mode ("ref exists, field and event do not") is the single-artifact case that a compensating delete can trivially reverse. The alternate order (field and event written, ref missing) has no corresponding reversal, because the event log is append-only per chapter 5.
- Compatible with the in-process reference impl's reader convention. Every in-repo consumer already treats `trial.trial_commit_sha is not None` as the integration marker — see `_promote_successful_trials` in the dispatch driver. Because the field is written last, an in-process reader never observes a field-set state where the ref or event is missing.

**Cons:**

- An external filesystem reader (for example, a human running `git log trial/*` in a terminal during an integrator call) MAY observe a `trial/*` ref that is about to be compensated away. After the promotion returns, a fresh read reflects the final state.
- Requires the drafters to pick one reading of the normative text and codify it. §3.4 as originally written was ambiguous; this option requires tightening the prose.

### Option 2 — Outbox pattern in the store

Extend the `Store` Protocol with three new operations: `begin_integration(trial_id, commit_sha)` records the intent in an outbox table; the integrator writes the git ref; then `commit_integration(trial_id)` atomically promotes the outbox row into the trial's `trial_commit_sha` field and the `trial.integrated` event, deleting the outbox row in the same transaction. If any step fails, `abandon_integration(trial_id)` clears the outbox row and the compensating ref-delete runs as in Option 1.

**Pros:**

- Improves restart-safety: if the integrator process crashes mid-promotion, a replay can inspect the outbox to determine whether the promotion had been committed or not.
- Follows a widely-used industry pattern (transactional outbox) that future implementors will recognize.

**Cons:**

- Does not actually satisfy the instantaneous reading of rule 4. The window between the ref write and the `commit_integration` call is the same shape as Option 1's window — during that window, an external ref-walker can still observe a ref before the field and event exist. The outbox shortens the window under *in-process* failure handling but does not eliminate it against external readers.
- Requires extending the `Store` Protocol with three new operations and implementing them in both reference backends (`InMemoryStore`, `SqliteStore`). Roughly 3× the scope of Option 1.
- Restart-safety is a separate concern from §3.4's invariant. The reference impl is in-process at Phase 7b; there is no crash-replay scenario to defend against. An outbox would be solving a problem that does not yet exist.
- Phase 8's wire-protocol work is a more natural point to evaluate whether an outbox is needed, because Phase 8 is where cross-process readers first appear. Speculatively adding it in Phase 7b would bake in a design shape before the actual requirements are concrete.

### Option 3 — Two-phase commit across the store and git

Use a two-phase commit protocol (XA-style or equivalent) to coordinate the SQLite transaction with the git ref write. Both resources prepare; if both succeed, both commit; otherwise both abort.

**Pros:**

- Genuinely satisfies the instantaneous reading of rule 4. Observers never see a partial state, because the commit-or-abort decision is durable before any artifact becomes visible.

**Cons:**

- Python's standard `sqlite3` driver does not support XA. Neither does the in-memory backend in any meaningful sense.
- Git has no XA plumbing. A workaround using git's low-level reference-transaction stdin (`git update-ref -z --stdin` with `start` / `prepare` / `commit` verbs via `core.refsStorage = reftable`) can approximate prepare/commit but does not compose with a conventional database transaction without a custom coordinator.
- The reference impl would need to build an XA coordinator, integrate it with both backends plus git, and handle the failure modes of the coordinator itself — an order-of-magnitude more work than Options 1 or 2 and well beyond the scope envelope for a v0 reference implementation.
- A conforming third-party implementation built on an XA-capable store (for example, PostgreSQL with prepared transactions) MAY choose this path. The reference impl does not.

## Decision

The drafters chose **Option 1**.

The deciding factors:

- **Rule 3 names compensating deletes explicitly.** The drafters concluded that a normative text listing compensating deletes as a permitted mechanism cannot simultaneously require a reader invariant that compensating deletes by construction cannot satisfy against external readers. The two rules must be read together. The post-promotion reading is the only reading under which rules 3 and 4 are coherent.

- **Reference impl scope.** Option 2 triples the implementation scope without closing the window it purports to close. Option 3 is infeasible for the reference impl's chosen backends. Option 1 fits within Phase 7b's scope.

- **Observable-invariant clarity.** Making the reading explicit in the chapter text — as the tightening at the end of §3.4 now does — removes the ambiguity for future implementors without expanding the protocol surface.

## Consequences

### For implementors

A conforming integrator MAY use compensating deletes against an external reader. The observable invariant applies to completed promotions. An implementor MAY choose an outbox or XA scheme instead if their target backend makes it cheap, but the protocol does not require one.

Every in-repo consumer of the store (for example, a dispatch driver deciding which trials still need promotion) SHOULD consult `trial.trial_commit_sha` as the canonical integration marker rather than walking `trial/*` refs. This is not normative, but it is the convention the reference impl uses, and it gives in-process readers consistent observations even during a running promotion.

### For operators and external readers

An operator running ad-hoc git commands against the repository during active experiments MAY transiently observe a `trial/*` ref that disappears a moment later. This can only happen on rollback and is rare in practice — rollbacks mean a store-side failure (typically a disk-full or IO-error condition). The final state is always consistent with the three-artifact invariant.

### For future chapters

Other chapters that cross-reference §3.4's atomicity contract inherit the post-promotion reading. If a future chapter needs stronger guarantees — for example, a cross-experiment index that walks `trial/*` refs across multiple repositories and cannot tolerate transient states — that chapter must specify the stronger requirement explicitly and §3.4 must be tightened in lockstep.

## Revisit triggers

Reconsider this decision if any of the following become true:

- **A protocol-owned reader is specified that walks `trial/*` refs directly** rather than consulting `trial_commit_sha`. Today no such reader exists; a future chapter that introduces one forces the stricter reading.

- **Cross-process readers with a conforming claim** start consuming the protocol. Phase 8's wire-protocol is the first point at which this becomes concrete. If the wire-protocol surface includes a direct `trial/*` ref enumeration, §3.4 may need tightening.

- **An XA-capable backend becomes the reference default.** If the reference impl migrates from SQLite to a backend that supports prepared transactions, Option 3 becomes feasible, and the cost argument against it no longer applies.

- **Observational evidence of the transient window causing bugs.** The current design rests on the assertion that external ref-walkers during a running promotion are a human-with-terminal case, not a protocol-participant case. If that assumption is falsified in practice — for example, a tool repeatedly misbehaves due to a transient ref observation — revisit.

- **A conformance-test scenario benefits from the instantaneous reading.** Phase 11's conformance suite may uncover cases where the post-promotion reading produces observable flakiness; if so, the suite's requirements may drive a spec tightening.

Any revision tightening §3.4 toward the instantaneous reading should update this note to record what changed and why, and should confirm that the chosen mechanism (outbox, XA, or another) is actually sufficient rather than just more elaborate than compensating deletes.
