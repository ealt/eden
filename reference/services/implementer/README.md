# eden-implementer-host

Reference implementer worker: claims pending `implement` tasks, writes a real commit to the shared bare repo using `GitRepo`, and submits the new SHA. Honors the full `proposal.parent_commits` list — single-parent proposals yield single-parent commits, merge proposals yield merge commits.

## Run

```bash
python -m eden_implementer_host \
  --task-store-url http://127.0.0.1:8080 \
  --experiment-id exp-1 \
  --worker-id implementer-1 \
  --repo-path /tmp/eden-bare-repo
```

Runs until SIGTERM.

## Subprocess mode

Pass `--mode subprocess` plus `--experiment-config <path>`,
`--experiment-dir <path>`, and (optionally) `--worktrees-dir
<path>` to invoke a user-supplied per-task implement command
instead of the scripted profile. The command string is read from
the experiment-config YAML's `implement_command` key.

For each implement task the host:

1. Generates `trial_id` and persists `Trial(status="starting")`
   before any repo write.
2. Creates a per-task git worktree at `parent_commits[0]`.
3. Writes `<wt>/.eden/task.json`, runs the command with cwd=wt
   and env carrying `EDEN_TASK_JSON` / `EDEN_OUTPUT` /
   `EDEN_WORKTREE` / `EDEN_EXPERIMENT_DIR`.
4. Reads `<wt>/.eden/outcome.json` (`status` + `commit_sha`),
   validates §3.3 reachability, creates `refs/heads/work/<…>`,
   and submits.

Worktrees live under `<worktrees-dir>/<container_hostname>/<task_id>/`
so cross-host sweeps cannot collide; see the
[reference binding](../../../spec/v0/reference-bindings/worker-host-subprocess.md).
