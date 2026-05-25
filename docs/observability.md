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
| **Process logs** | Container stdout from each service | Captured by Docker's logging driver; ephemeral |

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
- `/admin/experiments/` is only mounted when the web-ui is started with `--control-plane-url`. The default Compose stack omits this flag; the route returns 404 ("page does not exist") until a control-plane-server is wired up. To enable it on the demo stack, run a sibling control-plane container and recreate the web-ui with the [`compose.control-plane.yaml`](../reference/compose/compose.control-plane.yaml) overlay — see [§3.4](#34-enabling-the-multi-experiment-control-plane).

### 2.2 Forgejo Web UI (`http://localhost:3001`)

Sign in as user `eden`, password from `FORGEJO_REMOTE_PASSWORD` in `.env`.

Useful for: browsing branches (`work/*`, `variant/*`, `main`), diffing arbitrary commits, inspecting the `.eden/variants/<id>/evaluation.json` manifests the integrator writes onto the canonical `variant/*` lineage.

### 2.3 Artifacts

Worker-emitted artifacts (idea content markdown, evaluation outputs) live under `${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/` on the host. That directory is the same bind-mount the web-ui sees as `/var/lib/eden/artifacts/` — host path and container path are two views of the same bytes.

**Important caveat — scripted mode produces no artifact files by default.** The default Compose deployment runs the worker hosts in **scripted** mode; the scripted ideator / executor / evaluator stamp `artifacts_uri = file:///tmp/artifacts/...` onto every submission but never write those files to disk. The strings are fictional pointers. Two ways to get real artifact files:

- **Subprocess mode** ([`user-guide.md` §3](user-guide.md#3-worker-host-modes), `-f compose.yaml -f compose.subprocess.yaml`) — the user-supplied `*_command` workers emit real bytes under `/var/lib/eden/artifacts/`.
- **Scripted mode with `--emit-fixture-artifacts`** (issue #111) — each scripted host (ideator / executor / evaluator) accepts an opt-in flag that writes small placeholder files under `--artifacts-dir` and stamps real `file:///var/lib/eden/artifacts/<path>` URIs onto submissions. Useful for demos / onboarding where the artifacts substrate should be observable without an experiment config providing real worker code. Pair the flag with `--artifacts-dir` (the host's view of the same bind-mount the web-ui sees as `/var/lib/eden/artifacts/`).

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

```bash
cd reference/compose
docker compose --env-file .env ps                    # service health snapshot
docker compose --env-file .env logs -f orchestrator  # tail one service
docker compose --env-file .env logs --since 5m       # all services, last 5 min
```

The orchestrator's log is the highest-signal one for "what is the system doing right now" — every dispatch decision is logged with a `decision_type` + `outcome`.

**Log retention.** Each service's docker logging driver is set to `json-file` with `max-size: 50m` and `max-file: 5` — roughly 250MB per service before rotation. This is enough for a multi-hour debugging window. The hard limits today:

- **`docker compose stop`** preserves logs (the container is retained, only the process exits).
- **`docker compose down`** removes containers and **deletes** their logs.
- A SIGKILL'd container may lose the last in-flight stdout write; Python's logging module flushes on signal, so the practical risk is small but not zero.

The four-rung ladder for production-grade persistence — only **L1** is in place today:

| Rung | What it gives you | Status |
|---|---|---|
| **L1** | Bumped docker rotation (50MB × 5 per service) | In place — applies on next container recreate |
| **L2** | Per-service file handler writing to `${EDEN_EXPERIMENT_DATA_ROOT}/logs/` (bind-mount; survives `compose down -v`) | Tracked in [#109](https://github.com/ealt/eden/issues/109) |
| **L3** | In-stack search UI (Loki + Promtail + Grafana overlay at `localhost:3000`) | Tracked in [#110](https://github.com/ealt/eden/issues/110) |
| **L4** | External streaming to CloudWatch / Datadog / Grafana Cloud / etc. | Production-grade; out of scope for the reference stack — wire each service's stdout to your collector of choice |

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

The `/admin/experiments/` route is gated on the web-ui being started with `--control-plane-url`. Phase 12c shipped the control-plane-server as a separate service that the default Compose stack does not start. To enable the route on a running demo stack:

```bash
# 1. Spin up control-plane-server as a sibling container on the eden network.
ADMIN=$(grep '^EDEN_ADMIN_TOKEN=' reference/compose/.env | cut -d= -f2)
docker run --rm -d --name eden-demo-control-plane \
  --network eden-reference_default \
  -p 8081:8081 \
  eden-reference:dev \
  python -m eden_control_plane_server \
    --store-url ':memory:' \
    --host 0.0.0.0 --port 8081 \
    --admin-token "$ADMIN" \
    --task-store-url http://task-store-server:8080

# 2. Tell the web-ui's overlay where to find it, then recreate web-ui only.
cd reference/compose
echo 'EDEN_CONTROL_PLANE_URL=http://eden-demo-control-plane:8081' >> .env
docker compose --env-file .env \
  -f compose.yaml -f compose.control-plane.yaml \
  up -d --force-recreate --no-deps web-ui
```

`compose.control-plane.yaml` is an overlay that re-declares the web-ui's `command:` with the two extra flags (`--control-plane-url` + `--control-plane-admin-token`). Compose replaces (not merges) list-shaped command keys, so the overlay carries the full command — keep it in lockstep with `compose.yaml` if web-ui flags change.

`/admin/experiments/` now resolves (303 → sign-in if you're not authenticated, 200 once you are). The control-plane API itself listens at `http://localhost:8081/v0/control/*` (14 endpoints; bearer-authed with the same admin token); fetch its `/openapi.json` with the bearer for the full surface.

State-sync caveat: the demo command above uses `:memory:` storage, so the control-plane forgets its experiment registry on container restart. For a persistent deployment you'd point `--store-url` at Postgres (a separate database / schema from the task store), and likely run it as a first-class Compose service rather than a sibling container.

Tear down:

```bash
docker rm -f eden-demo-control-plane
# Optional: revert the web-ui to the no-control-plane command.
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
