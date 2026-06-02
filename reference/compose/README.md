# EDEN reference Compose stack

A Docker Compose stack that runs the EDEN reference implementation
end-to-end locally: third-party infrastructure (Postgres, Forgejo) plus
the six EDEN services (`task-store-server`, `orchestrator`,
`ideator-host`, `executor-host`, `evaluator-host`, `web-ui`), plus a
one-shot `eden-repo-init` setup service.

Durable substrate state (postgres data, forgejo data, artifacts,
per-host bare clones, per-host worker credentials) lives as host
bind-mounts under `${EDEN_EXPERIMENT_DATA_ROOT}/<subdir>/` (default
`$HOME/.eden/experiments/$EDEN_EXPERIMENT_ID/`) per Phase 12a-1g.
See [`../../docs/operations/experiment-data-durability.md`](../../docs/operations/experiment-data-durability.md).

## What this stack provides

### Infrastructure

| Service     | Image                                | Purpose                                             |
| ----------- | ------------------------------------ | --------------------------------------------------- |
| `postgres`  | `postgres:16.6-alpine`               | Durable backend for the EDEN task store (`PostgresStore`) |
| `forgejo`     | `codeberg.org/forgejo/forgejo:11-rootless`        | Git remote for `work/*` and `variant/*` branches |

### EDEN reference services (10b)

| Service             | Module                              | Purpose                                                |
| ------------------- | ----------------------------------- | ------------------------------------------------------ |
| `task-store-server` | `eden_task_store_server`            | Hosts the `Store` over the wire protocol               |
| `orchestrator`      | `eden_orchestrator`                 | Runs finalize → dispatch → integrate to quiescence     |
| `ideator-host`      | `eden_ideator_host`                 | Scripted ideator worker                                |
| `executor-host`  | `eden_executor_host`             | Scripted executor worker; writes work commits       |
| `evaluator-host`    | `eden_evaluator_host`               | Scripted evaluator worker                              |
| `web-ui`            | `eden_web_ui`                       | Backend-for-frontend Web UI on `localhost:${WEB_UI_HOST_PORT}` |
| `control-plane`     | `eden_control_plane_server`         | Always-on chapter-11 control plane (registry + leases + state-sync) on `localhost:${CONTROL_PLANE_HOST_PORT:-8081}` |

### Setup-time services (10c)

| Service          | Image                | Purpose                                                |
| ---------------- | -------------------- | ------------------------------------------------------ |
| `eden-repo-init` | `eden-reference:dev` | Profile `setup`. Idempotent bare-repo seed; invoked by setup-experiment |

### Storage layout (Phase 12a-1g)

Every durable substrate is a host bind-mount under
`${EDEN_EXPERIMENT_DATA_ROOT}/<subdir>` (default
`$HOME/.eden/experiments/$EDEN_EXPERIMENT_ID/<subdir>`). See
[`docs/operations/experiment-data-durability.md`](../../docs/operations/experiment-data-durability.md)
and [`spec/v0/01-concepts.md`](../../spec/v0/01-concepts.md) §13.

| Substrate subdirectory                          | Mounted by                | Substrate role                                       |
| ----------------------------------------------- | ------------------------- | ---------------------------------------------------- |
| `<DATA_ROOT>/postgres/`                         | `postgres`                | task-store-server's `PostgresStore` backend          |
| `<DATA_ROOT>/forgejo/`                            | `forgejo`                   | git remote for `work/*` and `variant/*` refs         |
| `<DATA_ROOT>/orchestrator-repo/`                | `orchestrator`            | per-host bare clone of the Forgejo repo                |
| `<DATA_ROOT>/executor-repo/`                    | `executor-host`           | per-host bare clone of the Forgejo repo                |
| `<DATA_ROOT>/evaluator-repo/`                   | `evaluator-host` (subprocess overlay) | per-host bare clone for subprocess evaluator |
| `<DATA_ROOT>/web-ui-repo/`                      | `web-ui`                  | per-host bare clone for the web-ui executor module   |
| `<DATA_ROOT>/artifacts/`                        | `web-ui`, `ideator-host`, `executor-host` (ro) | Artifact store / idea markdown   |
| `<DATA_ROOT>/credentials/{orchestrator,ideator,executor,evaluator,web-ui}/` | per-host services | Persisted per-worker registration tokens             |

The two remaining named volumes are intentionally **ephemeral** —
they hold per-task scratch or bootstrap-staging state that is
recreated on each setup or restart, and they are NOT covered by
the chapter-01 §13 durability invariant:

| Named volume             | Mounted by                          | Why ephemeral                                                  |
| ------------------------ | ----------------------------------- | -------------------------------------------------------------- |
| `eden-repo-init-staging` | `eden-repo-init`                    | bootstrap-only; setup-experiment `docker volume rm`s on reseed |
| `eden-worktrees`         | `executor-host`/`evaluator-host` (subprocess overlay) | per-task scratch worktrees; per-host startup `git worktree remove --force` already assumes they don't survive restart |

The opt-in log-search overlay (`compose.logging.yaml`, below) adds two
more host bind-mount dirs under the data root — `<DATA_ROOT>/loki/`
(Loki index + chunks) and `<DATA_ROOT>/alloy/` (Alloy file-tail
positions). Both are **DERIVED / observability** storage, NOT
protocol-owned durable state (chapter 01 §13): they are a queryable
projection of `logs/`, rebuild by re-ingesting, and are NOT covered by
the §13 durability invariant. `setup-experiment.sh` creates them
unconditionally (they stay empty until the overlay runs); the same
`rm -rf ${EDEN_EXPERIMENT_DATA_ROOT}` reset wipes them.

**Postgres backs the EDEN task store, not Forgejo.** Forgejo uses its
own embedded SQLite. Pointing Forgejo at our Postgres would couple the
git host's recovery story to the task store schema; production
deployments must be free to swap either component independently.

**Forgejo is the workers' canonical git remote.** Each worker keeps a
per-host bare clone under `${EDEN_EXPERIMENT_DATA_ROOT}/<service>-repo/`
and fetches from / pushes to Forgejo over plain HTTP using a generated
credential helper (see `setup-experiment.sh` — the Forgejo password is
preserved in `.env` across re-runs). The integrator publishes
`variant/*` refs back to Forgejo per chapter 6 §3.4 (Phase 10d
follow-up B landed this).

**The web-ui executor module overlaps with `executor-host`.**
Passing `--repo-path` to web-ui activates the full executor
module (chunk 9c). Both can claim execution tasks; whoever wins the
claim does the work via `Store.claim`'s atomicity guarantee. The
`executor-host` is the unattended scripted worker; web-ui's
executor module is for human override / debugging. Operators
who want only one or the other can use compose `profiles:` (a
future enhancement).

## Log search overlay (`compose.logging.yaml`)

Opt-in overlay (issue #110) adding an in-stack log-search UI: **Loki**
(log store + LogQL), **Grafana Alloy** (log shipper), and **Grafana**
(search UI, pre-provisioned with the Loki datasource + an *EDEN explore*
dashboard). Alloy tails the issue-#109 per-service JSONL bind-mount
(`${EDEN_EXPERIMENT_DATA_ROOT}/logs`) read-only and ships every line to
Loki — **no docker socket** in the default overlay.

```bash
# Layer on top of any stack you'd normally bring up:
docker compose -f compose.yaml -f compose.logging.yaml --env-file .env up -d --wait
# Grafana → http://localhost:3000 (user `admin`, pw = EDEN_GRAFANA_ADMIN_PASSWORD).
```

No extra setup-experiment flag is needed (it always generates the
Grafana password + creates the `loki/`+`alloy/` data-root subdirs). Loki
and Alloy are internal-only; Grafana's host port is overridable with
`GRAFANA_HOST_PORT`. Pinned images: `grafana/loki:3.7.2`,
`grafana/alloy:v1.16.1`, `grafana/grafana:12.4.3`.

**Optional infra-stdout capture** (`compose.logging-infra.yaml`): a
second overlay, layered on top, that ALSO ships Postgres + Forgejo
container stdout (not captured by the JSONL tail). It mounts the host
docker socket read-only, so — per the privilege-isolation discipline —
it is a separate overlay and requires the `:?`-guarded
`EDEN_LOGGING_DOCKER_GID` (the in-container socket gid; see that file's
header for the probe). Validate the whole overlay with
`bash healthcheck/smoke-logging.sh`.

Full operator walkthrough + LogQL examples: [`docs/observability.md`
§2.8](../../docs/observability.md#28-log-search-ui-loki--alloy--grafana).

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

Expected output: every service `Up (healthy)`. The orchestrator
runs the experiment to quiescence (~10–30s for the fixture
experiment) and then exits 0.

**Re-running setup-experiment is safe.** Existing secrets
(`POSTGRES_PASSWORD`, `EDEN_ADMIN_TOKEN`, `EDEN_SESSION_SECRET`,
`FORGEJO_*`) are preserved across re-runs. To pick up a config
change, re-run setup-experiment and then `docker compose
--env-file .env up -d` (which detects config drift and recreates
affected services). `restart` is **not** sufficient — it doesn't
pick up changes to `command:`, env files, or `configs:`.

## Connection details (with default ports)

| Service    | Host endpoint                  | Default credentials                            |
| ---------- | ------------------------------ | ---------------------------------------------- |
| Postgres   | `localhost:5433`               | user `eden` / db `eden` / password from `.env` |
| Forgejo HTTP | `http://localhost:3001/`       | user `eden` / password from `.env` (`FORGEJO_REMOTE_PASSWORD`) — provisioned by setup-experiment |
| Forgejo SSH  | `ssh://git@localhost:2222`     | (same forgejo credential; HTTP-Basic is the default workers use) |
| Web UI     | `http://localhost:8090/`       | sign in with any worker_id                     |

Defaults intentionally avoid the well-known ports (5432, 3000, 22)
to sidestep collisions with locally-running Postgres or Forgejo
instances. Override via `.env`.

## Operations

| Task               | Command                                 |
| ------------------ | --------------------------------------- |
| Tail logs          | `docker compose logs -f`                |
| Get a `psql` shell | `docker compose exec postgres psql -U eden -d eden` |
| Stop the stack     | `docker compose stop`                   |
| Stop and clean     | `docker compose down`                   |
| Remove containers + ephemeral volumes | `docker compose down -v` |
| **Wipe all data**  | `docker compose down -v && rm -rf "$EDEN_EXPERIMENT_DATA_ROOT"` |
| Smoke test         | `bash healthcheck/smoke.sh`             |

`down -v` removes the ephemeral named volumes (`eden-repo-init-staging`,
`eden-worktrees`) — these are scratch state and safe to lose.
The durable substrates (Postgres data, Forgejo data, artifacts, per-host
clones, per-host credentials) live as host bind-mounts under
`${EDEN_EXPERIMENT_DATA_ROOT}` and are **not** affected by `down -v`.
To wipe the experiment entirely, `rm -rf "$EDEN_EXPERIMENT_DATA_ROOT"`
after the stack is down. See
[`docs/operations/experiment-data-durability.md`](../../docs/operations/experiment-data-durability.md).

## Troubleshooting

- **Port collision on 5433/3001/2222.** A developer with their own
  Postgres on 5433 (or Forgejo on 3001) will see `bind: address
  already in use`. Override `POSTGRES_HOST_PORT`, `FORGEJO_HOST_PORT`,
  or `FORGEJO_SSH_HOST_PORT` in `.env`.
- **`compose up` errors with `POSTGRES_PASSWORD must be set`.** You
  haven't copied `.env.example` to `.env` (or you've deleted a
  required variable). The Compose file uses `${VAR:?msg}` syntax to
  fail fast rather than silently fall back to a weak default.
- **Forgejo healthcheck fails on first boot.** Cold-boot of the
  rootless image takes 15-25 seconds; the healthcheck has a 30s
  `start_period` to absorb this. If it consistently fails, check
  `docker compose logs forgejo` for an obvious config error.
- **`docker compose down -v` doesn't reclaim the data.** The
  project name is pinned to `eden-reference` (set via the top-level
  `name:` field in `compose.yaml`); volumes are prefixed with that
  name. If you renamed the project, use `docker volume ls` to find
  the historical names.

## Multi-experiment mode (control plane)

The `control-plane` service (chapter 11) is always-on, but it is
**opt-in** for the orchestrator and web-ui: with `EDEN_CONTROL_PLANE_URL`
empty (the default), the orchestrator runs single-experiment and the
web-ui hides the cross-experiment dashboard, so the control plane just
starts cleanly and idles. Setting `EDEN_CONTROL_PLANE_URL=http://control-plane:8081`
flips both into chapter-11 lease-driven mode (the orchestrator CLI reads
the env var as the fallback for `--control-plane-url`).

The control plane is Postgres-backed: a separate `eden_control_plane`
database in the same instance (chapter 11 §3.4 Option A), created by the
[`init-control-plane-db.sh`](init-control-plane-db.sh) postgres init
hook. That hook runs **only on a fresh Postgres data dir** (upstream
image behavior), so on a data root that predates this feature the
database won't exist and `control-plane` will fail to start. To upgrade
an existing data root, either `docker compose down -v` + re-run
setup-experiment, or create the database manually:

```bash
docker compose --env-file .env exec postgres \
  psql -U eden -d eden -c 'CREATE DATABASE eden_control_plane OWNER eden;'
```

The canonical reference for the lease lifecycle on this substrate is the
[`smoke-multi-experiment.sh`](healthcheck/smoke-multi-experiment.sh)
smoke (control-plane health, two lease-contending orchestrator replicas,
and a lease-handoff chaos drill). Note the reference impl hosts **one**
experiment per task-store-server; true multi-experiment hosting +
cross-experiment isolation is tracked in
[#254](https://github.com/ealt/eden/issues/254).

## Security note

`.env.example` ships intentionally weak development credentials
(passwords, secret keys) that are committed to the repository.
**Never use these values in production.** Generate fresh values
for any deployment that handles real data.

## What's not here yet

- Workers integrate with Forgejo as their actual git remote (deferred
  to a follow-up sub-chunk after 10d; see "Forgejo is idle" above).
- An admin user / API token for Forgejo — created when Forgejo actually
  starts being consumed.
- LLM-backed worker hosts — added in 10d.
- A comprehensive end-to-end Compose integration test (with Web UI
  walkthroughs and admin actions) — added in 10e.
