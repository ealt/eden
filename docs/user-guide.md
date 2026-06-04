# EDEN operator guide

> **Last verified against commit `84adb50` (Phase 12a-1).** EDEN moves fast — if a flag name, file path, or behavior described here doesn't match what you see, trust the code and file an issue.

This guide is for someone bringing up an EDEN experiment and driving it through one or more roles. It is not a contributor guide ([`AGENTS.md`](../AGENTS.md) covers that) or a protocol-design reference ([`docs/glossary.md`](glossary.md) + [`spec/v0/`](../spec/v0/) cover those). Terms-of-art used below — *ideator*, *executor*, *evaluator*, *integrator*, *idea*, *variant*, *evaluation*, *task*, *event*, *claim* — are defined in the glossary; this guide assumes you've at least skimmed it.

## Contents

1. [Components in 30 seconds](#1-components-in-30-seconds)
2. [Workflow 0: setting up an experiment](#2-workflow-0-setting-up-an-experiment)
3. [Worker host modes](#3-worker-host-modes)
4. [Storage backends](#4-storage-backends)
5. [Workflow 1: ideation](#5-workflow-1-ideation)
6. [Workflow 2: execution](#6-workflow-2-execution)
7. [Workflow 3: evaluation](#7-workflow-3-evaluation)
8. [Workflow 4: admin operations](#8-workflow-4-admin-operations)
9. [Workflow 5: passive observation](#9-workflow-5-passive-observation)
10. [Auth principal matrix](#10-auth-principal-matrix)
11. [Gotchas + resets](#11-gotchas--resets)
12. [Multi-experiment deployments](#12-multi-experiment-deployments)

## 1. Components in 30 seconds

The reference deployment is a Docker Compose stack with the following pieces. You rarely interact with all of them directly; most operator work flows through the Web UI, the `eden-manual` CLI, Forgejo, or `setup-experiment.sh`.

| Component | What it is | When you touch it |
|---|---|---|
| **task-store-server** | The wire API. Owns task / idea / variant / event state. | Almost never directly — Web UI, CLI, and workers all talk to it. Curl it for ad-hoc state inspection. |
| **orchestrator** | Polling loop. Seeds initial ideation tasks at startup, then dispatches execution tasks from ready ideas, evaluation tasks from successful variants, and integrates `success` variants. Exits 0 on quiescence. | Restart it when it quiesces between manual actions (see [§11](#11-gotchas--resets)). |
| **ideator-host** / **executor-host** / **evaluator-host** | Auto-worker hosts. Tight-poll for tasks of their kind, claim, and submit results. Each runs in one of three [modes](#3-worker-host-modes). | Stop them when you want to drive a role manually; start them when you want the demo to flow on its own. |
| **web-ui** | Operator UI (`localhost:8090`). Surfaces ideator / executor / evaluator forms + admin dashboards (`/admin/*`) + a work-refs GC page. | Day-to-day manual driving + observation. |
| **forgejo** | The workers' git remote (`localhost:3001`). Holds the experiment repo: seed commit + `work/*` branches (executor scratch) + `variant/*` branches (canonical integrated lineage). | Inspecting the actual git tree (web UI or local clone). |
| **postgres** | Backing store for the task-store-server in the default Compose deployment (`localhost:5433`). | Almost never. SQL inspection is possible but you usually want the wire API. |
| **artifacts directory** | Host bind-mount under `${EDEN_EXPERIMENT_DATA_ROOT}/artifacts/` (Phase 12a-1g) where worker-emitted artifacts live (idea content, evaluation output files). Mounted into web-ui at `/var/lib/eden/artifacts`. | Reading an idea's content before you execute it: `docker exec eden-web-ui cat /var/lib/eden/artifacts/<idea-id>.md`, or directly on the host at `$EDEN_EXPERIMENT_DATA_ROOT/artifacts/`. |
| **service logs** | Per-service JSON-line logs under `${EDEN_EXPERIMENT_DATA_ROOT}/logs/<service>/<service>.jsonl` (issue #109). Same records as `docker compose logs`; survives `compose down -v` because it's a host bind-mount. Rotates at 50MB × 5 per service. | Post-crash forensics: `tail -F $EDEN_EXPERIMENT_DATA_ROOT/logs/orchestrator/orchestrator.jsonl \| jq`. See [`observability.md` §2.5](observability.md#25-container-logs). |
| **grafana** (opt-in) | Log search UI at `localhost:3000`, opt-in via `-f compose.logging.yaml` (issue #110). Loki + Alloy index the service-logs JSONL for cross-service / time-window search. Not in the default stack. | Cross-service log search: sign in as `admin` / `EDEN_GRAFANA_ADMIN_PASSWORD`. See [`observability.md` §2.8](observability.md#28-log-search-ui-loki--alloy--grafana). |
| **setup-experiment.sh** | Idempotent bootstrap. Generates secrets, copies the experiment config into the compose dir, provisions Forgejo (user + repo + credential helper), seeds the bare-repo volume + pushes the seed to Forgejo, captures the seed SHA. | Every experiment starts here. Re-run to rotate config; secrets are preserved. |
| **eden-manual CLI** | A thin wrapper over the wire API for manual role-driving from the terminal. Located at [`reference/scripts/manual-ui/eden-manual`](../reference/scripts/manual-ui/eden-manual). | When you want to drive a role end-to-end without the Web UI. |
| **.env** | `reference/compose/.env` — the secrets + deployment knobs file. Generated by `setup-experiment`. Gitignored. | Read it for admin token / Forgejo password / etc. Per-experiment knobs like the quiescence budget now live in the experiment-config YAML (`max_quiescent_iterations`), not here. |

## 2. Workflow 0: setting up an experiment

### Prereqs

- Docker + the `docker compose` v2 plugin
- Python 3 (for setup-experiment's secret generation + the eden-manual CLI)
- This repo cloned, working directory in the repo root
- `uv sync` is only needed if you plan to run pytest / lints; not for operating the stack

### Authoring an experiment config

Every experiment is driven by a YAML config validated against [`spec/v0/schemas/experiment-config.schema.json`](../spec/v0/schemas/experiment-config.schema.json). The required + optional fields:

| Field | Type | Required | Notes |
|---|---|---|---|
| `parallel_variants` | integer ≥ 1 | yes | Max variants in flight simultaneously. |
| `evaluation_schema` | map | yes | Field name → one of `"integer"`, `"real"`, `"text"`. Reserved names (`variant_id`, `commit_sha`, `parent_commits`, `branch`, `status`, `artifacts_uri`, `description`, `timestamp`, `started_at`, `completed_at`) are forbidden. |
| `objective.expr` | string | yes | Scalar expression over `evaluation_schema` fields. |
| `objective.direction` | `"maximize"` \| `"minimize"` | yes | |
| `dispatch_mode.<key>` | `"auto"` \| `"manual"` | no | Per-key gate on the orchestrator's five decision types (`termination` defaults to `"manual"`; the four operational keys default to `"auto"`). See [`02-data-model.md`](../spec/v0/02-data-model.md) §2.4. |
| `ideation_policy` | object | no | Named ideation-creation policy (`kind`: `maintain_pending` \| `fixed_total`). Defaults to `maintain_pending(target=3)`. See [`02-data-model.md`](../spec/v0/02-data-model.md) §2.4. |
| `termination_policy` | object | conditional | Named termination policy (`kind`: `never_terminate` \| `max_variants` \| `max_wall_time` \| `convergence_window` \| `target_condition`). **Required when `dispatch_mode.termination == "auto"`**, ignored when `"manual"`. See the recipes in [`docs/operations/experiment-lifecycle.md`](operations/experiment-lifecycle.md). |
| `baseline.enabled` | boolean | no | When `true` (the default, also the meaning of an absent `baseline` block), the orchestrator elevates the experiment seed to a first-class `kind == "baseline"` variant that is scored like any other variant. Set `false` to suppress baseline creation. See [`02-data-model.md`](../spec/v0/02-data-model.md) §2.7 / §9.4. |
| `baseline.metrics` | map | no | Optional config-supplied evaluation metrics (subset of `evaluation_schema`). When present, the orchestrator stamps these and creates the baseline directly in `success`, skipping the baseline's evaluation dispatch — useful when the evaluator is expensive or a known-good baseline score exists. Supplying `metrics` with `enabled: false` is a config error. |
| `max_quiescent_iterations` | integer ≥ 2 | no | A polling orchestrator exits after N consecutive no-progress iterations. Default 3; manual-UI sessions want a much higher value (e.g. 3600). |
| `ideation_task_deadline` / `execution_task_deadline` / `evaluation_task_deadline` | number > 0 | no | Seconds each worker host waits for one `*_command` invocation. Defaults: 120 / 600 / 300. |
| `auto_checkpoint` | object | no | Opt-in automatic checkpointing ([issue #131](https://github.com/ealt/eden/issues/131)). Absent block ≡ `{enabled: false}`. See [the subsection below](#automatic-checkpointing-auto_checkpoint). |
| `ideation_command` / `execution_command` / `evaluation_command` | string | no (impl-specific) | Shell commands to invoke for [subprocess mode](#3-worker-host-modes). Travel under additional-properties; the spec defers role-bindings to a future chapter. |

The pre-12a-3 top-level termination fields (`max_variants` / `max_wall_time` / `convergence_window` / `target_condition`) are **removed** from the normative schema — their semantics now round-trip as `termination_policy.kind` values selected declaratively per [`03-roles.md`](../spec/v0/03-roles.md) §6.2 decision-type 0 (see [`docs/operations/experiment-lifecycle.md`](operations/experiment-lifecycle.md) for the operator playbook). Configs that still carry the old top-level fields validate (they round-trip under the schema's permissive additional-properties posture) but the orchestrator ignores them.

The `termination_policy` / `max_quiescent_iterations` / `*_task_deadline` fields were CLI flags / deployment env vars before [issue #157](https://github.com/ealt/eden/issues/157); they moved into the experiment config because two experiments sharing one deployment plausibly want different values. A worked example using all of them:

```yaml
parallel_variants: 4
evaluation_schema:
  accuracy: real
objective:
  expr: "accuracy"
  direction: "maximize"
dispatch_mode:
  termination: auto          # required for termination_policy to be consulted
ideation_policy:
  kind: fixed_total
  total: 20
termination_policy:
  kind: max_wall_time
  duration: "PT2H"           # ISO 8601 duration; terminate after 2 hours
max_quiescent_iterations: 60
ideation_task_deadline: 180.0
execution_task_deadline: 900.0
evaluation_task_deadline: 300.0
```

Concrete example — the repo's fixture at [`tests/fixtures/experiment/.eden/config.yaml`](../tests/fixtures/experiment/.eden/config.yaml):

```yaml
parallel_variants: 1
evaluation_command: "python3 ${EDEN_EXPERIMENT_DIR}/evaluation.py"
execution_command: "python3 ${EDEN_EXPERIMENT_DIR}/execution.py"
ideation_command: "python3 ${EDEN_EXPERIMENT_DIR}/ideation.py"
evaluation_schema:
  score: real
objective:
  expr: "score"
  direction: "maximize"
```

**Convention** (not required by the schema): put your config at `<your-experiment-dir>/.eden/config.yaml`. `setup-experiment.sh` defaults `--experiment-id` from the directory containing `.eden` and uses that directory as the host-side bind-mount source in subprocess mode.

### Automatic checkpointing (`auto_checkpoint`)

[Issue #131](https://github.com/ealt/eden/issues/131) makes portable checkpoints ([Phase 12b](https://github.com/ealt/eden/issues/152)) happen *automatically* so the "I can accept interruptions / teardown / redeployment" posture is viable without operator action. It is a deployment behavior consumed by the reference orchestrator — not a protocol contract — and is **opt-in** (an absent block means `{enabled: false}`).

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | boolean | `false` | Master switch. |
| `interval_seconds` | number > 0 | `3600` | Cadence between periodic checkpoints, in **seconds** (matching the `*_task_deadline` convention — `3600` is 60 minutes; `1800` is 30 minutes). |
| `retention_count` | integer ≥ 1 | `6` | Ring-buffer depth for periodic checkpoints; the oldest periodic archives beyond this count are pruned. Does **not** bound the terminal checkpoint. |
| `on_terminate` | boolean | `true` | Take one terminal checkpoint when the experiment reaches `state == "terminated"`. |

```yaml
auto_checkpoint:
  enabled: true
  interval_seconds: 1800     # every 30 minutes (seconds, not minutes!)
  retention_count: 6
  on_terminate: true
```

Two triggers fire while the orchestrator is running: a **cadence** checkpoint every `interval_seconds` (the first fires one full interval after orchestrator startup), and one **terminal** checkpoint if the orchestrator observes the experiment reach `terminated`. Restoration stays **operator-driven** — there is no auto-restore; use the manual `eden-experiment restore` flow.

**The destination directory is deployment wiring, not a config field.** A host filesystem path is meaningless on a different deployment, so the portable config carries only the *intent* (enabled / cadence / retention / on-terminate); *where* the archives land is the orchestrator's `--auto-checkpoint-dir` flag (env `EDEN_AUTO_CHECKPOINT_DIR`). In the Compose deployment this is pre-wired to `/var/lib/eden/checkpoints`, bind-mounted from `${EDEN_EXPERIMENT_DATA_ROOT}/checkpoints` — so enabling the block in your config is all it takes; the archives appear under the experiment's host data root. When `auto_checkpoint.enabled` is `true` the orchestrator **fails fast at startup** if no admin token is available (the export endpoint is admin-gated per [`07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) §14) or the destination directory is missing/unwritable.

Periodic archives are named `<safe_exp>-<UTC-timestamp>.tar`; the terminal archive is `<safe_exp>-terminated-<UTC-timestamp>.tar` (kept outside the retention ring). See [`docs/observability.md`](observability.md) §"Checkpoint cadence" for the cadence guarantees, the documented orphan gap, and the disk-growth consideration.

### Specifying the starting git tree

By default, `setup-experiment.sh` seeds the experiment's git repo with an empty commit containing only a `.gitkeep` file. To start from real content (e.g. an existing application repo you want to evolve):

```bash
bash reference/scripts/setup-experiment/setup-experiment.sh \
    path/to/your/experiment/.eden/config.yaml \
    --name my-experiment \
    --seed-from /path/to/your/app/repo
```

Since [issue #128](https://github.com/ealt/eden/issues/128), the
experiment id is **system-minted and opaque** (`exp_<26-char-ULID>`);
`--name` supplies an optional operator-facing display label, not the id.
The minted id is written to `.env` as `EDEN_EXPERIMENT_ID`. See [§2's
"Running setup-experiment"](#running-setup-experiment) for the full
flag set and what setup mints.

Seed-from semantics (see [`repo_init.py`](../reference/services/_common/src/eden_service_common/repo_init.py) + [`repo.py:seed_bare_repo_from_dir`](../reference/services/_common/src/eden_service_common/repo.py)):

- If `<seed-from>` is itself a git working tree, only tracked + untracked-but-not-ignored files are copied (respects `.gitignore`).
- Otherwise the directory's contents are copied verbatim, skipping nested `.git/`.
- **No history is preserved** — the result is a single seed commit on `main` with a fixed identity/date.
- Submodules are NOT recursively snapshotted (skipped).

`--seed-from` requires a fresh seed. Re-running setup with a different `--seed-from` against an already-seeded stack: setup-experiment drops the staging volume so the new content takes effect, but the existing repo on Forgejo + the per-host bare-clone directories under `${EDEN_EXPERIMENT_DATA_ROOT}` still hold the prior seed. If you've already run `compose up` once, do a full wipe (see [§11](#11-gotchas--resets)) before re-seeding.

### Running setup-experiment

```bash
bash reference/scripts/setup-experiment/setup-experiment.sh <config.yaml> [flags]
```

Flags (all optional except the positional config):

| Flag | Default | Effect |
|---|---|---|
| `--name <display-name>` | none (config's parent-dir basename, if supplying a label) | Optional operator-facing display label for the experiment. **Not** the id: since [#128](https://github.com/ealt/eden/issues/128) setup mints an opaque `exp_<ULID>` id and writes it to `.env` as `EDEN_EXPERIMENT_ID`. Names MAY collide; the id is the stable handle. |
| `--admin-token <T>` | preserved or generated | Admin bearer used by the operator + setup-time scripts. The `admin` bearer principal is a literal sentinel — no `worker_id` is minted for it. |
| `--postgres-password <P>` | preserved or generated | Postgres credential. Percent-encoded into the DSN. |
| `--env-file <path>` | `reference/compose/.env` | Where to write the generated `.env`. |
| `--experiment-dir <path>` | `<config>/..` | Host-side bind-mount source for subprocess mode. |
| `--data-root <path>` | `$HOME/.eden/experiments/<id>` | Host-side parent dir for every durable substrate bind-mount (Phase 12a-1g, chapter 01 §13). See [`docs/operations/experiment-data-durability.md`](operations/experiment-data-durability.md). |
| `--ideas-per-ideation <N>` | `1` | How many ideas each subprocess-mode ideation task asks for. |
| `--exec-mode host\|docker` | `host` | `docker` wraps each subprocess-mode `*_command` in a sibling container via DooD (host docker socket). |
| `--seed-from <host-dir>` | empty seed | See above. |
| `--no-auto-host-workers` | off (auto-hosts pre-registered) | Skip pre-registering the auto-host workers (display names `ideator-host-1` / `executor-host-1` / `evaluator-host-1`; their opaque `wkr_*` ids are minted at setup and written to `.env`). Use when running a fully-manual experiment where the auto-host services won't come up — avoids phantom workers in `/admin/workers/`. Tradeoff: reassigning a task to one of those workers returns `error=unknown-target` until the corresponding host self-registers (which never happens in fully-manual flows). The manual-UI wrapper (`eden-experiment up` without `--with-workers`) passes this automatically. |

Re-running setup is **idempotent**: existing secrets (`EDEN_ADMIN_TOKEN`, `POSTGRES_PASSWORD`, `EDEN_SESSION_SECRET`, `FORGEJO_*`) **and the minted opaque ids** (`EDEN_EXPERIMENT_ID` and the `wkr_*` / `grp_*` ids) are read back from `.env` and reused. Post-[#128](https://github.com/ealt/eden/issues/128) idempotency lives in `.env`, not the store: there is no operator-typed id to re-register against, so a fresh mint would create a new entity — setup reuses what `.env` already holds. Run it again to pick up config edits.

Produces:

- `reference/compose/.env` — generated. Gitignored (covered by the `reference/compose/.env.*` rule).
- `reference/compose/experiment-config.yaml` — copy of the input config, mounted into services.
- `.forgejo-creds-<exp_id>/credential-helper.sh` — git credential helper for workers pushing to Forgejo (the `<exp_id>` path segment is the minted opaque `exp_*` id).
- `${EDEN_EXPERIMENT_DATA_ROOT}/` — host-side substrate tree (postgres / forgejo / artifacts / per-host repo + credentials subdirs); the data-root path segment is the opaque `exp_*` id. See [`docs/operations/experiment-data-durability.md`](operations/experiment-data-durability.md).
- A seeded forgejo repo at `eden/<exp_id>` (opaque) and per-host bare-clone directories under `${EDEN_EXPERIMENT_DATA_ROOT}/`. The `wkr_*` worker ids and `grp_*` group ids for `operator` / orchestrator / web-ui / auto-hosts and the reserved `admins` / `orchestrators` groups are minted and written to `.env`.
- The `EDEN_BASE_COMMIT_SHA` line in `.env` replaced with the real seed SHA.

### Bringing the stack up

Full auto:

```bash
cd reference/compose
docker compose --env-file .env up -d --wait
open http://localhost:8090/
```

Selective (e.g. manual driving — start everything except auto-workers):

```bash
docker compose --env-file .env up -d --wait \
    postgres forgejo task-store-server orchestrator web-ui
```

Subprocess mode (workers invoke the `*_command` from the experiment config):

```bash
docker compose --env-file .env -f compose.yaml -f compose.subprocess.yaml up -d --wait
```

Docker-exec mode (subprocess + sibling-container isolation; requires `setup-experiment --exec-mode docker`):

```bash
docker compose --env-file .env \
    -f compose.yaml -f compose.subprocess.yaml -f compose.docker-exec.yaml \
    up -d --wait
```

> **Multi-experiment / control-plane mode.** A `control-plane` service
> (chapter 11: experiment registry + orchestrator leases + state-sync)
> runs on every stack but is opt-in — set
> `EDEN_CONTROL_PLANE_URL=http://control-plane:8081` to flip the
> orchestrator into lease-driven mode and surface the web-ui's
> `/admin/experiments/` dashboard. The reference impl hosts one
> experiment per task-store-server; the lease lifecycle (including a
> lease-handoff chaos drill) is exercised by
> `reference/compose/healthcheck/smoke-multi-experiment.sh`. See
> [`reference/compose/README.md`](../reference/compose/README.md)
> "Multi-experiment mode" for details.

### The orchestrator's quiescence-exit

The orchestrator is tuned for CI: the default budget (`max_quiescent_iterations: 3`, and `30` in the smoke-injected configs) × 1s poll is seconds of zero progress before it exits 0. With a human at the keyboard this fires constantly. Since [issue #157](https://github.com/ealt/eden/issues/157) the budget is the experiment-config `max_quiescent_iterations` field (the `EDEN_MAX_QUIESCENT_ITERATIONS` env var was retired). Set it in your experiment-config YAML **before** running setup-experiment:

```yaml
# in your experiment-config.yaml
max_quiescent_iterations: 86400   # 24h-equivalent budget for a human-paced session
```

The field requires `>= 2`; "never exit" is not available. Pick a large enough value for your session. See GitHub issue [#98](https://github.com/ealt/eden/issues/98) (multi-experiment orchestrator) for the structural fix tracked against Phase 12c.

### Verifying health

```bash
docker compose --env-file .env ps                          # everything Up + healthy
ADMIN=$(grep '^EDEN_ADMIN_TOKEN=' .env | cut -d= -f2)
EXP=$(grep '^EDEN_EXPERIMENT_ID=' .env | cut -d= -f2)       # opaque exp_* id minted by setup
curl -s -H "Authorization: Bearer admin:$ADMIN" \
     -H "X-Eden-Experiment-Id: $EXP" \
     "http://localhost:8080/v0/experiments/$EXP/tasks" | jq 'length'
```

The `{experiment_id}` path segment is now the opaque `exp_*` id (the operator-typed mnemonic is gone). To find an experiment by its display name, use the control-plane registry lookup: `GET /v0/control/experiments?name=<name>` returns 0..N matches (see [§12](#12-multi-experiment-deployments)).

You should see (default) 3 ideation tasks pending and nothing else.

### Tearing down

```bash
# Preserve substrate state — resume later with `compose up`:
docker compose --env-file .env stop
# Or:
docker compose --env-file .env down       # NOTE: no -v

# Full wipe — destroys everything including the bind-mounted
# substrate tree under $EDEN_EXPERIMENT_DATA_ROOT.
# (Assumed cwd: reference/compose/ — the surrounding `compose` calls
# pass `--env-file .env`, so `.env` resolves to reference/compose/.env.)
docker compose --env-file .env down -v
docker rm -f $(docker ps -aq --filter 'name=eden-' 2>/dev/null) 2>/dev/null
docker volume ls -q | grep eden | xargs -I{} docker volume rm {} 2>&1 | head
DATA_ROOT="$(sed -n 's/^EDEN_EXPERIMENT_DATA_ROOT=//p' .env)"
rm -rf "$DATA_ROOT"   # this is the destructive step — see chapter 01 §13
```

Note: as of Phase 12a-1g (chapter 01 §13), durable substrate state is on the host filesystem under `${EDEN_EXPERIMENT_DATA_ROOT}/`, NOT inside Docker named volumes. So `docker compose down -v` removes the remaining named volumes (`eden-repo-init-staging`, `eden-worktrees`) but DOES NOT touch the substrate tree. To force a full reset you must additionally `rm -rf` the data root. See [`docs/operations/experiment-data-durability.md`](operations/experiment-data-durability.md).

The full wipe (including the `rm -rf` of the data root) is required before re-seeding with `--seed-from` (see above) and before changing the postgres password (an old data dir + a fresh `.env` password = unhealthy task-store-server).

## 3. Worker host modes

Each of `ideator-host` / `executor-host` / `evaluator-host` runs in one of three modes. Selection is `--mode` (per-host CLI flag) + `--exec-mode` (orthogonal isolation overlay).

| Mode | What runs the work | When to pick |
|---|---|---|
| **scripted** (default) | Deterministic in-process Python fixture | CI smoke; demos where you want predictable behavior; learning the protocol |
| **subprocess** | The `*_command` from your experiment config, invoked as a subprocess on the host container | Real experiments where you supply ideation / execution / evaluation logic |
| **subprocess + docker-exec** | Same as subprocess but each invocation is wrapped in a sibling container via DooD | Real experiments where you want bug-isolation / dependency-isolation from your worker code |

Subprocess and docker-exec modes use the non-normative JSON-line protocol documented at [`spec/v0/reference-bindings/worker-host-subprocess.md`](../spec/v0/reference-bindings/worker-host-subprocess.md). The ideator is long-running (holds an LLM session across ideation tasks); the executor and evaluator spawn per-task with cwd set to a freshly-created git worktree.

Docker-exec is a **soft** isolation boundary — a malicious `*_command` has full docker daemon access. Use it for bug isolation, not for hostile-code containment. See the chapter's §7 informative note.

## 4. Storage backends

The task-store-server's `--store-url` accepts three forms; the default Compose deployment uses postgres.

| URL | Backend | When |
|---|---|---|
| `postgresql://…` | Postgres | Default Compose deployment. Durable across restarts; required for any deployment where you care about not losing state. |
| `sqlite:///<path>` | SQLite (WAL, `synchronous=FULL`) | Local development. Durable across restarts. Single-writer. |
| `:memory:` | In-memory | Tests only. Non-durable. |

The schema is parallel across backends (the `Store` Protocol is in [`reference/packages/eden-storage/src/eden_storage/_base.py`](../reference/packages/eden-storage/src/eden_storage/_base.py)), so switching is a `--store-url` change at task-store-server startup.

## 5. Workflow 1: ideation

The ideator turns an `ideation` task into one or more `Idea` records. An idea names a slug, a priority, parent commits, and a rationale (free-text markdown stored as an artifact).

### Ideation via the Web UI

Sign in to `http://localhost:8090/`. Navigate to the ideator page. The form lets you draft N ideas with slugs / priorities / parent_commits / rationale, then submit. The Web UI walks the spec's three-phase write (`create_idea(drafting)` × N → `mark_ready` × N → `submit`).

Each idea row accepts an optional markdown body **plus** any number of file uploads (issue #120). The bundler picks the right shape automatically:

- text only → stored as `<idea_id>.md` (the existing single-file shape);
- one file, no text → stored as `<idea_id>.<ext>`;
- text + one-or-more files OR two-or-more files → wrapped server-side into `<idea_id>.tar.gz` with a top-level `manifest.json` enumerating each entry's path, size, and content-type. The evaluator's draft page renders the manifest as a per-file link table, and an inline `idea.md` (when present in the bundle) is shown as the headline above the table.

When you attach files, the text body is optional — uploads alone are a valid artifact. **Browser file inputs do not survive "add another row"**, so finalize file selections in the same submission as the row's text and metadata.

### Ideation via the CLI (`eden-manual`)

```bash
EDEN=reference/scripts/manual-ui/eden-manual

$EDEN list-tasks --kind ideation --state pending
$EDEN claim <task-id> --worker-name eden-manual  # registers worker on first use; server mints the wkr_* id
# Author an ideas JSON file (see the skill or just emit a `{"ideas": [...]}`)
$EDEN ideation-submit <task-id> --ideas-file /path/to/ideas.json --status success
```

Each idea entry in the JSON file may include `content_files` (a list of host-local paths) alongside `content` (issue #120). Same single-file vs `.tar.gz` selection as the Web UI:

```json
{
  "ideas": [
    {
      "slug": "design-with-diagram",
      "priority": 1.5,
      "parent_commits": ["a1b2..."],
      "content": "# rationale\n\nuse SVG ...",
      "content_files": ["/tmp/arch.svg", "/tmp/sample-data.json"]
    }
  ]
}
```

On first claim the CLI registers a worker with the display name `eden-manual` (`--worker-name`, default `eden-manual`), reads the server-minted opaque `worker_id` (`wkr_*`) back from the wire response, and persists `{worker_id, name, token}` at `/tmp/eden-manual/.credentials.json` (mode 0600). Since [#128](https://github.com/ealt/eden/issues/128) there is no operator-typed id to re-register against, so the cached `worker_id` is the recovery handle: on a fresh `/tmp` with the same cached id still in the registry, the CLI re-mints the credential via `reissue_credential(worker_id)` rather than re-registering by name. Names MAY collide — to find a worker by name, `GET .../workers?name=eden-manual` returns 0..N matches.

### Ideation via Claude

The `/eden-manual-ideator` skill walks you through it: lists tasks, claims, prompts you for idea content, submits via the CLI. See [`.claude/skills/eden-manual-ideator/SKILL.md`](../.claude/skills/eden-manual-ideator/SKILL.md).

## 6. Workflow 2: execution

The executor turns one execution task (which references an idea) into a git commit on top of `idea.parent_commits`, pushes the commit + a canonical `work/<variant_id>-<slug>` ref to Forgejo, and records a `Variant`. The integrator later squashes the work branch into a `variant/<variant_id>-<slug>` ref.

### Execution via the Web UI

The executor page lists pending execution tasks in a high-signal table — **slug**, **priority**, **target**, **created by** — sorted by priority (highest first) by default. Click the **slug** or **priority** header to re-sort (click again to flip direction). Filter chips above the table drive the view via URL query params (so a sorted/filtered view is bookmarkable and shareable):

- **eligible for me** (default ON) — hides tasks you can't claim (targeted at another worker, or a group you're not in). Toggle it off to see every pending task; ineligible rows then render a disabled claim button with a tooltip explaining why.
- **target: all / targeted / untargeted** — filter by whether the task names a `target` at all.
- **group by creator** — collapse the rows into a `<details>` group per idea author.

Each row has a **context links** expander (there is no inline content preview): one click reveals admin-detail links for the task, creator, and idea, plus the idea's artifacts (a per-entry link list for bundle artifacts, a single "view content" link for single-file artifacts). Claim a task with its in-row **claim** button. The claim form then surfaces the idea (slug / parents / rationale, rendered inline if the artifact is reachable). You provide the `commit_sha` of your already-pushed branch; the UI does the create-variant + ref-create + submit. **You're responsible for getting the commit into Forgejo** before pasting the SHA — clone forgejo locally, edit, commit, push, then run `git rev-parse HEAD` in your worktree to print the SHA to paste.

### Execution via the CLI (full end-to-end)

```bash
$EDEN list-tasks --kind execution --state pending
$EDEN show <task-id>                                       # see idea + rationale
$EDEN claim <task-id> --worker-name eden-manual            # registers (mints wkr_* id) on first use; mints stable variant_id
$EDEN checkout <task-id>                                   # clones forgejo at parent into /tmp/eden-manual/<task-id>

# Edit /tmp/eden-manual/<task-id> in your editor. Commit intermediate
# snapshots however you like — the orchestrator's integrator squashes
# the whole branch at integration time, so intermediate commit messages
# survive on work/* but not on the canonical variant/* lineage.

$EDEN push <task-id> --message "..."
# Pushes to the canonical `work/<variant_id>-<slug>` ref derived from
# the claim record + task — no operator-facing branch name to pick.
# If you committed yourself first, `push` skips the auto-commit and
# pushes existing HEAD. A no-op variant (same tree as parent) is
# refused at execution-submit time.

$EDEN execution-submit <task-id> --sha <sha> --description "..."
```

`description` is set on the variant record (read by the evaluator); it is NOT free-form metadata on the wire submission.

### Execution via mixed UI + local editor

If you've already claimed in the UI, the claim is held by the web-ui's own worker (display name `web-ui-1`, opaque `wkr_*` id). Post-12a-1, claim ownership is identity-keyed, so the CLI (acting as the `eden-manual` worker, a different `wkr_*` id) can't submit against that claim — `wrong-claimant`. Two paths:

- **Easiest:** clone forgejo locally yourself (read the opaque repo path from `.env`: `EXP=$(grep '^EDEN_EXPERIMENT_ID=' reference/compose/.env | cut -d= -f2)`, then `git clone http://eden:<pass>@localhost:3001/eden/$EXP.git`), edit, commit, push, paste the SHA into the UI's executor submit form.
- **Switch to CLI:** open `http://localhost:8090/admin/tasks/<task-id>/`, click reclaim. The web-ui's claim is wiped. Then claim again via the CLI and continue end-to-end.

### Execution via Claude

The `/eden-manual-executor` skill does claim + checkout + (your edits) + push + submit via the CLI. See [`.claude/skills/eden-manual-executor/SKILL.md`](../.claude/skills/eden-manual-executor/SKILL.md).

## 7. Workflow 3: evaluation

The evaluator scores a variant against the experiment's `evaluation_schema` and submits an `EvaluationSubmission` with `status: success / error / evaluation_error`. Successful evaluation is the trigger that lets the integrator integrate the variant.

### Evaluation via the Web UI

The evaluator page lists pending evaluation tasks in the same high-signal table as the executor page — **slug**, **priority**, **target**, **created by**, priority-sorted by default, with the same **eligible for me** / **target** / **group by creator** filter chips (all query-param driven). Each row's **context links** expander adds a link to the variant under evaluation (and shows its work-branch name) alongside the task / creator / idea / artifact links. Claim a task with its in-row **claim** button. The claim flow then surfaces the variant (branch / commit_sha / parent_commits / executor description / artifacts_uri) plus the idea context. The form auto-generates one input per `evaluation_schema` field, typed by the declared metric type. Submit a metric per field for `status=success`.

### Evaluation via the CLI

```bash
$EDEN list-tasks --kind evaluation --state pending
$EDEN claim <task-id> --worker-name eden-manual            # registers (mints wkr_* id) on first use

# Clone the variant's commit locally to inspect:
$EDEN checkout <task-id>                                   # clones at variant.commit_sha

# Decide your metrics, then submit:
$EDEN evaluation-submit <task-id> --status success \
    --field score=0.87 \
    --field retries=3
```

Metric values are type-checked against the schema before submission. For integer fields, the wire-legal `1.0` form is accepted; non-finite floats and out-of-schema fields are rejected.

### Evaluation via Claude

`/eden-manual-evaluator` walks you through it. See [`.claude/skills/eden-manual-evaluator/SKILL.md`](../.claude/skills/eden-manual-evaluator/SKILL.md).

## 8. Workflow 4: admin operations

This section covers the mutating admin actions. For read-only inspection (tasks / ideas / variants / events / workers via wire API or SQL), see [`docs/observability.md`](observability.md).

Boilerplate for the wire-API examples below:

```bash
ADMIN=$(grep '^EDEN_ADMIN_TOKEN=' reference/compose/.env | cut -d= -f2)
EXP=$(grep '^EDEN_EXPERIMENT_ID=' reference/compose/.env | cut -d= -f2)   # opaque exp_* id
H=(-H "Authorization: Bearer admin:$ADMIN" -H "X-Eden-Experiment-Id: $EXP")
BASE="http://localhost:8080/v0/experiments/$EXP"
```

### Reclaiming a stuck task

If a worker died holding a claim and the claim has no `expires_at`, the task is stuck `claimed` forever.

- **Web UI:** `http://localhost:8090/admin/tasks/<task-id>/` exposes a "reclaim" button (and a separate "force-reclaim, replays work" variant for already-submitted tasks).
- **Wire API:** `POST /v0/experiments/<id>/tasks/<task-id>/reclaim` with `{"cause": "operator"}`. **Requires a worker bearer**, not admin (per spec §13.3, admin bearers MUST NOT call worker-gated endpoints). Workers' own bearers are inside the corresponding containers; in practice, use the Web UI.

### Worker registry

Since [#128](https://github.com/ealt/eden/issues/128), `worker_id` is **system-minted and opaque** (`wkr_<26-char-ULID>`). You register a worker by posting an optional display `name` — the server mints and returns the id:

```bash
# Register: server mints the wkr_* id; capture it + the one-time token.
RESP=$(curl -s "${H[@]}" -H "Content-Type: application/json" \
    -d '{"name":"my-worker"}' -X POST "$BASE/workers")
WORKER_ID=$(echo "$RESP" | jq -r '.worker_id')
echo "$RESP" | jq -r '.registration_token'

# Rotate a worker's credential (path-param is the opaque id):
curl -s "${H[@]}" -X POST "$BASE/workers/$WORKER_ID/reissue-credential"

# Find a worker by display name (names MAY collide — 0..N matches):
curl -s "${H[@]}" "$BASE/workers?name=my-worker" | jq
```

Reserved values now live in **name-space**, not id-space: the worker names `admin`, `system`, `internal` are reserved (`register_worker(name=…)` with one of those returns 409 `eden://error/reserved-identifier`); the group names `admins`, `orchestrators` are reserved (auto-created at setup with minted `grp_*` ids). Display names are free-form Unicode (1–128 code points, NFC-normalized; an ill-formed name returns 422 `eden://error/invalid-name`). The deployment-admin **bearer principal** stays the literal `admin` — no `worker_id` is minted for it. To list / read the registry, see [`observability.md` §2.4](observability.md#24-wire-api-raw).

### Work-ref garbage collection

`http://localhost:8090/admin/work-refs/` lists every `refs/heads/work/*` branch in the local clone, classified by status (terminal-handled / orphaned / live). The page offers CAS-guarded deletion for the safe cases. Available only when the web-ui was started with `--repo-path`.

## 9. Workflow 5: passive observation

Moved to [`docs/observability.md`](observability.md). That doc enumerates every surface — first-party (web UI admin dashboards, Forgejo, artifacts directory, wire API, container logs, readonly Postgres role) and bring-your-own (Adminer, Swagger UI, desktop clients).

## 10. Auth principal matrix

Per [`spec/v0/07-wire-protocol.md`](../spec/v0/07-wire-protocol.md) §13 (12a-1). Bearer format is `<principal>:<secret>`; the principal is either the literal `admin` or an opaque `wkr_*` worker id. Post-[#128](https://github.com/ealt/eden/issues/128) the principal grammar is `^(admin|wkr_[0-9a-hjkmnp-tv-z]{26})$` — operator-typed kebab ids are gone. The `admin` principal is a deployment-scoped sentinel (no worker row); every other principal is a minted `wkr_*`.

| Endpoint class | Admin can | Worker can | Web-UI session can |
|---|---|---|---|
| Read tasks / ideas / variants / events / workers | ✅ | ✅ | ✅ |
| `POST /workers` (register) | ✅ | ❌ | ❌ |
| `POST /workers/<id>/reissue-credential` | ✅ | ❌ | ❌ |
| `POST /tasks/<id>/claim` | ❌ (§13.3) | ✅ | ✅ (acts as `web-ui-1`) |
| `POST /tasks/<id>/submit` | ❌ | ✅ (must own claim) | ✅ (must own claim) |
| `POST /tasks/<id>/reclaim` | ❌ (§13.3) | ✅ | ✅ (admin UI flow) |
| `POST /ideas` / `POST /variants` | ❌ | ✅ | ✅ |
| `POST /groups/*` | ✅ | ❌ | ❌ |
| `GET /whoami` | ✅ | ✅ | ✅ |

The Web UI is itself a worker (display name `web-ui-1`, opaque `wkr_*` id minted at setup) — its session-authenticated user actions are bearer-signed as that worker. This is why admin can read everything but can't act as a worker.

## 11. Gotchas + resets

### The quiescence trap

Covered in [§2](#2-workflow-0-setting-up-an-experiment). If the orchestrator's container shows `Exited (0)` and you don't know why, this is it.

### Scripted worker hosts will out-race you

If you start the full stack (with `compose up -d --wait` and no service list), the scripted worker hosts claim every pending task within milliseconds. To drive a role manually, either stop the corresponding host before bringing the stack up, or `docker compose stop <host>` after the fact. See [§2](#2-workflow-0-setting-up-an-experiment) selective-up.

### Credential file lost

If `/tmp/eden-manual/.credentials.json` is deleted, the cached opaque `worker_id` is lost too. The next CLI claim re-registers a **new** worker (display name `eden-manual` again, a fresh `wkr_*` id) and persists it — post-[#128](https://github.com/ealt/eden/issues/128) there is no operator-typed id to re-register against, so registration always mints a new worker (names MAY collide). The prior `eden-manual`-named worker remains in the registry as an orphan but is harmless. To recover the *same* worker identity instead, you'd need its persisted `wkr_*` id + `reissue_credential(worker_id)` (the recovery handle lives in the credentials file, not in the name).

### Substrate cleanup between full resets

`setup-experiment` rotates the postgres password on every invocation; the new password lands in `.env`, but the existing postgres data directory under `${EDEN_EXPERIMENT_DATA_ROOT}/postgres/` still has the old password baked into `pg_authid`. The next `compose up` then fails task-store-server's healthcheck with a `password authentication failed for user "eden"` deep in its logs. To force a full reset, after `docker compose down -v`, also `rm -rf "$EDEN_EXPERIMENT_DATA_ROOT"` (the bind-mount tree). See [`docs/operations/experiment-data-durability.md`](operations/experiment-data-durability.md). CI gets it for free because each job starts on a fresh runner and uses a per-script `mktemp -d` data root.

**Note**: `rm -rf "$EDEN_EXPERIMENT_DATA_ROOT"` also wipes the per-service log history under `${EDEN_EXPERIMENT_DATA_ROOT}/logs/` (issue #109). If a previous run crashed and you need the JSONL files for forensics, copy them out *before* the reset.

### `--seed-from` doesn't take effect on re-seed

If you've already run `compose up` and re-run `setup-experiment --seed-from <new-dir>`, the staging volume gets dropped but the per-host bare-clone directories under `${EDEN_EXPERIMENT_DATA_ROOT}/*-repo/` and the Forgejo repo at `${EDEN_EXPERIMENT_DATA_ROOT}/forgejo/` already hold the prior seed. A full wipe (`rm -rf "$EDEN_EXPERIMENT_DATA_ROOT"`) is required to truly re-seed.

### Wire 400 on `worker_id` in body

If you hand-craft a wire call for `claim` or `submit` and include `worker_id` in the JSON body, you'll get `eden://error/bad-request` with `"Extra inputs are not permitted"`. 12a-1 dropped that field — the claimant is the bearer's principal.

### Wire 403 admin on worker endpoints

`eden://error/forbidden` with `endpoint is worker-gated; admin bearers MUST NOT access it (§13.3)`. Admin can read but not claim / submit / reclaim. Use a worker bearer or go through the Web UI.

## 12. Multi-experiment deployments

A single task-store-server URL serves many experiments — the wire path is `/v0/experiments/{id}/…`, so the `{id}` segment selects the experiment per call. With a **control plane** configured (`--control-plane-url`), the Web UI lets one deployment register and switch between multiple experiments.

### 12.1 The experiment switcher

Register experiments on the cross-experiment dashboard at `/admin/experiments/` (the register form takes an optional display `name`; the control plane mints the opaque `exp_*` id), then pick the active one from the **top-nav switcher dropdown** (present on every page when a control plane is configured). The switcher renders each experiment as `<name> (<exp_*>)` when a name exists, the bare opaque id otherwise; it shows the active selection (or the default before you've selected one). Selecting an experiment is load-bearing: every per-experiment page — ideator, executor, evaluator, `/admin/tasks`, `/admin/variants`, `/admin/workers`, `/admin/groups`, `/admin/work-refs`, … — now reads that experiment's data, not just a relabelled header.

Because the experiment-id path segment is opaque (the operator-typed mnemonic is gone), the way to find an experiment by its display name is the control-plane registry lookup `GET /v0/control/experiments?name=<name>` (0..N matches; names MAY collide). The switcher's name rendering mitigates the loss of mnemonic URLs.

Notes:

- The selection is **per session**, not per tab. Switching in one tab changes the other tab's data on its next request.
- If you switch experiments while a draft form is open and then submit it, the submission is **discarded** (a banner explains why) rather than written to the experiment you switched to. Re-enter it under the now-active experiment.
- Selecting an experiment the control plane no longer knows about clears the stale selection and returns you to the dashboard.
- An experiment registered on the dashboard but not yet seeded (no `setup-experiment` / checkpoint-import run for it) renders an "initialize me" page rather than empty data.

Operationally the web-ui needs a worker credential in each experiment it talks to; with the deployment admin token present at runtime it mints these on first switch. See [`docs/operations/web-ui-multi-experiment.md`](operations/web-ui-multi-experiment.md) for the credential-bootstrap postures and the per-experiment config / repo layout.

### 12.2 Separate stacks (isolation)

For hard isolation (separate Postgres, Forgejo, secrets) run two Compose projects instead:

1. Use distinct `COMPOSE_PROJECT_NAME` values (e.g. `eden-exp-a` and `eden-exp-b`).
2. Override host ports so they don't collide: `POSTGRES_HOST_PORT`, `FORGEJO_HOST_PORT`, `FORGEJO_SSH_HOST_PORT`, `WEB_UI_HOST_PORT`.
3. Use distinct `--env-file` paths so each project has its own secrets.
4. Build the shared image once and re-use it across projects (Compose's image cache makes this automatic).
