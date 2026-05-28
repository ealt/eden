**Missing Context**

Assessment: the plan is now mostly self-contained and the previously missing design context is there. The helper-home decision, full filename policy, and CLI asymmetry are all explicit.

- Minor stale phrasing remains at the top: the intro still says “see §3 for the one filename-convention decision left open,” but §D.2 now resolves that decision rather than leaving it open ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:29), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:184)).

**Feasibility**

Assessment: the revised approach is feasible. The plan now matches the actual code shape much better.

- The helper-home and interface issues are fixed: the plan explicitly moves the source of truth to `eden_service_common` and no longer relies on “prepend a directory to an id-derived filename” ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:148), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:165)).
- The new D.4 correctly identifies the real CLI trap: write-side URI stamping is basename-only today ([eden-manual](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/scripts/manual-ui/eden-manual:351), [eden-manual](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/scripts/manual-ui/eden-manual:358), [eden-manual](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/scripts/manual-ui/eden-manual:405)), while read-side translation is already nested-safe.

**Alternatives**

Assessment: the chosen approach still looks right.

- Putting the shared helper in `eden_service_common` is the right dependency direction. I don’t see a better alternative than the current “shared Python implementation plus CLI hand-mirror” split.

**Completeness**

Assessment: much stronger than the prior draft, with one remaining suggestion.

- The plan should add an automated check for the CLI’s write-side stamped URI shape, not just the nested-path host-translate helper. D.4 correctly says the three stamp sites must change, but the explicit test language still centers on host-translate round-trip plus a manual smoke ([plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:265), [plan](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/docs/plans/issue-168-hierarchical-artifacts-substrate.md:556)). Since the write-side basename-only bug is the subtle one, I’d pin that with an automated assertion on the returned URI from `_write_artifact_for_role`, at least for the ideation text-only path.
- Otherwise the scope/files/tests sections are now aligned, and the roadmap-entry correction is fixed.

**Edge Cases And Risks**

Assessment: the risk section now covers the main real hazards well.

- No substantive new risk gaps stand out. The no-overwrite framing is now accurate, and the `content.md` read/write/predict lockstep is clearly documented against the current `_helpers.py` reader behavior ([routes/_helpers.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:25), [routes/_helpers.py](/Users/ericalt/Documents/eden-worktrees/plan-issue-168-artifacts-layout/reference/services/web-ui/src/eden_web_ui/routes/_helpers.py:186)).

Overall assessment: this plan is now in good shape and is implementable. I’d make one last small revision to add an explicit automated test for the CLI write-side stamped URI shape, and clean up the stale “one decision left open” wording at the top, but I don’t see any remaining design-level blockers.