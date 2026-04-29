# EDEN reference Compose stack

A Docker Compose stack that runs the EDEN reference implementation
end-to-end locally: third-party infrastructure (Postgres, Gitea, blob
volume) plus the six EDEN services (`task-store-server`,
`orchestrator`, `planner-host`, `implementer-host`,
`evaluator-host`, `web-ui`), plus a one-shot `eden-repo-init` setup
service.

**Phase 10 chunks 10a + 10b + 10c are complete.** LLM-driven worker
hosts (10d) and the comprehensive end-to-end Compose integration
test (10e) are still ahead.

## What this stack provides

### Infrastructure

| Service     | Image                                | Purpose                                             |
| ----------- | ------------------------------------ | --------------------------------------------------- |
| `postgres`  | `postgres:16.6-alpine`               | Durable backend for the EDEN task store (`PostgresStore`) |
| `gitea`     | `gitea/gitea:1.22.6-rootless`        | Git remote for `work/*` and `trial/*` branches (idle this chunk; see "Gitea is idle" below) |
| `blob-init` | `busybox:1.36.1`                     | One-shot — ensures the blob volume exists           |

### EDEN reference services (10b)

| Service             | Module                              | Purpose                                                |
| ------------------- | ----------------------------------- | ------------------------------------------------------ |
| `task-store-server` | `eden_task_store_server`            | Hosts the `Store` over the wire protocol               |
| `orchestrator`      | `eden_orchestrator`                 | Runs finalize → dispatch → integrate to quiescence     |
| `planner-host`      | `eden_planner_host`                 | Scripted planner worker                                |
| `implementer-host`  | `eden_implementer_host`             | Scripted implementer worker; writes work commits       |
| `evaluator-host`    | `eden_evaluator_host`               | Scripted evaluator worker                              |
| `web-ui`            | `eden_web_ui`                       | Backend-for-frontend Web UI on `localhost:${WEB_UI_HOST_PORT}` |

### Setup-time services (10c)

| Service          | Image                | Purpose                                                |
| ---------------- | -------------------- | ------------------------------------------------------ |
| `eden-repo-init` | `eden-reference:dev` | Profile `setup`. Idempotent bare-repo seed; invoked by setup-experiment |

### Volumes

| Volume                | Mounted by                                                | Purpose                                          |
| --------------------- | --------------------------------------------------------- | ------------------------------------------------ |
| `eden-postgres-data`  | `postgres`                                                | task-store-server's `PostgresStore` backend      |
| `eden-gitea-data`     | `gitea`                                                   | git host data (idle this chunk)                  |
| `eden-blob-data`      | `blob-init`                                               | implementer-host artifact storage (10d)          |
| `eden-bare-repo`      | `orchestrator`, `implementer-host`, `web-ui`, `eden-repo-init` | Shared bare git repo                            |
| `eden-artifacts-data` | `web-ui`                                                  | Web UI's `--artifacts-dir` for proposal markdown |

**Postgres backs the EDEN task store, not Gitea.** Gitea uses its
own embedded SQLite. Pointing Gitea at our Postgres would couple the
git host's recovery story to the task store schema; production
deployments must be free to swap either component independently.

**Gitea is idle this chunk.** The roadmap reserves Gitea-as-the-
workers'-actual-git-remote for a follow-up sub-chunk after 10d:
workers currently use the local bare repo via `eden_git.GitRepo`
(subprocess `git`), and refactoring to HTTPS push/pull against
Gitea is a meaningful body of work. Gitea is in the stack so 10c's
setup-experiment can adopt it incrementally; for now it sits
healthy but unconsumed.

**The web-ui implementer module overlaps with `implementer-host`.**
Passing `--repo-path` to web-ui activates the full implementer
module (chunk 9c). Both can claim implement tasks; whoever wins the
claim does the work via `Store.claim`'s atomicity guarantee. The
`implementer-host` is the unattended scripted worker; web-ui's
implementer module is for human override / debugging. Operators
who want only one or the other can use compose `profiles:` (a
future enhancement).

## Prerequisites

- Docker Desktop **or** Docker Engine with the Compose v2 plugin
  (`docker compose` ≥ v2.20).
- `jq` and `curl` are required for `healthcheck/smoke.sh`.

## Quickstart

```bash
cd reference
bash scripts/setup-experiment/setup-experiment.sh \
    ../tests/fixtures/experiment/.eden/config.yaml \
    --experiment-id smoke-exp
cd compose
docker compose --env-file .env up -d --wait
docker compose ps
```

Expected output: every service `Up (healthy)` (or `Exited (0)` for
`blob-init`). The orchestrator runs the experiment to quiescence
(~10–30s for the fixture experiment) and then exits 0.

**Re-running setup-experiment is safe.** Existing secrets
(`POSTGRES_PASSWORD`, `EDEN_SHARED_TOKEN`, `EDEN_SESSION_SECRET`,
`GITEA_*`) are preserved across re-runs. To pick up a config
change, re-run setup-experiment and then `docker compose
--env-file .env up -d` (which detects config drift and recreates
affected services). `restart` is **not** sufficient — it doesn't
pick up changes to `command:`, env files, or `configs:`.

## Connection details (with default ports)

| Service    | Host endpoint                  | Default credentials                            |
| ---------- | ------------------------------ | ---------------------------------------------- |
| Postgres   | `localhost:5433`               | user `eden` / db `eden` / password from `.env` |
| Gitea HTTP | `http://localhost:3001/`       | no admin user (idle this chunk; provisioning lands in a follow-up) |
| Gitea SSH  | `ssh://git@localhost:2222`     | (idem)                                         |
| Web UI     | `http://localhost:8090/`       | sign in with any worker_id                     |
| Blob volume | `eden-reference_eden-blob-data` (Docker named volume) | mounted at `/var/lib/eden/blobs` by future consumers |
| Bare repo  | `eden-bare-repo` (Docker named volume; `name:` pinned in compose.yaml) | mounted at `/var/lib/eden/repo` by orchestrator/implementer/web-ui |

Defaults intentionally avoid the well-known ports (5432, 3000, 22)
to sidestep collisions with locally-running Postgres or Gitea
instances. Override via `.env`.

## Operations

| Task               | Command                                 |
| ------------------ | --------------------------------------- |
| Tail logs          | `docker compose logs -f`                |
| Get a `psql` shell | `docker compose exec postgres psql -U eden -d eden` |
| Stop the stack     | `docker compose stop`                   |
| Stop and clean     | `docker compose down`                   |
| **Wipe all data**  | `docker compose down -v`                |
| Smoke test         | `bash healthcheck/smoke.sh`             |

`down -v` is destructive — it deletes all three named volumes.
Use `down` (no `-v`) to stop containers but keep data.

## Troubleshooting

- **Port collision on 5433/3001/2222.** A developer with their own
  Postgres on 5433 (or Gitea on 3001) will see `bind: address
  already in use`. Override `POSTGRES_HOST_PORT`, `GITEA_HOST_PORT`,
  or `GITEA_SSH_HOST_PORT` in `.env`.
- **`compose up` errors with `POSTGRES_PASSWORD must be set`.** You
  haven't copied `.env.example` to `.env` (or you've deleted a
  required variable). The Compose file uses `${VAR:?msg}` syntax to
  fail fast rather than silently fall back to a weak default.
- **Gitea healthcheck fails on first boot.** Cold-boot of the
  rootless image takes 15-25 seconds; the healthcheck has a 30s
  `start_period` to absorb this. If it consistently fails, check
  `docker compose logs gitea` for an obvious config error.
- **`docker compose down -v` doesn't reclaim the data.** The
  project name is pinned to `eden-reference` (set via the top-level
  `name:` field in `compose.yaml`); volumes are prefixed with that
  name. If you renamed the project, use `docker volume ls` to find
  the historical names.

## Security note

`.env.example` ships intentionally weak development credentials
(passwords, secret keys) that are committed to the repository.
**Never use these values in production.** Generate fresh values
for any deployment that handles real data.

## What's not here yet

- Workers integrate with Gitea as their actual git remote (deferred
  to a follow-up sub-chunk after 10d; see "Gitea is idle" above).
- An admin user / API token for Gitea — created when Gitea actually
  starts being consumed.
- LLM-backed worker hosts — added in 10d.
- A comprehensive end-to-end Compose integration test (with Web UI
  walkthroughs and admin actions) — added in 10e.
