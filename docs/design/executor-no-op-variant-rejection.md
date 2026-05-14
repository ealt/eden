# Executor no-op variant rejection — design discussion

**Status:** design exploration; informs a future amendment to
`spec/v0/03-roles.md` §3.3 + `spec/v0/04-task-protocol.md` §4.2 + a
matching conformance scenario. Tracked in
[issue #83](https://github.com/ealt/eden/issues/83).
**Origin:** prompted by manual-UI session 2026-05-13 (@ericalt). The
CLI helper [`reference/scripts/manual-ui/eden-manual`](../../reference/scripts/manual-ui/eden-manual)
now refuses no-op submissions defensively; this doc proposes the
normative rule that would move enforcement into the spec + store.
**Relation to current implementation:** the reference store enforces
chapter 3 §3.3 reachability (the variant `commit_sha` MUST descend
from every entry in `idea.parent_commits`) but does not enforce that
the variant tree DIFFERS from its parent. A worker can submit
`commit_sha == parent_commits[0]` or an empty commit on top of
parent, and the store will accept it.

## Premise

> An execution submission represents a candidate solution to an idea.
> A submission whose tree state is identical to the idea's parent is
> not a candidate — it is the absence of a candidate. The protocol
> should reject it at the role contract, not silently accept and
> propagate it to evaluation + integration.

This matters for three reasons:

1. **Evaluator semantics.** The evaluator's metrics describe a
   variant's *change* relative to its parent. A no-op variant has no
   change; any non-trivial metric ("did this build?", "did this pass
   tests?", "did this improve some score?") is meaningless or
   trivially-pass.
2. **Integrator squash.** Chapter 6 §3.2 builds a single squash
   commit on `refs/heads/variant/<id>-<slug>` whose tree is the
   worker tip's tree plus an `.eden/variants/<id>/evaluation.json`
   manifest. If the worker tip's tree equals the parent's tree, the
   squashed variant ref points to a commit that is effectively
   "parent + just the evaluation manifest" — indistinguishable from a
   degenerate canonical lineage entry.
3. **Search-space integrity.** Directed evolution depends on the
   population being a set of *distinct* candidate solutions. Allowing
   no-ops admits a trivial maximum-likelihood attack against any
   evaluation function that scores "produces no regression" highly.

## Goals

- Make "the variant tree MUST differ from its parent's tree" a
  normative property of chapter 3 §3.3 executor submission.
- Define "the parent's tree" precisely in both single-parent and
  multi-parent (merge) cases.
- Wire enforcement into the reference `Store.submit` (or
  `Store._accept_execute`) so the store rejects the submission with a
  closed-vocabulary error.
- Add a v1+roles conformance scenario asserting the rejection.

## Non-goals

- Detecting semantically-trivial-but-textually-different variants
  (e.g. whitespace-only edits, comment-only edits, file renames with
  unchanged content). These are *real* variants under any
  tree-equality definition; whether they're useful is the evaluator's
  judgment, not the protocol's.
- Forbidding variants that REVERT to a prior commit in the lineage
  (e.g. parent → A → B → revert-to-A's-tree). The variant tree
  differs from the immediate parent, so this is a valid candidate;
  the evaluator may score it however.

## Definitional question: what is "the parent's tree"?

The idea carries `parent_commits: list[str]` (chapter 02 §4). Three
sub-cases:

### Single parent (`len(parent_commits) == 1`)

The straightforward case. The rule: `variant.commit_sha^{tree} !=
parent_commits[0]^{tree}`. The CLI helper's current guard handles
this case.

### Multi-parent (`len(parent_commits) >= 2`)

Multi-parent ideas exist by design — chapter 02 §4 allows them so
that an idea can be "merge these two prior variants and tweak". The
canonical variant for a multi-parent idea is itself a merge commit.

Two candidate rules:

**Option A — differ from at least one parent.** The variant tree
MUST differ from at least one entry in `parent_commits`. Rejects the
literal no-op case (variant tree equals all parents — only possible
if all parents have the same tree). Accepts a "merge of A and B
where the result happens to equal A's tree" — which is a real merge
outcome (B contributed nothing new).

**Option B — differ from the natural merge result.** The variant
tree MUST differ from the three-way merge of `parent_commits`. This
is the strict reading of "the variant contributes work beyond the
merge". But it requires the store to compute a merge, which is
expensive and binding-specific (different merge strategies produce
different trees).

Recommendation: **Option A**, on two grounds. (1) Option B leaks
git-merge semantics into the spec, which is otherwise binding-agnostic;
(2) Option A's edge case ("merge where one side contributed nothing")
is rare in practice and not clearly wrong — the evaluator can flag
it via metrics if it matters.

### Zero parents (`len(parent_commits) == 0`)

Currently allowed by the schema (`parent_commits: list[str]` with no
non-empty constraint). An idea with zero parents corresponds to a
clean-slate variant — anything is a non-no-op against "nothing". The
rule should not fire for this case; the variant trivially differs
from "no parent".

(Independent question: should `parent_commits == []` be disallowed
altogether? Out of scope for this RFC.)

## Proposed normative rule

Amend `spec/v0/03-roles.md` §3.3 (executor submission) to add:

> A `VariantSubmission` with `status: success` MUST satisfy: there
> exists at least one entry `p` in `idea.parent_commits` such that
> the git tree of `commit_sha` differs from the git tree of `p`. A
> submission that violates this rule MUST be rejected with the
> closed-vocabulary error type `eden://error/no-op-variant`.
>
> This rule does not apply when `idea.parent_commits` is empty.

Amend `spec/v0/07-wire-protocol.md` §7 to add `no-op-variant` to the
closed error vocabulary.

Amend `spec/v0/04-task-protocol.md` §4.2 to clarify: the no-op check
runs after reachability and before the variant `commit_sha` write;
on rejection, neither the variant `commit_sha` field nor the
`variant.succeeded` event is emitted (consistent with the existing
"failed write produces neither field nor event" pattern from chunk
11d).

## Enforcement in the reference store

The check belongs in `Store._accept_execute` (the variant-side write
gate), not `Store.submit` (the task-side gate), because it requires
git-tree access — the store's submit path doesn't currently load
trees. The reference impl can either:

1. **Resolve trees via the integrator's `GitRepo`.** The store calls
   into a `GitRepo` helper (`tree_for_commit(sha) -> str`) to load
   the tree-of-commit for both `sha` and each parent. Adds a git
   dependency to the store's accept path.
2. **Defer to the integrator.** Let the variant land in `starting`,
   and have the integrator reject at integrate time. Cleaner
   separation but wrong semantically — the no-op submission has
   already produced a `variant.succeeded` event by then, and the
   downstream evaluation task has dispatched. The rejection wants to
   fire BEFORE the variant transitions to `success`.

Recommendation: **Option 1.** The store already imports `eden_git`
indirectly via the integrator wiring; adding a tree-load helper is
small. The alternative (variant rejection at integrate time) leaves
observable artifacts mid-pipeline, which is the exact pattern §3.4
atomicity is trying to avoid.

## Conformance scenario

Add to `conformance/scenarios/test_executor_submission.py` (chapter
9 §5 group `Executor submission`):

- `test_no_op_variant_rejected` — submit `status=success` with
  `commit_sha = parent_commits[0]` (literal no-op). Assert the
  submit returns 409 `eden://error/no-op-variant`, the variant does
  not transition to `success`, and no `variant.succeeded` event is
  emitted.
- `test_empty_commit_on_parent_rejected` — submit `status=success`
  with `commit_sha = <new commit whose tree equals
  parent_commits[0]'s tree>`. Same assertions.
- `test_multi_parent_no_op_rejected` — for an idea with two parents
  sharing a tree (or constructed such that the variant tree equals
  both), assert rejection. Skip if the IUT's idea-creation surface
  doesn't allow constructing this state.

## Open questions

1. **Where exactly does the rejection surface — at `/submit` or at
   the implicit accept inside the store?** Chunk 11c established the
   "transport-neutral semantic tests" pattern — the conformance suite
   should assert the end-state ("variant cannot be `success`") not
   the endpoint. Implementation latitude per chapter 9 §6.
2. **Should the rule cover `status=error` submissions?** No — an
   error submission represents a failed execution attempt, not a
   candidate. The variant terminalizes as `error` regardless of tree
   state. The rule fires only on `status=success`.
3. **Resubmit idempotency interaction.** Chapter 04 §4.2 says
   `status + variant_id + commit_sha` is the equivalence formula.
   If a submission is rejected as no-op, can the same submission be
   retried? Yes — the rejection is content-derived, not state-derived;
   the same shape will be rejected the same way on retry. No
   idempotency special-casing needed.
4. **Should the spec also forbid variants where the tree equals
   *any* ancestor commit, not just the parent?** Probably not. A
   variant that reverts changes is a legitimate candidate. Restrict
   the rule to the immediate parent_commits set.

## Migration

There is no migration cost. The rule is additive — it rejects
submissions that the current impl would silently accept. Workers
that were producing no-op submissions (unintentionally or
otherwise) will start getting 409s; this is the correct outcome.

The CLI guard in `reference/scripts/manual-ui/eden-manual` can be
removed once the store enforces the rule, since the store rejection
will surface the same error to the manual operator (just one round
trip later). Keeping the CLI guard until then is the right shape —
defense-in-depth for the demo workflow.
