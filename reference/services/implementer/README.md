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
