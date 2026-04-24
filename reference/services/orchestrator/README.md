# eden-orchestrator

Reference orchestrator service. Connects to a task-store-server via HTTP and runs the chapter 4/5/6 orchestrator half of the dispatch loop: finalize → dispatch → integrate. Workers live in separate processes (`eden-planner-host`, `eden-implementer-host`, `eden-evaluator-host`).

## Run

```bash
python -m eden_orchestrator \
  --task-store-url http://127.0.0.1:8080 \
  --experiment-id exp-1 \
  --repo-path /tmp/eden-bare-repo \
  --plan-tasks plan-1,plan-2,plan-3
```

Exits 0 after `--max-quiescent-iterations` consecutive zero-progress iterations.

## Shutdown

SIGTERM / SIGINT breaks the loop; the process exits 0.
