# eden-service-common

Shared scaffolding for the reference-impl service hosts under [`reference/services/`](../).

Not a service itself; every service process depends on it.

## What lives here

| Module | Role |
|---|---|
| `logging.py` | Structured JSON-line logging. |
| `signals.py` | SIGTERM/SIGINT handler installing a `stopping` flag. |
| `readiness.py` | `wait_for_task_store(...)` — polls the task-store with bounded backoff until the server is live. |
| `cli.py` | Shared argparse helpers: `add_common_arguments` (`--task-store-url`, `--experiment-id`, `--admin-token`, `--credentials-dir`, `--log-level`), `add_exec_arguments` (`--exec-mode`, `--exec-image`, `--exec-volume`, `--exec-bind`, `--cidfile-dir`, `--exec-network`), `add_substrate_arguments` (`--artifact-url`, `--artifact-path-root`, `--readonly-store-url`, `--repo-path`). |
| `auth.py` | Post-12a-1 worker-credential bootstrap: `resolve_worker_bearer` / `bootstrap_worker_credential` — verify the persisted per-worker token via `/whoami`, reissue under the admin token when stale. |
| `scripted.py` | Canonical `ideation_fn` / `execution_fn` / `evaluation_fn` used by the scripted worker hosts. |
| `subprocess_runner.py` | `--mode subprocess` machinery: spawn a user-supplied `*_command`, JSON-line exchange, timeout + SIGKILL escalation. |
| `container_exec.py` | `--exec-mode docker` DooD wrap: `wrap_command`, per-spawn cidfiles, `post_kill_callback`, `reap_orphaned_containers`. |
| `worktrees.py` | Per-task git-worktree lifecycle for the executor/evaluator subprocess modes (host-scoped subdirs, path-scoped cleanup). |
| `experiment_config.py` | Load + validate the experiment-config YAML for the service hosts. |
| `artifacts.py` | Artifact-substrate helpers shared by the worker hosts. |
| `repo.py` | `seed_bare_repo(path)` and `ensure_*` git-clone helpers for services holding a local clone. |
| `repo_init.py` | `python3 -m eden_service_common.repo_init` — idempotent bare-repo seed (+ optional `--push-to` a Forgejo remote); used by setup-experiment. |

Nothing here is normative; a third-party service in any language needs none of it.
