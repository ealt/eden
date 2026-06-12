**Round-2 Status**
The main round-2 concerns are materially addressed. The publisher trust-root design is now concrete, task pods no longer receive a Kubernetes API token, evaluator reporting is moved out of the user container, and the plan now includes a reporter/publisher deadline.

I still found a few substantive cleanup items before I’d call it implementation-ready.

**Completeness**
- The reporter deadline is still not quite safe as written. `EDEN_OUTCOME_DEADLINE_SECONDS = activeDeadlineSeconds - margin` only works if the reporter starts when the Job deadline starts. It does not account for scheduling, image pull, or init-clone time before regular containers start ([plan](</Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:773>)). Use an absolute deadline computed by the host at Job creation, or otherwise subtract elapsed time before the reporter waits.

- The two-ServiceAccount story needs one explicit chart contract. The values sketch has only `workers.serviceAccount.name` for the host SA ([plan](</Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:571>)), while Decision 10 introduces `<release>-eden-task` for task pods ([plan](</Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:505>)). Spell out whether the task SA is always chart-created, what happens under `workers.rbac.create=false`, and whether operators must precreate both SAs in GitOps-managed RBAC mode.

- The pod-exec image contract still has a stale `python3` requirement in Decision 3 ([plan](</Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:329>)), but §3.6 now correctly says Python is no longer required in the user image because reporting/publishing moved to chart-image containers ([plan](</Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:813>)). Update the minimum user-image surface to match the new design.

**Overall**
The architecture is sound now. I don’t see a remaining reason to revisit the main approach; the remaining issues are plan precision around deadlines, service-account ownership, and the image contract.