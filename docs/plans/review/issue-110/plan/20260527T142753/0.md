# Issue #110 — In-stack log search UI (Loki + Promtail + Grafana overlay)

> Plan for [issue #110](https://github.com/ealt/eden/issues/110). Adds an **opt-in** Compose overlay (`compose.logging.yaml`) that ships Loki (log store + LogQL), Promtail (shipper), and Grafana (search UI, pre-provisioned) so operators can search EDEN's logs across services + time windows from one UI instead of a multi-window `docker compose logs` session.

## 1. Context

EDEN has no in-stack way to search logs across services. `docker compose logs -f <service>` tails one service; `grep` across multiple `docker compose logs` outputs works but doesn't survive container removal. Operators investigating cross-service incidents (e.g. an orchestrator dispatch decision and the evaluator's later processing of the resulting variant) need correlated search across services + time windows.

The four-rung log-persistence ladder is already documented in [`docs/observability.md`](../observability.md) §2.5:

| Rung | What it gives you | Status before this chunk |
|---|---|---|
| **L1** | Bumped docker rotation (50 MB × 5 per service) | In place |
| **L2** | Per-service file handler → `${EDEN_EXPERIMENT_DATA_ROOT}/logs/` bind-mount (survives `compose down -v`) | In place ([#109](https://github.com/ealt/eden/issues/109)) |
| **L3** | **In-stack search UI (Loki + Promtail + Grafana overlay)** | **Tracked in #110 — this chunk** |
| **L4** | External streaming (CloudWatch / Datadog / Grafana Cloud) | Production-grade; out of scope |

This chunk fills L3. It is a pure **deployment-binding** addition: a Compose overlay plus the Loki/Promtail/Grafana config files and operator docs. **No EDEN service code, no wire/spec/schema/Pydantic changes, no conformance changes.**

### 1.1 The one fact that reshapes the issue's design: #109 has shipped

The issue body was written when [#109 (`EDEN_LOG_DIR`)](https://github.com/ealt/eden/issues/109) was still open and proposed Promtail scrape logs via `docker_sd_configs` (which needs the docker socket) "+ (when #109 lands) tails `${EDEN_EXPERIMENT_DATA_ROOT}/logs/*.jsonl` as well." **#109 is now closed and merged.** Every long-running EDEN service writes a structured JSON-line file to `${EDEN_EXPERIMENT_DATA_ROOT}/logs/<service>/<service>.jsonl` (bind-mount; see [`compose.yaml`](../../reference/compose/compose.yaml) `EDEN_LOG_DIR` env + per-service `logs/<service>` volume, and [`eden_service_common/logging.py`](../../reference/services/_common/src/eden_service_common/logging.py)). Records are flat JSON with stable keys (`ts`, `level`, `service`, `experiment_id`, `message`, plus per-record context keys).

That changes the recommended primary log source — see §2 decision 1. The net effect is a **simpler and lower-privilege** design than the issue sketched: Promtail tails the JSONL bind-mount read-only and does **not** need the docker socket at all.

## 2. Decisions captured before drafting

These are the load-bearing calls this plan makes. They are defensible defaults, called out explicitly so codex-review and the operator can challenge each one rather than have it buried in a config file. None is treated as settled against a contradicting spec MUST (there are no spec MUSTs in scope).

1. **Primary log source = tail the #109 JSONL bind-mount, NOT `docker_sd_configs`.** Promtail mounts `${EDEN_EXPERIMENT_DATA_ROOT}/logs` read-only and tails `**/<service>.jsonl`. Rationale:
   - **No docker socket.** The issue's `docker_sd_configs` path requires bind-mounting `/var/run/docker.sock` into Promtail — the same privilege the `compose.docker-exec.yaml` overlay carefully gates behind its own overlay (AGENTS.md "any deployment mode that needs new privilege … goes in its own compose overlay"). Tailing files needs only read access to a bind-mount. Strictly less privilege for the common case.
   - **Richer labels.** The JSONL is already structured; Promtail's pipeline promotes `service` / `level` / `experiment_id` to Loki labels directly. The docker-stdout path delivers the same JSON wrapped in docker's log envelope and would need re-parsing.
   - **Survives container removal.** The bind-mount is the durable copy that motivated #109; it outlives `compose down`.
   - **Cost:** this source covers only the EDEN services that use `eden_service_common` logging (the 6 services). Postgres + Forgejo log to stdout only and are NOT captured by the JSONL tail. That is acceptable for v0 — the high-signal logs are the EDEN services' (orchestrator decisions etc.). Capturing infra stdout is a **documented optional extension** (§3.4), added as a second Promtail scrape job behind the docker socket only if an operator wants it. The default ships without the socket.

2. **Loki storage location: bind-mount under `${EDEN_EXPERIMENT_DATA_ROOT}/loki/`, classified as DERIVED / non-protocol-owned.** The issue asks for `${EDEN_EXPERIMENT_DATA_ROOT}/loki/`. Honored — but with an explicit classification note, because 12a-1g established that everything under the data root is presumed protocol-owned durable state (chapter 01 §13). Loki's index + chunks are a **queryable projection of the `logs/` bind-mount**, not protocol-owned state: losing it loses search history but no experiment state, and it can be rebuilt by re-ingesting (subject to Loki/Promtail's position tracking — practically, re-ingestion covers logs still present in `logs/`). It is therefore NOT covered by the §13 durability invariant and NOT exercised by the durability smoke. The classification is documented in `compose.logging.yaml`'s volume comment and in the operator doc. (Alternative considered: a named volume, fully ephemeral. Rejected as default because co-locating under the data root lets "search history survives restart" hold and keeps one cleanup path — `rm -rf ${EDEN_EXPERIMENT_DATA_ROOT}` — but the alternative is one line away if an operator prefers strict ephemerality.)

3. **Grafana auth: generated admin password via env; no anonymous access by default.** `setup-experiment.sh` generates `EDEN_GRAFANA_ADMIN_PASSWORD` (same secret-generation path as the other secrets) and passes it as `GF_SECURITY_ADMIN_PASSWORD`. The default `admin/admin` is overridden. Anonymous viewer access (`GF_AUTH_ANONYMOUS_ENABLED=true`) is a documented one-line ergonomics option for pure local demos but is **off by default** so the posture matches the rest of the stack (admin-token-gated surfaces). The demo posture is local-only and not exposed; we still don't ship `admin/admin`.

4. **Frictionless bring-up: no new setup flag.** `setup-experiment.sh` **always** generates `EDEN_GRAFANA_ADMIN_PASSWORD` and **always** creates `${EDEN_EXPERIMENT_DATA_ROOT}/loki/` (chmod 0777, per the 12a-1g multi-uid precedent — Loki runs as uid 10001). Both are negligible when the overlay isn't used (one unused secret line, one empty dir), and this keeps the issue's stated bring-up frictionless: `docker compose -f compose.yaml -f compose.logging.yaml up -d` works against any setup-experiment-bootstrapped stack with no extra flag. (Alternative considered: a `--with-logging` flag gating the dir/secret. Rejected: it adds a setup-time decision for a teardown-cheap artifact and breaks the "just add `-f compose.logging.yaml`" story.)

5. **Ship a path-gated CI smoke (`compose-smoke-logging`), not docs-only.** AGENTS.md is explicit that smokes are the literal pre-push validation gate and that overlay wiring regressions hide from scoped pytest. A `smoke-logging.sh` + a `compose-smoke-logging` CI job gated on the existing `compose` paths-filter bucket validates the overlay end-to-end (Loki ready, Promtail shipping, a LogQL query returns EDEN lines, Grafana healthy + datasource/dashboard provisioned). The issue's "do not require it on lean CI" constraint is honored by the path gate — the job only runs when `reference/compose/**` or `reference/scripts/**` changes, exactly like the other `compose-smoke-*` jobs, and it is added as a non-required status (same posture as `compose-smoke-checkpoint` / `compose-smoke-multi-orchestrator`).

6. **Pin image tags; Loki and Promtail major versions MUST match.** Use explicit tags (`grafana/loki:3.x.y`, `grafana/promtail:3.x.y`, `grafana/grafana:11.x.y`), never `latest`. Loki + Promtail are released in lockstep and skew breaks ingestion silently. Exact patch tags chosen at impl time against the then-current stable line.

## 3. Design

### 3.1 New overlay: `reference/compose/compose.logging.yaml`

Sibling to `compose.subprocess.yaml` / `compose.docker-exec.yaml` / `compose.control-plane.yaml` / `compose.multi-orchestrator.yaml`. Layered as:

```bash
cd reference/compose
docker compose -f compose.yaml -f compose.logging.yaml --env-file .env up -d --wait
```

Composes cleanly on top of any other overlay set (it adds 3 new services and touches none of the existing ones), e.g. `-f compose.yaml -f compose.subprocess.yaml -f compose.logging.yaml`.

Three services:

| Service | Image (pinned) | Host port | Internal | Reads | Writes |
|---|---|---|---|---|---|
| `loki` | `grafana/loki:3.x.y` | — (internal only) | 3100 | — | `${EDEN_EXPERIMENT_DATA_ROOT}/loki/` (bind-mount) + `./logging/loki-config.yaml` (ro) |
| `promtail` | `grafana/promtail:3.x.y` | — (internal only) | 9080 | `${EDEN_EXPERIMENT_DATA_ROOT}/logs` (ro bind-mount) + `./logging/promtail-config.yaml` (ro) + `${EDEN_EXPERIMENT_DATA_ROOT}/promtail/` (positions file, rw) | pushes to `loki:3100` |
| `grafana` | `grafana/grafana:11.x.y` | `${GRAFANA_HOST_PORT:-3000}:3000` | 3000 | `./logging/grafana/provisioning/**` (ro) + `./logging/grafana/dashboards/**` (ro) | (in-memory sqlite; no durable mount needed) |

Notes:

- **Loki + Grafana are internal-only except Grafana's host port.** Loki is reached by Promtail and Grafana over the compose network (`http://loki:3100`); no host port. Grafana is the only host-exposed surface, at `${GRAFANA_HOST_PORT:-3000}`. Grafana's canonical port is 3000; the override knob handles the rare local collision (and respects [`reference/compose/README.md`](../../reference/compose/README.md)'s "avoid well-known ports" note — the *default* of 3000 is Grafana's well-known port, which operators expect, but it's overridable).
- **Healthchecks.** `loki`: `GET /ready`. `grafana`: `GET /api/health`. `promtail`: `GET /ready` (or `/metrics`). All `CMD`/argv form, `wget --spider` (alpine-based images) consistent with the existing forgejo healthcheck pattern.
- **`depends_on`.** `promtail` and `grafana` depend on `loki: service_healthy`. The overlay's services do NOT `depends_on` any base-stack service (the JSONL files Promtail tails are created by setup-experiment + the base services; if a base service hasn't written yet, Promtail simply has no lines to ship — not an error).
- **Grafana durable storage: none.** Grafana's own DB (dashboards, users) is fully reconstructed from provisioning on every start, so it gets no bind-mount. This keeps Grafana stateless and avoids a fourth uid/permission surface. (The pre-provisioned dashboard + datasource are repo-tracked; nothing operator-created needs to survive.)
- **`logging: *eden-logging`.** The three overlay services reuse the base `x-eden-logging` anchor? — No: that anchor is defined in `compose.yaml`; YAML anchors do **not** cross files in a multi-file compose merge. Either inline the same `driver: json-file` / rotation options on each overlay service, or omit (accept docker defaults). Recommend inlining the same 50 MB × 5 rotation for parity. (Tricky-area §8.5.)

### 3.2 Config files (new directory `reference/compose/logging/`)

Repo-tracked static config, bind-mounted read-only into the containers (the same pattern the stack uses for `credential-helper.sh`):

```text
reference/compose/logging/
  loki-config.yaml                                   # Loki: filesystem storage, single-binary, retention
  promtail-config.yaml                               # Promtail: tail logs/**/*.jsonl, JSON pipeline, push to loki
  grafana/
    provisioning/
      datasources/loki.yaml                          # auto-provision the Loki datasource (default)
      dashboards/dashboards.yaml                      # provider pointing at /var/lib/grafana/dashboards
    dashboards/
      eden-explore.json                               # the pre-provisioned "EDEN explore" dashboard
```

- **`loki-config.yaml`** — single-binary / monolithic mode, `filesystem` object store under `/loki` (the bind-mount), boltdb-shipper or tsdb index. Set a finite `retention_period` (e.g. 168h) + `compactor` retention so a long-lived demo's disk doesn't grow unbounded. `auth_enabled: false` (single-tenant; this is a local demo).
- **`promtail-config.yaml`** — one `scrape_config` with a `static_configs`/file target on `/var/lib/eden/logs/**/*.jsonl` (the read-only bind-mount path inside the Promtail container). Pipeline stages: `json` (extract `level`, `service`, `experiment_id`, `ts`), `timestamp` (use the record `ts`), `labels` (promote `service`, `level`, `experiment_id` — all low-cardinality). The `service` label can alternatively be derived from the path (`<service>/<service>.jsonl`) as a fallback if a record lacks the field. **Glob must handle rotation:** tail `*.jsonl` only (not `*.jsonl.1`…`.5`) so rotated backups aren't re-ingested; Promtail's positions file (`${EDEN_EXPERIMENT_DATA_ROOT}/promtail/positions.yaml`) tracks offsets across restart.
- **`grafana/provisioning/datasources/loki.yaml`** — Loki datasource at `http://loki:3100`, `isDefault: true`.
- **`grafana/provisioning/dashboards/dashboards.yaml`** — a file provider loading `/var/lib/grafana/dashboards`.
- **`grafana/dashboards/eden-explore.json`** — a starter dashboard: a logs panel with a LogQL query scoped to EDEN services (`{service=~".+"}` or `{experiment_id="$experiment_id"}`), a `service` template variable (multi-select), a `level` filter, and a time picker. Kept deliberately small (one logs panel + variables); operators extend in Grafana's Explore view.

### 3.3 `setup-experiment.sh` + `.env.example`

- **`.env.example`** — add a "Log search (Loki/Grafana overlay)" section: `EDEN_GRAFANA_ADMIN_PASSWORD=` and `GRAFANA_HOST_PORT=3000` (commented default).
- **`setup-experiment.sh`** — (1) generate `EDEN_GRAFANA_ADMIN_PASSWORD` via the existing secret generator and write it to `.env` (preserved across re-runs via the existing `read_env_key` idempotency, like every other secret); (2) extend the substrate-dir-tree creation to `mkdir -p` + `chmod 0777` two new subdirs: `${DATA_ROOT}/loki/` and `${DATA_ROOT}/promtail/`. Both classified in a comment as derived/observability (non-protocol-owned), distinct from the durable substrate subdirs.

### 3.4 Optional infra-log extension (documented, not default)

For operators who also want Postgres/Forgejo stdout in the search UI, document a second Promtail scrape job using `docker_sd_configs` + a `/var/run/docker.sock:ro` mount, enabled by uncommenting a block in `promtail-config.yaml` (and adding the socket mount to `compose.logging.yaml`'s promtail service). Framed exactly like the issue's original sketch, but as an opt-in addition that pulls in the socket trust posture only when chosen. Cross-reference the `compose.docker-exec.yaml` socket-trust note.

### 3.5 Docs

Per the issue's "Documentation updates required", plus the natural homes:

- **`docs/observability.md`** — (a) §1 "Process logs" substrate row: cross-link Loki/Grafana as the search interface over the logs substrate. (b) §2.5 ladder table: flip the **L3** row from "Tracked in #110" to "In place" and link the overlay. (c) Promote the bring-your-own framing: add a new first-party subsection (§2.7 "Log search UI (Loki + Grafana)") covering the bring-up command, Grafana URL (`localhost:3000`), credentials (`admin` / `EDEN_GRAFANA_ADMIN_PASSWORD`), the pre-provisioned dashboard, an example LogQL query, and the optional infra-log extension. (Issue says "promote from §3 bring-your-own to §2"; today there is no Loki/Grafana entry in §3 — there's only Adminer/Swagger/desktop clients — so this is a net-new §2 subsection rather than a move. Note in the PR.)
- **`docs/user-guide.md`** — §1 components table: add a **grafana** row pointing at `localhost:3000` ("Log search UI; opt-in via `-f compose.logging.yaml`"). §9 already points at `observability.md`; verify the pointer still resolves after the §2 additions (no content change beyond that).
- **`reference/compose/README.md`** — add a "Log search overlay (`compose.logging.yaml`)" subsection alongside the other overlay docs; note the derived (non-durable) classification of `loki/` + `promtail/` so the storage-layout durable/ephemeral table stays accurate.
- **`AGENTS.md`** — "Commands" table: add the `docker compose -f compose.yaml -f compose.logging.yaml up -d` bring-up and the `bash reference/compose/healthcheck/smoke-logging.sh` smoke.
- **`CHANGELOG.md` [Unreleased]** + **`docs/roadmap.md`** — chunk-completion entry + planless/issue-driven one-liner pointing at this plan, per the AGENTS.md chunk-completion discipline (this is the impl-stage docs obligation; the plan-stage record is the codex transcript committed under `docs/plans/review/issue-110/plan/`).

## 4. Scope

**In scope:**

- New overlay `reference/compose/compose.logging.yaml` (3 services: loki, promtail, grafana).
- New config dir `reference/compose/logging/` (loki + promtail configs; Grafana datasource/dashboard provisioning + one starter dashboard).
- `reference/compose/.env.example` — `EDEN_GRAFANA_ADMIN_PASSWORD` + `GRAFANA_HOST_PORT`.
- `reference/scripts/setup-experiment/setup-experiment.sh` — generate the Grafana password (idempotent); create + chmod the `loki/` and `promtail/` subdirs.
- New `reference/compose/healthcheck/smoke-logging.sh` + `compose-smoke-logging` CI job (path-gated, non-required).
- Docs: `observability.md`, `user-guide.md`, `reference/compose/README.md`, `AGENTS.md`, `CHANGELOG.md`, `docs/roadmap.md`.

**Out of scope (deferred / non-goals):**

- **External streaming** (CloudWatch / Datadog / Grafana Cloud) — L4; production-grade, separate concern. Tracked as the existing L4 ladder rung; file a follow-up issue only if an operator asks for it.
- **Per-experiment label namespacing in Loki.** Single-experiment Compose deployments don't need it; the `experiment_id` label already disambiguates. Revisit if multi-experiment shared deployments land (cross-link the multi-experiment control-plane work). → file follow-up issue if not already covered.
- **Infra (Postgres/Forgejo) stdout capture by default** — shipped only as the documented opt-in §3.4 extension.
- **Grafana alerting / Loki rules** — search UI only; no alert routing.
- **Helm/k8s equivalent** (a Loki/Grafana chart for the Phase 13 substrate) — Compose-only here. → file follow-up issue.
- **EDEN service code changes** — none. The JSONL logs (#109) are the contract Promtail consumes; this chunk does not touch logging.py or any service.

Each deferral above with a "→ file follow-up issue" marker MUST land a GitHub issue at impl time and be referenced in the CHANGELOG entry, per the AGENTS.md deferral-tracking rule.

## 5. Spec / contract impact

**None.** This is entirely a deployment-binding addition.

- **Spec chapters:** no amendments. Observability tooling attached to a deployment is not protocol surface.
- **JSON Schema:** no changes.
- **Pydantic models:** no changes.
- **Wire bindings:** no changes. Loki/Grafana speak their own HTTP APIs; nothing routes through the EDEN wire protocol.
- **`eden_service_common/logging.py`:** unchanged — its JSONL output format is the de-facto input contract for the Promtail pipeline. (Risk note: this is now an *implicit* contract; see §10. The Promtail `json` pipeline degrades gracefully if a field is missing — it just doesn't get that label — so a logging-schema change can't break ingestion, only label richness.)

## 6. Naming map

No renames of existing identifiers. New identifiers introduced (validated against [`docs/glossary.md`](../glossary.md) — these are deployment-infra names, not EDEN protocol vocabulary, so the role/verb/kind patterns don't apply; the discipline here is consistency with existing Compose naming):

| New identifier | Kind | Convention followed |
|---|---|---|
| `compose.logging.yaml` | overlay file | `compose.<concern>.yaml` (matches `compose.subprocess.yaml`, `compose.docker-exec.yaml`, `compose.control-plane.yaml`, `compose.multi-orchestrator.yaml`) |
| `loki` / `promtail` / `grafana` | compose service names | upstream tool names, lowercase (matches `forgejo`, `postgres`) |
| `EDEN_GRAFANA_ADMIN_PASSWORD` | env var (secret) | `EDEN_<THING>_<ROLE>` (matches `EDEN_READONLY_PASSWORD`, `EDEN_ADMIN_TOKEN`, `EDEN_SESSION_SECRET`) |
| `GRAFANA_HOST_PORT` | env var (port knob) | `<SERVICE>_HOST_PORT` (matches `FORGEJO_HOST_PORT`, `TASK_STORE_HOST_PORT`, `POSTGRES_HOST_PORT`, `WEB_UI_HOST_PORT`) |
| `${EDEN_EXPERIMENT_DATA_ROOT}/loki/`, `/promtail/` | data-root subdirs | `<DATA_ROOT>/<substrate>/` (matches `postgres/`, `forgejo/`, `artifacts/`) — but classified DERIVED, not durable |
| `smoke-logging.sh` | healthcheck script | `smoke-<concern>.sh` (matches `smoke-subprocess.sh`, `smoke-checkpoint.sh`, `smoke-multi-orchestrator.sh`) |
| `compose-smoke-logging` | CI job | `compose-smoke-<concern>` (matches `compose-smoke-subprocess-docker`, `compose-smoke-checkpoint`) |
| `eden-explore` (dashboard) | Grafana dashboard uid/title | new; `eden-<purpose>` |

The `rename-discipline` CI job scans nothing new here (no legacy-vocab patterns), but it gates the chunk because `reference/compose/**` is in its trigger set.

## 7. Migration / cleanup

Nothing to retire. This is purely additive — a new overlay that did not exist before. No backwards-compat shims (consistent with the CLAUDE.md pre-external-user posture, and irrelevant here since nothing is being replaced).

One cleanup consideration: `${EDEN_EXPERIMENT_DATA_ROOT}/loki/` and `/promtail/` are created by `setup-experiment.sh` for **every** experiment (decision §2.4), even ones that never run the overlay. They stay empty until the overlay runs. The existing `rm -rf ${EDEN_EXPERIMENT_DATA_ROOT}` reset already removes them; no separate cleanup path. The smoke's existing root-owned-file cleanup-via-sibling-container pattern (loki writes as uid 10001) is reused — see §9.

## 8. Conformance impact

**None.** Per [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §6, the chapter-7 HTTP binding is the only IUT contract a conformance harness can rely on. Loki/Promtail/Grafana are off-wire deployment tooling — the suite cannot and should not observe them. No §-references change, no scenarios added, no `CONFORMANCE_GROUP` declarations. `check_citations.py` is unaffected. (This is the AGENTS.md "filter MUSTs through the IUT contract" pitfall applied trivially: there are no MUSTs here, and nothing is wire-observable.)

## 9. Chunked execution plan

Realistically one cohesive PR (~250 lines: ~100 overlay, ~80 config, ~40 setup/.env, ~30 smoke/CI, plus docs). Structured as three waves with explicit gates; if convenient they land as three commits in one PR.

### Wave 1 — overlay + config + setup-experiment

Author `compose.logging.yaml`, the `logging/` config tree, the `.env.example` + `setup-experiment.sh` changes.

**Validation gate (all must pass):**

```bash
# Static: the merged compose resolves and the new services are well-formed.
cd reference/compose
docker compose -f compose.yaml -f compose.logging.yaml --env-file .env config >/dev/null

# Live bring-up against a real bootstrapped experiment (subprocess mode so
# real log lines exist), then assert each service is healthy + Loki has EDEN lines:
bash ../scripts/setup-experiment/setup-experiment.sh ../../tests/fixtures/experiment/.eden/config.yaml --experiment-id logtest --data-root "$(mktemp -d)"
docker compose -f compose.yaml -f compose.subprocess.yaml -f compose.logging.yaml --env-file .env up -d --wait
#   - loki:      curl -fsS http://localhost:<loki>/ready          (via docker exec; loki has no host port)
#   - grafana:   curl -fsS http://localhost:3000/api/health       -> "ok"
#   - datasource present: curl (grafana admin) /api/datasources    -> Loki listed
#   - LogQL returns EDEN lines: query_range {service="orchestrator"} -> >=1 result
```

### Wave 2 — smoke script + CI job

Author `reference/compose/healthcheck/smoke-logging.sh` (mirrors `smoke-checkpoint.sh` shape: preflight tool check, `mktemp -d` data root + `trap` cleanup incl. the sibling-container root-owned-file delete, setup-experiment, bring up base+subprocess+logging overlays, wait for a few EDEN log lines to exist, assert Loki `/ready` + a LogQL query returns ≥1 EDEN line + Grafana `/api/health` ok + Loki datasource provisioned + dashboard present). Add the `compose-smoke-logging` job to `.github/workflows/ci.yml`, gated on the `compose` paths-filter bucket, `bash` 3.2-safe, non-required status.

**Validation gate:**

```bash
bash reference/compose/healthcheck/smoke-logging.sh   # exits 0
```

### Wave 3 — docs + completion records

`observability.md` (§1 row, §2.5 ladder L3 flip, new §2.7), `user-guide.md` (§1 grafana row, §9 pointer check), `reference/compose/README.md` (overlay subsection + classification), `AGENTS.md` (Commands table), `CHANGELOG.md` [Unreleased] entry, `docs/roadmap.md` one-liner. File the §4 deferral follow-up issues and reference them in the CHANGELOG entry.

**Validation gate (full pre-push quartet + lints, per AGENTS.md "literal validation gate"):**

```bash
npx --yes markdownlint-cli2@0.14.0 "**/*.md" "#node_modules" "#.venv" "#docs/archive/**" "#docs/plans/review/**"
python3 scripts/check-rename-discipline.py
uv run ruff check . && uv run pyright && uv run pytest -q          # no Python touched; proves no accidental damage
bash reference/compose/healthcheck/smoke.sh                        # base stack unaffected by the new overlay
bash reference/compose/healthcheck/smoke-logging.sh               # the new surface
```

(`spec-xref-check.py`, `check-jsonschema`, conformance, schema-parity are unaffected — no spec/schema/conformance changes — but run them anyway as the literal gate.)

## 10. Risks / things to watch

- **Loki ↔ Promtail version skew.** Released in lockstep; a mismatched major breaks the push protocol with a confusing error. Pin both to the same line (§2.6). Watch on any future image bump.
- **Implicit log-schema contract.** Promtail's pipeline depends on the `eden_service_common/logging.py` field names (`ts`, `level`, `service`, `experiment_id`). A future logging-schema change degrades labels **silently** (the `json` stage just drops missing fields; ingestion still works, search by `service`/`level` quietly stops). Mitigation: the `smoke-logging.sh` LogQL assertion queries by `{service="orchestrator"}`, so a label regression fails the smoke. Note the coupling in `logging/promtail-config.yaml`'s header comment so a logging.py editor sees it.
- **Port 3000 collision on the operator's machine.** Common (lots of dev tools default to 3000). The `${GRAFANA_HOST_PORT:-3000}` knob is the escape hatch; document it. Loki/Promtail aren't host-exposed so they can't collide.
- **`chmod 0777` on `loki/` + `promtail/`.** Same security pushback surface as 12a-1g's substrate dirs; same justification (local-dev multi-uid portability, Loki runs as uid 10001). Hold the 0777 line per the 12a-1g precedent; Phase 13 substrates are the production path.
- **Loki disk growth on a long-lived demo.** Unbounded ingestion fills `${DATA_ROOT}/loki/`. Mitigation: finite `retention_period` + compactor retention in `loki-config.yaml`. Document the knob.
- **Grafana provisioning fails silently.** A malformed datasource/dashboard YAML leaves Grafana up but without the datasource — `/api/health` still returns ok. Mitigation: the smoke asserts the datasource is present via `/api/datasources` and the dashboard via `/api/dashboards`, not just `/api/health`.
- **YAML anchor scope across compose files.** `*eden-logging` is defined in `compose.yaml` and is NOT visible in `compose.logging.yaml` (anchors don't cross the multi-file merge). Inline the rotation options on the overlay services; a bare `*eden-logging` reference in the overlay would fail to parse (§3.1 / §8.5 trap).
- **`compose -f compose.yaml -f compose.logging.yaml config` interaction with other overlays.** The logging overlay must compose with subprocess/docker-exec/control-plane sets. Since it adds only new services and edits none, the merge is additive and safe — but the smoke deliberately layers it on top of the subprocess overlay (which is what produces real per-task log volume) to prove the combination.
- **`smoke-logging.sh` runtime + CI cost.** Pulls ~400 MB of loki/grafana/promtail images + brings up the full stack. The path gate keeps it off lean CI (only runs on `reference/compose/**` / `reference/scripts/**` changes); `timeout-minutes: 20` matches the other compose smokes. Non-required status until it's stayed green on main ~2 weeks (same posture as `compose-smoke-checkpoint`).
- **Promtail positions file across container restart.** Tailing files (not docker_sd) means Promtail must persist `positions.yaml` to avoid re-ingesting from the top on restart. That's why `${DATA_ROOT}/promtail/` is a rw bind-mount, not in-container scratch. If it were lost, restart would double-ingest already-shipped lines (duplicates in Loki, not data loss) — acceptable but the bind-mount avoids it.
- **bash 3.2 in `smoke-logging.sh`.** Per AGENTS.md, no `mapfile`/`declare -A`; reuse the existing smoke scripts' `while read` + parallel-array idioms.

## 11. Sequence within the chunk

1. Rebase impl branch against `origin/main` at impl-start.
2. Wave 1 (overlay + config + setup-experiment) → gate.
3. Wave 2 (smoke + CI job) → gate.
4. Wave 3 (docs + completion records + deferral issues) → full gate.
5. Codex-review to convergence (plan PR caught plan-level concerns; impl PR catches config correctness — Loki/Promtail config validity, provisioning paths, the JSONL-vs-socket decision, the derived-storage classification).
6. Open impl PR; body covers: overlay shape, the #109-driven JSONL-tail decision (vs the issue's docker_sd sketch), the derived-storage classification, the optional infra extension, deferral issue links, test-plan checklist, codex round count. Use the PR template; the "Fresh-operator walkthrough" section is required (this changes operator-facing surfaces: a new UI + bring-up command).

## 12. Estimated effort

| Activity | Estimate |
|---|---|
| `compose.logging.yaml` + `logging/` config tree (loki/promtail/grafana) | ~0.5 day |
| `setup-experiment.sh` + `.env.example` (password gen + dir creation) | ~0.25 day |
| `smoke-logging.sh` + `compose-smoke-logging` CI job | ~0.5 day |
| Docs (observability.md / user-guide / README / AGENTS / CHANGELOG / roadmap) + deferral issues | ~0.5 day |
| Validation gates + local bring-up iteration (config-tuning is the variable) | ~0.5 day |
| Codex-review iterations (plan + impl) | ~0.75 day |
| **Total** | **~3 days** |

The dominant variable is Loki/Promtail config tuning — the pipeline stages + label promotion + retention need a live bring-up to validate, and Grafana provisioning paths are finicky. Image version selection (§2.6) should be locked early.
