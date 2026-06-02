# EDEN observability guide

> **Last verified against commit `ff3d4a9` (Phase 12c).** Companion to [`docs/user-guide.md`](user-guide.md). EDEN moves fast — if a route, schema, or port doesn't match what you see, trust the code.

This guide enumerates every place you can look at live state in an EDEN deployment: the first-party surfaces that ship with the Compose stack, and the third-party admin UIs you can attach to the same network for deeper introspection. It assumes you already have a stack up per [`user-guide.md` §2](user-guide.md#2-workflow-0-setting-up-an-experiment).

For the model — what tasks / ideas / variants / events / claims are — see [`glossary.md`](glossary.md).

## Contents

1. [What "observability" means here](#1-what-observability-means-here)
2. [First-party surfaces](#2-first-party-surfaces)
3. [Bring-your-own admin UIs](#3-bring-your-own-admin-uis)
4. [Following a variant end-to-end](#4-following-a-variant-end-to-end)
5. [Alternatives and informational notes](#5-alternatives-and-informational-notes)

## 1. What "observability" means here

EDEN's state lives in four substrates:

| Substrate | What's in it | How it's persisted |
|---|---|---|
| **Task store** (Postgres) | Tasks, ideas, variants, submissions, events, workers, groups, dispatch decisions, leases, experiment state | Host bind-mount at `${EDEN_EXPERIMENT_DATA_ROOT}/postgres/` (12a-1g) |
| **Git remote** (Forgejo) | The experiment repo: seed commit + `work/*` (executor scratch) + `variant/*` (integrated lineage) + `main` | Host bind-mount at `${EDEN_EXPERIMENT_DATA_ROOT}/forgejo/` |
| **Artifacts** | Worker-emitted blobs: idea content, evaluation outputs | Host bind-mount at `${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/` ⇄ `/var/lib/eden/artifacts/` inside web-ui; served at `GET /artifacts?uri=file://...` (session-authed). **Empty in scripted mode** — only subprocess mode writes real files. |
| **Process logs** | Container stdout from each service | Captured by Docker's logging driver (`json-file`, 50MB × 5 per service); ephemeral. **Issue #109** also persists each service's JSON-line log to `${EDEN_EXPERIMENT_DATA_ROOT}/logs/<service>/<service>.jsonl` (host bind-mount; rotates at 50MB × 5; survives `compose down -v`). The opt-in Loki + Alloy + Grafana overlay ([§2.8](#28-log-search-ui-loki--alloy--grafana)) indexes that JSONL substrate for cross-service / time-window search. |

Every observability tool below reads from one or more of these. The event log in the task store is the closest thing to a single source of truth — every state change of consequence emits an event, and the orchestrator's loop is driven entirely by reads against it.

## 2. First-party surfaces

These ship with the Compose stack. No extra setup beyond `setup-experiment.sh` + `compose up`.

### 2.1 Web UI admin dashboards (`http://localhost:8090/admin/*`)

Sign in with principal `admin`, secret from `EDEN_ADMIN_TOKEN` in `.env`.

| Route | Shows |
|---|---|
| `/admin/` | Index of the dashboards below |
| `/admin/tasks/` | All tasks, filterable by `kind` + `state`; claim-age + claim-expired badges |
| `/admin/tasks/<id>/` | Per-task detail: full payload, claim status, lineage chain, reassign + reclaim forms |
| `/admin/ideas/` | All ideas, with inline content preview |
| `/admin/ideas/<id>/` | Per-idea detail; admin "Create execution task" form (12a-3) |
| `/admin/variants/` | All variants, filterable by `status`; orphaned-starting badge |
| `/admin/variants/<id>/` | Per-variant detail: lineage + related-events filter |
| `/admin/events/` | Event log with limit + reverse + filter; stable indexing across filter operations |
| `/admin/workers/` | Worker registry; register + reissue-credential forms |
| `/admin/groups/` | Group registry; add / remove / delete; reserved-id rejection |
| `/admin/work-refs/` | `refs/heads/work/*` branches classified by status; CAS-guarded deletion (requires web-ui started with `--repo-path`) |
| `/admin/experiment/` | Experiment state (`running` / `terminated`); terminate form (12a-3) |
| `/admin/dispatch-mode/` | Per-decision-type dispatch mode toggle: auto vs manual (12a-2) |
| `/admin/experiments/` | Multi-experiment registry; register / select / unregister (12c). **Gated on `--control-plane-url`** — see below |

Notes:

- These are read views over the wire API. Filter changes do not mutate state.
- Reclaim / reassign / terminate / dispatch-mode toggles do mutate, behind CSRF + the same authorization model as the wire API.
- **Auth.** Every `/admin/*` page load requires the signed-in session's worker to be a transitive member of the `admins` group; non-admin sessions get a 403 forbidden page from the route-layer middleware (issue #144). The `setup-experiment.sh` script seeds the `admins` group with the web-ui's worker so the default Compose deployment already meets this requirement. Sign-ups created after a deployment is up are not added to `admins` by default and will hit the 403 page until an existing admin adds them via `/admin/groups/admins/`.
- `/admin/experiments/` is only mounted when the web-ui is started with `--control-plane-url`. Since issue #147 the control-plane-server runs as a first-class always-on Compose service, but the web-ui's `--control-plane-url` is still opt-in via the `EDEN_CONTROL_PLANE_URL` env var (empty by default), so the route returns 404 ("page does not exist") on the default stack. To enable it, set `EDEN_CONTROL_PLANE_URL` and recreate the web-ui — see [§3.4](#34-enabling-the-multi-experiment-control-plane).

### 2.2 Forgejo Web UI (`http://localhost:3001`)

Sign in as user `eden`, password from `FORGEJO_REMOTE_PASSWORD` in `.env`.

Useful for: browsing branches (`work/*`, `variant/*`, `main`), diffing arbitrary commits, inspecting the `.eden/variants/<id>/evaluation.json` manifests the integrator writes onto the canonical `variant/*` lineage.

### 2.3 Artifacts

Worker-emitted artifacts (idea content markdown, evaluation outputs) live under `${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/` on the host. That directory is the same bind-mount the web-ui sees as `/var/lib/eden/artifacts/` — host path and container path are two views of the same bytes.

**Important caveat — scripted mode produces no artifact files.** The default Compose deployment runs the worker hosts in **scripted** mode; the scripted ideator / executor / evaluator stamp `artifacts_uri = file:///tmp/artifacts/...` onto every submission but never write those files to disk. The strings are fictional pointers. To see real artifact files in this directory, run [subprocess mode](user-guide.md#3-worker-host-modes) (`-f compose.yaml -f compose.subprocess.yaml`) — only then do the user-supplied `*_command` workers emit real bytes under `/var/lib/eden/artifacts/`.

Three ways to read artifacts when they exist:

```bash
# 1. Direct host read:
ls "$(sed -n 's/^EDEN_EXPERIMENT_DATA_ROOT=//p' reference/compose/.env)/artifacts/"

# 2. Inside the web-ui container (same bytes, different view):
docker exec eden-web-ui ls /var/lib/eden/artifacts/

# 3. Through the web-ui's /artifacts route (session-authed via the
#    sign-in cookie, NOT bearer-authed):
#    Sign in to http://localhost:8090, then in the same browser session:
#    http://localhost:8090/artifacts?uri=file:///var/lib/eden/artifacts/<file>
#    Path-traversal outside /var/lib/eden/artifacts returns 404.
```

The web-ui's executor / evaluator forms inline artifacts for you automatically when the URI resolves under the `--artifacts-dir` bind. There is no `/_reference/` prefix; there is no directory-listing endpoint — only the `?uri=...` lookup form.

**Multi-file `.tar.gz` artifacts (issue #120).** When an `artifacts_uri` points at a `<id>.tar.gz` file inside the artifacts dir, the bundle was produced by the Web UI's multi-file upload form (or by `eden-manual` with `--content-file`). The convention is:

- A top-level `manifest.json` enumerates the bundle entries (`path`, `size`, `content_type`), versioned by a `version` int. The web-ui treats `manifest.json` as a reserved filename and excludes it from the operator-visible entry list.
- A role-specific headline file (`idea.md`, `evaluation.md`, or `variant.md`) holds the operator's markdown body if any was provided. The evaluator/executor draft pages render it inline above the per-file link table.
- Each entry is fetchable individually via `GET /artifacts?uri=<bundle-uri>&entry=<entry-name>` — the entry name must be a single safe basename (no slashes, no `..`), and the route only honors `entry=` when the resolved file ends in `.tar.gz`. There is no automatic unpacking on disk; bytes are streamed out of the archive on demand.

Manifest inspection from the shell:

```bash
tar -xzf "${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/<id>.tar.gz" -O manifest.json | jq .
```

### 2.4 Wire API (raw)

The task-store-server speaks JSON over HTTP. Every state-mutating operation in EDEN lives here. Convenient one-liners:

```bash
ADMIN=$(grep '^EDEN_ADMIN_TOKEN=' reference/compose/.env | cut -d= -f2)
EXPERIMENT_ID=demo-phase12
H=(-H "Authorization: Bearer admin:$ADMIN" -H "X-Eden-Experiment-Id: $EXPERIMENT_ID")
BASE="http://localhost:8080/v0/experiments/$EXPERIMENT_ID"

curl -s "${H[@]}" "$BASE/tasks"           | jq
curl -s "${H[@]}" "$BASE/ideas"           | jq
curl -s "${H[@]}" "$BASE/variants"        | jq
curl -s "${H[@]}" "$BASE/events?cursor=0" | jq '.events[].type' | tail -30
curl -s "${H[@]}" "$BASE/workers"         | jq
```

FastAPI's `/docs`, `/openapi.json`, and `/redoc` are mounted on the task-store-server but auth-gated. For a browsable spec, see [§3.2](#32-swagger-ui-for-the-wire-api).

### 2.5 Container logs

Every long-running EDEN service writes JSON-lines to **two** destinations simultaneously:

1. **stdout**, captured by Docker's `json-file` logging driver.
2. **A host-side file** under `${EDEN_EXPERIMENT_DATA_ROOT}/logs/<service>/<service>.jsonl` — issue [#109](https://github.com/ealt/eden/issues/109).

Both carry the same records in the same format ([`eden_service_common/logging.py`](../reference/services/_common/src/eden_service_common/logging.py)); the redundancy exists to survive the failure modes each one has alone.

**Reading via docker:**

```bash
cd reference/compose
docker compose --env-file .env ps                    # service health snapshot
docker compose --env-file .env logs -f orchestrator  # tail one service
docker compose --env-file .env logs --since 5m       # all services, last 5 min
```

**Reading the bind-mounted JSONL files directly:**

```bash
DATA_ROOT=$(sed -n 's/^EDEN_EXPERIMENT_DATA_ROOT=//p' reference/compose/.env)

# Tail one service's structured log:
tail -F "$DATA_ROOT/logs/orchestrator/orchestrator.jsonl" | jq

# Last 20 dispatch decisions across the whole experiment:
jq -c 'select(.decision_type)' \
    "$DATA_ROOT/logs/orchestrator/orchestrator.jsonl" | tail -20

# Filter by level across all services:
jq -c 'select(.level=="error")' "$DATA_ROOT/logs/"*"/"*.jsonl
```

The orchestrator's log is the highest-signal one for "what is the system doing right now" — every dispatch decision is logged with a `decision_type` + `outcome`. `jq` is the right tool for ad-hoc filtering; the records are flat JSON objects with stable field names (`ts`, `level`, `service`, `experiment_id`, `message`, plus per-record context keys).

**Retention + crash-survival caveats.**

- Docker `json-file` driver: rotates per-service at `max-size: 50m`, `max-file: 5` — roughly 250 MB per service before the oldest archive is dropped.
- Bind-mounted JSONL: rotates at `EDEN_LOG_MAX_BYTES` (default 50 MB) × `EDEN_LOG_BACKUP_COUNT` (default 5), both override-able in the per-service environment. Backups land alongside as `<service>.jsonl.1` … `<service>.jsonl.5`.
- **`docker compose stop`** preserves both destinations (containers retained; bind-mount is on the host).
- **`docker compose down`** removes the containers and **deletes the docker-driver logs**, but the bind-mounted JSONL **survives**. This is the primary motivation for #109 — post-crash forensics need a copy of the logs the docker driver no longer holds.
- **`docker compose down -v`** still does not touch the bind-mount — `-v` only drops named volumes, and the logs dir is a host bind-mount (12a-1g posture, chapter 01 §13).
- **`rm -rf ${EDEN_EXPERIMENT_DATA_ROOT}/logs`** is the only operator action that wipes the bind-mounted JSONL. The same `rm -rf ${EDEN_EXPERIMENT_DATA_ROOT}` reset that wipes postgres / forgejo / artifacts wipes logs too.
- A SIGKILL'd container may lose the last in-flight write to either destination. The file handler flushes after every record (the stdlib `StreamHandler.emit` default), so the practical at-risk window is the few bytes of one not-yet-flushed JSON object — not minutes of log.

The four-rung ladder for production-grade persistence:

| Rung | What it gives you | Status |
|---|---|---|
| **L1** | Bumped docker rotation (50MB × 5 per service) | In place |
| **L2** | Per-service file handler writing to `${EDEN_EXPERIMENT_DATA_ROOT}/logs/` (bind-mount; survives `compose down -v`) | In place ([#109](https://github.com/ealt/eden/issues/109)) |
| **L3** | In-stack search UI (Loki + Alloy + Grafana overlay at `localhost:3000`) | **In place** ([#110](https://github.com/ealt/eden/issues/110)) — opt-in [`compose.logging.yaml`](../reference/compose/compose.logging.yaml); see [§2.8](#28-log-search-ui-loki--alloy--grafana) |
| **L4** | External streaming to CloudWatch / Datadog / Grafana Cloud / etc. | Production-grade; out of scope for the reference stack — wire each service's stdout to your collector of choice |

> **Why Alloy, not Promtail?** Promtail reached end-of-life on 2026-03-02. Grafana's supported successor is [Grafana Alloy](https://grafana.com/docs/alloy/) (their OpenTelemetry Collector distribution); the L3 overlay ships Alloy. The pipeline is identical — tail the #109 JSONL bind-mount → ship to Loki → search in Grafana — only the collector binary + its config language changed.

### 2.6 Read-only local clone of Forgejo

For deeper git introspection (`git log --graph`, `git diff` between arbitrary refs, scripted traversal), clone the Forgejo remote locally and never push:

```bash
PASS=$(grep '^FORGEJO_REMOTE_PASSWORD=' reference/compose/.env | cut -d= -f2)
git clone "http://eden:${PASS}@localhost:3001/eden/<experiment-id>.git" /tmp/eden-readonly
cd /tmp/eden-readonly
git log --all --decorate --oneline --graph
```

Refresh with `git fetch --all --prune`. The Forgejo Web UI (§2.2 above) covers casual browsing; a local clone wins for batch / scripted inspection.

### 2.7 The readonly Postgres role

The task-store-server provisions an `eden_readonly` SQL role at startup, gated by `EDEN_READONLY_PASSWORD` in `.env`. It has `SELECT` on the event log, tasks, ideas, variants, submissions, workers (column-restricted; no credential hashes), groups, and migration bookkeeping. Schema reference: [`docs/operations/agent-readonly-db.md`](operations/agent-readonly-db.md).

For casual variant exploration the server also creates a `variant_unpacked` view that unpacks the `variant.data` JSON blob into typed scalar columns — one per public `Variant` field plus one per metric declared in the experiment's `evaluation_schema`. Operators in Adminer write `SELECT * FROM variant_unpacked WHERE correctness > 0.7` instead of nested JSON traversals on the base table. See [§5 of the readonly substrate doc](operations/agent-readonly-db.md#5-the-variant_unpacked-convenience-view).

Connect with `psql`:

```bash
PG_PASS=$(grep '^EDEN_READONLY_PASSWORD=' reference/compose/.env | cut -d= -f2)
PGPASSWORD="$PG_PASS" psql -h localhost -p 5433 -U eden_readonly -d eden
```

Or attach a browser via Adminer — see [§3.1](#31-adminer-for-postgres).

### 2.8 Log search UI (Loki + Alloy + Grafana)

The container logs in [§2.5](#25-container-logs) are great for tailing one service or `jq`-filtering one file. For **cross-service, time-windowed search** — "show me every `error` from any service in the last hour", or correlating an orchestrator dispatch decision with the evaluator's later processing of the resulting variant — there's an opt-in overlay that indexes the #109 JSONL substrate into Loki and exposes a Grafana search UI. This is rung **L3** of the ladder in [§2.5](#25-container-logs).

It is **not** part of the default stack (it pulls ~400 MB of images); add it with one extra `-f`:

```bash
cd reference/compose
# Layer the overlay on top of any stack you'd normally bring up.
docker compose -f compose.yaml -f compose.logging.yaml --env-file .env up -d --wait

# With subprocess workers (real per-task log volume), layer all three:
docker compose -f compose.yaml -f compose.subprocess.yaml -f compose.logging.yaml \
    --env-file .env up -d --wait
```

No extra setup-experiment flag is needed: `setup-experiment.sh` always generates `EDEN_GRAFANA_ADMIN_PASSWORD` and creates the `loki/` + `alloy/` data-root subdirs, so the overlay works against any bootstrapped stack.

**What it runs (three internal services; only Grafana is host-exposed):**

| Service | Role |
|---|---|
| `alloy` | [Grafana Alloy](https://grafana.com/docs/alloy/) tails `${EDEN_EXPERIMENT_DATA_ROOT}/logs/**/*.jsonl` read-only and ships each line to Loki, lifting `service` / `level` / `experiment_id` to Loki labels. No docker socket. |
| `loki` | Stores + indexes the lines. Internal-only (`http://loki:3100` on the compose network); no host port. |
| `grafana` | The search UI, at `http://localhost:3000` (override with `GRAFANA_HOST_PORT`). |

**Using it:**

1. Open `http://localhost:3000`. Sign in as user `admin`, password = `EDEN_GRAFANA_ADMIN_PASSWORD` from `.env`:

   ```bash
   grep '^EDEN_GRAFANA_ADMIN_PASSWORD=' reference/compose/.env | cut -d= -f2
   ```

2. The **EDEN explore** dashboard (folder *EDEN*) is pre-provisioned: a logs panel with `service` / `level` / `experiment_id` multi-select template variables and a time picker. Expand any line to see the inline JSON context fields (`decision_type`, `task_id`, `variant_id`, …).

3. For ad-hoc queries, use Grafana's **Explore** view against the pre-provisioned **Loki** datasource. Example LogQL:

   ```logql
   # every error from any service:
   {level="error"}
   # orchestrator dispatch lines mentioning a variant:
   {service="orchestrator"} |= "variant"
   # all services for one experiment, last 15m (set the time picker):
   {experiment_id="<your-experiment-id>"}
   ```

**Storage is DERIVED, not durable.** Loki's index/chunks live at `${EDEN_EXPERIMENT_DATA_ROOT}/loki/` and Alloy's tail positions at `${EDEN_EXPERIMENT_DATA_ROOT}/alloy/`. These are a *queryable projection* of the `logs/` JSONL substrate, **not** protocol-owned state (chapter 01 §13): losing them loses search history but no experiment state, and they rebuild by re-ingesting whatever remains under `logs/`. They are wiped by the same `rm -rf ${EDEN_EXPERIMENT_DATA_ROOT}` reset and are **not** covered by the durability invariant. A finite `retention_period` (default 7 days, in [`loki-config.yaml`](../reference/compose/logging/loki-config.yaml)) bounds disk growth on a long-lived demo.

**Coverage caveat — EDEN services only, by default.** The file-tail captures only the six EDEN services (the ones using `eden_service_common` logging). Postgres + Forgejo log to stdout, which the JSONL tail does not see. To also capture their stdout, layer the optional **infra** overlay on top — it mounts the host docker socket (read-only) into Alloy, so it lives in its own overlay per the privilege-isolation discipline (exactly how `compose.docker-exec.yaml` isolates the same privilege):

```bash
# Probe the socket's in-container gid (NOT a host-side stat), then bring up:
EDEN_LOGGING_DOCKER_GID=$(docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock alpine:3.20 \
  stat -c '%g' /var/run/docker.sock) \
docker compose -f compose.yaml -f compose.logging.yaml -f compose.logging-infra.yaml \
  --env-file .env up -d --wait
```

`EDEN_LOGGING_DOCKER_GID` is a dedicated required var (`:?`-guarded) — deliberately *not* `EDEN_DOCKER_GID`, which defaults to `0` and would silently bring the overlay up with the wrong gid. See [`compose.logging-infra.yaml`](../reference/compose/compose.logging-infra.yaml)'s header.

Infra stdout streams carry only a `service` label (`postgres` / `forgejo`) — they have no `experiment_id` or `level` (those are EDEN-JSON fields). The **EDEN explore** dashboard still surfaces them: its template variables default to an all-value that doesn't require those labels (`experiment_id`/`level` use `.*`, which matches streams missing the label; `service` uses `.+` as the load-bearing non-empty matcher Loki requires). Filtering by a specific `level` or `experiment_id`, naturally, narrows to the EDEN-service streams that carry those labels.

**Implicit contract with `logging.py`.** Alloy's JSON-parse stage keys on the field names emitted by [`eden_service_common/logging.py`](../reference/services/_common/src/eden_service_common/logging.py) (`ts`, `level`, `service`, `experiment_id`). A future logging-schema rename degrades labels *silently* (ingestion keeps working; search by the renamed label quietly stops). The `compose-smoke-logging` CI job asserts `{service="orchestrator"}` returns lines, so a `service`-label regression fails the smoke. Validate locally with `bash reference/compose/healthcheck/smoke-logging.sh`.

## 3. Bring-your-own admin UIs

These do not ship with the Compose stack. They're one-shot `docker run` siblings on the same docker network. Useful for ad-hoc inspection; tear them down when you're done.

### 3.1 Adminer for Postgres

```bash
docker run --rm -d --name eden-demo-adminer \
  --network eden-reference_default \
  -p 8091:8080 \
  adminer:4
```

Open `http://localhost:8091`. Connect as:

- **System**: PostgreSQL
- **Server**: `postgres` (the in-network container name)
- **Username**: `eden_readonly` (safer than the superuser) or `eden` (write access)
- **Password**: `EDEN_READONLY_PASSWORD` or `POSTGRES_PASSWORD` from `.env`
- **Database**: `eden`

Useful queries against the readonly role: see [`agent-readonly-db.md` §3](operations/agent-readonly-db.md). The schema is byte-for-byte parallel to SQLite — JSON blobs in a `data` text column rather than typed `jsonb`, so query the structure with `data::jsonb ->> 'field'`.

Tear down: `docker rm -f eden-demo-adminer`.

### 3.2 Swagger UI for the wire API

The task-store-server's OpenAPI spec is auth-gated, so we snapshot it once and serve it from a separate container.

```bash
ADMIN=$(grep '^EDEN_ADMIN_TOKEN=' reference/compose/.env | cut -d= -f2)
mkdir -p /tmp/eden-demo-swagger
curl -s -H "Authorization: Bearer admin:$ADMIN" \
  http://localhost:8080/openapi.json -o /tmp/eden-demo-swagger/openapi.json

docker run --rm -d --name eden-demo-swagger \
  -p 8092:8080 \
  -v /tmp/eden-demo-swagger/openapi.json:/openapi.json:ro \
  -e SWAGGER_JSON=/openapi.json \
  swaggerapi/swagger-ui
```

Open `http://localhost:8092`. Browsing the spec works without auth.

**"Try it out" caveat:** Swagger UI sends real requests to `http://localhost:8080`, which is bearer-gated. The OpenAPI spec doesn't declare a `securitySchemes` block, so the Authorize button isn't useful. Two workarounds:

- Copy the curl command Swagger generates and add `-H "Authorization: Bearer admin:<TOKEN>"` yourself.
- Use a browser extension (ModHeader, Requestly) to inject the header for `localhost:8080` requests.

Re-snapshot the spec after any code change touching wire endpoints.

Tear down: `docker rm -f eden-demo-swagger`.

### 3.3 Desktop database clients

If you prefer a desktop tool over Adminer, connect directly to `localhost:5433` with `eden_readonly` / `EDEN_READONLY_PASSWORD`. Tested combinations:

- TablePlus, DBeaver, Postico, DataGrip — all work standard PostgreSQL connections.
- pgAdmin — works but feels heavy for a single experiment; better suited to multi-cluster environments.

### 3.4 Enabling the multi-experiment control plane

Since issue #147 the control-plane-server is a **first-class always-on Compose service** (`control-plane`, port 8081), Postgres-backed (a separate `eden_control_plane` database in the same instance, created by the postgres init hook). `setup-experiment.sh` provisions its store DSN. So the control plane is already running on any `docker compose up` stack — what's opt-in is whether the **web-ui** talks to it.

The `/admin/experiments/` route is gated on the web-ui being started with `--control-plane-url`, which the web-ui CLI reads from the `EDEN_CONTROL_PLANE_URL` env var (empty by default → route stays 404). To enable the cross-experiment dashboard on a running stack:

```bash
cd reference/compose
# Point the web-ui at the in-network control-plane service, then
# recreate web-ui only. The control-plane admin token defaults to
# EDEN_ADMIN_TOKEN inside the CLI, so no separate token is needed.
echo 'EDEN_CONTROL_PLANE_URL=http://control-plane:8081' >> .env
docker compose --env-file .env up -d --force-recreate --no-deps web-ui
```

`/admin/experiments/` now resolves (303 → sign-in if you're not authenticated, 200 once you are). The control-plane API itself listens at `http://localhost:${CONTROL_PLANE_HOST_PORT:-8081}/v0/control/*` (bearer-authed with the same admin token; `/healthz` is unauthenticated); fetch its `/openapi.json` with the bearer for the full surface. Note the registry is empty until an experiment is registered via `POST /v0/control/experiments` (the lease-handoff smoke and a lease-driven orchestrator do this).

Tear down (revert the web-ui to the no-control-plane command):

```bash
cd reference/compose
sed -i.bak '/^EDEN_CONTROL_PLANE_URL=/d' .env && rm .env.bak
docker compose --env-file .env up -d --force-recreate --no-deps web-ui
```

### 3.5 Desktop HTTP clients

For richer API exploration than curl + Swagger:

- Postman, Insomnia, Bruno — import the OpenAPI spec from `/tmp/eden-demo-swagger/openapi.json` (Postman: File → Import → openapi.json). Set a collection-level bearer of `admin:<token>` and the auth-gated endpoints become clickable.

## 4. Following a variant end-to-end

A worked example: trace variant `v-abc123` from idea to integration.

1. **Find the idea** that spawned the execution task:
   - Web UI: `/admin/variants/v-abc123/` → click into "lineage".
   - SQL: `SELECT data FROM idea WHERE idea_id = (SELECT data::jsonb ->> 'idea_id' FROM variant WHERE variant_id = 'v-abc123');`
2. **See the execution work** on Forgejo: `http://localhost:3001/eden/<experiment-id>/src/branch/work/<slug>-v-abc123` — every intermediate commit the executor made.
3. **See the integrated lineage**: `http://localhost:3001/eden/<experiment-id>/src/branch/variant/v-abc123-<slug>` — the squashed single commit + `.eden/variants/v-abc123/evaluation.json`.
4. **Read the evaluation submission**: `/admin/events/?filter=variant.succeeded&q=v-abc123` (web UI) or `SELECT data FROM submission WHERE data::jsonb ->> 'variant_id' = 'v-abc123';` (SQL).
5. **See every event mentioning this variant** in order:

   ```sql
   SELECT seq, type, occurred_at FROM event
   WHERE data::jsonb ->> 'variant_id' = 'v-abc123'
   ORDER BY seq;
   ```

   The orchestrator's loop never has access to anything you can't reproduce from this stream.

## 5. Alternatives and informational notes

### Forgejo (and Gitea)

The reference stack runs Forgejo (`codeberg.org/forgejo/forgejo:11-rootless`) as of PR #113 — switched from upstream Gitea since the two are API + UI source-compatible for the features EDEN uses (private repos, HTTP-Basic auth, repo-create API, push) and Forgejo has the more active maintenance posture post the 2022 governance fork. Swapping back to Gitea is a one-line image change in `compose.yaml` and would work unchanged for the EDEN use case; no reason to do so unless you have an operational requirement.

### Other git-storage substrates

The Forgejo container is acting as a per-experiment private HTTP-Basic-authenticated git remote. Anything that satisfies that interface is a candidate replacement. The integrator's contract is just "push `variant/*` refs, read them back" — it does not depend on any Forgejo-specific feature.

If you're evaluating alternatives, the relevant questions are: does it support HTTP-Basic auth for cloning + pushing; does it expose a repo-creation API; does it survive container restart. We don't ship any other substrate in the reference stack today.

### pgAdmin vs Adminer

Adminer is a single-file PHP app — small image, quick to spin up, fine for a single experiment. pgAdmin is heavier (Python + a queryable internal store) but more capable: query history, ER diagrams, server groups. For one-shot demo inspection, Adminer wins; for multi-cluster operational work, pgAdmin is worth the weight.

### Teardown

When you're done with the bring-your-own tools:

```bash
docker rm -f eden-demo-adminer eden-demo-swagger
rm -rf /tmp/eden-demo-swagger
```

These run outside Compose, so a `compose down` does not touch them.
