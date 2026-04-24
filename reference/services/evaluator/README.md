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
