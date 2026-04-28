# eden-planner-host

Reference planner worker: polls a task-store-server for pending `plan` tasks, claims each in turn, drafts proposals using the canonical Phase 8b scripted profile, and submits.

Proposals carry `parent_commits=(--base-commit-sha,)` so the Integrator's reachability check has a real parent to verify against.

## Run

```bash
python -m eden_planner_host \
  --task-store-url http://127.0.0.1:8080 \
  --experiment-id exp-1 \
  --worker-id planner-1 \
  --base-commit-sha <40-hex-sha>
```

Runs until SIGTERM.

## Subprocess mode

Pass `--mode subprocess` plus `--experiment-config <path>`,
`--experiment-dir <path>`, and `--artifacts-dir <path>` to invoke a
user-supplied long-running planner command instead of the scripted
profile. The command string is read from the experiment-config
YAML's `plan_command` key. The subprocess exchanges JSON-line
messages with the host per the
[reference binding](../../../spec/v0/reference-bindings/worker-host-subprocess.md):

1. Subprocess emits `{"event": "ready"}` once on startup.
2. For each plan task, host writes a `{"event": "plan", ...}` line
   on stdin; subprocess emits any number of `proposal` lines and
   then `{"event": "plan-done", "task_id": …}` (or `plan-error`).
3. cwd = the experiment directory; env carries
   `EDEN_EXPERIMENT_DIR`.

Use `--plan-env-file path/to/env` to inject `ANTHROPIC_API_KEY` and
similar.
