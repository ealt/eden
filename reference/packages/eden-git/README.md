# eden-git

Reference git primitives for the EDEN integrator role (spec/v0 chapter 6). Wraps the `git` CLI as subprocesses and exposes the porcelain and plumbing operations the reference integrator needs to shape `trial/*` commits.

This package is a thin subprocess wrapper around the local `git` binary — the conformance contract is defined in [`spec/v0/06-integrator.md`](../../../spec/v0/06-integrator.md), not here. A conforming integrator that talks to Gitea, GitHub, or an in-memory fake is equally valid; this package is simply the local-repo reference.

## Surface

### `GitRepo` (Phase 7a) — subprocess wrapper

- Factories: `GitRepo.init(path)`, `GitRepo.init_bare(path)`.
- Ref and object inspection: `rev_parse`, `commit_exists`, `resolve_ref`, `ref_exists`, `list_refs`, `is_ancestor`, `ls_tree`, `tree_entry_exists`, `commit_tree_sha`, `commit_parents`, `commit_message`, `commit_message_subject`, `read_blob`, `zero_oid`.
- Worktree management: `add_worktree`, `remove_worktree`, `prune_worktrees`, `list_worktrees`.
- Branch management: `create_branch`, `delete_branch`, `current_branch`.
- Plumbing for integrator squashing: `write_blob`, `empty_tree_sha`, `write_tree_from_entries`, `write_tree_with_file`, `commit_tree`, `create_ref`, `update_ref`, `delete_ref`.

### `Integrator` (Phase 7b) — §3.2 / §3.4 promotion

Given a `success` trial with a recorded `commit_sha`, produces the canonical `trial/*` commit (worker-tip tree + exactly the eval manifest at `.eden/trials/<trial_id>/eval.json`), atomically coupling the git ref, `trial_commit_sha`, and `trial.integrated` event per §3.4 via compensating deletes.

```python
from eden_git import GitRepo, Identity, Integrator

integrator = Integrator(
    store=store,
    repo=GitRepo(experiment_repo_path),
    author=Identity("Integrator", "integrator@example.org"),
)
result = integrator.integrate(trial_id)
```

Error hierarchy: `IntegratorError` (base), `NotReadyForIntegration` (§2 preconditions fail), `ReachabilityViolation` (§1.4 fails), `EvalManifestPathCollision` (§3.2 collision), `CorruptIntegrationState` (§5.3 corrupt), `AtomicityViolation` (§3.4 double failure).

Atomicity follows the post-promotion reading of §3.4 documented in [`spec/v0/design-notes/integrator-atomicity.md`](../../../spec/v0/design-notes/integrator-atomicity.md).

### Integration with `eden-dispatch`

The standalone orchestrator service (`reference/services/orchestrator`) calls `eden_dispatch.run_orchestrator_iteration` with `integrate_trial=integrator.integrate`, so successful trials are promoted through the real integrator.
