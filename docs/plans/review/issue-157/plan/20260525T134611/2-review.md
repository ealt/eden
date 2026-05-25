**1. Missing Context**

Assessment: mostly resolved. The plan now explains the single-experiment vs. multi-experiment split much more clearly.

- One context thread is still muddy because the control-plane overlay is described two different ways. Section 3.5 correctly says [`compose.control-plane.yaml`](../../reference/compose/compose.control-plane.yaml) has no orchestrator service today and therefore needs no edit ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:276)), but later sections still talk as if that overlay contains a multi-experiment orchestrator service to preserve/update ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:309), [issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:395)). That needs one canonical statement.

**2. Feasibility**

Assessment: the previously blocking design problems look fixed.

- The `DurationStr` approach with a positive-duration validator is implementable and matches the repo’s strict-model discipline ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:49), [issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:132)).
- The orchestrator-warning approach is also feasible and avoids the earlier silent no-op problem in the design section itself ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:244)).

I do not see a new feasibility blocker.

**3. Alternatives**

Assessment: the chosen approach is reasonable.

- Deferring `ideas_per_ideation` instead of forcing an asymmetric partial promotion is the right move here ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:29)).
- Keeping the two orchestrator flags registered only because the multi-experiment branch still depends on them is a pragmatic compromise until #214 lands ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:41)).

**4. Completeness**

Assessment: this is still the blocking area. The plan is close, but it still contains several internal contradictions that would mislead implementation.

- The control-plane overlay is still inconsistent across sections. Section 3.5 says no orchestrator service exists there and no edit is needed ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:276)), but scope says that overlay’s multi-experiment orchestrator service keeps the CLI flags ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:309)), and Wave 3 again says to update that overlay’s orchestrator flags/comments ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:395)). Those cannot all be true at once.
- The single-experiment orchestrator behavior is still described inconsistently. Decision 1 and §3.4 now correctly say non-default CLI values trigger a startup warning before being ignored ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:41), [issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:244)), but the migration section still says the values are “silently ignored” / a “silent no-op” ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:352), [issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:360)).
- The file inventory still drifts. “Compose + smokes (9 files)” actually lists 10 files ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:440)). More importantly, the “not in the touch list” note for `compose.control-plane.yaml` ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:453)) conflicts with the scope and Wave 3 sections that still claim edits there ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:309), [issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:395)).
- Wave 3 still says to “drop the worker-host deadline flags wherever they appear” ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:394)) even though §3.5 now says they do not appear in compose files today ([issue-157-cli-flags-to-config.md](/Users/ericalt/Documents/eden-worktrees/issue-157-plan/docs/plans/issue-157-cli-flags-to-config.md:274)). That is small, but it is another stale instruction that should be tightened.

I’m stopping here rather than going deeper into edge cases/risks, because these completeness contradictions should be cleaned up first.

**Overall Assessment**

This is much closer. The design itself now looks sound, and the earlier feasibility blockers are addressed. The remaining problem is document coherence: a few stale sections still describe an older rollout shape. Once the control-plane references, warning semantics, and touch-list/Wave-3 inventory are made consistent, the plan should be ready for implementation.