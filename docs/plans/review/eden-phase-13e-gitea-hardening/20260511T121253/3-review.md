**Missing Context**

Assessment: resolved. The org topology and `eden.role` source are now explicit enough.

**Feasibility**

Assessment: acceptable. The switch to `gitea admin user generate-access-token`, coarse documented scopes, Secret-volume token delivery, and additive `update_worker_labels` op address the round-2 blockers.

**Alternatives**

Assessment: no material concern. The chosen direction still looks reasonable for the hardening goal.

**Completeness**

Assessment: much better. The round-3 reconciliation fixed the main internal drifts.

**Edge Cases And Risks**

Assessment: this is now the first level with material concerns.

- `CRITICAL` The plan assumes the bootstrap image already contains `kubectl`, but the actual referenced image does not. The plan says the bootstrap jobs can reuse `eden-reference:dev` because the image “already does” include `kubectl` ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1848)). The actual [reference/compose/Dockerfile](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/reference/compose/Dockerfile:1) installs `git`, `ca-certificates`, `curl`, and the Docker CLI, but not `kubectl`. Since the chosen token-mint path depends on `kubectl exec`, the plan needs to either add `kubectl` to the image explicitly or switch the bootstrap jobs to use the Kubernetes Python client already present in the image.

- `CRITICAL` The `_token.py` dispatch rule is too environment-dependent for the workflows the runbook describes. The plan says `_token.py` auto-selects `kubectl exec` vs `docker exec` based on `KUBERNETES_SERVICE_HOST` ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1817)). But the same plan has operator-run Helm-side commands such as migration/admin flows invoked from outside the cluster context (`setup-experiment-helm.sh`, `eden-gitea-admin migrate`, `rename-user`, `provision-org`) ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1502), [eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1708), [eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1852)). On an operator machine targeting Helm, `KUBERNETES_SERVICE_HOST` will usually be unset, so this heuristic would choose `docker exec` incorrectly. The plan needs an explicit execution-mode input such as `--exec-backend {kubectl,docker}` or a clearer rule that Helm-side provisioning always happens inside cluster Jobs.

**Overall Assessment**

The plan is close. The design is now mostly coherent, but the execution mechanism around token minting still has two real operational gaps: the missing `kubectl` binary in the referenced image, and the fragile auto-detection of Helm vs Compose execution context. Once those are pinned down, the plan should be in good shape for a final pass.