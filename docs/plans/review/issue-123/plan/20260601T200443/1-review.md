**Missing Context**

No substantive gaps remain here. The revised plan is now explicit about config drift, objective semantics, the browse-vs-clone URL split, and what the automated checks can and cannot prove.

**Feasibility**

Two substantive issues remain.

- The objective compiler still validates only against schema *keys*, not schema *types*. In [§4 D.2](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:63>) `compile_objective()` checks that each `Name` exists in `schema_keys`, but `evaluation_schema` admits `text` metrics as well as numeric ones ([§3 background facts](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:36>)). An expression like `summary_score + 1` where `summary_score` is declared `text` is an experiment-global config bug, not a per-node grey case. As written, the page would likely degrade into node-by-node greys instead of a loud invalid-objective banner. The global validation step should reject any referenced metric whose declared type is not `integer` or `real`.
- The plan still has one unresolved implementation-policy fork around the “first metric” fallback. [Decision 2](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:19>) and [§4 D.2](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:65>) keep an off-by-default fallback path “implemented but gated behind a flag,” but the plan never defines what that flag is, whether it is operator-configurable, or whether it is just a local constant. For a plan at this level, that should be pinned down. My recommendation is simpler: drop the fallback implementation from this chunk entirely and defer it, since the default posture is already “off.”

**Completeness**

Mostly converged, but the plan still has a few internal contract mismatches that should be cleaned up before implementation.

- The naming/config map still says `--forgejo-web-url` uses `FORGEJO_CLONE_URL_HOST` in [§5](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:106>), which contradicts the corrected design in [decision 6](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:27>) and [§4 D.5](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:86>) that introduced a distinct `FORGEJO_WEB_URL`.
- The objective-evaluator naming map is stale. [§4 D.2](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:61>) now defines `compile_objective`, `evaluate`, `ObjectiveError`, and `ObjectiveEvalError`, but [§5](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:103>) still lists `evaluate_objective_expr` and omits `ObjectiveError`.
- The route/template handoff is still named inconsistently. [§4 D.1](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:57>) says the route passes a dict and the template renders `graph_dict|tojson`, but [§4 D.3](</Users/ericalt/Documents/eden-worktrees/plan-issue-123-variant-lineage-tree/docs/plans/issue-123-variant-lineage-tree.md:73>) still says the route renders `graph_json`. That is small, but this repo’s plans are meant to be executable contracts, so the names should line up.

**Overall Assessment**

This is close to convergence. The major round-0 concerns are fixed, and the zero spec/schema/wire/conformance framing is now defensible. I would not call it fully converged yet, though: fix the numeric-type validation for objective expressions, either remove or fully specify the latent fallback flag, and clean up the remaining naming inconsistencies. After that, I’d consider the plan ready to implement.