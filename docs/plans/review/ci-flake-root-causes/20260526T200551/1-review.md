**1. Missing Context**

Assessment: addressed. The revised plan now makes the inventory auditable and internally reconciled.

No significant issues at this level. The Appendix A run-ID buckets and signature list close the main reproducibility gap in [docs/plans/ci-flake-root-causes.md](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:466), and the 84-run accounting now adds up cleanly in [§2.2](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:69).

**2. Feasibility**

Assessment: mostly sound, with one remaining substantive concern.

- The new “precise progress” rule is too narrow and can classify healthy runs as stuck. In the plan, progress is defined as `completed` increasing or `open_tasks + in_flight` decreasing [§3.1](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:149). But the real lifecycle has healthy forward transitions like `task.created -> task.claimed -> task.submitted -> task.completed` [test_e2e.py](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/services/orchestrator/tests/test_e2e.py:368), and workers explicitly claim before they later submit [workers.py](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/packages/eden-dispatch/src/eden_dispatch/workers.py:123), [workers.py](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/packages/eden-dispatch/src/eden_dispatch/workers.py:201). A long-running claimed task can therefore be making real progress while `completed` is flat and `open + in_flight` is unchanged. I would change the rule to “any lifecycle/event transition since the prior heartbeat” and use `last_transition_age_s` or a monotonic event cursor as the authority, not the current aggregate-count delta.

**3. Alternatives**

Assessment: the revised direction is right; the main adjustment is how to measure progress, not whether to add heartbeat-based diagnosis.

- The better-fitting alternative to the current count-delta rule is already implicit in your own design: use the heartbeat’s `last_transition_age_s` as the primary stuck/healthy signal, driven by any task/idea/variant lifecycle transition, rather than only by `completed`/live-count movement [§3.1](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/docs/plans/ci-flake-root-causes.md:141).

**4. Completeness**

Assessment: much improved and close to plan-ready.

No major completeness gaps remain. The shift of §3.2 to smoke/setup validation, the explicit `--data-root` migration in Chunk B, the GHCR decision in Chunk C, and the honest non-filed tracking table in §10 all move this back into `docs/plans/` territory.

**5. Edge Cases and Risks**

Assessment: one implementation risk still needs to be nailed down in the plan text.

- The smoke/setup guard needs to run after each script’s final config mutation, not merely after `setup-experiment`. Several smokes append the terminating `fixed_total` block after setup-experiment returns, for example [smoke.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/healthcheck/smoke.sh:84), [smoke-manual-mode.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/healthcheck/smoke-manual-mode.sh:66), and [smoke-multi-orchestrator.sh](/Users/ericalt/Documents/eden-worktrees/ci-flake-root-causes/reference/compose/healthcheck/smoke-multi-orchestrator.sh:70). If the shared helper validates too early, it will bless the pre-mutation config and miss the exact drift this chunk is meant to catch. The plan should say explicitly: validate the final copied config after any script-specific append/overlay mutation and before `compose up`.

**Overall Assessment**

This is materially stronger than round 0. The context, auditability, chunking, and scoping corrections are in good shape. I would treat the plan as near-ready, with one real blocker to tighten: redefine heartbeat “progress” around actual lifecycle transitions rather than the current count-delta rule, and make the smoke/setup validation point explicit as “after final per-script config mutation, before bring-up.”