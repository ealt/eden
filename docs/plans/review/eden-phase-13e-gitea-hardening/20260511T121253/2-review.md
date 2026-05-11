**Missing Context**

Assessment: resolved. The repo/org topology and the `eden.role` source are now explicit enough to review the design itself.

**Feasibility**

Assessment: the round-2 fixes materially improve the plan. I do not see the prior mechanism blockers in the updated `generate-access-token` / Secret-volume approach relative to [Gitea Command Line](https://docs.gitea.com/administration/command-line), [Gitea API Usage](https://docs.gitea.com/development/api-usage), and [Kubernetes Secret docs](https://kubernetes.io/docs/tasks/inject-data-application/distribute-credentials-secure/).

**Alternatives**

Assessment: no material concern. The chosen direction still looks reasonable for the plan’s goals.

**Completeness**

Assessment: this is now the first level with material problems, so I would stop here and clean up the execution contract before going deeper into edge cases and risks.

- `CRITICAL` The plan still contradicts itself about whether 13e changes the wire/protocol surface. Early sections now explicitly add `update_worker_labels(...)` as a new wire op ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:214), [eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:791), [eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1821)), but §3.10 still says “13e does not change the wire surface, the protocol, or any spec text” ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1645)). The document needs one consistent position, then the summary/scope/conformance sections need to be rewritten to match it.

- `CRITICAL` The scope/files inventory is still partly describing the pre-round-2 token design, so the plan is no longer a reliable implementation contract. The in-scope service section still says workers gain `--gitea-token-env` and read the token from env ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1687)), and the new-package table still says `_admin.py` wraps `/api/v1/users/{u}/tokens` ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1809)) even though §3.1.1 now chooses `gitea admin user generate-access-token` inside the Gitea pod ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:887)). This needs a full consistency pass across §§4-5.

- `CRITICAL` The chosen `kubectl exec` minting path is not fully carried through into the chart/RBAC surface. §3.1.1 says the bootstrap Job will `kubectl exec` into the Gitea pod and that this depends on pod/exec RBAC ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:895)), but the chart-additions table only gives the bootstrap ServiceAccount secret access and does not specify the needed `pods/exec` permissions or the bootstrap image/tooling that provides `kubectl` ([eden-phase-13e-gitea-hardening.md](/Users/ericalt/Documents/eden-worktrees/phase-13cde-substrate-plans/docs/plans/eden-phase-13e-gitea-hardening.md:1839)). The mechanism is plausible, but the implementation contract is incomplete.

I’d stop before edge cases/risks and fix those completeness drifts first.

**Overall Assessment**

The plan is much closer. The core mechanism is now plausible, but the document still needs one cleanup pass so its summary, scope, files-to-touch, and chart/RBAC surface all reflect the same design.