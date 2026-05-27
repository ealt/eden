Converged.

The two round-2 changes close the remaining review points:

- The intro wording now correctly says D.2 resolves the filename convention rather than leaving it open ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:38)).
- The plan now explicitly adds the right automated CLI test for the real bug: `_write_artifact_for_role` currently returns basename-only URIs at the three stamp sites ([eden-manual](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/scripts/manual-ui/eden-manual:351), [eden-manual](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/scripts/manual-ui/eden-manual:358), [eden-manual](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/scripts/manual-ui/eden-manual:405)), and the updated test inventory now pins the returned nested URI shape directly ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:447)).
- Wave 1 and the helper-home sections are aligned with the decided `eden_service_common` design.

I don’t see any remaining design-level or document-consistency blockers. This looks ready to implement.