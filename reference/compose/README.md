# EDEN reference Compose stack

A Docker Compose stack that stands up the third-party infrastructure
the EDEN reference implementation runs against locally.

**Phase 10 chunk 10a delivers infrastructure only.** The EDEN services
(`task-store-server`, `orchestrator`, `planner-host`,
`implementer-host`, `evaluator-host`, `web-ui`) are dockerized in
chunk 10b. The `setup-experiment` script lands in 10c. LLM-driven
worker hosts arrive in 10d. An end-to-end Compose integration test
ships in 10e.

## What this stack provides

| Service     | Image                                | Purpose                                             |
| ----------- | ------------------------------------ | --------------------------------------------------- |
| `postgres`  | `postgres:16.6-alpine`               | Durable backend for the EDEN task store (10b)       |
| `gitea`     | `gitea/gitea:1.22.6-rootless`        | Git remote for `work/*` and `trial/*` branches (10b/10c) |
| `blob-init` | `busybox:1.36.1`                     | One-shot — ensures the blob volume exists           |

| Volume                | Mounted by      | Future consumer                       |
| --------------------- | --------------- | ------------------------------------- |
| `eden-postgres-data`  | `postgres`      | task-store-server backend (10b)       |
| `eden-gitea-data`     | `gitea`         | git host data (10b/10c)               |
| `eden-blob-data`      | `blob-init`     | implementer-host artifact storage (10d) |

**Postgres backs the EDEN task store, not Gitea.** Gitea uses its
own embedded SQLite. Pointing Gitea at our Postgres would couple the
git host's recovery story to the task store schema; production
deployments must be free to swap either component independently.

## Prerequisites

- Docker Desktop **or** Docker Engine with the Compose v2 plugin
  (`docker compose` ≥ v2.20).
- `jq` and `curl` are required for `healthcheck/smoke.sh`.

## Quickstart

```bash
cd reference/compose
cp .env.example .env
docker compose up -d --wait
docker compose ps
```

Expected output:

- `eden-postgres` — `Up (healthy)`
- `eden-gitea` — `Up (healthy)`
- `eden-blob-init` — `Exited (0)`

## Connection details (with `.env.example` defaults)

| Service    | Host endpoint              | Default credentials                       |
| ---------- | -------------------------- | ----------------------------------------- |
| Postgres   | `localhost:5433`           | user `eden` / db `eden` / password from `.env` |
| Gitea HTTP | `http://localhost:3001/`   | no admin user yet (created in 10c)        |
| Gitea SSH  | `ssh://git@localhost:2222` | (admin user not provisioned until 10c)    |
| Blob volume | `eden-reference_eden-blob-data` (Docker named volume) | mounted at `/var/lib/eden/blobs` by future consumers |

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

- Dockerfiles for the EDEN services themselves — added in 10b.
- A `PostgresStore` backend in `eden-storage` — added in 10b
  alongside the dockerized `task-store-server`.
- The `setup-experiment` script — added in 10c.
- An admin user / API token for Gitea — created by the
  `setup-experiment` script in 10c.
- LLM-backed worker hosts — added in 10d.
- An end-to-end Compose integration test — added in 10e.
