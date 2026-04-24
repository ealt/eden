# eden-git

Reference git primitives for the EDEN integrator role (spec/v0 chapter
6). Wraps the `git` CLI as subprocesses and exposes the porcelain and
plumbing operations the reference integrator needs to shape `trial/*`
commits.

This package is a thin subprocess wrapper around the local `git` binary
— the conformance contract is defined in
[`spec/v0/06-integrator.md`](../../../spec/v0/06-integrator.md), not
here. A conforming integrator that talks to Gitea, GitHub, or an
in-memory fake is equally valid; this package is simply the local-repo
reference.

Current surface (Phase 7a):

- `GitRepo` — opens an existing repository path (or creates one via
  the `init` / `init_bare` classmethods).
- Ref and object inspection: `rev_parse`, `commit_exists`,
  `resolve_ref`, `ref_exists`, `list_refs`, `is_ancestor`, `ls_tree`,
  `tree_entry_exists`, `commit_tree_sha`, `commit_parents`,
  `commit_message`, `commit_message_subject`, `read_blob`, `zero_oid`.
- Worktree management: `add_worktree`, `remove_worktree`, `prune_worktrees`,
  `list_worktrees`.
- Branch management: `create_branch`, `delete_branch`, `current_branch`.
- Plumbing for integrator squashing (Phase 7b): `write_blob`,
  `empty_tree_sha`, `write_tree_from_entries`, `write_tree_with_file`,
  `commit_tree`, `create_ref`, `update_ref`, `delete_ref`.

The integrator flow itself — observing `trial.succeeded`, shaping the
squashed commit per §3.2, atomically writing ref + `trial_commit_sha` +
`trial.integrated` per §3.4 — lands in Phase 7b.
