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
