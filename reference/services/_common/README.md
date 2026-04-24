# eden-service-common

Shared scaffolding for the reference-impl service hosts under [`reference/services/`](../).

Not a service itself; every service process depends on it.

## What lives here

| Module | Role |
|---|---|
| `logging.py` | Structured JSON-line logging. |
| `signals.py` | SIGTERM/SIGINT handler installing a `stopping` flag. |
| `readiness.py` | `wait_for_task_store(...)` — polls `GET /v0/experiments/{E}/events` with bounded backoff until the server is live. |
| `cli.py` | Shared argparse helpers (`--task-store-url`, `--experiment-id`, `--shared-token`, `--log-level`). |
| `scripted.py` | Canonical 8b `plan_fn` / `implement_fn` / `evaluate_fn` used by the scripted worker hosts. |
| `repo.py` | `seed_bare_repo(path)` — write an empty initial commit so the implementer has a base parent. |

Nothing here is normative; a third-party service in any language needs none of it.
