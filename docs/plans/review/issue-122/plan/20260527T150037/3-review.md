**Findings**

- Low: the spec file count is still off. `Files to touch` says `Spec (10 files)` but lists 9 spec files. See [issue-122-baseline-variant.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/docs/plans/issue-122-baseline-variant.md:289). That is editorial, but worth fixing so the inventory is trustworthy.

- Low: D.4.3 still has one slightly stale sentence after the MUST-kind change. It says the default-path baseline has “no new event semantics there beyond the optional `idea_id`,” but for baselines the `variant.started` payload now also MUST carry `kind`. See [issue-122-baseline-variant.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/docs/plans/issue-122-baseline-variant.md:117), [issue-122-baseline-variant.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-122-baseline/docs/plans/issue-122-baseline-variant.md:119). Minor wording cleanup only.

**Assessment**

The substantive gaps are closed. The legacy-experiment rule is now internally consistent, the `variant.started.kind` requirement is aligned, and the `read_experiment` helper/docstring surfaces are in scope.

I don’t see any remaining design-level or completeness-level blockers. What’s left is minor editorial consistency.

**Overall**

This looks converged. I’d fix the spec-file count and the one stale D.4.3 sentence, but otherwise I would treat the plan as ready.