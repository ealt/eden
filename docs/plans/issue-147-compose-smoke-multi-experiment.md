# Issue #147 — Compose-smoke-multi-experiment CI job (Phase 12c backfill)

**Status.** Re-scoped (impl) — see §0.

**Predecessors.** Phase 12c (control plane) merged ([CHANGELOG](../../CHANGELOG.md) §"Phase 12c"); chapter 11 normative surface + `eden-control-plane` package + `reference/services/control-plane/` reference service + orchestrator `LeaseManager` + web-ui `/admin/experiments/` dashboard are all shipped. Reference impl is `v1+roles+orchestrator-substrate+lifecycle+checkpoints+multi-experiment` conformant: 246/246 conformance scenarios pass at the chapter-7 binding level. What 12c deferred was the **deployment-substrate** integration — `control-plane` is not yet a first-class Compose service, and there is no end-to-end multi-experiment smoke. This plan backfills both.

**Issue.** [eden#147](https://github.com/ealt/eden/issues/147). The deferral note at [`CHANGELOG.md`](../../CHANGELOG.md):

> *Compose smoke `compose-smoke-multi-experiment`* deferred: the chapter 11 surface is fully exercised by unit + wire + conformance tests. The existing 6 Compose smokes are unchanged in posture (when `--control-plane-url` is unset, behavior is unchanged) and continue to pass. A control-plane Compose service + multi-experiment smoke lands in a follow-up substrate chunk (Phase 13a Helm path is the natural home).

**Substrate choice — Compose (not Helm).** The CHANGELOG note left the substrate open ("Phase 13a Helm path is the natural home"). Phase 13a is still a draft plan ([`docs/plans/eden-phase-13a-helm-base-chart.md`](eden-phase-13a-helm-base-chart.md) §Status); shipping a multi-experiment smoke today should not block on 13a. The Compose stack is the canonical reference deployment for v0; this chunk fills the multi-experiment gap there, and 13a (when it lands) inherits the same smoke shape against the Helm substrate. This is the same posture #152 (compose-smoke-checkpoint) took for the Phase 12b checkpoint backfill — Compose first, Helm follows.

**Naming.** Pre-draft check against [`docs/glossary.md`](../glossary.md) and AGENTS.md "Naming discipline":

- "Multi-experiment" is the canonical noun for the deployment-level posture where one task-store-server + one control-plane + one (or more) orchestrator replicas host more than one registered experiment. No identifier renames; everything threaded through this plan reuses chapter 11's vocabulary (`register_experiment`, `acquire_lease`, `last_known_state`, `holder_instance`, …).
- "Cross-experiment isolation" is the smoke's load-bearing assertion shape — no task-id / event-stream / variant-id leakage between two registered experiments sharing the deployment substrate. Not a new spec term; observational only.
- The new smoke script and CI job follow the existing naming convention: `smoke-multi-experiment.sh` (parallel to `smoke.sh` / `smoke-subprocess.sh` / `smoke-multi-orchestrator.sh` / `smoke-checkpoint.sh`) and `compose-smoke-multi-experiment` (parallel to `compose-smoke-multi-orchestrator` / `compose-smoke-checkpoint`).

## 0. Re-scope (2026-05-31, operator-authorized) — THIS GOVERNS

During impl, a read of the focal code paths surfaced a structural blocker the draft plan did not anticipate: **the reference implementation cannot host more than one experiment per deployment substrate.** Three independent sites enforce single-experiment hosting:

1. **Task-store-server is single-experiment-bound.** `build_store(...)` ([`reference/services/task-store-server/src/eden_task_store_server/app.py`](../../reference/services/task-store-server/src/eden_task_store_server/app.py)) constructs the `Store` with one fixed `experiment_id`, and the wire layer rejects any other path id: [`reference/packages/eden-wire/src/eden_wire/_dependencies.py:73`](../../reference/packages/eden-wire/src/eden_wire/_dependencies.py) → `if path_exp != deps.store.experiment_id: raise ExperimentIdMismatch(...)`. No multi-experiment `Store` class exists in `eden-storage`.
2. **The orchestrator multi-experiment loop targets a single task-store URL for all experiments.** [`reference/services/orchestrator/src/eden_orchestrator/multi_loop.py:255-302`](../../reference/services/orchestrator/src/eden_orchestrator/multi_loop.py) builds `StoreClient(task_store_url, experiment_id)` per experiment against the same CLI-supplied URL — no `experiment_id → endpoint` mapping.
3. **The orchestrator integrator is one shared bare repo / forgejo remote deployment-wide.** [`reference/services/orchestrator/src/eden_orchestrator/cli.py:633-657`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py) documents the v0 design: "one task-store-server (and one canonical bare repo) deployment-wide."

12c's multi-experiment surface was validated only against fake stores (`test_multi_loop_unit.py`) + the conformance suite's single-IUT chapter-7 binding (the 9 documented skips are precisely the ones that need >1 hosted experiment). The deployed reference stack has never hosted two experiments — and as written, it cannot.

**Decision (operator-authorized 2026-05-31): re-scope #147 to what the reference impl actually supports.** This plan now delivers:

- **The control-plane as a first-class Compose service** (the genuinely-new, genuinely-shippable 12c substrate piece) — §3.1.1–§3.1.4 below are retained.
- **A lease-handoff chaos smoke** against the deployed stack: ONE registered experiment, TWO orchestrator replicas contending for its single lease (multi-experiment / lease-driven mode), with the chaos drill killing the lease holder and asserting the standby replica picks it up cleanly and the experiment still completes. This exercises the chapter-11 control-plane + lease lifecycle end-to-end on the real Compose substrate.

**Deferred to [#254](https://github.com/ealt/eden/issues/254) (multi-experiment task-store-server hosting — the prereq):** the cross-experiment-isolation smoke (two experiments end-to-end; disjoint task-id / variant-id / idea-id / event streams). The following draft-plan content is **SUPERSEDED** by this re-scope and folded into #254:

- **Decision 4** (two experiments end-to-end) and **Decision 5** (per-experiment worker host trios + per-experiment forgejo repos) — see the rewritten Decisions below.
- **§3.1.5** (`compose.multi-experiment.yaml` second host trio) — replaced by a second *orchestrator replica* in lease mode, §3.1.5′.
- **§3.2** (`setup-experiment --register-additional-experiment`) — not needed; a single experiment is registered with the control plane (§3.2′).
- **§3.4** (per-experiment `_2` env-var namespacing) — not needed.
- The two-experiment portions of **§3.3** (smoke phases 4/6 cross-experiment isolation) — replaced by the lease-handoff smoke design, §3.3′.

**Impl refinement (no entrypoint wrappers).** The draft plan's §3.1.3 Shape A used a bash entrypoint wrapper to omit `--experiment-id` in multi-experiment mode. That is unnecessary: the orchestrator CLI selects mode **solely** on `--control-plane-url` being set ([`cli.py:359`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py) `if args.control_plane_url is not None`), and `--experiment-id` is merely a logging label in multi mode. So the impl instead adds an **env fallback** for `--control-plane-url` to the orchestrator + web-ui CLIs (mirroring the existing `EDEN_CONTROL_PLANE_ADMIN_TOKEN` fallback): an empty `${EDEN_CONTROL_PLANE_URL:-}` → single-experiment mode (unchanged); a non-empty value → lease-driven mode. No wrapper scripts, no Dockerfile change. `--experiment-id` and `--lease-duration-seconds` stay as always-present flags (harmless in the mode that ignores them). This supersedes §3.1.3 Shape A and the `orchestrator-entrypoint.sh` / `web-ui-entrypoint.sh` / Dockerfile-COPY items in §4.1/§5.

Where the rest of this document (written pre-re-scope) describes "two experiments" / "cross-experiment isolation," read it as historical context superseded by §0 + the primed (′) sections. §0 governs on any conflict.

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

4. **[RE-SCOPED — see §0] The smoke runs ONE registered experiment with TWO orchestrator replicas contending for its lease.** The draft plan ran two experiments end-to-end; that is unbuildable (§0) and deferred to [#254](https://github.com/ealt/eden/issues/254). The substrate-level value retained here is exercising the chapter-11 control-plane + lease lifecycle on the real Compose stack: one experiment registered with the control plane, two orchestrator replicas in lease-driven (multi-experiment) mode contending for its single lease, with a chaos drill that kills the lease holder and asserts clean hand-off. This is meaningfully different from `smoke.sh` (which runs a single orchestrator with no control plane and no lease machinery).

5. **[RE-SCOPED — see §0] One experiment, one forgejo repo, one task-store-server, one control-plane, the existing single worker-host trio, and TWO orchestrator replicas in lease mode.** The per-experiment worker-host trios + per-experiment forgejo repos from the draft plan are deferred to #254. The two orchestrator replicas (`orchestrator`, `orchestrator-2`) both run with `--control-plane-url` set and no `--experiment-id` (lease-driven mode via the §3.1.3 entrypoint wrapper); they self-register deployment-scoped credentials, join the `orchestrators` group, and contend for the single experiment's lease. Exactly one holds it at any instant; the standby idles. Each replica has its own bare-clone + credentials volumes (mirrors `compose.multi-orchestrator.yaml`).

6. **[RE-SCOPED — see §0] No `setup-experiment --register-additional-experiment`.** A single experiment is provisioned by the normal `setup-experiment.sh` flow; the smoke then registers that one experiment with the control plane via an admin-authenticated `POST /v0/control/experiments` (§3.2′). The `_2`-namespaced env convention from the draft plan is not needed.

7. **[RE-SCOPED — see §0] The overlay (`compose.multi-experiment.yaml`) adds a second orchestrator replica in lease mode**, not a second host trio. It does NOT redefine shared services. Layered as `-f compose.yaml -f compose.multi-experiment.yaml` (mirrors `compose.multi-orchestrator.yaml`). See §3.1.5′.

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

#### 3.1.5′ `compose.multi-experiment.yaml` — RE-SCOPED overlay (second orchestrator replica in lease mode)

Per §0, the overlay adds a SECOND orchestrator replica (`orchestrator-2`) in lease-driven mode, NOT a second host trio. Structurally it mirrors [`compose.multi-orchestrator.yaml`](../../reference/compose/compose.multi-orchestrator.yaml)'s `orchestrator-2` (its own bare-clone + credentials volumes, worker_id `orchestrator-2`), with two additions: it passes `--lease-duration-seconds ${EDEN_LEASE_DURATION_SECONDS:-30}` and sets `EDEN_CONTROL_PLANE_URL: ${EDEN_CONTROL_PLANE_URL:-}` in its `environment:` (the env-fallback that flips it into lease mode — see the §0 impl refinement). The command stays a plain `python -m eden_orchestrator …` list (no wrapper). It `depends_on` `control-plane: service_healthy` in addition to `task-store-server`. The base-compose `orchestrator` flips to lease mode the same way (it carries the same `EDEN_CONTROL_PLANE_URL` env), so both replicas contend for the one experiment's lease when the smoke sets the env var. Per-replica volumes:

```yaml
volumes:
  eden-orchestrator-2-repo:
  eden-orchestrator-2-credentials:
```

### 3.2′ setup-experiment + control-plane registration (RE-SCOPED — see §0)

No `setup-experiment` change is needed. The normal `setup-experiment.sh <config>` flow provisions the single experiment (forgejo repo, creds, seed, `.env`). The smoke then registers that one experiment with the control plane via an admin-authenticated wire call (issued from inside the control-plane container so no host curl/port-guessing is needed, mirroring setup-experiment's `bootstrap_curl`):

```text
POST http://control-plane:8081/v0/control/experiments
  Authorization: Bearer admin:${EDEN_ADMIN_TOKEN}
  {"experiment_id": "${EDEN_EXPERIMENT_ID}", "config_uri": "file:///etc/eden/experiment-config.yaml"}
  → accept 201 (first register) or 200 (idempotent replay; chapter 11 §2 / 12c round-6).
```

Both orchestrator replicas' multi-experiment loops then observe the registered experiment via `manager.refresh()` and contend for its lease. `config_uri` is informational here — the orchestrator reads ideation/termination policy from its `--experiment-config` CLI flag, and the control-plane state-sync poller reads `experiment.state` from `--task-store-url`, not from `config_uri`.

The numbered `--register-additional-experiment` steps below are SUPERSEDED by §0 and folded into [#254](https://github.com/ealt/eden/issues/254); retained as historical context only.

### 3.2 setup-experiment changes [SUPERSEDED — see §3.2′ + §0]

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

### 3.3′ The smoke script — `reference/compose/healthcheck/smoke-multi-experiment.sh` (RE-SCOPED, lease-handoff — see §0)

ONE registered experiment + TWO orchestrator replicas contending for its lease. Structure mirrors `smoke-checkpoint.sh` / `smoke-multi-orchestrator.sh`:

```text
Phase 0 — Preflight (docker / jq / curl / python3 available; docker compose v2).
          Volume cleanup before run (AGENTS.md: rotate-password trap).

Phase 1 — Provision the single experiment + pin lease-mode env.
  setup-experiment.sh <config> --experiment-id <id> --env-file $ENV \
      --data-root $(mktemp -d)   # per-run data root → no rotate-password trap
  # The smoke rewrites EDEN_CONTROL_PLANE_URL (setup wrote it empty) and
  # appends the lease knobs to $ENV (it does NOT hand-edit baseline secrets):
  #   EDEN_CONTROL_PLANE_URL=http://control-plane:8081   (flips lease mode on)
  #   EDEN_LEASE_DURATION_SECONDS=10                      (fast hand-off drill)
  #   EDEN_STATE_SYNC_INTERVAL_SECONDS=5
  # Cap ideation in the experiment-config YAML (ideation_policy fixed_total:3)
  # so the run is bounded. Termination is OPERATOR-DRIVEN (Phase 5), NOT
  # dispatch_mode.termination=auto — the orchestrator's auto-termination
  # decision 403s under wire auth (terminate is admins-gated; #256).

Phase 2 — Bring up the stack with the lease overlay.
  docker compose -f compose.yaml -f compose.multi-experiment.yaml \
      --env-file $ENV up -d --wait --wait-timeout 300
  # control-plane comes up healthy (depends_on postgres + task-store-server).

  Assertions:
    - control-plane /healthz returns 200.
    - Register the experiment: POST /v0/control/experiments (admin bearer,
      from inside the control-plane container) → 201 or 200 (§3.2′).
    - control-plane /v0/control/experiments lists the experiment.
    - Seed the task-store `orchestrators` group with both replica worker_ids
      (the lease-driven path joins only the CONTROL-PLANE orchestrators group,
      not the task-store one — without this the lease holder's §3.7-gated
      dispatch/integrate calls 403; folded into #254).
    - Within a 60s deadline: exactly ONE active lease exists for the
      experiment, held by one of {orchestrator, orchestrator-2}. Record the
      holder worker_id as $HOLDER. (lease-singleton invariant — chapter 11 §4.)

Phase 3 — Lease-handoff drill (chaos).
  # Kill the current lease holder; assert the standby acquires the lease.
  docker rm -f eden-<$HOLDER>            # e.g. eden-orchestrator or eden-orchestrator-2
  Within lease_duration*2 + poll slack (~45s deadline): a single active lease
  exists again, held by the OTHER replica ($HOLDER changed) — no split-brain.

Phase 4 — The surviving replica drives the pipeline.
  Poll the events stream (240s deadline) until ≥2 variant.integrated. Assert
  ≥2 variant.integrated AND ≥2 execution-task.completed AND ≥2
  evaluation-task.completed (the post-hand-off holder drove dispatch +
  execute + evaluate + integrate end-to-end on the deployed stack).

Phase 5 — Operator-driven termination + state-sync convergence.
  # Register a throwaway worker, add it to `admins`, terminate via its
  # worker bearer (terminate_experiment rejects the literal admin bearer).
  POST /v0/experiments/<id>/terminate  (admins worker bearer)
  Assert:
    - an experiment.terminated event appears (60s deadline).
    - control-plane /v0/control/experiments shows last_known_state ==
      "terminated" (state-sync poller running→terminated convergence,
      chapter 11 §3; 30s deadline).

PASS
```

Substrate-cleanup posture mirrors `smoke-checkpoint.sh`: a per-run `mktemp -d` data root, and the `cleanup()` trap runs `docker compose ... down -v` + wipes the data root via a sibling Alpine container (uid-mismatch dance). bash-3.2 discipline applies (no `mapfile`/assoc-arrays).

#### 3.3 The smoke script [SUPERSEDED — see §3.3′ + §0]

The two-experiment / cross-experiment-isolation smoke design below is deferred to [#254](https://github.com/ealt/eden/issues/254); retained as historical context only. (Original Phase 4/6 cross-experiment-isolation assertions presuppose multi-experiment hosting the reference impl does not provide.)

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

- Add a `/healthz` endpoint to the control-plane server ([`reference/services/control-plane/src/eden_control_plane_server/app.py`](../../reference/services/control-plane/src/eden_control_plane_server/app.py); unauthenticated, outside `/v0/control`; shape mirrors the web-ui's `/healthz`). + a unit test.
- **[RE-SCOPED — see §0]** Add an `EDEN_CONTROL_PLANE_URL` env fallback for `--control-plane-url` in the orchestrator CLI ([`reference/services/orchestrator/src/eden_orchestrator/cli.py`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py)) and the web-ui CLI ([`reference/services/web-ui/src/eden_web_ui/cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py)), treating empty as unset. **No entrypoint wrapper scripts, no Dockerfile change** (supersedes the draft plan's wrapper items).

**Compose:**

- Add `control-plane` service to [`compose.yaml`](../../reference/compose/compose.yaml).
- Add `init-control-plane-db.sh` postgres-init hook + mount.
- Modify the `orchestrator` + `web-ui` services in compose.yaml: add the `EDEN_CONTROL_PLANE_URL` env (the lease-mode toggle via the §0 CLI env fallback), the `--lease-duration-seconds` flag + `control-plane` `depends_on` on the orchestrator. **No entrypoint wrappers.**
- Add `POSTGRES_DB_CONTROL_PLANE`, `EDEN_CONTROL_PLANE_STORE_URL`, `CONTROL_PLANE_HOST_PORT`, `EDEN_CONTROL_PLANE_URL`, `EDEN_LEASE_DURATION_SECONDS`, `EDEN_STATE_SYNC_INTERVAL_SECONDS`, `EDEN_STATE_SYNC_FAILURE_THRESHOLD` to [`.env.example`](../../reference/compose/.env.example). (`setup-experiment` emits the first three + `EDEN_CONTROL_PLANE_URL=` empty.)
- New `compose.multi-experiment.yaml` overlay — **RE-SCOPED**: a second `orchestrator-2` replica in lease mode (§3.1.5′), NOT a second host trio.
- Delete `compose.control-plane.yaml`.

**setup-experiment.sh:**

- **[RE-SCOPED — see §0] No change.** The single-experiment flow is used as-is; the smoke registers the one experiment with the control plane via a wire call (§3.2′). The `--register-additional-experiment` mode is deferred to [#254](https://github.com/ealt/eden/issues/254).

**Smoke + CI:**

- New [`reference/compose/healthcheck/smoke-multi-experiment.sh`](../../reference/compose/healthcheck/smoke-multi-experiment.sh) — the **lease-handoff** smoke (§3.3′).
- New `compose-smoke-multi-experiment` job in [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) (20-minute timeout, mirrors compose-smoke-multi-orchestrator + compose-smoke-checkpoint shape, not branch-protected initially).

**Docs:**

- AGENTS.md "Commands" table — new row.
- [`reference/compose/README.md`](../../reference/compose/README.md) — new section.
- [`docs/user-guide.md`](../../docs/user-guide.md) — short aside.
- `CHANGELOG.md [Unreleased]` — chunk-completion entry on impl-merge, with explicit "closes #147".
- [`docs/roadmap.md`](../../docs/roadmap.md) — planless-chunk one-line status flip on impl-merge (this is a backfill issue, not a roadmap chunk).

### 4.2 Out of scope (followups; file as issues if not already)

- **Helm-chart multi-experiment substrate.** Folded into the existing Phase 13a plan ([`docs/plans/eden-phase-13a-helm-base-chart.md`](eden-phase-13a-helm-base-chart.md)) when that lands; this plan is Compose-only. No new issue needed — Phase 13a's existing scope covers it.
- **[RE-SCOPED] Cross-experiment-isolation smoke (two experiments end-to-end).** Deferred to [#254](https://github.com/ealt/eden/issues/254) — the reference impl cannot host >1 experiment per deployment (§0). This is the headline deferral of the re-scope.
- **N-experiment generalization beyond N=2.** Subsumed by #254.
- **Multi-experiment load testing.** Per the issue: this is a smoke, not a stress test.
- **Cross-experiment scheduling intelligence** (e.g. lease-stealing for fair work distribution). Out of scope; chapter 11 §3.9 alternatives-considered documents this as a future v1 amendment.
- **Worker-host multi-experiment refactor.** Worker hosts stay single-experiment-scoped; a future refactor that lets one host trio serve multiple experiments is part of the #254 family.
- **`eden_control_plane` admin-pages in the web-ui.** Phase 12c shipped the read-only `/admin/experiments/` dashboard; a parallel `/admin/control/workers/` + `/admin/control/groups/` admin surface for the deployment-scoped registry is a follow-up (already deferred per the 12c CHANGELOG entry "Deployment-scoped worker/group registry admin pages not shipped").

### 4.3 Non-goals

- Replacing the existing 6 Compose smokes. They remain single-experiment + no-control-plane, exactly as 12c promised. This adds a 7th smoke.
- Changing the orchestrator's lease/decision behavior. All lease semantics + chapter 11 conformance is shipped; the smoke OBSERVES, it doesn't extend.

## 5. Files to touch

| File | Change |
|---|---|
| [`reference/compose/compose.yaml`](../../reference/compose/compose.yaml) | Add `control-plane` service block (§3.1.1). Add `EDEN_CONTROL_PLANE_URL` env + `--lease-duration-seconds` flag to `orchestrator`; `control-plane` to its `depends_on`. Add `EDEN_CONTROL_PLANE_URL` env to `web-ui`. Add postgres-init script mount on `postgres` (§3.1.2). |
| `reference/compose/init-control-plane-db.sh` (new) | Postgres init hook creating `eden_control_plane` database (§3.1.2). |
| [`reference/services/orchestrator/src/eden_orchestrator/cli.py`](../../reference/services/orchestrator/src/eden_orchestrator/cli.py) | **[RE-SCOPED]** `EDEN_CONTROL_PLANE_URL` env fallback for `--control-plane-url` (empty→unset). |
| [`reference/services/web-ui/src/eden_web_ui/cli.py`](../../reference/services/web-ui/src/eden_web_ui/cli.py) | **[RE-SCOPED]** `EDEN_CONTROL_PLANE_URL` env fallback for `--control-plane-url` (empty→unset). |
| [`reference/compose/compose.control-plane.yaml`](../../reference/compose/compose.control-plane.yaml) | **Delete** (§3.1.4). |
| `reference/compose/compose.multi-experiment.yaml` (new) | **RE-SCOPED**: second `orchestrator-2` replica in lease mode, layered as `-f compose.yaml -f compose.multi-experiment.yaml` (§3.1.5′). |
| [`reference/compose/.env.example`](../../reference/compose/.env.example) | Document the control-plane + lease env vars (§3.1.1/§3.1.3). (`_2` per-experiment namespacing deferred to #254.) |
| ~~`reference/scripts/setup-experiment/setup-experiment.sh`~~ | **RE-SCOPED — no change** (§3.2′). `--register-additional-experiment` deferred to #254. |
| `reference/compose/healthcheck/smoke-multi-experiment.sh` (new) | The lease-handoff smoke (§3.3′). |
| [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) | New `compose-smoke-multi-experiment` job mirroring `compose-smoke-checkpoint` shape. |
| [`reference/services/control-plane/src/eden_control_plane_server/app.py`](../../reference/services/control-plane/src/eden_control_plane_server/app.py) | Verify `/healthz` endpoint exists; add if missing. |
| [`AGENTS.md`](../../AGENTS.md) | New row in "Commands" table for the smoke. |
| [`reference/compose/README.md`](../../reference/compose/README.md) | New "Multi-experiment mode" section. |
| [`docs/user-guide.md`](../../docs/user-guide.md) | Short aside under "Running an experiment". |
| [`CHANGELOG.md`](../../CHANGELOG.md) | `[Unreleased]` entry on impl-merge. |
| [`docs/roadmap.md`](../../docs/roadmap.md) | Planless-chunk status flip on impl-merge. |

## 6. Test design

This is a substrate-level smoke; the assertions ARE the test. There are no new unit tests, no new wire tests, no new conformance scenarios (all of those shipped with 12c). Verification gates:

### 6.1 Smoke-level assertions (per §3.3′ above — RE-SCOPED)

- **Stack-startup**: control-plane `/healthz` 200; control-plane lists the one registered experiment; exactly ONE active lease held by one of the two replicas within ~60s (lease-singleton invariant).
- **Lease-handoff chaos**: killing the lease holder → the standby replica acquires the lease within `lease_duration * 2` + poll slack; at no observed instant are there two active leases (no split-brain).
- **Progress**: the post-hand-off holder reaches `≥2 variant.integrated`, `≥2 execution-task.completed`, `≥2 evaluation-task.completed` (full dispatch→execute→evaluate→integrate pipeline on the deployed stack).
- **Control-plane state-sync**: after an OPERATOR-DRIVEN `terminate_experiment` (admins worker — the orchestrator's auto-termination decision 403s under wire auth, [#256](https://github.com/ealt/eden/issues/256)), the experiment's `last_known_state` converges to `"terminated"` (chapter 11 §3 poller).

### 6.2 Local-repro discipline

Per AGENTS.md "Local repro beats log-tail reading", the smoke MUST be runnable locally on macOS bash 3.2 (no bash-4 builtins; no `mapfile` / `readarray` / associative arrays). Validation gate before merge: the implementing operator runs the smoke locally end-to-end at least twice (one cold-start, one against a leftover data-root to verify cleanup posture works).

### 6.3 CI verification gates

- `bash reference/compose/healthcheck/smoke-multi-experiment.sh` passes in the new CI job.
- All existing 6 compose smokes continue to pass (regression: the compose.yaml changes in §3.1 are conditional on env vars not set by the existing smokes).
- `uv run pytest -q` passes (regression: the entrypoint-wrapper scripts and setup-experiment changes don't break unit tests).
- `python3 scripts/check-rename-discipline.py` clean.
- `npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"` clean.
- Manual UI smoke (RE-SCOPED): spin up the stack with the lease overlay + control-plane env; verify `/admin/experiments/` shows the one registered experiment (the cross-experiment switcher is exercised by #254).

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

**Wave 2 — Lease overlay (RE-SCOPED — see §0)** (covers Decisions 4, 5, 7 + §3.1.5′):

- New `compose.multi-experiment.yaml` overlay adding `orchestrator-2` in lease mode (§3.1.5′).
- **Validation gate**: `docker compose -f compose.yaml -f compose.multi-experiment.yaml --env-file <env> up -d --wait` (with `EDEN_ORCHESTRATOR_MULTI_EXPERIMENT=1` + `EDEN_CONTROL_PLANE_URL` set) brings up both orchestrator replicas in lease mode against the control plane. (setup-experiment is unchanged; `--register-additional-experiment` deferred to #254.)

**Wave 3 — Smoke + CI + docs** (covers Decision 8 + §3.3′ + §3.5):

- New `smoke-multi-experiment.sh` (lease-handoff).
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
6. **Audit-of-substrate-rename trap.** AGENTS.md "Substrate migrations need a same-PR audit" applies. The compose.control-plane.yaml deletion is the main concrete reference to audit. Grep audit run in the impl PR — every hit classified per the AGENTS.md checklist (real consumer vs doc reference):
   - **Real consumers** (compose / scripts / CI / operator docs) updated: `docs/observability.md` §2.1/§3.4 rewritten to the first-class-service + `EDEN_CONTROL_PLANE_URL` toggle. No compose/CI/script still references the deleted overlay.
   - **Forward-looking plan references** updated: the sibling-overlay example lists in `issue-110` and the §3.4 bring-up step in `issue-182` now point at the first-class service / `compose.multi-experiment.yaml`.
   - **Historical analysis preserved** (deliberately not edited): `docs/plans/issue-157-cli-flags-to-config.md` references the overlay's web-ui-only shape as point-in-time analysis of a now-superseded state; rewriting it would corrupt that plan's record. These remaining `compose.control-plane` hits are expected and intentional.
   - `grep -rn 'control-plane' reference/compose/` — shows the new compose.yaml `control-plane` service block + the postgres init-hook mount + the `EDEN_CONTROL_PLANE_URL` env wiring + nothing else (no wrapper scripts — see §0 impl refinement).
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
