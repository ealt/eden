**Findings**

Medium: [docs/plans/issue-131-auto-checkpointing.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/docs/plans/issue-131-auto-checkpointing.md:380) says the smoke drives termination via `termination=auto`, but the plan does not specify the smoke-local config changes needed to make that deterministic. The current fixture has no `dispatch_mode.termination: auto` and no `termination_policy`. Add an explicit smoke config block, likely `dispatch_mode: { termination: auto }` plus `termination_policy: { kind: max_variants, target: 3 }`, and include that in the Wave 4 file/change list.

Medium: [docs/plans/issue-131-auto-checkpointing.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/docs/plans/issue-131-auto-checkpointing.md:302) says `sanitize(id) + sha256(id)[:8]` makes the prefix “collision-free.” Eight hex chars is collision-resistant, not collision-free. Either use a reversible encoding, use the full hash, or soften the claim and tests to “practically collision-resistant.” Since pruning is destructive, I’d avoid an overclaim here.

Low: [docs/plans/issue-131-auto-checkpointing.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/docs/plans/issue-131-auto-checkpointing.md:350) only says fail fast when no destination dir is resolved. Tighten this to say the CLI validates the resolved path exists/is a directory/is writable, or creates it deliberately. Otherwise a missing or unwritable path becomes best-effort warning churn after startup, which undercuts the fail-fast promise.

Low: [docs/plans/issue-131-auto-checkpointing.md](/Users/ericalt/Documents/eden-worktrees/plan-issue-131-auto-checkpointing/docs/plans/issue-131-auto-checkpointing.md:69) still shows filenames as `<experiment_id>-...`, while the implementation design now uses `<safe_exp>-...`. Update the naming map and D6 text so a fresh implementer does not glob/prune on raw IDs.

**Assessment**

No high-severity blockers remain. The prior D4 concern is fixed: admin bearer for export is coherent and does not require a wire/spec authority change. D6 is now scoped honestly to observed `terminated` state, and the repo-bundle issue is correctly framed as inherited 12b incompleteness rather than hidden auto-checkpoint scope.

Overall: this plan is implementable after tightening the smoke fixture details and the filename/destination wording.