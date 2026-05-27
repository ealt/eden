# Issue #110 — In-stack log search UI (Loki + Alloy + Grafana overlay)

> Plan for [issue #110](https://github.com/ealt/eden/issues/110). Adds an **opt-in** Compose overlay (`compose.logging.yaml`) that ships Loki (log store + LogQL), Grafana Alloy (log shipper), and Grafana (search UI, pre-provisioned) so operators can search EDEN's logs across services + time windows from one UI instead of a multi-window `docker compose logs` session.
>
> **Collector choice supersedes the issue text.** Issue #110 proposed **Promtail** as the shipper. Promtail reached **end-of-life on 2026-03-02** (LTS ended 2026-02-28); Grafana's supported successor is **Grafana Alloy** (their OpenTelemetry Collector distribution), and the official Loki docs now route all "send log data" guidance through Alloy. This plan therefore substitutes **Alloy** for Promtail. The architecture is identical — tail the #109 JSONL bind-mount → ship to Loki → search in Grafana — only the collector binary + its config language change. (Sources: Grafana Loki "Migrate to Alloy" + the Promtail-EOL announcements, verified 2026-05-27.)

## 1. Context

EDEN has no in-stack way to search logs across services. `docker compose logs -f <service>` tails one service; `grep` across multiple `docker compose logs` outputs works but doesn't survive container removal. Operators investigating cross-service incidents (e.g. an orchestrator dispatch decision and the evaluator's later processing of the resulting variant) need correlated search across services + time windows.

The four-rung log-persistence ladder is already documented in [`docs/observability.md`](../observability.md) §2.5:

| Rung | What it gives you | Status before this chunk |
|---|---|---|
| **L1** | Bumped docker rotation (50 MB × 5 per service) | In place |
| **L2** | Per-service file handler → `${EDEN_EXPERIMENT_DATA_ROOT}/logs/` bind-mount (survives `compose down -v`) | In place ([#109](https://github.com/ealt/eden/issues/109)) |
| **L3** | **In-stack search UI (Loki + Alloy + Grafana overlay)** | **Tracked in #110 — this chunk** |
| **L4** | External streaming (CloudWatch / Datadog / Grafana Cloud) | Production-grade; out of scope |

This chunk fills L3. It is a pure **deployment-binding** addition: a Compose overlay plus the Loki/Alloy/Grafana config files and operator docs. **No EDEN service code, no wire/spec/schema/Pydantic changes, no conformance changes.**

### 1.1 Two facts reshape the issue's design

**(a) #109 has shipped.** The issue body was written when [#109 (`EDEN_LOG_DIR`)](https://github.com/ealt/eden/issues/109) was still open and proposed the shipper scrape logs via `docker_sd_configs` (which needs the docker socket) "+ (when #109 lands) tails `${EDEN_EXPERIMENT_DATA_ROOT}/logs/*.jsonl` as well." **#109 is now closed and merged.** Every long-running EDEN service writes a structured JSON-line file to `${EDEN_EXPERIMENT_DATA_ROOT}/logs/<service>/<service>.jsonl` (bind-mount; see [`compose.yaml`](../../reference/compose/compose.yaml) `EDEN_LOG_DIR` env + per-service `logs/<service>` volume, and [`eden_service_common/logging.py`](../../reference/services/_common/src/eden_service_common/logging.py)). Records are flat JSON with stable keys (`ts`, `level`, `service`, `experiment_id`, `message`, plus per-record context keys).

That changes the recommended primary log source — see §2 decision 1. The net effect is a **simpler and lower-privilege** design than the issue sketched: the collector tails the JSONL bind-mount read-only and does **not** need the docker socket at all.

**(b) Promtail is EOL; the collector is Alloy.** The issue named Promtail; it reached EOL 2026-03-02. Grafana Alloy is the supported successor (a distribution of the OpenTelemetry Collector that handles logs, metrics, and traces in one binary). Alloy's `local.file_match` + `loki.source.file` + `loki.process` + `loki.write` components reproduce exactly the file-tail-and-ship pipeline Promtail offered, against the same Loki backend. The config language is Alloy's River-like syntax (`.alloy` files) rather than Promtail's YAML; capability-wise it is a strict superset. Because EDEN ships no existing Promtail config, this is a greenfield collector choice, not a migration — `alloy convert --source-format=promtail` is irrelevant here.

## 2. Decisions captured before drafting

These are the load-bearing calls this plan makes. They are defensible defaults, called out explicitly so codex-review and the operator can challenge each one rather than have it buried in a config file. None is treated as settled against a contradicting spec MUST (there are no spec MUSTs in scope).

1. **Collector = Grafana Alloy; primary log source = tail the #109 JSONL bind-mount, NOT a docker-socket discovery.** Alloy mounts `${EDEN_EXPERIMENT_DATA_ROOT}/logs` read-only and tails `**/<service>.jsonl` via `local.file_match` + `loki.source.file`. Rationale:
   - **Supported collector.** Promtail is EOL (§1.1b); Alloy is the current Grafana-blessed log shipper. Choosing Alloy avoids shipping new first-party infra built on a dead binary.
   - **No docker socket.** The issue's `docker_sd_configs` path requires bind-mounting `/var/run/docker.sock` into the collector — the same privilege the `compose.docker-exec.yaml` overlay carefully gates behind its own overlay (AGENTS.md "any deployment mode that needs new privilege … goes in its own compose overlay"). Tailing files needs only read access to a bind-mount. Strictly less privilege for the common case.
   - **Richer labels.** The JSONL is already structured; Alloy's `loki.process` `stage.json` + `stage.labels` promote `service` / `level` / `experiment_id` to Loki labels directly. <!-- rename-discipline:cite --> The docker-stdout path delivers the same JSON wrapped in docker's log envelope and would need re-parsing.
   - **Survives container removal.** The bind-mount is the durable copy that motivated #109; it outlives `compose down`.
   - **Cost:** this source covers only the EDEN services that use `eden_service_common` logging (the 6 services). Postgres + Forgejo log to stdout only and are NOT captured by the JSONL tail. That is acceptable for v0 — the high-signal logs are the EDEN services' (orchestrator decisions etc.). Capturing infra stdout is an **opt-in second overlay** (§3.4, `compose.logging-infra.yaml`) that adds Alloy's `discovery.docker` + `loki.source.docker` and the socket mount — isolated in its own overlay precisely so the default ships without the socket (the AGENTS.md privilege-isolation discipline).

2. **Loki storage location: bind-mount under `${EDEN_EXPERIMENT_DATA_ROOT}/loki/`, classified as DERIVED / non-protocol-owned.** The issue asks for `${EDEN_EXPERIMENT_DATA_ROOT}/loki/`. Honored — but with an explicit classification note, because 12a-1g established that everything under the data root is presumed protocol-owned durable state (chapter 01 §13). Loki's index + chunks are a **queryable projection of the `logs/` bind-mount**, not protocol-owned state: losing it loses search history but no experiment state, and it can be rebuilt by re-ingesting (subject to Alloy's file-tail position tracking — practically, re-ingestion covers logs still present in `logs/`). It is therefore NOT covered by the §13 durability invariant and NOT exercised by the durability smoke. The classification is documented in `compose.logging.yaml`'s volume comment and in the operator doc. (Alternative considered: a named volume, fully ephemeral. Rejected as default because co-locating under the data root lets "search history survives restart" hold and keeps one cleanup path — `rm -rf ${EDEN_EXPERIMENT_DATA_ROOT}` — but the alternative is one line away if an operator prefers strict ephemerality.)

3. **Grafana auth: generated admin password via env; no anonymous access by default.** `setup-experiment.sh` generates `EDEN_GRAFANA_ADMIN_PASSWORD` (same secret-generation path as the other secrets) and passes it as `GF_SECURITY_ADMIN_PASSWORD`. The default `admin/admin` is overridden. Anonymous viewer access (`GF_AUTH_ANONYMOUS_ENABLED=true`) is a documented one-line ergonomics option for pure local demos but is **off by default** so the posture matches the rest of the stack (admin-token-gated surfaces). The demo posture is local-only and not exposed; we still don't ship `admin/admin`.

4. **Frictionless bring-up: no new setup flag.** `setup-experiment.sh` **always** generates `EDEN_GRAFANA_ADMIN_PASSWORD` and **always** creates `${EDEN_EXPERIMENT_DATA_ROOT}/loki/` (chmod 0777, per the 12a-1g multi-uid precedent — Loki runs as uid 10001). Both are negligible when the overlay isn't used (one unused secret line, one empty dir), and this keeps the issue's stated bring-up frictionless: `docker compose -f compose.yaml -f compose.logging.yaml up -d` works against any setup-experiment-bootstrapped stack with no extra flag. (Alternative considered: a `--with-logging` flag gating the dir/secret. Rejected: it adds a setup-time decision for a teardown-cheap artifact and breaks the "just add `-f compose.logging.yaml`" story.)

5. **Ship a path-gated CI smoke (`compose-smoke-logging`), not docs-only.** AGENTS.md is explicit that smokes are the literal pre-push validation gate and that overlay wiring regressions hide from scoped pytest. A `smoke-logging.sh` + a `compose-smoke-logging` CI job gated on the existing `compose` paths-filter bucket validates the overlay end-to-end (Loki ready, Alloy shipping, a LogQL query returns EDEN lines, Grafana healthy + datasource/dashboard provisioned). The issue's "do not require it on lean CI" constraint is honored by the path gate — the job only runs when `reference/compose/**` or `reference/scripts/**` changes, exactly like the other `compose-smoke-*` jobs, and it is added as a non-required status (same posture as `compose-smoke-checkpoint` / `compose-smoke-multi-orchestrator`).

6. **Pin image tags; verify Alloy ↔ Loki API compatibility.** Use explicit tags (`grafana/loki:3.x.y`, `grafana/alloy:vX.Y.Z`, `grafana/grafana:11.x.y`), never `latest`. Alloy pushes to Loki over the stable `/loki/api/v1/push` API, so the lockstep constraint that bound Promtail+Loki versions no longer applies — but confirm the chosen Alloy release targets the Loki 3.x push API at impl time. Exact patch tags chosen at impl time against the then-current stable lines.

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
| `alloy` | `grafana/alloy:vX.Y.Z` | — (internal only) | 12345 | `${EDEN_EXPERIMENT_DATA_ROOT}/logs` (ro bind-mount) + `./logging/alloy-config.alloy` (ro) + `${EDEN_EXPERIMENT_DATA_ROOT}/alloy/` (storage / positions, rw) | pushes to `loki:3100` |
| `grafana` | `grafana/grafana:11.x.y` | `${GRAFANA_HOST_PORT:-3000}:3000` | 3000 | `./logging/grafana/provisioning/**` (ro) + `./logging/grafana/dashboards/**` (ro) | (in-memory sqlite; no durable mount needed) |

Notes:

- **Loki + Alloy are internal-only except Grafana's host port.** Loki is reached by Alloy and Grafana over the compose network (`http://loki:3100`); no host port. Grafana is the only host-exposed surface, at `${GRAFANA_HOST_PORT:-3000}`. Grafana's canonical port is 3000; the override knob handles the rare local collision (and respects [`reference/compose/README.md`](../../reference/compose/README.md)'s "avoid well-known ports" note — the *default* of 3000 is Grafana's well-known port, which operators expect, but it's overridable). Alloy's HTTP server (debug UI + readiness) is on `:12345`; not host-exposed by default (an operator can add a port mapping to inspect Alloy's component graph if debugging).
- **Alloy run command.** Alloy needs an explicit `run` argument: `command: ["run", "/etc/alloy/config.alloy", "--storage.path=/var/lib/alloy/data", "--server.http.listen-addr=0.0.0.0:12345", "--disable-reporting"]`. The `--storage.path` is the rw bind-mount that holds the file-tail positions (Alloy's equivalent of Promtail's `positions.yaml`). **`--disable-reporting` is deliberate:** Alloy enables anonymous usage reporting (an outbound phone-home) by default; a local reference stack should not silently make outbound calls, so the flag is on. Document the choice in the overlay comment.
- **Healthchecks.** `loki`: `GET /ready`. `grafana`: `GET /api/health`. `alloy`: `GET /-/ready` on `:12345`. All `CMD`/argv form, `wget --spider` consistent with the existing forgejo healthcheck pattern (confirm `wget` is present in each image at impl time; fall back to the image's own health convention otherwise).
- **`depends_on`.** `alloy` and `grafana` depend on `loki: service_healthy`. The overlay's services do NOT `depends_on` any base-stack service (the JSONL files Alloy tails are created by setup-experiment + the base services; if a base service hasn't written yet, Alloy simply has no lines to ship — not an error).
- **Grafana durable storage: none.** Grafana's own DB (dashboards, users) is fully reconstructed from provisioning on every start, so it gets no bind-mount. This keeps Grafana stateless and avoids a fourth uid/permission surface. (The pre-provisioned dashboard + datasource are repo-tracked; nothing operator-created needs to survive.)
- **`logging: *eden-logging`.** The three overlay services reuse the base `x-eden-logging` anchor? — No: that anchor is defined in `compose.yaml`; YAML anchors do **not** cross files in a multi-file compose merge. Either inline the same `driver: json-file` / rotation options on each overlay service, or omit (accept docker defaults). Recommend inlining the same 50 MB × 5 rotation for parity. (Tricky-area §8.5.)

### 3.2 Config files (new directory `reference/compose/logging/`)

Repo-tracked static config, bind-mounted read-only into the containers (the same pattern the stack uses for `credential-helper.sh`):

```text
reference/compose/logging/
  loki-config.yaml                                   # Loki: filesystem storage, single-binary, retention
  alloy-config.alloy                                  # Alloy: tail logs/**/*.jsonl, JSON pipeline, push to loki
  grafana/
    provisioning/
      datasources/loki.yaml                          # auto-provision the Loki datasource (default)
      dashboards/dashboards.yaml                      # provider pointing at /var/lib/grafana/dashboards
    dashboards/
      eden-explore.json                               # the pre-provisioned "EDEN explore" dashboard
```

- **`loki-config.yaml`** — single-binary / monolithic mode, `filesystem` object store under `/loki` (the bind-mount), tsdb index. Set a finite `retention_period` (e.g. 168h) + `compactor` retention so a long-lived demo's disk doesn't grow unbounded. `auth_enabled: false` (single-tenant; this is a local demo).
- **`alloy-config.alloy`** — Alloy River-syntax pipeline: `local.file_match "eden"` with `path_targets = [{__path__ = "/var/lib/eden/logs/**/*.jsonl"}]` (the read-only bind-mount path inside the Alloy container) → `loki.source.file "eden"` (tails matched files) → `loki.process "eden"` stages `stage.json` (extract `level`/`service`/`experiment_id`/`ts`), `stage.timestamp` (use the record `ts`), `stage.labels` (promote `service`/`level`/`experiment_id` — all low-cardinality; path-derived `service` fallback if a record lacks the field) → `loki.write "default"` pushing to `http://loki:3100/loki/api/v1/push`. <!-- rename-discipline:cite --> **Glob excludes rotated backups:** match `*.jsonl` only (not `*.jsonl.1`…`.5`) so rotated files aren't re-ingested; Alloy's `--storage.path=/var/lib/alloy/data` (the rw bind-mount) holds file-tail positions across restart. (The `local.file_match` + `loki.source.file` two-component split is the standard Alloy idiom — globbing lives in `local.file_match`, which feeds `targets` into `loki.source.file`; it is not redundant. Confirm against the pinned Alloy version's component reference at impl time.)
- **`grafana/provisioning/datasources/loki.yaml`** — Loki datasource at `http://loki:3100`, `isDefault: true`.
- **`grafana/provisioning/dashboards/dashboards.yaml`** — a file provider loading `/var/lib/grafana/dashboards`.
- **`grafana/dashboards/eden-explore.json`** — a starter dashboard: a logs panel with a LogQL query scoped to EDEN services (`{service=~".+"}` or `{experiment_id="$experiment_id"}`), a `service` template variable (multi-select), a `level` filter, and a time picker. Kept deliberately small (one logs panel + variables); operators extend in Grafana's Explore view.

### 3.3 `setup-experiment.sh` + `.env.example`

- **`.env.example`** — add a "Log search (Loki/Grafana overlay)" section: `EDEN_GRAFANA_ADMIN_PASSWORD=` and `GRAFANA_HOST_PORT=3000` (commented default).
- **`setup-experiment.sh`** — (1) generate `EDEN_GRAFANA_ADMIN_PASSWORD` via the existing secret generator and write it to `.env` (preserved across re-runs via the existing `read_env_key` idempotency, like every other secret); (2) extend the substrate-dir-tree creation to `mkdir -p` + `chmod 0777` two new subdirs: `${DATA_ROOT}/loki/` and `${DATA_ROOT}/alloy/`. Both classified in a comment as derived/observability (non-protocol-owned), distinct from the durable substrate subdirs.

### 3.4 Optional infra-log capture: its own overlay `compose.logging-infra.yaml`

For operators who also want Postgres/Forgejo **stdout** (not covered by the JSONL tail) in the search UI, ship a small second overlay layered on top of `compose.logging.yaml`:

```bash
docker compose -f compose.yaml -f compose.logging.yaml -f compose.logging-infra.yaml --env-file .env up -d --wait
```

It re-declares the `alloy` service to (a) add the `/var/run/docker.sock:/var/run/docker.sock:ro` mount + `group_add: ["${EDEN_LOGGING_DOCKER_GID:?set EDEN_LOGGING_DOCKER_GID to the gid of /var/run/docker.sock as seen from inside a container — see compose.logging-infra.yaml header}"]`, and (b) point Alloy at an extended config (`alloy-config-infra.alloy`) that adds `discovery.docker` + `loki.source.docker` components for the `postgres` / `forgejo` containers alongside the file-tail pipeline. Isolating the socket-requiring path in its own overlay is the AGENTS.md "any deployment mode that needs new privilege goes in its own compose overlay" discipline — the default `compose.logging.yaml` never touches the socket, matching how `compose.docker-exec.yaml` isolates the same privilege.

  **Fail-fast on the gid (round-1 catch).** This overlay deliberately uses a *dedicated* required var `${EDEN_LOGGING_DOCKER_GID:?...}`, NOT the docker-exec overlay's `EDEN_DOCKER_GID`. The reason: `setup-experiment.sh` writes `EDEN_DOCKER_GID=0` by default (it's only probed to the real gid under `--exec-mode docker`), so reusing it would let the infra overlay come up with a plausible-but-wrong gid 0 and then fail at runtime with confusing docker-socket permission errors. A distinct `:?`-guarded var forces the operator to supply the correct probed gid (the one-line probe documented in the overlay header — same probe-from-inside-a-container technique as docker-exec, NOT a host-side `stat`) and fails loud at `compose config`/`up` time if it's missing. Cross-reference the `compose.docker-exec.yaml` socket-trust note.

### 3.5 Docs

Per the issue's "Documentation updates required", plus the natural homes:

- **`docs/observability.md`** — (a) §1 "Process logs" substrate row: cross-link Loki/Grafana as the search interface over the logs substrate. (b) §2.5 ladder table: flip the **L3** row from "Tracked in #110" to "In place" and link the overlay. (c) Elevate the bring-your-own framing: add a new first-party subsection (§2.7 "Log search UI (Loki + Alloy + Grafana)") covering the bring-up command, Grafana URL (`localhost:3000`), credentials (`admin` / `EDEN_GRAFANA_ADMIN_PASSWORD`), the pre-provisioned dashboard, an example LogQL query, and the optional `compose.logging-infra.yaml` extension. (Issue says "promote from §3 bring-your-own to §2"; today there is no Loki/Grafana entry in §3 — there's only Adminer/Swagger/desktop clients — so this is a net-new §2 subsection rather than a move. Note in the PR.) <!-- rename-discipline:cite -->
- **`docs/user-guide.md`** — §1 components table: add a **grafana** row pointing at `localhost:3000` ("Log search UI; opt-in via `-f compose.logging.yaml`"). §9 already points at `observability.md`; verify the pointer still resolves after the §2 additions (no content change beyond that).
- **`reference/compose/README.md`** — add a "Log search overlay (`compose.logging.yaml`)" subsection alongside the other overlay docs (note the optional `compose.logging-infra.yaml` socket overlay too); note the derived (non-durable) classification of `loki/` + `alloy/` so the storage-layout durable/ephemeral table stays accurate.
- **`AGENTS.md`** — "Commands" table: add the `docker compose -f compose.yaml -f compose.logging.yaml up -d` bring-up and the `bash reference/compose/healthcheck/smoke-logging.sh` smoke.
- **`CHANGELOG.md` [Unreleased]** + **`docs/roadmap.md`** — chunk-completion entry + planless/issue-driven one-liner pointing at this plan, per the AGENTS.md chunk-completion discipline (this is the impl-stage docs obligation; the plan-stage record is the codex transcript committed under `docs/plans/review/issue-110/plan/`).

## 4. Scope

**In scope:**

- New overlay `reference/compose/compose.logging.yaml` (3 services: loki, alloy, grafana).
- New config dir `reference/compose/logging/` (loki config + Alloy `.alloy` config; Grafana datasource/dashboard provisioning + one starter dashboard).
- Optional second overlay `reference/compose/compose.logging-infra.yaml` + `logging/alloy-config-infra.alloy` (socket-gated postgres/forgejo stdout capture; §3.4), with a `${EDEN_LOGGING_DOCKER_GID:?}` fail-fast contract.
- `reference/compose/.env.example` — `EDEN_GRAFANA_ADMIN_PASSWORD` + `GRAFANA_HOST_PORT` (+ a commented `EDEN_LOGGING_DOCKER_GID` with the one-line probe, documented as required only for the infra overlay).
- `reference/scripts/setup-experiment/setup-experiment.sh` — generate the Grafana password (idempotent); create + chmod the `loki/` and `alloy/` subdirs.
- New `reference/compose/healthcheck/smoke-logging.sh` + `compose-smoke-logging` CI job (path-gated, non-required).
- Docs: `observability.md`, `user-guide.md`, `reference/compose/README.md`, `AGENTS.md`, `CHANGELOG.md`, `docs/roadmap.md`.

**Out of scope (deferred / non-goals):**

- **External streaming** (CloudWatch / Datadog / Grafana Cloud) — L4; production-grade, separate concern. Tracked as the existing L4 ladder rung; file a follow-up issue only if an operator asks for it.
- **Per-experiment label namespacing in Loki.** Single-experiment Compose deployments don't need it; the `experiment_id` label already disambiguates. Revisit if multi-experiment shared deployments land (cross-link the multi-experiment control-plane work). → file follow-up issue if not already covered.
- **Infra (Postgres/Forgejo) stdout capture by default** — shipped only as the opt-in `compose.logging-infra.yaml` overlay (§3.4).
- **Grafana alerting / Loki rules** — search UI only; no alert routing.
- **Helm/k8s equivalent** (a Loki/Grafana chart for the Phase 13 substrate) — Compose-only here. → file follow-up issue.
- **EDEN service code changes** — none. The JSONL logs (#109) are the contract Alloy consumes; this chunk does not touch logging.py or any service.

Each deferral above with a "→ file follow-up issue" marker MUST land a GitHub issue at impl time and be referenced in the CHANGELOG entry, per the AGENTS.md deferral-tracking rule.

## 5. Spec / contract impact

**None.** This is entirely a deployment-binding addition.

- **Spec chapters:** no amendments. Observability tooling attached to a deployment is not protocol surface.
- **JSON Schema:** no changes.
- **Pydantic models:** no changes.
- **Wire bindings:** no changes. Loki/Grafana speak their own HTTP APIs; nothing routes through the EDEN wire protocol.
- **`eden_service_common/logging.py`:** unchanged — its JSONL output format is the de-facto input contract for the Alloy pipeline. (Risk note: this is now an *implicit* contract; see §10. Alloy's `stage.json` degrades gracefully if a field is missing — it just doesn't get that label — so a logging-schema change can't break ingestion, only label richness.)

## 6. Naming map

No renames of existing identifiers. New identifiers introduced (validated against [`docs/glossary.md`](../glossary.md) — these are deployment-infra names, not EDEN protocol vocabulary, so the role/verb/kind patterns don't apply; the discipline here is consistency with existing Compose naming):

| New identifier | Kind | Convention followed |
|---|---|---|
| `compose.logging.yaml` / `compose.logging-infra.yaml` | overlay files | `compose.<concern>.yaml` (matches `compose.subprocess.yaml`, `compose.docker-exec.yaml`, `compose.control-plane.yaml`, `compose.multi-orchestrator.yaml`) |
| `loki` / `alloy` / `grafana` | compose service names | upstream tool names, lowercase (matches `forgejo`, `postgres`) |
| `EDEN_GRAFANA_ADMIN_PASSWORD` | env var (secret) | `EDEN_<THING>_<ROLE>` (matches `EDEN_READONLY_PASSWORD`, `EDEN_ADMIN_TOKEN`, `EDEN_SESSION_SECRET`) |
| `EDEN_LOGGING_DOCKER_GID` | env var (infra-overlay required) | `EDEN_<CONCERN>_<THING>` — distinct from docker-exec's `EDEN_DOCKER_GID` so the infra overlay fails fast instead of inheriting the default `0` (§3.4) |
| `GRAFANA_HOST_PORT` | env var (port knob) | `<SERVICE>_HOST_PORT` (matches `FORGEJO_HOST_PORT`, `TASK_STORE_HOST_PORT`, `POSTGRES_HOST_PORT`, `WEB_UI_HOST_PORT`) |
| `${EDEN_EXPERIMENT_DATA_ROOT}/loki/`, `/alloy/` | data-root subdirs | `<DATA_ROOT>/<substrate>/` (matches `postgres/`, `forgejo/`, `artifacts/`) — but classified DERIVED, not durable |
| `smoke-logging.sh` | healthcheck script | `smoke-<concern>.sh` (matches `smoke-subprocess.sh`, `smoke-checkpoint.sh`, `smoke-multi-orchestrator.sh`) |
| `compose-smoke-logging` | CI job | `compose-smoke-<concern>` (matches `compose-smoke-subprocess-docker`, `compose-smoke-checkpoint`) |
| `eden-explore` (dashboard) | Grafana dashboard uid/title | new; `eden-<purpose>` |

The `rename-discipline` CI job scans nothing new here (no legacy-vocab patterns), but it gates the chunk because `reference/compose/**` is in its trigger set.

## 7. Migration / cleanup

Nothing to retire. This is purely additive — a new overlay that did not exist before. No backwards-compat shims (consistent with the CLAUDE.md pre-external-user posture, and irrelevant here since nothing is being replaced).

One cleanup consideration: `${EDEN_EXPERIMENT_DATA_ROOT}/loki/` and `/alloy/` are created by `setup-experiment.sh` for **every** experiment (decision §2.4), even ones that never run the overlay. They stay empty until the overlay runs. The existing `rm -rf ${EDEN_EXPERIMENT_DATA_ROOT}` reset already removes them; no separate cleanup path. The smoke's existing root-owned-file cleanup-via-sibling-container pattern (loki writes as uid 10001) is reused — see §9.

## 8. Conformance impact

**None.** Per [`spec/v0/09-conformance.md`](../../spec/v0/09-conformance.md) §6, the chapter-7 HTTP binding is the only IUT contract a conformance harness can rely on. Loki/Alloy/Grafana are off-wire deployment tooling — the suite cannot and should not observe them. No §-references change, no scenarios added, no `CONFORMANCE_GROUP` declarations. `check_citations.py` is unaffected. (This is the AGENTS.md "filter MUSTs through the IUT contract" pitfall applied trivially: there are no MUSTs here, and nothing is wire-observable.)

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

**`compose.logging-infra.yaml` validation (round-1 catch).** The privileged infra overlay is a shipped deliverable, so it MUST have a catch point — otherwise its `group_add` / socket-mount / alternate-config wiring drifts silently. Two-tier:

- **Always (no socket needed):** a static merge gate `EDEN_LOGGING_DOCKER_GID=0 docker compose -f compose.yaml -f compose.logging.yaml -f compose.logging-infra.yaml --env-file .env config >/dev/null` — proves the three-file merge resolves and the `:?`-guarded gid var is wired (a dummy gid is fine for `config`, which doesn't start containers). Add this line to `smoke-logging.sh` (it runs even on socket-less CI).
- **When a docker socket is reachable (local; not lean CI):** the `smoke-logging.sh` ending extends to optionally probe the real gid, layer the infra overlay, and assert a postgres/forgejo log line reaches Loki. Gate this tail behind a `docker info` socket-reachability check so it no-ops cleanly where the socket isn't available.

**Validation gate:**

```bash
bash reference/compose/healthcheck/smoke-logging.sh   # exits 0; includes the infra-overlay static merge gate
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

- **Collector lifecycle (the round-0 catch).** Promtail is EOL (2026-03-02); this plan uses Alloy instead (§1.1b). Watch that Alloy itself stays current at impl time, and that the pinned Alloy release still targets the Loki 3.x `/loki/api/v1/push` API (§2.6).
- **Alloy config-language learning curve.** Alloy's River-like `.alloy` syntax differs from Promtail's YAML; the `local.file_match` → `loki.source.file` → `loki.process` → `loki.write` component graph must be authored + validated against a live bring-up (Alloy's `alloy fmt` / the debug UI on `:12345` help). This is the dominant impl-time iteration cost (§12) — budget a live loop, don't hand-write the config blind.
- **Implicit log-schema contract.** Alloy's `stage.json`/`stage.labels` depend on the `eden_service_common/logging.py` field names (`ts`, `level`, `service`, `experiment_id`). A future logging-schema change degrades labels **silently** (the `stage.json` just drops missing fields; ingestion still works, search by `service`/`level` quietly stops). Mitigation: the `smoke-logging.sh` LogQL assertion queries by `{service="orchestrator"}`, so a label regression fails the smoke. Note the coupling in `logging/alloy-config.alloy`'s header comment so a logging.py editor sees it.
- **Port 3000 collision on the operator's machine.** Common (lots of dev tools default to 3000). The `${GRAFANA_HOST_PORT:-3000}` knob is the escape hatch; document it. Loki/Alloy aren't host-exposed so they can't collide.
- **`chmod 0777` on `loki/` + `alloy/`.** Same security pushback surface as 12a-1g's substrate dirs; same justification (local-dev multi-uid portability, Loki runs as uid 10001). Hold the 0777 line per the 12a-1g precedent; Phase 13 substrates are the production path.
- **Loki disk growth on a long-lived demo.** Unbounded ingestion fills `${DATA_ROOT}/loki/`. Mitigation: finite `retention_period` + compactor retention in `loki-config.yaml`. Document the knob.
- **Grafana provisioning fails silently.** A malformed datasource/dashboard YAML leaves Grafana up but without the datasource — `/api/health` still returns ok. Mitigation: the smoke asserts the datasource is present via `/api/datasources` and the dashboard via `/api/dashboards`, not just `/api/health`.
- **YAML anchor scope across compose files.** `*eden-logging` is defined in `compose.yaml` and is NOT visible in `compose.logging.yaml` (anchors don't cross the multi-file merge). Inline the rotation options on the overlay services; a bare `*eden-logging` reference in the overlay would fail to parse (§3.1 / §8.5 trap).
- **`compose -f compose.yaml -f compose.logging.yaml config` interaction with other overlays.** The logging overlay must compose with subprocess/docker-exec/control-plane sets. Since it adds only new services and edits none, the merge is additive and safe — but the smoke deliberately layers it on top of the subprocess overlay (which is what produces real per-task log volume) to prove the combination.
- **`smoke-logging.sh` runtime + CI cost.** Pulls ~400 MB of loki/grafana/alloy images + brings up the full stack. The path gate keeps it off lean CI (only runs on `reference/compose/**` / `reference/scripts/**` changes); `timeout-minutes: 20` matches the other compose smokes. Non-required status until it's stayed green on main ~2 weeks (same posture as `compose-smoke-checkpoint`).
- **Alloy positions across container restart.** Tailing files (not docker_sd) means Alloy must persist its file-tail offsets (`--storage.path`) to avoid re-ingesting from the top on restart. That's why `${DATA_ROOT}/alloy/` is a rw bind-mount, not in-container scratch. If it were lost, restart would double-ingest already-shipped lines (duplicates in Loki, not data loss) — acceptable but the bind-mount avoids it.
- **bash 3.2 in `smoke-logging.sh`.** Per AGENTS.md, no `mapfile`/`declare -A`; reuse the existing smoke scripts' `while read` + parallel-array idioms.

## 11. Sequence within the chunk

1. Rebase impl branch against `origin/main` at impl-start.
2. Wave 1 (overlay + config + setup-experiment) → gate.
3. Wave 2 (smoke + CI job) → gate.
4. Wave 3 (docs + completion records + deferral issues) → full gate.
5. Codex-review to convergence (plan PR caught plan-level concerns; impl PR catches config correctness — Loki/Alloy config validity, provisioning paths, the JSONL-vs-socket decision, the derived-storage classification).
6. Open impl PR; body covers: overlay shape, the #109-driven JSONL-tail decision (vs the issue's docker_sd sketch), the derived-storage classification, the optional infra extension, deferral issue links, test-plan checklist, codex round count. Use the PR template; the "Fresh-operator walkthrough" section is required (this changes operator-facing surfaces: a new UI + bring-up command).

## 12. Estimated effort

| Activity | Estimate |
|---|---|
| `compose.logging.yaml` + `logging/` config tree (loki/alloy/grafana) | ~0.5 day |
| `setup-experiment.sh` + `.env.example` (password gen + dir creation) | ~0.25 day |
| `smoke-logging.sh` + `compose-smoke-logging` CI job | ~0.5 day |
| Docs (observability.md / user-guide / README / AGENTS / CHANGELOG / roadmap) + deferral issues | ~0.5 day |
| Validation gates + local bring-up iteration (config-tuning is the variable) | ~0.5 day |
| Codex-review iterations (plan + impl) | ~0.75 day |
| **Total** | **~3 days** |

The dominant variable is Loki/Alloy config tuning — the Alloy component graph + label promotion + retention need a live bring-up to validate, and Grafana provisioning paths are finicky. <!-- rename-discipline:cite --> Image version selection (§2.6) should be locked early.
