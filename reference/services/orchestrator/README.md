# eden-orchestrator

Reference orchestrator service. Connects to a task-store-server via HTTP and runs the chapter-3 §6 orchestrator role: per iteration, the termination decision, ideation-task creation (per the experiment's `ideation_policy`), execution/evaluation dispatch, terminal finalization, and integration — each gated by the experiment's `dispatch_mode`. Workers live in separate processes (`eden-ideator-host`, `eden-executor-host`, `eden-evaluator-host`).

## Run

```bash
python -m eden_orchestrator \
  --task-store-url http://127.0.0.1:8080 \
  --experiment-id exp_0123456789abcdefghjkmnpqst \
  --worker-id wkr_0123456789abcdefghjkmnpqst \
  --admin-token "$EDEN_ADMIN_TOKEN" \
  --repo-path /var/lib/eden/repo \
  --experiment-config .eden/config.yaml
```

`--worker-id` is the setup-minted opaque `wkr_*` id (see `setup-experiment.sh`); the orchestrator self-joins the reserved `orchestrators` group at startup. Behavior knobs (`termination_policy`, `ideation_policy`, `max_quiescent_iterations`) come from the experiment-config YAML and win over the corresponding CLI flags.

Notable optional flags (see `python -m eden_orchestrator --help` for the full list):

- `--forgejo-url` + `--credential-helper` — treat Forgejo as the git remote of record: clone/fetch before integrating, push `variant/*` refs back, reconcile remote orphans at startup.
- `--termination-policy module:callable` — override the chapter-3 §6.2 decision-type-0 policy.
- `--auto-checkpoint-dir` — enable cadence + on-terminate automatic checkpoint export (issue #131).
- `--max-quiescent-iterations` — exit 0 after N consecutive zero-progress iterations; `0` means never exit on quiescence (the Kubernetes long-running posture).
- `--control-plane-url` (or `$EDEN_CONTROL_PLANE_URL`) — switch to chapter-11 multi-experiment mode: acquire/renew per-experiment leases and drive only leased experiments.

## Shutdown

SIGTERM / SIGINT breaks the loop (releasing any held leases); the process exits 0.
