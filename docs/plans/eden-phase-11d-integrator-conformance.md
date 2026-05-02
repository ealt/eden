# EDEN Phase 11 chunk 11d — integrator conformance scenarios

## 1. Context

Phase 11 builds out the conformance suite. Chunk 11a + 11b shipped harness +
v1 (task-store / event-log / wire) scenarios. Chunk 11c shipped v1+roles
(role-contract submission semantics from chapter 03). Chunk 11d is the
final unit of Phase 11: it adds **v1+roles+integrator** scenarios that
exercise [`spec/v0/06-integrator.md`](../../spec/v0/06-integrator.md) MUSTs.

Per the existing `Integrate idempotency` group in
[`conformance/scenarios/test_integrate_idempotency.py`](../../conformance/scenarios/test_integrate_idempotency.py)
(landed in 11b under the v1 level), the **HTTP-binding** projection of
the integrator atomic-three invariant is already covered: chapter 7 §5
pins same-value idempotency, different-SHA rejection, and the
status-precondition check. Those tests stay where they are — they cite
chapter 7 §5 and belong to the v1 level.

What chunk 11d adds is the **chapter-06 projection**: tests that cite
chapter 06 §3.4 (atomic-three invariant, projected onto the wire-visible
artifacts: trial-object field + event log) and chapter 06 §2 (promotion
trigger preconditions across the full trial-status vocabulary). This is
the v1+roles+integrator delta over v1.

## 2. Reframe — what's wire-observable in chapter 06

This is the load-bearing scope question for the chunk and warrants
making the scope explicit before writing tests. Chapter 06 §3.4 names
**three** atomic-three artifacts:

1. The `trial/*` git ref.
2. The `trial_commit_sha` field on the trial object.
3. The `trial.integrated` event in the event log.

Of those, the chapter-7 HTTP binding exposes only #2 and #3 to a
conformance harness. Git refs are an integrator-side internal detail;
chapter 9 §6 makes the chapter-7 binding the **only** IUT contract a
conformance harness can rely on.

§2 (promotion trigger) names **three** preconditions:

(a) `status == "success"` — wire-observable (`read_trial.status`).
(b) `commit_sha` is set — **NOT independently wire-observable** in v0.
    The trial-status lifecycle (chapter 02 §7.1, chapter 03 §3.2, §4.4)
    requires `commit_sha` to be written before a trial can transition
    to `success`: implementer-accept writes `commit_sha` on the
    `starting` trial; evaluator-accept transitions `starting` →
    `success`. So a `success` trial with `commit_sha` unset is
    unreachable through normal wire endpoints. Without a back-door,
    a wire-only suite cannot isolate (b) from (a) — any test that
    sees `commit_sha` unset will also see `status != success`, and
    the IUT may reject for either reason. The reference impl's
    `Store.integrate_trial` checks (a) and same-value-idempotency
    only (see [`reference/packages/eden-storage/src/eden_storage/_base.py`](../../reference/packages/eden-storage/src/eden_storage/_base.py)
    `integrate_trial`); it does NOT inspect `trial.commit_sha`. The
    suite therefore covers (b) only transitively, through (a)'s
    enforcement of the lifecycle ordering.
(c) `commit_sha` resolves to a commit on the trial's `branch` in the
    experiment repository — **NOT** wire-observable through chapter 7
    alone (would require the IUT's wire endpoint to validate against
    a real git repo, which the wire spec does not require). This is
    the integrator-side §1.4 reachability check restated as a §2
    trigger condition.

This means several MUSTs from chapter 06 cannot be asserted by a
chapter-7-only suite, even at the v1+roles+integrator level:

| Chapter-06 MUST | Wire-observable? |
|---|---|
| §1.4 reachability check | NO — requires git access |
| §2 (c) commit_sha-resolves-on-branch | NO — same as §1.4 |
| §3.2 squash shape | NO — git tree shape, off-wire |
| §3.3 commit message format | NO — git commit, off-wire |
| §4.x eval-manifest path/shape | NO — git tree content, off-wire |
| §1.3 work/* access discipline | NO — git ref existence, off-wire |
| §2 (a) `status == success` | YES — `read_trial.status` |
| §2 (b) `commit_sha` is set | TRANSITIVELY only — see (b) note above; the lifecycle enforces (b) as a precondition for `status == success`, so (a)'s assertion subsumes (b) under any wire-driven seed |
| §3.4 atomic-three (field + event projection) | YES — `read_trial` + `events` |
| §5.3 repeat-promotion (no-overwrite) | YES — `read_trial` + event count |
| §5.1 metrics-schema-violation detection | PARTIAL — orchestrator catches at evaluate-terminal (§2 last paragraph) |

### 2.1 Spec gap surfaced

Chapter 9 §3 prose enumerates "[`06-integrator.md`](06-integrator.md)"
as an in-scope MUST source for v1+roles+integrator. Chapter 9 §4 names
the level's scope as "squash shape, eval-manifest shape, `work/*` access
discipline, atomicity ladder under transport-indeterminate failures."
The first three of those four items are **not** wire-observable through
chapter-7 alone. This is the **same class of inter-chapter restatement
drift the chunk-11c plan-review caught** between chapter 03 §4.4 and
chapter 04 §4.2: chapter 9 §4 over-promises what a wire-only conformance
suite can assert, in apparent disagreement with chapter 9 §6 ("the
chapter-7 binding is the only IUT contract").

The chunk-11c codification rule **applies directly here** (cite:
[AGENTS.md plan-writing pitfalls — "Spec inter-chapter restatement is
a conflict surface"](../../AGENTS.md)): when two chapters restate the
same constraint with drift, defer the role-side restatement to the
canonical statement and tighten the prose. The fix shape:

- **Chapter 9 §4** — rewrite the v1+roles+integrator bullet to scope
  the level to "the wire-observable projection of chapter 06: §2
  promotion preconditions on the trial-status vocabulary; §3.4
  atomicity-of-(field, event) on `integrate_trial`; §5.3 no-overwrite
  under repeat promotion. The git-side artifacts (squash shape,
  eval-manifest shape, work/* discipline, reachability) are part of
  chapter 06 but are **not** asserted by a wire-only suite; a future
  binding chapter that defines a 'conformance + git access' contract
  MAY add those tests at a higher level."
- **Chapter 9 §3** — already cites chapter 06 generically; no edit.
- **Chapter 9 §5** — gain two new groups (see §3.2 below). The prose
  immediately above the v1+roles index table — currently "The
  **v1+roles+integrator** level adds its own group; its contents are
  out of scope for chunk 11c and will be appended in chunk 11d." —
  is also rewritten to "The **v1+roles+integrator** level adds the
  groups below" (singular → plural), and a new sub-table is
  appended for the v1+roles+integrator entries to mirror the §3
  prose's level distinction.

This is a single-paragraph spec amendment, not a refactor. It
preserves the v0 contract and tightens chapter 9's claim to match
what the suite actually delivers.

## 3. Scope

### 3.1 In scope

Two new chapter-9 §5 groups, with new scenario files under
[`conformance/scenarios/`](../../conformance/scenarios/):

#### 3.1.a Group `Integrator atomicity` — citing chapter 06 §3.4 + §5.3

Asserts the wire-observable projection of the atomic-three invariant
on the **two** wire-visible artifacts (field + event), and the
no-overwrite half of repeat-promotion idempotency.

- **cross-artifact-consistency-on-success.** Single test that asserts
  both directions of the atomic-three (field, event) projection in
  one composed assertion: after `POST /trials/{T}/integrate` returns
  2xx, (1) GET `/trials/{T}` shows `trial_commit_sha == X`, AND
  (2) the event log contains exactly one `trial.integrated` event
  for T with `data.trial_commit_sha == X`, AND (3) the field's value
  and the event's payload reference the **same** SHA — i.e. they
  are derived from the same atomic write. The cross-SHA-equality
  assertion is the chunk's central new content; the field-only and
  event-only halves are already covered by the v1 `Integrate
  idempotency` group. Cites: [chapter 06 §3.4](../../spec/v0/06-integrator.md)
  atomic-write.
- **divergent-resubmit-leaves-no-second-event.** The existing v1
  `test_different_sha_returns_409` asserts the field is unchanged
  but does **not** explicitly assert the event log shows exactly
  one `trial.integrated` (not two). This test pins that AND that
  the field's stored SHA is still the original — the §5.3
  no-overwrite rule projected onto the event side. Cites: chapter 06
  §5.3.

The cross-consistency direction-dual test (event-implies-field as a
distinct test) was considered and dropped: it is logically the same
invariant as field-implies-event and adds little debug value over a
single composed assertion. Likewise, "failed-integrate-leaves-no-
artifacts" was considered but moved to the §3.1.b group, where it
belongs (it is a §2 precondition rejection, not a §3.4 atomicity
test).

#### 3.1.b Group `Promotion preconditions` — citing chapter 06 §2

Asserts the §2(a) status-precondition requirement across the two
**non-`starting`** terminal-error trial statuses (`error`,
`eval_error`). The §2(b) commit_sha-set precondition is not
independently wire-observable in v0 (per §2 above); a wire-only suite
covers it only transitively through (a). The §2(c) branch-reachability
precondition is off-wire in v0 (per §2 above); a future binding
chapter MAY add it. The existing v1
`test_integrate_against_non_success_trial_returns_409` covers (a) for
the `starting` status; the two new tests below cover (a) for the
remaining two non-`starting` non-`success` statuses, completing the
status vocabulary.

Each test asserts the **end-state**, not just the wire response, per
the chunk-11c "end-state, not endpoint" pattern (
[`conformance/README.md`](../../conformance/README.md)): the response
is 4xx in `eden://error/invalid-precondition`, AND the trial's
`trial_commit_sha` is unset, AND no `trial.integrated` event was
appended for T. The composite end-state pins the §3.4 rollback-half
"a failed write produces neither field nor event" without needing a
separate §3.4 test for it.

- **`error` trial → 409 invalid-precondition + no artifacts.** §2 says
  "trials in `error` ... MUST NOT receive a `trial/*` commit." Cites
  chapter 06 §2.
- **`eval_error` trial → 409 invalid-precondition + no artifacts.**
  Same MUST, different status. Cites §2.

That's **4 new scenarios across two new groups**, plus the spec
amendment to chapter 9 §4 + §5 index.

### 3.2 Out of scope (deferred)

Per §2 above, all of the following remain explicitly NOT asserted by
the v1+roles+integrator suite. The amended chapter 9 §4 records this
as deliberate, not an oversight:

- §1.3 work/* discipline.
- §1.4 reachability check.
- §3.2 squash shape.
- §3.3 commit message format.
- §4.x eval-manifest path/shape/contents.
- §5.1 partial-write rollback (server-side internal; wire only sees
  the post-rollback state, which the §3.1.b precondition tests
  pin via their composite end-state assertion).
- §5.2 ref-durability.

A future spec lineage that introduces a "git-access conformance
contract" (the IUT exposes its bare-repo path through an extended
adapter) MAY add a new level (e.g., `v2+integrator-git`) that asserts
these. That is **not** chunk 11d.

### 3.3 Non-goals

- No changes to the harness adapter contract (`IutAdapter` /
  `IutHandle`). Git access is not a v1+roles+integrator IUT contract;
  see §3.2.
- No changes to `eden_storage` or `eden_wire`. The reference impl
  already passes the chapter-06 §3.4 wire projection (chunk-11b's
  `Integrate idempotency` group covers most of it; the new group's
  delta is cross-artifact consistency, which the reference impl's
  composite-commit primitive already enforces).
- No changes to the integrator package (`eden_git`). The integrator's
  squash/manifest behavior is not under conformance assertion in v0.

## 4. The "end-state, not endpoint" pattern (carried forward from 11c)

For the §2 precondition tests, the rejection point in the IUT's pipeline
is implementation-defined (the reference impl rejects synchronously at
the wire layer with 409 invalid-precondition; a different IUT might
defer the check to an async integrator background task). Per the
chunk-11c codified pattern in
[`conformance/README.md`](../../conformance/README.md) "Asserting
end-state, not endpoint", these tests pin the **end-state**: trial is
not promoted, no `trial.integrated` event, `trial_commit_sha` unset —
regardless of where in the IUT's call-chain the rejection surfaces.

Concretely, each precondition test asserts both:

1. The HTTP response is in the 4xx range (the wire MUST per chapter 7
   §5 + §7), AND
2. The end-state is consistent: GET `/trials/{T}` shows no
   `trial_commit_sha`, AND the event log shows no `trial.integrated`
   for T.

The 4xx-range assertion is intentionally weaker than `== 409`: an IUT
that returns 400 bad-request for a malformed precondition would still
be conforming (the chapter-7 §7 vocabulary allows it), and the conformance
suite shouldn't lock in 409 specifically when chapter 06 §2 is a
**semantic** precondition, not a wire-layer constraint. The existing v1
`test_integrate_against_non_success_trial_returns_409` does pin 409 +
`invalid-precondition`; that's correct because chapter 7 §5
**explicitly** maps the integrate-precondition class to that code. The
new chapter-06-citing tests can either follow the same pattern (since
they're testing the same wire endpoint) or relax to 4xx for portability.
**Decision: follow the existing pattern (assert 409 +
`invalid-precondition`)**, matching the existing v1 test's discipline so
new tests look uniform.

## 5. Files to touch

### 5.1 Spec

- `spec/v0/09-conformance.md` — amend §4 (rewrite v1+roles+integrator
  bullet per §2.1 above) and §5 (add the two new groups to the
  v1+roles index table OR add a new v1+roles+integrator index table —
  prefer a new table to mirror §3 prose's level distinction).

### 5.2 Conformance suite

- `conformance/scenarios/test_integrator_atomicity.py` — NEW file.
  Group `Integrator atomicity`. 2 tests: cross-artifact-consistency-
  on-success (cites §3.4) and divergent-resubmit-leaves-no-second-
  event (cites §5.3).
- `conformance/scenarios/test_promotion_preconditions.py` — NEW file.
  Group `Promotion preconditions`. 2 tests: error trial → 409,
  eval_error trial → 409. Both cite §2.
- `conformance/src/conformance/harness/_seed.py` — extend with
  `drive_to_error_trial(client, *, proposal_id=None) -> str` and
  `drive_to_eval_error_trial(client, *, proposal_id=None) -> str`.
  Note: the helper `drive_to_eval_error_trial` takes
  `proposal_id` for symmetry but does not use it directly — it
  forwards to `drive_to_starting_trial(proposal_id=...)` and then
  calls `declare_trial_eval_error`.
  Both follow the chunk-11c hardening pattern: call
  `raise_for_status()` on every wire call AND assert the end-state
  trial status before returning. The `error`-trial helper drives
  through `submit_implement(status="error")` then `reject` (the
  orchestrator's reject path terminalizes the trial as error
  atomically per chapter 05 §2.2); the `eval_error`-trial helper
  drives through `drive_to_starting_trial` then
  `declare_trial_eval_error`. (Both primitives exist in `_seed`'s
  prior surface; only the composite drive-to-end-state helpers are
  new.)

### 5.3 Repo metadata

- `AGENTS.md` — current-phase header gains a chunk-11d-complete entry.
- `docs/roadmap.md` — Phase 11 chunk 11d marked complete; Phase 11
  exit criterion ("reference impl passes the full v1 suite; suite is
  documented as the conformance contract for `eden-protocol/v0`")
  marked met. Phase 11 itself transitions from chunk-pending to
  fully complete.

## 6. Reference-impl behavior (verification)

The reference impl's `Store.integrate_trial` lives at
[`reference/packages/eden-storage/src/eden_storage/_base.py`](../../reference/packages/eden-storage/src/eden_storage/_base.py)
(method `integrate_trial`). It is a single composite-commit (chapter 05
§2.2) that:

1. Validates `trial.status == "success"`; otherwise raises
   `InvalidPrecondition` (mapped to 409 `eden://error/invalid-precondition`
   at the wire layer per chapter 7 §5/§7).
2. Validates same-value idempotency: if `trial.trial_commit_sha`
   equals the request body's `trial_commit_sha`, returns no-op (no
   second write, no second event).
3. Validates no-overwrite: if `trial.trial_commit_sha` is set and
   differs from the request body's value, raises `InvalidPrecondition`.
4. On success, writes the new `trial_commit_sha` field and emits
   `trial.integrated` in one transaction.

The reference impl does **not** inspect `trial.commit_sha` at integrate
time; the §2(b) precondition is enforced transitively by the trial-
status lifecycle (a trial cannot reach `success` without `commit_sha`
set; see chunk-11c hardening of `drive_to_starting_trial` /
`drive_to_success_trial`).

The new tests are **not expected to fail** against the reference impl;
they extend coverage of cross-artifact consistency (already enforced
by the composite-commit primitive) and broaden status-precondition
coverage to `error` and `eval_error` (which the existing
`Store.integrate_trial` rejects via the same `status != "success"`
branch as the existing v1 `starting` test). If any new test reveals
a gap, it will surface at run time and the resolution is one of:
amend the spec, fix the impl, fix the suite (per chapter 9 §7 — the
reference impl does not have authority to reinterpret the spec).

## 7. Test-design notes

### 7.1 Cross-artifact-consistency tests are stronger than per-artifact tests

The chunk's central new content is the **cross-artifact consistency**
assertion: it's not enough to verify "field is set" and "event was
emitted" independently; the new tests assert that they reference the
**same** SHA. A bug where the composite-commit writes the field but
emits the event with a stale SHA (or vice versa) would not be caught
by the existing v1 tests; it would be caught by the new tests.

This is also the right shape because it's **wire-observable** — a
client that reads the field and the event sees the same value.

### 7.2 Helper hardening discipline (per chunk-11c codification)

`drive_to_error_trial` and `drive_to_eval_error_trial` MUST verify
their own postconditions before returning. Specifically:

- `drive_to_error_trial` asserts `read_trial(...).get("status") ==
  "error"` after `accept` of the implement task.
- `drive_to_eval_error_trial` asserts `read_trial(...).get("status")
  == "eval_error"` after `declare_trial_eval_error`.

This matches the codified rule in
[`conformance/README.md`](../../conformance/README.md) "Hardening
setup helpers".

### 7.3 Citation-check discipline

Each test's docstring first line MUST cite a real chapter-06 MUST
section. Each new scenario file MUST declare
`CONFORMANCE_GROUP = '<group name>'`, where the group name matches
one of the two new chapter-9 §5 entries. Each test's citation MUST
fall inside its group's chapter-9 §5 entry's spec range.

The check is automated by
[`conformance/src/conformance/tools/check_citations.py`](../../conformance/src/conformance/tools/check_citations.py)
and runs in CI after pytest. Adding a group to chapter 9 §5 without
a citing test fails the check ("no valid citing test declares
CONFORMANCE_GROUP = ... AND cites one of: ..."). Per-test
group-relevance is also enforced.

## 8. Verification plan

Before opening the PR:

1. `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"`
   passes — covers the chapter-9 amendment + the new plan + roadmap.
2. `python3 scripts/spec-xref-check.py` passes — chapter-9 §4 amendment
   may add cross-references (chapter 06 §3.4 / §2 / §5.3).
3. `pipx run 'check-jsonschema==0.29.4' --check-metaschema spec/v0/schemas/*.schema.json`
   passes — no schema changes expected, but defensive run.
4. `uv run ruff check .` passes — new scenario files + harness extension.
5. `uv run pyright` passes — typed harness extension; new scenarios
   are type-checked.
6. `uv run pytest -q` passes (full reference test suite, including
   the chunk-11c v1+roles scenarios). Total scenario count rises
   from 106 to ~110.
7. `uv run python conformance/src/conformance/tools/check_citations.py`
   passes — every new test cites a real MUST; every new group has a
   valid citing test; per-test relevance check passes.
8. CI on the PR is fully green (12 existing jobs).

## 9. Risks

### 9.1 Chapter-9 amendment scope creep

The §4 amendment is a single paragraph that **scopes** v1+roles+integrator
to wire-observable MUSTs. Codex review of the plan may push to also
land the "future binding chapter for git-access conformance" prose.
Decision: leave a forward-pointer in the amendment ("a future spec
lineage MAY ...") but do **not** define that future contract in this
chunk. Scope discipline > prose completeness.

### 9.2 Citation-check group bleed

The new `Promotion preconditions` group cites §2; the existing
`Integrate idempotency` group cites chapter 7 §5, which itself
**references** chapter 06 §3.4. The citation-check matches
chapter+section pairs (it does NOT follow cross-references), so the
two groups don't bleed at the check level. This is correct: the v1
test cites the **HTTP-layer** rule; the v1+roles+integrator test
cites the **integrator-layer** rule. Both citations are
independently true MUSTs.

### 9.3 Test redundancy

The new `divergent-resubmit-leaves-no-second-event` test partially
overlaps with the v1 `test_different_sha_returns_409` — both submit
the same wire shape. Decision: keep both. Each cites a different
chapter and asserts a different invariant (HTTP idempotency vs
chapter-06 §5.3 no-overwrite). The test-redundancy cost is small
(one wire round-trip) and the assertion strengthening is real
(the new test additionally asserts the event log's cardinality).

### 9.4 Trial-status helper coverage

`drive_to_error_trial` exercises a path the existing test_implementer_submission.py
already covers in chunk-11c (status=error terminalizes trial as
error). The new helper just consolidates the harness shape. No
spec-level surprise expected.

`drive_to_eval_error_trial` uses `declare_trial_eval_error` directly
(orchestrator-level call, exposed on the wire as `POST
/trials/{T}/declare-eval-error`). The reference impl's
`_base._declare_trial_eval_error` writes the trial-status transition
together with `trial.eval_errored` atomically per chapter-05 §3.3.
The helper should work without surprise.

## 10. What this chunk explicitly does NOT do

- Does **not** add git-access tests. Squash, manifest, work/*, and
  reachability MUSTs remain unasserted by the conformance suite.
- Does **not** define a future "v2+integrator-git" level. That is a
  future spec lineage decision.
- Does **not** rewrite the existing `Integrate idempotency` v1
  group. Those tests cite chapter 7 §5 and stay there.
- Does **not** modify the IUT adapter contract.
- Does **not** add control-plane / multi-experiment scenarios (Phase 12).
