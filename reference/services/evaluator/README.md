# eden-evaluator-host

Reference evaluator worker: claims pending `evaluate` tasks, emits deterministic metrics matching the experiment's `metrics_schema`, and submits.

## Run

```bash
python -m eden_evaluator_host \
  --task-store-url http://127.0.0.1:8080 \
  --experiment-id exp-1 \
  --worker-id evaluator-1 \
  --experiment-config tests/fixtures/experiment/.eden/config.yaml
```

Runs until SIGTERM.

## Subprocess mode

Pass `--mode subprocess` plus `--experiment-dir <path>`,
`--repo-path <path>`, and (optionally) `--worktrees-dir <path>` to
invoke a user-supplied per-task evaluate command instead of the
scripted profile. The command string is read from the
experiment-config YAML's `evaluate_command` key.

For each evaluate task the host creates a worktree at
`trial.commit_sha`, runs the command with cwd=wt and env
carrying `EDEN_TASK_JSON` / `EDEN_OUTPUT` / `EDEN_WORKTREE` /
`EDEN_EXPERIMENT_DIR`, then reads `<wt>/.eden/eval-outcome.json`
(`status` + `metrics`). Metrics are validated against
`metrics_schema` before submit; type mismatches route to
`status=eval_error`. See the
[reference binding](../../../spec/v0/reference-bindings/worker-host-subprocess.md).
