**Missing Context**

Assessment: substantially improved. The plan now explains the experiment-image requirement, claim expiry, and credential split clearly enough for a fresh executor.

One context tweak: the phrase “no deployment secret reachable from user code” should be scoped. It is true for the intended pod-exec user container only if the publisher path is hardened, but subprocess mode still runs user code in the same container as a host process that may hold `EDEN_ADMIN_TOKEN` / `FORGEJO_REMOTE_PASSWORD` in its own env ([plan:380](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:380), [plan:1185](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:1185)).

**Feasibility**

Two issues remain.

1. The publisher container must not trust git config/hooks written by the user container. The plan confines `FORGEJO_REMOTE_PASSWORD` to init/publisher containers ([plan:459](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:459)), but the user command can mutate the shared worktree/gitdir before publisher runs. If publisher uses the existing `GitRepo.push_ref`, it pushes to repo-configured `origin` and does not pass `--no-verify` ([repo.py:640](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/reference/packages/eden-git/src/eden_git/repo.py:640)). A malicious command could rewrite `origin` or install a `pre-push` hook, causing the credentialed publisher process or helper to leak the Forgejo password. Fix: specify a publisher-safe push path: explicit expected Forgejo URL, credential helper set per command, hooks disabled (`--no-verify` and/or `core.hooksPath=/dev/null`), no reliance on repo-local `origin`, and tests with malicious remote config + pre-push hook.

2. The pod-exec experiment-dir startup check is aimed at the wrong container. The plan says non-scripted hosts fail at startup when `--experiment-dir` is missing ([plan:307](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:307), [plan:876](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:876)), but pod-exec’s experiment dir lives in the per-task `podExec.image`, not necessarily the worker-host image. The final example sets only `executor.podExec.image` / `evaluator.podExec.image` ([plan:1172](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:1172)). Keep the host startup check for `subprocess`; for `podExec`, validate inside the task pod wrapper/init against the actual user image path and surface a clear sentinel/log failure.

**Alternatives**

Assessment: the main approach still looks right. Job-per-task plus a publisher container is a better fit than `pods/exec`, DooD-on-k8s, or forcing #290 first. I would not switch approaches; I would harden the publisher boundary.

**Completeness**

Add two items to the plan contract.

- Publisher should validate before publishing, not only leave validation to the host. Currently publisher pushes after reading `outcome.json` ([plan:667](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:667)), while host validation happens after the remote ref exists ([plan:697](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:697)). That can leave ordinary invalid-commit refs, not just indeterminate-push residue. Either have publisher validate commit shape, existence, ancestry, and no-op before push, or have host delete the remote ref on validation failure.

- Update scope/waves to include the new publisher work explicitly. `pod_publish.py` appears in the file table, but Wave 3 only names `pod_exec.py` and `pod_exec_mode.py` ([plan:1113](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:1113)). Include publisher hardening and its tests there.

**Edge Cases And Risks**

Add an explicit multi-container Job exit-code rule. If the main wrapper exits nonzero after a user-command failure, the Pod becomes `Failed`, which conflicts with the plan’s “Job-level failed is infrastructure” classification ([plan:708](/Users/ericalt/Documents/eden-worktrees/plan-issue-291-k8s-worker-modes/docs/plans/eden-phase-13f-k8s-worker-modes.md:708)). Specify that wrappers convert user-command failures into marker/sentinel outcomes and exit 0 when they can write the outcome channel.

Overall: the plan is much closer and the chosen architecture is defensible. I would do one more revision focused on publisher hardening, pod-exec image validation placement, and pre-push validation/cleanup semantics before treating it as implementation-ready.