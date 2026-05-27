# Issue #147 — Compose-smoke-multi-experiment CI job (Phase 12c backfill)

**Status.** Draft (plan).

**Predecessors.** Phase 12c (control plane) merged ([CHANGELOG](../../CHANGELOG.md) §"Phase 12c"); chapter 11 normative surface + `eden-control-plane` package + `reference/services/control-plane/` reference service + orchestrator `LeaseManager` + web-ui `/admin/experiments/` dashboard are all shipped. Reference impl is `v1+roles+orchestrator-substrate+lifecycle+checkpoints+multi-experiment` conformant: 246/246 conformance scenarios pass at the chapter-7 binding level. What 12c deferred was the **deployment-substrate** integration — `control-plane` is not yet a first-class Compose service, and there is no end-to-end multi-experiment smoke. This plan backfills both.

**Issue.** [eden#147](https://github.com/ealt/eden/issues/147). The deferral note at [`CHANGELOG.md`](../../CHANGELOG.md):

> *Compose smoke `compose-smoke-multi-experiment`* deferred: the chapter 11 surface is fully exercised by unit + wire + conformance tests. The existing 6 Compose smokes are unchanged in posture (when `--control-plane-url` is unset, behavior is unchanged) and continue to pass. A control-plane Compose service + multi-experiment smoke lands in a follow-up substrate chunk (Phase 13a Helm path is the natural home).

**Substrate choice — Compose (not Helm).** The CHANGELOG note left the substrate open ("Phase 13a Helm path is the natural home"). Phase 13a is still a draft plan ([`docs/plans/eden-phase-13a-helm-base-chart.md`](eden-phase-13a-helm-base-chart.md) §Status); shipping a multi-experiment smoke today should not block on 13a. The Compose stack is the canonical reference deployment for v0; this chunk fills the multi-experiment gap there, and 13a (when it lands) inherits the same smoke shape against the Helm substrate. This is the same posture #152 (compose-smoke-checkpoint) took for the Phase 12b checkpoint backfill — Compose first, Helm follows.

**Naming.** Pre-draft check against [`docs/glossary.md`](../glossary.md) and AGENTS.md "Naming discipline":

- "Multi-experiment" is the canonical noun for the deployment-level posture where one task-store-server + one control-plane + one (or more) orchestrator replicas host more than one registered experiment. No identifier renames; everything threaded through this plan reuses chapter 11's vocabulary (`register_experiment`, `acquire_lease`, `last_known_state`, `holder_instance`, …).
- "Cross-experiment isolation" is the smoke's load-bearing assertion shape — no task-id / event-stream / variant-id leakage between two registered experiments sharing the deployment substrate. Not a new spec term; observational only.
- The new smoke script and CI job follow the existing naming convention: `smoke-multi-experiment.sh` (parallel to `smoke.sh` / `smoke-subprocess.sh` / `smoke-multi-orchestrator.sh` / `smoke-checkpoint.sh`) and `compose-smoke-multi-experiment` (parallel to `compose-smoke-multi-orchestrator` / `compose-smoke-checkpoint`).

## 1. Context

### 1.1 What 12c shipped vs what's missing

Phase 12c landed the protocol-level multi-experiment surface end-to-end through unit + wire + conformance tests, but the deployment-substrate integration is partial:

| Layer | 12c state | Gap |
|---|---|---|
| Spec (chapter 11 + cross-references) | shipped | — |
| `eden-control-plane` package (client + models + errors) | shipped | — |
| Control-plane reference service (FastAPI app + Postgres / in-memory store + state-sync poller) | shipped at [`reference/services/control-plane/`](../../reference/services/control-plane/) | NOT wired into [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) — no `control-plane` Compose service exists |
| Orchestrator `LeaseManager` + `run_multi_experiment_loop` | shipped | Compose `orchestrator` service always passes `--experiment-id`; never invoked in multi-experiment mode under Compose |
| Web-ui `/admin/experiments/` dashboard | shipped at [`reference/services/web-ui/src/eden_web_ui/routes/admin_experiments.py`](../../reference/services/web-ui/src/eden_web_ui/routes/admin_experiments.py) | Compose `web-ui` service always sets `--experiment-id`; the cross-experiment dashboard route is registered ONLY when `control_plane is not None` — under Compose's default deployment that's never the case |
| `compose.control-plane.yaml` overlay file | exists but only overrides the web-ui service's `command:` to add `--control-plane-url` and `--control-plane-admin-token` | The overlay does NOT define a `control-plane` service of its own — layering it today produces a stack that THREADS the control-plane URL into web-ui but has no control-plane container to talk to |
| Conformance: `v1+multi-experiment` scenarios (chapter 11 surface) | shipped (28 scenarios; 9 documented skips per chapter 9 §6) | The 9 skipped scenarios are exactly the ones that need a running orchestrator + task-store-server alongside the control-plane (e.g. `Lease decision gating` event-log half, `Multi-experiment dispatch` event disjointness, `State synchronization` two-service hand-off, `Checkpoint import auto-register`). A Compose smoke is the right shape for those — they're substrate-bound, not chapter-7-binding-only |
| Compose smoke (`compose-smoke-multi-experiment`) | NOT shipped | This plan |
| CI job (`compose-smoke-multi-experiment`) | NOT shipped | This plan |
| setup-experiment ergonomics for multi-experiment | NOT shipped (single `EXPERIMENT_ID` per invocation) | This plan |

### 1.2 What "multi-experiment Compose deployment" means concretely

Per chapter 11 §2 + Decision 11 of [eden-phase-12c-control-plane.md](eden-phase-12c-control-plane.md), the v0 multi-experiment topology is:

- **One** task-store-server hosting ALL experiments (the wire's existing `experiments/{id}/...` path structure routes per-call).
- **One** control-plane service maintaining the deployment-level experiment registry + leases + deployment-scoped worker registry.
- **One or more** orchestrator replicas, each running the multi-experiment lease-driven loop (no `--experiment-id` flag; the replica acquires leases for whichever experiments the control-plane registers).
- **One web-ui** with the cross-experiment dashboard surfaced.
- **One shared Postgres** instance (two logical schemas: `eden` for task-store-server, `eden_control_plane` for the control plane — though the v0 reference uses the same instance for both; chapter 11 §3.4 Option A).
- **One forgejo** instance hosting **N** repos (one per experiment) — the existing setup-experiment shape already creates a per-experiment forgejo repo (`eden/${EDEN_EXPERIMENT_ID}.git`), so two experiments → two repos.
- **Per-experiment worker hosts** (ideator-host / executor-host / evaluator-host). The reference worker-host CLI binaries still take `--experiment-id` as a required arg (single-experiment-scoped). The v0 reference design (chapter 11 §1) does not collapse worker hosts across experiments — each registered experiment runs its own host trio bound to its forgejo repo + credentials. Two experiments → six host containers.

The smoke asserts the cross-experiment isolation invariants this topology promises:

- Task ids do not collide across experiments (chapter 4 §1).
- Event streams are disjoint per experiment (chapter 5 §2 per-experiment transactional invariant).
- The control-plane registry sees both experiments and both lease holders correctly (chapter 11 §2 + §4).
- The orchestrator's lease-handoff drill (kill the active replica, second replica acquires both leases) works end-to-end through the Compose stack.
- Drives at least 2 `variant.integrated` per experiment (small numbers; this is a smoke not a load test).

### 1.3 Why a smoke matters even though 12c shipped 246/246 conformance tests

Chapter 9 §6 pins the conformance harness to the chapter-7 binding — the suite spawns a single IUT and exercises it through its HTTP surface. 9 of the 37 `v1+multi-experiment` scenarios are documented `@pytest.mark.skip` for exactly this reason: they require the IUT bundled with an orchestrator and a task-store-server running alongside the control plane (event-log non-emission under decision-gating, two-service hand-off under state-sync, checkpoint-import-auto-register). The contracts those skipped scenarios test ARE covered in-process by wire-layer + integration tests; what they're NOT covered against is the **deployed compose substrate** — where the failure modes look like service-boundary races (chapter 11 §3.3 hand-off window), volume/credential isolation traps (per-experiment forgejo creds dirs), and orchestrator-loop concurrency bugs that only surface when the loop drives real worker hosts.

The "compose-smoke-multi-experiment" job is the substrate-level backstop for the 9 conformance skips. It's not a substitute for them (different IUT contract); it's the smoke that makes sure the deployed reference stack actually does what the conformance suite asserts the chapter-7 binding promises.

## 2. Decisions

These are the load-bearing design calls; §3 unpacks each.

1. **Substrate: Compose, not Helm.** Phase 13a Helm is still a draft plan; gating this backfill on it would defer indefinitely. Compose is the canonical v0 reference deployment, and Phase 13a (when it lands) inherits this smoke shape against Helm. Same posture #152 took for the Phase 12b checkpoint backfill.

2. **Control-plane as a first-class Compose service goes in [`compose.yaml`](../../reference/compose/compose.yaml), not the overlay.** The existing single-experiment smokes (`smoke.sh`, `smoke-subprocess.sh`, etc.) all use `--control-plane-url` unset → the orchestrator and web-ui fall back to single-experiment mode. Adding the control-plane service to the base compose doesn't change that posture: as long as the orchestrator and web-ui services don't get `--control-plane-url` threaded in, they ignore the control plane. The control-plane service is cheap (one FastAPI process + a Postgres schema); making it always-on means the existing smokes verify the control-plane container starts cleanly even when nothing talks to it, which catches the most common regression class (broken image build, missing env var, etc.).

   - **Alternative considered: control-plane only under an overlay.** The existing `compose.control-plane.yaml` is shaped this way (web-ui-only override). Rejected: layering an overlay every time we want the control plane available adds friction for operators wanting to use the cross-experiment dashboard ergonomically, and creates a second "is the control plane up?" failure mode in CI. The base compose adding one always-on FastAPI container is the cleaner posture; the existing `compose.control-plane.yaml` is **retired** (its only contents were the web-ui flag-passing, which moves into compose.yaml conditional on env, see Decision 3).

3. **Multi-experiment mode is opt-in via env-var gating, not a separate compose-file overlay.** Two related sub-decisions:
   - **`orchestrator` service's `--experiment-id` becomes optional via env-var.** Today compose.yaml hard-codes `--experiment-id ${EDEN_EXPERIMENT_ID:?}`. The orchestrator CLI already supports multi-experiment mode (no `--experiment-id`) when `--control-plane-url` is set. Change compose.yaml so the `--experiment-id` flag is threaded conditionally — when `EDEN_ORCHESTRATOR_MULTI_EXPERIMENT=1` is set in `.env`, the flag is omitted and `--control-plane-url` is set. When unset (default), behavior is unchanged (single-experiment, no control plane URL).
   - **`web-ui`'s `--control-plane-url` is threaded from env, defaulting empty.** The existing `compose.control-plane.yaml` overlay's only purpose is to add `--control-plane-url`; bake that conditionally into compose.yaml from `${EDEN_CONTROL_PLANE_URL:-}`. When empty, the flag is omitted via `${VAR:+--control-plane-url ${VAR}}` shell-substitution pattern (compose supports this).

   Together, **the existing 6 smokes need NO change** (they don't set `EDEN_ORCHESTRATOR_MULTI_EXPERIMENT` or `EDEN_CONTROL_PLANE_URL` in their generated `.env`, so the orchestrator runs single-experiment and the web-ui ignores the control-plane). The new multi-experiment smoke sets both. This satisfies the [CHANGELOG](../../CHANGELOG.md) note's "The existing 6 Compose smokes are unchanged in posture" pledge.

4. **The multi-experiment smoke runs TWO experiments end-to-end, not one.** A smoke that registers one experiment via the control plane is not meaningfully different from `smoke.sh` (which exercises a single experiment without a control plane). The substrate-level value of this job is exercising the multi-experiment topology: two registered experiments, two leases held simultaneously, cross-experiment isolation asserted via wire reads. The smoke MUST therefore set up two distinct experiments end-to-end.

5. **The two experiments share the SAME forgejo + postgres + task-store-server + control-plane + ONE multi-experiment orchestrator, but use DISTINCT per-experiment worker hosts.** Per §1.2: worker hosts are single-experiment-scoped in the v0 reference impl. Two experiments means two forgejo repos (existing setup-experiment shape supports this — each experiment-id maps to `eden/<id>.git`) and two sets of host containers (six total: `ideator-host-A`, `ideator-host-B`, `executor-host-A`, `executor-host-B`, `evaluator-host-A`, `evaluator-host-B`). The orchestrator runs in multi-experiment mode (no `--experiment-id`); it acquires both leases and drives both loops.

   - **Alternative considered: one set of worker hosts shared across experiments.** Rejected: the worker-host CLIs require `--experiment-id` and have per-experiment forgejo credentials + per-experiment substrate paths. Refactoring host CLIs to multi-experiment is a separate, much bigger lift that arguably belongs in a future phase; it is not required to expose multi-experiment ORCHESTRATION at the smoke level.

6. **setup-experiment.sh becomes idempotently re-runnable against the same data root for a different experiment-id.** Today, running setup-experiment a second time against a different `--experiment-id` clobbers the `.env` file with the new experiment's settings. For the multi-experiment smoke, we need either (a) two `.env` files merged, or (b) `setup-experiment` extended to support a "register-additional-experiment" mode.
   - **Decision: option (b) — add `--register-additional-experiment <id>` flag.** When passed, setup-experiment treats the existing `.env` as the BASELINE (postgres password, admin token, control-plane URL, etc. are reused as-is from the first invocation), provisions only the experiment-specific resources (forgejo repo + creds dir + data subdirs + bare-repo seed for that experiment), and appends per-experiment env vars under a namespaced prefix (`EDEN_EXPERIMENT_ID_2`, `EDEN_BASE_COMMIT_SHA_2`, etc.). The smoke script then renders the per-experiment host containers using those prefixed values via compose's variable substitution.
   - **Why not option (a) — merged .env files:** compose doesn't naturally support that; either we'd have a custom merge step or move to two compose projects sharing a network. Both add complexity orthogonal to the smoke's intent. Option (b) is bounded — setup-experiment grows one new code path + the env-namespacing convention is restricted to the new multi-experiment-overlay scope.

7. **The multi-experiment overlay is a new compose file: `compose.multi-experiment.yaml`.** It defines the second per-experiment host trio (`ideator-host-2`, `executor-host-2`, `evaluator-host-2`) plus any per-experiment-2 volumes; it does NOT redefine shared services (task-store-server, control-plane, orchestrator, postgres, forgejo, web-ui). Layered as `-f compose.yaml -f compose.multi-experiment.yaml` (mirrors `compose.multi-orchestrator.yaml`'s pattern).

8. **CI job follows the established not-required-then-bump posture.** Same as compose-smoke-multi-orchestrator (12a-2) and compose-smoke-checkpoint (#152): the new `compose-smoke-multi-experiment` job is added unrequired in the implementation PR; bumped to required-status after staying clean on main for ~2 weeks. Documented in the implementation PR description.

9. **Control-plane Postgres lives in the SAME postgres instance as the task-store-server in the Compose deployment.** The control-plane's `--store-url` can be `postgresql://...` against a separate database (logical schema separation per chapter 11 §3.4 Option A). The Compose deployment creates the `eden_control_plane` database alongside the existing `eden` database via a simple init step in the existing `postgres` service. No new postgres instance.

   - **Alternative: in-memory store for the control plane in Compose.** Rejected: the control-plane in-memory store is single-replica-by-construction and loses lease state on restart, which is fine for unit tests but inappropriate for the substrate smoke (the smoke asserts state-sync persistence across orchestrator restarts in the chaos drill).

## 3. Design

### 3.1 Compose-layer changes

#### 3.1.1 `control-plane` service added to [`compose.yaml`](../../reference/compose/compose.yaml)

A new service block alongside `task-store-server`:

```yaml
control-plane:
  image: eden-reference:dev
  build:
    context: ../..
    dockerfile: reference/compose/Dockerfile
  container_name: eden-control-plane
  restart: unless-stopped
  logging: *eden-logging
  depends_on:
    postgres:
      condition: service_healthy
    task-store-server:
      condition: service_healthy
  command:
    - python
    - -m
    - eden_control_plane_server
    - --store-url
    - postgresql://${POSTGRES_USER:-eden}:${POSTGRES_PASSWORD:?}@postgres:5432/${POSTGRES_DB_CONTROL_PLANE:-eden_control_plane}
    - --host
    - 0.0.0.0
    - --port
    - "8081"
    - --admin-token
    - ${EDEN_ADMIN_TOKEN:?}
    - --task-store-url
    - http://task-store-server:8080
    - --lease-duration-seconds
    - "${EDEN_LEASE_DURATION_SECONDS:-30}"
    - --state-sync-interval-seconds
    - "${EDEN_STATE_SYNC_INTERVAL_SECONDS:-30}"
    - --log-level
    - info
  ports:
    - "${CONTROL_PLANE_HOST_PORT:-8081}:8081"
  environment:
    EDEN_LOG_DIR: /var/lib/eden/logs
  volumes:
    - ${EDEN_EXPERIMENT_DATA_ROOT:?}/logs/control-plane:/var/lib/eden/logs
  healthcheck:
    test: ["CMD", "python3", "-c", "import urllib.request,sys; urllib.request.urlopen('http://localhost:8081/healthz', timeout=2)"]
    interval: 5s
    timeout: 3s
    retries: 10
    start_period: 10s
```

Notes:

- `--task-store-url` is always set → the chapter 11 §3 state-sync poller is always on. This is the production-shape posture; the poller is cheap and the single-experiment smokes don't care about its presence (no registered experiments → poller no-ops).
- The control-plane server already has a `/healthz` endpoint per its FastAPI app (used by the conformance fixture); add one if missing (small impl task — verify in [`reference/services/control-plane/src/eden_control_plane_server/app.py`](../../reference/services/control-plane/src/eden_control_plane_server/app.py)).
- The Postgres database name is namespaced via `POSTGRES_DB_CONTROL_PLANE` env var (default `eden_control_plane`); the postgres init step ensures it's created at first start (see §3.1.2).

#### 3.1.2 Postgres init: create `eden_control_plane` database

The existing `postgres` service uses the upstream `postgres:16.6-alpine` image which creates exactly one database (`$POSTGRES_DB`). To add a second database we use the upstream image's `/docker-entrypoint-initdb.d/` hook — a small `init-control-plane-db.sh` mounted into that directory:

```bash
#!/bin/sh
set -e
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE \"${POSTGRES_DB_CONTROL_PLANE:-eden_control_plane}\" OWNER \"$POSTGRES_USER\";"
```

Mounted via a new `postgres` volume:

```yaml
volumes:
  - ./init-control-plane-db.sh:/docker-entrypoint-initdb.d/01-control-plane-db.sh:ro
```

The hook runs only on a fresh Postgres data dir (the upstream image's standard behavior). For idempotency on re-runs, the script uses `CREATE DATABASE` without `IF NOT EXISTS` (Postgres doesn't support that syntax for databases); the smoke's substrate-wipe between runs (cleanup of `${EDEN_EXPERIMENT_DATA_ROOT}/postgres`) means each run starts fresh.

#### 3.1.3 Orchestrator + web-ui conditional flag wiring

Modify [`compose.yaml`](../../reference/compose/compose.yaml)'s `orchestrator` and `web-ui` service commands so the `--experiment-id` and `--control-plane-url` flags are gated on env vars. Compose's variable-substitution syntax supports the `${VAR:+...}` form which expands to the second value if VAR is set, else empty.

For the orchestrator:

```yaml
command:
  - python
  - -m
  - eden_orchestrator
  - --task-store-url
  - http://task-store-server:8080
  # When EDEN_ORCHESTRATOR_MULTI_EXPERIMENT=1, omit --experiment-id and set
  # --control-plane-url. When unset, behavior is unchanged.
  ...
```

Compose limitation: compose can't conditionally include / exclude an entry in a `command:` list based on an env var. Two workable shapes:

- **Shape A — entrypoint wrapper script.** A small `/usr/local/bin/orchestrator-entrypoint.sh` inside the runtime image reads `EDEN_ORCHESTRATOR_MULTI_EXPERIMENT`, `EDEN_EXPERIMENT_ID`, and `EDEN_CONTROL_PLANE_URL` from env, builds the `python -m eden_orchestrator …` command line, and `exec`s it. compose.yaml's `command:` becomes a single-element list invoking the wrapper. **Trade-off**: one new shell script in the runtime image; clear semantics.
- **Shape B — two compose-yaml service definitions, one per mode.** Define `orchestrator` (single-experiment, current shape) AND `orchestrator-multi` (multi-experiment, no `--experiment-id`, with `--control-plane-url`). The smoke selects via compose `--profile`. **Trade-off**: more compose-yaml surface, but no runtime-image change.

**Decision: Shape A (entrypoint wrapper).** Compose profiles work for one-or-the-other selection but the wrapper script is simpler at the call site and matches the existing pattern of `credential-helper.sh` already living in the runtime image. The wrapper is ~15 lines of bash and conforms to the bash 3.2 / no-bash-4-builtins discipline from AGENTS.md.

Same shape for the web-ui (conditional `--control-plane-url` + `--control-plane-admin-token`); same wrapper-script approach.

#### 3.1.4 Retire `compose.control-plane.yaml`

The current overlay file overrides the web-ui's `command:` only to add `--control-plane-url`. With §3.1.3 baking that conditionally into the base compose, the overlay's purpose is gone. Per AGENTS.md no-back-compat-shims posture (pre-external-user project), the file is **deleted**, not deprecated. The audit-of-substrate-rename discipline (AGENTS.md "Substrate migrations need a same-PR audit") applies: grep for references to `compose.control-plane.yaml` in docs, scripts, and CI; update each in the same PR.

#### 3.1.5 `compose.multi-experiment.yaml` — new overlay

```yaml
name: eden-reference

services:
  ideator-host-2:
    image: eden-reference:dev
    container_name: eden-ideator-host-2
    restart: unless-stopped
    depends_on:
      task-store-server:
        condition: service_healthy
      forgejo:
        condition: service_healthy
    command:
      - python
      - -m
      - eden_ideator_host
      - --task-store-url
      - http://task-store-server:8080
      - --experiment-id
      - ${EDEN_EXPERIMENT_ID_2:?EDEN_EXPERIMENT_ID_2 must be set (run setup-experiment --register-additional-experiment)}
      - --admin-token
      - ${EDEN_ADMIN_TOKEN:?}
      - --experiment-config
      - /etc/eden/experiment-config-2.yaml
      - --worker-id
      - ideator-host-2
      ...
    volumes:
      - ${EDEN_FORGEJO_CREDS_DIR_HOST_2:?}/credential-helper.sh:/etc/eden/credential-helper.sh:ro
      - ${EDEN_EXPERIMENT_DATA_ROOT}/exp-2-artifacts:/var/lib/eden/artifacts
      ...

  executor-host-2:
    # Same shape as ideator-host-2 but for execution.
    ...

  evaluator-host-2:
    # Same shape for evaluation.
    ...
```

Notes:

- Per-experiment forgejo creds dir, artifact dir, worker-host volumes all use the `_2` suffix on env vars; setup-experiment's `--register-additional-experiment` mode (§3.2) is what populates them.
- Worker-ids are deterministic (`ideator-host-2`, etc.) so the `_ensure_orchestrators_membership`-style bootstrap works idempotently.
- The host trio shares the same `task-store-server` healthcheck dependency as the experiment-1 trio; both trios register against the same task-store-server.

### 3.2 setup-experiment changes

Add a new `--register-additional-experiment <id>` flag to [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh). When passed:

1. **Preconditions.** An `.env` file from a prior setup-experiment run MUST exist (passed via `--env-file`); the script reads the baseline shared values (postgres password, admin token, forgejo internal token, control-plane URL, session secret, etc.) from it.
2. **Per-experiment provisioning** (mirrors the existing single-experiment flow but namespaced with `_2`):
   - Create the forgejo repo `eden/<id>.git` (existing forgejo API call, exp-2 id).
   - Generate per-experiment forgejo credentials (`EDEN_FORGEJO_PASSWORD_2`) and write the per-experiment creds dir + credential-helper.
   - Materialize the experiment-config YAML at `${COMPOSE_DIR}/experiment-config-2.yaml` (per-experiment baseline; the smoke uses the same fixture config for both experiments since the smoke's intent is cross-experiment isolation, not differentiated behavior — but the wrap supports a different config per experiment trivially).
   - Run `eden-repo-init` against `${EDEN_EXPERIMENT_DATA_ROOT}/exp-2-repo` to seed the bare repo for exp-2; capture `EDEN_BASE_COMMIT_SHA_2`.
   - Create the per-experiment substrate subdirs (`exp-2-artifacts`, `exp-2-orchestrator-repo`, `exp-2-web-ui-repo`, `exp-2-executor-repo`, `exp-2-evaluator-repo`, `exp-2-credentials`, `logs/ideator-host-2`, etc.) — these mirror the existing single-experiment dirs but with `exp-2-` prefixes.
3. **Control-plane registration.** POST `register_experiment` for the new id to the control-plane (the orchestrator's multi-experiment loop picks it up via the §3.2 acquisition thread once it's registered). Idempotent on existing record (chapter 11 §2 + Phase 12c round-6 codex fix: 200 on idempotent replay vs 201 on first register).
4. **Append to `.env`.** The script appends the `_2`-namespaced env vars to the existing `.env`. Existing baseline vars are untouched.

The `--register-additional-experiment` flag is intentionally suffixed `_2` rather than building a fully-generic N-experiment registry. For the smoke's needs, two experiments is enough; a future generalization to N can follow the same pattern with `_<N>` suffixes if needed.

### 3.3 The smoke script — `reference/compose/healthcheck/smoke-multi-experiment.sh`

Structure (mirrors smoke-checkpoint.sh / smoke-multi-orchestrator.sh patterns):

```text
Phase 0 — Preflight (docker / jq / curl / python3 available; docker compose v2)

Phase 1 — Provision both experiments
  setup-experiment.sh <config> --experiment-id exp-A --env-file $ENV --data-root $ROOT
  setup-experiment.sh <config> --register-additional-experiment exp-B \
      --env-file $ENV --data-root $ROOT

  # The smoke pins:
  #   EDEN_ORCHESTRATOR_MULTI_EXPERIMENT=1
  #   EDEN_CONTROL_PLANE_URL=http://control-plane:8081
  #   EDEN_IDEATION_POLICY_MAX_TOTAL=2 for both experiments
  #   EDEN_LEASE_DURATION_SECONDS=10 (faster for the chaos drill)
  #   EDEN_STATE_SYNC_INTERVAL_SECONDS=5

Phase 2 — Bring up the stack with multi-experiment overlay
  docker compose -f compose.yaml -f compose.multi-experiment.yaml \
      --env-file $ENV up -d --wait --wait-timeout 300

  # Assertions:
  #   - control-plane /healthz returns 200
  #   - control-plane /v0/control/experiments contains both ids
  #   - control-plane /v0/control/leases lists 2 active leases (one per
  #     experiment) held by the orchestrator worker_id within ~30s

Phase 3 — Drive both experiments to quiescence
  # Both experiments use max_total=2 → 2 integrated variants each. The
  # orchestrator's multi-experiment loop runs both lease loops; quiescence
  # exit fires when ALL held leases have drained.

  Wait for orchestrator container to exit 0 (timeout 300s).

Phase 4 — Cross-experiment isolation assertions
  curl -fsS .../experiments/exp-A/events | jq …
  curl -fsS .../experiments/exp-B/events | jq …

  Assert:
    - exp-A events count >= some floor (≥6 task.completed, ≥2 variant.integrated)
    - exp-B events count >= same floor
    - Per-experiment event task_ids are disjoint
      (no exp-A task_id appears in exp-B's event stream, vice versa)
    - exp-A variant_ids and exp-B variant_ids are disjoint
    - Each experiment's idea_ids are disjoint
    - control-plane registry shows both with last_known_state observed
      (running OR terminated depending on policy; the smoke's
      termination policy in the experiment-config drives terminated)

Phase 5 — Lease-handoff drill (chaos)
  # Bring up a second orchestrator replica via compose.multi-orchestrator.yaml
  # NO — that conflicts with this overlay. Instead: this overlay layers
  # a second orchestrator-multi instance directly.
  #
  # Decision: include `orchestrator-2` (multi-experiment shape) in
  # compose.multi-experiment.yaml itself, so the chaos drill works
  # without needing a third overlay file. Two replicas; chaos-kill
  # the lease holder; assert the other replica picks up its lease.

  docker rm -f eden-orchestrator        # current lease holder
  Wait up to lease_duration * 2 (= 20s) for orchestrator-2 to acquire
  both leases via control-plane /v0/control/leases.
  Assert: orchestrator-2 now holds both leases; experiment-A and
          experiment-B both continue to make progress (or are already
          quiesced).

Phase 6 — Final cross-experiment cardinality cross-check
  Re-fetch /v0/control/experiments; assert:
    - Both experiments still registered.
    - Both have last_known_state == "terminated" (the smoke's
      termination-policy drives this).
    - Neither leak across experiment boundaries.

PASS
```

Substrate-cleanup posture mirrors `smoke-checkpoint.sh`: the smoke's `cleanup()` trap runs `docker compose down -v`, removes the per-experiment forgejo creds dirs, and wipes the bind-mount data root via a sibling Alpine container (uid-mismatch dance).

### 3.4 Per-experiment env-var namespacing convention

The convention is:

- Baseline (shared across experiments): `EDEN_ADMIN_TOKEN`, `POSTGRES_PASSWORD`, `FORGEJO_INTERNAL_TOKEN`, `EDEN_SESSION_SECRET`, `EDEN_CONTROL_PLANE_URL`, etc. Set once by the first `setup-experiment` invocation.
- Per-experiment-1 (the default-shaped env vars, no suffix): `EDEN_EXPERIMENT_ID`, `EDEN_BASE_COMMIT_SHA`, `EDEN_FORGEJO_PASSWORD`, `EDEN_FORGEJO_CREDS_DIR_HOST`, `FORGEJO_REMOTE_URL`, `EDEN_ARTIFACT_URL`, etc.
- Per-experiment-2 (suffix `_2`): `EDEN_EXPERIMENT_ID_2`, `EDEN_BASE_COMMIT_SHA_2`, `EDEN_FORGEJO_PASSWORD_2`, `EDEN_FORGEJO_CREDS_DIR_HOST_2`, `FORGEJO_REMOTE_URL_2`, `EDEN_ARTIFACT_URL_2`, etc.

[`reference/compose/.env.example`](../../reference/compose/.env.example) is updated to enumerate both groups so operators can see the shape; the actual values are stamped by setup-experiment.

### 3.5 Documentation

- [`reference/compose/README.md`](../../reference/compose/README.md): a new "Multi-experiment mode" section explaining how to set up two experiments + bring up the multi-experiment overlay, with a pointer to the smoke as the canonical reference.
- [`docs/user-guide.md`](../../docs/user-guide.md): a one-paragraph aside under the "Running an experiment" section noting that multi-experiment deployment is available; pointer to the README.
- AGENTS.md's "Commands" table gains a new row for `bash reference/compose/healthcheck/smoke-multi-experiment.sh`.

## 4. Scope

### 4.1 In scope

**Spec / contracts:**

- **No spec changes.** Chapter 11 is shipped; this plan is observational only. The CHANGELOG (12c) already names this deferral.
- **No JSON Schema changes.** Same reason.
- **No Pydantic model changes.** Same reason.
- **No wire-binding changes.** Same reason.

**Code (reference impl):**

- Verify a `/healthz` endpoint exists on the control-plane server; add if missing. (Verify in [`reference/services/control-plane/src/eden_control_plane_server/app.py`](../../reference/services/control-plane/src/eden_control_plane_server/app.py); shape mirrors the web-ui's `/healthz`.)
- Orchestrator entrypoint wrapper script (~15 lines bash) under [`reference/compose/`](../../reference/compose/) (e.g. `orchestrator-entrypoint.sh`); web-ui entrypoint wrapper (~15 lines bash).
- Modify the runtime image's Dockerfile so the entrypoint wrappers are installed (small COPY + chmod).

**Compose:**

- Add `control-plane` service to [`compose.yaml`](../../reference/compose/compose.yaml).
- Add `init-control-plane-db.sh` postgres-init hook + mount.
- Modify `orchestrator` and `web-ui` services in compose.yaml to invoke the entrypoint wrappers.
- Add `POSTGRES_DB_CONTROL_PLANE`, `CONTROL_PLANE_HOST_PORT`, `EDEN_ORCHESTRATOR_MULTI_EXPERIMENT`, `EDEN_CONTROL_PLANE_URL`, `EDEN_LEASE_DURATION_SECONDS`, `EDEN_STATE_SYNC_INTERVAL_SECONDS`, `EDEN_STATE_SYNC_FAILURE_THRESHOLD` to [`.env.example`](../../reference/compose/.env.example).
- New `compose.multi-experiment.yaml` overlay.
- Delete `compose.control-plane.yaml`.

**setup-experiment.sh:**

- Add `--register-additional-experiment <id>` mode with the §3.2 semantics. Existing single-experiment flow unchanged.
- Per-experiment-2 env-var namespacing convention documented in the script's help text.

**Smoke + CI:**

- New [`reference/compose/healthcheck/smoke-multi-experiment.sh`](../../reference/compose/healthcheck/smoke-multi-experiment.sh).
- New `compose-smoke-multi-experiment` job in [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) (20-minute timeout, mirrors compose-smoke-multi-orchestrator + compose-smoke-checkpoint shape, not branch-protected initially).

**Docs:**

- AGENTS.md "Commands" table — new row.
- [`reference/compose/README.md`](../../reference/compose/README.md) — new section.
- [`docs/user-guide.md`](../../docs/user-guide.md) — short aside.
- `CHANGELOG.md [Unreleased]` — chunk-completion entry on impl-merge, with explicit "closes #147".
- [`docs/roadmap.md`](../../docs/roadmap.md) — planless-chunk one-line status flip on impl-merge (this is a backfill issue, not a roadmap chunk).

### 4.2 Out of scope (followups; file as issues if not already)

- **Helm-chart multi-experiment substrate.** Folded into the existing Phase 13a plan ([`docs/plans/eden-phase-13a-helm-base-chart.md`](eden-phase-13a-helm-base-chart.md)) when that lands; this plan is Compose-only. No new issue needed — Phase 13a's existing scope covers it.
- **N-experiment generalization beyond N=2.** The `_2` suffix convention is bounded; a generic N-experiment registry would generalize to `_N` suffixes. Out of scope for the smoke's needs.
- **Multi-experiment load testing.** Per the issue: this is a smoke, not a stress test.
- **Cross-experiment scheduling intelligence** (e.g. lease-stealing for fair work distribution). Out of scope; chapter 11 §3.9 alternatives-considered documents this as a future v1 amendment.
- **Worker-host multi-experiment refactor.** Per Decision 5, worker hosts stay single-experiment-scoped; a future refactor that lets one host trio serve multiple experiments would simplify deployment but is a different concern.
- **`eden_control_plane` admin-pages in the web-ui.** Phase 12c shipped the read-only `/admin/experiments/` dashboard; a parallel `/admin/control/workers/` + `/admin/control/groups/` admin surface for the deployment-scoped registry is a follow-up (already deferred per the 12c CHANGELOG entry "Deployment-scoped worker/group registry admin pages not shipped").

### 4.3 Non-goals

- Replacing the existing 6 Compose smokes. They remain single-experiment + no-control-plane, exactly as 12c promised. This adds a 7th smoke.
- Changing the orchestrator's lease/decision behavior. All lease semantics + chapter 11 conformance is shipped; the smoke OBSERVES, it doesn't extend.

## 5. Files to touch

| File | Change |
|---|---|
| [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) | Add `control-plane` service block (§3.1.1). Modify `orchestrator` and `web-ui` services to invoke entrypoint wrappers (§3.1.3). Add postgres-init script mount on the `postgres` service (§3.1.2). |
| `reference/compose/init-control-plane-db.sh` (new) | Postgres init hook creating `eden_control_plane` database (§3.1.2). |
| `reference/compose/orchestrator-entrypoint.sh` (new) | Bash wrapper that decides single- vs multi-experiment invocation from env (§3.1.3). |
| `reference/compose/web-ui-entrypoint.sh` (new) | Bash wrapper that conditionally adds `--control-plane-url` (§3.1.3). |
| [`reference/compose/Dockerfile`](../../reference/compose/Dockerfile) | `COPY` + `chmod +x` the two entrypoint scripts into the runtime image. |
| [`reference/compose/compose.control-plane.yaml`](../../reference/compose/compose.control-plane.yaml) | **Delete** (§3.1.4). |
| `reference/compose/compose.multi-experiment.yaml` (new) | Second host trio + second orchestrator-multi-instance, layered as `-f compose.yaml -f compose.multi-experiment.yaml` (§3.1.5). |
| [`reference/compose/.env.example`](../../reference/compose/.env.example) | Document the per-experiment env-var namespacing convention (§3.4). |
| [`reference/scripts/setup-experiment/setup-experiment.sh`](../../reference/scripts/setup-experiment/setup-experiment.sh) | Add `--register-additional-experiment <id>` mode (§3.2). |
| `reference/compose/healthcheck/smoke-multi-experiment.sh` (new) | The smoke (§3.3). |
| [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) | New `compose-smoke-multi-experiment` job mirroring `compose-smoke-checkpoint` shape. |
| [`reference/services/control-plane/src/eden_control_plane_server/app.py`](../../reference/services/control-plane/src/eden_control_plane_server/app.py) | Verify `/healthz` endpoint exists; add if missing. |
| [`AGENTS.md`](../../AGENTS.md) | New row in "Commands" table for the smoke. |
| [`reference/compose/README.md`](../../reference/compose/README.md) | New "Multi-experiment mode" section. |
| [`docs/user-guide.md`](../../docs/user-guide.md) | Short aside under "Running an experiment". |
| [`CHANGELOG.md`](../../CHANGELOG.md) | `[Unreleased]` entry on impl-merge. |
| [`docs/roadmap.md`](../../docs/roadmap.md) | Planless-chunk status flip on impl-merge. |

## 6. Test design

This is a substrate-level smoke; the assertions ARE the test. There are no new unit tests, no new wire tests, no new conformance scenarios (all of those shipped with 12c). Verification gates:

### 6.1 Smoke-level assertions (per §3.3 above)

- **Stack-startup**: control-plane `/healthz` 200; control-plane lists 2 registered experiments; 2 active leases held by the orchestrator within ~30s.
- **Cross-experiment isolation**: exp-A and exp-B event streams disjoint by task_id; variant_ids disjoint; idea_ids disjoint.
- **Per-experiment progress**: each experiment reaches `≥2 variant.integrated` events, `≥6 task.completed` events.
- **Control-plane state-sync**: each experiment's `last_known_state` in `read_experiment_metadata` converges to `"terminated"` after the in-experiment policy fires (smoke configures `max_variants_policy(2)` for both).
- **Chaos drill** (Phase 5): killing the lease-holding orchestrator → second orchestrator acquires both leases within `lease_duration * 2`; experiments still complete.

### 6.2 Local-repro discipline

Per AGENTS.md "Local repro beats log-tail reading", the smoke MUST be runnable locally on macOS bash 3.2 (no bash-4 builtins; no `mapfile` / `readarray` / associative arrays). Validation gate before merge: the implementing operator runs the smoke locally end-to-end at least twice (one cold-start, one against a leftover data-root to verify cleanup posture works).

### 6.3 CI verification gates

- `bash reference/compose/healthcheck/smoke-multi-experiment.sh` passes in the new CI job.
- All existing 6 compose smokes continue to pass (regression: the compose.yaml changes in §3.1 are conditional on env vars not set by the existing smokes).
- `uv run pytest -q` passes (regression: the entrypoint-wrapper scripts and setup-experiment changes don't break unit tests).
- `python3 scripts/check-rename-discipline.py` clean.
- `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` clean.
- Manual UI smoke: spin up the stack with the multi-experiment overlay; verify `/admin/experiments/` shows both experiments; switch between them via the dashboard's select form.

## 7. Chunked execution plan

The work is bounded enough to land as ONE impl PR after this plan PR merges, but it sequences naturally into three commits within that PR so a reviewer can isolate failures.

**Wave 1 — Control-plane as first-class Compose service** (covers Decisions 2, 3, 9 + §3.1.1, §3.1.2, §3.1.3, §3.1.4):

- Add `control-plane` service to compose.yaml.
- Add postgres-init hook.
- Add the two entrypoint wrappers + Dockerfile changes.
- Modify orchestrator + web-ui service definitions.
- Delete `compose.control-plane.yaml` + grep audit per AGENTS.md substrate-migration discipline.
- `.env.example` updates for the base shared baseline.
- Verify control-plane `/healthz` endpoint (add if missing).
- **Validation gate**: existing 6 smokes (`smoke.sh`, `smoke-subprocess.sh`, `smoke-subprocess-docker.sh`, `smoke-manual-mode.sh`, `smoke-multi-orchestrator.sh`, `smoke-checkpoint.sh`, `e2e.sh`) all pass unchanged.

**Wave 2 — Multi-experiment substrate + setup-experiment ergonomics** (covers Decisions 4, 5, 6, 7 + §3.1.5, §3.2, §3.4):

- New `compose.multi-experiment.yaml` overlay.
- `setup-experiment.sh --register-additional-experiment <id>` flag.
- `.env.example` updates for the per-experiment-2 namespaced vars.
- **Validation gate**: `setup-experiment.sh <config> --experiment-id exp-A && setup-experiment.sh <config> --register-additional-experiment exp-B --env-file <same>` produces an `.env` with both groups of vars; `docker compose -f compose.yaml -f compose.multi-experiment.yaml up -d --wait` brings up the full multi-experiment stack.

**Wave 3 — Smoke + CI + docs** (covers Decision 8 + §3.3 + §3.5):

- New `smoke-multi-experiment.sh`.
- New `compose-smoke-multi-experiment` CI job.
- AGENTS.md / README / user-guide updates.
- CHANGELOG `[Unreleased]` entry referencing #147.
- `docs/roadmap.md` planless-chunk status flip.
- **Validation gate**: the smoke passes in CI; the existing 6 smokes all stay green; all pre-push commands from AGENTS.md "Commands" pass.

Each wave's validation gate is the "go / no-go" for the next wave. If wave 1 breaks any existing smoke, that's the signal to course-correct before wave 2 (e.g. the env-var-gated `--experiment-id` substitution may need a different shape than the entrypoint wrapper).

## 8. Risks

1. **The control-plane container always-running posture breaks an existing smoke.** Wave 1's validation gate is the backstop. The risk is real because the existing 6 smokes don't currently start a control-plane; adding one means the smoke's bring-up step has to wait for it. Mitigation: control-plane's `depends_on` is `postgres` + `task-store-server` (both healthchecked), not the worker hosts; once it's healthy the worker hosts can come up in parallel.
2. **Postgres init hook fires only on fresh data dirs.** Per upstream Postgres image. The smoke's substrate-wipe between runs (cleanup of `${EDEN_EXPERIMENT_DATA_ROOT}/postgres`) handles this. The risk is for operators running setup-experiment against an existing data-root (post-upgrade): the `eden_control_plane` database won't exist, and the control-plane container will fail to start. Mitigation: setup-experiment's flow already tears down volumes on re-run (the existing `cleanup()` of the smokes already wipes postgres); for operators, document the upgrade path in `reference/compose/README.md` (manual `psql` to create the database, or `docker compose down -v` + re-run setup-experiment).
3. **Compose's `${VAR:+...}` substitution doesn't work inside `command:` lists.** Verify before committing to Shape A (entrypoint wrapper) vs Shape B (two service definitions). The decision is Shape A precisely because compose's flag-omission semantics are awkward in list-style command args.
4. **The chaos drill flake risk.** Lease handoff is bounded by `lease_duration * 2` (20s with the smoke's `EDEN_LEASE_DURATION_SECONDS=10`), but the orchestrator's acquisition thread polls per `poll_interval` (default 1s in the compose config) — so the worst-case detection window is ~22s. CI timeout is 300s on bring-up + 240s on quiescence; the chaos drill adds another ~30s. Total smoke runtime ≤ 10 min on the GitHub Actions runner. Mitigation: explicit `deadline = $((SECONDS + 60))` on the lease-acquisition assertion and `docker compose logs --tail 60` dump on failure (mirrors the existing smokes' diagnostic posture).
5. **GitHub Actions runner resource pressure (six host containers + control-plane + multi-orchestrator + postgres + forgejo = ~10 containers).** Mitigation: cap `EDEN_IDEATION_POLICY_MAX_TOTAL=2` for both experiments, run the scripted reference ideator/executor/evaluator (not the LLM ones), and rely on the 20-minute timeout. If memory pressure causes flakes, fall back to running the chaos drill in a separate CI job (split the smoke into base + chaos; base goes required first).
6. **Audit-of-substrate-rename trap.** AGENTS.md "Substrate migrations need a same-PR audit" applies. The compose.control-plane.yaml deletion is the main concrete reference to audit. Grep checklist (run in the impl PR):
   - `grep -rn 'compose.control-plane' .` — must return zero hits after the wave-1 commit lands.
   - `grep -rn 'EDEN_CONTROL_PLANE_URL' .` — must surface only documented call-sites (web-ui CLI + orchestrator CLI + the new entrypoint wrappers + .env.example + this plan + the CHANGELOG entry).
   - `grep -rn 'control-plane' reference/compose/` — must show the new compose.yaml block + the new entrypoint wrappers + nothing else.
7. **Worker-host conflict on shared substrate paths.** The exp-2 host containers' substrate paths (`exp-2-artifacts`, etc.) are distinct from the default-shape exp-1 paths by construction (suffix `_2`). The risk is that a shared mount target inside the container (e.g. `/var/lib/eden/artifacts`) collides if both trios mount different host paths to the SAME container target — which they do, but the trios are different containers so this is fine. Mitigation: documented in the compose.multi-experiment.yaml's per-service `volumes:` blocks.
8. **The `--register-additional-experiment` flag interacts poorly with checkpoint-import auto-register.** 12c's checkpoint-import endpoint auto-registers the imported experiment with the control plane (Decision 9 of the 12c plan). Operator workflow: import a checkpoint as experiment B; then run `setup-experiment.sh --register-additional-experiment B` against the existing baseline. The setup-experiment flow's control-plane registration is idempotent (chapter 11 §2 / 12c round-6 fix: 200 on idempotent replay), so this is safe — but the smoke doesn't test it. Mitigation: out-of-scope for this plan; flag as a followup if needed.
9. **EnvVar `EDEN_ADMIN_TOKEN` reuse across both experiments.** Both forgejo repos use the same `EDEN_ADMIN_TOKEN` (deployment-scoped, not per-experiment). This is the correct posture — the chapter 11 §6 deployment-scoped worker registry uses the admin token, NOT per-experiment tokens. The risk is conceptual confusion (operators might expect per-experiment admin tokens); mitigation is the `.env.example` documentation explicitly calling out which vars are deployment-scoped vs experiment-scoped.

## 9. Conformance impact

**None at the chapter-7 binding level.** Phase 12c already shipped 28 conformance scenarios for `v1+multi-experiment` with 9 documented skips per chapter 9 §6 (the suite is bound to a single IUT). This plan's smoke covers the substrate-level half of those 9 skips (running orchestrator + task-store-server + control-plane together exercises the contracts the chapter-7 IUT can't drive alone), but the conformance suite itself is untouched.

The smoke's assertions DO mirror the contracts the 9 skipped conformance scenarios test:

| Skipped conformance scenario (chapter 9 §6) | Smoke phase that exercises the same contract |
|---|---|
| `Lease decision gating` event-log non-emission | Phase 4 cross-experiment isolation (task_id disjointness across event streams) |
| `Multi-experiment dispatch` event disjointness | Phase 4 + Phase 6 |
| `State synchronization` two-service hand-off | Phase 2 + Phase 6 (last_known_state convergence) |
| `Checkpoint import auto-register` | Not exercised by this smoke; remains a future smoke (could combine with smoke-checkpoint if operator demand) |
| `Lease-ownership authority` admin-token-driven | Phase 2 control-plane wire reads |
| Lease handoff under failed-replica | Phase 5 chaos drill |

These are observational; this plan does not edit chapter 9 §6 nor any conformance scenario file. The mapping above is documentation only.

## 10. Migration / cleanup

Per AGENTS.md "No backwards-compatibility shims in greenfield / pre-external-user projects":

- **`compose.control-plane.yaml` is deleted, not deprecated.** Its only contents were web-ui flag-passing that moves into compose.yaml.
- **The existing 6 smokes are unchanged.** No back-compat shim needed — the smokes don't set `EDEN_ORCHESTRATOR_MULTI_EXPERIMENT` or `EDEN_CONTROL_PLANE_URL`, so they hit the unchanged-behavior branch of the entrypoint wrappers.
- **No version-flag-gated behavior.** The control-plane is always-on in the compose stack post-merge; orchestrator + web-ui still treat the URL as opt-in via env-var, no v0/v1 split.

The audit-of-substrate-rename discipline (per AGENTS.md "Substrate migrations need a same-PR audit") is gated on the wave-1 commit; risk #6 enumerates the grep checklist.

## 11. Sequencing

This plan PR is followed by ONE impl PR with three sequential commits matching §7's three waves. The impl PR uses `/codex-review` per AGENTS.md (3-5 rounds expected; the multi-service interaction surface and the substrate-rename audit are the main correctness concerns).

After impl-merge:

- CHANGELOG.md `[Unreleased]` entry is appended (per AGENTS.md "Recording chunk completions" planless-chunk shape: roadmap one-liner points at the merged PR).
- `docs/roadmap.md` planless-chunk status flip: `- [#147](https://github.com/ealt/eden/pull/<N>) — Backfill: compose-smoke-multi-experiment CI job (Phase 12c deferral) — **shipped <YYYY-MM-DD>** (see [CHANGELOG](../CHANGELOG.md))`.
- The `compose-smoke-multi-experiment` CI job is added unrequired; bumped to required-status ~2 weeks after the impl PR merges and stays clean on main.

## 12. Estimated effort

- **Wave 1** (control-plane as Compose service + entrypoint wrappers + retire overlay): ~1.5 days. Bulk of the trickiness is the compose-yaml conditional-flag wiring through the wrappers; the postgres-init hook is mechanical.
- **Wave 2** (multi-experiment overlay + setup-experiment ergonomics): ~1.5 days. The `--register-additional-experiment` mode is a focused extension; the multi-experiment overlay is structurally similar to the existing `compose.multi-orchestrator.yaml`.
- **Wave 3** (smoke + CI + docs): ~1 day. The smoke is bigger than the existing smokes (six containers + lease assertions + chaos drill) but the patterns are established.
- **Codex review + iteration**: ~1 day.

**Realistic total: ~5 working days** of focused work. Comparable in size to the chunk-10c original Compose plumbing.

## 13. Why this is one plan and not three

The three waves are sequential but bounded; splitting into three plan-stage PRs would over-process the work. The control-plane-as-Compose-service piece (wave 1) is the load-bearing concern that interacts with every existing smoke; coupling it with the multi-experiment overlay + smoke in one impl PR means a single codex-review pass + a single grep-audit + a single CI integration. Three plan-stage PRs would each repeat the substrate-audit discipline; doing it once at impl time is cheaper.

If during impl the work blows up beyond the §12 estimate (e.g. wave 1 surfaces a runtime-image change that needs its own review, or the substrate-rename audit finds more buried references than expected), the right shape is to split wave 1 into its own impl PR + land it first, then sequence wave 2 + wave 3 separately. The plan stays the same; only the merge cadence changes.
