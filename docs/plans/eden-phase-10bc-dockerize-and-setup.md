# EDEN Phase 10 chunks 10b + 10c: Dockerize services + setup-experiment

## Context

Phase 10 chunk 10a shipped the Compose infrastructure layer: Postgres,
Gitea (headless), and a `blob-init` one-shot that creates the
`eden-blob-data` volume. No EDEN services are dockerized yet; Postgres
sits idle; Gitea sits idle.

This chunk closes the gap **for the scripted reference services that
already exist**:

> **10b** — Each reference service dockerized with its own image.
>
> **10c** — `setup-experiment` script: registers an experiment
> end-to-end (initializes the bare git repo, writes per-service
> sub-configs, …).
>
> *Chunks: 10b + 10c one chunk* — dockerize and setup co-evolve.

After this chunk, `cd reference/compose && bash scripts/setup-experiment.sh
<config.yaml> && docker compose up` brings the whole reference stack
online and the fixture experiment runs to quiescence. The end-to-end
integration test that *asserts* this in CI is 10e (deferred per the
roadmap); 10b+10c just makes it possible.

## Roadmap delta

The roadmap's wording for 10b/10c was written ahead of detail; this
chunk amends it explicitly. The amendments below should be reflected
in `docs/roadmap.md` as part of the chunk's diff so the source of
truth doesn't drift.

| Roadmap text (current) | This chunk does | Why the change |
|---|---|---|
| "10b — Each reference service dockerized with its own image." | One shared `eden-reference:dev` image, services selected by Compose `command:`. | The seven services share most deps (FastAPI, uvicorn, eden-contracts, eden-storage, eden-wire, eden-service-common). N images means either replicating the workspace into each (greater total image bytes than one) or a multi-stage tower whose complexity outweighs the benefit. Phase 13 (k8s, Helm) is when "one image per service with its own minimal closure" becomes valuable; not now. |
| "10c — builds experiment-specific image" | **Deferred to 10d.** | The scripted workers don't need an experiment-specific image. The LLM implementer (10d) does, since its sandbox runs the experiment's `implement_command`. Pinning the work to the chunk that *uses* it. |
| "10c — registers with the control plane" | **Deferred to Phase 12.** | No control plane exists yet. "Registration" reduces to "generate the right env file," which is what setup-experiment does. |
| (Implicit) workers integrate with Gitea as the bare-repo remote | **Deferred to a follow-up sub-chunk after 10d.** | Workers currently use local bare repos via `eden_git.GitRepo` (subprocess `git`). HTTPS push/pull against Gitea requires either remote-aware refactoring of `GitRepo` or per-worker local-clone+sync logic — a substantial body of work, not what 10b/10c is asking for. Same posture 10a took toward the blob volume (infrastructure exists; consumer arrives later). |

The roadmap update commits alongside this chunk's code so future
readers see the amendment.

## Critical scope clarifications

Re-stating, with the deltas above accounted for, the four 10c
sub-jobs:

| 10c sub-job | Disposition |
|---|---|
| Initialize the bare git repo + seed it | **In scope.** Repo lives in a Compose-shared volume `eden-bare-repo`; setup-experiment runs a one-shot `docker compose run --rm --no-deps eden-repo-init` that seeds the volume and prints the seed SHA. |
| Write per-service sub-configs | **In scope.** The script emits a generated `.env` Compose reads; service `command:` entries thread the right flags. |
| Build experiment-specific image | **Deferred to 10d** (see Roadmap delta above). |
| Register with the control plane | **Deferred to Phase 12** (see Roadmap delta above). |

**Gitea stays idle this chunk** (see Roadmap delta). Document the
smell explicitly in `reference/compose/README.md` so a reader doesn't
wonder why Gitea is running.

**`PostgresStore` is in scope.** 10a's chunk note explicitly reserved
Postgres for "`PostgresStore` consumption in 10b". A new
`reference/packages/eden-storage/src/eden_storage/postgres.py` adds a
third backend that satisfies the same `Store` Protocol as
`InMemoryStore` and `SqliteStore`, sharing the transition logic in
`_base.py`. Test parametrization runs the same conformance scenarios
against all three backends. CI gains a test job that brings up a
disposable Postgres container for the parametrized backend tests.

## What gets created/changed

### A. `eden-storage` — `PostgresStore`

**New module:** [`reference/packages/eden-storage/src/eden_storage/postgres.py`](../../reference/packages/eden-storage/src/eden_storage/postgres.py)

**Design:**

- Backed by [`psycopg[binary]>=3.2`](https://www.psycopg.org/psycopg3/) — pure
  Python with prebuilt binaries; no system libpq dependency. Pin to
  `>=3.2,<4`.
- Single connection per `PostgresStore` instance (matches the
  single-process reference pattern; chapter 8 §1.2 atomicity needs
  ordered locking, which is simplest with one connection serialized
  by `_lock`). Pooling is a Phase 12 concern when multiple orchestrator
  replicas appear.
- `BEGIN ISOLATION LEVEL SERIALIZABLE` per public op, mirroring
  `SqliteStore`'s `BEGIN IMMEDIATE`. Serializable is the strongest
  isolation Postgres provides; it's the right starting default for
  "make this work; tune later."
- Schema mirrors `_schema.py`'s SQLite version 1 with type
  substitutions:
  - `TEXT` → `text`
  - `AUTOINCREMENT` → `BIGINT GENERATED ALWAYS AS IDENTITY`
  - `data TEXT` → `data text` (kept as text rather than `jsonb`
    so the read path is byte-for-byte parallel to SqliteStore;
    psycopg returns `jsonb` columns as Python dicts, which would
    diverge from SQLite's "always a string" return type and
    require a per-backend branch in every `_get_*` method.
    Migrating to `jsonb` is a future Phase-12+ concern when
    indexing on `data->>'…'` becomes valuable.)
  - `event(seq … AUTOINCREMENT)` → `event(seq BIGINT GENERATED
    ALWAYS AS IDENTITY PRIMARY KEY)`
- `ensure_schema(conn)` lives in a new `_postgres_schema.py` so the
  SqliteStore's `_schema.py` stays SQLite-specific. Migrations table
  is `schema_version(version int primary key)` — same shape as
  SQLite.
- `experiment_id` and `metrics_schema` reopen-validation matches
  SqliteStore exactly (chapter 8 §4.2).
- The `event_id` resumption logic — re-resuming the per-process
  counter from `MAX(seq)` on reopen — ports verbatim.

**Public API:**

```python
class PostgresStore(_StoreBase):
    def __init__(
        self,
        experiment_id: str,
        dsn: str,                    # postgresql://… libpq URL
        *,
        metrics_schema: MetricsSchema | None = None,
        now: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None: ...
    def close(self) -> None: ...
```

**Tests:** [`reference/packages/eden-storage/tests/test_postgres_store.py`](../../reference/packages/eden-storage/tests/test_postgres_store.py).
The existing parametrized conformance scenarios (`test_store_*`) gain
a third backend factory keyed on a new `EDEN_TEST_POSTGRES_DSN`
environment variable; if unset, the parametrization skips the
postgres rows. (Same pattern Python projects use for opt-in
integration tests.) CI sets the env var so coverage is enforced
there.

**`task-store-server` integration:** [`reference/services/task-store-server/src/eden_task_store_server/app.py`](../../reference/services/task-store-server/src/eden_task_store_server/app.py)'s
`build_store(db_path, …)` becomes `build_store(store_url, …)`. The
URL scheme dispatches:

- `:memory:` → `InMemoryStore`
- `sqlite:///<path>` (or bare `<path>`, for compatibility) →
  `SqliteStore`
- `postgresql://…` → `PostgresStore`

CLI flag rename: `--db-path` → `--store-url`. The old `--db-path`
flag stays as a deprecated alias for one phase to avoid churn —
emits a deprecation warning, mapped to `sqlite:///<value>` when the
value isn't `:memory:`. (We can drop it entirely in 10d; nothing in
the production tree uses it.)

### B. Service Dockerfiles

**Image strategy: one shared image** containing the whole uv
workspace. Per-service `command:` in `compose.yaml` selects which
module runs.

**Why one image, not seven:** the seven services share most
dependencies (FastAPI, uvicorn, eden-contracts, eden-storage,
eden-wire, eden-service-common). Building seven images requires
either replicating the whole workspace into each (bigger total
storage than one image) or a multi-stage trick whose complexity
outweighs the benefit. One shared image is what every reference
microservices repo I've worked on does; we get back to the
seven-image story in Phase 13 when each service ships its own Helm
chart with its own minimal image.

**File:** [`reference/compose/Dockerfile`](../../reference/compose/Dockerfile)

```dockerfile
# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12.7
ARG UV_VERSION=0.5.4

FROM python:${PYTHON_VERSION}-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# git is required by eden_git's subprocess wrapper (orchestrator,
# implementer, integrator, web-ui).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 1000 --create-home --shell /bin/bash eden

FROM base AS builder
ARG UV_VERSION
RUN pip install --no-cache-dir uv==${UV_VERSION}
WORKDIR /app
# Copy the workspace layout first (pyproject + per-member pyproject)
# so dependency layers cache before source.
COPY pyproject.toml uv.lock ./
COPY reference/packages/eden-contracts/pyproject.toml reference/packages/eden-contracts/pyproject.toml
COPY reference/packages/eden-dispatch/pyproject.toml reference/packages/eden-dispatch/pyproject.toml
COPY reference/packages/eden-git/pyproject.toml      reference/packages/eden-git/pyproject.toml
COPY reference/packages/eden-storage/pyproject.toml  reference/packages/eden-storage/pyproject.toml
COPY reference/packages/eden-wire/pyproject.toml     reference/packages/eden-wire/pyproject.toml
COPY reference/services/_common/pyproject.toml          reference/services/_common/pyproject.toml
COPY reference/services/task-store-server/pyproject.toml reference/services/task-store-server/pyproject.toml
COPY reference/services/orchestrator/pyproject.toml      reference/services/orchestrator/pyproject.toml
COPY reference/services/planner/pyproject.toml           reference/services/planner/pyproject.toml
COPY reference/services/implementer/pyproject.toml       reference/services/implementer/pyproject.toml
COPY reference/services/evaluator/pyproject.toml         reference/services/evaluator/pyproject.toml
COPY reference/services/web-ui/pyproject.toml            reference/services/web-ui/pyproject.toml
# README files referenced from per-member pyproject (hatchling needs them).
COPY reference/packages reference/packages
COPY reference/services reference/services
# `--all-packages` installs every workspace member; without it the
# eden-* packages would be skipped because the root `pyproject.toml`
# is non-installable (`tool.uv.package = false`) and the workspace
# members are pulled in only via the `dev` group, which `--no-dev`
# excludes. Verified empirically: `uv sync --frozen --no-dev` against
# this workspace removes every eden-* package from the venv.
RUN uv sync --frozen --no-dev --all-packages

FROM base AS runtime
WORKDIR /app
COPY --from=builder /app /app
# Make uv-installed venv binaries discoverable.
ENV PATH="/app/.venv/bin:${PATH}"
USER eden
# No ENTRYPOINT — compose.yaml's `command:` selects the service
# module.
```

**Build context:** the **repo root** (so the workspace files are
reachable). compose.yaml uses `context: ..` and
`dockerfile: reference/compose/Dockerfile`.

**`.dockerignore`** (new file at repo root) excludes `.git`,
`.venv`, `node_modules`, `__pycache__`, `*.egg-info`,
`.pytest_cache`, `.ruff_cache`, `dist`, `build`, `htmlcov`. Without
this, `uv sync --frozen` re-downloads every dep on each build
because the hash of the build context changes whenever `git`
internal files do.

### C. Compose service entries

[`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) gains six service entries
(below). Each service:

1. `build:` references the shared image.
2. `depends_on:` waits for `postgres` (healthy) for those that talk
   to the task-store-server, which itself depends on postgres.
3. `command:` selects the module + threads the right flags from
   `${VAR}` substitutions, so setup-experiment generating a `.env`
   is the one source of dynamic config.
4. Bare-repo-touching services mount the named volume `eden-bare-repo`.

**Service config matrix.** Each row spells out every required flag,
where the flag's value comes from, and what mounts the service
needs.

| Service | `command:` flags | Source of each value | Mounts | `depends_on` (healthy) | Healthcheck | Host port |
|---|---|---|---|---|---|---|
| `task-store-server` | `--store-url $EDEN_STORE_URL`, `--experiment-id $EDEN_EXPERIMENT_ID`, `--experiment-config /etc/eden/experiment-config.yaml`, `--shared-token $EDEN_SHARED_TOKEN`, `--host 0.0.0.0`, `--port 8080`, `--log-level info` | env from `.env` (setup-experiment); experiment-config from Compose `configs:` (setup-experiment) | `eden-experiment-config` (configs), no volumes | `postgres` | `curl -fsS http://localhost:8080/v0/experiments/$EDEN_EXPERIMENT_ID/events -H "Authorization: Bearer $EDEN_SHARED_TOKEN" -H "X-Eden-Experiment-Id: $EDEN_EXPERIMENT_ID"` | none (internal) |
| `orchestrator` | `--task-store-url http://task-store-server:8080`, `--experiment-id $EDEN_EXPERIMENT_ID`, `--shared-token $EDEN_SHARED_TOKEN`, `--repo-path /var/lib/eden/repo`, `--plan-tasks $EDEN_PLAN_TASKS`, `--log-level info` | env + Compose service DNS | `eden-bare-repo` at `/var/lib/eden/repo` | `task-store-server` | none (long-running client) | none |
| `planner-host` | `--task-store-url http://task-store-server:8080`, `--experiment-id $EDEN_EXPERIMENT_ID`, `--shared-token $EDEN_SHARED_TOKEN`, `--worker-id planner-1`, `--base-commit-sha $EDEN_BASE_COMMIT_SHA`, `--log-level info` | env (worker-id is a static literal in compose.yaml; override via env if multiple replicas needed later) | none | `task-store-server` | none | none |
| `implementer-host` | `--task-store-url http://task-store-server:8080`, `--experiment-id $EDEN_EXPERIMENT_ID`, `--shared-token $EDEN_SHARED_TOKEN`, `--worker-id implementer-1`, `--repo-path /var/lib/eden/repo`, `--log-level info` | env | `eden-bare-repo` at `/var/lib/eden/repo` | `task-store-server` | none | none |
| `evaluator-host` | `--task-store-url http://task-store-server:8080`, `--experiment-id $EDEN_EXPERIMENT_ID`, `--shared-token $EDEN_SHARED_TOKEN`, `--worker-id evaluator-1`, `--experiment-config /etc/eden/experiment-config.yaml`, `--log-level info` | env + configs | `eden-experiment-config` (configs) | `task-store-server` | none | none |
| `web-ui` | `--task-store-url http://task-store-server:8080`, `--experiment-id $EDEN_EXPERIMENT_ID`, `--shared-token $EDEN_SHARED_TOKEN`, `--experiment-config /etc/eden/experiment-config.yaml`, `--session-secret $EDEN_SESSION_SECRET`, `--artifacts-dir /var/lib/eden/artifacts`, `--repo-path /var/lib/eden/repo`, `--worker-id web-ui-1`, `--host 0.0.0.0`, `--port 8090`, `--log-level info` | env + configs | `eden-experiment-config` (configs); `eden-artifacts-data` at `/var/lib/eden/artifacts`; `eden-bare-repo` at `/var/lib/eden/repo` (**read-write** — see note below) | `task-store-server` | `curl -fsS http://localhost:8090/healthz` (new endpoint added in this chunk) | `${WEB_UI_HOST_PORT:-8090}:8090` |
| `eden-repo-init` (profile: setup) | `python -m eden_service_common.repo_init --repo-path /var/lib/eden/repo` | n/a — invoked by setup-experiment via `compose run --rm --no-deps` | `eden-bare-repo` at `/var/lib/eden/repo` | none | n/a (one-shot) | n/a |

**New named volumes**: `eden-bare-repo` (replaces the host-path repo
the existing CLIs assume) and `eden-artifacts-data` (web-ui's
`--artifacts-dir`; isolates UI artifact writes from any host path).

**`EDEN_SESSION_SECRET`** is a new env var setup-experiment
generates (32 bytes hex). Preserved across re-runs like the other
secrets.

**The web-ui `/healthz` endpoint.** Currently the web-ui has only
auth-gated routes; Compose's healthcheck must be unauthenticated so
it can run before any user signs in. Add `routes/_healthz.py` (or
inline in `make_app`) returning HTTP 200 `{"status":"ok"}` from
`GET /healthz`. The route is unauthenticated by design — it carries
no secrets and reveals only "the process is up." Document this in
the web-ui README.

**Web-ui repo mount: read-write, not read-only.** Passing
`--repo-path` to web-ui activates the entire implementer module
(see [`web-ui app factory`](../../reference/services/web-ui/src/eden_web_ui/app.py)),
which writes refs on submit. A read-only mount would 500 on
implementer submit. So we mount read-write.

The redundancy with the standalone `implementer-host` service is
intentional: both can claim implement tasks, the user picks which
they prefer. The chunk-9c implementer module is for human
override / debugging; `implementer-host` is the unattended scripted
worker. They race for the same tasks via `Store.claim` (which has
its own atomicity guarantees per chapter 04 §3); whoever wins the
claim does the work. Document this concurrency posture in
`reference/compose/README.md`. (A future opt-out — runtime flag to
disable the implementer module from the web-ui — is out of scope
for this chunk.)

**`postgres` healthcheck** already exists (10a). The healthcheck is
what `service_healthy` keys off; downstream services don't need
their own dependency on `blob-init` (they don't touch
`eden-blob-data`).

**Resource posture:** no `cpus:` / `memory:` limits this chunk —
local dev, scripted workers; we add limits in 10d when LLM workers
land and profiling matters.

**The `name: eden-reference` project name stays load-bearing.** All
volumes resolve as `eden-reference_<volume>`.

### D. Bare-repo bootstrap — one authoritative owner

**Owner:** `setup-experiment.sh`. The repo is seeded **before**
`docker compose up` runs, so the seed SHA is known synchronously and
can be written into `.env` for downstream service flags.

**Mechanism:** setup-experiment builds the shared image and then
runs the one-shot init service (canonical command shape, matched
in section E):

```bash
docker compose --env-file <generated-env> build eden-repo-init
docker compose --env-file <generated-env> run --rm --no-deps eden-repo-init
```

against a new `eden-repo-init` service in `compose.yaml`. That
service:

1. `image:` references the same shared `eden-reference:dev` image
   (so it has Python + the workspace + git available).
2. Mounts only the `eden-bare-repo` named volume at
   `/var/lib/eden/repo`.
3. `command: ["python", "-m", "eden_service_common.repo_init",
   "--repo-path", "/var/lib/eden/repo"]` — a new module that:
   a. If `/var/lib/eden/repo/HEAD` already exists, prints
      `EDEN_REPO_ALREADY_SEEDED sha=<existing-sha>` and exits 0.
   b. Otherwise: runs `git init --bare
      --initial-branch=main`, calls `seed_bare_repo`, prints
      `EDEN_REPO_SEEDED sha=<hex>` to stdout, exits 0.

The script captures stdout, parses the SHA, and writes it to `.env`
as `EDEN_BASE_COMMIT_SHA=<hex>`. Then the operator runs `docker
compose up`.

**Crucially:** `eden-repo-init` is **NOT** in any other service's
`depends_on`. It is a setup-time tool, invoked by setup-experiment
via `compose run --rm --no-deps`, not a `compose up`-time
dependency. This collapses the dual-owner contradiction codex
flagged in plan-review round 0: setup-experiment is the sole owner
of repo seeding, and
the seed SHA is known *before* any service starts.

The compose `profiles:` keyword keeps `eden-repo-init` out of plain
`docker compose up`'s service set:

```yaml
eden-repo-init:
  profiles: ["setup"]
  ...
```

`compose run --rm --no-deps <name>` runs services regardless of
profile, so setup-experiment invokes it normally; plain `compose
up` skips it.

**Re-runnability:** `setup-experiment.sh` is idempotent. Running it
a second time:

- Re-runs `eden-repo-init`, which short-circuits on the existing
  `HEAD` and re-prints the existing SHA.
- Preserves existing secrets (postgres password, shared token) so
  re-running doesn't rotate them by accident.
- Overwrites `EDEN_PLAN_TASKS` and `experiment-config.yaml`.

### E. setup-experiment script

**File:** [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh)
(plus `README.md`).

**Why bash, not Python:** the script's job is mostly to (a) read a
YAML field from an existing config, (b) generate a `.env` file from
a template, (c) print human-readable next steps. Bash is the
simplest tool that fits, and it doesn't introduce a "you must `uv
sync` before you can set up an experiment" bootstrap problem.
Python would make the YAML read prettier (vs. `yq` or `python3 -c
"import yaml; …"`), but trades it for the bootstrap requirement.
The bash script will use a one-line `python3 -c` for YAML parsing
since CI's runner has Python — that single dependency stays minor.

**Inputs:**

- Required positional arg: path to an experiment config YAML
  (defaults to repo's fixture at
  `tests/fixtures/experiment/.eden/config.yaml`).
- `--experiment-id <id>` (default: derived from config filename
  parent directory).
- `--shared-token <T>` (default: a freshly generated 32-byte hex
  string).
- `--postgres-password <P>` (default: read from existing `.env` if
  present, else freshly generated).

**Outputs:**

- `reference/compose/.env` (or `--env-file <path>`) — generated
  Compose env. See section F for the full variable list.
- `reference/compose/experiment-config.yaml` — copy of the input
  config the task-store-server reads at boot via Compose `configs:`.
- Stdout: a short "next steps" message — `docker compose up -d
  --wait`. (**`up -d`, not `restart`** — `restart` does not pick
  up changes to `command:`, env files, or `configs:`. `up -d`
  detects config drift and recreates affected services. See
  the [docker compose up reference](https://docs.docker.com/reference/cli/docker/compose/up/).)

**Step ordering** (one-shot, synchronous, no chicken-and-egg):

1. Generate / preserve secrets. Write a partial `.env` with
   everything except `EDEN_BASE_COMMIT_SHA`.
2. Copy the input config to `reference/compose/experiment-config.yaml`.
3. `docker compose --env-file .env build eden-repo-init` so the
   shared image is available before the next step runs. Build
   output streams to stdout so the operator sees progress.
4. `docker compose --env-file .env run --rm --no-deps eden-repo-init`,
   capture stdout, parse the seed SHA.
   - `--no-deps` keeps future dependency edits from accidentally
     starting Postgres/Gitea/etc. just to seed a repo.
   - The build in step 3 means this never fails on a missing image.
5. Append `EDEN_BASE_COMMIT_SHA=<sha>` to `.env`.
6. Print next-steps message.

(`docker compose run --build --rm` would collapse 3+4 into one
call, but the explicit two-step keeps build output and seed
output cleanly separated in the operator's terminal — so a
build failure is unambiguous and a seed failure is too.)

### F. Compose dependency graph

Final shape (post-setup-experiment):

```text
postgres          → healthy (10a)
gitea             → healthy (10a, idle this chunk)
blob-init         → exited 0 (10a)
eden-repo-init    → exited 0 (new; profile=setup, NOT in `compose up`)
task-store-server → healthy [depends_on: postgres]
orchestrator      → running [depends_on: task-store-server]
planner-host      → running [depends_on: task-store-server]
implementer-host  → running [depends_on: task-store-server]
evaluator-host    → running [depends_on: task-store-server]
web-ui            → healthy [depends_on: task-store-server]
```

`eden-repo-init` is invoked once by setup-experiment via
`compose run --rm --no-deps` *before* `compose up`; the bare-repo volume is
already seeded by the time `compose up` starts the long-running
services, so no `compose up`-time dependency on `eden-repo-init` is
needed.

Worker hosts and orchestrator are long-running clients without HTTP
healthchecks; `compose up --wait` waits on `service_healthy` for
healthcheck-declaring services and `service_started` for the
others. Acceptable for this chunk.

### F.1 Generated `.env` shape

`reference/compose/.env` after a successful setup-experiment run:

```dotenv
# 10a (preserved or generated)
POSTGRES_DB=eden
POSTGRES_USER=eden
POSTGRES_PASSWORD=<32-hex>
POSTGRES_HOST_PORT=5433
GITEA_SECRET_KEY=<64-hex>
GITEA_INTERNAL_TOKEN=<64-hex>
GITEA_HOST_PORT=3001
GITEA_SSH_HOST_PORT=2222

# 10b/c (new)
EDEN_EXPERIMENT_ID=<from --experiment-id>
EDEN_SHARED_TOKEN=<32-hex>
EDEN_SESSION_SECRET=<32-hex>
EDEN_STORE_URL=postgresql://eden:<password>@postgres:5432/eden
EDEN_PLAN_TASKS=3
EDEN_BASE_COMMIT_SHA=<seed-sha>
WEB_UI_HOST_PORT=8090
```

### G. `compose-smoke` CI job extension

The existing `compose-smoke` job (10a) already brings the stack up
with `--wait` and asserts a few invariants. With 10b/10c added, it
extends to:

1. Run `bash reference/scripts/setup-experiment/setup-experiment.sh
   tests/fixtures/experiment/.eden/config.yaml --experiment-id
   smoke-exp` to generate `.env`. (This step also builds the image
   and seeds the repo per section E.)
2. **Assert seeded ref before starting the stack.** Reuse the
   already-built `eden-reference:dev` image (it has git
   installed) for a one-shot probe that mounts the volume:
   `docker compose --env-file .env run --rm --no-deps
   --entrypoint sh eden-repo-init -c "git -C /var/lib/eden/repo
   show-ref refs/heads/main"` returns a SHA matching
   `EDEN_BASE_COMMIT_SHA` from `.env`. Reusing the image
   avoids depending on a floating `latest` tag of an external
   image and matches the rest of the stack's pinning discipline.
   This step also avoids races on long-running services that may
   not yet exist or may have already exited.
3. `docker compose --env-file .env up -d --wait --wait-timeout
   180`.
4. Existing volume + Postgres + Gitea assertions (10a).
5. **New:** wait up to 60s for the orchestrator to exit 0 (the
   scripted workers run a 3-trial experiment and the
   orchestrator's `--max-quiescent-iterations` flag triggers
   exit). The poll uses `docker compose ps -a --format json
   orchestrator` and reads the exit code via `docker inspect
   --format '{{.State.ExitCode}}' eden-orchestrator` (10a's
   shape-stable pattern).
6. **New:** assert the final task-store state — read `/v0/.../events`
   over HTTP via `curl` + `jq` and confirm presence of a
   `task.terminated` event for each plan task.

Steps 5 + 6 turn the 10a smoke into a *light* end-to-end test —
*not* the comprehensive 10e e2e (which adds Web UI walkthroughs,
admin actions, and termination scenarios), but enough to catch a
broken Compose wiring before merge. The tradeoff: smoke wallclock
goes up from ~30s to ~90s. Acceptable.

**Test job for parametrized backends:** A new `python-test-postgres`
CI job brings up `postgres:16.6-alpine` as a service container,
sets `EDEN_TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/eden`,
and runs the parametrized backend tests. This is in addition to —
not replacing — the existing `python-test` job, which continues to
run with the `EDEN_TEST_POSTGRES_DSN` unset (so the postgres rows
skip). Splitting keeps the fast `python-test` job fast and isolates
the postgres flakiness if any surfaces.

### H. Documentation

- [`reference/compose/README.md`](../../reference/compose/README.md):
  - Replace "What's not here yet" list with the post-10c reality.
  - Document setup-experiment workflow.
  - Document the **Gitea-is-idle** posture explicitly so a reader
    doesn't think it's a bug.
- [`reference/scripts/setup-experiment/README.md`](../../reference/scripts/setup-experiment/README.md):
  new. Documents inputs, outputs, idempotency.
- [`reference/services/task-store-server/README.md`](../../reference/services/task-store-server/README.md):
  add `--store-url` documentation.
- `AGENTS.md`: prepend a "Phase 10 chunk 10b + 10c complete" paragraph;
  refresh the commands table (new `--store-url` flag, new
  setup-experiment command, new compose `up` workflow).
- `docs/roadmap.md`: mark 10b + 10c complete with a paragraph
  summary.

## Files to reference

- [`reference/packages/eden-storage/src/eden_storage/sqlite.py`](../../reference/packages/eden-storage/src/eden_storage/sqlite.py)
  and [`_schema.py`](../../reference/packages/eden-storage/src/eden_storage/_schema.py)
  — SqliteStore is the structural template for PostgresStore.
- [`reference/packages/eden-storage/src/eden_storage/_base.py`](../../reference/packages/eden-storage/src/eden_storage/_base.py)
  — shared transition logic; PostgresStore extends `_StoreBase`.
- [`reference/services/task-store-server/src/eden_task_store_server/app.py`](../../reference/services/task-store-server/src/eden_task_store_server/app.py)
  — `build_store` URL dispatch.
- [`reference/services/task-store-server/src/eden_task_store_server/cli.py`](../../reference/services/task-store-server/src/eden_task_store_server/cli.py)
  — CLI flag rename.
- [`reference/services/orchestrator/tests/test_e2e.py`](../../reference/services/orchestrator/tests/test_e2e.py)
  — pre-existing real-subprocess e2e is the closest cousin to what
  the smoke job exercises.
- [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml)
  — extended in place.
- [`reference/compose/healthcheck/smoke.sh`](../../reference/compose/healthcheck/smoke.sh)
  — extended in place.
- The 10a impl-review patterns: setup script must read the
  generated `.env` rather than duplicating values; trap-cleanup
  ordering in shell scripts.

## Verification

Run before declaring done:

1. `uv run ruff check .` clean.
2. `uv run pyright` clean.
3. `uv run pytest -q` clean (postgres rows skip without DSN).
4. With Postgres running locally: `EDEN_TEST_POSTGRES_DSN=…
   uv run pytest reference/packages/eden-storage/tests` — full
   parametrized backend coverage green.
5. `cd reference/compose && bash
   ../scripts/setup-experiment/setup-experiment.sh
   ../../tests/fixtures/experiment/.eden/config.yaml
   --experiment-id smoke-exp` generates a sensible `.env` +
   `experiment-config.yaml`.
6. `docker compose --env-file .env up --wait` — every service
   reaches its terminal state (healthy / exited-0 / running)
   within the wait window.
7. `bash healthcheck/smoke.sh` — full extended smoke green
   locally.
8. `docker compose down -v` cleans up.
9. CI green on all jobs including the new `python-test-postgres`
   and the extended `compose-smoke`.

## Execution order

1. **PostgresStore** (chunk-internal milestone 1).
   1. New `postgres.py` + `_postgres_schema.py`.
   2. `task-store-server` URL dispatch.
   3. Tests parametrized over the new backend.
   4. Local + CI green before moving on.
2. **Service Dockerfile + .dockerignore**.
   1. Add `Dockerfile` + `.dockerignore`.
   2. `docker build` against repo root succeeds; image runs each
      service module under a smoke `--help` invocation.
3. **Compose service entries**.
   1. Wire all six EDEN services + the bare-repo init service.
   2. `docker compose up --wait` brings the whole stack online.
4. **setup-experiment script**.
   1. Wire the script.
   2. End-to-end: `setup-experiment.sh && docker compose up
      --wait` runs the fixture experiment to quiescence.
5. **Smoke + CI extension**.
   1. Extend `smoke.sh` with the new assertions.
   2. Add `python-test-postgres` job.
6. **Documentation refresh**.

If implementation runs over context, stop at the next **chunk-internal
milestone** boundary — never mid-milestone.

## Out of scope

- **Gitea integration** as the workers' actual git remote (deferred
  to a follow-up sub-chunk after 10d).
- **Experiment-specific images** for the LLM implementer's sandbox
  (10d).
- **Control-plane registration** (Phase 12).
- **Postgres connection pooling** (single connection per store
  instance is fine for one orchestrator + one task-store-server).
- **Multi-replica orchestrator coordination** (Phase 12).
- **Resource limits** on Compose services (10d when LLM workers
  introduce real cost).
- **Branch-protection update** to require the new
  `python-test-postgres` job — defer to a follow-up after a few
  clean runs (same posture 10a took for `compose-smoke`).

## Risks / things to watch

- **`uv sync --frozen` in Docker requires `uv.lock`.** If the lock
  file ever becomes stale relative to `pyproject.toml`, `--frozen`
  fails the build. We commit `uv.lock`; the existing `python-lint`
  / `python-typecheck` / `python-test` jobs all use `--frozen`
  successfully, so this is consistent with current discipline.

- **`postgres:16.6-alpine` image hash.** 10a pinned the major.minor;
  we should consider pinning the digest in CI for absolute
  determinism. Currently we don't — out of scope for this chunk.

- **Compose `configs:` vs bind-mounting the experiment-config
  YAML.** Compose configs are nicer (immutable, declared centrally)
  but only work in Swarm or a recent-enough Compose v2. The
  fallback is a `volumes:` entry mounting the file. Verify whether
  CI's Compose version supports `configs:` in non-Swarm mode; if
  not, fall back to a regular bind-mount. (Compose v2.20+ supports
  it; CI's `ubuntu-latest` ships v2.27+; should be fine.)

- **psycopg's binary wheel availability on Linux/arm64.** Most
  platforms have wheels for `psycopg[binary]>=3.2`; if a CI runner
  doesn't, fall back to building from source — adds ~30s to image
  build. Acceptable.

- **Image bloat.** The shared image will be ~400MB (Python slim +
  workspace + git). If this becomes problematic later, multi-stage
  with a `wheels` builder is the standard fix; not needed yet.

- **The web-ui's `--repo-path` flag.** The web-ui needs the bare
  repo for the work-refs admin sub-page. We mount it. The flag
  also gates the entire implementer module (chunk 9c), which the
  scripted implementer-host service makes redundant — both modules
  now race for the same tasks. **In Compose**, only the
  implementer-host should claim implement tasks; the web-ui's
  implementer module is for human override / debugging. We keep
  both available; the user opens whichever they prefer. Document
  this in the compose README.
