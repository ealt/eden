Findings:

- **major**: [spec/v0/10-checkpoints.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/10-checkpoints.md:215) still says the default import path “mints a fresh opaque `exp_*`” and cannot identity-conflict. [spec/v0/10-checkpoints.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/10-checkpoints.md:111) has the same stale “absent override it mints a fresh `exp_*`” wording. That contradicts the amended §10 language at line 200 and the single-experiment implementation at [_checkpoint.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/reference/packages/eden-storage/src/eden_storage/_checkpoint.py:685).

- **major**: [spec/v0/07-wire-protocol.md](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/spec/v0/07-wire-protocol.md:485) still defines the import header target as “the manifest’s `experiment_id` after any `as_experiment_id` rewrite.” For an unkeyed single-experiment import, the implementation checks the receiver id at [checkpoints.py](/Users/ericalt/Documents/eden-worktrees/impl-issue-128-disambiguate-names/reference/packages/eden-wire/src/eden_wire/routers/checkpoints.py:132), not the source manifest id. The main sentence says “resulting id,” but the parenthetical preserves the old rule.

The amended core paragraphs at §14.2 line 474 and §10 lines 200-209 are now aligned with the implementation, but the stale adjacent text keeps the checkpoint-import spec/impl split unresolved.

I did not find another blocking/major issue beyond that stale spec residue in this Round 2 pass. `python3 scripts/spec-xref-check.py` passes.

**CONVERGED: no**.